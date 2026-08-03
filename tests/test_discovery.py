#!/usr/bin/env python3
"""
Offline tests for the portal-discovery logic (govspend_free.discovery) - the
pure, network-free parts: state inference from an institution name, slug
extraction from a tenant hostname, higher-ed/K-12 segment classification, and
the candidate-CSV writer. No network.
"""

import csv
from pathlib import Path

from govspend_free import discovery


def test_state_from_name_matches_longest_state_first():
    assert discovery.state_from_name("Iowa State University") == "iowa"
    assert discovery.state_from_name("University of Montana") == "montana"
    # "West Virginia" must win over the substring "Virginia".
    assert discovery.state_from_name("West Virginia University") == "west_virginia"
    assert discovery.state_from_name("North Carolina State University") == "north_carolina"
    assert discovery.state_from_name("Grayson College") == ""      # no state in the name
    assert discovery.state_from_name("") == ""


def test_slug_from_host():
    assert discovery._slug_from_host("iastate.ionwave.net", "ionwave") == "iastate"
    assert discovery._slug_from_host("stlcc.bonfirehub.com", "bonfire") == "stlcc"
    assert discovery._slug_from_host("www.ionwave.net", "ionwave") is None        # dropped
    assert discovery._slug_from_host("vendor.bonfirehub.com", "bonfire") is None  # dropped
    assert discovery._slug_from_host("a.b.ionwave.net", "ionwave") is None        # nested, skip
    assert discovery._slug_from_host("example.com", "ionwave") is None            # wrong suffix


def test_segment_classification():
    assert discovery._segment("Iowa State University Procurement") == "higher_ed"
    assert discovery._segment("Grayson College Purchasing") == "higher_ed"
    assert discovery._segment("Arlington Independent School District") == "k12"
    assert discovery._segment("Cy-Fair ISD eBid System") == "k12"
    assert discovery._segment("City of Denton Purchasing") == "other"


def test_write_candidates_csv_puts_higher_ed_first():
    rows = [
        {"slug": "cityx", "live": True, "segment": "other", "state": "", "open": 0, "name": "City X"},
        {"slug": "iastate", "live": True, "segment": "higher_ed", "state": "iowa", "open": 8, "name": "ISU"},
        {"slug": "somecc", "live": True, "segment": "higher_ed", "state": "", "open": 0, "name": "Some College"},
    ]
    out = Path(__file__).resolve().parent / "_discovery_cand_tmp.csv"   # tests/ is writable
    try:
        discovery.write_candidates_csv(rows, out)
        got = list(csv.DictReader(out.open(encoding="utf-8-sig")))
        assert got[0]["segment"] == "higher_ed", "higher-ed rows should sort first"
        assert {r["slug"] for r in got} == {"cityx", "iastate", "somecc"}
        assert got[0]["slug"] == "iastate"   # higher-ed with a state sorts ahead of stateless higher-ed
    finally:
        out.unlink(missing_ok=True)


FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "planetbids_laccd.html"


def test_path_family_id_regex():
    pb = discovery._PATH_FAMILIES["planetbids"]["id_re"]
    assert pb.search("https://vendors.planetbids.com/portal/21372/bo/bo-search").group(1) == "21372"
    og = discovery._PATH_FAMILIES["opengov"]["id_re"]
    assert og.search("https://procurement.opengov.com/portal/mtsac/projects").group(1) == "mtsac"


def test_classify_rendered_planetbids_on_fixture():
    # `fetch` is stubbed with the saved LACCD render - no browser, no network.
    html = FIXTURE.read_text(encoding="utf-8")
    row = discovery.classify_rendered("planetbids", "21372", lambda url: html)
    assert row["live"] and row["segment"] == "higher_ed", row
    assert "Los Angeles Community College District" in row["name"], row
    assert isinstance(row["open"], int) and row["open"] >= 25, row


def test_classify_rendered_dead_when_render_returns_none():
    row = discovery.classify_rendered("planetbids", "999", lambda url: None)
    assert row["live"] is False and row["segment"] == "dead"


def test_to_sources_entries_only_emits_live_higher_ed():
    rows = [
        {"slug": "21372", "live": True, "segment": "higher_ed", "state": "",
         "name": "Los Angeles Community College District", "open": 30},
        {"slug": "39475", "live": True, "segment": "other", "state": "",
         "name": "City of Riverside", "open": 30},
        {"slug": "88", "live": False, "segment": "dead", "state": "", "name": "", "open": 0},
    ]
    y = discovery.to_sources_entries(rows, "planetbids")
    assert "Los Angeles Community College District" in y
    assert "type: planetbids" in y and "portal/21372/bo/bo-search" in y
    assert "City of Riverside" not in y   # non-higher-ed excluded


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
