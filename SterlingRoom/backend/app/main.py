"""Sterling_Room API — FastAPI application entry point.

Deliberately a separate service from Working/backend (StopLossPro Pro's
licensing API) per the 2026-08-16 hosting decision ("Live on Render, keep
separate") — own database, own deploy target, own secrets.

Phase 1-8 scope: calls in (app/api.py), calls out to Telegram
(app/telegram_bot.py), the interactive subscriber bot (app/bot.py),
subscription lifecycle (app/subscriptions.py), performance ledger
(app/performance.py), and the admin dashboard (app/admin.py).
"""
import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.logging_config import configure_logging
from app.request_context import reset_request_id, set_request_id

settings = get_settings()

# Structured (JSON) logs everywhere except local development, where a human
# is reading stdout directly and plain text is easier to scan. Same signal
# either way, different shipping — see app/logging_config.py.
configure_logging(level="DEBUG" if settings.DEBUG else "INFO", json_output=not settings.DEBUG)
log = logging.getLogger("sterling.api")

app = FastAPI(
    title="Sterling_Room API",
    version="0.1.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attaches a correlation ID to every request — reused from
    X-Request-ID if the caller (e.g. a load balancer) already set one, else
    generated fresh. Every log line emitted while handling this request
    (including from app/rate_limit.py) carries it automatically via
    app/request_context.py + app/logging_config.py's JSONFormatter, and
    it's echoed back in the response header so a caller can correlate their
    own logs with Sterling_Room's."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = set_request_id(rid)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers["X-Request-ID"] = rid
    return response


def _mask_chat_id(chat_id: str) -> str:
    """Never log a full chat/channel ID (it's not secret, but there's no
    reason to put it in plaintext logs either) — just enough to eyeball
    "does dev point at the same destination as prod" without exposing the
    full value."""
    if not chat_id:
        return "(unset)"
    return f"...{chat_id[-4:]}" if len(chat_id) > 4 else "***"


@app.on_event("startup")
def _startup_guard() -> None:
    problems = settings.assert_production_ready()
    if problems:
        for p in problems:
            log.critical("PRODUCTION CONFIG ERROR: %s", p)
        raise RuntimeError(
            "Refusing to start in production with an unsafe configuration:\n  - " + "\n  - ".join(problems)
        )
    # Logged every boot, in every environment, specifically so a
    # dev/staging process pointed at the WRONG (e.g. production) Telegram
    # destination is visible immediately in the boot log rather than
    # discovered later by a test message landing in a real channel
    # (master-prompt Phase 8: "do not allow test systems to accidentally
    # publish to production Telegram channels"). This is a visibility
    # control, not an enforcement one — enforcement is operational (separate
    # .env files / separate bot tokens per environment, see DEPLOYMENT.md).
    log.info(
        "Sterling_Room API starting",
        extra={
            "env": settings.ENV, "debug": settings.DEBUG,
            "db_backend": "sqlite" if settings.is_sqlite else "postgresql",
            "telegram_configured": settings.telegram_configured,
            "telegram_free_chat": _mask_chat_id(settings.TELEGRAM_FREE_CHAT_ID),
            "telegram_premium_chat": _mask_chat_id(settings.TELEGRAM_PREMIUM_CHAT_ID),
            # No separate results chat — verified results post to the free
            # chat above (2026-08-16 production Telegram architecture).
            "payment_provider": settings.PAYMENT_PROVIDER,
            "rate_limit_backend": "redis" if settings.REDIS_URL else "in-memory",
        },
    )
    for warning in settings.production_warnings():
        log.warning(warning)


from app.admin import router as admin_router  # noqa: E402
from app.api import router as api_router  # noqa: E402

app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(admin_router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
def readiness(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        log.exception("readiness check failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready", "telegram_configured": settings.telegram_configured}
