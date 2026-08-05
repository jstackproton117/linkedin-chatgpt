"""MTEB — via the leaderboard's own backend API, not the raw GitHub results
repo (which is a directory-per-model tree, impractical to poll daily at
GitHub's unauthenticated rate limit). Confirmed live (2026-08-05):
mteb-leaderboard-backend.hf.space has a documented FastAPI backend
(/openapi.json); the flagship English benchmark is "MTEB(eng, v2)" (179
models), only visible with include_hidden=true on the benchmark list.
"""

from urllib.parse import quote

import requests

from normalize import make_record

BENCHMARK_NAME = "MTEB(eng, v2)"
SCORES_URL = f"https://mteb-leaderboard-backend.hf.space/v1/benchmarks/{quote(BENCHMARK_NAME)}/scores"


def fetch():
    resp = requests.get(SCORES_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize(raw):
    records = []
    for row in raw.get("rows", []):
        model = row.get("model", {})
        name = model.get("name")
        mean_task = row.get("meanTask")
        if not name or mean_task is None:
            continue
        org = name.split("/")[0] if "/" in name else None
        records.append(
            make_record(
                name=name,
                org=org,
                category="embeddings",
                score=round(mean_task * 100, 2),
                score_type="mteb_eng_v2_mean_task",
                weight_availability="open" if model.get("openWeights") else "closed",
                modalities=model.get("modalities"),
                release_date=model.get("releaseDate"),
                raw_payload={"scoresByTaskType": row.get("scoresByTaskType")},
            )
        )
    return records
