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
    )
    assert result.bids == [] and result.contracts == [] and result.skipped == []
    counts = result.counts()
    assert counts["bids"] == 0
    json.dumps(counts)  # counts() must be JSON-serializable for the UI


def test_parse_date():
    from datetime import date
    assert utils.parse_date("2026-03-09") == date(2026, 3, 9)
    assert utils.parse_date("3/14/2026") == date(2026, 3, 14)
    assert utils.parse_date("2026-03-09-minutes.pdf") == date(2026, 3, 9)  # embedded in filename
    assert utils.parse_date("no date here") is None
    assert utils.parse_date("") is None
    assert utils.parse_date(None) is None


def test_scrape_criteria():
    c = pipeline.ScrapeCriteria.build(only_keywords="retention, attendance")
    assert c.active()
    assert c.keep("student retention program", None)
    assert not c.keep("parking lot striping", None)

    comp = pipeline.ScrapeCriteria.build(only_competitors="Ellucian, Civitas")
    assert comp.keep("migrating off Ellucian Banner", None)
    assert not comp.keep("unrelated text", None)

    rng = pipeline.ScrapeCriteria.build(date_from="2026-01-01", date_to="2026-06-30")
    assert rng.keep("x", "2026-03-09")
    assert not rng.keep("x", "2025-12-31")   # before range
    assert not rng.keep("x", "2026-07-01")   # after range
    assert rng.keep("x", "no parseable date")  # unparseable -> kept, never silently dropped

    empty = pipeline.ScrapeCriteria()
    assert not empty.active() and empty.keep("anything at all", None)


def test_retag_documents(tmp_conn):
    # A doc that should match the new keywords, and one that should match nothing.
    db.insert_document(
        tmp_conn, doc_type="board_minutes", state="or", institution="Oregon Tech",
        title="Board Minutes", url="http://x/1",
        text="discussion of persistence and space utilization and SACSCOC reaffirmation",
        categories=["Stale Old Label"],
    )
    db.insert_document(
        tmp_conn, doc_type="board_minutes", state="or", institution="X", title="Board Minutes",
        url="http://x/2", text="approval of prior minutes and adjournment", categories=["Stale Old Label"],
    )
    kw = {
        "categories": {
            "sched": {"label": "Academic & Space Scheduling", "keywords": ["space utilization"]},
            "success": {"label": "Student Success & Retention", "keywords": ["persistence"]},
            "accred": {"label": "Accreditation", "keywords": ["reaffirmation", "SACSCOC"]},
        },
        "watchlist": ["SEAtS"],
    }
    stats = pipeline.retag_documents(tmp_conn, kw)
    assert stats["total"] == 2
    assert stats["changed"] == 2
    assert stats["now_empty"] == 1

    row1 = tmp_conn.execute("SELECT categories FROM documents WHERE id = 1").fetchone()
    assert "Student Success & Retention" in row1["categories"] and "Accreditation" in row1["categories"]
    row2 = tmp_conn.execute("SELECT categories, watchlist_hits FROM documents WHERE id = 2").fetchone()
    assert row2["categories"] == "" and row2["watchlist_hits"] == ""


def test_run_scrape_unknown_state_raises(tmp_conn):
    with pytest.raises(ValueError):
        pipeline.run_scrape(
            tmp_conn, {"texas": {}}, {"categories": {}, "watchlist": []},
            selected_state="atlantis",
            skip_bids=True, skip_board_minutes=True, skip_transparency=True,
            skip_contracts=True, skip_contacts=True,
        )


def test_run_scrape_accepts_a_list_of_states(tmp_conn):
    # A subset (list) runs just those states, stays offline, and raises on a bad key.
    result = pipeline.run_scrape(
        tmp_conn, {"texas": {}, "ohio": {}, "iowa": {}}, {"categories": {}, "watchlist": []},
        selected_state=["texas", "ohio"],
        skip_bids=True, skip_board_minutes=True, skip_transparency=True,
        skip_contracts=True, skip_contacts=True, skip_federal=True,
    )
    assert result.counts()["bids"] == 0 and result.skipped == []

    with pytest.raises(ValueError):
        pipeline.run_scrape(
            tmp_conn, {"texas": {}, "ohio": {}}, {"categories": {}, "watchlist": []},
            selected_state=["texas", "atlantis"],
            skip_bids=True, skip_board_minutes=True, skip_transparency=True,
            skip_contracts=True, skip_contacts=True, skip_federal=True,
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


def test_api_start_brief_guards(monkeypatch):
    # Both guard paths return before any thread/claude call, so this is safe
    # to run headlessly.
    api = desktop.Api()
    monkeypatch.setattr(desktop.brief, "claude_available", lambda: False)
    r = api.start_brief("1")
    assert r["started"] is False and "claude" in r["error"].lower()

    monkeypatch.setattr(desktop.brief, "claude_available", lambda: True)
    api._briefing = True  # simulate a brief already in flight
    r2 = api.start_brief("1")
    assert r2["started"] is False


def test_api_home_stats(api_on_seeded_db):
    s = api_on_seeded_db.home_stats()
    assert set(s) == {"cards", "top_opportunities", "top_competitors", "has_data"}
    assert s["has_data"] is True          # seeded DB has 1 document
    assert len(s["cards"]) == 6
    for c in s["cards"]:
        assert c["rag"] in {"green", "amber", "red", "gray"}
        assert "value" in c and "sub" in c
    json.dumps(s)  # the bridge serializes to the page
    # Seeded DB: 1 document, a contract 120d out (within 180, not 90), no payments.
    by_label = {c["label"]: c for c in s["cards"]}
    assert by_label["Documents"]["value"] == "1"
    exp = next(c for c in s["cards"] if "Expiring" in c["label"])
    assert exp["value"] == "0" and exp["rag"] == "amber"


def test_api_list_states_reads_config():
    # No DB needed - reads config/sources.yaml. Confirms the 10 pilot states.
    states = desktop.Api().list_states()
    assert "texas" in states and "arkansas" in states and len(states) >= 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
