"""Persistence for The Model Cantina — backend-agnostic (see db.py).

Pattern borrowed from content-radar's storage.py: get_db() applies inline
idempotent schema creation, an append-only event_log records everything
(LOG EVERYTHING), and callers get a small set of high-level functions
rather than writing raw SQL themselves. As of the Vercel deployment work,
the actual SQLite-vs-Postgres connection handling lives in db.py — this
module only ever calls conn.execute(sql, params) with `?` placeholders and
dict-like row access, which works against either backend.
"""

import json
from datetime import datetime, timezone

import db

_VALID_EVENT_TYPES = {
    "poll_started",
    "poll_completed",
    "poll_failed",
    "new_model_found",
    "score_recorded",
    "note_added",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    return db.get_db()


def log_event(conn, event_type, source=None, model_id=None, payload=None):
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(f"invalid event_type: {event_type!r}")
    conn.execute(
        "INSERT INTO event_log (event_type, source, model_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, source, model_id, json.dumps(payload) if payload is not None else None, now_iso()),
    )


def _get_or_create_model(conn, name, org=None, weight_availability=None,
                          local_runnable=None, modalities=None, release_date=None):
    # NULL-safe equality on org, done by branching in Python rather than a
    # SQL operator: "IS NOT DISTINCT FROM" needs SQLite 3.39+ (this box's
    # bundled sqlite3 is 3.38.4 — checked, not assumed) and plain "IS" with
    # a bound parameter isn't portable to Postgres either. Two plain queries
    # sidesteps the whole dialect question.
    if org is None:
        row = conn.execute(
            "SELECT id FROM models WHERE name = ? AND org IS NULL", (name,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM models WHERE name = ? AND org = ?", (name, org)
        ).fetchone()
    if row:
        model_id = row["id"]
        # Refresh mutable attributes if the source has an updated view of them.
        updates, params = [], []
        if weight_availability is not None:
            updates.append("weight_availability = ?")
            params.append(weight_availability)
        if local_runnable is not None:
            updates.append("local_runnable = ?")
            params.append(1 if local_runnable else 0)
        if modalities is not None:
            updates.append("modalities = ?")
            params.append(json.dumps(modalities))
        if release_date is not None:
            updates.append("release_date = ?")
            params.append(release_date)
        if updates:
            params.append(model_id)
            conn.execute(f"UPDATE models SET {', '.join(updates)} WHERE id = ?", params)
        return model_id, False

    cur = conn.execute(
        "INSERT INTO models (name, org, first_seen_at, release_date, weight_availability, "
        "local_runnable, modalities) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            name,
            org,
            now_iso(),
            release_date,
            weight_availability,
            1 if local_runnable else 0,
            json.dumps(modalities) if modalities is not None else None,
        ),
    )
    return cur.fetchone()["id"], True


def _update_source(conn, source_key, tier, status, error=None):
    conn.execute(
        "INSERT INTO sources (source_key, tier, last_polled_at, last_status, last_error) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_key) DO UPDATE SET "
        "tier=excluded.tier, last_polled_at=excluded.last_polled_at, "
        "last_status=excluded.last_status, last_error=excluded.last_error",
        (source_key, tier, now_iso(), status, error),
    )


def record_poll_results(conn, source_key, tier, records):
    """records: list of dicts with keys name, org (optional), weight_availability
    (optional), local_runnable (optional), modalities (optional), category, score,
    score_type, raw_payload (optional dict).
    Commits on success. Logs one poll_completed event with summary counts, plus
    one new_model_found event per newly discovered model.

    Batches DB round-trips rather than doing one SELECT+INSERT per record —
    against a local SQLite file the per-record cost is negligible, but against
    a real remote Postgres (Vercel/Neon) each round-trip is real network
    latency: a ~230-record source at one round-trip per record measured at
    68s total for a full 11-source poll, over Vercel Hobby's 60s function
    ceiling. One bulk SELECT to resolve already-known models, plus one
    executemany() for all score inserts, is what makes the poll fit.
    """
    if not records:
        _update_source(conn, source_key, tier, "ok")
        log_event(conn, "poll_completed", source=source_key,
                   payload={"new_models": 0, "scores_recorded": 0})
        conn.commit()
        return {"new_models": 0, "scores_recorded": 0}

    distinct_names = list({r["name"] for r in records})
    placeholders = ", ".join("?" for _ in distinct_names)
    existing_rows = conn.execute(
        f"SELECT id, name, org FROM models WHERE name IN ({placeholders})", distinct_names
    ).fetchall()
    existing_by_key = {(row["name"], row["org"]): row["id"] for row in existing_rows}

    new_models = 0
    model_id_by_key = {}
    for r in records:
        key = (r["name"], r.get("org"))
        if key in model_id_by_key:
            continue
        if key in existing_by_key:
            model_id = existing_by_key[key]
            # Refresh mutable attributes for models we already knew about —
            # small subset of the batch in practice, individual round-trips
            # here are fine (the score-insert loop below is the hot path).
            updates, params = [], []
            if r.get("weight_availability") is not None:
                updates.append("weight_availability = ?")
                params.append(r["weight_availability"])
            if r.get("local_runnable") is not None:
                updates.append("local_runnable = ?")
                params.append(1 if r["local_runnable"] else 0)
            if r.get("modalities") is not None:
                updates.append("modalities = ?")
                params.append(json.dumps(r["modalities"]))
            if r.get("release_date") is not None:
                updates.append("release_date = ?")
                params.append(r["release_date"])
            if updates:
                params.append(model_id)
                conn.execute(f"UPDATE models SET {', '.join(updates)} WHERE id = ?", params)
        else:
            model_id, created = _get_or_create_model(
                conn, r["name"], org=r.get("org"),
                weight_availability=r.get("weight_availability"),
                local_runnable=r.get("local_runnable"),
                modalities=r.get("modalities"),
                release_date=r.get("release_date"),
            )
            if created:
                new_models += 1
                log_event(conn, "new_model_found", source=source_key, model_id=model_id,
                           payload={"name": r["name"], "org": r.get("org")})
        model_id_by_key[key] = model_id

    collected_at = now_iso()
    score_rows = [
        (
            model_id_by_key[(r["name"], r.get("org"))],
            source_key,
            r["category"],
            r.get("score"),
            r.get("score_type"),
            collected_at,
            json.dumps(r["raw_payload"]) if r.get("raw_payload") is not None else None,
        )
        for r in records
    ]
    conn.executemany(
        "INSERT INTO scores (model_id, source, category, score, score_type, "
        "collected_at, raw_payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        score_rows,
    )
    scores_recorded = len(score_rows)

    _update_source(conn, source_key, tier, "ok")
    log_event(conn, "poll_completed", source=source_key,
               payload={"new_models": new_models, "scores_recorded": scores_recorded})
    conn.commit()
    return {"new_models": new_models, "scores_recorded": scores_recorded}


def record_poll_failure(conn, source_key, tier, error):
    _update_source(conn, source_key, tier, "error", error=str(error))
    log_event(conn, "poll_failed", source=source_key, payload={"error": str(error)})
    conn.commit()


def add_manual_note(conn, model_id, category, rating, note):
    conn.execute(
        "INSERT INTO manual_notes (model_id, category, rating, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (model_id, category, rating, note, now_iso()),
    )
    log_event(conn, "note_added", model_id=model_id,
               payload={"category": category, "rating": rating})
    conn.commit()


def find_or_create_model_by_name(conn, name, org=None):
    """Used by the manual-note form to resolve/create a model by plain name.

    Looks up by name alone first — the note form only has a name field, and
    requiring an exact org match (like the source-ingestion path does) would
    silently create a duplicate model row whenever a note is added for a
    model that a source already recorded with an org attached.
    """
    row = conn.execute(
        "SELECT id FROM models WHERE name = ? ORDER BY id LIMIT 1", (name,)
    ).fetchone()
    if row:
        return row["id"]

    cur = conn.execute(
        "INSERT INTO models (name, org, first_seen_at) VALUES (?, ?, ?) RETURNING id",
        (name, org, now_iso()),
    )
    model_id = cur.fetchone()["id"]
    log_event(conn, "new_model_found", source="manual", model_id=model_id,
               payload={"name": name, "org": org})
    conn.commit()
    return model_id


# ---------------------------------------------------------------------------
# Read queries for the CLI and dashboard
# ---------------------------------------------------------------------------

def list_models(conn, category=None, local_runnable=None, org=None):
    query = "SELECT DISTINCT models.* FROM models"
    joins, wheres, params = [], [], []
    if category:
        joins.append("JOIN scores ON scores.model_id = models.id")
        wheres.append("scores.category = ?")
        params.append(category)
    if local_runnable is not None:
        wheres.append("models.local_runnable = ?")
        params.append(1 if local_runnable else 0)
    if org:
        wheres.append("models.org = ?")
        params.append(org)
    sql = query + (" " + " ".join(joins) if joins else "")
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY models.name"
    rows = conn.execute(sql, params).fetchall()

    if not category or not rows:
        return rows

    # Attach one representative score per model for this category — a model
    # can have scores from several sources in the same category (e.g.
    # "coding" has SWE-bench, LiveCodeBench, Aider...), so this isn't a
    # join (that would duplicate rows); take the most recently collected
    # one per model instead. Not the same "compare everything" view as the
    # category leaderboard page — this is just "does this model have a
    # score here at all, and what's the latest one."
    model_ids = [r["id"] for r in rows]
    placeholders = ", ".join("?" for _ in model_ids)
    score_rows = conn.execute(
        f"SELECT model_id, score, score_type, source, id FROM scores "
        f"WHERE category = ? AND model_id IN ({placeholders}) ORDER BY id DESC",
        [category, *model_ids],
    ).fetchall()
    latest_by_model = {}
    for sr in score_rows:
        latest_by_model.setdefault(sr["model_id"], sr)  # first hit = highest id, since DESC

    enriched = []
    for r in rows:
        latest = latest_by_model.get(r["id"])
        row = dict(r)
        row["category_score"] = latest["score"] if latest else None
        row["category_score_type"] = latest["score_type"] if latest else None
        row["category_score_source"] = latest["source"] if latest else None
        enriched.append(row)
    return enriched


def get_model(conn, model_id):
    return conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()


def get_scores_for_model(conn, model_id):
    return conn.execute(
        "SELECT * FROM scores WHERE model_id = ? ORDER BY collected_at DESC", (model_id,)
    ).fetchall()


def get_notes_for_model(conn, model_id):
    return conn.execute(
        "SELECT * FROM manual_notes WHERE model_id = ? ORDER BY created_at DESC", (model_id,)
    ).fetchall()


def get_category_leaderboard(conn, category):
    """Latest score per (model, source) for this category, newest first by score."""
    return conn.execute(
        """
        SELECT s.*, m.name as model_name, m.org as model_org, m.release_date as model_release_date
        FROM scores s
        JOIN models m ON m.id = s.model_id
        WHERE s.category = ?
        AND s.id IN (
            SELECT MAX(id) FROM scores
            WHERE category = ? GROUP BY model_id, source
        )
        ORDER BY s.score DESC
        """,
        (category, category),
    ).fetchall()


def get_sources_health(conn):
    return conn.execute("SELECT * FROM sources ORDER BY source_key").fetchall()


def get_whats_new(conn, since_iso, limit=50):
    new_models = conn.execute(
        "SELECT * FROM models WHERE first_seen_at >= ? ORDER BY first_seen_at DESC LIMIT ?",
        (since_iso, limit),
    ).fetchall()
    recent_scores = conn.execute(
        """
        SELECT s.*, m.name as model_name FROM scores s
        JOIN models m ON m.id = s.model_id
        WHERE s.collected_at >= ?
        ORDER BY s.collected_at DESC LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()
    return new_models, recent_scores


def get_recent_events(conn, event_type=None, limit=50):
    if event_type:
        return conn.execute(
            "SELECT * FROM event_log WHERE event_type = ? ORDER BY created_at DESC LIMIT ?",
            (event_type, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM event_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_stats(conn):
    model_count = conn.execute("SELECT COUNT(*) c FROM models").fetchone()["c"]
    score_count = conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"]
    note_count = conn.execute("SELECT COUNT(*) c FROM manual_notes").fetchone()["c"]
    sources = get_sources_health(conn)
    return {
        "model_count": model_count,
        "score_count": score_count,
        "note_count": note_count,
        "sources": [dict(s) for s in sources],
    }
