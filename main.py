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

Every scrape persists everything into the SQLite DB at db/govspend_free.db,
which is the single source of truth (query it with --search / --opportunities,
the desktop UI, or any SQLite viewer).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

from govspend_free import (
    alerts,
    brief,
    db,
    doctor,
    llm,
    normalize,
    opportunities,
    ops,
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
    p.add_argument("--state", help="Only scan these state keys from sources.yaml (one, or a comma-separated list, e.g. arkansas,texas). Omit to scan all.")
    p.add_argument("--list-states", action="store_true", help="List configured state keys and exit")
    p.add_argument("--doctor", action="store_true", help="Report what's configured/working (deps, config files, tokens, DB contents), then exit")
    p.add_argument("--discover", metavar="FAMILY", choices=["bonfire", "ionwave"], help="Enumerate + classify Bonfire/Ion Wave tenants (Common Crawl + a live fetch each) into reports/discovered_<family>_<ts>.csv, then exit. Slow + rate-limited; higher-ed yield on these platforms is small.")
    p.add_argument("--skip-transparency", action="store_true")
    p.add_argument("--skip-federal", action="store_true", help="Skip the USAspending federal-grant pass")
    p.add_argument("--skip-sam", action="store_true", help="Skip the SAM.gov federal-RFP pass (nationwide; runs only on a full scrape when config/sam.yaml is enabled)")
    p.add_argument("--skip-grants", action="store_true", help="Skip the Grants.gov federal grant-opportunity pass (nationwide, keyless; runs only on a full scrape when config/grants_gov.yaml is enabled)")
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
    p.add_argument("--coverage", action="store_true", help="Print the national (50-state) coverage scorecard - configured vs represented vs covered - and write reports/coverage_<ts>.csv, then exit")
    p.add_argument("--play", action="store_true", help="Run the Ops 'Full Motion' account-prioritization play (read-only HubSpot via a Private App token), then exit")
    p.add_argument("--expirations", type=int, nargs="?", const=180, metavar="DAYS",
                    help="Print contracts expiring within DAYS (default 180), then exit")
    p.add_argument("--ask", metavar="QUESTION", help="AI Search: ask a natural-language question over your local data (needs Anthropic API key)")
    p.add_argument("--chat", type=int, metavar="DOC_ID", help="Record-Level Chat REPL for one document id (needs Anthropic API key)")
    p.add_argument("--brief", metavar="DOC_ID_OR_INSTITUTION", help="Generate a SEAtS account brief for a scraped doc id or institution name (uses the local `claude` CLI, your Claude login)")
    p.add_argument("--send-alerts-only", action="store_true", help="Email a digest of the most recently scraped documents (from the DB), without scraping again")
    p.add_argument("--retag", action="store_true", help="Re-run keyword/watchlist matching over already-scraped documents with the current keywords.yaml, updating their tags (use after editing keywords)")
    p.add_argument("--normalize-payments", action="store_true", help="Resolve stored state-checkbook rows into the normalized `payments` table (competitor/client footprint), then exit")
    p.add_argument("--ingest-spend", action="store_true", help="Pull state-checkbook payments live via Socrata (SODA) into the normalized `payments` table, then exit")
    p.add_argument("--reset-payments", action="store_true", help="Empty the payments table (regenerable via --ingest-spend), then exit")
    p.add_argument("--backfill-dates", action="store_true", help="Derive & store each document's own date (from its text) for rows that lack one, so the Opportunities feed can age-filter them, then exit")

    p.add_argument("-q", "--quiet", action="store_true", help="Only log warnings/errors; still prints results (report path, summary, search output)")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG-level) logging")

    return p.parse_args()


