"""Ollama Library — local-runnable model discovery.

Confirmed live (2026-08-05): the unofficial third-party mirror
ollamadb.dev does NOT resolve (DNS failure — `Could not resolve host:
ollamadb.dev`), even though other unrelated hosts (huggingface.co,
ollama.com, artificialanalysis.ai) all resolved fine in the same check, so
this isn't a local network problem — that API appears to be dead/gone.
Fell back to scraping https://ollama.com/library directly, per the plan's
documented fallback.

The library page turned out not to need scraping tricks: a single GET to
https://ollama.com/library?sort=popular returns all ~231 models in one
HTML response (no pagination/infinite-scroll to fight), each as an
`<li>` inside `#repo` with the model slug in its `<a href="/library/...">`,
name/description in nested `<h2>`/`<p>`, and tag chips split between
capability tags (e.g. "tools", "vision", "embedding", "thinking") and
parameter-size tags (e.g. "8b", "70b") — distinguished here by a
`^\\d+(\\.\\d+)?[bmkBMK]$`-style regex.

This is a discovery/registry feed, not a benchmark (mirrors sources/hf_hub.py's
handling of the same kind of feed): score is left null and score_type just
marks how the model was found. Every model here is, by construction, in
Ollama's library, so weight_availability="open" and local_runnable=True are
set unconditionally.
"""

import re

import requests
from bs4 import BeautifulSoup

from normalize import make_record

LIBRARY_URL = "https://ollama.com/library"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ModelCantina/1.0)"}

_SIZE_TAG_RE = re.compile(r"^\d+(\.\d+)?[bmkBMK]$")


def fetch():
    resp = requests.get(LIBRARY_URL, params={"sort": "popular"}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def normalize(raw):
    soup = BeautifulSoup(raw, "html.parser")
    records = []
    seen = set()

    for li in soup.select("#repo li"):
        a = li.find("a", href=True)
        if not a or not a["href"].startswith("/library/"):
            continue
        slug = a["href"].split("/library/", 1)[1].strip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)

        name_el = a.select_one("h2 span")
        name = name_el.get_text(strip=True) if name_el else slug

        desc_el = a.find("p", class_="max-w-lg")
        description = desc_el.get_text(strip=True) if desc_el else None

        tag_chips = [t.get_text(strip=True) for t in a.select("span.inline-flex")]
        size_tags = [t for t in tag_chips if _SIZE_TAG_RE.match(t)]
        capability_tags = [t for t in tag_chips if t not in size_tags]

        meta_text = [s.get_text(" ", strip=True) for s in a.select("p.my-4 span.flex")]

        payload = {
            "slug": slug,
            "description": description,
            "sizes": size_tags,
            "capabilities": capability_tags,
            "meta": meta_text,
        }

        records.append(
            make_record(
                name=name,
                category="local_open_weight",
                score=None,
                score_type="ollama_library_present",
                weight_availability="open",
                local_runnable=True,
                modalities=capability_tags or None,
                raw_payload=payload,
            )
        )

    return records
