"""
Database models.

Design note — why two tables for what was one JSON file:

  Job        is REGENERATED every triage run (parsers + classifier output).
  JobState   holds USER edits (status + note) and is NEVER touched by a run.

They share the same stable key (`stable_id`, produced by the engine's
_job_id: "title|company" slugified) so user edits survive re-runs and
re-classification. This mirrors the old job_notes.json keyed by _job_id,
promoted to a real table.

Multi-user seam: when Aidan becomes a real second user, add a `user_id`
column to JobState (and a Users table) and make the PK composite
(stable_id, user_id). Nothing else in the schema needs to change.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    # Stable across runs: slug of "title|company". Same job re-seen = same row.
    stable_id: str = Field(primary_key=True)

    title: str = ""
    company: str = ""
    location: str = ""
    salary: Optional[str] = None
    source: str = ""
    url: str = ""
    snippet: str = ""

    # Classifier output
    category: str = "skipped"          # pursue | review | skipped
    reason: Optional[str] = None       # rule-based reason (review/skipped)
    claude_reason: Optional[str] = None  # Claude evaluator explanation, if demoted
    warning: bool = False              # snippet-only / JD-unfetchable flag (the old ⚠)

    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    last_alert_date: Optional[datetime] = None  # most recent job-alert email date (freshness signal)
    last_run_id: Optional[int] = Field(default=None, foreign_key="run.id")


class JobState(SQLModel, table=True):
    # Same key as Job.stable_id. Kept separate so triage runs never clobber edits.
    stable_id: str = Field(primary_key=True)
    status: Optional[str] = None       # applied | pass | later | closed | None
    note: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    hours_back: int = 24
    run_type: str = "triage"           # triage (email) | scan (portals)
    status: str = "running"            # running | done | error
    error: Optional[str] = None
    n_pursue: int = 0
    n_review: int = 0
    n_stretch: int = 0
    n_skipped: int = 0
    n_emails: int = 0

    # The Gmail-timestamp point this run's query covered up through (set only
    # on a successful email run — never for a portal scan, never on error).
    # The next run_triage call uses the most recent one of these as its query
    # floor instead of a fixed hours_back rollback, so gaps between irregular
    # run cadences can't silently drop emails between two runs' lookback
    # windows. See CLAUDE.md's "Run-coverage gaps" gotcha.
    covered_through: Optional[datetime] = None


class JobFit(SQLModel, table=True):
    """Cached Job Fit Evaluator result, keyed by the job's stable id. Computed
    on demand (one Claude call per job, triggered from the frontend, not part
    of the triage/scan pipeline) and never touched by a run — same caching
    pattern as JdCache below. Matches/gaps/flags are stored JSON-encoded since
    SQLite has no native array column."""
    stable_id: str = Field(primary_key=True)
    overall_score: int
    title_fit: int
    experience_bar: int
    niche_match: int
    verdict: str                # apply | caution | skip
    verdict_reason: str
    matches_json: str = "[]"
    gaps_json: str = "[]"
    flags_json: str = "[]"
    summary: str
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


class JdCache(SQLModel, table=True):
    """Cached Tavily JD-lookup result, keyed by the job's stable id.

    Ensures a role is only looked up once (cost control). `found=False` with an
    empty `jd_text` is a negative cache entry, so we don't re-query a miss.
    """
    stable_id: str = Field(primary_key=True)
    jd_text: str = ""
    jd_url: str = ""
    found: bool = False
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
