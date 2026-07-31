#!/usr/bin/env python3
"""
Offline test for the Ion Wave RFP-portal adapter. Serves a captured RadGrid
listing (real shape: rgRow/rgAltRow rows + a `_clientKeyValues` BidID map)
instead of the live site, driven through the real bid entry point
(`bid_scraper.scrape_bid_board`) so the `type: ionwave` dispatch is covered too.
No network. Run with `pytest`.
"""

from pathlib import Path

import yaml

from govspend_free import bid_scraper, utils

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

LISTING_URL = "https://testu.ionwave.net/SourcingEvents.aspx?SourceType=1"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, routes: dict[str, Path]):
        self.routes = routes

    def get(self, url, timeout=None, **kwargs):
        path = self.routes.get(url)
        if path is None:
            raise AssertionError(f"FakeSession got an unexpected URL: {url}")
        return FakeResponse(path.read_text(encoding="utf-8"))


def _matchers():
    kw = yaml.safe_load((ROOT_DIR / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(kw["categories"])


def test_ionwave_parses_filters_and_dedups():
    print("[test] ionwave adapter ...")
    matchers = _matchers()
    session = FakeSession({LISTING_URL: FIXTURES_DIR / "ionwave_listing.html"})
    source = {"type": "ionwave", "slug": "testu"}   # url derived from slug
    seen: set[str] = set()

    matches, skipped = bid_scraper.scrape_bid_board(source, session, seen, matchers)

    assert not skipped, f"expected no skips, got {skipped}"
    # Only the attendance RFP survives the SEAtS category filter; milk supply drops.
    assert len(matches) == 1, f"expected 1 category match, got {len(matches)}: {matches}"
    m = matches[0]
    assert "Attendance" in m["title"], m
    assert m["categories"], m
    assert m["detail_url"].endswith("bidID=111&SourceType=1"), m   # from _clientKeyValues
    assert m["date"] == "2026-07-01", m                            # issue date parsed
    assert not any("Milk" in x["title"] for x in matches), matches

    again, _ = bid_scraper.scrape_bid_board(source, session, seen, matchers)
    assert again == [], f"expected dedup to suppress repeats, got {again}"
    print("  OK - 1 relevant bid kept, milk-supply ignored, dispatch + dedup work")


def test_ionwave_missing_slug_is_skipped():
    matchers = _matchers()
    matches, skipped = bid_scraper.scrape_bid_board(
        {"type": "ionwave", "url": "https://x.ionwave.net/SourcingEvents.aspx"}, FakeSession({}), set(), matchers
    )
    assert matches == []
    assert len(skipped) == 1 and skipped[0]["reason"] == "ionwave_misconfigured", skipped


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
