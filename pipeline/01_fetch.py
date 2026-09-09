"""Stage 1: Fetch, score with learned weights, sort, and cap at max_articles.

No articles are filtered out by score. Everything is ranked — highest relevance
at the top, unmatched articles at the bottom. Cap at max_articles (default 200).

Keyword weights are updated from selection history in small steps.
Theme frequency is tracked to surface rising topics in the review UI.
"""

import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from dateutil import parser as dateparser

BASE = Path(__file__).parent
SETTINGS = json.loads((BASE / "settings.json").read_text())
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

SEEN_PATH = DATA / "seen_urls.json"
OUT_PATH = DATA / "articles.json"
THEME_PATH = DATA / "theme_counts.json"
WEIGHTS_PATH = DATA / "weights.json"
SELECTION_LOG_PATH = DATA / "selection_log.json"
QUEUE_PATH = DATA / "queue.json"

MAX_ARTICLES = SETTINGS["pipeline"]["max_articles"]
FRESHNESS_H = SETTINGS["pipeline"]["freshness_hours"]

# How long an UNREVIEWED article stays on the review list after Joe first
# saw it. Distinct from a feed's freshness_hours, which only governs how old
# an item may be when first fetched. Before this existed the list sat
# permanently at the cap ("collecting and collecting"). 0 = clear the
# unreviewed list on every fetch. Reviewed items live in queue.json and are
# never touched by this.
REVIEW_TTL_H = SETTINGS["pipeline"].get("review_ttl_hours", 72)

# Must exceed the longest per-feed freshness window, or an item could be
# forgotten while still inside its window and get re-fetched as a duplicate.
SEEN_RETENTION_DAYS = 30

HF_PAPERS_API = "https://huggingface.co/api/daily_papers"

# Weight learning — conservative by design
MIN_TOTAL_SELECTIONS = 10       # don't adjust until we have this many total selections
MIN_KEYWORD_APPEARANCES = 5     # don't adjust a keyword until it's appeared this many times
WEIGHT_STEP = 0.05              # max change per keyword per run
WEIGHT_MAX = 2.0
WEIGHT_MIN = 0.2
DRAFT_RATE_THRESHOLD = 0.6      # 60%+ draft rate → bump weight up
SKIP_RATE_THRESHOLD = 0.6       # 60%+ skip rate → bump weight down

# Rising theme detection
THEME_WINDOW_DAYS = 7
THEME_RISE_COUNT = 3            # appearances in 7 days to qualify as "rising"


def load_seen():
    if not SEEN_PATH.exists():
        return {}
    entries = json.loads(SEEN_PATH.read_text())
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    return {url: ts for url, ts in entries.items()
            if datetime.fromisoformat(ts) > cutoff}


