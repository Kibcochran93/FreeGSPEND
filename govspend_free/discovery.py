"""
Portal discovery - the repeatable way to widen Bonfire / Ion Wave coverage.

Two steps:
  1. ENUMERATE tenants nationwide via the keyless Common Crawl index
     (`*.bonfirehub.com/portal/*`, `*.ionwave.net/SourcingEvents.aspx*`).
  2. CLASSIFY each candidate by fetching it directly: is it live, is it
     higher-ed (vs a K-12 district or a city/county), and which state? - so a
     human can verify + wire the good ones into config/sources.yaml.

Run: `python main.py --discover ionwave` (or `bonfire`) -> writes a candidate CSV
to reports/discovered_<family>_<ts>.csv.

Reality check (why this is "best-effort"): Common Crawl's index endpoint 504s on
broad wildcard queries under load, so enumeration may come back thin - pass your
own slug list via a `seed` file if so. Classification (a direct fetch per slug)
is the reliable part. Bonfire also rate-limits by IP (HTTP 429), so a big
classify run gets throttled; Ion Wave is friendlier. Higher-ed presence on both
platforms is genuinely sparse - most tenants are K-12 districts or local govs.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

from . import utils
from .coverage import US_STATES, state_key
from .utils import log

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"

_HE_RE = re.compile(r"\b(college|university|universities|institute|polytechnic|seminary)\b", re.I)
_K12_RE = re.compile(r"\b(isd|independent school|school district|public schools|"
                     r"unified school|county schools|charter)\b", re.I)
# Longest state names first so "West Virginia" wins over "Virginia".
_STATE_NAMES = sorted(((name, state_key(name)) for _, name in US_STATES),
                      key=lambda p: -len(p[0]))

_FAMILIES = {
    "bonfire": {"suffix": ".bonfirehub.com", "cc_url": "*.bonfirehub.com/portal/*",
                "drop": {"www", "vendor"}},
    "ionwave": {"suffix": ".ionwave.net", "cc_url": "*.ionwave.net/SourcingEvents.aspx*",
                "drop": {"www", "support", "vendor"}},
}


def state_from_name(name: str) -> str:
    """A sources.yaml state key if a US state name appears in the institution
    name (e.g. 'Iowa State University' -> 'iowa'), else '' (needs manual lookup)."""
    low = (name or "").lower()
    for full, key in _STATE_NAMES:
        if full.lower() in low:
            return key
    return ""


def _slug_from_host(host: str, family: str) -> str | None:
    fam = _FAMILIES[family]
    host = (host or "").lower()
    if not host.endswith(fam["suffix"]):
        return None
    slug = host[: -len(fam["suffix"])]
    if not slug or "." in slug or slug in fam["drop"]:
        return None
    return slug


def enumerate_hosts(family: str, session, crawls: int = 2) -> tuple[list[str], str]:
    """Best-effort Common Crawl enumeration of tenant slugs. Returns (slugs, note);
    an empty list with a note when Common Crawl is unavailable (504s are common)."""
    fam = _FAMILIES[family]
    resp = utils.fetch(CC_COLLINFO, session=session)
    if resp is None:
        return [], "could not reach the Common Crawl index"
    try:
        collections = resp.json()
    except ValueError:
        return [], "Common Crawl index returned unreadable JSON"
    slugs: set[str] = set()
    errors = 0
    for col in collections[:crawls]:
        endpoint = (f"https://index.commoncrawl.org/{col['id']}-index"
                    f"?url={fam['cc_url']}&output=json&fl=url&filter=status:200&collapse=urlkey")
        r = utils.fetch(endpoint, session=session, timeout=60)
        if r is None:
            errors += 1
            continue
        for line in r.text.splitlines():
            if not line.strip():
                continue
            try:
                host = urlparse(json.loads(line)["url"]).hostname
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            s = _slug_from_host(host or "", family)
            if s:
                slugs.add(s)
    note = "" if slugs else f"Common Crawl returned nothing ({errors} crawl error(s); its index often 504s)"
    return sorted(slugs), note


def classify_ionwave(slug: str, session) -> dict:
    """Fetch an Ion Wave tenant's listing and classify it."""
    from . import ionwave
    resp = utils.fetch(f"https://{slug}.ionwave.net/SourcingEvents.aspx?SourceType=1",
                       session=session, headers={"Accept": "text/html"})
    if resp is None:
        return {"slug": slug, "live": False, "segment": "dead", "open": 0, "name": "", "state": ""}
    title, listings = ionwave.parse_listing(resp.text)
    return {"slug": slug, "live": True, "segment": _segment(title), "open": len(listings),
            "name": title, "state": state_from_name(title)}


def classify_bonfire(slug: str, session) -> dict:
    """Live-check a Bonfire tenant via its public JSON endpoint. (The endpoint
    carries no institution name, so segment/state can't be inferred here - that's
    why Bonfire relies on a separate hand-classification.)"""
    from . import bonfire
    matches, skipped = bonfire.scrape_bonfire_portal(
        {"type": "bonfire", "slug": slug}, session, set(),
        utils.build_category_matchers({}))   # empty matchers: we only want liveness
    reason = skipped[0]["reason"] if skipped else ""
    live = not (reason in ("fetch_failed", "bad_json", "bonfire_misconfigured") or reason.startswith("http_"))
    return {"slug": slug, "live": live, "segment": "unknown", "open": "",
            "name": "", "state": "", "note": reason}


def _segment(name: str) -> str:
    if _K12_RE.search(name or ""):
        return "k12"
    if _HE_RE.search(name or ""):
        return "higher_ed"
    return "other"


def run(family: str, session=None, seed_slugs: list[str] | None = None,
        limit: int = 300, delay: float = 0.0) -> list[dict]:
    """Enumerate (Common Crawl) + classify. `seed_slugs` supplements/replaces the
    Common Crawl result (useful when CC 504s). Returns classified rows.

    Politeness note: classification fetches through `utils.fetch`, which already
    sleeps ~1.5s between requests, so a few-hundred-tenant run takes several
    minutes. These platforms still rate-limit bursts by IP - if many come back
    as fetch failures, they're throttled, not dead; re-run a smaller batch."""
    if family not in _FAMILIES:
        raise ValueError(f"unknown family {family!r}; use one of {sorted(_FAMILIES)}")
    session = session or utils.get_session()
    slugs, note = enumerate_hosts(family, session)
    if note:
        log.warning("  [discover] %s", note)
    slugs = sorted(set(slugs) | set(seed_slugs or []))[:limit]
    log.info("  [discover] classifying %d %s tenant(s)...", len(slugs), family)
    classify = classify_ionwave if family == "ionwave" else classify_bonfire
    rows = []
    for i, slug in enumerate(slugs):
        rows.append(classify(slug, session))
        if delay:
            time.sleep(delay)
    return rows


def write_candidates_csv(rows: list[dict], path) -> None:
    import csv
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["slug", "live", "segment", "state", "open", "name", "note"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        # Actionable first: higher-ed, then rows with a known state, then slug.
        for r in sorted(rows, key=lambda r: (r.get("segment") != "higher_ed",
                                             r.get("state") or "zzzz", r["slug"])):
            w.writerow(r)
