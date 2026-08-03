"""
Simple desktop UI (pywebview), replicating GovSpend's dashboard feel: a native
window showing a searchable, ranked view of everything you've scraped, plus a
button to kick off a new scrape with live progress.

Run it:
    pip install -e ".[ui]"     # installs pywebview
    govspend-free-ui           # (or: python -m govspend_free.desktop)

On Windows this uses the built-in Edge WebView2 runtime (present on Win11). The
UI is a self-contained HTML page rendered in a native window; all data comes
from Python via pywebview's js_api bridge - no web server, no external network
except the scrapes you trigger yourself.

`webview` is imported lazily inside main() so this module (and the Api data
methods) stay importable/testable even where pywebview isn't installed.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from pathlib import Path

import yaml

from . import brief, db, doctor, opportunities, ops, pipeline, utils
from .utils import log

CONFIG_DIR = utils.ROOT_DIR / "config"


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Api:
    """The Python side of the UI. Every public method here is callable from
    the page as `window.pywebview.api.<method>(...)` and its return value is
    JSON-serialized back to JavaScript."""

    def __init__(self) -> None:
        self._window = None
        self._scraping = False
        self._briefing = False
        self._playing = False

    def set_window(self, window) -> None:
        self._window = window

    # -------------------- read-only dashboard queries --------------------

    def list_states(self) -> list[str]:
        return sorted(_load_yaml(CONFIG_DIR / "sources.yaml").keys())

    def opportunities(self, limit: int = 5000, include_old: bool = False) -> list[dict]:
        conn = db.get_conn()
        try:
            # Default: hide items whose own date is over a year old. The UI loads
            # a high limit (not the top-50) so EVERY doc type is represented in
            # the results - otherwise the type filter only ever sees the most
            # numerous type (board minutes).
            max_age = None if include_old else 365
            return opportunities.rank_opportunities(conn, limit=int(limit), max_age_days=max_age)
        finally:
            conn.close()

    def search(self, query: str, limit: int = 50) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        conn = db.get_conn()
        try:
            return [dict(r) for r in db.search(conn, query, limit=int(limit))]
        except Exception as exc:  # FTS5 raises on malformed match queries
            log.warning("search(%r) failed: %s", query, exc)
            return []
        finally:
            conn.close()

    def expirations(self, days: int = 180) -> list[dict]:
        conn = db.get_conn()
        try:
            return [dict(r) for r in db.upcoming_expirations(conn, within_days=int(days))]
        finally:
            conn.close()

    def export_rows_csv(self, rows: list | None = None, name: str = "export") -> dict:
        """Write a list of row dicts to reports/<name>_<ts>.csv (the Opportunities
        / Search 'Export CSV' buttons). Excel-friendly UTF-8-SIG. Returns
        {ok, path} or {ok: False, error}."""
        import csv
        import re
        import time
        rows = rows or []
        if not rows:
            return {"ok": False, "error": "nothing to export"}
        safe = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "export"
        out = utils.REPORTS_DIR / f"{safe}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.csv"
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        try:
            with out.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in cols})
            return {"ok": True, "path": str(out)}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def open_external(self, url: str) -> None:
        """Open a result link in the user's real browser, not the app window."""
        if url:
            webbrowser.open(url)

    def doctor(self) -> dict:
        """Setup report for the UI (same as `python main.py --doctor`)."""
        report = doctor.gather()
        return {"text": doctor.format_report(report), "report": report}

    def home_stats(self) -> dict:
        """Dashboard summary with a RAG (red/amber/green) status per metric, plus
        a couple of mini-lists. All read-only from the DB."""
        conn = db.get_conn()
        try:
            q = conn.execute
            # Data freshness - how stale is the newest scraped document?
            fr = q("SELECT MAX(scraped_at) m, "
                   "CAST(julianday('now') - julianday(MAX(scraped_at)) AS INTEGER) age "
                   "FROM documents").fetchone()
            if fr["m"] is None:
                fresh_val, fresh_rag, fresh_sub = "no data", "red", "run a scrape"
            else:
                age = fr["age"] or 0
                fresh_val = "today" if age <= 0 else f"{age}d ago"
                fresh_rag = "green" if age <= 7 else "amber" if age <= 30 else "red"
                fresh_sub = f"last scrape {str(fr['m'])[:10]}"

            total_docs = q("SELECT COUNT(*) n FROM documents").fetchone()["n"]
            by_type = {r["doc_type"]: r["n"]
                       for r in q("SELECT doc_type, COUNT(*) n FROM documents GROUP BY doc_type")}

            opps = opportunities.rank_opportunities(conn, limit=100000)
            opp_count = len(opps)
            top_score = opps[0]["score"] if opps else 0
            opp_rag = "green" if top_score >= 30 else "amber" if opp_count else "red"

            exp90 = len(db.upcoming_expirations(conn, within_days=90))
            exp180 = len(db.upcoming_expirations(conn, within_days=180))
            exp_rag = "red" if exp90 else "amber" if exp180 else "green"

            kinds = {r["vendor_kind"]: r["n"]
                     for r in q("SELECT vendor_kind, COUNT(*) n FROM payments GROUP BY vendor_kind")}
            comp, client = kinds.get("competitor", 0), kinds.get("client", 0)
            top_comp = [{"vendor": r["vendor_canonical"], "n": r["n"]}
                        for r in q("SELECT vendor_canonical, COUNT(*) n FROM payments "
                                   "WHERE vendor_kind='competitor' AND vendor_canonical IS NOT NULL "
                                   "GROUP BY 1 ORDER BY n DESC LIMIT 6")]
            fp_rag = "green" if comp else "amber" if client else "gray"

            states_with = q("SELECT COUNT(DISTINCT state) n FROM documents "
                            "WHERE state IS NOT NULL AND state != ''").fetchone()["n"]
            total_states = len(self.list_states())
            cov_rag = "green" if states_with >= 8 else "amber" if states_with >= 3 else "red"

            top_opps = [{"score": o["score"], "doc_type": o["doc_type"], "institution": o["institution"],
                         "title": o["title"], "url": o["url"]} for o in opps[:6]]
        finally:
            conn.close()

        cards = [
            {"label": "Data freshness", "value": fresh_val, "rag": fresh_rag, "sub": fresh_sub},
            {"label": "Opportunities", "value": str(opp_count), "rag": opp_rag, "sub": f"top score {top_score}"},
            {"label": "Expiring ≤ 90d", "value": str(exp90), "rag": exp_rag, "sub": f"{exp180} within 180d"},
            {"label": "Competitor payments", "value": str(comp), "rag": fp_rag,
             "sub": f"{client} to client · {len(top_comp)} vendors"},
            {"label": "Documents", "value": str(total_docs), "rag": "green" if total_docs else "red",
             "sub": ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())) or "none"},
            {"label": "States covered", "value": f"{states_with}/{total_states}", "rag": cov_rag,
             "sub": "with scraped data"},
        ]
        return {"cards": cards, "top_opportunities": top_opps, "top_competitors": top_comp,
                "has_data": total_docs > 0}

    # ------------------------------ scrape ------------------------------

    def start_scrape(self, options: dict | None = None) -> dict:
        if self._scraping:
            return {"started": False, "error": "A scrape is already running."}
        self._scraping = True
        threading.Thread(target=self._scrape_worker, args=(options or {},), daemon=True).start()
        return {"started": True}

    def _scrape_worker(self, options: dict) -> None:
        handler = _WebviewLogHandler(self._window)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg_logger = logging.getLogger("govspend_free")
        pkg_logger.addHandler(handler)
        try:
            sources = _load_yaml(CONFIG_DIR / "sources.yaml")
            keywords = _load_yaml(CONFIG_DIR / "keywords.yaml")
            # Fresh connection: this runs on a worker thread, and sqlite
            # connections aren't shareable across threads.
            conn = db.get_conn()
            try:
                result = pipeline.run_scrape(
                    conn, sources, keywords,
                    selected_state=(options.get("states") or None),
                    skip_bids=options.get("skip_bids", False),
                    skip_board_minutes=options.get("skip_board_minutes", False),
                    skip_transparency=options.get("skip_transparency", False),
                    skip_federal=options.get("skip_federal", False),
                    skip_sam=options.get("skip_sam", False),
                    skip_grants=options.get("skip_grants", False),
                    skip_contracts=options.get("skip_contracts", False),
                    # Apollo costs credits and needs config - opt in explicitly.
                    skip_contacts=options.get("skip_contacts", True),
                    criteria=pipeline.ScrapeCriteria.build(
                        date_from=options.get("date_from"),
                        date_to=options.get("date_to"),
                        only_keywords=options.get("only_keyword"),
                        only_competitors=options.get("only_competitor"),
                    ),
                    use_browser=options.get("use_browser", False),
                )
                counts = result.counts()
            finally:
                conn.close()
            log.info("Scrape complete.")
            self._emit("scrapeDone", counts)
        except Exception as exc:
            log.error("Scrape failed: %s", exc)
            self._emit("scrapeError", {"error": str(exc)})
        finally:
            pkg_logger.removeHandler(handler)
            self._scraping = False

    # --------------------------- settings + auto-update ---------------------------

    def _settings_path(self):
        return utils.STATE_DIR / "ui_settings.json"

    def _read_settings(self) -> dict:
        p = self._settings_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _write_settings(self, data: dict) -> None:
        try:
            self._settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save UI settings: %s", exc)

    def get_settings(self) -> dict:
        s = self._read_settings()
        return {"auto_update": s.get("auto_update", "off"),
                "last_auto_update": s.get("last_auto_update", "")}

    def set_auto_update(self, mode: str = "off") -> dict:
        mode = mode if mode in ("off", "daily", "weekly") else "off"
        s = self._read_settings()
        s["auto_update"] = mode
        self._write_settings(s)
        log.info("Automatic updates set to: %s", mode)
        return {"ok": True, "auto_update": mode}

    def start_scheduler(self) -> None:
        """Start the background auto-update checker (daemon thread). Runs a full
        update when the configured schedule is due AND the app is open, so a
        nontechnical user never has to open the Update tab."""
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    def _scheduler_loop(self) -> None:
        import time as _time
        _time.sleep(25)  # let the window finish loading before the first check
        while True:
            try:
                self._maybe_auto_update()
            except Exception as exc:  # a bad check must never kill the loop
                log.warning("auto-update check failed: %s", exc)
            _time.sleep(1800)  # re-check every 30 min while the app is open

    def _maybe_auto_update(self) -> None:
        import datetime as _dt
        s = self._read_settings()
        mode = s.get("auto_update", "off")
        if mode not in ("daily", "weekly") or self._scraping:
            return
        interval = 86400 if mode == "daily" else 604800
        last = s.get("last_auto_update")
        if last:
            try:
                if (_dt.datetime.now() - _dt.datetime.fromisoformat(last)).total_seconds() < interval:
                    return
            except ValueError:
                pass
        log.info("Automatic update starting (%s schedule)...", mode)
        self._scraping = True
        self._scrape_worker({})   # runs synchronously here; clears self._scraping in its finally
        s = self._read_settings()
        s["auto_update"] = mode
        s["last_auto_update"] = _dt.datetime.now().isoformat(timespec="seconds")
        self._write_settings(s)

    # --------------------------- account brief ---------------------------

    def start_brief(self, target: str) -> dict:
        if self._briefing:
            return {"started": False, "error": "A brief is already being generated."}
        if not brief.claude_available():
            return {"started": False, "error": (
                "The `claude` CLI wasn't found on PATH. Install Claude Code to generate briefs."
            )}
        self._briefing = True
        threading.Thread(target=self._brief_worker, args=(str(target),), daemon=True).start()
        return {"started": True}

    def _brief_worker(self, target: str) -> None:
        handler = _WebviewLogHandler(self._window)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg_logger = logging.getLogger("govspend_free")
        pkg_logger.addHandler(handler)
        try:
            conn = db.get_conn()
            try:
                result = brief.generate_brief(conn, target)
            finally:
                conn.close()
            self._emit("briefDone", {
                "institution": result["institution"],
                "markdown": result["markdown"],
                "path": result["path"],
            })
        except Exception as exc:
            log.error("Brief failed: %s", exc)
            self._emit("briefError", {"error": str(exc)})
        finally:
            pkg_logger.removeHandler(handler)
            self._briefing = False

    # ------------------------- Ops: Full Motion play -------------------------

    def ops_status(self) -> dict:
        """Is the read-only HubSpot path ready? Drives the button's gate."""
        return ops.hubspot_status()

    def export_play_csv(self, markdown: str) -> dict:
        """Write the last play's table to a CSV and return its path."""
        try:
            path = ops.export_play_csv(markdown or "")
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            log.error("export_play_csv failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def run_play(self) -> dict:
        """Kick off the Full Motion play on a worker thread. Progress streams
        back via onPyEvent('playLog'/'playDone'/'playError')."""
        if self._playing:
            return {"started": False, "error": "The play is already running."}
        self._playing = True
        threading.Thread(target=self._play_worker, daemon=True).start()
        return {"started": True}

    def _play_worker(self) -> None:
        try:
            result = ops.run_full_motion_play(
                on_progress=lambda line: self._emit("playLog", {"line": line}),
            )
            if result["ok"]:
                self._emit("playDone", {
                    "markdown": result["markdown"],
                    "report_path": result["report_path"],
                })
            else:
                self._emit("playError", {"error": result["error"] or "Unknown error."})
        except Exception as exc:  # never let the worker die silently
            log.error("Full Motion play failed: %s", exc)
            self._emit("playError", {"error": str(exc)})
        finally:
            self._playing = False

    def _emit(self, event: str, payload: dict) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"window.onPyEvent({json.dumps(event)}, {json.dumps(payload)})")


