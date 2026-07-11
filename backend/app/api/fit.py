"""
Job Fit Evaluator — Claude evaluation of a job against the candidate profile.

Two entry points share the same candidate-context, prompt-schema, and Claude-
call logic:
  - POST /api/fit-evaluate       — a full pasted job posting (manual tab)
  - jobs.py's per-job endpoint   — structured fields (title/company/snippet)
    from an already-ingested Job row, evaluated automatically and cached

Same candidate-context pattern as the batch evaluator in triage/legacy.py:
candidate_profile.md in config.PROJECT_DIR is the single source of truth
(see CLAUDE.md rule #1) — never hardcode a candidate blurb here. Same
urllib-based Claude call pattern too (no SDK dependency). Both entry points
use Sonnet — this is on-demand, per-item evaluation, not the batch
evaluator's high-volume Haiku pass.
"""

import json
import logging
import os
import ssl
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException

from ..triage import config
from ..schemas import FitEvaluateRequest, FitEvaluateResult

router = APIRouter(prefix="/api", tags=["fit"])
log = logging.getLogger("fit")

MODEL = "claude-sonnet-5"

RESPONSE_SCHEMA = """Return ONLY valid JSON (no markdown, no code fences, no other text) with exactly this structure:
{
  "overall_score": <integer 0-100>,
  "title_fit": <integer 0-100, how well the role's title/level matches his target titles>,
  "experience_bar": <integer 0-100, how clear he is of the role's experience requirements — 100 means no barrier, 0 means a hard blocker>,
  "niche_match": <integer 0-100, alignment with his clinical ML / calibration / multimodal EHR niche>,
  "verdict": "apply" | "caution" | "skip",
  "verdict_reason": "<one sentence>",
  "matches": ["<short strength-match phrase>", ...],
  "gaps": ["<short gap phrase>", ...],
  "flags": ["<short strategy-flag phrase>", ...],
  "summary": "<3-5 sentences covering fit, main risks, and recommended angle if applying>"
}"""

TASK_INSTRUCTIONS = (
    "Assess how good a fit this posting is for the candidate, using the profile above "
    "(experience, skills, honest framing of what he can defend) and the Search Strategy "
    "& Title Targeting section (title fit, niche value, target employers, strategic "
    "mismatches). Do not credit him with skills or experience the profile says he doesn't have."
)


def load_candidate_profile() -> str:
    profile_path = os.path.join(config.PROJECT_DIR, "candidate_profile.md")
    try:
        with open(profile_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        log.warning("candidate_profile.md not found at %s", profile_path)
        return "No candidate profile available."


def build_prompt_from_posting(posting: str) -> str:
    candidate_context = load_candidate_profile()
    return f"""You are evaluating a job posting against a specific candidate's profile and job-search strategy.

CANDIDATE PROFILE AND STRATEGY:
{candidate_context}

JOB POSTING (as pasted by the candidate):
{posting}

TASK:
{TASK_INSTRUCTIONS}

{RESPONSE_SCHEMA}"""


def build_prompt_from_job(title: str, company: str, location: str, snippet: str, url: str) -> str:
    """Same schema/task as build_prompt_from_posting, but for an already-ingested
    Job row — only structured fields are available (no full posting text), since
    Job doesn't persist fetched JD text."""
    candidate_context = load_candidate_profile()
    return f"""You are evaluating a job listing against a specific candidate's profile and job-search strategy.

CANDIDATE PROFILE AND STRATEGY:
{candidate_context}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location or 'Not specified'}
Description / snippet: {snippet or 'Not available — assess based on title and company only'}
URL: {url or 'Not available'}

TASK:
{TASK_INSTRUCTIONS} Note this listing may only have a short snippet rather than a full
posting — if requirements aren't stated, judge experience_bar and gaps from what's
actually known rather than assuming a blocker, and let confidence show through
lower scores/flags rather than a false-precise verdict.

{RESPONSE_SCHEMA}"""


def call_claude_fit(prompt: str) -> FitEvaluateResult:
    """Shared Claude call + response parsing for both fit-evaluation entry points.
    Raises HTTPException on any failure (missing key, API error, bad JSON)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not set on the server")

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
        method="POST",
    )

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            response_data = json.loads(raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.error("fit evaluate: Claude API error %s: %s", e.code, body_text[:300])
        raise HTTPException(status_code=502, detail=f"Claude API error: {e.code}")
    except Exception as e:
        log.error("fit evaluate: request failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to reach Claude API")

    content = response_data.get("content", [])
    text = next((c["text"] for c in content if c.get("type") == "text"), "")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]

    try:
        result = json.loads(text.strip())
        return FitEvaluateResult(**result)
    except Exception as e:
        log.error("fit evaluate: failed to parse Claude response: %s — raw: %s", e, text[:300])
        raise HTTPException(status_code=502, detail="Could not parse Claude's evaluation response")


@router.post("/fit-evaluate", response_model=FitEvaluateResult)
def fit_evaluate(body: FitEvaluateRequest) -> FitEvaluateResult:
    posting = body.posting.strip()
    if not posting:
        raise HTTPException(status_code=400, detail="posting text is required")
    return call_claude_fit(build_prompt_from_posting(posting))
