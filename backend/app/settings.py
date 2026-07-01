"""Application settings. Override any value with an environment variable."""

import os
from pathlib import Path

# Repo root = .../backend
BASE_DIR = Path(__file__).resolve().parent.parent

# SQLite lives next to the backend by default. For a single-process deploy
# (FastAPI serving the built frontend), this file is the whole datastore.
DB_PATH = os.environ.get("JOBSEARCH_DB", str(BASE_DIR / "jobsearch.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Default look-back window for a triage run (hours). Matches the old --hours default.
DEFAULT_HOURS_BACK = int(os.environ.get("JOBSEARCH_HOURS", "24"))

# CORS origins for local dev (Vite runs on 5173). Comma-separated env override.
CORS_ORIGINS = os.environ.get(
    "JOBSEARCH_CORS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

# When True, the engine skips the Claude evaluation step (useful for testing
# without an ANTHROPIC_API_KEY, equivalent to the old --dry-run for eval only).
SKIP_CLAUDE_EVAL = os.environ.get("JOBSEARCH_SKIP_CLAUDE", "").lower() in ("1", "true", "yes")
