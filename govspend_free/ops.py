"""
Ops "plays" - GovSpend Free's account-prioritization motion.

The flagship "Full Motion" play scores every account that has a signal in the
local DB, cross-references your CRM for pipeline status and decision-makers,
and drafts a spend-grounded opener per account - the whole prospecting motion
in one ranked table.

Two halves, both deterministic and inspectable (no LLM, on purpose - same
philosophy as opportunities.py):

  1. Signal extraction from db/govspend_free.db (offline): which institutions,
     which competitors, which categories are "in scope".
  2. CRM enrichment via HubSpot's REST API, READ-ONLY, using a Private App
     token (see hubspot_client.py). Pipeline status + the best decision-maker
     contact per account.

Then a transparent 0-100 score, a template opener grounded in each account's
real signal (never a fabricated number), and a ranked markdown table saved to
reports/ops/.

Read-only guarantee: the only CRM surface used is hubspot_client, which has no
create/update/delete methods and expects a read-scoped token.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import db, hubspot_client, utils

CONFIG_PATH = utils.ROOT_DIR / "config" / "ops.yaml"
OPS_REPORTS_DIR = utils.REPORTS_DIR / "ops"

# Watchlist terms that ARE the client (SEAtS), not a competitor.
_SELF_VENDOR_TERMS = {"seats", "seats software", "seats software limited"}

# Titles we most want to reach, best first. Used to pick one decision-maker
# from an account's CRM contacts.
_CONTACT_PRIORITY = [
    "provost", "vice chancellor", "vice president", "chancellor", "president",
    "dean", "registrar", "enrollment", "student success", "student affairs",
    "retention", "chief information", "cio", "director of", "director",
]


@dataclass
class OpsConfig:
    client: str = "SEAtS Software"
    what_we_sell: str = (
        "student attendance, engagement, retention and early-alert software for "
        "higher education"
    )
    competitors: list[str] = field(default_factory=lambda: [
        "EAB", "Starfish", "Civitas", "Watermark", "Anthology",
        "Ellucian", "Jenzabar", "Workday Student",
    ])


def load_config(path: Path = CONFIG_PATH) -> OpsConfig:
    if not path.exists():
        return OpsConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {f.name for f in OpsConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return OpsConfig(**{k: v for k, v in raw.items() if k in known})


# --------------------------------------------------------------------------
# Step 1 - signal extraction from the local DB (offline, deterministic).
# --------------------------------------------------------------------------

def _split_csv(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def gather_signals(conn) -> list[dict]:
    """Roll the documents table up to one row per institution, keeping only
    accounts that carry a real signal (a named competitor, a bid, or an
    existing-vendor mention). This is the "in scope" account universe."""
    rows = conn.execute(
        "SELECT doc_type, state, institution, date, categories, watchlist_hits "
        "FROM documents WHERE institution IS NOT NULL AND institution != ''"
    ).fetchall()

    accounts: dict[str, dict] = {}
    for r in rows:
        inst = r["institution"].strip()
        hits = _split_csv(r["watchlist_hits"])
        competitors = [h for h in hits if h.lower() not in _SELF_VENDOR_TERMS]
        self_here = len(competitors) < len(hits)

        acc = accounts.setdefault(inst, {
            "institution": inst, "state": r["state"], "doc_types": set(),
            "competitors": set(), "categories": set(), "doc_count": 0,
            "client_present": False,
        })
        acc["doc_types"].add(r["doc_type"])
        acc["competitors"].update(competitors)
        acc["categories"].update(_split_csv(r["categories"]))
        acc["doc_count"] += 1
        acc["client_present"] = acc["client_present"] or self_here

    scoped = []
    for acc in accounts.values():
        has_bid = "bid" in acc["doc_types"]
        has_federal = "federal_award" in acc["doc_types"]
        # In scope if there's a competitor to displace, a live bid, an existing
        # SEAtS footprint, OR a federal student-success grant (budget signal).
        if not (acc["competitors"] or has_bid or acc["client_present"] or has_federal):
            continue
        scoped.append({
            "institution": acc["institution"], "state": acc["state"],
            "doc_types": sorted(acc["doc_types"]),
            "competitors": sorted(acc["competitors"]),
            "categories": sorted(acc["categories"]),
            "doc_count": acc["doc_count"], "client_present": acc["client_present"],
        })
    scoped.sort(key=lambda a: (len(a["competitors"]), a["doc_count"]), reverse=True)
    return scoped


# --------------------------------------------------------------------------
# Step 2 - CRM enrichment (read-only) + scoring.
# --------------------------------------------------------------------------

def _best_company_match(query: str, results: list[dict]) -> dict | None:
    """Pick the most plausible company for `query` from HubSpot results."""
    if not results:
        return None
    q = query.lower().strip()
    def rank(r):
        name = (r.get("properties", {}).get("name") or "").lower().strip()
        if name == q:
            return 0
        if name and (name.startswith(q) or q.startswith(name)):
            return 1
        if name and (q in name or name in q):
            return 2
        return 3
    return sorted(results, key=rank)[0]


def _best_contact(contacts: list[dict]) -> dict | None:
    """Choose the best decision-maker by title priority; tie-break on email."""
    def rank(c):
        title = (c.get("properties", {}).get("jobtitle") or "").lower()
        for i, kw in enumerate(_CONTACT_PRIORITY):
            if kw in title:
                pri = i
                break
        else:
            pri = len(_CONTACT_PRIORITY)
        has_email = 0 if (c.get("properties", {}).get("email") or "").strip() else 1
        return (pri, has_email)
    ranked = sorted(contacts, key=rank)
    return ranked[0] if ranked else None


def _fmt_contact(contact: dict | None) -> str:
    if not contact:
        return "(enrich - no CRM contact)"
    p = contact.get("properties", {})
    name = f"{p.get('firstname', '') or ''} {p.get('lastname', '') or ''}".strip() or (p.get("email") or "unknown")
    title = p.get("jobtitle") or ""
    return f"{name}{' - ' + title if title else ''}".strip()


def crm_lookup(client, account: dict) -> dict:
    """Look an account up in HubSpot (read-only) and return its CRM status +
    best contact. status is In Pipeline / Cold / Whitespace."""
    results = client.search_company(account["institution"])
    company = _best_company_match(account["institution"], results)
    if not company:
        return {"status": "Whitespace", "company_id": None, "contact": None, "deal_count": 0}
    cid = str(company["id"])
    props = company.get("properties", {})
    deals = client.company_deals(cid)
    in_pipeline = bool(deals) or (props.get("lifecyclestage", "") or "").lower() == "customer"
    contacts = client.company_contacts(cid)
    return {
        "status": "In Pipeline" if in_pipeline else "Cold",
        "company_id": cid,
        "contact": _best_contact(contacts),
        "deal_count": len(deals),
    }


def score_account(account: dict, crm: dict) -> tuple[int, dict]:
    """Transparent 0-100 score. Components (max): competitor pressure 25,
    spend/budget evidence 20, active bid 15, board/meeting signal 20, CRM
    readiness 20. There is deliberately no contract-expiry component here: the
    tool has no expiry data, so we never fabricate that highest-weight factor -
    competitor pressure stands in for displacement opportunity instead."""
    doc_types = set(account["doc_types"])
    competitors = account["competitors"]

    competitor_pressure = round(min(len(competitors), 3) / 3 * 25)

    # A federal student-success grant is hard budget evidence, same weight as
    # showing up in a state checkbook.
    if "transparency" in doc_types or "federal_award" in doc_types or account["client_present"]:
        evidence = 20
    elif "board_minutes" in doc_types:
        evidence = 8
    else:
        evidence = 0

    bid = 15 if "bid" in doc_types else 0

    if "board_minutes" in doc_types:
        board = 20
    elif "transparency" in doc_types:
        board = 10
    else:
        board = 0

    readiness = {"In Pipeline": 20, "Cold": 12, "Whitespace": 4}.get(crm["status"], 4)
    if crm["contact"] is None and readiness > 2:
        readiness -= 2

    breakdown = {
        "competitor_pressure": competitor_pressure, "evidence": evidence,
        "bid": bid, "board_signal": board, "crm_readiness": readiness,
    }
    total = max(0, min(100, sum(breakdown.values())))
    return total, breakdown


def _focus_phrase(categories: list[str]) -> str:
    cats = " ".join(categories).lower()
    if "erp" in cats or "sis" in cats:
        return "attendance and early-alert data during the SIS/ERP migration"
    if "retention" in cats or "success" in cats:
        return "attendance and early-alert"
    if "attendance" in cats or "compliance" in cats:
        return "attendance-compliance reporting"
    return "student attendance and engagement"


def top_signal(account: dict) -> str:
    doc_types = set(account["doc_types"])
    if account["competitors"]:
        return "Competitor named: " + ", ".join(account["competitors"][:2])
    if "bid" in doc_types:
        return "Active/upcoming bid"
    if "federal_award" in doc_types:
        return "Federal student-success grant"
    if account["client_present"]:
        return "Existing vendor footprint (spend)"
    if "transparency" in doc_types:
        return "Spend footprint"
    if "board_minutes" in doc_types:
        return "Board/budget signal"
    return "Signal"


def incumbent(account: dict, cfg: OpsConfig) -> str:
    if account["client_present"]:
        return f"{cfg.client} (self)"
    if account["competitors"]:
        return ", ".join(account["competitors"][:3])
    return "-"


def opener(account: dict, crm: dict, cfg: OpsConfig) -> str:
    """A template opener grounded in the account's real signal - no invented
    numbers, contracts, or names beyond what's in the data."""
    state = (account["state"] or "").replace("_", " ").title()
    focus = _focus_phrase(account["categories"])
    doc_types = set(account["doc_types"])
    comp = account["competitors"][0] if account["competitors"] else None

    if account["client_present"]:
        return (f"{cfg.client} already shows up in {state} spending records for this system - "
                f"worth comparing notes on extending the same coverage to the rest of the campuses.")
    if comp and "board_minutes" in doc_types:
        return (f"Your board materials reference {comp} on student success - {cfg.client} covers the "
                f"{focus} layer those tools leave thin; happy to benchmark against what your campuses run.")
    if "bid" in doc_types:
        return (f"Saw an active procurement in the {state} records - {cfg.client} fits the {focus} "
                f"requirement; can share a quick comparison from similar campuses.")
    if "federal_award" in doc_types:
        return (f"Saw {account['institution']}'s recent federal student-success grant - that's usually "
                f"when {focus} tooling gets funded; {cfg.client} is purpose-built for it, worth a short compare?")
    if comp:
        return (f"{comp} is the incumbent to beat here - {cfg.client} tends to win on the {focus} gap; "
                f"open to a short benchmark?")
    return (f"Your {state} board/spend signals point to active work on {focus} - {cfg.client} is "
            f"purpose-built for that; worth a 20-minute compare?")


