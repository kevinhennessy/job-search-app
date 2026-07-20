# CLAUDE.md — job-search-app

Standing context and rules for working in this repo. Read this first; it encodes
decisions that aren't obvious from the code alone. Keep it lean — add a rule here
only when it would have prevented a real mistake.

---

## What this is

A personal job-search triage tool. It ingests job postings from two sources —
Gmail job-alert emails and employer career-site portals — runs each through a
shared classify → JD-resolve → Claude-evaluate pipeline, and presents them in one
list bucketed **Pursue / Review / Stretch / Skipped**. The user marks status and
notes per job. The target search is entry-level Data Science / ML / AI roles in
the NC Research Triangle, healthcare-leaning.

Stack: **FastAPI + SQLite (SQLModel)** backend, **React + Vite + TypeScript**
frontend. Single-developer, runs locally on Windows.

---

## Architecture map

Backend (`backend/app/`):

- `main.py` — FastAPI app. Mounts `backend/static/` as a SPA at `/` **only if that
  directory exists** (used for single-port production builds; dormant otherwise).
- `models.py` — `Job`, `JobState`, `Run`, `JdCache`.
  - **`Job`** is regenerated every run and upserted by `stable_id` (a hash of
    title|company). Do not treat `Job` rows as durable user data.
  - **`JobState`** holds the user's edits (status, notes). It is keyed to the job
    and **must never be written by a run** — runs only touch `Job`.
- `triage/legacy.py` — the heart. Ported, consolidated logic from the original
  standalone `triage.py`: Gmail parsing, `build_query`, `extract_jobs`,
  `identify_source`, the per-sender hand parsers, `classify_job`,
  `claude_evaluate_jobs` / `evaluate_job`, and `llm_extract_jobs`. This is the
  single consolidated module — when changing evaluation/parsing logic, change it
  here, not in a copy.
- `triage/config.py` — all tunable rules: `SENDERS` (the curated sender allowlist),
  `JOB_INTENT_KEYWORDS`, include/exclude keyword lists, skip-company lists, salary
  gates, `PROJECT_DIR`, budgets.
- `triage/scanner.py` — portal scanner (Workday / Greenhouse / SmartRecruiters
  public APIs). `scan_all_portals()` returns job dicts tagged
  `source="Scanner/<company>"`. Ported from the standalone `scan.py`; the CLI,
  `scan_seen.json`, and digest writers were intentionally **not** ported (the DB
  handles persistence and dedup).
- `triage/engine.py` — orchestration. `run_triage` (email) and `run_scan` (portals)
  both call the shared `_process_and_persist` pipeline (classify → dedup →
  JD-resolve → evaluate → Stretch-route → persist). Keep that shared helper shared.
- `triage/jd_resolver.py` — Tavily / SerpAPI JD fetch + `JdCache`.
- `triage/run.py` — CLI entry (`python -m app.triage.run --hours 24`).
- `api/jobs.py` — job list + state (mark/notes) endpoints.
- `api/runs.py` — `POST /api/runs` (email), `POST /api/runs/scan` (portals),
  list/get. Both triggers share a single-run guard.
- `api/engine_bridge.py` — gives background tasks their own DB session
  (`run_triage_bg`, `run_scan_bg`).

Frontend (`frontend/src/`):

- `App.tsx` — main view; the Run triage / Scan portals buttons live in `.run-actions`.
- `components/JobCard.tsx` — renders a job; shows the `source` pill and the
  conversational reason note (green `.positive` for Pursue, amber otherwise).
- `lib/api.ts` — API client; uses **relative** URLs so it works under the Vite
  dev proxy *and* a single-process production build.
- `index.css` — styles.

**`candidate_profile.md` is NOT in this repo.** It lives in `PROJECT_DIR`
(`C:\Users\maryk\OneDrive\Documents\job-search`, overridable via the `JOBSEARCH_DIR`
env var) and is read by both this app and the standalone scripts. It is the single
source of truth for who the candidate is.

---

## Non-negotiable rules

1. **`candidate_profile.md` is the only candidate context.** The evaluator loads it
   from `config.PROJECT_DIR`. Never reintroduce a hardcoded candidate blurb in code —
   that drift was already removed once. If candidate context seems missing, check the
   path / `JOBSEARCH_DIR`, don't inline a substitute.

