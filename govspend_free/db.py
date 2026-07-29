"""
Persistent SQLite store shared across runs. This is what makes "unified
full-text search" and "opportunity scoring" possible - CSV reports are
per-run and thrown away conceptually, but everything that gets inserted
here accumulates over time.

Schema:
  documents   - one row per bid/minutes-doc/transparency-hit/contract that
                matched a category or watchlist term. Mirrored into an
                FTS5 virtual table for full-text search.
  contracts   - vendor/start/end date rows pulled out of transparency CSVs.
  contacts    - people found via the Apollo Contacts module.

No ORM on purpose - this is a personal script, not a service; plain SQL
keeps it easy to read and to poke at directly with the `sqlite3` CLI if
you want to (`sqlite3 db/govspend_free.db`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import utils

DB_PATH = utils.ROOT_DIR / "db" / "govspend_free.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,        -- 'bid' | 'board_minutes' | 'transparency'
    state TEXT,
    institution TEXT,
    title TEXT,
    url TEXT,
    text TEXT,
    date TEXT,
    categories TEXT,                -- comma-joined category labels
    watchlist_hits TEXT,            -- comma-joined watchlist terms
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(doc_type, url, title)
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, text, content='documents', content_rowid='id'
);

-- INVARIANT: documents rows are insert-only (never UPDATEd or DELETEd), so
-- an AFTER INSERT trigger is enough to keep the external-content FTS index in
-- sync. If you ever add updates or deletes, you MUST also add matching
-- 'documents_ad'/'documents_au' triggers (the standard fts5 external-content
-- pattern) or full-text search will silently return stale rows.
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;

CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT,
    institution TEXT,
    vendor TEXT,
    start_date TEXT,
    end_date TEXT,
    value TEXT,
    days_until_expiration INTEGER,
    source_url TEXT,
    scraped_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apollo_id TEXT UNIQUE,
    state TEXT,
    institution TEXT,
    name TEXT,
    title TEXT,
    email TEXT,
    linkedin_url TEXT,
    organization_name TEXT,
    scraped_at TEXT DEFAULT (datetime('now'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Could not initialize the local database at {DB_PATH} ({exc}). "
            "This usually means the project folder is on a network drive, a "
            "FUSE/cloud-sync mount, or another filesystem that doesn't support "
            "SQLite's normal file locking. Fix: copy this whole project folder "
            "to a plain local folder (not a network/synced drive) and run it "
            "from there."
        ) from exc
    return conn


def insert_document(conn: sqlite3.Connection, *, doc_type: str, state: str = "",
                     institution: str = "", title: str = "", url: str = "",
                     text: str = "", date: str = "", categories: list[str] | None = None,
                     watchlist_hits: list[str] | None = None) -> int | None:
    """Insert a document, skipping if (doc_type, url, title) was already
    stored (UNIQUE constraint). Returns the new row id, or None if it was
    a duplicate (or url/title were both empty, which we don't store)."""
    if not url and not title:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO documents (doc_type, state, institution, title, url, text, date, categories, watchlist_hits) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                doc_type, state, institution, title, url, text, date,
                ", ".join(categories or []),
                ", ".join(watchlist_hits or []),
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already have this exact document


def insert_contract(conn: sqlite3.Connection, *, state: str = "", institution: str = "",
                     vendor: str = "", start_date: str = "", end_date: str = "",
                     value: str = "", days_until_expiration: int | None = None,
                     source_url: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO contracts (state, institution, vendor, start_date, end_date, value, "
        "days_until_expiration, source_url) VALUES (?,?,?,?,?,?,?,?)",
        (state, institution, vendor, start_date, end_date, value, days_until_expiration, source_url),
    )
    conn.commit()
    return cur.lastrowid


def insert_contact(conn: sqlite3.Connection, *, apollo_id: str | None, state: str = "",
                    institution: str = "", name: str = "", title: str = "",
                    email: str | None = None, linkedin_url: str | None = None,
                    organization_name: str = "") -> int | None:
    try:
        cur = conn.execute(
            "INSERT INTO contacts (apollo_id, state, institution, name, title, email, "
            "linkedin_url, organization_name) VALUES (?,?,?,?,?,?,?,?)",
            (apollo_id, state, institution, name, title, email, linkedin_url, organization_name),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already have this Apollo person


def search(conn: sqlite3.Connection, query: str, limit: int = 25) -> list[sqlite3.Row]:
    """Full-text search across every document ever stored (all-time,
    cumulative across runs), ranked by FTS5's built-in relevance."""
    return conn.execute(
        "SELECT d.id, d.doc_type, d.state, d.institution, d.title, d.url, d.date, "
        "       d.categories, d.watchlist_hits, "
        "       snippet(documents_fts, 1, '[', ']', '...', 12) AS snippet "
        "FROM documents_fts "
        "JOIN documents d ON d.id = documents_fts.rowid "
        "WHERE documents_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()


def all_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM documents ORDER BY scraped_at DESC").fetchall()


def existing_apollo_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT apollo_id FROM contacts WHERE apollo_id IS NOT NULL").fetchall()
    return {r["apollo_id"] for r in rows}


def upcoming_expirations(conn: sqlite3.Connection, within_days: int = 180) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE days_until_expiration IS NOT NULL "
        "AND days_until_expiration BETWEEN 0 AND ? ORDER BY days_until_expiration ASC",
        (within_days,),
    ).fetchall()
