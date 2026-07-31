"""
Bonfire RFP-portal adapter (source `type: bonfire` in a university system's
`bid_boards`). Ported from the SEAtS RFP-monitor into govspend_free's
conventions.

Why this exists: Bonfire (bonfirehub.com) hosts a lot of higher-ed procurement,
and its portal page looks JS-rendered - but the page itself fetches its open
opportunities from a public JSON endpoint via XHR:

    https://<slug>.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData

So no browser is needed: a plain GET with an `X-Requested-With` header returns
the open opportunities as JSON. That closes part of the "bid boards are the weak
point" gap the README documents, without the Playwright machinery.

Like `bid_scraper`, this keeps only rows that match a SEAtS bid *category* (so a
portal's janitorial / construction / catering RFPs don't flood the feed), and
dedups via the shared `seen` set. Matches flow into the same `documents` table
(`doc_type='bid'`) as every other bid source, so they surface in
`--opportunities`, `--search`, and the Ops play with no extra wiring.

Public data only; no logins or access-control bypass (same posture as the other
scrapers). One portal == one `bid_boards` entry: `{type: bonfire, slug: <slug>}`.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import requests

from . import utils
from .utils import log

# Bonfire rate-limits by IP across ALL *.bonfirehub.com tenants at once, so in a
# national run the first HTTP 429 means every other portal will 429 too. When we
# hit one we set a process-wide cooldown and stop touching the network for the
# rest of the run (subsequent portals return a `rate_limited_cooldown` skip and
# get retried next run) instead of hammering a limiter that's already tripped.
_COOLDOWN_SECONDS = 900          # fallback penalty when a 429 carries no Retry-After
_cooldown_until = 0.0            # monotonic deadline; 0.0 => not cooling down


def reset_cooldown() -> None:
    """Clear the rate-limit cooldown (used by tests; harmless in production)."""
    global _cooldown_until
    _cooldown_until = 0.0

# Generous field-name aliases: Bonfire's JSON keys drift between UI releases and
# between the "section data" and detail payloads, so we try several spellings
# for each logical field rather than coupling to one release.
_ID_KEYS = ("ProjectID", "ProjectId", "projectId", "OpportunityId", "opportunityId", "Id", "id")
_TITLE_KEYS = ("ProjectName", "projectName", "ProjectTitle", "projectTitle", "Title", "title", "Name", "name")
_REF_KEYS = ("ReferenceID", "ReferenceId", "referenceId", "ProjectRef", "ProjectCode", "SolicitationNumber")
_CLOSE_KEYS = ("DateClose", "CloseDate", "closeDate", "ClosingDate", "closingDate")
_OPEN_KEYS = ("DateOpen", "OpenDate", "openDate", "PublishDate", "publishDate")
_TYPE_KEYS = ("ProjectType", "projectType", "SolicitationType", "solicitationType", "Type", "type")
_DESC_KEYS = ("Description", "description", "ProjectDescription", "projectDescription", "Summary", "summary")


def scrape_bonfire_portal(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Pull open opportunities from one Bonfire portal. Same contract as
    `bid_scraper.scrape_bid_board`: returns (new_matches, skipped)."""
    slug = str(source.get("slug") or "").strip()
    portal_url = source.get("url") or (f"https://{slug}.bonfirehub.com/portal/?tab=openOpportunities" if slug else "")
    if not slug:
        return [], [{"url": portal_url, "reason": "bonfire_misconfigured",
                     "notes": "a bonfire source needs a `slug` (e.g. slug: stlcc)"}]

    endpoint = f"https://{slug}.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData"
    payload, skip = _fetch_payload(endpoint, portal_url, session, source)
    if skip is not None:
        return [], [skip]

    new_matches: list[dict] = []
    for record in find_opportunity_records(payload):
        match = _record_to_match(record, slug, portal_url, matchers)
        if match is None:
            continue   # no SEAtS category matched - skip (same rule as bid_scraper)
        h = utils.item_hash(slug, match["_record_id"], match["title"])
        if h in seen:
            continue
        seen.add(h)
        match.pop("_record_id", None)
        new_matches.append(match)

    return new_matches, []


