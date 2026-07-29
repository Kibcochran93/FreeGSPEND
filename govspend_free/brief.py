"""
Account-brief generator. Turns scraped board-minutes text into a SEAtS-style
account brief (Why now / Pain points / Buying committee / Competitor stack /
Deal window / Objection map) by handing the assembled context to the local
`claude` CLI (Claude Code).

This uses your existing Claude login - if ANTHROPIC_API_KEY is NOT set, the
`claude` CLI authenticates with your subscription, so this needs no separate
API key and isn't billed at metered API rates. (If ANTHROPIC_API_KEY *is* set
in the environment, the CLI will prefer it.)

Entry point wired into main.py:
    python main.py --brief 17                    # brief for one scraped doc id
    python main.py --brief "Lane Community College"   # brief across an institution
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess

from . import db, utils
from .pipeline import _slug
from .utils import log

GTM_PROFILE_PATH = utils.ROOT_DIR / "config" / "gtm_profile.md"
BRIEFS_DIR = utils.REPORTS_DIR / "briefs"
# None => let the `claude` CLI use the account's default model. (Forcing an
# alias like "sonnet" can 404 if that specific model isn't on the account's
# plan, so we don't pass --model unless the caller asks for one.)
DEFAULT_MODEL = None
MAX_CONTEXT_CHARS = 14000  # keep the prompt well under the CLI arg limit
CLAUDE_TIMEOUT = 180  # seconds; a real brief returns well under this

_AUTH_HINT = (
    "The `claude` CLI couldn't authenticate (its login token is missing or "
    "expired). Run `claude login` once in a normal terminal (outside this app) "
    "to sign in with your Claude subscription, then try again."
)

SYSTEM_PROMPT = (
    "You are a B2B sales researcher for SEAtS Software. Using ONLY the board-"
    "meeting material provided and the SEAtS GTM profile, write a concise, "
    "skimmable account brief in GitHub Markdown.\n"
    "Rules: ground every claim in the provided material; where the material "
    "does not say, write 'not evidenced in source' rather than guessing. Never "
    "invent names, numbers, dates, or events. Mark first-name-only or uncertain "
    "names with '(confirm)'. Do not use any tools - just write the brief from "
    "the text given.\n"
    "Use EXACTLY these sections, in this order:\n"
    "# Account Brief: <institution>\n"
    "## Why now\n"
    "## Top pain points (with evidence)\n"
    "## Buying committee\n"
    "## Competitor / incumbent stack\n"
    "## Deal window\n"
    "## Objection map\n"
    "Finish with a line starting 'Source:' listing the document id(s), title(s) "
    "and URL(s) you drew from."
)


def claude_available() -> bool:
    return shutil.which("claude") is not None


def _looks_like_auth_error(text: str) -> bool:
    low = text.lower()
    return ("oauth" in low and "invalid" in low) or "authentication_error" in low or "please run /login" in low


def _gather_context(conn, target: str) -> tuple[str, str, list[dict]]:
    """Return (institution_label, context_text, sources) for a doc id or an
    institution name. Raises ValueError if nothing is found."""
    target = str(target).strip()
    if target.isdigit():
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(target),)).fetchone()
        if row is None:
            raise ValueError(f"No document with id={target}. Use --search to find an id.")
        institution = row["institution"] or "Unknown institution"
        docs = [row]
    else:
        institution = target
        # Prioritize the highest-signal documents (most category/watchlist
        # matches) so the limited context budget is spent on the minutes that
        # actually mention retention/accreditation/etc., not administrative
        # agendas that happened to be scraped most recently.
        docs = conn.execute(
            "SELECT * FROM documents WHERE institution LIKE ? "
            "ORDER BY (LENGTH(categories) + LENGTH(watchlist_hits)) DESC, scraped_at DESC",
            (f"%{target}%",),
        ).fetchall()
        if not docs:
            raise ValueError(f"No scraped documents for an institution matching '{target}'.")
        institution = docs[0]["institution"] or target

    parts: list[str] = []
    sources: list[dict] = []
    used = 0
    for d in docs:
        text = (d["text"] or "").strip()
        if not text:
            continue
        header = f"\n\n=== DOCUMENT [{d['id']}] {d['doc_type']} | {d['title']} | {d['url']} ===\n"
        remaining = MAX_CONTEXT_CHARS - used
        if remaining <= len(header):
            break
        chunk = header + text[: remaining - len(header)]
        parts.append(chunk)
        used += len(chunk)
        sources.append({"id": d["id"], "title": d["title"], "url": d["url"]})
        if used >= MAX_CONTEXT_CHARS:
            break

    if not parts:
        raise ValueError(f"Found documents for '{institution}' but none had extractable text.")

    # Any contract rows on file for this institution are useful deal-window signal.
    contracts = conn.execute(
        "SELECT vendor, end_date, days_until_expiration FROM contracts WHERE institution LIKE ?",
        (f"%{institution}%",),
    ).fetchall()
    if contracts:
        lines = [f"- {c['vendor']} ends {c['end_date']} ({c['days_until_expiration']}d)" for c in contracts]
        parts.append("\n\n=== CONTRACTS ON FILE ===\n" + "\n".join(lines))

    return institution, "".join(parts), sources


def generate_brief(conn, target: str, *, model: str | None = DEFAULT_MODEL, write: bool = True) -> dict:
    """Generate an account brief for a doc id or institution via the `claude`
    CLI. Returns {institution, markdown, path, sources}. Raises ValueError for
    bad input and RuntimeError if the CLI is missing or the call fails."""
    if not claude_available():
        raise RuntimeError(
            "The `claude` CLI wasn't found on PATH. Install Claude Code "
            "(https://claude.com/claude-code) to use --brief, or generate the "
            "brief interactively instead."
        )

    gtm = GTM_PROFILE_PATH.read_text(encoding="utf-8") if GTM_PROFILE_PATH.exists() else ""
    institution, context_text, sources = _gather_context(conn, target)

    prompt = (
        f"SEAtS GTM PROFILE (your positioning lens):\n{gtm}\n\n"
        f"SCRAPED MATERIAL FOR: {institution}\n{context_text}\n\n"
        f"TASK: Write the account brief for {institution} following the required section structure."
    )

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--strict-mcp-config",  # ignore global MCP connectors: faster, and a
                                # pure text call doesn't need them
    ]
    if model:  # None => use the account's default model
        cmd += ["--model", model]
    log.info("Generating brief for %s via claude (model=%s)...", institution, model or "account default")
    try:
        proc = subprocess.run(
            cmd,
            input="",  # empty stdin so the CLI never blocks waiting for input
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(utils.ROOT_DIR),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Could not launch the `claude` CLI: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # A bad/expired login makes the CLI retry auth and hang until timeout,
        # so a timeout most often means auth, not a slow model.
        raise RuntimeError(
            f"`claude` didn't return within {CLAUDE_TIMEOUT}s. {_AUTH_HINT}"
        ) from exc

    stderr = (proc.stderr or "")
    if _looks_like_auth_error(stderr) or _looks_like_auth_error(proc.stdout or ""):
        raise RuntimeError(_AUTH_HINT)
    if proc.returncode != 0:
        raise RuntimeError(f"`claude` failed (exit {proc.returncode}): {stderr.strip()[:600]}")

    brief_md = (proc.stdout or "").strip()
    if not brief_md:
        raise RuntimeError(f"`claude` returned no output. {_AUTH_HINT}")

    path = None
    if write:
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = BRIEFS_DIR / f"{_slug(institution)}_{ts}.md"
        path.write_text(brief_md, encoding="utf-8")
        log.info("Brief written to %s", path)

    return {
        "institution": institution,
        "markdown": brief_md,
        "path": str(path) if path else None,
        "sources": sources,
    }
