"""SWE-bench Verified — official structured JSON, no scraping.

Real file location confirmed live (2026-08-05): the repo's default branch is
`master`, not `main`, and the actual data file is `info_for_leaderboard.json`
(per-instance detail keyed by submission name), not `leaderboards.json` as
initially guessed from research — that path 404s. As of this check it holds
4 submissions x 500 instances, which matches SWE-bench *Verified* (the
curated 500-instance split), not Lite or Full.
"""

import requests

from normalize import make_record

URL = "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/info_for_leaderboard.json"


def fetch():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize(raw):
    records = []
    for name, instances in raw.items():
        total = len(instances)
        if total == 0:
            continue
        resolved = sum(1 for v in instances.values() if v.get("resolved"))
        rate = round(resolved / total * 100, 2)
        records.append(
            make_record(
                name=name,
                category="coding",
                score=rate,
                score_type="swebench_verified_resolve_rate",
                raw_payload={"total_instances": total, "resolved": resolved},
            )
        )
    return records
