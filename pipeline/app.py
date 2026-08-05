"""Rebel Intel — Local web UI for the LinkedIn Content Pipeline."""

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests as http_requests
from flask import Flask, abort, jsonify, render_template, request

app = Flask(__name__)

BASE = Path(__file__).parent
DATA = BASE / "data"
DRAFTS_DIR = DATA / "drafts"

SETTINGS_PATH = BASE / "settings.json"
ARTICLES_PATH = DATA / "articles.json"
QUEUE_PATH = DATA / "queue.json"
POST_LOG_PATH = DATA / "post_log.json"
THEME_PATH = DATA / "theme_counts.json"
SELECTION_LOG_PATH = DATA / "selection_log.json"
EXPIRY_LOG_PATH = DATA / "expiry_log.json"

DRAFT_EXPIRY_HOURS = 24


# ── Helpers ──────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if default is None:
        default = []
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_settings():
    return load_json(SETTINGS_PATH, {})


def format_age(published_iso):
    try:
        pub_dt = datetime.fromisoformat(published_iso)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
        if hours < 1:
            return f"{int(hours * 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except Exception:
        return "??"


def age_hours(published_iso):
    try:
        pub_dt = datetime.fromisoformat(published_iso)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600)
    except Exception:
        return 36.0


def _has_drafted_content(draft_file):
    """True if the draft file has real, pasted-in content -- not just the
    empty placeholder 04_draft.py writes the moment prompt files are built.
    Distinguishes an actual candidate to publish from a queued-but-untouched
    article, which shouldn't clutter Mission Log with a live PUBLISH button."""
    if not draft_file:
        return False
    path = DRAFTS_DIR / draft_file
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8", errors="replace")
    return len(content.strip()) > 200 and "Paste the frontier model" not in content


def mark_overdue(post_log):
    """Flag scheduled posts whose date has passed without being manually
    confirmed published. Read-only bookkeeping only — published_at is set
    ONLY by the explicit PUBLISH action (api_publish), never automatically,
    since a passed schedule date doesn't mean Joe actually posted it (and
    silently marking it 'published' hid the missing LinkedIn URL with no way
    to add it later). Annotates each entry with '_overdue' and returns the
    count, so Joe is prompted to confirm instead of the system assuming."""
    today = date.today().isoformat()
    count = 0
    for p in post_log:
        overdue = bool(p.get("scheduled_for") and not p.get("published_at") and p["scheduled_for"] < today)
        p["_overdue"] = overdue
        if overdue:
            count += 1
    return count


def get_next_scheduled(post_log):
    """Return info on the soonest upcoming (or overdue) scheduled post."""
    today = date.today()
    upcoming = []
    for p in post_log:
        if p.get("scheduled_for") and not p.get("published_at"):
            try:
                d = date.fromisoformat(p["scheduled_for"])
                days = (d - today).days
                upcoming.append({"date": p["scheduled_for"], "days": days, "overdue": days < 0,
                                  "title": p.get("article_title", "")})
            except Exception:
                pass
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x["date"])
    return upcoming[0]


