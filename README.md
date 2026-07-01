# Job Search Triage — Web App

A rebuild of the original `triage.py` job-search tool as a proper web
application: **FastAPI + SQLite backend, React (Vite + TypeScript) frontend.**
The classification engine (Gmail parsing, keyword/salary/experience rules,
Claude evaluation) is carried over unchanged from the original script — only
the orchestration, storage, and UI layers are new.

```
job-search-app/
├── backend/
│   ├── app/
│   │   ├── main.py          FastAPI app (CORS, routers, optional static SPA)
│   │   ├── settings.py      env-overridable config (DB path, hours, CORS)
│   │   ├── db.py            SQLite engine + session + init
│   │   ├── models.py        Job / JobState / Run tables
│   │   ├── schemas.py       API request/response models
│   │   ├── api/
│   │   │   ├── jobs.py      GET /api/jobs, PATCH /api/jobs/{id}, GET /api/stats
│   │   │   ├── runs.py      POST /api/runs, GET /api/runs, GET /api/runs/{id}
│   │   │   └── engine_bridge.py   background-task session wrapper
│   │   └── triage/
│   │       ├── legacy.py    ← the original triage.py, unchanged except one import
│   │       ├── config.py    ← the original config.py (PROJECT_DIR now env-overridable)
│   │       ├── engine.py    orchestrates the pipeline → writes to SQLite
│   │       └── run.py       CLI runner (replaces the Task Scheduler entry)
│   └── requirements.txt
└── frontend/
    └── src/                 React app replicating the digest UI
```

## What maps to what (old → new)

| Original | Rebuild |
|---|---|
| `triage.py` parsers + `classify_job` + `claude_evaluate_jobs` | unchanged, now in `app/triage/legacy.py` |
| `triage.py main()` pipeline | `app/triage/engine.py: run_triage()` (persists instead of writing files) |
| HTML/markdown digest files | SQLite `Job` rows + React UI |
| `job_notes.json` (keyed by `_job_id`) | `JobState` table (same key) |
| Flask `--serve` + `localStorage` | FastAPI `/api` + DB persistence |
| Task Scheduler → `python triage.py` | Task Scheduler → `python -m app.triage.run` |

The key design choice: **`Job` rows are regenerated every run; `JobState`
(status + note) is never touched by a run.** They share the engine's stable
`_job_id` so your edits survive re-runs and re-classification — exactly the old
`job_notes.json` behavior, promoted to a table.

## Run it locally

You'll need the Gmail OAuth files (`credentials.json` / `token.json`) the
original tool used. By default the engine looks for them in
`C:\Users\maryk\OneDrive\Documents\job-search`; override with the
`JOBSEARCH_DIR` env var.

**Backend** (terminal 1):
```bash
cd backend
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# API docs: http://localhost:8000/docs
```

**Frontend** (terminal 2):
```bash
cd frontend
npm install
npm run dev
# App: http://localhost:5173  (proxies /api to :8000)
```

Set `ANTHROPIC_API_KEY` in the backend terminal (or the venv environment) so
the Claude evaluation step runs — same key the old `run_triage.bat` used.

### Triggering a run
Click **Run triage** in the UI (POSTs `/api/runs`, runs in the background, the
UI polls until done), or run the CLI directly:
```bash
cd backend
python -m app.triage.run --hours 24
python -m app.triage.run --skip-claude     # test without the Claude step
```

### Scheduled runs (replacing Task Scheduler)
Point Task Scheduler at the CLI runner instead of the old script:
```
python -m app.triage.run --hours 24
```
(working directory = `backend`, with `ANTHROPIC_API_KEY` and `JOBSEARCH_DIR`
set in the task's environment). It writes to the same `jobsearch.db` the web
app reads.

## Single-process deploy
For one process serving both API and UI:
```bash
cd frontend && npm run build
cp -r dist ../backend/static            # backend/static is auto-served if present
cd ../backend && uvicorn app.main:app --port 8000
```
Then everything is at `http://localhost:8000`.

## The path to cloud (decisions for later — not built yet)

Running locally first is the right call. Three things change when this goes to
the cloud, and each is a real decision worth making deliberately:

1. **Gmail OAuth token** — currently a local `token.json` tied to the
   `kevinmaryh@gmail.com` account. A cloud backend needs that token stored as a
   secret (or a service-account / domain-wide approach). This is the biggest
   single item.
2. **Scheduled runs** — Task Scheduler becomes a cloud scheduler (Render/Railway
   cron, Fly machines, or a GitHub Action hitting `POST /api/runs`).
3. **Persistence** — SQLite on a local disk is fine single-user. On a serverless
   host with an ephemeral filesystem you'd need a persistent volume or a managed
   Postgres. SQLModel makes the swap to Postgres a one-line `DATABASE_URL` change.

## Multi-user (when Aidan becomes a real second user)
The seam is one table. Add a `user_id` to `JobState`, make its PK composite
(`stable_id`, `user_id`), and add a minimal auth layer. Nothing else in the
schema or engine needs to change — `Job` rows (the classifier output) are shared;
only status/notes are per-user.
