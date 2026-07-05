"""
config.py — Filter rules, sender definitions, and path configuration
for the job search email triage tool.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# PROJECT_DIR holds Gmail credentials (credentials.json / token.json) and the
# canonical candidate_profile.md. Defaults to Kevin's existing job-search folder;
# override with the JOBSEARCH_DIR env var when running elsewhere (CI, cloud, another machine).
PROJECT_DIR   = os.environ.get(
    "JOBSEARCH_DIR",
    r"C:\Users\maryk\OneDrive\Documents\job-search",
)
DIGESTS_DIR   = os.path.join(PROJECT_DIR, "digests")
LOGS_DIR      = os.path.join(PROJECT_DIR, "logs")
TOKEN_FILE    = os.path.join(PROJECT_DIR, "token.json")

# Rename your downloaded OAuth file to credentials.json, or update this path.
# Current file found: client_secret_942349015170-...apps.googleusercontent.com.json
CREDENTIALS_FILE = os.path.join(
    PROJECT_DIR,
    "client_secret_942349015170-cj5fhi1h39k3sduquhic6rdtao64ni5n.apps.googleusercontent.com.json"
)

# ---------------------------------------------------------------------------
# Gmail OAuth scopes
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ---------------------------------------------------------------------------
# Senders to monitor
# Each entry must have a 'source' label.
# Match modes:
#   'exact'           — from header equals this email address
#   'domain_contains' — email address contains this substring
#   'subject_filter'  — only include if subject contains this string (AND condition)
# ---------------------------------------------------------------------------

SENDERS = [
    {
        "source": "Indeed",
        "match": "exact",
        "address": "donotreply@jobalert.indeed.com",
    },
    {
        "source": "NCWorks",
        "match": "exact",
        "address": "NCWorksonline@ncworks.gov",
        "subject_filter": "Virtual Recruiter",  # subject: "Virtual Recruiter Notification from NCWorks Online"
    },
    {
        "source": "LinkedIn",
        "match": "exact",
        "address": "jobalerts-noreply@linkedin.com",
        "subject_filter": "job alert",
    },
    {
        "source": "NC Biotech Center",
        "match": "name_contains",
        "substring": "north carolina biotechnology",
        "subject_filter": ["new jobs", "new job"],
        # Biotech board surfaces many non-DS roles (engineers, project managers,
        # clinical ops).  Override keeps only DS/ML/AI-relevant titles.
        "title_include_override": [
            "data scientist",
            "data science",
            "data analyst",
            "data manager",
            "machine learning",
            "ml engineer",
            "ai engineer",
            "ai scientist",
            "analytics engineer",
            "biostatistician",
            "research analyst",
            "research data",
            "quantitative analyst",
            "nlp",
            "predictive",
            "clinical ai",
        ],
    },
    {
        "source": "Duke Careers",
        "match": "exact",
        "address": "duke-jobnotification@noreply.jobs2web.com",
    },
    {
        "source": "Cisco",
        "match": "exact",
        "address": "opportunities@recruiting.cisco.com",
    },
    {
        "source": "NetApp",
        "match": "exact",
        "address": "no-reply@tbjobalerts.com",
        "subject_filter": "NetApp",   # subject: "New jobs at NetApp, Inc."
    },
    {
        "source": "CVS Health",
        "match": "domain_contains",
        "substring": "careerinfo.cvshealth.com",
        "subject_filter": "job",      # catches "job alert", "jobs for you" etc.
    },
    {
        "source": "State of NC",
        "match": "exact",
        "address": "workday@nc.gov",
        "subject_filter": "Job Alert Notification",
    },
    {
        "source": "Government Jobs",
        "match": "exact",
        "address": "info@governmentjobs.com",
        "subject_filter": "Job Interest Card Notification",
    },
    {
        "source": "Red Hat",
        "match": "domain_contains",
        "substring": "redhat",         # catches @redhat.com, recruiting@redhat.com, etc.
    },
    {
        "source": "Red Hat",
        "match": "name_contains",      # matches display name, e.g. "Red Hat Recruiting"
        "substring": "red hat",
    },
    {
        "source": "Fidelity",
        "match": "domain_contains",
        "substring": "fidelity.com",
    },
    {
        "source": "MetLife",
        "match": "domain_contains",
        "substring": "metlife.com",
    },
    {
        "source": "SAS Institute",
        "match": "domain_contains",
        "substring": "sas.com",
    },
    {
        "source": "Siemens Healthineers",
        "match": "domain_contains",
        "substring": "siemens-healthineers.com",
        # Only DS/ML/AI-track titles — ops/admin roles share the domain but
        # are irrelevant. Replaces the global INCLUDE_TITLE_KEYWORDS for this
        # sender so "analyst" doesn't pull in Operations Analyst, etc.
        "title_include_override": [
            "data scientist",
            "data science",
            "machine learning",
            "ml engineer",
            "ai engineer",
            "deep learning",
            "clinical ai",
            "healthcare ai",
            "computer vision",
            "nlp",
            "predictive",
            "analytics engineer",
        ],
    },
    {
        "source": "GSK",
        "match": "domain_contains",
        "substring": "gsk.com",
    },
    {
        "source": "ViiV Healthcare",
        "match": "domain_contains",
        "substring": "viivhealthcare.com",
    },
    {
        "source": "Syneos Health",
        "match": "domain_contains",
        "substring": "syneoshealth.com",
    },
    {
        "source": "UNC Chapel Hill",
        "match": "domain_contains",
        "substring": "unc.edu",
        # subject_filter as a list = OR logic: subject must contain at least one
        "subject_filter": ["job", "career", "position"],
    },
]

# ---------------------------------------------------------------------------
# Dynamic discovery — job-intent keywords (keyword-based detection)
# build_query adds a subject:(...) clause built from this list, so job-alert
# emails from senders NOT in SENDERS above are still fetched. The generic LLM
# extractor then reads those unknown-sender emails (and returns nothing for
# non-job mail), which is what makes a looser net safe. Tune freely.
# ---------------------------------------------------------------------------

JOB_INTENT_KEYWORDS = [
    "job alert",
    "new jobs",
    "jobs for you",
    "jobs you may be interested",
    "recommended jobs",
    "recommended for you",
    "job matches",
    "jobs matching",
    "new job matches",
    "job recommendations",
    "your job alert",
    "jobs we found",
    "matching jobs",
]

# Max generic LLM-extractor calls per run (cost control). Only unknown-sender
# emails that matched a job-intent keyword can trigger a call; known senders use
# their hand-written parsers and never count against this budget.
LLM_EXTRACT_MAX_PER_RUN = 25

# ---------------------------------------------------------------------------
# Role title keywords to INCLUDE (case-insensitive, any-word match)
# ---------------------------------------------------------------------------

INCLUDE_TITLE_KEYWORDS = [
    "data scientist",
    "data analyst",
    "ml engineer",
    "machine learning engineer",
    "ai engineer",
    "agentic engineer",
    "ai research",
    "research analyst",
    "research data analyst",
    "research data scientist",
    "data science",
    "quantitative analyst",
    "ai/ml engineer",
    "ai/ml software engineer",
    "data analytics fellow",   # entry-level clinical/research analytics roles (e.g. Duke DCRI)
    "emerging talent",         # NetApp Emerging Talent program and similar
    "quantitative associate",  # Duke DUMAC and similar investment/research analytics roles
    "quantitative analyst",    # similar quant roles requiring DS/ML skills
    "quantitative developer",  # quant dev roles requiring programming/ML skills
    # Expanded AI title coverage — the landscape of AI-prefixed titles is
    # growing rapidly. Specific two-word combos keep noise low while catching
    # the long tail of "AI <something> Engineer/Scientist/Developer" roles.
    "ai application",
    "ai solutions",
    "ai platform",
    "ai software",
    "ai developer",
    "ai systems",
    "ai implementation",
    "ai integration",
    "ai infrastructure",
    "ai research engineer",
    "ai product",
    "conversational ai",
    "generative ai",
    "applied ai",
    "ai analyst",
    # NOTE: bare "analyst" removed — it matched Credit Risk Analyst, Financial Analyst,
    # Operations Analyst, etc. which are outside the DS/MLE 80/20 target.
    # Add specific analyst subtypes above if new domains need coverage.
]

# ---------------------------------------------------------------------------
# Locations to INCLUDE (case-insensitive substring match in title or body)
# ---------------------------------------------------------------------------

INCLUDE_LOCATIONS = [
    "durham",
    "raleigh",
    "cary",
    "morrisville",
    "chapel hill",
    "research triangle",
    "rtp",
    "remote",
]

# ---------------------------------------------------------------------------
# Keywords that trigger EXCLUDE (case-insensitive, checked in title + snippet)
# "lead" is handled separately (see EXCLUDE_LEAD_UNLESS_SALARY_BELOW).
# ---------------------------------------------------------------------------

EXCLUDE_KEYWORDS = [
    "senior",
    "sr ",        # catches "Sr Data Scientist", "Sr. Engineer" etc at start or mid-title
    "sr.",
    "principal",
    "director",
    "manager",
    " vp ",
    "vice president",
    "head of",
    "clearance required",
    "secret clearance",
    "ts/sci",
    " sap ",
    "sap ",          # catches "SAP Master Data Analyst" etc. at start of title
    "hadoop only",
    "java only",
    "fixed income",   # finance trading roles, not DS/ML
    "nga ",           # National Geospatial-Intelligence Agency — requires clearance
    "kubernetes",     # infrastructure/platform engineering, not DS/ML
    "platform engineer",  # same — infra signal
    " avp",           # Associate Vice President mid-title — banking seniority, 3+ years
    "avp,",           # AVP at start of title e.g. "AVP, AI Engineering"
    "avp ",           # AVP at start of title with space after
    "associate vice president",  # same, spelled out
]

# If a title contains "lead", skip it — UNLESS a salary is found and it is
# at or below this threshold (signals the role may be entry/mid-level despite the label).
EXCLUDE_LEAD           = True
LEAD_SALARY_KEEP_BELOW = 90_000   # annual USD

# Roles where the stated salary ceiling is below this are demoted to 'review'
# rather than 'pursue', regardless of title match.
# Uses the upper end of a salary range so "$70K-$95K" is not penalised.
SALARY_MINIMUM = 90_000           # annual USD

# ---------------------------------------------------------------------------
# Companies already applied to, or active pipeline — skip these entirely
# to avoid re-surfacing roles Aidan has already engaged with. See
# SKIP_COMPANIES_NEVER below for companies we exclude by policy.
# ---------------------------------------------------------------------------

SKIP_COMPANIES = [
    # Already-evaluated or pipeline companies
    "almac",
    "covar",
    "duke biostatistician",
    "duke health",
    "palantir",
    # "thermo fisher",  # removed — active scan target
    "truist",
    # NOTE: UNC Chapel Hill intentionally not blocked — good target employer.
    # Aidan applied to Research Data & AI Solutions Specialist (not selected).
    # Role will age out of the email window naturally.
    # Staffing / recruiting firms — filter recruiter noise
    "astyra",
    "chainit",
    "cybercoders",
    "experis",
    "fidelity talentsource",
    "htc global",
    "innovet",
    "idr inc",           # staffing agency, undisclosed end clients
    "kairos recruitment",
    "maxonic",
    "sean e. merryman",
    "spectraforce",      # staffing agency, undisclosed end clients
    "us tech solutions",   # staffing agency, undisclosed end clients
]

# ---------------------------------------------------------------------------
# Companies we never want to pursue — regardless of role title.
# Primarily gig-economy AI-training platforms (piecework, no benefits,
# not comparable to salaried DS/MLE roles even at comparable hourly rates)
# and other recurring noise sources. Matched by case-insensitive substring
# against the company name.
# ---------------------------------------------------------------------------

SKIP_COMPANIES_NEVER = [
    "dataannotation",
    "outlier ai",
    "outlier",          # also catches "Outlier", "Outlier AI LLC"
    "welocalize",       # gig-based AI data labeling, not a DS/ML career role
    "tsmg",             # same gig labeling business model as WeLocalize
]

# ---------------------------------------------------------------------------
# Snippet/JD content keywords to EXCLUDE (case-insensitive, checked against
# the job snippet and title). Use for gig/crowdsource patterns that slip
# through company-name filtering regardless of who posts them.
# ---------------------------------------------------------------------------

EXCLUDE_SNIPPET_KEYWORDS = [
    "data labeling, annotation",    # gig AI labeling boilerplate
    "task-based work",              # gig/crowdsource signal
    "task-based role",              # gig/crowdsource signal
    "data contributor",             # WeLocalize/TSMG specific title pattern
]

# ---------------------------------------------------------------------------
# Experience years gate
# Snippets mentioning more than this many years of experience will be skipped.
# Set to None to disable. Aidan has ~1 year academic + capstone experience,
# so roles requiring 4+ years are a stretch; 3 is a reasonable threshold
# given "academic experience included" language on many entry-level postings.
# ---------------------------------------------------------------------------

MAX_YEARS_EXPERIENCE = 3

# Roles that are otherwise a good fit but require MORE than this many years are
# routed to the "Stretch" tier (a deliberate, referral-gated pile) instead of
# being skipped or cluttering Pursue. Aidan can credibly claim ~1-2 years
# (internships + capstone), so the evaluator treats >2 as a stretch.
# (Web-app specific: consumed by engine.py's Stretch-tier routing.)
STRETCH_YEARS_THRESHOLD = 2

PREFER_COMPANIES = [
    "fujifilm",
    "lenovo",
]
