"""LiveCodeBench — via a third-party structured mirror, not the official
leaderboard.

Prior research was right that the official leaderboard (livecodebench.github.io)
has no clean API — but it turns out it does load a client-side JSON file
(`performances_generation.json`, found by inspecting leaderboard.html's
fetch() call). Checked it directly (2026-08-05): it's real and ~7MB, but it's
raw per-problem pass@1 data that stops at 2025-04-07 and only covers ~28
models up through O3/DeepSeek-R1-0528 — the official page has not been
re-run against current-generation models. The livecodebench/leaderboard HF
Space is similarly stale (a static space last modified 2024-06-07).

Instead, llm-stats.com's LiveCodeBench page (https://llm-stats.com/benchmarks/livecodebench)
is current — its own embedded JSON-LD metadata links a `contentUrl` of
`https://api.zeroeval.com/leaderboard/benchmarks/livecodebench/details`,
which is the actual JSON API backing that page. Confirmed live (2026-08-05):
`updated_at` on the payload is today, 73 models, includes current-generation
entries like DeepSeek-V4-Pro-Max. Caveat: llm-stats.com marks essentially all
of these `self_reported: true` / `verified: false` — these are aggregated
self-reported scores, not independently reproduced runs, so treat this as
directionally useful rather than authoritative.
"""

import requests

from normalize import make_record

URL = "https://api.zeroeval.com/leaderboard/benchmarks/livecodebench/details"


def fetch():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize(raw):
    records = []
    for m in raw.get("models", []):
        name = m.get("model_name")
        score = m.get("score")
        if not name or score is None:
            continue
        records.append(
            make_record(
                name=name,
                org=m.get("organization_name"),
                category="coding",
                score=round(score * 100, 2),
                score_type="livecodebench_pass_at_1",
                weight_availability="open" if m.get("is_open_source") else "closed",
                raw_payload={
                    "rank": m.get("rank"),
                    "verified": m.get("verified"),
                    "self_reported": m.get("self_reported"),
                    "announcement_date": m.get("announcement_date"),
                    "param_count": m.get("param_count"),
                    "provider_id": m.get("provider_id"),
                },
            )
        )
    return records
