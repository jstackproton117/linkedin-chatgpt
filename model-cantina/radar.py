#!/usr/bin/env python3
"""The Model Cantina — CLI entry point.

Usage:
    python radar.py poll [source_key]      # poll all sources, or just one
    python radar.py report [category]      # print a leaderboard table
    python radar.py note <model_name> <category> <rating> <text...>
    python radar.py stats                  # counts + source health
"""

import sys

import config
import poller
import storage


def cmd_poll(args):
    targets = [args[0]] if args else config.source_keys()
    print(f"Polling {len(targets)} source(s)...")
    summary = poller.poll_all(targets)
    for source_key in targets:
        result = summary[source_key]
        if "error" in result:
            print(f"  [{source_key}] FAILED — {result['error']}")
        else:
            print(f"  [{source_key}] ok — {result['new_models']} new models, "
                  f"{result['scores_recorded']} scores recorded")


def cmd_report(args):
    conn = storage.get_db()
    category = args[0] if args else None
    if category:
        categories = [category]
    else:
        categories = list(config.load_config()["categories"].keys())
    for cat in categories:
        rows = storage.get_category_leaderboard(conn, cat)
        print(f"\n=== {config.category_name(cat)} ===")
        if not rows:
            print("  (no scores yet)")
            continue
        for score_type, group_rows in rows:
            print(f"  -- {config.score_type_label(score_type)} --")
            for r in group_rows[:10]:
                score_str = f"{r['score']:>8}" if r["score"] is not None else " " * 7 + "—"
                print(f"  {score_str}  {r['model_name']:<35} [{r['source']}]")
    conn.close()


def cmd_note(args):
    if len(args) < 4:
        print("usage: python radar.py note <model_name> <category> <rating> <text...>")
        sys.exit(1)
    name, category, rating = args[0], args[1], args[2]
    text = " ".join(args[3:])
    valid_ratings = config.load_config()["manual_note_ratings"]
    if rating not in valid_ratings:
        print(f"rating must be one of: {', '.join(valid_ratings)}")
        sys.exit(1)
    conn = storage.get_db()
    model_id = storage.find_or_create_model_by_name(conn, name)
    storage.add_manual_note(conn, model_id, category, rating, text)
    print(f"Noted: {name} / {config.category_name(category)} / {rating}")
    conn.close()


def cmd_stats(args):
    conn = storage.get_db()
    stats = storage.get_stats(conn)
    print(f"Models: {stats['model_count']}")
    print(f"Scores: {stats['score_count']}")
    print(f"Manual notes: {stats['note_count']}")
    print("\nSource health:")
    for s in stats["sources"]:
        print(f"  [{s['tier']}] {s['source_key']:<22} {s['last_status']:<8} "
              f"{s['last_polled_at']}" + (f"  ({s['last_error']})" if s['last_error'] else ""))
    conn.close()


COMMANDS = {
    "poll": cmd_poll,
    "report": cmd_report,
    "note": cmd_note,
    "stats": cmd_stats,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
