"""Privacy & trust-boundary enforcement — AgentDojo's results table, not
JailbreakBench or HarmBench.

Checked all three candidates from the plan live (2026-08-05):

1. `JailbreakBench/jailbreakbench` on GitHub has no committed leaderboard
   data file — its repo only has `src/jailbreakbench/artifact.py`, a client
   for *downloading* pre-generated jailbreak-string artifacts from their
   separate `jbb-artifacts` bucket for people to run themselves. That's
   attack strings, not a model-vs-model results table.
2. `centerforaisafety/HarmBench` is the same shape: the repo runs the
   benchmark; results live in a local Jupyter notebook
   (`notebooks/analyze_results.ipynb`) that people fill in after running
   it themselves, not a published data file in the repo.
3. `ethz-spylab/agentdojo` (confirmed correct org/repo — this is the real
   AgentDojo) is different: `docs/results-table.html` is a plain, already-
   populated HTML `<table id="results-table">` that their own docs site
   includes verbatim into a documentation page. It has real rows going back
   to mid-2024 through the newest models they've tested, each with
   Provider, Model, Defense, Attack, Utility, "Utility under attack",
   "Targeted ASR" (attack success rate), and Date. This is exactly the
   "prompt-injection / trust-boundary robustness" measurement the
   privacy_trust category wants, and it needs no JS rendering — just
   requests + BeautifulSoup on a static HTML file in the repo.

Caveat worth being explicit about: AgentDojo's own docs explicitly say
"this is *not* a leaderboard" — they haven't run every model against every
attack/defense combination, so cross-model comparisons here are apples-to-
oranges unless you also check the Defense/Attack columns (kept in
raw_payload).

Score direction fix (2026-08-05): the dashboard's category leaderboards
always sort score DESC and treat higher as better, app-wide — there's no
per-source "lower is better" handling anywhere in storage.py or the
templates. Storing raw "attack success rate" (where LOWER is safer) would
have silently ranked the least-safe model as #1 on the Privacy &
Trust-Boundary Enforcement page. Inverted at ingestion instead: score =
100 - ASR, so it reads as a "held the line" / defense rate and higher is
uniformly better, consistent with every other category. The original raw
ASR is preserved in raw_payload for anyone who wants it.
"""

import requests
from bs4 import BeautifulSoup

from normalize import make_record

URL = "https://raw.githubusercontent.com/ethz-spylab/agentdojo/main/docs/results-table.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ModelCantina/1.0)"}


def fetch():
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="results-table")
    if table is None:
        raise RuntimeError(
            "AgentDojo: no <table id='results-table'> found in "
            "docs/results-table.html — page structure may have changed."
        )
    thead = table.find("thead")
    tbody = table.find("tbody")
    if thead is None or tbody is None:
        raise RuntimeError("AgentDojo: results table is missing <thead>/<tbody>.")

    headers = [th.get_text(strip=True) for th in thead.find_all("th")]
    rows = []
    for tr in tbody.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _parse_percent(value):
    if not value:
        return None
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


def normalize(raw):
    records = []
    for row in raw:
        model = row.get("Model")
        asr = _parse_percent(row.get("Targeted ASR"))
        if not model or asr is None:
            continue
        records.append(
            make_record(
                name=model,
                org=row.get("Provider"),
                category="privacy_trust",
                score=round(100 - asr, 2),
                score_type="agentdojo_targeted_defense_rate",
                raw_payload={
                    "defense": row.get("Defense") or None,
                    "attack": row.get("Attack") or None,
                    "utility": row.get("Utility"),
                    "utility_under_attack": row.get("Utility under attack"),
                    "date": row.get("Date"),
                    "raw_targeted_attack_success_rate": asr,
                },
            )
        )
    return records
