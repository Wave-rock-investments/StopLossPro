"""Database engine and session management.

Production target is PostgreSQL. SQLite is supported for local development and
fast tests, with one important caveat documented below.

── The SQLite caveat, stated plainly ──────────────────────────────────────
The one-active-session guarantee (Phase 5) relies on TWO mechanisms:

  1. a partial unique index  -> supported by both SQLite and PostgreSQL
  2. SELECT ... FOR UPDATE row locking -> PostgreSQL only; a silent no-op on SQLite

Mechanism 1 alone is enough to make a double-active-session state *impossible
to persist* on either backend — the second INSERT fails. Mechanism 2 is what
turns that hard failure into an orderly, race-free takeover.

Therefore: unit tests may run on SQLite, but the concurrency tests that prove
the race-condition requirement MUST run against PostgreSQL. `Settings.
assert_production_ready()` refuses to start a production server on SQLite.
"""
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args = {}
if settings.is_sqlite:
    # check_same_thread=False so TestClient/threaded tests can share a connection
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    echo=settings.DEBUG and not settings.is_production,
)


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """SQLite ignores foreign keys unless explicitly told not to.

        Without this the FK constraints in our models would be decorative in
        development and would only start failing once deployed to PostgreSQL.
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency. One session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