def _parse_dt(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


def auto_expire_drafts(queue, post_log):
    """Drop drafts (and queued-but-never-drafted articles) that are neither
    published nor scheduled and have sat for longer than DRAFT_EXPIRY_HOURS.
    News goes stale fast — an unpublished draft this old is assumed dead
    rather than risk publishing outdated commentary. Mutates queue/post_log
    in place and appends a record of what was dropped to expiry_log.json."""
    cutoff = datetime.now() - timedelta(hours=DRAFT_EXPIRY_HOURS)
    expired_urls = set()
    dropped = []

    kept_log = []
    for p in post_log:
        if p.get("published_at") or p.get("scheduled_for"):
            kept_log.append(p)
            continue
        created = _parse_dt(p.get("created_at"))
        if created and created < cutoff:
            expired_urls.add(p.get("article_url"))
            dropped.append({"title": p.get("article_title", ""), "url": p.get("article_url", ""),
                             "stage": "draft", "created_at": p.get("created_at", "")})
            for field in ("draft_file", "prompt_file"):
                fname = p.get(field)
                if fname:
                    (DRAFTS_DIR / fname).unlink(missing_ok=True)
        else:
            kept_log.append(p)

    logged_urls = {p.get("article_url") for p in kept_log}
    kept_queue = []
    for q in queue:
        url = q.get("url")
        if url in expired_urls:
            continue
        if q.get("status") == "draft" and url not in logged_urls:
            reviewed = _parse_dt(q.get("reviewed_at"))
            if reviewed and reviewed < cutoff:
                dropped.append({"title": q.get("title", ""), "url": url,
                                 "stage": "queued", "created_at": q.get("reviewed_at", "")})
                continue
        kept_queue.append(q)

    # Orphan draft/prompt files with no post_log entry at all (e.g. left behind
    # by manual edits or old merges) — judge age by file mtime instead.
    referenced = {p.get(f) for p in kept_log for f in ("draft_file", "prompt_file") if p.get(f)}
    cutoff_ts = cutoff.timestamp()
    if DRAFTS_DIR.exists():
        seen_bases = set()
        for f in DRAFTS_DIR.iterdir():
            if f.name in referenced or not (f.name.endswith("_draft.md") or f.name.endswith("_prompt.txt")):
                continue
            base = f.name.removesuffix("_draft.md").removesuffix("_prompt.txt")
            if base in seen_bases:
                continue
            seen_bases.add(base)
            pair = [p2 for p2 in (DRAFTS_DIR / f"{base}_draft.md", DRAFTS_DIR / f"{base}_prompt.txt") if p2.exists()]
            if any(p2.name in referenced for p2 in pair):
                continue
            mtime = max((p2.stat().st_mtime for p2 in pair), default=0)
            if mtime and mtime < cutoff_ts:
                dropped.append({"title": base, "url": "", "stage": "orphan_file", "created_at": ""})
                for p2 in pair:
                    p2.unlink(missing_ok=True)

    if dropped:
        post_log[:] = kept_log
        queue[:] = kept_queue
        expiry_log = load_json(EXPIRY_LOG_PATH, [])
        now_iso = datetime.now().isoformat()
        for d in dropped:
            expiry_log.append({**d, "expired_at": now_iso})
        save_json(EXPIRY_LOG_PATH, expiry_log)

    return dropped


def get_rising_themes():
    counts = load_json(THEME_PATH, {})
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    rising = []
    for kw, dates in counts.items():
        recent = [d for d in dates if d >= cutoff]
        if len(recent) >= 3:
            rising.append({"keyword": kw, "count": len(recent)})
    return sorted(rising, key=lambda x: -x["count"])[:8]


def run_script(script_name, timeout=300):
    python = BASE / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    result = subprocess.run(
        [str(python), str(BASE / script_name)],
        capture_output=True, text=True, cwd=str(BASE), timeout=timeout,
    )
    return result.returncode == 0, result.stdout + result.stderr


_SPELLED_QUANTITY_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:in|of)\s+\w+\b"
    r"|\bmajority\b|\bhalf\b|\ba third\b|\ba quarter\b",
    re.I,
)


def _has_unverified_number(text, summary):
    """True if text cites a stat that doesn't appear (in substance) in the
    source summary — qwen2.5:7b fabricates numbers like '42%', 'more than
    half', or 'one in four' even when explicitly told not to, so this is a
    mechanical backstop rather than trusting the model's self-restraint.
    Digits/percents must match verbatim; spelled-out quantity phrases ('one
    in four', 'the majority') are flagged outright since they're virtually
    always fabricated when the source itself has no digits to paraphrase."""
    for tok in re.findall(r"\d[\d,.]*%?", text):
        if tok not in summary:
            return True
    if _SPELLED_QUANTITY_RE.search(text) and not re.search(r"\d", summary):
        return True
    return False


def _is_bad_response(angle, hook, summary):
    if not angle or not hook:
        return True
    if _has_unverified_number(angle, summary) or _has_unverified_number(hook, summary):
        return True
    # The model sometimes echoes the JSON schema's own example text verbatim
    # instead of filling it in — catch that instead of shipping it as real.
    placeholder_bits = ("grounded in a real detail from the article", "restatement of the title")
    if any(p in angle.lower() for p in placeholder_bits) or any(p in hook.lower() for p in placeholder_bits):
        return True
    return False


