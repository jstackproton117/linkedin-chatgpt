"""DB connection abstraction — SQLite (default, used by Thornwick/local dev)
or Postgres (used when DATABASE_URL is set, e.g. on Vercel).

storage.py's business logic calls conn.execute(sql, params) with `?`
placeholders and dict-like row access (row["col"]) against either backend —
this module hides the difference so storage.py stays backend-agnostic.

Schema DDL is intentionally duplicated per-backend rather than shared: the
syntax differences (AUTOINCREMENT vs IDENTITY) aren't worth papering over
with a lowest-common-denominator dialect.
"""

import os


def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return _get_postgres_db(database_url)
    return _get_sqlite_db()


class SQLiteConnection:
    """Thin pass-through — sqlite3's own execute()/Row already give us the
    interface storage.py wants."""

    def __init__(self, conn):
        self._conn = conn
        self.is_postgres = False

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def executemany(self, sql, params_list):
        self._conn.executemany(sql, params_list)

    def executescript(self, sql):
        self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class PostgresConnection:
    def __init__(self, conn):
        self._conn = conn
        self.is_postgres = True

    def execute(self, sql, params=()):
        # Safe here: no query in this codebase has a literal '?' inside a
        # string value, so a blind replace is fine — no real SQL parser needed.
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql, params_list):
        # One round-trip for the whole batch instead of one per row — this is
        # what makes a full poll fit inside Vercel's function time limit
        # against a real remote Postgres (measured: without this, ~1,300
        # individual score inserts alone pushed a full poll to 68s against
        # Neon, over Hobby's 60s ceiling).
        cur = self._conn.cursor()
        cur.executemany(sql.replace("?", "%s"), params_list)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _get_sqlite_db():
    import sqlite3
    from pathlib import Path

    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(data_dir / "cantina.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    wrapper = SQLiteConnection(conn)
    _init_tables(wrapper)
    return wrapper


def _get_postgres_db(database_url):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(database_url, row_factory=dict_row)
    wrapper = PostgresConnection(conn)
    _init_tables(wrapper)
    return wrapper


_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    org TEXT,
    first_seen_at TEXT NOT NULL,
    release_date TEXT,
    weight_availability TEXT,
    local_runnable INTEGER DEFAULT 0,
    modalities TEXT,
    notes TEXT,
    UNIQUE(name, org)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id),
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    score REAL,
    score_type TEXT,
    collected_at TEXT NOT NULL,
    raw_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_model ON scores(model_id);
CREATE INDEX IF NOT EXISTS idx_scores_category ON scores(category);
CREATE INDEX IF NOT EXISTS idx_scores_source ON scores(source);
CREATE INDEX IF NOT EXISTS idx_scores_collected ON scores(collected_at DESC);

CREATE TABLE IF NOT EXISTS manual_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id),
    category TEXT NOT NULL,
    rating TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_model ON manual_notes(model_id);

CREATE TABLE IF NOT EXISTS sources (
    source_key TEXT PRIMARY KEY,
    tier TEXT,
    last_polled_at TEXT,
    last_status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source TEXT,
    model_id INTEGER,
    payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_log(event_type);
CREATE INDEX IF NOT EXISTS idx_event_created ON event_log(created_at DESC);
"""

_DDL_POSTGRES = [
    """
    CREATE TABLE IF NOT EXISTS models (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL,
        org TEXT,
        first_seen_at TEXT NOT NULL,
        release_date TEXT,
        weight_availability TEXT,
        local_runnable INTEGER DEFAULT 0,
        modalities TEXT,
        notes TEXT,
        UNIQUE(name, org)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scores (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        model_id INTEGER NOT NULL REFERENCES models(id),
        source TEXT NOT NULL,
        category TEXT NOT NULL,
        score REAL,
        score_type TEXT,
        collected_at TEXT NOT NULL,
        raw_payload TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scores_model ON scores(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_scores_category ON scores(category)",
    "CREATE INDEX IF NOT EXISTS idx_scores_source ON scores(source)",
    "CREATE INDEX IF NOT EXISTS idx_scores_collected ON scores(collected_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS manual_notes (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        model_id INTEGER NOT NULL REFERENCES models(id),
        category TEXT NOT NULL,
        rating TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_model ON manual_notes(model_id)",
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_key TEXT PRIMARY KEY,
        tier TEXT,
        last_polled_at TEXT,
        last_status TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        event_type TEXT NOT NULL,
        source TEXT,
        model_id INTEGER,
        payload TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_type ON event_log(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_event_created ON event_log(created_at DESC)",
]


def _init_tables(wrapper):
    if wrapper.is_postgres:
        for stmt in _DDL_POSTGRES:
            wrapper.execute(stmt)
    else:
        wrapper.executescript(_DDL_SQLITE)
    wrapper.commit()
    _run_migrations(wrapper)


def _run_migrations(wrapper):
    """Idempotent ALTER TABLEs for columns added after the initial schema —
    CREATE TABLE IF NOT EXISTS above only helps on a fresh database, not an
    existing one that predates the column."""
    if wrapper.is_postgres:
        # Postgres supports IF NOT EXISTS on ADD COLUMN natively.
        wrapper.execute("ALTER TABLE models ADD COLUMN IF NOT EXISTS release_date TEXT")
    else:
        # SQLite has no IF NOT EXISTS for ADD COLUMN — try and ignore the
        # "duplicate column" error if it's already there, same pattern
        # content-radar uses for its own inline migrations.
        try:
            wrapper.execute("ALTER TABLE models ADD COLUMN release_date TEXT")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    wrapper.commit()
