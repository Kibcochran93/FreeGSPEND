"""
Scrape orchestration, extracted from main.py so the CLI and the desktop UI
run the exact same passes. `run_scrape()` walks the configured sources,
inserts new matches into the DB, writes the CSV report, and returns a
ScrapeResult the caller can summarize however it likes (terminal, GUI, email).

Progress is emitted through utils.log (the shared package logger), so any
caller that wants live progress just attaches a logging handler - the CLI
prints it, the desktop UI streams it into the window.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    bid_scraper,
    board_minutes_scraper,
    contacts,
    contracts_scraper,
    db,
    transparency_scraper,
    usaspending_scraper,
    utils,
)
from .utils import log


@dataclass
class ScrapeResult:
    bids: list = field(default_factory=list)
    minutes: list = field(default_factory=list)
    transparency: list = field(default_factory=list)
    federal: list = field(default_factory=list)
    contracts: list = field(default_factory=list)
    contacts: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    report_paths: list[Path] = field(default_factory=list)

    def counts(self) -> dict:
        """Flat, JSON-serializable summary for a UI or a console line."""
        return {
            "bids": len(self.bids),
            "minutes": len(self.minutes),
            "transparency": len(self.transparency),
            "federal": len(self.federal),
            "contracts": len(self.contracts),
            "contracts_expiring_soon": sum(1 for c in self.contracts if c.get("expiring_soon")),
            "contacts": len(self.contacts),
            "skipped": len(self.skipped),
            "reports": len(self.report_paths),
            "reports_dir": str(utils.REPORTS_DIR),
        }


@dataclass
class ScrapeCriteria:
    """Optional filters that scope a scrape run. All default to 'no filter'."""
    date_from: "dt.date | None" = None
    date_to: "dt.date | None" = None
    only_keywords: list[str] | None = None      # keep only docs whose text contains one of these
    only_competitors: list[str] | None = None    # keep only docs mentioning one of these names

    @classmethod
    def build(cls, *, date_from=None, date_to=None, only_keywords=None, only_competitors=None):
        """Construct from raw strings (dates as 'YYYY-MM-DD', keyword/competitor
        as lists or comma-strings). Empty/blank entries are dropped."""
        def _terms(v):
            if not v:
                return None
            items = v.split(",") if isinstance(v, str) else list(v)
            items = [t.strip() for t in items if t and t.strip()]
            return items or None
        return cls(
            date_from=utils.parse_date(date_from),
            date_to=utils.parse_date(date_to),
            only_keywords=_terms(only_keywords),
            only_competitors=_terms(only_competitors),
        )

    def active(self) -> bool:
        return any((self.date_from, self.date_to, self.only_keywords, self.only_competitors))

    def keep(self, blob: str, date_text: str | None) -> bool:
        """Does a matched document (its searchable text + a date string) pass?"""
        low = (blob or "").lower()
        if self.only_keywords and not any(k.lower() in low for k in self.only_keywords):
            return False
        if self.only_competitors and not any(c.lower() in low for c in self.only_competitors):
            return False
        if self.date_from or self.date_to:
            d = utils.parse_date(date_text)
            if d is not None:  # unparseable date -> keep (don't silently drop)
                if self.date_from and d < self.date_from:
                    return False
                if self.date_to and d > self.date_to:
                    return False
        return True


def run_scrape(
    conn,
    sources: dict,
    keywords_cfg: dict,
    *,
    selected_state: str | None = None,
    skip_bids: bool = False,
    skip_board_minutes: bool = False,
    skip_transparency: bool = False,
    skip_federal: bool = False,
    skip_contracts: bool = False,
    skip_contacts: bool = False,
    criteria: "ScrapeCriteria | None" = None,
    use_browser: bool = False,
    write_report: bool = True,
) -> ScrapeResult:
    """Run the configured scrape passes and persist results to `conn`.

    `selected_state` (already normalized to a lowercase key, or None for all)
    limits the run to one state. Raises ValueError if it isn't a known key.
    `criteria` optionally scopes the run by date range / keyword / competitor.
    `use_browser` renders js_rendered sources through headless Chromium.
    """
    criteria = criteria or ScrapeCriteria()
    utils.USE_BROWSER = bool(use_browser)
    if use_browser and not utils.browser_available():
        log.warning("  [browser] --browser requested but playwright isn't installed; "
                    "js_rendered sources will still be skipped. "
                    'Run: pip install -e ".[browser]" && playwright install chromium')
    matchers = utils.build_category_matchers(keywords_cfg.get("categories", {}))
    watchlist_patterns = utils.build_watchlist_matchers(keywords_cfg.get("watchlist", []))

    seen = utils.load_seen()
    session = utils.get_session()

    states_to_scan = [selected_state] if selected_state else list(sources.keys())
    unknown = [s for s in states_to_scan if s not in sources]
    if unknown:
        raise ValueError(f"Unknown state key(s): {unknown}. Valid keys: {list(sources.keys())}")

    result = ScrapeResult()

    for state_key in states_to_scan:
        state_cfg = sources[state_key]
        log.info("\n=== %s ===", state_key.upper())

        if not skip_bids:
            for system in state_cfg.get("university_systems", []):
                for board in system.get("bid_boards", []):
                    log.info("  [bids] %s -> %s", system["name"], board["url"])
                    new_matches, skipped = bid_scraper.scrape_bid_board(board, session, seen, matchers)
                    new_matches = [m for m in new_matches
                                   if criteria.keep(f"{m['title']} {m.get('description', '')}", m.get("date"))]
                    for m in new_matches:
                        m["state"], m["institution"] = state_key, system["name"]
                        db.insert_document(
                            conn, doc_type="bid", state=state_key, institution=system["name"],
                            title=m["title"], url=m.get("detail_url") or m["source_url"],
                            text=m.get("description", ""), date=m.get("date", ""),
                            categories=m["categories"], source="bids",
                        )
                    result.bids.extend(new_matches)
                    for s in skipped:
                        s.update(state=state_key, institution=system["name"], pass_type="bids")
                    result.skipped.extend(skipped)

        if not skip_board_minutes:
            for system in state_cfg.get("university_systems", []):
                for minutes_src in system.get("board_minutes", []):
                    log.info("  [minutes] %s -> %s", system["name"], minutes_src["url"])
                    new_matches, skipped = board_minutes_scraper.scrape_board_minutes(
                        minutes_src, session, seen, matchers, watchlist_patterns,
                        date_from=criteria.date_from, date_to=criteria.date_to,
                    )
                    new_matches = [m for m in new_matches
                                   if criteria.keep(m.get("full_text", ""), None)]
                    for m in new_matches:
                        m["state"], m["institution"] = state_key, system["name"]
                        db.insert_document(
                            conn, doc_type="board_minutes", state=state_key, institution=system["name"],
                            title=m["document_title"], url=m["document_url"], text=m.get("full_text", ""),
                            categories=m["categories"], watchlist_hits=m["watchlist_hits"],
                            source="board_minutes",
                        )
                    result.minutes.extend(new_matches)
                    for s in skipped:
                        s.update(state=state_key, institution=system["name"], pass_type="board_minutes")
                    result.skipped.extend(skipped)

        if not skip_transparency:
            for t_src in state_cfg.get("transparency", []):
                log.info("  [transparency] %s -> %s", t_src["name"], t_src["url"])
                new_matches, skipped = transparency_scraper.scrape_transparency(t_src, session, seen, watchlist_patterns)
                new_matches = [m for m in new_matches
                               if criteria.keep(m.get("row", "") or m.get("file_url", ""), None)]
                for m in new_matches:
                    m["state"], m["institution"] = state_key, t_src["name"]
                    # Use the matched row text (not the bare file_url) as the
                    # title so that several watchlist hits in the SAME file get
                    # stored as distinct rows. The UNIQUE(doc_type, url, title)
                    # constraint would otherwise collapse them all to one.
                    row_text = m.get("row", "")
                    db.insert_document(
                        conn, doc_type="transparency", state=state_key, institution=t_src["name"],
                        title=(row_text[:150] if row_text else m.get("file_url", "")),
                        url=m.get("file_url", ""),
                        text=(row_text or m.get("file_url", "")),
                        watchlist_hits=m.get("watchlist_hits", []),
                        source=("socrata" if t_src.get("type") == "socrata" else "transparency"),
                    )
                result.transparency.extend(new_matches)
                for s in skipped:
                    s.update(state=state_key, institution=t_src["name"], pass_type="transparency")
                result.skipped.extend(skipped)

                if not skip_contracts:
                    log.info("  [contracts] %s (reusing transparency CSVs) ...", t_src["name"])
                    contract_matches, contract_skipped = contracts_scraper.scrape_contracts(t_src, session, seen)
                    contract_matches = [c for c in contract_matches
                                        if criteria.keep(c.get("vendor", ""), c.get("end_date"))]
                    for c in contract_matches:
                        c["state"], c["institution"] = state_key, t_src["name"]
                        db.insert_contract(
                            conn, state=state_key, institution=t_src["name"], vendor=c["vendor"],
                            start_date=c["start_date"], end_date=c["end_date"], value=c["value"],
                            days_until_expiration=c["days_until_expiration"], source_url=c["source_url"],
                        )
                    result.contracts.extend(contract_matches)
                    for s in contract_skipped:
                        s.update(state=state_key, institution=t_src["name"], pass_type="contracts")
                    result.skipped.extend(contract_skipped)

        if not skip_federal:
            for f_src in state_cfg.get("federal_grants", []):
                log.info("  [federal] %s -> USAspending", f_src.get("name", "federal grants"))
                new_matches, skipped = usaspending_scraper.scrape_usaspending(
                    f_src, session, seen, matchers, watchlist_patterns,
                )
                new_matches = [m for m in new_matches
                               if criteria.keep(m["blob"], m.get("date"))]
                for m in new_matches:
                    m["state"] = state_key
                    db.insert_document(
                        conn, doc_type="federal_award", state=state_key,
                        institution=m["institution"], title=m["title"],
                        url=m["award_url"], text=m["blob"], date=m.get("date", ""),
                        categories=m["categories"], watchlist_hits=m.get("watchlist_hits", []),
                        source="usaspending",
                    )
                result.federal.extend(new_matches)
                for s in skipped:
                    s.update(state=state_key, institution=f_src.get("name", ""), pass_type="federal")
                result.skipped.extend(skipped)

    if not skip_contacts:
        log.info("\n=== CONTACTS (Apollo.io) ===")
        seen_apollo_ids = db.existing_apollo_ids(conn)
        contacts_sources = sources if not selected_state else {selected_state: sources[selected_state]}
        result.contacts = contacts.run_contacts_pass(contacts_sources, conn, seen_apollo_ids)

    utils.save_seen(seen)
    if write_report:
        result.report_paths = write_reports(result)
    return result


# Each type label -> (subfolder/file prefix, CSV header, row builder). Splitting
# by type gives every category its own columns instead of one generic schema.
_REPORT_SPECS = {
    "bids": (
        ["state", "institution", "categories", "title", "url", "date", "description"],
        lambda m: [m["state"], m["institution"], "; ".join(m["categories"]), m["title"],
                   m.get("detail_url") or m["source_url"], m.get("date", ""), m.get("description", "")],
    ),
    "board_minutes": (
        ["state", "institution", "categories_and_watchlist", "document_title", "document_url", "snippets"],
        lambda m: [m["state"], m["institution"], "; ".join(m["categories"] + m.get("watchlist_hits", [])),
                   m["document_title"], m["document_url"],
                   " | ".join(f"{k}: {v}" for k, v in m.get("snippets", {}).items())],
    ),
    "transparency": (
        ["state", "institution", "watchlist_hits", "file_url", "matched_row"],
        lambda m: [m["state"], m["institution"], "; ".join(m.get("watchlist_hits", [])),
                   m.get("file_url", ""), m.get("row", "")],
    ),
    "federal": (
        ["state", "institution", "program", "cfda", "amount", "agency",
         "start_date", "end_date", "award_url"],
        lambda m: [m["state"], m["institution"], m.get("program_title", ""), m.get("cfda", ""),
                   m.get("amount_str", ""), m.get("agency", ""), m.get("start_date", ""),
                   m.get("end_date", ""), m.get("award_url", "")],
    ),
    "contracts": (
        ["state", "institution", "vendor", "start_date", "end_date", "value",
         "days_until_expiration", "expiring_soon", "source_url"],
        lambda c: [c["state"], c["institution"], c["vendor"], c["start_date"], c["end_date"], c["value"],
                   c.get("days_until_expiration", ""), "yes" if c.get("expiring_soon") else "", c["source_url"]],
    ),
    "contacts": (
        ["state", "institution", "name", "title", "email", "linkedin_url"],
        lambda c: [c["state"], c["institution"], c["name"], c.get("title", ""),
                   c.get("email") or "", c.get("linkedin_url") or ""],
    ),
    "skipped": (
        ["pass_type", "state", "institution", "reason", "url", "notes"],
        lambda s: [s.get("pass_type", ""), s.get("state", ""), s.get("institution", ""),
                   s.get("reason", ""), s.get("url", ""), s.get("notes", "")],
    ),
}


def _slug(value: str) -> str:
    """Filesystem-safe token for a state key (e.g. 'North Carolina' -> 'north_carolina')."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (value or "unknown").strip()).strip("_").lower() or "unknown"


