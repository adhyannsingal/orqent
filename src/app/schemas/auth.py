"""Authentication request and response models.

Transport only: these describe the JSON on the wire and nothing else. They hold
no behaviour, import no ORM model, and are never passed into the service layer —
routes unpack them into plain arguments so the service stays independent of HTTP.

Two response shapes exist for what looks like one concept, and the difference is
deliberate. :class:`UserResponse` describes a user that was just read from the
database. :class:`CurrentUserResponse` describes what an access token *asserts*,
which is strictly less: identity, tenant, and roles, with no email, because
resolving the caller never touches the database (ADR-010). Collapsing them into
one model would mean a nullable ``email`` that is always null on ``/auth/me``.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

# Argon2 hashes whatever it is given, so an unbounded password is an invitation
# to burn CPU on a megabyte of input. The ceiling is a resource guard, not a
# policy; it is far above any real passphrase.
_MAX_PASSWORD_LENGTH = 1024

# A floor low enough not to reject reasonable passphrases. Registration also
# applies a modest composition rule below; login remains shape-agnostic so old
# accounts cannot be locked out by a policy change.
_MIN_PASSWORD_LENGTH = 8

# Signed tokens are a few hundred bytes; the ceiling only stops a caller making
# the server hash and parse something enormous.
_MAX_TOKEN_LENGTH = 4096


class RegisterRequest(BaseModel):
    """Payload for creating an account and its organization."""

    email: EmailStr
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=_MAX_PASSWORD_LENGTH)
    organization_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def _password_must_meet_complexity(cls, value: str) -> str:
        """Registration policy; login deliberately stays shape-agnostic below."""

        missing: list[str] = []
        if not any(character.isalpha() for character in value):
            missing.append("a letter")
        if not any(character.isdigit() for character in value):
            missing.append("a number")
        if not any(not character.isalnum() and not character.isspace() for character in value):
            missing.append("a special character")
        if missing:
            raise ValueError(f"Password must include {', '.join(missing)}.")
        return value


class LoginRequest(BaseModel):
    """Payload for exchanging credentials for tokens."""

    email: EmailStr
    # Deliberately no minimum length, unlike registration. Raising the minimum
    # later must not lock out accounts created under the old rule, and a 422 for
    # a too-short password would answer a login attempt differently depending on
    # the input's shape — the endpoint should return one 401 for every failure.
    password: str = Field(max_length=_MAX_PASSWORD_LENGTH)


class UserResponse(BaseModel):
    """A user account, as read from storage."""

    public_id: str
    email: EmailStr
    organization_id: str
    """The organization's *public* ID; internal keys are never exposed (ADR-004)."""

    roles: list[str]


class CurrentUserResponse(BaseModel):
    """The caller's identity, as asserted by their access token.

    No email: the token does not carry one, and inventing a lookup to supply it
    would trade the point of stateless verification for a cosmetic field.
    """

    public_id: str
    organization_id: str
    roles: list[str]


class RefreshRequest(BaseModel):
    """Payload carrying a refresh token.

    Used by both ``/auth/refresh`` and ``/auth/logout``: each presents the same
    credential, and giving them separate identical models would only duplicate
    the field.
    """

    refresh_token: str = Field(min_length=1, max_length=_MAX_TOKEN_LENGTH)


class TokenPairResponse(BaseModel):
    """A freshly issued access and refresh token.

    Returned by login *and* refresh, since both hand back the same thing —
    named for the payload rather than for one of its callers.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    """How the access token must be presented: ``Authorization: Bearer <token>``."""
