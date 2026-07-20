"""
FastAPI entrypoint.

Run locally:   uvicorn app.main:app --reload --port 8000
API docs at:   http://localhost:8000/docs

In production you can build the React app and drop its dist/ into
backend/static/ — this app will then serve the SPA at / and the API under
/api, all from one process (and one SQLite file).
"""

import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import settings
from .api import fit, jobs, runs
from .db import init_db, sweep_stale_runs

app = FastAPI(title="Job Search Triage", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Files that actually drive triage/scan behavior — see CLAUDE.md's
# "Run-coverage gaps" and "Indeed digest-header" gotchas, both of which
# turned out to hinge on which process was actually running which code
# across a --reload restart. Hashed (not git-commit-hashed) since this
# workflow routinely has uncommitted working-tree changes.
_TRIAGE_FILES = [
    Path(__file__).resolve().parent / "triage" / "legacy.py",
    Path(__file__).resolve().parent / "triage" / "engine.py",
    Path(__file__).resolve().parent / "triage" / "config.py",
]


def _hash_triage_files() -> str:
    h = hashlib.sha256()
    for f in _TRIAGE_FILES:
        try:
            h.update(f.read_bytes())
        except FileNotFoundError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


# Captured once at process start — reflects what THIS process actually loaded,
# as opposed to _hash_triage_files() called fresh per-request below (what's on
# disk right now). If the two ever disagree, the running process is serving
# stale code despite the files having changed underneath it — exactly the
# failure mode that let a pre-fix process silently serve a run tonight even
# though the fix was already on disk. See /api/health's "stale" field.
_APP_STARTED_AT = datetime.utcnow()
_LOADED_CODE_HASH = _hash_triage_files()


@app.on_event("startup")
def _startup() -> None:
    init_db()
    recovered = sweep_stale_runs()
    if recovered:
        import logging
        logging.getLogger("triage").warning(
            "Startup sweep auto-recovered %d stale 'running' run row(s)", recovered
        )


@app.get("/api/health")
def health() -> dict:
    current_hash = _hash_triage_files()
    return {
        "ok": True,
        "started_at": _APP_STARTED_AT.isoformat(),
        "loaded_code_hash": _LOADED_CODE_HASH,
        "current_disk_hash": current_hash,
        "stale": current_hash != _LOADED_CODE_HASH,
    }


app.include_router(jobs.router)
app.include_router(runs.router)
app.include_router(fit.router)

# Optional: serve a built frontend (backend/static) as a SPA at /.
# Safe to leave in — it only mounts if the directory exists.
_static = Path(__file__).resolve().parent.parent / "static"
if _static.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
