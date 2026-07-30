"""
Best-effort scraper for state financial transparency / "open checkbook"
portals. These are the least consistent category across states - roughly
half of the 10-state pilot are AJAX/Tableau-driven and get skipped here
(see sources.yaml notes per state).

Strategy for the scrapable ones (CA FI$Cal, Georgia open.ga.gov, NC bulk
download): find direct-download CSV or PDF links on the page, pull them,
and search rows/text for your watchlist vendor names.

This intentionally does NOT try to be a general-purpose "search by vendor"
tool the way GovSpend or the state's own AJAX search would - that would
require reverse-engineering each site's private API or driving a real
browser. It's a lighter, dependency-free first pass: "did any of the
handful of downloadable files on this page mention my watchlist vendors."
"""

from __future__ import annotations

import csv
import hashlib
import io

from . import utils
from .utils import log

DATA_EXTENSIONS = (".csv", ".xlsx", ".xls", ".pdf")
# Format hints that qualify a "download"/"export" link that has no file
# extension of its own (e.g. /data/export?format=csv). Requiring one of
# these avoids grabbing every "/downloads/" nav or brochure link.
FORMAT_HINTS = ("csv", "excel", "xls", "spreadsheet", "data")
MAX_FILES_PER_SOURCE = 15  # cap per source per run, be polite


def _is_data_download(href_lower: str, text_lower: str) -> bool:
    path_part = href_lower.split("?", 1)[0]
    if path_part.endswith(DATA_EXTENSIONS):
        return True
    if ("download" in href_lower or "export" in href_lower) and any(
        fmt in href_lower or fmt in text_lower for fmt in FORMAT_HINTS
    ):
        return True
    return False


def scrape_transparency(source: dict, session, seen: set[str], watchlist_patterns) -> tuple[list[dict], list[dict]]:
    url = source["url"]
    matches: list[dict] = []
    skipped: list[dict] = []

    soup, skip = utils.fetch_page_or_skip(
        source, session,
        empty_shell_notes="Page looks JS-rendered even though config says otherwise.",
    )
    if skip is not None:
        return matches, [skip]

    download_links = []
    for a in soup.find_all("a", href=True):
        if _is_data_download(a["href"].lower(), a.get_text(" ", strip=True).lower()):
            download_links.append(utils.absolute_url(url, a["href"]))

    if not download_links:
        skipped.append({
            "url": url,
            "reason": "no_download_links_found",
            "notes": "No obvious CSV/XLS/PDF download links on this page - "
                     "may need manual review of the site's data catalog.",
        })
        return matches, skipped

    for link in download_links[:MAX_FILES_PER_SOURCE]:
        lower = link.lower().split("?", 1)[0]

        if lower.endswith(".pdf"):
            # PDFs at a stable URL don't change - URL dedup is fine here.
            pdf_key = utils.item_hash("transparency-pdf", link)
            if pdf_key in seen:
                continue
            path = utils.download_pdf(link, session)
            if not path:
                continue
            seen.add(pdf_key)
            text = utils.extract_pdf_text(path)
            hits = utils.match_watchlist(text, watchlist_patterns)
            if hits:
                matches.append({
                    "source_url": url,
                    "file_url": link,
                    "file_type": "pdf",
                    "watchlist_hits": hits,
                })
            continue

        if lower.endswith(".csv"):
            # Dedup on CONTENT, not URL: a checkbook CSV at a stable URL that
            # gets updated will hash differently and be re-scanned. URL dedup
            # would skip it forever after the first run.
            text, content_hash = utils.download_text_hashed(link, session)
            if text is None:
                continue
            csv_key = utils.item_hash("transparency-csv", link, content_hash)
            if csv_key in seen:
                continue
            seen.add(csv_key)
            try:
                for row in csv.reader(io.StringIO(text)):
                    row_text = " ".join(row)
                    hits = utils.match_watchlist(row_text, watchlist_patterns)
                    if hits:
                        matches.append({
                            "source_url": url,
                            "file_url": link,
                            "file_type": "csv",
                            "watchlist_hits": hits,
                            "row": row_text[:300],
                        })
            except csv.Error as exc:
                log.warning("  [csv parse error] %s -> %s", link, exc)
            continue

        if lower.endswith(".xlsx"):
            # e.g. Open Georgia's TER_Data_*.xlsx. Dedup on content like CSVs.
            data = utils.download_bytes(link, session)
            if data is None:
                continue
            xlsx_key = utils.item_hash("transparency-xlsx", link,
                                       hashlib.sha256(data).hexdigest())
            if xlsx_key in seen:
                continue
            seen.add(xlsx_key)
            for row in utils.iter_xlsx_rows(data):
                row_text = " ".join(row)
                hits = utils.match_watchlist(row_text, watchlist_patterns)
                if hits:
                    matches.append({
                        "source_url": url,
                        "file_url": link,
                        "file_type": "xlsx",
                        "watchlist_hits": hits,
                        "row": row_text[:300],
                    })
            continue

        # .xls (legacy binary) or anything else: not parsed here. Log so you
        # know it's there.
        skipped.append({
            "url": link,
            "reason": "unsupported_download_format",
            "notes": "Only .csv, .xlsx and .pdf are parsed (legacy .xls is not).",
        })

    return matches, skipped
