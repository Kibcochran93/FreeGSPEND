#!/usr/bin/env python3
"""
Offline tests for the closed-world spending normalizer (normalize.py). The
headline case is real: the Connecticut checkbook's substring match tags both
"SEATS SOFTWARE LIMITED" (really SEAtS) and "VIVID SEATS *HARTFORD" (a ticket
reseller) as "SEAtS". The normalizer must resolve the first and REJECT the
second. No network.

Run with: pytest tests/test_normalize.py
"""

import tempfile
from pathlib import Path

from govspend_free import db, normalize


def _norm():
    return normalize.Normalizer(
        client_name="SEAtS Software",
        client_aliases=["SEAtS", "SEAtS Software", "SEATS SOFTWARE", "SEATS SOFTWARE LIMITED"],
        competitors={"Ellucian": "Ellucian", "EAB": "EAB", "EAB Navigate": "EAB",
                     "Watermark Insights": "Watermark", "Jenzabar": "Jenzabar"},
        ambiguous=["SEATS"],
        agency_aliases={"Charter Oak State College Brd For Fund": "Charter Oak State College"},
        category_crosswalk={"53755": "Software"},
    )


# ------------------------------ canon ------------------------------

def test_canon_strips_legal_suffixes_and_punct():
    assert normalize._canon("SEATS SOFTWARE LIMITED") == "SEATS SOFTWARE"
    assert normalize._canon("Ellucian Company, Inc.") == "ELLUCIAN"
    assert normalize._canon("VIVID SEATS *HARTFORD") == "VIVID SEATS HARTFORD"


# ------------------------- vendor resolution -------------------------

def test_resolves_real_client():
    assert _norm().vendor("SEATS SOFTWARE LIMITED") == ("SEAtS Software", "client")
    assert _norm().vendor("SEATS SOFTWARE") == ("SEAtS Software", "client")


def test_rejects_vivid_seats():  # the whole point
    assert _norm().vendor("VIVID SEATS *HARTFORD") == (None, "unknown")
    assert _norm().vendor("VIVID SEATS MJ  THE M") == (None, "unknown")


def test_resolves_competitors_through_suffixes_and_aliases():
    assert _norm().vendor("ELLUCIAN INC") == ("Ellucian", "competitor")
    assert _norm().vendor("Watermark Insights LLC") == ("Watermark", "competitor")
    assert _norm().vendor("EAB Navigate") == ("EAB", "competitor")   # product alias -> canonical
    assert _norm().vendor("EAB Global") == ("EAB", "competitor")     # word-boundary prefix


def test_ambiguous_term_matches_only_exactly():
    # 'SEATS' is a client alias but flagged ambiguous: exact only, never a prefix.
    assert _norm().vendor("SEATS") == ("SEAtS Software", "client")
    assert _norm().vendor("SEATS ETC HOLDINGS") == (None, "unknown")  # not SEAtS


def test_institution_recipient():
    canonical, kind = _norm().vendor("UNIVERSITY OF MISSOURI SYSTEM")
    assert kind == "institution" and "University of Missouri" in canonical


def test_agency_and_category():
    n = _norm()
    assert n.agency("Charter Oak State College Brd For Fund") == "Charter Oak State College"
    assert n.category("53755") == "Software"
    assert n.category("99999") is None


def test_parse_vendor_from_row():
    row = "SEATS SOFTWARE LIMITED | BORAA000289671100187409541 2024 2 EDUCATION Charter Oak"
    assert normalize.parse_vendor_from_row(row) == "SEATS SOFTWARE LIMITED"


# ------------------------- from_config seeding -------------------------

def test_from_config_seeds_competitors_from_watchlist(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        kw = Path(td) / "keywords.yaml"
        kw.write_text("watchlist: [SEAtS, Ellucian, EAB Navigate]\n", encoding="utf-8")
        cfg = Path(td) / "normalize.yaml"  # absent -> defaults
        monkeypatch.setattr(normalize, "KEYWORDS_PATH", kw)
        n = normalize.Normalizer.from_config(config_path=cfg, keywords_path=kw)
    assert n.vendor("Ellucian")[1] == "competitor"
    assert n.vendor("EAB Navigate") == ("EAB", "competitor")   # default alias grouping
    assert n.vendor("SEATS SOFTWARE LIMITED")[1] == "client"


# ------------------------- backfill from documents -------------------------

def test_backfill_resolves_and_stores(tmp_conn):
    db.insert_document(tmp_conn, doc_type="transparency", state="connecticut",
                       institution="CT", title="t1", url="http://ct/1", source="socrata",
                       text="SEATS SOFTWARE LIMITED | BORAA 2024 EDUCATION Charter Oak")
    db.insert_document(tmp_conn, doc_type="transparency", state="connecticut",
                       institution="CT", title="t2", url="http://ct/2", source="socrata",
                       text="VIVID SEATS *HARTFORD | DCFR 2026 CO")
    db.insert_document(tmp_conn, doc_type="transparency", state="delaware",
                       institution="DE", title="t3", url="http://de/1", source="socrata",
                       text="ELLUCIAN INC | payment 2025")
    tmp_conn.commit()

    stats = normalize.backfill_payments_from_documents(tmp_conn, _norm())
    assert stats["scanned"] == 3
    assert stats["client"] == 1 and stats["competitor"] == 1 and stats["unknown"] == 1
    assert stats["inserted"] == 2   # VIVID SEATS is NOT stored

    kinds = {r["vendor_canonical"]: r["vendor_kind"]
             for r in tmp_conn.execute("SELECT vendor_canonical, vendor_kind FROM payments")}
    assert kinds == {"SEAtS Software": "client", "Ellucian": "competitor"}

    # Idempotent: re-running inserts nothing new (ref dedup).
    again = normalize.backfill_payments_from_documents(tmp_conn, _norm())
    assert again["inserted"] == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
