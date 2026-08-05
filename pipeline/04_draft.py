"""Stage 3: Build copy-paste prompts for each queued post.

For each item marked 'draft' in queue.json, writes:
  - A _prompt.txt file  (paste into Claude.ai or any frontier model)
  - A _draft.md file    (paste the response here)

Also writes an entry to data/post_log.json linking the article to its files.
Run 05_log_post.py after publishing to record the LinkedIn URL.
Run 06_log_metrics.py after collecting screenshot metrics.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
# Voice files normally live one level up (alongside the OneDrive project root),
# but a self-contained deploy (e.g. the NUC) keeps its own copy in voice/ --
# check that first, fall back to the old location so nothing breaks.
VOICE_DIRS = [BASE / "voice", BASE.parent]
DATA = BASE / "data"
DRAFTS_DIR = DATA / "drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_PATH = DATA / "queue.json"
POST_LOG_PATH = DATA / "post_log.json"

VOICE_FILES = {
    "identity": "identity.txt",
    "voice": "brand voice guide.txt",
    "templates": "post template.txt",
    "checklist": "post qa checklist.txt",
}


def load_voice():
    parts = {}
    for key, filename in VOICE_FILES.items():
        text = None
        for d in VOICE_DIRS:
            path = d / filename
            if path.exists():
                text = path.read_text(encoding="utf-8")
                break
        parts[key] = text if text is not None else f"[{filename} not found]"
    return parts


def build_prompt(article, voice):
    return f"""You are drafting LinkedIn posts for Joe Stackpole.

## Who Joe is
{voice['identity']}

## Joe's voice and brand
{voice['voice']}

## Post format templates (pick two different ones)
{voice['templates']}

## Pre-post QA checklist
{voice['checklist']}

---

## Your task

Write 2 LinkedIn post drafts based on this news item.

**Source:** {article['source']}
**Title:** {article['title']}
**URL:** {article['url']}
**Summary:** {article.get('snippet', '')[:300]}
**Angle:** {article['angle']}
**Hook starter:** {article['hook']}
**Content pillar:** {article.get('pillar', 'General')}

Rules:
- Joe adds personal experience himself. Where an anecdote belongs, write: [Joe's experience here]
- Each draft must be 900-1,500 characters total.
- Use a different template for Draft 1 vs Draft 2.
- No emojis. No cheesy CTA. Strong hook in the first 2 lines.
- No confidential information about GetChkd or prior employers.
- Clear and specific -- no buzzword soup.
- NEVER use relative time references like "this week", "recently", "today", "last week", or "this month".
  Posts may be scheduled days or weeks after drafting. Instead use specific dates:
  "On June 25, 2026..." or "A June 2026 study found..." or "As of June 2026..."
- Ground every claim in a specific fact already in the Summary above (a name, a date, a concrete
  consequence). Never invent a statistic or fact that isn't in the Summary or URL.
- Do not open with a restatement of the headline. Do not use the pattern "Engineering leaders
  should/can [verb] to [benefit]" anywhere in either draft -- that phrasing is a tell that this was
  written by an AI, not a person.
- Cut AI-tell phrases entirely: "it's worth noting", "in today's landscape", "as we navigate",
  "importantly", "furthermore", "it is important to", "it's no secret", "in conclusion", "unlock",
  "game-changer", "double-edged sword", "let's dive in", "at the end of the day".
- Prefer short, declarative sentences. Fragments for emphasis are fine. Don't hedge with "could",
  "might", or "arguably" unless the claim is genuinely uncertain -- say the thing directly.
- Don't close with generic engagement bait ("What are your thoughts?", "Let's discuss below").
  Close with an actual point of view.

Format your response exactly like this:

## Draft 1 -- [Template Name]

[post content]

Character count: [N]

---

## Draft 2 -- [Template Name]

[post content]

Character count: [N]"""


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", text).strip("-")[:40]


def load_post_log():
    if not POST_LOG_PATH.exists():
        return []
    try:
        return json.loads(POST_LOG_PATH.read_text())
    except Exception:
        return []


def save_post_log(log):
    POST_LOG_PATH.write_text(json.dumps(log, indent=2))


def main():
    if not QUEUE_PATH.exists():
        print("ERROR: data/queue.json not found. Run 03_review.py first.")
        sys.exit(1)

    queue = json.loads(QUEUE_PATH.read_text())
    to_draft = [x for x in queue if x["status"] == "draft"]

    if not to_draft:
        print("No items marked for drafting in queue.json.")
        print("Run 03_review.py and mark at least one article as [d]raft.")
        sys.exit(0)

    print(f"Building prompts for {len(to_draft)} post(s)...")
    voice = load_voice()
    post_log = load_post_log()
    logged_ids = {x["id"] for x in post_log}
    today = datetime.now().strftime("%Y-%m-%d")

    for i, article in enumerate(to_draft, 1):
        title = article["title"]
        slug = slugify(title)
        base_name = f"{today}-{i:02d}-{slug}"
        entry_id = f"{today}-{i:02d}"

        prompt_path = DRAFTS_DIR / f"{base_name}_prompt.txt"
        draft_path = DRAFTS_DIR / f"{base_name}_draft.md"

        prompt_path.write_text(build_prompt(article, voice), encoding="utf-8")

        if not draft_path.exists():
            draft_path.write_text(
                f"# {title}\n\n"
                f"**Source:** {article['url']}\n"
                f"**Angle:** {article['angle']}\n\n"
                f"---\n\n"
                f"<!-- Paste the frontier model response below -->\n\n",
                encoding="utf-8",
            )

        print(f"  [{i}] {base_name}_prompt.txt")

        if entry_id not in logged_ids:
            post_log.append({
                "id": entry_id,
                "article_url": article["url"],
                "article_title": article["title"],
                "article_source": article.get("source", ""),
                "pillar": article.get("pillar", ""),
                "angle": article.get("angle", ""),
                "prompt_file": str(prompt_path.name),
                "draft_file": str(draft_path.name),
                "created_at": datetime.now().isoformat(),
                "published_at": None,
                "linkedin_url": None,
                "metrics": None,
            })

    save_post_log(post_log)

    print(f"""
Done. {len(to_draft)} prompt file(s) in:
  {DRAFTS_DIR}

For each post:
  1. Open the _prompt.txt file
  2. Copy the entire contents
  3. Paste into Claude.ai (or any frontier model)
  4. Paste the response into the matching _draft.md file

After publishing:
  Run 05_log_post.py to record the LinkedIn URL.
  Run 06_log_metrics.py to record post performance (after screenshotting).
""")


if __name__ == "__main__":
    main()
