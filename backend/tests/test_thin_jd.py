"""Regression tests for the two thin-JD detection layers.

There are two, deliberately distinct, mechanisms (see the architecture
assessment that motivated this suite):

  * jd_resolver._is_thin_jd -- a length/stub-marker PRE-FILTER, run before
    Claude ever sees the text. Cheap, but blind to content that's long
    (e.g. hits the 8000-char cap) yet still not real JD prose -- a
    LinkedIn login-wall page is the textbook case.

  * legacy._claude_flagged_thin_jd -- reads Claude's own reason text AFTER
    evaluation, catching what the pre-filter misses. Added because live
    verification (real API calls against these exact fixtures) showed
    Claude doesn't reliably follow the prompt's "set is_fit=true,
    confidence=low" instruction for unavailable JD text -- it sometimes
    returns is_fit=False, confidence=high, having found a title-based
    signal to justify a rejection, while its reason text still plainly
    says the JD content was unavailable. So the gate reads the reason
    text itself rather than trusting is_fit/confidence to encode this.

All jd_text fixtures below are REAL cached JdCache rows from the app's own
database (see real_jd_text_samples.json), not synthesized examples. The
Claude reason strings are the actual text returned by a live
claude_evaluate_jobs() call against those same fixtures during this fix's
verification pass.
"""
import json
from pathlib import Path

from app.triage import legacy
from app.triage.jd_resolver import _is_thin_jd

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "real_jd_text_samples.json").read_text(encoding="utf-8")
)


def _jd(label: str) -> str:
    return FIXTURES[label]["jd_text"]


# --- jd_resolver._is_thin_jd (the pre-filter) -------------------------------

def test_genuine_short_jd_is_flagged_thin():
    # Transdev's real cached JD text: short but real prose, no stub markers.
    assert _is_thin_jd(_jd("genuine_short_transdev")) is True


def test_missing_text_is_flagged_thin():
    assert _is_thin_jd(None) is True
    assert _is_thin_jd("") is True


def test_pre_filter_does_not_catch_a_long_login_wall_page():
    # Documents the known, real gap this fix compensates for (see
    # _claude_flagged_thin_jd below) -- NOT a bug in _is_thin_jd itself.
    # The LinkedIn login-wall text hits the 8000-char truncation cap, so
    # the length check never fires and there aren't 2+ stub markers either.
    assert _is_thin_jd(_jd("linkedin_login_wall_wgu")) is False


def test_pre_filter_does_not_catch_the_wrong_listings_page():
    # Same documented gap: the Hemophilia Alliance listings-page text is
    # substantial (5208 chars) even though it's the wrong page, not the
    # specific posting.
    assert _is_thin_jd(_jd("wrong_listing_page_hemophilia")) is False


# --- legacy._claude_flagged_thin_jd (the verdict-based catch) --------------

# Real reason text from a live evaluate_job() call against the LinkedIn
# login-wall fixture above (is_fit=False, confidence=high -- Claude didn't
# follow the prompt's "is_fit=true/confidence=low" instruction, but still
# said the content was unavailable).
REAL_REASON_LOGIN_WALL = (
    "Job description content unavailable (LinkedIn login wall); cannot "
    "assess explicit experience requirements, seniority level, or "
    "job-specific blockers. Title 'Senior AI Engineer' signals seniority, "
    "but without JD text cannot confirm whether this is a hard blocker or "
    "merely a title mismatch."
)

# Real reason text from a live call against the Hemophilia Alliance fixture.
REAL_REASON_WRONG_PAGE = (
    "JD text unavailable — only navigation/job board listing headers "
    "provided, no actual job description content to evaluate years "
    "requirement, responsibilities, or qualifications."
)

# Real reason text for an ordinary experience-gap demotion, unrelated to
# JD availability -- must NOT trigger the retry.
REAL_REASON_ORDINARY_STRETCH = (
    "Role explicitly requires minimum 3 years of professional experience "
    "in data extraction, ingestion, and modeling; Aidan has ~1 year of "
    "internship experience. Additionally, active TS/SCI + CI Poly security "
    "clearance is a hard requirement; Aidan is unlikely to have this as a "
    "recent graduate."
)

# Real reason text for an ordinary hard-blocker demotion (location), also
# unrelated to JD availability -- must NOT trigger the retry.
REAL_REASON_ORDINARY_HARD_BLOCKER = (
    "JD states 'No home office possible' — this contradicts the remote "
    "designation and indicates a requirement for physical presence at a "
    "non-Triangle location (Raleigh office-based work), which conflicts "
    "with Aidan's inability to relocate and location constraints."
)

# Real reason text for a genuine, positive fit highlight -- must NOT trigger.
REAL_REASON_GENUINE_FIT = (
    "UNC Health system in Research Triangle; healthcare data analytics "
    "focus; direct match to Aidan's BI/dashboard experience (NetApp, NCDIT "
    "internships) and stakeholder communication skills."
)


def test_flags_the_real_login_wall_verdict():
    assert legacy._claude_flagged_thin_jd(REAL_REASON_LOGIN_WALL) is True


def test_flags_the_real_wrong_page_verdict():
    assert legacy._claude_flagged_thin_jd(REAL_REASON_WRONG_PAGE) is True


def test_does_not_flag_an_ordinary_stretch_demotion():
    assert legacy._claude_flagged_thin_jd(REAL_REASON_ORDINARY_STRETCH) is False


def test_does_not_flag_an_ordinary_hard_blocker_demotion():
    assert legacy._claude_flagged_thin_jd(REAL_REASON_ORDINARY_HARD_BLOCKER) is False


def test_does_not_flag_a_genuine_fit_highlight():
    assert legacy._claude_flagged_thin_jd(REAL_REASON_GENUINE_FIT) is False


def test_does_not_flag_empty_or_missing_reason():
    assert legacy._claude_flagged_thin_jd("") is False
    assert legacy._claude_flagged_thin_jd(None) is False
