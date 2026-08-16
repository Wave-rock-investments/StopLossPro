"""Application configuration.

Every value comes from the environment, following the exact same discipline
as Working/backend/app/config.py in this repo: nothing sensitive is ever
hardcoded here. That rule exists project-wide because of a real incident
(three GitHub PATs previously found hardcoded — see this repo's
PROJECT_STATUS.md §3). Sterling_Room is a deliberately SEPARATE service from
Working/backend (StopLossPro Pro's licensing API) — separate database,
separate deploy target, separate secrets — per an explicit hosting decision
made 2026-08-16 (Sterling_Room audit, "keep separate").

Local development reads a `.env` file. Production supplies real environment
variables through the hosting platform's secret manager. `.env` must stay
gitignored; `.env.example` documents variable NAMES only, never values.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="STERLING_",
        extra="ignore",
    )

    # ── Environment ────────────────────────────────────────────────────────
    ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # ── Database ───────────────────────────────────────────────────────────
    # Production: postgresql+psycopg://user:pass@host:5432/sterling_room
    # Local/tests: sqlite:///./sterling_room_dev.db
    DATABASE_URL: str = "sqlite:///./sterling_room_dev.db"

    # ── API auth ───────────────────────────────────────────────────────────
    # Shared-secret bearer token(s) the StopLossPro adapter (and any other
    # trusted caller) presents on POST /calls. Comma-separated so a key can be
    # rotated by adding the new one before removing the old one. Empty means
    # the endpoint is closed (fails safe, not open) — see api.py.
    ADAPTER_API_KEYS: str = ""

    # ── Telegram ───────────────────────────────────────────────────────────
    # Per master-prompt §17.3: the bot and channels already exist. Values are
    # supplied by the operator at deploy time — never hardcoded, never
    # fabricated here.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_FREE_CHAT_ID: str = ""
    TELEGRAM_PREMIUM_CHAT_ID: str = ""
    # No separate TELEGRAM_RESULTS_CHAT_ID — per the 2026-08-16 production
    # Telegram architecture decision there is no separate Results
    # destination; verified CLOSED/STOPPED results post to
    # TELEGRAM_FREE_CHAT_ID (see app/api.py::transition_call). A stray
    # STERLING_TELEGRAM_RESULTS_CHAT_ID left set in an old .env/host secret
    # is harmless — model_config's extra="ignore" above means it's simply
    # unused, not an error.
    TELEGRAM_CHANNEL_LINK: str = ""

    # ── Interactive bot (Phase 4) ────────────────────────────────────────────
    TELEGRAM_FREE_CHANNEL_LINK: str = ""      # t.me/... shown by the FREE ACCESS button
    TELEGRAM_SUPPORT_CONTACT: str = ""        # @username or contact info shown by SUPPORT
    # Secret path segment for the webhook URL (…/telegram/webhook/<this>) —
    # Telegram's own recommended way to make the webhook URL unguessable,
    # since Telegram webhooks have no other built-in caller auth. Required in
    # production (see assert_production_ready) — an empty value means the
    # webhook route 404s, matching the adapter's fail-closed default.
    TELEGRAM_WEBHOOK_SECRET: str = ""

    # ── Admin bootstrap (mirrors Working/backend's pattern) ─────────────────
    ADMIN_BOOTSTRAP_TOKEN: str = ""
    # Signs admin session cookies (app/security.py). Generate with e.g.
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    # Falls back to ADAPTER_API_KEYS in dev only if unset — see
    # assert_production_ready(), which refuses to boot in production without
    # a real value here.
    ADMIN_SESSION_SECRET: str = ""

    # ── Payments (Phase 4-5) ─────────────────────────────────────────────────
    # Which PaymentProvider app/payments.py.get_provider() returns. "manual"
    # (cash/crypto, admin-verified) is the only implementation that exists —
    # see app/payments.py's module docstring for why a real processor isn't
    # wired in yet.
    PAYMENT_PROVIDER: str = "manual"

    # ── API ────────────────────────────────────────────────────────────────
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = ""

    # ── Rate limiting (Phase 9 — launch hardening) ───────────────────────────
    # See app/rate_limit.py for the full design. Empty means "use the
    # in-memory backend" — correct for a single-process deployment (dev,
    # staging with one worker), NOT correct once more than one worker
    # process is running (each process would get its own counters, silently
    # multiplying the effective limit). Set this before scaling beyond one
    # worker in production — assert_production_ready() warns (does not
    # block boot) if it's unset in production, since a single-worker
    # production deployment is a legitimate, if constrained, choice.
    REDIS_URL: str = ""
    # Whether to trust the X-Forwarded-For header for rate-limit identity
    # (client IP). Only enable this if Sterling_Room sits behind a proxy/load
    # balancer that YOU control and that overwrites/strips any
    # client-supplied X-Forwarded-For before setting its own — otherwise any
    # client can forge this header and evade or frame another IP's rate
    # limit. Default false: trust only the TCP-level peer address.
    TRUST_PROXY_HEADERS: bool = False

    # ── Background worker (Phase 10 — launch hardening) ──────────────────────
    # app/worker.py's loop mode sleeps this long between ticks. Irrelevant in
    # --once mode (a cron-triggered invocation). 60s default: frequent enough
    # that a failed Telegram delivery or an expiring subscription is caught
    # promptly, infrequent enough not to matter for load on a single-digit
    # req/s service. Tune per deployment, not a value worth over-thinking.
    WORKER_INTERVAL_SECONDS: int = 60

    # ── Freemium call delivery (2026-08-16 production architecture) ─────────
    # Premium receives every call immediately with full execution detail.
    # Free receives a separate, deliberately sanitized teaser (see
    # app/telegram_bot.py::render_free_teaser_message — no Entry/SL/TP/risk
    # numbers) only after this delay, via app/worker.py's
    # process_delayed_free_calls job. 900s (15 min) is the value explicitly
    # confirmed for production launch — override via
    # STERLING_FREE_CALL_DELAY_SECONDS if that changes.
    FREE_CALL_DELAY_SECONDS: int = 900

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def adapter_api_key_list(self) -> list[str]:
        return [k.strip() for k in self.ADAPTER_API_KEYS.split(",") if k.strip()]

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN)

    def assert_production_ready(self) -> list[str]:
        """Return a list of blocking problems. Empty list == safe to serve prod.

        Mirrors Working/backend/app/config.py's boot-guardrail pattern:
        fail loudly at startup, not quietly at 3am under load.
        """
        problems: list[str] = []
        if not self.is_production:
            return problems
        if self.is_sqlite:
            problems.append(
                "DATABASE_URL is SQLite in production. The idempotent call-"
                "creation guarantee (a unique constraint on source_call_id) "
                "works on SQLite too, but production-scale concurrent writes "
                "need PostgreSQL. Use PostgreSQL."
            )
        if self.DEBUG:
            problems.append("DEBUG is enabled in production.")
        if not self.ADAPTER_API_KEYS:
            problems.append(
                "ADAPTER_API_KEYS is empty. POST /calls would have no valid "
                "caller and fails closed by design, but that means nothing "
                "can ever reach it — set at least one key."
            )
        if not self.ADMIN_SESSION_SECRET:
            problems.append(
                "ADMIN_SESSION_SECRET is empty. Admin sessions would fall back "
                "to signing with ADAPTER_API_KEYS, re-coupling two secrets that "
                "should be independent (same reasoning Working/backend applies "
                "to its signing/TOTP key split) — set an independent value."
            )
        if self.telegram_configured and not self.TELEGRAM_WEBHOOK_SECRET:
            problems.append(
                "TELEGRAM_BOT_TOKEN is set but TELEGRAM_WEBHOOK_SECRET is empty. "
                "The webhook route fails closed (404) without it — set a secret "
                "path segment before registering the webhook URL with Telegram."
            )
        return problems

    def production_warnings(self) -> list[str]:
        """Non-blocking warnings — things worth an operator's attention in
        production but not worth refusing to boot over (unlike
        assert_production_ready's problems list). Logged at startup."""
        warnings: list[str] = []
        if self.is_production and not self.REDIS_URL:
            warnings.append(
                "REDIS_URL is unset in production. Rate limiting will use the "
                "in-memory backend, which is only correct for a single worker "
                "process — if this deployment ever runs more than one worker, "
                "each gets independent counters and the effective rate limit "
                "silently multiplies. Fine for a single-worker launch; set "
                "REDIS_URL before scaling out."
            )
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
