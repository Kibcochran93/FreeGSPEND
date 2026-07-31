# govspend_free

A free, self-hosted stand-in for GovSpend, built around 10 pilot states
(AR, TX, FL, CA, GA, OH, NC, TN, IL, PA). You run it yourself, on your own
machine, on whatever schedule you want. No subscription, no vendor lock-in
- just a Python project you own.

## Which GovSpend modules this covers

| GovSpend module         | Here                                                              | Cost              |
| ------------------------ | ------------------------------------------------------------------ | ----------------- |
| Bids & RFPs               | `bid_scraper.py` - native HTML bid boards; `bonfire.py` - Bonfire JSON portals; `ionwave.py` - Ion Wave RadGrid portals (all browser-free) | Free              |
| Meeting Intelligence      | `board_minutes_scraper.py` - downloads + keyword-searches minutes  | Free              |
| Spending & POs            | `transparency_scraper.py` - best-effort CSV/PDF downloads          | Free              |
| Co-Ops & Contracts        | `contracts_scraper.py` - detects vendor/start/end date columns, flags expirations | Free |
| Contacts                  | `contacts.py` + `apollo_client.py` - real Apollo.io People Search  | Free search, Apollo credits for email reveal |
| Agencies                  | not built (see "What this doesn't do")                             | -                  |
| AI Search + Notebook      | `llm.py` `--ask` - RAG over your local data via Claude              | Anthropic API rates |
| Record-Level Chat         | `llm.py` `--chat <id>` - REPL over one document                     | Anthropic API rates |
| Dashboard                 | `--opportunities` (terminal) **or** the desktop UI (`govspend-free-ui`) | Free           |
| Alerts                    | `alerts.py` - SMTP email digest after each run                      | Free (your own email account) |
| CRM Integration           | read-only HubSpot lookup in the Ops play; otherwise query `db/govspend_free.db` directly | Free |
| Opportunities             | `opportunities.py` - rule-based scoring, not AI-ranked               | Free               |

Everything free-tier runs with `pip install -r requirements.txt` and no
API keys. Three modules are opt-in and require your own account/keys:
Apollo.io (Contacts), Anthropic (AI Search/Chat), and SMTP credentials
(Alerts). None of the free modules depend on these - skip all three and
you still get Bids, Meeting Intelligence, Spending, Contracts, unified
search, and rule-based Opportunities.

## Install

```bash
cd govspend_free
pip install -r requirements.txt
```

Or install it as an editable package (adds a `govspend-free` command and lets
`import govspend_free` resolve from anywhere - handy for the tests and for
hacking on it):

```bash
pip install -e .          # then: govspend-free --list-states
```

Install editable rather than a plain `pip install .`: the tool reads `config/`
and writes `db/`, `reports/`, `state/`, and `cache/` relative to its own
location, so it needs to keep running from this project folder.