def save_seen(seen):
    SEEN_PATH.write_text(json.dumps(seen, indent=2))


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(date_str):
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = dateparser.parse(date_str)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat() if dt else datetime.now(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def get_content(entry):
    if hasattr(entry, "content") and entry.content:
        return strip_html(entry.content[0].get("value", ""))
    for attr in ("summary", "description"):
        val = entry.get(attr)
        if val:
            return strip_html(val)
    return ""


def fetch_hf_papers(conf):
    """Hugging Face Daily Papers — community-curated, with upvote counts.

    Not an RSS feed, so it gets its own adapter. Upvotes are the quality
    signal: raw arXiv is ~800 papers a day and would drown everything, while
    this is the subset the ML community actually surfaced and voted on.
    """
    resp = requests.get(HF_PAPERS_API, timeout=30,
                        headers={"User-Agent": "rebel-intel/1.0"})
    resp.raise_for_status()
    min_upvotes = conf.get("min_upvotes", 0)
    out = []
    for item in resp.json():
        paper = item.get("paper") or {}
        paper_id = paper.get("id")
        if not paper_id:
            continue
        upvotes = paper.get("upvotes") or 0
        if upvotes < min_upvotes:
            continue
        title = strip_html(paper.get("title") or item.get("title") or "")
        # ai_summary is a plain-language summary; the raw abstract is the
        # fallback. Either beats the title alone for embedding and ranking.
        snippet = strip_html(paper.get("ai_summary") or paper.get("summary") or "")
        published = parse_date(paper.get("publishedAt") or item.get("publishedAt") or "")
        if not title:
            continue
        out.append({
            "title": title,
            "url": f"https://huggingface.co/papers/{paper_id}",
            "published": published,
            "snippet": snippet[:400],
            "upvotes": upvotes,
        })
    out.sort(key=lambda x: -x["upvotes"])
    return out


# Per-run record of what each source actually returned, so a feed that quietly
# dies is visible instead of just contributing nothing forever. Both the
# hnrss.org feed and every arXiv query had been returning zero for an unknown
# length of time before this existed.
FEED_STATUS = {}


def fetch_rss(conf):
    name = conf.get("name", conf["url"])
    # Use feedparser's own User-Agent by default. Overriding it globally made
    # 17 of 48 feeds start returning 301/307/404 — plenty of publishers treat
    # an unknown UA differently. Only set `user_agent` on feeds that need one
    # (Reddit does).
    headers = {}
    if conf.get("user_agent"):
        headers["User-Agent"] = conf["user_agent"]
    feed = feedparser.parse(conf["url"], request_headers=headers or None)
    status = getattr(feed, "status", None)
    # feedparser follows redirects and still parses the result, so a 301/302
    # that yields entries is perfectly healthy — only an empty result is a
    # real failure. Reporting on status alone flagged 17 working feeds.
    if not feed.entries:
        hint = {
            301: "moved and no longer serves a feed — find the new URL",
            302: "redirect leads nowhere — find the new URL",
            307: "redirect leads nowhere — find the new URL",
            400: "bad request — the query URL is malformed",
            401: "auth required",
            403: "blocked (needs a User-Agent, or login)",
            404: "gone — find a new feed URL",
            429: "rate limited — fetch it less often",
        }.get(status, "returned no entries")
        FEED_STATUS[name] = f"HTTP {status}: {hint}" if status else hint
        print(f"  Warning: {name} — {FEED_STATUS[name]}")
    elif status and status not in (200, 301, 302, 307):
        # Parsed fine, but worth noting.
        print(f"  Note: {name} returned HTTP {status} but parsed {len(feed.entries)} entries")
    out = []
    for entry in feed.entries:
        title = strip_html(entry.get("title", ""))
        url = entry.get("link", "")
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "published": parse_date(entry.get("published", "") or entry.get("updated", "")),
            "snippet": get_content(entry)[:400],
            "upvotes": None,
        })
    return out


DISCORD_API = "https://discord.com/api/v10"
DISCORD_CONFIG_PATH = BASE / "discord_config.json"

# Links worth turning into a reviewable item. Everything else in a message is
# chatter — the point is the papers people share, not the conversation.
PAPER_LINK_RE = re.compile(
    r"https?://(?:www\.)?("
    r"arxiv\.org/(?:abs|pdf)/[\w.\-/]+"
    r"|huggingface\.co/papers/[\w.\-/]+"
    r"|openreview\.net/forum\?id=[\w.\-]+"
    r"|aclanthology\.org/[\w.\-/]+"
    r")",
    re.I,
)
GENERIC_LINK_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.I)


def load_discord_config():
    """Bot token lives in its own gitignored file, the same pattern
    notify_config.json already uses for the Gmail app password.

    Returns None (not an error) when unconfigured, so the daily job runs
    normally on a box with no Discord token.
    """
    if not DISCORD_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(DISCORD_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  Warning: discord_config.json unreadable -- {e}")
        return None
    token = (cfg.get("bot_token") or "").strip()
    if not token or token.startswith("PASTE_"):
        return None
    return cfg


def _resolve_paper_title(url):
    """Best-effort real title/abstract for a shared paper link.

    A chat message makes a poor title for ranking, and the embedding stage
    works far better on an actual paper title. Failure is fine — the caller
    falls back to the message text.
    """
    try:
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-]+?)(?:v\d+)?(?:\.pdf)?$", url, re.I)
        if m:
            # https, not http — export.arxiv.org 301s plain http and feedparser
            # then returns nothing, the same bug that killed the feed queries.
            feed = feedparser.parse(
                f"https://export.arxiv.org/api/query?id_list={m.group(1)}&max_results=1")
            # arXiv asks for ~3s between API calls and 429s a burst after
            # about two. A failed resolve here means no categories, which
            # means the non-AI filter cannot fire — so politeness is what
            # makes the filter work at all.
            time.sleep(3)
            if feed.entries:
                entry = feed.entries[0]
                cats = [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]
                return (strip_html(entry.get("title", "")),
                        strip_html(entry.get("summary", ""))[:400],
                        cats)
        m = re.search(r"huggingface\.co/papers/([\w.\-]+)", url, re.I)
        if m:
            resp = requests.get(f"https://huggingface.co/api/papers/{m.group(1)}",
                                timeout=20, headers={"User-Agent": "rebel-intel/1.0"})
            if resp.ok:
                data = resp.json()
                # HF Daily Papers is ML by construction; no category to check.
                return (strip_html(data.get("title", "")),
                        strip_html(data.get("ai_summary") or data.get("summary") or "")[:400],
                        ["cs.LG"])
    except Exception:
        pass
    return None, None, []


