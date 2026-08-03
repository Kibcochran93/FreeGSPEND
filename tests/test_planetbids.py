#!/usr/bin/env python3
"""
Offline tests for the PlanetBids adapter. The parser runs against a REAL rendered
PlanetBids page saved as a fixture (LACCD portal 21372) - no browser, no Scrapling
install needed. Rendering itself (render.fetch_rendered) is opt-in and not
exercised here.
"""

from pathlib import Path

import yaml

from govspend_free import planetbids, utils

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "planetbids_laccd.html"


def _matchers():
    kw = yaml.safe_load((ROOT / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(kw["categories"])


def test_parse_planetbids_extracts_rows():
    events = planetbids.parse_planetbids(FIXTURE.read_text(encoding="utf-8"))
    assert len(events) >= 25, f"expected the ~30 shown rows, got {len(events)}"
    e = events[0]
    assert e["title"], e
    # Every row should carry a solicitation number and a stage.
    assert any(ev["number"] for ev in events), "expected invitation numbers"
    assert any(ev["stage"] for ev in events), "expected a stage label"
    # Dates normalize to ISO (from MM/DD/YYYY).
    assert any(ev["close"].startswith("20") for ev in events if ev["close"]), events[:2]


def test_scrape_planetbids_matches_seats_categories(monkeypatch):
    # Feed the fixture straight through the scrape path (render mocked) and confirm
    # the SEAtS category filter keeps relevant bids and drops the rest.
    monkeypatch.setattr(utils, "USE_BROWSER", True)
    monkeypatch.setattr(planetbids.render, "fetch_rendered",
                        lambda url, **kw: FIXTURE.read_text(encoding="utf-8"))
    src = {"type": "planetbids", "url": "https://vendors.planetbids.com/portal/21372/bo/bo-search"}
    matches, skipped = planetbids.scrape_planetbids_portal(src, None, set(), _matchers())
    assert not skipped, skipped
    # Some LACCD rows are facilities/construction (dropped); any kept row must have
    # matched a SEAtS category and carry a title + categories.
    for m in matches:
        assert m["title"] and m["categories"]
        assert m["source_url"].endswith("bo-search")


def test_planetbids_needs_browser_when_flag_off(monkeypatch):
    monkeypatch.setattr(utils, "USE_BROWSER", False)
    src = {"type": "planetbids", "url": "https://vendors.planetbids.com/portal/21372/bo/bo-search"}
    matches, skipped = planetbids.scrape_planetbids_portal(src, None, set(), _matchers())
    assert matches == []
    assert skipped and skipped[0]["reason"] == "needs_browser", skipped


def test_render_unavailable_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(utils, "USE_BROWSER", True)
    monkeypatch.setattr(planetbids.render, "fetch_rendered", lambda url, **kw: None)
    src = {"type": "planetbids", "url": "https://vendors.planetbids.com/portal/21372/bo/bo-search"}
    matches, skipped = planetbids.scrape_planetbids_portal(src, None, set(), _matchers())
    assert matches == []
    assert skipped and skipped[0]["reason"] == "render_unavailable", skipped


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
