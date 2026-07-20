"""
jd_resolver.py — fetch real job-description text for roles whose source hides
the JD (NCWorks, Indeed), using the Tavily Search API with a SerpApi
``google_jobs`` fallback.

Why this exists: those sources only give a one-line email snippet, so Claude
can't judge the experience requirement and parks the role in Pursue with a ⚠.
Tavily searches the open web for the real employer posting and returns its
text. When Tavily comes back thin or stub-like (e.g. an aggregator page that's
mostly structured metadata — the Teal page that mislabeled the Cadence role as
Ph.D.-required), we fall through to SerpApi's google_jobs engine, which returns
the actual posting body in ``jobs_results[].description``.

Cost control:
  * Results are CACHED in SQLite (JdCache), keyed by the job's stable id, so a
    role is only ever looked up once — re-runs cost nothing.
  * Tavily (free tier ~1,000/mo) is tried first; SerpApi (paid) is only called
    on thin results, and is itself capped per run (SERPAPI_MAX_LOOKUPS).
  * Per-run budgets cap how many *new* lookups happen, as a hard safety net.
Failures degrade gracefully: any error returns no text, and the role simply
falls back to the old snippet-only behaviour. A run never crashes on lookups.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import requests
from sqlmodel import Session

from ..models import JdCache

TAVILY_ENDPOINT = "https://api.tavily.com/search"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# Sources whose JD pages can't be fetched directly — these get a Tavily lookup.
JD_LOOKUP_SOURCES = {"NCWorks", "Indeed"}

# Keep JD text generous enough to include the requirements section, which on
# many postings sits well below the job-summary preamble. 8000 chars ≈ 2k tokens.
_MAX_JD_CHARS = 8000


def _tavily_search(query: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """One Tavily search. Returns (best page text, its url), or (None, None) on failure."""
    try:
        resp = requests.post(
            TAVILY_ENDPOINT,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "include_raw_content": True,
                "max_results": 3,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None, None
        results = resp.json().get("results") or []
        # Prefer the fullest page text; return the URL it came from.
        for r in results:
            raw = (r.get("raw_content") or "").strip()
            if raw:
                return raw[:_MAX_JD_CHARS], (r.get("url") or None)
        # Fall back to result snippets; attribute to the top result's URL.
        snippets = " ".join((r.get("content") or "").strip() for r in results).strip()
        url = results[0].get("url") if results else None
        return (snippets[:_MAX_JD_CHARS] or None), url
    except Exception:
        return None, None


def _query_for(job: dict) -> str:
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    return f"{title} {company} job posting requirements".strip()


def _serpapi_jobs(job: dict, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """One SerpApi ``google_jobs`` lookup for a specific role. Returns the full
    structured description and an apply URL, or (None, None) on failure.

    Unlike a generic web search, this hits Google for Jobs directly and returns
    the actual posting body in ``jobs_results[].description`` — which is what the
    evaluator needs and what Tavily sometimes can't get (e.g. aggregator stubs).
    """
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    try:
        resp = requests.get(
            SERPAPI_ENDPOINT,
            params={
                "engine": "google_jobs",
                "q": f"{title} {company}".strip(),
                "api_key": api_key,
                "hl": "en",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return None, None
        results = resp.json().get("jobs_results") or []
        if not results:
            return None, None
        # Prefer a result whose company matches the role; else take the top hit.
        best = results[0]
        if company:
            for r in results:
                if company.lower() in (r.get("company_name") or "").lower():
                    best = r
                    break
        desc = (best.get("description") or "").strip()
        url = None
        for opt in (best.get("apply_options") or []):
            if opt.get("link"):
                url = opt["link"]
                break
        return (desc[:_MAX_JD_CHARS] or None), url
    except Exception:
        return None, None


# Markers of an aggregator "stub" page — structured-field chrome with little
# actual requirements prose (e.g. the Teal page that mislabeled the Cadence role
# as requiring a Ph.D.). These trigger the SerpApi fallback.
_STUB_MARKERS = (
    "## Job Search Resources", "## Similar", "Job Search Resources",
    "Education Level", "Career Level",
)


def _is_thin_jd(text: Optional[str]) -> bool:
    """True if the JD text is missing, too short, or looks like an aggregator
    stub — i.e. not enough real prose for the evaluator to judge fit."""
    if not text:
        return True
    if len(text) < 1200:
        return True
    hits = sum(1 for m in _STUB_MARKERS if m in text)
    return hits >= 2 and len(text) < 3000


def retry_thin_verdict(session: Optional[Session], job: dict, budget_remaining: int) -> tuple[Optional[str], bool]:
    """SerpApi retry triggered by Claude's own evaluation verdict, not the
    pre-filter above. Called from evaluate_job() (legacy.py) after Claude has
    judged the JD text it was given as thin/unavailable ("is_fit=true,
    confidence=low" — the case the prompt asks it to return for
    navigation-only/unavailable content). This catches what the length-based
    ``_is_thin_jd`` pre-filter misses: e.g. a Tavily result that landed on a
    LinkedIn login-wall page, which is long (often hits the 8000-char cap)
    but is entirely cookie/sign-in chrome, not real JD prose.

    Returns (new_text_or_None, attempted) — ``attempted`` is True whenever a
    SerpApi call was actually made (so the caller can track its own budget),
    False when skipped for lack of a key or budget. ``new_text`` is None both
    when the call failed AND when it succeeded but the result was itself thin
    (e.g. a genuine login-wall posting with no better source anywhere) — the
    caller should leave the job's existing jd_text/verdict alone in that case.
    """
    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not serpapi_key or budget_remaining <= 0:
        return None, False

    text, url = _serpapi_jobs(job, serpapi_key)
    if not text or _is_thin_jd(text):
        return None, True

    if url:
        job["jd_url"] = url

    if session is not None:
        from .legacy import _job_id
        sid = _job_id(job)
        capped_text = text[:_MAX_JD_CHARS]
        cached = session.get(JdCache, sid)
        if cached is not None:
            cached.jd_text = capped_text
            cached.jd_url = url or cached.jd_url
            cached.found = True
            session.add(cached)
        else:
            session.add(JdCache(
                stable_id=sid, jd_text=capped_text, jd_url=(url or ""),
                found=True, fetched_at=datetime.utcnow(),
            ))
        session.commit()

    return text[:_MAX_JD_CHARS], True


def resolve_for_jobs(session: Session, jobs: list, max_lookups: int) -> int:
    """
    Populate ``job['jd_text']`` for blocked-source jobs in ``jobs`` (a list of
    (job, reason) tuples). Cache first, then Tavily, then a SerpApi google_jobs
    fallback when Tavily's result is thin or stub-like. Returns the number of new
    Tavily lookups performed (the SerpApi fallback count is logged separately).

    Works with TAVILY_API_KEY, SERPAPI_API_KEY, or both. No-ops (returns 0) if
    neither is set — the system then behaves exactly as before (snippet-only).
    """
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not tavily_key and not serpapi_key:
        return 0

    from .legacy import _job_id, log as _log  # lazy: legacy pulls in google libs

    serp_budget = int(os.environ.get("SERPAPI_MAX_LOOKUPS", "30"))
    used = 0        # Tavily lookups (what the engine logs)
    serp_used = 0   # SerpApi google_jobs fallbacks
    for job, _reason in jobs:
        if job.get("source") not in JD_LOOKUP_SOURCES:
            continue

        sid = _job_id(job)

        # 1) cache hit — free, no network
        cached = session.get(JdCache, sid)
        if cached is not None:
            if cached.jd_text:
                job["jd_text"] = cached.jd_text
            if cached.jd_url:
                job["jd_url"] = cached.jd_url
            continue

        # 2) cache miss — try Tavily (free) first, then SerpApi if needed.
        can_tavily = bool(tavily_key) and used < max_lookups
        can_serp = bool(serpapi_key) and serp_used < serp_budget
        if not can_tavily and not can_serp:
            continue  # out of budget this run — leave un-cached for a later run

        text, url = (None, None)
        if can_tavily:
            text, url = _tavily_search(_query_for(job), tavily_key)
            used += 1

        # SerpApi google_jobs fallback (or primary, if no Tavily key) whenever the
        # Tavily result is thin/stub-like. Accept it only if it's actually better.
        if can_serp and _is_thin_jd(text):
            s_text, s_url = _serpapi_jobs(job, serpapi_key)
            serp_used += 1
            if s_text and not _is_thin_jd(s_text):
                text, url = s_text, (s_url or url)
            elif s_text and text is None:
                text, url = s_text, (s_url or url)

        # Cache the outcome either way (negative cache avoids re-querying misses).
        session.add(JdCache(
            stable_id=sid,
            jd_text=(text or "")[:_MAX_JD_CHARS],
            jd_url=(url or ""),
            found=bool(text),
            fetched_at=datetime.utcnow(),
        ))
        session.commit()

        if text:
            job["jd_text"] = text
        if url:
            job["jd_url"] = url

    if serp_used:
        _log.info("SerpAPI google_jobs fallbacks this run: %d", serp_used)
    return used
