#!/usr/bin/env python3
"""
triage.py — Daily job-search email triage tool.

Usage:
    python triage.py              # triage the last 24 hours
    python triage.py --hours 48   # triage the last 48 hours (catch-up)
    python triage.py --dry-run    # print results without writing the digest file
    python triage.py --debug      # dump every email the API finds before filtering
"""

import argparse
import json
import base64
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config

# Module-level debug flag — set to True by --debug argument in main()
DEBUG = False

# ---------------------------------------------------------------------------
# Logging — errors go to file; INFO goes to stdout
# ---------------------------------------------------------------------------

Path(config.LOGS_DIR).mkdir(parents=True, exist_ok=True)
Path(config.DIGESTS_DIR).mkdir(parents=True, exist_ok=True)

log = logging.getLogger("triage")
log.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(
    os.path.join(config.LOGS_DIR, "triage-errors.log"), encoding="utf-8"
)
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_console_handler)


# ---------------------------------------------------------------------------
# Gmail authentication
# ---------------------------------------------------------------------------

def _retry(fn, retries: int = 3, delay: float = 10.0, label: str = ""):
    """Call fn() up to `retries` times, waiting `delay` seconds between attempts.
    Retries on SSL, network, and transient HTTP errors."""
    import time
    import ssl
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            err = str(exc)
            # Retry on transient network/SSL errors
            transient = any(k in err for k in [
                "SSL", "EOF", "ConnectionReset", "RemoteDisconnected",
                "timeout", "timed out", "TransportError", "503", "500",
            ])
            if transient and attempt < retries:
                log.warning("Transient error on attempt %d/%d for %s: %s — retrying in %ds",
                            attempt, retries, label, exc, int(delay))
                time.sleep(delay)
                delay *= 2  # exponential backoff
            else:
                raise


