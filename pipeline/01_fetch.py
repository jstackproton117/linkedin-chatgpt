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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import feedparser
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

MAX_ARTICLES = SETTINGS["pipeline"]["max_articles"]
FRESHNESS_H = SETTINGS["pipeline"]["freshness_hours"]

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
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
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
    """Adjust keyword weights based on draft/skip history. Small steps only."""
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

    for feed_conf in feeds:
        feed_name = feed_conf.get("name", feed_conf["url"])
        try:
            feed = feedparser.parse(feed_conf["url"])
            for entry in feed.entries:
                title = strip_html(entry.get("title", ""))
                url = entry.get("link", "")
                if not title or not url or url in seen:
                    continue

                published = parse_date(entry.get("published", ""))
                try:
                    pub_dt = datetime.fromisoformat(published)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt < cutoff_dt:
                        continue
                except Exception:
                    pass

                snippet = get_content(entry)[:400]
                pillar, score, keywords = score_article(f"{title} {snippet}", pillars, weights)

                total_scanned += 1
                seen[url] = datetime.now(timezone.utc).isoformat()

                articles.append({
                    "url": url,
                    "title": title,
                    "source": feed_name,
                    "published": published,
                    "snippet": snippet,
                    "pillar": pillar,
                    "score": score,
                    "keywords_matched": keywords,
                })

                if keywords:
                    record_themes(keywords)

        except Exception as e:
            print(f"  Warning: {feed_name} -- {e}")

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

    articles.sort(key=lambda a: a["score"] * _freshness(_age_hours(a)), reverse=True)
    articles = articles[:MAX_ARTICLES]

    OUT_PATH.write_text(json.dumps(articles, indent=2))
    save_seen(seen)

    rising = get_rising_themes()

    print(f"\nScanned {total_scanned} new articles -> {len(articles)} saved (ranked by relevance).")
    if rising:
        print("Rising themes:")
        for kw, n in rising[:5]:
            print(f"  * {kw} -- {n} articles this week")


if __name__ == "__main__":
    main()
