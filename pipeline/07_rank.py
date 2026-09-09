"""Stage 1.5: Re-rank fetched articles by "how likely is Joe to have something
useful to say about this?"

Runs after 01_fetch.py, before review. Three stages, cheapest first:

  A. Noise filter      — regex blocklist from experience.yaml. Pushes deals,
                         gadget roundups and shopping items to the bottom.
  B. Embeddings        — nomic-embed-text. Scores every article against
                         (1) Joe's experience corpus and (2) the centroids of
                         what he has previously drafted vs skipped. Also
                         collapses near-duplicate coverage of the same story.
  C. Local model judge — qwen2.5:7b with a constrained output schema. Classifies
                         what KIND of article it is (announcement / analysis /
                         opinion / experience report / shopping) and writes a
                         one-line summary of the claim.

Division of labour in stages B and C is deliberate. qwen2.5:7b was measured to
be unreliable at deciding whether an article matches Joe's experience — asked
directly, it answers "not relevant" to almost everything. Embeddings do that
job well. What the model IS reliable at is telling an announcement apart from
an argument, which is the take-ability signal. Each stage does only what it
was measured to be good at.

Nothing is deleted. Every article keeps its original keyword `score` and gains
a `rank_score` plus a full `rank` breakdown. Noise and duplicates sort to the
bottom rather than disappearing.

Fails safe: if Ollama is unreachable, the existing keyword ordering is left
exactly as 01_fetch.py wrote it and this script exits 0.

Usage:
    python 07_rank.py [--dry-run] [--limit N] [--no-llm] [--verbose]
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

SETTINGS_PATH = BASE / "settings.json"
EXPERIENCE_PATH = BASE / "experience.yaml"

ARTICLES_PATH = DATA / "articles.json"
QUEUE_PATH = DATA / "queue.json"
SELECTION_LOG_PATH = DATA / "selection_log.json"
POST_LOG_PATH = DATA / "post_log.json"
EMBED_CACHE_PATH = DATA / "embeddings.json"
KIND_CACHE_PATH = DATA / "kind_cache.json"
RANK_LOG_PATH = DATA / "rank_log.jsonl"

EMBED_MODEL = "nomic-embed-text"

# User-feedback weight is bounded by policy: not less than a third (Joe's
# judgment matters), not more than half (one person's taste shouldn't
# dominate the signal). Enforced in code, not just in the config comments.
TASTE_WEIGHT_MIN = 0.33
TASTE_WEIGHT_MAX = 0.50

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "how", "in", "into", "is", "it", "its", "new", "of", "on",
    "or", "that", "the", "their", "this", "to", "up", "was", "what", "when",
    "which", "who", "will", "with", "you", "your",
}


# ── Utilities ─────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def sha1(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def cosine(a, b):
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def centroid(vectors, weights=None):
    if not vectors:
        return None
    dim = len(vectors[0])
    if weights is None:
        weights = [1.0] * len(vectors)
    total = sum(weights)
    if total == 0:
        return None
    out = [0.0] * dim
    for vec, w in zip(vectors, weights):
        for i in range(dim):
            out[i] += vec[i] * w
    return [v / total for v in out]


def minmax(values):
    """Normalize to 0..1 across the batch. Cosine similarities from an
    embedding model sit in a narrow absolute band, so raw values are a poor
    ranking signal — relative position within the batch is what matters."""
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    if hi - lo < 1e-9:
        return [0.5 if v is not None else None for v in values]
    return [None if v is None else (v - lo) / (hi - lo) for v in values]


def age_hours(published_iso):
    try:
        dt = datetime.fromisoformat(published_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 36.0


def freshness(hours, item_type="news"):
    """News decays in hours; papers and podcasts do not.

    A news story is dead in two days, so the steep curve 01_fetch.py uses is
    right for it. A paper published last week is just as worth writing about
    today, and Latent Space ships an episode only every ~4 days — scoring
    either on the news curve buries them permanently.
    """
    if item_type in ("paper", "podcast"):
        days = hours / 24.0
        if days < 2:
            return 1.00
        if days < 5:
            return 0.85
        if days < 10:
            return 0.70
        if days < 21:
            return 0.50
        return 0.30
    if hours < 4:
        return 1.00
    if hours < 12:
        return 0.85
    if hours < 24:
        return 0.70
    if hours < 48:
        return 0.45
    return 0.20


def title_tokens(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Ollama ────────────────────────────────────────────────────────────────

class Ollama:
    def __init__(self, settings):
        cfg = settings.get("local_model", {})
        self.chat_url = cfg.get("url", "http://127.0.0.1:11434/api/chat")
        self.base = self.chat_url.split("/api/")[0]
        self.embed_url = f"{self.base}/api/embeddings"
        self.chat_model = cfg.get("model", "qwen2.5:7b")
        self.timeout = cfg.get("timeout", 60)

    def available(self):
        try:
            resp = requests.get(f"{self.base}/api/tags", timeout=5)
            resp.raise_for_status()
            names = {m.get("name", "") for m in resp.json().get("models", [])}
            have_embed = any(n.split(":")[0] == EMBED_MODEL for n in names)
            have_chat = any(n == self.chat_model or n.split(":")[0] == self.chat_model.split(":")[0]
                            for n in names)
            return True, have_embed, have_chat, sorted(names)
        except Exception as e:
            return False, False, False, str(e)

    def embed(self, text):
        resp = requests.post(
            self.embed_url,
            json={"model": EMBED_MODEL, "prompt": f"search_document: {text}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def chat(self, prompt, schema=None):
        payload = {
            "model": self.chat_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0},
        }
        if schema:
            # Constrained decoding — guarantees parseable JSON and means the
            # prompt needs no worked example, which the model would otherwise
            # copy verbatim instead of actually classifying.
            payload["format"] = schema
        resp = requests.post(self.chat_url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["message"]["content"]


# ── Embedding cache ───────────────────────────────────────────────────────

class ContentCache:
    """Keyed by content hash, so an article carried over from yesterday's run
    is never recomputed. This is what keeps the daily job cheap: at a 72h
    freshness window most of each batch is carried forward, and only genuinely
    new articles cost an embedding or a model call."""

    def __init__(self, path, retain_days):
        self.path = path
        self.retain_days = retain_days
        self.store = load_json(path, {})
        self.hits = 0
        self.misses = 0
        self.dirty = False

    def get_or_compute(self, text, compute):
        key = sha1(text)
        entry = self.store.get(key)
        if entry and entry.get("v"):
            self.hits += 1
            entry["ts"] = datetime.now(timezone.utc).isoformat()
            return entry["v"]
        value = compute(text)
        self.misses += 1
        if isinstance(value, list):
            # Embedding vectors round to 5dp to keep the cache file small.
            value = [round(x, 5) for x in value]
        self.store[key] = {
            "v": value,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.dirty = True
        return self.store[key]["v"]

    def prune_and_save(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retain_days)
        before = len(self.store)
        kept = {}
        for key, entry in self.store.items():
            try:
                if datetime.fromisoformat(entry["ts"]) > cutoff:
                    kept[key] = entry
            except Exception:
                continue
        self.store = kept
        save_json(self.path, self.store)
        return before, len(kept)


# ── Stage A: noise filter ─────────────────────────────────────────────────

def compile_noise_patterns(config):
    patterns = []
    for raw in config.get("noise_filters", {}).get("title_patterns", []):
        try:
            patterns.append((raw, re.compile(raw, re.I)))
        except re.error as e:
            print(f"  WARNING: bad noise pattern {raw!r} — {e}")
    return patterns


def noise_match(title, patterns):
    for raw, rx in patterns:
        if rx.search(title or ""):
            return raw
    return None


# ── Taste model: what Joe has drafted vs skipped ──────────────────────────

def build_training_set():
    """Labelled examples from Joe's own review decisions.

    selection_log.json is the full history but only stores the title.
    queue.json holds the complete article for entries still in the queue,
    so join on both title and url to recover snippets where we can.
    """
    queue = load_json(QUEUE_PATH, [])
    by_title = {}
    for q in queue:
        if q.get("title"):
            by_title[q["title"].strip().lower()] = q

    # Signal tiers. A verdict is not one bit: what Joe actually published
    # (and how it performed) says more than what he merely drafted.
    #   published + engagement percentile  2.0 .. 2.5
    #   published, no metrics yet          2.0
    #   approved, not yet out              1.5
    #   draft                              1.0
    #   hold                               0.5
    #   skip                               negative, 1.0
    TIER_W = {"draft": 1.0, "hold": 0.5, "approved": 1.5, "published": 2.0}

    examples = {}

    def put(key, title, snippet, label, tier, weight, typ):
        cur = examples.get(key)
        # Higher tier wins; a skip never overrides a later positive tier and
        # a positive never erases a skip from the same article — keep the
        # stronger claim.
        if cur and cur["weight"] >= weight and cur["label"] == label:
            if not cur["snippet"] and snippet:
                cur["snippet"] = snippet
                cur["has_snippet"] = True
            return
        if cur and cur["label"] != label and cur["weight"] > weight:
            return
        examples[key] = {"title": title, "snippet": snippet, "has_snippet": bool(snippet),
                         "label": label, "tier": tier, "weight": weight, "type": typ,
                         # kept for older call sites
                         "action": {"published": "draft", "approved": "draft"}.get(tier, tier)}

    for entry in load_json(SELECTION_LOG_PATH, []):
        title = (entry.get("article_title") or "").strip()
        action = entry.get("action")
        if not title or action not in ("draft", "skip", "hold"):
            continue
        key = title.lower()
        snippet = entry.get("snippet") or ""
        match = by_title.get(key)
        if not snippet and match:
            snippet = match.get("snippet", "")
        typ = entry.get("type") or (match.get("type") if match else None) or "news"
        if action == "skip":
            put(key, title, snippet, "neg", "skip", 1.0, typ)
        else:
            put(key, title, snippet, "pos", action, TIER_W[action], typ)

    for q in queue:
        key = (q.get("title") or "").strip().lower()
        if key and key not in examples and q.get("status") in ("draft", "skip", "hold"):
            st = q["status"]
            put(key, q["title"], q.get("snippet", ""), "neg" if st == "skip" else "pos",
                st, 1.0 if st == "skip" else TIER_W[st], q.get("type", "news"))

    # Post log: approvals and real publications, weighted by how the post did.
    post_log = load_json(POST_LOG_PATH, [])
    rates = sorted(p["metrics"]["engagement_rate"] for p in post_log
                   if (p.get("metrics") or {}).get("engagement_rate") is not None)
    for p in post_log:
        title = (p.get("article_title") or "").strip()
        if not title:
            continue
        key = title.lower()
        match = by_title.get(key)
        snippet = (match or {}).get("snippet", "") or (examples.get(key) or {}).get("snippet", "")
        typ = (match or {}).get("type") or (examples.get(key) or {}).get("type") or "news"
        if p.get("published_at"):
            w = TIER_W["published"]
            r = (p.get("metrics") or {}).get("engagement_rate")
            if r is not None and len(rates) >= 5:
                w = 1.5 + sum(1 for x in rates if x <= r) / len(rates)   # 1.5 .. 2.5
            put(key, title, snippet, "pos", "published", round(w, 3), typ)
        elif p.get("approved_at"):
            put(key, title, snippet, "pos", "approved", TIER_W["approved"], typ)

    return list(examples.values())


def embed_text_for(title, snippet, chars=400):
    body = (snippet or "")[:chars]
    return f"{title}. {body}".strip()


# ── Stage C: local model judge ────────────────────────────────────────────

KIND_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["announcement", "analysis", "opinion",
                     "experience_report", "shopping"],
        },
        "reason": {"type": "string"},
    },
    "required": ["kind", "reason"],
}

KIND_PROMPT = """Classify this article.