def call_local_model(title, summary):
    settings = get_settings()
    lm = settings.get("local_model", {})
    url = lm.get("url", "http://192.168.1.46:11434/api/chat")
    model = lm.get("model", "qwen2.5:3b")
    timeout = lm.get("timeout", 60)

    def build_prompt(scold=False):
        scold_line = (
            "Your previous attempt invented a number that wasn't in the summary. "
            "Do not do that again — describe it in words with no digits or percent sign.\n\n"
            if scold else ""
        )
        return (
            "Output JSON only. No explanation, no markdown, no preamble.\n\n"
            f"Article title: {title}\n"
            f"Summary: {summary[:200]}\n\n"
            "Task: find ONE specific, opinionated angle for a LinkedIn post aimed at engineering leaders.\n"
            "State your take on what this means or what's actually happening — do NOT tell the reader\n"
            "what to do ('consider doing X', 'you should Y').\n\n"
            "Only use a number, percentage, or company/product name if it is written verbatim in the\n"
            "summary above. Never invent or guess one. If the summary has no specific number or name,\n"
            "write the angle without one rather than inventing one — a made-up specific is worse than\n"
            "none.\n\n"
            f"{scold_line}"
            "BANNED: the pattern 'Engineering leaders should/can [verb] to [benefit]'. If your angle\n"
            "fits that shape, it is wrong — try again with something concrete and specific instead.\n\n"
            'Output this exact JSON structure:\n'
            '{"angle": "one specific, opinionated sentence grounded in a real detail from the article", '
            '"hook": "a punchy first line, NOT a restatement of the title"}\n\n'
            "JSON:\n{"
        )

    def ask(prompt):
        resp = http_requests.post(
            url,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]
        try:
            return json.loads(raw.strip())
        except Exception:
            m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return None

    try:
        parsed = ask(build_prompt())
        angle = str(parsed.get("angle", "")).strip() if parsed else ""
        hook = str(parsed.get("hook", "")).strip() if parsed else ""
        if parsed and _is_bad_response(angle, hook, summary):
            # One retry with a pointed reminder before giving up on specifics.
            parsed = ask(build_prompt(scold=True))
            angle = str(parsed.get("angle", "")).strip() if parsed else angle
            hook = str(parsed.get("hook", "")).strip() if parsed else hook
            if _is_bad_response(angle, hook, summary):
                # Still bad — a bland-but-honest angle beats a wrong or empty one.
                return {"angle": f"Worth a closer look: {title[:80]}", "hook": title[:60], "ok": False}
        if angle:
            return {"angle": angle, "hook": hook, "ok": True}
    except Exception:
        pass
    return {"angle": f"Worth a closer look: {title[:80]}", "hook": title[:60], "ok": False}


# ── Dashboard ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    articles = load_json(ARTICLES_PATH, [])
    queue = load_json(QUEUE_PATH, [])
    post_log = load_json(POST_LOG_PATH, [])
    if auto_expire_drafts(queue, post_log):
        save_json(QUEUE_PATH, queue)
        save_json(POST_LOG_PATH, post_log)
    to_draft = sum(1 for x in queue if x.get("status") == "draft")
    on_hold = sum(1 for x in queue if x.get("status") == "hold")
    unpublished = sum(1 for x in post_log
                      if not x.get("published_at") and _has_drafted_content(x.get("draft_file")))
    no_metrics = sum(1 for x in post_log if x.get("published_at") and not x.get("metrics"))
    return render_template("index.html",
        article_count=len(articles),
        to_draft=to_draft,
        on_hold=on_hold,
        unpublished=unpublished,
        no_metrics=no_metrics,
        rising=get_rising_themes(),
        next_scheduled=get_next_scheduled(post_log),
    )


# ── Fetch / Scan ──────────────────────────────────────────────────────────

@app.route("/fetch")
def fetch_page():
    return render_template("fetch.html")


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    try:
        ok, output = run_script("01_fetch.py", timeout=300)
        articles = load_json(ARTICLES_PATH, [])
        return jsonify({"ok": ok, "output": output,
                        "article_count": len(articles), "rising": get_rising_themes()})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "Timed out after 5 minutes.", "article_count": 0, "rising": []})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e), "article_count": 0, "rising": []})


