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

from . import brief, db, opportunities, pipeline, utils
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

    def set_window(self, window) -> None:
        self._window = window

    # -------------------- read-only dashboard queries --------------------

    def list_states(self) -> list[str]:
        return list(_load_yaml(CONFIG_DIR / "sources.yaml").keys())

    def opportunities(self, limit: int = 50) -> list[dict]:
        conn = db.get_conn()
        try:
            return opportunities.rank_opportunities(conn, limit=int(limit))
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

    def open_external(self, url: str) -> None:
        """Open a result link in the user's real browser, not the app window."""
        if url:
            webbrowser.open(url)

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
                    selected_state=(options.get("state") or None),
                    skip_bids=options.get("skip_bids", False),
                    skip_board_minutes=options.get("skip_board_minutes", False),
                    skip_transparency=options.get("skip_transparency", False),
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


def main() -> None:
    utils.setup_logging()
    import webview  # lazy: only needed to actually open the window

    api = Api()
    window = webview.create_window(
        "GovSpend Free",
        html=HTML,
        js_api=api,
        width=1120,
        height=800,
        min_size=(860, 620),
    )
    api.set_window(window)
    webview.start()


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
  #scrapeLog {
    margin-top: 14px; background: #0f172a; color: #cbd5e1; border-radius: 8px;
    padding: 12px; height: 260px; overflow: auto; white-space: pre-wrap;
    font: 12px/1.5 "Cascadia Code", Consolas, monospace;
  }
  .status { margin-top: 10px; font-size: 13px; }
  .status.ok { color: #15803d; } .status.err { color: var(--soon); }
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
    <h1>GovSpend&nbsp;Free</h1>
    <span class="sub">local procurement dashboard</span>
    <span class="spacer"></span>
    <span class="sub" id="dbNote"></span>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="opps">Opportunities</button>
    <button class="tab" data-tab="search">Search</button>
    <button class="tab" data-tab="exp">Expirations</button>
    <button class="tab" data-tab="scrape">Scrape</button>
  </nav>

  <main>
    <!-- Opportunities -->
    <section id="tab-opps" class="tabpane">
      <div class="panel">
        <div class="row">
          <strong>Ranked opportunities</strong>
          <span class="spacer" style="flex:1"></span>
          <button class="primary" onclick="loadOpps()">Refresh</button>
        </div>
        <p class="hint">Everything ever scraped, scored by keyword strength + recency. Click a title to open it.</p>
        <div class="row" id="oppFilters" style="margin-top:4px">
          <label class="chk">Min score <input type="number" id="fScore" value="0" min="0" step="5" style="width:70px" oninput="applyOppFilters()"></label>
          <input type="text" id="fKeyword" placeholder="filter: keyword / title / institution" style="min-width:220px" oninput="applyOppFilters()">
          <select id="fCategory" onchange="applyOppFilters()"><option value="">All categories</option></select>
          <label class="chk">From <input type="date" id="fFrom" oninput="applyOppFilters()"></label>
          <label class="chk">To <input type="date" id="fTo" oninput="applyOppFilters()"></label>
          <button class="ghost" onclick="clearOppFilters()">Clear</button>
          <span class="spacer" style="flex:1"></span>
          <span class="hint" id="oppCount"></span>
        </div>
        <div id="oppsTable"></div>
      </div>
    </section>

    <!-- Search -->
    <section id="tab-search" class="tabpane hidden">
      <div class="panel">
        <form class="row" onsubmit="doSearch(); return false;">
          <input type="text" id="q" placeholder="Full-text search titles &amp; documents (e.g. attendance software)">
          <button class="primary" type="submit">Search</button>
        </form>
        <div id="searchTable"></div>
      </div>
    </section>

    <!-- Expirations -->
    <section id="tab-exp" class="tabpane hidden">
      <div class="panel">
        <div class="row">
          <label class="chk">Within <input type="number" id="days" value="180" min="1" max="3650" style="width:90px"> days</label>
          <button class="primary" onclick="loadExp()">Show</button>
        </div>
        <div id="expTable"></div>
      </div>
    </section>

    <!-- Scrape -->
    <section id="tab-scrape" class="tabpane hidden">
      <div class="panel">
        <div class="row">
          <label class="chk">State:
            <select id="state"><option value="">All states</option></select>
          </label>
          <label class="chk"><input type="checkbox" id="skip_bids"> skip bids</label>
          <label class="chk"><input type="checkbox" id="skip_board_minutes"> skip minutes</label>
          <label class="chk"><input type="checkbox" id="skip_transparency"> skip transparency</label>
          <label class="chk"><input type="checkbox" id="skip_contracts"> skip contracts</label>
          <label class="chk"><input type="checkbox" id="inc_contacts"> include Apollo contacts (uses credits)</label>
          <label class="chk"><input type="checkbox" id="use_browser"> render JS sources (headless browser, slower)</label>
        </div>
        <div class="row" style="margin-top:10px">
          <label class="chk">From <input type="date" id="scrape_from"></label>
          <label class="chk">To <input type="date" id="scrape_to"></label>
          <input type="text" id="scrape_keyword" placeholder="only keyword(s), comma-separated" style="min-width:200px">
          <input type="text" id="scrape_competitor" placeholder="only competitor(s), e.g. Ellucian, Civitas" style="min-width:200px">
        </div>
        <div class="row" style="margin-top:12px">
          <button class="primary" id="runBtn" onclick="runScrape()">Run scrape</button>
          <span class="hint">Criteria are optional filters &mdash; leave blank to scrape everything. A date range also skips downloading out-of-range minutes PDFs.</span>
        </div>
        <div id="scrapeStatus" class="status"></div>
        <div id="scrapeLog"></div>
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
  const api = () => window.pywebview.api;
  const el = (id) => document.getElementById(id);

  // ---- tab switching ----
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.tabpane').forEach(p => p.classList.add('hidden'));
    el('tab-' + t.dataset.tab).classList.remove('hidden');
  });

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
  function renderTable(mount, cols, rows, buildRow) {
    const m = el(mount); m.innerHTML = '';
    if (!rows || !rows.length) { m.innerHTML = '<div class="empty">Nothing to show yet.</div>'; return; }
    const table = document.createElement('table');
    const thead = document.createElement('thead'); const htr = document.createElement('tr');
    cols.forEach(c => { const th = document.createElement('th'); th.textContent = c; htr.appendChild(th); });
    thead.appendChild(htr); table.appendChild(thead);
    const tb = document.createElement('tbody');
    rows.forEach(r => tb.appendChild(buildRow(r)));
    table.appendChild(tb); m.appendChild(table);
  }

  // ---- Opportunities ----
  let _oppsAll = [];

  async function loadOpps() {
    _oppsAll = await api().opportunities(200);
    // Rebuild the category dropdown from whatever's in the data.
    const cats = new Set();
    _oppsAll.forEach(r => (r.categories || '').split(',').map(s => s.trim()).filter(Boolean).forEach(c => cats.add(c)));
    const sel = el('fCategory'), cur = sel.value;
    sel.innerHTML = '<option value="">All categories</option>';
    [...cats].sort().forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; sel.appendChild(o); });
    sel.value = cur;
    applyOppFilters();
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
    const from = el('fFrom').value ? new Date(el('fFrom').value) : null;
    let to = el('fTo').value ? new Date(el('fTo').value) : null;
    if (to) to.setHours(23, 59, 59, 999);  // inclusive end-of-day
    const rows = _oppsAll.filter(r => {
      if (r.score < minScore) return false;
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
    el('oppCount').textContent = `${rows.length} of ${_oppsAll.length}`;
    renderOppRows(rows);
  }

  function clearOppFilters() {
    el('fScore').value = 0; el('fKeyword').value = ''; el('fCategory').value = '';
    el('fFrom').value = ''; el('fTo').value = '';
    applyOppFilters();
  }

  function renderOppRows(rows) {
    renderTable('oppsTable', ['Score', 'Type', 'State / Institution', 'Title', 'Tags', ''], rows, (r) => {
      const tr = document.createElement('tr');
      const s = td(r.score); s.className = 'score';
      tr.appendChild(s);
      tr.appendChild(td(Object.assign(document.createElement('span'), {className:'type', textContent:r.doc_type})));
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
    });
  }

  // ---- Search ----
  async function doSearch() {
    const q = el('q').value;
    const rows = await api().search(q, 100);
    renderTable('searchTable', ['Type', 'State / Institution', 'Title', 'Snippet'], rows, (r) => {
      const tr = document.createElement('tr');
      tr.appendChild(td(Object.assign(document.createElement('span'), {className:'type', textContent:r.doc_type})));
      tr.appendChild(td(`${r.state || ''} / ${r.institution || ''}`));
      tr.appendChild(td(link(r.url, r.title || '(untitled)')));
      tr.appendChild(td(r.snippet || ''));
      return tr;
    });
  }

  // ---- Expirations ----
  async function loadExp() {
    const days = parseInt(el('days').value, 10) || 180;
    const rows = await api().expirations(days);
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
    });
  }

  // ---- Scrape ----
  window.appendLog = (line) => {
    const box = el('scrapeLog');
    box.textContent += (line + '\n');
    box.scrollTop = box.scrollHeight;
  };
  window.onPyEvent = (event, payload) => {
    if (event === 'scrapeDone') {
      const c = payload;
      el('scrapeStatus').className = 'status ok';
      el('scrapeStatus').textContent =
        `Done - ${c.bids} bids, ${c.minutes} minutes, ${c.transparency} transparency, ` +
        `${c.contracts} contracts (${c.contracts_expiring_soon} expiring soon), ${c.contacts} contacts, ` +
        `${c.skipped} sources skipped.` +
        (c.reports ? ` ${c.reports} report file(s) in ${c.reports_dir}` : '');
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
    el('scrapeStatus').textContent = 'Scraping...';
    el('scrapeLog').textContent = '';
    const opts = {
      state: el('state').value,
      skip_bids: el('skip_bids').checked,
      skip_board_minutes: el('skip_board_minutes').checked,
      skip_transparency: el('skip_transparency').checked,
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

  // ---- init ----
  async function init() {
    const states = await api().list_states();
    const sel = el('state');
    states.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); });
    loadOpps();
  }
  window.addEventListener('pywebviewready', init);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
