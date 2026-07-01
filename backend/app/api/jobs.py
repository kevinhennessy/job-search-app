"""
Jobs API.

The "passed" bucket is derived, not stored as a category: a job is "passed"
when its JobState.status == 'pass'. The list endpoint exposes both the
classifier category and the user status so the frontend can replicate the
old digest's Pursue / Review / Passed / Skipped sections and filter tabs.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..db import get_session
from ..models import Job, JobState
from ..schemas import JobOut, JobPatch, StatsOut

router = APIRouter(prefix="/api", tags=["jobs"])

VALID_STATUSES = {"applied", "pass", "later", "closed"}


def _merge(job: Job, state: Optional[JobState]) -> JobOut:
    return JobOut(
        id=job.stable_id,
        title=job.title,
        company=job.company,
        location=job.location,
        salary=job.salary,
        source=job.source,
        url=job.url,
        snippet=job.snippet,
        category=job.category,
        reason=job.reason,
        claude_reason=job.claude_reason,
        warning=job.warning,
        status=state.status if state else None,
        note=state.note if state else "",
        first_seen=job.first_seen,
        last_seen=job.last_seen,
        last_alert_date=job.last_alert_date,
    )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: Session = Depends(get_session),
    category: Optional[str] = Query(None, description="pursue | review | skipped"),
    include_skipped: bool = Query(False, description="Include skipped jobs (excluded by default)."),
):
    jobs = session.exec(select(Job)).all()
    states = {s.stable_id: s for s in session.exec(select(JobState)).all()}

    out = []
    for job in jobs:
        if category and job.category != category:
            continue
        if not category and not include_skipped and job.category == "skipped":
            continue
        out.append(_merge(job, states.get(job.stable_id)))

    # Stable ordering: pursue, review, stretch, then skipped; alpha by title within.
    order = {"pursue": 0, "review": 1, "stretch": 2, "skipped": 3}
    out.sort(key=lambda j: (order.get(j.category, 4), j.title.lower()))
    return out


@router.patch("/jobs/{stable_id}", response_model=JobOut)
def patch_job(stable_id: str, patch: JobPatch, session: Session = Depends(get_session)):
    job = session.get(Job, stable_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    state = session.get(JobState, stable_id) or JobState(stable_id=stable_id)

    if patch.status is not None:
        # Empty string clears the status; otherwise must be a known value.
        if patch.status == "":
            state.status = None
        elif patch.status in VALID_STATUSES:
            state.status = patch.status
        else:
            raise HTTPException(status_code=400, detail=f"invalid status: {patch.status}")
    if patch.note is not None:
        state.note = patch.note

    state.updated_at = datetime.utcnow()
    session.add(state)
    session.commit()
    session.refresh(state)
    return _merge(job, state)


@router.get("/stats", response_model=StatsOut)
def stats(session: Session = Depends(get_session)):
    jobs = session.exec(select(Job)).all()
    passed_ids = {
        s.stable_id for s in session.exec(select(JobState)).all() if s.status == "pass"
    }
    pursue = sum(1 for j in jobs if j.category == "pursue" and j.stable_id not in passed_ids)
    review = sum(1 for j in jobs if j.category == "review" and j.stable_id not in passed_ids)
    stretch = sum(1 for j in jobs if j.category == "stretch" and j.stable_id not in passed_ids)
    skipped = sum(1 for j in jobs if j.category == "skipped")
    return StatsOut(pursue=pursue, review=review, stretch=stretch, passed=len(passed_ids), skipped=skipped)
