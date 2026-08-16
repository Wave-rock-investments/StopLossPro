"""Sterling_Room API — the StopLossPro adapter target (master-prompt §51).

Only the endpoints needed for Phases 1-3 (schema, adapter, call lifecycle)
exist here. Subscriber/plan/payment/performance endpoints are schema-ready
(see models.py) but have no routes yet — deliberately not stubbed with fake
success responses, since master-prompt §10 forbids fabricating functionality.
"""
from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    Call, CallEventType, CallStatus, CallMessage, DeliveryStatus, MessageType,
    Payment, PaymentStatus, SupportTicket, TicketStatus, TERMINAL_CALL_STATUSES,
)
from app import bot as interactive_bot
from app import performance
from app import services
from app import telegram_bot
from app.rate_limit import check_rate_limit, key_hash, telegram_webhook_limit

router = APIRouter()
settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════
# Auth — shared-secret bearer token for trusted adapter callers.
# Fails CLOSED: if no keys are configured, nothing can authenticate.
# ══════════════════════════════════════════════════════════════════════════
def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization[len("Bearer "):]


def require_adapter_key(authorization: str | None = Header(default=None)) -> str:
    token = _bearer(authorization)
    valid_keys = settings.adapter_api_key_list
    if not valid_keys:
        raise HTTPException(status_code=503, detail="No adapter API keys configured — endpoint is closed")
    if not any(hmac.compare_digest(token, k) for k in valid_keys):
        raise HTTPException(status_code=401, detail="Invalid adapter API key")
    return token


# ══════════════════════════════════════════════════════════════════════════
# Rate limiting (Phase 9) — scoped per adapter API key, not per IP.
# StopLossPro's own traffic is authenticated, trusted machine traffic, not
# a public surface to defend against — see app/rate_limit.py's module
# docstring ("trusted internal calls"). The limit here is a safety net
# against a runaway bug on the caller's side (a retry loop gone wrong),
# generous enough that it never interferes with real call volume. Depends
# on require_adapter_key so rate-limit identity is the authenticated key,
# never the caller's IP — a legitimate caller behind a NAT/proxy shares
# nothing with any other caller's quota.
# ══════════════════════════════════════════════════════════════════════════
def _adapter_write_limit(request: Request, token: str = Depends(require_adapter_key)) -> None:
    check_rate_limit("adapter_write", key_hash(token), request)


def _adapter_read_limit(request: Request, token: str = Depends(require_adapter_key)) -> None:
    check_rate_limit("adapter_read", key_hash(token), request)


# ══════════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════════
class CallIn(BaseModel):
    source_call_id: str = Field(..., description="Client-generated idempotency key, e.g. a UUID4 per SHARE tap")
    source: str = "stoplosspro"
    instrument: str
    direction: str
    entry_min: float | None = None
    entry_max: float | None = None
    stop_loss: float
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    risk_percent: float | None = None
    setup_type: str | None = None
    analysis: str | None = None
    invalidation: str | None = None
    route_free: bool = False
    route_premium: bool = True


class CallOut(BaseModel):
    trade_id: str
    status: str
    instrument: str
    direction: str
    stop_loss: float
    tp1: float | None
    tp2: float | None
    tp3: float | None
    created_at: str

    @classmethod
    def from_model(cls, c: Call) -> "CallOut":
        return cls(
            trade_id=c.trade_id, status=c.status.value, instrument=c.instrument,
            direction=c.direction.value, stop_loss=float(c.stop_loss),
            tp1=float(c.tp1) if c.tp1 is not None else None,
            tp2=float(c.tp2) if c.tp2 is not None else None,
            tp3=float(c.tp3) if c.tp3 is not None else None,
            created_at=c.created_at.isoformat(),
        )


class TransitionIn(BaseModel):
    new_status: str
    detail: str | None = None
    # Required (by convention, not enforced at the schema level — see
    # api.py's transition_call route) when new_status is CLOSED or STOPPED:
    # the performance ledger (app/performance.py) only counts calls that
    # have a result_r, so a close/stop without one silently never appears in
    # any performance report rather than erroring — documented, not hidden,
    # in transition_call's docstring below.
    result_r: float | None = None
    # optional template kwargs for the Telegram message this transition sends
    reason: str | None = None
    management_instruction: str | None = None
    update_text: str | None = None


def _err(exc: services.ServiceError):
    raise HTTPException(status_code=exc.http_status, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════
@router.post("/calls", tags=["calls"], response_model=CallOut)
def create_call(body: CallIn, db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                 _rl: None = Depends(_adapter_write_limit)):
    payload = body.model_dump()
    try:
        call = services.create_call(db, payload, actor=f"adapter:{payload.get('source', 'unknown')}")
    except services.DuplicateCall as dup:
        db.commit()
        return CallOut.from_model(dup.call)
    except services.ValidationError as e:
        db.rollback()
        _err(e)
    else:
        chat_ids = telegram_bot.resolve_chat_ids(
            call, free_chat_id=settings.TELEGRAM_FREE_CHAT_ID, premium_chat_id=settings.TELEGRAM_PREMIUM_CHAT_ID,
        )
        if chat_ids and settings.telegram_configured:
            telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=chat_ids)
            services.audit(db, "CALL_SENT", actor="system", detail=f"trade_id={call.trade_id} chats={chat_ids}")
        db.commit()
        db.refresh(call)
        return CallOut.from_model(call)


