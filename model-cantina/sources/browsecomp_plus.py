"""BrowseComp-Plus — research/web-search-and-synthesis benchmark, feeding
the new `research` category.

Added 2026-08-06 after Joe asked for a Research job type. Research (an
agent, background, verified live): Humanity's Last Exam is real multi-vendor
data but is closed-book trivia, not research/browsing — wrong fit. GAIA
measures stacked multi-model agent scaffolds, not individual models — not a
clean per-model comparison. Kaggle's FACTS Search V2 looked strongest
(standardized search tool given equally to every model, removing retriever-
quality as a confound) but its leaderboard data comes from an undocumented
internal RPC endpoint (api/i/benchmarks.BenchmarkService/
GetUnifiedBenchmarkLeaderboard) that returned 400 on every guessed request
body — reverse-engineering an unstable private API wasn't worth it for a
first cut. BrowseComp-Plus was the next-best real option and turned out
easy: unlike its Gradio-hosted cousins in this project (IDP Leaderboard,
Vals.ai), this HF Space is a custom static-JSON frontend — confirmed live
by watching the Space's own network requests, no scraping/JS-parsing
needed. 84 rows across 27 distinct LLMs when checked.

Each LLM is evaluated with multiple retrievers (BM25, various embedding
models) against a FIXED 100K-document corpus, not the live web — this
isolates model quality from retriever quality (deliberately, per the
benchmark's own design), so it's closer to "how well can this model
research-and-synthesize given consistent search results" than "how good is
this model at web browsing" literally. Each (LLM, Retriever) pair is
recorded as its own name here (e.g. "GPT-5 (BM25)") rather than collapsed
into one row per LLM, since retriever choice measurably changes the score
and picking one arbitrarily to "represent" the model would hide that.
"""

import requests

from normalize import make_record

URL = "https://tevatron-browsecomp-plus.hf.space/data/leaderboard.json"


def fetch():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize(raw):
    records = []
    for row in raw:
        llm = row.get("LLM")
        accuracy = row.get("Accuracy (%)")
        if not llm or accuracy is None:
            continue
        retriever = row.get("Retriever")
        name = f"{llm} ({retriever})" if retriever else llm
        open_weights = row.get("Open Weights?")
        weight_availability = (
            "open" if open_weights == "Yes" else "closed" if open_weights == "No" else None
        )
        records.append(
            make_record(
                name=name,
                category="research",
                score=accuracy,
                score_type="browsecomp_plus_accuracy",
                weight_availability=weight_availability,
                raw_payload={
                    "llm": llm,
                    "retriever": retriever,
                    "recall_pct": row.get("Recall (%)"),
                    "search_calls": row.get("Search Calls"),
                    "calibration_error_pct": row.get("Calibration Error (%)"),
                    "evaluation_date": row.get("Evaluation Date"),
                    "llm_link": row.get("LLM Link"),
                },
            )
        )
    return records