Requires Python 3.10+. Copy the whole project folder to a **plain local
folder**, not a network drive or cloud-sync mount (OneDrive/Dropbox live-
sync folders are usually fine; unusual network filesystems can break
SQLite's file locking - see Troubleshooting below if you hit a "disk I/O
error").

## Verify it works (no network, no API keys required)

The tests use `pytest`:

```bash
pip install -r requirements-dev.txt   # one-time: installs pytest
# (or, with the editable install above:  pip install -e ".[dev]")
pytest                                # runs both offline suites
```

This exercises bid + minutes scraping against local fixtures, plus `db.py`,
`contracts_scraper.py` (including content-based re-scan of updated CSVs), and
`opportunities.py` - all offline, no API keys. Re-run after any edits to
`govspend_free/*.py`.

## Run it for real

```bash
python main.py                 # scrape every configured state
python main.py --state texas   # just one state (state key is case-insensitive)
python main.py --list-states
python main.py --skip-transparency --skip-contracts --skip-contacts   # pick and choose passes
python main.py --quiet         # only warnings/errors; still prints the report + summary
python main.py --verbose       # DEBUG-level logging
```

Everything is persisted into `db/govspend_free.db` (SQLite) - the single source
of truth. Nothing is dumped to CSV; the DB accumulates across runs, which is what
makes the next three commands possible (they read all history, not just the
latest run):

```bash
python main.py --search "attendance software"     # full-text search everything ever scraped
python main.py --opportunities                    # ranked feed, scored by recency + keyword strength
python main.py --coverage                         # national 50-state coverage scorecard + reports/coverage_<ts>.csv
python main.py --expirations 90                   # contracts expiring within 90 days (default 180)
python main.py --backfill-dates                   # one-time: derive each doc's own date from its text (for age-filtering)
```

### Where the data lives

One file: `db/govspend_free.db`. Browse it in the desktop UI (Opportunities /
Search tabs), query it from the terminal with the commands above, or open it in
any SQLite viewer / the `sqlite3` CLI:

```bash
sqlite3 db/govspend_free.db "SELECT doc_type, source, COUNT(*) FROM documents GROUP BY 1,2"
```

Each row carries a `source` provenance tag (`bids` / `board_minutes` /
`transparency` / `socrata` / `usaspending`) plus `scraped_at` / `updated_at`, and
the `documents` table is indexed on the columns you filter by. (`reports/` still
holds account briefs and Ops-play outputs - just no per-run CSV dumps.)

## Desktop UI (dashboard + scraping)

Prefer clicking to typing? There's a small native desktop app - a searchable,
ranked dashboard over everything you've scraped, plus a button to kick off a
new scrape with live progress.

```bash
pip install -e ".[ui]"     # installs pywebview
govspend-free-ui           # (or: python -m govspend_free.desktop)
```

It opens a window with five tabs:

- **Opportunities** - the ranked feed (same scoring as `--opportunities`), click a title to open it. Each row has a **Brief** button that generates an account brief for that document (see "Account briefs" below).
- **Search** - full-text search over everything ever scraped (same index as `--search`).
- **Expirations** - contracts expiring within N days, with the soon-to-expire ones flagged.
- **Ops** - the **Full Motion** account-prioritization play (see below).
- **Scrape** - pick a state (or all), toggle which passes to run, hit **Run scrape**, and watch the log stream live. When it finishes, the Opportunities tab refreshes automatically.

Apollo contacts are **off by default** in the UI (they cost credits) - tick
"include Apollo contacts" to opt in, and only if `config/apollo.yaml` is set up.

## Ops: the "Full Motion" play (CRM-grounded prospecting)

The **Ops** tab has one flagship button, **Run Full Motion play**. In a single
run it scores every account that has a signal in your local DB 0-100, checks
each one's CRM status (In Pipeline / Cold / Whitespace), pulls the
decision-maker, and drafts a spend-grounded opener - the whole prospecting
motion, ranked, top-down. Also runnable from the terminal:

```
python main.py --play
```

Both halves are deterministic and inspectable (no LLM, same philosophy as
`opportunities.py`): signals are extracted offline from `db/govspend_free.db`,
and CRM status + contacts come from HubSpot's REST API, **read-only**, via
`hubspot_client.py` (which has no create/update/delete methods). The 0-100 score
is transparent arithmetic; openers are templated from each account's real signal
(never a fabricated number).

**One-time setup - a read-only HubSpot Private App token:**

1. HubSpot > Settings > Integrations > **Private Apps** > *Create a private app*.
2. Grant **only** these read scopes: `crm.objects.companies.read`,
   `crm.objects.contacts.read`, `crm.objects.deals.read`.
3. Copy the access token (`pat-...`) into `config/hubspot.yaml`
   (`cp config/hubspot.yaml.example config/hubspot.yaml`), or set `HUBSPOT_TOKEN`.

Until a valid token is present the button stays disabled and says so. Copy
`config/ops.yaml.example` to `config/ops.yaml` to change the client/competitor
context. Reports are saved to `reports/ops/full_motion_<timestamp>.md`.

(Why a Private App token and not the HubSpot MCP / Claude connector? HubSpot's
remote MCP has no dynamic client registration, so the local `claude` CLI can't
self-authenticate to it - a static, read-scoped Private App token is the
reliable path.)

On **Windows** this uses the built-in Edge WebView2 runtime, which ships with
Windows 11 - no extra install. (On macOS/Linux, pywebview uses the system
WebKit/GTK webview.) It's all local: the window talks straight to Python and
the SQLite DB, and the only network access is the scrapes you trigger yourself.

## Account briefs (`--brief`)

Turn a scraped board-minutes document into a structured sales brief - Why now,
top pain points (with evidence), buying committee, competitor/incumbent stack,
deal window, objection map - synthesized against your own GTM playbook.

```bash
cp config/gtm_profile.md.example config/gtm_profile.md   # fill in your positioning
python main.py --brief 17                                # by scraped document id
python main.py --brief "University of Arkansas System"   # by institution name
```

Or click the **Brief** button on any Opportunities row in the desktop UI.

This runs through the local **`claude` CLI** (Claude Code), so it uses your
existing Claude login - no separate Anthropic API key, and it isn't billed at
metered API rates. One-time setup: run `claude login` in a normal terminal so
the CLI has valid credentials. Briefs are written to `reports/briefs/`
(gitignored). `config/gtm_profile.md` is your own strategy and is gitignored
too - only the `.example` template is committed.

## Contacts (Apollo.io) - GovSpend's Contacts module, for real

1. Get an API key: https://developer.apollo.io/#/keys
2. `cp config/apollo.yaml.example config/apollo.yaml`
3. Set `enabled: true`, paste your key, tune `target_titles` to the roles
   who'd actually evaluate/buy what you sell (procurement directors, CFOs,
   VPs of student affairs, whatever's relevant).
4. Next `python main.py` run will search for those people at every
   institution that has a `domain:` field in `sources.yaml` (all 10 pilot
   institutions have one set already).

**Cost:** People Search itself is free (0 Apollo credits) - you get
names, titles, and LinkedIn URLs at no cost. Email addresses require the
separate Enrichment endpoint, which costs Apollo credits (1 if it finds an
email, 0 if not). This is off by default (`reveal_emails: false` in
apollo.yaml) - turn it on deliberately, and `max_enrich_per_run` caps how
many credit-spending calls happen in one run so you don't burn your
balance by accident. **Phone numbers are not implemented** - Apollo's
phone reveal requires standing up a public webhook endpoint to receive an
async callback, which is out of scope for a personal script (the hook
point is documented in `apollo_client.py` if you want to add it yourself).

Re-runs don't re-spend credits on people you've already enriched - contacts
are deduped by Apollo's own person ID in the `contacts` table.

## AI Search + Record-Level Chat (optional, costs money)

This is the one part of the tool that isn't free - it uses your own
Anthropic API key at standard API rates.

```bash
pip install anthropic
cp config/llm.yaml.example config/llm.yaml   # paste your key, or just export ANTHROPIC_API_KEY
python main.py --ask "which Texas institutions are evaluating ERP systems?"
python main.py --search "SEAtS"    # find a document id first...
python main.py --chat 42           # ...then chat about just that one record
```

`--ask` does retrieval-augmented generation: it full-text-searches your
local `documents_fts` index for the top 10 matches, hands only that
context to Claude, and asks it to answer using just those sources (with
citations) - it does not call out to the open web or use outside
knowledge about these institutions, unlike GovSpend's own AI Search.

## SAM.gov federal RFPs (optional, free key)

The one **nationwide** RFP source: SAM.gov is the federal government's solicitation
board, so a single keyed feed gives federal contract-opportunity coverage in all
50 states at once (`sam_gov.py`, stored as `doc_type='federal_rfp'`). It's the
federal *contract* complement to USAspending's federal *grants*.

```bash
cp config/sam.yaml.example config/sam.yaml   # then set enabled: true + paste your key
python main.py                               # a nationwide SAM.gov pass runs once per full scrape
python main.py --skip-sam                    # ...or skip it
```

Get a **free** key: sign in at https://sam.gov -> Account Details -> "Request API
Key" (or set `SAM_API_KEY` in the environment). It's off until a key is present.
Notices are filtered to education + a SEAtS bid category (so it doesn't flood the
DB), attributed to the place-of-performance state, and surface in `--opportunities`
/ `--search` like every other source. `lookback_days` / `max_pages` in `sam.yaml`
bound how much of each busy day's postings one run pulls (it warns if a window had
more than it scanned). The key is sent as a query parameter, so the tool logs only
HTTP status codes - never the key. Federal RFPs are nationwide and do **not** count
toward the state-education numbers in `--coverage`.

