"""Stage 8: Sync Buffer -> post_log. Runs daily after rank (non-fatal).

QUOTA DESIGN (Buffer Free: 250 requests / 24h, 3,000 / 30 days):
  * ONE batched `posts` call per run fetches status + metrics for every
    post Buffer sent or has queued in the last `metrics_window_days`. Cost
    does not grow with the number of posts. A second page is fetched only
    if Buffer says there is one.
  * Posts older than the window are FROZEN: never fetched again. Metrics
    plateau within weeks; refreshing a 4-month-old post daily is waste.
  * A single-post fallback fetch happens only for a linked post that is
    inside the window yet missing from the batch (should not happen).
  * The client enforces the self-imposed budget in settings.json before
    every call; hitting it ends the run cleanly rather than 429-ing.

For every post_log entry with buffer_post_id:
  * status         -> buffer_status; `sent` sets published_at from Buffer's
                      real sentAt and linkedin_url from externalLink.
  * error          -> buffer_error, printed loudly.
  * metrics        -> post_log.metrics (impressions, reach, reactions,
                      comments, reposts, clicks, engagement_rate, viewers)
                      and, when Buffer's metricsUpdatedAt changed, a snapshot
                      appended to data/metrics_history.jsonl.

Usage:  python 08_sync_buffer.py [--dry-run]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from buffer_client import (Buffer, BufferError, BudgetExceeded, load_config,  # noqa: E402
                           metrics_to_dict, usage_summary)

BASE = Path(__file__).parent
DATA = BASE / "data"
POST_LOG_PATH = DATA / "post_log.json"
HISTORY_PATH = DATA / "metrics_history.jsonl"
SETTINGS_PATH = BASE / "settings.json"

ACTIVE_STATUSES = ["sent", "scheduled", "needs_approval", "sending", "error", "draft"]

METRIC_MAP = {
    "impressions": "impressions", "reactions": "reactions", "comments": "comments",
    "reposts": "reposts", "shares": "shares", "reach": "reach", "clicks": "clicks",
    "engagementRate": "engagement_rate", "viewers": "viewers", "views": "views",
    "totalTimeWatched": "total_time_watched",
}


def settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("buffer", {})
    except Exception:
        return {}


def parse_iso(iso):
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def apply_post(entry, post, tz, now_iso, snapshots, newly_published, errors):
    """Fold one Buffer post into its post_log entry. Returns #fields changed."""
    changed = 0
    status = post.get("status")
    if status != entry.get("buffer_status"):
        entry["buffer_status"] = status
        changed += 1
    if status == "error":
        err = post.get("error") or {}
        msg = err.get("message") or err.get("rawError") or "unknown error"
        if entry.get("buffer_error") != msg:
            entry["buffer_error"] = msg
            changed += 1
        errors.append((entry["id"], msg))
    if status == "sent":
        sent_dt = parse_iso(post.get("sentAt") or post.get("dueAt"))
        if sent_dt and not entry.get("published_at"):
            entry["published_at"] = sent_dt.astimezone(tz).date().isoformat()
            entry["published_via"] = "buffer"
            newly_published.append(entry["id"])
            changed += 1
        if sent_dt and not entry.get("buffer_sent_at"):
            entry["buffer_sent_at"] = sent_dt.isoformat()
            changed += 1
        link = post.get("externalLink")
        if link and not entry.get("linkedin_url"):
            entry["linkedin_url"] = link
            changed += 1
    m = metrics_to_dict(post.get("metrics"))
    updated_at = post.get("metricsUpdatedAt")
    if m and updated_at and updated_at != entry.get("metrics_updated_at"):
        mapped = {METRIC_MAP[k]: v for k, v in m.items() if k in METRIC_MAP}
        merged = dict(entry.get("metrics") or {})
        merged.update(mapped)
        merged["source"] = "buffer"
        merged["updated_at"] = updated_at
        entry["metrics"] = merged
        entry["metrics_updated_at"] = updated_at
        changed += 1
        snapshots.append({"post_id": entry["id"], "buffer_post_id": entry["buffer_post_id"],
                          "captured_at": now_iso, "metrics_updated_at": updated_at,
                          "published_at": entry.get("published_at"), "metrics": mapped})
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except BufferError as e:
        print(f"  Buffer: {e}"); return
    if not cfg:
        print("  Buffer: not configured (no key in buffer_config.json) -- skipping"); return
    if not cfg.get("channel_id"):
        print("  Buffer: no channel_id yet -- run check_buffer.py first"); return

    s = settings()
    tz = ZoneInfo(s.get("timezone", "America/New_York"))
    window_days = int(s.get("metrics_window_days", 60))
    batch_size = int(s.get("batch_size", 50))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    window_start = now - timedelta(days=window_days)

    post_log = json.loads(POST_LOG_PATH.read_text(encoding="utf-8"))
    linked = [p for p in post_log if p.get("buffer_post_id")]
    print(f"  Buffer: {len(linked)} post(s) linked")
    if not linked:
        return

    b = Buffer(cfg)
    calls_before = usage_summary()["calls_24h"]

    # Freeze first: anything sent before the window is done, no request needed.
    frozen_now = 0
    active = []
    for e in linked:
        if e.get("metrics_frozen"):
            continue
        sent_dt = parse_iso(e.get("buffer_sent_at")) or (
            parse_iso(e["published_at"] + "T12:00:00+00:00") if e.get("published_at") else None)
        if sent_dt and sent_dt < window_start and e.get("metrics"):
            e["metrics_frozen"] = True
            frozen_now += 1
        else:
            active.append(e)
    if frozen_now:
        print(f"  Buffer: froze {frozen_now} post(s) older than {window_days} days (no further fetches)")
    if not active:
        print("  Buffer: nothing active inside the window — no requests made")
        if not args.dry_run and frozen_now:
            POST_LOG_PATH.write_text(json.dumps(post_log, indent=2), encoding="utf-8")
        return

    # The one batched request (plus a second page only if Buffer has one).
    try:
        batch = b.posts_all(cfg["organization_id"], cfg["channel_id"], ACTIVE_STATUSES,
                            extra_fields=Buffer.POST_EXTRA,
                            due_start=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            page_size=batch_size, max_pages=3)
    except BudgetExceeded as e:
        print(f"  Buffer: {e} — stopping for today"); return
    except BufferError as e:
        print(f"  Buffer: batch fetch failed -- {e}"); return
    by_id = {p["id"]: p for p in batch}
    print(f"  Buffer: batch returned {len(batch)} post(s) within {window_days} days")

    changed = 0
    snapshots, newly_published, errors = [], [], []
    fallback = 0
    for e in active:
        post = by_id.get(e["buffer_post_id"])
        if post is None:
            # Inside the window but not in the batch: a queued post with a
            # dueAt outside the range, or something odd. One targeted fetch.
            try:
                post = b.get_post(e["buffer_post_id"], extra_fields=Buffer.POST_EXTRA)
                fallback += 1
            except BudgetExceeded as ex:
                print(f"  Buffer: {ex} — stopping"); break
            except BufferError as ex:
                print(f"    {e['id']}: fetch failed -- {ex}"); continue
            if not post:
                if e.get("buffer_status") != "missing":
                    e["buffer_status"] = "missing"; changed += 1
                print(f"    {e['id']}: Buffer post {e['buffer_post_id']} not found (deleted in Buffer?)")
                continue
        changed += apply_post(e, post, tz, now_iso, snapshots, newly_published, errors)

    if args.dry_run:
        print(f"  DRY RUN: {changed} change(s), {len(snapshots)} snapshot(s) — nothing written")
    else:
        if changed or frozen_now:
            POST_LOG_PATH.write_text(json.dumps(post_log, indent=2), encoding="utf-8")
        if snapshots:
            with HISTORY_PATH.open("a", encoding="utf-8") as f:
                for snap in snapshots:
                    f.write(json.dumps(snap) + "\n")

    for pid in newly_published:
        print(f"    {pid}: SENT by Buffer — marked published")
    for pid, msg in errors:
        print(f"    {pid}: BUFFER ERROR — {msg}")
    with_metrics = sum(1 for p in post_log if (p.get("metrics") or {}).get("impressions") is not None)
    u = usage_summary()
    print(f"  Buffer: {changed} field update(s), {len(snapshots)} snapshot(s), {fallback} fallback fetch(es); "
          f"{with_metrics} post(s) with impressions")
    reported = ""
    if u.get("reported_24h") is not None:
        reported = f" · Buffer reports {u['reported_24h']}/{u['plan_24h']} today, {u['reported_30d']}/{u['plan_30d']} this month"
    print(f"  Buffer usage: {u['ledger_24h'] - calls_before} call(s) this run · "
          f"budget {u['calls_24h']}/{u['daily_budget']} today, "
          f"{u['calls_30d']}/{u['monthly_budget']} this month{reported}")


if __name__ == "__main__":
    main()
