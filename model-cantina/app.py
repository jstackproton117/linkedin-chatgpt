"""The Model Cantina — Flask dashboard.

Single-file app, page routes (render templates) + /api/ routes (jsonify),
same pattern as Rebel Intel's pipeline/app.py. Runs unchanged on both
deployments: Thornwick (SQLite, always-on, systemd + cron) and Vercel
(Postgres via DATABASE_URL, serverless — see db.py/poller.py for how the
backend is selected and polling is made to fit a function time limit).
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import config
import poller
import storage

BASE = Path(__file__).parent

app = Flask(__name__)

# True on Vercel (Postgres via DATABASE_URL), False on Thornwick/local dev
# (SQLite). Used to gate the manual per-source "Poll now" button — it's a
# same-LAN admin convenience on Thornwick, but Vercel is public, and a
# button-triggered POST has no sensible way to carry the CRON_SECRET the
# scheduled /cron/poll route uses (embedding a secret in client-side JS
# would defeat the point of it). Simplest correct answer: that feature just
# doesn't exist on the public deployment, which relies on the daily cron
# instead.
IS_HOSTED_PUBLICLY = bool(os.environ.get("DATABASE_URL"))


# ── Home ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    conn = storage.get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    new_models, recent_scores = storage.get_whats_new(conn, since, limit=30)
    sources = storage.get_sources_health(conn)
    stats = storage.get_stats(conn)
    conn.close()
    return render_template(
        "home.html",
        new_models=new_models,
        recent_scores=recent_scores,
        sources=sources,
        stats=stats,
        categories=config.load_config()["categories"],
    )


# ── Models ───────────────────────────────────────────────────────────────

@app.route("/models")
def models_list():
    conn = storage.get_db()
    category = request.args.get("category") or None
    local_runnable = request.args.get("local_runnable")
    if local_runnable == "1":
        local_runnable = True
    elif local_runnable == "0":
        local_runnable = False
    else:
        local_runnable = None
    rows = storage.list_models(conn, category=category, local_runnable=local_runnable)
    conn.close()
    return render_template(
        "models.html",
        models=rows,
        categories=config.load_config()["categories"],
        selected_category=category,
        selected_local_runnable=local_runnable,
    )


def _load_model_detail(model_id):
    conn = storage.get_db()
    model = storage.get_model(conn, model_id)
    scores = storage.get_scores_for_model(conn, model_id)
    notes = storage.get_notes_for_model(conn, model_id)
    conn.close()
    if model is None:
        return None
    return dict(
        model=model,
        scores=scores,
        notes=notes,
        categories=config.load_config()["categories"],
        ratings=config.load_config()["manual_note_ratings"],
    )


@app.route("/models/<int:model_id>")
def model_detail(model_id):
    ctx = _load_model_detail(model_id)
    if ctx is None:
        return "Model not found", 404
    return render_template("model_detail.html", **ctx)


@app.route("/models/<int:model_id>/modal")
def model_detail_modal(model_id):
    """Content-only fragment (no layout chrome) for the in-page modal —
    same data as model_detail(), just rendered without the surrounding
    header/nav so it can be dropped straight into the modal body."""
    ctx = _load_model_detail(model_id)
    if ctx is None:
        return "Model not found", 404
    return render_template("_model_detail_content.html", **ctx)


# ── Category views ──────────────────────────────────────────────────────

@app.route("/category/<category_key>")
def category_view(category_key):
    conn = storage.get_db()
    groups = storage.get_category_leaderboard(conn, category_key)
    cfg = config.load_config()
    sources_feeding = [
        {"key": k, **v} for k, v in cfg["sources"].items()
        if category_key in v.get("categories", []) or category_key in v.get("proxy_for", [])
    ]
    proxy_source_keys = {
        k for k, v in cfg["sources"].items() if category_key in v.get("proxy_for", [])
    }
    metric_groups = [
        {
            "score_type": score_type,
            "label": config.score_type_label(score_type),
            "description": config.score_type_description(score_type),
            "source": group_rows[0]["source"] if group_rows else None,
            "is_proxy": bool(group_rows) and group_rows[0]["source"] in proxy_source_keys,
            "rows": group_rows,
        }
        for score_type, group_rows in groups
    ]
    # Some categories (local_open_weight) are fed only by discovery/registry
    # sources that never carry a numeric score by design — a leaderboard
    # format doesn't fit them at all, but there can still be real models to
    # look at. Only bother with this count when there's no leaderboard to
    # show, since it's otherwise unused.
    model_count = None
    if not metric_groups:
        model_count = len(storage.list_models(conn, category=category_key))
    conn.close()
    return render_template(
        "category.html",
        category_key=category_key,
        category_name=config.category_name(category_key),
        metric_groups=metric_groups,
        sources_feeding=sources_feeding,
        proxy_source_keys=proxy_source_keys,
        categories=cfg["categories"],
        model_count=model_count,
    )


# ── Health ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    conn = storage.get_db()
    sources_health = {s["source_key"]: dict(s) for s in storage.get_sources_health(conn)}
    events = storage.get_recent_events(conn, limit=50)
    conn.close()
    cfg = config.load_config()
    sources = []
    for key, scfg in cfg["sources"].items():
        row = sources_health.get(key, {})
        sources.append({"key": key, "name": scfg["name"], "tier": scfg["tier"],
                         "categories": scfg.get("categories", []),
                         "last_polled_at": row.get("last_polled_at"),
                         "last_status": row.get("last_status", "never polled"),
                         "last_error": row.get("last_error")})
    return render_template("health.html", sources=sources, events=events,
                            show_poll_buttons=not IS_HOSTED_PUBLICLY)


# ── API ──────────────────────────────────────────────────────────────────

@app.route("/api/notes", methods=["POST"])
def api_add_note():
    data = request.get_json()
    name = (data.get("model_name") or "").strip()
    category = data.get("category")
    rating = data.get("rating")
    note = (data.get("note") or "").strip()
    valid_ratings = config.load_config()["manual_note_ratings"]
    if not name or not category or rating not in valid_ratings:
        return jsonify({"ok": False, "error": "missing or invalid fields"}), 400
    conn = storage.get_db()
    model_id = storage.find_or_create_model_by_name(conn, name)
    storage.add_manual_note(conn, model_id, category, rating, note)
    conn.close()
    return jsonify({"ok": True, "model_id": model_id})


@app.route("/api/poll/<source_key>", methods=["POST"])
def api_poll_source(source_key):
    if IS_HOSTED_PUBLICLY:
        return jsonify({"ok": False, "error": "manual per-source polling is disabled on the "
                                               "public deployment — it runs on the daily cron "
                                               "schedule instead"}), 403
    result = poller.poll_all([source_key])[source_key]
    return jsonify({"ok": "error" not in result, "result": result})


# ── Cron (Vercel) ────────────────────────────────────────────────────────
# Vercel Cron hits this on a schedule (see vercel.json) with an
# "Authorization: Bearer <CRON_SECRET>" header it adds automatically once
# CRON_SECRET is set as an env var — that's what stops anyone else on the
# public internet from triggering a poll. No CRON_SECRET set (e.g. on
# Thornwick, which isn't internet-facing) means this is open, same trust
# boundary as the existing LAN-only "Poll now" buttons on the health page.
@app.route("/cron/poll", methods=["GET", "POST"])
def cron_poll():
    secret = os.environ.get("CRON_SECRET")
    if secret and request.headers.get("Authorization") != f"Bearer {secret}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    summary = poller.poll_all()
    return jsonify({"ok": True, "summary": summary})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
