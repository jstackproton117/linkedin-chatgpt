"""Arena (formerly LMArena/Chatbot Arena) — via the community JSON mirror at
api.wulong.dev, NOT the official Arena API (Arena.ai has no public API of its
own; this is an unofficial third-party mirror that scrapes/republishes it).

Confirmed live (2026-08-05): the bare
`GET /arena-ai-leaderboards/v1/leaderboard` endpoint from research 400s with
`{"error": "Missing required parameter: name"}` — it requires a `?name=`
query param naming one specific leaderboard slug, not a single combined feed.
Hitting the service root (`/arena-ai-leaderboards/`) returns a small
self-description listing the real endpoints, and
`/arena-ai-leaderboards/v1/leaderboards` (plural, no name param) lists all
current slugs with model counts, e.g. text, code, vision, agent, document,
text-to-image, etc. For this project's `chat_reasoning` category, "text" is
the matching general chat/reasoning slug (confirmed 20 models, scores ~1480-
1510, an Elo-like range consistent with Arena's actual scale).
"""

import requests

from normalize import make_record

LEADERBOARD_SLUG = "text"
URL = "https://api.wulong.dev/arena-ai-leaderboards/v1/leaderboard"


def fetch():
    resp = requests.get(URL, params={"name": LEADERBOARD_SLUG}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize(raw):
    records = []
    for m in raw.get("models", []):
        name = m.get("model")
        score = m.get("score")
        if not name or score is None:
            continue
        license_ = m.get("license")
        weight_availability = "open" if license_ == "open" else "closed" if license_ else None
        records.append(
            make_record(
                name=name,
                org=m.get("vendor"),
                category="chat_reasoning",
                score=score,
                score_type="arena_elo",
                weight_availability=weight_availability,
                raw_payload={
                    "rank": m.get("rank"),
                    "ci": m.get("ci"),
                    "votes": m.get("votes"),
                    "license": license_,
                },
            )
        )
    return records
