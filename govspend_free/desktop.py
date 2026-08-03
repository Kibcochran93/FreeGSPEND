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
        html=_build_html(),
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
# The UI. A SolidJS single-page app (buildless) lives in webui/; here we
# assemble it into one self-contained page for the pywebview window by inlining
# the stylesheet, the vendored Solid runtime, and the app script. No web server
# and no external assets. All data still flows over the pywebview bridge
# (window.pywebview.api.*). The browser-only mock bridge (webui/mock.js) is
# dropped from this build.  Edit the UI by editing the files in webui/.
# --------------------------------------------------------------------------
_WEBUI = Path(__file__).with_name("webui")


def _build_html() -> str:
    """Return the complete HTML document for the desktop window, with the
    stylesheet, Solid runtime and app JS inlined from webui/."""
    idx = (_WEBUI / "index.html").read_text(encoding="utf-8")
    css = (_WEBUI / "styles.css").read_text(encoding="utf-8")
    solid = (_WEBUI / "vendor" / "solid.iife.js").read_text(encoding="utf-8")
    app = (_WEBUI / "app.js").read_text(encoding="utf-8")
    idx = idx.replace(
        '<link rel="stylesheet" href="styles.css">',
        "<style>\n" + css + "\n</style>",
    )
    idx = idx.replace(
        '<script src="vendor/solid.iife.js"></script>',
        "<script>\n" + solid + "\n</script>",
    )
    # browser-preview only — never ship the mock bridge into the desktop window
    idx = idx.replace('<script src="mock.js"></script>', "")
    idx = idx.replace(
        '<script src="app.js"></script>',
        "<script>\n" + app + "\n</script>",
    )
    return idx


if __name__ == "__main__":
    main()
