"""Application configuration.

Single, validated source of truth for all settings. Everything is driven by
environment variables (prefixed ``APP_``) so the same image runs unchanged
across local, staging, and production. No other module should call
``os.getenv`` directly — they import ``get_settings`` instead.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

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

    # --- Password reset (AH2) ---
    # Far shorter than either token above, and for a different reason: a reset
    # link grants the power to *change* the password, and it travels through
    # email — a channel the platform does not control and cannot revoke. The
    # window is sized for someone acting on a link they just asked for.
    password_reset_token_ttl_seconds: int = Field(default=1_800, gt=0)  # 30 minutes
    # Where the reset link points: the frontend route that collects the new
    # password. Configured rather than derived because the API and the UI need
    # not share an origin, and hard-coding a development address into service
    # logic would ship it to production. ``None`` means resets cannot be sent —
    # the endpoint stays enumeration-safe either way.
    password_reset_url_base: str | None = None

    # --- Refresh cookie (AH3) ---
    # The refresh token lives in an HttpOnly cookie rather than in storage the
    # browser's JavaScript can read. The name is not a secret — it is the only
    # part of the cookie anyone is meant to see.
    refresh_cookie_name: str = Field(default="orqent_refresh", min_length=1)
    # `Secure` refuses to send the cookie over plain HTTP. False by default so
    # local development on http://localhost works at all; the validator below
    # makes shipping that default to production impossible.
    refresh_cookie_secure: bool = False
    # `Lax` is the deliberate choice, not the lazy one. Orqent's frontend and
    # API are same-*site* in every supported deployment — a Vite proxy in
    # development, a shared registrable domain (app./api.) in production — and
    # SameSite is a site-level rule that ignores the port. Lax therefore sends
    # the cookie on the app's own requests while blocking the cross-site POSTs
    # that CSRF depends on. `none` exists for a genuinely cross-site
    # deployment and is refused without `Secure`.
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # Left unset for host-only cookies, which is what localhost needs and what
    # a single-host deployment wants. Set it only to share one cookie across
    # sibling subdomains, which widens who can receive it.
    refresh_cookie_domain: str | None = None

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

    # Which model embeds text for retrieval (Phase 10, M4). Chosen by **asking
    # the API** which models advertise `embedContent`, the same way M2's chat
    # model was chosen after an obsolete identifier returned 404:
    # `gemini-embedding-001` is the stable, generally-available one, and it
    # produces 3072-dimension vectors. A different embedding model produces
    # vectors that are not comparable with existing ones, so changing this
    # setting means re-embedding the corpus — which is why it is deployment
    # configuration and never document data.
    gemini_embedding_model: str = Field(default="models/gemini-embedding-001", min_length=1)

    # --- Reserved for later phases (declared, intentionally unused now) ---
    database_url: str | None = None
    # Declared in Phase 1 and unused until M4, which is the first milestone with
    # anything to put in a vector store.
    chroma_host: str | None = None
    chroma_port: int | None = None

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def refresh_cookie_path(self) -> str:
        """The narrowest path that still covers every endpoint using the cookie.

        Login sets it, refresh rotates it, logout clears it — all three live
        under ``/auth``, so the browser never attaches this credential to a
        workflow, run, or document request. Scoping it to ``/auth/refresh``
        alone would be narrower still and would break logout.
        """

        return f"{self.api_v1_prefix}/auth"

    @model_validator(mode="after")
    def _cookie_settings_must_be_safe(self) -> Settings:
        """Refuse the two cookie configurations that are silently insecure.

        ``SameSite=None`` without ``Secure`` is rejected by every current
        browser, so the cookie would simply never be stored — a failure that
        looks like "sessions don't persist" rather than like a misconfiguration.
        Shipping ``Secure=False`` to production would send a refresh token over
        plain HTTP, which is the whole thing this milestone moved it away from.
        """

        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError(
                "refresh_cookie_samesite='none' requires refresh_cookie_secure=True; "
                "browsers reject the combination and the cookie would never be stored."
            )
        if self.is_production and not self.refresh_cookie_secure:
            raise ValueError(
                "refresh_cookie_secure must be True in production, "
                "or the refresh token is sent over plain HTTP."
            )
        return self

    @model_validator(mode="after")
    def _credentialed_cors_cannot_be_open(self) -> Settings:
        """Refuse a wildcard origin, because credentials are always allowed.

        ``allow_credentials=True`` with ``Access-Control-Allow-Origin: *`` is
        the combination that lets any site on the internet make credentialed
        requests to this API. Browsers refuse to *honour* it, but the danger is
        the configuration existing at all: a reflected-origin workaround added
        later to "fix" the resulting breakage would be a real vulnerability.
        Refusing here means the mistake never reaches middleware.
        """

        if "*" in self.cors_origins:
            raise ValueError(
                "cors_origins must not contain '*': the API allows credentials, "
                "and a wildcard origin with credentials is never valid."
            )
        return self

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