# arXiv categories that count as "AI" for the purpose of a paper feed. The HN
# paper query matches ANY arxiv.org link, which pulled in "Higher multipoles of
# the cow" (astrophysics, 116 points) and "Longest straight line paths on
# Earth" (207 points) — great HN stories, useless for a LinkedIn AI post.
ARXIV_AI_CATEGORIES = {
    "cs.AI", "cs.CL", "cs.LG", "cs.SE", "cs.MA", "cs.IR", "cs.DC", "cs.CR",
    "cs.HC", "cs.NE", "cs.PF", "cs.DB", "cs.CY", "cs.RO", "stat.ML",
}


def fetch_discord(conf):
    """Harvest paper links shared in Discord channels the bot can read.

    Each shared link becomes one reviewable item rather than each message —
    the goal is the papers people surface, not the surrounding chat. Reaction
    counts are the quality signal, the same role upvotes play for HF papers.

    Requires a bot token in discord_config.json AND the bot to be a member of
    the server with View Channel + Read Message History. Note a bot can only
    be added to a server by someone holding Manage Server there, so this works
    for servers Joe controls (including ones mirroring another server's
    announcement channels via Discord's Channel Following feature).
    """
    cfg = load_discord_config()
    if not cfg:
        print("  Discord: not configured (no token in discord_config.json) -- skipping")
        return []

    channels = conf.get("channels") or cfg.get("channels") or []
    if not channels:
        print("  Discord: no channels configured -- skipping")
        return []

    per_channel = conf.get("per_channel_limit", 100)
    papers_only = conf.get("papers_only", True)
    min_reactions = conf.get("min_reactions", 0)
    headers = {
        "Authorization": f"Bot {cfg['bot_token']}",
        "User-Agent": "rebel-intel/1.0",
    }

    found = {}
    for channel in channels:
        is_obj = isinstance(channel, dict)
        channel_id = channel["id"] if is_obj else channel
        channel_name = channel.get("name", str(channel_id)) if is_obj else str(channel_id)
        try:
            resp = requests.get(f"{DISCORD_API}/channels/{channel_id}/messages",
                                headers=headers, params={"limit": per_channel}, timeout=30)
            if resp.status_code == 401:
                print("  Discord: token rejected (401) -- check bot_token")
                return []
            if resp.status_code == 403:
                print(f"  Discord: no access to #{channel_name} (403) -- bot needs "
                      "View Channel + Read Message History on that channel")
                continue
            if resp.status_code == 404:
                print(f"  Discord: channel #{channel_name} not found (404)")
                continue
            resp.raise_for_status()
            messages = resp.json()
        except Exception as e:
            print(f"  Discord: #{channel_name} failed -- {e}")
            continue

        for msg in messages:
            content = msg.get("content") or ""
            # A bare URL usually arrives as an embed rather than message text.
            for embed in msg.get("embeds") or []:
                for key in ("url", "title", "description"):
                    if embed.get(key):
                        content += " " + str(embed[key])

            urls = [m.group(0) for m in PAPER_LINK_RE.finditer(content)]
            if not urls and not papers_only:
                urls = [m.group(0) for m in GENERIC_LINK_RE.finditer(content)][:1]
            if not urls:
                continue

            reactions = sum((r.get("count") or 0) for r in (msg.get("reactions") or []))
            if reactions < min_reactions:
                continue

            author = ((msg.get("author") or {}).get("global_name")
                      or (msg.get("author") or {}).get("username") or "someone")

            for url in urls[:2]:
                url = url.rstrip(".,);]")
                if url in found:
                    # Same paper shared twice — keep the more-reacted mention.
                    if reactions > (found[url].get("upvotes") or 0):
                        found[url]["upvotes"] = reactions
                    continue

                title, abstract, _cats = _resolve_paper_title(url)
                body = strip_html(content)
                if not title:
                    title = body[:110] or url
                if abstract and body:
                    snippet = f"Shared by {author}: {body[:150]} — {abstract}"
                else:
                    snippet = abstract or body
                found[url] = {
                    "title": title,
                    "url": url,
                    "published": parse_date(msg.get("timestamp") or ""),
                    "snippet": snippet[:400],
                    "upvotes": reactions,
                }

    out = sorted(found.values(), key=lambda x: -(x["upvotes"] or 0))
    print(f"  Discord: {len(out)} shared link(s) from {len(channels)} channel(s)")
    return out


