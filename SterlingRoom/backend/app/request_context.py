"""Per-request correlation ID (Phase 9 — launch hardening).

A tiny, dependency-free contextvar so any code running within a request's
handling — a route, a service function, the rate limiter — can attach the
same `request_id` to its log lines without threading it through every
function signature. Set once by `RequestIDMiddleware` (app/main.py) at the
top of each request; read anywhere via `get_request_id()`.

contextvars propagate correctly through Starlette/anyio's
`run_in_threadpool` (used for sync `def` routes) — the context is copied
into the worker thread, so this is safe to read from inside a synchronous
route function, not just async code.
"""
from __future__ import annotations

import contextvars

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)
