"""
Grants.gov federal grant-OPPORTUNITY adapter - the public "Search2" API.
Docs: https://www.grants.gov/api/search2  (POST https://api.grants.gov/v1/api/search2)

Why: Grants.gov is the federal government's grant/assistance board - a single,
**keyless**, **nationwide** feed of funding OPPORTUNITIES (open + forecasted),
i.e. the forward calendar of federal money colleges can apply for. It completes
the federal picture:

  - USAspending  -> grants already AWARDED  (who won; a budget/mandate signal)
  - SAM.gov      -> contract RFPs/RFIs      (procurement solicitations)
  - Grants.gov   -> grant OPPORTUNITIES     (funding announcements, forward-looking)

Unlike SAM, no API key is needed - the Search2 API is public. Kept tight the
same way bids and SAM notices are: pull the Education funding category, then
require a SEAtS bid-category match (keywords.yaml) - only student-success /
retention / scheduling / attendance / SIS-relevant grants are stored, so this
doesn't flood the DB with unrelated federal grants (NIH research, USDA, etc.).

An opportunity whose CFDA/ALN is one of the Dept-of-Ed student-success programs
(TRIO / GEAR UP / Title III-V / FIPSE - the same set USAspending pulls) is kept
even if its title carries no SEAtS keyword, with the ICP category guaranteed
from the program - exactly like the USAspending adapter. That catches an open
"TRIO Talent Search" cycle that a plain keyword match would miss.

Stored as `doc_type='federal_grant_opp'`, `source='grants_gov'`. Grants.gov
opportunities are national programs (the applicants are colleges), so there is
no place-of-performance state: `state=''` and `institution` = the funding agency.
"""

from __future__ import annotations

import re
import time

import requests

from . import utils
from .sam_gov import PREFILTER_TERMS          # shared education pre-screen (one source of truth)
from .usaspending_scraper import STUDENT_SUCCESS_PROGRAMS  # CFDA -> readable label
from .utils import log

SEARCH_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL_TMPL = "https://www.grants.gov/search-results-detail/{id}"
CONFIG_PATH = utils.ROOT_DIR / "config" / "grants_gov.yaml"

# Defaults (overridable in config/grants_gov.yaml).
#
# Query LENSES. The "Education" funding category is a trap - it's ~93% NIH/NSF
# research-training grants (CFDA 93.xxx), not Dept-of-Ed money colleges apply
# for. So the precise default lenses are:
#   1. cfda      -> the Dept-of-Ed student-success programs (TRIO/GEAR UP/Title
#                   III-V/FIPSE), the exact set USAspending pulls. Every hit is
#                   guaranteed-ICP by its CFDA - near-zero noise. This is the
#                   "a SEAtS-relevant grant competition just opened" signal.
#   2. agencies  -> all Dept-of-Education-issued opportunities, then narrowed by
#                   a SEAtS title/category match (catches well-named competitions
#                   outside the CFDA set, e.g. a new "Postsecondary Student
#                   Success" grant). Cheap - Dept-of-Ed's open-opp count is small.
# The noisy fundingCategories="ED"/keyword lens is OFF by default; enable it in
# config only if you want the wider (and mostly irrelevant) net.
DEFAULT_STATUSES = "posted|forecasted"     # open now + announced/upcoming
_STUDENT_SUCCESS_CFDAS = set(STUDENT_SUCCESS_PROGRAMS)   # {"84.031", "84.044", ...}
DEFAULT_CFDA = "|".join(sorted(_STUDENT_SUCCESS_CFDAS))  # precise ICP lens
DEFAULT_AGENCIES = "ED"                     # Dept-of-Education issuer lens
DEFAULT_FUNDING_CATEGORIES = ""             # off by default (ED category is ~93% NIH)
DEFAULT_ROWS = 100                          # page size (API hard cap is 1000)
DEFAULT_MAX_PAGES = 5                       # bound one run's scan of a busy window

_BASE_CATEGORY = "Student Success & Retention"

