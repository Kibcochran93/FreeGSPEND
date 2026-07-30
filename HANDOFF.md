# Project handoff — govspend_free (a.k.a. FreeGSPEND)

A free, self-hosted sales-intelligence tool for SEAtS Software. It scrapes
public US higher-ed records (board minutes, bids/RFPs, spending), flags what
matters to SEAtS + competitors, ranks leads, and drafts account briefs.

## Where things live
- **Project folder:** `C:\Users\KibCochran\OneDrive - SEAtS Software\Desktop\Freegspend`
- **GitHub:** https://github.com/Kibcochran93/FreeGSPEND (branch `main`, private)
- Windows + PowerShell. Python 3.14. On OneDrive (SQLite works; if a rare
  `disk I/O error` appears, pause OneDrive during a scrape).

## Setup (already done on this machine)
```powershell
cd "C:\Users\KibCochran\OneDrive - SEAtS Software\Desktop\Freegspend"
pip install -e ".[dev,ui,browser]"     # core + pytest + pywebview + playwright
playwright install chromium            # for --browser
# For account briefs: run `claude login` once in a normal terminal (uses your
# Claude subscription; no API key). Model is auto (don't force --model sonnet;
# it 404s on this account).
```

## Run it
```powershell
python -m govspend_free.desktop        # desktop UI (Opportunities/Search/Expirations/Scrape)
python main.py --state arkansas        # scrape one state
python main.py --opportunities         # ranked feed
python main.py --search "retention"
python main.py --brief "University of Arkansas System"   # account brief via claude CLI
python main.py --retag                 # re-tag stored docs after editing keywords.yaml
pytest                                 # 25 tests, offline
```
Scrape filters: `--from/--to YYYY-MM-DD`, `--only-keyword`, `--only-competitor`, `--browser`.

## What works
- **Board minutes** — the reliable engine (~250 docs, AR + TN). Best lead source.
- **Bids/RFPs** — works on native-HTML boards (most are JS SPAs → need `--browser`).
- **Account briefs** — `--brief` + UI "Brief" button, via local `claude` CLI, grounded on `config/gtm_profile.md`.
- **Keyword/watchlist** tuned from the SEAtS market-intel docs (board vocabulary, competitors). Watchlist matching is case-sensitive + word-boundary.
- **Socrata spending** — `type: socrata` sources (CT/DE/NY wired). Surfaces competitor payments; found `SEATS SOFTWARE LIMITED` in the CT checkbook.
- **xlsx parsing**, **date/keyword/competitor filters** (UI + CLI), **--retag**, split CSV reports (`reports/<type>/`), CI (GitHub Actions runs pytest).

## Key decisions / limits (don't re-litigate)
- **Spending data is walled for the core territory.** Tableau Public (TX/CA) is WAF-blocked; Socrata only covers a few non-core states. Spending = competitive-footprint intel, considered DONE.
- **`--browser`** (Playwright) renders JS pages but many need per-site interaction (Jaggaer search, Tableau canvas). Foundation only; opt-in, off by default.
- Secrets & the real GTM profile are gitignored (`config/*.yaml`, `config/gtm_profile.md`); `.example` templates are committed. Briefs go to `reports/briefs/` (gitignored).
- Git uses a separate gitdir workaround only if the folder path is very deep — not needed at this Desktop path.

## Best next step (recommended)
**Expand board-minutes coverage to SEAtS's core territory (Missouri, Oklahoma,
Kansas, Nebraska).** Board minutes work everywhere and generate leads where SEAtS
actually sells — unlike spending. Research each state's university-system
board-of-trustees minutes pages and add them to `config/sources.yaml`
(`type: html_list`/`html_table`), then `--retag`.

## Conventions
- Commit messages end with a Co-Authored-By trailer. Run `pytest` before committing.
- Credentials are stored (git push works non-interactively).
