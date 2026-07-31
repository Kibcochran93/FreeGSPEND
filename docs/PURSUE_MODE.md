# Pursue Mode — design & roadmap

> **Status: PLANNED (not built).** Priority: this sits **behind** the endpoint-cracking
> coverage work in [`SPENDING_SOURCES.md`](SPENDING_SOURCES.md). Oklahoma CKAN and
> Arkansas ark.org add *coverage* (the tool's stated nationwide goal); Pursue Mode
> deepens the value of opportunities already surfaced. **Do the coverage wins first,
> then Pursue Mode as the next feature.**

Pursue Mode turns an Opportunities row (a bid / RFP / federal RFP with a decent score)
from "read it and guess whether to chase it" into a one-screen, evidence-backed
**GO / REVIEW / NO-GO** decision — deterministic disqualifier gates in Python, judgment
and prose from the `claude` CLI. Same closed-world, offline-testable, token-frugal
philosophy as the Ops "Full Motion" play.

## Primary use case

- **Actor:** a SEAtS SDR running the tool weekly.
- **Trigger:** the Opportunities feed surfaces a `bid` / `rfp` / `federal_rfp` with a
  decent opportunity score. Today the rep reads it and guesses. Pursue Mode replaces the guess.

**Flow**

1. `python main.py --pursue --doc <id>` (or a **Pursue** button on an Opportunities row).
2. **Deterministic gates run first, in Python.** If the doc trips a hard disqualifier from
   `gtm_profile` — K-12 in North America, under 500 students, admissions-/recruitment-CRM-only,
   signed EAB/Civitas multi-year inside 18 months — the tool returns **NO-GO immediately** with
   the reason and spends **zero** LLM tokens.
3. If it passes the gates, the tool assembles context from existing internals: the RFP text,
   matched `keywords.yaml` categories, watchlist hits in the doc, the institution's competitor
   footprint from the `payments` table, HubSpot relationship status (if configured), and Apollo
   contacts at the institution's domain (if configured). **Missing sources degrade gracefully.**
4. The `claude` CLI fills the fit rubric with an **evidence quote per dimension**, then writes the
   positioning section using ghost-theme and evaluator-perspective framing.
5. Output: a **Pursue Brief** markdown file in `reports/pursue/`, plus a decision row in the DB.

**Outcome:** a one-screen decision (GO/REVIEW/NO-GO + win-probability + confidence), a
requirement-to-capability map, the incumbent picture with a displacement angle, named contacts
and CRM status, and a spend-grounded opener in the spirit of the Ops play.

## Secondary use cases

- **Batch triage (Phase 2):** `--pursue-opportunities --top 10 --min-score N` runs gates + rubric
  across the top of the feed and prints a ranked GO/REVIEW/NO-GO table, so a rep triages a week's
  solicitations in one pass.
- **Displacement targeting:** because the `payments` table already resolves competitors
  closed-world, an RFP from an institution that pays Ellucian or EAB gets a pre-populated
  displacement angle — connecting the spend intel to a concrete sales action.

## The pursue-fit rubric (adapted BQB)

Eight dimensions, each scored 0–5 with a required one-line **evidence quote**. Deterministic gates
are separate and run first.

| Dimension | What it measures | Primary source |
| --- | --- | --- |
| Requirement fit | RFP scope maps to SEAtS modules (Attend, Engage, CRM, Schedule, Space, Analytics) | RFP text vs `gtm_profile` |
| Category strength | Strength of the `keywords.yaml` category matches already tagged | DB tags |
| Competitive signal | Incumbent present and displaceable, or greenfield | watchlist + `payments` table |
| Timing | Deadline proximity, fiscal window, document freshness | doc date, RFP deadline |
| Buyer access | CRM relationship and known decision-makers | HubSpot + Apollo |
| Platform fit | Microsoft 365 / Azure standardization signal | RFP text vs `gtm_profile` |
| Evidence confidence | How much real text backed the assessment | doc length / quality |
| Strategic coherence | Do the above point the same direction | derived |

Hard-gate failures force **NO-GO**; otherwise a weighted sum maps to GO / REVIEW / NO-GO with a
win-probability band. Weights live in an optional `config/pursue.yaml` with sensible defaults in
code (matching the `normalize.yaml` convention).

## Output artifact — the Pursue Brief

Fixed sections:

1. **Header** — institution, RFP title, source, deadline, doc date, existing opportunity score.
2. **Decision line** — GO/REVIEW/NO-GO, win-probability band, confidence.
3. **Rubric table** — the eight dimensions with evidence quotes.
4. **Requirement → capability map** — RFP ask, SEAtS module, proof point.
5. **Incumbent & competitive picture** — with the displacement angle.
6. **Positioning** — two or three ghost themes + evaluator-perspective win themes.
7. **Buyer access** — HubSpot status, named Apollo contacts, suggested entry role.
8. **Risk & disqualifier check.**
9. **Suggested next action** — with a spend-grounded opener.
10. **Provenance footer** — source doc IDs.

## Architecture & design principles

- **Deterministic gates in Python, judgment in the LLM.** Every hard disqualifier is code —
  testable offline and free. Only the qualitative dimensions and the prose go to `claude`. Keeps
  the closed-world philosophy, keeps `pytest` offline, controls token spend.
- **Small footprint:** one module `pursue.py`, one prompt template, one rubric config, tests. Reuses
  the existing `claude` CLI wrapper, the DB layer, the config loader, and the `payments` / HubSpot
  readers. **No new dependencies for the MVP.**
- **Storage:** a new `pursue_briefs` table (`doc_id` FK, decision, dimension scores, win-probability,
  confidence, generated_at) so the DB stays the single source of truth, plus the markdown file in
  `reports/pursue/`. Auto-migrate on open, consistent with current schema behavior.
- **CLI & UI:** MVP ships the CLI (`--pursue --doc <id>` and `--pursue "<institution>"`). Phase 2
  adds a **Pursue** button on Opportunities rows and the batch mode.

## MVP scope

**In scope:** single-document Pursue Mode for `doc_type` in `bid` / `rfp` / `federal_rfp`;
deterministic disqualifier gates; the LLM-filled fit rubric producing GO/REVIEW/NO-GO with
win-probability and confidence; the Pursue Brief markdown to `reports/pursue/`; DB persistence of the
decision; graceful use of `gtm_profile`, watchlist, `payments`, and optionally HubSpot and Apollo;
offline `pytest` with the `claude` CLI and network mocked.

**Out of scope for MVP (planned later):** docx export via the lifted converter; batch triage; the UI
button; a ghost-theme library; feeding the pursue decision back into opportunity ranking; any
win/loss learning loop; anything resembling a multi-agent proposal pipeline.

## Effort & phasing

- **Phase 1 (MVP):** ~1–1.5 days — one module + prompt + rubric config + tests on top of existing plumbing.
- **Phase 2 (UI button + batch):** ~0.5 day.
- **Phase 3 (docx export, ghost-theme tuning, ranking feedback):** opportunistic.

## Test plan (offline pytest)

Fixtures with the `claude` CLI mocked:

- a nursing-program RFP that should **GO** on Attend + compliance fit;
- a K-12 North America doc that must **hard-gate to NO-GO without an LLM call**;
- an admissions-CRM-only RFP that **disqualifies**;
- an RFP from an institution with **EAB in its `payments` footprint**, asserting the displacement
  angle appears.

Assertions cover gate behavior, decision mapping, rubric completeness with evidence present, and
provenance correctness.

## Risks & mitigations

- **LLM over-optimism on fit** → deterministic gates first + a required evidence quote per dimension,
  so an unsupported score is visible.
- **Thin document text** → the confidence level drops when little real text was available.
- **Token cost** → gates run before any LLM call.
- **Scope creep back toward a proposal pipeline** → the non-goals above are explicit.
