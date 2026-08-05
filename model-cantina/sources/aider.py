"""Aider Polyglot Leaderboard — the raw YAML data file behind the page, not
HTML scraping.

aider.chat/docs/leaderboards/ turned out to be a static, server-rendered
Jekyll table (no JS rendering needed, BeautifulSoup would have worked) — but
the page's own footer links straight to its source data file in the Aider
GitHub repo: `aider/website/_data/polyglot_leaderboard.yml`. Fetching that
raw YAML directly is more robust than parsing the HTML table (survives
template/CSS changes) and is the same "official structured file, no
scraping" pattern used by sources/swebench.py. Confirmed live (2026-08-05):
69 entries; spot-checked the "gpt-5 (high)" row's `pass_rate_2: 88.0` against
the rendered "88.0%" cell on the page — matches. `pass_rate_2` (pass rate
after Aider's second/retry attempt) is the figure the site labels "Percent
correct" and sorts by, so that's the score used here, not `pass_rate_1`.

Each YAML entry is one benchmark *run*, not one unique model — the same
underlying model can appear multiple times under different `model` display
strings (e.g. "o3" vs "o3 (high)" for different reasoning-effort settings,
or reruns after a prompting/harness change). That matches how the leaderboard
itself presents them as distinct rows, so this module records one score per
run/entry rather than trying to collapse them.
"""

import yaml
import requests

from normalize import make_record

URL = "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml"


def fetch():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    return yaml.safe_load(resp.text)


def _org_from_command(command):
    if not command:
        return None
    parts = command.split()
    if "--model" not in parts:
        return None
    idx = parts.index("--model")
    if idx + 1 >= len(parts):
        return None
    model_ref = parts[idx + 1]
    segments = model_ref.split("/")
    if len(segments) < 2:
        return None
    # openrouter/<provider>/<model> — the interesting org is the provider,
    # not the routing service.
    if segments[0] == "openrouter" and len(segments) > 2:
        return segments[1]
    return segments[0]


def normalize(raw):
    records = []
    for entry in raw or []:
        name = entry.get("model")
        score = entry.get("pass_rate_2")
        if not name or score is None:
            continue
        command = entry.get("command")
        date = entry.get("date")
        if date is not None:
            date = str(date)  # PyYAML parses bare YYYY-MM-DD as a date object
        records.append(
            make_record(
                name=name,
                org=_org_from_command(command),
                category="coding",
                score=score,
                score_type="aider_polyglot_pass_rate_2",
                raw_payload={
                    "dirname": entry.get("dirname"),
                    "edit_format": entry.get("edit_format"),
                    "test_cases": entry.get("test_cases"),
                    "pass_rate_1": entry.get("pass_rate_1"),
                    "percent_cases_well_formed": entry.get("percent_cases_well_formed"),
                    "command": command,
                    "date": date,
                    "total_cost": entry.get("total_cost"),
                },
            )
        )
    return records