2. **Never overclaim on the candidate's behalf.** This project's governing value:
   every skill/credential surfaced must be something the candidate can defend. The
   evaluator should assess honestly against the profile; don't pad or flatter. (The
   résumé/cover-letter materials are a *separate* workstream outside this repo and
   follow the same rule even harder.)

3. **`JobState` is sacred.** Runs regenerate `Job` rows; they must never overwrite
   the user's status/notes in `JobState`.

4. **Classifier / evaluator changes take effect on the *next run*,** not
   retroactively. Evaluations are computed at run time and stored. Say so when a
   change won't show until a fresh Run triage / Scan.

5. **The scan feature is one interdependent unit.** `scanner.py`, the
   `engine.run_scan` + `_process_and_persist` split, the `runs.py` scan endpoint,
   `engine_bridge.run_scan_bg`, and the frontend button move together. If you change
   the evaluator's return shape, update *every* caller in the same change.

---

## How ingestion works

- **Run triage** = read Gmail. `build_query` builds
  `((from:allowlist…) OR subject:(job-intent keywords…)) newer_than:Nd`. Known
  senders route to fast, free hand parsers. Unknown senders that matched a
  job-intent keyword go to `llm_extract_jobs` (Haiku, **budget-capped** per run via
  `config.LLM_EXTRACT_MAX_PER_RUN`, counter reset in `build_query`), which returns
  structured listings or nothing for non-job mail.
- **Scan portals** = query employer career-site APIs directly. No email involved.
- Both feed the same pipeline and land in the same list. Scanned jobs are tagged
  `Scanner/<company>`; unknown-sender email jobs are tagged with the sender's
  display name or domain.

Evaluator model: `claude-haiku-4-5-20251001`, called via `urllib` (no SDK
dependency). Keep that pattern if adding new LLM calls.

---

## Conventions

- **Validate before declaring done.** Backend: `python -m py_compile <files>` (or
  `compileall app`). Frontend TSX/TS: `esbuild <file> --format=esm --jsx=automatic`
  (syntax check without the full project types).
- **Run locally:** `start-local.bat` (backend on :8000 with `--reload`, Vite dev on
  :5173). First run creates the venv and installs deps. `start-local.bat --run` does
  a triage pass first.
- **Single-port production build** (for sharing/deployment): `npm run build` in
  `frontend/`, copy the `dist/` output into `backend/static/`, then run only the
  backend — it serves UI + API on :8000.

---

## Gotchas / hard-won lessons

- **Empty-employer artifact guard.** `classify_job` skips any job with no real
  company (`""`/`"unknown"`) whose URL contains `/jobs/search` — these are
  constructed keyword-search links, not real postings. Root cause: the
  Getro / NC-Biotech parser (`parse_getro_jobs`) builds a
  `linkedin.com/jobs/search?keywords=…Unknown` fallback URL when it can't parse the
  employer. The guard is the safety net; the parser is the root. The standalone
  `triage.py` has the same parser and would want the same guard if run.
- **Indeed digest-header phantom entries.** `_parse_indeed_jobs_h2` walks every
  `<h2>` in the email to find job-card titles — but Indeed's own alert-summary
  headline ("2 new data analyst jobs in Durham, NC") is *also* wrapped in an
  `<h2>`, right alongside the real listings, so a naive walk picks it up as if
  it were a posting. It's a convincing fake: the title is job-shaped ("...jobs
  in Durham, NC") and it survives every existing exclusion (footer-link list,
  unsubscribe/manage/privacy keywords) since it's neither. Symptom: it lands in
  Review as "cannot evaluate, no real JD" — because there is no real JD, there's
  no real job, just Indeed's own count-of-results sentence with a "company" of
  literally "These jobs match your saved job alert…". Confirmed via the full
  historical corpus (150 persisted Indeed rows): 16 (~11%) were this artifact,
  100% of them sharing that exact company boilerplate — a real, systematic
  noise source, not a one-off. Fixed by `_is_digest_header(title, company)`,
  gated on BOTH signals together (title matches `^\d+ new .* jobs? .* in`, AND
  company starts with the alert-boilerplate string) rather than title alone —
  a real job title starting with a number and mentioning "jobs...in" somewhere
  is conceivable in isolation, but pairing that with the literal alert-summary
  company string is not, so the dual check has zero false-positive risk against
  a genuine posting. Checked NCWorks and LinkedIn for the same class of bug
  (a digest parser walking all headings/blocks without distinguishing a
  summary line from real listings) — neither is vulnerable: NCWorks walks
  structured `<td class="JOBSLISTCELLTITLE">` table cells, not headings, so a
  summary sentence has nowhere to attach; LinkedIn's plain-text block parser
  doesn't produce the artifact in practice either (zero instances across 668
  real LinkedIn/NCWorks rows checked). Indeed-specific.
