"""Business logic — call validation, idempotent creation, state transitions,
audit logging. Mirrors Working/backend/app/services.py's shape (typed
ServiceError hierarchy, an audit() helper used everywhere) for consistency
across the two backends in this repo, even though they are separate services.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AuditEvent,
    Call,
    CallDirection,
    CallEvent,
    CallEventType,
    CallStatus,
    CALL_STATUS_TRANSITIONS,
    TERMINAL_CALL_STATUSES,
)
from app.trade_id import allocate_unique_trade_id


# ══════════════════════════════════════════════════════════════════════════
# Errors — typed, not bare strings, matching Working/backend's pattern
# ══════════════════════════════════════════════════════════════════════════
class ServiceError(Exception):
    code = "service_error"
    http_status = 400


class ValidationError(ServiceError):
    code = "validation_error"
    http_status = 422


class InvalidTransition(ServiceError):
    code = "invalid_transition"
    http_status = 409


class DuplicateCall(ServiceError):
    """Not actually an error condition for the caller — signals "this
    source_call_id was already processed, here's the existing call" so
    retries are idempotent rather than rejected (master-prompt §28)."""
    code = "duplicate_call"
    http_status = 200

    def __init__(self, call: Call):
        self.call = call
        super().__init__(f"source_call_id already processed as {call.trade_id}")


# ══════════════════════════════════════════════════════════════════════════
# Audit — append-only, matches Working/backend/app/services.py::audit()
# ══════════════════════════════════════════════════════════════════════════
def audit(db: Session, event_type: str, *, actor: str | None = None,
          result: str = "SUCCESS", detail: str | None = None) -> None:
    db.add(AuditEvent(event_type=event_type, actor=actor, result=result, detail=detail))


def _record_call_event(db: Session, call: Call, event_type: CallEventType, *,
                        actor: str | None, old_status: CallStatus | None,
                        new_status: CallStatus | None, detail: str | None = None) -> None:
    db.add(CallEvent(
        call_id=call.id, event_type=event_type, actor=actor,
        old_status=old_status, new_status=new_status, detail=detail,
    ))


# ══════════════════════════════════════════════════════════════════════════
# Validation (master-prompt §27) — reject malformed calls, never silently
# repair a dangerous financial value.
# ══════════════════════════════════════════════════════════════════════════
def validate_call_payload(payload: dict) -> None:
    required = ["instrument", "direction", "stop_loss", "source_call_id"]
    missing = [f for f in required if payload.get(f) in (None, "")]
    if missing:
        raise ValidationError(f"Missing required field(s): {', '.join(missing)}")

    direction = str(payload["direction"]).upper()
    if direction not in (CallDirection.BUY.value, CallDirection.SELL.value):
        raise ValidationError(f"direction must be BUY or SELL, got {payload['direction']!r}")

    def _finite_positive(name: str, required_field: bool) -> float | None:
        val = payload.get(name)
        if val is None:
            if required_field:
                raise ValidationError(f"{name} is required")
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ValidationError(f"{name} must be a number, got {val!r}")
        if not math.isfinite(f):
            raise ValidationError(f"{name} must be finite, got {val!r}")
        if f <= 0:
            raise ValidationError(f"{name} must be positive, got {val!r}")
        return f

    _finite_positive("stop_loss", required_field=True)
    for opt in ("tp1", "tp2", "tp3", "entry_min", "entry_max", "risk_percent"):
        _finite_positive(opt, required_field=False)

    instrument = str(payload["instrument"]).strip()
    if not instrument:
        raise ValidationError("instrument must not be blank")
    if len(instrument) > 20:
        raise ValidationError("instrument exceeds 20 characters — check for garbage input")


# ══════════════════════════════════════════════════════════════════════════
# Idempotent creation (master-prompt §28) — a retry can never mint a second
# Trade ID for the same real call.
# ══════════════════════════════════════════════════════════════════════════
def create_call(db: Session, payload: dict, *, actor: str) -> Call:
    """Create a call from an adapter payload, or return the existing one if
    source_call_id was already processed (idempotent — raises DuplicateCall,
    which callers should treat as a 200 with the existing call, not an
    error).
    """
    validate_call_payload(payload)

    existing = db.execute(
        select(Call).where(Call.source_call_id == payload["source_call_id"])
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateCall(existing)

    direction = CallDirection(str(payload["direction"]).upper())

    route_free = bool(payload.get("route_free", False))
    # Freemium delivery timing: computed ONCE, here, from "now" at creation
    # — never re-derived later from a global setting, so a mid-flight
    # change to FREE_CALL_DELAY_SECONDS never retroactively reschedules an
    # already-created call. NULL (not set) for premium-only calls, since
    # there is nothing to schedule.
    free_call_due_at = (
        datetime.now(timezone.utc) + timedelta(seconds=get_settings().FREE_CALL_DELAY_SECONDS)
        if route_free else None
    )

    for _attempt in range(5):
        trade_id = allocate_unique_trade_id(db)
        call = Call(
            trade_id=trade_id,
            source_call_id=payload["source_call_id"],
            source=payload.get("source", "stoplosspro"),
            instrument=str(payload["instrument"]).strip(),
            direction=direction,
            entry_min=payload.get("entry_min"),
            entry_max=payload.get("entry_max"),
            stop_loss=float(payload["stop_loss"]),
            tp1=payload.get("tp1"),
            tp2=payload.get("tp2"),
            tp3=payload.get("tp3"),
            risk_percent=payload.get("risk_percent"),
            setup_type=payload.get("setup_type"),
            analysis=payload.get("analysis"),
            invalidation=payload.get("invalidation"),
            status=CallStatus.ACTIVE,
            route_free=route_free,
            route_premium=bool(payload.get("route_premium", True)),
            free_call_due_at=free_call_due_at,
        )
        db.add(call)
        try:
            db.flush()  # surfaces a unique-constraint race now, inside this loop
        except IntegrityError:
            db.rollback()
            # Someone else took this Trade ID (or, separately, this
            # source_call_id) between our check and our insert. Re-check
            # source_call_id first — if THAT'S what collided, this really is
            # a duplicate call, not a Trade ID race.
            existing = db.execute(
                select(Call).where(Call.source_call_id == payload["source_call_id"])
            ).scalar_one_or_none()
            if existing is not None:
                raise DuplicateCall(existing)
            continue  # otherwise it was a Trade ID race — try the next number
        else:
            _record_call_event(
                db, call, CallEventType.CALL_CREATED, actor=actor,
                old_status=None, new_status=CallStatus.ACTIVE,
                detail=json.dumps({"instrument": call.instrument, "direction": call.direction.value}),
            )
            audit(db, "CALL_CREATED", actor=actor, detail=f"trade_id={call.trade_id}")
            return call

    raise RuntimeError("create_call: exhausted retries allocating a unique Trade ID")


# ══════════════════════════════════════════════════════════════════════════
# State machine (master-prompt §20) — reject invalid transitions, e.g.
# CLOSED -> ACTIVE must be impossible.
# ══════════════════════════════════════════════════════════════════════════
def transition_call(db: Session, call: Call, new_status: CallStatus, *,
                     actor: str, event_type: CallEventType, detail: str | None = None) -> Call:
    if call.status in TERMINAL_CALL_STATUSES:
        raise InvalidTransition(f"{call.trade_id} is {call.status.value} (terminal) — cannot move to {new_status.value}")

    allowed = CALL_STATUS_TRANSITIONS.get(call.status, set())
    if new_status not in allowed:
        raise InvalidTransition(
            f"{call.trade_id}: {call.status.value} -> {new_status.value} is not a legal transition "
            f"(allowed: {sorted(s.value for s in allowed) or 'none — terminal'})"
        )

    old_status = call.status
    call.status = new_status
    if new_status in TERMINAL_CALL_STATUSES:
        call.closed_at = datetime.now(timezone.utc)

    _record_call_event(db, call, event_type, actor=actor, old_status=old_status, new_status=new_status, detail=detail)
    audit(db, event_type.value, actor=actor, detail=f"trade_id={call.trade_id} {old_status.value}->{new_status.value}")
    return call


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
