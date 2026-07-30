#!/usr/bin/env python3
"""
Offline tests for the DB schema hardening: new columns (source, updated_at),
the idempotent in-place migration of an older DB, provenance on insert, and the
performance indexes. Everything runs against temp/in-memory SQLite - no network.

Run with: pytest tests/test_db_schema.py
"""

import sqlite3

from govspend_free import db


# The pre-hardening documents schema, to simulate an existing/older database.
_LEGACY_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL, state TEXT, institution TEXT, title TEXT, url TEXT,
    text TEXT, date TEXT, categories TEXT, watchlist_hits TEXT,
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(doc_type, url, title)
);
"""


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# ------------------------------ fresh schema ------------------------------

def test_fresh_schema_has_new_columns(tmp_conn):
    cols = _cols(tmp_conn, "documents")
    assert "source" in cols and "updated_at" in cols


def test_insert_records_source_and_updated_at(tmp_conn):
    db.insert_document(tmp_conn, doc_type="federal_award", state="missouri",
                       institution="Lincoln University", title="grant", url="http://x/1",
                       text="Title III", source="usaspending")
    row = tmp_conn.execute("SELECT source, updated_at FROM documents").fetchone()
    assert row["source"] == "usaspending"
    assert row["updated_at"]  # populated on insert


def test_retag_bumps_updated_at(tmp_conn):
    doc_id = db.insert_document(tmp_conn, doc_type="bid", institution="U", title="t",
                               url="http://x/2", text="body", source="bids")
    tmp_conn.execute("UPDATE documents SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (doc_id,))
    db.update_document_tags(tmp_conn, doc_id, ["Student Success & Retention"], [])
    after = tmp_conn.execute("SELECT updated_at FROM documents WHERE id = ?", (doc_id,)).fetchone()["updated_at"]
    assert after != "2000-01-01 00:00:00"  # refreshed by the tag update


# ------------------------------ migration ------------------------------

def test_migrate_adds_columns_and_backfills():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEGACY_SCHEMA)
    # Seed legacy rows with no source/updated_at columns at all.
    conn.execute("INSERT INTO documents (doc_type, state, institution, title, url, scraped_at) "
                 "VALUES ('board_minutes','ar','UA','m','http://x/a','2025-01-01 00:00:00')")
    conn.execute("INSERT INTO documents (doc_type, state, institution, title, url, scraped_at) "
                 "VALUES ('federal_award','mo','Lincoln','g','http://x/b','2025-02-02 00:00:00')")
    conn.commit()
    assert "source" not in _cols(conn, "documents")

    db._migrate(conn)

    cols = _cols(conn, "documents")
    assert "source" in cols and "updated_at" in cols
    rows = {r["doc_type"]: r for r in conn.execute("SELECT doc_type, source, updated_at, scraped_at FROM documents")}
    assert rows["board_minutes"]["source"] == "board_minutes"     # inferred from doc_type
    assert rows["federal_award"]["source"] == "usaspending"
    assert rows["board_minutes"]["updated_at"] == "2025-01-01 00:00:00"  # backfilled from scraped_at


def test_migrate_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    db._migrate(conn)
    db._migrate(conn)  # second run must not raise (duplicate-column) or change anything
    assert "source" in _cols(conn, "documents")


# ------------------------------ indexes ------------------------------

def test_indexes_are_created():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    db._migrate(conn)
    conn.executescript(db._INDEXES)
    idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for expected in ("idx_documents_doc_type", "idx_documents_state",
                     "idx_documents_institution", "idx_documents_source",
                     "idx_contracts_expiration"):
        assert expected in idx


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
