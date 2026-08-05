"""Stage 2: Ranked article list -> Joe selects numbers -> detail + AI angle -> mark draft/skip/hold.

Shows all articles sorted by relevance. Joe enters article numbers to open them.
Local model generates a post angle and hook for each opened article.
All selections are logged for keyword weight learning (see 01_fetch.py).
"""

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
SETTINGS = json.loads((BASE / "settings.json").read_text())
DATA = BASE / "data"

ARTICLES_PATH = DATA / "articles.json"
QUEUE_PATH = DATA / "queue.json"
SELECTION_LOG_PATH = DATA / "selection_log.json"
THEME_PATH = DATA / "theme_counts.json"

MODEL_URL = SETTINGS["local_model"]["url"]
MODEL_NAME = SETTINGS["local_model"]["model"]
TIMEOUT = SETTINGS["local_model"]["timeout"]

ANGLE_PROMPT = """Output JSON only. No explanation, no markdown, no preamble.

Article title: {title}
Summary: {summary}

Task: suggest a LinkedIn post angle for engineering leaders.

Output this exact JSON structure:
{{"angle": "one sentence post angle", "hook": "first 10 words of the post"}}

JSON:
{{"""


def format_age(published_iso):
    try:
        pub_dt = datetime.fromisoformat(published_iso)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
        if hours < 1:    return f"{int(hours * 60)}m"
        if hours < 24:   return f"{int(hours)}h"
        return f"{int(hours / 24)}d"
    except Exception:
        return "??"


def score_bar(score):
    if score >= 80: return "[*****]"
    if score >= 60: return "[****-]"
    if score >= 40: return "[***--]"
    if score >= 20: return "[**---]"
    if score > 0:   return "[*----]"
    return "[-----]"


def get_rising_themes():
    if not THEME_PATH.exists():
        return []
    counts = json.loads(THEME_PATH.read_text())
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    rising = []
    for kw, dates in counts.items():
        recent = [d for d in dates if d >= cutoff]
        if len(recent) >= 3:
            rising.append((kw, len(recent)))
    return sorted(rising, key=lambda x: -x[1])


def show_list(articles, reviewed_indices):
    rising = get_rising_themes()

    print(f"\n{'='*70}")
    print(f"  CONTENT REVIEW -- {date.today().isoformat()}")
    print(f"  {len(articles)} articles  |  {len(reviewed_indices)} reviewed this session")
    print(f"{'='*70}")

    if rising:
        print("\n  RISING THEMES (3+ articles this week):")
        for kw, n in rising[:5]:
            print(f"    * {kw}  ({n} articles)")

    print(f"\n  {'#':<5} {'SCORE':<9} {'AGE':<6} {'PILLAR':<22} TITLE")
    print(f"  {'-'*5} {'-'*9} {'-'*6} {'-'*22} {'-'*32}")

    for i, a in enumerate(articles, 1):
        pillar = (a.get("pillar") or "--")[:20]
        title = a["title"]
        if len(title) > 38:
            title = title[:35] + "..."
        done = ">" if (i - 1) in reviewed_indices else " "
        bar = score_bar(a["score"])
        age = format_age(a.get("published", ""))
        print(f"  {done}{i:<4} {bar:<9} {age:<6} {pillar:<22} {title}")

    print()


