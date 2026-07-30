#!/usr/bin/env python3
"""
Offline tests for the USAspending federal-grant source (usaspending_scraper.py)
and its pipeline wiring. No network: the API page fetch (_request_page) is
monkeypatched, so we exercise parsing, higher-ed filtering, tagging, dedup,
pagination, error handling, and DB storage without any HTTP.

Run with: pytest tests/test_usaspending.py
"""

import requests

from govspend_free import db, pipeline, usaspending_scraper as usa, utils


def _award(recipient, cfda="84.031", amount=1_000_000, gid="ABC-1", award_id="AID-1"):
    return {
        "Recipient Name": recipient, "CFDA Number": cfda, "Award Amount": amount,
        "Awarding Agency": "Department of Education", "Start Date": "2024-01-01",
        "End Date": "2029-12-31", "Award ID": award_id, "generated_internal_id": gid,
    }


def _page(results, has_next=False):
    return {"results": results, "page_metadata": {"hasNext": has_next}}


def _matchers():
    return utils.build_category_matchers({}), utils.build_watchlist_matchers([])


# ------------------------------ parsing / tagging ------------------------------

def test_parses_recipient_and_tags(monkeypatch):
    monkeypatch.setattr(usa, "_request_page",
                        lambda *a, **k: _page([_award("Lincoln University", amount=19_340_711)]))
    cats, watch = _matchers()
    matches, skipped = usa.scrape_usaspending({"state": "MO"}, None, set(), cats, watch)
    assert skipped == [] and len(matches) == 1
    m = matches[0]
    assert m["institution"] == "Lincoln University"          # recipient becomes the institution
    assert m["amount_str"] == "$19,340,711"                   # formatted, not fabricated
    assert "Student Success & Retention" in m["categories"]   # ICP category guaranteed
    assert any(c.startswith("Federal Grant: Higher Education Institutional Aid") for c in m["categories"])
    assert m["award_url"].endswith("/award/ABC-1")


def test_requires_state():
    matches, skipped = usa.scrape_usaspending({}, None, set(), *_matchers())
    assert matches == [] and skipped and skipped[0]["reason"] == "usaspending_misconfigured"


def test_titlecase_normalizes_shouty_names():
    assert usa._titlecase("LINCOLN UNIVERSITY") == "Lincoln University"
    assert usa._titlecase("THE CURATORS OF THE UNIVERSITY OF MISSOURI") == \
        "The Curators of the University of Missouri"
    assert usa._titlecase("HARRIS-STOWE STATE UNIVERSITY") == "Harris-Stowe State University"
    assert usa._titlecase("SUNY RESEARCH FOUNDATION") == "SUNY Research Foundation"  # acronym kept


def test_allcaps_recipient_is_titlecased(monkeypatch):
    monkeypatch.setattr(usa, "_request_page",
                        lambda *a, **k: _page([_award("LINCOLN UNIVERSITY")]))
    matches, _ = usa.scrape_usaspending({"state": "MO"}, None, set(), *_matchers())
    assert matches[0]["institution"] == "Lincoln University"


def test_higher_ed_filter_drops_k12(monkeypatch):
    page = _page([
        _award("Lincoln University", gid="U1", award_id="A1"),
        _award("Springfield R-XII School District", gid="K1", award_id="A2"),
        _award("Metro Community College", gid="C1", award_id="A3"),
    ])
    monkeypatch.setattr(usa, "_request_page", lambda *a, **k: page)
    matches, _ = usa.scrape_usaspending({"state": "MO"}, None, set(), *_matchers())
    names = {m["institution"] for m in matches}
    assert "Lincoln University" in names and "Metro Community College" in names
    assert "Springfield R-XII School District" not in names   # K-12 dropped


def test_higher_ed_filter_can_be_disabled(monkeypatch):
    page = _page([_award("Springfield R-XII School District", gid="K1")])
    monkeypatch.setattr(usa, "_request_page", lambda *a, **k: page)
    matches, _ = usa.scrape_usaspending({"state": "MO", "higher_ed_only": False}, None, set(), *_matchers())
    assert len(matches) == 1


def test_dedup_via_seen(monkeypatch):
    monkeypatch.setattr(usa, "_request_page", lambda *a, **k: _page([_award("Lincoln University")]))
    seen = set()
    first, _ = usa.scrape_usaspending({"state": "MO"}, None, seen, *_matchers())
    second, _ = usa.scrape_usaspending({"state": "MO"}, None, seen, *_matchers())
    assert len(first) == 1 and second == []   # same award not re-emitted


def test_pagination_walks_until_no_next(monkeypatch):
    def fake(session, state, programs, time_period, page):
        if page == 1:
            return _page([_award("Alpha University", gid="P1", award_id="A1")], has_next=True)
        return _page([_award("Beta College", gid="P2", award_id="A2")], has_next=False)
    monkeypatch.setattr(usa, "_request_page", fake)
    matches, _ = usa.scrape_usaspending({"state": "MO"}, None, set(), *_matchers())
    assert {m["institution"] for m in matches} == {"Alpha University", "Beta College"}


def test_http_error_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise requests.RequestException("503")
    monkeypatch.setattr(usa, "_request_page", boom)
    matches, skipped = usa.scrape_usaspending({"state": "MO"}, None, set(), *_matchers())
    assert matches == [] and skipped[0]["reason"] == "usaspending_http_error"


# ------------------------------ pipeline wiring ------------------------------

def test_pipeline_stores_federal_award(tmp_conn, monkeypatch):
    monkeypatch.setattr(usa, "_request_page",
                        lambda *a, **k: _page([_award("Lincoln University", amount=2_500_000)]))
    monkeypatch.setattr(utils, "load_seen", lambda: set())
    monkeypatch.setattr(utils, "save_seen", lambda seen: None)

    sources = {"missouri": {"university_systems": [],
                            "federal_grants": [{"name": "USAspending (MO)", "type": "usaspending", "state": "MO"}]}}
    result = pipeline.run_scrape(
        tmp_conn, sources, {"categories": {}, "watchlist": []},
        selected_state="missouri",
        skip_bids=True, skip_board_minutes=True, skip_transparency=True,
        skip_contracts=True, skip_contacts=True, write_report=False,
    )
    assert len(result.federal) == 1
    assert result.counts()["federal"] == 1
    row = tmp_conn.execute(
        "SELECT doc_type, institution, categories FROM documents WHERE doc_type='federal_award'"
    ).fetchone()
    assert row is not None
    assert row["institution"] == "Lincoln University"
    assert "Student Success & Retention" in row["categories"]


def test_skip_federal_flag(tmp_conn, monkeypatch):
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return {"results": [], "page_metadata": {"hasNext": False}}
    monkeypatch.setattr(usa, "_request_page", spy)
    monkeypatch.setattr(utils, "load_seen", lambda: set())
    monkeypatch.setattr(utils, "save_seen", lambda seen: None)

    sources = {"missouri": {"university_systems": [],
                            "federal_grants": [{"name": "x", "type": "usaspending", "state": "MO"}]}}
    pipeline.run_scrape(
        tmp_conn, sources, {"categories": {}, "watchlist": []},
        selected_state="missouri", skip_bids=True, skip_board_minutes=True,
        skip_transparency=True, skip_federal=True, skip_contracts=True,
        skip_contacts=True, write_report=False,
    )
    assert called["n"] == 0   # skip_federal short-circuits the pass entirely


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
