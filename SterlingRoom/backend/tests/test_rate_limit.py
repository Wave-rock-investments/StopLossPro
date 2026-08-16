"""Tests for Phase 9 rate limiting (app/rate_limit.py) — launch hardening,
master-prompt §4: "Implement appropriate limits ... Tests required: limit
enforcement, reset/window behavior, IP separation, user separation,
concurrent requests, 429 responses, Retry-After, trusted internal calls,
Redis/shared-state mode, failure behavior, production configuration, bypass
prevention."

Covers three layers:
  1. The backend implementations directly (InMemoryRateLimitBackend,
     RedisRateLimitBackend against a REAL local Redis instance).
  2. check_rate_limit()/client_ip()/key_hash() with lightweight fake Request
     objects — no HTTP needed to prove identity separation and the 429 shape.
  3. Full HTTP round-trips through the real FastAPI app (TestClient) proving
     the dependencies are actually wired onto the routes that need them.
"""
from __future__ import annotations

import os
import threading
import uuid

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STERLING_ADAPTER_API_KEYS", "test-key-123,test-key-456")
os.environ.setdefault("STERLING_ADMIN_BOOTSTRAP_TOKEN", "bootstrap-secret-xyz")
os.environ.setdefault("STERLING_ADMIN_SESSION_SECRET", "admin-session-secret-for-tests")
os.environ.setdefault("STERLING_TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STERLING_TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest
from fastapi import HTTPException

from app.rate_limit import (
    InMemoryRateLimitBackend,
    RedisRateLimitBackend,
    check_rate_limit,
    client_ip,
    get_backend,
    key_hash,
    reset_backend_for_tests,
)

REDIS_TEST_URL = "redis://127.0.0.1:6399/0"


def _redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_TEST_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not _redis_available(), reason="local test Redis (port 6399) not reachable")


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """Every test starts with a fresh backend singleton — counters (and the
    in-memory-vs-Redis choice) must never leak between tests."""
    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


# ══════════════════════════════════════════════════════════════════════════
# Fakes — a minimal stand-in for FastAPI's Request, just enough surface for
# client_ip()/check_rate_limit() (request.url.path, request.client.host,
# request.headers.get(...)).
# ══════════════════════════════════════════════════════════════════════════
class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class FakeRequest:
    def __init__(self, path: str = "/x", client_host: str = "1.2.3.4", headers: dict | None = None) -> None:
        self.url = _FakeURL(path)
        self.client = _FakeClient(client_host)
        self.headers = headers or {}


# ══════════════════════════════════════════════════════════════════════════
# InMemoryRateLimitBackend — limit enforcement, window reset, key
# separation, concurrency safety.
# ══════════════════════════════════════════════════════════════════════════
class TestInMemoryBackend:
    def test_allows_up_to_limit(self):
        b = InMemoryRateLimitBackend()
        for i in range(5):
            r = b.hit("k", 5, 60)
            assert r.allowed, f"request {i + 1}/5 should be allowed"
            assert r.remaining == 5 - (i + 1)

    def test_blocks_after_limit(self):
        b = InMemoryRateLimitBackend()
        for _ in range(5):
            b.hit("k", 5, 60)
        r = b.hit("k", 5, 60)
        assert not r.allowed
        assert r.remaining == 0
        assert r.retry_after >= 1

    def test_separates_keys(self):
        """Two independent identities (two IPs, or two users) never share a
        counter — exhausting one leaves the other untouched."""
        b = InMemoryRateLimitBackend()
        for _ in range(5):
            assert b.hit("user-a", 5, 60).allowed
        assert not b.hit("user-a", 5, 60).allowed  # 6th hit — exhausted
        r = b.hit("user-b", 5, 60)
        assert r.allowed  # untouched, independent counter

    def test_window_resets(self, monkeypatch):
        clock = [1_000.0]
        monkeypatch.setattr("app.rate_limit.time.time", lambda: clock[0])
        b = InMemoryRateLimitBackend()
        for _ in range(3):
            assert b.hit("k", 3, 10).allowed
        assert not b.hit("k", 3, 10).allowed
        clock[0] += 11  # past the 10s window
        assert b.hit("k", 3, 10).allowed

    def test_concurrent_requests_never_exceed_limit(self):
        """50 threads hammering the same key with a limit of 20 must let
        through exactly 20 — no race lets more through than the limit, and
        none are lost either (the Lock in InMemoryRateLimitBackend.hit
        serializes the read-increment-write)."""
        b = InMemoryRateLimitBackend()
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            r = b.hit("shared", 20, 60)
            with lock:
                results.append(r.allowed)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert results.count(True) == 20
        assert results.count(False) == 30


