#!/usr/bin/env python3
"""
Offline test for the JAGGAER / SciQuest public-events adapter. Serves a captured
PublicEvent page (btn-link-header rows + Open/Close/Type/Number fields) instead
of the live marketplace, driven through the real bid entry point
(`bid_scraper.scrape_bid_board`) so the `type: jaggaer` dispatch is covered too.
No network. Run with `pytest`.
"""

from pathlib import Path

import yaml

from govspend_free import bid_scraper, utils

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

EVENT_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=Test"


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


def test_jaggaer_parses_filters_and_dedups():
    print("[test] jaggaer adapter ...")
    matchers = _matchers()
    session = FakeSession({EVENT_URL: FIXTURES_DIR / "jaggaer_events.html"})
    source = {"type": "jaggaer", "url": EVENT_URL}
    seen: set[str] = set()

    matches, skipped = bid_scraper.scrape_bid_board(source, session, seen, matchers)

    assert not skipped, f"expected no skips, got {skipped}"
    # Only the retention RFP survives the SEAtS category filter; the paving IFB drops.
    assert len(matches) == 1, f"expected 1 category match, got {len(matches)}: {matches}"
    m = matches[0]
    assert "Retention" in m["title"], m
    assert m["categories"], m
    assert m["detail_url"].endswith("/apps/Router/PublicEvent/Detail?id=111"), m   # relative href -> absolute
    assert m["date"] == "2026-07-01", m                                            # open date, MDT stripped
    assert not any("Paving" in x["title"] for x in matches), matches

    again, _ = bid_scraper.scrape_bid_board(source, session, seen, matchers)
    assert again == [], f"expected dedup to suppress repeats, got {again}"
    print("  OK - 1 relevant event kept, paving ignored, dispatch + dedup work")


def test_jaggaer_missing_url_is_skipped():
    matchers = _matchers()
    matches, skipped = bid_scraper.scrape_bid_board({"type": "jaggaer"}, FakeSession({}), set(), matchers)
    assert matches == []
    assert len(skipped) == 1 and skipped[0]["reason"] == "jaggaer_misconfigured", skipped


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
