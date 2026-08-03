"""
PlanetBids adapter (source `type: planetbids`).

PlanetBids vendor portals (`vendors.planetbids.com/portal/<id>/bo/bo-search`) are
Ember JS single-page apps: the bid list loads from a header-guarded API, so a
plain fetch gets an empty shell. This RENDERS the portal in a headless browser
(render.fetch_rendered -> Scrapling) and parses the resulting bid rows. Heavy
California community-college presence (SEAtS ICP: LACCD, State Center, Foothill-
De Anza, Yosemite, ...). Public data only - the page's own app loads it.

Rendering is opt-in and gated on `--browser` (utils.USE_BROWSER) since launching
a browser is slow; without it the source is skipped as needs_browser. Parsing
(parse_planetbids) is pure BeautifulSoup, so it's unit-tested offline against a
saved fixture with no browser or Scrapling install.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from . import render, utils

_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def scrape_planetbids_portal(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Render one PlanetBids portal and keep the SEAtS-relevant open bids. Same
    contract as bid_scraper.scrape_bid_board: returns (new_matches, skipped)."""
    url = str(source.get("url") or "").strip()
    if not url:
        return [], [{"url": "", "reason": "planetbids_misconfigured",
                     "notes": "a planetbids source needs a bo-search url"}]
    if not utils.USE_BROWSER:
        return [], [{"url": url, "reason": "needs_browser",
                     "notes": "PlanetBids is a JS SPA - run with --browser (needs scrapling[fetchers])"}]

    html = render.fetch_rendered(url, network_idle=True)
    if html is None:
        return [], [{"url": url, "reason": "render_unavailable",
                     "notes": 'render failed or scrapling not installed (pip install "scrapling[fetchers]")'}]

    events = parse_planetbids(html)
    if not events:
        return [], [{"url": url, "reason": "no_bids_parsed",
                     "notes": "portal rendered but no bid rows were found"}]

    new_matches: list[dict] = []
    for ev in events:
        blob = " ".join(p for p in (ev["title"], ev["number"], ev["stage"]) if p)
        categories = utils.match_categories(blob, matchers)
        if not categories:
            continue     # keep only SEAtS-relevant bids (same rule as every bid source)
        h = utils.item_hash("planetbids", url, ev["number"] or ev["title"])
        if h in seen:
            continue
        seen.add(h)
        desc = " | ".join(p for p in (
            f"[{ev['stage']}]" if ev["stage"] else "",
            f"No. {ev['number']}" if ev["number"] else "",
            f"Closes {ev['close']}" if ev["close"] else "",
        ) if p)
        new_matches.append({
            "source_url": url,
            "title": ev["title"],
            "description": desc,
            "detail_url": url,          # rows are Ember-routed; no per-bid href in the DOM
            "date": ev["posted"] or ev["close"],
            "categories": categories,
        })
    return new_matches, []


def parse_planetbids(html: str) -> list[dict]:
    """Parse a rendered PlanetBids bo-search page into bid rows. Pure (no browser),
    so it's unit-testable offline. Each `.row-highlight` carries `.title`,
    `.invitationNum`, `.stageStr`, and date cells."""
    soup = BeautifulSoup(html or "", "html.parser")
    events: list[dict] = []
    for row in soup.select(".row-highlight"):
        title = _text(row.select_one(".title"))
        if not title:
            continue
        dates = [_iso(m) for m in _DATE_RE.findall(" ".join(row.stripped_strings))]
        events.append({
            "title": title,
            "number": _text(row.select_one(".invitationNum")),
            "stage": _text(row.select_one(".stageStr")),
            "posted": dates[0] if dates else "",
            "close": dates[-1] if len(dates) > 1 else "",
        })
    return events


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _iso(match: tuple[str, str, str]) -> str:
    mm, dd, yyyy = match
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
