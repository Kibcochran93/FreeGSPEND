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
import datetime as dt
import re
import sys
from pathlib import Path

import yaml

from govspend_free import (
    alerts,
    brief,
    db,
    llm,
    opportunities,
    pipeline,
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

    # Scrape criteria (optional filters that scope a run)
    p.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="Only keep documents dated on/after this date")
    p.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="Only keep documents dated on/before this date")
    p.add_argument("--only-keyword", metavar="TERMS", help="Only keep documents whose text contains one of these terms (comma-separated)")
    p.add_argument("--only-competitor", metavar="NAMES", help="Only keep documents mentioning one of these vendor/competitor names (comma-separated)")
    p.add_argument("--browser", action="store_true", help="Render js_rendered sources with headless Chromium (needs the [browser] extra + `playwright install chromium`). Slower.")

    # Query-only modes (no scraping, read from the local DB)
    p.add_argument("--search", metavar="QUERY", help="Full-text search everything ever scraped, then exit")
    p.add_argument("--opportunities", action="store_true", help="Print the ranked opportunities feed, then exit")
    p.add_argument("--expirations", type=int, nargs="?", const=180, metavar="DAYS",
                    help="Print contracts expiring within DAYS (default 180), then exit")
    p.add_argument("--ask", metavar="QUESTION", help="AI Search: ask a natural-language question over your local data (needs Anthropic API key)")
    p.add_argument("--chat", type=int, metavar="DOC_ID", help="Record-Level Chat REPL for one document id (needs Anthropic API key)")
    p.add_argument("--brief", metavar="DOC_ID_OR_INSTITUTION", help="Generate a SEAtS account brief for a scraped doc id or institution name (uses the local `claude` CLI, your Claude login)")
    p.add_argument("--send-alerts-only", action="store_true", help="Send an email digest of the most recent report.csv, without scraping again")
    p.add_argument("--retag", action="store_true", help="Re-run keyword/watchlist matching over already-scraped documents with the current keywords.yaml, updating their tags (use after editing keywords)")

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

    if args.brief:
        try:
            result = brief.generate_brief(conn, args.brief)
        except (ValueError, RuntimeError) as exc:
            log.error("%s", exc)
            sys.exit(1)
        print("\n" + result["markdown"])
        if result["path"]:
            print(f"\n(brief saved to {result['path']})")
        return

    if args.send_alerts_only:
        _resend_last_report_digest()
        return

    if args.retag:
        stats = pipeline.retag_documents(conn, keywords_cfg)
        print(f"\nRe-tagged {stats['total']} stored document(s) with the current keywords:")
        print(f"  changed tags:                          {stats['changed']}")
        print(f"  gained tags (more specific matches):   {stats['gained']}")
        print(f"  lost tags (noise removed):             {stats['lost']}")
        print(f"  now match nothing (noise candidates):  {stats['now_empty']}")
        print("\nRun `python main.py --opportunities` to see the re-ranked feed.")
        return

    # ---- Normal scrape mode ----
    try:
        result = pipeline.run_scrape(
            conn, sources, keywords_cfg,
            selected_state=selected_state,
            skip_bids=args.skip_bids,
            skip_board_minutes=args.skip_board_minutes,
            skip_transparency=args.skip_transparency,
            skip_contracts=args.skip_contracts,
            skip_contacts=args.skip_contacts,
            criteria=pipeline.ScrapeCriteria.build(
                date_from=args.from_date, date_to=args.to_date,
                only_keywords=args.only_keyword, only_competitors=args.only_competitor,
            ),
            use_browser=args.browser,
        )
    except ValueError as exc:
        log.error("%s Run --list-states to see valid keys.", exc)
        sys.exit(1)

    _print_summary(result)

    digest_sent = alerts.send_digest(
        subject=f"govspend_free digest - {dt.datetime.now():%Y-%m-%d}",
        body_text=alerts.build_digest_text(
            result.bids, result.minutes, result.transparency, result.contracts, result.contacts,
        ),
    )
    if not digest_sent:
        log.info("(Email digest not sent - see config/alerts.yaml.example to enable it.)")

    if result.report_paths:
        print(f"\n{len(result.report_paths)} report file(s) written under {utils.REPORTS_DIR}"
              f" (one per type/state).")
    print(f"Tip: run `python main.py --opportunities` to see everything ranked, "
          f"or `python main.py --search \"term\"` to query what's in {utils.ROOT_DIR / 'db'}.")


def _resend_last_report_digest():
    # Reports are now split into reports/<type>/<type>_<state>_<timestamp>.csv.
    # Re-send the most recent *run* - i.e. every file sharing the newest
    # timestamp - concatenated into one digest body.
    report_files = sorted(utils.REPORTS_DIR.glob("*/*.csv"))
    if not report_files:
        print("No reports found yet - run a scrape first.")
        return

    def timestamp_of(path: Path) -> str:
        m = re.search(r"_(\d{4}-\d{2}-\d{2}_\d{6})\.csv$", path.name)
        return m.group(1) if m else ""

    latest_ts = max((timestamp_of(p) for p in report_files), default="")
    run_files = [p for p in report_files if timestamp_of(p) == latest_ts] or [report_files[-1]]

    parts = [f"Re-sending most recent report run ({latest_ts}), {len(run_files)} file(s):\n"]
    for p in run_files:
        parts.append(f"\n=== {p.parent.name}/{p.name} ===\n" + p.read_text(encoding="utf-8"))
    body = "".join(parts)[:20000]

    sent = alerts.send_digest(subject=f"govspend_free digest (resend) - {latest_ts}", body_text=body)
    if not sent:
        print("Could not send - check config/alerts.yaml.")


def _print_summary(result: pipeline.ScrapeResult):
    c = result.counts()
    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"New bid matches:            {c['bids']}")
    print(f"New board-minutes matches:  {c['minutes']}")
    print(f"New transparency matches:   {c['transparency']}")
    print(f"New contract records:       {c['contracts']} ({c['contracts_expiring_soon']} expiring soon)")
    print(f"New contacts (Apollo):      {c['contacts']}")
    print(f"Sources skipped:            {c['skipped']}")

    if result.skipped:
        print("\nSkipped sources (by reason):")
        by_reason: dict[str, int] = {}
        for s in result.skipped:
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