# Grants.gov's CDN is normally friendly, but the shared session UA contains
# "Bot" (which trips some WAFs - see usaspending_scraper). Send clean, honest
# headers on this host only, overriding whatever the session carries.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; govspend_free/0.1; personal grant research)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_MDY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def load_config() -> dict:
    """Return the parsed config/grants_gov.yaml (or {} if absent). Keyless -
    the only thing that matters for gating is `enabled`."""
    import yaml
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def is_configured() -> bool:
    return bool(load_config().get("enabled"))


def _search(session, body: dict) -> tuple[dict | None, str | None]:
    """POST the Search2 endpoint. Returns (data, None) or (None, reason).
    `data` is the payload's inner `data` object (hitCount, oppHits, ...)."""
    poster = session.post if session is not None else requests.post
    try:
        resp = poster(SEARCH_URL, json=body, headers=_HEADERS, timeout=utils.DEFAULT_TIMEOUT)
    except requests.RequestException:
        return None, "grants_fetch_failed"
    finally:
        time.sleep(utils.POLITE_DELAY_SECONDS)   # 0 during tests
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        return None, f"grants_http_{status}"
    try:
        payload = resp.json()
    except ValueError:
        return None, "grants_bad_json"
    code = payload.get("errorcode")
    if code not in (0, None):
        return None, f"grants_api_error_{code}"
    return payload.get("data") or {}, None


def scrape_grants_gov(session, seen: set[str], matchers, *,
                      statuses: str = DEFAULT_STATUSES,
                      cfda: str = DEFAULT_CFDA, agencies: str = DEFAULT_AGENCIES,
                      funding_categories: str = DEFAULT_FUNDING_CATEGORIES,
                      keyword: str = "", eligibilities: str = "",
                      rows: int = DEFAULT_ROWS,
                      max_pages: int = DEFAULT_MAX_PAGES) -> tuple[list[dict], list[dict]]:
    """Pull recent Grants.gov opportunities, keep the SEAtS-relevant ones.
    Returns (new_matches, skipped). `matchers` are the shared category matchers.

    Runs one or more query LENSES (cfda / agencies / fundingCategories+keyword)
    and merges them through a single per-record keep + dedup, so overlaps
    collapse. See the DEFAULT_* notes above for why the CFDA + agency lenses are
    the precise defaults and the Education funding category is not.
    """
    rows = max(1, min(1000, int(rows)))
    statuses = statuses or DEFAULT_STATUSES

    # Assemble the query lenses from whatever is configured. Each is a partial
    # Search2 body; the shared fields (statuses/rows/eligibilities) are added per
    # request. Dedup across lenses is handled by `seen` (hash of the opp id).
    lenses: list[tuple[str, dict]] = []
    if cfda:
        # The Search2 `cfda` filter only matches a SINGLE code reliably (pipe/
        # comma lists return nothing; space is a loose OR). So expand the
        # configured list - written pipe/comma/space-separated for readability -
        # into one exact query per program. Each is cheap (usually 0 open opps).
        for code in [c for c in re.split(r"[\s|,]+", cfda) if c]:
            lenses.append((f"cfda:{code}", {"cfda": code}))
    if agencies:
        lenses.append(("agencies", {"agencies": agencies}))
    if funding_categories or keyword:
        extra: dict = {}
        if funding_categories:
            extra["fundingCategories"] = funding_categories
        if keyword:
            extra["keyword"] = keyword
        lenses.append(("custom", extra))
    if not lenses:
        lenses.append(("all", {}))   # unfiltered fallback

    matches: list[dict] = []
    skipped: list[dict] = []
    for name, lens in lenses:
        base: dict = {"oppStatuses": statuses, "rows": rows, **lens}
        if eligibilities:
            base["eligibilities"] = eligibilities
        lens_matches, reason = _scan_lens(session, base, seen, matchers, max_pages, name)
        matches.extend(lens_matches)
        if reason is not None:
            skipped.append({"url": SEARCH_URL, "reason": reason,
                            "notes": f"Grants.gov {name} lens failed (network/API)"})
    return matches, skipped


