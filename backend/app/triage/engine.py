"""
engine.py — orchestrates a triage run and persists results to SQLite.

This reuses the existing pipeline verbatim (parsers, classify_job,
deduplicate, claude_evaluate_jobs) from ``legacy.py`` — the hard-won,
fragile logic is NOT reimplemented. The only new behaviour is:

  * the NCWorks dedup + URL-resolution steps that were nested inside the old
    ``main()`` are lifted here (they could not be imported), and
  * results are written to the Job / Run tables instead of HTML/markdown files,
    while never touching JobState (user edits).

``legacy`` is imported lazily inside ``run_triage`` so the FastAPI app can
start without the Google / bs4 dependencies installed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from .. import settings
from ..models import Job, Run


# --- helpers lifted from the old main() (were nested, hence not importable) ---

def _normalize_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower().strip())


def _ncworks_search_url(title: str, company: str) -> str:
    from urllib.parse import quote
    company_lower = company.lower()
    title_enc = quote(title)
    if "duke" in company_lower:
        return f"https://careers.duke.edu/search/?q={title_enc}"
    if "north carolina at chapel hill" in company_lower or "unc chapel hill" in company_lower:
        return f"https://unc.peopleadmin.com/postings/search?query={title_enc}"
    if "north carolina state" in company_lower or "nc state" in company_lower:
        return f"https://jobs.ncsu.edu/postings/search?query={title_enc}"
    return ""


def _resolve_ncworks_url(tracking_url: str, title: str, company: str) -> str:
    import requests
    if not tracking_url:
        return _ncworks_search_url(title, company)
    try:
        r = requests.get(
            tracking_url,
            allow_redirects=True,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        final = r.url
        if "JobSearchCriteriaQuick" in final or "SearchCriteria" in final:
            return _ncworks_search_url(title, company)
        return final
    except Exception:
        return _ncworks_search_url(title, company)


_AGGREGATOR_DOMAINS = (
    "indeed.com",
    "linkedin.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "monster.com",
    "simplyhired.com",
    "talent.com",
    "google.com",
)


def _is_aggregator_url(url: Optional[str]) -> bool:
    if not url:
        return False
    url_lower = url.lower()
    return any(domain in url_lower for domain in _AGGREGATOR_DOMAINS)


def _is_ncworks_url(url: Optional[str]) -> bool:
    return bool(url) and "ncworks.gov" in url.lower()


def _classify_reason_fields(reason: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
    """Split the engine's reason string into (display_reason, claude_reason, warning)."""
    if not reason:
        return None, None, False
    warning = reason.startswith("\u26a0")          # ⚠ snippet-only tag
    claude_reason = None
    if reason.startswith("Claude: "):
        claude_reason = reason[len("Claude: "):]
    elif reason.startswith("Stretch: "):
        claude_reason = reason[len("Stretch: "):]
    elif reason.startswith("Skip: "):
        claude_reason = reason[len("Skip: "):]
    return reason, claude_reason, warning


def _is_stretch_reason(reason: Optional[str]) -> bool:
    """Claude tagged this demotion as an experience stretch (see legacy evaluator)."""
    return bool(reason) and reason.startswith("Stretch:")


def _is_hard_blocker_reason(reason: Optional[str]) -> bool:
    """Claude tagged this demotion as a hard blocker — either location
    (relocation required, non-NC residency restriction, hybrid based outside
    the Triangle; see candidate_profile.md's Location constraints section and
    legacy evaluator's location_hard_blocker field) or general (program-
    specific eligibility, staffing/contracting with an undisclosed client,
    clearance/citizenship/license — legacy evaluator's hard_blocker field).
    Routed to Skipped rather than Review/Stretch since either is a harder
    disqualifier than an ordinary demotion."""
    return bool(reason) and reason.startswith("Skip:")


def _is_snippet_only(reason: Optional[str]) -> bool:
    """Claude could only see the email snippet (JD unfetchable) — the ⚠ warning."""
    return bool(reason) and reason.startswith("\u26a0")


def _is_experience_skip(reason: Optional[str]) -> bool:
    """The snippet-level experience gate skipped this for too many required years."""
    return bool(reason) and "years experience (exceeds" in reason