## Alerts (optional, free)

```bash
cp config/alerts.yaml.example config/alerts.yaml   # fill in SMTP creds, set enabled: true
python main.py                                     # digest auto-sends after the run
python main.py --send-alerts-only                   # re-send the most recent report without re-scraping
```

Works with any SMTP provider (Gmail example with an App Password is in
the `.example` file).

## The honest limitation: this is NOT actually nationwide (yet)

10 states were piloted; every URL in `config/sources.yaml` was fetched and
verified directly. What that pilot found:

**Board meeting minutes** are the most reliable category - 9 of 10 states
have a plain HTML/PDF listing this scraper handles natively (all except
Illinois, which needs manual date-filling - see the `{YEAR}` placeholder
in its config entry).

**University bid boards are the weak point almost everywhere outside
Arkansas.** Texas, Florida, California, and Georgia all route real bid
postings through a JavaScript single-page app (Jaggaer/SciQuest or GEP
SMART) - a basic HTTP fetch gets an empty shell, no HTML table to parse.
These are marked `js_rendered` and skipped on purpose, showing up in your
report as `SKIPPED:bids / needs_browser`.

**One class of "JS" bid board is reachable without a browser: Bonfire**
(`*.bonfirehub.com`). The portal page looks JS-rendered, but it fetches its
open opportunities from a public JSON endpoint - so `bonfire.py` (source
`type: bonfire`, with a `slug`) pulls them with a plain request, filtered to
the same SEAtS bid categories as every other bid source. **24 verified-live
higher-ed Bonfire portals across 12 states are wired** - Texas is the big one
(the UT System plus El Paso / Tarrant County / Texas Southmost), alongside FL,
NC, AZ, CA, IL, MO, NJ, NM, VA, WV, and AK. Note most MO/OK/KS/NE flagships are
*not* on Bonfire (they use other platforms), so core-territory yield is thin;
the volume is in TX/FL/NC. Bonfire rate-limits by IP across *all* tenants, so
`bonfire.py` backs off on HTTP 429 with a process-wide cooldown (skipped portals
retry next run). Widen coverage with rfp-monitor's Common-Crawl discovery pass,
which enumerates bonfirehub tenants nationwide.

