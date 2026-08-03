#!/usr/bin/env python3
"""
Offline self-test. Runs the scrapers against local HTML/PDF fixtures instead
of live sites, so you can confirm your install and parsing logic actually
work before pointing the tool at real state config.

Run with:  pytest tests/test_offline.py   (or just `pytest` for everything)
Expected:  all tests pass, with no network access.
"""

from pathlib import Path

import yaml

from govspend_free import bid_scraper, board_minutes_scraper, utils

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"


class FakeResponse:
    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    """Serves fixture files instead of hitting the network. `routes` maps
    exact URLs to local fixture Paths."""

    def __init__(self, routes: dict[str, Path]):
        self.routes = routes

    def get(self, url, timeout=None, **kwargs):
        path = self.routes.get(url)
        if path is None:
            raise AssertionError(f"FakeSession got an unexpected URL: {url}")
        if path.suffix == ".pdf":
            return FakeResponse(content=path.read_bytes())
        return FakeResponse(text=path.read_text(encoding="utf-8"))


def test_bid_scraper():
    print("[test] bid_scraper ...")
    keywords_cfg = yaml.safe_load((ROOT_DIR / "config" / "keywords.yaml").read_text())
    matchers = utils.build_category_matchers(keywords_cfg["categories"])

    url = "http://fake.test/bids"
    routes = {url: FIXTURES_DIR / "sample_bid_board.html"}
    session = FakeSession(routes)

    source = {"url": url, "type": "html_table"}
    seen: set[str] = set()
    matches, skipped = bid_scraper.scrape_bid_board(source, session, seen, matchers)

    assert not skipped, f"expected no skips, got {skipped}"
    assert len(matches) == 2, f"expected 2 category matches (retention RFP + ERP RFP), got {len(matches)}: {matches}"

    titles = {m["title"] for m in matches}
    assert any("Early Alert" in t for t in titles), titles
    assert any("Enterprise Resource Planning" in t for t in titles), titles

    # The parking-lot-striping row should NOT match any category.
    assert not any("Parking" in t for t in titles), titles

    # Re-running with the same `seen` set should produce zero new matches
    # (dedup working).
    matches_2, _ = bid_scraper.scrape_bid_board(source, session, seen, matchers)
    assert matches_2 == [], f"expected dedup to suppress repeats, got {matches_2}"

    print("  OK - found 2 relevant bids, correctly ignored the parking bid, dedup works")


def test_board_minutes_scraper():
    print("[test] board_minutes_scraper ...")
    keywords_cfg = yaml.safe_load((ROOT_DIR / "config" / "keywords.yaml").read_text())
    matchers = utils.build_category_matchers(keywords_cfg["categories"])
    watchlist_patterns = utils.build_watchlist_matchers(keywords_cfg["watchlist"])

    list_url = "http://fake.test/minutes"
    pdf_url_1 = "http://fake.test/minutes/2026-03-09-minutes.pdf"
    pdf_url_2 = "http://fake.test/minutes/2026-01-28-minutes.pdf"

    routes = {
        list_url: FIXTURES_DIR / "sample_minutes_list.html",
        pdf_url_1: FIXTURES_DIR / "2026-03-09-minutes.pdf",
        # Reuse the same fixture PDF for the second link - fine for this test.
        pdf_url_2: FIXTURES_DIR / "2026-03-09-minutes.pdf",
    }
    session = FakeSession(routes)

    source = {"url": list_url, "type": "html_list"}
    seen: set[str] = set()
    matches, skipped = board_minutes_scraper.scrape_board_minutes(
        source, session, seen, matchers, watchlist_patterns
    )

    assert not skipped, f"expected no skips, got {skipped}"
    assert len(matches) == 2, f"expected both PDFs to match (SEAtS watchlist + attendance/compliance category), got {len(matches)}"

    for m in matches:
        assert "SEAtS" in m["watchlist_hits"], m
        assert any("Attendance" in c for c in m["categories"]), m
        assert "SEAtS" in m["snippets"], m

    print("  OK - both minutes PDFs correctly flagged for SEAtS watchlist hit + Attendance/Compliance category")


def test_category_matching_is_word_boundary():
    """Category keywords match whole tokens, not raw substrings - this is what
    lets short acronyms like "SIS" be listed safely."""
    cfg = {"sis": {"label": "SIS", "keywords": ["SIS"]},
           "sched": {"label": "Sched", "keywords": ["room booking", "banner"]}}
    m = utils.build_category_matchers(cfg)
    # SIS hits the standalone token (any case) ...
    assert utils.match_categories("Student Information System (SIS) replacement", m) == ["SIS"]
    assert utils.match_categories("new sis needed", m) == ["SIS"]
    # ... but never inside ordinary words.
    for noise in ["data analysis", "on that basis", "the thesis", "a crisis", "emphasis added"]:
        assert utils.match_categories(noise, m) == [], noise
    # Phrases and existing single words still match; "banner" no longer fires mid-word.
    assert "Sched" in utils.match_categories("ad hoc room booking platform", m)
    assert utils.match_categories("Ellucian Banner ERP", m) == ["Sched"]
    assert utils.match_categories("the event was bannered across campus", m) == []


def test_real_config_matches_known_rfp_titles():
    """Guard the fix for the missed-tenders report: real US RFP titles must land
    in a category (they were all silently dropped before)."""
    keywords_cfg = yaml.safe_load((ROOT_DIR / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    m = utils.build_category_matchers(keywords_cfg["categories"])
    for title in [
        "Enterprise Academic Scheduling and Resource Management Software",   # Savannah State
        "Event and Room Booking Solution",                                  # U. of Maine
        "Student Information System Replacement",                           # umbrella SIS
        "Cloud-Based Timetabling Software",
        "Attendance Monitoring System",
    ]:
        assert utils.match_categories(title, m), f"no category matched: {title!r}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
