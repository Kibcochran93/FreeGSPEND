"""
Best-effort Co-Ops & Contracts equivalent. Reuses the SAME `transparency`
source list from sources.yaml (rather than requiring a separately-verified
set of "contracts" URLs) and looks specifically for CSV downloads whose
header row looks like contract data - a vendor column plus a start/end (or
"expiration") date column - as opposed to plain expenditure rows.

Where found, computes days_until_expiration relative to today and flags
anything inside CONTRACT_EXPIRATION_WINDOW_DAYS, mirroring GovSpend's
"identify upcoming expirations" pitch for this module.

Like transparency_scraper.py, this skips js_rendered/form_post sources -
several states' actual contracts data (e.g. Arkansas's own Contracts tab)
is AJAX-driven and not reachable this way. See sources.yaml notes.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re

from dateutil import parser as date_parser

from . import utils
from .utils import log

CONTRACT_EXPIRATION_WINDOW_DAYS = 180

VENDOR_HEADER_HINTS = ("vendor", "supplier", "contractor", "company")
START_HEADER_HINTS = ("start date", "start_date", "effective date", "begin date")
END_HEADER_HINTS = ("end date", "end_date", "expir", "termination date")
VALUE_HEADER_HINTS = ("contract value", "amount", "total value", "value")


def scrape_contracts(transparency_source: dict, session, seen: set[str]) -> tuple[list[dict], list[dict]]:
    url = transparency_source["url"]
    contracts: list[dict] = []

    soup, skip = utils.fetch_page_or_skip(transparency_source, session)
    if skip is not None:
        return contracts, [skip]

    csv_links = [
        utils.absolute_url(url, a["href"])
        for a in soup.find_all("a", href=True)
        if a["href"].lower().endswith(".csv")
    ]

    if not csv_links:
        return contracts, [{"url": url, "reason": "no_csv_links_found",
                            "notes": "Contracts scraper only reads CSV downloads."}]

    for link in csv_links[:10]:
        # Dedup on CONTENT, not URL, so an updated contracts CSV (new rows,
        # changed end dates) at a stable URL is re-read rather than skipped
        # forever after the first run.
        text, content_hash = utils.download_text_hashed(link, session)
        if text is None:
            continue
        key = utils.item_hash("contracts", link, content_hash)
        if key in seen:
            continue
        seen.add(key)

        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
        except csv.Error as exc:
            log.warning("  [contracts csv error] %s -> %s", link, exc)
            continue

        if header is None:
            continue

        col_idx = _match_columns(header)
        if col_idx["start"] is None or col_idx["end"] is None:
            continue  # not contract-shaped data, skip silently (expenditure CSV etc)

        for row in reader:
            try:
                vendor = row[col_idx["vendor"]] if col_idx["vendor"] is not None and col_idx["vendor"] < len(row) else ""
                start_raw = row[col_idx["start"]] if col_idx["start"] < len(row) else ""
                end_raw = row[col_idx["end"]] if col_idx["end"] < len(row) else ""
                value = row[col_idx["value"]] if col_idx["value"] is not None and col_idx["value"] < len(row) else ""
            except IndexError:
                continue

            days_left = _days_until(end_raw)

            contracts.append({
                "source_url": link,
                "vendor": vendor.strip(),
                "start_date": start_raw.strip(),
                "end_date": end_raw.strip(),
                "value": value.strip(),
                "days_until_expiration": days_left,
                "expiring_soon": days_left is not None and 0 <= days_left <= CONTRACT_EXPIRATION_WINDOW_DAYS,
            })

    return contracts, []


def _match_columns(header: list[str]) -> dict:
    header_lower = [h.strip().lower() for h in header]

    def find(hints):
        for i, h in enumerate(header_lower):
            if any(hint in h for hint in hints):
                return i
        return None

    return {
        "vendor": find(VENDOR_HEADER_HINTS),
        "start": find(START_HEADER_HINTS),
        "end": find(END_HEADER_HINTS),
        "value": find(VALUE_HEADER_HINTS),
    }


_DATE_LIKE = re.compile(r"\d")


def _days_until(date_str: str) -> int | None:
    if not date_str or not _DATE_LIKE.search(date_str):
        return None
    try:
        parsed = date_parser.parse(date_str, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return (parsed.date() - dt.date.today()).days