- **Hard-blocker routing covers location AND general disqualifiers.** The
  evaluator prompt's JSON schema has two boolean flags — `location_hard_blocker`
  (relocation/residency/hybrid-outside-the-Triangle, added first) and
  `hard_blocker` (program-specific eligibility like "must be currently
  enrolled," staffing/contracting with an undisclosed client, clearance/
  citizenship/license — criteria 2-4 in the prompt, added after an ABB
  internship posting requiring current enrollment landed in Review with
  Claude's own reasoning correctly calling it a hard blocker, but nothing in
  the schema to act on that reasoning). Both route identically: tagged
  `"Skip: …"` in the reason string, read by `engine._is_hard_blocker_reason`
  (checks the prefix, not which specific flag fired) to bucket into Skipped
  ahead of the Stretch check. If a *third* category of hard blocker shows up,
  add another boolean to the schema rather than overloading one of the
  existing two — keeps each flag's prompt guidance specific and auditable.
- **`JD_BLOCKED_SOURCES`** (e.g. NCWorks, Indeed) get snippet-based evaluation with a
  warning tag — don't attempt a full JD fetch for them.
- **NCWorks URL resolution keeps off-site redirects.** `_resolve_ncworks_url` follows
  the NCWorks tracking link and keeps wherever it lands — including off-`ncworks.gov`
  redirects to the employer's real ATS (e.g. `unc.peopleadmin.com`) — since that's the
  actual posting. It only falls back to the NCWorks search URL when the redirect lands
  on a search/criteria page or the request fails. Relatedly, `_process_and_persist`
  only overwrites `job["url"]` with `jd_url` when the current URL is weak (empty,
  NCWorks, or an aggregator) and `jd_url` isn't itself an aggregator — otherwise a good
  direct posting link could get clobbered by whatever page the JD resolver pulled text
  from. Like all evaluator/engine changes, this only affects jobs picked up on the
  *next* Run triage / Scan, not already-persisted rows.
- **Gmail OAuth must be in Production** (not Testing) in Google Cloud Console, or the
  token expires under Google's 2SV enforcement.
- **The standalone scripts** (`triage.py`, `scan.py`) predate this app and evolve
  separately. This app's `legacy.py` is the source of truth for app behavior; the
  standalones won't automatically have app-side fixes.