# ── Review ────────────────────────────────────────────────────────────────

@app.route("/review")
def review_page():
    articles = load_json(ARTICLES_PATH, [])
    queue = load_json(QUEUE_PATH, [])
    post_log = load_json(POST_LOG_PATH, [])
    reviewed = {x["url"]: x["status"] for x in queue}
    log_by_url = {x.get("article_url"): x for x in post_log}
    enriched = []
    for i, a in enumerate(articles):
        log_entry = log_by_url.get(a["url"])
        if log_entry:
            if log_entry.get("published_at"):
                log_status = "published"
            elif log_entry.get("scheduled_for"):
                log_status = "scheduled"
            else:
                log_status = "pending"
        else:
            log_status = None
        enriched.append({
            **a,
            "num": i + 1,
            "age": format_age(a.get("published", "")),
            "age_hours": round(age_hours(a.get("published", "")), 1),
            "status": reviewed.get(a["url"]),
            "log_status": log_status,
        })
    return render_template("review.html", articles=enriched, rising=get_rising_themes())


@app.route("/api/article/<int:num>/angle")
def api_angle(num):
    articles = load_json(ARTICLES_PATH, [])
    if num < 1 or num > len(articles):
        abort(404)
    a = articles[num - 1]
    return jsonify(call_local_model(a["title"], a.get("snippet", "")))


@app.route("/api/article/<int:num>/action", methods=["POST"])
def api_action(num):
    articles = load_json(ARTICLES_PATH, [])
    if num < 1 or num > len(articles):
        abort(404)
    data = request.get_json()
    action = data.get("action")
    if action not in ("draft", "skip", "hold"):
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    article = articles[num - 1]
    queue = load_json(QUEUE_PATH, [])
    existing = {x["url"]: i for i, x in enumerate(queue)}

    entry = {**article, "angle": data.get("angle", ""), "hook": data.get("hook", ""),
             "status": action, "reviewed_at": datetime.now().isoformat()}
    if article["url"] in existing:
        queue[existing[article["url"]]] = entry
    else:
        queue.append(entry)
    save_json(QUEUE_PATH, queue)

    sel_log = load_json(SELECTION_LOG_PATH, [])
    sel_log.append({
        "date": date.today().isoformat(),
        "article_title": article["title"],
        "source": article.get("source", ""),
        "pillar": article.get("pillar", ""),
        "score": article.get("score", 0),
        "keywords_matched": article.get("keywords_matched", []),
        "action": action,
    })
    save_json(SELECTION_LOG_PATH, sel_log)
    return jsonify({"ok": True, "action": action})


# ── Drafts ────────────────────────────────────────────────────────────────

@app.route("/drafts")
def drafts_page():
    queue = load_json(QUEUE_PATH, [])
    post_log = load_json(POST_LOG_PATH, [])
    if auto_expire_drafts(queue, post_log):
        save_json(QUEUE_PATH, queue)
        save_json(POST_LOG_PATH, post_log)
    log_by_file = {x.get("draft_file"): x for x in post_log}

    has_log_entry = {x.get("article_url") for x in post_log}
    pending_queue = [x for x in queue if x.get("status") == "draft" and x.get("url") not in has_log_entry]

    active_drafts = []
    archived_drafts = []
    if DRAFTS_DIR.exists():
        for f in sorted(DRAFTS_DIR.glob("*_draft.md")):
            content = f.read_text(encoding="utf-8", errors="replace")
            log_entry = log_by_file.get(f.name)
            prompt_file = f.with_name(f.name.replace("_draft.md", "_prompt.txt"))
            prompt_content = prompt_file.read_text(encoding="utf-8", errors="replace") if prompt_file.exists() else None
            is_published = bool(log_entry and log_entry.get("published_at"))
            is_scheduled = bool(log_entry and log_entry.get("scheduled_for") and not log_entry.get("published_at"))
            d = {
                "filename": f.name,
                "content": content,
                "prompt_content": prompt_content,
                "has_real_content": _has_drafted_content(f.name),
                "published": is_published,
                "scheduled": is_scheduled,
                "article_url": log_entry["article_url"] if log_entry else None,
                "log_id": log_entry["id"] if log_entry else None,
                "title": log_entry["article_title"] if log_entry else f.stem,
                "scheduled_for": log_entry.get("scheduled_for") if log_entry else None,
                "published_at": log_entry.get("published_at") if log_entry else None,
            }
            if is_published or is_scheduled:
                archived_drafts.append(d)
            else:
                active_drafts.append(d)

    return render_template("drafts.html",
        drafts=active_drafts,
        archived_drafts=archived_drafts,
        pending_queue=pending_queue,
    )


