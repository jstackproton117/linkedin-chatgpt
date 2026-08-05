"""Hugging Face Hub API — new/local/open-weight model discovery.

Confirmed live (2026-08-05): GET huggingface.co/api/models?sort=createdAt
(camelCase, not created_at)&direction=-1&filter=<pipeline_tag> works
unauthenticated, but sorting by pure recency is a firehose of test repos and
personal fine-tunes with zero engagement seconds after creation — a same-day
poll will always see 0 likes/downloads on them regardless of legitimacy.
Switched to sort=trendingScore instead, which surfaced real, notable models
(DeepSeek-V4, GLM-5.2, LiquidAI LFM2.5) in a live check — a much better
"worth surfacing today" signal than raw creation timestamp. This is a
discovery feed, not a benchmark, so scores are left null and score_type just
marks how the model was found.

Every kept model is recorded under its mapped task category AND under
local_open_weight, since discovery of "a new open-weight model exists" is
itself the signal for that category.
"""

import requests

from config import load_secrets
from normalize import make_record

PIPELINE_CATEGORY_MAP = {
    "text-generation": "chat_reasoning",
    "feature-extraction": "embeddings",
    "image-text-to-text": "ocr_document",
}

KEEP_PER_TAG = 15
FETCH_PER_TAG = 30


def fetch():
    token = load_secrets().get("hf_token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    results = {}
    for tag in PIPELINE_CATEGORY_MAP:
        resp = requests.get(
            "https://huggingface.co/api/models",
            params={"sort": "trendingScore", "direction": -1, "limit": FETCH_PER_TAG, "filter": tag},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        results[tag] = resp.json()
    return results


def normalize(raw):
    records = []
    for tag, models in raw.items():
        category = PIPELINE_CATEGORY_MAP[tag]
        kept = 0
        for m in models:
            if kept >= KEEP_PER_TAG:
                break
            likes = m.get("likes", 0) or 0
            downloads = m.get("downloads", 0) or 0
            if likes < 1 and downloads < 10:
                continue
            model_id = m.get("id") or m.get("modelId")
            if not model_id:
                continue
            kept += 1
            org = model_id.split("/")[0] if "/" in model_id else None
            payload = {
                "likes": likes,
                "downloads": downloads,
                "createdAt": m.get("createdAt"),
                "tags": m.get("tags"),
            }
            # createdAt is when this HF repo was created, which is a
            # reasonable proxy for release date but not exact — a
            # quantization/mirror repo's createdAt is when THAT repo went
            # up, not necessarily when the underlying model first shipped.
            release_date = m.get("createdAt")
            records.append(
                make_record(
                    name=model_id,
                    org=org,
                    category=category,
                    score=None,
                    score_type="hf_new_release",
                    weight_availability="open",
                    release_date=release_date,
                    raw_payload=payload,
                )
            )
            records.append(
                make_record(
                    name=model_id,
                    org=org,
                    category="local_open_weight",
                    score=None,
                    score_type="hf_new_release",
                    weight_availability="open",
                    release_date=release_date,
                    raw_payload=payload,
                )
            )
    return records