- **Run-coverage gaps, not a multi-job parsing bug.** A job ("Alta Planning +
  Design") once appeared to vanish entirely from an Indeed multi-listing digest
  email, with a sibling listing from the same run landing fine — looked exactly
  like "the Indeed parser only extracts the first job." Direct testing (fetching
  the real message by id and running `extract_jobs`/`classify_job`/
  `claude_evaluate_jobs` on it in isolation, bypassing the run entirely) proved
  the parser and pipeline handle multi-job Indeed emails correctly; both listings
  extracted and evaluated fine. The sibling "successful" job in the DB turned out
  to have come from a *different* sender (NCWorks) that coincidentally listed the
  same employer — confirmed by an exact company-string match, not assumed. The
  real cause: `run_triage` queried Gmail for a fixed `hours_back` rollback from
  "now," so any two runs spaced further apart than that window leave an
  uncovered gap between them — whatever arrived in the gap is silently never
  fetched, by any sender, not just Indeed. Fixed by `Run.covered_through`: each
  successful email run stamps the instant its query was issued (captured
  *before* the fetch, so anything arriving mid-run is left for the next run
  rather than risking a race); `engine._coverage_floor` uses the last
  successful run's `covered_through` as the next run's query floor (via
  `build_query`'s new `since` param → an exact `after:<epoch-seconds>` clause,
  not day-granular, so it doesn't reintroduce the day-boundary problem
  `newer_than:Nd` was chosen to avoid) instead of a fixed rollback — capped by
  `config.COVERAGE_MAX_LOOKBACK_HOURS` (14 days) so a long-dormant app doesn't
  suddenly pull months of backlog on its next run. `covered_through` is only
  set on success, never on error, so a failed run doesn't falsely advance
  coverage past a gap it never actually queried. Portal scans are unaffected —
  they scan currently-open roles, not a time window, so this doesn't apply.
  Lesson: when a "missing item" report includes a sibling that *did* succeed,
  verify the sibling actually came from the same source before trusting that
  as evidence of a single-item bug — here it was two unrelated facts, not one
  inconsistency.
- **`--reload` can silently hang mid-restart; `WATCHFILES_FORCE_POLLING`
  does NOT fix it.** Tested directly (2026-07-19): started uvicorn with
  polling enabled, modified a watched file, WatchFiles correctly detected it
  and logged "Reloading..." — but the worker process was never actually
  replaced. Same PID, same stale `/api/health` code hash, 20+ seconds later
  with zero progress. Polling only changes how file changes are *detected*,
  which wasn't the broken step here; the *respawn* itself hangs regardless,
  at least in this dev environment. Don't re-try this fix blind in a future
  session — if `--reload` seems to have silently stopped applying changes,
  kill the whole process tree (reloader + worker) and start fresh rather than
  trusting an env var to unstick it. `/api/health`'s `stale` field
  (`loaded_code_hash` vs `current_disk_hash`, the latter rehashed fresh per
  request) is the actual safety net for catching this live instead of
  discovering it days later — check it, don't assume reload worked.
- **Scan Portals verified working end-to-end (2026-07-19).** After a session
  that touched only the email-triage path (`build_query`, `_coverage_floor`,
  the Indeed h2 parser), triggered a real portal scan to confirm it was
  actually unaffected rather than assuming so: 54 jobs landed, correctly
  tagged `Scanner/<company>` across 7 employers, correctly classified, visible
  through both the DB and the live API. Worth re-verifying the same way after
  any future change to `engine.py` or `scanner.py`, since `_process_and_persist`
  is shared but nothing in a typical email-triage session exercises
  `scanner.py`'s own code paths (`scan_all_portals`, `scanner_location_ok`).

---

## Deployment status (planned, on hold)

Phase 1, agreed but not yet built: keep ingestion local (Gmail OAuth + keys stay on
this machine), expose the app via a **secure tunnel** (Tailscale or Cloudflare
Tunnel — undecided), **shared workspace** (no per-user accounts), and give the second
viewer **view + mark only** by hiding the Run/Scan buttons when
`window.location.hostname` is not localhost. Single-port production build is the
thing to tunnel. Currently paused — the second user is busy elsewhere and already
sees curated results through an external tracker.

---

## Front-end reskin (planned)

A *visual-only* refresh of the UI, to be done as its own self-contained pass —
**not** while functional/pipeline changes are in flight (keep "does it behave" and
"does it look right" separable).

Sequence:
1. Do it once the app is stable and you're settled in Claude Code — not mid-change.
2. Prototype the new look in **Claude Design** (claude.ai/design). Seed it with the
   current `frontend/src/index.css` and a real `JobCard.tsx` so it riffs on the
   existing visual language (the pursue-green / review-amber / stretch-purple
   tokens, pill and card styling) rather than inventing a generic look from a blank
   canvas — Design produces "functional but generic" output without a design system
   to anchor to.
3. Hand the result back to **Claude Code** (Design has a one-instruction handoff
   bundle) to wire it into the real `App.tsx`, `JobCard.tsx`, and `index.css`
   against live data. Design yields a static artifact; it doesn't know the card is
   fed by real `Job` objects with conditional rendering.

**Boundary — visual only.** In scope: layout, spacing, color, typography, the card
and list presentation. Out of scope: the data flow and any *what-renders* logic —
the warning-vs-positive note, the `source` pill, collapsed sections, and the
localhost button-gating for the view-only deployment. If a change alters *what*
renders rather than *how* it looks, it's functional work for Claude Code, not Design.

---

## Who does what (out-of-repo context)

Infrastructure, this app, and résumé/cover-letter production are handled by one
person; networking and direct applications by the candidate. New résumé skills are
*earned before they're added* (e.g. via a hackathon project) — relevant only because
it explains why the candidate profile changes over time, which changes evaluations.