HN_SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"


def fetch_hn(conf, seen=None):
    """Hacker News via the Algolia API.

    `seen` lets this skip URLs the main loop would discard anyway. That
    matters because resolving a paper link costs an arXiv call plus a 3s
    courtesy delay: on 2026-09-05 the feed resolved 25 already-seen links
    (~75s) to keep exactly one new paper.

    Replaces hnrss.org, which is dead — it returns no entries at all, so the
    "Hacker News - Best" feed had been silently contributing nothing.

    Doubles as a paper source: pointing `query` at arxiv.org surfaces papers
    that working engineers upvoted, which is a different and usually earlier
    population than the one voting on Hugging Face Daily Papers.
    """
    params = {
        "tags": "story",
        "hitsPerPage": conf.get("hits", 50),
        "numericFilters": f"points>{conf.get('min_points', 20)}",
    }
    if conf.get("query"):
        params["query"] = conf["query"]
    try:
        resp = requests.get(HN_SEARCH_API, params=params, timeout=30,
                            headers={"User-Agent": "rebel-intel/1.0"})
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as e:
        print(f"  HN: request failed -- {e}")
        return []

    resolve = conf.get("resolve_papers", False)
    seen = seen or {}
    max_items = conf.get("max_items")
    out = []
    for h in hits:
        if max_items is not None and len(out) >= max_items:
            # Enough kept; every further candidate would be a wasted resolve.
            break
        url = h.get("url") or ""
        title = strip_html(h.get("title") or "")
        if not title:
            continue
        if not url:
            # Ask HN / self-post — link to the discussion itself.
            url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        if url in seen:
            continue
        snippet = strip_html(h.get("story_text") or "")
        if resolve:
            # This is a paper feed: only actual paper links belong in it. An
            # HN post pointing at an arXiv *listing* page ("ArXiv has almost
            # 600 submissions today") is a story about arXiv, not a paper.
            if not PAPER_LINK_RE.search(url):
                continue
            # A bare paper title carries little signal; the abstract ranks far
            # better. Same resolver the Discord adapter uses.
            paper_title, abstract, cats = _resolve_paper_title(url)
            if cats and not (set(cats) & ARXIV_AI_CATEGORIES):
                # Resolved fine, but it is astrophysics or maths — skip.
                continue
            if paper_title:
                title = paper_title
            if abstract:
                snippet = abstract
        if not snippet:
            snippet = f"{title} — {h.get('points', 0)} points, {h.get('num_comments', 0)} comments on Hacker News"
        out.append({
            "title": title,
            "url": url,
            "published": parse_date(h.get("created_at") or ""),
            "snippet": snippet[:400],
            "upvotes": h.get("points") or 0,
        })
    return out


def fetch_source(conf, seen=None):
    """Every source normalises to the same shape, so the main loop does not
    care whether an item came from RSS, an Atom feed, a JSON API or Discord."""
    source = conf.get("source")
    if source == "hf_papers":
        return fetch_hf_papers(conf)
    if source == "discord":
        return fetch_discord(conf)
    if source == "hn":
        return fetch_hn(conf, seen)
    return fetch_rss(conf)


