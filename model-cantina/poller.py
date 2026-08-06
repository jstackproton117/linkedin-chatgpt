"""Concurrent source polling.

fetch()+normalize() per source is pure I/O-bound HTTP work with no DB
access, so it runs in a thread pool — wall-clock drops to roughly the
slowest single source instead of the sum of all of them. DB writes then
happen sequentially on the main thread against one connection (simpler than
reasoning about concurrent writes, and the wall-clock win is already
captured in the fetch step). Shared by radar.py's CLI `poll` command and
the Vercel cron endpoint (api/poll.py) — Thornwick's daily cron gets faster
for free, and it's what makes the Vercel version fit inside a serverless
function's execution time limit at all.
"""

import importlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import storage

SOURCE_MODULES = {
    "swebench": "sources.swebench",
    "mteb": "sources.mteb",
    "hf_hub": "sources.hf_hub",
    "artificial_analysis": "sources.artificial_analysis",
    "arena": "sources.arena",
    "livecodebench": "sources.livecodebench",
    "aider": "sources.aider",
    "ollama_library": "sources.ollama_library",
    "ocr_benchmarks": "sources.ocr_benchmarks",
    "safety_benchmarks": "sources.safety_benchmarks",
    "vals_classification": "sources.vals_classification",
    "browsecomp_plus": "sources.browsecomp_plus",
}


def _fetch_and_normalize(source_key):
    """Runs in a worker thread — fetch + normalize only, no DB access."""
    module_path = SOURCE_MODULES.get(source_key)
    if module_path is None:
        raise RuntimeError(f"no source module registered for {source_key!r}")
    module = importlib.import_module(module_path)
    raw = module.fetch()
    return module.normalize(raw)


def _expand_proxy_records(source_key, records):
    """config.yaml's proxy_for documents that a source's scores also stand
    in for another category (e.g. SWE-bench's coding scores as a proxy for
    software_architecture) — but normalize() only ever tags a record with
    the source's real category, nothing actually duplicates it into the
    proxy category. Without this, a proxy category with real sources
    configured just silently shows zero scores forever (found 2026-08-06:
    software_architecture and chunking_index had been empty since launch).
    Duplicates every record from this source into each of its proxy
    categories, unchanged except the category field."""
    proxy_for = config.source_config(source_key).get("proxy_for") or []
    if not proxy_for:
        return records
    expanded = list(records)
    for category in proxy_for:
        expanded.extend({**r, "category": category} for r in records)
    return expanded


def poll_all(source_keys=None, max_workers=8):
    """Concurrently fetch+normalize every requested source, then write
    results to the DB sequentially. Returns {source_key: result_dict} where
    result_dict is either {"new_models": N, "scores_recorded": M} (success)
    or {"error": str} (failure) — same shape either way, callers don't need
    to branch on success/failure to read a summary.
    """
    source_keys = list(source_keys or config.source_keys())
    conn = storage.get_db()
    storage.log_event(conn, "poll_started")
    conn.commit()

    fetch_results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_and_normalize, key): key for key in source_keys}
        for future in as_completed(futures):
            key = futures[future]
            try:
                fetch_results[key] = (True, future.result())
            except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
                fetch_results[key] = (False, e)

    summary = {}
    for key in source_keys:
        tier = config.source_config(key)["tier"]
        ok, payload = fetch_results[key]
        if ok:
            records = _expand_proxy_records(key, payload)
            summary[key] = storage.record_poll_results(conn, key, tier, records)
        else:
            storage.record_poll_failure(conn, key, tier, payload)
            summary[key] = {"error": str(payload)}

    conn.close()
    return summary