def _scan_lens(session, base: dict, seen: set[str], matchers, max_pages: int,
               name: str) -> tuple[list[dict], str | None]:
    """Paginate one query lens, returning (new kept matches, reason-or-None)."""
    matches: list[dict] = []
    offset = pages = total = 0
    truncated = False
    while True:
        data, reason = _search(session, {**base, "startRecordNum": offset})
        if reason is not None:
            return matches, reason
        hits = data.get("oppHits") or []
        for rec in hits:
            m = _record_to_match(rec, matchers)
            if m is None:
                continue
            h = utils.item_hash("grants_gov", m["_id"])
            if h in seen:
                continue
            seen.add(h)
            m.pop("_id", None)
            matches.append(m)
        pages += 1
        offset += len(hits)
        total = int(data.get("hitCount") or len(hits))
        if not hits or offset >= total or pages >= max_pages:
            truncated = offset < total
            break

    if truncated:
        # No silent caps: say what was left unscanned.
        log.warning("  [grants] %s lens had more opportunities than scanned (%d of %d); raise "
                    "rows/max_pages in config/grants_gov.yaml to cover the rest", name, offset, total)
    return matches, None


def _record_to_match(rec: dict, matchers) -> dict | None:
    """One Grants.gov opportunity -> a federal_grant_opp document match, or None
    if it's not education-related or matches no SEAtS category."""
    raw_id = str(rec.get("id") or "").strip()
    title = str(rec.get("title") or "").strip()
    agency = str(rec.get("agency") or rec.get("agencyCode") or "").strip()
    number = str(rec.get("number") or "").strip()
    if not title and not raw_id:
        return None

    cfdas = [str(c).strip() for c in (rec.get("cfdaList") or []) if str(c).strip()]
    is_student_success = bool(set(cfdas) & _STUDENT_SUCCESS_CFDAS)

    # Education-context pre-screen (unless a student-success CFDA already vouches
    # for it). Ordinary topic words - "retention", "attendance" - fire on
    # unrelated federal grants (NIH cell "retention", etc.); the pre-screen plus
    # the SEAtS category match below is what keeps this tight, same as SAM.
    prescreen = f"{title} {agency}".lower()
    if not is_student_success and not any(t in prescreen for t in PREFILTER_TERMS):
        return None

    status = str(rec.get("oppStatus") or "").strip()
    open_date = _iso_date(rec.get("openDate"))
    close_date = _iso_date(rec.get("closeDate")) or str(rec.get("closeDate") or "").strip()

    blob = " ".join(p for p in (
        title,
        f"({status})" if status else "",
        agency,
        f"CFDA {', '.join(cfdas)}" if cfdas else "",
        number,
        f"closes {close_date}" if close_date else "",
    ) if p)

    categories = utils.match_categories(blob, matchers)
    if is_student_success:
        # Guarantee the ICP category from the CFDA program (like USAspending),
        # then union whatever the keyword matchers also found.
        labels = [STUDENT_SUCCESS_PROGRAMS[c] for c in cfdas if c in STUDENT_SUCCESS_PROGRAMS]
        guaranteed = [_BASE_CATEGORY] + [f"Federal Grant: {lbl}" for lbl in labels]
        categories = guaranteed + [c for c in categories if c not in guaranteed]
    elif not categories:
        return None   # education-adjacent but no SEAtS topic -> drop (noise control)

    if raw_id:
        opp_id, url = raw_id, DETAIL_URL_TMPL.format(id=raw_id)
    else:
        opp_id = utils.item_hash(number, title, open_date)
        url = f"https://www.grants.gov/search-grants?keywords={number or title}".strip()

    return {
        "_id": opp_id,
        "title": title or "Untitled federal grant opportunity",
        "institution": agency,       # the funding federal agency
        "state": "",                 # nationwide program - no place-of-performance
        "url": url,
        "text": blob,
        "date": open_date,
        "categories": categories,
    }


def _iso_date(value) -> str:
    """Grants.gov dates are 'MM/DD/YYYY'. Return 'YYYY-MM-DD' (or '')."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = _MDY_RE.match(s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    iso = _ISO_RE.match(s)
    return iso.group(1) if iso else ""
