"""
Jobs API.

The "passed" bucket is derived, not stored as a category: a job is "passed"
when its JobState.status == 'pass'. The list endpoint exposes both the
classifier category and the user status so the frontend can replicate the
old digest's Pursue / Review / Passed / Skipped sections and filter tabs.

Also exposes a per-job Job Fit Evaluator endpoint (backed by JobFit, a cache
table): the frontend fires one call per not-yet-evaluated active job, in
parallel, and the result is persisted so it's never recomputed on a later
page load unless explicitly forced. This sits alongside the existing
classify_job/claude_evaluate_jobs category — it does not replace it.
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..db import get_session
from ..models import Job, JobFit, JobState
from ..schemas import JobOut, JobPatch, StatsOut
from .fit import build_prompt_from_job, call_claude_fit

router = APIRouter(prefix="/api", tags=["jobs"])

VALID_STATUSES = {"applied", "pass", "later", "closed"}


def _merge(job: Job, state: Optional[JobState], fit: Optional[JobFit] = None) -> JobOut:
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
        fit_overall=fit.overall_score if fit else None,
        fit_title=fit.title_fit if fit else None,
        fit_experience=fit.experience_bar if fit else None,
        fit_niche=fit.niche_match if fit else None,
        fit_verdict=fit.verdict if fit else None,
        fit_verdict_reason=fit.verdict_reason if fit else None,
        fit_matches=json.loads(fit.matches_json) if fit else [],
        fit_gaps=json.loads(fit.gaps_json) if fit else [],
        fit_flags=json.loads(fit.flags_json) if fit else [],
        fit_summary=fit.summary if fit else None,
    )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: Session = Depends(get_session),
    category: Optional[str] = Query(None, description="pursue | review | skipped"),
    include_skipped: bool = Query(False, description="Include skipped jobs (excluded by default)."),
):
    jobs = session.exec(select(Job)).all()
    states = {s.stable_id: s for s in session.exec(select(JobState)).all()}
    fits = {f.stable_id: f for f in session.exec(select(JobFit)).all()}

    out = []
    for job in jobs:
        if category and job.category != category:
            continue
        if not category and not include_skipped and job.category == "skipped":
            continue
        out.append(_merge(job, states.get(job.stable_id), fits.get(job.stable_id)))

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
    fit = session.get(JobFit, stable_id)
    return _merge(job, state, fit)


@router.post("/jobs/{stable_id}/fit-evaluate", response_model=JobOut)
def evaluate_job_fit(
    stable_id: str,
    force: bool = Query(False, description="Re-evaluate even if a cached result exists."),
    session: Session = Depends(get_session),
):
    """Evaluate (or return the cached evaluation for) one job against the
    candidate profile. Cached in JobFit so a given job is only ever scored
    once unless force=true (e.g. after a candidate_profile.md update)."""
    job = session.get(Job, stable_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    existing = session.get(JobFit, stable_id)
    if existing and not force:
        state = session.get(JobState, stable_id)
        return _merge(job, state, existing)

    prompt = build_prompt_from_job(job.title, job.company, job.location, job.snippet, job.url)
    result = call_claude_fit(prompt)

    fit = existing or JobFit(stable_id=stable_id)
    fit.overall_score = result.overall_score
    fit.title_fit = result.title_fit
    fit.experience_bar = result.experience_bar
    fit.niche_match = result.niche_match
    fit.verdict = result.verdict
    fit.verdict_reason = result.verdict_reason
    fit.matches_json = json.dumps(result.matches)
    fit.gaps_json = json.dumps(result.gaps)
    fit.flags_json = json.dumps(result.flags)
    fit.summary = result.summary
    fit.evaluated_at = datetime.utcnow()
    session.add(fit)
    session.commit()
    session.refresh(fit)

    state = session.get(JobState, stable_id)
    return _merge(job, state, fit)


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
