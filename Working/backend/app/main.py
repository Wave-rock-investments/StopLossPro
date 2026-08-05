"""FastAPI application entry point.

Phase 1 deliberately exposes almost nothing: a health check and a readiness
check. Authentication arrives in Phase 2, licensing in Phase 3. Shipping an
empty-but-correct skeleton first means every later phase lands on a foundation
that is already migrated, configured and tested.
"""
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db

settings = get_settings()
log = logging.getLogger("stoploss.api")

app = FastAPI(
    title="StopLossPro Licensing API",
    version="0.1.0",
    # Interactive docs are useful in development and are an information
    # disclosure surface in production.
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


@app.on_event("startup")
def _startup_guard() -> None:
    """Refuse to serve a misconfigured production deployment.

    Fail loudly at boot rather than quietly at 3am under a race condition.
    """
    problems = settings.assert_production_ready()
    if problems:
        for p in problems:
            log.critical("PRODUCTION CONFIG ERROR: %s", p)
        raise RuntimeError(
            "Refusing to start in production with an unsafe configuration:\n  - "
            + "\n  - ".join(problems)
        )
    log.info("StopLossPro API starting — env=%s db=%s", settings.ENV,
             "sqlite" if settings.is_sqlite else "postgresql")


from app import admin as admin_module  # noqa: E402
from app.api import router as api_router  # noqa: E402

app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(admin_module.router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness. Deliberately reveals nothing about internals."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
def readiness(db: Session = Depends(get_db)):
    """Readiness — verifies the database is actually reachable."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        log.exception("readiness check failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}
