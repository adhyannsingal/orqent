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