def main():
    args = parse_args()
    utils.setup_logging(verbosity=(1 if args.verbose else -1 if args.quiet else 0))
    sources = load_yaml(CONFIG_DIR / "sources.yaml")
    keywords_cfg = load_yaml(CONFIG_DIR / "keywords.yaml")

    # Normalize the state key(s) so `--state Texas` or `--state Texas,Ohio`
    # match the lowercase keys in sources.yaml. One state stays a str; several
    # become a list; omitted is None (scan all).
    if args.state:
        _states = [s.strip().lower() for s in args.state.split(",") if s.strip()]
        selected_state = _states[0] if len(_states) == 1 else (_states or None)
    else:
        selected_state = None

    if args.list_states:
        for key in sorted(sources):
            print(key)
        return

    if args.doctor:
        doctor.run_doctor()
        return

    if args.discover:
        import time as _time
        from govspend_free import discovery
        rows = discovery.run(args.discover)
        he = [r for r in rows if r.get("segment") == "higher_ed" and r.get("live")]
        out = utils.REPORTS_DIR / f"discovered_{args.discover}_{_time.strftime('%Y%m%dT%H%M%SZ', _time.gmtime())}.csv"
        discovery.write_candidates_csv(rows, out)
        live = sum(1 for r in rows if r.get("live"))
        print(f"\nClassified {len(rows)} {args.discover} tenant(s): {live} live, {len(he)} look higher-ed.")
        for r in sorted(he, key=lambda r: (r.get("state") or "zz", r["slug"]))[:40]:
            print(f"  higher-ed: {r['slug']:20} state={r.get('state') or '?':14} open={r.get('open', '')}  {str(r.get('name', ''))[:45]}")
        print(f"\nCSV: {out}")
        print("Review it, confirm each institution's state, then add verified higher-ed portals to config/sources.yaml.")
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

    if args.coverage:
        import time as _time
        from govspend_free import coverage as coverage_mod
        rows = coverage_mod.build_coverage(conn, sources)
        s = coverage_mod.summarize(rows)
        n_cov, n_rep, n_cfg, n_missing = (len(s["covered"]), len(s["represented"]),
                                          len(s["configured"]), len(s["missing"]))
        print("\n=== National coverage scorecard (education RFP/bid sources) ===")
        print(f"  Covered     (>=2 institutions w/ docs): {n_cov:>2}/50  {', '.join(s['covered']) or '-'}")
        print(f"  Represented (>=1 institution w/ docs):  {n_rep:>2}/50  {', '.join(s['represented']) or '-'}")
        print(f"  Configured  (sources set, no docs yet): {n_cfg:>2}/50  {', '.join(s['configured']) or '-'}")
        print(f"  Missing     (no education source):      {n_missing:>2}/50")
        print( "  ---")
        print(f"  Represented or better: {len(s['represented_or_better'])}/50 states")
        print(f"  Configured or better:  {len(s['configured_or_better'])}/50 states")
        print(f"  Federal grants configured: {len(s['with_federal'])}/50 states")
        shown = [r for r in rows if r.status != "missing"]
        if shown:
            order = {"covered": 0, "represented": 1, "configured": 2}
            print("\n  State  Status       Src  Inst  Docs  Families")
            for r in sorted(shown, key=lambda r: (order[r.status], r.abbr)):
                print(f"  {r.abbr:<5}  {r.status:<11}  {r.configured_sources:>3}  "
                      f"{r.institutions_with_docs:>4}  {r.education_docs:>4}  {','.join(r.families)}")
        print(f"\n  Missing ({n_missing}): {', '.join(s['missing'])}")
        ts = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
        out = utils.REPORTS_DIR / f"coverage_{ts}.csv"
        coverage_mod.write_coverage_csv(rows, out)
        print(f"\n  CSV: {out}")
        return

    if args.play:
        result = ops.run_full_motion_play(on_progress=lambda line: log.info("%s", line), conn=conn)
        if result["ok"]:
            print("\n" + (result["markdown"] or "(no output)"))
            print(f"\nSaved to {result['report_path']}")
        else:
            log.error("%s", result["error"])
            sys.exit(1)
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
        _resend_last_report_digest(conn)
        return

    if args.reset_payments:
        n = db.clear_payments(conn)
        print(f"Cleared {n} row(s) from the payments table. "
              "Repopulate with `python main.py --ingest-spend`.")
        return

    if args.backfill_dates:
        stats = db.backfill_dates(conn)
        print(f"Backfilled dates: {stats['filled']} of {stats['scanned']} dateless "
              f"document(s) now carry a derived date.")
        return

    if args.ingest_spend:
        from govspend_free import spend_ingest
        stats = spend_ingest.ingest(conn, sources, selected_state=selected_state)
        print(f"\nIngested {stats['inserted']} new payment(s) from {stats['sources']} Socrata source(s) "
              f"({stats['resolved']} resolved):")
        for st, n in sorted(stats["by_state"].items()):
            print(f"  {st}: {n}")
        rollup = db.payments_summary(conn)
        if rollup:
            print("\nVendor footprint (resolved payments):")
            for r in rollup:
                print(f"  {r['state']:14} {r['vendor_kind']:11} {r['vendor_canonical'] or '':22} {r['n']}")
        return

    if args.normalize_payments:
        stats = normalize.backfill_payments_from_documents(conn)
        print(f"\nNormalized {stats['scanned']} checkbook row(s) into the payments table:")
        print(f"  client (SEAtS):   {stats['client']}")
        print(f"  competitor:       {stats['competitor']}")
        print(f"  institution:      {stats['institution']}")
        print(f"  unknown (skipped): {stats['unknown']}")
        print(f"  newly inserted:   {stats['inserted']}")
        rollup = db.payments_summary(conn)
        if rollup:
            print("\nVendor footprint (resolved payments):")
            for r in rollup:
                print(f"  {r['state']:12} {r['vendor_kind']:11} {r['vendor_canonical'] or '':22} {r['n']}")
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
            skip_federal=args.skip_federal,
            skip_sam=args.skip_sam,
            skip_grants=args.skip_grants,
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

    print(f"\nEverything is stored in {utils.ROOT_DIR / 'db' / 'govspend_free.db'}.")
    print(f"Tip: run `python main.py --opportunities` to see everything ranked, "
          f"or `python main.py --search \"term\"` to query it.")


def _resend_last_report_digest(conn):
    """Email a digest built from the DB (the most recently scraped documents),
    without scraping again. Reads straight from db/govspend_free.db - no CSVs."""
    rows = pipeline.documents_since(conn, limit=200)
    if not rows:
        print("Nothing stored yet - run a scrape first.")
        return

    lines = [f"Digest of the {len(rows)} most recently scraped documents:\n"]
    for r in rows:
        lines.append(f"[{r['doc_type']}] {r['state']}/{r['institution']}: {r['title']}")
        if r["url"]:
            lines.append(f"    {r['url']}")
    body = "\n".join(lines)[:20000]

    sent = alerts.send_digest(subject=f"govspend_free digest - {dt.datetime.now():%Y-%m-%d}", body_text=body)
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
    print(f"New federal grant awards:   {c['federal']}")
    print(f"New SAM.gov federal RFPs:    {c['federal_rfps']}")
    print(f"New Grants.gov grant opps:   {c['federal_grant_opps']}")
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