@app.route("/api/build-drafts", methods=["POST"])
def api_build_drafts():
    try:
        ok, output = run_script("04_draft.py", timeout=60)
        return jsonify({"ok": ok, "output": output})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)})


@app.route("/api/draft-content/<filename>")
def api_draft_content(filename):
    if ".." in filename or not filename.endswith("_draft.md"):
        abort(400)
    path = DRAFTS_DIR / filename
    if not path.exists():
        abort(404)
    content = path.read_text(encoding="utf-8", errors="replace")
    post_log = load_json(POST_LOG_PATH, [])
    log_entry = next((x for x in post_log if x.get("draft_file") == filename), {})
    return jsonify({
        "content": content,
        "body": _extract_post_body(content),
        "article_url": log_entry.get("article_url", ""),
        "article_title": log_entry.get("article_title", ""),
    })


def _extract_post_body(content):
    """Return just the post body — strips metadata headers and source footnotes."""
    lines = content.splitlines()
    dividers = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dividers) >= 2:
        body_lines = lines[dividers[0] + 1 : dividers[1]]
    elif len(dividers) == 1:
        body_lines = lines[dividers[0] + 1:]
    else:
        # No dividers — strip leading # comment lines
        start = 0
        while start < len(lines) and (lines[start].startswith("#") or not lines[start].strip()):
            start += 1
        body_lines = lines[start:]
    # Drop trailing Sources / Character count
    result = []
    for l in body_lines:
        if l.strip().startswith("Sources:") or l.strip().startswith("Character count"):
            break
        result.append(l)
    return "\n".join(result).strip()


def _call_local_model_rewrite(post_body):
    settings = get_settings()
    lm = settings.get("local_model", {})
    url   = lm.get("url",   "http://192.168.1.46:11434/api/chat")
    model = lm.get("model", "qwen2.5:3b")
    timeout = lm.get("timeout", 120)

    prompt = (
        "Rewrite this LinkedIn post so it sounds like a real person wrote it, not an AI.\n\n"
        "STRICT RULES:\n"
        "- Output ONLY the rewritten post. No intro, no explanation, no commentary after.\n"
        "- Remove filler phrases: 'it is worth noting', 'in today\\'s landscape', 'as we navigate',\n"
        "  'importantly', 'furthermore', 'it is important to', 'it\\'s no secret', 'in conclusion'.\n"
        "- Use short, direct sentences. First person throughout.\n"
        "- Keep the same structure, same key points, same approximate length.\n"
        "- Do not add new information or change the argument.\n\n"
        f"POST:\n{post_body}\n\n"
        "REWRITTEN POST:"
    )
    try:
        resp = http_requests.post(
            url,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        # Strip any preamble the model snuck in
        for prefix in ["REWRITTEN POST:", "Rewritten Post:", "Here is", "Here's", "Sure,", "Of course,"]:
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):].strip()
                break
        return {"content": raw, "ok": True}
    except Exception as e:
        return {"content": "", "ok": False, "error": str(e)}


@app.route("/api/rewrite", methods=["POST"])
def api_rewrite():
    data = request.get_json()
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"ok": False, "error": "No content provided"}), 400
    return jsonify(_call_local_model_rewrite(content))