def get_gmail_service():
    """Authenticate with Gmail and return a service object."""
    creds = None

    if os.path.exists(config.TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(config.TOKEN_FILE, config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            _retry(lambda: creds.refresh(Request()), label="token refresh")
        else:
            if not os.path.exists(config.CREDENTIALS_FILE):
                log.error(
                    "credentials.json not found at %s\n"
                    "Download your OAuth 2.0 client secret from Google Cloud Console "
                    "and save it at that path.",
                    config.CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                config.CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        log.info("Token saved to %s", config.TOKEN_FILE)

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Build Gmail search query
# ---------------------------------------------------------------------------

# Per-run budget for the generic LLM extractor (unknown-sender emails). Reset at
# the start of each run inside build_query(); checked/incremented by
# llm_extract_jobs(). Module-level so it persists across per-message extract_jobs calls.
_generic_extract_calls = 0


def build_query(hours_back: int, since: "datetime | None" = None) -> str:
    """
    Build a Gmail search query covering all monitored senders.

    Default mode: within the last `hours_back` hours, via newer_than:Nd
    (relative, user-timezone-aware) rather than after:YYYY/MM/DD, to avoid
    silent misses at day boundaries.

    If `since` is given (the previous successful run's covered_through —
    see engine.run_triage), it's used as the query floor instead, via
    after:<unix-epoch-seconds> — Gmail's after:/before: accept a timestamp
    at second precision, not just YYYY/MM/DD, so this doesn't reintroduce
    the day-boundary problem the newer_than fallback above avoids. This
    closes the run-coverage gap: two runs spaced further apart than
    hours_back no longer lose whatever arrived in between (see CLAUDE.md).
    """
    # Round up to whole days; minimum 1.
    days_back = max(1, -(-hours_back // 24))   # ceiling division

    # build_query runs once per run, so reset the generic-extractor budget here.
    global _generic_extract_calls
    _generic_extract_calls = 0

    sender_clauses = []
    seen = set()
    for sender in config.SENDERS:
        if sender["match"] == "exact":
            clause = f"from:{sender['address']}"
        elif sender["match"] in ("domain_contains", "name_contains"):
            # Quote multi-word substrings — an unquoted space breaks the
            # Gmail query parser and silently zeroes out all results.
            sub = sender["substring"]
            clause = f'from:"{sub}"' if " " in sub else f"from:{sub}"
        else:
            continue
        if clause not in seen:
            sender_clauses.append(clause)
            seen.add(clause)

    sender_part = " OR ".join(sender_clauses)

    # Keyword discovery: also fetch job-alert emails from senders NOT on the
    # allowlist, so the generic LLM extractor can read them. Matches job-intent
    # phrases in the subject line; the extractor returns nothing for non-job mail.
    intent_kw = getattr(config, "JOB_INTENT_KEYWORDS", [])
    intent_clauses = " OR ".join(f'"{kw}"' for kw in intent_kw)

    time_clause = f"after:{int(since.timestamp())}" if since is not None else f"newer_than:{days_back}d"

    if sender_part and intent_clauses:
        query = f"(({sender_part}) OR subject:({intent_clauses})) {time_clause}"
    elif intent_clauses:
        query = f"(subject:({intent_clauses})) {time_clause}"
    else:
        query = f"({sender_part}) {time_clause}"
    log.info("Gmail query: %s", query)
    log.info("Tip: paste that query into the Gmail search box to verify it matches your emails.")
    return query


# ---------------------------------------------------------------------------
# Message fetching
# ---------------------------------------------------------------------------

def fetch_messages(service, query: str) -> list[dict]:
    """Return full message dicts matching `query`."""
    messages = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            result = _retry(
                lambda: service.users().messages().list(**kwargs).execute(),
                label="messages.list"
            )
        except HttpError as exc:
            log.error("Gmail API error listing messages: %s", exc)
            break
        except Exception as exc:
            log.error("Network error listing messages: %s", exc)
            break

        ids = result.get("messages", [])
        if DEBUG and not messages:   # log on first page only to avoid spam
            log.info("[debug] API returned %d message id(s) on first page", len(ids))

        for item in ids:
            try:
                msg = _retry(
                    lambda: service.users().messages().get(
                        userId="me", id=item["id"], format="full"
                    ).execute(),
                    label=f"messages.get({item['id']})"
                )
                messages.append(msg)
            except HttpError as exc:
                log.warning("Could not fetch message %s: %s", item["id"], exc)
            except Exception as exc:
                log.warning("Network error fetching message %s: %s", item["id"], exc)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if DEBUG:
        _debug_dump_messages(messages)

    return messages


def _debug_dump_messages(messages: list[dict]) -> None:
    """Print a raw inventory of every fetched message before any filtering."""
    sep = "-" * 70
    log.info("")
    log.info("=" * 70)
    log.info("  DEBUG — RAW EMAILS RETURNED BY GMAIL API (%d total)", len(messages))
    log.info("=" * 70)
    if not messages:
        log.info("  *** Zero messages returned. ***")
        log.info("  Possible causes:")
        log.info("    1. The Gmail account that completed OAuth is not the one")
        log.info("       receiving job alerts. Check token.json or re-run to re-auth.")
        log.info("    2. The emails exist but are outside the newer_than:Nd window.")
        log.info("       Try:  python triage.py --debug --hours 168  (7 days)")
        log.info("    3. The sender addresses in config.py don't match the actual")
        log.info("       From headers. Paste the query into Gmail to verify.")
    for i, msg in enumerate(messages, 1):
        headers = msg.get("payload", {}).get("headers", [])
        from_h  = get_header(headers, "From")
        subj    = get_header(headers, "Subject")
        date_h  = get_header(headers, "Date")
        source, _ = identify_source(from_h)
        log.info("")
        log.info("%s", sep)
        log.info("  Email %d/%d", i, len(messages))
        log.info("  From   : %s", from_h)
        log.info("  Subject: %s", subj)
        log.info("  Date   : %s", date_h)
        log.info("  Source : %s", source or "*** DID NOT MATCH ANY CONFIGURED SENDER ***")
    log.info("%s", sep)
    log.info("")


# ---------------------------------------------------------------------------
# Email body decoding
# ---------------------------------------------------------------------------

def _decode_part(part: dict) -> tuple[str, str]:
    """Recursively extract (text_plain, text_html) from a message part."""
    mime = part.get("mimeType", "")
    body_data = part.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace"), ""
    if mime == "text/html" and body_data:
        return "", base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    plain, html = "", ""
    for sub in part.get("parts", []):
        p, h = _decode_part(sub)
        plain += p
        html += h
    return plain, html


def decode_payload(payload: dict) -> tuple[str, str]:
    """Return (plain_text, html_text) from a Gmail message payload."""
    return _decode_part(payload)


def strip_html(html_text: str) -> str:
    """Convert HTML to plain text, collapsing whitespace."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

def get_header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h["name"].lower() == name_lower:
            return h["value"]
    return ""


# ---------------------------------------------------------------------------
# Sender identification
# ---------------------------------------------------------------------------

def identify_source(from_header: str) -> tuple[str | None, dict | None]:
    """
    Return (source_label, sender_config) if the From header matches a
    configured sender, else (None, None).
    """
    _, addr = parseaddr(from_header)
    addr_lower = addr.lower()
    name_lower = from_header.lower()

    for sender in config.SENDERS:
        match = sender["match"]
        if match == "exact":
            if addr_lower == sender["address"].lower():
                return sender["source"], sender
        elif match == "domain_contains":
            if sender["substring"].lower() in addr_lower:
                return sender["source"], sender
        elif match == "name_contains":
            if sender["substring"].lower() in name_lower:
                return sender["source"], sender

    return None, None


def _get_sender_cfg(source_label: str) -> dict | None:
    """Return the first SENDERS entry whose source matches source_label."""
    for sender in config.SENDERS:
        if sender.get("source") == source_label:
            return sender
    return None


# ---------------------------------------------------------------------------
# Salary extraction
# ---------------------------------------------------------------------------

_SALARY_RE = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?(?:\s?[kK])?"         # $80,000 or $80k or $80.5k
    r"(?:\s?[-\u2013]\s?\$?\s?[\d,]+(?:\.\d+)?(?:\s?[kK])?)?"   # optional range
    r"(?:\s?(?:a\s+year|/yr|per\s+year|annually|/year|/hour|/hr|an\s+hour|/mo|per\s+month))?",
    re.IGNORECASE,
)


def extract_salary(text: str) -> str | None:
    """Return the first salary string found in text, or None."""
    m = _SALARY_RE.search(text)
    if m:
        val = m.group(0).strip().rstrip("-–")
        if val:
            return val
    return None


def salary_to_annual(salary_str: str) -> int | None:
    """
    Convert a salary string to an approximate annual integer.
    Returns None if conversion is not possible.
    """
    if not salary_str:
        return None
    text = salary_str.lower()
    nums = re.findall(r"[\d,]+(?:\.\d+)?", salary_str)
    if not nums:
        return None
    amount = float(nums[0].replace(",", ""))
    if "k" in text:
        amount *= 1000
    if any(x in text for x in ["/hour", "/hr", "an hour", "per hour"]):
        amount *= 2080   # 40h × 52w
    elif any(x in text for x in ["/mo", "per month"]):
        amount *= 12
    return int(amount)


def _annual_salary_max(salary_str: str) -> int | None:
    """
    Return the UPPER bound of a salary range as an annual integer.
    For "$60K-$90K" returns 90000; for "$95K" returns 95000.
    Falls back to salary_to_annual (single number) when no range is present.
    Using the upper bound avoids penalising ranges like "$70K-$100K" where
    the ceiling meets or exceeds the target.
    """
    if not salary_str:
        return None
    text = salary_str.lower()
    nums = re.findall(r"[\d,]+(?:\.\d+)?", salary_str)
    if not nums:
        return None
    amount = float(nums[-1].replace(",", ""))   # last number = upper bound
    if "k" in text:
        amount *= 1000
    if any(x in text for x in ["/hour", "/hr", "an hour", "per hour"]):
        amount *= 2080
    elif any(x in text for x in ["/mo", "per month"]):
        amount *= 12
    return int(amount)


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

def extract_location(text: str) -> str | None:
    """Return the first matching target location found in text."""
    text_lower = text.lower()
    for loc in config.INCLUDE_LOCATIONS:
        if loc.lower() in text_lower:
            return loc.title()
    return None


# ---------------------------------------------------------------------------
# Company extraction (best-effort heuristic)
# ---------------------------------------------------------------------------

_COMPANY_NOISE = re.compile(
    r"\b(apply|view|job|new|alert|opportunity|opportunities|opening|position|role)\b",
    re.IGNORECASE,
)


# Aggregator senders — the email is FROM them but the job is AT a third-party company,
# so company must be extracted from the content.  All other senders are direct company
# senders where the source label already is the company name.
_AGGREGATOR_SOURCES = {"Indeed", "NCWorks"}


def extract_company(subject: str, body_text: str, source: str) -> str:
    """Try to extract a company name from subject or early body text."""
    # Direct-company senders: the source label IS the company name.
    # Returning it directly avoids subject-regex bleed (e.g. "Siemens Healthineers
    # you may be interested in" being captured as the company).
    if source not in _AGGREGATOR_SOURCES:
        return source

    # Aggregator emails (Indeed, NCWorks): parse company from content.
    # Look for "at <Company>" pattern in subject
    m = re.search(r"\bat\s+([A-Z][A-Za-z0-9 &,.\-]+?)(?:\s+in\s|\s*[|\-,]|\s*$)", subject)
    if m:
        candidate = m.group(1).strip()
        if 2 < len(candidate) < 60:
            return candidate

    # Look in first 300 chars of body
    snippet = body_text[:300]
    m = re.search(r"\bat\s+([A-Z][A-Za-z0-9 &,.\-]+?)(?:\s+in\s|\s*[|\-,]|\.|\n)", snippet)
    if m:
        candidate = m.group(1).strip()
        if 2 < len(candidate) < 60:
            return candidate

    return "Unknown"


# ---------------------------------------------------------------------------
# Indeed multi-job parser
# ---------------------------------------------------------------------------

_INDEED_FOOTER_SKIP = {
    # Time-range filter links that appear in the footer
    "since yesterday", "for last 7 days", "for last 14 days", "for last 30 days",
    # Footer navigation
    "edit this job alert", "delete this job alert", "manage job alerts",
    "manage alerts", "view all jobs", "see all jobs", "view all",
    "view in browser", "unsubscribe",
    # Legal / help links
    "indeed terms of service", "terms of service", "terms",
    "help center", "privacy", "privacy policy", "cookie policy", "accessibility",
}

# Company-Location on the same line: "Vaco by Highspring - Raleigh, NC"
_CO_LOC_RE = re.compile(
    r"^(.+?)\s*[-–]\s*([A-Za-z][A-Za-z '\-]+,\s+[A-Z]{2})\s*$"
)

# Indeed's own digest headline ("N new <query> jobs in <location>") is wrapped
# in an <h2> right alongside the real job-card headlines, so a naive h2 walk
# picks it up as if it were a listing — the title looks job-shaped ("...jobs
# in Durham, NC") and there's no real company/JD behind it, just the alert's
# own summary sentence (company td resolves to the "These jobs match your
# saved job alert…" boilerplate). Matched on BOTH signals together, never
# title alone, so a genuine, unusually-phrased job title is never at risk.
_DIGEST_HEADER_TITLE_RE = re.compile(r"^\d+\s+new\b.*\bjobs?\b.*\bin\b", re.IGNORECASE)
_DIGEST_HEADER_COMPANY_PREFIX = "these jobs match your saved job alert"


def _is_digest_header(title: str, company: str) -> bool:
    """True if this h2 is Indeed's own alert-summary headline, not a real job
    listing. See CLAUDE.md's Indeed digest-header gotcha."""
    if not _DIGEST_HEADER_TITLE_RE.match((title or "").strip()):
        return False
    return (company or "").strip().lower().startswith(_DIGEST_HEADER_COMPANY_PREFIX)


def _parse_indeed_jobs_h2(html_content: str, email_date: str) -> list[dict]:
    """
    Parse Indeed job-alert emails that use an <h2> tag for the job title
    (newer Indeed email format as of ~2025).

    Note: lxml collapses nested <a> tags (invalid HTML), so we cannot rely
    on parent <a> traversal. Instead we navigate by <tr> siblings within the
    job card table to find company, location, salary, and the job URL.
    """
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    for h2 in soup.find_all("h2"):
        title = h2.get_text(separator=" ", strip=True)
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if title.lower() in _INDEED_FOOTER_SKIP:
            continue
        if any(w in title.lower() for w in ["unsubscribe", "manage", "view all", "privacy", "edit"]):
            continue

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        # Get the URL - look in the card table for an indeed job link
        # (lxml collapses nested <a> tags so parent traversal may miss it)
        # Indeed uses two link patterns:
        #   Organic:   /rc/clk/dl?jk=...
        #   Sponsored: /pagead/clk/dl?mo=r&ad=...
        _INDEED_JOB_LINK = ("jk=", "/rc/clk", "/pagead/clk")
        href = ""
        node = h2.parent
        for _ in range(6):
            if node is None:
                break
            if node.name == "a":
                candidate = node.get("href", "")
                if "indeed" in candidate.lower() and any(p in candidate for p in _INDEED_JOB_LINK):
                    href = candidate
                    break
            node = node.parent

        # Navigate up to find the containing <table> for this job card,
        # then collect all <td> text from its rows.
        card_table = None
        node = h2.parent
        for _ in range(8):
            if node is None:
                break
            if node.name == "table":
                card_table = node
                break
            node = node.parent

        company  = "Unknown"
        location = ""
        salary   = None
        snippet  = ""

        if card_table:
            # If we didn't find the URL via parent traversal, search the card
            # table AND its parent container. lxml strips <a> tags that wrap
            # <table> elements (invalid HTML), so the pagead/clk href on the
            # outer card link ends up on card_table.parent instead.
            if not href:
                search_roots = [card_table]
                if card_table.parent:
                    search_roots.append(card_table.parent)
                for root in search_roots:
                    for a in root.find_all("a", href=True):
                        candidate = a.get("href", "")
                        if "indeed" in candidate.lower() and any(p in candidate for p in _INDEED_JOB_LINK):
                            href = candidate
                            break
                    if href:
                        break

            for td in card_table.find_all("td"):
                if td.find("h2"):
                    continue  # skip the title td
                raw = td.get_text(separator=" ", strip=True)
                clean = _LOOSE_DIGIT_RE.sub("", _RATING_RE.sub("", raw)).strip()
                if not clean or len(clean) < 2:
                    continue
                if clean.lower() in _NOISE_LINES or clean.lower() in _INDEED_FOOTER_SKIP:
                    continue
                if _AGO_RE.search(clean):
                    continue
                if clean.lower() in {"easily apply", "actively recruiting", "actively hiring",
                                     "just posted", "new", "responsive employer"}:
                    continue
                # Skip short numeric fragments left by rating stripping
                if re.match(r"^\d+(\.\d+)?$", clean):
                    continue

                if not location and _LOC_RE.match(clean):
                    location = extract_location(clean) or clean
                elif not salary and _SALARY_LINE_RE.match(clean):
                    salary = clean
                elif company == "Unknown" and not _SALARY_LINE_RE.match(clean) and not _LOC_RE.match(clean):
                    company = clean
                elif not snippet and len(clean) > 20 and company != "Unknown" and location:
                    snippet = clean[:250]

        if not href and not company and company == "Unknown":
            continue  # nothing useful extracted

        if _is_digest_header(title, company):
            if DEBUG:
                log.info("[debug] _parse_indeed_jobs_h2: skipping digest-header artifact: %r", title)
            continue

        jobs.append({
            "title":    title,
            "company":  company,
            "location": location or "",
            "salary":   salary,
            "source":   "Indeed",
            "snippet":  snippet,
            "url":      href,
            "date":     email_date,
        })

    if DEBUG:
        log.info("[debug] _parse_indeed_jobs_h2: %d job(s) extracted", len(jobs))

    return jobs

    if DEBUG:
        log.info("[debug] _parse_indeed_jobs_h2: %d job(s) extracted", len(jobs))

    return jobs


def parse_indeed_jobs(html_content: str, email_date: str) -> list[dict]:
    """
    Parse an Indeed job-alert HTML email and return a list of job dicts.

    Tries the newer h2-based format first, then falls back to the legacy
    link-based parser for older email formats.

    Indeed tracking links use indeedmail.com or click.em.indeed.com — we
    match on 'indeed' (not 'indeed.com') to catch all variants.
    Footer navigation links (time filters, ToS, help, unsubscribe) are skipped
    via _INDEED_FOOTER_SKIP.
    """
    # Try newer h2-based format first
    jobs = _parse_indeed_jobs_h2(html_content, email_date)
    if jobs:
        return jobs

    # Fall back to legacy link-based parser
    soup = BeautifulSoup(html_content, "lxml")
    jobs = []
    seen_titles = set()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        # Match any link that goes through Indeed's infrastructure
        if "indeed" not in href.lower():
            continue

        title = link.get_text(separator=" ", strip=True)

        # Skip navigation / footer links
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if title.lower() in _INDEED_FOOTER_SKIP:
            continue
        if any(w in title.lower() for w in ["unsubscribe", "manage", "settings", "click", "view all", "privacy"]):
            continue

        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        # Build context with newlines so we can extract structured fields.
        # Walk up the DOM until we have at least 3 non-empty lines.
        context_lines: list[str] = []
        node = link.parent
        for _ in range(6):
            if node is None:
                break
            raw_lines = [l.strip() for l in node.get_text(separator="\n").splitlines() if l.strip()]
            if len(raw_lines) >= 3:
                context_lines = raw_lines
                break
            node = node.parent
        if not context_lines:
            context_lines = [title]

        # Find the title position so we can look at what follows it.
        title_idx = next((i for i, l in enumerate(context_lines) if l.lower() == title.lower()), None)
        after = context_lines[title_idx + 1 :] if title_idx is not None else context_lines

        company  = "Unknown"
        location = ""
        salary   = None

        for line in after[:8]:
            # Strip star-rating noise: "3.8  3.8 out of 5 stars" -> ""
            clean = _LOOSE_DIGIT_RE.sub("", _RATING_RE.sub("", line)).strip()
            if not clean or clean.lower() in _NOISE_LINES or clean.lower() in _INDEED_FOOTER_SKIP:
                continue

            # "Company - City, ST" on one line
            m = _CO_LOC_RE.match(clean)
            if m:
                if company == "Unknown":
                    company = m.group(1).strip()
                if not location:
                    location = extract_location(m.group(2)) or m.group(2)
                continue

            # Standalone "City, ST"
            if not location and _LOC_RE.match(clean):
                location = extract_location(clean) or clean
                continue

            # Salary
            if not salary and _SALARY_LINE_RE.match(clean):
                salary = clean
                continue

            # First meaningful non-noise line after title = company (if not yet found)
            if company == "Unknown" and len(clean) > 2 and not _AGO_RE.search(clean):
                company = clean

        # Build a short snippet from the full context block
        snippet = " ".join(context_lines)[:250].strip()

        jobs.append({
            "title":    title,
            "company":  company,
            "location": location or "",
            "salary":   salary,
            "source":   "Indeed",
            "snippet":  snippet,
            "url":      href,
            "date":     email_date,
        })

    return jobs


# Indeed-specific plain-text layout:
#   Job Title
#   Company Name  [X.X  X.X out of 5 stars]   ← tabs may be present
#   City, ST
#   $salary range                               ← optional
#   Easily apply                                ← optional
#   Description snippet
#   N days ago / Just posted

_LOC_RE    = re.compile(
    r"^(?:[A-Za-z][A-Za-z '\-]*),\s+[A-Z]{2}(?:\s|$)|^United States$|^Remote(?:\s|$)",
    re.IGNORECASE,
)
_SALARY_LINE_RE = re.compile(r"^\$[\d,]")
_RATING_RE      = re.compile(r"\b\d+(?:\.\d+)?\s+out of \d+ stars\b", re.IGNORECASE)
_LOOSE_DIGIT_RE = re.compile(r"\s+\d+(?:\.\d+)?\s*$")
_AGO_RE         = re.compile(r"\b(?:\d+\s+(?:hours?|days?)\s+ago|just\s+posted)\b", re.IGNORECASE)
_NOISE_LINES    = {"easily apply", "actively hiring", "hiring multiple candidates", "new", "¹", ""}

# Known footer / navigation strings that can appear 2 lines above an address line
# (e.g. "Austin, TX" in Indeed's footer) and get mis-identified as job titles.
_FOOTER_TITLES = {
    "since yesterday", "for last 7 days", "for last 30 days",
    "edit this job alert", "delete this job alert", "manage job alerts",
    "indeed terms of service", "terms of service", "terms", "privacy",
    "privacy policy", "help center", "cookie policy", "accessibility",
    "unsubscribe", "view in browser", "contact us", "feedback",
    "view all jobs", "see all jobs", "view more jobs", "view all",
    "jobs", "job alert", "job alerts", "new job alert",
    "jobs by email", "email settings", "manage alerts",
    "|", "· ", "•",
}


def parse_indeed_text(body_text: str, email_date: str) -> list[dict]:
    """
    Parse an Indeed job-alert email that arrives as plain text (or whose
    HTML links couldn't be resolved by parse_indeed_jobs).

    Strategy: find every line that looks like 'City, ST', then look one line
    back for the company and two lines back for the title.
    """
    # Normalise: collapse tabs to single space, strip Unicode junk
    text = re.sub(r"\t+", " ", body_text)
    lines = [ln.strip() for ln in text.splitlines()]
    # Keep blank lines as sentinels so index arithmetic stays stable,
    # but strip pure-noise lines later when inspecting.

    jobs: list[dict] = []
    seen: set[str] = set()

    def _reject_title(t: str) -> bool:
        tl = t.lower().strip()
        return (
            not t
            or _SALARY_LINE_RE.match(t) is not None
            or _RATING_RE.search(t) is not None
            or tl in _NOISE_LINES
            or tl in _FOOTER_TITLES
            or tl in _INDEED_FOOTER_SKIP
            or _AGO_RE.search(t) is not None
            or bool(re.match(r"^(since|for|during|in the|view|see|manage|edit|delete|unsubscribe|https?://)", tl))
        )

    for idx, line in enumerate(lines):
        # -----------------------------------------------------------------
        # Format A: standalone "City, ST" line
        #   lines[idx-2] = title
        #   lines[idx-1] = company (may have star-rating suffix)
        #   lines[idx]   = "City, ST"
        # -----------------------------------------------------------------
        if _LOC_RE.match(line):
            company_raw = lines[idx - 1] if idx >= 1 else ""
            company = _LOOSE_DIGIT_RE.sub("", _RATING_RE.sub("", company_raw)).strip()
            # If the line immediately above location is noise (e.g. "Easily apply",
            # a rating, salary), look one more line up for the company name.
            if not company or company.lower() in _NOISE_LINES or _SALARY_LINE_RE.match(company):
                company_raw2 = lines[idx - 2] if idx >= 2 else ""
                company2 = _LOOSE_DIGIT_RE.sub("", _RATING_RE.sub("", company_raw2)).strip()
                if company2 and company2.lower() not in _NOISE_LINES and not _SALARY_LINE_RE.match(company2):
                    # company was noise; shift: company=lines[idx-2], title=lines[idx-3]
                    company = company2
                    title_candidate = lines[idx - 3] if idx >= 3 else ""
                else:
                    title_candidate = lines[idx - 2] if idx >= 2 else ""
            else:
                title_candidate = lines[idx - 2] if idx >= 2 else ""
            if not title_candidate:
                title_candidate = company
                company = "Unknown"
            location_text = line
            salary_start = idx + 1

        # -----------------------------------------------------------------
        # Format B: "Company - City, ST" on one line (real API email format)
        #   lines[idx-1] = title
        #   lines[idx]   = "Company - City, ST"
        # -----------------------------------------------------------------
        elif (m := _CO_LOC_RE.match(line)):
            company = _LOOSE_DIGIT_RE.sub("", _RATING_RE.sub("", m.group(1))).strip()
            title_candidate = lines[idx - 1] if idx >= 1 else ""
            location_text = m.group(2).strip()
            salary_start = idx + 1

        else:
            continue

        if _reject_title(title_candidate):
            continue

        title_key = title_candidate.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        # ---- salary + snippet: lines after the location line ----
        salary = None
        snippet_parts: list[str] = []
        for j in range(salary_start, min(salary_start + 8, len(lines))):
            ln = lines[j]
            if _AGO_RE.search(ln) or re.match(r"^https?://", ln):
                break
            if _SALARY_LINE_RE.match(ln) and not salary:
                salary = ln.split("  ")[0].strip()
            elif ln.lower() not in _NOISE_LINES and not _RATING_RE.search(ln) and ln:
                snippet_parts.append(ln)

        jobs.append({
            "title":    title_candidate,
            "company":  company or "Unknown",
            "location": extract_location(location_text) or location_text,
            "salary":   salary,
            "source":   "Indeed",
            "snippet":  " ".join(snippet_parts)[:280],
            "date":     email_date,
        })

    if DEBUG:
        log.info(
            "[debug] parse_indeed_text: %d location lines found -> %d job(s) extracted",
            sum(1 for l in lines if _LOC_RE.match(l)),
            len(jobs),
        )

    return jobs


# ---------------------------------------------------------------------------
# Cisco job-alert parser
#
# Cisco emails route all links through SendGrid tracking URLs.
# The job listing section is in a div with data-uiwidget-type='SIMILAR_JOBS_WIDGET'.
# Each job title is in an <a> tag; "Apply Now" buttons are filtered out.
# ---------------------------------------------------------------------------

_CISCO_SKIP = {
    "apply now", "explore open roles", "have a great job search",
    "cisco talent acquisition team", "power an inclusive future for all",
    "unsubscribe", "manage", "privacy", "view in browser",
}


def parse_cisco_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse individual job listings from a Cisco job-alert HTML email."""
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    # Prefer to search inside the jobs widget so we don't pick up header/footer links.
    container = soup.find(attrs={"data-uiwidget-type": "SIMILAR_JOBS_WIDGET"})
    scope = container if container else soup

    for link in scope.find_all("a", href=True):
        # Cisco email links go through SendGrid; the header logo also goes
        # through SendGrid but carries an <img> child with no text.
        if link.find("img"):
            continue

        title = link.get_text(separator=" ", strip=True)
        if not title or len(title) < 5:
            continue
        if title.lower() in _CISCO_SKIP:
            continue
        # Skip short boilerplate fragments
        if any(phrase in title.lower() for phrase in _CISCO_SKIP):
            continue

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        if DEBUG:
            log.info("[debug] Cisco link text: %r", title)

        jobs.append({
            "title":    title,
            "company":  "Cisco",
            "location": "",   # email says "Available in N locations" — no specific city
            "salary":   None,
            "source":   "Cisco",
            "snippet":  "Cisco job alert (multiple locations — check link for details)",
            "url":      link.get("href", ""),
            "date":     email_date,
        })

    return jobs


# ---------------------------------------------------------------------------
# Duke Careers job-alert parser
#
# Duke emails use a simple HTML format:
#   <a class="agentjoblink" href="...careers.duke.edu...">
#     TITLE - Durham, NC, US, 27710
#   </a>
# ---------------------------------------------------------------------------

_DUKE_LOC_SUFFIX = re.compile(
    r"\s*-\s*[A-Za-z\s]+,\s+[A-Z]{2}(?:,\s+[A-Z]{2})?(?:,\s+\d+)?\s*$"
)


def parse_duke_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse individual job listings from a Duke Careers job-alert HTML email."""
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("a", class_="agentjoblink"):
        href = link.get("href", "")
        raw = link.get_text(separator=" ", strip=True)
        if not raw:
            continue

        # Strip trailing " - Durham, NC, US, 27710" location suffix
        title = _DUKE_LOC_SUFFIX.sub("", raw).strip()
        if not title:
            title = raw

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        jobs.append({
            "title":    title,
            "company":  "Duke University",
            "location": "Durham",
            "salary":   None,
            "source":   "Duke Careers",
            "url":      href,
            "snippet":  "",
            "date":     email_date,
        })

    if DEBUG:
        log.info("[debug] parse_duke_jobs: %d job(s) extracted", len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# Siemens Healthineers job-alert parser
#
# Siemens sends multi-job digest emails (subject "A new role at Siemens…")
# that look single-job from the subject but contain several listings.
# Job links route through an AWS tracking redirect whose decoded URL starts
# with careers.siemens-healthineers.com — that substring is the selector.
# "Apply now" buttons share the same href prefix and are excluded by text.
# ---------------------------------------------------------------------------

_SIEMENS_LINK_SKIP = {
    "apply now", "contact us", "privacy policy", "unsubscribe",
    "siemens-healthineers.com",
}


def parse_government_jobs(html_content: str, subject: str, email_date: str) -> list[dict]:
    """Parse governmentjobs.com (NEOGOV) job interest card notification emails.

    Used by City of Durham, Wake County, and other local governments.
    Format: Job titles are linked with direct governmentjobs.com URLs.
    The employer name is extracted from the subject line:
      "City of Durham Job Interest Card Notification"
    """
    from bs4 import BeautifulSoup
    import re
    jobs = []
    seen: set[str] = set()

    if not html_content:
        return jobs

    # Extract employer name from subject: "City of Durham Job Interest Card..."
    employer = "Local Government"
    m = re.match(r"^(.+?)\s+Job Interest Card", subject)
    if m:
        employer = m.group(1).strip()

    soup = BeautifulSoup(html_content, "lxml")

    for a in soup.find_all("a", href=True):
        url = a["href"]
        if "governmentjobs.com" not in url or "/jobs/" not in url:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 4 or len(title) > 150:
            continue
        # Skip management links
        if "jobInterestCards" in url or "manage" in url:
            continue

        key = title.lower()
        if key in seen:
            continue
        seen.add(key)

        jobs.append({
            "title":    title,
            "company":  employer,
            "location": "Durham, NC" if "durhamnc" in url else "North Carolina",
            "url":      url,
            "salary":   "",
            "snippet":  "",
            "date":     email_date,
            "source":   "Government Jobs",
        })

    if DEBUG:
        log.info("[debug] parse_government_jobs: %d job(s) extracted", len(jobs))
    return jobs


def parse_nc_state_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse State of North Carolina Workday job alert emails from workday@nc.gov.

    Format: Job titles are linked with direct myworkdayjobs.com URLs inside
    a <span> tag. Each job appears as:
      <a href="https://nc.wd108.myworkdayjobs.com/...">Job Title</a> (County, NC)
    """
    from bs4 import BeautifulSoup
    import re
    jobs = []
    seen: set[str] = set()

    if not html_content:
        return jobs

    soup = BeautifulSoup(html_content, "lxml")

    for a in soup.find_all("a", href=True):
        url = a["href"]
        if "myworkdayjobs.com" not in url:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 4 or len(title) > 150:
            continue

        # Extract location from the text immediately following the link
        # Format: "Job Title (County, NC)"
        next_text = ""
        for sibling in a.next_siblings:
            text = str(sibling).strip()
            if text:
                next_text = text
                break
        location_match = re.search(r"\(([^)]+NC[^)]*)\)", next_text)
        location = location_match.group(1).strip() if location_match else "North Carolina"

        key = title.lower()
        if key in seen:
            continue
        seen.add(key)

        jobs.append({
            "title":    title,
            "company":  "State of North Carolina",
            "location": location,
            "url":      url,
            "salary":   "",
            "snippet":  "",
            "date":     email_date,
            "source":   "State of NC",
        })

    if DEBUG:
        log.info("[debug] parse_nc_state_jobs: %d job(s) extracted", len(jobs))
    return jobs


def parse_netapp_jobs(html_content: str, body_text: str, email_date: str) -> list[dict]:
    """Parse NetApp job alert emails from no-reply@tbjobalerts.com.

    These emails are sparse — typically 1-3 jobs listed as plain text or
    minimal HTML with job title as a link. We extract from both HTML and
    plain text since the HTML body is often minimal.
    """
    from bs4 import BeautifulSoup
    jobs = []
    seen: set[str] = set()

    # Try HTML first — job titles are typically in <a> tags
    if html_content:
        soup = BeautifulSoup(html_content, "lxml")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            url = a["href"]
            if not title or len(title) < 4 or len(title) > 120:
                continue
            # Filter out navigation links
            skip_phrases = ["manage", "unsubscribe", "view", "click", "here",
                            "netapp", "careers", "privacy", "contact"]
            if any(p in title.lower() for p in skip_phrases):
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            jobs.append({
                "title":    title,
                "company":  "NetApp",
                "location": "",   # NetApp alerts don't include location in email
                "url":      url,
                "salary":   "",
                "snippet":  "",
                "date":     email_date,
                "source":   "NetApp",
            })

    # Fallback: extract from plain text snippet if HTML yielded nothing
    if not jobs and body_text:
        for line in body_text.splitlines():
            line = line.strip()
            if not line or len(line) < 4 or len(line) > 120:
                continue
            skip = ["manage my alerts", "unsubscribe", "new opportunities",
                    "check out", "match your interests", "netapp"]
            if any(s in line.lower() for s in skip):
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            jobs.append({
                "title":    line,
                "company":  "NetApp",
                "location": "",
                "url":      "https://careers.netapp.com",
                "salary":   "",
                "snippet":  "",
                "date":     email_date,
                "source":   "NetApp",
            })

    if DEBUG:
        log.info("[debug] parse_netapp_jobs: %d job(s) extracted", len(jobs))
    return jobs


def parse_cvs_jobs(html_content: str, body_text: str, email_date: str) -> list[dict]:
    """Parse CVS Health Careers job alert emails.

    CVS uses a standard career site alert format with job titles as links.
    We extract <a> tags that look like job titles, filtering nav/footer links.
    """
    from bs4 import BeautifulSoup
    jobs = []
    seen: set[str] = set()

    if not html_content:
        return jobs

    soup = BeautifulSoup(html_content, "lxml")

    # CVS alerts typically have job titles as linked text in the email body
    skip_phrases = ["unsubscribe", "manage", "privacy", "contact", "cvs health",
                    "click here", "view all", "apply", "careers", "social",
                    "facebook", "twitter", "linkedin", "instagram"]

    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        url = a["href"]
        if not title or len(title) < 5 or len(title) > 150:
            continue
        if any(p in title.lower() for p in skip_phrases):
            continue
        # Job titles tend to contain role-like words
        role_signals = ["analyst", "engineer", "scientist", "developer", "manager",
                        "specialist", "coordinator", "associate", "director", "lead"]
        if not any(s in title.lower() for s in role_signals):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "title":    title,
            "company":  "CVS Health",
            "location": "",
            "url":      url,
            "salary":   "",
            "snippet":  "",
            "date":     email_date,
            "source":   "CVS Health",
        })

    if DEBUG:
        log.info("[debug] parse_cvs_jobs: %d job(s) extracted", len(jobs))
    return jobs


def parse_ncworks_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse NCWorks Virtual Recruiter notification emails.

    The email body is a simple HTML table where each job row contains four
    <td class="JOBSLISTCELLTITLE"> cells (title, employer, location) followed
    by a <td class="JOBSLISTCELLVIEW"> cell containing the View link.

    The View links are geosolinc.com tracking redirects. URL resolution to
    the final NCWorks job page happens post-classification (pursue/review only)
    to avoid making HTTP requests for all 148 jobs when most get skipped.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "lxml")
    jobs = []

    rows = soup.find_all("tr")
    for row in rows:
        cells = row.find_all("td", class_="JOBSLISTCELLTITLE")
        view_cell = row.find("td", class_="JOBSLISTCELLVIEW")
        if len(cells) < 3 or not view_cell:
            continue

        title    = cells[0].get_text(strip=True)
        company  = cells[1].get_text(strip=True)
        location = cells[2].get_text(strip=True)

        link_tag = view_cell.find("a", href=True)
        url = link_tag["href"] if link_tag else ""

        if not title or title.lower() in ("job title",):
            continue  # skip header row

        jobs.append({
            "title":    title,
            "company":  company,
            "location": location,
            "url":      url,
            "salary":   "",
            "snippet":  "",
            "date":     email_date,
            "source":   "NCWorks",
        })

    if DEBUG:
        log.info("[debug] parse_ncworks_jobs: %d job(s) extracted", len(jobs))
    return jobs


def parse_siemens_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse job listings from a Siemens Healthineers job-alert HTML email."""
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        # All job links (and their "Apply now" siblings) pass through
        # an AWS tracker that embeds the careers.siemens-healthineers.com URL.
        if "careers.siemens-healthin" not in link.get("href", ""):
            continue

        title = link.get_text(separator=" ", strip=True)
        if not title or len(title) < 5:
            continue
        if title.lower() in _SIEMENS_LINK_SKIP:
            continue

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        location = extract_location(title) or ""

        jobs.append({
            "title":    title,
            "company":  "Siemens Healthineers",
            "location": location,
            "salary":   None,
            "source":   "Siemens Healthineers",
            "snippet":  title,
            "url":      link.get("href", ""),
            "date":     email_date,
        })

    return jobs


# ---------------------------------------------------------------------------
# UNC Chapel Hill job-alert parser
#
# UNC's "Careers at Carolina Notification" emails (from noreply@hr.unc.edu)
# are HTML digests with N job postings. Each posting is an
# <a href="http://unc.peopleadmin.com/postings/NNNNNN"> anchor containing
# the title, followed by a sibling <div> with the description.
# Duplicate TITLES are normal (same role name across departments);
# deduplicate by URL, not title.
# ---------------------------------------------------------------------------

_UNC_POSTING_HREF = "unc.peopleadmin.com/postings/"


def parse_unc_jobs(html_content: str, email_date: str) -> list[dict]:
    """Parse individual job listings from a UNC Chapel Hill alert email."""
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if _UNC_POSTING_HREF not in href:
            continue
        if href in seen:
            continue
        seen.add(href)

        title = anchor.get_text(separator=" ", strip=True)
        if not title:
            continue

        # Description lives in a <div> inside the same parent <div>.
        desc = ""
        parent = anchor.find_parent("div")
        if parent is not None:
            inner_divs = [d for d in parent.find_all("div") if d is not parent]
            if inner_divs:
                desc = inner_divs[0].get_text(separator=" ", strip=True)

        jobs.append({
            "title":    title,
            "company":  "UNC Chapel Hill",
            "location": "Chapel Hill",
            "salary":   None,
            "source":   "UNC Chapel Hill",
            "snippet":  desc,
            "url":      href,
            "date":     email_date,
        })

    if DEBUG:
        log.info("[debug] parse_unc_jobs: %d job(s) extracted", len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# LinkedIn job-alert digest parser
#
# LinkedIn sends multi-job HTML digests from jobalerts-noreply@linkedin.com.
# The plain-text part is the most reliable: each job block looks like:
#   TITLE
#   Company Name
#   City, ST
#   [optional social proof line]
#   View job: https://www.linkedin.com/comm/jobs/view/JOBID/?...
#   ---------------------------------------------------------
# Direct linkedin.com/jobs/view URLs are preserved (not tracking redirects).
# ---------------------------------------------------------------------------

_LI_VIEW_RE = re.compile(r"View job:\s*(https://www\.linkedin\.com/\S+)", re.IGNORECASE)
_LI_SEPARATOR = re.compile(r"^-{10,}\s*$")
_LI_NOISE = {
    "this company is actively hiring", "actively recruiting",
    "1 connection", "2 connections", "3 connections",
    "school alumni", "school alum",
    "see all jobs on linkedin", "see all jobs",
    "stand out and let hirers know", "try premium",
    "manage job alerts", "unsubscribe", "help",
    "you are receiving job alert emails",
}


def parse_linkedin_jobs(body_text: str, email_date: str) -> list[dict]:
    """Parse a LinkedIn job-alert digest email (plain-text part)."""
    lines = [ln.strip() for ln in body_text.splitlines()]
    jobs: list[dict] = []
    seen: set[str] = set()

    # Split into blocks by separator lines
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _LI_SEPARATOR.match(line):
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        # Extract URL first
        url = ""
        content_lines = []
        for line in block:
            m = _LI_VIEW_RE.search(line)
            if m:
                url = m.group(1).strip()
            elif line and line.lower() not in _LI_NOISE and not line.startswith("http"):
                content_lines.append(line)

        if not content_lines:
            continue

        # First non-noise line = title
        title = content_lines[0].strip()
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if title.lower() in _LI_NOISE:
            continue

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        # Second line = "Company · City, ST" or just company
        company = "Unknown"
        location = ""
        if len(content_lines) > 1:
            second = content_lines[1]
            if "·" in second:
                parts = second.split("·", 1)
                company = parts[0].strip()
                location = extract_location(parts[1].strip()) or parts[1].strip()
            else:
                company = second.strip()
                if len(content_lines) > 2:
                    location = extract_location(content_lines[2]) or ""

        salary = extract_salary(" ".join(content_lines))

        jobs.append({
            "title":    title,
            "company":  company,
            "location": location,
            "salary":   salary,
            "source":   "LinkedIn",
            "snippet":  " ".join(content_lines[2:])[:250].strip(),
            "url":      url,
            "date":     email_date,
        })

    return jobs


# ---------------------------------------------------------------------------
# NC Biotech Center / Getro job-alert digest parser
#
# Getro sends HTML digests on behalf of NC Biotech Center's job board.
# Each job block is a <table> with:
#   - Title in <strong><a> tag
#   - Company in a <td> one row below (smaller font, links to company page)
#   - Location in the next <td> (e.g. "Durham, NC, USA")
# All links route through url3473.getro.com tracking redirects — we store
# a search fallback URL rather than the opaque redirect.
# ---------------------------------------------------------------------------

_GETRO_NOISE = {
    "see more jobs", "unsubscribe", "make a new search", "this link",
    "powered by getro", "learning center", "getro team",
    "do not forward this email",
}


def parse_getro_jobs(html_content: str, email_date: str, source: str = "NC Biotech Center") -> list[dict]:
    """Parse a Getro job-board digest HTML email."""
    soup = BeautifulSoup(html_content, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    # Each job block is a <table style="margin-top: 16px; ...">
    # Title is in a <strong><a> tag; company + location follow in sibling <td>s.
    for strong in soup.find_all("strong"):
        a = strong.find("a")
        if not a:
            continue
        title = a.get_text(separator=" ", strip=True)
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if title.lower() in _GETRO_NOISE:
            continue

        title_key = title.lower()
        if title_key in seen:
            continue
        seen.add(title_key)

        # Walk up to find the containing <td>, then look at sibling rows
        # for company (font-size 14px) and location (font-size 13px).
        company = "Unknown"
        location = ""
        parent_td = strong.find_parent("td")
        if parent_td:
            parent_table = parent_td.find_parent("table")
            if parent_table:
                rows = parent_table.find_all("tr")
                for row in rows:
                    tds = row.find_all("td")
                    for td in tds:
                        style = td.get("style", "")
                        text = td.get_text(separator=" ", strip=True)
                        if not text or text.lower() in _GETRO_NOISE:
                            continue
                        if "font-size: 14px" in style and company == "Unknown":
                            company = text
                        elif "font-size: 13px" in style and not location:
                            location = extract_location(text) or text

        # Build search fallback URL
        search_url = (
            f"https://www.linkedin.com/jobs/search/?keywords="
            f"{title.replace(' ', '%20')}%20{company.replace(' ', '%20')}"
        )

        jobs.append({
            "title":    title,
            "company":  company,
            "location": location,
            "salary":   None,
            "source":   source,
            "snippet":  f"{title} at {company}",
            "url":      search_url,
            "date":     email_date,
        })

    if DEBUG:
        log.info("[debug] parse_getro_jobs (%s): %d job(s) extracted", source, len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# Generic single-job parser (NCWorks, NetApp, Red Hat, talent alerts)
# ---------------------------------------------------------------------------

# Subjects that are generic talent-community messages rather than job titles.
# When matched, we skip the subject and try to extract the title from the body.
_GENERIC_SUBJECT_RE = re.compile(
    r"""
    (?:^a\s+new\s+role\b          # "A new role at..."
    |you\s+may\s+be\s+interested  # "...you may be interested"
    |\btalent\s+community\b       # "talent community alert"
    |\bnew\s+opportunity\s+for\b  # "new opportunity for you"
    |\bjob(?:s)?\s+matched?\b     # "jobs matching your profile"
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_body_title(body_text: str) -> str | None:
    """
    Try to pull a job title from the first ~1000 chars of the body of a
    generic talent-alert email where the subject carries no title.

    Strategy:
      1. Labeled fields — "Position: ...", "Role: ...", "Job Title: ..."
      2. Line scan — first short line (8-80 chars) that contains a known
         DS/ML/AI title keyword.
    """
    search_zone = body_text[:1000]

    # 1. Labeled fields
    m = re.search(
        r"(?:position|role|job\s+title)\s*[:\-–]\s*([^\n]{5,80})",
        search_zone,
        re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip().rstrip(".")
        if 5 < len(candidate) < 80:
            return candidate

    # 2. Line scan: short title-like lines containing a DS/ML/AI keyword.
    #    Use global INCLUDE_TITLE_KEYWORDS plus a few expansions not in the list.
    kws = [kw.lower() for kw in config.INCLUDE_TITLE_KEYWORDS] + [
        "machine learning", "data science", "artificial intelligence",
        "engineer", "scientist",
    ]
    for line in search_zone.splitlines():
        line = line.strip()
        if 8 < len(line) < 80 and "http" not in line and not line.endswith("."):
            if any(kw in line.lower() for kw in kws):
                return line

    return None


def parse_single_job(subject: str, body_text: str, source: str, email_date: str) -> dict:
    """Build a single job dict from an email subject + body."""
    # Heuristic: subject often contains the job title
    # Strip common prefixes like "New Job Alert:", "Job Opportunity:", etc.
    title = re.sub(
        r"^(?:new\s+)?(?:job\s+)?(?:alert|opportunity|opening|notification|posting)[\s:\-–|]+",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip()

    # For generic talent-alert subjects ("A new role at Siemens..."), the
    # actual role title lives in the body.  Try to extract it before we fall
    # back to the unhelpful subject string.
    if _GENERIC_SUBJECT_RE.search(title) or _GENERIC_SUBJECT_RE.search(subject):
        body_title = _extract_body_title(body_text)
        if body_title:
            title = body_title

    # Some subjects include "Company - Title" or "Title | Company"
    company = extract_company(subject, body_text, source)
    location = extract_location(subject) or extract_location(body_text[:500])
    salary   = extract_salary(subject) or extract_salary(body_text[:800])

    # Build a ~2-sentence snippet from the body
    sentences = re.split(r"(?<=[.!?])\s+", body_text.replace("\n", " "))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
    snippet = " ".join(sentences[:2])[:300]

    return {
        "title":    title,
        "company":  company,
        "location": location or "",
        "salary":   salary,
        "source":   source,
        "snippet":  snippet,
        "date":     email_date,
    }


# ---------------------------------------------------------------------------
# Email -> job(s) dispatcher
# ---------------------------------------------------------------------------

_GENERIC_SENDER_GENERIC_NAMES = {
    "jobs", "job alerts", "job alert", "no-reply", "noreply", "no reply",
    "notifications", "alerts", "do-not-reply", "donotreply", "info",
    "careers", "talent", "recruiting", "team", "mailer", "news",
}


def _sender_label(from_header: str) -> str:
    """Readable source tag for an email from a sender not in SENDERS — the
    display name when it's meaningful, otherwise the domain (with a common mail
    subdomain stripped for readability)."""
    name, addr = parseaddr(from_header)
    name = (name or "").strip()
    if name and "@" not in name and name.lower() not in _GENERIC_SENDER_GENERIC_NAMES:
        return name[:40]
    domain = addr.split("@")[-1].lower() if "@" in (addr or "") else (addr or "").lower()
    for pre in ("email.", "mail.", "e.", "mailer.", "notifications.", "jobs.", "careers."):
        if domain.startswith(pre):
            domain = domain[len(pre):]
            break
    return domain or "unknown sender"


def llm_extract_jobs(body_text: str, from_hdr: str, subject: str, email_date: str) -> list[dict]:
    """Generic fallback extractor for emails from senders with no hand-written
    parser. Sends the email to Claude (Haiku) and asks for structured job
    listings, or nothing if it isn't a job-alert email. Cost-controlled per run
    via config.LLM_EXTRACT_MAX_PER_RUN (counter reset in build_query). Extracted
    jobs flow through the same classify -> evaluate pipeline as parsed ones."""
    global _generic_extract_calls
    import json as _json
    import os as _os
    import ssl as _ssl
    import urllib.request
    import urllib.error

    cap = getattr(config, "LLM_EXTRACT_MAX_PER_RUN", 25)
    if _generic_extract_calls >= cap:
        if DEBUG:
            log.info("[debug] generic extract: per-run cap (%d) reached, skipping: %s", cap, subject)
        return []

    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []

    source = _sender_label(from_hdr)
    body = (body_text or "").strip()
    if len(body) < 40:
        return []
    body = body[:6000]

    prompt = f"""You are extracting job postings from an email that may be a job-alert digest, a single job posting, or NOT a job email at all (newsletter, marketing, account notice).

FROM: {from_hdr}
SUBJECT: {subject}
BODY:
{body}

TASK: Extract every concrete job posting in this email. For each posting capture: title, hiring company, location (if stated), the direct link to that posting (if present), and a one-line snippet.

RULES:
- Include ONLY real, specific job postings. Never include "see all jobs", "search", category, or generic keyword-search links.
- If a posting has no identifiable hiring company, omit it.
- If this is not a job-alert email, or it has no concrete postings, return an empty list.
- Use the apply/view link for "url" when present; otherwise an empty string.

Respond in JSON only, no other text:
{{"jobs": [{{"title": "...", "company": "...", "location": "...", "url": "...", "snippet": "..."}}]}}"""

    _generic_extract_calls += 1
    try:
        payload = _json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
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
        ctx = _ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, context=ctx, timeout=20) as resp:
                response_data = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise Exception(f"HTTP {e.code}: {err_body[:200]}")

        content = response_data.get("content", [])
        text = next((c["text"] for c in content if c.get("type") == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        result = _json.loads(text.strip())
        raw_jobs = result.get("jobs", []) if isinstance(result, dict) else []

        jobs: list[dict] = []
        for r in raw_jobs:
            if not isinstance(r, dict):
                continue
            title = (r.get("title") or "").strip()
            company = (r.get("company") or "").strip()
            if not title or not company:
                continue   # require both — mirrors the no-empty-employer guard
            jobs.append({
                "title":    title[:140],
                "company":  company[:100],
                "location": (r.get("location") or "").strip(),
                "salary":   None,
                "source":   source,
                "snippet":  (r.get("snippet") or "").strip()[:250],
                "url":      (r.get("url") or "").strip(),
                "date":     email_date,
            })
        return jobs

    except Exception as e:
        if DEBUG:
            log.info("[debug] generic extract error for %r: %s", subject, e)
        return []


def extract_jobs(message: dict) -> list[dict]:
    """Parse a Gmail message and return a list of job dicts."""
    headers    = message.get("payload", {}).get("headers", [])
    from_hdr   = get_header(headers, "From")
    subject    = get_header(headers, "Subject")
    date_hdr   = get_header(headers, "Date")

    source, sender_cfg = identify_source(from_hdr)
    if source is None:
        # Unknown sender. The Gmail query only fetched this because its subject
        # matched a job-intent keyword (see build_query), so hand it to the
        # generic LLM extractor instead of dropping it. Budget-capped per run;
        # returns nothing if the email isn't actually a job alert.
        plain, html = decode_payload(message["payload"])
        body_text = plain or strip_html(html)
        jobs = llm_extract_jobs(body_text, from_hdr, subject, date_hdr)
        if DEBUG:
            log.info("[debug] generic LLM extract (unknown sender): From=%s Subject=%s -> %d job(s)",
                     from_hdr, subject, len(jobs))
        return jobs

    # Apply subject filter (e.g. NCWorks, UNC Chapel Hill).
    # subject_filter may be a string (exact match) or a list (OR logic — at
    # least one entry must appear in the subject).
    if sender_cfg and "subject_filter" in sender_cfg:
        sf = sender_cfg["subject_filter"]
        filters = [sf] if isinstance(sf, str) else sf
        if not any(f.lower() in subject.lower() for f in filters):
            if DEBUG:
                log.info(
                    "[debug] SKIP (subject filter %r not found): Subject=%s",
                    filters, subject,
                )
            return []

    plain, html = decode_payload(message["payload"])
    body_text   = plain or strip_html(html)

    if source == "Indeed":
        # Try HTML parser first (catches proper job-link structure)
        if html:
            jobs = parse_indeed_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_indeed_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   HTML parser found 0 jobs -- falling back to text parser")

        # Fallback: plain-text layout parser
        text_jobs = parse_indeed_text(body_text, date_hdr)
        if DEBUG:
            log.info("[debug] parse_indeed_text fallback: Subject=%s -> %d job(s)", subject, len(text_jobs))
        if text_jobs:
            return text_jobs

        if DEBUG:
            log.info("[debug]   Both Indeed parsers returned 0 -- treating as single-job email")

    elif source == "Cisco":
        if html:
            jobs = parse_cisco_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_cisco_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   Cisco HTML parser found 0 jobs -- falling back to single-job parse")

    elif source == "Government Jobs":
        jobs = parse_government_jobs(html, subject, date_hdr)
        if DEBUG:
            log.info("[debug] parse_government_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
        if jobs:
            return jobs

    elif source == "State of NC":
        jobs = parse_nc_state_jobs(html, date_hdr)
        if DEBUG:
            log.info("[debug] parse_nc_state_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
        if jobs:
            return jobs

    elif source == "NetApp":
        jobs = parse_netapp_jobs(html, body_text, date_hdr)
        if DEBUG:
            log.info("[debug] parse_netapp_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
        if jobs:
            return jobs

    elif source == "CVS Health":
        jobs = parse_cvs_jobs(html, body_text, date_hdr)
        if DEBUG:
            log.info("[debug] parse_cvs_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
        if jobs:
            return jobs

    elif source == "NCWorks":
        if html:
            jobs = parse_ncworks_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_ncworks_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   NCWorks HTML parser found 0 jobs -- falling back to single-job parse")

    elif source == "Duke Careers":
        if html:
            jobs = parse_duke_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_duke_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   Duke HTML parser found 0 jobs -- falling back to single-job parse")

    elif source == "Grifols":
        if html:
            jobs = parse_duke_jobs(html, date_hdr)  # same jobs2web format as Duke
            # Override company name to Grifols
            for job in jobs:
                job["company"] = "Grifols"
                job["source"] = "Grifols"
            if DEBUG:
                log.info("[debug] parse_grifols_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs

    elif source == "Siemens Healthineers":
        if html:
            jobs = parse_siemens_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_siemens_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   Siemens HTML parser found 0 jobs -- falling back to single-job parse")

    elif source == "LinkedIn":
        jobs = parse_linkedin_jobs(body_text, date_hdr)
        if DEBUG:
            log.info("[debug] parse_linkedin_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
        if jobs:
            return jobs
        if DEBUG:
            log.info("[debug]   LinkedIn parser found 0 jobs -- falling back to single-job parse")

    elif source == "NC Biotech Center":
        if html:
            jobs = parse_getro_jobs(html, date_hdr, source="NC Biotech Center")
            if DEBUG:
                log.info("[debug] parse_getro_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   Getro HTML parser found 0 jobs -- falling back to single-job parse")

    elif source == "UNC Chapel Hill":
        if html:
            jobs = parse_unc_jobs(html, date_hdr)
            if DEBUG:
                log.info("[debug] parse_unc_jobs: Subject=%s -> %d job(s)", subject, len(jobs))
            if jobs:
                return jobs
            if DEBUG:
                log.info("[debug]   UNC HTML parser found 0 jobs -- falling back to single-job parse")

    job = parse_single_job(subject, body_text, source, date_hdr)
    if DEBUG:
        log.info(
            "[debug] %s single-job parse: Subject=%s -> title=%r  company=%r  location=%r  salary=%r",
            source, subject, job["title"], job["company"], job["location"], job["salary"],
        )
    return [job]


# ---------------------------------------------------------------------------
# Job classification
# ---------------------------------------------------------------------------

def classify_job(job: dict) -> tuple[str, str | None]:
    """
    Return (category, reason) where category is 'pursue', 'review', or 'skipped'.
    reason is None for pursue, a short string for review/skipped.
    """
    title_lower   = job["title"].lower()
    company_lower = job["company"].lower()
    # Combine title + snippet for keyword scanning
    full_text     = (job["title"] + " " + job["snippet"]).lower()

    # 0. Drop broken parser artifacts. A row with no real employer whose only
    #    link is a constructed keyword search (e.g. the Getro / NC-Biotech
    #    fallback "linkedin.com/jobs/search?keywords=Title%20Unknown" URL) is not
    #    an actionable posting. Real postings carry a company AND a direct link,
    #    so this never catches a legitimate role.
    url_lower = (job.get("url") or "").lower()
    if company_lower.strip() in ("", "unknown") and "/jobs/search" in url_lower:
        return "skipped", "No employer — constructed search link, not a real posting"

    # 1a. Never-pursue companies (gig platforms, policy excludes)
    for co in config.SKIP_COMPANIES_NEVER:
        if co.lower() in company_lower:
            return "skipped", f"Never-pursue company: {job['company']}"

    # 1b. Companies already in active pipeline
    for co in config.SKIP_COMPANIES:
        if co.lower() in company_lower:
            return "skipped", f"Already in pipeline: {job['company']}"

    # 2. Security clearance hard stop
    for kw in ["clearance required", "secret clearance", "ts/sci", "ts/sci"]:
        if kw in full_text:
            return "skipped", "Requires clearance"

    # 3. Title must match at least one include keyword.
    #    Per-sender title_include_override replaces the global list when set,
    #    preventing broad keywords like "analyst" from matching ops titles at
    #    specific senders (e.g. Siemens Healthineers).
    #
    #    For override senders, also check the body snippet as a fallback:
    #    talent-alert emails may have a generic subject but mention the role
    #    title in the opening paragraph (parse_single_job tries to extract it
    #    first; this check catches cases where extraction wasn't possible).
    sender_cfg = _get_sender_cfg(job.get("source", ""))
    if sender_cfg and "title_include_override" in sender_cfg:
        title_keywords = sender_cfg["title_include_override"]
        title_match = any(kw.lower() in title_lower for kw in title_keywords) or \
                      any(kw.lower() in full_text  for kw in title_keywords)
    else:
        title_keywords = config.INCLUDE_TITLE_KEYWORDS
        title_match = any(kw.lower() in title_lower for kw in title_keywords)
    if not title_match:
        return "skipped", "No matching title keyword"

    # 4. Seniority exclusions in title
    for kw in config.EXCLUDE_KEYWORDS:
        if kw.lower() in title_lower:
            return "skipped", f"Excluded keyword: '{kw.strip()}'"

    # 4b. Snippet/JD content exclusions — catch gig labeling roles regardless
    #     of company name. Checked against snippet + title combined.
    snippet_lower = job.get("snippet", "").lower()
    for kw in config.EXCLUDE_SNIPPET_KEYWORDS:
        if kw.lower() in snippet_lower or kw.lower() in title_lower:
            return "skipped", f"Excluded content: '{kw.strip()}'"

    # 4c. Experience years gate — skip if snippet mentions more years than
    #     Aidan can credibly claim. Checks for patterns like "5+ years",
    #     "7 years of experience", "minimum 5 years" etc.
    #     Only fires when the minimum years found exceeds the configured threshold.
    if config.MAX_YEARS_EXPERIENCE is not None:
        exp_matches = re.findall(
            r'(\d+)\+?\s*(?:or more\s*)?years?(?:\s+of)?(?:\s+relevant)?\s*(?:experience|exp\b)',
            snippet_lower
        )
        if exp_matches:
            min_years = min(int(y) for y in exp_matches)
            if min_years > config.MAX_YEARS_EXPERIENCE:
                return "skipped", f"Requires {min_years}+ years experience (exceeds {config.MAX_YEARS_EXPERIENCE}-year threshold)"

    # 5. "Lead" check — skip unless salary is clearly entry-level
    if config.EXCLUDE_LEAD and "lead" in title_lower:
        annual = salary_to_annual(job.get("salary"))
        if annual is None or annual >= config.LEAD_SALARY_KEEP_BELOW:
            return "skipped", "Excluded keyword: 'lead' (salary unclear or above threshold)"

    # 5b. Salary gate — demote to review when the stated ceiling is below target.
    #     Uses the upper bound of a range ("$60K-$90K" → 90K) so roles with a
    #     ceiling that meets the target are not penalised.
    #     Roles with no stated salary are left unchanged (don't punish transparency).
    annual_max = _annual_salary_max(job.get("salary"))
    if annual_max is not None and annual_max < config.SALARY_MINIMUM:
        return "review", (
            f"Salary {job['salary']} — ceiling below "
            f"${config.SALARY_MINIMUM // 1_000}K target"
        )

    # 6. Location check
    location_match = (
        job["location"] != ""
        or any(loc.lower() in (job["snippet"] + job["title"]).lower()
               for loc in config.INCLUDE_LOCATIONS)
    )

    if not location_match:
        # Preferred companies are known RTP targets — don't bury them in review
        # just because the email didn't include a parseable location string.
        if any(co.lower() in company_lower for co in config.PREFER_COMPANIES):
            return "pursue", None
        return "review", "Location not found — verify manually"

    return "pursue", None


# ---------------------------------------------------------------------------
# Digest writer
# ---------------------------------------------------------------------------

DIVIDER = "---"


def _job_block(job: dict, show_reason: bool = False, reason: str = None) -> str:
    lines = [f"### {job['title']} — {job['company']}"]
    lines.append(f"- **Location:** {job['location'] or '_(not listed)_'}")
    if job.get("salary"):
        lines.append(f"- **Salary:** {job['salary']}")
    lines.append(f"- **Source:** {job['source']}")
    if job.get("url"):
        lines.append(f"- **Link:** {job['url']}")
    lines.append(f"- **Alert date:** {job['date']}")
    if show_reason and reason:
        lines.append(f"- **Reason:** {reason}")
    if job.get("snippet"):
        lines.append(f"\n> {job['snippet'][:280]}")
    return "\n".join(lines)


def _build_html_digest(
    pursue: list[tuple],
    review: list[tuple],
    skipped: list[tuple],
    date_str: str,
) -> str:
    """Build a self-contained HTML digest with clickable links and localStorage persistence."""
    import json as _json

    def _job_dict(job, reason=None):
        return {
            "title":   job["title"],
            "company": job["company"],
            "location": job.get("location") or "",
            "salary":  job.get("salary") or None,
            "source":  job.get("source") or "",
            "reason":  reason or None,
            "url":     job.get("url") or "",
            "snippet": (job.get("snippet") or "")[:280],
        }

    data = {
        "pursue":  [_job_dict(j, r) for j, r in pursue],
        "review":  [_job_dict(j, r) for j, r in review],
        "skipped": [_job_dict(j, r) for j, r in skipped],
    }
    jobs_json = _json.dumps(data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Digest \u2014 {date_str}</title>
<style>
:root{{--pursue:#1a5c38;--pursue-bg:#eaf5ef;--pursue-border:#a8d8bc;--review:#7a4f00;--review-bg:#fef8ec;--review-border:#f5d98a;--skipped-bg:#f7f7f5;--skipped-border:#ddddd8;--applied-bg:#e8f0fb;--applied-border:#a8c0e8;--applied-text:#1a4a8a;--pass-bg:#f0f0ee;--pass-border:#ccc;--pass-text:#6b6b6b;--closed-bg:#fdf0f0;--closed-border:#e8a8a8;--closed-text:#8a1a1a;--font:'Georgia','Times New Roman',serif;--mono:'Courier New',monospace;--text:#1a1a18;--muted:#666660;--bg:#fafaf8;--card-bg:#fff;--border:#e0e0da}}
@media(prefers-color-scheme:dark){{:root{{--pursue:#6dd4a0;--pursue-bg:#0d2e1e;--pursue-border:#1e5c3a;--review:#f5d98a;--review-bg:#2a1f00;--review-border:#5c4400;--skipped-bg:#1e1e1c;--skipped-border:#333330;--applied-bg:#0d1e33;--applied-border:#1e3a5c;--applied-text:#7aafee;--pass-bg:#1a1a18;--pass-border:#333;--pass-text:#888;--closed-bg:#2e0d0d;--closed-border:#5c1e1e;--closed-text:#ee7a7a;--text:#e8e8e4;--muted:#999992;--bg:#111110;--card-bg:#1a1a18;--border:#2e2e2a}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--font);background:var(--bg);color:var(--text);font-size:15px;line-height:1.6;padding:2rem 1rem;max-width:860px;margin:0 auto}}
.header{{border-bottom:2px solid var(--border);padding-bottom:1.25rem;margin-bottom:2rem}}
.header h1{{font-size:1.6rem;font-weight:normal;letter-spacing:-.02em}}
.meta{{font-size:.8rem;color:var(--muted);font-family:var(--mono);margin-top:.4rem}}
.stats{{display:flex;gap:1.5rem;margin-top:1rem}}
.stat{{font-size:.8rem;font-family:var(--mono)}}.stat .num{{font-size:1.4rem;display:block;font-weight:bold;letter-spacing:-.02em}}
.stat.pursue .num{{color:var(--pursue)}}.stat.review .num{{color:var(--review)}}.stat.skipped .num{{color:var(--muted)}}
section{{margin-bottom:2.5rem}}
.section-header{{display:flex;align-items:baseline;gap:.75rem;margin-bottom:1rem;cursor:pointer;user-select:none}}
.section-header h2{{font-size:.75rem;font-family:var(--mono);text-transform:uppercase;letter-spacing:.1em;font-weight:normal}}
.section-header.pursue h2{{color:var(--pursue)}}.section-header.review h2{{color:var(--review)}}.section-header.skipped h2{{color:var(--muted)}}
.toggle{{font-size:.75rem;color:var(--muted);font-family:var(--mono)}}
.card{{border:1px solid var(--border);border-radius:6px;background:var(--card-bg);padding:1rem 1.25rem;margin-bottom:.75rem;transition:border-color .15s}}
.card:hover{{border-color:#aaa}}
.card.status-applied{{border-left:3px solid var(--applied-border);background:var(--applied-bg)}}
.card.status-pass{{opacity:.4}}
.card.status-later{{border-left:3px solid var(--review-border)}}
.card.status-closed{{opacity:.4;border-left:3px solid var(--closed-border);background:var(--closed-bg)}}
.card-title{{font-size:1rem;font-weight:bold;color:var(--text);text-decoration:none;line-height:1.3}}
.card-title:hover{{text-decoration:underline}}.card-title.no-link{{cursor:default}}
.company{{font-size:.85rem;color:var(--muted);margin-top:.2rem}}
.card-meta{{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.6rem;align-items:center}}
.pill{{font-family:var(--mono);font-size:.7rem;padding:.15rem .5rem;border-radius:3px;white-space:nowrap}}
.pill.location{{background:var(--skipped-bg);color:var(--muted);border:1px solid var(--skipped-border)}}
.pill.salary{{background:var(--pursue-bg);color:var(--pursue);border:1px solid var(--pursue-border)}}
.pill.source{{background:var(--skipped-bg);color:var(--muted);border:1px solid var(--skipped-border)}}
.pill.reason{{background:var(--review-bg);color:var(--review);border:1px solid var(--review-border)}}
.snippet{{font-size:.82rem;color:var(--muted);margin-top:.6rem;line-height:1.5;font-style:italic}}
.actions{{display:flex;gap:.5rem;margin-top:.75rem;padding-top:.6rem;border-top:1px solid var(--border)}}
.btn{{font-family:var(--mono);font-size:.7rem;padding:.25rem .6rem;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .12s}}
.btn:hover{{background:var(--skipped-bg);color:var(--text)}}
.btn.active-applied{{background:var(--applied-bg);color:var(--applied-text);border-color:var(--applied-border)}}
.btn.active-pass{{background:var(--pass-bg);color:var(--pass-text);border-color:var(--pass-border)}}
.btn.active-later{{background:var(--review-bg);color:var(--review);border-color:var(--review-border)}}
.btn.active-closed{{background:var(--closed-bg);color:var(--closed-text);border-color:var(--closed-border)}}
.collapsed{{display:none}}
.filter-bar{{display:flex;gap:.5rem;margin-bottom:1.5rem;flex-wrap:wrap}}
.filter-btn{{font-family:var(--mono);font-size:.72rem;padding:.3rem .75rem;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer}}
.filter-btn.active{{background:var(--text);color:var(--bg);border-color:var(--text)}}
</style>
</head>
<body>
<div class="header">
  <h1>Job Search Digest</h1>
  <div class="meta">Generated {date_str} &middot; Aidan Hennessy</div>
  <div class="stats">
    <div class="stat pursue"><span class="num" id="stat-pursue">0</span>pursue</div>
    <div class="stat review"><span class="num" id="stat-review">0</span>review</div>
    <div class="stat skipped"><span class="num" id="stat-skipped">0</span>skipped</div>
  </div>
</div>
<div class="filter-bar">
  <button class="filter-btn active" onclick="setFilter('all',this)">all</button>
  <button class="filter-btn" onclick="setFilter('pending',this)">pending</button>
  <button class="filter-btn" onclick="setFilter('applied',this)">applied</button>
  <button class="filter-btn" onclick="setFilter('later',this)">review later</button>
  <button class="filter-btn" onclick="setFilter('pass',this)">passed</button>
  <button class="filter-btn" onclick="setFilter('closed',this)">closed</button>
</div>
<section>
  <div class="section-header pursue" onclick="toggleSection('pursue')">
    <h2>Pursue</h2><span class="toggle" id="toggle-pursue">\u25be</span>
  </div>
  <div id="cards-pursue"></div>
</section>
<section>
  <div class="section-header review" onclick="toggleSection('review')">
    <h2>Review</h2><span class="toggle" id="toggle-review">\u25be</span>
  </div>
  <div id="cards-review"></div>
</section>
<section>
  <div class="section-header skipped" onclick="toggleSection('skipped')">
    <h2>Skipped</h2><span class="toggle" id="toggle-skipped">\u25b8</span>
  </div>
  <div id="cards-skipped" class="collapsed"></div>
</section>
<script>
const DATA={jobs_json};
const KEY='digest_{date_str}';
function load(){{try{{return JSON.parse(localStorage.getItem(KEY)||'{{}}');}}catch(e){{return {{}};}}}}
function save(id,s){{const d=load();d[id]=s;localStorage.setItem(KEY,JSON.stringify(d));}}
function jid(j){{return(j.title+'|'+j.company).toLowerCase().replace(/[^a-z0-9|]/g,'_');}}
let filter='all';
function setFilter(f,btn){{filter=f;document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');renderAll();}}
function toggleSection(s){{const c=document.getElementById('cards-'+s),t=document.getElementById('toggle-'+s);if(c.classList.contains('collapsed')){{c.classList.remove('collapsed');t.textContent='\u25be';}}else{{c.classList.add('collapsed');t.textContent='\u25b8';}}}}
function renderCards(jobs,cid){{
  const states=load(),el=document.getElementById(cid);
  el.innerHTML='';
  jobs.forEach(j=>{{
    const id=jid(j),st=states[id]||'pending';
    if(filter!=='all'&&st!==filter)return;
    const tEl=j.url?`<a class="card-title" href="${{j.url}}" target="_blank">${{j.title}}</a>`:`<span class="card-title no-link">${{j.title}}</span>`;
    const pills=[];
    if(j.location)pills.push(`<span class="pill location">${{j.location}}</span>`);
    if(j.salary)pills.push(`<span class="pill salary">${{j.salary}}</span>`);
    if(j.source)pills.push(`<span class="pill source">${{j.source}}</span>`);
    if(j.source==='NCWorks')pills.push(`<span class="pill reason">search manually on NCWorks</span>`);
    if(j.reason)pills.push(`<span class="pill reason">${{j.reason}}</span>`);
    const card=document.createElement('div');
    card.className=`card status-${{st}}`;
    card.innerHTML=`${{tEl}}<div class="company">${{j.company||'Unknown'}}</div>${{pills.length?`<div class="card-meta">${{pills.join('')}}</div>`:''}}${{j.snippet?`<div class="snippet">${{j.snippet}}</div>`:''}}<div class="actions"><button class="btn ${{st==='applied'?'active-applied':''}}" onclick="setStatus('${{id}}','applied')">&#10003; applied</button><button class="btn ${{st==='later'?'active-later':''}}" onclick="setStatus('${{id}}','later')">&#8635; review later</button><button class="btn ${{st==='pass'?'active-pass':''}}" onclick="setStatus('${{id}}','pass')">&#10005; pass</button><button class="btn ${{st==='closed'?'active-closed':''}}" onclick="setStatus('${{id}}','closed')">&#128274; closed</button><button class="btn" onclick="setStatus('${{id}}','pending')">&#8634; reset</button></div>`;
    el.appendChild(card);
  }});
}}
function setStatus(id,s){{save(id,s);renderAll();}}
function renderAll(){{
  renderCards(DATA.pursue,'cards-pursue');
  renderCards(DATA.review,'cards-review');
  renderCards(DATA.skipped,'cards-skipped');
  document.getElementById('stat-pursue').textContent=DATA.pursue.length;
  document.getElementById('stat-review').textContent=DATA.review.length;
  document.getElementById('stat-skipped').textContent=DATA.skipped.length;
}}
renderAll();
</script>
</body>
</html>"""


def claude_evaluate_jobs(
    pursue: list[tuple[dict, str]],
    review: list[tuple[dict, str]],
) -> tuple[list, list]:
    """Use Claude API to evaluate Pursue jobs for experience fit.

    For each Pursue job:
    1. Attempt to fetch the JD text from the job URL
    2. Pass title, company, location, snippet, and JD text (if available) to Claude
    3. Claude assesses whether the role is realistic for a recent MS Data Science
       grad with ~1 year academic/capstone experience
    4. Jobs flagged as requiring significantly more experience are moved to Review
       with Claude's explanation stored as the reason

    Cost: ~$0.02-0.03/day at typical Pursue volumes.
    """
    import requests as req_lib
    import json

    def _load_candidate_profile() -> str:
        """Load the candidate profile from disk so the evaluator grades each role
        against the current, richer profile (skills, fit signals, the honest
        inherited-multimodal framing, the explicit not-yet-earned skills) rather
        than a stale inline blurb. Canonical source is candidate_profile.md in
        PROJECT_DIR — the same file the standalone triage.py and scan.py read, so
        there is a single source of truth. Falls back to a minimal summary if the
        file is missing."""
        profile_path = os.path.join(config.PROJECT_DIR, "candidate_profile.md")
        try:
            with open(profile_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            log.warning(
                "candidate_profile.md not found at %s — using fallback summary",
                profile_path,
            )
            return (
                "Aidan is a recent MS Data Science graduate (American University, Dec 2025). "
                "He has approximately 1 year of professional DS internship experience: "
                "Data Science Intern at NetApp (May–Aug 2025, LDA topic modeling, >90% accuracy) and "
                "Operations Data Analyst Intern at NC Dept of Information Technology (May–Aug 2024, "
                "Power BI, Python, NLP). He also completed a capstone project analyzing 197,962 "
                "clinical ICU admissions (MIMIC-IV) using time-series ML models (54.5% F1). "
                "Skills: Python, R, SQL, scikit-learn, PyTorch, pandas, Tableau, Power BI, AWS, Snowflake, Git. "
                "No full-time industry employment. "
                "Target roles: entry-level DS/ML/AI positions, ideally in healthcare or tech."
            )

    CANDIDATE_CONTEXT = _load_candidate_profile()

    def fetch_jd_text(url: str) -> str:
        """Attempt to fetch job description text from URL. Returns empty string on failure."""
        if not url:
            if DEBUG:
                log.info("[debug] fetch_jd: no URL provided")
            return ""
        # Skip URLs that are known to block scraping
        blocked_domains = ["indeed.com", "linkedin.com", "glassdoor.com"]
        if any(d in url for d in blocked_domains):
            if DEBUG:
                log.info("[debug] fetch_jd: skipped (blocked domain) url=%s", url[:80])
            return ""
        try:
            r = req_lib.get(
                url,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")
            # Remove nav/footer/script noise
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            # Truncate to ~8000 chars: enough to include the requirements
            # section, which often sits below the job-summary preamble.
            result = text[:8000]
            if DEBUG:
                log.info("[debug] fetch_jd: url=%s chars=%d", url[:80], len(result))
            return result
        except Exception as e:
            if DEBUG:
                log.info("[debug] fetch_jd: failed url=%s error=%s", url[:80], e)
            return ""

    def evaluate_job(job: dict) -> tuple:
        """Ask Claude if this job is realistic for Aidan. Returns
        (is_fit, reason, years_required, jd_substantive, highlight, location_hard_blocker)."""
        # Prefer JD text resolved upstream (e.g. via Tavily for blocked sources);
        # fall back to the direct fetch when none was supplied.
        jd_text = job.get("jd_text") or fetch_jd_text(job.get("url", ""))
        # Did we actually have a real JD to judge, or only a title/snippet?
        # A role only earns Pursue if it was judged against substantive JD text.
        jd_substantive = len(jd_text or "") >= 400

        prompt = f"""You are helping evaluate whether a job posting is a realistic fit for a specific candidate.

CANDIDATE:
{CANDIDATE_CONTEXT}

JOB:
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Location: {job.get('location', '')}
Snippet: {job.get('snippet', '') or 'Not available'}
Job Description: {jd_text or 'Not available — assess based on title and company only'}

TASK:
Assess whether this role is realistic for Aidan given his background.
Focus specifically on:
1. Years of experience required — judge by what the JD EXPLICITLY states, not by the title. If the JD states a requirement of more than 2 years (e.g. "3+ years", "3 years", "minimum 3 years", "five years of experience"), this is NOT a fit. Requirements of 1-2 years are acceptable given Aidan's internship experience. Even "preferred" language with more than 2 years should be flagged as NOT a fit. BUT: if the JD describes the role as early-career, entry-level, new-grad, campus/university hire, or states no specific years requirement, treat it as a FIT — do NOT infer a higher requirement from the title or from seniority guesses. Set "years_required" only to a number the JD actually states; use null when the JD does not state one.
2. Program-specific eligibility requirements (e.g. "Duke MIDS graduates only", "current students only", specific degree from specific school) — these are hard blockers
3. Whether the company is a staffing/contracting firm posting on behalf of unknown clients
4. Any other hard blockers (clearance required, specific industry license, citizenship requirements, etc.)
5. Seniority signals — ONLY explicit title markers count: "Senior"/"Sr.", "Staff", "Lead", "Principal", "Manager", "Director", or a level suffix (II, III, IV, V). A plain title like "AI Engineer", "Agentic AI Engineer", "Data Scientist", or "ML Engineer" with no such marker spans all levels and is NOT inherently senior — defer to the JD text. Do not speculate about the experience or technical "depth" a role "likely" requires beyond what the JD explicitly states.
6. Job-board metadata fields — aggregator pages (Teal, LinkedIn, etc.) auto-populate structured fields like "Education Level", "Career Level", or "Job Type" that are frequently inaccurate or internally contradictory (e.g. "Career Level: Entry Level" shown right next to "Education Level: Ph.D."). A degree or education requirement counts as a hard blocker ONLY when the actual requirements/qualifications PROSE states it — never from a standalone "Education Level" field on its own, and especially NOT when an "Entry Level" / early-career marker is present. Treat an explicit "Entry Level" or "Career Level: Entry Level" marker as a fit signal, not a blocker.
7. Location hard blockers (see candidate profile's Location constraints section) — check the JD body text, not just the title/location field, for relocation requirements, a residency restriction that excludes North Carolina, or a hybrid schedule based outside the Research Triangle (Durham/Raleigh/Cary/Morrisville/Chapel Hill/RTP). If present, set "location_hard_blocker": true. A remote role with only an occasional on-site requirement (e.g. quarterly travel, periodic team offsites) is NOT a hard blocker — note the on-site cadence in "reason" instead and leave "location_hard_blocker": false; this should demote like any other not-a-fit role, not be treated as disqualifying.

IMPORTANT: If the job description text is unavailable or contains only navigation/UI markup rather than actual job content, set is_fit=true and confidence=low with reason explaining JD was unavailable. Do NOT move jobs to Review simply because the JD could not be fetched — only flag as not fit when you have clear evidence of a blocker.

Respond in JSON only with these fields:
{{
  "is_fit": true/false,
  "confidence": "high/medium/low",
  "reason": "one sentence explanation — if not fit, state the specific blocker",
  "years_required": number or null,
  "location_hard_blocker": true/false,
  "highlight": "SELECTIVE note, for a FIT role only — default to an EMPTY STRING. Populate it ONLY when there is something genuinely worth flagging to Aidan about this specific role: a standout reason to prioritize it (e.g. direct clinical/healthcare or MIMIC-IV alignment, an unusually strong match to his skills or stated interests) OR a real caveat worth watching despite the fit (e.g. a borderline experience requirement, an ambiguous seniority signal). For ordinary, unremarkable fits leave it empty — do NOT invent something to say. Under 18 words, concrete, specific to this role."
}}

Return ONLY valid JSON, no other text."""

        try:
            import urllib.request
            import ssl
            import os

            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                if DEBUG:
                    log.info("[debug] claude_eval: ANTHROPIC_API_KEY not set, skipping evaluation")
                return True, "", None, jd_substantive, "", False

            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            }).encode("utf-8")

            request = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": api_key,
                },
                method="POST"
            )

            ctx = ssl.create_default_context()
            try:
                with urllib.request.urlopen(request, context=ctx, timeout=15) as resp:
                    raw = resp.read().decode("utf-8")
                    if DEBUG:
                        log.info("[debug] claude_eval raw response for '%s': status=%s body=%s",
                                 job.get("title","?"), resp.status, raw[:200])
                    response_data = json.loads(raw)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                raise Exception(f"HTTP {e.code}: {body[:200]}")

            # Extract text content from response
            content = response_data.get("content", [])
            text = next((c["text"] for c in content if c.get("type") == "text"), "")
            # Strip markdown code fences if Claude wrapped the JSON
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]  # remove first line (```json)
                text = text.rsplit("```", 1)[0]  # remove trailing ```
            result = json.loads(text.strip())

            is_fit = result.get("is_fit", True)
            reason = result.get("reason", "")
            years = result.get("years_required")
            confidence = result.get("confidence", "low")
            location_hard_blocker = bool(result.get("location_hard_blocker", False))
            highlight = (result.get("highlight") or "").strip()
            if len(highlight) > 200:        # defensive cap; prompt asks for <18 words
                highlight = highlight[:200].rstrip()

            if DEBUG:
                log.info("[debug] claude_eval: '%s' -> fit=%s conf=%s reason=%s highlight=%s location_hard_blocker=%s",
                         job["title"], is_fit, confidence, reason, highlight, location_hard_blocker)

            return is_fit, reason, years, jd_substantive, highlight, location_hard_blocker

        except Exception as e:
            if DEBUG:
                log.info("[debug] claude_eval error for '%s': %s", job["title"], e)
            # On error, default to keeping in Pursue — never auto-skip on a parse/API failure.
            return True, "", None, jd_substantive, "", False

    new_pursue = []
    new_review = list(review)  # start with existing review items

    # Sources whose JD pages require a login or block scraping.
    # Claude will still evaluate these using the snippet from the email,
    # but we tag the result to flag that the full JD was unavailable.
    JD_BLOCKED_SOURCES = {"NCWorks", "Indeed"}

    for job, existing_reason in pursue:
        source = job.get("source", "")
        # A blocked source whose JD we resolved upstream (Tavily) is no longer
        # snippet-only — evaluate it normally instead of tagging it ⚠.
        jd_blocked = source in JD_BLOCKED_SOURCES and not job.get("jd_text")

        is_fit, claude_reason, years_req, jd_substantive, highlight, location_hard_blocker = evaluate_job(job)

        # An experience-gap demotion is routed to the Stretch tier (tagged
        # "Stretch:") rather than Review, so Review stays for other mismatches.
        # A location hard blocker (relocation required / non-NC residency
        # restriction / hybrid based outside the Triangle — see
        # candidate_profile.md's Location constraints section) is routed to
        # Skipped (tagged "Skip:") instead: it's a harder disqualifier than an
        # ordinary demotion and shouldn't sit in Review. Checked before the
        # Stretch test since a location blocker outranks an experience-gap
        # reason if Claude somehow flagged both. The engine reads these
        # prefixes to bucket the job. Threshold is config-driven.
        _stretch_threshold = getattr(config, "STRETCH_YEARS_THRESHOLD", 2)
        _is_stretch = (not is_fit) and (years_req is not None) and (years_req > _stretch_threshold)
        if location_hard_blocker:
            _demote_prefix = "Skip"
            _dest = "Skipped"
        elif _is_stretch:
            _demote_prefix = "Stretch"
            _dest = "Stretch"
        else:
            _demote_prefix = "Claude"
            _dest = "Review"

        if jd_blocked:
            # Always tag with warning regardless of Claude's verdict,
            # since evaluation was based on snippet only.
            warning = "\u26a0 JD not fetchable (snippet-only evaluation) — verify experience requirements manually"
            if not is_fit:
                # Claude found a blocker even from snippet alone — move to Review/Stretch
                new_review.append((job, f"{_demote_prefix}: {claude_reason}"))
                log.info("Moved to %s (Claude, snippet-only): %s \u2014 %s \u2014 %s",
                         _dest, job["title"], job["company"], claude_reason)
            else:
                # Claude found no blocker but only had snippet — keep in Pursue with warning
                new_pursue.append((job, warning))
                log.info("JD unavailable (%s): %s \u2014 %s \u2014 snippet-only evaluation, manual review recommended",
                         source, job["title"], job["company"])
        else:
            if is_fit and jd_substantive:
                # Keep in Pursue. Surface a note only when the evaluator flagged
                # something genuinely worth noting (a standout strength or a
                # caveat); otherwise the card stays clean (no reason-note).
                pursue_reason = f"Claude: {highlight}" if highlight else existing_reason
                new_pursue.append((job, pursue_reason))
            elif is_fit:
                # Claude found no blocker, but only had a title/snippet to judge
                # — not enough to confirm the experience bar. Verify manually.
                new_review.append((job, "Claude: unconfirmed — JD too thin to verify the experience requirement; check manually"))
                log.info("Moved to Review (unconfirmed JD): %s \u2014 %s",
                         job["title"], job["company"])
            else:
                # Move to review/stretch with Claude's reason
                new_review.append((job, f"{_demote_prefix}: {claude_reason}"))
                log.info("Moved to %s (Claude): %s \u2014 %s \u2014 %s",
                         _dest, job["title"], job["company"], claude_reason)

    # Re-sort review since we may have added items
    new_review.sort(key=lambda x: x[0]["title"].lower())

    return new_pursue, new_review


def write_digest(
    pursue: list[tuple],
    review: list[tuple],
    skipped: list[tuple],
    date: datetime,
    dry_run: bool = False,
) -> str:
    """Build the markdown digest and HTML viewer, writing both to the digests folder."""
    date_str = date.strftime("%Y-%m-%d")
    lines = [
        f"# Job Search Digest — {date_str}",
        "",
        f"Generated: {date.strftime('%Y-%m-%d %H:%M')}  ",
        f"**Pursue:** {len(pursue)}  |  **Review:** {len(review)}  |  **Skipped:** {len(skipped)}",
        "",
    ]

    # --- Pursue ---
    lines += [f"## Pursue ({len(pursue)} roles)", ""]
    if pursue:
        for job, reason in pursue:
            lines += [_job_block(job, show_reason=bool(reason), reason=reason or ""), "", DIVIDER, ""]
    else:
        lines += ["_No roles to pursue today._", ""]

    # --- Review ---
    lines += [f"## Review ({len(review)} roles)", ""]
    if review:
        for job, reason in review:
            lines += [_job_block(job, show_reason=True, reason=reason), "", DIVIDER, ""]
    else:
        lines += ["_No roles to review._", ""]

    # --- Skipped ---
    lines += [f"## Skipped ({len(skipped)} roles)", ""]
    if skipped:
        for job, reason in skipped:
            lines += [_job_block(job, show_reason=True, reason=reason), "", DIVIDER, ""]
    else:
        lines += ["_Nothing skipped._", ""]

    content = "\n".join(lines)

    if not dry_run:
        out_path = os.path.join(config.DIGESTS_DIR, f"{date_str}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("Digest written -> %s", out_path)

        html_path = os.path.join(config.DIGESTS_DIR, f"{date_str}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(_build_html_digest(pursue, review, skipped, date_str))
        log.info("HTML digest written -> %s", html_path)

    return content


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(items: list[tuple]) -> list[tuple]:
    """
    Merge (job, reason) pairs that share the same title + company.

    The first occurrence wins for field values; subsequent duplicates
    contribute any fields the first was missing and append their source
    label so the digest shows e.g. 'Source: Indeed, Duke Careers'.
    """
    index: dict[tuple[str, str], int] = {}   # (title_key, company_key) -> position
    result: list[tuple] = []

    for job, reason in items:
        key = (job["title"].lower().strip(), job["company"].lower().strip())

        if key in index:
            existing = result[index[key]][0]
            # Merge source labels (sorted for determinism)
            sources = sorted(
                set(existing["source"].split(", ")) | {job["source"]}
            )
            existing["source"] = ", ".join(sources)
            # Fill any missing fields from the duplicate
            if not existing.get("salary") and job.get("salary"):
                existing["salary"] = job["salary"]
            if not existing.get("location") and job.get("location"):
                existing["location"] = job["location"]
            if not existing.get("snippet") and job.get("snippet"):
                existing["snippet"] = job["snippet"]
        else:
            index[key] = len(result)
            result.append((job, reason))

    return result


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

def print_summary(pursue, review, skipped):
    sep = "=" * 60
    log.info("")
    log.info(sep)
    log.info("  JOB TRIAGE SUMMARY")
    log.info(sep)
    log.info("  PURSUE  (%d)", len(pursue))
    for job, _ in pursue:
        log.info("    • %s — %s (%s)", job["title"], job["company"], job["location"] or "location ?")
    log.info("")
    log.info("  REVIEW  (%d)", len(review))
    for job, reason in review:
        log.info("    • %s — %s  [%s]", job["title"], job["company"], reason)
    log.info("")
    log.info("  SKIPPED (%d)", len(skipped))
    for job, reason in skipped:
        log.info("    • %s — %s  [%s]", job["title"], job["company"], reason)
    log.info(sep)
    log.info("")



# ---------------------------------------------------------------------------
# Notes persistence helpers
# ---------------------------------------------------------------------------

def _notes_path() -> str:
    """Path to the persistent job notes JSON file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_notes.json")


def _load_notes() -> dict:
    """Load notes from disk. Returns empty dict if file missing or corrupt."""
    path = _notes_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_notes(notes: dict) -> None:
    """Persist notes to disk."""
    with open(_notes_path(), "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2, ensure_ascii=False)


def _job_id(job: dict) -> str:
    """Stable identifier for a job: title|company lowercased, non-alphanum -> _."""
    import re
    raw = f"{job.get('title','')}|{job.get('company','')}".lower()
    return re.sub(r"[^a-z0-9|]", "_", raw)


# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------

def serve_digest(pursue, review, skipped, host="0.0.0.0", port=5000):
    """Start a local Flask server serving the interactive digest with persistent notes."""
    try:
        from flask import Flask, request, jsonify, render_template_string
    except ImportError:
        log.error("Flask not installed. Run: pip install flask")
        return

    import json as _json

    app = Flask(__name__)

    # Build job data once
    def _job_dict(job, reason=None):
        return {
            "id":      _job_id(job),
            "title":   job["title"],
            "company": job["company"],
            "location": job.get("location") or "",
            "salary":  job.get("salary") or None,
            "source":  job.get("source") or "",
            "reason":  reason or None,
            "url":     job.get("url") or "",
            "snippet": (job.get("snippet") or "")[:280],
        }

    all_data = {
        "pursue":  [_job_dict(j, r) for j, r in pursue],
        "review":  [_job_dict(j, r) for j, r in review],
        "skipped": [_job_dict(j, r) for j, r in skipped],
    }

    @app.route("/")
    def index():
        notes = _load_notes()
        # Separate passed jobs out of pursue/review for the Passed section
        passed_ids = {jid for jid, n in notes.items() if n.get("status") == "pass"}
        data = {
            "pursue":  [j for j in all_data["pursue"]  if j["id"] not in passed_ids],
            "review":  [j for j in all_data["review"]  if j["id"] not in passed_ids],
            "passed":  [j for j in all_data["pursue"] + all_data["review"] if j["id"] in passed_ids],
            "skipped": all_data["skipped"],
        }
        return render_template_string(_SERVER_HTML, data=_json.dumps(data), notes=_json.dumps(notes))

    @app.route("/api/notes", methods=["GET"])
    def get_notes():
        return jsonify(_load_notes())

    @app.route("/api/notes", methods=["POST"])
    def post_notes():
        body = request.get_json(force=True)
        jid  = body.get("id")
        if not jid:
            return jsonify({"error": "missing id"}), 400
        notes = _load_notes()
        if jid not in notes:
            notes[jid] = {}
        if "status" in body:
            notes[jid]["status"] = body["status"]
        if "note" in body:
            notes[jid]["note"] = body["note"]
        _save_notes(notes)
        return jsonify({"ok": True})

    log.info("")
    log.info("=================================================================")
    log.info("  Digest server running at http://localhost:%d", port)
    log.info("  Press Ctrl+C to stop")
    log.info("=================================================================")
    import webbrowser, threading
    threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host=host, port=port, debug=False)


# ---------------------------------------------------------------------------
# Server HTML template
# ---------------------------------------------------------------------------

_SERVER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Digest — Aidan Hennessy</title>
<style>
:root{--pursue:#1a5c38;--pursue-bg:#eaf5ef;--pursue-border:#a8d8bc;--review:#7a4f00;--review-bg:#fef8ec;--review-border:#f5d98a;--passed-bg:#f0f0ee;--passed-border:#ccc;--passed-text:#6b6b6b;--skipped-bg:#f7f7f5;--skipped-border:#ddddd8;--applied-bg:#e8f0fb;--applied-border:#a8c0e8;--applied-text:#1a4a8a;--closed-bg:#fdf0f0;--closed-border:#e8a8a8;--closed-text:#8a1a1a;--font:'Georgia','Times New Roman',serif;--mono:'Courier New',monospace;--text:#1a1a18;--muted:#666660;--bg:#fafaf8;--card-bg:#fff;--border:#e0e0da}
@media(prefers-color-scheme:dark){:root{--pursue:#6dd4a0;--pursue-bg:#0d2e1e;--pursue-border:#1e5c3a;--review:#f5d98a;--review-bg:#2a1f00;--review-border:#5c4400;--passed-bg:#1a1a18;--passed-border:#333;--passed-text:#888;--skipped-bg:#1e1e1c;--skipped-border:#333330;--applied-bg:#0d1e33;--applied-border:#1e3a5c;--applied-text:#7aafee;--closed-bg:#2e0d0d;--closed-border:#5c1e1e;--closed-text:#ee7a7a;--text:#e8e8e4;--muted:#999992;--bg:#111110;--card-bg:#1a1a18;--border:#2e2e2a}}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);font-size:15px;line-height:1.6;padding:2rem 1rem;max-width:860px;margin:0 auto}
.header{border-bottom:2px solid var(--border);padding-bottom:1.25rem;margin-bottom:2rem}
.header h1{font-size:1.6rem;font-weight:normal;letter-spacing:-.02em}
.meta{font-size:.8rem;color:var(--muted);font-family:var(--mono);margin-top:.4rem}
.stats{display:flex;gap:1.5rem;margin-top:1rem}
.stat{font-size:.8rem;font-family:var(--mono)}.stat .num{font-size:1.4rem;display:block;font-weight:bold;letter-spacing:-.02em}
.stat.pursue .num{color:var(--pursue)}.stat.review .num{color:var(--review)}.stat.passed .num{color:var(--passed-text)}.stat.skipped .num{color:var(--muted)}
section{margin-bottom:2.5rem}
.section-header{display:flex;align-items:baseline;gap:.75rem;margin-bottom:1rem;cursor:pointer;user-select:none}
.section-header h2{font-size:.75rem;font-family:var(--mono);text-transform:uppercase;letter-spacing:.1em;font-weight:normal}
.section-header.pursue h2{color:var(--pursue)}.section-header.review h2{color:var(--review)}.section-header.passed h2{color:var(--passed-text)}.section-header.skipped h2{color:var(--muted)}
.toggle{font-size:.75rem;color:var(--muted);font-family:var(--mono)}
.card{border:1px solid var(--border);border-radius:6px;background:var(--card-bg);padding:1rem 1.25rem;margin-bottom:.75rem;transition:border-color .15s}
.card:hover{border-color:#aaa}
.card.status-applied{border-left:3px solid var(--applied-border);background:var(--applied-bg)}
.card.status-pass{opacity:.5;border-left:3px solid var(--passed-border)}
.card.status-later{border-left:3px solid var(--review-border)}
.card.status-closed{opacity:.4;border-left:3px solid var(--closed-border);background:var(--closed-bg)}
.card-title{font-size:1rem;font-weight:bold;color:var(--text);text-decoration:none;line-height:1.3}
.card-title:hover{text-decoration:underline}.card-title.no-link{cursor:default}
.company{font-size:.85rem;color:var(--muted);margin-top:.2rem}
.card-meta{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.6rem;align-items:center}
.pill{font-family:var(--mono);font-size:.7rem;padding:.15rem .5rem;border-radius:3px;white-space:nowrap}
.pill.location{background:var(--skipped-bg);color:var(--muted);border:1px solid var(--skipped-border)}
.pill.salary{background:var(--pursue-bg);color:var(--pursue);border:1px solid var(--pursue-border)}
.pill.source{background:var(--skipped-bg);color:var(--muted);border:1px solid var(--skipped-border)}
.pill.reason{background:var(--review-bg);color:var(--review);border:1px solid var(--review-border)}
.snippet{font-size:.82rem;color:var(--muted);margin-top:.6rem;line-height:1.5;font-style:italic}
.actions{display:flex;gap:.5rem;margin-top:.75rem;padding-top:.6rem;border-top:1px solid var(--border);flex-wrap:wrap;align-items:flex-start}
.btn{font-family:var(--mono);font-size:.7rem;padding:.25rem .6rem;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .12s}
.btn:hover{background:var(--skipped-bg);color:var(--text)}
.btn.active-applied{background:var(--applied-bg);color:var(--applied-text);border-color:var(--applied-border)}
.btn.active-pass{background:var(--passed-bg);color:var(--passed-text);border-color:var(--passed-border)}
.btn.active-later{background:var(--review-bg);color:var(--review);border-color:var(--review-border)}
.btn.active-closed{background:var(--closed-bg);color:var(--closed-text);border-color:var(--closed-border)}
.note-wrap{flex:1;min-width:200px}
.note-input{width:100%;font-family:var(--mono);font-size:.72rem;padding:.3rem .5rem;border:1px solid var(--border);border-radius:3px;background:var(--card-bg);color:var(--text);resize:vertical;min-height:2.4rem}
.note-input:focus{outline:none;border-color:#888}
.note-saved{font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-top:.2rem;height:.9rem;transition:opacity .3s}
.collapsed{display:none}
.filter-bar{display:flex;gap:.5rem;margin-bottom:1.5rem;flex-wrap:wrap}
.filter-btn{font-family:var(--mono);font-size:.72rem;padding:.3rem .75rem;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer}
.filter-btn.active{background:var(--text);color:var(--bg);border-color:var(--text)}
</style>
</head>
<body>
<div class="header">
  <h1>Job Search Digest</h1>
  <div class="meta">Aidan Hennessy &middot; Notes auto-save</div>
  <div class="stats">
    <div class="stat pursue"><span class="num" id="stat-pursue">0</span>pursue</div>
    <div class="stat review"><span class="num" id="stat-review">0</span>review</div>
    <div class="stat passed"><span class="num" id="stat-passed">0</span>passed</div>
    <div class="stat skipped"><span class="num" id="stat-skipped">0</span>skipped</div>
  </div>
</div>
<div class="filter-bar">
  <button class="filter-btn active" onclick="setFilter('all',this)">all</button>
  <button class="filter-btn" onclick="setFilter('pending',this)">pending</button>
  <button class="filter-btn" onclick="setFilter('applied',this)">applied</button>
  <button class="filter-btn" onclick="setFilter('later',this)">review later</button>
  <button class="filter-btn" onclick="setFilter('closed',this)">closed</button>
</div>
<section>
  <div class="section-header pursue" onclick="toggleSection('pursue')">
    <h2>Pursue</h2><span class="toggle" id="toggle-pursue">&#9662;</span>
  </div>
  <div id="cards-pursue"></div>
</section>
<section>
  <div class="section-header review" onclick="toggleSection('review')">
    <h2>Review</h2><span class="toggle" id="toggle-review">&#9662;</span>
  </div>
  <div id="cards-review"></div>
</section>
<section>
  <div class="section-header passed" onclick="toggleSection('passed')">
    <h2>Passed</h2><span class="toggle" id="toggle-passed">&#9658;</span>
  </div>
  <div id="cards-passed" class="collapsed"></div>
</section>
<section>
  <div class="section-header skipped" onclick="toggleSection('skipped')">
    <h2>Skipped</h2><span class="toggle" id="toggle-skipped">&#9658;</span>
  </div>
  <div id="cards-skipped" class="collapsed"></div>
</section>
<script>
const DATA = {{ data|safe }};
let NOTES = {{ notes|safe }};
let filter = 'all';

function setFilter(f, btn) {
  filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderAll();
}

function toggleSection(s) {
  const c = document.getElementById('cards-' + s);
  const t = document.getElementById('toggle-' + s);
  if (c.classList.contains('collapsed')) {
    c.classList.remove('collapsed');
    t.innerHTML = '&#9662;';
  } else {
    c.classList.add('collapsed');
    t.innerHTML = '&#9658;';
  }
}

async function setStatus(id, s) {
  if (!NOTES[id]) NOTES[id] = {};
  NOTES[id].status = s;
  await fetch('/api/notes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, status: s})
  });
  if (s === 'pass') {
    // Move from pursue/review to passed
    const job = [...DATA.pursue, ...DATA.review].find(j => j.id === id);
    if (job && !DATA.passed.find(j => j.id === id)) {
      DATA.passed.push(job);
      DATA.pursue = DATA.pursue.filter(j => j.id !== id);
      DATA.review = DATA.review.filter(j => j.id !== id);
    }
  }
  renderAll();
}

let noteTimers = {};
async function saveNote(id, val) {
  if (!NOTES[id]) NOTES[id] = {};
  NOTES[id].note = val;
  clearTimeout(noteTimers[id]);
  noteTimers[id] = setTimeout(async () => {
    await fetch('/api/notes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, note: val})
    });
    const el = document.getElementById('saved-' + id);
    if (el) { el.textContent = 'saved'; setTimeout(() => { el.textContent = ''; }, 1500); }
  }, 600);
}

function renderCards(jobs, cid, showPassBtn=true) {
  const el = document.getElementById(cid);
  el.innerHTML = '';
  let shown = 0;
  jobs.forEach(j => {
    const n = NOTES[j.id] || {};
    const st = n.status || 'pending';
    if (filter !== 'all' && st !== filter) return;
    shown++;
    const tEl = j.url
      ? `<a class="card-title" href="${j.url}" target="_blank">${j.title}</a>`
      : `<span class="card-title no-link">${j.title}</span>`;
    const pills = [];
    if (j.location) pills.push(`<span class="pill location">${j.location}</span>`);
    if (j.salary)   pills.push(`<span class="pill salary">${j.salary}</span>`);
    if (j.source)   pills.push(`<span class="pill source">${j.source}</span>`);
    if (j.reason)   pills.push(`<span class="pill reason">${j.reason}</span>`);
    const noteVal = (n.note || '').replace(/"/g, '&quot;');
    const card = document.createElement('div');
    card.className = `card status-${st}`;
    card.innerHTML = `
      ${tEl}
      <div class="company">${j.company || 'Unknown'}</div>
      ${pills.length ? `<div class="card-meta">${pills.join('')}</div>` : ''}
      ${j.snippet ? `<div class="snippet">${j.snippet}</div>` : ''}
      <div class="actions">
        <button class="btn ${st==='applied'?'active-applied':''}" onclick="setStatus('${j.id}','applied')">&#10003; applied</button>
        <button class="btn ${st==='later'?'active-later':''}" onclick="setStatus('${j.id}','later')">&#8635; review later</button>
        ${showPassBtn ? `<button class="btn ${st==='pass'?'active-pass':''}" onclick="setStatus('${j.id}','pass')">&#10005; pass</button>` : ''}
        <button class="btn ${st==='closed'?'active-closed':''}" onclick="setStatus('${j.id}','closed')">&#128274; closed</button>
        <button class="btn" onclick="setStatus('${j.id}','pending')">&#8634; reset</button>
        <div class="note-wrap">
          <textarea class="note-input" placeholder="Notes..." oninput="saveNote('${j.id}',this.value)">${noteVal}</textarea>
          <div class="note-saved" id="saved-${j.id}"></div>
        </div>
      </div>`;
    el.appendChild(card);
  });
  return shown;
}

function renderAll() {
  renderCards(DATA.pursue,  'cards-pursue',  true);
  renderCards(DATA.review,  'cards-review',  true);
  renderCards(DATA.passed,  'cards-passed',  false);
  renderCards(DATA.skipped, 'cards-skipped', false);
  document.getElementById('stat-pursue').textContent  = DATA.pursue.length;
  document.getElementById('stat-review').textContent  = DATA.review.length;
  document.getElementById('stat-passed').textContent  = DATA.passed.length;
  document.getElementById('stat-skipped').textContent = DATA.skipped.length;
}
renderAll();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global DEBUG

    parser = argparse.ArgumentParser(description="Job-search email triage")
    parser.add_argument("--hours",   type=int, default=24,  help="Look back this many hours (default: 24)")
    parser.add_argument("--dry-run", action="store_true",   help="Skip writing the digest file")
    parser.add_argument("--debug",   action="store_true",   help="Dump every email found before filtering")
    parser.add_argument("--serve",   action="store_true",   help="Run triage then start Flask server for interactive review")
    args = parser.parse_args()

    if args.debug:
        DEBUG = True
        log.info("*** DEBUG MODE — full email inventory will print before filtering ***")

    log.info("Authenticating with Gmail…")
    service = get_gmail_service()

    query = build_query(args.hours)   # query is also logged inside build_query

    messages = fetch_messages(service, query)
    log.info("Found %d matching email(s)", len(messages))

    all_jobs: list[dict] = []
    for msg in messages:
        try:
            jobs = extract_jobs(msg)
            all_jobs.extend(jobs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse message %s: %s", msg.get("id"), exc)

    log.info("Extracted %d job listing(s) from emails", len(all_jobs))

    # ---------------------------------------------------------------------------
    # NCWorks dedup — remove NCWorks jobs where the same title+company already
    # exists from a direct employer source (Duke Careers, UNC Chapel Hill).
    # The direct source version has a better URL and is authoritative.
    # ---------------------------------------------------------------------------
    def normalize_title(t: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]", "", t.lower().strip())

    direct_keys: set[str] = set()
    for job in all_jobs:
        if job.get("source") in ("Duke Careers", "UNC Chapel Hill"):
            key = normalize_title(job.get("title", "")) + "|" + normalize_title(job.get("company", ""))
            direct_keys.add(key)

    before_dedup = len(all_jobs)
    all_jobs = [
        job for job in all_jobs
        if not (
            job.get("source") == "NCWorks"
            and (normalize_title(job.get("title", "")) + "|" + normalize_title(job.get("company", ""))) in direct_keys
        )
    ]
    removed = before_dedup - len(all_jobs)
    if removed > 0:
        log.info("NCWorks dedup: removed %d duplicate(s) already present from direct sources", removed)

    pursue, review, skipped = [], [], []
    for job in all_jobs:
        category, reason = classify_job(job)
        if DEBUG:
            log.info(
                "[debug] classify: %r -> %s%s",
                job["title"], category.upper(), f"  [{reason}]" if reason else "",
            )
        if category == "pursue":
            pursue.append((job, reason))
        elif category == "review":
            review.append((job, reason))
        else:
            skipped.append((job, reason))

    # Deduplicate: same title+company from multiple sources -> one entry, merged sources
    pursue  = deduplicate(pursue)
    review  = deduplicate(review)
    skipped = deduplicate(skipped)

    # Sort pursue/review alphabetically by title for readability
    pursue.sort(key=lambda x: x[0]["title"].lower())
    review.sort(key=lambda x: x[0]["title"].lower())

    # Resolve NCWorks tracking URLs — only for pursue/review jobs to avoid
    # making 148 HTTP requests for jobs that were skipped anyway.
    def resolve_ncworks_url(tracking_url: str, title: str, company: str) -> str:
        import requests
        from urllib.parse import quote
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
            if "JobSearchCriteriaQuick" in final or "ncworks.gov" not in final:
                return _ncworks_search_url(title, company)
            return final
        except Exception:
            return _ncworks_search_url(title, company)

    def _ncworks_search_url(title: str, company: str) -> str:
        """Return a useful fallback URL based on the employer name."""
        from urllib.parse import quote
        company_lower = company.lower()
        title_enc = quote(title)

        # Duke — careers.duke.edu supports keyword search without login
        if "duke" in company_lower:
            return f"https://careers.duke.edu/search/?q={title_enc}"

        # UNC Chapel Hill — unc.edu careers search
        if "north carolina at chapel hill" in company_lower or "unc chapel hill" in company_lower:
            return f"https://unc.peopleadmin.com/postings/search?query={title_enc}"

        # NC State
        if "north carolina state" in company_lower or "nc state" in company_lower:
            return f"https://jobs.ncsu.edu/postings/search?query={title_enc}"

        # For all other NCWorks employers — no reliable public URL, return empty
        return ""

    for job_list in (pursue, review):
        for job, _ in job_list:
            if job.get("source") == "NCWorks":
                if DEBUG:
                    log.info("[debug] resolving NCWorks URL for: %s", job["title"])
                job["url"] = resolve_ncworks_url(
                    job.get("url", ""),
                    job.get("title", ""),
                    job.get("company", ""),
                )

    # ---------------------------------------------------------------------------
    # Claude JD evaluation — for each Pursue job, fetch the JD if possible and
    # ask Claude to assess experience requirements and fit for a recent MS grad.
    # Jobs that require significantly more experience than Aidan has are moved
    # from Pursue to Review with an explanation.
    # ---------------------------------------------------------------------------
    if not args.dry_run:
        pursue, review = claude_evaluate_jobs(pursue, review)

    print_summary(pursue, review, skipped)
    write_digest(pursue, review, skipped, datetime.now(), dry_run=args.dry_run)

    if args.serve:
        serve_digest(pursue, review, skipped)


if __name__ == "__main__":
    main()
