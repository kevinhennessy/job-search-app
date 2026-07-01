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
- **`JD_BLOCKED_SOURCES`** (e.g. NCWorks, Indeed) get snippet-based evaluation with a
  warning tag — don't attempt a full JD fetch for them.
- **Gmail OAuth must be in Production** (not Testing) in Google Cloud Console, or the
  token expires under Google's 2SV enforcement.
- **The standalone scripts** (`triage.py`, `scan.py`) predate this app and evolve
  separately. This app's `legacy.py` is the source of truth for app behavior; the
  standalones won't automatically have app-side fixes.

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
