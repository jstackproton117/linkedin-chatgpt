"""Stage 9: Learn from Joe's picks. Runs nightly after rank.

Three outputs, all bounded, all logged to data/learning_log.jsonl:

  data/source_stats.json
      Draft-rate per feed (Laplace-smoothed). The ranker turns it into a
      small "source affinity" factor; the fetcher nudges a feed's max_items
      up or down. This is how a feed that keeps producing picks gets more
      room, and one that never does gets less.

  data/learned_phrases.json
      Phrases that distinguish what Joe drafts/publishes from what he skips,
      chosen by log-odds over 1-3 word n-grams. The fetcher appends them to
      the arXiv query — this is the part that actually changes what gets
      SEARCHED, not just how results are ranked. Capped (max_learned_phrases),
      a few per night (phrases_per_night), and a phrase that never leads to a
      pick within decay_days is dropped.

  data/experience_proposals.json
      The same phrases, offered as additions to experience.yaml. That file
      defines Joe's actual experience, so it is never edited automatically:
      the Settings page shows proposals with ADD / DISMISS.

Usage:  python 09_learn.py [--dry-run] [--verbose]
"""

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BASE = Path(__file__).parent
DATA = BASE / "data"
SETTINGS_PATH = BASE / "settings.json"
EXPERIENCE_PATH = BASE / "experience.yaml"
CONFIG_PATH = BASE / "content_radar_config.yaml"
SELECTION_LOG_PATH = DATA / "selection_log.json"
QUEUE_PATH = DATA / "queue.json"
POST_LOG_PATH = DATA / "post_log.json"
ARTICLES_PATH = DATA / "articles.json"
SOURCE_STATS_PATH = DATA / "source_stats.json"
LEARNED_PATH = DATA / "learned_phrases.json"
PROPOSALS_PATH = DATA / "experience_proposals.json"
LOG_PATH = DATA / "learning_log.jsonl"

DEFAULTS = {
    "phrases_per_night": 3,
    "max_learned_phrases": 10,
    "decay_days": 21,
    "min_positive_docs": 3,      # a phrase must appear in this many picks
    "min_log_odds": 1.0,         # and be this much more common in picks than skips
    "max_corpus_df": 0.08,       # ...and in no more than this share of ALL articles
    "source_min_verdicts": 3,    # verdicts before a source gets a non-neutral rate
}

# The first run learned "information", "reports" and "recent" — generic
# words that merely skewed toward picks. Two guards now: arXiv phrases must
# be multi-word (a one-word abs:"..." search is far too broad anyway), and a
# phrase common across the whole article corpus is not distinctive whatever
# the picks say. STOP is also much wider.
GENERIC = set("""information report reports recent recently data system systems learning training
performance research work world time year years week today company companies people first last
best big small high low open free real next top way ways thing things make makes made get gets got
need needs help helps like one two three well still even much many part case cases key main major
across around against through per every another own back long short early late good bad better
worse great human humans users user tool tools technology tech article news blog post posts video
announce announced announces launch launches launched release released releases update updates
available support supports introduce introduces introducing build building built create creates
created provide provides providing enable enables improve improves improved improving better
important significant significantly important different various several multiple including
between within without across during after before under over general generally specific
specifically current currently future potential potentially""".split())
STOP_EXTRA = GENERIC

STOP = set("""a an the and or of to in on for with by from as at is are was were be been being it its this
that these those he she they them his her their we our you your i my me not no yes but if then than so such
into over under about after before between during without within while can could may might will would should
do does did done have has had having more most less least very just also only same other each any all some
new how what which who whom whose when where why via vs using use used uses based via toward towards
paper papers model models approach approaches method methods results result show shows shown study
propose proposed proposes present presents presented framework large language llm llms ai""".split())


def settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def log_event(kind, **fields):
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": kind, **fields}) + "\n")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def norm_text(s):
    return re.sub(r"[^a-z0-9\- ]+", " ", (s or "").lower())


