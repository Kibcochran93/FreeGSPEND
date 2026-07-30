"""
USAspending.gov federal-award source - the one spending feed that actually
covers SEAtS's core territory.

State checkbooks (Socrata) only exist for a few non-core states, and the big
ones are Tableau/WAF-walled. USAspending is the opposite: a free, keyless,
nationwide REST API (Treasury/DATA Act) with no browser or WAF wall. It won't
show a university buying EAB/Ellucian (that's institutional, not federal), so
it does NOT replace competitor-footprint intel. What it adds is a *budget +
mandate* signal: which institutions just landed federal money earmarked for
student success, retention, and access - exactly SEAtS's ICP trigger - in
Missouri, Oklahoma, Kansas, Nebraska, and everywhere else.

Endpoint (POST, no auth): https://api.usaspending.gov/api/v2/search/spending_by_award/
See config/sources.yaml `type: usaspending` entries.
"""

from __future__ import annotations

import datetime as dt

import requests

from . import utils
from .utils import log

AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL_TMPL = "https://www.usaspending.gov/award/{gid}"
TIMEOUT = 30

# USAspending's CDN returns a 500 block page for User-Agents containing "Bot"
# (which the project's default session UA does). Send a clean, still-honest UA
# for this host only, overriding whatever the shared session carries.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; govspend_free/0.1; personal procurement research)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
PAGE_LIMIT = 100
MAX_PAGES = 10  # be polite; 1000 awards/state/run is plenty

# Grant award-type codes (block/formula/project grants + cooperative agreements).
GRANT_TYPE_CODES = ["02", "03", "04", "05"]

# Dept. of Education CFDA programs that fund student success / retention /
# access work - SEAtS's ICP. Each maps to a readable program label and the
# category the document is tagged with (so it scores in Opportunities even if
# keywords.yaml doesn't happen to contain the program's exact wording).
STUDENT_SUCCESS_PROGRAMS = {
    "84.031": "Higher Education Institutional Aid (Title III/V)",
    "84.042": "TRIO Student Support Services",
    "84.044": "TRIO Talent Search",
    "84.047": "TRIO Upward Bound",
    "84.066": "TRIO Educational Opportunity Centers",
    "84.334": "GEAR UP",
    "84.116": "Fund for the Improvement of Postsecondary Education (FIPSE)",
}
DEFAULT_PROGRAMS = list(STUDENT_SUCCESS_PROGRAMS)
_BASE_CATEGORY = "Student Success & Retention"

_FIELDS = ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
           "Start Date", "End Date", "CFDA Number"]

# Keep higher-ed recipients, drop K-12 districts / community orgs that also
# receive TRIO/GEAR UP money (they aren't SEAtS accounts).
import re as _re
_HIGHER_ED_RE = _re.compile(
    r"universit|college|institut|regents|curators|polytechnic|state u\b", _re.IGNORECASE)
_NOT_HIGHER_ED_RE = _re.compile(r"school district|public schools|isd\b|unified school", _re.IGNORECASE)


def _looks_higher_ed(name: str) -> bool:
    return bool(_HIGHER_ED_RE.search(name or "")) and not _NOT_HIGHER_ED_RE.search(name or "")


def _default_time_period(years: int) -> dict:
    end = dt.date.today()
    start = end.replace(year=end.year - years)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def _request_page(session, state: str, programs: list[str], time_period: dict, page: int):
    body = {
        "filters": {
            "award_type_codes": GRANT_TYPE_CODES,
            "time_period": [time_period],
            "recipient_locations": [{"country": "USA", "state": state}],
            "program_numbers": programs,
        },
        "fields": _FIELDS,
        "page": page, "limit": PAGE_LIMIT, "sort": "Award Amount", "order": "desc",
    }
    poster = session.post if session is not None else requests.post
    resp = poster(AWARD_SEARCH_URL, json=body, headers=_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def scrape_usaspending(source: dict, session, seen: set[str],
                       matchers, watchlist_patterns) -> tuple[list[dict], list[dict]]:
    """Pull federal student-success grants for one state from USAspending.

    `source` (a config/sources.yaml `type: usaspending` entry) supports:
        state:          2-letter USPS code (required, e.g. "MO")
        programs:       list of CFDA numbers (default: the student-success set)
        lookback_years: how far back to search (default 3)
        higher_ed_only: keep only college/university recipients (default True)

    Returns (matches, skipped) in the same shape as the other scrapers.
    """
    state = (source.get("state") or "").strip().upper()
    if not state:
        return [], [{"url": AWARD_SEARCH_URL, "reason": "usaspending_misconfigured",
                     "notes": "usaspending source needs a 2-letter `state` code"}]

    programs = source.get("programs") or DEFAULT_PROGRAMS
    years = int(source.get("lookback_years", 3))
    higher_ed_only = source.get("higher_ed_only", True)
    time_period = _default_time_period(years)

    matches: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            data = _request_page(session, state, programs, time_period, page)
        except requests.RequestException as exc:
            return matches, [{"url": AWARD_SEARCH_URL, "reason": "usaspending_http_error",
                              "notes": f"{state} page {page}: {exc}"}]

        results = data.get("results", []) or []
        for r in results:
            recipient = (r.get("Recipient Name") or "").strip()
            if not recipient:
                continue
            if higher_ed_only and not _looks_higher_ed(recipient):
                continue

            award_id = r.get("Award ID") or r.get("generated_internal_id") or ""
            key = utils.item_hash("usaspending", state, str(award_id), recipient)
            if key in seen:
                continue
            seen.add(key)

            cfda = (r.get("CFDA Number") or "").strip()
            program_title = STUDENT_SUCCESS_PROGRAMS.get(cfda, f"CFDA {cfda}" if cfda else "Federal grant")
            amount = r.get("Award Amount")
            agency = r.get("Awarding Agency") or ""
            start_date = r.get("Start Date") or ""
            end_date = r.get("End Date") or ""
            gid = r.get("generated_internal_id")
            award_url = AWARD_URL_TMPL.format(gid=gid) if gid else "https://www.usaspending.gov/search"

            amount_str = f"${amount:,.0f}" if isinstance(amount, (int, float)) else str(amount or "")
            blob = (f"{recipient} | {program_title} (CFDA {cfda}) | {agency} | {amount_str} | "
                    f"{start_date}..{end_date}")

            # Category tags: guarantee the ICP category from the CFDA program,
            # then union whatever the keyword matchers also find in the blob.
            categories = [_BASE_CATEGORY, f"Federal Grant: {program_title}"]
            for c in utils.match_categories(blob, matchers):
                if c not in categories:
                    categories.append(c)
            watchlist_hits = utils.match_watchlist(blob, watchlist_patterns)

            matches.append({
                "institution": recipient,
                "title": f"{recipient} - {program_title} - {amount_str}".strip(" -"),
                "award_url": award_url,
                "blob": blob,
                "date": start_date,
                "amount": amount,
                "amount_str": amount_str,
                "cfda": cfda,
                "program_title": program_title,
                "agency": agency,
                "start_date": start_date,
                "end_date": end_date,
                "categories": categories,
                "watchlist_hits": watchlist_hits,
            })

        if not data.get("page_metadata", {}).get("hasNext"):
            break

    log.info("  [federal] %s: %d student-success grant(s) to higher-ed recipients", state, len(matches))
    return matches, []
