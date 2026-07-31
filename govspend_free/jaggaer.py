"""
JAGGAER / SciQuest public-events adapter (source `type: jaggaer` in a university
system's `bid_boards`). Ported from the SEAtS RFP-monitor.

Nuance worth knowing: FreeGSPEND marks most Jaggaer bid boards `js_rendered`
because the per-university entry points (e.g. CustomerOrg=UGA) are a SPA that
returns an empty shell. BUT the **statewide** SciQuest marketplaces serve a real
server-rendered public event table -
    https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=<Org>
- so those parse with a plain request (no browser). Each row is an `<a>` with
class `btn-link-header` (the title/detail link) plus Open/Close/Type/Number
fields. We keep only rows matching a SEAtS bid category and feed them into the
`documents` table (`doc_type='bid'`) like every other bid source. Public data
only; no logins. One marketplace == one `bid_boards` entry:
`{type: jaggaer, url: "...PublicEvent?CustomerOrg=..."}`.
"""

from __future__ import annotations

import html as _html
import re

from . import utils

_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_TITLE_RE = re.compile(r'<a\b[^>]*class="[^"]*btn-link-header[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_DESC_RE = re.compile(r'<div\b[^>]*class="[^"]*label-mini[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_FIELD_RE = re.compile(
    r'data-row-name[^>]*>.*?<div\b[^>]*>(Open|Close|Type|Number|Contact)</div>.*?'
    r'data-row-content[^>]*>(.*?)</div>', re.I | re.S)


def scrape_jaggaer_source(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Pull open events from one SciQuest public marketplace. Same contract as
    `bid_scraper.scrape_bid_board`: returns (new_matches, skipped)."""
    url = str(source.get("url") or "").strip()
    if not url:
        return [], [{"url": "", "reason": "jaggaer_misconfigured",
                     "notes": "a jaggaer source needs a public-event `url`"}]

    resp = utils.fetch(url, session=session, headers={"Accept": "text/html,application/xhtml+xml"})
    if resp is None:
        return [], [{"url": url, "reason": "fetch_failed", "notes": source.get("notes", "")}]

    events = parse_public_events(resp.text, url)
    if not events:
        # Loaded but nothing parseable - this CustomerOrg is likely the JS shell.
        return [], [{"url": url, "reason": "no_events_parsed",
                     "notes": "no public event rows found (this org may be JS-rendered)"}]

    new_matches: list[dict] = []
    for ev in events:
        blob = " ".join(p for p in (ev["title"], ev["number"], ev["notice_type"], ev["description"]) if p)
        categories = utils.match_categories(blob, matchers)
        if not categories:
            continue     # keep only SEAtS-relevant bids (same rule as bid_scraper)
        h = utils.item_hash("jaggaer", url, ev["number"] or ev["title"])
        if h in seen:
            continue
        seen.add(h)
        desc = " | ".join(p for p in (
            f"[{ev['notice_type']}]" if ev["notice_type"] else "",
            f"No. {ev['number']}" if ev["number"] else "",
            f"Closes {ev['close']}" if ev["close"] else "",
            ev["description"],
        ) if p)
        new_matches.append({
            "source_url": url,
            "title": ev["title"],
            "description": desc,
            "detail_url": ev["detail_url"] or url,
            "date": _date_iso(ev["open"]) or _date_iso(ev["close"]),
            "categories": categories,
        })
    return new_matches, []


def parse_public_events(page: str, base_url: str) -> list[dict]:
    """Extract event rows from a SciQuest PublicEvent page: every <tr> that
    carries a `btn-link-header` title link, with its Open/Close/Type/Number."""
    events: list[dict] = []
    for row in _ROW_RE.findall(page):
        tm = _TITLE_RE.search(row)
        if not tm:
            continue
        href = _html.unescape(tm.group(1))
        title = _clean(tm.group(2))
        if not title:
            continue
        dm = _DESC_RE.search(row)
        description = _clean(dm.group(1)) if dm else ""
        fields = {label.lower(): _clean(value) for label, value in _FIELD_RE.findall(row)}
        events.append({
            "title": title,
            "detail_url": utils.absolute_url(base_url, href),
            "number": fields.get("number", ""),
            "notice_type": fields.get("type", ""),
            "open": fields.get("open", ""),
            "close": fields.get("close", ""),
            "description": description,
        })
    return events


# Trailing timezone abbreviations (MDT, (CT), EST, ...) make dateutil warn; strip
# them before parsing. Deliberately does NOT match AM/PM.
_TZ_RE = re.compile(r'\s*\(?\b(?:[ECMP][DS]T|A[KS]?[DS]T|HS?T|[ECMP]T|UTC|GMT)\b\)?\s*$', re.I)


def _date_iso(value: str) -> str:
    d = utils.parse_date(_TZ_RE.sub('', value or ''))
    return d.isoformat() if d else ""


def _clean(value: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())