def retag_documents(conn, keywords_cfg: dict) -> dict:
    """Re-run category + watchlist matching over already-stored documents with
    the CURRENT keywords.yaml, and update each document's tags in place. Useful
    after retuning keywords - existing rows keep whatever tags they were scraped
    with until you re-tag. Returns stats; documents that now match nothing are
    counted as `now_empty` (the noise a retune eliminates)."""
    matchers = utils.build_category_matchers(keywords_cfg.get("categories", {}))
    watchlist_patterns = utils.build_watchlist_matchers(keywords_cfg.get("watchlist", []))

    rows = db.all_documents(conn)
    stats = {"total": len(rows), "changed": 0, "gained": 0, "lost": 0, "now_empty": 0}

    for row in rows:
        blob = f"{row['title'] or ''}\n{row['text'] or ''}"
        new_cats = utils.match_categories(blob, matchers)
        new_watch = utils.match_watchlist(blob, watchlist_patterns)
        old_cats = [c for c in (row["categories"] or "").split(", ") if c]
        old_watch = [w for w in (row["watchlist_hits"] or "").split(", ") if w]

        if set(new_cats) != set(old_cats) or set(new_watch) != set(old_watch):
            db.update_document_tags(conn, row["id"], new_cats, new_watch)
            stats["changed"] += 1
            delta = (len(new_cats) + len(new_watch)) - (len(old_cats) + len(old_watch))
            if delta > 0:
                stats["gained"] += 1
            elif delta < 0:
                stats["lost"] += 1
        if not new_cats and not new_watch:
            stats["now_empty"] += 1

    conn.commit()
    return stats


