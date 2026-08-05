"""Application configuration.

Every value comes from the environment. Nothing sensitive is ever hardcoded
here — that rule exists because three GitHub PATs were previously found
hardcoded across this project (see docs/CREDENTIAL_INCIDENT.md).

Local development reads a `.env` file. Production supplies real environment
variables through the hosting platform's secret manager. `.env` is gitignored;
`.env.example` documents the variable NAMES only, never their values.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="STOPLOSS_",
        extra="ignore",
    )

    # ── Environment ────────────────────────────────────────────────────────
    ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # ── Database ───────────────────────────────────────────────────────────
    # Production: postgresql+psycopg://user:pass@host:5432/stoploss
    # Local/tests: sqlite:///./stoploss_dev.db
    DATABASE_URL: str = "sqlite:///./stoploss_dev.db"

    # ── Signing (Ed25519) ──────────────────────────────────────────────────
    # PRIVATE key is server-only and MUST NEVER be shipped in a client build.
    # Both are PEM, base64-encoded to survive env-var transport.
    # This key signs authorization GRANTS only. It must not be reused for
    # anything else — see STOPLOSS_TOTP_ENCRYPTION_KEY_B64 below for why.
    SIGNING_PRIVATE_KEY_B64: str = ""
    SIGNING_PUBLIC_KEY_B64: str = ""
    SIGNING_KEY_ID: str = "k1"          # rotation: bump when issuing a new pair

    # ── MFA secret encryption (independent of signing) ─────────────────────
    # Protects TOTP secrets at rest. Deliberately a SEPARATE secret from the
    # Ed25519 signing key — the two protect different things (grant integrity
    # vs. MFA confidentiality) and must be rotatable independently. A signing
    # key rotation (e.g. after a suspected leak) must not force every
    # customer's TOTP secret to be re-encrypted, and vice versa. Generate
    # with: python -m app.keygen
    TOTP_ENCRYPTION_KEY_B64: str = ""

    # ── Token / session policy ─────────────────────────────────────────────
    GRANT_TTL_SECONDS: int = 180        # short-lived signed authorization grant
    HEARTBEAT_INTERVAL_SECONDS: int = 90
    # 24h bounded offline tolerance. Was 72h; reduced 2026-08-05 per the tradeoff
    # analysis in docs/OFFLINE_GRACE_ANALYSIS.md — caps the worst-case window an
    # admin revocation can go unnoticed by an offline device at one day instead
    # of three, while still covering the realistic offline cases (an overnight,
    # a travel day, a bad-ISP day) that matter for this customer base.
    OFFLINE_GRACE_SECONDS: int = 86400
    SESSION_IDLE_TIMEOUT_SECONDS: int = 900  # sweeper reclaims dead sessions

    # ── Auth policy ────────────────────────────────────────────────────────
    MAX_FAILED_LOGINS: int = 5
    LOCKOUT_SECONDS: int = 900
    RECOVERY_CODE_COUNT: int = 10

    # ── API ────────────────────────────────────────────────────────────────
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = ""              # comma-separated; empty = none allowed

    @field_validator("DATABASE_URL")
    @classmethod
    def _warn_sqlite_in_prod(cls, v: str, info):
        return v

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def assert_production_ready(self) -> list[str]:
        """Return a list of blocking problems. Empty list == safe to serve prod."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if self.is_sqlite:
            problems.append(
                "DATABASE_URL is SQLite in production. SQLite does not enforce "
                "SELECT ... FOR UPDATE row locking, so the one-active-session "
                "guarantee would be unsafe under concurrent logins. Use PostgreSQL."
            )
        if self.DEBUG:
            problems.append("DEBUG is enabled in production.")
        if not self.SIGNING_PRIVATE_KEY_B64:
            problems.append("SIGNING_PRIVATE_KEY_B64 is not set.")
        if not self.SIGNING_PUBLIC_KEY_B64:
            problems.append("SIGNING_PUBLIC_KEY_B64 is not set.")
        if not self.TOTP_ENCRYPTION_KEY_B64:
            problems.append(
                "TOTP_ENCRYPTION_KEY_B64 is not set. This must be an independent "
                "secret, not the signing key — see app/config.py."
            )
        elif self.TOTP_ENCRYPTION_KEY_B64 == self.SIGNING_PRIVATE_KEY_B64:
            problems.append(
                "TOTP_ENCRYPTION_KEY_B64 is identical to SIGNING_PRIVATE_KEY_B64. "
                "These must be independent secrets — generate a separate key."
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
