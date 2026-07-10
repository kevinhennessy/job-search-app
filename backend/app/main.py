"""
FastAPI entrypoint.

Run locally:   uvicorn app.main:app --reload --port 8000
API docs at:   http://localhost:8000/docs

In production you can build the React app and drop its dist/ into
backend/static/ — this app will then serve the SPA at / and the API under
/api, all from one process (and one SQLite file).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import settings
from .api import fit, jobs, runs
from .db import init_db

app = FastAPI(title="Job Search Triage", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


app.include_router(jobs.router)
app.include_router(runs.router)
app.include_router(fit.router)

# Optional: serve a built frontend (backend/static) as a SPA at /.
# Safe to leave in — it only mounts if the directory exists.
_static = Path(__file__).resolve().parent.parent / "static"
if _static.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