# --------------------------------------------------------------------------
# Step 3 - render + run.
# --------------------------------------------------------------------------

def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_report(rows: list[dict], cfg: OpsConfig) -> str:
    stamp = _now_stamp().replace("_", " ")
    lines = [
        f"# Full Motion - {cfg.client} account priority",
        f"_{len(rows)} in-scope account(s), scored on real signals + read-only CRM. Generated {stamp}._",
        "",
        "| Rank | Agency | Score | Top signal | Incumbent | CRM status | Contact | Opening line |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"| {i} | {_md_escape(r['agency'])} | {r['score']} | {_md_escape(r['top_signal'])} | "
            f"{_md_escape(r['incumbent'])} | {r['crm_status']} | {_md_escape(r['contact'])} | "
            f"{_md_escape(r['opening_line'])} |"
        )
    lines += ["", "## Top 3 plays"]
    for r in rows[:3]:
        lines.append(f"- **{r['agency']}** ({r['crm_status']}, score {r['score']}): {r['top_signal']}.")
    if not rows:
        lines.append("- (no in-scope accounts yet - run a scrape first)")
    lines += [
        "",
        "---",
        "_Score = competitor pressure (25) + spend/budget evidence (20) + active bid (15) + "
        "board/meeting signal (20) + CRM readiness (20). No contract-expiry factor: the tool has "
        "no expiry data, so competitor pressure stands in rather than fabricating one. Openers are "
        "template-generated from each account's real signal - personalize before sending._",
    ]
    return "\n".join(lines)


