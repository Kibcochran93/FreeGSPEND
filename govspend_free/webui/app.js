/* =========================================================================
   GovSpend Free — SolidJS frontend (buildless).
   Uses the vendored Solid runtime on window.Solid (see vendor/solid.iife.js)
   via the `html` tagged-template — no JSX, no build step.  All data flows over
   the pywebview bridge (window.pywebview.api.*); Python pushes progress back
   through window.onPyEvent / window.appendLog, which update Solid signals.
   ========================================================================= */
(function () {
  "use strict";
  var S = window.Solid;
  var html = S.html, render = S.render;
  var createSignal = S.createSignal, createMemo = S.createMemo, createEffect = S.createEffect;
  var For = S.For, Show = S.Show, onMount = S.onMount;

  var api = function () { return window.pywebview.api; };

  // ------------------------------------------------------------------ labels
  var TYPE_LABELS = {
    bid: "Bid/RFP", board_minutes: "Board minutes", transparency: "Spending",
    federal_award: "Federal grant", federal_rfp: "Federal RFP",
    federal_grant_opp: "Grant opportunity"
  };
  var typeLabel = function (t) { return TYPE_LABELS[t] || t || ""; };

  // Feather-style icons (static, trusted SVG inner markup).
  var ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    list: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    star: '<path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8-4.3-4.1 5.9-.9z"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.7-6.4"/><path d="M21 4v5h-5"/>',
    doc: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>',
    alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3.5 20c0-3 2.7-5 5.5-5s5.5 2 5.5 5"/><path d="M16 5.5a3 3 0 0 1 0 5.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'
  };
  function Icon(props) {
    return html`<svg class=${props.class || ""} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" innerHTML=${ICONS[props.name] || ICONS.doc}></svg>`;
  }
  var TABS = [
    { key: "home", label: "Home", icon: "home" },
    { key: "opps", label: "Opportunities", icon: "list" },
    { key: "search", label: "Search", icon: "search" },
    { key: "exp", label: "Expiring Contracts", icon: "clock" },
    { key: "ops", label: "Account Priorities", icon: "star" },
    { key: "scrape", label: "Update Data", icon: "refresh" }
  ];
  var CARD_ICONS = {
    "Data freshness": "clock", "Documents": "doc", "Opportunities": "list",
    "Expirations": "alert", "Competitor footprint": "users"
  };

  // ------------------------------------------------------------------- state
  var mqDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var theme = createSignal(mqDark ? "dark" : "light");
  var errNote = createSignal("");
  var activeTab = createSignal("home");

  var home = createSignal(null), homeErr = createSignal(false);

  var oppsAll = createSignal([]), oppsLoading = createSignal(false), oppsErr = createSignal(false);
  var fScore = createSignal(0), fKeyword = createSignal(""), fCategory = createSignal("");
  var fType = createSignal(""), fFrom = createSignal(""), fTo = createSignal(""), fOlder = createSignal(false);
  var oppExportNote = createSignal("");

  var searchQuery = createSignal(""), searchAll = createSignal([]), searchErr = createSignal(false);
  var searchDone = createSignal(false), searchBusy = createSignal(false);
  var sType = createSignal(""), sState = createSignal(""), searchExportNote = createSignal("");

  var expRows = createSignal([]), expDays = createSignal(180), expErr = createSignal(false);

  var opsGate = createSignal(null);           // {ok, reason} | null (loading)
  var playStatus = createSignal({ cls: "", text: "" });
  var playMarkdown = createSignal(""), playLog = createSignal(""), lastPlayMd = createSignal("");
  var playRunning = createSignal(false), playTools = createSignal(false);

  var states = createSignal([]), selectedStates = createSignal([]);
  var skip = {
    bids: createSignal(false), board_minutes: createSignal(false), transparency: createSignal(false),
    federal: createSignal(false), sam: createSignal(false), grants: createSignal(false),
    contracts: createSignal(false), contacts: createSignal(false), browser: createSignal(false)
  };
  var scrapeFrom = createSignal(""), scrapeTo = createSignal("");
  var scrapeKeyword = createSignal(""), scrapeCompetitor = createSignal("");
  var scrapeStatus = createSignal({ cls: "", text: "" }), scrapeLog = createSignal(""), scrapeRunning = createSignal(false);
  var autoUpdate = createSignal("off"), autoNote = createSignal(""), doctorText = createSignal(""), doctorShown = createSignal(false);

  var brief = createSignal({ open: false, title: "", status: { cls: "", text: "" }, body: "", path: "", busy: false });

  // read/write shorthand
  function get(sig) { return sig[0](); }
  function set(sig, v) { return sig[1](v); }

  // -------------------------------------------------------------- theme apply
  createEffect(function () { document.documentElement.dataset.theme = theme[0](); });

  // ------------------------------------------------------------------ loaders
  async function loadHome() {
    set(homeErr, false);
    try { set(home, await api().home_stats()); }
    catch (e) { set(homeErr, true); }
  }
  async function loadOpps() {
    set(oppsLoading, true); set(oppsErr, false);
    try { set(oppsAll, await api().opportunities(5000, get(fOlder)) || []); }
    catch (e) { set(oppsErr, true); set(oppsAll, []); }
    finally { set(oppsLoading, false); }
  }
  async function doSearch() {
    var q = get(searchQuery).trim();
    if (!q) { set(searchAll, []); set(searchDone, false); return; }
    set(searchBusy, true); set(searchErr, false); set(searchExportNote, "");
    try { set(searchAll, await api().search(q, 200) || []); set(searchDone, true); }
    catch (e) { set(searchErr, true); set(searchAll, []); set(searchDone, true); }
    finally { set(searchBusy, false); }
  }
  async function loadExp() {
    set(expErr, false);
    try { set(expRows, await api().expirations(parseInt(get(expDays), 10) || 180) || []); }
    catch (e) { set(expErr, true); set(expRows, []); }
  }
  async function checkOpsGate() {
    set(opsGate, null);
    try { set(opsGate, await api().ops_status()); }
    catch (e) { set(opsGate, { ok: false, reason: String(e) }); }
  }
  async function loadStates() {
    try { set(states, await api().list_states() || []); } catch (e) {}
  }
  async function loadSettings() {
    try {
      var s = await api().get_settings();
      set(autoUpdate, s.auto_update || "off");
      updateAutoNote(s);
    } catch (e) {}
  }
  function updateAutoNote(s) {
    if (s.auto_update && s.auto_update !== "off") {
      var every = s.auto_update === "daily" ? "day" : "week";
      var last = s.last_auto_update ? (" Last automatic update: " + String(s.last_auto_update).replace("T", " ") + ".") : "";
      set(autoNote, "On — updates about once a " + every + ", in the background whenever the app is open." + last);
    } else {
      set(autoNote, "Off — turn this on and you never have to click Update yourself.");
    }
  }

  // Trigger the right loader when the active tab changes (and on first render).
  createEffect(function () {
    var t = activeTab[0]();
    if (t === "home") loadHome();
    else if (t === "opps") loadOpps();
    else if (t === "exp") loadExp();
    else if (t === "ops") checkOpsGate();
  });

  // ------------------------------------------------------------- derived opps
  function effectiveDate(r) {
    if (r.date && !isNaN(Date.parse(r.date))) return new Date(r.date);
    if (r.scraped_at) { var d = new Date(String(r.scraped_at).replace(" ", "T")); if (!isNaN(d)) return d; }
    return null;
  }
  var oppCategories = createMemo(function () {
    var set2 = new Set();
    (oppsAll[0]() || []).forEach(function (r) {
      (r.categories || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (c) { set2.add(c); });
    });
    return Array.from(set2).sort();
  });
  var oppTypes = createMemo(function () {
    return Array.from(new Set((oppsAll[0]() || []).map(function (r) { return r.doc_type; }).filter(Boolean))).sort();
  });
  var oppsShown = createMemo(function () {
    var minScore = get(fScore) || 0, kw = get(fKeyword).trim().toLowerCase();
    var cat = get(fCategory), typ = get(fType);
    var from = get(fFrom) ? new Date(get(fFrom)) : null;
    var to = get(fTo) ? new Date(get(fTo)) : null;
    if (to) to.setHours(23, 59, 59, 999);
    return (oppsAll[0]() || []).filter(function (r) {
      if ((r.score || 0) < minScore) return false;
      if (typ && r.doc_type !== typ) return false;
      if (cat && (r.categories || "").indexOf(cat) === -1) return false;
      if (kw) {
        var hay = ((r.title || "") + " " + (r.institution || "") + " " + (r.state || "") + " " +
          (r.categories || "") + " " + (r.watchlist_hits || "")).toLowerCase();
        if (hay.indexOf(kw) === -1) return false;
      }
      if (from || to) {
        var d = effectiveDate(r);
        if (d) { if (from && d < from) return false; if (to && d > to) return false; }
      }
      return true;
    });
  });
  var searchShown = createMemo(function () {
    var typ = get(sType), st = get(sState);
    return (searchAll[0]() || []).filter(function (r) {
      return (!typ || r.doc_type === typ) && (!st || r.state === st);
    });
  });
  var searchTypes = createMemo(function () {
    return Array.from(new Set((searchAll[0]() || []).map(function (r) { return r.doc_type; }).filter(Boolean))).sort();
  });
  var searchStates = createMemo(function () {
    return Array.from(new Set((searchAll[0]() || []).map(function (r) { return r.state; }).filter(Boolean))).sort();
  });

  // --------------------------------------------------------------- csv export
  async function exportRows(rows, name, noteSig) {
    if (!rows || !rows.length) { set(noteSig, "Nothing to export yet."); return; }
    set(noteSig, "Saving…");
    try {
      var r = await api().export_rows_csv(rows, name);
      set(noteSig, r.ok ? ("Saved " + rows.length + " rows to " + r.path) : ("Export failed: " + r.error));
    } catch (e) { set(noteSig, "Export failed."); }
  }

  // ----------------------------------------------------------------- prettify
  function prettyState(key) {
    return String(key).split("_").map(function (w) { return w ? w[0].toUpperCase() + w.slice(1) : w; }).join(" ");
  }

  // -------------------------------------------------------- shared components
  function Ext(props) {
    return html`<a href="#" onClick=${function (e) { e.preventDefault(); if (props.url) api().open_external(props.url); }}>${function () { return props.text || props.url || ""; }}</a>`;
  }
  function Chips(props) {
    var items = function () { return String(props.value || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean); };
    return html`<${For} each=${items}>${function (c) { return html`<span class="chip">${c}</span>`; }}<//>`;
  }
  // Generic table. cols: [{label, cell:(row)=>node|string, cellClass?:(row)=>string}]
  // rows: accessor -> array.  empty: node.  cap: max rows drawn.
  function DataTable(props) {
    // In solid-js/html, function/signal props arrive as auto-resolved reactive
    // getters: read `props.rows` (already the current array), never call it.
    var cap = props.cap;
    var total = function () { return (props.rows || []).length; };
    var shown = createMemo(function () {
      var rs = props.rows || [];
      return (cap && rs.length > cap) ? rs.slice(0, cap) : rs;
    });
    return html`
      <${Show} when=${function () { return total() > 0; }} fallback=${props.empty}>
        <div class="tbl-wrap">
          <table>
            <thead><tr>${props.cols.map(function (c) { return html`<th>${c.label}</th>`; })}</tr></thead>
            <tbody>
              <${For} each=${shown}>${function (r) {
                return html`<tr>${props.cols.map(function (c) {
                  return html`<td class=${c.cellClass ? c.cellClass(r) : ""}>${function () { return c.cell(r); }}</td>`;
                })}</tr>`;
              }}<//>
            </tbody>
          </table>
          <${Show} when=${function () { return shown().length < total(); }}>
            <div class="tbl-note">Showing the top <strong>${function () { return shown().length; }}</strong> of ${total} matches, capped so the app stays responsive. Narrow with the filters above to surface the rest. (Export CSV includes all ${total}.)</div>
          <//>
        </div>
      <//>`;
  }
  function autoScroll(text) {
    // returns a ref callback that keeps a log box scrolled to the bottom
    return function (elm) { createEffect(function () { text(); elm.scrollTop = elm.scrollHeight; }); };
  }

  // ----------------------------------------------------------------- Home tab
  function HomeTab() {
    return html`
      <div class="panel">
        <div class="row">
          <span class="panel-title">Dashboard</span>
          <span class="spacer"></span>
          <button class="ghost" onClick=${loadHome}>${function () { return html`<span style="display:inline-flex;align-items:center;gap:6px">${html`<${Icon} name="refresh" class=""/>`} Refresh</span>`; }}</button>
        </div>
        <p class="hint">Status at a glance —
          <span class="dot rag-green"></span> good&nbsp;&nbsp;
          <span class="dot rag-amber"></span> watch&nbsp;&nbsp;
          <span class="dot rag-red"></span> act now.</p>

        <${Show} when=${function () { return home[0]() && !home[0]().has_data; }}>
          <div class="callout"><strong>Welcome! No data yet.</strong> Collect your first batch to fill this dashboard:
            <ol>
              <li>Go to <a onClick=${function () { set(activeTab, "scrape"); }}>Update Data</a>.</li>
              <li>Pick your states (or leave “All states”).</li>
              <li>Click <strong>Update everything</strong> and watch the progress log.</li>
            </ol>
            When it finishes, <a onClick=${function () { set(activeTab, "opps"); }}>Opportunities</a> and this dashboard fill in automatically.</div>
        <//>

        <${Show} when=${function () { return homeErr[0](); }}>
          <div class="empty">Couldn't load the dashboard. Click Refresh; if it keeps failing, open Update Data → Advanced → Check setup.</div>
        <//>

        <div class="homegrid">
          <${For} each=${function () { return (home[0]() || {}).cards || []; }}>${function (c) {
            return html`<div class=${"stat-card rag-" + (c.rag || "gray")}>
              <div class="k">${html`<${Icon} name=${CARD_ICONS[c.label] || "doc"}/>`} ${c.label}</div>
              <div class="v">${c.value}</div>
              <div class="s">${c.sub || ""}</div>
            </div>`;
          }}<//>
        </div>

        <div class="row" style="margin-top:20px; align-items:flex-start; gap:26px">
          <div style="flex:2; min-width:300px">
            <div class="subhead">Top opportunities</div>
            ${function () {
              return html`<${DataTable}
                cols=${[
                  { label: "Score", cell: function (r) { return r.score; }, cellClass: function () { return "score"; } },
                  { label: "Institution", cell: function (r) { return r.institution || ""; } },
                  { label: "Title", cell: function (r) { return html`<${Ext} url=${r.url} text=${r.title || "(untitled)"}/>`; } }
                ]}
                rows=${function () { return (home[0]() || {}).top_opportunities || []; }}
                empty=${html`<div class="empty">No opportunities yet.</div>`}/>`;
            }}
          </div>
          <div style="flex:1; min-width:220px">
            <div class="subhead">Competitor footprint</div>
            <${Show} when=${function () { return ((home[0]() || {}).top_competitors || []).length; }}
              fallback=${html`<div class="empty">No competitor spending data yet.</div>`}>
              <div style="margin-top:8px">
                <${For} each=${function () { return (home[0]() || {}).top_competitors || []; }}>${function (c) {
                  return html`<div class="minirow"><span>${c.vendor}</span><span class="n">${c.n}</span></div>`;
                }}<//>
              </div>
            <//>
          </div>
        </div>
      </div>`;
  }

  // --------------------------------------------------------- Opportunities tab
  function OppsTab() {
    var emptyNode = html`<div class="empty">${function () {
      return (oppsAll[0]() || []).length
        ? html`<span>No opportunities match your filters. <a onClick=${clearOppFilters}>Clear filters</a> to see all ${(oppsAll[0]() || []).length}.</span>`
        : html`<span>No opportunities yet. Go to <a onClick=${function () { set(activeTab, "scrape"); }}>Update Data</a> and click <strong>Update everything</strong>.</span>`;
    }}</div>`;
    return html`
      <div class="panel">
        <div class="row">
          <span class="panel-title">Ranked opportunities</span>
          <span class="spacer"></span>
          <button class="primary" disabled=${function () { return oppsLoading[0](); }} onClick=${loadOpps}>${function () { return oppsLoading[0]() ? "Refreshing…" : "Refresh"; }}</button>
        </div>
        <p class="hint">Everything ever scraped, scored by keyword strength + recency. Click a title to open it.</p>
        <div class="row" style="margin-top:8px">
          <label class="chk">Min score <input type="number" min="0" step="5" style="width:74px" value=${fScore[0]}
            onInput=${function (e) { set(fScore, parseFloat(e.target.value) || 0); }}></label>
          <input type="text" placeholder="filter: keyword / title / institution" style="min-width:230px" value=${fKeyword[0]}
            onInput=${function (e) { set(fKeyword, e.target.value); }}>
          <select value=${fCategory[0]} onChange=${function (e) { set(fCategory, e.target.value); }}>
            <option value="">All categories</option>
            <${For} each=${oppCategories}>${function (c) { return html`<option value=${c}>${c}</option>`; }}<//>
          </select>
          <select value=${fType[0]} onChange=${function (e) { set(fType, e.target.value); }}>
            <option value="">All types</option>
            <${For} each=${oppTypes}>${function (t) { return html`<option value=${t}>${typeLabel(t)}</option>`; }}<//>
          </select>
          <label class="chk">From <input type="date" value=${fFrom[0]} onInput=${function (e) { set(fFrom, e.target.value); }}></label>
          <label class="chk">To <input type="date" value=${fTo[0]} onInput=${function (e) { set(fTo, e.target.value); }}></label>
          <label class="chk" title="By default the feed hides items whose own date is more than a year old">
            <input type="checkbox" checked=${fOlder[0]} onChange=${function (e) { set(fOlder, e.target.checked); loadOpps(); }}> incl. &gt;1yr old</label>
          <button class="ghost" onClick=${clearOppFilters}>Clear</button>
          <button class="ghost" title="Save the rows shown below to a CSV you can open in Excel"
            onClick=${function () { exportRows(oppsShown(), "opportunities", oppExportNote); }}>Export CSV</button>
          <span class="spacer"></span>
          <span class="hint">${function () { return (oppsAll[0]() || []).length ? (oppsShown().length + " of " + (oppsAll[0]() || []).length) : ""; }}</span>
        </div>
        <p class="hint">${oppExportNote[0]}</p>
        ${function () {
          return html`<${DataTable}
            cols=${[
              { label: "Score", cell: function (r) { return r.score; }, cellClass: function () { return "score"; } },
              { label: "Type", cell: function (r) { return html`<span class="type">${typeLabel(r.doc_type)}</span>`; } },
              { label: "State / Institution", cell: function (r) { return (r.state || "") + " / " + (r.institution || ""); } },
              { label: "Title", cell: function (r) { return html`<${Ext} url=${r.url} text=${r.title || "(untitled)"}/>`; } },
              { label: "Tags", cell: function (r) { return html`<span>${html`<${Chips} value=${r.categories}/>`}${html`<${Chips} value=${r.watchlist_hits}/>`}</span>`; } },
              { label: "", cell: function (r) { return html`<button class="briefbtn" disabled=${function () { return brief[0]().busy; }} title="Generate an account brief from this document"
                  onClick=${function () { startBrief(r.id, r.institution || ("doc " + r.id)); }}>Brief</button>`; } }
            ]}
            rows=${oppsShown} cap=${300} empty=${emptyNode}/>`;
        }}
      </div>`;
  }
  function clearOppFilters() {
    var wasOld = get(fOlder);
    S.batch(function () {
      set(fScore, 0); set(fKeyword, ""); set(fCategory, ""); set(fType, ""); set(fFrom, ""); set(fTo, ""); set(fOlder, false);
    });
    if (wasOld) loadOpps();
  }

  // -------------------------------------------------------------- Search tab
  function SearchTab() {
    var emptyNode = html`<div class="empty">${function () {
      if (!searchDone[0]()) return "Type something to search.";
      return (searchAll[0]() || []).length
        ? html`<span>No results match these filters. <a onClick=${function () { set(sType, ""); set(sState, ""); }}>Clear</a> them.</span>`
        : html`<span>No matches. Try a different term, or collect more via <a onClick=${function () { set(activeTab, "scrape"); }}>Update Data</a>.</span>`;
    }}</div>`;
    return html`
      <div class="panel">
        <span class="panel-title">Search</span>
        <p class="tabhelp">Search everything you've collected — bid titles, board-minutes text, and spending rows. Try a vendor or a topic (e.g. <em>attendance software</em>, <em>Ellucian</em>, <em>K-12</em>).</p>
        <form class="row" onSubmit=${function (e) { e.preventDefault(); doSearch(); }}>
          <input class="grow" type="text" placeholder="Search titles & documents…" value=${searchQuery[0]}
            onInput=${function (e) { set(searchQuery, e.target.value); }}>
          <button class="primary" type="submit" disabled=${function () { return searchBusy[0](); }}>${function () { return searchBusy[0]() ? "Searching…" : "Search"; }}</button>
        </form>
        <div class="row" style="margin-top:10px">
          <select value=${sType[0]} onChange=${function (e) { set(sType, e.target.value); }}>
            <option value="">All types</option>
            <${For} each=${searchTypes}>${function (t) { return html`<option value=${t}>${typeLabel(t)}</option>`; }}<//>
          </select>
          <select value=${sState[0]} onChange=${function (e) { set(sState, e.target.value); }}>
            <option value="">All states</option>
            <${For} each=${searchStates}>${function (s) { return html`<option value=${s}>${s}</option>`; }}<//>
          </select>
          <button class="ghost" onClick=${function () { exportRows(searchShown(), "search", searchExportNote); }}>Export CSV</button>
          <span class="spacer"></span>
          <span class="hint">${function () { return (searchAll[0]() || []).length ? (searchShown().length + " of " + (searchAll[0]() || []).length) : ""; }}</span>
        </div>
        <p class="hint">${searchExportNote[0]}</p>
        ${function () {
          return html`<${DataTable}
            cols=${[
              { label: "Type", cell: function (r) { return html`<span class="type">${typeLabel(r.doc_type)}</span>`; } },
              { label: "State / Institution", cell: function (r) { return (r.state || "") + " / " + (r.institution || ""); } },
              { label: "Title", cell: function (r) { return html`<${Ext} url=${r.url} text=${r.title || "(untitled)"}/>`; } },
              { label: "Snippet", cell: function (r) { return r.snippet || ""; } }
            ]}
            rows=${searchShown} empty=${emptyNode}/>`;
        }}
      </div>`;
  }

  // ---------------------------------------------------------- Expirations tab
  function ExpTab() {
    return html`
      <div class="panel">
        <span class="panel-title">Expiring contracts</span>
        <p class="tabhelp">Vendor contracts with a known end date coming up — a renewal window is a good time to reach out. Only appears for states that publish contract end dates.</p>
        <div class="row">
          <label class="chk">Ending within <input type="number" min="1" max="3650" style="width:94px" value=${expDays[0]}
            onInput=${function (e) { set(expDays, parseInt(e.target.value, 10) || 180); }}> days</label>
          <button class="primary" onClick=${loadExp}>Show</button>
        </div>
        ${function () {
          return html`<${DataTable}
            cols=${[
              { label: "Vendor", cell: function (r) { return r.vendor || ""; } },
              { label: "State / Institution", cell: function (r) { return (r.state || "") + " / " + (r.institution || ""); } },
              { label: "Ends", cell: function (r) { return r.end_date || ""; } },
              { label: "Days left", cell: function (r) {
                  var n = r.days_until_expiration;
                  if (n == null) return "";
                  return (n <= 90) ? html`<span class="soon">${n}d</span>` : (n + "d");
                } },
              { label: "Source", cell: function (r) { return html`<${Ext} url=${r.source_url} text="source"/>`; } }
            ]}
            rows=${expRows[0]}
            empty=${html`<div class="empty">No contracts with an upcoming end date. This section only fills in for states that publish contract end dates (e.g. CA / GA / NC).</div>`}/>`;
        }}
      </div>`;
  }

  // ------------------------------------------------------------------ Ops tab
  function parseMarkdown(md) {
    var lines = (md || "").split("\n"), i = 0, blocks = [];
    var isSep = function (s) { return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(s) && s.indexOf("-") !== -1; };
    var cells = function (s) { return s.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) { return c.trim(); }); };
    while (i < lines.length) {
      var line = lines[i];
      if (line.indexOf("|") !== -1 && i + 1 < lines.length && isSep(lines[i + 1])) {
        var header = cells(line); i += 2; var body = [];
        while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) { body.push(cells(lines[i])); i++; }
        blocks.push({ t: "table", header: header, body: body }); continue;
      }
      var h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { blocks.push({ t: "h", level: h[1].length, text: h[2] }); i++; continue; }
      var li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (li) {
        var items = [];
        while (i < lines.length) { var mm = lines[i].match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/); if (!mm) break; items.push(mm[1]); i++; }
        blocks.push({ t: "ul", items: items }); continue;
      }
      if (line.trim()) blocks.push({ t: "p", text: line });
      i++;
    }
    return blocks;
  }
  function MarkdownView(props) {
    var blocks = createMemo(function () { return parseMarkdown(props.md); });
    return html`<div class="md"><${For} each=${blocks}>${function (b) {
      // NB: solid-js/html cannot take a string as a component (`<${'h1'}>` throws),
      // so headings are built as explicit conditional elements.
      if (b.t === "h") return b.level === 1 ? html`<h1>${b.text}</h1>` : b.level === 2 ? html`<h2>${b.text}</h2>` : html`<h3>${b.text}</h3>`;
      if (b.t === "ul") return html`<ul><${For} each=${function () { return b.items; }}>${function (it) { return html`<li>${it}</li>`; }}<//></ul>`;
      if (b.t === "p") return html`<p>${b.text}</p>`;
      // table
      return html`<div class="tbl-wrap"><table>
        <thead><tr>${b.header.map(function (hh) { return html`<th>${hh}</th>`; })}</tr></thead>
        <tbody>${b.body.map(function (row) { return html`<tr>${row.map(function (c) { return html`<td>${c}</td>`; })}</tr>`; })}</tbody>
      </table></div>`;
    }}<//></div>`;
  }

  function OpsTab() {
    return html`
      <div class="panel">
        <div class="row">
          <span class="panel-title">Account priorities</span>
          <span class="spacer"></span>
          <button class="primary" disabled=${function () { var g = opsGate[0](); return playRunning[0]() || !(g && g.ok); }} onClick=${runPlay}>Rank my accounts</button>
        </div>
        <p class="tabhelp">A ranked list of who to focus on this week: every account is scored 0–100 from its buying signals, matched to your CRM status, with the decision-maker and a ready-to-personalize opener. Your CRM is read <strong>read-only</strong> — nothing is ever written back.</p>

        <${Show} when=${function () { var g = opsGate[0](); return g && !g.ok; }}>
          <div class="gate-warn"><strong>One-time setup needed to turn this on.</strong><br>
            Account Priorities reads your CRM to rank accounts. It needs a one-time, <strong>read-only</strong> connection to HubSpot — nothing is ever written back. Once it's connected, this button switches on automatically. Ask whoever set up the tool, or open <em>Update Data → Advanced → Check setup</em> for the steps.
            <details style="margin-top:8px"><summary class="hint" style="cursor:pointer">Technical details</summary>
              <span class="hint">${function () { var g = opsGate[0](); return g ? String(g.reason || "") : ""; }}</span></details>
          </div>
        <//>

        <div class=${function () { return "status " + (playStatus[0]().cls || ""); }}>${function () { return playStatus[0]().text; }}</div>

        <${Show} when=${playTools[0]}>
          <div class="row" style="margin-top:8px">
            <button class="ghost" onClick=${copyPlay}>Copy table</button>
            <button class="ghost" onClick=${savePlayCsv}>Save as CSV</button>
          </div>
        <//>

        <${Show} when=${function () { return !!playMarkdown[0](); }}>
          ${function () { return html`<${MarkdownView} md=${playMarkdown[0]}/>`; }}
        <//>

        <details class="log" style="margin-top:14px">
          <summary class="hint">Run log</summary>
          <div class="log" ref=${autoScroll(playLog[0])}>${playLog[0]}</div>
        </details>
      </div>`;
  }
  async function runPlay() {
    S.batch(function () {
      set(playRunning, true); set(playStatus, { cls: "", text: "Running the Full Motion play (read-only)…" });
      set(playMarkdown, ""); set(playTools, false); set(lastPlayMd, ""); set(playLog, "");
    });
    try {
      var res = await api().run_play();
      if (!res.started) { set(playStatus, { cls: "err", text: res.error || "Could not start." }); set(playRunning, false); }
    } catch (e) { set(playStatus, { cls: "err", text: String(e) }); set(playRunning, false); }
  }
  function copyPlay() {
    var md = get(lastPlayMd);
    if (md && navigator.clipboard) { navigator.clipboard.writeText(md); set(playStatus, { cls: "ok", text: "Table copied to clipboard." }); }
  }
  async function savePlayCsv() {
    var md = get(lastPlayMd); if (!md) return;
    try { var r = await api().export_play_csv(md); set(playStatus, r.ok ? { cls: "ok", text: "Saved CSV: " + r.path } : { cls: "err", text: "CSV export failed: " + r.error }); }
    catch (e) { set(playStatus, { cls: "err", text: "CSV export failed." }); }
  }

  // ---------------------------------------------------------------- Scrape tab
  var allStatesOn = createMemo(function () { return (selectedStates[0]() || []).length === 0; });
  function toggleState(key, on) {
    var cur = selectedStates[0]().slice();
    var idx = cur.indexOf(key);
    if (on && idx === -1) cur.push(key);
    else if (!on && idx !== -1) cur.splice(idx, 1);
    set(selectedStates, cur);
  }
  function ScrapeTab() {
    var advSkips = [
      ["bids", "skip bids", ""], ["board_minutes", "skip board minutes", ""], ["transparency", "skip spending", ""],
      ["federal", "skip federal grants", ""],
      ["sam", "skip SAM.gov RFPs", "SAM.gov federal RFPs run once per full update when config/sam.yaml is enabled (uses your daily API key quota)"],
      ["grants", "skip Grants.gov opps", "Grants.gov federal grant opportunities run once per full update when config/grants_gov.yaml is enabled (keyless, no API key)"],
      ["contracts", "skip contracts", ""]
    ];
    return html`
      <div class="panel">
        <span class="panel-title">Update your data</span>
        <p class="tabhelp">Collect the latest bids, board minutes, spending, and federal opportunities from the public sources. This can take a few minutes — progress shows below.</p>

        <div class="callout" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
          ${html`<${Icon} name="refresh" class=""/>`}
          <label class="chk"><strong>Keep my data fresh automatically</strong>
            <select style="margin-left:8px" value=${autoUpdate[0]} onChange=${function (e) { set(autoUpdate, e.target.value); api().set_auto_update(e.target.value); updateAutoNote({ auto_update: e.target.value }); }}>
              <option value="off">Off</option>
              <option value="daily">Every day</option>
              <option value="weekly">Every week</option>
            </select>
          </label>
          <span class="hint" style="flex:1; min-width:240px">${autoNote[0]}</span>
        </div>

        <div class="row" style="align-items:flex-start">
          <div style="flex:1; min-width:260px">
            <div style="display:flex; align-items:center; gap:14px; margin-bottom:6px">
              <label class="chk" style="font-weight:600"><input type="checkbox" checked=${allStatesOn}
                onChange=${function () { set(selectedStates, []); }}> All states</label>
              <span class="hint">${function () { return allStatesOn() ? ("All " + (states[0]() || []).length + " states") : ((selectedStates[0]() || []).length + " selected"); }}</span>
              <a onClick=${function () { set(selectedStates, []); }}>clear</a>
            </div>
            <div class="state-grid">
              <${For} each=${states[0]}>${function (s) {
                return html`<label class="chk"><input type="checkbox"
                  checked=${function () { return (selectedStates[0]() || []).indexOf(s) !== -1; }}
                  onChange=${function (e) { toggleState(s, e.target.checked); }}> ${prettyState(s)}</label>`;
              }}<//>
            </div>
          </div>
          <button class="primary big" disabled=${scrapeRunning[0]} onClick=${runScrape}>${function () { return scrapeRunning[0]() ? "Updating…" : "Update everything"; }}</button>
        </div>
        <p class="hint">Runs every source for the state(s) you pick. Nationwide passes (SAM.gov, Grants.gov) run only when <em>All states</em> is on.</p>

        <details class="adv">
          <summary>Advanced options</summary>
          <p class="hint" style="margin-top:8px">Skip sources you don't need, or narrow the run. Leave everything unchecked to collect it all.</p>
          <div class="row">
            <${For} each=${function () { return advSkips; }}>${function (row) {
              return html`<label class="chk" title=${row[2]}><input type="checkbox" checked=${skip[row[0]][0]} onChange=${function (e) { set(skip[row[0]], e.target.checked); }}> ${row[1]}</label>`;
            }}<//>
            <label class="chk"><input type="checkbox" checked=${skip.contacts[0]} onChange=${function (e) { set(skip.contacts, e.target.checked); }}> include contact lookup (Apollo — uses credits)</label>
            <label class="chk" title="Render JavaScript-heavy sites with a headless browser (slower)"><input type="checkbox" checked=${skip.browser[0]} onChange=${function (e) { set(skip.browser, e.target.checked); }}> render JS-heavy sources (slower)</label>
          </div>
          <div class="row" style="margin-top:10px">
            <label class="chk">From <input type="date" value=${scrapeFrom[0]} onInput=${function (e) { set(scrapeFrom, e.target.value); }}></label>
            <label class="chk">To <input type="date" value=${scrapeTo[0]} onInput=${function (e) { set(scrapeTo, e.target.value); }}></label>
            <input type="text" placeholder="only keyword(s), comma-separated" style="min-width:210px" value=${scrapeKeyword[0]} onInput=${function (e) { set(scrapeKeyword, e.target.value); }}>
            <input type="text" placeholder="only competitor(s), e.g. Ellucian, Civitas" style="min-width:210px" value=${scrapeCompetitor[0]} onInput=${function (e) { set(scrapeCompetitor, e.target.value); }}>
          </div>
          <div class="row" style="margin-top:12px">
            <button class="ghost" onClick=${checkSetup}>Check setup</button>
            <span class="hint">Verify dependencies, config files, tokens, and what's in your local database.</span>
          </div>
          <${Show} when=${doctorShown[0]}>
            <pre class="brief-body" style="max-height:320px; margin-top:10px">${doctorText[0]}</pre>
          <//>
        </details>

        <div class=${function () { return "status " + (scrapeStatus[0]().cls || ""); }}>${function () { return scrapeStatus[0]().text; }}</div>
        <${Show} when=${function () { return !!scrapeLog[0](); }}>
          <div class="log" ref=${autoScroll(scrapeLog[0])}>${scrapeLog[0]}</div>
        <//>
      </div>`;
  }
  function selectedForRun() { return allStatesOn() ? [] : selectedStates[0]().slice(); }
  async function runScrape() {
    S.batch(function () {
      set(scrapeRunning, true); set(scrapeStatus, { cls: "", text: "Updating… this can take a few minutes." }); set(scrapeLog, "");
    });
    var opts = {
      states: selectedForRun(),
      skip_bids: get(skip.bids), skip_board_minutes: get(skip.board_minutes), skip_transparency: get(skip.transparency),
      skip_federal: get(skip.federal), skip_sam: get(skip.sam), skip_grants: get(skip.grants),
      skip_contracts: get(skip.contracts), skip_contacts: !get(skip.contacts),
      date_from: get(scrapeFrom), date_to: get(scrapeTo),
      only_keyword: get(scrapeKeyword), only_competitor: get(scrapeCompetitor),
      use_browser: get(skip.browser)
    };
    try {
      var res = await api().start_scrape(opts);
      if (!res.started) { set(scrapeStatus, { cls: "err", text: res.error || "Could not start." }); set(scrapeRunning, false); }
    } catch (e) { set(scrapeStatus, { cls: "err", text: String(e) }); set(scrapeRunning, false); }
  }
  async function checkSetup() {
    set(doctorShown, true); set(doctorText, "Checking…");
    try { var r = await api().doctor(); set(doctorText, r.text); }
    catch (e) { set(doctorText, "Could not run setup check: " + (e && e.message ? e.message : e)); }
  }

  // ------------------------------------------------------------- Brief modal
  function BriefModal() {
    return html`<${Show} when=${function () { return brief[0]().open; }}>
      <div class="overlay" onClick=${function (e) { if (e.target === e.currentTarget) closeBrief(); }}>
        <div class="modal">
          <div class="modal-head">
            <strong>${function () { return brief[0]().title || "Account brief"; }}</strong>
            <span class="spacer"></span>
            <button class="ghost" onClick=${copyBrief}>Copy</button>
            <button class="ghost" onClick=${closeBrief}>Close</button>
          </div>
          <div class=${function () { return "status " + (brief[0]().status.cls || ""); }}>${function () { return brief[0]().status.text; }}</div>
          <pre class="brief-body">${function () { return brief[0]().body; }}</pre>
          <div class="hint">${function () { return brief[0]().path ? ("Saved to: " + brief[0]().path) : ""; }}</div>
        </div>
      </div>
    <//>`;
  }
  function patchBrief(p) { set(brief, Object.assign({}, get(brief), p)); }
  function closeBrief() { patchBrief({ open: false }); }
  function copyBrief() { var t = get(brief).body || ""; if (navigator.clipboard) navigator.clipboard.writeText(t); }
  async function startBrief(id, label) {
    set(brief, { open: true, title: "Account brief: " + label, status: { cls: "", text: "Generating via claude — this can take up to a couple of minutes…" }, body: "", path: "", busy: true });
    try {
      var res = await api().start_brief(String(id));
      if (!res.started) patchBrief({ status: { cls: "err", text: res.error || "Could not start." }, busy: false });
    } catch (e) { patchBrief({ status: { cls: "err", text: String(e) }, busy: false }); }
  }

  // ------------------------------------------------- Python -> UI event bridge
  window.appendLog = function (line) { set(scrapeLog, get(scrapeLog) + line + "\n"); };
  window.onPyEvent = function (event, payload) {
    payload = payload || {};
    if (event === "playLog") { set(playLog, get(playLog) + payload.line + "\n"); }
    else if (event === "playDone") {
      S.batch(function () {
        set(playStatus, { cls: "ok", text: "Done." + (payload.report_path ? " Saved: " + payload.report_path : "") });
        set(lastPlayMd, payload.markdown || ""); set(playMarkdown, payload.markdown || "");
        set(playTools, true); set(playRunning, false);
      });
    } else if (event === "playError") { set(playStatus, { cls: "err", text: "Error: " + payload.error }); set(playRunning, false); }
    else if (event === "scrapeDone") {
      var c = payload;
      set(scrapeStatus, { cls: "ok", text:
        "Done — " + c.bids + " bids, " + c.minutes + " minutes, " + c.transparency + " transparency, " +
        c.federal + " federal, " + c.federal_grant_opps + " grant-opps, " + c.contracts + " contracts (" +
        c.contracts_expiring_soon + " expiring soon), " + c.contacts + " contacts, " + c.skipped + " sources skipped. Stored in the database." });
      set(scrapeRunning, false);
      loadOpps(); loadHome();
    } else if (event === "scrapeError") { set(scrapeStatus, { cls: "err", text: "Error: " + payload.error }); set(scrapeRunning, false); }
    else if (event === "briefDone") {
      patchBrief({ status: { cls: "ok", text: "Brief ready for " + payload.institution + "." }, body: payload.markdown || "", path: payload.path || "", busy: false });
    } else if (event === "briefError") { patchBrief({ status: { cls: "err", text: "Error: " + payload.error }, busy: false }); }
  };

  // --------------------------------------------------------------- App shell
  function App() {
    return html`
      <header>
        <span class="logo" aria-hidden="true">${html`<${LogoMark}/>`}</span>
        <h1>GovSpend&nbsp;Free</h1>
        <span class="sub">local procurement dashboard</span>
        <span class="spacer"></span>
        <span class="errnote">${errNote[0]}</span>
        <button class="iconbtn" title="Toggle light / dark" onClick=${function () { set(theme, get(theme) === "dark" ? "light" : "dark"); }}>
          ${function () { return html`<${Icon} name=${theme[0]() === "dark" ? "sun" : "moon"}/>`; }}
        </button>
      </header>

      <nav class="tabs">
        <${For} each=${function () { return TABS; }}>${function (t) {
          return html`<button class=${function () { return "tab" + (activeTab[0]() === t.key ? " active" : ""); }}
            onClick=${function () { set(activeTab, t.key); }}>${html`<${Icon} name=${t.icon}/>`} ${t.label}</button>`;
        }}<//>
      </nav>

      <main>
        <${Show} when=${function () { return activeTab[0]() === "home"; }}>${function () { return html`<${HomeTab}/>`; }}<//>
        <${Show} when=${function () { return activeTab[0]() === "opps"; }}>${function () { return html`<${OppsTab}/>`; }}<//>
        <${Show} when=${function () { return activeTab[0]() === "search"; }}>${function () { return html`<${SearchTab}/>`; }}<//>
        <${Show} when=${function () { return activeTab[0]() === "exp"; }}>${function () { return html`<${ExpTab}/>`; }}<//>
        <${Show} when=${function () { return activeTab[0]() === "ops"; }}>${function () { return html`<${OpsTab}/>`; }}<//>
        <${Show} when=${function () { return activeTab[0]() === "scrape"; }}>${function () { return html`<${ScrapeTab}/>`; }}<//>
      </main>

      ${html`<${BriefModal}/>`}`;
  }
  function LogoMark() {
    return html`<svg width="26" height="26" viewBox="0 0 26 26" fill="none" xmlns="http://www.w3.org/2000/svg"
      innerHTML='<rect x="1" y="1" width="24" height="24" rx="6" fill="#2563eb"/><rect x="5.6" y="13" width="3.2" height="7" rx="1" fill="#fff"/><rect x="11.4" y="9" width="3.2" height="11" rx="1" fill="#fff"/><rect x="17.2" y="6" width="3.2" height="14" rx="1" fill="#fff"/><circle cx="19" cy="7" r="2.4" fill="#93c5fd"/>'></svg>`;
  }

  // ------------------------------------------------------------------ boot
  // Surface uncaught errors in the header.
  window.addEventListener("error", function (ev) { set(errNote, "JS error: " + (ev.message || ev.error)); });
  window.addEventListener("unhandledrejection", function (ev) {
    set(errNote, "bridge error: " + (ev.reason && ev.reason.message ? ev.reason.message : ev.reason));
  });

  var booted = false;
  function boot() {
    if (booted) return; booted = true;
    try {
      render(App, document.getElementById("app"));
    } catch (e) {
      set(errNote, "UI failed to start: " + (e && e.message ? e.message : e));
      throw e;
    }
    loadStates();
    loadSettings();
  }
  window.addEventListener("pywebviewready", boot);
})();
