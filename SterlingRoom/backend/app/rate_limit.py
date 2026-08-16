"""Rate limiting (Phase 9 — launch hardening).

Every externally reachable route gets a limit appropriate to who's calling
it, not a single blanket number:

- Admin login / bootstrap: per-IP, tight — these are the routes an
  attacker without any prior credential would hit.
- Admin writes/reads: per-admin-session (identity exists once
  `require_admin` resolves), not per-IP — an office full of admins behind
  one NAT shouldn't share a quota, and an attacker who steals a session
  can't get a bigger budget by rotating IPs.
- Adapter (StopLossPro) traffic: per-API-key, generous. This is legitimate
  authenticated machine traffic, not a public surface to defend — see
  "trusted internal calls" below. Rate limiting it at all is a safety net
  against a runaway bug on the caller's side, not a defense against abuse.
- Telegram webhook: per-IP, generous (Telegram can burst-redeliver missed
  updates after any downtime — a tight limit here would make Sterling_Room
  drop legitimate redelivered updates, which is worse than not limiting).

BACKEND: a small abstraction (`RateLimitBackend`) with two implementations.
`InMemoryRateLimitBackend` is correct for exactly one worker process.
`RedisRateLimitBackend` is correct for any number of worker
processes/machines sharing one Redis — INCR+EXPIRE is atomic in Redis, so
concurrent requests across processes can't race past the limit. Selected
automatically by `get_backend()` based on `settings.REDIS_URL` — see
`app/config.py`'s `REDIS_URL` and `production_warnings()`.

FAIL-OPEN ON REDIS OUTAGE: if Redis is configured but unreachable, requests
are allowed through (logged loudly) rather than the whole API going down
because a side-car is unavailable. This is a deliberate, documented
tradeoff — admin login's separate, DB-backed account-lockout mechanism
(app/admin.py) is NOT dependent on Redis and keeps working during a Redis
outage, so brute-force protection on the highest-value target doesn't
disappear even if the general rate limiter does.
"""
from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Request

from app.config import get_settings

log = logging.getLogger("sterling.rate_limit")

# scope -> (max requests, window seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "admin_login": (10, 60),
    "admin_bootstrap": (5, 3600),
    "admin_write": (30, 60),
    "admin_read": (120, 60),
    "adapter_write": (300, 60),
    "adapter_read": (300, 60),
    "telegram_webhook": (120, 60),
}


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int  # seconds


class RateLimitBackend(ABC):
    @abstractmethod
    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...


class InMemoryRateLimitBackend(RateLimitBackend):
    """Fixed-window counter in this process's memory only. See the module
    docstring — correct for one worker, silently wrong (limit multiplies
    by worker count) for more than one."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.time()
        with self._lock:
            count, window_start = self._counters.get(key, (0, now))
            if now - window_start >= window_seconds:
                count, window_start = 0, now
            count += 1
            self._counters[key] = (count, window_start)
            allowed = count <= limit
            remaining = max(0, limit - count)
            retry_after = max(1, int(window_seconds - (now - window_start)) + 1)
        return RateLimitResult(allowed=allowed, remaining=remaining, retry_after=retry_after)


class RedisRateLimitBackend(RateLimitBackend):
    """Fixed-window counter via Redis INCR+EXPIRE — atomic, correct across
    any number of worker processes/machines. Requires `redis` (see
    requirements.txt) and a reachable Redis at `settings.REDIS_URL`."""

    def __init__(self, redis_url: str) -> None:
        import redis  # local import: don't require the dependency unless this backend is actually selected

        self._client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        bucket = int(time.time() // window_seconds)
        redis_key = f"sterling:ratelimit:{key}:{bucket}"
        try:
            pipe = self._client.pipeline()
            pipe.incr(redis_key, 1)
            pipe.expire(redis_key, window_seconds + 1)
            count, _ = pipe.execute()
        except Exception:
            log.error("rate_limit: Redis unavailable, failing open for this request", exc_info=True)
            return RateLimitResult(allowed=True, remaining=limit, retry_after=0)
        window_end = (bucket + 1) * window_seconds
        return RateLimitResult(
            allowed=count <= limit,
            remaining=max(0, limit - count),
            retry_after=max(1, int(window_end - time.time())),
        )


_backend: RateLimitBackend | None = None
_backend_lock = Lock()


def get_backend() -> RateLimitBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        settings = get_settings()
        if settings.REDIS_URL:
            try:
                _backend = RedisRateLimitBackend(settings.REDIS_URL)
                log.info("rate_limit: using Redis backend")
            except Exception:
                log.exception("rate_limit: failed to initialize Redis backend — falling back to in-memory (NOT multi-worker safe)")
                _backend = InMemoryRateLimitBackend()
        else:
            for warning in settings.production_warnings():
                log.warning(warning)
            _backend = InMemoryRateLimitBackend()
    return _backend


def reset_backend_for_tests() -> None:
    """Test-only: force the next get_backend() call to re-read settings and
    build a fresh backend (and a fresh InMemoryRateLimitBackend's counters,
    so tests don't leak state into each other)."""
    global _backend
    with _backend_lock:
        _backend = None


# ══════════════════════════════════════════════════════════════════════════
# Identity extraction
# ══════════════════════════════════════════════════════════════════════════
def client_ip(request: Request) -> str:
    """The rate-limit identity for unauthenticated/IP-scoped routes.
    X-Forwarded-For is only trusted if TRUST_PROXY_HEADERS is explicitly
    enabled — see app/config.py's field docstring for why blindly trusting
    it is a spoofing/bypass vector."""
    settings = get_settings()
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def key_hash(value: str) -> str:
    """Never use a raw secret (an adapter API key) as a Redis key or in a
    log line — hash it. A truncated SHA-256 is plenty to bucket by identity
    without leaking any part of the underlying secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def check_rate_limit(scope: str, identity: str, request: Request) -> None:
    """Raises HTTPException(429) if `identity` has exceeded `scope`'s
    limit. Public entry point — app/api.py and app/admin.py call this
    directly from small route-local dependency functions that resolve
    identity themselves (an admin's id, an adapter key's hash, or an IP),
    which keeps this module free of any import on api.py/admin.py and
    avoids a circular import rather than working around one."""
    limit, window = RATE_LIMITS[scope]
    key = f"{scope}:{identity}"
    result = get_backend().hit(key, limit, window)
    if not result.allowed:
        log.warning(
            "rate_limit_exceeded",
            extra={
                "scope": scope, "identity": identity, "limit": limit,
                "window_seconds": window, "retry_after": result.retry_after,
                "path": request.url.path,
            },
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests — slow down and retry after the interval below.",
            headers={"Retry-After": str(result.retry_after)},
        )


# ══════════════════════════════════════════════════════════════════════════
# The two IP-scoped dependencies need no identity resolved elsewhere, so
# they're plain, ready-to-use dependencies. The admin/adapter-scoped limits
# are defined in app/admin.py and app/api.py themselves (see their own
# _admin_write_limit/_admin_read_limit/_adapter_write_limit/
# _adapter_read_limit), since resolving "which admin" or "which adapter
# key" requires their own require_admin/require_adapter_key dependencies —
# importing those here would create the exact circular import this
# call-site-composition approach avoids.
# ══════════════════════════════════════════════════════════════════════════
def admin_login_limit(request: Request) -> None:
    check_rate_limit("admin_login", client_ip(request), request)


def admin_bootstrap_limit(request: Request) -> None:
    check_rate_limit("admin_bootstrap", client_ip(request), request)


def telegram_webhook_limit(request: Request) -> None:
    check_rate_limit("telegram_webhook", client_ip(request), request)
