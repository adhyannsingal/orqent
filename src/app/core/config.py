"""Application configuration.

Single, validated source of truth for all settings. Everything is driven by
environment variables (prefixed ``APP_``) so the same image runs unchanged
across local, staging, and production. No other module should call
``os.getenv`` directly — they import ``get_settings`` instead.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
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

    # --- Reserved for later phases (declared, intentionally unused now) ---
    database_url: str | None = None
    chroma_host: str | None = None
    chroma_port: int | None = None

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance (built once per process)."""

    return Settings()