@app.route("/api/post-log/<post_id>/delete", methods=["POST"])
def api_delete_post(post_id):
    post_log = load_json(POST_LOG_PATH, [])
    entry = next((x for x in post_log if x["id"] == post_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if entry.get("published_at") or entry.get("scheduled_for"):
        return jsonify({"ok": False, "error": "Cannot delete a scheduled or published post"}), 400
    post_log = [x for x in post_log if x["id"] != post_id]
    save_json(POST_LOG_PATH, post_log)
    return jsonify({"ok": True})


@app.route("/api/post-log/<post_id>/schedule", methods=["POST"])
def api_schedule(post_id):
    data = request.get_json()
    post_log = load_json(POST_LOG_PATH, [])
    for entry in post_log:
        if entry["id"] == post_id:
            entry["scheduled_for"] = data.get("scheduled_for", "")
            break
    save_json(POST_LOG_PATH, post_log)
    return jsonify({"ok": True})


@app.route("/api/draft/save", methods=["POST"])
def api_save_draft():
    data = request.get_json()
    filename = data.get("filename", "")
    content = data.get("content", "")
    if not filename or ".." in filename or not filename.endswith("_draft.md"):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    path = DRAFTS_DIR / filename
    path.write_text(content, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/draft/merge", methods=["POST"])
def api_merge_drafts():
    data = request.get_json()
    filename_a = data.get("filename_a", "")
    filename_b = data.get("filename_b", "")
    merged_content = data.get("merged_content", "").strip()
    delete_originals = data.get("delete_originals", False)
    title = data.get("title", "Merged Draft")[:200]

    for fn in [filename_a, filename_b]:
        if not fn or ".." in fn or not fn.endswith("_draft.md"):
            return jsonify({"ok": False, "error": f"Invalid filename: {fn}"}), 400

    if not merged_content:
        return jsonify({"ok": False, "error": "No merged content provided"}), 400

    import time
    slug = str(int(time.time()))
    new_filename = f"merged_{slug}_draft.md"
    (DRAFTS_DIR / new_filename).write_text(merged_content, encoding="utf-8")

    post_log = load_json(POST_LOG_PATH, [])
    log_by_file = {x.get("draft_file"): x for x in post_log}
    entry_a = log_by_file.get(filename_a, {})
    entry_b = log_by_file.get(filename_b, {})

    new_entry = {
        "id": f"merged-{slug}",
        "article_url": f"merged:{slug}",
        "article_title": title,
        "draft_file": new_filename,
        "status": "draft",
        "merged_from": [filename_a, filename_b],
        "source_urls": [entry_a.get("article_url", ""), entry_b.get("article_url", "")],
    }
    post_log.append(new_entry)

    if delete_originals:
        source_urls = {entry_a.get("article_url"), entry_b.get("article_url")} - {None, ""}
        post_log = [x for x in post_log if x.get("draft_file") not in (filename_a, filename_b)]
        for fn in [filename_a, filename_b]:
            for candidate in [DRAFTS_DIR / fn,
                               DRAFTS_DIR / fn.replace("_draft.md", "_prompt.txt")]:
                try:
                    if candidate.exists():
                        candidate.unlink()
                except Exception:
                    pass
        if source_urls:
            queue = load_json(QUEUE_PATH, [])
            queue = [x for x in queue if x.get("url") not in source_urls]
            save_json(QUEUE_PATH, queue)

    save_json(POST_LOG_PATH, post_log)
    return jsonify({"ok": True, "new_filename": new_filename})


@app.route("/api/draft/merge-ai", methods=["POST"])
def api_merge_drafts_ai():
    data = request.get_json()
    content_a = data.get("content_a", "").strip()
    content_b = data.get("content_b", "").strip()
    if not content_a or not content_b:
        return jsonify({"ok": False, "error": "Both drafts required"}), 400

    settings = get_settings()
    lm = settings.get("local_model", {})
    url   = lm.get("url",   "http://192.168.1.46:11434/api/chat")
    model = lm.get("model", "qwen2.5:3b")
    timeout = lm.get("timeout", 180)

    prompt = (
        "Merge these two LinkedIn posts into one stronger post.\n\n"
        "STRICT RULES:\n"
        "- Output ONLY the merged post. No intro, no explanation.\n"
        "- Keep the best hook from either post.\n"
        "- Combine the strongest points without repetition.\n"
        "- Target 900-1,200 characters.\n"
        "- First person. Short paragraphs. Direct, specific, no fluff.\n"
        "- No buzzwords, no hype.\n\n"
        f"POST A:\n{content_a}\n\n"
        f"POST B:\n{content_b}\n\n"
        "MERGED POST:"
    )
    try:
        resp = http_requests.post(
            url,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        for prefix in ["MERGED POST:", "Merged Post:", "Here is", "Here's", "Sure,", "Of course,"]:
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):].strip()
                break
        return jsonify({"ok": True, "content": raw})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Post Log ──────────────────────────────────────────────────────────────

@app.route("/post-log")
def post_log_page():
    posts = load_json(POST_LOG_PATH, [])
    overdue_count = mark_overdue(posts)
    # Hide queued-but-never-drafted placeholders -- they get a post_log entry
    # and a (nonsensical) PUBLISH button the moment prompt files are built,
    # before there's any actual content. Drafts page already shows those as
    # "AWAITING CONTENT"; Mission Log should only list real candidates.
    visible = [p for p in posts
               if p.get("published_at") or p.get("scheduled_for") or _has_drafted_content(p.get("draft_file"))]
    return render_template("post_log.html",
        posts=visible,
        overdue_count=overdue_count,
        next_scheduled=get_next_scheduled(posts),
    )


@app.route("/api/post-log/<post_id>/publish", methods=["POST"])
def api_publish(post_id):
    data = request.get_json()
    post_log = load_json(POST_LOG_PATH, [])
    for entry in post_log:
        if entry["id"] == post_id:
            entry["published_at"] = data.get("published_at", date.today().isoformat())
            entry["linkedin_url"] = data.get("linkedin_url", "")
            break
    save_json(POST_LOG_PATH, post_log)
    return jsonify({"ok": True})


@app.route("/api/post-log/<post_id>/metrics", methods=["POST"])
def api_metrics(post_id):
    data = request.get_json()
    post_log = load_json(POST_LOG_PATH, [])
    for entry in post_log:
        if entry["id"] == post_id:
            entry["metrics"] = data.get("metrics", {})
            break
    save_json(POST_LOG_PATH, post_log)
    return jsonify({"ok": True})


# ── Cascade Delete ────────────────────────────────────────────────────────

@app.route("/api/story/delete", methods=["POST"])
def api_story_delete():
    """Remove a story from all pipeline stages. Blocked if scheduled or published."""
    data = request.get_json()
    article_url = data.get("article_url")
    if not article_url:
        return jsonify({"ok": False, "error": "No article_url provided"}), 400

    post_log = load_json(POST_LOG_PATH, [])
    entry = next((x for x in post_log if x.get("article_url") == article_url), None)
    if entry:
        if entry.get("published_at") or entry.get("scheduled_for"):
            return jsonify({"ok": False,
                            "error": "This story is scheduled or published — it lives in the archive and cannot be deleted."}), 400
        for field in ("draft_file", "prompt_file"):
            fname = entry.get(field)
            if fname:
                fpath = DRAFTS_DIR / fname
                try:
                    if fpath.exists():
                        fpath.unlink()
                except Exception:
                    pass
        post_log = [x for x in post_log if x.get("article_url") != article_url]
        save_json(POST_LOG_PATH, post_log)

    queue = load_json(QUEUE_PATH, [])
    queue = [x for x in queue if x.get("url") != article_url]
    save_json(QUEUE_PATH, queue)

    return jsonify({"ok": True})


# ── Launch ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  ██████╗ ███████╗██████╗ ███████╗██╗         ██╗███╗   ██╗████████╗███████╗██╗")
    print("  ██╔══██╗██╔════╝██╔══██╗██╔════╝██║         ██║████╗  ██║╚══██╔══╝██╔════╝██║")
    print("  ██████╔╝█████╗  ██████╔╝█████╗  ██║         ██║██╔██╗ ██║   ██║   █████╗  ██║")
    print("  ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██║         ██║██║╚██╗██║   ██║   ██╔══╝  ██║")
    print("  ██║  ██║███████╗██████╔╝███████╗███████╗    ██║██║ ╚████║   ██║   ███████╗███████╗")
    print("  ╚═╝  ╚═╝╚══════╝╚═════╝ ╚══════╝╚══════╝    ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚══════╝")
    print()
    print("  May the content be with you.")
    print("  http://localhost:5000")
    print()
    app.run(debug=True, host="0.0.0.0", port=5000)
