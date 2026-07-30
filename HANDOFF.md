# Project handoff — govspend_free (a.k.a. FreeGSPEND)

A free, self-hosted sales-intelligence tool for SEAtS Software. It scrapes
public US higher-ed records (board minutes, bids/RFPs, spending) and federal
grants, normalizes competitor/client spend, ranks leads, drafts account briefs,
and runs a HubSpot-grounded account-prioritization play.

## Where things live
- **Project folder:** `C:\Users\KibCochran\OneDrive - SEAtS Software\Desktop\Freegspend`
- **GitHub:** https://github.com/Kibcochran93/FreeGSPEND (branch `main`, private).
  Current `main`: the full feature set below. Working tree clean.
- Windows + PowerShell. Python 3.14. On OneDrive (SQLite works; if a rare
  `disk I/O error` appears, pause OneDrive during a scrape).

## Setup (already done on this machine)
```powershell
cd "C:\Users\KibCochran\OneDrive - SEAtS Software\Desktop\Freegspend"
pip install -e ".[dev,ui,browser]"     # core + pytest + pywebview + playwright
playwright install chromium            # for --browser (re-run after Playwright updates)
# Account briefs: `claude login` once (uses your Claude sub; model auto, don't force sonnet).
# Ops play: create a READ-ONLY HubSpot Private App, put its token in
#   config/hubspot.yaml  (scopes: crm.objects.{companies,contacts,deals}.read)
```

## Run it
```powershell
python -m govspend_free.desktop   # desktop UI: Home (RAG dashboard) / Opportunities / Search / Expirations / Ops / Scrape
python main.py                    # scrape everything (writes to the DB; NO CSVs)
python main.py --state missouri   # one state (incl. USAspending federal grants for MO/OK/KS/NE/AR)
python main.py --opportunities    # ranked feed        | --search "retention"
python main.py --doctor           # what's configured/working (deps, tokens, DB counts)
python main.py --ingest-spend     # live Socrata payments -> normalized payments table (MA/CT/DE/MD)
python main.py --normalize-payments   # resolve already-stored checkbook rows -> payments
python main.py --reset-payments   # empty the payments cache (regenerable)
python main.py --play             # Ops "Full Motion" account-prioritization (read-only HubSpot)
python main.py --brief "University of Arkansas System"   # account brief via claude CLI
python main.py --retag            # re-tag stored docs after editing keywords.yaml
pytest                            # 85 tests, offline
```
Scrape filters: `--from/--to`, `--only-keyword`, `--only-competitor`, `--skip-federal`, `--browser`.

## What works
- **Board minutes** (AR/TN/TX/CA) — reliable lead source. **Bids/RFPs** on native-HTML boards.
- **USAspending federal grants** — keyless nationwide API; pulls Dept-of-Ed student-success
  grants (Title III/V, TRIO, GEAR UP) to colleges. Covers core territory (MO/OK/KS/NE) where
  board minutes are thin. `doc_type='federal_award'`; feeds Opportunities + the Ops play.
- **Spend normalization** — closed-world `payments` table + `normalize.py`: resolves a raw
  vendor against a KNOWN set (competitors from keywords.yaml + client + institutions), so
  `SEATS SOFTWARE LIMITED` -> SEAtS but `VIVID SEATS` is dropped. `--ingest-spend` pulls live
  Socrata for MA/CT/DE/MD (verified; ~476 payments). `--normalize-payments` backfills from docs.
  Full coverage + endpoint-cracking backlog in `docs/SPENDING_SOURCES.md`.
- **Ops "Full Motion" play** — `--play` + UI Ops tab. Ranked account table (0-100 score, CRM
  status, decision-maker, spend-grounded opener). Reads HubSpot READ-ONLY via a Private App
  token in `config/hubspot.yaml`. Copy / Save-CSV on the result.
- **Home dashboard** — default UI tab: RAG (red/amber/green) status tiles + top-opportunities
  and competitor-footprint lists.
- **Setup doctor** (`--doctor` / UI "Check setup"). **Keyword/watchlist** tuning, **--retag**,
  date/keyword/competitor filters (UI + CLI), CI (GitHub Actions runs pytest).
- **DB is the single source of truth** — `db/govspend_free.db` (SQLite). Indexed; every row
  has a `source` provenance tag + `updated_at`; auto-migrates on open. **No CSV reports**
  (removed; query the DB / UI / `sqlite3`). `reports/` holds only briefs + Ops-play outputs.

## Key decisions / limits (don't re-litigate)
- **Spending is NO LONGER walled/done.** SODA (Socrata) ingest works for the 4 higher-ed-in-scope
  states (MA/CT/DE/MD); USAspending federal is nationwide. Remaining state checkbooks need
  per-tech endpoint-cracking (CKAN/Tableau/Qlik/PowerBI/XHR) — mapped in docs/SPENDING_SOURCES.md.
- **Normalization is CLOSED-WORLD on purpose** (match known competitors/client/institutions),
  not open-world entity resolution (that's the OpenTheBooks/GovSpend build).
- **Ops play uses a read-only HubSpot Private App token, NOT the MCP/CLI.** HubSpot's remote MCP
  has no dynamic client registration and the `claude` CLI can't self-auth to it; a static
  read-scoped Private App token is the reliable path.
- **Secrets/local configs gitignored:** `config/{apollo,llm,alerts,hubspot,ops}.yaml` + `gtm_profile.md`
  (`.example` templates committed). `config/{keywords,sources}.yaml` are committed tuning data;
  `config/normalize.yaml` is optional (code has sensible defaults).
- **Socrata gotcha:** its JSON omits null fields, so a `$limit=1` probe on a payroll row makes the
  vendor column look absent — verify columns with a `$where upper(col) like '%TERM%'` filter.
- `--browser` (Playwright) is foundation-only; many JS sources need per-site interaction.

## Best next step (recommended)
**Phase 3 endpoint-cracking** for more spending states (see docs/SPENDING_SOURCES.md): the near-term
wins are **Oklahoma** (CKAN `datastore_search`, clean JSON, core territory) and **Arkansas**
(server-rendered PHP tables at ark.org — reclassify to a real table parser). Also easy: make the
Home dashboard tiles clickable (jump to the relevant tab), and wire the payments footprint into
the Ops play as a per-account competitor signal.

## Conventions
- Branch per feature -> `pytest` -> commit (Co-Authored-By trailer) -> `merge --ff-only` into main
  -> `push origin main` -> delete the branch. Credentials stored (push is non-interactive).
- Run `pytest` before committing; keep it offline (mock network/subprocess/CLI).
