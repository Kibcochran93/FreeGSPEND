# State spending sources — coverage & roadmap

How each state's checkbook is reachable, split by the closed-world test: *does a
payment to a tracked ed-tech vendor (Ellucian / EAB / Anthology / …) actually
appear on the state ledger?* Only then is it a displacement signal for SEAtS.

## Socrata (SODA) — wired into `--ingest-spend`

Higher-ed **in scope** (colleges appear as paying departments → competitor
payments show up). Column-mapped in `config/sources.yaml`, verified live:

| State | Domain | Dataset | vendor / amount / date / agency | Verified |
|-------|--------|---------|----------------------------------|----------|
| MA | cthru.data.socrata.com | `pegc-naaa` | vendor / amount / date / department | Ellucian via Berkshire CC; ~Feb 2026 |
| CT | data.ct.gov | `ajdm-rvz7` | vendor / amount / payment_date / department | Ellucian via CSU-Western; freshest (~2 wks) |
| DE | data.delaware.gov | `5s6n-7hpx` | vendor / amount / check_date / department | ELLUCIAN SUPPORT INC, EAB GLOBAL INC |
| MD | opendata.maryland.gov | `7syw-q4cy` | vendor_name / amount / date / agency_name | Ellucian + Anthology via Baltimore City CC |

Higher-ed **off-ledger** — Oracle appears but zero Ellucian/Anthology; public
universities buy off the state ledger, so market-intel only, not SEAtS buyers.
In `sources.yaml` without column mappings (so `--ingest-spend` skips them):
NJ `apet-rp2i`, OR `y9g9-xsxs`, VT `786x-sbp3`, MO `fzgp-ixr3`.

### Dataset corrections applied
- **CT** `jz5u-r6jf` → `ajdm-rvz7` (portal now labels the old one *Deprecated*).
- **DE** `cbcj-ys58` → `5s6n-7hpx` (`cbcj-ys58` is the *Archived* copy).
- **NY** `ehig-g5x3` is public-authorities procurement only; the real statewide
  checkbook (OSC Open Book) is a ColdFusion form app with no API. NY stays weak.

> Gotcha: Socrata's JSON **omits null fields**, so a `$limit=1` probe that lands
> on a payroll row (null vendor) makes the vendor column look absent. Always
> confirm a column with a `$where upper(col) like '%TERM%'` filter, not a sample.

### Not on Socrata / no vendor ledger (don't spend time)
TX, IL, PA, WA, CO, UT, HI, MI, ME, RI, SC, WV, MT, AK, NV, KS.

## Endpoint-cracking backlog (non-Socrata) — future buckets

Each needs its own fetcher; not built yet. Priority note: **AR** and **OK** are
the near-term wins (clean-ish APIs), the rest need a browser or a protocol.

| Source | Tech | Bucket | Notes |
|--------|------|--------|-------|
| Arkansas (state exec) | server-rendered PHP `www.ark.org/dfa/transparency/expenditures.php` (Tyler/NIC) | `html_table` | GET-scrapable HTML tables, daily. Needs a payments-table parser (current html path only finds download links). UA System is separate (below). |
| Oklahoma | `data.ok.gov` CKAN `datastore_search` JSON | `ckan_json` | Clean JSON API. `robots.txt` disallows `/api/` (a polite bot is blocked; a normal HTTP client works). Core territory. |
| Iowa | Looker "Data Hub" | `needs_browser` | Full checkbook (dataset 1021, 25.7M rows) **and** a Board of Regents vendor-payments-by-institution set (dataset 990, higher-ed in scope). No GET JSON; Looker query API or browser. |
| Illinois (Comptroller) | AJAX SPA | `xhr_json` | XHR endpoint is replayable but must be sniffed in a browser once. |
| Tennessee | Tableau Server `data.tn.gov/t/Public` | `tableau_vizql` | Anonymous `.csv` export 404s; POST `bootstrapSession`. |
| Texas | QlikView `bivisual.cpa.texas.gov` (WebSocket engine) | `qlik_engine` | No CSV on the landing page. Hardest of the set. |
| Arkansas UA System | Power BI publish-to-web | `powerbi_querydata` | One `?r=` token per fiscal year. Higher-ed AR portal. |
| PennWATCH | ASP.NET WebForms (`__VIEWSTATE` postback) | `needs_browser` | No CSV section. |
| Ohio Checkbook | likely custom SPA | `needs_browser` | Unverified (robots TLS failed through the sandbox proxy); needs a browser network trace. |
