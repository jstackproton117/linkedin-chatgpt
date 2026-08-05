"""Mark a draft as published and record the LinkedIn URL.

Run this after you post. It updates data/post_log.json with the publish date
and LinkedIn URL, which 06_log_metrics.py uses to match screenshots to posts.
"""

import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
POST_LOG_PATH = DATA / "post_log.json"


def load_log():
    if not POST_LOG_PATH.exists():
        print("ERROR: data/post_log.json not found.")
        print("Run 04_draft.py to create drafts first.")
        sys.exit(1)
    return json.loads(POST_LOG_PATH.read_text())


def save_log(log):
    POST_LOG_PATH.write_text(json.dumps(log, indent=2))


def main():
    log = load_log()
    unpublished = [x for x in log if x.get("published_at") is None]

    if not unpublished:
        print("No unpublished posts in the log.")
        print("All posts have been marked as published, or no drafts exist yet.")
        return

    print("\n" + "="*60)
    print("  LOG PUBLISHED POST")
    print("="*60)
    print("\nUnpublished drafts:\n")

    for i, post in enumerate(unpublished, 1):
        title = post["article_title"][:52]
        print(f"  {i}.  [{post['id']}]  {title}")

    print()
    raw = input("Select post number (or q to quit): ").strip().lower()
    if raw == "q":
        return

    try:
        idx = int(raw) - 1
        assert 0 <= idx < len(unpublished)
    except (ValueError, AssertionError):
        print("Invalid selection.")
        return

    post = unpublished[idx]
    today = date.today().isoformat()
    pub_date = input(f"Published date [{today}]: ").strip() or today
    linkedin_url = input("LinkedIn post URL (Enter to skip): ").strip() or None

    for entry in log:
        if entry["id"] == post["id"]:
            entry["published_at"] = pub_date
            entry["linkedin_url"] = linkedin_url
            break

    save_log(log)
    print(f"\nLogged: [{post['id']}] published on {pub_date}")
    if linkedin_url:
        print(f"URL: {linkedin_url}")
    print("Run 06_log_metrics.py after collecting your screenshot metrics.")


if __name__ == "__main__":
    main()
