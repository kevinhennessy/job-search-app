"""
scanner.py — proactive job-portal scanner for the web app.

Ports the portal-query logic from the standalone scan.py (Workday / Greenhouse /
SmartRecruiters public APIs). The CLI, the scan_seen.json store, and the
HTML/markdown digest writers from scan.py are intentionally NOT ported: the
app's SQLite Job table (stable_id upsert) handles persistence and cross-run
dedup, and engine.run_scan feeds scanned jobs through the same
classify -> JD-resolve -> evaluate -> persist pipeline as the email run.

Each scanned job is tagged source="Scanner/<company>" so it is self-identifying
in the same job list as email-sourced roles. Imported lazily by engine.run_scan
so the FastAPI app starts without requests/bs4 installed.
"""

from __future__ import annotations

import logging
import re  # noqa: F401  (kept for parity with ported helpers)
from datetime import datetime, timedelta, timezone  # noqa: F401

import requests
from bs4 import BeautifulSoup  # noqa: F401  (used by scan_greenhouse)

from . import config

DEBUG = False
log = logging.getLogger("scanner")

PORTALS = [

    # ------------------------------------------------------------------
    # WORKDAY — Healthcare / Life Sciences
    # ------------------------------------------------------------------
    {
        "type": "workday",
        "name": "Fortrea",
        "subdomain": "fortrea",
        "wd_number": "wd1",
        "job_site": "Fortrea",
        "search_terms": ["data scientist", "data analyst", "machine learning", "AI engineer"],
        "location": "Durham",
    },
    {
        "type": "workday",
        "name": "Syneos Health",
        "subdomain": "syneoshealth",
        "wd_number": "wd12",
        "job_site": "Syneos_Health_External_Site",
        "search_terms": ["data scientist", "data analyst", "machine learning", "biostatistician"],
        "location": "",
    },
    {
        "type": "workday",
        "name": "Pfizer",
        "subdomain": "pfizer",
        "wd_number": "wd1",
        "job_site": "PfizerCareers",
        "search_terms": ["data scientist", "data analyst", "machine learning", "AI engineer"],
        "location": "Durham",
    },
    {
        "type": "workday",
        "name": "GSK",
        "subdomain": "gsk",
        "wd_number": "wd5",
        "job_site": "GSKCareers",
        "search_terms": ["data scientist", "data analyst", "machine learning"],
        "location": "Durham",
    },
    {
        "type": "workday",
        "name": "Thermo Fisher Scientific",
        "subdomain": "thermofisher",
        "wd_number": "wd5",
        "job_site": "ThermoFisherCareers",
        "search_terms": ["data scientist", "data analyst", "machine learning", "AI engineer"],
        "location": "Morrisville",
    },
    # NOTE: Novo Nordisk uses careers.novonordisk.com (Phenom/custom ATS),
    # not a queryable Workday endpoint. Deferred to Phase 2.
    # {
    #     "type": "workday",
    #     "name": "Novo Nordisk",
    #     ...
    # },

    # ------------------------------------------------------------------
    # WORKDAY — Technology
    # ------------------------------------------------------------------
    {
        "type": "workday",
        "name": "Red Hat",
        "subdomain": "redhat",
        "wd_number": "wd5",
        "job_site": "Jobs",
        "search_terms": ["data scientist", "data analyst", "machine learning", "data science"],
        "location": "Raleigh",
    },
    {
        "type": "workday",
        "name": "Cisco",
        "subdomain": "cisco",
        "wd_number": "wd5",
        "job_site": "Cisco_Careers",
        "search_terms": ["data scientist", "machine learning engineer", "data science", "AI engineer"],
        "location": "Research Triangle",
    },

    # NOTE: MetLife uses a custom ATS (metlifecareers.com), not Workday.
    # First Citizens Bank Workday slug needs verification.
    # Both are deferred to Phase 2 (custom ATS / Brave Search layer).
    # {
    #     "type": "workday",
    #     "name": "MetLife",
    #     ...
    # },
    # {
    #     "type": "workday",
    #     "name": "First Citizens Bank",
    #     ...
    # },

    # ------------------------------------------------------------------
    # GREENHOUSE
    # ------------------------------------------------------------------
    {
        "type": "greenhouse",
        "name": "Pendo",
        "slug": "pendo",
        "search_terms": ["data scientist", "data analyst", "machine learning", "analytics"],
    },
    {
        "type": "greenhouse",
        "name": "Bandwidth",
        "slug": "bandwidth",
        "search_terms": ["data scientist", "data analyst", "machine learning", "analytics"],
    },

    # ------------------------------------------------------------------
    # SMARTRECRUITERS
    # ------------------------------------------------------------------
    {
        "type": "smartrecruiters",
        "name": "NetApp",
        "company_id": "NetApp2",
        "search_terms": ["data scientist", "data analyst", "machine learning", "AI"],
        "location": "Morrisville",
    },
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})


