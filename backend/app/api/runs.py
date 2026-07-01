"""
Runs API — trigger and inspect triage runs.

POST /api/runs starts a run in a background task and returns immediately with
the Run row (status='running'). The frontend polls GET /api/runs/{id} until
status becomes 'done' or 'error', then refreshes the job list.

A run does real network work (Gmail fetch + JD fetches + Claude calls), so it
must not block the request. For a single local user a FastAPI BackgroundTask
is sufficient; a cloud deployment would move this to a proper task queue.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from ..db import engine, get_session
from ..models import Run
from ..schemas import RunOut

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _run_in_background(hours_back: int | None) -> None:
    # Fresh session — this runs outside the request lifecycle.
    from .engine_bridge import run_triage_bg
    run_triage_bg(hours_back)


@router.post("", response_model=RunOut)
def trigger_run(background: BackgroundTasks, hours: int | None = None,
                session: Session = Depends(get_session)):
    # Refuse to start if one is already running (single-user simplicity).
    active = session.exec(select(Run).where(Run.status == "running")).first()
    if active:
        raise HTTPException(status_code=409, detail=f"run #{active.id} already in progress")

    background.add_task(_run_in_background, hours)
    # Return a lightweight placeholder; the real Run row is created in the task.
    # Frontend should poll the runs list for the newest entry.
    return RunOut(
        id=-1, started_at=__import__("datetime").datetime.utcnow(),
        hours_back=hours or 0, status="running",
        n_pursue=0, n_review=0, n_skipped=0, n_emails=0,
    )


@router.get("", response_model=list[RunOut])
def list_runs(session: Session = Depends(get_session), limit: int = 20):
    runs = session.exec(select(Run).order_by(Run.id.desc()).limit(limit)).all()
    return runs


def _scan_in_background(hours_back: int | None) -> None:
    # Fresh session — runs outside the request lifecycle.
    from .engine_bridge import run_scan_bg
    run_scan_bg(hours_back)


@router.post("/scan", response_model=RunOut)
def trigger_scan(background: BackgroundTasks, hours: int | None = None,
                 session: Session = Depends(get_session)):
    """Kick off a portal scan (Workday / Greenhouse / SmartRecruiters). Scanned
    roles flow through the same pipeline as the email run and land in the same
    job list tagged source="Scanner/<company>". Shares the single-run guard with
    the email run since both write the Job table."""
    active = session.exec(select(Run).where(Run.status == "running")).first()
    if active:
        raise HTTPException(status_code=409, detail=f"run #{active.id} already in progress")

    background.add_task(_scan_in_background, hours)
    return RunOut(
        id=-1, started_at=__import__("datetime").datetime.utcnow(),
        hours_back=hours or 0, status="running",
        n_pursue=0, n_review=0, n_skipped=0, n_emails=0,
    )


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run
