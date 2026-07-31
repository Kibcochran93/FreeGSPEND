"""
Ion Wave RFP-portal adapter (source `type: ionwave` in a university system's
`bid_boards`). Ported from the SEAtS RFP-monitor into govspend_free conventions.

Ion Wave (*.ionwave.net) hosts a lot of public procurement. Its "Sourcing Events"
page is a server-rendered ASP.NET **RadGrid** - so, like Bonfire, no browser is
needed: a plain GET returns the bid table as HTML, which we parse with a stdlib
HTMLParser plus the hidden `_clientKeyValues` map that carries each row's BidID
(used to build a per-bid detail link). Same cracking recipe applies to other
ASP.NET/RadGrid procurement sites (a foothold on the PennWATCH-style pattern).

Like every other bid source, only rows matching a SEAtS bid category are kept, so
this feeds the same `documents` table (`doc_type='bid'`) -> Opportunities / Ops
play with no extra wiring. Public data only; no logins. One portal == one
`bid_boards` entry: `{type: ionwave, slug: <slug>}`.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from . import utils


@dataclass(frozen=True)
class IonWaveListing:
    bid_id: str
    number: str
    title: str
    bid_type: str
    issuer: str
    issue_date: str
    close_date: str


def scrape_ionwave_portal(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Pull open bids from one Ion Wave portal. Same contract as
    `bid_scraper.scrape_bid_board`: returns (new_matches, skipped)."""
    slug = str(source.get("slug") or "").strip()
    base = f"https://{slug}.ionwave.net"
    listing_url = source.get("url") or f"{base}/SourcingEvents.aspx?SourceType=1"
    if not slug:
        return [], [{"url": listing_url, "reason": "ionwave_misconfigured",
                     "notes": "an ionwave source needs a `slug` (e.g. slug: iastate)"}]

    resp = utils.fetch(listing_url, session=session, headers={"Accept": "text/html,application/xhtml+xml"})
    if resp is None:
        return [], [{"url": listing_url, "reason": "fetch_failed", "notes": source.get("notes", "")}]

    _, listings = parse_listing(resp.text)
    new_matches: list[dict] = []
    for lst in listings:
        blob = " ".join(p for p in (lst.title, lst.number, lst.bid_type) if p)
        categories = utils.match_categories(blob, matchers)
        if not categories:
            continue     # closed-world: keep only SEAtS-relevant bids (same as bid_scraper)
        rec_id = lst.bid_id or utils.item_hash(slug, lst.number, lst.title)
        h = utils.item_hash("ionwave", slug, rec_id)
        if h in seen:
            continue
        seen.add(h)
        detail_url = (f"{base}/PublicDetail.aspx?bidID={lst.bid_id}&SourceType=1"
                      if lst.bid_id else listing_url)
        desc = " | ".join(p for p in (
            f"[{lst.bid_type}]" if lst.bid_type else "",
            f"No. {lst.number}" if lst.number else "",
            f"Closes {lst.close_date}" if lst.close_date else "",
        ) if p)
        new_matches.append({
            "source_url": listing_url,
            "title": lst.title,
            "description": desc,
            "detail_url": detail_url,
            "date": _date_iso(lst.issue_date) or _date_iso(lst.close_date),
            "categories": categories,
        })
    return new_matches, []


# --------------------------------------------------------------------------
# HTML parsing: the RadGrid rows + the hidden BidID map.
# --------------------------------------------------------------------------

class _TableParser(HTMLParser):
    """Collect (row_class, [cell_text, ...]) for every <tr>, handling the nested
    tables RadGrid emits (a row stack keeps outer rows intact)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._row_class = ""
        self._row_cells: list[str] | None = None
        self._cell_depth = 0
        self._cell_parts: list[str] = []
        self._row_stack: list[tuple[str, list[str] | None, int, list[str]]] = []
        self.rows: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "tr":
            if self._row_cells is not None:
                self._row_stack.append((self._row_class, self._row_cells, self._cell_depth, self._cell_parts))
            self._row_class = attributes.get("class", "") or ""
            self._row_cells = []
            self._cell_depth = 0
            self._cell_parts = []
        elif tag == "td" and self._row_cells is not None:
            self._cell_depth += 1
            if self._cell_depth == 1:
                self._cell_parts = []
        elif tag == "br" and self._cell_depth:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "td" and self._row_cells is not None and self._cell_depth:
            if self._cell_depth == 1:
                self._row_cells.append(_clean(" ".join(self._cell_parts)))
                self._cell_parts = []
            self._cell_depth -= 1
        elif tag == "tr" and self._row_cells is not None:
            self.rows.append((self._row_class, self._row_cells))
            if self._row_stack:
                self._row_class, self._row_cells, self._cell_depth, self._cell_parts = self._row_stack.pop()
            else:
                self._row_cells, self._cell_depth, self._cell_parts = None, 0, []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._cell_depth:
            self._cell_parts.append(data)


def parse_listing(html: str) -> tuple[str, list[IonWaveListing]]:
    parser = _TableParser()
    parser.feed(html)
    bid_ids = _extract_client_bid_ids(html)
    listings: list[IonWaveListing] = []
    row_index = 0
    for row_class, cells in parser.rows:
        if "rgRow" not in row_class and "rgAltRow" not in row_class:
            continue
        if len(cells) < 6:
            continue
        # The public grid leads with a view-icon cell, then may include a hidden
        # work-group/issuer column before the two dates.
        data = cells[1:]
        if len(data) >= 6:
            number, title, bid_type, issuer, issue_date, close_date = data[:6]
        else:
            number, title, bid_type, issue_date, close_date = data[:5]
            issuer = ""
        if not number or not title:
            continue
        listings.append(IonWaveListing(
            bid_id=bid_ids.get(row_index, ""), number=number, title=title,
            bid_type=bid_type, issuer=issuer, issue_date=issue_date, close_date=close_date,
        ))
        row_index += 1
    return parser.title.strip(), listings


def _extract_client_bid_ids(html: str) -> dict[int, str]:
    """Row-index -> BidID, read from RadGrid's embedded `_clientKeyValues` JSON."""
    decoded = _html.unescape(html)
    start = decoded.find('"_clientKeyValues"')
    if start < 0:
        return {}
    end = decoded.find('"_controlToFocus"', start)
    section = decoded[start:end if end >= 0 else start + 10000]
    pairs = re.findall(r'"(\d+)"\s*:\s*\{\s*"BidID"\s*:\s*"?(\d+)"?', section)
    return {int(index): bid_id for index, bid_id in pairs}


def _date_iso(value: str) -> str:
    """'YYYY-MM-DD' from an Ion Wave date cell (e.g. '3/14/2026 2:00 PM (CT)'), or ''."""
    d = utils.parse_date(value)
    return d.isoformat() if d else ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
