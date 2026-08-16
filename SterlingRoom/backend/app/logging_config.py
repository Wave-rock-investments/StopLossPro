"""Structured logging (Phase 8). Every log line becomes one JSON object on
stdout — the format a hosting platform's log aggregator (Render, Fly,
CloudWatch, etc.) can index and query without a separate log-shipping agent.
Deliberately stdlib logging + a custom Formatter, not a third-party logging
library — same "one fewer dependency" reasoning as telegram_bot.py's stdlib
urllib choice.

Call configure_logging() once, at process start (app/main.py, before the
FastAPI app does anything else). Every module in this codebase already does
`log = logging.getLogger("sterling.<module>")` — this file only changes how
those records are FORMATTED and WHERE they go, not what anyone logs.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Anything passed via logging's `extra={...}` rides along untouched —
        # e.g. a future caller doing log.info("...", extra={"trade_id": x}).
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value
        return json.dumps(payload, default=str)


_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter() if json_output else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet the noisiest third-party loggers down to WARNING so a request
    # storm doesn't drown Sterling_Room's own log lines — access logging is
    # uvicorn's job (already configured separately), not duplicated here.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
