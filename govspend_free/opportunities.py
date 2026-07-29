"""
Rule-based version of GovSpend's "Opportunities" module: pulls everything
out of the documents table (cumulative across all runs, not just today's)
and scores it so the most relevant, most timely items float to the top.

This is NOT an LLM ranking - it's transparent, inspectable arithmetic, on
purpose. If you enabled the optional LLM module (llm.py), `--ask` gives you
a natural-language layer on top of this same data; this module is the
free, deterministic baseline.

Score = keyword_strength + recency_bonus + type_bonus
  - keyword_strength: 10 points per matched category, 15 per watchlist hit
    (watchlist = your explicitly named vendors, so weighted higher than
    generic category keywords)
  - recency_bonus: up to 20 points, decaying linearly over 90 days since
    the document was first scraped (not the bid's own closing date, since
    that's not reliably parseable across every source's date format)
  - type_bonus: bids get +5 (they're the most actionable/time-boxed),
    board_minutes get +0, transparency gets +0
"""

from __future__ import annotations

import datetime as dt

from . import db

RECENCY_WINDOW_DAYS = 90
TYPE_BONUS = {"bid": 5, "board_minutes": 0, "transparency": 0}


def score_document(row) -> float:
    categories = [c for c in (row["categories"] or "").split(", ") if c]
    watchlist_hits = [w for w in (row["watchlist_hits"] or "").split(", ") if w]

    keyword_strength = len(categories) * 10 + len(watchlist_hits) * 15

    recency_bonus = 0.0
    scraped_at = row["scraped_at"]
    if scraped_at:
        try:
            # scraped_at is written by SQLite's datetime('now'), which is UTC
            # and naive (no offset). Compare against a naive UTC now.
            scraped_dt = dt.datetime.fromisoformat(scraped_at)
            age_days = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - scraped_dt).days
            recency_bonus = max(0.0, 20.0 * (1 - age_days / RECENCY_WINDOW_DAYS))
        except ValueError:
            pass

    type_bonus = TYPE_BONUS.get(row["doc_type"], 0)

    return round(keyword_strength + recency_bonus + type_bonus, 1)


def rank_opportunities(conn, limit: int = 50) -> list[dict]:
    rows = db.all_documents(conn)
    scored = []
    for row in rows:
        score = score_document(row)
        if score <= 0:
            continue
        scored.append({
            "score": score,
            "doc_type": row["doc_type"],
            "state": row["state"],
            "institution": row["institution"],
            "title": row["title"],
            "url": row["url"],
            "categories": row["categories"],
            "watchlist_hits": row["watchlist_hits"],
            "scraped_at": row["scraped_at"],
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


def print_opportunities(opportunities: list[dict], top_n: int = 15) -> None:
    if not opportunities:
        print("No scored opportunities yet - run a scrape first (python main.py).")
        return
    print(f"\nTop {min(top_n, len(opportunities))} opportunities (of {len(opportunities)} scored):")
    print("-" * 90)
    for i, opp in enumerate(opportunities[:top_n], start=1):
        tags = ", ".join(filter(None, [opp["categories"], opp["watchlist_hits"]]))
        print(f"{i:>2}. [{opp['score']:>5}] ({opp['doc_type']}) {opp['state']} / {opp['institution']}")
        print(f"      {opp['title']}")
        if tags:
            print(f"      tags: {tags}")
        print(f"      {opp['url']}")
