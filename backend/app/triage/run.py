"""
CLI runner — the SQLite-backed replacement for the old scheduled triage.py.

Point Windows Task Scheduler (or cron) at this instead of the old script:

    python -m app.triage.run --hours 24

It runs the same pipeline and writes jobs into jobsearch.db. The web app
then reads from that DB. ANTHROPIC_API_KEY must be set in the environment
for the Claude evaluation step (as it was in run_triage.bat).
"""

import argparse

from ..db import engine, init_db
from sqlmodel import Session
from .engine import run_triage


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a job-search triage and store results in SQLite.")
    parser.add_argument("--hours", type=int, default=None, help="Look-back window in hours (default: settings/24).")
    parser.add_argument("--skip-claude", action="store_true", help="Skip the Claude evaluation step.")
    args = parser.parse_args()

    init_db()
    with Session(engine) as session:
        run = run_triage(session, hours_back=args.hours, skip_claude=args.skip_claude or None)

    if run.status == "done":
        print(f"Run #{run.id} complete: {run.n_pursue} pursue / {run.n_review} review / "
              f"{run.n_stretch} stretch / {run.n_skipped} skipped, from {run.n_emails} emails.")
    else:
        print(f"Run #{run.id} FAILED: {run.error}")


if __name__ == "__main__":
    main()
