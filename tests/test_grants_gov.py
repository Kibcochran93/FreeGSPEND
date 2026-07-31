#!/usr/bin/env python3
"""
Offline tests for the Grants.gov federal grant-opportunity adapter. Serves a
canned Search2 payload instead of the live API. No network, no key (Grants.gov
is keyless).
"""

from pathlib import Path

import yaml

from govspend_free import grants_gov, utils

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, bad_json: bool = False):
        self._payload = payload
        self.status_code = status_code
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, payload, status_code: int = 200, bad_json: bool = False):
        self.payload = payload
        self.status_code = status_code
        self.bad_json = bad_json
        self.calls = 0
        self.bodies = []            # every POST body, for lens assertions

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.calls += 1
        self.bodies.append(json or {})
        return FakeResponse(self.payload, self.status_code, self.bad_json)


def _matchers():
    kw = yaml.safe_load((ROOT / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(kw["categories"])


def _envelope(opp_hits, hit_count=None, errorcode=0):
    return {"errorcode": errorcode, "msg": "Webservice Succeeds",
            "data": {"hitCount": hit_count if hit_count is not None else len(opp_hits),
                     "startRecord": 0, "oppHits": opp_hits}}


PAYLOAD = _envelope([
    {  # education + SEAtS category ("retention"/"early alert"), no student-success
       # CFDA -> kept via the keyword match. openDate -> ISO, detail URL.
        "id": "357498", "number": "ED-2026-001",
        "title": "Student Retention and Early Alert Grant Program",
        "agency": "Department of Education", "agencyCode": "ED",
        "openDate": "07/15/2026", "closeDate": "09/01/2026",
        "oppStatus": "posted", "docType": "synopsis", "cfdaList": [],
    },
    {  # NO SEAtS keyword in the title, but a Dept-of-Ed student-success CFDA
       # (84.044 = TRIO) -> kept via the CFDA guarantee, ICP category forced.
        "id": "360000", "number": "ED-TRIO-2026", "title": "TRIO Talent Search",
        "agency": "Department of Education", "openDate": "08/01/2026", "closeDate": "",
        "oppStatus": "forecasted", "docType": "forecast", "cfdaList": ["84.044"],
    },
    {  # passes the education pre-screen ("campus") but no SEAtS category and no
       # student-success CFDA -> dropped.
        "id": "361000", "number": "ED-FAC-2026",
        "title": "Campus Facilities Maintenance Grant",
        "agency": "Department of Education", "openDate": "07/20/2026",
        "oppStatus": "posted", "cfdaList": [],
    },
    {  # not education (NIH health research) -> dropped at the pre-screen.
        "id": "362000", "number": "NIH-R01",
        "title": "Dissemination and Implementation Research in Health",
        "agency": "National Institutes of Health", "openDate": "07/10/2026",
        "oppStatus": "posted", "cfdaList": ["93.279"],
    },
])


def test_grants_keeps_seats_relevant_and_forces_cfda_category():
    print("[test] grants.gov adapter ...")
    matchers = _matchers()
    session = FakeSession(PAYLOAD)
    seen: set[str] = set()

    matches, skipped = grants_gov.scrape_grants_gov(session, seen, matchers)
    assert not skipped, skipped
    assert len(matches) == 2, f"expected retention + TRIO, got {[m['title'] for m in matches]}"

    by_title = {m["title"]: m for m in matches}

    retention = by_title["Student Retention and Early Alert Grant Program"]
    assert retention["url"] == "https://www.grants.gov/search-results-detail/357498", retention
    assert retention["date"] == "2026-07-15", retention          # MM/DD/YYYY -> ISO
    assert retention["state"] == "", retention                   # nationwide
    assert retention["institution"] == "Department of Education", retention
    assert retention["categories"], retention                    # SEAtS keyword match

    trio = by_title["TRIO Talent Search"]
    # Kept despite no SEAtS keyword in the title: the 84.044 CFDA guarantees the
    # ICP category, same discipline as the USAspending adapter.
    assert "Student Success & Retention" in trio["categories"], trio
    assert any("TRIO" in c for c in trio["categories"]), trio

    again, _ = grants_gov.scrape_grants_gov(session, seen, matchers)
    assert again == [], f"expected dedup to suppress repeats, got {again}"
    print("  OK - kept 2 relevant opps (keyword + CFDA guarantee), dropped campus/NIH, deduped")


def test_grants_drops_non_education_and_uncategorized():
    matchers = _matchers()
    matches, skipped = grants_gov.scrape_grants_gov(FakeSession(PAYLOAD), set(), matchers)
    titles = {m["title"] for m in matches}
    assert "Campus Facilities Maintenance Grant" not in titles, "uncategorized education grant must drop"
    assert "Dissemination and Implementation Research in Health" not in titles, "NIH research must drop"
    assert not skipped, skipped


def test_grants_http_error_reported_gracefully():
    matchers = _matchers()
    session = FakeSession({}, status_code=503)
    matches, skipped = grants_gov.scrape_grants_gov(session, set(), matchers)
    assert matches == []
    assert skipped and skipped[0]["reason"] == "grants_http_503", skipped


def test_grants_api_errorcode_reported():
    matchers = _matchers()
    session = FakeSession(_envelope([], errorcode=1))
    matches, skipped = grants_gov.scrape_grants_gov(session, set(), matchers)
    assert matches == []
    assert skipped and skipped[0]["reason"] == "grants_api_error_1", skipped


def test_grants_bad_json_reported():
    matchers = _matchers()
    session = FakeSession(None, bad_json=True)
    matches, skipped = grants_gov.scrape_grants_gov(session, set(), matchers)
    assert matches == []
    assert skipped and skipped[0]["reason"] == "grants_bad_json", skipped


def test_grants_expands_cfda_into_individual_exact_queries():
    # The bug this guards: the Search2 `cfda` filter only matches a SINGLE code
    # (a pipe/comma list silently returns nothing). The config value is written
    # as a pipe-list for readability, so the adapter MUST expand it into one
    # exact query per code and never send a delimited list.
    matchers = _matchers()
    sess = FakeSession(_envelope([]))
    grants_gov.scrape_grants_gov(sess, set(), matchers,
                                 cfda="84.044|84.334", agencies="ED",
                                 funding_categories="", keyword="")
    cfda_vals = [b["cfda"] for b in sess.bodies if "cfda" in b]
    assert cfda_vals == ["84.044", "84.334"], cfda_vals
    assert all("|" not in b.get("cfda", "") for b in sess.bodies), "never send a pipe-delimited cfda"
    assert any(b.get("agencies") == "ED" for b in sess.bodies), "agency lens must run too"


def test_grants_lens_toggle_off():
    # Disabling a lens with "" must drop it entirely from the queries issued.
    matchers = _matchers()
    sess = FakeSession(_envelope([]))
    grants_gov.scrape_grants_gov(sess, set(), matchers,
                                 cfda="", agencies="ED", funding_categories="", keyword="")
    assert all("cfda" not in b for b in sess.bodies), "cfda lens should be off"
    assert sess.bodies and all(b.get("agencies") == "ED" for b in sess.bodies)


def test_iso_date_conversion():
    assert grants_gov._iso_date("07/15/2026") == "2026-07-15"
    assert grants_gov._iso_date("7/1/2026") == "2026-07-01"
    assert grants_gov._iso_date("2026-07-15") == "2026-07-15"
    assert grants_gov._iso_date("") == ""
    assert grants_gov._iso_date(None) == ""


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
