"""Bridge so background tasks get their own DB session for a triage run."""

from sqlmodel import Session

from ..db import engine
from ..triage.engine import run_triage, run_scan


def run_triage_bg(hours_back: int | None) -> None:
    with Session(engine) as session:
        run_triage(session, hours_back=hours_back)


def run_scan_bg(hours_back: int | None = None) -> None:
    with Session(engine) as session:
        run_scan(session, hours_back=hours_back)