def _parse_alert_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse a job-alert email's Date header (RFC 2822) to a naive UTC datetime.
    Returns None on anything unparseable, so freshness is simply 'unknown'."""
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is not None:
            from datetime import timezone
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _upsert_job(session: Session, job: dict, category: str, reason: Optional[str], run_id: int) -> None:
    """Insert or update a Job row by stable_id, preserving first_seen."""
    from .legacy import _job_id  # stable id used by the old notes system

    sid = _job_id(job)
    display_reason, claude_reason, warning = _classify_reason_fields(reason)
    now = datetime.utcnow()

    existing = session.get(Job, sid)
    alert_date = _parse_alert_date(job.get("date"))
    if existing is None:
        session.add(Job(
            stable_id=sid,
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location") or "",
            salary=job.get("salary") or None,
            source=job.get("source") or "",
            url=job.get("url") or "",
            snippet=(job.get("snippet") or "")[:280],
            category=category,
            reason=display_reason,
            claude_reason=claude_reason,
            warning=warning,
            first_seen=now,
            last_seen=now,
            last_alert_date=alert_date,
            last_run_id=run_id,
        ))
    else:
        existing.title = job.get("title", "") or existing.title
        existing.company = job.get("company", "") or existing.company
        existing.location = job.get("location") or existing.location
        existing.salary = job.get("salary") or existing.salary
        existing.source = job.get("source") or existing.source
        existing.url = job.get("url") or existing.url
        existing.snippet = (job.get("snippet") or "")[:280] or existing.snippet
        existing.category = category
        existing.reason = display_reason
        existing.claude_reason = claude_reason
        existing.warning = warning
        existing.last_seen = now
        if alert_date and (existing.last_alert_date is None or alert_date > existing.last_alert_date):
            existing.last_alert_date = alert_date
        existing.last_run_id = run_id
        session.add(existing)


def _process_and_persist(session: Session, all_jobs: list, run: Run,
                         skip_claude: bool) -> None:
    """Shared post-ingestion pipeline used by BOTH the email run and the portal
    scan: NCWorks dedup, classify, dedup, NCWorks URL resolution, Tavily JD
    resolution, Claude evaluation (now against candidate_profile.md), the
    verified-Pursue split, Stretch routing, and persistence. Source-agnostic —
    the NCWorks-specific steps are no-ops for portal jobs. Sets run.n_pursue /
    n_review / n_stretch / n_skipped (callers finalize run.status)."""
    from . import legacy  # lazy: needs google-api / bs4 only when actually running

    # --- NCWorks dedup against direct employer sources (Duke / UNC) ---
    direct_keys = {
        _normalize_title(j.get("title", "")) + "|" + _normalize_title(j.get("company", ""))
        for j in all_jobs if j.get("source") in ("Duke Careers", "UNC Chapel Hill")
    }
    all_jobs = [
        j for j in all_jobs
        if not (
            j.get("source") == "NCWorks"
            and (_normalize_title(j.get("title", "")) + "|" + _normalize_title(j.get("company", ""))) in direct_keys
        )
    ]

    # --- classify ---
    pursue, review, skipped = [], [], []
    for job in all_jobs:
        category, reason = legacy.classify_job(job)
        (pursue if category == "pursue" else review if category == "review" else skipped).append((job, reason))

    pursue = legacy.deduplicate(pursue)
    review = legacy.deduplicate(review)
    skipped = legacy.deduplicate(skipped)
    pursue.sort(key=lambda x: x[0]["title"].lower())
    review.sort(key=lambda x: x[0]["title"].lower())

    # --- resolve NCWorks tracking URLs for pursue/review only ---
    for job_list in (pursue, review):
        for job, _ in job_list:
            if job.get("source") == "NCWorks":
                job["url"] = _resolve_ncworks_url(
                    job.get("url", ""), job.get("title", ""), job.get("company", "")
                )

    # --- resolve real JD text for blocked sources (Tavily) before eval ---
    # Populates job['jd_text'] for NCWorks/Indeed roles via cache->Tavily,
    # so the evaluator can judge experience fit instead of snippet-only.
    # No-ops if TAVILY_API_KEY isn't set. Capped per run for cost safety.
    if not skip_claude:
        from .jd_resolver import resolve_for_jobs
        budget = int(os.environ.get("TAVILY_MAX_LOOKUPS", "60"))
        used = resolve_for_jobs(session, pursue, budget)
        if used:
            legacy.log.info("Tavily JD lookups this run: %d", used)
        # Link cards to the real posting Tavily found, but only when the
        # current link is weak (missing/NCWorks/aggregator) and the JD
        # resolver's page isn't itself an aggregator — otherwise a good
        # direct posting link could get downgraded. Done before eval so
        # demoted roles keep the link too.
        for job, _r in pursue:
            jd_url = job.get("jd_url")
            if not jd_url or _is_aggregator_url(jd_url):
                continue
            current_url = job.get("url") or ""
            if not current_url or _is_ncworks_url(current_url) or _is_aggregator_url(current_url):
                job["url"] = jd_url

    # --- Claude evaluation (demotes poor-fit Pursue -> Review) ---
    if not skip_claude:
        pursue, review = legacy.claude_evaluate_jobs(pursue, review)

    # Pursue should mean "JD-verified fit". Roles Claude could only judge from
    # the email snippet (JD unfetchable, ⚠) are moved to Review for manual
    # eyeballing, so they don't bloat the high-confidence Pursue pile.
    verified_pursue: list = []
    for job, reason in pursue:
        if _is_snippet_only(reason):
            review.append((job, reason))
        else:
            verified_pursue.append((job, reason))
    pursue = verified_pursue

    # --- route experience-gap roles into the Stretch tier, and hard
    #     blockers (location or general) into Skipped ---
    # Stretch comes from two places: Claude demotions tagged "Stretch:" and
    # the snippet-level experience gate's skips. Hard blockers (tagged
    # "Skip:" — location, per candidate_profile.md, OR general: eligibility,
    # staffing/contracting with an undisclosed client, clearance/citizenship/
    # license) are a harder disqualifier than an ordinary demotion, so they're
    # pulled out of Review into Skipped instead of cluttering it. Everything
    # else is unchanged.
    stretch: list = []
    review_final: list = []
    skipped_final: list = []
    for job, reason in review:
        if _is_hard_blocker_reason(reason):
            skipped_final.append((job, reason))
        elif _is_stretch_reason(reason):
            stretch.append((job, reason))
        else:
            review_final.append((job, reason))
    for job, reason in skipped:
        (stretch if _is_experience_skip(reason) else skipped_final).append((job, reason))

    # --- persist ---
    for job, reason in pursue:
        _upsert_job(session, job, "pursue", reason, run.id)
    for job, reason in review_final:
        _upsert_job(session, job, "review", reason, run.id)
    for job, reason in stretch:
        _upsert_job(session, job, "stretch", reason, run.id)
    for job, reason in skipped_final:
        _upsert_job(session, job, "skipped", reason, run.id)

    run.n_pursue = len(pursue)
    run.n_review = len(review_final)
    run.n_stretch = len(stretch)
    run.n_skipped = len(skipped_final)


def _coverage_floor(
    session: Session,
    current_run_id: int,
    query_as_of: datetime,
    explicit_hours_back: Optional[int] = None,
) -> Optional[datetime]:
    """The Gmail-query floor for this run: normally the last successful email
    run's covered_through, capped so a long-dormant app doesn't suddenly pull
    months of backlog in one go.

    `explicit_hours_back` is the caller-specified --hours/hours value, as
    opposed to run_triage's settings-default fallback — a manual "go back
    further than normal" request (e.g. a one-off backfill) that must actually
    take effect rather than being silently overridden by the narrower
    steady-state coverage floor. When given, the floor is whichever of the
    two goes further back: the explicit request can WIDEN the query beyond
    what coverage-tracking alone would fetch, but can never NARROW it below
    what coverage-tracking already knows it needs to fetch (e.g. after a
    genuine gap) — an explicit --hours is a "go back at least this far," not
    a ceiling.

    Returns None (falls back to build_query's default hours_back/newer_than
    behavior) only when there's no prior coverage AND no explicit override —
    e.g. the very first run ever."""
    from . import config

    last_covered = session.exec(
        select(Run)
        .where(Run.id != current_run_id, Run.covered_through.is_not(None))
        .order_by(Run.covered_through.desc())
    ).first()

    coverage_floor = None
    if last_covered and last_covered.covered_through:
        cap_floor = query_as_of - timedelta(hours=config.COVERAGE_MAX_LOOKBACK_HOURS)
        coverage_floor = max(last_covered.covered_through, cap_floor)

    if explicit_hours_back is None:
        return coverage_floor

    explicit_floor = query_as_of - timedelta(hours=explicit_hours_back)
    if coverage_floor is None:
        return explicit_floor
    return min(coverage_floor, explicit_floor)   # further back wins -- widen, never narrow


def run_triage(session: Session, hours_back: Optional[int] = None,
               skip_claude: Optional[bool] = None) -> Run:
    """
    Execute one full triage run and persist results. Returns the Run row.

    Mirrors the old triage.py main() pipeline step-for-step.
    """
    from . import legacy  # lazy: needs google-api / bs4 only when actually running

    # Captured before the settings-default fallback below so _coverage_floor
    # can tell "caller explicitly asked for N hours" (a backfill override)
    # apart from "nobody said anything, use the steady-state default."
    explicit_hours_back = hours_back
    hours_back = settings.DEFAULT_HOURS_BACK if hours_back is None else hours_back
    skip_claude = settings.SKIP_CLAUDE_EVAL if skip_claude is None else skip_claude

    run = Run(started_at=datetime.utcnow(), hours_back=hours_back, run_type="triage", status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        # Captured before the query runs, not after: this run's coverage is
        # "everything as of this instant," so a message that arrives mid-run
        # is simply left for the next run to pick up rather than risking a
        # gap if we stamped covered_through after processing finished.
        query_as_of = datetime.utcnow()
        since = _coverage_floor(session, run.id, query_as_of, explicit_hours_back)

        service = legacy.get_gmail_service()
        query = legacy.build_query(hours_back, since=since)
        messages = legacy.fetch_messages(service, query)
        run.n_emails = len(messages)

        all_jobs: list[dict] = []
        for msg in messages:
            try:
                all_jobs.extend(legacy.extract_jobs(msg))
            except Exception as exc:  # noqa: BLE001
                legacy.log.warning("Failed to parse message %s: %s", msg.get("id"), exc)

        _process_and_persist(session, all_jobs, run, skip_claude)
        run.status = "done"
        run.finished_at = datetime.utcnow()
        run.covered_through = query_as_of
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run = session.get(Run, run.id)
        run.status = "error"
        run.error = str(exc)
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def run_scan(session: Session, hours_back: Optional[int] = None,
             skip_claude: Optional[bool] = None) -> Run:
    """Execute one portal-scan run and persist results, reusing the same
    classify -> JD-resolve -> evaluate -> persist pipeline as the email run.
    Scanned jobs land in the same Job table tagged source="Scanner/<company>",
    so they appear in the same job list as email-sourced roles.
    """
    from . import scanner  # lazy: needs requests / bs4 only when actually running

    # Portals advertise currently-open roles, so a wide window is fine; hours_back
    # is stored on the Run for display only (scanning does not filter by posted date).
    hours_back = int(os.environ.get("SCAN_HOURS_BACK", "168")) if hours_back is None else hours_back
    skip_claude = settings.SKIP_CLAUDE_EVAL if skip_claude is None else skip_claude

    run = Run(started_at=datetime.utcnow(), hours_back=hours_back, run_type="scan", status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        all_jobs = scanner.scan_all_portals()
        run.n_emails = 0  # not applicable to portal scans

        # Scanner-specific location gate: classify_job is lenient on location for
        # some companies, so reject clearly out-of-area US roles up front. They are
        # still persisted as skipped (auditable), never silently dropped.
        location_ok, location_bad = [], []
        for job in all_jobs:
            (location_ok if scanner.scanner_location_ok(job) else location_bad).append(job)

        _process_and_persist(session, location_ok, run, skip_claude)

        for job in location_bad:
            _upsert_job(session, job, "skipped",
                        f"Location outside target area: {job.get('location')}", run.id)
        run.n_skipped += len(location_bad)

        run.status = "done"
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run = session.get(Run, run.id)
        run.status = "error"
        run.error = str(exc)
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run