# ══════════════════════════════════════════════════════════════════════════
# RedisRateLimitBackend — against a REAL local Redis (started for this
# session on 127.0.0.1:6399), proving atomicity across independent backend
# instances (simulating separate worker processes sharing one Redis), plus
# fail-open behavior when Redis is unreachable.
# ══════════════════════════════════════════════════════════════════════════
class TestRedisBackend:
    @requires_redis
    def test_allows_up_to_limit_and_blocks_after(self):
        key = f"test:{uuid.uuid4().hex}"
        b = RedisRateLimitBackend(REDIS_TEST_URL)
        for _ in range(5):
            assert b.hit(key, 5, 60).allowed
        r = b.hit(key, 5, 60)
        assert not r.allowed
        assert r.retry_after >= 1

    @requires_redis
    def test_shared_state_across_backend_instances(self):
        """Two RedisRateLimitBackend instances pointed at the same Redis
        simulate two separate worker processes — the whole point of the
        Redis backend is that they share one counter, unlike
        InMemoryRateLimitBackend where each instance is independent."""
        key = f"test:{uuid.uuid4().hex}"
        b1 = RedisRateLimitBackend(REDIS_TEST_URL)
        b2 = RedisRateLimitBackend(REDIS_TEST_URL)
        for _ in range(3):
            assert b1.hit(key, 5, 60).allowed
        for _ in range(2):
            assert b2.hit(key, 5, 60).allowed
        # 6th hit total, regardless of which "worker" makes it
        assert not b1.hit(key, 5, 60).allowed

    @requires_redis
    def test_window_resets(self):
        import time

        key = f"test:{uuid.uuid4().hex}"
        b = RedisRateLimitBackend(REDIS_TEST_URL)
        for _ in range(2):
            assert b.hit(key, 2, 2).allowed
        assert not b.hit(key, 2, 2).allowed
        time.sleep(2.3)
        assert b.hit(key, 2, 2).allowed

    def test_fails_open_when_redis_unreachable(self):
        """A configured-but-unreachable Redis must not take the whole API
        down — requests are allowed through (and loudly logged), matching
        the documented tradeoff in app/rate_limit.py's module docstring.
        Doesn't require requires_redis: this specifically tests the
        UNREACHABLE case."""
        b = RedisRateLimitBackend("redis://127.0.0.1:1/0")  # nothing listens on port 1
        r = b.hit("k", 1, 60)
        assert r.allowed
        assert r.remaining == 1


class TestBackendSelection:
    def test_selects_inmemory_when_redis_url_unset(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(REDIS_URL=""))
        assert isinstance(get_backend(), InMemoryRateLimitBackend)

    @requires_redis
    def test_selects_redis_when_configured(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(REDIS_URL=REDIS_TEST_URL))
        assert isinstance(get_backend(), RedisRateLimitBackend)

    def test_falls_back_to_inmemory_if_redis_init_fails(self, monkeypatch):
        """A garbage REDIS_URL that fails to even construct a client (not
        just fails to connect) must not crash the app — get_backend() falls
        back to in-memory rather than propagating the exception."""
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(REDIS_URL="not-a-valid-url::://"))
        assert isinstance(get_backend(), InMemoryRateLimitBackend)


# ══════════════════════════════════════════════════════════════════════════
# check_rate_limit() — the 429 shape (status code + Retry-After header),
# identity separation via fake requests.
# ══════════════════════════════════════════════════════════════════════════
class TestCheckRateLimit:
    def test_raises_429_with_retry_after_once_exhausted(self):
        for _ in range(10):
            check_rate_limit("admin_login", "ident-1", FakeRequest())
        with pytest.raises(HTTPException) as exc_info:
            check_rate_limit("admin_login", "ident-1", FakeRequest())
        assert exc_info.value.status_code == 429
        assert "Retry-After" in exc_info.value.headers
        assert int(exc_info.value.headers["Retry-After"]) >= 1
        assert exc_info.value.detail  # a safe, non-leaky message — not a stack trace

    def test_identities_are_independent(self):
        for _ in range(10):
            check_rate_limit("admin_login", "ident-a", FakeRequest())
        with pytest.raises(HTTPException):
            check_rate_limit("admin_login", "ident-a", FakeRequest())
        # ident-b has its own, untouched budget
        check_rate_limit("admin_login", "ident-b", FakeRequest())

    def test_scopes_are_independent(self):
        """Exhausting one scope (e.g. admin_login) must not affect a
        DIFFERENT scope for the same identity string."""
        for _ in range(10):
            check_rate_limit("admin_login", "shared-ident", FakeRequest())
        with pytest.raises(HTTPException):
            check_rate_limit("admin_login", "shared-ident", FakeRequest())
        check_rate_limit("admin_read", "shared-ident", FakeRequest())