def _fetch_payload(endpoint: str, portal_url: str, session, source: dict) -> tuple[object, dict | None]:
    """GET a portal's open-opportunities JSON. Returns (payload, None) on
    success or (None, skip). Fetches via the session directly (not utils.fetch)
    so it can see the status code + Retry-After and apply the shared-cooldown
    back-off on HTTP 429. Keeps utils.fetch's polite trailing delay."""
    global _cooldown_until
    remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        return None, {"url": portal_url, "reason": "rate_limited_cooldown",
                      "notes": f"skipped - Bonfire is rate-limiting this run "
                               f"(~{int(remaining)}s cooldown left; retries next run)"}

    sess = session or utils.get_session()
    try:
        resp = sess.get(
            endpoint, timeout=utils.DEFAULT_TIMEOUT,
            params={"_": int(time.time() * 1000)},   # cache-buster, like the portal's own XHR
            headers={"Accept": "application/json, text/javascript, */*; q=0.01",
                     "Referer": portal_url, "X-Requested-With": "XMLHttpRequest"},
        )
    except requests.RequestException as exc:
        log.warning("  [bonfire fetch error] %s -> %s", portal_url, exc)
        return None, {"url": portal_url, "reason": "fetch_failed", "notes": source.get("notes", "")}
    finally:
        time.sleep(utils.POLITE_DELAY_SECONDS)   # global politeness (0 during tests)

    status = getattr(resp, "status_code", 200)
    if status == 429:
        wait = _retry_after_seconds(resp) or _COOLDOWN_SECONDS
        _cooldown_until = time.monotonic() + wait
        log.warning("  [bonfire] HTTP 429 from %s - cooling down %ds "
                    "(shared bonfirehub rate limit; rest of this run skips)", portal_url, wait)
        return None, {"url": portal_url, "reason": "rate_limited",
                      "notes": f"HTTP 429; backing off {wait}s for the rest of this run"}
    if status >= 400:
        return None, {"url": portal_url, "reason": f"http_{status}", "notes": source.get("notes", "")}

    try:
        # Bonfire sometimes prefixes a UTF-8 BOM; decode with -sig to tolerate it.
        return json.loads(resp.content.decode("utf-8-sig")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {"url": portal_url, "reason": "bad_json",
                      "notes": "public endpoint returned unreadable JSON"}


def _retry_after_seconds(resp) -> int | None:
    """Parse a numeric Retry-After header (seconds) if present, clamped sane."""
    raw = (getattr(resp, "headers", {}) or {}).get("Retry-After")
    try:
        return max(1, min(3600, int(str(raw).strip()))) if raw else None
    except (TypeError, ValueError):
        return None


def _record_to_match(record: dict, slug: str, portal_url: str, matchers) -> dict | None:
    """Turn one Bonfire opportunity record into a bid match dict (the shape the
    pipeline's bid branch inserts), or None if it matches no SEAtS category."""
    title = str(_first(record, _TITLE_KEYS) or "Untitled opportunity").strip()
    reference = str(_first(record, _REF_KEYS) or "").strip()
    notice_type = str(_first(record, _TYPE_KEYS) or _infer_type(f"{reference} {title}")).strip()
    description = str(_first(record, _DESC_KEYS) or "").strip()
    due = _date_only(str(_first(record, _CLOSE_KEYS) or "").strip())
    posted = _date_only(str(_first(record, _OPEN_KEYS) or "").strip())

    record_id = str(_first(record, _ID_KEYS) or "").strip()
    if not record_id:
        record_id = utils.item_hash(reference, title, due)   # stable synthetic id

    # Filter to SEAtS relevance the same way bid_scraper does.
    blob = " ".join([title, reference, notice_type, description])
    categories = utils.match_categories(blob, matchers)
    if not categories:
        return None

    detail_url = f"https://{slug}.bonfirehub.com/opportunities/{record_id}" if record_id else portal_url
    text_parts = [
        f"[{notice_type}]" if notice_type else "",
        f"Ref {reference}" if reference else "",
        f"Closes {due}" if due else "",
        description,
    ]
    return {
        "_record_id": record_id,
        "source_url": portal_url,
        "title": title,
        "description": " | ".join(p for p in text_parts if p),
        "detail_url": detail_url,
        "date": posted or due,   # posted if the portal supplies it, else the close date
        "categories": categories,
    }


def find_opportunity_records(payload: object) -> list[dict]:
    """Find the opportunity collection without coupling to one Bonfire UI
    release. Walks the whole payload, collecting every dict that has both an id
    and a title; prefers the largest qualifying *list*, else the loose dicts
    (Bonfire's current shape is payload.projects = {id: {..}, ..}). Dedups by id.
    """
    candidates: list[list[dict]] = []
    singles: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            qualifying = [item for item in value if _has_identity_and_title(item)]
            if qualifying:
                candidates.append(qualifying)
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if _has_identity_and_title(value):
                singles.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
            try:
                visit(json.loads(value))
            except json.JSONDecodeError:
                pass

    visit(payload)
    records = max(candidates, key=len) if candidates else singles
    deduplicated: dict[str, dict] = {}
    for record in records:
        key = str(_first(record, _ID_KEYS) or id(record))
        deduplicated[key] = record
    return list(deduplicated.values())


def _has_identity_and_title(record: object) -> bool:
    return (
        isinstance(record, dict)
        and _first(record, _ID_KEYS) is not None
        and _first(record, _TITLE_KEYS) is not None
    )


def _first(record: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _infer_type(text: str) -> str:
    match = re.search(r"\b(RFP|RFI|RFQ|ITB|IFB|CSP)\b", text, re.IGNORECASE)
    return match.group(1).upper() if match else "solicitation"


def _date_only(value: str) -> str:
    """Best-effort YYYY-MM-DD from Bonfire's several date encodings: .NET
    `/Date(ms)/`, epoch seconds/ms, ISO, or 'YYYY-MM-DD HH:MM:SS'. Falls back to
    the leading date-looking token, else "" (never raises)."""
    if not value:
        return ""
    dotnet = re.search(r"/Date\((\d+)(?:[+-]\d+)?\)/", value)
    if dotnet:
        return datetime.fromtimestamp(int(dotnet.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    if value.isdigit():
        stamp = int(value)
        if stamp > 10_000_000_000:   # milliseconds
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    candidate = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return m.group(0) if m else ""