class _WebviewLogHandler(logging.Handler):
    """Forwards package log records into the page's live log panel."""

    def __init__(self, window) -> None:
        super().__init__()
        self._window = window

    def emit(self, record: logging.LogRecord) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(f"window.appendLog({json.dumps(self.format(record))})")
        except Exception:
            pass  # window may be closing; never let logging raise


def _app_icon():
    """Generate (once) and return the app-icon .ICO path, or None on any failure.
    Windows' window icon goes through System.Drawing.Icon, which requires a real
    .ico (a PNG raises ArgumentException on a pywebview worker thread and crashes
    the app), so we emit a 32-bit BMP-DIB ICO with a pure-stdlib writer - a
    rounded blue tile with white 'bar chart' bars. No image-library dependency."""
    try:
        import struct
        icon = utils.STATE_DIR / "app_icon.ico"
        if icon.exists():
            return icon
        S = 32
        BG, WH, AC = (37, 99, 235), (255, 255, 255), (147, 197, 253)
        m, r = 2, 7
        bars = ((8, 20), (14, 15), (20, 11))   # (x_start, top_y); 5px wide, bottom y=25

        def inside(x, y):
            x0, y0, x1, y1 = m, m, S - 1 - m, S - 1 - m
            if x < x0 or x > x1 or y < y0 or y > y1:
                return False
            for cx, cy, tx, ty in ((x0 + r, y0 + r, x < x0 + r, y < y0 + r),
                                   (x1 - r, y0 + r, x > x1 - r, y < y0 + r),
                                   (x0 + r, y1 - r, x < x0 + r, y > y1 - r),
                                   (x1 - r, y1 - r, x > x1 - r, y > y1 - r)):
                if tx and ty:
                    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            return True

        def color(x, y):
            if not inside(x, y):
                return None    # transparent
            if any(bx <= x < bx + 5 and top <= y <= 25 for bx, top in bars):
                return WH
            if (x - 22) ** 2 + (y - 9) ** 2 <= 6:
                return AC
            return BG

        xor = bytearray()          # BGRA pixels, bottom-up
        for row in range(S):
            y = S - 1 - row
            for x in range(S):
                c = color(x, y)
                xor += bytes((0, 0, 0, 0)) if c is None else bytes((c[2], c[1], c[0], 255))
        row_bytes = ((S + 31) // 32) * 4     # 1bpp AND mask, rows padded to 32 bits
        andmask = bytearray()
        for row in range(S):
            y = S - 1 - row
            bits = bytearray(row_bytes)
            for x in range(S):
                if color(x, y) is None:      # transparent -> AND bit set
                    bits[x // 8] |= 0x80 >> (x % 8)
            andmask += bits

        bmi = struct.pack("<IiiHHIIiiII", 40, S, S * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        image = bmi + bytes(xor) + bytes(andmask)
        icondir = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", S, S, 0, 0, 1, 32, len(image), 22)
        icon.write_bytes(icondir + entry + image)
        return icon
    except Exception:
        return None


def main() -> None:
    utils.setup_logging()
    import webview  # lazy: only needed to actually open the window

    api = Api()
    window = webview.create_window(
        "GovSpend Free - Procurement Dashboard",
        html=HTML,
        js_api=api,
        width=1120,
        height=800,
        min_size=(860, 620),
    )
    api.set_window(window)
    api.start_scheduler()          # background auto-updates (no-op unless enabled)
    icon = _app_icon()
    try:
        webview.start(icon=str(icon)) if icon else webview.start()
    except TypeError:
        webview.start()            # older pywebview without an icon kwarg


# --------------------------------------------------------------------------
# The UI. Self-contained HTML/CSS/JS rendered in the native window. All data
# is fetched over the pywebview bridge (window.pywebview.api.*).
# --------------------------------------------------------------------------
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f172a; --fg: #1c2230; --muted: #64748b; --line: #e2e8f0;
    --panel: #ffffff; --accent: #2563eb; --accent-fg: #ffffff;
    --chip: #eef2ff; --chip-fg: #3730a3; --soon: #b91c1c; --soon-bg: #fef2f2;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
    color: var(--fg); background: #f1f5f9;
  }
  header {
    display: flex; align-items: center; gap: 12px;
    padding: 14px 20px; background: #0f172a; color: #fff;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .sub { color: #94a3b8; font-size: 12px; }
  header .spacer { flex: 1; }
  .tabs { display: flex; gap: 4px; padding: 10px 20px 0; background: #f1f5f9; }
  .tab {
    padding: 8px 14px; border: none; background: transparent; cursor: pointer;
    font-size: 13px; color: var(--muted); border-radius: 8px 8px 0 0; font-weight: 550;
  }
  .tab.active { background: var(--panel); color: var(--fg); box-shadow: 0 -1px 0 var(--line); }
  .tab svg.tico { width: 15px; height: 15px; vertical-align: -2.5px; margin-right: 6px; }
  header .logo { display: inline-flex; align-items: center; }
  .stat-card .k svg.cico { width: 15px; height: 15px; margin-right: 6px; color: var(--muted); }
  .rag-green .cico { color: #16a34a; } .rag-amber .cico { color: #d97706; }
  .rag-red .cico { color: #dc2626; } .rag-gray .cico { color: var(--muted); }
  main { padding: 16px 20px 28px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  input[type=text], input[type=number], select {
    padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; background: #fff; color: var(--fg);
  }
  input[type=text] { flex: 1; min-width: 220px; }
  button.primary {
    padding: 8px 16px; border: none; border-radius: 8px; background: var(--accent);
    color: var(--accent-fg); font-size: 13px; font-weight: 600; cursor: pointer;
  }
  button.primary:disabled { opacity: .5; cursor: default; }
  label.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--fg); cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
  tr:hover td { background: #f8fafc; }
  td a { color: var(--accent); text-decoration: none; cursor: pointer; word-break: break-all; }
  td a:hover { text-decoration: underline; }
  .score { font-variant-numeric: tabular-nums; font-weight: 700; }
  .chip { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--chip); color: var(--chip-fg); font-size: 11px; margin: 1px 2px 1px 0; }
  .type { color: var(--muted); font-size: 12px; }
  .soon { color: var(--soon); font-weight: 700; background: var(--soon-bg); padding: 1px 6px; border-radius: 6px; }
  .empty { color: var(--muted); padding: 24px 8px; text-align: center; }
  .hint { color: var(--muted); font-size: 12px; margin: 2px 0 0; }
  #scrapeLog, #playLog {
    margin-top: 14px; background: #0f172a; color: #cbd5e1; border-radius: 8px;
    padding: 12px; height: 260px; overflow: auto; white-space: pre-wrap;
    font: 12px/1.5 "Cascadia Code", Consolas, monospace;
  }
  #playResult { margin-top: 12px; overflow-x: auto; }
  #playResult h1, #playResult h2, #playResult h3 { font-size: 15px; margin: 14px 0 4px; }
  #playResult ol, #playResult ul { margin: 6px 0 6px 18px; }
  #playResult td { font-size: 12.5px; }
  .gate-warn { color: #92400e; background: #fffbeb; border: 1px solid #fde68a;
    border-radius: 8px; padding: 10px 12px; }
  .callout { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px;
    padding: 14px 16px; margin: 8px 0 14px; color: #1e3a5f; }
  .callout strong { color: #1e40af; }
  .callout ol { margin: 8px 0 0 20px; padding: 0; } .callout li { margin: 2px 0; }
  .callout a { color: var(--accent); cursor: pointer; text-decoration: underline; font-weight: 600; }
  button.big { padding: 11px 22px; font-size: 14px; }
  .tabhelp { color: var(--muted); font-size: 12px; margin: 2px 0 12px; }
  details.adv { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 10px; }
  details.adv > summary { cursor: pointer; color: var(--accent); font-size: 13px; font-weight: 600; list-style: none; }
  details.adv > summary::-webkit-details-marker { display: none; }
  details.adv > summary::before { content: "\25B8 "; }
  details.adv[open] > summary::before { content: "\25BE "; }
  .gate-ok { color: #15803d; }
  .status { margin-top: 10px; font-size: 13px; }
  .status.ok { color: #15803d; } .status.err { color: var(--soon); }
  /* Home dashboard RAG cards */
  .homegrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; margin-top: 10px; }
  .stat-card { border: 1px solid var(--line); border-left-width: 4px; border-radius: 10px; padding: 12px 14px; background: #fff; }
  .stat-card .k { font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); display: flex; align-items: center; }
  .stat-card .v { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; margin: 3px 0 1px; }
  .stat-card .s { font-size: 11px; color: var(--muted); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); }
  .rag-green { border-left-color: #16a34a; } .rag-green .dot { background: #16a34a; }
  .rag-amber { border-left-color: #d97706; } .rag-amber .dot { background: #d97706; }
  .rag-red   { border-left-color: #dc2626; } .rag-red   .dot { background: #dc2626; }
  .rag-gray  { border-left-color: var(--line); } .rag-gray .dot { background: var(--muted); }
  .minirow { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  .minirow .n { font-variant-numeric: tabular-nums; color: var(--muted); }
  .hidden { display: none !important; }  /* must beat .overlay's display:flex */
  button.briefbtn {
    padding: 4px 10px; border: 1px solid var(--accent); background: #fff; color: var(--accent);
    border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap;
  }
  button.briefbtn:hover { background: var(--accent); color: #fff; }
  button.briefbtn:disabled { opacity: .5; cursor: default; }
  .overlay {
    position: fixed; inset: 0; background: rgba(15,23,42,.55);
    display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 50;
  }
  .modal {
    background: var(--panel); border-radius: 12px; width: min(860px, 100%); max-height: 88vh;
    display: flex; flex-direction: column; padding: 16px 18px; box-shadow: 0 12px 40px rgba(0,0,0,.3);
  }
  .modal-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  button.ghost {
    padding: 5px 12px; border: 1px solid var(--line); background: #fff; color: var(--fg);
    border-radius: 6px; font-size: 12px; cursor: pointer;
  }
  button.ghost:hover { background: #f1f5f9; }
  .brief-body {
    flex: 1; overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 8px 0;
    background: #f8fafc; border: 1px solid var(--line); border-radius: 8px; padding: 14px;
    font: 13px/1.6 -apple-system, "Segoe UI", sans-serif;
  }
</style>
</head>
<body>
  <header>
    <span class="logo" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 26 26" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="1" y="1" width="24" height="24" rx="6" fill="#2563eb"/>
      <rect x="5.6" y="13" width="3.2" height="7" rx="1" fill="#fff"/>
      <rect x="11.4" y="9" width="3.2" height="11" rx="1" fill="#fff"/>
      <rect x="17.2" y="6" width="3.2" height="14" rx="1" fill="#fff"/>
      <circle cx="19" cy="7" r="2.4" fill="#93c5fd"/>
    </svg></span>
    <h1>GovSpend&nbsp;Free</h1>
    <span class="sub">local procurement dashboard</span>
    <span class="spacer"></span>
    <span class="sub" id="dbNote"></span>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="home">Home</button>
    <button class="tab" data-tab="opps">Opportunities</button>
    <button class="tab" data-tab="search">Search</button>
    <button class="tab" data-tab="exp">Expiring Contracts</button>
    <button class="tab" data-tab="ops">Account Priorities</button>
    <button class="tab" data-tab="scrape">Update Data</button>
  </nav>

  <main>
    <!-- Home / Dashboard -->
    <section id="tab-home" class="tabpane">
      <div class="panel">
        <div class="row">
          <strong>Dashboard</strong>
          <span class="spacer" style="flex:1"></span>
          <button class="primary" onclick="loadHome()">Refresh</button>
        </div>
        <p class="hint">Status at a glance &mdash;
          <span class="dot rag-green"></span> good &nbsp;
          <span class="dot rag-amber"></span> watch &nbsp;
          <span class="dot rag-red"></span> act now.</p>
        <div id="homeGetStarted"></div>
        <div id="homeGrid" class="homegrid"></div>
        <div class="row" style="margin-top:18px; align-items:flex-start; gap:24px">
          <div style="flex:2; min-width:300px"><strong>Top opportunities</strong><div id="homeOpps"></div></div>
          <div style="flex:1; min-width:220px"><strong>Competitor footprint</strong><div id="homeComp"></div></div>
        </div>
      </div>
    </section>

    <!-- Opportunities -->
    <section id="tab-opps" class="tabpane hidden">
      <div class="panel">
        <div class="row">
          <strong>Ranked opportunities</strong>
          <span class="spacer" style="flex:1"></span>
          <button class="primary" id="refreshBtn" onclick="loadOpps()">Refresh</button>
        </div>
        <p class="hint">Everything ever scraped, scored by keyword strength + recency. Click a title to open it.</p>
        <div class="row" id="oppFilters" style="margin-top:4px">
          <label class="chk">Min score <input type="number" id="fScore" value="0" min="0" step="5" style="width:70px" oninput="applyOppFilters()"></label>
          <input type="text" id="fKeyword" placeholder="filter: keyword / title / institution" style="min-width:220px" oninput="applyOppFilters()">
          <select id="fCategory" onchange="applyOppFilters()"><option value="">All categories</option></select>
          <select id="fType" onchange="applyOppFilters()"><option value="">All types</option></select>
          <label class="chk">From <input type="date" id="fFrom" oninput="applyOppFilters()"></label>
          <label class="chk">To <input type="date" id="fTo" oninput="applyOppFilters()"></label>
          <label class="chk" title="By default the feed hides items whose own date is more than a year old"><input type="checkbox" id="fOlder" onchange="loadOpps()"> incl. &gt;1yr old</label>
          <button class="ghost" onclick="clearOppFilters()">Clear</button>
          <button class="ghost" onclick="exportOpps()" title="Save the rows shown below to a CSV you can open in Excel">Export CSV</button>
          <span class="spacer" style="flex:1"></span>
          <span class="hint" id="oppCount"></span>
        </div>
        <p class="hint" id="oppExportNote"></p>
        <div id="oppsTable"></div>
      </div>
    </section>

    <!-- Search -->
    <section id="tab-search" class="tabpane hidden">
      <div class="panel">
        <strong>Search</strong>
        <p class="tabhelp">Search everything you've collected &mdash; bid titles, board-minutes text, and spending rows. Try a vendor or a topic (e.g. <em>attendance software</em>, <em>Ellucian</em>, <em>K-12</em>).</p>
        <form class="row" onsubmit="doSearch(); return false;">
          <input type="text" id="q" placeholder="Search titles &amp; documents...">
          <button class="primary" type="submit">Search</button>
        </form>
        <div class="row" id="searchFilters" style="margin-top:8px">
          <select id="sType" onchange="applySearchFilters()"><option value="">All types</option></select>
          <select id="sState" onchange="applySearchFilters()"><option value="">All states</option></select>
          <button class="ghost" onclick="exportSearch()" title="Save the results below to a CSV you can open in Excel">Export CSV</button>
          <span class="spacer" style="flex:1"></span>
          <span class="hint" id="searchCount"></span>
        </div>
        <p class="hint" id="searchExportNote"></p>
        <div id="searchTable"></div>
      </div>
    </section>

    <!-- Expiring Contracts -->
    <section id="tab-exp" class="tabpane hidden">
      <div class="panel">
        <strong>Expiring contracts</strong>
        <p class="tabhelp">Vendor contracts with a known end date coming up &mdash; a renewal window is a good time to reach out. Only appears for states that publish contract end dates.</p>
        <div class="row">
          <label class="chk">Ending within <input type="number" id="days" value="180" min="1" max="3650" style="width:90px" onchange="loadExp()"> days</label>
          <button class="primary" onclick="loadExp()">Show</button>
        </div>
        <div id="expTable"></div>
      </div>
    </section>

    <!-- Ops -->
    <section id="tab-ops" class="tabpane hidden">
      <div class="panel">
        <div class="row">
          <strong>Account priorities</strong>
          <span class="spacer" style="flex:1"></span>
          <button class="primary" id="playBtn" onclick="runPlay()">Rank my accounts</button>
        </div>
        <p class="tabhelp">
          A ranked list of who to focus on this week: every account is scored 0&ndash;100 from its
          buying signals, matched to your CRM status, with the decision-maker and a
          ready-to-personalize opener. Your CRM is read <strong>read-only</strong> &mdash; nothing is ever written back.
        </p>
        <div id="opsGate" class="status"></div>
        <div id="playStatus" class="status"></div>
        <div id="playTools" class="row hidden" style="margin-top:8px">
          <button class="ghost" onclick="copyPlay()">Copy table</button>
          <button class="ghost" onclick="savePlayCsv()">Save as CSV</button>
        </div>
        <div id="playResult"></div>
        <details style="margin-top:12px">
          <summary class="hint" style="cursor:pointer">Run log</summary>
          <div id="playLog"></div>
        </details>
      </div>
    </section>

    <!-- Update Data -->
    <section id="tab-scrape" class="tabpane hidden">
      <div class="panel">
        <strong>Update your data</strong>
        <p class="tabhelp">Collect the latest bids, board minutes, spending, and federal opportunities from the public sources. This can take a few minutes &mdash; progress shows below.</p>
        <div class="callout" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
          <svg style="color:var(--accent); width:18px; height:18px; flex:none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.7-6.4"/><path d="M21 4v5h-5"/></svg>
          <label class="chk"><strong>Keep my data fresh automatically</strong>
            <select id="autoUpdate" onchange="setAutoUpdate()" style="margin-left:8px">
              <option value="off">Off</option>
              <option value="daily">Every day</option>
              <option value="weekly">Every week</option>
            </select>
          </label>
          <span class="hint" id="autoNote" style="flex:1; min-width:240px"></span>
        </div>
        <div class="row" style="align-items:flex-start">
          <div style="flex:1; min-width:260px">
            <div style="display:flex; align-items:center; gap:14px; margin-bottom:6px">
              <label class="chk" style="font-weight:600"><input type="checkbox" id="stateAll" checked onchange="onStateAllChange()"> All states</label>
              <span class="hint" id="stateCount"></span>
              <a onclick="selectAllStates(false)" style="cursor:pointer">clear</a>
            </div>
            <div id="stateGrid" style="display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:2px 12px; max-height:200px; overflow:auto; padding:6px 8px; border:1px solid var(--border,#ddd); border-radius:8px"></div>
          </div>
          <button class="primary big" id="runBtn" onclick="runScrape()">Update everything</button>
        </div>
        <p class="hint" style="margin:2px 0 0">Runs every source for the state(s) you pick. Nationwide passes (SAM.gov, Grants.gov) run only when <em>All states</em> is on.</p>

        <details class="adv">
          <summary>Advanced options</summary>
          <p class="hint" style="margin-top:8px">Skip sources you don't need, or narrow the run. Leave everything unchecked to collect it all.</p>
          <div class="row">
            <label class="chk"><input type="checkbox" id="skip_bids"> skip bids</label>
            <label class="chk"><input type="checkbox" id="skip_board_minutes"> skip board minutes</label>
            <label class="chk"><input type="checkbox" id="skip_transparency"> skip spending</label>
            <label class="chk"><input type="checkbox" id="skip_federal"> skip federal grants</label>
            <label class="chk" title="SAM.gov federal RFPs run once per full update when config/sam.yaml is enabled (uses your daily API key quota)"><input type="checkbox" id="skip_sam"> skip SAM.gov RFPs</label>
            <label class="chk" title="Grants.gov federal grant opportunities run once per full update when config/grants_gov.yaml is enabled (keyless, no API key)"><input type="checkbox" id="skip_grants"> skip Grants.gov opps</label>
            <label class="chk"><input type="checkbox" id="skip_contracts"> skip contracts</label>
            <label class="chk"><input type="checkbox" id="inc_contacts"> include contact lookup (Apollo &mdash; uses credits)</label>
            <label class="chk" title="Render JavaScript-heavy sites with a headless browser (slower)"><input type="checkbox" id="use_browser"> render JS-heavy sources (slower)</label>
          </div>
          <div class="row" style="margin-top:10px">
            <label class="chk">From <input type="date" id="scrape_from"></label>
            <label class="chk">To <input type="date" id="scrape_to"></label>
            <input type="text" id="scrape_keyword" placeholder="only keyword(s), comma-separated" style="min-width:200px">
            <input type="text" id="scrape_competitor" placeholder="only competitor(s), e.g. Ellucian, Civitas" style="min-width:200px">
          </div>
          <div class="row" style="margin-top:12px">
            <button class="ghost" onclick="checkSetup()">Check setup</button>
            <span class="hint">Verify dependencies, config files, tokens, and what's in your local database.</span>
          </div>
          <pre id="doctorOut" class="brief-body hidden" style="max-height:320px; margin-top:10px"></pre>
        </details>

        <div id="scrapeStatus" class="status"></div>
        <div id="scrapeLog" class="hidden"></div>
      </div>
    </section>
  </main>

  <!-- Account-brief modal -->
  <div id="briefOverlay" class="overlay hidden">
    <div class="modal">
      <div class="modal-head">
        <strong id="briefTitle">Account brief</strong>
        <span class="spacer" style="flex:1"></span>
        <button class="ghost" id="briefCopyBtn" onclick="copyBrief()">Copy</button>
        <button class="ghost" onclick="closeBrief()">Close</button>
      </div>
      <div id="briefStatus" class="status"></div>
      <pre id="briefBody" class="brief-body"></pre>
      <div id="briefPath" class="hint"></div>
    </div>
  </div>

<script>
  // Surface any uncaught JS error in the header so problems are visible.
  window.onerror = function(msg, src, line) {
    var b = document.getElementById('dbNote');
    if (b) { b.textContent = 'JS error: ' + msg + ' (line ' + line + ')'; b.style.color = '#fca5a5'; }
    return false;
  };
  window.addEventListener('unhandledrejection', function(ev) {
    var b = document.getElementById('dbNote');
    if (b) { b.textContent = 'bridge error: ' + (ev.reason && ev.reason.message ? ev.reason.message : ev.reason); b.style.color = '#fca5a5'; }
  });
  const api = () => window.pywebview.api;
  const el = (id) => document.getElementById(id);
  const TYPE_LABELS = { bid: 'Bid/RFP', board_minutes: 'Board minutes', transparency: 'Spending', federal_award: 'Federal grant', federal_rfp: 'Federal RFP' };
  const typeLabel = (t) => TYPE_LABELS[t] || t;

  // Light inline iconography (Feather-style strokes, inherit currentColor).
  const ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    list: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    star: '<path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8-4.3-4.1 5.9-.9z"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.7-6.4"/><path d="M21 4v5h-5"/>',
    doc: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>',
    alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3.5 20c0-3 2.7-5 5.5-5s5.5 2 5.5 5"/><path d="M16 5.5a3 3 0 0 1 0 5.5"/>',
  };
  function icon(name, cls) {
    return '<svg class="' + (cls || '') + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (ICONS[name] || ICONS.doc) + '</svg>';
  }
  const TAB_ICONS = { home: 'home', opps: 'list', search: 'search', exp: 'clock', ops: 'star', scrape: 'refresh' };
  const CARD_ICONS = { 'Data freshness': 'clock', 'Documents': 'doc', 'Opportunities': 'list', 'Expirations': 'alert', 'Competitor footprint': 'users' };
  function paintTabIcons() {
    document.querySelectorAll('.tab').forEach(t => {
      const n = TAB_ICONS[t.dataset.tab];
      if (n && !t.querySelector('svg')) t.insertAdjacentHTML('afterbegin', icon(n, 'tico'));
    });
  }

  // ---- tab switching ----
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.tabpane').forEach(p => p.classList.add('hidden'));
    el('tab-' + t.dataset.tab).classList.remove('hidden');
    if (t.dataset.tab === 'home') loadHome();
    else if (t.dataset.tab === 'opps') loadOpps();
    else if (t.dataset.tab === 'exp') loadExp();
    else if (t.dataset.tab === 'ops') checkOpsGate();
  });
  function showTab(name) { const b = document.querySelector('.tab[data-tab="' + name + '"]'); if (b) b.click(); }

  // ---- helpers ----
  function link(url, text) {
    const a = document.createElement('a');
    a.textContent = text || url;
    if (url) { a.href = '#'; a.onclick = (e) => { e.preventDefault(); api().open_external(url); }; }
    return a;
  }
  function chips(csv) {
    const wrap = document.createElement('span');
    (csv || '').split(',').map(s => s.trim()).filter(Boolean).forEach(c => {
      const s = document.createElement('span'); s.className = 'chip'; s.textContent = c; wrap.appendChild(s);
    });
    return wrap;
  }
  function td(content) {
    const c = document.createElement('td');
    if (content instanceof Node) c.appendChild(content);
    else c.textContent = (content == null ? '' : String(content));
    return c;
  }
  function renderTable(mount, cols, rows, buildRow, emptyHtml, cap) {
    const m = el(mount); m.innerHTML = '';
    if (!rows || !rows.length) { m.innerHTML = '<div class="empty">' + (emptyHtml || 'Nothing to show yet.') + '</div>'; return; }
    // Cap how many rows we actually build into the DOM. Rendering thousands of
    // <tr> freezes the webview; rows arrive score-sorted, so the top `cap` are
    // the most relevant. The full set stays available for the count + CSV export.
    const shown = (cap && rows.length > cap) ? rows.slice(0, cap) : rows;
    const table = document.createElement('table');
    const thead = document.createElement('thead'); const htr = document.createElement('tr');
    cols.forEach(c => { const th = document.createElement('th'); th.textContent = c; htr.appendChild(th); });
    thead.appendChild(htr); table.appendChild(thead);
    const tb = document.createElement('tbody');
    shown.forEach(r => tb.appendChild(buildRow(r)));
    table.appendChild(tb); m.appendChild(table);
    if (shown.length < rows.length) {
      const note = document.createElement('div');
      note.className = 'empty'; note.style.textAlign = 'left';
      note.innerHTML = 'Showing the top <strong>' + shown.length + '</strong> of ' + rows.length +
        ' matches, capped so the app stays responsive. Narrow with the filters above — type, ' +
        'category, keyword, min score, or dates — to surface the rest. (Export CSV includes all ' +
        rows.length + '.)';
      m.appendChild(note);
    }
  }

  // ---- Opportunities ----
  const OPP_RENDER_CAP = 300;   // max rows drawn into the DOM at once (keeps the webview responsive)
  let _oppsAll = [];
  let _oppsShown = [];

  async function exportRows(rows, name, noteMount) {
    const note = el(noteMount);
    if (!rows || !rows.length) { if (note) note.textContent = 'Nothing to export yet.'; return; }
    if (note) note.textContent = 'Saving...';
    try {
      const r = await api().export_rows_csv(rows, name);
      if (note) note.textContent = r.ok ? ('Saved ' + rows.length + ' rows to ' + r.path) : ('Export failed: ' + r.error);
    } catch (e) { if (note) note.textContent = 'Export failed.'; }
  }
  function exportOpps() { exportRows(_oppsShown, 'opportunities', 'oppExportNote'); }

  async function loadOpps() {
    const btn = el('refreshBtn');
    if (btn) btn.disabled = true;
    el('oppCount').textContent = 'Refreshing…';
    try {
      _oppsAll = await api().opportunities(5000, el('fOlder') && el('fOlder').checked);
    } catch (e) {
      el('oppCount').textContent = '';
      el('oppsTable').innerHTML = '<div class="empty">Couldn\'t load opportunities. Click Refresh to try again.</div>';
      if (btn) btn.disabled = false;
      return;
    }
    // Rebuild the category dropdown from whatever's in the data.
    const cats = new Set();
    (_oppsAll || []).forEach(r => (r.categories || '').split(',').map(s => s.trim()).filter(Boolean).forEach(c => cats.add(c)));
    const sel = el('fCategory'), cur = sel.value;
    sel.innerHTML = '<option value="">All categories</option>';
    [...cats].sort().forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; sel.appendChild(o); });
    sel.value = cur;
    // Rebuild the type dropdown from whatever doc types are present.
    const types = new Set((_oppsAll || []).map(r => r.doc_type).filter(Boolean));
    const tsel = el('fType'), tcur = tsel.value;
    tsel.innerHTML = '<option value="">All types</option>';
    [...types].sort().forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = typeLabel(t); tsel.appendChild(o); });
    tsel.value = tcur;
    applyOppFilters();
    if (btn) btn.disabled = false;
  }

  function effectiveDate(r) {
    // Document's own date if it parses, else the scrape date (the "Both" rule).
    if (r.date && !isNaN(Date.parse(r.date))) return new Date(r.date);
    if (r.scraped_at) { const d = new Date(String(r.scraped_at).replace(' ', 'T')); if (!isNaN(d)) return d; }
    return null;
  }

  function applyOppFilters() {
    const minScore = parseFloat(el('fScore').value) || 0;
    const kw = el('fKeyword').value.trim().toLowerCase();
    const cat = el('fCategory').value;
    const typ = el('fType').value;
    const from = el('fFrom').value ? new Date(el('fFrom').value) : null;
    let to = el('fTo').value ? new Date(el('fTo').value) : null;
    if (to) to.setHours(23, 59, 59, 999);  // inclusive end-of-day
    const rows = _oppsAll.filter(r => {
      if (r.score < minScore) return false;
      if (typ && r.doc_type !== typ) return false;
      if (cat && !(r.categories || '').includes(cat)) return false;
      if (kw) {
        const hay = `${r.title||''} ${r.institution||''} ${r.state||''} ${r.categories||''} ${r.watchlist_hits||''}`.toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      if (from || to) {
        const d = effectiveDate(r);
        if (d) { if (from && d < from) return false; if (to && d > to) return false; }
      }
      return true;
    });
    _oppsShown = rows;
    el('oppCount').textContent = `${rows.length} of ${_oppsAll.length}`;
    const empty = _oppsAll.length
      ? 'No opportunities match your filters. <a onclick="clearOppFilters()">Clear filters</a> to see all ' + _oppsAll.length + '.'
      : 'No opportunities yet. Go to <a onclick="showTab(\'scrape\')">Update Data</a> and click <strong>Update everything</strong>.';
    renderOppRows(rows, empty);
  }

  function clearOppFilters() {
    el('fScore').value = 0; el('fKeyword').value = ''; el('fCategory').value = '';
    el('fType').value = ''; el('fFrom').value = ''; el('fTo').value = '';
    const older = el('fOlder'); const wasOld = older && older.checked;
    if (older) older.checked = false;
    // If the >1yr set was loaded, reload (re-applies the cutoff); else just refilter.
    if (wasOld) loadOpps(); else applyOppFilters();
  }

  function renderOppRows(rows, emptyHtml) {
    renderTable('oppsTable', ['Score', 'Type', 'State / Institution', 'Title', 'Tags', ''], rows, (r) => {
      const tr = document.createElement('tr');
      const s = td(r.score); s.className = 'score';
      tr.appendChild(s);
      tr.appendChild(td(Object.assign(document.createElement('span'), {className:'type', textContent:typeLabel(r.doc_type)})));
      tr.appendChild(td(`${r.state || ''} / ${r.institution || ''}`));
      tr.appendChild(td(link(r.url, r.title || '(untitled)')));
      const tags = document.createElement('span');
      tags.appendChild(chips(r.categories)); tags.appendChild(chips(r.watchlist_hits));
      tr.appendChild(td(tags));
      const btn = document.createElement('button');
      btn.className = 'briefbtn'; btn.textContent = 'Brief';
      btn.title = 'Generate an account brief from this document';
      btn.onclick = () => startBrief(r.id, r.institution || `doc ${r.id}`);
      tr.appendChild(td(btn));
      return tr;
    }, emptyHtml, OPP_RENDER_CAP);
  }

  // ---- Home / Dashboard ----
  async function loadHome() {
    let d;
    try { d = await api().home_stats(); }
    catch (e) { el('homeGrid').innerHTML = '<div class="empty">Couldn\'t load the dashboard. Click Refresh; if it keeps failing, open Update Data &rarr; Advanced &rarr; Check setup.</div>'; return; }
    const gs = el('homeGetStarted');
    if (gs) gs.innerHTML = d.has_data ? '' :
      '<div class="callout"><strong>Welcome! No data yet.</strong> Collect your first batch to fill this dashboard:' +
      '<ol><li>Go to <a onclick="showTab(\'scrape\')">Update Data</a>.</li>' +
      '<li>Pick your states (or leave &ldquo;All states&rdquo;).</li>' +
      '<li>Click <strong>Update everything</strong> and watch the progress log.</li></ol>' +
      'When it finishes, <a onclick="showTab(\'opps\')">Opportunities</a> and this dashboard fill in automatically.</div>';
    const grid = el('homeGrid'); grid.innerHTML = '';
    (d.cards || []).forEach(c => {
      const card = document.createElement('div');
      card.className = 'stat-card rag-' + (c.rag || 'gray');
      const k = document.createElement('div'); k.className = 'k';
      k.innerHTML = icon(CARD_ICONS[c.label] || 'doc', 'cico');
      k.appendChild(document.createTextNode(c.label));
      const v = document.createElement('div'); v.className = 'v'; v.textContent = c.value;
      const s = document.createElement('div'); s.className = 's'; s.textContent = c.sub || '';
      card.appendChild(k); card.appendChild(v); card.appendChild(s);
      grid.appendChild(card);
    });
    renderTable('homeOpps', ['Score', 'Institution', 'Title'], d.top_opportunities || [], (r) => {
      const tr = document.createElement('tr');
      const sc = td(r.score); sc.className = 'score'; tr.appendChild(sc);
      tr.appendChild(td(r.institution || ''));
      tr.appendChild(td(link(r.url, r.title || '(untitled)')));
      return tr;
    });
    const comp = el('homeComp'); comp.innerHTML = '';
    if (!(d.top_competitors || []).length) { comp.innerHTML = '<div class="empty">No competitor spending data yet.</div>'; }
    else d.top_competitors.forEach(c => {
      const row = document.createElement('div'); row.className = 'minirow';
      const a = document.createElement('span'); a.textContent = c.vendor;
      const b = document.createElement('span'); b.className = 'n'; b.textContent = c.n;
      row.appendChild(a); row.appendChild(b); comp.appendChild(row);
    });
  }

  // ---- Search ----
  let _searchAll = [];
  let _searchShown = [];
  async function doSearch() {
    const q = el('q').value.trim();
    if (!q) { el('searchTable').innerHTML = '<div class="empty">Type something to search.</div>'; el('searchCount').textContent=''; return; }
    el('searchCount').textContent = 'Searching...';
    el('searchExportNote').textContent = '';
    try { _searchAll = await api().search(q, 200); }
    catch (e) { el('searchTable').innerHTML = '<div class="empty">Search failed. Try a simpler term.</div>'; el('searchCount').textContent=''; return; }
    fillSelect('sType', [...new Set(_searchAll.map(r => r.doc_type).filter(Boolean))].sort(), typeLabel);
    fillSelect('sState', [...new Set(_searchAll.map(r => r.state).filter(Boolean))].sort(), (s) => s);
    applySearchFilters();
  }
  function fillSelect(id, values, labelFn) {
    const sel = el(id), cur = sel.value;
    const first = sel.options[0] ? sel.options[0].outerHTML : '';
    sel.innerHTML = first;
    values.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = labelFn(v); sel.appendChild(o); });
    sel.value = cur;
  }
  function applySearchFilters() {
    const typ = el('sType').value, st = el('sState').value;
    const rows = _searchAll.filter(r => (!typ || r.doc_type === typ) && (!st || r.state === st));
    _searchShown = rows;
    el('searchCount').textContent = _searchAll.length ? (rows.length + ' of ' + _searchAll.length) : '';
    const empty = _searchAll.length
      ? 'No results match these filters. <a onclick="clearSearchFilters()">Clear</a> them.'
      : 'No matches. Try a different term, or collect more via <a onclick="showTab(\'scrape\')">Update Data</a>.';
    renderTable('searchTable', ['Type', 'State / Institution', 'Title', 'Snippet'], rows, (r) => {
      const tr = document.createElement('tr');
      tr.appendChild(td(Object.assign(document.createElement('span'), {className:'type', textContent:typeLabel(r.doc_type)})));
      tr.appendChild(td(`${r.state || ''} / ${r.institution || ''}`));
      tr.appendChild(td(link(r.url, r.title || '(untitled)')));
      tr.appendChild(td(r.snippet || ''));
      return tr;
    }, empty);
  }
  function clearSearchFilters() { el('sType').value = ''; el('sState').value = ''; applySearchFilters(); }
  function exportSearch() { exportRows(_searchShown, 'search', 'searchExportNote'); }

  // ---- Expirations ----
  async function loadExp() {
    const days = parseInt(el('days').value, 10) || 180;
    let rows;
    try { rows = await api().expirations(days); }
    catch (e) { el('expTable').innerHTML = '<div class="empty">Couldn\'t load contracts. Try again.</div>'; return; }
    renderTable('expTable', ['Vendor', 'State / Institution', 'Ends', 'Days left', 'Source'], rows, (r) => {
      const tr = document.createElement('tr');
      tr.appendChild(td(r.vendor || ''));
      tr.appendChild(td(`${r.state || ''} / ${r.institution || ''}`));
      tr.appendChild(td(r.end_date || ''));
      const d = document.createElement('span');
      const n = r.days_until_expiration;
      d.textContent = (n == null ? '' : n + 'd');
      if (n != null && n <= 90) d.className = 'soon';
      tr.appendChild(td(d));
      tr.appendChild(td(link(r.source_url, 'source')));
      return tr;
    }, 'No contracts with an upcoming end date. This section only fills in for states that publish contract end dates (e.g. CA / GA / NC).');
  }

  // ---- Ops: Full Motion play ----
  // Minimal, safe markdown renderer: only pipe-tables, headings, and list
  // items. Everything goes through textContent, so no HTML injection from the
  // play's output. Enough to render the ranked table + top plays.
  function renderMarkdown(mount, md) {
    const m = el(mount); m.innerHTML = '';
    const lines = (md || '').split('\n');
    let i = 0;
    const isTableSep = (s) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(s) && s.includes('-');
    const cells = (s) => s.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
    while (i < lines.length) {
      const line = lines[i];
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const header = cells(line);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { body.push(cells(lines[i])); i++; }
        const table = document.createElement('table');
        const thead = document.createElement('thead'), htr = document.createElement('tr');
        header.forEach(h => { const th = document.createElement('th'); th.textContent = h; htr.appendChild(th); });
        thead.appendChild(htr); table.appendChild(thead);
        const tb = document.createElement('tbody');
        body.forEach(r => {
          const tr = document.createElement('tr');
          r.forEach(c => { const cell = document.createElement('td'); cell.textContent = c; tr.appendChild(cell); });
          tb.appendChild(tr);
        });
        table.appendChild(tb); m.appendChild(table);
        continue;
      }
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { const el2 = document.createElement('h' + h[1].length); el2.textContent = h[2]; m.appendChild(el2); i++; continue; }
      const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (li) {
        const ul = document.createElement('ul');
        while (i < lines.length) {
          const mm = lines[i].match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
          if (!mm) break;
          const item = document.createElement('li'); item.textContent = mm[1]; ul.appendChild(item); i++;
        }
        m.appendChild(ul); continue;
      }
      if (line.trim()) { const p = document.createElement('p'); p.textContent = line; m.appendChild(p); }
      i++;
    }
  }

  async function checkOpsGate() {
    const gate = el('opsGate');
    let st;
    try { st = await api().ops_status(); } catch (e) { st = { ok: false, reason: String(e) }; }
    if (st.ok) {
      gate.className = 'status'; gate.innerHTML = '';
      el('playBtn').disabled = false;
    } else {
      gate.className = 'status';
      gate.innerHTML = '<div class="gate-warn"><strong>One-time setup needed to turn this on.</strong><br>' +
        'Account Priorities reads your CRM to rank accounts. It needs a one-time, ' +
        '<strong>read-only</strong> connection to HubSpot &mdash; nothing is ever written back. ' +
        'Once it\'s connected, this button switches on automatically. ' +
        'Ask whoever set up the tool, or open <em>Update Data &rarr; Advanced &rarr; Check setup</em> for the steps.' +
        '<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">Technical details</summary>' +
        '<span class="hint">' + String(st.reason).replace(/</g, '&lt;') + '</span></details></div>';
      el('playBtn').disabled = true;
    }
  }

  let _lastPlayMd = '';
  function copyPlay() {
    if (_lastPlayMd && navigator.clipboard) {
      navigator.clipboard.writeText(_lastPlayMd);
      el('playStatus').className = 'status ok';
      el('playStatus').textContent = 'Table copied to clipboard.';
    }
  }
  async function savePlayCsv() {
    if (!_lastPlayMd) return;
    const r = await api().export_play_csv(_lastPlayMd);
    el('playStatus').className = r.ok ? 'status ok' : 'status err';
    el('playStatus').textContent = r.ok ? ('Saved CSV: ' + r.path) : ('CSV export failed: ' + r.error);
  }

  async function runPlay() {
    el('playBtn').disabled = true;
    el('playStatus').className = 'status';
    el('playStatus').textContent = 'Running the Full Motion play (read-only)...';
    el('playResult').innerHTML = '';
    el('playTools').classList.add('hidden');
    _lastPlayMd = '';
    el('playLog').textContent = '';
    const res = await api().run_play();
    if (!res.started) {
      el('playStatus').className = 'status err';
      el('playStatus').textContent = res.error || 'Could not start.';
      el('playBtn').disabled = false;
    }
  }

  // ---- Scrape ----
  window.appendLog = (line) => {
    const box = el('scrapeLog');
    box.textContent += (line + '\n');
    box.scrollTop = box.scrollHeight;
  };
  window.onPyEvent = (event, payload) => {
    if (event === 'playLog') {
      const box = el('playLog');
      box.textContent += (payload.line + '\n');
      box.scrollTop = box.scrollHeight;
    } else if (event === 'playDone') {
      el('playStatus').className = 'status ok';
      el('playStatus').textContent = 'Done.' + (payload.report_path ? ' Saved: ' + payload.report_path : '');
      _lastPlayMd = payload.markdown || '';
      el('playTools').classList.remove('hidden');
      renderMarkdown('playResult', payload.markdown);
      el('playBtn').disabled = false;
    } else if (event === 'playError') {
      el('playStatus').className = 'status err';
      el('playStatus').textContent = 'Error: ' + payload.error;
      el('playBtn').disabled = false;
    } else if (event === 'scrapeDone') {
      const c = payload;
      el('scrapeStatus').className = 'status ok';
      el('scrapeStatus').textContent =
        `Done - ${c.bids} bids, ${c.minutes} minutes, ${c.transparency} transparency, ` +
        `${c.federal} federal, ${c.federal_grant_opps} grant-opps, ` +
        `${c.contracts} contracts (${c.contracts_expiring_soon} expiring soon), ${c.contacts} contacts, ` +
        `${c.skipped} sources skipped. Stored in the database.`;
      el('runBtn').disabled = false;
      loadOpps();  // refresh the dashboard with anything new
    } else if (event === 'scrapeError') {
      el('scrapeStatus').className = 'status err';
      el('scrapeStatus').textContent = 'Error: ' + payload.error;
      el('runBtn').disabled = false;
    } else if (event === 'briefDone') {
      el('briefStatus').className = 'status ok';
      el('briefStatus').textContent = 'Brief ready for ' + payload.institution + '.';
      el('briefBody').textContent = payload.markdown;
      el('briefPath').textContent = payload.path ? ('Saved to: ' + payload.path) : '';
      briefEnableButtons(true);
    } else if (event === 'briefError') {
      el('briefStatus').className = 'status err';
      el('briefStatus').textContent = 'Error: ' + payload.error;
      briefEnableButtons(true);
    }
  };

  // ---- Account brief ----
  function briefEnableButtons(on) {
    document.querySelectorAll('button.briefbtn').forEach(b => b.disabled = !on);
  }
  function openBrief() { el('briefOverlay').classList.remove('hidden'); }
  function closeBrief() { el('briefOverlay').classList.add('hidden'); }
  function copyBrief() {
    const t = el('briefBody').textContent || '';
    if (navigator.clipboard) navigator.clipboard.writeText(t);
  }
  async function startBrief(id, label) {
    openBrief();
    el('briefTitle').textContent = 'Account brief: ' + label;
    el('briefStatus').className = 'status';
    el('briefStatus').textContent = 'Generating via claude - this can take up to a couple of minutes...';
    el('briefBody').textContent = '';
    el('briefPath').textContent = '';
    briefEnableButtons(false);
    const res = await api().start_brief(String(id));
    if (!res.started) {
      el('briefStatus').className = 'status err';
      el('briefStatus').textContent = res.error || 'Could not start.';
      briefEnableButtons(true);
    }
  }
  async function runScrape() {
    el('runBtn').disabled = true;
    el('scrapeStatus').className = 'status';
    el('scrapeStatus').textContent = 'Updating… this can take a few minutes.';
    el('scrapeLog').textContent = '';
    el('scrapeLog').classList.remove('hidden');
    const opts = {
      states: selectedStates(),
      skip_bids: el('skip_bids').checked,
      skip_board_minutes: el('skip_board_minutes').checked,
      skip_transparency: el('skip_transparency').checked,
      skip_federal: el('skip_federal').checked,
      skip_sam: el('skip_sam').checked,
      skip_grants: el('skip_grants').checked,
      skip_contracts: el('skip_contracts').checked,
      skip_contacts: !el('inc_contacts').checked,
      date_from: el('scrape_from').value,
      date_to: el('scrape_to').value,
      only_keyword: el('scrape_keyword').value,
      only_competitor: el('scrape_competitor').value,
      use_browser: el('use_browser').checked,
    };
    const res = await api().start_scrape(opts);
    if (!res.started) {
      el('scrapeStatus').className = 'status err';
      el('scrapeStatus').textContent = res.error || 'Could not start.';
      el('runBtn').disabled = false;
    }
  }

  // ---- setup doctor ----
  async function checkSetup() {
    const out = el('doctorOut');
    out.classList.remove('hidden');
    out.textContent = 'Checking...';
    try {
      const r = await api().doctor();
      out.textContent = r.text;
    } catch (e) {
      out.textContent = 'Could not run setup check: ' + (e && e.message ? e.message : e);
    }
  }

  // ---- init ----
  // ---- automatic updates ----
  async function loadSettings() {
    try {
      const s = await api().get_settings();
      if (el('autoUpdate')) el('autoUpdate').value = s.auto_update || 'off';
      updateAutoNote(s);
    } catch (e) {}
  }
  async function setAutoUpdate() {
    const mode = el('autoUpdate').value;
    try { await api().set_auto_update(mode); } catch (e) {}
    updateAutoNote({ auto_update: mode });
  }
  function updateAutoNote(s) {
    const n = el('autoNote'); if (!n) return;
    if (s.auto_update && s.auto_update !== 'off') {
      const every = s.auto_update === 'daily' ? 'day' : 'week';
      const last = s.last_auto_update ? (' Last automatic update: ' + String(s.last_auto_update).replace('T', ' ') + '.') : '';
      n.textContent = 'On — updates about once a ' + every + ', in the background whenever the app is open.' + last;
    } else {
      n.textContent = 'Off — turn this on and you never have to click Update yourself.';
    }
  }

  // ---- State multi-select ----
  function prettyState(key) {
    return String(key).split('_').map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');
  }
  async function renderStateGrid() {
    const states = await api().list_states();   // alphabetical
    const grid = el('stateGrid'); grid.innerHTML = '';
    states.forEach(s => {
      const lab = document.createElement('label'); lab.className = 'chk'; lab.style.fontWeight = '400';
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.className = 'stateChk'; cb.value = s; cb.onchange = onStateGridChange;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(' ' + prettyState(s)));
      grid.appendChild(lab);
    });
    updateStateCount();
  }
  function stateBoxes() { return Array.from(document.querySelectorAll('.stateChk')); }
  function selectedStates() {
    // "All states" (or nothing ticked) -> [] -> the backend scans every state.
    if (el('stateAll').checked) return [];
    return stateBoxes().filter(c => c.checked).map(c => c.value);
  }
  function updateStateCount() {
    const n = stateBoxes().filter(c => c.checked).length;
    el('stateCount').textContent = el('stateAll').checked ? `All ${stateBoxes().length} states` : `${n} selected`;
  }
  function onStateGridChange() {
    const any = stateBoxes().some(c => c.checked);
    el('stateAll').checked = !any;      // ticking a state turns All off; clearing all returns to All
    updateStateCount();
  }
  function onStateAllChange() {
    if (el('stateAll').checked) stateBoxes().forEach(c => { c.checked = false; });
    else if (!stateBoxes().some(c => c.checked)) el('stateAll').checked = true;  // can't select nothing
    updateStateCount();
  }
  function selectAllStates(on) {
    el('stateAll').checked = !!on;
    stateBoxes().forEach(c => { c.checked = false; });
    updateStateCount();
  }

  async function init() {
    paintTabIcons();
    await renderStateGrid();
    loadSettings();
    loadHome();
  }
  window.addEventListener('pywebviewready', init);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
