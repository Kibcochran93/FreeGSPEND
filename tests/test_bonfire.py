#!/usr/bin/env python3
"""
Offline test for the Bonfire RFP-portal adapter. Serves a captured Bonfire
JSON payload (real shape: payload.projects = {id: {..}}) instead of the live
endpoint, and drives it through the real bid entry point
(`bid_scraper.scrape_bid_board`) so the `type: bonfire` dispatch is covered too.

No network. Run with `pytest` (or `pytest tests/test_bonfire.py`).
"""

from pathlib import Path

import yaml

from govspend_free import bid_scraper, bonfire, utils

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

# The base endpoint the adapter GETs (the cache-buster goes in params=, not the
# URL, so this exact string is what the fake session routes on).
STLCC_ENDPOINT = "https://stlcc.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData"


class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200, headers: dict | None = None):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        pass


class FakeSession:
    """Serves a fixture for the Bonfire endpoint; routes on the base URL and
    ignores query params/headers (as the real XHR's cache-buster is irrelevant
    to the fixture)."""

    def __init__(self, routes: dict[str, Path]):
        self.routes = routes
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        path = self.routes.get(url)
        if path is None:
            raise AssertionError(f"FakeSession got an unexpected URL: {url}")
        return FakeResponse(content=path.read_bytes())


class Rate429Session:
    """Always answers HTTP 429 (with a Retry-After), and counts calls so a test
    can assert the cooldown stops further network hits."""

    def __init__(self, retry_after: str = "2"):
        self.retry_after = retry_after
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        return FakeResponse(status_code=429, headers={"Retry-After": self.retry_after})


def _matchers():
    keywords_cfg = yaml.safe_load((ROOT_DIR / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(keywords_cfg["categories"])


def test_bonfire_portal_parses_filters_and_dedups():
    print("[test] bonfire adapter ...")
    bonfire.reset_cooldown()
    matchers = _matchers()
    session = FakeSession({STLCC_ENDPOINT: FIXTURES_DIR / "bonfire_stlcc.json"})
    # Routed through the real bid entry point => also exercises the type dispatch.
    source = {"type": "bonfire", "slug": "stlcc",
              "url": "https://stlcc.bonfirehub.com/portal/?tab=openOpportunities"}
    seen: set[str] = set()

    matches, skipped = bid_scraper.scrape_bid_board(source, session, seen, matchers)

    assert not skipped, f"expected no skips, got {skipped}"
    # Only the retention RFP should survive the SEAtS category filter; the
    # parking-lot RFP matches no category and is dropped.
    assert len(matches) == 1, f"expected 1 category match, got {len(matches)}: {matches}"
    m = matches[0]
    assert "Early Alert" in m["title"], m
    assert m["categories"], m
    assert m["detail_url"].endswith("/opportunities/246813"), m
    assert m["date"] == "2026-08-21", m                       # DateClose date part
    assert m["source_url"].startswith("https://stlcc.bonfirehub.com/portal/"), m
    assert not any("Parking" in x["title"] for x in matches), matches

    # Re-running with the same `seen` set yields nothing (dedup works).
    again, _ = bid_scraper.scrape_bid_board(source, session, seen, matchers)
    assert again == [], f"expected dedup to suppress repeats, got {again}"

    print("  OK - 1 relevant RFP kept, parking RFP ignored, dispatch + dedup work")


def test_bonfire_missing_slug_is_skipped_not_crashed():
    print("[test] bonfire misconfig ...")
    bonfire.reset_cooldown()
    matchers = _matchers()
    session = FakeSession({})
    matches, skipped = bid_scraper.scrape_bid_board(
        {"type": "bonfire", "url": "https://x.bonfirehub.com/portal/"}, session, set(), matchers
    )
    assert matches == []
    assert len(skipped) == 1 and skipped[0]["reason"] == "bonfire_misconfigured", skipped
    print("  OK - a slug-less bonfire source is skipped cleanly")


def test_bonfire_429_sets_cooldown_and_backs_off():
    print("[test] bonfire 429 back-off ...")
    bonfire.reset_cooldown()
    matchers = _matchers()
    session = Rate429Session(retry_after="2")

    # First portal hits the limiter: one network call, a `rate_limited` skip,
    # and Retry-After respected.
    m1, s1 = bid_scraper.scrape_bid_board({"type": "bonfire", "slug": "utexas"}, session, set(), matchers)
    assert m1 == [] and len(s1) == 1 and s1[0]["reason"] == "rate_limited", s1
    assert session.calls == 1, session.calls

    # Second portal, same run: the process-wide cooldown makes it skip WITHOUT
    # another network hit (so a national run backs off instead of hammering).
    m2, s2 = bid_scraper.scrape_bid_board({"type": "bonfire", "slug": "utdallas"}, session, set(), matchers)
    assert m2 == [] and len(s2) == 1 and s2[0]["reason"] == "rate_limited_cooldown", s2
    assert session.calls == 1, f"expected no 2nd network call during cooldown, got {session.calls}"

    bonfire.reset_cooldown()
    print("  OK - 429 sets a shared cooldown; later portals skip without hitting the network")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
