#!/usr/bin/env python3
"""
Offline tests for the national coverage scorecard (govspend_free.coverage).
Uses the in-memory `tmp_conn` fixture + a hand-built sources dict + inserted
documents. No network.
"""

from govspend_free import coverage, db


SOURCES = {
    # 2 education systems configured + federal -> can become "covered"
    "texas": {
        "university_systems": [
            {"name": "University of Texas System",
             "bid_boards": [{"type": "bonfire", "slug": "utsystem", "url": "u1"}],
             "board_minutes": [{"type": "html_table", "url": "u2"}]},
            {"name": "University of Texas at Austin",
             "bid_boards": [{"type": "bonfire", "slug": "utexas", "url": "u3"}]},
        ],
        "federal_grants": [{"type": "usaspending", "state": "TX"}],
    },
    # 1 education system configured -> can become "represented"
    "florida": {"university_systems": [
        {"name": "Florida Atlantic University",
         "bid_boards": [{"type": "bonfire", "slug": "fau", "url": "f1"}]}]},
    # configured, but we'll only give it a (non-education) transparency doc
    "arizona": {"university_systems": [
        {"name": "Maricopa CCD", "bid_boards": [{"type": "bonfire", "slug": "maricopa", "url": "a1"}]}]},
    # federal only, no education source -> stays "missing" (federal doesn't count)
    "oklahoma": {"university_systems": [], "federal_grants": [{"type": "usaspending", "state": "OK"}]},
}


def _row(rows, abbr):
    return next(r for r in rows if r.abbr == abbr)


def _add(conn, doc_type, state, institution, url):
    db.insert_document(conn, doc_type=doc_type, state=state, institution=institution,
                       title=f"{institution} {url}", url=url, source=doc_type)


def test_coverage_status_reconciles_config_and_docs(tmp_conn):
    print("[test] coverage scorecard ...")
    # TX: two institutions with education docs -> covered
    _add(tmp_conn, "bid", "texas", "University of Texas at Austin", "b1")
    _add(tmp_conn, "board_minutes", "texas", "University of Texas at Dallas", "b2")
    # FL: one institution with an education doc -> represented
    _add(tmp_conn, "board_minutes", "florida", "Florida Atlantic University", "b3")
    # AZ: only a transparency doc (NOT an education doc_type) -> stays configured
    _add(tmp_conn, "transparency", "arizona", "Maricopa CCD", "b4")

    rows = coverage.build_coverage(tmp_conn, SOURCES)
    assert len(rows) == 50, len(rows)

    assert _row(rows, "TX").status == "covered", _row(rows, "TX")
    assert _row(rows, "TX").institutions_with_docs == 2
    assert "bonfire" in _row(rows, "TX").families and "html_table" in _row(rows, "TX").families

    assert _row(rows, "FL").status == "represented", _row(rows, "FL")

    az = _row(rows, "AZ")
    assert az.status == "configured", az           # transparency doc doesn't count as education
    assert az.education_docs == 0

    ok = _row(rows, "OK")
    assert ok.status == "missing", ok               # federal-only is not education coverage
    assert ok.has_federal is True

    # A totally unconfigured state
    assert _row(rows, "WY").status == "missing"


def test_summarize_counts(tmp_conn):
    print("[test] coverage summarize ...")
    _add(tmp_conn, "bid", "texas", "University of Texas at Austin", "b1")
    _add(tmp_conn, "bid", "texas", "University of Texas at Dallas", "b2")
    _add(tmp_conn, "bid", "florida", "Florida Atlantic University", "b3")

    rows = coverage.build_coverage(tmp_conn, SOURCES)
    s = coverage.summarize(rows)
    assert s["covered"] == ["TX"], s["covered"]
    assert s["represented"] == ["FL"], s["represented"]
    assert s["configured"] == ["AZ"], s["configured"]
    assert "TX" not in s["missing"] and "OK" in s["missing"]
    assert set(s["with_federal"]) == {"TX", "OK"}, s["with_federal"]
    assert len(s["configured_or_better"]) == 3          # TX, FL, AZ
    assert len(s["represented_or_better"]) == 2         # TX, FL
    print("  OK - scorecard reconciles configured sources with document evidence")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