def write_reports(result: ScrapeResult) -> list[Path]:
    """Write per-type, per-state CSVs under reports/<type>/<type>_<state>_<ts>.csv.

    One timestamp is shared across every file in the run. Only non-empty
    (type, state) groups produce a file, so you don't get a litter of empty
    CSVs for passes that found nothing.
    """
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    written: list[Path] = []

    by_type = {
        "bids": result.bids,
        "board_minutes": result.minutes,
        "transparency": result.transparency,
        "federal": result.federal,
        "contracts": result.contracts,
        "contacts": result.contacts,
        "skipped": result.skipped,
    }

    for type_label, items in by_type.items():
        if not items:
            continue
        header, row_fn = _REPORT_SPECS[type_label]

        # Group this type's rows by state so each state gets its own file.
        by_state: dict[str, list] = {}
        for item in items:
            by_state.setdefault(item.get("state", ""), []).append(item)

        folder = utils.REPORTS_DIR / type_label
        folder.mkdir(parents=True, exist_ok=True)
        for state_key, state_items in by_state.items():
            path = folder / f"{type_label}_{_slug(state_key)}_{timestamp}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for item in state_items:
                    writer.writerow(row_fn(item))
            written.append(path)
            log.info("  wrote %s (%d rows)", path, len(state_items))

    if not written:
        log.info("  (no report files written - nothing new this run)")
    return written
