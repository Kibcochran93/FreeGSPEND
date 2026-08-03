#!/usr/bin/env python3
"""
Offline tests for the generic public-feed adapter (RSS / Atom / JSON). Serves
canned feed bytes instead of the live URL. No network.
"""

from pathlib import Path

import yaml

from govspend_free import feeds, utils

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class FakeSession:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.calls = 0

    def get(self, url, timeout=None, headers=None, **kwargs):
        self.calls += 1
        return FakeResponse(self.content, self.status_code)


def _matchers():
    kw = yaml.safe_load((ROOT / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    return utils.build_category_matchers(kw["categories"])


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example University Bids</title>
  <item>
    <title>RFP 24-01 Academic Scheduling and Room Booking Software</title>
    <link>https://ex.edu/bids/2401</link>
    <guid>bid-2401</guid>
    <description>Seeking a cloud scheduling platform.</description>
    <pubDate>Mon, 29 Jun 2026 07:07:13 +0000</pubDate>
  </item>
  <item>
    <title>Grounds and Landscaping Services</title>
    <link>https://ex.edu/bids/2402</link>
    <guid>bid-2402</guid>
    <description>Mowing and landscaping.</description>
    <pubDate>Tue, 30 Jun 2026 00:00:00 +0000</pubDate>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Invitation to Bid: Timetabling Software Solution</title>
    <link href="https://ex.edu/atom/1"/>
    <id>atom-1</id>
    <summary>Cloud timetabling.</summary>
    <updated>2026-07-01T12:00:00Z</updated>
  </entry>
</feed>"""

JSON = b"""{"data": {"opportunities": [
  {"name": "RFP Student Attendance Monitoring System", "link": "https://ex.edu/j/1",
   "id": "j1", "summary": "attendance software", "closeDate": "2026-08-15", "refNumber": "ATT-01"},
  {"name": "Office Furniture Purchase", "link": "https://ex.edu/j/2", "id": "j2", "summary": "chairs"}
]}}"""


def test_rss_keeps_relevant_and_parses_fields():
    matchers = _matchers()
    seen: set[str] = set()
    src = {"type": "rss", "url": "https://ex.edu/rss", "format": "rss"}
    matches, skipped = feeds.scrape_feed_source(src, FakeSession(RSS), seen, matchers)
    assert not skipped, skipped
    assert len(matches) == 1, [m["title"] for m in matches]
    m = matches[0]
    assert "Academic Scheduling" in m["title"], m
    assert m["detail_url"] == "https://ex.edu/bids/2401", m
    assert m["date"] == "2026-06-29", m                       # RFC-822 pubDate -> ISO
    assert any("Scheduling" in c for c in m["categories"]), m
    # dedup
    again, _ = feeds.scrape_feed_source(src, FakeSession(RSS), seen, matchers)
    assert again == [], again


def test_atom_entry_and_link_href():
    matchers = _matchers()
    src = {"type": "atom", "url": "https://ex.edu/atom"}
    matches, skipped = feeds.scrape_feed_source(src, FakeSession(ATOM), set(), matchers)
    assert not skipped, skipped
    assert len(matches) == 1
    assert matches[0]["detail_url"] == "https://ex.edu/atom/1"
    assert matches[0]["date"] == "2026-07-01"


def test_json_feed_with_list_path_and_field_map():
    matchers = _matchers()
    src = {"type": "json_feed", "url": "https://ex.edu/api",
           "list_path": "data.opportunities",
           "fields": {"title": "name", "url": "link", "description": "summary",
                      "due": "closeDate", "number": "refNumber", "id": "id"}}
    matches, skipped = feeds.scrape_feed_source(src, FakeSession(JSON), set(), matchers)
    assert not skipped, skipped
    assert len(matches) == 1, [m["title"] for m in matches]
    m = matches[0]
    assert "Attendance" in m["title"]
    assert m["detail_url"] == "https://ex.edu/j/1"
    assert "closes 2026-08-15" in m["description"]            # due date folded into text
    assert any("Attendance" in c for c in m["categories"])


def test_http_error_is_skipped():
    matchers = _matchers()
    src = {"type": "rss", "url": "https://ex.edu/rss"}
    matches, skipped = feeds.scrape_feed_source(src, FakeSession(b"", status_code=503), set(), matchers)
    assert matches == []
    assert skipped and skipped[0]["reason"] == "feed_http_503", skipped


def test_bad_xml_is_reported_not_raised():
    matchers = _matchers()
    src = {"type": "rss", "url": "https://ex.edu/rss"}
    matches, skipped = feeds.scrape_feed_source(src, FakeSession(b"<not-xml"), set(), matchers)
    assert matches == []
    assert skipped and skipped[0]["reason"] == "feed_bad_xml", skipped


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
