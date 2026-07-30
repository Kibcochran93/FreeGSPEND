#!/usr/bin/env python3
"""
Offline tests for the SODA (Socrata) payments ingest (spend_ingest.py). The
HTTP fetch is monkeypatched, so we exercise column mapping, the closed-world
normalizer filter (VIVID SEATS matched by SoQL `like` but dropped), amount
parsing, dedup, and DB insertion - no network.

Run with: pytest tests/test_spend_ingest.py
"""

from govspend_free import db, normalize, spend_ingest, utils


def _norm():
    return normalize.Normalizer(
        client_name="SEAtS Software",
        client_aliases=["SEAtS", "SEATS SOFTWARE"],
        competitors={"Ellucian": "Ellucian", "EAB": "EAB"},
        ambiguous=["SEATS"],
    )


class _Resp:
    def __init__(self, rows): self._rows = rows
    def json(self): return self._rows


# One fake Socrata response per SoQL vendor term. The `like` is intentionally
# loose: querying "SEATS SOFTWARE" also returns a VIVID SEATS row the DB happens
# to contain - the normalizer must drop it.
_FAKE = {
    "ELLUCIAN": [{"vendor": "ELLUCIAN SUPPORT INC", "amount": "108456.00",
                  "check_date": "2025-03-01T00:00:00.000", "department": "HIGHER EDUCATION"}],
    "EAB": [{"vendor": "EAB GLOBAL INC", "amount": "17,675", "check_date": "2025-02-01",
             "department": "DEPT OF EDUCATION"}],
    "SEATS SOFTWARE": [
        {"vendor": "SEATS SOFTWARE LIMITED", "amount": "14976", "check_date": "2025-06-01", "department": "CHARTER OAK"},
        {"vendor": "VIVID SEATS *HARTFORD", "amount": "88", "check_date": "2025-06-02", "department": "DCF"},
    ],
}


def _fake_fetch(url, session=None, params=None):
    where = (params or {}).get("$where", "")
    for term, rows in _FAKE.items():
        if term.replace("'", "''").upper() in where.upper():
            return _Resp(rows)
    return _Resp([])


SOURCE = {"type": "socrata", "domain": "data.delaware.gov", "dataset": "5s6n-7hpx",
          "vendor_col": "vendor", "amount_col": "amount", "date_col": "check_date",
          "agency_col": "department"}


def test_ingest_resolves_maps_and_drops_noise(monkeypatch):
    monkeypatch.setattr(utils, "fetch", _fake_fetch)
    payments, skipped = spend_ingest.ingest_socrata_payments(SOURCE, None, _norm(), set())
    assert skipped == []
    by_vendor = {p["vendor_canonical"]: p for p in payments}
    assert set(by_vendor) == {"Ellucian", "EAB", "SEAtS Software"}   # VIVID SEATS dropped
    assert by_vendor["Ellucian"]["amount"] == 108456.0
    assert by_vendor["EAB"]["amount"] == 17675.0                     # comma stripped
    assert by_vendor["Ellucian"]["paid_date"] == "2025-03-01"        # T-time trimmed
    assert by_vendor["SEAtS Software"]["vendor_kind"] == "client"


def test_ingest_requires_column_mapping():
    _, skipped = spend_ingest.ingest_socrata_payments(
        {"type": "socrata", "domain": "d", "dataset": "x"}, None, _norm(), set())
    assert skipped and skipped[0]["reason"] == "socrata_payments_misconfigured"


def test_clear_payments(tmp_conn):
    db.insert_payment(tmp_conn, ref="r1", state="ct", vendor_raw="ELLUCIAN",
                      vendor_canonical="Ellucian", vendor_kind="competitor")
    assert tmp_conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    removed = db.clear_payments(tmp_conn)
    assert removed == 1
    assert tmp_conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_amount_and_date_parsing():
    assert spend_ingest._parse_amount("$1,234.56") == 1234.56
    assert spend_ingest._parse_amount("") is None
    assert spend_ingest._parse_amount(None) is None
    assert spend_ingest._clean_date("2026-07-14T00:00:00.000") == "2026-07-14"


def test_ingest_driver_stores_to_payments(tmp_conn, monkeypatch):
    monkeypatch.setattr(utils, "fetch", _fake_fetch)
    sources = {"delaware": {"transparency": [SOURCE]},
               "oregon": {"transparency": [{"type": "socrata", "domain": "d", "dataset": "y"}]}}  # no vendor_col -> skipped
    stats = spend_ingest.ingest(tmp_conn, sources, normalizer=_norm(), session=object())
    assert stats["sources"] == 1 and stats["inserted"] == 3   # oregon skipped; VIVID SEATS not stored
    kinds = {r["vendor_canonical"]: r["vendor_kind"]
             for r in tmp_conn.execute("SELECT vendor_canonical, vendor_kind FROM payments")}
    assert kinds == {"Ellucian": "competitor", "EAB": "competitor", "SEAtS Software": "client"}
    # Idempotent: same refs, nothing new.
    again = spend_ingest.ingest(tmp_conn, sources, normalizer=_norm(), session=object())
    assert again["inserted"] == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
