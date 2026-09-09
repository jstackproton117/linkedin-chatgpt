"""Stage 8: Sync Buffer -> post_log. Runs daily after rank (non-fatal).

For every post_log entry that was sent to Buffer (has buffer_post_id):
  * status         -> buffer_status; `sent` sets published_at from Buffer's
                      real sentAt and linkedin_url from externalLink. That is
                      a confirmation, not a guess — the old "date passed, so
                      mark it published" bug is structurally impossible here.
  * error          -> buffer_error, printed loudly so it is not missed.
  * metrics        -> post_log.metrics (impressions, reach, reactions,
                      comments, reposts, clicks, engagement_rate, viewers), and
                      a daily snapshot appended to data/metrics_history.jsonl
                      so day-1 / day-3 / day-7 curves exist. LOG EVERYTHING.

Buffer refreshes metrics from LinkedIn once a day; a post's first numbers
appear ~24h after it is sent. Manual metrics (06_log_metrics.py) remain the
fallback for posts Buffer did not send.

Usage:  python 08_sync_buffer.py [--dry-run]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from buffer_client import Buffer, BufferError, load_config, metrics_to_dict  # noqa: E402

BASE = Path(__file__).parent
DATA = BASE / "data"
POST_LOG_PATH = DATA / "post_log.json"
HISTORY_PATH = DATA / "metrics_history.jsonl"
SETTINGS_PATH = BASE / "settings.json"

# Buffer PostMetricType -> the post_log.metrics keys the pipeline already uses,
# plus a few extras worth keeping.
METRIC_MAP = {
    "impressions": "impressions",
    "reactions": "reactions",
    "comments": "comments",
    "reposts": "reposts",
    "shares": "shares",
    "reach": "reach",
    "clicks": "clicks",
    "engagementRate": "engagement_rate",
    "viewers": "viewers",
    "views": "views",
    "totalTimeWatched": "total_time_watched",
}


def local_tz():
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return ZoneInfo(settings.get("buffer", {}).get("timezone", "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def iso_to_local_date(iso, tz):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date().isoformat()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except BufferError as e:
        print(f"  Buffer: {e}")
        return
    if not cfg:
        print("  Buffer: not configured (no key in buffer_config.json) -- skipping")
        return
    if not cfg.get("channel_id"):
        print("  Buffer: no channel_id yet -- run check_buffer.py first")
        return

    b = Buffer(cfg)
    tz = local_tz()
    try:
        post_log = json.loads(POST_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        print("  Buffer: post_log.json unreadable")
        return

    linked = [p for p in post_log if p.get("buffer_post_id")]
    print(f"  Buffer: {len(linked)} post(s) linked to Buffer")
    if not linked:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    changed = 0
    snapshots = []
    newly_published = []
    errors = []

    for entry in linked:
        try:
            post = b.get_post(entry["buffer_post_id"], extra_fields=Buffer.POST_EXTRA)
        except BufferError as e:
            print(f"    {entry['id']}: could not fetch Buffer post -- {e}")
            continue
        if not post:
            print(f"    {entry['id']}: Buffer post {entry['buffer_post_id']} not found (deleted in Buffer?)")
            if entry.get("buffer_status") != "missing":
                entry["buffer_status"] = "missing"
                changed += 1
            continue

        status = post.get("status")
        if status != entry.get("buffer_status"):
            entry["buffer_status"] = status
            changed += 1

        if status == "error":
            msg = ((post.get("error") or {}).get("message")
                   or (post.get("error") or {}).get("rawError") or "unknown error")
            if entry.get("buffer_error") != msg:
                entry["buffer_error"] = msg
                changed += 1
            errors.append((entry["id"], msg))

        if status == "sent":
            sent_date = iso_to_local_date(post.get("sentAt") or post.get("dueAt") or "", tz)
            if sent_date and not entry.get("published_at"):
                entry["published_at"] = sent_date
                entry["published_via"] = "buffer"
                newly_published.append(entry["id"])
                changed += 1
            link = post.get("externalLink")
            if link and not entry.get("linkedin_url"):
                entry["linkedin_url"] = link
                changed += 1

        m = metrics_to_dict(post.get("metrics"))
        updated_at = post.get("metricsUpdatedAt")
        if m and updated_at and updated_at != entry.get("metrics_updated_at"):
            mapped = {METRIC_MAP[k]: v for k, v in m.items() if k in METRIC_MAP}
            # Buffer numbers replace manual ones, but never wipe a manual
            # screenshot entry's extra fields (first_line, post_date).
            merged = dict(entry.get("metrics") or {})
            merged.update(mapped)
            merged["source"] = "buffer"
            merged["updated_at"] = updated_at
            entry["metrics"] = merged
            entry["metrics_updated_at"] = updated_at
            changed += 1
            snapshots.append({
                "post_id": entry["id"],
                "buffer_post_id": entry["buffer_post_id"],
                "captured_at": now_iso,
                "metrics_updated_at": updated_at,
                "published_at": entry.get("published_at"),
                "metrics": mapped,
            })

    if args.dry_run:
        print(f"  DRY RUN: {changed} change(s), {len(snapshots)} snapshot(s) — nothing written")
    else:
        if changed:
            POST_LOG_PATH.write_text(json.dumps(post_log, indent=2), encoding="utf-8")
        if snapshots:
            with HISTORY_PATH.open("a", encoding="utf-8") as f:
                for s in snapshots:
                    f.write(json.dumps(s) + "\n")

    for pid in newly_published:
        print(f"    {pid}: SENT by Buffer — marked published")
    for pid, msg in errors:
        print(f"    {pid}: BUFFER ERROR — {msg}")
    with_metrics = sum(1 for p in post_log if p.get("metrics") and p["metrics"].get("impressions") is not None)
    print(f"  Buffer: {changed} field update(s), {len(snapshots)} metric snapshot(s); "
          f"{with_metrics} post(s) now have impressions")


if __name__ == "__main__":
    main()