def _get(url: str, params: dict = None, timeout: int = 10) -> dict | None:
    """GET JSON from url, returning None on failure."""
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            log.info("[debug] GET failed: %s  error=%s", url, e)
        return None


def _post(url: str, payload: dict, timeout: int = 10) -> dict | None:
    """POST JSON to url, returning None on failure."""
    try:
        r = SESSION.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            log.info("[debug] POST failed: %s  error=%s", url, e)
        return None


# ---------------------------------------------------------------------------
# Workday scanner
# ---------------------------------------------------------------------------

def scan_workday(portal: dict, search_term: str) -> list[dict]:
    """
    Query a Workday tenant's public job search JSON endpoint.

    Workday exposes: POST https://{subdomain}.{wd_number}.myworkdayjobs.com/
                              wday/cxs/{subdomain}/{job_site}/jobs
    with a JSON body containing searchText and optional locationHierarchy filters.
    Returns up to 20 results per call (Workday's default page size).
    """
    subdomain = portal["subdomain"]
    wd_number = portal["wd_number"]
    job_site  = portal["job_site"]

    url = (
        f"https://{subdomain}.{wd_number}.myworkdayjobs.com"
        f"/wday/cxs/{subdomain}/{job_site}/jobs"
    )

    payload = {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": search_term,
    }

    # Add location as a free-text refinement via searchText if specified
    location = portal.get("location", "")
    if location:
        payload["searchText"] = f"{search_term} {location}"

    data = _post(url, payload)
    if not data:
        return []

    jobs = []
    for item in data.get("jobPostings", []):
        title   = item.get("title", "").strip()
        ext_url = item.get("externalPath", "")
        posted  = item.get("postedOn", "")  # e.g. "Posted 3 Days Ago"

        if not title:
            continue

        # Build full URL
        full_url = (
            f"https://{subdomain}.{wd_number}.myworkdayjobs.com"
            f"/en-US/{job_site}{ext_url}"
            if ext_url else ""
        )

        # Extract location from locationsText
        loc_text = item.get("locationsText", "") or ""
        location_found = _match_location(loc_text + " " + title)

        # Skip international roles
        if not _is_us_or_remote(loc_text):
            if DEBUG:
                log.info("[debug] workday skip (non-US): %r  loc=%r", title, loc_text)
            continue

        # Workday returns "N Locations" for multi-location jobs instead of
        # actual location strings. For global CROs with predominantly
        # international postings, skip unverifiable multi-location jobs.
        # For tech/pharma companies with strong US presence, allow through.
        GLOBAL_CRO_PORTALS = {"Syneos Health", "Fortrea", "IQVIA", "Parexel"}
        if re.match(r'^\d+ Locations?$', loc_text, re.IGNORECASE):
            if portal["name"] in GLOBAL_CRO_PORTALS:
                # Try to verify US presence from locations array
                raw_locations = item.get("locations", []) or []
                if raw_locations:
                    combined = " ".join(
                        f"{l.get('countryIsoCode','')} {l.get('city','')} {l.get('state','')}"
                        for l in raw_locations
                    )
                    has_us = any(
                        s in combined.upper()
                        for s in ["USA", " NC", " TX", " CA", " NY", " FL",
                                  " GA", " VA", " MD", " DC", " MA", " IL"]
                    )
                    if not has_us:
                        if DEBUG:
                            log.info("[debug] workday skip (CRO multi-loc, no US): %r", title)
                        continue
                    loc_text = combined.strip()
                else:
                    # No location detail — skip for CROs
                    if DEBUG:
                        log.info("[debug] workday skip (CRO multi-loc, unverifiable): %r", title)
                    continue
            # For non-CRO portals, allow multi-location jobs through unchanged

        jobs.append({
            "title":    title,
            "company":  portal["name"],
            "location": location_found or loc_text[:60],
            "salary":   None,
            "source":   f"Scanner/{portal['name']}",
            "snippet":  item.get("jobDescription", "")[:280] if item.get("jobDescription") else "",
            "url":      full_url,
            "date":     datetime.now().strftime("%Y-%m-%d"),
        })

    if DEBUG:
        log.info("[debug] workday %s / %r -> %d result(s)", portal["name"], search_term, len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# Greenhouse scanner
# ---------------------------------------------------------------------------

def scan_greenhouse(portal: dict, search_term: str) -> list[dict]:
    """
    Query the Greenhouse public jobs API.
    Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
    Returns all open jobs; we filter client-side by search_term.
    """
    slug = portal["slug"]
    url  = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    data = _get(url, params={"content": "true"})
    if not data:
        return []

    term_lower = search_term.lower()
    jobs = []

    for item in data.get("jobs", []):
        title = item.get("title", "").strip()
        if not title:
            continue

        # Client-side keyword filter
        if term_lower not in title.lower():
            continue

        location = item.get("location", {}).get("name", "") or ""
        location_found = _match_location(location + " " + title)

        # Skip international roles
        if not _is_us_or_remote(location):
            if DEBUG:
                log.info("[debug] greenhouse skip (non-US): %r  loc=%r", title, location)
            continue

        # Greenhouse absolute URL
        gh_url = item.get("absolute_url", "")

        # Snippet from content if available
        snippet = ""
        content = item.get("content", "")
        if content:
            text = BeautifulSoup(content, "lxml").get_text(separator=" ", strip=True)
            snippet = text[:280]

        jobs.append({
            "title":    title,
            "company":  portal["name"],
            "location": location_found or location[:60],
            "salary":   None,
            "source":   f"Scanner/{portal['name']}",
            "snippet":  snippet,
            "url":      gh_url,
            "date":     datetime.now().strftime("%Y-%m-%d"),
        })

    if DEBUG:
        log.info("[debug] greenhouse %s / %r -> %d result(s)", portal["name"], search_term, len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# SmartRecruiters scanner
# ---------------------------------------------------------------------------

def scan_smartrecruiters(portal: dict, search_term: str) -> list[dict]:
    """
    Query the SmartRecruiters public job postings API.
    Endpoint: GET https://api.smartrecruiters.com/v1/companies/{id}/postings
    """
    company_id = portal["company_id"]
    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"

    params = {
        "q": search_term,
        "limit": 20,
    }
    location = portal.get("location", "")
    if location:
        params["location"] = location

    data = _get(url, params=params)
    if not data:
        return []

    jobs = []
    for item in data.get("content", []):
        title = item.get("name", "").strip()
        if not title:
            continue

        loc_obj   = item.get("location", {}) or {}
        city      = loc_obj.get("city", "") or ""
        remote    = item.get("typeOfEmployment", {}).get("label", "")
        loc_text  = city or remote or ""
        location_found = _match_location(loc_text + " " + title)

        sr_url = item.get("ref", "")  # direct posting URL

        jobs.append({
            "title":    title,
            "company":  portal["name"],
            "location": location_found or loc_text[:60],
            "salary":   None,
            "source":   f"Scanner/{portal['name']}",
            "snippet":  item.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "")[:280] if isinstance(item.get("jobAd"), dict) else "",
            "url":      sr_url,
            "date":     datetime.now().strftime("%Y-%m-%d"),
        })

    if DEBUG:
        log.info("[debug] smartrecruiters %s / %r -> %d result(s)", portal["name"], search_term, len(jobs))

    return jobs


# ---------------------------------------------------------------------------
# Location matcher (reuses config.INCLUDE_LOCATIONS)
# ---------------------------------------------------------------------------

def _is_us_or_remote(loc_text: str) -> bool:
    """Return True if a location string appears to be US-based or remote."""
    if not loc_text:
        return True  # no location = assume US or remote, let classify_job handle it
    loc_lower = loc_text.lower()
    # Explicit remote signals
    if "remote" in loc_lower or "work from home" in loc_lower:
        return True
    # Known international signals — reject these
    international_signals = [
        # Countries / regions
        "budapest", "hungary", "buenos aires", "argentina", "hyderabad", "india",
        "serbia", "romania", "spain", "poland", "netherlands", "uk only", "emea",
        "london", "manchester", "paris", "france", "germany", "australia",
        "singapore", "japan", "china", "canada", "brazil", "mexico", "toronto",
        # Common non-US patterns
        "homebased", "home-based",
    ]
    for signal in international_signals:
        if signal in loc_lower:
            return False
    # If it contains a US state abbreviation pattern (", NC", ", CA" etc) it's US
    if re.search(r",\s*[A-Z]{2}$", loc_text.strip()):
        return True
    # Otherwise allow through — better to get a false positive than miss a role
    return True


def _match_location(text: str) -> str:
    """Return the first matching target location found in text, or empty string."""
    text_lower = text.lower()
    for loc in config.INCLUDE_LOCATIONS:
        if loc.lower() in text_lower:
            return loc.title()
    return ""


def scanner_location_ok(job: dict) -> bool:
    """Return False if job has a US location clearly outside target area."""
    loc = (job.get("location") or "").lower()
    if not loc:
        return True  # no location, let classify_job decide
    # Target locations are fine
    for target in config.INCLUDE_LOCATIONS:
        if target.lower() in loc:
            return True
    # Common US non-target cities/states to reject
    non_target = [
        "sunnyvale", "san jose", "san francisco", "seattle", "new york",
        "chicago", "boston", "austin", "atlanta", "denver", "phoenix",
        "los angeles", "dallas", "houston", "minneapolis", "portland",
        "salt lake", "pittsburgh", "detroit", "columbus", "indianapolis",
    ]
    for city in non_target:
        if city in loc:
            return False
    return True  # unknown US location, allow through


def scan_all_portals() -> list[dict]:
    """Query every configured portal across its search terms and return a
    within-run de-duplicated list of job dicts in the shape the engine expects
    (title/company/location/source/url/snippet/date). Per-portal network or
    parse errors are logged and skipped so one bad portal never fails the run.
    Cross-run dedup is handled downstream by the Job table's stable_id upsert."""
    all_jobs: list[dict] = []
    for portal in PORTALS:
        ptype = portal["type"]
        for term in portal["search_terms"]:
            try:
                if ptype == "workday":
                    results = scan_workday(portal, term)
                elif ptype == "greenhouse":
                    results = scan_greenhouse(portal, term)
                elif ptype == "smartrecruiters":
                    results = scan_smartrecruiters(portal, term)
                else:
                    log.warning("Unknown portal type: %s", ptype)
                    continue
                all_jobs.extend(results)
            except Exception as e:  # noqa: BLE001
                log.warning("Error scanning %s / %r: %s", portal["name"], term, e)

    log.info("Scanner: %d raw result(s) from all portals", len(all_jobs))

    seen: set[str] = set()
    deduped: list[dict] = []
    for job in all_jobs:
        k = (job.get("title", "").strip().lower() + "|"
             + job.get("company", "").strip().lower())
        if k not in seen:
            seen.add(k)
            deduped.append(job)
    log.info("Scanner: %d result(s) after within-run dedup", len(deduped))
    return deduped
