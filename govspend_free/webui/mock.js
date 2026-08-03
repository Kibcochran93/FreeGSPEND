/* =========================================================================
   Browser-preview mock of the pywebview bridge.  Only installs when the real
   window.pywebview.api is absent (i.e. opened in a normal browser, not the
   desktop window).  Lets you develop / visually verify the UI without pywebview
   and without a live database.  desktop.py strips this file from the inlined
   build, so it never ships in the desktop window.
   ========================================================================= */
(function () {
  if (window.pywebview && window.pywebview.api) return; // real bridge present

  var wait = function (ms, val) { return new Promise(function (r) { setTimeout(function () { r(val); }, ms); }); };

  var OPPS = [
    { id: 1, score: 92, doc_type: "bid", state: "Texas", institution: "University of Houston System", title: "Student Attendance & Engagement Platform RFP 24-071", url: "https://example.edu/rfp/24-071", categories: "Attendance, Retention", watchlist_hits: "Ellucian", date: "2026-07-12", scraped_at: "2026-07-30 09:12:00" },
    { id: 2, score: 88, doc_type: "federal_grant_opp", state: "", institution: "U.S. Dept of Education", title: "TRIO Student Support Services — FY26 Competition", url: "https://grants.gov/opp/trio-fy26", categories: "Student Success", watchlist_hits: "", date: "2026-07-28", scraped_at: "2026-07-31 06:00:00" },
    { id: 3, score: 81, doc_type: "board_minutes", state: "Arkansas", institution: "University of Arkansas System", title: "Board of Trustees — June 2026 Minutes (LMS discussion)", url: "https://uasys.edu/board/2026-06.pdf", categories: "LMS", watchlist_hits: "Anthology, Canvas", date: "2026-06-18", scraped_at: "2026-07-29 14:22:00" },
    { id: 4, score: 74, doc_type: "bid", state: "California", institution: "Los Angeles Community College District", title: "Early-Alert / Case Management Software", url: "https://laccd.planetbids.com/e/12345", categories: "Retention, Advising", watchlist_hits: "Civitas", date: "2026-07-05", scraped_at: "2026-07-30 09:40:00" },
    { id: 5, score: 66, doc_type: "federal_award", state: "Missouri", institution: "Metropolitan Community College", title: "GEAR UP award — cohort 2026", url: "https://usaspending.gov/award/abc", categories: "Student Success", watchlist_hits: "", date: "2026-05-30", scraped_at: "2026-07-30 09:41:00" },
    { id: 6, score: 58, doc_type: "bid", state: "Georgia", institution: "University System of Georgia", title: "Enterprise Scheduling & Room Utilization", url: "https://bids.sciquest.com/e/999", categories: "Scheduling", watchlist_hits: "", date: "2025-04-02", scraped_at: "2026-07-30 09:42:00" }
  ];
  var SEARCH = OPPS.map(function (o) { return Object.assign({ snippet: "…matched “" + (o.watchlist_hits || o.categories || "attendance") + "” in the document text…" }, o); });
  var EXP = [
    { vendor: "Ellucian", state: "California", institution: "Foothill-De Anza CCD", end_date: "2026-09-30", days_until_expiration: 58, source_url: "https://example.edu/contract/1" },
    { vendor: "Anthology", state: "Georgia", institution: "University System of Georgia", end_date: "2026-12-15", days_until_expiration: 134, source_url: "https://example.edu/contract/2" }
  ];
  var STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "florida", "georgia", "iowa", "kansas", "missouri", "montana", "nebraska", "new_hampshire", "north_carolina", "oklahoma", "tennessee", "texas", "utah"];
  var PLAY_MD = [
    "# Full Motion — account priorities",
    "",
    "| Rank | Account | Score | CRM status | Decision-maker | Opener |",
    "|---|---|---|---|---|---|",
    "| 1 | University of Houston System | 92 | Open opp | VP Student Success | Saw your 24-071 attendance RFP — we cut no-shows 18% at a peer TX system. |",
    "| 2 | Univ. of Arkansas System | 81 | Lead | Registrar | Your June board flagged LMS gaps; worth a 20-min look at our early-alert. |",
    "| 3 | LACCD | 74 | None | Dean of Students | Your early-alert bid closes soon — happy to share a peer CCD reference. |",
    "",
    "## Top 3 plays this week",
    "- Reply to UH RFP with the TX case study before the 15th.",
    "- Warm intro to Arkansas registrar via the June board contact.",
    "- Send LACCD the Foothill-De Anza reference one-pager."
  ].join("\n");

  var api = {
    list_states: function () { return wait(60, STATES.slice()); },
    home_stats: function () {
      return wait(160, {
        has_data: true,
        cards: [
          { label: "Data freshness", value: "3d", sub: "last update Jul 31", rag: "green" },
          { label: "Documents", value: "4,812", sub: "across 20 states", rag: "green" },
          { label: "Opportunities", value: "137", sub: "score ≥ 40", rag: "amber" },
          { label: "Expirations", value: "6", sub: "within 90 days", rag: "red" },
          { label: "Competitor footprint", value: "23", sub: "vendors tracked", rag: "gray" }
        ],
        top_opportunities: OPPS.slice(0, 4).map(function (o) { return { score: o.score, institution: o.institution, title: o.title, url: o.url }; }),
        top_competitors: [
          { vendor: "Ellucian", n: 14 }, { vendor: "Anthology", n: 9 },
          { vendor: "Civitas", n: 6 }, { vendor: "Canvas / Instructure", n: 5 }
        ]
      });
    },
    opportunities: function (limit, includeOld) {
      var rows = OPPS.slice();
      if (!includeOld) rows = rows.filter(function (r) { return r.date >= "2025-08-01"; });
      return wait(220, rows);
    },
    search: function (q, limit) {
      q = (q || "").toLowerCase();
      return wait(220, SEARCH.filter(function (r) {
        return (r.title + " " + r.institution + " " + r.snippet + " " + r.watchlist_hits).toLowerCase().indexOf(q) !== -1;
      }));
    },
    expirations: function (days) { return wait(160, EXP.filter(function (r) { return r.days_until_expiration <= days; })); },
    export_rows_csv: function (rows, name) { return wait(120, { ok: true, path: "reports\\" + name + "_mock.csv" }); },
    export_play_csv: function (md) { return wait(120, { ok: true, path: "reports\\play_mock.csv" }); },
    open_external: function (url) { window.open(url, "_blank", "noopener"); },
    doctor: function () { return wait(200, { text: "[mock] setup check\n  deps: OK\n  db rows: 4,812\n  hubspot.yaml: found (read-only)\n  sam.yaml: disabled\n  grants_gov.yaml: enabled (keyless)" }); },
    ops_status: function () { return wait(140, { ok: true }); },
    get_settings: function () { return wait(80, { auto_update: "weekly", last_auto_update: "2026-07-31T06:00:00" }); },
    set_auto_update: function (mode) { return wait(60, { ok: true }); },
    start_scrape: function (opts) {
      setTimeout(function () { window.appendLog && window.appendLog("[mock] scraping " + ((opts.states && opts.states.length) ? opts.states.join(", ") : "all states") + "…"); }, 250);
      setTimeout(function () { window.appendLog && window.appendLog("[mock] bonfire: 3 new  •  planetbids: 1 new  •  grants.gov: 2 opps"); }, 900);
      setTimeout(function () {
        window.onPyEvent && window.onPyEvent("scrapeDone", { bids: 4, minutes: 2, transparency: 0, federal: 5, federal_grant_opps: 2, contracts: 1, contracts_expiring_soon: 1, contacts: 0, skipped: 3 });
      }, 1600);
      return Promise.resolve({ started: true });
    },
    run_play: function () {
      setTimeout(function () { window.onPyEvent && window.onPyEvent("playLog", { line: "[mock] reading HubSpot (read-only)…" }); }, 300);
      setTimeout(function () { window.onPyEvent && window.onPyEvent("playLog", { line: "[mock] scoring 42 accounts…" }); }, 900);
      setTimeout(function () { window.onPyEvent && window.onPyEvent("playDone", { markdown: PLAY_MD, report_path: "reports\\play_mock.md" }); }, 1700);
      return Promise.resolve({ started: true });
    },
    start_brief: function (id) {
      setTimeout(function () {
        window.onPyEvent && window.onPyEvent("briefDone", {
          institution: "University of Houston System",
          markdown: "[mock] Account brief\n\nUH System is actively procuring an attendance & engagement platform (RFP 24-071). Ellucian is the incumbent SIS. Angle: peer TX outcome (18% no-show reduction). Decision-maker: VP Student Success. Next step: reply to RFP with the case study before Jul 15.",
          path: "reports\\brief_uh_mock.md"
        });
      }, 1400);
      return Promise.resolve({ started: true });
    }
  };

  window.pywebview = { api: api };
  // Boot the app exactly like the desktop window does.
  window.addEventListener("DOMContentLoaded", function () {
    window.dispatchEvent(new Event("pywebviewready"));
  });
  // If DOM is already parsed by the time this runs, fire on next tick.
  if (document.readyState !== "loading") {
    setTimeout(function () { window.dispatchEvent(new Event("pywebviewready")); }, 0);
  }
})();
