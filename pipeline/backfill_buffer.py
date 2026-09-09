#!/usr/bin/env python3
"""Link post_log entries to posts Buffer already sent, so their metrics and
LinkedIn permalinks flow in through 08_sync_buffer.py.

Joe had been scheduling through Buffer's UI before the pipeline knew about
it, so Buffer holds sent posts the post_log only knows by hand-entered date
and (sometimes) URL. Matching, most to least certain:

  1. URL      linkedin_url on the entry == Buffer's externalLink
  2. date+text  same local send date AND the draft body's opening resembles
                Buffer's post text (difflib ratio >= 0.55)

Anything weaker is listed as unmatched for a human decision. Dry-run by
default; --apply writes buffer_post_id (and nothing else — the sync script
fills status, url and metrics on its next run).

Usage:  python backfill_buffer.py [--apply]
"""

import argparse
import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from buffer_client import Buffer, BufferError, load_config  # noqa: E402

BASE = Path(__file__).parent
DATA = BASE / "data"
POST_LOG_PATH = DATA / "post_log.json"
DRAFTS_DIR = DATA / "drafts"
SETTINGS_PATH = BASE / "settings.json"


def extract_post_body(content):
    """Same rule app.py uses to turn a draft file into post text."""
    lines = content.splitlines()
    dividers = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dividers) >= 2:
        body = lines[dividers[0] + 1: dividers[1]]
    elif len(dividers) == 1:
        body = lines[dividers[0] + 1:]
    else:
        start = 0
        while start < len(lines) and (lines[start].startswith("#") or not lines[start].strip()):
            start += 1
        body = lines[start:]
    out = []
    for l in body:
        if l.strip().startswith("Sources:") or l.strip().startswith("Character count"):
            break
        out.append(l)
    return "\n".join(out).strip()


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def share_id(url):
    m = re.search(r"urn:li:(?:share|ugcPost|activity):(\d+)", url or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write buffer_post_id links")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg or not cfg.get("channel_id"):
        print("Buffer not configured — run check_buffer.py first"); sys.exit(1)
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        tz = ZoneInfo(settings.get("buffer", {}).get("timezone", "America/New_York"))
    except Exception:
        tz = ZoneInfo("America/New_York")

    b = Buffer(cfg)
    try:
        sent = b.posts(cfg["organization_id"], cfg["channel_id"], status="sent", first=50,
                       extra_fields=("status", "sentAt", "externalLink"))
    except BufferError as e:
        print("Buffer error:", e); sys.exit(1)
    post_log = json.loads(POST_LOG_PATH.read_text(encoding="utf-8"))

    def local_date(iso):
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone(tz).date().isoformat()
        except Exception:
            return None

    already = {p["buffer_post_id"] for p in post_log if p.get("buffer_post_id")}
    candidates = [p for p in sent if p["id"] not in already]
    unlinked = [e for e in post_log if not e.get("buffer_post_id")]
    print(f"Buffer sent posts: {len(sent)} ({len(candidates)} not yet linked)")
    print(f"post_log entries without a Buffer link: {len(unlinked)}")
    print()

    matches = []   # (entry, post, how, score)
    used = set()

    # Pass 1 — URL
    for e in unlinked:
        sid = share_id(e.get("linkedin_url"))
        if not sid:
            continue
        for p in candidates:
            if p["id"] in used:
                continue
            if share_id(p.get("externalLink")) == sid:
                matches.append((e, p, "url", 1.0)); used.add(p["id"]); break

    matched_entries = {id(e) for e, *_ in matches}

    # Pass 2 — same local date + text similarity. The date is published_at
    # when Joe marked it, otherwise scheduled_for: several posts went out via
    # Buffer's UI and were never marked published in the dashboard, which is
    # precisely the gap this backfill closes.
    for e in unlinked:
        e_date = (e.get("published_at") or e.get("scheduled_for") or "")[:10]
        if id(e) in matched_entries or not e_date:
            continue
        draft = DRAFTS_DIR / (e.get("draft_file") or "")
        body = extract_post_body(draft.read_text(encoding="utf-8")) if draft.exists() else ""
        head = norm(body)[:160]
        best = None
        for p in candidates:
            if p["id"] in used:
                continue
            if local_date(p.get("sentAt") or p.get("dueAt") or "") != e_date:
                continue
            ratio = difflib.SequenceMatcher(None, head, norm(p.get("text"))[:160]).ratio() if head else 0.0
            if best is None or ratio > best[1]:
                best = (p, ratio)
        if best and best[1] >= 0.55:
            matches.append((e, best[0], "date+text", round(best[1], 2))); used.add(best[0]["id"])
        elif best:
            print(f"  ? {e['id']}  same day as Buffer post {best[0]['id']} but text ratio only {best[1]:.2f} — not linking")

    print()
    print(f"{len(matches)} match(es):")
    for e, p, how, score in matches:
        print(f"  {e['id']:<16} <- {p['id']}  [{how} {score}]  sent {local_date(p.get('sentAt') or '')}  "
              f"{(p.get('text') or '')[:50]!r}")

    leftover = [p for p in candidates if p["id"] not in used]
    if leftover:
        print()
        print(f"{len(leftover)} Buffer post(s) with no post_log entry (posted outside the pipeline):")
        for p in leftover:
            print(f"  {p['id']}  sent {local_date(p.get('sentAt') or '')}  {(p.get('text') or '')[:60]!r}")

    if not args.apply:
        print()
        print("Dry run. Re-run with --apply to write the links, then run 08_sync_buffer.py.")
        return
    for e, p, how, score in matches:
        e["buffer_post_id"] = p["id"]
        e["buffer_link_method"] = how
        e["buffer_linked_at"] = datetime.now(timezone.utc).isoformat()
    POST_LOG_PATH.write_text(json.dumps(post_log, indent=2), encoding="utf-8")
    print()
    print(f"Wrote {len(matches)} link(s) to post_log.json. Now run: .venv/bin/python 08_sync_buffer.py")


if __name__ == "__main__":
    main()
