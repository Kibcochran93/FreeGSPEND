#!/usr/bin/env python3
"""
Offline tests for the Opportunities/search fixes:
  - FTS5 query sanitization (search no longer crashes on '-', ':', '"', AND/OR)
  - document-date derivation from text
  - the Opportunities age cutoff (hide items whose own date is > N days old)
"""

import datetime as dt

from govspend_free import db, opportunities, utils


# --------------------------- FTS sanitization ---------------------------

def test_fts_match_query_quotes_tokens_and_survives_operators():
    assert utils.fts_match_query("K-12") == '"K-12"'
    assert utils.fts_match_query("student-success") == '"student-success"'
    assert utils.fts_match_query("a AND b") == '"a" "AND" "b"'      # AND is literal, not an operator
    assert utils.fts_match_query('vendor:foo') == '"vendor:foo"'
    assert utils.fts_match_query("   ") == ""                        # nothing searchable
    assert utils.fts_match_query('"') == ""                          # pure punctuation dropped


def test_search_does_not_crash_on_special_characters(tmp_conn):
    db.insert_document(tmp_conn, doc_type="bid", title="K-12 e-learning platform",
                       url="u1", text="student success and retention for K-12", source="bids")
    for q in ["K-12", "e-learning", "student-success", "AND", 'attendance "', "vendor:foo"]:
        rows = db.search(tmp_conn, q)          # must not raise
        assert isinstance(rows, list)
    assert len(db.search(tmp_conn, "K-12")) == 1, "the K-12 doc should be found"


# --------------------------- date derivation ---------------------------

def test_derive_doc_date_reads_meeting_date_from_text():
    assert utils.derive_doc_date("Agenda", "Board of Trustees meeting of March 9, 2026. Present:") == "2026-03-09"
    assert utils.derive_doc_date("Minutes", "SEPTEMBER 11, 2024 regular session") == "2024-09-11"
    assert utils.derive_doc_date("x", "adopted 2025-05-20 by the board") == "2025-05-20"
    assert utils.derive_doc_date("no date here", "just some text") == ""
    assert utils.derive_doc_date("", "") == ""


# --------------------------- age cutoff ---------------------------

def _add_dated(conn, title, date_str):
    db.insert_document(conn, doc_type="board_minutes", title=title, url=title,
                       text="attendance", date=date_str,
                       categories=["Attendance & Compliance"], source="board_minutes")


def test_rank_opportunities_hides_items_older_than_cutoff(tmp_conn):
    today = dt.date.today()
    _add_dated(tmp_conn, "recent", (today - dt.timedelta(days=30)).isoformat())
    _add_dated(tmp_conn, "old", (today - dt.timedelta(days=400)).isoformat())   # > 1 year
    _add_dated(tmp_conn, "undated", "")                                          # no date -> always kept

    default = {o["title"] for o in opportunities.rank_opportunities(tmp_conn, limit=100)}
    assert "recent" in default
    assert "undated" in default, "items with no determinable date must be kept"
    assert "old" not in default, "items dated > 1 year old must be hidden by default"

    everything = {o["title"] for o in opportunities.rank_opportunities(tmp_conn, limit=100, max_age_days=None)}
    assert {"recent", "old", "undated"} <= everything, "max_age_days=None must include the old item"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
