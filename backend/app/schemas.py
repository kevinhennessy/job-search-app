"""API request/response schemas (kept separate from DB tables)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class JobOut(BaseModel):
    """A job merged with its user state (status + note)."""
    id: str                    # == Job.stable_id
    title: str
    company: str
    location: str
    salary: Optional[str] = None
    source: str
    url: str
    snippet: str
    category: str
    reason: Optional[str] = None
    claude_reason: Optional[str] = None
    warning: bool = False
    status: Optional[str] = None   # from JobState
    note: str = ""                 # from JobState
    first_seen: datetime
    last_seen: datetime
    last_alert_date: Optional[datetime] = None

    # Cached Job Fit Evaluator result (from JobFit), None until evaluated.
    fit_overall: Optional[int] = None
    fit_title: Optional[int] = None
    fit_experience: Optional[int] = None
    fit_niche: Optional[int] = None
    fit_verdict: Optional[str] = None       # apply | caution | skip
    fit_verdict_reason: Optional[str] = None
    fit_matches: list[str] = []
    fit_gaps: list[str] = []
    fit_flags: list[str] = []
    fit_summary: Optional[str] = None


class JobPatch(BaseModel):
    """Partial update to a job's user state. Either field may be present."""
    status: Optional[str] = None   # applied | pass | later | closed | "" (clear) | None
    note: Optional[str] = None


class StatsOut(BaseModel):
    pursue: int
    review: int
    stretch: int
    passed: int
    skipped: int


class FitEvaluateRequest(BaseModel):
    posting: str


class FitEvaluateResult(BaseModel):
    overall_score: int
    title_fit: int
    experience_bar: int
    niche_match: int
    verdict: str            # apply | caution | skip
    verdict_reason: str
    matches: list[str] = []
    gaps: list[str] = []
    flags: list[str] = []
    summary: str


class RunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    hours_back: int
    status: str
    error: Optional[str] = None
    n_pursue: int
    n_review: int
    n_stretch: int = 0
    n_skipped: int
    n_emails: int
