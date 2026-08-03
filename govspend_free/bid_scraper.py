"""
Scrapes university bid/RFP boards that are plain server-rendered HTML
(type: html_table or html_list in sources.yaml).

Sources marked js_rendered or form_post are intentionally SKIPPED here -
see README for how to extend this with Playwright if you want to cover
those (roughly half of the 10-state pilot: TX, FL, CA, GA university bid
boards all route through a JS single-page app).
"""

from __future__ import annotations

import re
from typing import Any

from . import utils

_DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def scrape_bid_board(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Returns (new_matches, skipped) for one bid_board source dict from sources.yaml.

    Dispatches on `type`: a `bonfire` source is a JSON-API portal handled by the
    bonfire adapter; everything else is scraped as server-rendered HTML here."""
    if source.get("type") == "bonfire":
        from . import bonfire
        return bonfire.scrape_bonfire_portal(source, session, seen, matchers)

    if source.get("type") == "ionwave":
        from . import ionwave
        return ionwave.scrape_ionwave_portal(source, session, seen, matchers)

    if source.get("type") == "jaggaer":
        from . import jaggaer
        return jaggaer.scrape_jaggaer_source(source, session, seen, matchers)

    if source.get("type") in ("rss", "atom", "feed", "json_feed"):
        from . import feeds
        return feeds.scrape_feed_source(source, session, seen, matchers)

    if source.get("type") == "planetbids":
        from . import planetbids
        return planetbids.scrape_planetbids_portal(source, session, seen, matchers)

    url = source["url"]
    new_matches: list[dict] = []

    soup, skip = utils.fetch_page_or_skip(
        source, session,
        empty_shell_notes="Page loaded but has almost no text - probably JS-rendered "
                          "even though sources.yaml says otherwise. Update the config.",
    )
    if skip is not None:
        return new_matches, [skip]

    rows = _extract_rows(soup, url)

    for row in rows:
        text_blob = " ".join([row["title"], row.get("description", "")])
        categories = utils.match_categories(text_blob, matchers)
        if not categories:
            continue

        h = utils.item_hash(url, row["title"], row.get("detail_url", ""))
        if h in seen:
            continue
        seen.add(h)

        new_matches.append({
            "source_url": url,
            "title": row["title"],
            "description": row.get("description", ""),
            "detail_url": row.get("detail_url", ""),
            "date": row.get("date", ""),
            "categories": categories,
        })

    return new_matches, []


def _extract_rows(soup, base_url: str) -> list[dict[str, Any]]:
    """Best-effort generic extraction of "bid-like" rows from a page.

    University bid boards vary a lot in markup, and critically, the
    "which column is the description" order is NOT consistent (some put
    Closing Date first, some put it last - see sources.yaml notes). So
    rather than assuming a fixed column order, this heuristic:
      1. Real <table> rows: uses the linked <a> tag's own text as the
         title when present (the RFP/IFB name is almost always the link
         text on real bid boards), falling back to the longest cell.
         Any cell matching a date-like pattern is used as `date`.
      2. Fallback for non-table pages: any <a> tag whose link text
         contains a bid-ish keyword (RFP, RFQ, IFB, Bid, Solicitation).
    """
    rows: list[dict[str, Any]] = []

    tables = soup.find_all("table")
    for table in tables:
        for tr in table.find_all("tr"):
            cell_tags = tr.find_all(["td", "th"])
            if not cell_tags:
                continue
            cell_texts = [c.get_text(" ", strip=True) for c in cell_tags]
            cell_texts = [c for c in cell_texts if c]
            if not cell_texts:
                continue

            link = tr.find("a", href=True)
            link_text = link.get_text(" ", strip=True) if link else ""
            title = link_text if link_text else max(cell_texts, key=len)

            date = next((c for c in cell_texts if _DATE_PATTERN.search(c)), "")
            other_cells = [c for c in cell_texts if c != title]

            rows.append({
                "title": title,
                "description": " | ".join(other_cells),
                "detail_url": utils.absolute_url(base_url, link["href"]) if link else "",
                "date": date,
            })

    if rows:
        return rows

    # Fallback for list-style (non-table) bid boards.
    bid_keywords = ("rfp", "rfq", "ifb", "bid", "solicitation", "rfi ")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text:
            continue
        if any(kw in text.lower() for kw in bid_keywords):
            rows.append({
                "title": text,
                "description": "",
                "detail_url": utils.absolute_url(base_url, a["href"]),
                "date": "",
            })

    return rows