**Transparency portals split roughly in half.** California (FI$Cal),
Georgia (open.ga.gov), and North Carolina's bulk-download CSVs are
scrapable as-is (and are what the Contracts module also draws from).
Texas, Ohio, Tennessee, Illinois, and Pennsylvania are AJAX/Tableau-driven
and get skipped the same way. Florida's portal works but explicitly does
**not** cover the 12 public universities (separate system, flbog.edu).

**Contracts expiration detection depends entirely on the transparency
portal actually publishing a CSV with start/end date columns** - most
states that get skipped for Spending also get skipped for Contracts, for
the same JS-rendered reason. Where it does work (CA/GA/NC), the column
detection is a best-effort header-name match (`contracts_scraper.py:
_match_columns`), so verify a sample against the source before trusting
an expiration date for anything important.

Run the tool once and read the `SKIPPED:` rows in your report - that's
the most accurate live picture of coverage, since sites change.

## Federal grants (USAspending) - nationwide, keyless, no wall

The one spending feed that *does* cover the whole country - including the
core sales territory (Missouri, Oklahoma, Kansas, Nebraska) where no state
checkbook is scrapable. `usaspending_scraper.py` queries the U.S. Treasury's
[USAspending API](https://api.usaspending.gov) (`type: usaspending` sources in
`config/sources.yaml`) - free, **no API key**, no Tableau/WAF wall.

It pulls federal **Dept. of Education student-success grants** to colleges in a
state - Title III/V institutional aid, TRIO (Student Support Services, Talent
Search, Upward Bound), GEAR UP, FIPSE (CFDA 84.031/042/044/047/066/116/334).
Each award is stored as a `federal_award` document (recipient = the institution,
tagged `Student Success & Retention`), so it flows straight into the
Opportunities feed and Search - ranked lead-gen where SEAtS actually sells.