# ══════════════════════════════════════════════════════════════════════════
# Trusted internal calls — adapter (StopLossPro) traffic is scoped per
# authenticated API key (hashed), never per IP. Two adapter keys behind the
# same IP must not share a quota, and the raw key must never appear in the
# hash.
# ══════════════════════════════════════════════════════════════════════════
class TestTrustedInternalCalls:
    def test_key_hash_deterministic_and_never_leaks_raw_value(self):
        h1 = key_hash("super-secret-adapter-key")
        h2 = key_hash("super-secret-adapter-key")
        h3 = key_hash("a-different-key")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16
        assert "super-secret-adapter-key" not in h1

    def test_adapter_keys_do_not_share_a_quota(self):
        req = FakeRequest()
        ident1 = key_hash("test-key-123")
        ident2 = key_hash("test-key-456")
        for _ in range(300):
            check_rate_limit("adapter_write", ident1, req)
        with pytest.raises(HTTPException):
            check_rate_limit("adapter_write", ident1, req)
        # A DIFFERENT key, even hitting the exact same route from the exact
        # same IP, is completely unaffected — this is what "do not
        # accidentally rate-limit legitimate StopLossPro traffic" means in
        # practice: identity is the key, not the network address.
        check_rate_limit("adapter_write", ident2, req)


