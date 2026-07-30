"""
Spending normalization - the hard part, done the tractable way.

Open-world entity resolution (normalize *every* vendor/agency across 50 states)
is the OpenTheBooks/GovSpend build. SEAtS doesn't need that: it needs to know
whether a payment went to a **tracked competitor**, to **SEAtS itself**, or to a
**higher-ed institution** we care about. That collapses the problem into a
CLOSED-WORLD match against a known set - deterministic, inspectable, testable.

The motivating real example (Connecticut checkbook): the case-insensitive
watchlist match tags both `SEATS SOFTWARE LIMITED` (really SEAtS) and
`VIVID SEATS *HARTFORD` (a ticket reseller) as "SEAtS". The normalizer must
resolve the first to the client and REJECT the second.

Config: config/normalize.yaml (see .example). Competitor set is seeded from the
keywords.yaml watchlist; client aliases + ambiguous terms + agency/category
crosswalks come from normalize.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import db, utils

CONFIG_PATH = utils.ROOT_DIR / "config" / "normalize.yaml"
KEYWORDS_PATH = utils.ROOT_DIR / "config" / "keywords.yaml"

# Trailing tokens stripped when canonicalizing a company name.
_LEGAL_SUFFIXES = {
    "LIMITED", "LTD", "LLC", "LLP", "LP", "INC", "INCORPORATED", "CORP",
    "CORPORATION", "CO", "COMPANY", "PLC", "GMBH", "NV", "SA", "AG",
}
# Recognizes a higher-ed institution as the *agency* being paid (or a recipient).
_INSTITUTION_RE = re.compile(
    r"universit|college|institut|regents|curators|polytechnic|\bstate u\b", re.IGNORECASE)

# Default client + competitor grouping. Overridable in config/normalize.yaml.
_DEFAULT_CLIENT_NAME = "SEAtS Software"
_DEFAULT_CLIENT_ALIASES = ["SEAtS", "SEAtS Software", "SEAtS ONE", "SEATS SOFTWARE",
                           "SEATS SOFTWARE LIMITED"]
_DEFAULT_AMBIGUOUS = ["SEATS"]  # match only exactly - never as a prefix (kills 'VIVID SEATS')
_DEFAULT_COMPETITOR_ALIASES = {
    "EAB Navigate": "EAB", "Civitas Learning": "Civitas",
    "Watermark Insights": "Watermark", "Workday Student": "Workday",
}


def _canon(name: str | None) -> str:
    """Canonical key: uppercase, punctuation->space, trailing legal suffixes
    dropped, whitespace collapsed. 'SEATS Software, Ltd.' -> 'SEATS SOFTWARE'."""
    tokens = re.sub(r"[^A-Za-z0-9 ]+", " ", (name or "").upper()).split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


class Normalizer:
    def __init__(self, client_name: str, client_aliases: list[str],
                 competitors: dict[str, str], ambiguous: list[str] | None = None,
                 agency_aliases: dict | None = None, category_crosswalk: dict | None = None):
        self._ambiguous = {_canon(t) for t in (ambiguous or [])}
        self._agency_aliases = {_canon(k): v for k, v in (agency_aliases or {}).items()}
        self._category_crosswalk = {str(k).strip().upper(): v for k, v in (category_crosswalk or {}).items()}

        # entry canon -> (display, kind). `competitors` maps alias -> canonical.
        self._exact: dict[str, tuple[str, str]] = {}
        for alias in client_aliases:
            self._exact.setdefault(_canon(alias), (client_name, "client"))
        for alias, canonical in competitors.items():
            self._exact.setdefault(_canon(alias), (canonical, "competitor"))
        # Prefix-matchable entries = everything except the ambiguous short ones.
        self._prefix = [(c, disp, kind) for c, (disp, kind) in self._exact.items()
                        if c not in self._ambiguous]

        # Raw terms to query a checkbook's vendor column for (one per canonical
        # form; ambiguous short tokens like "SEATS" are dropped so we don't pull
        # a flood of "VIVID SEATS" noise - the specific "SEATS SOFTWARE" stays).
        self.search_terms: list[str] = []
        _seen_canon: set[str] = set()
        for alias in list(client_aliases) + list(competitors.keys()):
            c = _canon(alias)
            if not c or c in self._ambiguous or c in _seen_canon:
                continue
            _seen_canon.add(c)
            self.search_terms.append(alias)

    # ------------------------------ vendor ------------------------------

    def vendor(self, raw: str) -> tuple[str | None, str]:
        """Resolve a raw vendor string to (canonical, kind). kind is
        'client' | 'competitor' | 'institution' | 'unknown'. Only exact
        canonical matches (plus word-boundary prefixes for non-ambiguous
        entries) count - so 'VIVID SEATS' does NOT resolve to SEAtS."""
        vc = _canon(raw)
        if not vc:
            return None, "unknown"
        if vc in self._exact:
            return self._exact[vc]
        for entry_canon, disp, kind in self._prefix:
            if vc == entry_canon or vc.startswith(entry_canon + " "):
                return disp, kind
        if _INSTITUTION_RE.search(raw or ""):
            return _titlecase(raw), "institution"
        return None, "unknown"

    # ------------------------------ agency ------------------------------

    def agency(self, raw: str) -> str:
        aliased = self._agency_aliases.get(_canon(raw))
        return aliased or _titlecase(raw)

    def is_institution_agency(self, raw: str) -> bool:
        return bool(_INSTITUTION_RE.search(raw or ""))

    # ------------------------------ category ------------------------------

    def category(self, code_or_label: str | None) -> str | None:
        if not code_or_label:
            return None
        return self._category_crosswalk.get(str(code_or_label).strip().upper())

    # ------------------------------ factory ------------------------------

    @classmethod
    def from_config(cls, config_path: Path = CONFIG_PATH, keywords_path: Path = KEYWORDS_PATH) -> "Normalizer":
        cfg = {}
        if config_path.exists():
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        client_name = cfg.get("client_name", _DEFAULT_CLIENT_NAME)
        client_aliases = cfg.get("client_aliases", _DEFAULT_CLIENT_ALIASES)
        ambiguous = cfg.get("ambiguous_terms", _DEFAULT_AMBIGUOUS)
        comp_alias_map = {**_DEFAULT_COMPETITOR_ALIASES, **(cfg.get("competitor_aliases") or {})}

        # Seed competitors from the keywords.yaml watchlist (minus the client),
        # each mapped to its canonical name (via comp_alias_map, else itself).
        watchlist = []
        if keywords_path.exists():
            kw = yaml.safe_load(keywords_path.read_text(encoding="utf-8")) or {}
            watchlist = kw.get("watchlist", []) or []
        client_keys = {_canon(a) for a in client_aliases}
        competitors: dict[str, str] = {}
        for name in watchlist:
            if _canon(name) in client_keys:
                continue
            competitors[name] = comp_alias_map.get(name, name)

        return cls(client_name, client_aliases, competitors, ambiguous,
                   cfg.get("agency_aliases"), cfg.get("category_crosswalk"))


def _titlecase(name: str) -> str:
    small = {"of", "the", "and", "for", "at", "in", "on", "to", "a", "an"}
    out = []
    for i, w in enumerate(( name or "").split()):
        if "-" in w:
            out.append("-".join(p.capitalize() for p in w.split("-")))
        elif i != 0 and w.lower() in small:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def parse_vendor_from_row(text: str) -> str:
    """The stored transparency/checkbook row leads with the vendor, then ' | '
    then the rest ('SEATS SOFTWARE LIMITED | BORAA... EDUCATION ...')."""
    return (text or "").split("|", 1)[0].strip()


# --------------------------------------------------------------------------
# Backfill: normalize the state-checkbook rows already in `documents` into the
# `payments` table. Proves the normalizer on the CT/DE/NY data we already have.
# --------------------------------------------------------------------------

def backfill_payments_from_documents(conn, normalizer: Normalizer | None = None) -> dict:
    """Read stored transparency documents, resolve each row's vendor, and write
    the ones that resolve to a competitor / the client / an institution into the
    payments table (SEAtS-targeted: unknown vendors are counted but not stored).
    Idempotent via the `ref` dedup key. Returns stats."""
    normalizer = normalizer or Normalizer.from_config()
    rows = conn.execute(
        "SELECT id, state, source, url, title, text FROM documents WHERE doc_type='transparency'"
    ).fetchall()

    stats = {"scanned": len(rows), "client": 0, "competitor": 0, "institution": 0,
             "unknown": 0, "inserted": 0}
    for r in rows:
        vendor_raw = parse_vendor_from_row(r["text"] or r["title"] or "")
        canonical, kind = normalizer.vendor(vendor_raw)
        stats[kind] = stats.get(kind, 0) + 1
        if kind == "unknown":
            continue  # SEAtS-targeted: don't store unresolved vendors
        inserted = db.insert_payment(
            conn, ref=f"doc:{r['id']}", state=r["state"] or "",
            source=r["source"] or "socrata", source_url=r["url"] or "",
            vendor_raw=vendor_raw, vendor_canonical=canonical, vendor_kind=kind,
        )
        if inserted is not None:
            stats["inserted"] += 1
    return stats
