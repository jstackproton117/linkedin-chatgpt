"""Record post performance metrics from a frontier model screenshot analysis.

Workflow:
  1. Screenshot your LinkedIn post (showing impressions, reactions, etc.)
  2. Open Claude.ai or any frontier model
  3. Paste the screenshot + the contents of prompts/extract_metrics_prompt.txt
  4. Copy the JSON output
  5. Run this script and paste the JSON when prompted

Metrics are stored in data/post_log.json against the correct post.
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
POST_LOG_PATH = DATA / "post_log.json"
PROMPT_PATH = BASE / "prompts" / "extract_metrics_prompt.txt"

EXPECTED_FIELDS = {"impressions", "reactions", "comments", "reposts", "post_date", "first_line"}


def load_log():
    if not POST_LOG_PATH.exists():
        print("ERROR: data/post_log.json not found.")
        print("Run 04_draft.py and 05_log_post.py first.")
        sys.exit(1)
    return json.loads(POST_LOG_PATH.read_text())


def save_log(log):
    POST_LOG_PATH.write_text(json.dumps(log, indent=2))


def read_pasted_json():
    """Read multiline pasted JSON. Blank line ends input."""
    print("  Paste the JSON (press Enter twice when done):\n")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines)


def main():
    log = load_log()
    published_no_metrics = [
        x for x in log
        if x.get("published_at") and x.get("metrics") is None
    ]

    if not published_no_metrics:
        print("No published posts without metrics.")
        print("Run 05_log_post.py to mark posts as published first.")
        return

    print("\n" + "="*60)
    print("  LOG POST METRICS")
    print("="*60)

    if PROMPT_PATH.exists():
        print(f"\n  Tip: use prompts/extract_metrics_prompt.txt with your screenshot.")

    print("\nPublished posts without metrics:\n")
    for i, post in enumerate(published_no_metrics, 1):
        pub = post.get("published_at", "unknown")
        title = post["article_title"][:48]
        print(f"  {i}.  {pub}  {title}")

    print()
    raw = input("Select post number (or q to quit): ").strip().lower()
    if raw == "q":
        return

    try:
        idx = int(raw) - 1
        assert 0 <= idx < len(published_no_metrics)
    except (ValueError, AssertionError):
        print("Invalid selection.")
        return

    post = published_no_metrics[idx]
    print(f"\nPost: {post['article_title']}")
    print(f"Published: {post.get('published_at')}\n")

    raw_json = read_pasted_json()

    try:
        metrics = json.loads(raw_json.strip())
    except json.JSONDecodeError as e:
        print(f"\nERROR: Invalid JSON -- {e}")
        print("Copy the JSON from the frontier model and try again.")
        return

    missing = EXPECTED_FIELDS - set(metrics.keys())
    if missing:
        print(f"  Note: missing fields ({', '.join(sorted(missing))}) -- stored as-is")

    for entry in log:
        if entry["id"] == post["id"]:
            entry["metrics"] = metrics
            break

    save_log(log)

    print(f"\nMetrics saved for [{post['id']}].")
    for field in ("impressions", "reactions", "comments", "reposts"):
        val = metrics.get(field)
        if val is not None:
            print(f"  {field}: {val}")


if __name__ == "__main__":
    main()
