"""
Shared helpers: HTTP fetching, dedup state, keyword matching, PDF text
extraction, and light logging. Kept dependency-light on purpose so the
whole project runs with just requirements.txt (no browser automation
required for the sources that don't need it).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

# Shared logger for the whole package. Modules log diagnostics through this
# (fetch errors, per-source progress, skip reasons); actual program *results*
# (the report path, the summary, search/opportunities output) stay on print()
# so `--quiet` can silence the chatter without hiding the answers.
log = logging.getLogger("govspend_free")


def setup_logging(verbosity: int = 0) -> None:
    """Configure the package logger. verbosity < 0 => WARNING (quiet),
    0 => INFO (default), > 0 => DEBUG (verbose)."""
    if verbosity < 0:
        level = logging.WARNING
    elif verbosity == 0:
        level = logging.INFO
    else:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(message)s")
    log.setLevel(level)

USER_AGENT = (
    "Mozilla/5.0 (compatible; GovspendFreeResearchBot/0.1; "
    "personal/non-commercial procurement research tool)"
)

DEFAULT_TIMEOUT = 25
POLITE_DELAY_SECONDS = 1.5  # be a decent citizen on small university sites

ROOT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT_DIR / "state"
REPORTS_DIR = ROOT_DIR / "reports"
CACHE_DIR = ROOT_DIR / "cache" / "pdfs"

for d in (STATE_DIR, REPORTS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

SEEN_FILE = STATE_DIR / "seen.json"


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch(url: str, session: requests.Session | None = None, **kwargs) -> requests.Response | None:
    """GET a URL with a polite delay and basic error handling.

    Returns None (and logs) on any failure instead of raising, so one bad
    source never kills the whole run. The polite delay runs even on failure
    (in `finally`) so a source that returns errors quickly still can't be
    hammered.
    """
    sess = session or requests
    try:
        resp = sess.get(url, timeout=DEFAULT_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.warning("  [fetch error] %s -> %s", url, exc)
        return None
    finally:
        time.sleep(POLITE_DELAY_SECONDS)


def soupify(resp: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(resp.text, "lxml")


def fetch_page_or_skip(
    source: dict, session: requests.Session, *, empty_shell_notes: str = ""
) -> tuple[BeautifulSoup | None, dict | None]:
    """Shared scraper preamble. Returns (soup, None) when the page fetched
    and parsed cleanly, or (None, skip_dict) when the source should be
    skipped. Collapses the js_rendered/form_post short-circuit, the fetch
    failure case, and empty-shell detection that all four scrapers repeat.

    Note: callers with an extra pre-fetch condition (e.g. the board-minutes
    {YEAR} URL-pattern check) should test that BEFORE calling this.
    """
    url = source["url"]
    src_type = source.get("type", "unknown")

    if src_type in ("js_rendered", "form_post"):
        reason = "needs_browser" if src_type == "js_rendered" else "needs_session"
        return None, {"url": url, "reason": reason, "notes": source.get("notes", "")}

    resp = fetch(url, session=session)
    if resp is None:
        return None, {"url": url, "reason": "fetch_failed", "notes": ""}

    soup = soupify(resp)
    if looks_like_empty_shell(soup):
        return None, {"url": url, "reason": "empty_shell_detected", "notes": empty_shell_notes}

    return soup, None


def looks_like_empty_shell(soup: BeautifulSoup, min_text_len: int = 400) -> bool:
    """Heuristic: JS-rendered SPAs usually leave almost no visible text in
    the raw HTML. If the visible text is suspiciously short, the source is
    probably js_rendered even if the config says otherwise (sites change).
    """
    text = soup.get_text(separator=" ", strip=True)
    return len(text) < min_text_len


# --------------------------------------------------------------------------
# Date parsing (best-effort) - used by the date-range scrape/dashboard filters
# --------------------------------------------------------------------------

import datetime as _dt

# Embedded numeric dates in filenames/link text, e.g. "2026-03-09-minutes.pdf"
# or "Board-Agenda-3-14-2026". Tried after a clean parse fails.
_YMD_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_MDY_RE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")


def parse_date(text: str | None) -> "_dt.date | None":
    """Best-effort date from arbitrary text (a link/filename, a CSV cell, or a
    scraped_at timestamp). Returns a date or None if nothing confident is found.
    Deliberately conservative (no fuzzy token-guessing) to avoid mis-dating."""
    if not text:
        return None
    s = str(text).strip()
    try:
        from dateutil import parser as _p
        return _p.parse(s, fuzzy=False).date()
    except (ValueError, OverflowError, TypeError):
        pass
    m = _YMD_RE.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return _dt.date(y, mo, d)
        except ValueError:
            return None
    m = _MDY_RE.search(s)
    if m:
        mo, d, y = (int(g) for g in m.groups())
        try:
            return _dt.date(y, mo, d)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Dedup state: a flat JSON set of hashes we've already reported, so re-runs
# only surface genuinely new items.
# --------------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def item_hash(*parts: str) -> str:
    joined = "||".join(p.strip() for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------

def build_category_matchers(categories_cfg: dict) -> list[tuple[str, str, list[re.Pattern]]]:
    """Turn keywords.yaml's `categories` block into (key, label, [compiled patterns])."""
    matchers = []
    for key, cfg in categories_cfg.items():
        patterns = [
            re.compile(re.escape(kw), re.IGNORECASE) for kw in cfg.get("keywords", [])
        ]
        matchers.append((key, cfg.get("label", key), patterns))
    return matchers


def match_categories(text: str, matchers: list[tuple[str, str, list[re.Pattern]]]) -> list[str]:
    """Return the list of category labels whose keywords appear in `text`."""
    hits = []
    for _key, label, patterns in matchers:
        if any(p.search(text) for p in patterns):
            hits.append(label)
    return hits


