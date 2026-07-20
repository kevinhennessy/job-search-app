"""Regression test for classify_job's empty-employer artifact guard (see
CLAUDE.md's "Empty-employer artifact guard" gotcha).

Root cause: parse_getro_jobs (the Getro / NC-Biotech parser) builds a
'linkedin.com/jobs/search?keywords=<title>%20Unknown' fallback URL when it
can't parse a real employer out of the email (see legacy.py's
parse_getro_jobs, company defaults to the literal string "Unknown" and the
fallback search_url is built directly from it -- this fixture mirrors that
exact real construction, not an invented shape). The guard in classify_job
is the safety net that keeps that non-posting from ever reaching evaluation.
"""
from app.triage import legacy


def _base_job(**overrides):
    job = {
        "title": "Data Scientist",
        "company": "Unknown",
        "location": "",
        "salary": None,
        "source": "NC Biotech Center",
        "url": "https://www.linkedin.com/jobs/search/?keywords=Data%20Scientist%20Unknown",
        "snippet": "",
        "date": "Sat, 18 Jul 2026 12:00:00 -0400",
    }
    job.update(overrides)
    return job


def test_unknown_company_with_constructed_search_link_is_skipped():
    category, reason = legacy.classify_job(_base_job())
    assert category == "skipped"
    assert "search link" in (reason or "").lower()


def test_empty_string_company_with_search_link_is_also_skipped():
    category, reason = legacy.classify_job(_base_job(company=""))
    assert category == "skipped"


def test_unknown_company_with_a_real_direct_link_is_not_caught_by_this_guard():
    # Same broken company, but a real (non-search) URL -- the guard is
    # specifically about the constructed search-link artifact, not about
    # "Unknown" company on its own, so this should fall through to whatever
    # classify_job's other rules decide (not necessarily "pursue"), just NOT
    # this specific "no employer" skip reason.
    category, reason = legacy.classify_job(
        _base_job(url="https://realcompany.com/careers/data-scientist-12345")
    )
    assert reason != "No employer — constructed search link, not a real posting"


def test_real_employer_with_a_jobs_search_url_is_not_caught():
    # A genuine posting whose URL happens to contain /jobs/search should
    # never be caught -- the guard requires BOTH signals (no real employer
    # AND a constructed search link), exactly like _is_digest_header's
    # both-signals design.
    category, reason = legacy.classify_job(
        _base_job(
            company="Duke University",
            url="https://www.linkedin.com/jobs/search/?keywords=Data%20Scientist%20Duke%20University",
        )
    )
    assert reason != "No employer — constructed search link, not a real posting"
