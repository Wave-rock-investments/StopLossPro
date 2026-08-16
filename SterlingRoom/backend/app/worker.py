"""Sterling_Room background worker (Phase 10 — launch hardening).

Runs the two jobs that exist as functions but that nothing invoked on a
schedule until now (per DEPLOYMENT.md's own "Not yet built" note):

- Telegram delivery retry (app/telegram_bot.py::process_telegram_retries)
- Subscription lifecycle: mark EXPIRING_SOON, expire, revoke lapsed
  premium Telegram access (app/subscriptions.py::run_lifecycle_job)

DELIBERATELY NOT Celery/RQ/Kafka/anything requiring a message broker — this
service runs at a call volume where a broker would be pure overhead, and
every job here is already idempotent by construction (unique constraints,
status-filtered WHERE clauses, a persisted "already done" marker column),
which is what actually makes a lightweight polling loop production-safe:
there is no in-memory queue whose loss on a crash would lose work, because
nothing is ever "only" in memory — every decision is re-derived from
database state on every run. See app/telegram_bot.py's RetryRunStats and
app/subscriptions.py's LifecycleStats docstrings for the idempotency
argument job-by-job.

Two ways to run this, both production-valid — pick whichever fits the
hosting platform (documented in DEPLOYMENT.md):

    python -m app.worker --once     # run every job exactly once, then exit
                                     # (trigger via the host's own cron/
                                     # scheduled-job feature, e.g. Render
                                     # Cron Jobs)

    python -m app.worker            # loop forever, sleeping
                                     # STERLING_WORKER_INTERVAL_SECONDS
                                     # between ticks (run as a long-lived
                                     # "Background Worker" process)

Running more than one instance concurrently (either mode) is safe, not
just tolerated — every job is idempotent against concurrent execution the
same way the rest of this codebase is (see the modules above), so this
intentionally does NOT implement a distributed lock. That's a deliberate
scope choice, not an oversight: adding one would be exactly the kind of
infrastructure this module's docstring above says not to introduce
without genuine need, and duplicate ticks are wasted work, not a
correctness bug, here.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.logging_config import configure_logging
from app import subscriptions as subs
from app import telegram_bot

log = logging.getLogger("sterling.worker")

_shutdown_requested = False


def _handle_shutdown_signal(signum, _frame) -> None:
    global _shutdown_requested
    log.info("worker_shutdown_requested", extra={"signal": signum})
    _shutdown_requested = True


def run_once() -> dict:
    """Runs every job exactly one time against a fresh DB session, and
    returns a plain-dict summary suitable for a single structured log line
    (and for tests to assert against without re-querying the database).
    Each job gets its OWN session so a failure in one job cannot leave the
    other job's session in an unusable rolled-back-but-not-rolled-back
    state — see the two `finally: db.close()` blocks below.
    """
    now = datetime.now(timezone.utc)
    summary: dict = {"started_at": now.isoformat()}

    db = SessionLocal()
    try:
        retry_stats = telegram_bot.process_telegram_retries(db, now=now)
        summary["telegram_retry"] = {
            "candidates": retry_stats.candidates,
            "sent": retry_stats.sent,
            "still_failing": retry_stats.still_failing,
            "exhausted": retry_stats.exhausted,
            "not_due": retry_stats.not_due,
            "skipped_no_text": retry_stats.skipped_no_text,
        }
    except Exception:
        db.rollback()
        log.exception("telegram_retry_job_failed")
        summary["telegram_retry"] = {"error": "job raised — see exception log above"}
    finally:
        db.close()

    db = SessionLocal()
    try:
        lifecycle_stats = subs.run_lifecycle_job(db, now=now)
        summary["subscription_lifecycle"] = {
            "marked_expiring_soon": lifecycle_stats.marked_expiring_soon,
            "expired": lifecycle_stats.expired,
            "access_revoked": lifecycle_stats.access.revoked,
            "access_revoke_failed": lifecycle_stats.access.failed,
            "access_revoke_skipped_not_configured": lifecycle_stats.access.skipped_not_configured,
        }
    except Exception:
        db.rollback()
        log.exception("subscription_lifecycle_job_failed")
        summary["subscription_lifecycle"] = {"error": "job raised — see exception log above"}
    finally:
        db.close()

    log.info("worker_tick_complete", extra=summary)
    return summary


def run_forever(interval_seconds: int) -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    log.info("worker_starting", extra={"interval_seconds": interval_seconds})
    while not _shutdown_requested:
        run_once()
        # Sleep in short slices so a shutdown signal during the sleep is
        # honored promptly instead of waiting out the full interval.
        slept = 0
        while slept < interval_seconds and not _shutdown_requested:
            time.sleep(min(1, interval_seconds - slept))
            slept += 1
    log.info("worker_stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sterling_Room background worker")
    parser.add_argument("--once", action="store_true", help="Run every job exactly once, then exit (for cron-triggered invocation).")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(level="DEBUG" if settings.DEBUG else "INFO", json_output=not settings.DEBUG)

    if args.once:
        run_once()
    else:
        run_forever(settings.WORKER_INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
