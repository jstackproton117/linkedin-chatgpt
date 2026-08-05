"""Stage 2: Generate post angle suggestions using the local model.

For each article, calls the local Ollama model with a short, single-task prompt.
The model only generates an angle (one sentence) and a hook (opening words).
All brand judgment is deferred to Claude in stage 4.

Writes results to data/suggestions.json.
"""

import json
import re
import sys
from pathlib import Path

import requests

BASE = Path(__file__).parent
SETTINGS = json.loads((BASE / "settings.json").read_text())
DATA = BASE / "data"

IN_PATH = DATA / "articles.json"
OUT_PATH = DATA / "suggestions.json"

MODEL_URL = SETTINGS["local_model"]["url"]
MODEL_NAME = SETTINGS["local_model"]["model"]
TIMEOUT = SETTINGS["local_model"]["timeout"]

# Prompt is short and single-task so the small model doesn't get confused.
# Double braces {{ }} are escaped Python format-string literals for the JSON example.
PROMPT = """Output JSON only. No explanation, no markdown, no preamble.

Article title: {title}
Summary: {summary}

Task: suggest a LinkedIn post angle for engineering leaders.

Output this exact JSON structure:
{{"angle": "one sentence post angle", "hook": "first 10 words of the post"}}

JSON:
{{"""


def call_model(prompt):
    resp = requests.post(
        MODEL_URL,
        json={
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def extract_json(text):
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None


def check_connectivity():
    base = MODEL_URL.split("/api/")[0]
    try:
        resp = requests.get(f"{base}/api/tags", timeout=5)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach local model at {MODEL_URL}")
        print(f"  {e}")
        print("  Make sure Ollama is running and reachable on your network.")
        return False


def main():
    if not IN_PATH.exists():
        print("ERROR: data/articles.json not found. Run 01_fetch.py first.")
        sys.exit(1)

    articles = json.loads(IN_PATH.read_text())

    if not articles:
        print("No articles to process. Run 01_fetch.py or lower min_relevance_score in settings.json.")
        OUT_PATH.write_text("[]")
        return

    print(f"Connecting to local model ({MODEL_NAME} at {MODEL_URL})...")
    if not check_connectivity():
        sys.exit(1)
    print("Connected.\n")

    suggestions = []
    print(f"Generating suggestions for {len(articles)} articles...")

    for i, article in enumerate(articles, 1):
        title = article["title"]
        summary = article.get("snippet", "")[:200]
        print(f"  [{i}/{len(articles)}] {title[:65]}...")

        prompt = PROMPT.format(title=title, summary=summary)

        try:
            raw = call_model(prompt)
            parsed = extract_json(raw)

            if parsed and "angle" in parsed:
                angle = str(parsed.get("angle", "")).strip()
                hook = str(parsed.get("hook", "")).strip()
            else:
                angle = f"What this means for engineering leaders: {title}"
                hook = title[:60]
                print(f"    (model output unparseable — using fallback)")

            suggestions.append({**article, "angle": angle, "hook": hook})

        except requests.Timeout:
            print(f"    Timeout — skipping")
        except Exception as e:
            print(f"    Error — {e} — skipping")

    OUT_PATH.write_text(json.dumps(suggestions, indent=2))
    print(f"\n{len(suggestions)} suggestions saved to data/suggestions.json")


if __name__ == "__main__":
    main()
