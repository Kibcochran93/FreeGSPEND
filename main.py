#!/usr/bin/env python3
"""
govspend_free - a free, self-hosted stand-in for GovSpend's core modules:
Bids & RFPs, Meeting Intelligence (board minutes), Spending & POs
(transparency), Co-Ops & Contracts (expirations), Contacts (via Apollo.io),
plus a rule-based Opportunities feed and (optional) AI Search/Chat.

Everyday usage:
    pip install -r requirements.txt
    python main.py                 # scrape everything configured
    python main.py --state texas   # just one state
    python main.py --list-states

Query what's already been collected (no scraping, instant):
    python main.py --search "attendance software"
    python main.py --opportunities
    python main.py --expirations

Optional, needs your own Apollo.io API key (config/apollo.yaml):
    Contacts pass runs automatically as part of a normal scrape if enabled.

Optional, needs your own Anthropic API key (config/llm.yaml or env var):
    python main.py --ask "which Texas institutions are evaluating ERP systems?"
    python main.py --chat 42

Optional, needs your own SMTP credentials (config/alerts.yaml):
    Alerts are sent automatically after a scrape if enabled, or manually:
    python main.py --send-alerts-only   (re-sends a digest of the last run's results)

Every scrape also still writes reports/report_<timestamp>.csv (unchanged
format) alongside the new persistent SQLite DB at db/govspend_free.db.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import yaml

from govspend_free import (
    alerts,
    bid_scraper,
    board_minutes_scraper,
    contacts,
    contracts_scraper,
    db,
    llm,
    opportunities,
    transparency_scraper,
    utils,
)
from govspend_free.utils import log

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", help="Only scan this state key from sources.yaml (e.g. arkansas)")
    p.add_argument("--list-states", action="store_true", help="List configured state keys and exit")
    p.add_argument("--skip-transparency", action="store_true")
    p.add_argument("--skip-board-minutes", action="store_true")
    p.add_argument("--skip-bids", action="store_true")
    p.add_argument("--skip-contracts", action="store_true")
    p.add_argument("--skip-contacts", action="store_true", help="Skip the Apollo pass even if config/apollo.yaml is enabled")

    # Query-only modes (no scraping, read from the local DB)
    p.add_argument("--search", metavar="QUERY", help="Full-text search everything ever scraped, then exit")
    p.add_argument("--opportunities", action="store_true", help="Print the ranked opportunities feed, then exit")
    p.add_argument("--expirations", type=int, nargs="?", const=180, metavar="DAYS",
                    help="Print contracts expiring within DAYS (default 180), then exit")
    p.add_argument("--ask", metavar="QUESTION", help="AI Search: ask a natural-language question over your local data (needs Anthropic API key)")
    p.add_argument("--chat", type=int, metavar="DOC_ID", help="Record-Level Chat REPL for one document id (needs Anthropic API key)")
    p.add_argument("--send-alerts-only", action="store_true", help="Send an email digest of the most recent report.csv, without scraping again")

    p.add_argument("-q", "--quiet", action="store_true", help="Only log warnings/errors; still prints results (report path, summary, search output)")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG-level) logging")

    return p.parse_args()


def main():
    args = parse_args()
    utils.setup_logging(verbosity=(1 if args.verbose else -1 if args.quiet else 0))
    sources = load_yaml(CONFIG_DIR / "sources.yaml")
    keywords_cfg = load_yaml(CONFIG_DIR / "keywords.yaml")

    # Normalize the state key so `--state Texas` matches the lowercase keys
    # in sources.yaml.
    selected_state = args.state.lower() if args.state else None

    if args.list_states:
        for key in sources:
            print(key)
        return

    conn = db.get_conn()

    # ---- Query-only modes: read from the DB, don't scrape, then exit ----
    if args.search:
        rows = db.search(conn, args.search)
        print(f"\n{len(rows)} result(s) for '{args.search}':\n")
        for r in rows:
            print(f"[{r['id']}] ({r['doc_type']}) {r['state']}/{r['institution']}: {r['title']}")
            print(f"    {r['snippet']}")
            print(f"    {r['url']}\n")
        return

    if args.opportunities:
        opportunities.print_opportunities(opportunities.rank_opportunities(conn))
        return

    if args.expirations is not None:
        rows = db.upcoming_expirations(conn, within_days=args.expirations)
        print(f"\n{len(rows)} contract(s) expiring within {args.expirations} days:\n")
        for r in rows:
            print(f"  {r['vendor']} - {r['institution']} ({r['state']}) - ends {r['end_date']} "
                  f"({r['days_until_expiration']}d) - {r['source_url']}")
        return

    if args.ask:
        llm.ai_search(args.ask, conn)
        return

    if args.chat is not None:
        llm.chat_with_record(args.chat, conn)
        return

    if args.send_alerts_only:
        _resend_last_report_digest()
        return

    # ---- Normal scrape mode ----
    matchers = utils.build_category_matchers(keywords_cfg.get("categories", {}))
    watchlist_patterns = utils.build_watchlist_matchers(keywords_cfg.get("watchlist", []))

    seen = utils.load_seen()
    session = utils.get_session()

    states_to_scan = [selected_state] if selected_state else list(sources.keys())
    unknown = [s for s in states_to_scan if s not in sources]
    if unknown:
        log.error("Unknown state key(s): %s. Run --list-states to see valid keys.", unknown)
        sys.exit(1)

    all_bid_matches, all_minutes_matches = [], []
    all_transparency_matches, all_contract_matches = [], []
    all_skipped = []

    for state_key in states_to_scan:
        state_cfg = sources[state_key]
        log.info("\n=== %s ===", state_key.upper())

        if not args.skip_bids:
            for system in state_cfg.get("university_systems", []):
                for board in system.get("bid_boards", []):
                    log.info("  [bids] %s -> %s", system["name"], board["url"])
                    new_matches, skipped = bid_scraper.scrape_bid_board(board, session, seen, matchers)
                    for m in new_matches:
                        m["state"], m["institution"] = state_key, system["name"]
                        db.insert_document(
                            conn, doc_type="bid", state=state_key, institution=system["name"],
                            title=m["title"], url=m.get("detail_url") or m["source_url"],
                            text=m.get("description", ""), date=m.get("date", ""),
                            categories=m["categories"],
                        )
                    all_bid_matches.extend(new_matches)
                    for s in skipped:
                        s.update(state=state_key, institution=system["name"], pass_type="bids")
                    all_skipped.extend(skipped)

        if not args.skip_board_minutes:
            for system in state_cfg.get("university_systems", []):
                for minutes_src in system.get("board_minutes", []):
                    log.info("  [minutes] %s -> %s", system["name"], minutes_src["url"])
                    new_matches, skipped = board_minutes_scraper.scrape_board_minutes(
                        minutes_src, session, seen, matchers, watchlist_patterns
                    )
                    for m in new_matches:
                        m["state"], m["institution"] = state_key, system["name"]
                        db.insert_document(
                            conn, doc_type="board_minutes", state=state_key, institution=system["name"],
                            title=m["document_title"], url=m["document_url"], text=m.get("full_text", ""),
                            categories=m["categories"], watchlist_hits=m["watchlist_hits"],
                        )
                    all_minutes_matches.extend(new_matches)
                    for s in skipped:
                        s.update(state=state_key, institution=system["name"], pass_type="board_minutes")
                    all_skipped.extend(skipped)

        if not args.skip_transparency:
            for t_src in state_cfg.get("transparency", []):
                log.info("  [transparency] %s -> %s", t_src["name"], t_src["url"])
                new_matches, skipped = transparency_scraper.scrape_transparency(t_src, session, seen, watchlist_patterns)
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
                    )
                all_transparency_matches.extend(new_matches)
                for s in skipped:
                    s.update(state=state_key, institution=t_src["name"], pass_type="transparency")
                all_skipped.extend(skipped)

                if not args.skip_contracts:
                    log.info("  [contracts] %s (reusing transparency CSVs) ...", t_src["name"])
                    contract_matches, contract_skipped = contracts_scraper.scrape_contracts(t_src, session, seen)
                    for c in contract_matches:
                        c["state"], c["institution"] = state_key, t_src["name"]
                        db.insert_contract(
                            conn, state=state_key, institution=t_src["name"], vendor=c["vendor"],
                            start_date=c["start_date"], end_date=c["end_date"], value=c["value"],
                            days_until_expiration=c["days_until_expiration"], source_url=c["source_url"],
                        )
                    all_contract_matches.extend(contract_matches)
                    for s in contract_skipped:
                        s.update(state=state_key, institution=t_src["name"], pass_type="contracts")
                    all_skipped.extend(contract_skipped)

    all_contact_matches = []
    if not args.skip_contacts:
        log.info("\n=== CONTACTS (Apollo.io) ===")
        seen_apollo_ids = db.existing_apollo_ids(conn)
        contacts_sources = sources if not selected_state else {selected_state: sources[selected_state]}
        all_contact_matches = contacts.run_contacts_pass(contacts_sources, conn, seen_apollo_ids)

    utils.save_seen(seen)
    report_path = _write_report(all_bid_matches, all_minutes_matches, all_transparency_matches,
                                 all_contract_matches, all_contact_matches, all_skipped)
    _print_summary(all_bid_matches, all_minutes_matches, all_transparency_matches,
                    all_contract_matches, all_contact_matches, all_skipped)

    digest_sent = alerts.send_digest(
        subject=f"govspend_free digest - {dt.datetime.now():%Y-%m-%d}",
        body_text=alerts.build_digest_text(
            all_bid_matches, all_minutes_matches, all_transparency_matches,
            all_contract_matches, all_contact_matches,
        ),
    )
    if not digest_sent:
        log.info("(Email digest not sent - see config/alerts.yaml.example to enable it.)")

    print(f"\nTip: run `python main.py --opportunities` to see everything ranked, "
          f"or `python main.py --search \"term\"` to query what's in {report_path.parent.parent / 'db'}.")


def _resend_last_report_digest():
    reports = sorted(utils.REPORTS_DIR.glob("report_*.csv"))
    if not reports:
        print("No reports found yet - run a scrape first.")
        return
    latest = reports[-1]
    body = f"Re-sending most recent report: {latest.name}\n\n" + latest.read_text(encoding="utf-8")[:20000]
    sent = alerts.send_digest(subject=f"govspend_free digest (resend) - {latest.name}", body_text=body)
    if not sent:
        print("Could not send - check config/alerts.yaml.")


def _write_report(bid_matches, minutes_matches, transparency_matches, contract_matches, contact_matches, skipped) -> Path:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = utils.REPORTS_DIR / f"report_{timestamp}.csv"

    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["pass_type", "state", "institution", "categories/watchlist_hits/tags",
                          "title_or_name", "url_or_email", "detail"])

        for m in bid_matches:
            writer.writerow(["bid", m["state"], m["institution"], "; ".join(m["categories"]),
                              m["title"], m.get("detail_url") or m["source_url"], m.get("description", "")])

        for m in minutes_matches:
            cats = "; ".join(m["categories"] + m.get("watchlist_hits", []))
            snippet_text = " | ".join(f"{k}: {v}" for k, v in m.get("snippets", {}).items())
            writer.writerow(["board_minutes", m["state"], m["institution"], cats,
                              m["document_title"], m["document_url"], snippet_text])

        for m in transparency_matches:
            writer.writerow(["transparency", m["state"], m["institution"], "; ".join(m.get("watchlist_hits", [])),
                              m.get("file_url", ""), m.get("file_url", ""), m.get("row", "")])

        for c in contract_matches:
            tag = "EXPIRING_SOON" if c.get("expiring_soon") else ""
            writer.writerow(["contract", c["state"], c["institution"], tag,
                              c["vendor"], c["end_date"], f"start={c['start_date']} value={c['value']}"])

        for c in contact_matches:
            writer.writerow(["contact", c["state"], c["institution"], c.get("title", ""),
                              c["name"], c.get("email") or c.get("linkedin_url", ""), ""])

        for s in skipped:
            writer.writerow([f"SKIPPED:{s.get('pass_type', '')}", s.get("state", ""), s.get("institution", ""),
                              s.get("reason", ""), "", s.get("url", ""), s.get("notes", "")])

    print(f"\nReport written to: {report_path}")
    return report_path


def _print_summary(bid_matches, minutes_matches, transparency_matches, contract_matches, contact_matches, skipped):
    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"New bid matches:            {len(bid_matches)}")
    print(f"New board-minutes matches:  {len(minutes_matches)}")
    print(f"New transparency matches:   {len(transparency_matches)}")
    print(f"New contract records:       {len(contract_matches)} "
          f"({sum(1 for c in contract_matches if c.get('expiring_soon'))} expiring soon)")
    print(f"New contacts (Apollo):      {len(contact_matches)}")
    print(f"Sources skipped:            {len(skipped)}")

    if skipped:
        print("\nSkipped sources (by reason):")
        by_reason: dict[str, int] = {}
        for s in skipped:
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
