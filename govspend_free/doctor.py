"""
Setup doctor - `python main.py --doctor`.

A quick, read-only "what's configured and working here" report: optional
dependencies, which config files exist (and whether their secrets look real vs.
placeholder - without ever printing the secret value), the `claude` CLI, and
what's actually in the local database. Handy on a fresh machine, or when a pass
unexpectedly gets skipped and you're not sure what's wired up.

Nothing here mutates anything. It's importable and unit-tested; the CLI just
calls `run_doctor()`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3

import yaml

from . import utils

CONFIG_DIR = utils.ROOT_DIR / "config"
DB_PATH = utils.ROOT_DIR / "db" / "govspend_free.db"

OK, WARN, MISSING = "ok", "warn", "missing"
_GLYPH = {OK: "OK  ", WARN: "WARN", MISSING: "--  "}


def _dep(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _looks_placeholder(value: str) -> bool:
    low = value.lower()
    return (not value) or ("xxxx" in low) or ("..." in value) or low.startswith("your") \
        or low in ("changeme", "replace-me", "todo")


def _config_status(name: str, *, secret_key: str | None = None) -> tuple[str, str]:
    """(status, note) for a config file. If secret_key is given, also check the
    value isn't an obvious placeholder - without returning the value itself."""
    path = CONFIG_DIR / name
    example = CONFIG_DIR / (name + ".example")
    if not path.exists():
        hint = f" - copy {example.name}" if example.exists() else ""
        return MISSING, f"not created{hint}"
    if secret_key is None:
        return OK, "present"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return WARN, f"present but unreadable ({exc})"
    if _looks_placeholder(str(cfg.get(secret_key, "")).strip()):
        return WARN, f"present but '{secret_key}' looks like a placeholder"
    return OK, "configured"


def gather() -> dict:
    """Collect the full status report as a plain dict (JSON-serializable)."""
    deps = {
        "requests": _dep("requests"), "beautifulsoup4": _dep("bs4"),
        "PyYAML": _dep("yaml"), "pdfplumber": _dep("pdfplumber"),
        "python-dateutil": _dep("dateutil"), "lxml": _dep("lxml"),
        "pywebview (desktop UI)": _dep("webview"),
        "playwright (--browser)": _dep("playwright"),
        "anthropic (--ask/--chat)": _dep("anthropic"),
    }

    configs = {
        "sources.yaml": _config_status("sources.yaml"),
        "keywords.yaml": _config_status("keywords.yaml"),
        "apollo.yaml (Contacts)": _config_status("apollo.yaml", secret_key="api_key"),
        "llm.yaml (AI Search)": _config_status("llm.yaml", secret_key="api_key"),
        "hubspot.yaml (Ops play)": _config_status("hubspot.yaml", secret_key="token"),
        "sam.yaml (Federal RFPs)": _config_status("sam.yaml", secret_key="api_key"),
        "grants_gov.yaml (Federal grant opps)": _config_status("grants_gov.yaml"),
        "alerts.yaml (email)": _config_status("alerts.yaml"),
        "ops.yaml (Ops context)": _config_status("ops.yaml"),
    }
    if os.environ.get("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN"):
        configs["hubspot.yaml (Ops play)"] = (OK, "HUBSPOT_TOKEN set in environment")
    if os.environ.get("SAM_API_KEY"):
        configs["sam.yaml (Federal RFPs)"] = (OK, "SAM_API_KEY set in environment")

    tools = {
        "claude CLI (--brief / Ops)": OK if shutil.which("claude") else MISSING,
    }

    database = _db_status()

    return {"deps": deps, "configs": configs, "tools": tools, "database": database}


def _db_status() -> dict:
    if not DB_PATH.exists():
        return {"exists": False, "path": str(DB_PATH)}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        by_type = {r["doc_type"]: r["n"]
                   for r in conn.execute("SELECT doc_type, COUNT(*) n FROM documents GROUP BY doc_type")}
        states = conn.execute("SELECT COUNT(DISTINCT state) n FROM documents").fetchone()["n"]
        contracts = conn.execute("SELECT COUNT(*) n FROM contracts").fetchone()["n"]
        contacts = conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    except sqlite3.Error as exc:
        return {"exists": True, "path": str(DB_PATH), "error": str(exc)}
    finally:
        conn.close()
    return {
        "exists": True, "path": str(DB_PATH),
        "documents": sum(by_type.values()), "by_type": by_type,
        "states": states, "contracts": contracts, "contacts": contacts,
    }


def format_report(report: dict) -> str:
    lines = ["", "=" * 60, "GOVSPEND FREE - SETUP DOCTOR", "=" * 60]

    lines.append("\nDependencies:")
    for label, present in report["deps"].items():
        lines.append(f"  [{_GLYPH[OK if present else MISSING]}] {label}")

    lines.append("\nConfig files (config/):")
    for label, (status, note) in report["configs"].items():
        lines.append(f"  [{_GLYPH[status]}] {label} - {note}")

    lines.append("\nExternal tools:")
    for label, status in report["tools"].items():
        lines.append(f"  [{_GLYPH[status]}] {label}")
    lines.append(f"  [{_GLYPH[WARN]}] playwright browser binary - can't verify here; "
                 "if `--browser` fails to launch, run `playwright install chromium`")

    db = report["database"]
    lines.append("\nLocal database:")
    if not db["exists"]:
        lines.append(f"  [{_GLYPH[MISSING]}] {db['path']} - not created yet; run a scrape first")
    elif db.get("error"):
        lines.append(f"  [{_GLYPH[WARN]}] {db['path']} - {db['error']}")
    else:
        lines.append(f"  [{_GLYPH[OK]}] {db['path']}")
        lines.append(f"       {db['documents']} documents across {db['states']} state(s); "
                     f"{db['contracts']} contracts, {db['contacts']} contacts")
        if db["by_type"]:
            lines.append("       by type: " + ", ".join(f"{k}={v}" for k, v in sorted(db["by_type"].items())))

    lines.append("")
    return "\n".join(lines)


def run_doctor() -> dict:
    report = gather()
    print(format_report(report))
    return report