def sync_weights(pillars):
    """Load weights.json, adding any new keywords at 1.0."""
    weights = {}
    if WEIGHTS_PATH.exists():
        weights = json.loads(WEIGHTS_PATH.read_text())
    added = []
    for pillar_data in pillars.values():
        for kw in pillar_data.get("keywords", []):
            key = kw.lower()
            if key not in weights:
                weights[key] = 1.0
                added.append(key)
    if added:
        WEIGHTS_PATH.write_text(json.dumps(weights, indent=2, sort_keys=True))
        print(f"  Added {len(added)} new keyword(s) to weights.json at 1.0")
    return weights


def update_weights(weights):
    """Adjust keyword weights based on draft/skip history. Small steps only.

    FROZEN by default since 2026-09-09. After 86 verdicts this had pinned
    exactly four generic keywords at the 2.0 ceiling — openai, anthropic,
    llm, ai model — i.e. it had learned "Joe likes articles about OpenAI",
    which distorted the fetch cap without helping. The ranker (07_rank.py)
    learns from picks properly; 09_learn.py adjusts the search. Re-enable
    with settings.pipeline.keyword_learning = true if ever wanted.
    """
    if not SETTINGS["pipeline"].get("keyword_learning", False):
        pinned = {k: v for k, v in weights.items() if abs(v - 1.0) > 1e-9}
        if pinned:
            for k in pinned:
                weights[k] = 1.0
            WEIGHTS_PATH.write_text(json.dumps(weights, indent=2, sort_keys=True))
            print(f"  Keyword weight learning is frozen; reset {len(pinned)} drifted weight(s) to 1.0: "
                  + ", ".join(sorted(pinned)))
        else:
            print("  Keyword weight learning: frozen (the ranker learns from picks instead).")
        return weights
    if not SELECTION_LOG_PATH.exists():
        return weights
    log = json.loads(SELECTION_LOG_PATH.read_text())
    if len(log) < MIN_TOTAL_SELECTIONS:
        remaining = MIN_TOTAL_SELECTIONS - len(log)
        print(f"  Weight learning: need {remaining} more selection(s) before adjustments kick in.")
        return weights

    draft_counts = {}
    skip_counts = {}
    for entry in log:
        action = entry.get("action", "")
        for kw in entry.get("keywords_matched", []):
            key = kw.lower()
            if action == "draft":
                draft_counts[key] = draft_counts.get(key, 0) + 1
            elif action == "skip":
                skip_counts[key] = skip_counts.get(key, 0) + 1

    adjustments = []
    for key in list(weights.keys()):
        d = draft_counts.get(key, 0)
        s = skip_counts.get(key, 0)
        total = d + s
        if total < MIN_KEYWORD_APPEARANCES:
            continue
        old = weights[key]
        if d / total >= DRAFT_RATE_THRESHOLD:
            weights[key] = min(old + WEIGHT_STEP, WEIGHT_MAX)
        elif s / total >= SKIP_RATE_THRESHOLD:
            weights[key] = max(old - WEIGHT_STEP, WEIGHT_MIN)
        if weights[key] != old:
            adjustments.append((key, old, weights[key]))

    if adjustments:
        WEIGHTS_PATH.write_text(json.dumps(weights, indent=2, sort_keys=True))
        print(f"  Weight adjustments this run ({len(adjustments)}):")
        for key, old, new in adjustments:
            arrow = "up" if new > old else "down"
            print(f"    {arrow}  {key}: {old:.2f} -> {new:.2f}")
    else:
        print("  No weight adjustments this run.")

    return weights


def score_article(text, pillars, weights):
    text_lower = text.lower()
    best_pillar = ""
    best_score = 0.0
    best_keywords = []

    for pillar_data in pillars.values():
        matched = []
        pillar_score = 0.0
        for kw in pillar_data.get("keywords", []):
            if kw.lower() in text_lower:
                matched.append(kw)
                pillar_score += 15 * weights.get(kw.lower(), 1.0)
        if matched:
            pillar_score += min(len(matched) * 5, 25)  # bonus for multiple matches
            pillar_score = min(pillar_score, 100)
            if pillar_score > best_score:
                best_score = pillar_score
                best_pillar = pillar_data["name"]
                best_keywords = matched

    return best_pillar, round(best_score, 1), best_keywords


