#!/usr/bin/env python3
"""
Offline tests for the second-round modules: db.py (SQLite + FTS5),
contracts_scraper.py (CSV column detection + expiration math),
opportunities.py (scoring), and the opt-in modules' graceful-skip behavior
when their config files don't exist (contacts.py / alerts.py / llm.py).

Run with: pytest tests/test_offline_extended.py   (or just `pytest`)
"""

import datetime as dt

from govspend_free import alerts, contacts, contracts_scraper, db, opportunities


class _FakeResp:
    """Minimal response supporting both the .text path (soupify) and the
    .iter_content path (utils.download_text_hashed)."""

    def __init__(self, text: str = "", content: bytes | None = None):
        self.text = text
        self._content = content if content is not None else text.encode("utf-8")

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size: int = 1):
        yield self._content


class _FakeSession:
    """Serves in-memory fixtures. `routes` maps a URL to a _FakeResp (or a
    callable returning one, so a URL's content can change between runs)."""

    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, url, timeout=None, **kwargs):
        entry = self.routes.get(url)
        if entry is None:
            raise AssertionError(f"_FakeSession got an unexpected URL: {url}")
        return entry() if callable(entry) else entry


def test_db_roundtrip_and_search(tmp_conn):
    print("[test] db.py insert + FTS5 search ...")

    doc_id = db.insert_document(
        tmp_conn, doc_type="board_minutes", state="arkansas", institution="Test University",
        title="March Board Meeting Minutes", text="The board discussed SEAtS Software for attendance tracking.",
        categories=["Attendance & Compliance"], watchlist_hits=["SEAtS"],
    )
    assert doc_id is not None

    # Duplicate insert (same doc_type/url/title) should be a no-op, not an error.
    dup_id = db.insert_document(
        tmp_conn, doc_type="board_minutes", state="arkansas", institution="Test University",
        title="March Board Meeting Minutes", text="different text this time",
    )
    assert dup_id is None, "expected duplicate insert to be skipped"

    results = db.search(tmp_conn, "SEAtS")
    assert len(results) == 1, f"expected 1 FTS hit, got {len(results)}"
    assert results[0]["title"] == "March Board Meeting Minutes"
    assert "SEAtS" in results[0]["snippet"] or "attendance" in results[0]["snippet"].lower()

    no_hit = db.search(tmp_conn, "nonexistent_term_xyz")
    assert no_hit == []

    print("  OK - insert, dedup, and FTS5 search all work")


def test_db_contracts_and_expirations(tmp_conn):
    print("[test] db.py contracts + expirations ...")

    soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=900)).isoformat()

    db.insert_contract(tmp_conn, state="arkansas", institution="Test University", vendor="Acme Software",
                        start_date="2024-01-01", end_date=soon, value="50000", days_until_expiration=30,
                        source_url="http://example.test/contracts.csv")
    db.insert_contract(tmp_conn, state="arkansas", institution="Test University", vendor="Far Away Corp",
                        start_date="2024-01-01", end_date=far, value="10000", days_until_expiration=900,
                        source_url="http://example.test/contracts.csv")

    expiring = db.upcoming_expirations(tmp_conn, within_days=180)
    vendors = {r["vendor"] for r in expiring}
    assert "Acme Software" in vendors
    assert "Far Away Corp" not in vendors, "900-day-out contract should not show up in a 180-day window"

    print("  OK - expiration window filtering works")


def test_contracts_scraper_column_detection():
    print("[test] contracts_scraper column detection + date math ...")

    header = ["Vendor Name", "Contract Start Date", "Contract End Date", "Contract Value"]
    cols = contracts_scraper._match_columns(header)
    assert cols["vendor"] == 0
    assert cols["start"] == 1
    assert cols["end"] == 2
    assert cols["value"] == 3

    expenditure_header = ["Agency", "Vendor", "Amount", "Fiscal Year"]
    cols2 = contracts_scraper._match_columns(expenditure_header)
    assert cols2["start"] is None and cols2["end"] is None, \
        "plain expenditure data (no date-range columns) should not be misdetected as contract data"

    future_date = (dt.date.today() + dt.timedelta(days=45)).strftime("%m/%d/%Y")
    days = contracts_scraper._days_until(future_date)
    assert days is not None and 44 <= days <= 46, f"expected ~45 days, got {days}"

    assert contracts_scraper._days_until("") is None
    assert contracts_scraper._days_until("not a date") is None

    print("  OK - correctly distinguishes contract CSVs from plain expenditure CSVs, date math is right")


