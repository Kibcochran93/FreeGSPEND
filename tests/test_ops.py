#!/usr/bin/env python3
"""
Offline tests for the Ops "Full Motion" play (ops.py) and the read-only
HubSpot client (hubspot_client.py). No network, no token: the HubSpot client
is a hand-rolled fake, so we exercise signal extraction, scoring, opener
grounding, and the run/render/save loop without any HTTP.

Run with: pytest tests/test_ops.py
"""

import json
import tempfile
from pathlib import Path

import pytest

from govspend_free import db, hubspot_client, ops


# ------------------------------ gather_signals ------------------------------

def _seed_signals(conn):
    db.insert_document(
        conn, doc_type="board_minutes", state="arkansas",
        institution="University of Arkansas System", title="Agenda",
        text="Workday Student migration and EAB review",
        categories=["SIS / ERP Migration", "Student Success & Retention"],
        watchlist_hits=["EAB", "Workday Student"],
    )
    db.insert_document(
        conn, doc_type="transparency", state="connecticut",
        institution="State of Connecticut Open Expenditures", title="SEATS SOFTWARE LIMITED payment",
        text="payment", categories=[], watchlist_hits=["SEAtS", "Ellucian"],
    )
    db.insert_document(
        conn, doc_type="board_minutes", state="texas", institution="Nowhere College",
        title="Minutes", text="routine business", categories=["Student Success & Retention"],
        watchlist_hits=[],
    )
    conn.commit()


def test_gather_signals_keeps_only_signal_accounts(tmp_conn):
    _seed_signals(tmp_conn)
    names = {s["institution"] for s in ops.gather_signals(tmp_conn)}
    assert "University of Arkansas System" in names
    assert "State of Connecticut Open Expenditures" in names
    assert "Nowhere College" not in names


def test_gather_signals_splits_client_from_competitors(tmp_conn):
    _seed_signals(tmp_conn)
    by_name = {s["institution"]: s for s in ops.gather_signals(tmp_conn)}
    ct = by_name["State of Connecticut Open Expenditures"]
    assert ct["client_present"] is True
    assert "SEAtS" not in ct["competitors"] and "Ellucian" in ct["competitors"]
    ar = by_name["University of Arkansas System"]
    assert ar["client_present"] is False
    assert set(ar["competitors"]) == {"EAB", "Workday Student"}


# --------------------------------- scoring ---------------------------------

def _acc(**kw):
    base = {"institution": "X", "state": "arkansas", "doc_types": ["board_minutes"],
            "competitors": [], "categories": [], "doc_count": 1, "client_present": False}
    base.update(kw)
    return base


def test_score_is_bounded_and_deterministic():
    acc = _acc(competitors=["EAB", "Ellucian", "Workday Student"], doc_types=["board_minutes", "bid"])
    crm = {"status": "In Pipeline", "contact": {"properties": {}}, "deal_count": 1}
    s1, b1 = ops.score_account(acc, crm)
    s2, b2 = ops.score_account(acc, crm)
    assert s1 == s2 and b1 == b2           # deterministic
    assert 0 <= s1 <= 100
    assert sum(b1.values()) == s1


def test_score_rewards_competitors_and_pipeline():
    weak = ops.score_account(_acc(competitors=[]), {"status": "Whitespace", "contact": None})[0]
    strong = ops.score_account(
        _acc(competitors=["EAB", "Ellucian", "Civitas"], doc_types=["board_minutes", "bid", "transparency"]),
        {"status": "In Pipeline", "contact": {"properties": {}}},
    )[0]
    assert strong > weak


def test_no_expiry_component_leaks_into_breakdown():
    _, breakdown = ops.score_account(_acc(), {"status": "Cold", "contact": None})
    assert "expiry" not in " ".join(breakdown.keys()).lower()
    assert set(breakdown) == {"competitor_pressure", "evidence", "bid", "board_signal", "crm_readiness"}


# --------------------------- signal / opener text ---------------------------

def test_top_signal_and_incumbent():
    cfg = ops.OpsConfig()
    ar = _acc(competitors=["EAB", "Workday Student"])
    assert "EAB" in ops.top_signal(ar)
    assert ops.incumbent(ar, cfg) == "EAB, Workday Student"
    ct = _acc(competitors=[], client_present=True, doc_types=["transparency"])
    assert cfg.client in ops.incumbent(ct, cfg)


def test_opener_is_grounded_no_fabrication():
    cfg = ops.OpsConfig()
    ar = _acc(competitors=["EAB"], doc_types=["board_minutes"],
              categories=["SIS / ERP Migration"])
    line = ops.opener(ar, {"status": "Cold", "contact": None}, cfg)
    assert "EAB" in line and cfg.client in line
    # No stray dollar figures - openers never invent numbers.
    assert "$" not in line
    ct = ops.opener(_acc(client_present=True, state="connecticut"),
                    {"status": "In Pipeline", "contact": None}, cfg)
    assert cfg.client in ct and "Connecticut" in ct


# ----------------------- company / contact selection -----------------------

def test_best_company_match_prefers_exact_name():
    results = [
        {"id": "9", "properties": {"name": "University of Texas at El Paso"}},
        {"id": "1", "properties": {"name": "University of Texas System"}},
    ]
    assert ops._best_company_match("University of Texas System", results)["id"] == "1"
    assert ops._best_company_match("Nonexistent", []) is None


