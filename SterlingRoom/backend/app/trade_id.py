"""Trade ID generation — master-prompt §18: SR-YYMMDD-NNN, unique, immutable,
never reused.

Concurrency note: this project's own documented caveat (Working/backend/app/
database.py) is that SELECT...FOR UPDATE is PostgreSQL-only and a silent
no-op on SQLite. Rather than depend on that row-locking mechanism at all,
Trade ID allocation here uses the SAME pattern this project already relies on
elsewhere for the same reason: a unique constraint that makes a collision
structurally impossible to persist (calls.trade_id is UNIQUE), combined with
a bounded retry loop on the rare IntegrityError a race produces. This is
correct and safe on SQLite AND PostgreSQL, with no dependency on locking
semantics that differ between them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Call

_MAX_ATTEMPTS = 10


def _day_prefix(now: datetime) -> str:
    return f"SR-{now.strftime('%y%m%d')}-"


def next_trade_id(db: Session, *, now: datetime | None = None) -> str:
    """Return the next unused Trade ID for "today" (UTC).

    Pure allocation logic — does NOT insert a Call row itself. Callers should
    retry the whole create-attempt (see services.create_call) if the eventual
    INSERT hits a unique-constraint race, using this function again to pick
    a fresh number. That keeps this function simple (just "what's next") and
    puts the actual collision handling where the real insert happens.
    """
    now = now or datetime.now(timezone.utc)
    prefix = _day_prefix(now)

    count_today = db.execute(
        select(func.count()).select_from(Call).where(Call.trade_id.like(f"{prefix}%"))
    ).scalar_one()

    return f"{prefix}{count_today + 1:03d}"


def allocate_unique_trade_id(db: Session, *, now: datetime | None = None) -> str:
    """Like next_trade_id, but re-checks for an exact collision before
    returning — narrows (does not eliminate; the real guarantee is the DB
    unique constraint) the race window between two concurrent requests.
    """
    now = now or datetime.now(timezone.utc)
    for _ in range(_MAX_ATTEMPTS):
        candidate = next_trade_id(db, now=now)
        exists = db.execute(select(Call.id).where(Call.trade_id == candidate)).first()
        if not exists:
            return candidate
    raise RuntimeError(
        f"Could not allocate a unique Trade ID after {_MAX_ATTEMPTS} attempts "
        f"for {_day_prefix(now)} — unexpectedly high contention."
    )


__all__ = ["next_trade_id", "allocate_unique_trade_id", "IntegrityError"]
