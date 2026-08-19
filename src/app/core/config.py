"""Application configuration.

Single, validated source of truth for all settings. Everything is driven by
environment variables (prefixed ``APP_``) so the same image runs unchanged
across local, staging, and production. No other module should call
``os.getenv`` directly — they import ``get_settings`` instead.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr, model_validator
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
        # So a field carrying a `validation_alias` can still be set by its own
        # name. Without this, `Settings(gemini_api_key=...)` is silently ignored
        # — `extra="ignore"` swallows it — and the caller gets a default while
        # believing they configured something. Aliases keep working; this only
        # adds the field name alongside them.
        populate_by_name=True,
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

    # --- AI agent execution (Phase 10, M2) ---
    gemini_api_key: SecretStr | None = Field(
        default=None,
        # **Deliberately not `APP_`-prefixed.** Every other setting here is
        # Orqent's own; this one is the credential Google's own tooling, SDKs,
        # and documentation all call `GEMINI_API_KEY`, and renaming it would mean
        # every developer and deployment translating between two names for one
        # secret. `validation_alias` overrides `env_prefix` for this field only.
        validation_alias=AliasChoices("GEMINI_API_KEY"),
    )
    """The Gemini Developer API credential, or ``None`` when unconfigured.

    ``SecretStr`` so it cannot be printed by accident: its ``repr`` and ``str``
    are ``**********``, which means a settings dump, a traceback frame, or a
    logged model object cannot leak it. Reading it requires
    ``get_secret_value()``, and exactly one module does that.

    **Optional, and that is a requirement rather than a convenience.** The
    application must start, workflows must validate, the catalogue must serve,
    and every non-AI node must run with no credential present — so this cannot be
    mandatory. Only an attempted agent execution needs it, and that failure is
    explicit (see ``Container.agent_runner``).
    """

    # Which model the `"default"` profile resolves to. Ordinary non-secret
    # configuration, so it takes the `APP_` prefix like everything else.
    #
    # `gemini-3.5-flash`, chosen by **asking the API** rather than from memory.
    # The first attempt used `gemini-2.5-flash`, which the credential-gated smoke
    # test showed returns HTTP 404 — it is no longer served on this endpoint.
    # Listing the models the Developer API actually offers, and calling the
    # candidates, is the only way to establish that; a mocked test cannot.
    #
    # A *flash* model because this is a POC integration, not a quality benchmark,
    # and it keeps smoke-test latency and quota negligible. Nothing in the code
    # depends on the choice: it is one string, resolved in one place, and
    # changing it is a deployment setting rather than a code change (ADR-013's
    # provider neutrality applies to model identity too).
    gemini_model: str = Field(default="gemini-3.5-flash", min_length=1)

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
