#!/usr/bin/env python3
"""
Offline tests for the scrape-orchestration refactor (pipeline.py) and the
desktop UI's Python data layer (desktop.Api). The UI's actual window can't be
opened headlessly, but everything behind the pywebview bridge - the queries and
their JSON-serializable output - is tested here.

Run with: pytest tests/test_pipeline_ui.py   (or just `pytest`)
"""

import json
import sqlite3

import pytest

from govspend_free import db, desktop, pipeline


# --------------------------- pipeline.run_scrape ---------------------------

def test_run_scrape_all_skipped_is_empty(tmp_conn):
    # With every pass skipped there's nothing to fetch, so this stays fully
    # offline and must produce an empty-but-well-formed result.
    result = pipeline.run_scrape(
        tmp_conn, {"texas": {}}, {"categories": {}, "watchlist": []},
        selected_state="texas",
        skip_bids=True, skip_board_minutes=True, skip_transparency=True,
        skip_contracts=True, skip_contacts=True,
        write_report=False,
    )
    assert result.bids == [] and result.contracts == [] and result.skipped == []
    counts = result.counts()
    assert counts["bids"] == 0 and counts["report_path"] is None
    json.dumps(counts)  # counts() must be JSON-serializable for the UI


def test_run_scrape_unknown_state_raises(tmp_conn):
    with pytest.raises(ValueError):
        pipeline.run_scrape(
            tmp_conn, {"texas": {}}, {"categories": {}, "watchlist": []},
            selected_state="atlantis",
            skip_bids=True, skip_board_minutes=True, skip_transparency=True,
            skip_contracts=True, skip_contacts=True, write_report=False,
        )


# ------------------------------ desktop.Api ------------------------------

def _seed(conn):
    db.insert_document(
        conn, doc_type="bid", state="texas", institution="Test U",
        title="RFP - Early Alert Retention Platform", url="http://example.test/1",
        text="student retention early alert", categories=["Student Success & Retention"],
    )
    db.insert_contract(
        conn, state="texas", institution="Test U", vendor="Acme Software",
        start_date="2024-01-01", end_date="2026-12-31", value="50000",
        days_until_expiration=120, source_url="http://example.test/contracts.csv",
    )
    conn.commit()


@pytest.fixture
def api_on_seeded_db(monkeypatch):
    # A named, shared-cache in-memory DB avoids the filesystem entirely. The
    # "keeper" connection stays open so the DB survives the Api methods opening
    # and closing their own short-lived connections to the same name.
    uri = "file:govspend_ui_test?mode=memory&cache=shared"
    keeper = sqlite3.connect(uri, uri=True)
    keeper.row_factory = sqlite3.Row
    keeper.executescript(db._SCHEMA)
    _seed(keeper)

    def fake_get_conn():
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    try:
        yield desktop.Api()
    finally:
        keeper.close()


def test_api_opportunities_json_serializable(api_on_seeded_db):
    opps = api_on_seeded_db.opportunities()
    assert isinstance(opps, list) and opps
    assert opps[0]["title"].startswith("RFP")
    json.dumps(opps)  # the bridge serializes return values to the page


def test_api_search(api_on_seeded_db):
    hits = api_on_seeded_db.search("retention")
    assert hits and any("Retention" in h["title"] for h in hits)
    json.dumps(hits)
    # Empty / whitespace query short-circuits without touching the DB.
    assert api_on_seeded_db.search("   ") == []


def test_api_search_malformed_query_is_safe(api_on_seeded_db):
    # FTS5 raises on some punctuation; the Api should swallow it, not crash.
    assert api_on_seeded_db.search('"') == []


def test_api_expirations(api_on_seeded_db):
    exps = api_on_seeded_db.expirations(180)
    assert exps and exps[0]["vendor"] == "Acme Software"
    json.dumps(exps)
    assert api_on_seeded_db.expirations(30) == []  # 120 days out, outside window


def test_api_list_states_reads_config():
    # No DB needed - reads config/sources.yaml. Confirms the 10 pilot states.
    states = desktop.Api().list_states()
    assert "texas" in states and "arkansas" in states and len(states) == 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
