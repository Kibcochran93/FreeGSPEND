#!/usr/bin/env python3
"""
Offline tests for the SAM.gov federal-RFP adapter. Serves a canned
opportunities payload instead of the live API. No network, no key.
"""

from pathlib import Path

import yaml

from govspend_free import sam_gov, utils

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        return FakeResponse(self.payload, self.status_code)


def _matchers():
    kw = yaml.safe_load((ROOT / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(kw["categories"])


PAYLOAD = {
    "totalRecords": 3, "limit": 1000, "offset": 0,
    "opportunitiesData": [
        {  # education + SEAtS category ("retention"/"early alert") -> KEPT, MT
            "noticeId": "N1", "title": "Student Retention and Early Alert Platform",
            "solicitationNumber": "ED-2026-001", "type": "Solicitation",
            "fullParentPathName": "DEPARTMENT OF EDUCATION", "postedDate": "2026-07-30",
            "responseDeadLine": "2026-08-20T17:00:00-04:00",
            "placeOfPerformance": {"state": {"code": "MT", "name": "Montana"}},
            "uiLink": "https://sam.gov/opp/N1/view", "active": "Yes",
        },
        {  # passes the education pre-screen ("campus") but no SEAtS category -> DROPPED
            "noticeId": "N2", "title": "Campus Landscaping and Mowing Services",
            "type": "Solicitation", "fullParentPathName": "GENERAL SERVICES ADMINISTRATION",
            "postedDate": "2026-07-30", "placeOfPerformance": {"state": {"code": "TX"}},
        },
        {  # not education -> dropped at the pre-screen
            "noticeId": "N3", "title": "Aircraft Turbine Engine Components",
            "type": "Solicitation", "fullParentPathName": "DEPARTMENT OF DEFENSE",
            "postedDate": "2026-07-30", "placeOfPerformance": {"state": {"code": "VA"}},
        },
    ],
}


def test_sam_filters_to_seats_relevant_and_attributes_state():
    print("[test] sam.gov adapter ...")
    matchers = _matchers()
    session = FakeSession(PAYLOAD)
    seen: set[str] = set()

    matches, skipped = sam_gov.scrape_sam_gov(session, seen, matchers, api_key="TESTKEY")
    assert not skipped, skipped
    assert len(matches) == 1, f"expected only the retention RFP, got {matches}"
    m = matches[0]
    assert "Retention" in m["title"], m
    assert m["state"] == "montana", m                 # MT place-of-performance -> state key
    assert m["categories"], m
    assert m["url"] == "https://sam.gov/opp/N1/view", m
    assert m["date"] == "2026-07-30", m

    again, _ = sam_gov.scrape_sam_gov(session, seen, matchers, api_key="TESTKEY")
    assert again == [], f"expected dedup to suppress repeats, got {again}"
    print("  OK - kept 1 SEAtS-relevant federal RFP, attributed state, deduped")


def test_sam_no_key_is_skipped():
    matchers = _matchers()
    matches, skipped = sam_gov.scrape_sam_gov(FakeSession(PAYLOAD), set(), matchers, api_key="")
    assert matches == []
    assert skipped and skipped[0]["reason"] == "sam_not_configured", skipped


def test_sam_auth_failure_reported_without_leaking_key():
    matchers = _matchers()
    session = FakeSession({}, status_code=401)
    matches, skipped = sam_gov.scrape_sam_gov(session, set(), matchers, api_key="SECRETKEY")
    assert matches == []
    assert skipped and skipped[0]["reason"] == "sam_auth_failed", skipped
    assert "SECRETKEY" not in str(skipped), "api key must never appear in skip output"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
