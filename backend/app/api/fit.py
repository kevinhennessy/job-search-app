"""
Job Fit Evaluator API — on-demand Claude evaluation of a pasted job posting
against the candidate profile, independent of the triage/scan pipeline.

Same candidate-context pattern as the batch evaluator in triage/legacy.py:
candidate_profile.md in config.PROJECT_DIR is the single source of truth
(see CLAUDE.md rule #1) — never hardcode a candidate blurb here. Same
urllib-based Claude call pattern too (no SDK dependency), but this is a
single on-demand call per user action rather than a per-run batch, so it
uses Sonnet rather than the batch evaluator's Haiku.
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


def _load_candidate_profile() -> str:
    profile_path = os.path.join(config.PROJECT_DIR, "candidate_profile.md")
    try:
        with open(profile_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        log.warning("candidate_profile.md not found at %s", profile_path)
        return "No candidate profile available."


def _build_prompt(posting: str) -> str:
    candidate_context = _load_candidate_profile()
    return f"""You are evaluating a job posting against a specific candidate's profile and job-search strategy.

CANDIDATE PROFILE AND STRATEGY:
{candidate_context}

JOB POSTING (as pasted by the candidate):
{posting}

TASK:
Assess how good a fit this posting is for the candidate, using the profile above (experience, skills, honest framing of what he can defend) and the Search Strategy & Title Targeting section (title fit, niche value, target employers, strategic mismatches). Do not credit him with skills or experience the profile says he doesn't have.

Return ONLY valid JSON (no markdown, no code fences, no other text) with exactly this structure:
{{
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
}}"""


@router.post("/fit-evaluate", response_model=FitEvaluateResult)
def fit_evaluate(body: FitEvaluateRequest) -> FitEvaluateResult:
    posting = body.posting.strip()
    if not posting:
        raise HTTPException(status_code=400, detail="posting text is required")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not set on the server")

    prompt = _build_prompt(posting)
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
        log.error("fit_evaluate: Claude API error %s: %s", e.code, body_text[:300])
        raise HTTPException(status_code=502, detail=f"Claude API error: {e.code}")
    except Exception as e:
        log.error("fit_evaluate: request failed: %s", e)
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
        log.error("fit_evaluate: failed to parse Claude response: %s — raw: %s", e, text[:300])
        raise HTTPException(status_code=502, detail="Could not parse Claude's evaluation response")