def ngrams(text, n_max=3):
    words = [w for w in norm_text(text).split() if len(w) > 2]
    out = set()
    stop = STOP | STOP_EXTRA
    for n in range(1, n_max + 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in stop or gram[-1] in stop:
                continue
            if all(w in stop for w in gram):
                continue
            if any(w.isdigit() for w in gram):
                continue
            out.add(" ".join(gram))
    return out


# ── Signal: what Joe picked, with tiers ───────────────────────────────────

def collect_signal():
    """Positives and negatives as (text, weight). Tiers:
    published+engaged 2.0-2.5 · published 2.0 · approved 1.5 · draft 1.0 ·
    hold 0.5 · skip -> negative 1.0. Text is title + snippet where known."""
    sel = load_json(SELECTION_LOG_PATH, [])
    queue = load_json(QUEUE_PATH, [])
    post_log = load_json(POST_LOG_PATH, [])
    snippet_by_title = {}
    source_by_title = {}
    for q in queue:
        if q.get("title"):
            snippet_by_title[q["title"].strip().lower()] = q.get("snippet", "")
            source_by_title[q["title"].strip().lower()] = q.get("source", "")
    for e in sel:
        t = (e.get("article_title") or "").strip().lower()
        if e.get("snippet"):
            snippet_by_title[t] = e["snippet"]
        if e.get("source"):
            source_by_title[t] = e["source"]

    # Engagement percentile among published posts with metrics.
    rates = sorted(p["metrics"]["engagement_rate"] for p in post_log
                   if (p.get("metrics") or {}).get("engagement_rate") is not None)

    def eng_weight(p):
        r = (p.get("metrics") or {}).get("engagement_rate")
        if r is None or len(rates) < 5:
            return 2.0
        pct = sum(1 for x in rates if x <= r) / len(rates)
        return 1.5 + pct  # 1.5 .. 2.5

    pos, neg = {}, {}
    per_source = defaultdict(lambda: Counter())

    for e in sel:
        t = (e.get("article_title") or "").strip()
        if not t:
            continue
        key = t.lower()
        text = t + ". " + (snippet_by_title.get(key) or "")
        a = e.get("action")
        src = e.get("source") or source_by_title.get(key) or "?"
        if a == "draft":
            pos[key] = max(pos.get(key, 0), 1.0); per_source[src]["draft"] += 1
        elif a == "hold":
            pos[key] = max(pos.get(key, 0), 0.5); per_source[src]["hold"] += 1
        elif a == "skip":
            neg[key] = 1.0; per_source[src]["skip"] += 1
        snippet_by_title.setdefault(key, text)

    for p in post_log:
        key = (p.get("article_title") or "").strip().lower()
        if not key:
            continue
        if p.get("published_at"):
            pos[key] = max(pos.get(key, 0), eng_weight(p))
        elif p.get("approved_at"):
            pos[key] = max(pos.get(key, 0), 1.5)
        if key not in snippet_by_title:
            snippet_by_title[key] = p.get("article_title", "")

    def texts(d):
        return [(snippet_by_title.get(k) or k, w) for k, w in d.items()]

    return texts(pos), texts(neg), per_source


# ── Source stats ──────────────────────────────────────────────────────────

def source_stats(per_source, cfg):
    min_v = cfg["source_min_verdicts"]
    out = {}
    for src, c in per_source.items():
        d, h, s = c["draft"], c["hold"], c["skip"]
        n = d + h + s
        # Laplace-smoothed pick rate; holds count half.
        rate = (d + 0.5 * h + 1.0) / (n + 2.0)
        out[src] = {"draft": d, "hold": h, "skip": s, "verdicts": n,
                    "rate": round(rate, 3), "informative": n >= min_v}
    return out


# ── Phrase learning ───────────────────────────────────────────────────────

def existing_phrases():
    """Phrases already in the arXiv query or experience.yaml — never re-learn."""
    have = set()
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        for f in cfg.get("feeds", []):
            if "arxiv.org" in f.get("url", ""):
                for m in re.finditer(r"abs:%22([^%]+)%22", f["url"]):
                    have.add(m.group(1).replace("+", " ").lower())
    except Exception:
        pass
    try:
        exp = yaml.safe_load(EXPERIENCE_PATH.read_text(encoding="utf-8"))
        for item in exp.get("experience", []):
            for p in item.get("phrases", []):
                have.add(norm_text(p).strip())
    except Exception:
        pass
    return have


def experience_covers(phrase, exp_phrases):
    """True if some existing experience phrase already contains all the
    phrase's words — no point proposing "agent memory" when "agent memory
    and context window management" is there."""
    words = set(phrase.split())
    for p in exp_phrases:
        if words <= set(p.split()):
            return True
    return False


def corpus_df(articles):
    """How common each n-gram is across everything fetched lately, picks or
    not. The distinctiveness test is against this, not just against skips."""
    df = Counter()
    for a in articles:
        for g in ngrams(a.get("title", "") + " " + a.get("snippet", "")):
            df[g] += 1
    return df, max(1, len(articles))


def learn_phrases(pos, neg, cfg, already, corpus=None):
    """Log-odds of n-gram presence in picks vs skips (add-one smoothed),
    weighted by tier. Returns [(phrase, score, pos_docs)] best first."""
    pos_df, neg_df = Counter(), Counter()
    pos_total = sum(w for _, w in pos) or 1.0
    neg_total = sum(w for _, w in neg) or 1.0
    pos_docs = Counter()
    for text, w in pos:
        for g in ngrams(text):
            pos_df[g] += w
            pos_docs[g] += 1
    for text, w in neg:
        for g in ngrams(text):
            neg_df[g] += w
    cdf, csize = corpus or (Counter(), 1)
    scored = []
    for g, pw in pos_df.items():
        if pos_docs[g] < cfg["min_positive_docs"]:
            continue
        if g in already or len(g) < 6:
            continue
        if " " not in g:
            # One-word searches are too broad to be worth a slot.
            continue
        if csize > 20 and cdf[g] / csize > cfg["max_corpus_df"]:
            continue
        lo = math.log((pw + 1) / (pos_total + 2)) - math.log((neg_df[g] + 1) / (neg_total + 2))
        if lo < cfg["min_log_odds"]:
            continue
        # Prefer multi-word phrases: they search far more precisely.
        bonus = 0.3 * (g.count(" "))
        scored.append((g, round(lo + bonus, 3), pos_docs[g]))
    scored.sort(key=lambda x: (-x[1], -x[2]))
    # Drop phrases subsumed by a higher-ranked longer one ("agent" vs "agent memory").
    kept = []
    for g, s, n in scored:
        if any((g in k or k in g) for k, _, _ in kept):
            continue
        kept.append((g, s, n))
    return kept


def phrase_hits_and_picks(phrase, articles, sel):
    hits = sum(1 for a in articles if phrase in norm_text(a.get("title", "") + " " + a.get("snippet", "")))
    picks = sum(1 for e in sel if e.get("action") == "draft"
                and phrase in norm_text((e.get("article_title") or "") + " " + (e.get("snippet") or "")))
    return hits, picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = {**DEFAULTS, **(settings().get("learning") or {})}
    now = datetime.now(timezone.utc)

    pos, neg, per_source = collect_signal()
    print(f"  Learn: {len(pos)} positive, {len(neg)} negative example(s)")
    if len(pos) < 5 or len(neg) < 5:
        print("  Learn: not enough verdicts yet — nothing changed")
        return

    # 1. Sources
    stats = source_stats(per_source, cfg)
    informative = sorted(((s, v) for s, v in stats.items() if v["informative"]),
                         key=lambda x: -x[1]["rate"])
    print(f"  Learn: source stats for {len(stats)} source(s), {len(informative)} informative")
    for s, v in informative[:5]:
        print(f"      {v['rate']:.2f}  {s[:32]:32s} draft={v['draft']} hold={v['hold']} skip={v['skip']}")

    # 2. Phrases for the arXiv query
    already = existing_phrases()
    learned = load_json(LEARNED_PATH, {"phrases": []})
    # Phrases Joe removed in Settings stay removed.
    vetoed = set(learned.get("vetoed", []))
    already |= vetoed
    sel = load_json(SELECTION_LOG_PATH, [])
    articles = load_json(ARTICLES_PATH, [])
    active = []
    dropped = []
    for ph in learned.get("phrases", []):
        hits, picks = phrase_hits_and_picks(ph["phrase"], articles, sel)
        ph["hits"] = hits
        ph["picks"] = picks
        age = (now - datetime.fromisoformat(ph["added_at"])).days
        if picks == 0 and age >= cfg["decay_days"]:
            dropped.append(ph)
        else:
            active.append(ph)
    for ph in dropped:
        print(f"  Learn: dropped '{ph['phrase']}' — no picks in {cfg['decay_days']} days")
        log_event("phrase_dropped", phrase=ph["phrase"], hits=ph.get("hits"), age_days=cfg["decay_days"])
    already |= {p["phrase"] for p in active}

    candidates = learn_phrases(pos, neg, cfg, already, corpus=corpus_df(articles))
    room = max(0, cfg["max_learned_phrases"] - len(active))
    take = candidates[: min(room, cfg["phrases_per_night"])]
    for g, s, n in take:
        active.append({"phrase": g, "score": s, "positive_docs": n,
                       "added_at": now.isoformat(), "hits": 0, "picks": 0})
        print(f"  Learn: + arXiv phrase '{g}' (log-odds {s}, in {n} picks)")
        log_event("phrase_added", phrase=g, log_odds=s, positive_docs=n)
    if not take:
        print(f"  Learn: no new arXiv phrases ({len(candidates)} candidate(s), room for {room})")
    if args.verbose:
        for g, s, n in candidates[:10]:
            print(f"      cand {s:5.2f} x{n}  {g}")

    # 3. Proposals for experience.yaml (never auto-applied)
    proposals = load_json(PROPOSALS_PATH, {"proposals": [], "dismissed": []})
    dismissed = set(proposals.get("dismissed", []))
    try:
        exp = yaml.safe_load(EXPERIENCE_PATH.read_text(encoding="utf-8"))
        exp_phrases = [norm_text(p).strip() for i in exp.get("experience", []) for p in i.get("phrases", [])]
    except Exception:
        exp_phrases = []
    current = {p["phrase"] for p in proposals.get("proposals", [])}
    new_props = []
    for g, s, n in candidates[:12]:
        if g in dismissed or g in current or experience_covers(g, exp_phrases):
            continue
        if " " not in g:      # single words make poor experience phrases
            continue
        new_props.append({"phrase": g, "score": s, "positive_docs": n, "proposed_at": now.isoformat()})
    proposals["proposals"] = (proposals.get("proposals", []) + new_props)[-15:]
    if new_props:
        print(f"  Learn: {len(new_props)} new experience proposal(s) for Settings")

    if args.dry_run:
        print("  DRY RUN — nothing written")
        return
    SOURCE_STATS_PATH.write_text(json.dumps({"updated_at": now.isoformat(), "sources": stats}, indent=2), encoding="utf-8")
    LEARNED_PATH.write_text(json.dumps({"updated_at": now.isoformat(), "phrases": active,
                                        "vetoed": sorted(vetoed)}, indent=2), encoding="utf-8")
    PROPOSALS_PATH.write_text(json.dumps(proposals, indent=2), encoding="utf-8")
    log_event("run", positives=len(pos), negatives=len(neg), sources=len(stats),
              learned_active=len(active), added=[g for g, _, _ in take], dropped=[p["phrase"] for p in dropped],
              proposals=len(proposals["proposals"]))
    print(f"  Learn: {len(active)} learned arXiv phrase(s) active, {len(proposals['proposals'])} proposal(s) pending")


if __name__ == "__main__":
    main()