def test_contracts_content_based_dedup():
    print("[test] contracts_scraper content-based dedup (updated CSV re-parses) ...")

    listing_url = "http://fake.test/contracts"
    csv_url = "http://fake.test/contracts.csv"
    # Padded so looks_like_empty_shell() doesn't flag it as a JS shell.
    listing_html = (
        "<html><body><h1>Contracts</h1><p>"
        + ("This is the contracts data catalog listing page. " * 20)
        + f'</p><a href="contracts.csv">Download contracts CSV</a></body></html>'
    )
    future = (dt.date.today() + dt.timedelta(days=45)).strftime("%m/%d/%Y")
    header = "Vendor Name,Contract Start Date,Contract End Date,Contract Value\n"
    csv_v1 = header + f"Acme Software,01/01/2024,{future},50000\n"
    csv_v2 = csv_v1 + f"Beta Corp,01/01/2024,{future},75000\n"  # file updated: a row added

    current_csv = {"body": csv_v1}
    routes = {
        listing_url: lambda: _FakeResp(text=listing_html),
        csv_url: lambda: _FakeResp(text=current_csv["body"]),
    }
    session = _FakeSession(routes)
    source = {"url": listing_url, "type": "html_table"}
    seen: set[str] = set()

    first, _ = contracts_scraper.scrape_contracts(source, session, seen)
    assert len(first) == 1, f"expected 1 contract on first run, got {len(first)}"

    # Same content on the second run -> content hash unchanged -> skipped.
    second, _ = contracts_scraper.scrape_contracts(source, session, seen)
    assert second == [], f"expected identical CSV to be skipped on re-run, got {second}"

    # The file at the same URL gets updated -> new content hash -> re-parsed.
    current_csv["body"] = csv_v2
    third, _ = contracts_scraper.scrape_contracts(source, session, seen)
    assert len(third) == 2, (
        "expected an updated CSV at the same URL to be re-parsed "
        f"(URL-based dedup would have skipped it); got {len(third)}"
    )

    print("  OK - unchanged CSV skipped, updated CSV at the same URL re-read")


def test_opportunities_scoring(tmp_conn):
    print("[test] opportunities.py scoring ...")

    db.insert_document(
        tmp_conn, doc_type="bid", state="texas", institution="Test U",
        title="RFP - Early Alert Retention Platform", text="",
        categories=["Student Success & Retention"],
    )
    db.insert_document(
        tmp_conn, doc_type="board_minutes", state="texas", institution="Test U",
        title="Old minutes mentioning SEAtS and scheduling", text="...",
        categories=["Academic & Space Scheduling"], watchlist_hits=["SEAtS"],
    )

    ranked = opportunities.rank_opportunities(tmp_conn)
    # Fresh in-memory DB per test (tmp_conn fixture), so exactly our two docs
    # are present.
    assert len(ranked) == 2
    scores_by_title = {r["title"]: r["score"] for r in ranked}
    assert "RFP - Early Alert Retention Platform" in scores_by_title
    assert "Old minutes mentioning SEAtS and scheduling" in scores_by_title
    # The watchlist-hit + category doc should outscore the single-category bid
    # (15 watchlist + 10 category + type_bonus(0) vs 10 category + type_bonus(5)),
    # both get near-max recency bonus since they were just inserted.
    assert scores_by_title["Old minutes mentioning SEAtS and scheduling"] > scores_by_title["RFP - Early Alert Retention Platform"]

    print("  OK - watchlist hits correctly outweigh a single category match")


def test_optional_modules_skip_gracefully():
    print("[test] contacts.py / alerts.py skip cleanly with no config file ...")

    # These config files should NOT exist in a fresh checkout - only the
    # .example versions ship. If a real config/apollo.yaml or
    # config/alerts.yaml exists (e.g. you're testing with your own creds),
    # this test still just checks the loader doesn't crash either way.
    apollo_cfg = contacts.load_apollo_config()
    alerts_cfg = alerts.load_alerts_config()
    # No assertion on the value - just confirming these don't raise.
    print(f"  OK - load_apollo_config() -> {apollo_cfg is not None}, "
          f"load_alerts_config() -> {alerts_cfg is not None} (both None expected on a fresh checkout)")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
