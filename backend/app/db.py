"""SQLite engine, session factory, and schema init."""

from datetime import datetime, timedelta

from sqlmodel import SQLModel, Session, create_engine, select

from . import settings

# A real run finishes in seconds to low minutes; nothing legitimate stays
# "running" this long, so anything older is an orphan, not a live run.
STALE_RUN_THRESHOLD_MINUTES = 30

# check_same_thread=False so the engine can be used across FastAPI's threadpool
# and background tasks. SQLite + a single local user handles this fine.
engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create tables if they don't exist. Import models first so they register."""
    from . import models  # noqa: F401  (registers tables on SQLModel.metadata)
    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Idempotent, concurrency-safe column additions for DBs that predate a column.

    create_all() only creates missing *tables*, not missing *columns*, so an
    older DB needs a one-time ALTER. The check-then-ALTER isn't atomic, so two
    startups racing (a duplicate backend window, or a --reload restart firing
    while another process is still up) can both pass the check and both try the
    ALTER — the loser sees "duplicate column". That's benign; we swallow it.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    additions = {
        "run": [
            ("n_stretch", "INTEGER NOT NULL DEFAULT 0"),
            ("covered_through", "TIMESTAMP"),
            ("run_type", "TEXT NOT NULL DEFAULT 'triage'"),
        ],
        "jdcache": [("jd_url", "TEXT NOT NULL DEFAULT ''")],
        "job": [("last_alert_date", "TIMESTAMP")],
    }
    with engine.begin() as conn:
        for table, cols in additions.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for name, decl in cols:
                if name in existing:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
                except OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                    continue  # already added by a racing startup — no backfill needed either
                if table == "run" and name == "run_type":
                    # One-time best-effort backfill for rows that predate this column,
                    # only reachable on the ALTER that actually just added it (not on
                    # every later startup, or a legitimate triage run with n_emails=0
                    # — e.g. a narrow coverage-gap window with nothing new — would get
                    # wrongly flipped to "scan" on every subsequent restart). Portal
                    # scans always set n_emails=0 (see engine.run_scan); this isn't
                    # perfect for old rows but is far better than leaving them all
                    # mislabeled "triage".
                    conn.execute(text("UPDATE run SET run_type='scan' WHERE n_emails=0"))


def sweep_stale_runs() -> int:
    """Auto-recover Run rows orphaned by a backend crash mid-run.

    See CLAUDE.md's run #33 incident: the process died between marking a run
    "running" and ever reaching the except-block status update, leaving it
    stuck at "running" forever with nothing to flip it back. There's no
    queue/worker layer here (FastAPI BackgroundTasks, in-process, no
    retry/resume — see CLAUDE.md's deployment notes), so this startup sweep
    is the recovery path: any row still "running" after
    STALE_RUN_THRESHOLD_MINUTES gets marked "error" with a message that
    identifies it as an auto-recovery, not a live crash diagnosis. Runs once
    at startup only — a live run's own process wouldn't be restarting out
    from under itself mid-run. Returns the number of rows recovered.
    """
    from .models import Run

    threshold = datetime.utcnow() - timedelta(minutes=STALE_RUN_THRESHOLD_MINUTES)
    with Session(engine) as session:
        stale = session.exec(
            select(Run).where(Run.status == "running", Run.started_at < threshold)
        ).all()
        for run in stale:
            run.status = "error"
            run.error = (
                f"Auto-recovered at startup: stuck in 'running' for over "
                f"{STALE_RUN_THRESHOLD_MINUTES} minutes, almost certainly an "
                f"orphaned run from a backend crash rather than a live failure."
            )
            run.finished_at = datetime.utcnow()
            session.add(run)
        if stale:
            session.commit()
        return len(stale)


def get_session():
    """FastAPI dependency: yields a session, closes it after the request."""
    with Session(engine) as session:
        yield session
