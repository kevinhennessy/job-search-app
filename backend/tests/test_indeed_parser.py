"""Regression test for the Indeed digest parser (_parse_indeed_jobs_h2) and
the digest-header phantom-entry filter it uses (_is_digest_header).

Fixture: the real Indeed alert email that triggered this week's "Alta
Planning + Design silently missing" investigation (see CLAUDE.md's
"Run-coverage gaps" gotcha) and the follow-up phantom-header fix (commit
3e52d05). Captured by fetching the real message by id and saving its raw
HTML — not synthesized.

The email contains three <h2>s: Indeed's own alert-summary headline ("2 new
data analyst entry level jobs jobs in Durham, NC" / company text starting
"These jobs match your saved job alert") plus the two real listings, Alta
Planning + Design and Transdev. A regression here means either the phantom
header started slipping back through as a fake "job", or a real listing
started silently dropping again — both are exactly the failure modes hit
this week.
"""
from pathlib import Path

from app.triage import legacy

FIXTURE = Path(__file__).parent / "fixtures" / "indeed_alta_transdev_digest.html"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_extracts_exactly_the_two_real_listings():
    jobs = legacy._parse_indeed_jobs_h2(_load_fixture(), "Sat, 18 Jul 2026 12:00:00 -0400")
    titles = [j["title"] for j in jobs]
    assert len(jobs) == 2, f"expected 2 real listings, got {len(jobs)}: {titles}"
    assert "Civic Data Analyst (Sustainable Mobility)" in titles
    assert "Data Analyst/Dispatcher" in titles


def test_alta_and_transdev_have_real_companies_not_the_digest_artifact():
    jobs = legacy._parse_indeed_jobs_h2(_load_fixture(), "Sat, 18 Jul 2026 12:00:00 -0400")
    by_title = {j["title"]: j for j in jobs}
    assert "Alta Planning" in by_title["Civic Data Analyst (Sustainable Mobility)"]["company"]
    assert "Transdev" in by_title["Data Analyst/Dispatcher"]["company"]


def test_digest_summary_headline_is_excluded():
    jobs = legacy._parse_indeed_jobs_h2(_load_fixture(), "Sat, 18 Jul 2026 12:00:00 -0400")
    titles = [j["title"] for j in jobs]
    assert not any("new" in t.lower() and "jobs in" in t.lower() for t in titles), (
        f"a digest-summary headline slipped through as a fake job: {titles}"
    )


def test_is_digest_header_matches_the_real_artifact_pair():
    # The exact title/company text Indeed's alert-summary <h2> produces in
    # this real email (see _is_digest_header's docstring / commit 3e52d05).
    assert legacy._is_digest_header(
        "2 new data analyst entry level jobs jobs in Durham, NC",
        "These jobs match your saved job alert ¹",
    ) is True


def test_is_digest_header_does_not_flag_the_real_listings():
    assert legacy._is_digest_header(
        "Civic Data Analyst (Sustainable Mobility)", "Alta Planning + Design 3.7 Durham, NC"
    ) is False
    assert legacy._is_digest_header(
        "Data Analyst/Dispatcher", "Transdev 3.1 Raleigh, NC"
    ) is False


def test_is_digest_header_requires_both_signals_together():
    # Title alone matching the digest pattern isn't enough -- a real posting
    # could coincidentally be titled this way. Only the exact company-side
    # boilerplate confirms it's the artifact, not a real listing.
    assert legacy._is_digest_header(
        "2 new data analyst entry level jobs jobs in Durham, NC", "Some Real Employer"
    ) is False
