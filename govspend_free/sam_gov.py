"""
SAM.gov federal RFP adapter - the "Get Opportunities Public API".
Docs: https://open.gsa.gov/api/get-opportunities-public-api/

Why: SAM.gov is the federal government's solicitation board - a single, keyed,
**nationwide** feed. One adapter gives FreeGSPEND federal RFP reach in all 50
states at once (the tool's national-coverage goal, on the federal axis).
USAspending gives us federal *grants*; this gives us federal *contract
opportunities* (RFPs/RFIs/sources-sought).

Stored as `doc_type='federal_rfp'`, `source='sam_gov'`, attributed to the
place-of-performance state when present. Kept tight the same way bids are: an
education pre-screen, then a SEAtS bid-category match - only relevant notices
are stored, so this doesn't flood the DB with unrelated federal procurement.

Auth: a free API key (SAM.gov account -> Account Details -> API Key). Put it in
`config/sam.yaml` (`api_key:`) or the `SAM_API_KEY` env var. The key travels as a
query parameter, so this module fetches via the session directly and logs only
HTTP status codes - never the URL or the raw exception - so the key can't leak
into logs.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import yaml

from . import utils
from .coverage import US_STATES, state_key
from .utils import log

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
CONFIG_PATH = utils.ROOT_DIR / "config" / "sam.yaml"

# 2-letter place-of-performance code -> sources.yaml/DB state key.
_ABBR_TO_KEY = {abbr: state_key(name) for abbr, name in US_STATES}

# Education-CONTEXT pre-screen (substring, lowercased), required before the SEAtS
# category match. This must be strictly about higher-ed/education - NOT the SEAtS
# topic words (retention, attendance, scheduling, ...), because those are ordinary
# English that fire on unrelated federal procurement: "retention POND" (USDA),
# "retention CLAMP" (Defense Logistics), "attendance at a conference", etc. A real
# education RFP still passes here via its title or its soliciting org (e.g. issuer
# "DEPARTMENT OF EDUCATION" contains "education"); the SEAtS category match then
# supplies topic relevance.
PREFILTER_TERMS = (
    "student", "university", "college", "campus", "academ", "education",
    "enroll", "higher ed", "community college", "school district", "registrar",
    "faculty", "classroom", "coursework", "provost", "student success",
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_config() -> tuple[dict, str]:
    """Return (config dict, api_key). Key comes from config/sam.yaml or the
    SAM_API_KEY env var. Missing file/key is fine (returns {}, "")."""
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    key = str(cfg.get("api_key") or os.environ.get("SAM_API_KEY", "") or "").strip()
    return cfg, key


def is_configured() -> bool:
    cfg, key = load_config()
    return bool(cfg.get("enabled") and key)


def _sam_get(session, params: dict) -> tuple[dict | None, str | None]:
    """GET the search endpoint. Returns (payload, None) or (None, reason).
    Never logs the URL or exception (they contain the api_key) - only a reason."""
    sess = session or utils.get_session()
    try:
        resp = sess.get(SEARCH_URL, timeout=utils.DEFAULT_TIMEOUT, params=params)
    except requests.RequestException:
        return None, "sam_fetch_failed"
    finally:
        time.sleep(utils.POLITE_DELAY_SECONDS)   # 0 during tests
    status = getattr(resp, "status_code", 200)
    if status in (401, 403):
        return None, "sam_auth_failed"           # bad/missing key
    if status == 429:
        return None, "sam_rate_limited"          # daily quota exhausted
    if status >= 400:
        return None, f"sam_http_{status}"
    try:
        return resp.json(), None
    except ValueError:
        return None, "sam_bad_json"


def scrape_sam_gov(session, seen: set[str], matchers, *, api_key: str,
                   lookback_days: int = 3, page_size: int = 1000,
                   max_pages: int = 1) -> tuple[list[dict], list[dict]]:
    """Pull recent SAM.gov solicitations, keep the SEAtS-relevant ones. Returns
    (new_matches, skipped). `matchers` are the shared category matchers."""
    if not api_key:
        return [], [{"url": SEARCH_URL, "reason": "sam_not_configured",
                     "notes": "set config/sam.yaml api_key or the SAM_API_KEY env var"}]

    end = date.today()
    start = end - timedelta(days=max(1, lookback_days))
    page_size = max(1, min(1000, page_size))     # SAM.gov hard cap is 1000
    base = {"api_key": api_key, "postedFrom": start.strftime("%m/%d/%Y"),
            "postedTo": end.strftime("%m/%d/%Y"), "limit": page_size}

    matches: list[dict] = []
    offset = pages = 0
    truncated = False
    while True:
        payload, reason = _sam_get(session, {**base, "offset": offset})
        if reason is not None:
            return matches, [{"url": SEARCH_URL, "reason": reason,
                              "notes": "SAM.gov request failed (key/quota/network)"}]
        records = payload.get("opportunitiesData") or []
        for rec in records:
            m = _record_to_match(rec, matchers)
            if m is None:
                continue
            h = utils.item_hash("sam", m["_id"])
            if h in seen:
                continue
            seen.add(h)
            m.pop("_id", None)
            matches.append(m)
        pages += 1
        offset += len(records)
        total = int(payload.get("totalRecords") or len(records))
        if not records or offset >= total or pages >= max_pages:
            truncated = offset < total
            break

    if truncated:
        # No silent caps: say what was left unscanned.
        log.warning("  [sam] window had more notices than scanned (%d of %d); raise "
                    "max_pages or narrow lookback_days to cover the rest", offset, total)
    return matches, []


def _record_to_match(rec: dict, matchers) -> dict | None:
    """One SAM.gov opportunity -> a federal_rfp document match, or None if it's
    not education-related or matches no SEAtS category."""
    notice_id = str(rec.get("noticeId") or "").strip()
    title = str(rec.get("title") or "").strip()
    issuer = str(rec.get("fullParentPathName") or rec.get("department")
                 or rec.get("subTier") or "").strip()
    if not title and not notice_id:
        return None

    prescreen = f"{title} {issuer}".lower()
    if not any(term in prescreen for term in PREFILTER_TERMS):
        return None

    sol = str(rec.get("solicitationNumber") or "").strip()
    ntype = str(rec.get("type") or "").strip()
    posted = _date_only(rec.get("postedDate"))
    deadline = _date_only(rec.get("responseDeadLine")) or str(rec.get("responseDeadLine") or "").strip()
    state = _pop_state(rec.get("placeOfPerformance"))

    blob = " ".join(p for p in (title, ntype, sol, issuer,
                                f"closes {deadline}" if deadline else "") if p)
    categories = utils.match_categories(blob, matchers)
    if not categories:
        return None

    if not notice_id:
        notice_id = utils.item_hash(sol, title, posted)
    url = str(rec.get("uiLink") or f"https://sam.gov/opp/{notice_id}/view").strip()
    return {
        "_id": notice_id,
        "title": title or "Untitled federal opportunity",
        "institution": issuer,          # the soliciting federal org
        "state": state,                 # place-of-performance state key, or ""
        "url": url,
        "text": blob,
        "date": posted,
        "categories": categories,
    }


def _pop_state(pop) -> str:
    if not isinstance(pop, dict):
        return ""
    s = pop.get("state")
    code = s.get("code") if isinstance(s, dict) else s
    return _ABBR_TO_KEY.get(str(code or "").strip().upper(), "")


def _date_only(value) -> str:
    m = _DATE_RE.search(str(value or ""))
    return m.group(0) if m else ""
