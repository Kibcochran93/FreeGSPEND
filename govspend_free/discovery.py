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
# Wayback Machine's CDX index - a second keyless URL index. More reliable than
# Common Crawl's endpoint (which frequently 504s / is unreachable), so it's the
# primary enumeration source for the path families.
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"

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

# PATH-based platform families: the tenant id lives in the URL PATH (a numeric
# portal id for PlanetBids, a text slug for OpenGov), not the subdomain. These
# are JS SPAs, so classification RENDERS each portal (render.browser_session) to
# read its institution name off the page - slower, but the only way to get the
# name. Bulk-discovery pays off because enumeration finds hundreds at once.
_PATH_FAMILIES = {
    "planetbids": {
        "cc_url": "vendors.planetbids.com/portal/*",
        "wb_url": "vendors.planetbids.com/portal*",
        "id_re": re.compile(r"/portal/(\d+)"),
        "url": lambda pid: f"https://vendors.planetbids.com/portal/{pid}/bo/bo-search",
        "type": "planetbids",
        "stealth": False,
    },
    "opengov": {
        "cc_url": "procurement.opengov.com/portal/*",
        "wb_url": "procurement.opengov.com/portal*",
        "id_re": re.compile(r"/portal/([a-z0-9][a-z0-9_-]{1,40})"),
        "url": lambda slug: f"https://procurement.opengov.com/portal/{slug}",
        "type": "opengov",
        "stealth": True,   # Cloudflare-walled -> needs the stealth fetcher
    },
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
    if family in _PATH_FAMILIES:
        return run_path_family(family, session, seed_slugs, limit)
    if family not in _FAMILIES:
        raise ValueError(f"unknown family {family!r}; use one of "
                         f"{sorted(list(_FAMILIES) + list(_PATH_FAMILIES))}")
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


def enumerate_path_ids(family: str, session, cc_crawls: int = 1,
                       wb_limit: int = 40000) -> tuple[list[str], str]:
    """Enumerate path-based tenant ids/slugs (`.../portal/<id>`) from the Wayback
    Machine CDX index (primary - reliable, keyless) plus Common Crawl (best-effort
    supplement; its endpoint frequently 504s / is unreachable). Returns (ids, note)."""
    fam = _PATH_FAMILIES[family]
    ids: set[str] = set()

    # 1) Wayback Machine CDX - the reliable source.
    resp = utils.fetch(WAYBACK_CDX, session=session, timeout=90, params={
        "url": fam["wb_url"], "output": "json", "fl": "original",
        "collapse": "urlkey", "limit": wb_limit})
    if resp is not None:
        try:
            rows = resp.json()
        except ValueError:
            rows = []
        for row in rows:
            url = row[0] if isinstance(row, list) and row else ""
            if not url or url == "original":
                continue    # header row / empty
            m = fam["id_re"].search(url)
            if m:
                ids.add(m.group(1))

    # 2) Common Crawl - supplement (union), tolerated when unreachable.
    cc = utils.fetch(CC_COLLINFO, session=session)
    if cc is not None:
        try:
            collections = cc.json()
        except ValueError:
            collections = []
        for col in collections[:cc_crawls]:
            r = utils.fetch(f"https://index.commoncrawl.org/{col['id']}-index"
                            f"?url={fam['cc_url']}&output=json&fl=url&collapse=urlkey",
                            session=session, timeout=90)
            if r is None:
                continue
            for line in r.text.splitlines():
                if not line.strip():
                    continue
                try:
                    url = json.loads(line)["url"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                m = fam["id_re"].search(url)
                if m:
                    ids.add(m.group(1))

    note = "" if ids else "no portals found (Wayback CDX + Common Crawl both empty/unreachable)"
    return sorted(ids), note


def _rendered_name(html: str, family: str) -> str:
    """Institution name read off a rendered portal page."""
    from bs4 import BeautifulSoup
    text = re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()
    if family == "planetbids":
        m = re.search(r"Skip to main content (.+?) (?:Help Center|Home LOG IN|Bid Opportunities)", text)
        return m.group(1).strip() if m else ""
    # opengov / generic: the <title>, minus any " | OpenGov"-style suffix.
    t = re.search(r"<title>([^<]+)</title>", html or "", re.I)
    return re.sub(r"\s*[|–\-].*$", "", (t.group(1).strip() if t else "")).strip()


def classify_rendered(family: str, pid: str, fetch) -> dict:
    """Render one path-family portal (via a browser_session `fetch`) and classify
    it: live?, higher-ed vs K-12 vs other, state (best-effort from name), open count."""
    fam = _PATH_FAMILIES[family]
    html = fetch(fam["url"](pid))
    if not html:
        return {"slug": pid, "live": False, "segment": "dead", "open": 0, "name": "", "state": ""}
    name = _rendered_name(html, family)
    open_ct: object = ""
    if family == "planetbids":
        from . import planetbids
        open_ct = len(planetbids.parse_planetbids(html))
    return {"slug": pid, "live": True, "segment": _segment(name), "open": open_ct,
            "name": name, "state": state_from_name(name)}


def run_path_family(family: str, session=None, seed_ids: list[str] | None = None,
                    limit: int = 200, chunk: int = 10, delay: float = 0.3,
                    chunk_cooldown: float = 45.0) -> list[dict]:
    """Enumerate (Wayback/CC) + render-classify a path-based family.

    Renders in CHUNKS with a COOLDOWN between them. PlanetBids (and similar) rate-
    limit by IP: after ~11-13 rapid page loads they start returning empty shells,
    and a fresh browser alone doesn't reset it (the limit is per-IP/time-window).
    So each chunk gets a fresh browser AND we sleep `chunk_cooldown`s between
    chunks to let the window reset - the price of a complete, un-throttled sweep.
    A full sweep is therefore slow (tens of minutes); run it in the background."""
    from . import render
    fam = _PATH_FAMILIES[family]
    session = session or utils.get_session()
    ids, note = enumerate_path_ids(family, session)
    if note:
        log.warning("  [discover] %s", note)
    ids = sorted(set(ids) | set(seed_ids or []), key=lambda x: (len(x), x))[:limit]
    if not render.scrapling_available():
        log.warning('  [discover] %s discovery needs the render layer - '
                    'pip install "scrapling[fetchers]"', family)
        return []
    log.info("  [discover] rendering + classifying %d %s portal(s) in chunks of %d - slow...",
             len(ids), family, chunk)
    rows: list[dict] = []
    step = max(1, chunk)
    for start in range(0, len(ids), step):
        batch = ids[start:start + step]
        with render.browser_session(stealth=fam["stealth"]) as fetch:  # fresh browser per chunk
            for pid in batch:
                rows.append(classify_rendered(family, pid, fetch))
                if delay:
                    time.sleep(delay)
        done = min(start + step, len(ids))
        log.info("  [discover] ...%d/%d classified", done, len(ids))
        if chunk_cooldown and done < len(ids):
            time.sleep(chunk_cooldown)   # let the per-IP rate-limit window reset
    return rows


def to_sources_entries(rows: list[dict], family: str) -> str:
    """Ready-to-paste sources.yaml `university_systems` entries for the LIVE,
    higher-ed candidates. State is best-effort from the name - verify before use."""
    fam = _PATH_FAMILIES.get(family, {})
    url_fn = fam.get("url", lambda s: s)
    typ = fam.get("type", family)
    lines: list[str] = []
    for r in sorted(rows, key=lambda r: (r.get("state") or "zz", r.get("name") or "")):
        if r.get("segment") != "higher_ed" or not r.get("live"):
            continue
        lines.append(f'    - name: "{r["name"]}"')
        lines.append(f'      bid_boards: [{{type: {typ}, url: "{url_fn(r["slug"])}"}}]'
                     f'  # state={r.get("state") or "?"} open={r.get("open")}')
    return "\n".join(lines)


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
