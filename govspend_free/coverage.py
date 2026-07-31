"""
National coverage scorecard - where does FreeGSPEND actually reach across all 50
states? The tool's goal is nationwide coverage, so this is the meta-tool that
measures progress and shows the gaps.

It reconciles two things per state:
  - CONFIGURED: education-buyer sources declared in `config/sources.yaml`
    (a state's `university_systems` and their `bid_boards` / `board_minutes`).
  - EVIDENCE: education documents actually in the DB (doc_type bid / board_minutes),
    which proves a configured source really produces data.

Status (education coverage - USAspending federal grants are nationwide and do
NOT count as state education coverage, matching the rfp-monitor definition):
  missing      - no education source configured for the state
  configured   - source(s) configured, but no education docs in the DB yet
  represented  - >=1 education institution has produced docs
  covered      - >=2 distinct education institutions have produced docs

Design note: rfp-monitor tracks per-source *poll* health; FreeGSPEND doesn't yet,
so "represented" here is evidenced by stored documents. A source that polls fine
but produced no category-matching docs (e.g. a Bonfire portal with no SEAtS-
relevant RFP open right now) shows as "configured". Adding source-poll health
tracking later would distinguish "working, no match yet" from "never polled".
"""

from __future__ import annotations

from dataclasses import dataclass

# All 50 states. `key` (the sources.yaml top-level key + the DB `state` value) is
# derived as name.lower() with spaces -> underscores, e.g. "North Carolina" ->
# "north_carolina" - the convention sources.yaml already uses.
US_STATES: list[tuple[str, str]] = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
]

# Documents that count as "education buyer" coverage (federal grants + spending
# transparency are tracked separately - they're not education-RFP sources).
EDUCATION_DOC_TYPES = ("bid", "board_minutes")


def state_key(name: str) -> str:
    """sources.yaml / DB key for a state name ('New Jersey' -> 'new_jersey')."""
    return name.lower().replace(" ", "_")


@dataclass(frozen=True)
class StateCoverage:
    abbr: str
    name: str
    configured_sources: int          # education bid_boards + board_minutes in sources.yaml
    families: tuple[str, ...]        # distinct source `type`s configured (bonfire, html_table, ...)
    institutions_with_docs: int      # distinct education institutions with docs in the DB
    education_docs: int              # count of bid + board_minutes docs
    last_doc_at: str                 # most recent scraped_at among those docs ("" if none)
    has_federal: bool                # USAspending federal-grants pass configured

    @property
    def represented(self) -> bool:
        return self.institutions_with_docs >= 1

    @property
    def covered(self) -> bool:
        return self.institutions_with_docs >= 2

    @property
    def status(self) -> str:
        if self.covered:
            return "covered"
        if self.represented:
            return "represented"
        if self.configured_sources:
            return "configured"
        return "missing"


def _configured_for_state(cfg: dict) -> tuple[int, tuple[str, ...], bool]:
    """(education-source count, distinct families, has_federal) from a state's
    sources.yaml block."""
    count = 0
    families: set[str] = set()
    for system in (cfg.get("university_systems") or []):
        for key in ("bid_boards", "board_minutes"):
            for src in (system.get(key) or []):
                count += 1
                families.add(str(src.get("type", "unknown")))
    return count, tuple(sorted(families)), bool(cfg.get("federal_grants"))


def _evidence_by_state(conn) -> dict[str, tuple[int, int, str]]:
    """{state_key: (distinct_institutions, doc_count, last_scraped_at)} for
    education docs already in the DB."""
    placeholders = ",".join("?" for _ in EDUCATION_DOC_TYPES)
    rows = conn.execute(
        f"SELECT state, COUNT(DISTINCT institution) AS insts, COUNT(*) AS n, "
        f"MAX(scraped_at) AS last FROM documents "
        f"WHERE doc_type IN ({placeholders}) AND state IS NOT NULL AND state != '' "
        f"GROUP BY state",
        EDUCATION_DOC_TYPES,
    ).fetchall()
    return {r["state"]: (r["insts"], r["n"], r["last"] or "") for r in rows}


def build_coverage(conn, sources: dict) -> list[StateCoverage]:
    """Per-state coverage across all 50 states, reconciling configured sources
    (sources.yaml) with document evidence (the DB)."""
    evidence = _evidence_by_state(conn)
    result: list[StateCoverage] = []
    for abbr, name in US_STATES:
        key = state_key(name)
        cfg = sources.get(key, {}) or {}
        configured, families, has_federal = _configured_for_state(cfg)
        insts, docs, last = evidence.get(key, (0, 0, ""))
        result.append(StateCoverage(
            abbr=abbr, name=name, configured_sources=configured, families=families,
            institutions_with_docs=insts, education_docs=docs, last_doc_at=last,
            has_federal=has_federal,
        ))
    return result


def summarize(rows: list[StateCoverage]) -> dict:
    """National scorecard totals + the state lists behind them."""
    covered = [r.abbr for r in rows if r.status == "covered"]
    represented = [r.abbr for r in rows if r.status == "represented"]
    configured = [r.abbr for r in rows if r.status == "configured"]
    missing = [r.abbr for r in rows if r.status == "missing"]
    return {
        "total": len(rows),
        "covered": covered,
        "represented": represented,          # represented-but-not-covered
        "configured": configured,            # configured-but-no-docs
        "missing": missing,
        # "any representation" = covered + represented (>=1 institution with docs)
        "represented_or_better": sorted(covered + represented),
        "configured_or_better": sorted(covered + represented + configured),
        "with_federal": [r.abbr for r in rows if r.has_federal],
    }


def write_coverage_csv(rows: list[StateCoverage], path) -> None:
    import csv
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "abbr", "state", "status", "configured_sources", "families",
            "institutions_with_docs", "education_docs", "last_doc_at", "has_federal",
        ))
        for r in rows:
            writer.writerow((
                r.abbr, r.name, r.status, r.configured_sources, "|".join(r.families),
                r.institutions_with_docs, r.education_docs, r.last_doc_at,
                "yes" if r.has_federal else "no",
            ))
