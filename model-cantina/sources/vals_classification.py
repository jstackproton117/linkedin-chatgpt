"""Classification, routing & triage — Vals.ai's LegalBench page, used as an
imperfect proxy, no exact match exists on the site.

Checked live (2026-08-05): `vals.ai/benchmarks` is an Astro-built, mostly
server-rendered site (unlike the Next.js apps elsewhere in this project) —
`curl` gets full HTML back, no headless browser needed. But there is no
benchmark on the site literally named or tagged "classification" or
"routing" — the listing groups everything under verticals like Legal,
Finance, Medical, Coding, Education, Agentic, etc. (checked every section
heading and every `/benchmarks/<slug>` link on the page; nothing routing- or
triage-flavored exists).

The closest real match is **LegalBench** (`/benchmarks/legal_bench`): the
actual academic LegalBench suite is, underneath its "legal" branding,
predominantly a set of *text classification* tasks (e.g. clause
classification, issue-spotting, hearsay classification) rather than
generative tasks — so it's a legitimate if domain-narrow proxy for
classification ability, not a proxy for query routing/triage specifically.
Treated explicitly as a proxy via the `_proxy` suffix on score_type and this
docstring, even though config.yaml's `proxy_for` for this source is left
empty per instructions not to edit that file.

Data extraction: the LegalBench page has no `<table>` in server-rendered
HTML and no `/api/*` JSON endpoint (checked common paths). What it does have
is an Astro island: `<astro-island ... component-url=".../ScatterGraph...js"
props="{&quot;benchmarkView&quot;:...}">`, where `props` is an HTML-entity-
encoded JSON blob in Astro's serialization format (values wrapped as
`[typeTag, value]` pairs) containing the full per-model results, including a
canonical `"models"` list (132 entries, matching the "132" model count shown
on the benchmarks index page for LegalBench). Extraction here is a targeted
regex over the unescaped blob for `"<model>":[0,{"accuracy":[0,<number>]`
pairs, keeping each model's *first* occurrence — verified against the raw
data that this yields exactly the 132 canonical models in the same
descending order as the site's own "Overall" ranking (i.e. first-seen ==
overall score, not a sub-task score). This is a hand-parsed, undocumented
prop-serialization format specific to Vals.ai's current Astro build, so it
is more fragile than a real API — if their component or serialization
format changes, fetch() will raise (no matching astro-island / no pairs
found), which is an honest failure rather than silently wrong data.
"""

import html
import re

import requests

from normalize import make_record

URL = "https://www.vals.ai/benchmarks/legal_bench"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ModelCantina/1.0)"}

_ISLAND_PROPS_RE = re.compile(r'<astro-island[^>]*props="([^"]*accuracy[^"]*)"')
_MODEL_ACCURACY_RE = re.compile(r'"([a-zA-Z0-9_./\-]+)":\[0,\{"accuracy":\[0,([0-9.]+)\]')


def fetch():
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    match = _ISLAND_PROPS_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Vals.ai LegalBench: no astro-island props blob containing score "
            "data found on the page — layout may have changed."
        )
    raw = html.unescape(match.group(1))

    pairs = _MODEL_ACCURACY_RE.findall(raw)
    if not pairs:
        raise RuntimeError(
            "Vals.ai LegalBench: astro-island props blob found but no "
            "model/accuracy pairs matched — serialization format may have "
            "changed."
        )

    scores = {}
    for name, acc in pairs:
        if name not in scores:  # first occurrence = overall score
            scores[name] = float(acc)
    return scores


def normalize(raw):
    records = []
    for model_slug, accuracy in raw.items():
        org = model_slug.split("/")[0] if "/" in model_slug else None
        records.append(
            make_record(
                name=model_slug,
                org=org,
                category="classification_routing",
                score=round(accuracy, 2),
                score_type="vals_legalbench_overall_accuracy_proxy",
                raw_payload={
                    "benchmark": "LegalBench",
                    "note": "Legal text-classification suite used as a proxy "
                            "for general classification ability; not a "
                            "measurement of routing/triage specifically.",
                },
            )
        )
    return records