```
python main.py --state missouri     # ~120 MO higher-ed student-success grants
```

**What it is and isn't.** This is a *budget / mandate* signal - money earmarked
for exactly SEAtS's ICP (retention, access, student success). It is **federal,
not institutional procurement**, so it will *not* surface a university buying
EAB/Ellucian (those are state/tuition-funded) - it complements the
competitor-footprint intel from state checkbooks, it doesn't replace it. Only
higher-ed recipients are kept (K-12 districts that also get TRIO/GEAR UP money
are filtered out); pass `higher_ed_only: false` in a source to include everyone.

## Spending normalization (the `payments` table)

State checkbooks are messy: the same case-insensitive match that finds
`SEATS SOFTWARE LIMITED` (really SEAtS) also tags `VIVID SEATS *HARTFORD` (a
ticket reseller). `normalize.py` resolves that. It's **closed-world** on purpose
- instead of trying to normalize every vendor in America (the OpenTheBooks /
GovSpend build), it matches each raw vendor only against a known set: the
`keywords.yaml` competitors, the client's aliases, and a higher-ed institution
pattern. Deterministic, inspectable, tuned in `config/normalize.yaml`
(canonical names, `ambiguous_terms` like `SEATS` that must match exactly, and
per-state agency/category crosswalks).

```
python main.py --normalize-payments   # resolve stored checkbook rows -> payments table
```

Run against the CT/DE/NY data already scraped, it produced a clean vendor
footprint - SEAtS 12, Jenzabar 50, Ellucian 73, Watermark 10, Anthology 2 -
with the `VIVID SEATS` rows correctly dropped as `unknown`. Each `payments` row
keeps the raw values (`vendor_raw`, `agency_raw`, `category_code_raw`) next to
the canonical ones, so nothing is lost. This is the foundation for pulling more
states via the SODA API (Socrata) and bulk CSVs.

## Closing the JS-rendered gap (`--browser`, optional)

`js_rendered` sources need a real browser, not just `requests`. That's now
built in, opt-in, via headless Chromium (Playwright):

```bash
pip install -e ".[browser]" && playwright install chromium
python main.py --state georgia --browser        # renders js_rendered sources
```

Or tick **"render JS sources"** on the Scrape tab. When `--browser` is on,
`utils.fetch_page_or_skip` renders `js_rendered` sources through Chromium
(`utils.fetch_with_browser`) and feeds the rendered HTML into the same
BeautifulSoup parsing as everything else; with it off, those sources skip as
before. It's off by default because the browser is slow and heavyweight.

**Honest scope.** This is a foundation, not a universal unlock:

- **Works** where the data lands in the rendered DOM after load (e.g. Jaggaer
  sourcing pages render their solicitation tables).
- **Doesn't help by itself** where the data needs *interaction* (clicking a
  status filter or running a search - many Jaggaer/GEP result lists), or lives
  in a **canvas/viz** (Tableau checkbooks) or behind a **data catalog** you
  click into (CA FI$Cal). Those need per-platform selector/wait tuning or the
  site's own export API.
- `form_post` sources still skip - they need a form submission, not a load.

So `--browser` gets you past the empty-shell wall; squeezing data out of a
specific platform is still per-site work (Jaggaer, GEP SMART, Oracle Fusion,
Tableau, Ariba each differ).

## Extending to more states

Copy a state block in `config/sources.yaml`, add a `domain:` field for
Apollo, and point the URLs at the new state's equivalents. Practical
search order, based on what worked in the pilot:

1. `"<university system> board of trustees meetings minutes"` - most
   likely to be a plain scrapable page.
2. `"<university system> procurement bid opportunities"` - check if it's
   native (good) or redirects to `bids.sciquest.com`, `smart.gep.com`, an
   Oracle Fusion login, or SAP Ariba (`js_rendered`, mark it and move on
   unless you want the Playwright layer).
