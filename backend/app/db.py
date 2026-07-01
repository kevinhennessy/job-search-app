"""SQLite engine, session factory, and schema init."""

from sqlmodel import SQLModel, Session, create_engine

from . import settings

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
        "run": [("n_stretch", "INTEGER NOT NULL DEFAULT 0")],
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


def get_session():
    """FastAPI dependency: yields a session, closes it after the request."""
    with Session(engine) as session:
        yield session
