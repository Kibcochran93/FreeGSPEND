"""
Generic public RFP/bid FEED adapter - RSS / Atom / XML / JSON.

Many procurement portals and institutions publish their open solicitations as a
public feed. One reusable adapter lets FreeGSPEND wire ANY of them by URL, with
no per-portal scraping code - a force multiplier for coverage. (Ported from the
sibling rfp-monitor tool's public_feeds.py, adapted to the bid_scraper contract.)

Config: a `bid_boards` entry in sources.yaml, dispatched on `type: rss`:

    bid_boards:
      - type: rss                 # dispatch key (rss/atom/xml/json all use this)
        url: "https://example.edu/procurement/opportunities.rss"
        format: rss               # rss | atom | xml | json   (default: rss)
        # JSON feeds only - how to find and read the records:
        list_path: "data.opportunities"   # dotted path to the array of records
        fields: {title: name, url: link, date: closeDate,
                 description: summary, id: id, number: refNumber}

The institution and state are taken from the surrounding sources.yaml hierarchy
by the pipeline, so the feed source only needs the URL (+ format). Only entries
that match a SEAtS category (keywords.yaml) are kept, same as every other bid
source. Verify a feed is a real, public, institution-owned procurement feed
before wiring it (see rfp-monitor's SOURCE-EXPANSION.md).
"""

from __future__ import annotations

import json as _json
import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

from . import utils

_ENTRY_TAGS = {"item", "entry"}   # RSS <item>, Atom <entry>


class _FeedError(Exception):
    def __init__(self, reason: str, notes: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.notes = notes


def scrape_feed_source(source: dict, session, seen: set[str], matchers) -> tuple[list[dict], list[dict]]:
    """Pull one public feed, keep the SEAtS-relevant entries. Returns
    (new_matches, skipped) in the shared bid-source shape."""
    url = source.get("url", "")
    # `format` (if given) wins; otherwise infer JSON from the dispatch type,
    # else default to the RSS/Atom/XML family.
    fmt = str(source.get("format") or ("json" if source.get("type") == "json_feed" else "rss")).lower()
    if not url:
        return [], [{"url": url, "reason": "feed_misconfigured", "notes": "feed source needs a url"}]

    sess = session or utils.get_session()
    try:
        resp = sess.get(url, timeout=utils.DEFAULT_TIMEOUT, headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                      "text/xml, application/json",
        })
    except requests.RequestException as exc:
        return [], [{"url": url, "reason": "feed_fetch_failed", "notes": str(exc)[:200]}]
    finally:
        time.sleep(utils.POLITE_DELAY_SECONDS)   # 0 during tests

    status = getattr(resp, "status_code", 200)
    if status >= 400:
        return [], [{"url": url, "reason": f"feed_http_{status}", "notes": "feed request failed"}]

    raw = getattr(resp, "content", b"") or b""
    try:
        if fmt in ("rss", "atom", "xml"):
            entries = _parse_xml(raw)
        elif fmt == "json":
            entries = _parse_json(raw, source)
        else:
            raise _FeedError("feed_bad_format", f"unsupported feed format {fmt!r}")
    except _FeedError as exc:
        return [], [{"url": url, "reason": exc.reason, "notes": exc.notes}]

    matches: list[dict] = []
    for e in entries:
        title = e.get("title", "").strip()
        if not title:
            continue
        due = e.get("due", "")
        blob = " ".join(p for p in (
            title, e.get("description", ""), e.get("number", ""),
            f"closes {due}" if due else "",
        ) if p)
        categories = utils.match_categories(blob, matchers)
        if not categories:
            continue

        h = utils.item_hash("feed", url, e.get("id") or title, e.get("detail_url", ""))
        if h in seen:
            continue
        seen.add(h)

        # Store the due date in the description so it's searchable, but use the
        # posted date as the doc date (what the Opportunities feed ages on).
        desc = e.get("description", "")
        if due:
            desc = (desc + f"  (closes {due})").strip()
        matches.append({
            "source_url": url,
            "title": title,
            "description": desc,
            "detail_url": e.get("detail_url", ""),
            "date": e.get("posted", "") or due,
            "categories": categories,
        })

    return matches, []


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _parse_xml(raw: bytes) -> list[dict]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise _FeedError("feed_bad_xml", f"unreadable XML feed: {exc}") from exc
    entries = [el for el in root.iter() if _local(el.tag) in _ENTRY_TAGS]
    out: list[dict] = []
    for entry in entries:
        title = _xml_text(entry, "title")
        if not title:
            continue
        out.append({
            "title": title,
            "description": _xml_text(entry, "description", "summary", "content"),
            "detail_url": _xml_link(entry),
            "id": _xml_text(entry, "guid", "id"),
            "number": _xml_text(entry, "solicitationnumber", "referencenumber", "bidnumber"),
            "posted": _norm_date(_xml_text(entry, "pubdate", "published", "updated", "issued", "date")),
            "due": _norm_date(_xml_text(entry, "duedate", "closedate", "closingdate", "deadline")),
        })
    return out


def _parse_json(raw: bytes, source: dict) -> list[dict]:
    try:
        payload = _json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise _FeedError("feed_bad_json", f"unreadable JSON feed: {exc}") from exc
    list_path = str(source.get("list_path") or "")
    records = _path(payload, list_path) if list_path else payload
    if isinstance(records, dict):
        records = list(records.values())
    if not isinstance(records, list):
        raise _FeedError("feed_bad_json", "list_path did not resolve to an array of records")

    fields = source.get("fields") or {}

    def field(record: dict, name: str) -> str:
        return str(_path(record, fields.get(name, name)) or "").strip()

    out: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = field(record, "title")
        if not title:
            continue
        out.append({
            "title": title,
            "description": field(record, "description"),
            "detail_url": field(record, "url"),
            "id": field(record, "id"),
            "number": field(record, "number"),
            "posted": _norm_date(field(record, "date") or field(record, "posted")),
            "due": _norm_date(field(record, "due")),
        })
    return out


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Strip an XML namespace: '{http://...}item' -> 'item'."""
    return tag.rsplit("}", 1)[-1].lower()


def _xml_text(entry: ElementTree.Element, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for el in entry.iter():
        if _local(el.tag) in wanted and el.text and el.text.strip():
            return " ".join(el.text.split())
    return ""


def _xml_link(entry: ElementTree.Element) -> str:
    for el in entry.iter():
        if _local(el.tag) != "link":
            continue
        if el.attrib.get("href"):
            return el.attrib["href"].strip()
        if el.text and el.text.strip():
            return el.text.strip()
    return ""


def _path(value: object, path: str) -> object:
    current = value
    for part in (p for p in path.split(".") if p):
        if not isinstance(current, dict):
            return ""
        current = current.get(part, "")
    return current


def _norm_date(text: str) -> str:
    """Best-effort -> 'YYYY-MM-DD'. Handles RFC-822 (RSS pubDate), ISO, and
    M/D/Y; returns '' if nothing parses (never raises)."""
    s = (text or "").strip()
    if not s:
        return ""
    try:                                    # RSS pubDate: "Mon, 29 Jun 2026 07:07:13 +0000"
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    d = utils.parse_date(s)                  # ISO 'YYYY-MM-DD' or 'M/D/YYYY'
    return d.isoformat() if d else ""