# ══════════════════════════════════════════════════════════════════════════
# Bypass prevention — client_ip() ignores X-Forwarded-For unless
# TRUST_PROXY_HEADERS is explicitly enabled, so an attacker can't reset
# their own IP-scoped quota by sending a different spoofed header on every
# request.
# ══════════════════════════════════════════════════════════════════════════
class TestBypassPrevention:
    def test_xff_ignored_by_default(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(TRUST_PROXY_HEADERS=False))
        req = FakeRequest(client_host="10.0.0.1", headers={"x-forwarded-for": "1.2.3.4"})
        assert client_ip(req) == "10.0.0.1"

    def test_xff_honored_only_when_trusted(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(TRUST_PROXY_HEADERS=True))
        req = FakeRequest(client_host="10.0.0.1", headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
        assert client_ip(req) == "1.2.3.4"

    def test_missing_client_falls_back_safely(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr("app.rate_limit.get_settings", lambda: Settings(TRUST_PROXY_HEADERS=False))
        req = FakeRequest()
        req.client = None
        assert client_ip(req) == "unknown"


# ══════════════════════════════════════════════════════════════════════════
# Production configuration — the non-blocking warning when REDIS_URL is
# unset in production (settings.production_warnings(), consumed by
# app/main.py's startup log and app/rate_limit.py's get_backend()).
# ══════════════════════════════════════════════════════════════════════════
class TestProductionConfiguration:
    def test_warns_when_redis_unset_in_production(self):
        from app.config import Settings

        s = Settings(ENV="production", REDIS_URL="", ADAPTER_API_KEYS="k", ADMIN_SESSION_SECRET="s",
                     DATABASE_URL="postgresql://x/y")
        warnings = s.production_warnings()
        assert any("REDIS_URL" in w for w in warnings)

    def test_silent_when_redis_configured_in_production(self):
        from app.config import Settings

        s = Settings(ENV="production", REDIS_URL="redis://prod-redis:6379/0", ADAPTER_API_KEYS="k",
                     ADMIN_SESSION_SECRET="s", DATABASE_URL="postgresql://x/y")
        assert s.production_warnings() == []

    def test_silent_outside_production_regardless_of_redis(self):
        from app.config import Settings

        s = Settings(ENV="development", REDIS_URL="")
        assert s.production_warnings() == []


# ══════════════════════════════════════════════════════════════════════════
# Full HTTP round-trips — proves the dependencies are actually wired onto
# the routes (not just present as unused functions), and exercises the
# real 429 response a caller receives.
# ══════════════════════════════════════════════════════════════════════════
def _fresh_client():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import get_db
    from app.main import app
    from app.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app, TestClient(app)


@pytest.fixture()
def http():
    app, client = _fresh_client()
    with client:
        yield client
    app.dependency_overrides.clear()


BOOTSTRAP_HEADERS = {"X-Bootstrap-Token": "bootstrap-secret-xyz"}


def test_admin_login_returns_429_after_limit_with_retry_after(http):
    for _ in range(10):
        r = http.post("/admin/login", data={"email": "nobody@x.com", "password": "wrong"})
        assert r.status_code == 200  # renders the login page (with an error), not blocked yet

    r = http.post("/admin/login", data={"email": "nobody@x.com", "password": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1
    assert "detail" in r.json()


def test_admin_bootstrap_returns_429_after_limit(http):
    for i in range(5):
        r = http.post("/admin/bootstrap", data={"email": f"a{i}@x.com", "password": "x" * 12}, headers=BOOTSTRAP_HEADERS)
        assert r.status_code in (200, 409)  # 200 first time, 409 (already exists) after
    r = http.post("/admin/bootstrap", data={"email": "final@x.com", "password": "x" * 12}, headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 429


def test_xff_spoofing_does_not_bypass_admin_login_limit(http):
    """TRUST_PROXY_HEADERS defaults to False in this test env, so sending a
    different X-Forwarded-For on every request must NOT reset the quota —
    real identity is TestClient's fixed connection, not the spoofed header."""
    for i in range(10):
        r = http.post(
            "/admin/login", data={"email": "x@x.com", "password": "wrong"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert r.status_code == 200
    r = http.post(
        "/admin/login", data={"email": "x@x.com", "password": "wrong"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert r.status_code == 429


def test_admin_read_routes_require_auth_before_rate_limit_matters(http):
    # Unauthenticated request redirects to login (require_admin), which runs
    # BEFORE the rate limiter — an anonymous caller can't burn an admin's
    # quota just by hitting a read route repeatedly.
    r = http.get("/admin/calls", follow_redirects=False)
    assert r.status_code == 303


def _bootstrap_and_login(client, email="owner@sterlingroom.test", password="a-strong-passphrase-1"):
    r = client.post("/admin/bootstrap", data={"email": email, "password": password}, headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 200, r.text
    # follow_redirects=False: TestClient follows redirects by default, which
    # would silently swallow a login failure that also happens to 3xx to
    # somewhere renderable — asserting on the raw login response is the
    # actual proof credentials worked.
    r = client.post("/admin/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def test_admin_read_rate_limited_per_admin_once_authenticated(http, monkeypatch):
    import app.rate_limit as rl

    monkeypatch.setitem(rl.RATE_LIMITS, "admin_read", (3, 60))
    _bootstrap_and_login(http)
    for _ in range(3):
        r = http.get("/admin/calls")
        assert r.status_code == 200
    r = http.get("/admin/calls")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_adapter_write_rate_limited_per_key_not_per_ip(monkeypatch):
    """Same TestClient (same "IP") — two different adapter keys must have
    independent quotas. This is the HTTP-level proof of "trusted internal
    calls" scoping (the unit-level proof is
    TestTrustedInternalCalls.test_adapter_keys_do_not_share_a_quota)."""
    import app.rate_limit as rl

    monkeypatch.setitem(rl.RATE_LIMITS, "adapter_write", (2, 60))
    # app.api's `settings` is a module-level singleton captured at import
    # time (before this test's env vars can influence it), so the second
    # key is granted directly on that instance rather than via env/reload.
    import app.api as api_module

    monkeypatch.setattr(api_module.settings, "ADAPTER_API_KEYS", "test-key-123,test-key-456")
    app, client = _fresh_client()
    key1 = {"Authorization": "Bearer test-key-123"}
    key2 = {"Authorization": "Bearer test-key-456"}
    with client:
        for i in range(2):
            r = client.post("/api/v1/calls", json={
                "source_call_id": f"rl-{i}", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
            }, headers=key1)
            assert r.status_code == 200, r.text

        r = client.post("/api/v1/calls", json={
            "source_call_id": "rl-3", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=key1)
        assert r.status_code == 429
        assert "Retry-After" in r.headers

        # key2 is a completely independent identity — unaffected by key1's exhaustion.
        r2 = client.post("/api/v1/calls", json={
            "source_call_id": "rl-key2", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=key2)
        assert r2.status_code == 200, r2.text
    app.dependency_overrides.clear()


def test_telegram_webhook_rate_limited(monkeypatch):
    import app.rate_limit as rl

    monkeypatch.setitem(rl.RATE_LIMITS, "telegram_webhook", (2, 60))
    app, client = _fresh_client()
    with client:
        for _ in range(2):
            r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={"update_id": 1})
            assert r.status_code in (200, 400, 404)  # rate limiter runs before payload/secret validation
        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={"update_id": 1})
        assert r.status_code == 429
    app.dependency_overrides.clear()
