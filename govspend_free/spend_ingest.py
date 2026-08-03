"""
SODA (Socrata) payments ingest -> the normalized `payments` table.

SEAtS-targeted and closed-world: for each state checkbook on Socrata, query the
vendor column for our KNOWN vendors (competitors + client, from the normalizer),
run every candidate row back through the normalizer (so 'VIVID SEATS' is dropped
even though the SoQL `like` matched it), and store the resolved payments.

Config lives in sources.yaml `type: socrata` entries with column mappings:
    domain, dataset, vendor_col, amount_col, date_col, agency_col
See `python main.py --ingest-spend`. Verified live for MA / CT / DE / MD.
"""

from __future__ import annotations

import re

from . import db, utils
from .utils import log

ROWS_PER_TERM = 500  # Socrata page cap per vendor term per dataset


def _parse_amount(value) -> float | None:
    if value is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(s) if s not in ("", "-", ".", "-.") else None
    except ValueError:
        return None


def _clean_date(value) -> str:
    return str(value or "").split("T", 1)[0]   # '2026-07-14T00:00:00.000' -> '2026-07-14'


def ingest_socrata_payments(source: dict, session, normalizer, seen: set[str]) -> tuple[list[dict], list[dict]]:
    """Pull resolved payments from one Socrata checkbook. Returns (payments,
    skipped) - payment dicts ready for db.insert_payment."""
    domain, dataset, vcol = source.get("domain"), source.get("dataset"), source.get("vendor_col")
    if not (domain and dataset and vcol):
        return [], [{"reason": "socrata_payments_misconfigured",
                     "notes": "needs domain, dataset and vendor_col", "url": source.get("url", "")}]

    acol, dcol, gcol = source.get("amount_col"), source.get("date_col"), source.get("agency_col")
    ccol = source.get("category_col")
    api = f"https://{domain}/resource/{dataset}.json"
    token = {"$$app_token": source["app_token"]} if source.get("app_token") else {}
    select = ",".join([c for c in (vcol, acol, dcol, gcol, ccol) if c])

    payments: list[dict] = []
    for term in normalizer.search_terms:
        safe = term.replace("'", "''")
        params = {"$where": f"upper({vcol}) like upper('%{safe}%')",
                  "$select": select, "$limit": ROWS_PER_TERM, **token}
        # utils.fetch already sets timeout + polite delay; a slow term just
        # returns None and is skipped (re-run fills the gap - ingest is idempotent).
        resp = utils.fetch(api, session=session, params=params)
        if resp is None:
            continue
        try:
            rows = resp.json()
        except ValueError:
            continue

        for row in rows:
            vendor_raw = str(row.get(vcol, "") or "").strip()
            canonical, kind = normalizer.vendor(vendor_raw)
            if kind == "unknown":
                continue   # closed-world: SoQL matched, but it isn't really one of ours
            amount = _parse_amount(row.get(acol)) if acol else None
            paid_date = _clean_date(row.get(dcol)) if dcol else ""
            agency_raw = str(row.get(gcol, "") or "").strip() if gcol else ""
            category_raw = str(row.get(ccol, "") or "").strip() if ccol else ""
            ref = f"{dataset}:" + utils.item_hash(vendor_raw, str(amount), paid_date, agency_raw)
            if ref in seen:
                continue
            seen.add(ref)
            payments.append({
                "ref": ref, "source_url": f"https://{domain}/d/{dataset}",
                "vendor_raw": vendor_raw, "vendor_canonical": canonical, "vendor_kind": kind,
                "amount": amount, "paid_date": paid_date,
                "agency_raw": agency_raw,
                "agency_canonical": normalizer.agency(agency_raw) if agency_raw else "",
                "category_code_raw": category_raw,
                "category_canonical": normalizer.category(category_raw),
            })
    return payments, []


def ingest(conn, sources: dict, *, normalizer=None, session=None,
           selected_state: "str | list[str] | None" = None) -> dict:
    """Ingest every Socrata checkbook (with column mappings) into `payments`.
    Returns stats. Idempotent across runs via the payments.ref UNIQUE key.

    `selected_state` may be None (all), a single key, or a list of keys."""
    from .normalize import Normalizer
    normalizer = normalizer or Normalizer.from_config()
    session = session or utils.get_session()
    seen: set[str] = set()

    if selected_state is None:
        states = list(sources.keys())
    elif isinstance(selected_state, str):
        states = [selected_state]
    else:
        states = list(selected_state)
    stats = {"sources": 0, "resolved": 0, "inserted": 0, "by_state": {}}
    for state_key in states:
        for src in (sources.get(state_key, {}) or {}).get("transparency", []):
            if src.get("type") != "socrata" or not src.get("vendor_col"):
                continue
            log.info("  [spend] %s -> %s/%s", state_key, src.get("domain"), src.get("dataset"))
            payments, _ = ingest_socrata_payments(src, session, normalizer, seen)
            stats["sources"] += 1
            stats["resolved"] += len(payments)
            inserted = 0
            for p in payments:
                rid = db.insert_payment(
                    conn, ref=p["ref"], state=state_key, source="socrata",
                    source_url=p["source_url"], agency_raw=p["agency_raw"],
                    agency_canonical=p["agency_canonical"], vendor_raw=p["vendor_raw"],
                    vendor_canonical=p["vendor_canonical"], vendor_kind=p["vendor_kind"],
                    amount=p["amount"], paid_date=p["paid_date"],
                    category_code_raw=p["category_code_raw"], category_canonical=p["category_canonical"],
                )
                if rid is not None:
                    inserted += 1
            stats["by_state"][state_key] = stats["by_state"].get(state_key, 0) + inserted
            stats["inserted"] += inserted
    return stats