def record_themes(keywords):
    """Track keyword appearances by date for rising theme detection."""
    if not keywords:
        return
    counts = {}
    if THEME_PATH.exists():
        counts = json.loads(THEME_PATH.read_text())
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    for kw in keywords:
        key = kw.lower()
        counts.setdefault(key, []).append(today)
        counts[key] = [d for d in counts[key] if d >= cutoff]
    THEME_PATH.write_text(json.dumps(counts, indent=2))


def get_rising_themes():
    if not THEME_PATH.exists():
        return []
    counts = json.loads(THEME_PATH.read_text())
    cutoff = (date.today() - timedelta(days=THEME_WINDOW_DAYS)).isoformat()
    rising = []
    for kw, dates in counts.items():
        recent = [d for d in dates if d >= cutoff]
        if len(recent) >= THEME_RISE_COUNT:
            rising.append((kw, len(recent)))
    return sorted(rising, key=lambda x: -x[1])


def main():
    radar_path = Path(SETTINGS["content_radar_config"])
    if not radar_path.exists():
        print(f"ERROR: content-radar config not found at {radar_path}")
        sys.exit(1)

    with open(radar_path) as f:
        radar = yaml.safe_load(f)

    pillars = radar["pillars"]
    feeds = radar["feeds"]

    print("Syncing keyword weights...")
    weights = sync_weights(pillars)
    weights = update_weights(weights)

    seen = load_seen()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_H)
    articles = []
    total_scanned = 0

    print(f"\nFetching {len(feeds)} feeds...")

    now = datetime.now(timezone.utc)
    yields = {}

    # ── What 09_learn.py taught us ────────────────────────────────────────
    # Learned phrases extend the arXiv query (the search itself changes);
    # source stats nudge how many items a feed may contribute.
    learned = []
    try:
        learned = [p["phrase"] for p in json.loads((DATA / "learned_phrases.json").read_text(encoding="utf-8")).get("phrases", [])]
    except Exception:
        pass
    source_stats = {}
    try:
        source_stats = json.loads((DATA / "source_stats.json").read_text(encoding="utf-8")).get("sources", {})
    except Exception:
        pass
    for feed_conf in feeds:
        if feed_conf.get("learn_phrases") and learned and "search_query=" in feed_conf.get("url", ""):
            extra = "".join("+OR+abs:%22" + p.replace(" ", "+") + "%22" for p in learned)
            feed_conf["url"] = feed_conf["url"].replace("&sortBy=", extra + "&sortBy=", 1)
            feed_conf["_learned"] = list(learned)
        st = source_stats.get(feed_conf.get("name", ""))
        if st and st.get("informative") and feed_conf.get("max_items"):
            base = feed_conf["max_items"]
            if st["rate"] >= 0.6:
                feed_conf["max_items"] = min(base * 2, int(base * 1.5) + 1)
            elif st["rate"] <= 0.2 and st["verdicts"] >= 5:
                feed_conf["max_items"] = max(2, base // 2)
            if feed_conf["max_items"] != base:
                feed_conf["_nudged"] = (base, feed_conf["max_items"], st["rate"])
    if learned:
        print(f"  Search: {len(learned)} learned phrase(s) added to the arXiv query: "
              + ", ".join(learned[:5]) + (" …" if len(learned) > 5 else ""))
    nudged = [(f["name"], *f["_nudged"]) for f in feeds if f.get("_nudged")]
    for name, base, new, rate in nudged:
        print(f"  Search: {name} max_items {base} -> {new} (pick rate {rate:.2f})")

    # Fetch every source concurrently. 42 feeds fetched one after another is
    # 20-30s of pure network wait, and it also hides the HN resolver's arXiv
    # courtesy delays behind other feeds' downloads. Parsing, scoring and the
    # seen/theme bookkeeping below stay sequential on purpose.
    active = [f for f in feeds if f.get("enabled") is not False]

    def _fetch(conf):
        try:
            return conf, fetch_source(conf, seen), None
        except Exception as e:
            return conf, [], e

    fetched = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for conf, entries, err in pool.map(_fetch, active):
            fetched[id(conf)] = (entries, err)

    for feed_conf in feeds:
        # A feed can be parked in the config without being fetched, so sources
        # that need credentials can ship disabled until they are set up.
        if feed_conf.get("enabled") is False:
            continue

        feed_name = feed_conf.get("name", feed_conf["url"])
        feed_type = feed_conf.get("type", "news")

        # Per-feed freshness. A daily news site is stale in 72h, but the
        # Latent Space podcast ships an episode every ~4 days (max gap 13),
        # so a 72h window would silently miss most episodes entirely.
        feed_hours = feed_conf.get("freshness_hours", FRESHNESS_H)
        feed_cutoff = now - timedelta(hours=feed_hours)
        max_items = feed_conf.get("max_items")

        entries, err = fetched.get(id(feed_conf), ([], None))
        if err is not None:
            print(f"  Warning: {feed_name} -- {err}")
            continue

        kept = 0
        for item in entries:
            if max_items is not None and kept >= max_items:
                break

            url = item["url"]
            title = item["title"]
            if not title or not url or url in seen:
                continue

            published = item["published"]
            try:
                pub_dt = datetime.fromisoformat(published)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < feed_cutoff:
                    continue
            except Exception:
                pub_dt = now

            snippet = item.get("snippet", "")
            pillar, score, keywords = score_article(f"{title} {snippet}", pillars, weights)

            total_scanned += 1
            kept += 1
            seen[url] = now.isoformat()

            record = {
                "url": url,
                "title": title,
                "source": feed_name,
                "type": feed_type,
                "published": published,
                # Carried forward until this passes, so each feed ages out on
                # its own schedule rather than a single global window.
                "expires_at": (pub_dt + timedelta(hours=feed_hours)).isoformat(),
                # When Joe first saw it — the review-list TTL counts from here.
                "fetched_at": now.isoformat(),
                "snippet": snippet,
                "pillar": pillar,
                "score": score,
                "keywords_matched": keywords,
            }
            if item.get("upvotes") is not None:
                record["upvotes"] = item["upvotes"]
            if feed_conf.get("_learned"):
                # Which learned phrases this item matches — 09_learn.py uses
                # picks among these to keep or drop a phrase.
                hay = f"{title} {snippet}".lower()
                matched = [p for p in feed_conf["_learned"] if p in hay]
                if matched:
                    record["learned_matches"] = matched
            articles.append(record)

            if keywords:
                record_themes(keywords)

        yields[feed_name] = kept
        if kept == 0 and feed_name not in FEED_STATUS:
            # Reached the source fine, but nothing was new or fresh enough.
            FEED_STATUS[feed_name] = "no new items in window"

        if feed_type != "news":
            print(f"  {feed_name} [{feed_type}]: {kept} item(s) within {feed_hours}h")

    # Carry forward anything from the previous run that Joe has not reviewed
    # yet and that is still inside the freshness window. seen_urls stops these
    # coming back from the feeds, so without this step a daily fetch would
    # quietly discard yesterday's unreviewed backlog. Reviewed articles are
    # not carried — they already live in queue.json.
    carried = 0
    expired_ttl = 0
    expired_window = 0
    if OUT_PATH.exists() and REVIEW_TTL_H > 0:
        try:
            reviewed = set()
            if QUEUE_PATH.exists():
                reviewed = {x.get("url") for x in json.loads(QUEUE_PATH.read_text())}
            new_urls = {a["url"] for a in articles}
            for prev in json.loads(OUT_PATH.read_text()):
                url = prev.get("url")
                if not url or url in new_urls or url in reviewed:
                    continue
                # Prefer the item's own expiry (set from its feed's window);
                # fall back to the global window for records written before
                # per-feed freshness existed.
                try:
                    if prev.get("expires_at"):
                        exp_dt = datetime.fromisoformat(prev["expires_at"])
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if exp_dt < datetime.now(timezone.utc):
                            expired_window += 1
                            continue
                    else:
                        pub_dt = datetime.fromisoformat(prev.get("published", ""))
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        if pub_dt < cutoff_dt:
                            expired_window += 1
                            continue
                except Exception:
                    continue
                # Review-list TTL, counted from first sight. Records written
                # before fetched_at existed get stamped now, so the backlog
                # gets one full TTL rather than vanishing in a single run.
                if not prev.get("fetched_at"):
                    prev["fetched_at"] = now.isoformat()
                try:
                    seen_dt = datetime.fromisoformat(prev["fetched_at"])
                    if seen_dt.tzinfo is None:
                        seen_dt = seen_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    seen_dt = now
                if now - seen_dt > timedelta(hours=REVIEW_TTL_H):
                    expired_ttl += 1
                    continue
                prev["_carried"] = True
                articles.append(prev)
                carried += 1
        except Exception as e:
            print(f"  Warning: could not carry forward previous articles -- {e}")

    def _age_hours(article):
        try:
            pub_dt = datetime.fromisoformat(article["published"])
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600)
        except Exception:
            return 36.0

    def _freshness(hours):
        if hours < 4:   return 1.00
        if hours < 12:  return 0.85
        if hours < 24:  return 0.70
        if hours < 48:  return 0.45
        return 0.20

    # Cap in two tiers. New items have no rank yet, so they all get a look
    # (ordered by keyword score x freshness, the only signal they have).
    # Carried survivors compete for the remaining slots on post potential —
    # previously the cap trimmed everything by keyword score, discarding
    # articles the ranker had scored highly.
    fresh = [a for a in articles if not a.get("_carried")]
    held = [a for a in articles if a.get("_carried")]
    # New arrivals are ordered by recency. The keyword score used to decide
    # this, but with its learner frozen it is a crude relevance proxy at
    # best; the ranker scores everything properly moments later anyway.
    fresh.sort(key=lambda a: (_age_hours(a), -a.get("score", 0)))

    # Carried items compete on rank_score PERCENTILE WITHIN THEIR OWN TYPE,
    # not raw rank_score. Papers score systematically higher (gentler
    # freshness curve, take-ability floor, and a taste model that has never
    # seen a paper verdict), so a raw sort turned a 20%-paper pool into a
    # 56%-paper list on the first capped run. Percentile-within-type keeps
    # the surviving mix proportional to what came in.
    by_type = {}
    for a in held:
        by_type.setdefault(a.get("type", "news"), []).append(a)
    for items in by_type.values():
        items.sort(key=lambda a: a.get("rank_score") or 0, reverse=True)
        n = len(items)
        for i, a in enumerate(items):
            a["_pct"] = 1.0 - i / n
    held.sort(key=lambda a: (a["_pct"], a.get("rank_score") or 0), reverse=True)

    before_cap = len(articles)
    articles = (fresh + held)[:MAX_ARTICLES]
    trimmed = before_cap - len(articles)
    for a in articles:
        a.pop("_carried", None)
        a.pop("_pct", None)

    OUT_PATH.write_text(json.dumps(articles, indent=2))
    save_seen(seen)

    # ── Feed health ──────────────────────────────────────────────────────
    # A broken feed is invisible otherwise: it just silently stops
    # contributing. Anything that errored, redirected or came back empty is
    # named here and persisted for the dashboard.
    broken = {n: r for n, r in FEED_STATUS.items()
              if not r.startswith("no new items")}
    health = {
        "checked_at": now.isoformat(),
        "feeds_total": len(feeds),
        "feeds_active": len(active),
        "feeds_with_items": sum(1 for v in yields.values() if v > 0),
        "retention": {
            "review_ttl_hours": REVIEW_TTL_H,
            "max_articles": MAX_ARTICLES,
            "new": total_scanned,
            "carried": carried,
            "expired_ttl": expired_ttl,
            "expired_window": expired_window,
            "trimmed_by_cap": trimmed,
        },
        "yields": yields,
        "problems": FEED_STATUS,
    }
    (DATA / "feed_health.json").write_text(json.dumps(health, indent=2))

    if broken:
        print(f"\n  FEED PROBLEMS ({len(broken)}) — these contributed nothing:")
        for n, reason in sorted(broken.items()):
            print(f"    {n}: {reason}")

    rising = get_rising_themes()

    print(f"\nScanned {total_scanned} new articles, carried {carried} unreviewed "
          f"-> {len(articles)} saved (cap {MAX_ARTICLES}).")
    print(f"  Retention: {expired_ttl} left after {REVIEW_TTL_H}h unreviewed, "
          f"{expired_window} aged past their feed window, {trimmed} trimmed by the cap.")
    if rising:
        print("Rising themes:")
        for kw, n in rising[:5]:
            print(f"  * {kw} -- {n} articles this week")


if __name__ == "__main__":
    main()
