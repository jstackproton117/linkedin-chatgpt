"""OCR & document conversion — IDP Leaderboard (idp-leaderboard.org, run by
Nanonets), not olmOCR-bench or the OmniDocBench repo directly.

Checked all three candidates from the plan live (2026-08-05):

1. `allenai/olmOCR-bench` on the HF datasets API is genuinely just the raw
   eval set — 1,403 PDFs + ~7,010 unit-test cases (`bench_data/*.jsonl`).
   No per-model scores anywhere in the dataset itself. Not usable as a
   source of model scores.
2. `opendatalab/OmniDocBench`'s GitHub repo is the evaluation *code*, not
   published results — its README has no score table, and it points users
   at a local notebook (`tools/generate_result_tables.ipynb`) to generate
   their own leaderboard after running the eval themselves. Not fetchable
   as-is.
3. `idp-leaderboard.org` (redirects to `www.idp-leaderboard.org`) turned out
   to be the one that actually works, but not the obvious way. It's a
   Next.js app with no `/api/*` route that returns data (confirmed several
   candidate paths all 404). Its own `schema.org Dataset` JSON-LD claims a
   `contentUrl` of `https://idp-leaderboard.nanonets.com/api/leaderboard`,
   but that hostname doesn't even resolve — aspirational/stale metadata,
   not a real endpoint. The `NanoNets/idp-leaderboard-benchmarks` GitHub
   repo is only the eval *pipeline* (empty `caches/` dir, no committed
   results).
   What actually works: the site's homepage JS bundle bakes the full
   leaderboard in directly at build time as `JSON.parse('[...]')` inside
   one of the `/_next/static/chunks/*.js` files (confirmed by grepping a
   downloaded chunk for a model name from the page's own FAQ text and
   finding a literal `[{"model_name":"Nanonets OCR-3",...,"scores":{...}}]`
   array — 29 models, three sub-benchmarks each: olmOCR (OCR fidelity),
   OmniDocBench (document parsing), and "idp" (IDP Core: KIE/OCR/table/VQA).
   Since the chunk filename is a content hash that changes on redeploy,
   fetch() re-discovers it each run: pull the homepage HTML, walk its
   `/_next/static/chunks/*.js` script tags, and scan each chunk's text for
   the embedded array until found. This is inherently coupled to their
   current bundler output shape (Turbopack) — if they switch to real
   server-side data fetching this will need rework, but as of this check
   it's real, current (release dates up to 2026-03), structured data with
   no headless browser needed.
"""

import json
import re

import requests

from normalize import make_record

BASE_URL = "https://www.idp-leaderboard.org"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ModelCantina/1.0)"}

_CHUNK_RE = re.compile(r"/_next/static/chunks/[a-zA-Z0-9]+\.js")
_JSON_PARSE_PREFIX = "JSON.parse('"


def _extract_js_string(js_text, start):
    """Walk a JS single-quoted string literal starting right after its
    opening quote, honoring backslash escapes, and return the raw
    (still-escaped) contents up to the closing quote."""
    buf = []
    i = start
    n = len(js_text)
    while i < n:
        ch = js_text[i]
        if ch == "\\":
            buf.append(js_text[i:i + 2])
            i += 2
            continue
        if ch == "'":
            break
        buf.append(ch)
        i += 1
    return "".join(buf)


def _find_embedded_models(js_text):
    idx = js_text.find("model_name")
    if idx == -1:
        return None
    call_start = js_text.rfind(_JSON_PARSE_PREFIX, 0, idx)
    if call_start == -1:
        return None
    raw = _extract_js_string(js_text, call_start + len(_JSON_PARSE_PREFIX))
    unescaped = raw.replace("\\'", "'").replace("\\\\", "\\")
    return json.loads(unescaped)


def fetch():
    resp = requests.get(BASE_URL + "/", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    chunk_paths = sorted(set(_CHUNK_RE.findall(resp.text)))
    for path in chunk_paths:
        chunk_resp = requests.get(BASE_URL + path, headers=HEADERS, timeout=30)
        if chunk_resp.status_code != 200:
            continue
        models = _find_embedded_models(chunk_resp.text)
        if models:
            return models
    raise RuntimeError(
        "IDP Leaderboard: could not find the embedded model-scores array in "
        f"any of {len(chunk_paths)} homepage JS chunks — page bundling may "
        "have changed."
    )


def normalize(raw):
    records = []
    for model in raw:
        name = model.get("model_name")
        if not name:
            continue
        org = model.get("company")
        model_type = model.get("type")
        weight_availability = (
            "open" if model_type == "open" else "closed" if model_type == "closed" else None
        )
        for benchmark_key, benchmark_scores in (model.get("scores") or {}).items():
            overall = benchmark_scores.get("overall") if isinstance(benchmark_scores, dict) else None
            if overall is None:
                continue
            records.append(
                make_record(
                    name=name,
                    org=org,
                    category="ocr_document",
                    score=overall,
                    score_type=f"idp_leaderboard_{benchmark_key}_overall",
                    weight_availability=weight_availability,
                    release_date=model.get("release_date"),
                    raw_payload={
                        "slug": model.get("slug"),
                        "benchmark": benchmark_key,
                        "detail": benchmark_scores,
                        "cost_per_1k": model.get("cost_per_1k"),
                    },
                )
            )
    return records