3. `"<state> financial transparency" OR "<state> checkbook" OR "<state>
   open budget"` - look specifically for a "bulk download"/"open data"/CSV
   section, not just the interactive search box (the search box is almost
   always AJAX; bulk-download links, when they exist, usually aren't).

Always fetch and eyeball a URL before adding it - site structures change.

## Editing what it looks for

- `config/keywords.yaml` -> `categories` - the 5 EdTech-ish bid categories
  (retention, scheduling, attendance/compliance, chatbot/messaging,
  SIS/ERP). Add/remove freely.
- `config/keywords.yaml` -> `watchlist` - free-text vendor/product names
  searched in board minutes and transparency data (seeded with `SEAtS` /
  `SEAtS Software` / `SEAtS ONE`). Add your own competitors/target vendors.
- `config/apollo.yaml` -> `target_titles` / `target_seniorities` - who
  Contacts looks for.

## Troubleshooting

**`sqlite3.OperationalError: disk I/O error` / `RuntimeError` on startup**
mentioning the database - the project folder is probably on a network
drive, FUSE mount, or unusual cloud-sync filesystem that doesn't support
SQLite's normal file locking. Copy the whole project folder to a plain
local folder and run it from there.

**A source that worked before shows up as `SKIPPED`** - sites change
their markup over time. Re-check the URL in a browser; if it's genuinely
changed, update `config/sources.yaml`'s `type` field or notes accordingly.

## Project layout

```
govspend_free/
├── main.py                        # CLI entry point / orchestrator
├── pyproject.toml                 # packaging + pytest config
├── requirements.txt
├── requirements-dev.txt           # pytest (test-only)
├── config/
│   ├── sources.yaml                # states -> institutions -> source URLs + domains
│   ├── keywords.yaml               # bid categories + watchlist terms
│   ├── apollo.yaml.example         # Contacts module (copy -> apollo.yaml)
│   ├── llm.yaml.example            # AI Search/Chat (copy -> llm.yaml)
│   └── alerts.yaml.example         # Email digest (copy -> alerts.yaml)
├── govspend_free/
│   ├── utils.py                    # HTTP, dedup state, keyword matching, PDF text
│   ├── pipeline.py                 # scrape orchestration (shared by CLI + UI)
│   ├── desktop.py                  # optional pywebview desktop dashboard
│   ├── db.py                       # SQLite + FTS5 persistent store
│   ├── bid_scraper.py
│   ├── board_minutes_scraper.py
│   ├── transparency_scraper.py
│   ├── contracts_scraper.py        # expiration detection
│   ├── apollo_client.py            # Apollo.io REST API wrapper
│   ├── contacts.py                 # Contacts pass orchestration
│   ├── opportunities.py            # rule-based ranking
│   ├── llm.py                      # optional Anthropic AI Search/Chat
│   └── alerts.py                   # optional SMTP digest
├── tests/
│   ├── conftest.py                 # pytest fixtures (fresh in-memory DB per test)
│   ├── test_offline.py             # bid/minutes scraping, no network
│   ├── test_offline_extended.py    # db/contracts/opportunities, no network
│   └── fixtures/
├── db/govspend_free.db             # the single source of truth (created on first run)
├── state/seen.json                 # created on first real run (scrape dedup)
├── cache/pdfs/                     # downloaded minutes/transparency PDFs
└── reports/                        # account briefs + Ops-play outputs (no per-run CSVs)
```

## What this deliberately does NOT do

- No Agencies module (a unified per-agency profile page) - the data is
  implicitly there (`state`/`institution` columns everywhere), just not
  rolled up into its own report. Would be a straightforward addition if
  you want it: a `GROUP BY institution` query over `documents` +
  `contracts` + `contacts`.
- No automatic CRM push - query `db/govspend_free.db` (or the Ops play's
  read-only HubSpot lookup) for what you need; add your CRM's API client if you
  want automatic sync.
- No unified search box across your browser - `--search` is a
  terminal command, not a web UI, matching the "standalone script I own"
  choice over a hosted dashboard.
- Doesn't handle JS-rendered sources out of the box (see above) - it
  tells you honestly which sources it couldn't reach rather than
  pretending coverage it doesn't have.
