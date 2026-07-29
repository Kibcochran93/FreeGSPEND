"""
Optional AI layer, replicating GovSpend's "AI Search + Notebook" and
"Record-Level Chat" features. Fully opt-in: nothing else in this project
imports this module or requires an API key. You pay standard Anthropic API
rates for whatever you run through here - this is not free, unlike the
rest of the tool.

Requires:
  pip install anthropic
  export ANTHROPIC_API_KEY=sk-ant-...   (or put it in config/llm.yaml)

Two entry points, both wired into main.py:
  --ask "question"      AI Search: full-text-search your local DB for the
                         most relevant documents, then ask Claude to answer
                         your question using only that retrieved context
                         (basic RAG, not a raw model guess).
  --chat <document_id>  Record-Level Chat: load one document's full text
                         and drop into a REPL where you can ask follow-up
                         questions about just that record.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from . import db, utils

CONFIG_PATH = utils.ROOT_DIR / "config" / "llm.yaml"
DEFAULT_MODEL = "claude-sonnet-4-5"


def _get_api_key() -> str | None:
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if cfg.get("api_key"):
            return cfg["api_key"]
    return os.environ.get("ANTHROPIC_API_KEY")


def _get_client():
    try:
        import anthropic
    except ImportError:
        print("The AI features need the `anthropic` package: pip install anthropic")
        return None

    api_key = _get_api_key()
    if not api_key:
        print("No Anthropic API key found. Set ANTHROPIC_API_KEY, or put "
              "`api_key: sk-ant-...` in config/llm.yaml (copy config/llm.yaml.example).")
        return None

    return anthropic.Anthropic(api_key=api_key)


def ai_search(query: str, conn, top_k: int = 10) -> None:
    """AI Search: retrieve top_k matching documents from the local FTS
    index, then have Claude answer the question using only that context.
    Prints the answer and the sources it drew from.
    """
    client = _get_client()
    if client is None:
        return

    rows = db.search(conn, query, limit=top_k)
    if not rows:
        print(f"No local documents matched '{query}' - try `python main.py --search \"{query}\"` "
              "first to see what's actually indexed, or broaden the query.")
        return

    context_blocks = []
    for r in rows:
        context_blocks.append(
            f"[doc_id={r['id']} type={r['doc_type']} state={r['state']} institution={r['institution']}]\n"
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}\n"
        )
    context = "\n---\n".join(context_blocks)

    system = (
        "You are answering questions about US higher-education procurement "
        "data (bids, board meeting minutes, spending records) that the user "
        "scraped themselves with a personal tool. Answer ONLY using the "
        "provided context blocks below - do not use outside knowledge about "
        "these institutions. If the context doesn't answer the question, say "
        "so plainly rather than guessing. Cite doc_id and URL for any claim."
    )

    message = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {query}",
        }],
    )

    print("\n" + "=" * 70)
    print("AI SEARCH ANSWER")
    print("=" * 70)
    for block in message.content:
        if hasattr(block, "text"):
            print(block.text)
    print("\nSources consulted:")
    for r in rows:
        print(f"  [{r['id']}] {r['title']} - {r['url']}")


def chat_with_record(document_id: int, conn) -> None:
    """Record-Level Chat: a simple REPL over one document's full text."""
    client = _get_client()
    if client is None:
        return

    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        print(f"No document with id={document_id}. Use --search first to find an id.")
        return

    system = (
        f"You are answering questions about ONE specific document scraped from "
        f"a US higher-education procurement source. Title: {row['title']}. "
        f"URL: {row['url']}. Institution: {row['institution']}. Answer only "
        f"using the document text provided - say so if it doesn't contain the answer."
    )
    doc_context = f"DOCUMENT TEXT:\n{(row['text'] or '')[:12000]}"  # keep it under a reasonable token budget

    print(f"\nChatting about doc {document_id}: {row['title']}")
    print("Type your question, or 'quit' to exit.\n")

    history = []
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question or question.lower() in ("quit", "exit"):
            break

        history.append({"role": "user", "content": question})
        message = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1024,
            system=system + "\n\n" + doc_context,
            messages=history,
        )
        answer_text = "".join(b.text for b in message.content if hasattr(b, "text"))
        print(answer_text + "\n")
        history.append({"role": "assistant", "content": answer_text})