def hubspot_status(client=None) -> dict:
    """Is the read-only HubSpot path ready? Drives the button's gate."""
    if client is None:
        if hubspot_client.load_token() is None:
            return {"ok": False, "reason": (
                "No HubSpot token. Create a read-only Private App and put its token in "
                "config/hubspot.yaml (see config/hubspot.yaml.example) or set HUBSPOT_TOKEN."
            )}
        client = hubspot_client.HubSpotClient.from_config()
    return client.ping()


def run_full_motion_play(on_progress=None, cfg: OpsConfig | None = None,
                         conn=None, client=None) -> dict:
    """Run the play end to end. `on_progress(str)` streams log lines. Returns
    {ok, markdown, report_path, error}."""
    cfg = cfg or load_config()
    emit = on_progress or (lambda _msg: None)

    if client is None:
        client = hubspot_client.HubSpotClient.from_config()
    status = hubspot_status(client=client)
    if not status["ok"]:
        return {"ok": False, "markdown": "", "report_path": None, "error": status["reason"]}

    own_conn = conn is None
    conn = conn or db.get_conn()
    try:
        emit("Reading local signal database...")
        signals = gather_signals(conn)
    finally:
        if own_conn:
            conn.close()

    if not signals:
        return {"ok": False, "markdown": "", "report_path": None,
                "error": "No accounts with signals in the local DB yet - run a scrape first."}
    emit(f"{len(signals)} in-scope account(s). Cross-referencing HubSpot (read-only)...")

    rows = []
    for acc in signals:
        emit(f"  Looking up {acc['institution']}...")
        crm = crm_lookup(client, acc)
        score, _ = score_account(acc, crm)
        rows.append({
            "agency": acc["institution"], "score": score,
            "top_signal": top_signal(acc), "incumbent": incumbent(acc, cfg),
            "crm_status": crm["status"], "contact": _fmt_contact(crm["contact"]),
            "opening_line": opener(acc, crm, cfg),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)

    markdown = render_report(rows, cfg)
    report_path = _save_report(markdown)
    emit(f"Done. Saved to {report_path}")
    return {"ok": True, "markdown": markdown, "report_path": str(report_path), "error": None}


def _now_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _save_report(markdown: str) -> Path:
    OPS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = OPS_REPORTS_DIR / f"full_motion_{_now_stamp()}.md"
    path.write_text(markdown or "", encoding="utf-8")
    return path


def markdown_table_to_rows(markdown: str) -> list[list[str]]:
    """Extract the first GitHub pipe-table from `markdown` as rows (header
    first), dropping the `---|---` separator. Returns [] if there's no table."""
    rows: list[list[str]] = []
    for line in (markdown or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if rows:
                break                     # table block ended
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(c and set(c) <= set("-: ") for c in cells):
            continue                       # separator row
        rows.append(cells)
    return rows


def export_play_csv(markdown: str) -> Path:
    """Write the play's ranked table (parsed from its markdown) to a CSV next to
    the markdown reports. Returns the path."""
    import csv as _csv
    OPS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = OPS_REPORTS_DIR / f"full_motion_{_now_stamp()}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        _csv.writer(f).writerows(markdown_table_to_rows(markdown))
    return path
