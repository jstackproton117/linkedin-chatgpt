"""Artificial Analysis — chat/reasoning + coding leaderboard.

The plan flagged a real risk here: artificialanalysis.ai is a Next.js/React
app, and the full API needs a paid key. Checked empirically (2026-08-05)
rather than assuming either way:

- `curl -s https://artificialanalysis.ai` and the `/models` page are NOT
  client-only — they're server-rendered, and the homepage's React Server
  Component flight payload (`self.__next_f.push(...)`) does contain live
  numbers. But that flight-stream format is an internal Next.js wire format
  (numbered chunks of escaped, concatenated text), not something worth
  hand-parsing for a stable integration.
- Much better: every model/evaluation page also embeds one or more
  `<script type="application/ld+json">` blocks of `@type: "Dataset"`, each
  with a plain `"data": [{"label": ..., "<metric>": ..., "detailsUrl": ...}]`
  array. This is standard SEO structured data, not React internals, so it's
  stable to parse with a regex + json.loads and needs no headless browser
  or API key.
- https://artificialanalysis.ai/models has a "Artificial Analysis
  Intelligence Index" dataset (20 models, general capability composite —
  used here for chat_reasoning).
- https://artificialanalysis.ai/evaluations/terminalbench-v2-1 has a
  "Terminal-Bench v2.1: Score" dataset (20 models, an agentic
  coding/terminal-use benchmark — used here for coding).

So this source turned out fetchable for free after all — no paid key or
browser rendering needed. If artificialanalysis.ai ever drops these
ld+json blocks or renames the datasets, fetch() will raise (no matching
block found), which is the same clean "poll failed" outcome the plan
anticipated for the harder case.
"""

import json
import re

import requests

from normalize import make_record

# A bare/minimal User-Agent (or keep-alive reuse against this CDN) reliably
# hung until read-timeout in testing; a fuller browser-like header set with
# Connection: close fixed it immediately and consistently.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

# category -> (page to fetch, dataset "name" field in its ld+json blocks, our
# score_type label, multiplier to bring the raw metric onto a 0-100 scale)
# Intelligence Index is already reported ~0-100; Terminal-Bench v2.1 (like
# most of AA's individual eval datasets) reports a 0-1 resolve fraction.
TARGETS = {
    "chat_reasoning": (
        "https://artificialanalysis.ai/models",
        "Artificial Analysis Intelligence Index",
        "aa_intelligence_index",
        1,
    ),
    "coding": (
        "https://artificialanalysis.ai/evaluations/terminalbench-v2-1",
        "Terminal-Bench v2.1: Score",
        "aa_terminalbench_v2_1_score",
        100,
    ),
}

_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)


def _extract_dataset(html, dataset_name, source_url):
    for block in _LD_JSON_RE.findall(html):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if obj.get("@type") == "Dataset" and obj.get("name") == dataset_name:
            return obj
    raise RuntimeError(
        f"Artificial Analysis: no ld+json Dataset named {dataset_name!r} found "
        f"on {source_url} — page structure may have changed."
    )


def fetch():
    raw = {}
    for category, (url, dataset_name, _score_type, _scale) in TARGETS.items():
        resp = requests.get(url, headers=HEADERS, timeout=45)
        resp.raise_for_status()
        dataset = _extract_dataset(resp.text, dataset_name, url)
        raw[category] = dataset
    return raw


def normalize(raw):
    records = []
    for category, (_url, _dataset_name, score_type, scale) in TARGETS.items():
        dataset = raw.get(category)
        if not dataset:
            continue
        for item in dataset.get("data", []):
            label = item.get("label")
            if not label:
                continue
            metric_keys = [k for k in item if k not in ("label", "detailsUrl")]
            if not metric_keys:
                continue
            value = item.get(metric_keys[0])
            if value is None:
                continue
            records.append(
                make_record(
                    name=label,
                    category=category,
                    score=round(value * scale, 2),
                    score_type=score_type,
                    raw_payload={
                        "detailsUrl": item.get("detailsUrl"),
                        "metric": metric_keys[0],
                        "raw_value": value,
                    },
                )
            )
    return records