@router.get("/calls", tags=["calls"], response_model=list[CallOut])
def list_calls(status: str | None = None, limit: int = 50, db: Session = Depends(get_db),
                token: str = Depends(require_adapter_key), _rl: None = Depends(_adapter_read_limit)):
    q = select(Call).order_by(Call.created_at.desc()).limit(min(limit, 200))
    if status:
        try:
            q = q.where(Call.status == CallStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")
    return [CallOut.from_model(c) for c in db.execute(q).scalars().all()]


@router.get("/calls/{trade_id}", tags=["calls"], response_model=CallOut)
def get_call(trade_id: str, db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
             _rl: None = Depends(_adapter_read_limit)):
    call = db.execute(select(Call).where(Call.trade_id == trade_id)).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Not found")
    return CallOut.from_model(call)


_TRANSITION_EVENT_MAP = {
    CallStatus.TP1_HIT: CallEventType.TP1_REACHED,
    CallStatus.BREAKEVEN: CallEventType.BREAKEVEN,
    CallStatus.CLOSED: CallEventType.CALL_CLOSED,
    CallStatus.STOPPED: CallEventType.CALL_STOPPED,
    CallStatus.INVALIDATED: CallEventType.CALL_INVALIDATED,
    CallStatus.CANCELLED: CallEventType.CALL_CANCELLED,
}
_TRANSITION_MESSAGE_MAP = {
    CallStatus.TP1_HIT: MessageType.TP1,
    CallStatus.CLOSED: MessageType.EXIT,
    CallStatus.STOPPED: MessageType.EXIT,
    CallStatus.INVALIDATED: MessageType.INVALIDATED,
}


@router.post("/calls/{trade_id}/events", tags=["calls"], response_model=CallOut)
def transition_call(trade_id: str, body: TransitionIn, db: Session = Depends(get_db),
                     token: str = Depends(require_adapter_key), _rl: None = Depends(_adapter_write_limit)):
    call = db.execute(select(Call).where(Call.trade_id == trade_id)).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        new_status = CallStatus(body.new_status.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown status {body.new_status!r}")

    event_type = _TRANSITION_EVENT_MAP.get(new_status, CallEventType.CALL_UPDATED)

    # Idempotent retry of the SAME terminal close event (master-prompt Phase
    # 10 §2: "same close event twice -> one ledger result, one Results
    # post") — e.g. StopLossPro retrying a CLOSE call whose first response
    # was lost to a network blip, even though the first attempt already
    # succeeded server-side. The state machine correctly refuses ANY
    # transition out of a terminal status (see services.transition_call),
    # including a re-request of the status it's already in, so that case is
    # handled here instead: skip the state machine and, deliberately, skip
    # re-assigning result_r — an already-recorded historical result must
    # never be silently overwritten by a bare retry (master-prompt §33/§60).
    # Message/results delivery below still runs either way, and is itself
    # idempotent via distribute_call's (call, chat, type, content) unique
    # constraint, so the caller gets a clean 200 in both the first-attempt
    # and retry cases.
    already_in_target_status = call.status == new_status and new_status in TERMINAL_CALL_STATUSES

    if not already_in_target_status:
        try:
            services.transition_call(db, call, new_status, actor=f"adapter:{call.source}",
                                      event_type=event_type, detail=body.detail)
            if new_status in (CallStatus.CLOSED, CallStatus.STOPPED) and body.result_r is not None:
                call.result_r = body.result_r
        except services.InvalidTransition as e:
            db.rollback()
            _err(e)

    message_type = _TRANSITION_MESSAGE_MAP.get(new_status)
    if message_type is not None:
        chat_ids = telegram_bot.resolve_chat_ids(
            call, free_chat_id=settings.TELEGRAM_FREE_CHAT_ID, premium_chat_id=settings.TELEGRAM_PREMIUM_CHAT_ID,
        )
        if chat_ids and settings.telegram_configured:
            telegram_bot.distribute_call(
                db, call, message_type, chat_ids=chat_ids,
                reason=body.reason or "", management_instruction=body.management_instruction or "",
                update_text=body.update_text or "",
            )

    # Results-channel automation (Phase 10 §2): CALL CLOSED -> Performance
    # Ledger -> Verified R Result -> RESULTS CHANNEL. `call.result_r` is the
    # SAME authoritative field app/performance.py sums the ledger over —
    # nothing is recomputed separately for this post.
    if (new_status in (CallStatus.CLOSED, CallStatus.STOPPED) and call.result_r is not None
            and settings.TELEGRAM_RESULTS_CHAT_ID and settings.telegram_configured):
        telegram_bot.distribute_call(
            db, call, MessageType.RESULTS, chat_ids=[settings.TELEGRAM_RESULTS_CHAT_ID],
        )

    db.commit()
    db.refresh(call)
    return CallOut.from_model(call)


# ══════════════════════════════════════════════════════════════════════════
# Performance (Phase 6) — read-only, always derived from the calls table
# ══════════════════════════════════════════════════════════════════════════
class PerformanceOut(BaseModel):
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float | None
    net_r: float
    avg_winner_r: float | None
    avg_loser_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    trade_ids: list[str]

    @classmethod
    def from_stats(cls, s: performance.PerformanceStats) -> "PerformanceOut":
        return cls(**s.__dict__)


@router.get("/performance", tags=["performance"], response_model=PerformanceOut)
def get_performance(db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                     _rl: None = Depends(_adapter_read_limit)):
    return PerformanceOut.from_stats(performance.compute_stats(db))


@router.get("/performance/daily", tags=["performance"], response_model=PerformanceOut)
def get_performance_daily(db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                           _rl: None = Depends(_adapter_read_limit)):
    return PerformanceOut.from_stats(performance.daily_results(db))


@router.get("/performance/weekly", tags=["performance"], response_model=PerformanceOut)
def get_performance_weekly(db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                            _rl: None = Depends(_adapter_read_limit)):
    return PerformanceOut.from_stats(performance.weekly_results(db))


@router.get("/performance/monthly", tags=["performance"], response_model=PerformanceOut)
def get_performance_monthly(year: int | None = None, month: int | None = None,
                             db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                             _rl: None = Depends(_adapter_read_limit)):
    return PerformanceOut.from_stats(performance.monthly_results(db, year=year, month=month))


# ══════════════════════════════════════════════════════════════════════════
# Monitoring (Phase 8) — machine-readable counterpart to /admin/health's
# human-facing page. Same underlying queries, JSON instead of HTML, gated by
# the same adapter-key auth as the rest of this router (an ops/monitoring
# integration is a "trusted caller", same trust tier as the adapter) rather
# than by admin cookie auth (a monitoring system has no browser session).
# ══════════════════════════════════════════════════════════════════════════
class MonitoringOut(BaseModel):
    env: str
    db_ok: bool
    telegram_configured: bool
    payment_provider: str
    active_calls: int
    failed_telegram_deliveries: int
    open_support_tickets: int
    pending_payments_awaiting_confirmation: int
    production_ready: bool
    production_problems: list[str]


@router.get("/monitoring", tags=["ops"], response_model=MonitoringOut)
def monitoring(db: Session = Depends(get_db), token: str = Depends(require_adapter_key),
                _rl: None = Depends(_adapter_read_limit)):
    db_ok = True
    try:
        db.execute(select(Call.id).limit(1))
    except Exception:
        db_ok = False

    problems = settings.assert_production_ready()
    return MonitoringOut(
        env=settings.ENV,
        db_ok=db_ok,
        telegram_configured=settings.telegram_configured,
        payment_provider=settings.PAYMENT_PROVIDER,
        active_calls=db.query(Call).filter(Call.status.notin_(TERMINAL_CALL_STATUSES)).count(),
        failed_telegram_deliveries=db.query(CallMessage).filter_by(delivery_status=DeliveryStatus.FAILED).count(),
        open_support_tickets=db.query(SupportTicket).filter_by(status=TicketStatus.OPEN).count(),
        pending_payments_awaiting_confirmation=db.query(Payment).filter_by(status=PaymentStatus.PENDING).count(),
        production_ready=not problems,
        production_problems=problems,
    )


# ══════════════════════════════════════════════════════════════════════════
# Telegram webhook (Phase 4) — the interactive /start bot.
#
# Auth model deliberately differs from require_adapter_key above: Telegram's
# webhook mechanism carries no bearer-token header of its own, so the
# standard mitigation (Telegram's own recommendation) is an unguessable
# secret PATH segment instead. Fails closed the same way require_adapter_key
# does: an unset TELEGRAM_WEBHOOK_SECRET means the route always 404s, never
# "falls open" to an unauthenticated bot. A wrong/guessed secret 404s too
# (not 401/403) so a scanner can't distinguish "wrong secret" from "route
# doesn't exist here" — same reasoning as not leaking which trade_ids exist.
# ══════════════════════════════════════════════════════════════════════════
@router.post("/telegram/webhook/{secret}", tags=["telegram"], include_in_schema=False)
async def telegram_webhook(secret: str, request: Request, db: Session = Depends(get_db),
                            _rl: None = Depends(telegram_webhook_limit)):
    if not settings.TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(secret, settings.TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=404)
    try:
        update = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    interactive_bot.handle_update(db, update, settings)
    return {"ok": True}
