"""
Scrapes board of trustees/regents meeting minutes pages: finds PDF links on
a listing page, downloads any we haven't seen before, extracts text, and
keyword-searches that text against both the bid categories and the free-text
watchlist (config/keywords.yaml -> watchlist).

This is the most reliable category across states per the pilot research -
plain HTML/PDF, no JS needed, for 9 of the 10 pilot states.
"""

from __future__ import annotations

from . import utils


def scrape_board_minutes(source: dict, session, seen: set[str], matchers, watchlist_patterns) -> tuple[list[dict], list[dict]]:
    url = source["url"]
    matches: list[dict] = []

    if "{YEAR}" in url or "{Month}" in url or "{DD}" in url or "{YYYY}" in url:
        # This is a URL *pattern*, not a listing page (see Illinois in
        # sources.yaml). Skip auto-discovery; you have to feed in real
        # dates yourself. See README for how.
        return matches, [{
            "url": url,
            "reason": "url_pattern_not_listing_page",
            "notes": "Fill in a real date and fetch directly, see README.",
        }]

    soup, skip = utils.fetch_page_or_skip(
        source, session,
        empty_shell_notes="Listing page looks JS-rendered even though config says otherwise.",
    )
    if skip is not None:
        return matches, [skip]

    pdf_links = utils.find_pdf_links(soup, url)
    if not pdf_links:
        return matches, [{"url": url, "reason": "no_pdf_links_found", "notes": ""}]

    for link in pdf_links:
        pdf_hash = utils.item_hash(link["url"])
        if pdf_hash in seen:
            continue
        seen.add(pdf_hash)

        pdf_path = utils.download_pdf(link["url"], session)
        if pdf_path is None:
            continue

        text = utils.extract_pdf_text(pdf_path)
        if not text:
            continue

        categories = utils.match_categories(text, matchers)
        watchlist_hits = utils.match_watchlist(text, watchlist_patterns)

        if not categories and not watchlist_hits:
            continue

        snippets = {}
        for pattern in watchlist_patterns:
            snip = utils.snippet_around(text, pattern)
            if snip:
                snippets[pattern.pattern] = snip

        matches.append({
            "source_url": url,
            "document_title": link["text"] or link["url"],
            "document_url": link["url"],
            "categories": categories,
            "watchlist_hits": watchlist_hits,
            "snippets": snippets,
            "full_text": text,
        })

    return matches, []