def test_best_contact_prefers_decision_maker_title():
    contacts = [
        {"properties": {"firstname": "A", "lastname": "Analyst", "jobtitle": "Data Analyst"}},
        {"properties": {"firstname": "R", "lastname": "Reg", "jobtitle": "Registrar", "email": "r@x.edu"}},
    ]
    best = ops._best_contact(contacts)
    assert best["properties"]["lastname"] == "Reg"
    assert ops._fmt_contact(None) == "(enrich - no CRM contact)"


# ----------------------------- hubspot_status -----------------------------

def test_status_flags_missing_token(monkeypatch):
    monkeypatch.setattr(hubspot_client, "load_token", lambda path=hubspot_client.CONFIG_PATH: None)
    st = ops.hubspot_status()
    assert st["ok"] is False and "token" in st["reason"].lower()


def test_status_uses_client_ping():
    class _C:
        def ping(self): return {"ok": True, "reason": "ok"}
    assert ops.hubspot_status(client=_C())["ok"] is True


# ------------------------- run_full_motion_play -------------------------

class FakeClient:
    """Stand-in for hubspot_client.HubSpotClient - no HTTP."""
    def __init__(self, companies=None, deals=None, contacts=None, ping_ok=True):
        self._companies = companies or {}   # name-substring -> [result,...]
        self._deals = deals or {}           # company_id -> [deal,...]
        self._contacts = contacts or {}     # company_id -> [contact,...]
        self._ping_ok = ping_ok

    def ping(self):
        return {"ok": self._ping_ok, "reason": "ok" if self._ping_ok else "rejected"}

    def search_company(self, name, limit=5):
        for key, res in self._companies.items():
            if key.lower() in name.lower():
                return res
        return []

    def company_deals(self, company_id):
        return self._deals.get(str(company_id), [])

    def company_contacts(self, company_id):
        return self._contacts.get(str(company_id), [])


def test_crm_lookup_classifies_status():
    client = FakeClient(
        companies={"arkansas": [{"id": "1", "properties": {"name": "University of Arkansas System"}}]},
        deals={},  # no deals => Cold
        contacts={"1": [{"properties": {"firstname": "Jeanne", "lastname": "Stovall", "jobtitle": "Registrar"}}]},
    )
    ar = ops.crm_lookup(client, _acc(institution="University of Arkansas System"))
    assert ar["status"] == "Cold" and ar["contact"]["properties"]["lastname"] == "Stovall"
    # No company match => Whitespace.
    assert ops.crm_lookup(client, _acc(institution="Ghost University"))["status"] == "Whitespace"
    # A deal present => In Pipeline.
    client2 = FakeClient(
        companies={"charter": [{"id": "5", "properties": {"name": "Charter Oak State College"}}]},
        deals={"5": [{"properties": {"dealname": "Renewal"}}]},
    )
    assert ops.crm_lookup(client2, _acc(institution="Charter Oak State College"))["status"] == "In Pipeline"


def test_run_play_blocks_when_hubspot_unavailable(tmp_conn):
    res = ops.run_full_motion_play(conn=tmp_conn, client=FakeClient(ping_ok=False))
    assert res["ok"] is False and "reject" in res["error"].lower()


def test_run_play_needs_signals(tmp_conn):
    res = ops.run_full_motion_play(conn=tmp_conn, client=FakeClient(ping_ok=True))
    assert res["ok"] is False and "scrape" in res["error"].lower()


def test_run_play_happy_path_ranks_and_saves(tmp_conn, monkeypatch):
    _seed_signals(tmp_conn)
    client = FakeClient(
        companies={"arkansas": [{"id": "1", "properties": {"name": "University of Arkansas System", "lifecyclestage": "lead"}}]},
        deals={},
        contacts={"1": [{"properties": {"firstname": "Jeanne", "lastname": "Stovall",
                                        "jobtitle": "Registrar", "email": "j@uasys.edu"}}]},
    )
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "ops"
        monkeypatch.setattr(ops, "OPS_REPORTS_DIR", out_dir)
        progress = []
        res = ops.run_full_motion_play(on_progress=progress.append, conn=tmp_conn, client=client)

        assert res["ok"] is True
        md = res["markdown"]
        assert "University of Arkansas System" in md
        assert "Rank | Agency | Score" in md
        assert "Jeanne Stovall - Registrar" in md
        assert "Cold" in md            # UA System: in CRM, no deal
        saved = Path(res["report_path"])
        assert saved.exists() and saved.read_text(encoding="utf-8").startswith("# Full Motion")
    assert any("Cross-referencing HubSpot" in p for p in progress)


# ----------------------- hubspot_client (no HTTP) -----------------------

def test_client_requires_token():
    with pytest.raises(hubspot_client.HubSpotError):
        hubspot_client.HubSpotClient("")


def test_client_from_config_none_without_token(monkeypatch):
    monkeypatch.setattr(hubspot_client, "load_token", lambda path=hubspot_client.CONFIG_PATH: None)
    assert hubspot_client.HubSpotClient.from_config() is None


def test_client_has_no_write_methods():
    # Read-only guarantee: no create/update/delete surface on the client.
    for banned in ("create", "update", "delete", "post_object", "write"):
        assert not any(banned in m for m in dir(hubspot_client.HubSpotClient)), f"write-ish method: {banned}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