def build_watchlist_matchers(watchlist: Iterable[str]) -> list[tuple[str, re.Pattern]]:
    """Compile watchlist terms into (label, pattern) pairs.

    Matching is CASE-SENSITIVE and word-boundaried on purpose: watchlist
    terms are brand / vendor names (SEAtS, Ellucian, Coursedog), and case
    sensitivity is what stops the brand "SEAtS" from matching the ordinary
    word "seats". Word boundaries stop partial-word matches. (Category
    keywords stay loose and case-insensitive - see build_category_matchers.)

    Trade-off: an ALL-CAPS mention in a heading (e.g. "ELLUCIAN") won't match.
    Add that exact casing to the watchlist if you need to catch it.
    """
    return [(term, re.compile(r"\b" + re.escape(term) + r"\b")) for term in watchlist]


def match_watchlist(text: str, matchers: list[tuple[str, re.Pattern]]) -> list[str]:
    return [label for label, pattern in matchers if pattern.search(text)]


def snippet_around(text: str, pattern: re.Pattern, context_chars: int = 160) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    start = max(0, m.start() - context_chars)
    end = min(len(text), m.end() + context_chars)
    snippet = text[start:end].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return f"...{snippet}..."


# --------------------------------------------------------------------------
# Streaming download for large tabular files (transparency / contracts CSVs)
# --------------------------------------------------------------------------

# Cap how much of a single download we hold in memory for parsing. The hash
# still covers the WHOLE file (so any change anywhere re-triggers parsing),
# but state "open checkbook" CSVs can be hundreds of MB - we don't want to
# materialize all of that. A trailing partial row past the cap is tolerated
# by the CSV parsers, which already guard on row length.
MAX_PARSE_BYTES = 64 * 1024 * 1024  # 64 MB


def download_text_hashed(
    url: str, session: requests.Session, max_parse_bytes: int = MAX_PARSE_BYTES
) -> tuple[str | None, str | None]:
    """Stream a text download, returning (text_for_parsing, content_hash).

    `content_hash` is a sha256 over the entire file, so callers can dedup on
    *content* rather than URL - a CSV that lives at a stable URL but gets
    updated (new contracts, new rows) produces a new hash and is re-parsed,
    unlike URL-based dedup which would skip it forever after the first run.

    `text_for_parsing` is truncated to `max_parse_bytes` to bound memory.
    Returns (None, None) on fetch failure.
    """
    resp = fetch(url, session=session, stream=True)
    if resp is None:
        return None, None

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    parsed_bytes = 0
    try:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            hasher.update(chunk)
            if parsed_bytes < max_parse_bytes:
                take = chunk[: max_parse_bytes - parsed_bytes]
                chunks.append(take)
                parsed_bytes += len(take)
    except requests.RequestException as exc:
        log.warning("  [download error] %s -> %s", url, exc)
        return None, None

    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text, hasher.hexdigest()


def download_bytes(url: str, session: requests.Session, max_bytes: int = 200 * 1024 * 1024) -> bytes | None:
    """Download a binary file fully into memory (needed for zip-based .xlsx,
    which can't be parsed as a stream). Skips files larger than max_bytes.
    Returns None on failure/oversize."""
    resp = fetch(url, session=session, stream=True)
    if resp is None:
        return None
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                log.warning("  [download too large] %s exceeds %d bytes - skipping", url, max_bytes)
                return None
            chunks.append(chunk)
    except requests.RequestException as exc:
        log.warning("  [download error] %s -> %s", url, exc)
        return None
    return b"".join(chunks)


def iter_xlsx_rows(data: bytes, max_rows: int = 200_000):
    """Yield rows (list[str]) from the first worksheet of an .xlsx byte blob,
    using openpyxl in read-only mode so large files stream row-by-row. Yields
    nothing if openpyxl is missing or the file can't be parsed."""
    try:
        import openpyxl
    except ImportError:
        log.warning("  [warn] openpyxl not installed, skipping .xlsx "
                    "(pip install -r requirements.txt)")
        return
    import io
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several types on bad/other formats
        log.warning("  [xlsx parse error] %s", exc)
        return
    try:
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            yield ["" if c is None else str(c) for c in row]
    except Exception as exc:
        log.warning("  [xlsx read error] %s", exc)
    finally:
        wb.close()


# --------------------------------------------------------------------------
# PDF text extraction
# --------------------------------------------------------------------------

def download_pdf(url: str, session: requests.Session, dest: Path | None = None) -> Path | None:
    resp = fetch(url, session=session)
    if resp is None:
        return None
    if dest is None:
        dest = CACHE_DIR / f"{item_hash(url)}.pdf"
    try:
        dest.write_bytes(resp.content)
        return dest
    except OSError as exc:
        log.warning("  [pdf save error] %s -> %s", url, exc)
        return None


def extract_pdf_text(path: Path, max_pages: int = 80) -> str:
    try:
        import pdfplumber
    except ImportError:
        log.warning("  [warn] pdfplumber not installed, skipping PDF text extraction "
                    "(pip install -r requirements.txt)")
        return ""
    text_parts = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    break
                text_parts.append(page.extract_text() or "")
    except Exception as exc:  # pdfplumber can raise several different errors
        log.warning("  [pdf parse error] %s -> %s", path, exc)
        return ""
    return "\n".join(text_parts)


# --------------------------------------------------------------------------
# Generic link extraction helpers used by the bid + board-minutes scrapers
# --------------------------------------------------------------------------

def absolute_url(base_url: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def find_pdf_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Find <a> tags that look like they point at a PDF, returning
    {text, url} dicts. Used for board-minutes pages."""
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower():
            out.append({
                "text": a.get_text(strip=True),
                "url": absolute_url(base_url, href),
            })
    return out
