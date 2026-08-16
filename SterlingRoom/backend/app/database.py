"""Database engine and session management — mirrors Working/backend/app/database.py's
pattern exactly (same project, same discipline), pointed at a separate database.
"""
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args = {}
if settings.is_sqlite:
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
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        # Without this, a second connection trying to write while another
        # holds the write lock gets an immediate "database is locked"
        # error instead of waiting — WAL mode helps concurrent READERS,
        # concurrent WRITERS still serialize on one lock either way. 15s
        # covers the worker (app/worker.py) and API process ever touching
        # the same SQLite file concurrently in a dev/single-file setup,
        # plus real-world contention spikes; irrelevant once on real
        # PostgreSQL in production.
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
