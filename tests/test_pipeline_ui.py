#!/usr/bin/env python3
"""
Offline tests for the scrape-orchestration refactor (pipeline.py) and the
desktop UI's Python data layer (desktop.Api). The UI's actual window can't be
opened headlessly, but everything behind the pywebview bridge - the queries and
their JSON-serializable output - is tested here.

Run with: pytest tests/test_pipeline_ui.py   (or just `pytest`)
"""

import csv
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from govspend_free import brief, db, desktop, pipeline, utils


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
    assert result.report_paths == []
    counts = result.counts()
    assert counts["bids"] == 0 and counts["reports"] == 0
    json.dumps(counts)  # counts() must be JSON-serializable for the UI


def test_run_scrape_unknown_state_raises(tmp_conn):
    with pytest.raises(ValueError):
        pipeline.run_scrape(
            tmp_conn, {"texas": {}}, {"categories": {}, "watchlist": []},
            selected_state="atlantis",
            skip_bids=True, skip_board_minutes=True, skip_transparency=True,
            skip_contracts=True, skip_contacts=True, write_report=False,
        )


def test_write_reports_splits_by_type_and_state(monkeypatch):
    # Point REPORTS_DIR at a throwaway dir (not pytest's tmp_path, which is
    # blocked in this sandbox) and check the per-type/per-state layout.
    tmp = Path(tempfile.mkdtemp(prefix="gsf_reports_"))
    try:
        monkeypatch.setattr(utils, "REPORTS_DIR", tmp)
        result = pipeline.ScrapeResult(
            bids=[
                {"state": "texas", "institution": "UT", "title": "RFP A", "source_url": "http://x/a",
                 "detail_url": "http://x/a", "date": "", "categories": ["Retention"]},
                {"state": "florida", "institution": "UF", "title": "RFP B", "source_url": "http://x/b",
                 "detail_url": "", "date": "", "categories": ["Retention"]},
            ],
            contracts=[{"state": "texas", "institution": "UT", "vendor": "Acme", "start_date": "2024-01-01",
                        "end_date": "2026-01-01", "value": "1", "days_until_expiration": 10,
                        "expiring_soon": True, "source_url": "http://x/c"}],
            skipped=[{"state": "texas", "institution": "UT", "reason": "needs_browser", "url": "http://x",
                      "notes": "", "pass_type": "bids"}],
        )
        paths = pipeline.write_reports(result)
        rel = sorted(p.relative_to(tmp).as_posix() for p in paths)

        # bids split into two per-state files; contracts + skipped each get one.
        assert len(paths) == 4
        assert any(n.startswith("bids/bids_texas_") for n in rel)
        assert any(n.startswith("bids/bids_florida_") for n in rel)
        assert any(n.startswith("contracts/contracts_texas_") for n in rel)
        assert any(n.startswith("skipped/skipped_texas_") for n in rel)

        # Type-specific header + row content on the Texas bids file.
        bids_tx = next(p for p in paths if "bids_texas_" in p.name)
        rows = list(csv.reader(bids_tx.read_text(encoding="utf-8").splitlines()))
        assert rows[0] == ["state", "institution", "categories", "title", "url", "date", "description"]
        assert rows[1][0] == "texas" and rows[1][3] == "RFP A"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_brief_context_gathering(tmp_conn):
    # Exercises the DB-side of --brief (context assembly + input validation)
    # without invoking the `claude` CLI.
    db.insert_document(
        tmp_conn, doc_type="board_minutes", state="oregon",
        institution="Lane Community College", title="July 1 Board Minutes",
        url="http://example.test/lcc", text="retention 50-55% NWCCU accreditation reporting collapse",
    )
    inst, ctx, sources = brief._gather_context(tmp_conn, "Lane")
    assert inst == "Lane Community College"
    assert "NWCCU" in ctx and "DOCUMENT" in ctx
    assert sources and sources[0]["url"] == "http://example.test/lcc"

    inst_by_id, _, _ = brief._gather_context(tmp_conn, "1")
    assert inst_by_id == "Lane Community College"

    with pytest.raises(ValueError):
        brief._gather_context(tmp_conn, "no-such-institution")
    with pytest.raises(ValueError):
        brief._gather_context(tmp_conn, "9999")


def test_brief_auth_error_detection():
    assert brief._looks_like_auth_error('{"error":{"type":"authentication_error"}}')
    assert brief._looks_like_auth_error("OAuth access token is invalid.")
    assert not brief._looks_like_auth_error("normal model output about retention")


def test_api_list_states_reads_config():
    # No DB needed - reads config/sources.yaml. Confirms the 10 pilot states.
    states = desktop.Api().list_states()
    assert "texas" in states and "arkansas" in states and len(states) == 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