Title: {title}
Summary: {summary}

kind: which one of these best describes the article?
  "announcement"      = a company announced a product, feature, funding, partnership, or policy
  "analysis"          = explains how or why something works, with evidence or detail
  "opinion"           = argues a position that a reader could disagree with
  "experience_report" = a practitioner describing what happened when they actually tried something
  "shopping"          = a deal, discount, price drop, or consumer gadget review

reason: under 12 words, what the article actually claims or announces."""

# How much of a post is there in each kind of article? An experience report
# gives Joe something to compare his own results against; an announcement
# gives him a press release to summarise, which is the weakest kind of post.
KIND_TAKEABILITY = {
    "experience_report": 0.95,
    "opinion": 0.90,
    "analysis": 0.65,
    "announcement": 0.20,
    "shopping": 0.00,
}

# A paper states a claim and shows its working; a podcast episode is an hour of
# opinion. Both are inherently things Joe can react to from experience, more so
# than the local model can tell from a one-line description, so their
# take-ability has a floor independent of how stage C classifies them.
TYPE_TAKEABILITY = {
    "paper": 0.85,
    "podcast": 0.90,
}

TYPE_LABEL = {
    "paper": "PAPER",
    "podcast": "PODCAST",
}

KIND_LABEL = {
    "experience_report": "experience report",
    "opinion": "opinion",
    "analysis": "analysis",
    "announcement": "announcement",
    "shopping": "shopping",
}


def classify_article(ollama, article, verbose=False):
    """Returns (result_dict, raw_text, error). Never raises."""
    prompt = KIND_PROMPT.format(
        title=article["title"],
        summary=(article.get("snippet") or "")[:280],
    )
    last_raw = ""
    last_err = None
    for attempt in (1, 2):
        try:
            raw = ollama.chat(prompt, schema=KIND_SCHEMA)
            last_raw = raw
            parsed = json.loads(raw)
            kind = parsed.get("kind")
            if kind in KIND_TAKEABILITY:
                return ({
                    "kind": kind,
                    "takeability": KIND_TAKEABILITY[kind],
                    "reason": str(parsed.get("reason", "")).strip()[:160],
                    "attempts": attempt,
                }, raw, None)
            last_err = f"unknown kind {kind!r}"
        except requests.Timeout:
            last_err = "timeout"
        except Exception as e:
            last_err = str(e)
        if verbose:
            print(f"      retry ({last_err})")
    return (None, last_raw, last_err)


# ── Performance-metrics feedback (gated) ──────────────────────────────────

def metrics_multiplier(post_log, min_posts):
    """Reserved slot for learning from LinkedIn performance.

    Deliberately inert. With a handful of posts whose reaction counts sit in
    the low single digits and no comments at all, anything fitted to that data
    is modelling noise, not taste. The multiplier stays at 1.0 until there are
    `min_posts` posts with metrics; the stats are logged every run so the
    threshold can be watched as it fills up.
    """
    scored = [p for p in post_log if p.get("metrics")]
    n = len(scored)
    stats = {"posts_with_metrics": n, "threshold": min_posts, "active": False}

    if n:
        impressions = [p["metrics"].get("impressions") or 0 for p in scored]
        engagements = [
            (p["metrics"].get("reactions") or 0)
            + (p["metrics"].get("comments") or 0)
            + (p["metrics"].get("reposts") or 0)
            for p in scored
        ]
        stats["impressions_mean"] = round(sum(impressions) / n, 1)
        stats["impressions_range"] = [min(impressions), max(impressions)]
        stats["engagement_range"] = [min(engagements), max(engagements)]
        stats["engagement_mean"] = round(sum(engagements) / n, 2)

    if n < min_posts:
        stats["note"] = (
            f"inactive — {n}/{min_posts} posts with metrics. "
            "Not enough signal to learn from; multiplier held at 1.0."
        )
        return 1.0, stats

    stats["active"] = True
    stats["note"] = (
        f"{n} posts with metrics available — threshold reached. "
        "Enable a pillar-level multiplier here when you are ready to tune it."
    )
    return 1.0, stats


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Rank articles by post potential.")
    ap.add_argument("--dry-run", action="store_true",
                    help="score and print, but do not write articles.json")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N articles (for testing)")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip stage C, rank on embeddings alone")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    run_started = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    settings = load_json(SETTINGS_PATH, {})
    if not EXPERIENCE_PATH.exists():
        print(f"ERROR: {EXPERIENCE_PATH} not found.")
        sys.exit(1)
    config = yaml.safe_load(EXPERIENCE_PATH.read_text(encoding="utf-8"))

    tun = config.get("tunables", {})
    weights_cfg = dict(tun.get("weights", {}))

    # Policy bound on the user-feedback weight, enforced here rather than
    # trusted to the config file.
    requested_taste = weights_cfg.get("taste", 0.40)
    taste_weight = min(TASTE_WEIGHT_MAX, max(TASTE_WEIGHT_MIN, requested_taste))
    if abs(taste_weight - requested_taste) > 1e-9:
        print(f"  NOTE: taste weight {requested_taste} clamped to {taste_weight} "
              f"(policy bound {TASTE_WEIGHT_MIN}-{TASTE_WEIGHT_MAX}).")
    weights_cfg["taste"] = taste_weight

    articles = load_json(ARTICLES_PATH, [])
    if not articles:
        print("No articles to rank. Run 01_fetch.py first.")
        return
    if args.limit:
        articles = articles[: args.limit]

    print(f"Ranking {len(articles)} articles...")

    ollama = Ollama(settings)
    reachable, have_embed, have_chat, detail = ollama.available()
    if not reachable:
        print(f"  Ollama unreachable at {ollama.base} — {detail}")
        print("  Leaving the keyword ranking from 01_fetch.py untouched. Nothing changed.")
        log_run(run_id, {"status": "skipped_model_unavailable", "detail": str(detail)}, [])
        return
    if not have_embed:
        print(f"  '{EMBED_MODEL}' not installed on this Ollama instance.")
        print(f"  Install it with:  ollama pull {EMBED_MODEL}")
        print("  Leaving the keyword ranking untouched. Nothing changed.")
        log_run(run_id, {"status": "skipped_no_embed_model", "models": detail}, [])
        return

    use_llm = have_chat and not args.no_llm
    if not have_chat and not args.no_llm:
        print(f"  NOTE: chat model '{ollama.chat_model}' not found — "
              "ranking on embeddings only.")

    cache = ContentCache(EMBED_CACHE_PATH, tun.get("embedding_cache_days", 14))

    def embed(text):
        return cache.get_or_compute(text, ollama.embed)

    # ── Stage A: noise filter ────────────────────────────────────────────
    t0 = time.time()
    patterns = compile_noise_patterns(config)
    noise_hits = 0
    for a in articles:
        hit = noise_match(a.get("title", ""), patterns)
        a["_noise_pattern"] = hit
        if hit:
            noise_hits += 1
    stage_a_secs = time.time() - t0
    print(f"  [A] noise filter: {noise_hits} article(s) matched a blocklist pattern")

    # ── Stage B: embeddings ──────────────────────────────────────────────
    t0 = time.time()

    exp_items = config.get("experience", [])
    if not exp_items:
        print("  ERROR: experience.yaml has no `experience` entries.")
        sys.exit(1)
    exp_vectors = []
    phrase_total = 0
    for item in exp_items:
        phrases = item.get("phrases")
        if not phrases:
            # Backwards compatible with a single `text` blob, though phrases
            # match far more sharply — see the note in experience.yaml.
            phrases = [item.get("text", item.get("short", "")).strip()]
        vecs = [(p, embed(p)) for p in phrases if p and p.strip()]
        if not vecs:
            print(f"  WARNING: experience item {item.get('id')!r} has no phrases — skipped")
            continue
        exp_vectors.append((item, vecs))
        phrase_total += len(vecs)
    if not exp_vectors:
        print("  ERROR: no usable experience phrases in experience.yaml.")
        sys.exit(1)
    print(f"  [B] experience corpus: {len(exp_vectors)} item(s), {phrase_total} phrase(s) embedded")

    training = build_training_set()
    positives = [t for t in training if t["label"] == "pos"]
    negatives = [t for t in training if t["label"] == "neg"]
    drafts = [t for t in positives if t["tier"] != "hold"]
    holds = [t for t in positives if t["tier"] == "hold"]
    skips = negatives
    tiers = {}
    for t in training:
        tiers[t["tier"]] = tiers.get(t["tier"], 0) + 1

    min_d = tun.get("min_draft_samples", 12)
    min_s = tun.get("min_skip_samples", 12)
    taste_active = len(drafts) >= min_d and len(skips) >= min_s

    # Source affinity from 09_learn.py: pick-rate per feed, min-max scaled
    # across the informative sources so the factor is relative, not absolute.
    source_rate = {}
    try:
        _ss = load_json(DATA / "source_stats.json", {}).get("sources", {})
        inf = {k: v["rate"] for k, v in _ss.items() if v.get("informative")}
        if len(inf) >= 2:
            lo, hi = min(inf.values()), max(inf.values())
            source_rate = {k: ((v - lo) / (hi - lo) if hi > lo else 0.5) for k, v in inf.items()}
    except Exception:
        source_rate = {}
    if source_rate:
        print(f"  [B] source affinity: {len(source_rate)} informative source(s)")

    train_vecs = {}
    if taste_active:
        for t in training:
            train_vecs[t["title"]] = embed(embed_text_for(t["title"], t["snippet"]))
        print(f"  [B] taste model: {len(drafts)} draft + {len(holds)} hold "
              f"vs {len(skips)} skip — ACTIVE")
    else:
        print(f"  [B] taste model: {len(drafts)}/{min_d} drafts, {len(skips)}/{min_s} skips "
              "— INACTIVE (weight redistributed)")

    for a in articles:
        a["_vec"] = embed(embed_text_for(a["title"], a.get("snippet", "")))

    # Mean-centering. Every AI news story and every experience phrase shares a
    # large "this text is about AI" direction, which puts raw cosines between
    # any two of them at ~0.70 and leaves the argmax over topics to noise —
    # measured top-1 to top-5 spreads were 0.015-0.04 before centering.
    # Subtracting the corpus mean removes that shared component and leaves the
    # part that actually distinguishes one topic from another.
    pool = [pv for _, vecs in exp_vectors for _, pv in vecs] + [a["_vec"] for a in articles]
    mean_vec = centroid(pool)

    def centered(vec):
        return [x - m for x, m in zip(vec, mean_vec)]

    exp_centered = [(item, [(p, centered(pv)) for p, pv in vecs])
                    for item, vecs in exp_vectors]

    c_draft = c_skip = None
    c_pos_by_type, c_neg_by_type = {}, {}
    if taste_active:
        # Global centroids, weighted by tier (published > approved > draft > hold).
        pos_vecs = [centered(train_vecs[t["title"]]) for t in positives]
        pos_ws = [t["weight"] for t in positives]
        neg_vecs = [centered(train_vecs[t["title"]]) for t in negatives]
        neg_ws = [t["weight"] for t in negatives]
        c_draft = centroid(pos_vecs, pos_ws)
        c_skip = centroid(neg_vecs, neg_ws)

        # Per-type centroids with shrinkage toward the global one. A paper is
        # judged mostly against paper verdicts once there are enough of them;
        # with few, it leans on everything Joe has picked. k is the number of
        # weighted examples at which the type's own evidence equals the prior.
        k = float(tun.get("type_shrinkage", 8))

        def blended(items, c_global):
            out = {}
            for typ in {t["type"] for t in items}:
                vs = [centered(train_vecs[t["title"]]) for t in items if t["type"] == typ]
                ws = [t["weight"] for t in items if t["type"] == typ]
                n = sum(ws)
                c_raw = centroid(vs, ws)
                if c_raw is None or c_global is None:
                    continue
                out[typ] = [(n * a + k * b) / (n + k) for a, b in zip(c_raw, c_global)]
            return out

        c_pos_by_type = blended(positives, c_draft)
        c_neg_by_type = blended(negatives, c_skip)
        by_t = {}
        for t in training:
            by_t.setdefault(t["type"], [0, 0])[0 if t["label"] == "pos" else 1] += 1
        print("  [B] taste by type: " + ", ".join(f"{k2}={v[0]}+/{v[1]}-" for k2, v in sorted(by_t.items()))
              + f" (shrinkage k={k:g})")

    min_gap = tun.get("min_attribution_gap", 0.02)
    min_cos = tun.get("min_attribution_cosine", 0.12)
    strong_cos = tun.get("strong_attribution_cosine", 0.20)
    unattributed = 0

    for a in articles:
        vec_c = centered(a["_vec"])

        # An item scores as its single best-matching phrase; the article
        # scores as the mean of its two best-matching items, so one lucky
        # phrase hit cannot carry an article on its own.
        sims = []
        for item, vecs in exp_centered:
            best_p, best_s = max(((p, cosine(vec_c, pv)) for p, pv in vecs),
                                 key=lambda x: x[1])
            sims.append((best_s, item, best_p))
        sims.sort(key=lambda x: -x[0])
        top = sims[:2]
        a["_exp_raw"] = sum(s for s, _, _ in top) / len(top)
        a["_exp_gap"] = round(sims[0][0] - sims[1][0], 4) if len(sims) > 1 else 1.0
        a["_exp_second"] = None
        a["_exp_best_sim"] = round(sims[0][0], 4)
        a["_exp_best_phrase"] = sims[0][2]

        # Name the matched experience only when the match is strong in
        # absolute terms — naming the wrong topic is worse than admitting the
        # article is just generally AI-adjacent.
        #
        # A small margin over the runner-up is NOT ambiguity to hide. When two
        # of Joe's experience areas both match strongly that is a better
        # article, not a murkier one, so both get named.
        # Two ways to earn a label. A STRONG match (>= strong_cos) is named
        # regardless of margin. A MARGINAL one (>= min_cos) is named only if
        # it clearly beat the runner-up. Dense paper abstracts push cosines
        # past the floor even when nothing really matched — measured: 96% of
        # papers were labelled vs 77% of news, and the weakest labels were a
        # 3D-tokenization paper as "local inference" and an image-captioning
        # paper as "enterprise IT", all with gaps under 0.01.
        best, gap = a["_exp_best_sim"], a["_exp_gap"]
        strong = best >= strong_cos
        marginal = (not strong) and best >= min_cos and gap >= min_gap
        if strong or marginal:
            a["_exp_best"] = sims[0][1]["id"]
            labels = [sims[0][1]["label"]]
            # Name a runner-up only when BOTH are strong. A marginal pair with
            # a tiny gap is two guesses, not two matches.
            if strong and len(sims) > 1 and gap < min_gap and sims[1][0] >= strong_cos:
                labels.append(sims[1][1]["label"])
                a["_exp_second"] = sims[1][1]["id"]
            a["_exp_best_label"] = " + ".join(labels)
        else:
            a["_exp_best"] = None
            a["_exp_best_label"] = None
            unattributed += 1

        if taste_active:
            typ = a.get("type", "news")
            cp = c_pos_by_type.get(typ, c_draft)
            cn = c_neg_by_type.get(typ, c_skip)
            a["_taste_raw"] = cosine(vec_c, cp) - cosine(vec_c, cn)
        else:
            a["_taste_raw"] = None
        a["_source_raw"] = source_rate.get(a.get("source", ""))

    print(f"  [B] experience match: {len(articles) - unattributed} attributed, "
          f"{unattributed} unlabelled (cosine < {min_cos}, or < {strong_cos} "
          f"with no clear winner)")

    exp_norms = minmax([a["_exp_raw"] for a in articles])
    taste_norms = minmax([a["_taste_raw"] for a in articles])
    for a, en, tn in zip(articles, exp_norms, taste_norms):
        a["_exp_norm"] = en
        a["_taste_norm"] = tn

    # Near-duplicate collapse: same story, multiple outlets.
    dup_cos = tun.get("dup_cosine", 0.90)
    dup_jac = tun.get("dup_jaccard", 0.25)
    for a in articles:
        a["_tokens"] = title_tokens(a["title"])
        a["_dupe_of"] = None
        a["_dupe_sources"] = []

    merges = 0
    for i, a in enumerate(articles):
        if a["_dupe_of"] is not None:
            continue
        for b in articles[i + 1:]:
            if b["_dupe_of"] is not None:
                continue
            cos = cosine(a["_vec"], b["_vec"])
            if cos >= dup_cos and jaccard(a["_tokens"], b["_tokens"]) >= dup_jac:
                b["_dupe_of"] = a["url"]
                b["_dupe_cos"] = round(cos, 4)
                a["_dupe_sources"].append({
                    "source": b.get("source", ""),
                    "title": b["title"],
                    "url": b["url"],
                    "cosine": round(cos, 4),
                })
                merges += 1

    stage_b_secs = time.time() - t0
    print(f"  [B] embeddings: {cache.hits} cached, {cache.misses} computed "
          f"({stage_b_secs:.1f}s) — {merges} near-duplicate(s) merged")

    # ── Stage C: local model judge on the top slice ──────────────────────
    t0 = time.time()
    judged_count = 0
    judge_failures = 0

    def prior(a):
        """Pre-LLM ordering: what stage B alone thinks."""
        if a["_noise_pattern"] or a["_dupe_of"]:
            return -1.0
        taste = a["_taste_norm"] if a["_taste_norm"] is not None else 0.5
        return 0.55 * taste + 0.45 * (a["_exp_norm"] or 0.0)

    candidates = [a for a in articles if not a["_noise_pattern"] and not a["_dupe_of"]]
    candidates.sort(key=prior, reverse=True)
    top_n = tun.get("llm_judge_top_n", 30)
    to_judge = candidates[:top_n] if use_llm else []

    kind_cache = ContentCache(KIND_CACHE_PATH, tun.get("embedding_cache_days", 14))

    def classify_cached(article):
        """Cache on the article text, not the url — a retitled or edited
        article is genuinely different and should be reclassified."""
        key_text = f"{article['title']}||{(article.get('snippet') or '')[:280]}"
        holder = {}

        def compute(_):
            result, raw, err = classify_article(ollama, article, args.verbose)
            holder["raw"] = raw
            holder["err"] = err
            if result is None:
                raise RuntimeError(err or "classify failed")
            return result

        try:
            # get_or_compute stores under "v"; a dict round-trips through JSON
            # exactly as well as a vector does.
            return kind_cache.get_or_compute(key_text, compute), holder.get("raw", ""), None
        except Exception:
            return None, holder.get("raw", ""), holder.get("err") or "classify failed"

    if to_judge:
        print(f"  [C] classifying {len(to_judge)} with {ollama.chat_model}...")
        for n, a in enumerate(to_judge, 1):
            result, raw, err = classify_cached(a)
            if result:
                a["_llm"] = result
                judged_count += 1
                if args.verbose:
                    print(f"      [{n}/{len(to_judge)}] {result['kind']:<18} {a['title'][:48]}")
            else:
                a["_llm"] = None
                a["_llm_error"] = err
                judge_failures += 1
                if args.verbose:
                    print(f"      [{n}/{len(to_judge)}] FAILED ({err}) — {a['title'][:48]}")
            a["_llm_raw"] = (raw or "")[:500]
    stage_c_secs = time.time() - t0
    if use_llm:
        print(f"  [C] classified {judged_count} ({kind_cache.hits} cached, "
              f"{kind_cache.misses} new), {judge_failures} failed ({stage_c_secs:.1f}s)")

    # ── Composite score ──────────────────────────────────────────────────
    post_log = load_json(POST_LOG_PATH, [])
    mult, metrics_stats = metrics_multiplier(post_log, tun.get("min_metrics_posts", 20))
    print(f"  [M] metrics feedback: {metrics_stats['note']}")

    max_corr = max(1, tun.get("max_corroboration", 5))
    records = []

    for a in articles:
        llm = a.get("_llm")

        factors = {}
        factors["taste"] = a["_taste_norm"]
        # Experience overlap is embeddings-only by design — see the module
        # docstring for why the local model is not consulted here.
        factors["experience"] = a["_exp_norm"]
        take = llm["takeability"] if llm else None
        type_floor = TYPE_TAKEABILITY.get(a.get("type"))
        if type_floor is not None:
            take = type_floor if take is None else max(take, type_floor)
        factors["takeability"] = take
        factors["freshness"] = freshness(age_hours(a.get("published", "")),
                                         a.get("type", "news"))
        # Source affinity: None (renormalised away) for feeds with too few
        # verdicts to say anything, so a new feed is neither helped nor hurt.
        factors["source"] = a.get("_source_raw")
        n_dupes = len(a["_dupe_sources"])
        factors["corroboration"] = min(n_dupes, max_corr) / max_corr if n_dupes else 0.0

        used = {k: v for k, v in factors.items() if v is not None}
        wsum = sum(weights_cfg.get(k, 0.0) for k in used)
        if wsum <= 0:
            score = 0.0
        else:
            score = sum(weights_cfg.get(k, 0.0) * v for k, v in used.items()) / wsum
        score = round(max(0.0, min(1.0, score * mult)) * 100, 1)

        # Bucket 0 sorts first. Nothing is deleted — noise and duplicates are
        # pushed below the real candidates but stay in the list.
        if a["_noise_pattern"]:
            bucket, stage = 2, "noise"
        elif llm and llm.get("kind") == "shopping":
            bucket, stage = 2, "noise_model"
        elif a["_dupe_of"]:
            bucket, stage = 1, "duplicate"
        else:
            bucket, stage = 0, ("llm" if llm else "prior")

        # `reason` answers "why is this here?"; `summary` is what the article
        # actually says. Both are shown in the review UI.
        if stage == "noise":
            reason = f"noise filter: {a['_noise_pattern']}"
        elif stage == "noise_model":
            reason = "model classified as shopping/deal content"
        elif stage == "duplicate":
            reason = "same story as an article ranked above"
        else:
            bits = []
            if a.get("type") in TYPE_LABEL:
                bits.append(TYPE_LABEL[a["type"]].lower())
            if llm and llm.get("kind"):
                bits.append(KIND_LABEL.get(llm["kind"], llm["kind"]))
            if a["_exp_best_label"]:
                bits.append(f"your work on {a['_exp_best_label']}")
            else:
                bits.append("no clear match to your experience")
            reason = " · ".join(bits)
        summary = llm.get("reason", "") if llm else ""

        a["rank_score"] = score
        a["rank"] = {
            "stage": stage,
            "type": a.get("type", "news"),
            "reason": reason,
            "summary": summary,
            "kind": llm.get("kind") if llm else None,
            "matched_experience": a["_exp_best"],
            "matched_experience_second": a.get("_exp_second"),
            "matched_experience_label": a["_exp_best_label"],
            "factors": {k: (round(v, 4) if v is not None else None) for k, v in factors.items()},
            "weights_used": {k: weights_cfg.get(k, 0.0) for k in used},
            "raw": {
                "experience_cosine": round(a["_exp_raw"], 4),
                "best_experience_cosine": a["_exp_best_sim"],
                "best_experience_phrase": a.get("_exp_best_phrase", ""),
                "attribution_gap": a.get("_exp_gap"),
                "taste_delta": round(a["_taste_raw"], 4) if a["_taste_raw"] is not None else None,
                "keyword_score": a.get("score", 0),
            },
            "llm": {k: v for k, v in (llm or {}).items()} if llm else None,
            "llm_error": a.get("_llm_error"),
            "noise_pattern": a["_noise_pattern"],
            "dupe_of": a["_dupe_of"],
            "dupe_count": n_dupes,
            "dupe_sources": a["_dupe_sources"],
            "metrics_multiplier": mult,
            "taste_active": taste_active,
            "run_id": run_id,
        }
        a["_bucket"] = bucket

        records.append({
            # Spread the rank block FIRST so nothing inside it can clobber the
            # record schema. It carries its own "type" (news/paper/podcast),
            # which for two days overwrote this "article" tag and made every
            # log consumer that filters on it see zero rows.
            **a["rank"],
            "type": "article",
            "item_type": a["rank"].get("type", "news"),
            "run_id": run_id,
            "url": a["url"],
            "title": a["title"],
            "source": a.get("source", ""),
            "published": a.get("published", ""),
            "rank_score": score,
            "bucket": bucket,
            "llm_raw": a.get("_llm_raw", ""),
        })

    articles.sort(key=lambda a: (a["_bucket"], -a["rank_score"]))

    # Strip working fields before writing.
    for a in articles:
        for key in list(a.keys()):
            if key.startswith("_"):
                del a[key]

    top_bucket = [a for a in articles if a["rank"]["stage"] in ("llm", "prior")]
    print(f"\n  {len(top_bucket)} live candidate(s), "
          f"{sum(1 for a in articles if a['rank']['stage'] == 'duplicate')} duplicate(s), "
          f"{sum(1 for a in articles if a['rank']['stage'].startswith('noise'))} filtered as noise")

    types = {}
    for a in articles:
        t = a.get("type", "news")
        types[t] = types.get(t, 0) + 1
    print("  source mix: " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))

    kinds = {}
    for a in articles:
        if a["rank"].get("kind"):
            kinds[a["rank"]["kind"]] = kinds.get(a["rank"]["kind"], 0) + 1
    if kinds:
        print("  article kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    print("\n  TOP 10:")
    for i, a in enumerate(articles[:10], 1):
        print(f"  {i:>2}. [{a['rank_score']:>5.1f}] {a['title'][:58]}")
        print(f"      {a['rank']['reason'][:78]}")
        if a["rank"].get("summary"):
            print(f"      \"{a['rank']['summary'][:72]}\"")

    run_summary = {
        "status": "ok",
        "articles": len(articles),
        "candidates": len(top_bucket),
        "noise": sum(1 for a in articles if a["rank"]["stage"].startswith("noise")),
        "duplicates": sum(1 for a in articles if a["rank"]["stage"] == "duplicate"),
        "judged": judged_count,
        "judge_failures": judge_failures,
        "taste_active": taste_active,
        "training": {"draft": len(drafts), "skip": len(skips), "hold": len(holds),
                     "with_snippet": sum(1 for t in training if t["has_snippet"]),
                     "tiers": tiers,
                     "by_type": {typ: [sum(1 for t in training if t["type"] == typ and t["label"] == "pos"),
                                       sum(1 for t in training if t["type"] == typ and t["label"] == "neg")]
                                 for typ in {t["type"] for t in training}}},
        "source_affinity_sources": sorted(source_rate.keys()),
        "weights": weights_cfg,
        "taste_weight_requested": requested_taste,
        "taste_weight_applied": taste_weight,
        "metrics": metrics_stats,
        "embed_cache": {"hits": cache.hits, "misses": cache.misses},
        "kind_cache": {"hits": kind_cache.hits, "misses": kind_cache.misses},
        "timings_secs": {
            "stage_a": round(stage_a_secs, 2),
            "stage_b": round(stage_b_secs, 2),
            "stage_c": round(stage_c_secs, 2),
            "total": round(time.time() - run_started, 2),
        },
        "dry_run": args.dry_run,
        "llm_used": use_llm,
    }

    if args.dry_run:
        print("\n  DRY RUN — articles.json not written.")
    else:
        save_json(ARTICLES_PATH, articles)
        print(f"\n  Wrote {len(articles)} ranked articles to {ARTICLES_PATH.name}")

    before, after = cache.prune_and_save()
    if before != after:
        print(f"  Embedding cache pruned: {before} -> {after} entries")
    kind_cache.prune_and_save()

    log_run(run_id, run_summary, records, prune_days=tun.get("rank_log_days", 30))
    print(f"  Run logged to {RANK_LOG_PATH.name} (run_id {run_id})")
    print(f"  Done in {run_summary['timings_secs']['total']}s")


def log_run(run_id, summary, records, prune_days=30):
    """Append-only per-article log. LOG EVERYTHING: every factor, every weight,
    every model output, so any ranking decision can be reconstructed later."""
    lines = [json.dumps({"type": "run", "run_id": run_id,
                         "at": datetime.now(timezone.utc).isoformat(), **summary})]
    lines += [json.dumps(r) for r in records]

    existing = []
    if RANK_LOG_PATH.exists():
        cutoff = (datetime.now(timezone.utc) - timedelta(days=prune_days)).strftime("%Y%m%dT%H%M%SZ")
        for line in RANK_LOG_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rid = json.loads(line).get("run_id", "")
            except Exception:
                continue
            if rid >= cutoff:
                existing.append(line)

    RANK_LOG_PATH.write_text("\n".join(existing + lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
