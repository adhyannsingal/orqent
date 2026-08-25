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


def enforce_password_complexity(value: str) -> str:
    """Apply the platform's password composition rule, or raise ``ValueError``.

    **The single definition.** Registration and password reset both call it, so
    the two cannot drift into disagreeing about what a valid password is — a
    reset flow with a laxer rule would quietly become the way to install a weak
    password. Login deliberately does not call it: raising the standard must
    never lock out an account created under the old one.

    "Special" is defined as *not alphanumeric and not whitespace*, evaluated
    over Unicode rather than an ASCII allowlist. An allowlist would silently
    reject a legitimate character somebody's keyboard produces, and the
    complement is both shorter and more permissive in the right direction.
    """

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


class RegisterRequest(BaseModel):
    """Payload for creating an account and its organization."""

    email: EmailStr
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=_MAX_PASSWORD_LENGTH)
    organization_name: str = Field(min_length=1, max_length=255)

    _check_password = field_validator("password")(enforce_password_complexity)


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


class AccessTokenResponse(BaseModel):
    """A freshly issued access token.

    Returned by login *and* refresh, since both hand back the same thing.

    **The refresh token is deliberately absent** (AH3). It travels in an
    HttpOnly cookie the backend sets, so putting it here too would defeat the
    point entirely: a value in the JSON body is a value JavaScript has read.
    There is no ``refresh_token`` field to populate, which is a stronger
    guarantee than remembering not to populate one.

    ``/auth/refresh`` and ``/auth/logout`` take **no request body** for the same
    reason — the credential comes from the cookie, so a client cannot present an
    arbitrary refresh token even if it somehow obtained one.
    """

    access_token: str
    token_type: str = "bearer"
    """How the access token must be presented: ``Authorization: Bearer <token>``."""


class ForgotPasswordRequest(BaseModel):
    """Payload for requesting a password-reset link."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Payload for setting a new password with a reset token.

    ``new_password`` rather than ``password``: the field is unambiguous next to
    the old one it replaces, and a client that sends the wrong one gets a
    validation error instead of silently resetting to the current value.

    The same length bounds and the same composition rule as registration, from
    the one definition above.
    """

    token: str = Field(min_length=1, max_length=_MAX_TOKEN_LENGTH)
    new_password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=_MAX_PASSWORD_LENGTH)

    _check_password = field_validator("new_password")(enforce_password_complexity)


class MessageResponse(BaseModel):
    """A bare acknowledgement.

    Used by both password-reset endpoints. Neither has anything to return:
    forgot-password must not say whether the account exists, and reset-password
    must not hand back a session — see ``routes.auth`` for why the user is made
    to sign in again.
    """

    message: str
