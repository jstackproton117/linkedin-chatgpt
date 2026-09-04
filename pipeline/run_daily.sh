#!/usr/bin/env bash
# Rebel Intel — daily fetch + rank.
#
# Runs 01_fetch.py then 07_rank.py, so the Review page has a fresh, ranked
# list waiting before the 08:00 notify email goes out.
#
# Ranking is best-effort: 07_rank.py fails safe and leaves the keyword
# ordering from 01_fetch.py in place if the local models are unreachable, so
# a rank failure never leaves Joe without a review list. A fetch failure is
# fatal for the run — there is nothing to rank.
#
# Installed as a cron entry; see `crontab -l`.

set -uo pipefail

BASE="/home/joe/rebel-intel"
PY="$BASE/.venv/bin/python"
LOG_DIR="$BASE/logs"
LOG="$LOG_DIR/daily.log"
LOCK="$BASE/.daily.lock"
MAX_LOG_BYTES=$((2 * 1024 * 1024))

mkdir -p "$LOG_DIR"

# Rotate before writing so the log cannot grow without bound.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Never let a cron run overlap a manual fetch from the dashboard, or a
# previous run that is still going. -n = fail immediately rather than queue.
exec 9>"$LOCK"
if ! flock -n 9; then
    log "SKIP — another fetch/rank run holds the lock"
    exit 0
fi

cd "$BASE" || { log "FATAL — cannot cd to $BASE"; exit 1; }

log "=== daily run starting ==="

start=$(date +%s)
if ! "$PY" "$BASE/01_fetch.py" >> "$LOG" 2>&1; then
    log "FATAL — 01_fetch.py failed, skipping rank"
    exit 1
fi
log "fetch done in $(( $(date +%s) - start ))s"

start=$(date +%s)
if ! "$PY" "$BASE/07_rank.py" >> "$LOG" 2>&1; then
    log "WARN — 07_rank.py failed; keyword ordering from fetch is still in place"
    exit 0
fi
log "rank done in $(( $(date +%s) - start ))s"

count=$("$PY" -c "import json;print(len(json.load(open('$BASE/data/articles.json'))))" 2>/dev/null || echo "?")
log "=== daily run complete — $count articles ready for review ==="