def parse_selection(raw, total):
    indices = set()
    for part in raw.replace(" ", "").split(","):
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                for n in range(int(lo), int(hi) + 1):
                    if 1 <= n <= total:
                        indices.add(n - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if 1 <= n <= total:
                    indices.add(n - 1)
            except ValueError:
                pass
    return sorted(indices)


def get_angle(article, model_ok):
    if not model_ok:
        return (
            f"Review this article for a post angle: {article['title'][:80]}",
            article["title"][:60],
        )
    prompt = ANGLE_PROMPT.format(
        title=article["title"],
        summary=article.get("snippet", "")[:200],
    )
    try:
        resp = requests.post(
            MODEL_URL,
            json={"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]

        parsed = None
        try:
            parsed = json.loads(raw.strip())
        except Exception:
            match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                except Exception:
                    pass

        if parsed and "angle" in parsed:
            return str(parsed.get("angle", "")).strip(), str(parsed.get("hook", "")).strip()
    except Exception:
        pass

    return (
        f"Engineering perspective on: {article['title'][:80]}",
        article["title"][:60],
    )


def review_article(article, num, model_ok, selection_log):
    print(f"\n{'='*70}")
    print(f"  ARTICLE {num}")
    print(f"  Title  : {article['title']}")
    print(f"  Source : {article['source']}")
    print(f"  Age    : {format_age(article.get('published', ''))}  ({article.get('published', 'unknown')[:16]})")
    print(f"  Pillar : {article.get('pillar') or '--'}")
    print(f"  Score  : {article.get('score', 0)}")
    print(f"  URL    : {article['url']}")
    if article.get("snippet"):
        print(f"  Excerpt: {article['snippet'][:240]}...")
    print()
    print("  Getting angle...", end="", flush=True)
    angle, hook = get_angle(article, model_ok)
    print(f"\r  Angle  : {angle}")
    print(f"  Hook   : {hook}")
    print()

    while True:
        choice = input("  [d]raft  [s]kip  [h]old  [?]back to list  -> ").strip().lower()
        if choice in ("d", "s", "h", "?"):
            break
        print("  Enter d, s, h, or ?")

    if choice == "?":
        return None

    status = {"d": "draft", "s": "skip", "h": "hold"}[choice]

    selection_log.append({
        "date": date.today().isoformat(),
        "article_title": article["title"],
        "source": article.get("source", ""),
        "pillar": article.get("pillar", ""),
        "score": article.get("score", 0),
        "keywords_matched": article.get("keywords_matched", []),
        "action": status,
    })

    article["angle"] = angle
    article["hook"] = hook
    return {**article, "status": status, "reviewed_at": datetime.now().isoformat()}


def check_model():
    base = MODEL_URL.split("/api/")[0]
    try:
        requests.get(f"{base}/api/tags", timeout=5).raise_for_status()
        return True
    except Exception:
        print(f"  WARNING: Cannot reach local model at {MODEL_URL}")
        print("  Angles will not be generated. Check that Ollama is running.")
        print()
        return False


def load_queue():
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text())
    except Exception:
        return []


def save_queue(queue):
    QUEUE_PATH.write_text(json.dumps(queue, indent=2))


def load_selection_log():
    if not SELECTION_LOG_PATH.exists():
        return []
    try:
        return json.loads(SELECTION_LOG_PATH.read_text())
    except Exception:
        return []


def save_selection_log(log):
    SELECTION_LOG_PATH.write_text(json.dumps(log, indent=2))


def main():
    if not ARTICLES_PATH.exists():
        print("ERROR: data/articles.json not found. Run 01_fetch.py first.")
        sys.exit(1)

    articles = json.loads(ARTICLES_PATH.read_text())
    if not articles:
        print("No articles found. Run 01_fetch.py.")
        return

    model_ok = check_model()
    existing_queue = load_queue()
    selection_log = load_selection_log()
    existing_urls = {x["url"] for x in existing_queue}

    new_items = []
    reviewed_indices = set()

    # Show existing queue state if any
    on_hold = [x for x in existing_queue if x["status"] == "hold"]
    to_draft = [x for x in existing_queue if x["status"] == "draft"]
    if existing_queue:
        print(f"\n  Existing queue: {len(to_draft)} to draft, {len(on_hold)} on hold")

    show_list(articles, reviewed_indices)

    while True:
        raw = input("  Enter numbers to open (e.g. 1,3,5-8) or [q]uit -> ").strip().lower()

        if raw == "q":
            break

        if not raw:
            show_list(articles, reviewed_indices)
            continue

        indices = parse_selection(raw, len(articles))
        if not indices:
            print("  No valid numbers. Try again (e.g. 1 or 1,3 or 5-10).")
            continue

        for idx in indices:
            if idx in reviewed_indices:
                art = articles[idx]
                print(f"\n  #{idx+1} already reviewed this session.")
                continue

            result = review_article(articles[idx], idx + 1, model_ok, selection_log)

            if result is None:
                # '?' chosen — redraw list and break inner loop
                show_list(articles, reviewed_indices)
                break

            reviewed_indices.add(idx)
            if result["url"] not in existing_urls:
                new_items.append(result)
                existing_urls.add(result["url"])

    merged = existing_queue + new_items
    save_queue(merged)
    save_selection_log(selection_log)

    to_draft = [x for x in merged if x["status"] == "draft"]
    on_hold = [x for x in merged if x["status"] == "hold"]
    skipped = [x for x in merged if x["status"] == "skip"]

    print(f"\n{'='*70}")
    print(f"  Queue: {len(to_draft)} to draft  |  {len(on_hold)} on hold  |  {len(skipped)} skipped")
    if to_draft:
        print("  Run 04_draft.py to build your prompt files.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
