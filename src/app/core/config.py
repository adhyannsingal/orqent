"""Application configuration.

Single, validated source of truth for all settings. Everything is driven by
environment variables (prefixed ``APP_``) so the same image runs unchanged
across local, staging, and production. No other module should call
``os.getenv`` directly — they import ``get_settings`` instead.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Validated application settings loaded from environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    environment: Environment = Environment.LOCAL
    debug: bool = False
    app_name: str = "multi-agent-platform"

    # --- API ---
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=list)

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = True

    # --- Authentication (Phase 3, ADR-010) ---
    # Symmetric signing key for access/refresh JWTs. Optional here so that
    # importing settings never fails; the token adapter raises at construction
    # time when it is missing (same contract as ``database_url``/``create_engine``).
    jwt_secret_key: str | None = None
    # Signing algorithm. HS256 (symmetric) is the V1 choice; moving to an
    # asymmetric algorithm later is a configuration change, not a code change.
    jwt_algorithm: str = "HS256"
    # Access tokens are short-lived because they are stateless and therefore
    # cannot be revoked — the expiry *is* the revocation window (ADR-010).
    access_token_ttl_seconds: int = Field(default=900, gt=0)  # 15 minutes
    # Refresh tokens are long-lived; revocability comes from the server-side
    # hashed store with rotation, added in Phase 3B.
    refresh_token_ttl_seconds: int = Field(default=2_592_000, gt=0)  # 30 days

    # --- Worker (Phase 8, M5) ---
    # How long a claimed task is owned before another worker may reclaim it.
    # This is a presumption-of-death window, not a work budget: the heartbeat
    # extends it for as long as the worker is alive, so it should be sized by
    # how quickly a dead worker's run must be picked up, not by node duration.
    worker_lease_ttl_seconds: int = Field(default=60, gt=0)
    # How often a working worker renews. Must be comfortably shorter than the
    # TTL, so a renewal has time to fail and be retried before the lease lapses.
    worker_heartbeat_interval_seconds: int = Field(default=20, gt=0)
    # How long an idle worker waits before asking for work again.
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)

    # --- Schedule dispatcher (Phase 9, M6) ---
    # How long an idle dispatcher waits before looking for due schedules again.
    # Longer than the worker's, because it bounds *lateness* rather than
    # throughput: a schedule fires at most this long after it comes due, and
    # cron's finest granularity is a minute. There is deliberately no lease TTL
    # to match it — a dispatch is a short transaction holding a row lock, not
    # owned work that has to survive a crash.
    dispatcher_poll_interval_seconds: float = Field(default=5.0, gt=0)

    # --- Reserved for later phases (declared, intentionally unused now) ---
    database_url: str | None = None
    chroma_host: str | None = None
    chroma_port: int | None = None

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @model_validator(mode="after")
    def _heartbeat_must_outpace_expiry(self) -> Settings:
        """Refuse a configuration where the lease lapses before it is renewed.

        A heartbeat at or beyond the TTL means every worker loses its lease
        mid-run and its work is reclaimed while it is still running — the exact
        failure leasing exists to prevent, and one that would only show up under
        load. Cheaper to refuse at startup than to diagnose in production.
        """

        if self.worker_heartbeat_interval_seconds >= self.worker_lease_ttl_seconds:
            raise ValueError(
                "worker_heartbeat_interval_seconds must be shorter than "
                "worker_lease_ttl_seconds, or a lease lapses before it is renewed."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance (built once per process)."""

    return Settings()
