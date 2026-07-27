"""AuthService use cases, against in-memory doubles (no database).

The doubles enforce the same uniqueness rules the schema does and discard
pending work on rollback, so these tests exercise real behaviour rather than
call recording.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors import AuthenticationError, ConflictError, InfrastructureError
from app.domain.value_objects.token import TokenType
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_hashing import hash_token
from app.services.auth_service import _DUMMY_PASSWORD_HASH, DEFAULT_ROLE, AuthService, _slugify
from tests.unit.fakes import (
    FakeDatabase,
    FakePasswordHasher,
    FakeTokenService,
    FakeUnitOfWorkFactory,
    FakeUserRepository,
    integrity_error,
)

EMAIL = "founder@example.com"
PASSWORD = "correct horse battery staple"
ORGANIZATION = "Acme Inc"


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    database.add_role(DEFAULT_ROLE)
    return database


@pytest.fixture
def hasher() -> FakePasswordHasher:
    return FakePasswordHasher()


@pytest.fixture
def tokens() -> FakeTokenService:
    return FakeTokenService()


@pytest.fixture
def factory(db: FakeDatabase) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(db)


@pytest.fixture
def service(
    factory: FakeUnitOfWorkFactory, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> AuthService:
    return AuthService(factory, hasher, tokens)


async def _register(service: AuthService, **overrides: str) -> User:
    payload = {"email": EMAIL, "password": PASSWORD, "organization_name": ORGANIZATION}
    payload.update(overrides)
    return await service.register(**payload)


# --- Registration -----------------------------------------------------------


async def test_register_creates_user_organization_and_assignment(
    service: AuthService, db: FakeDatabase
) -> None:
    user = await _register(service)

    assert user.email == EMAIL
    assert len(db.organizations) == 1
    assert len(db.users) == 1
    assert len(db.user_roles) == 1


async def test_register_creates_the_organization_with_the_given_name(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    organization = db.organizations[0]
    assert organization.name == ORGANIZATION
    assert organization.slug == "acme-inc"


async def test_register_grants_the_owner_role(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)

    assert db.user_roles[0].role.name == DEFAULT_ROLE


async def test_register_stores_a_hash_from_the_port_not_the_password(
    service: AuthService, db: FakeDatabase, hasher: FakePasswordHasher
) -> None:
    # That the hash is irreversible is the adapter's guarantee, covered in
    # test_password_hasher; what matters here is that the service routes the
    # password through the port and stores only what came back.
    await _register(service)

    assert hasher.hashed == [PASSWORD]
    assert db.users[0].password_hash == f"hashed::{PASSWORD}"


async def test_register_links_the_user_to_the_organization(
    service: AuthService, db: FakeDatabase
) -> None:
    user = await _register(service)

    assert user.organization is db.organizations[0]
    assert user.organization_id == db.organizations[0].id


async def test_registered_user_is_returned_with_roles_loaded(service: AuthService) -> None:
    # The API layer serializes this outside the session; a relationship that was
    # never loaded would raise there rather than here.
    user = await _register(service)

    assert {assignment.role.name for assignment in user.user_roles} == {DEFAULT_ROLE}


async def test_register_normalizes_the_email(service: AuthService, db: FakeDatabase) -> None:
    await _register(service, email="  Founder@Example.COM  ")

    assert db.users[0].email == EMAIL


async def test_register_rejects_a_duplicate_email(service: AuthService) -> None:
    await _register(service)

    with pytest.raises(ConflictError):
        await _register(service, organization_name="Second Co")


async def test_duplicate_email_leaves_nothing_behind(
    service: AuthService, db: FakeDatabase
) -> None:
    # The duplicate check happens before any write, so the second attempt must
    # not leave a stray organization.
    await _register(service)

    with pytest.raises(ConflictError):
        await _register(service, organization_name="Second Co")

    assert len(db.organizations) == 1
    assert len(db.users) == 1


async def test_register_fails_when_the_role_catalog_is_not_seeded(
    factory: FakeUnitOfWorkFactory, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # A deployment that skipped the seeding migration, not a client error.
    empty = FakeDatabase()
    service = AuthService(FakeUnitOfWorkFactory(empty), hasher, tokens)

    with pytest.raises(InfrastructureError, match=DEFAULT_ROLE):
        await _register(service)


async def test_register_uses_one_transaction_and_commits_once(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    await _register(service)

    assert len(factory.created) == 1
    assert factory.only.entered == 1
    assert factory.only.commit_calls == 1


# --- Registration: slugs ----------------------------------------------------


async def test_second_organization_with_the_same_name_gets_a_suffixed_slug(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)
    await _register(service, email="second@example.com")

    assert [o.slug for o in db.organizations] == ["acme-inc", "acme-inc-2"]


async def test_slug_falls_back_to_a_unique_suffix_when_numbering_is_exhausted(
    service: AuthService, db: FakeDatabase
) -> None:
    # Probing forever would make a popular name cost unbounded queries.
    for index in range(7):
        await _register(service, email=f"user{index}@example.com")

    slugs = [o.slug for o in db.organizations]
    assert slugs[:6] == [
        "acme-inc",
        "acme-inc-2",
        "acme-inc-3",
        "acme-inc-4",
        "acme-inc-5",
        "acme-inc-6",
    ]
    assert slugs[6].startswith("acme-inc-")
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Inc", "acme-inc"),
        ("  Spaced  Out  ", "spaced-out"),
        ("Café Zürich", "cafe-zurich"),  # accents folded, not dropped
        ("A/B  Testing!", "a-b-testing"),
        ("--dashes--", "dashes"),
        ("!!!", "org"),  # nothing usable survives
        ("日本語", "org"),  # no ASCII equivalent
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert _slugify(name) == expected


def test_slugify_bounds_the_length_to_leave_room_for_a_suffix() -> None:
    assert len(_slugify("x" * 500)) == 200


# --- Registration: failure and rollback -------------------------------------


async def test_concurrent_duplicate_becomes_a_conflict_error(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # Simulates another request claiming the email between the pre-check and the
    # insert: the database refuses, and the service reports it in domain terms
    # rather than leaking IntegrityError.
    failing = FakeUserRepository(db, raise_on_add=integrity_error("uq_users_email_active"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = AuthService(factory, hasher, tokens)

    with pytest.raises(ConflictError):
        await _register(service)


async def test_failed_registration_rolls_everything_back(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # The organization is written before the user, so a later failure must not
    # leave an orphan tenant behind.
    failing = FakeUserRepository(db, raise_on_add=integrity_error("uq_users_email_active"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = AuthService(factory, hasher, tokens)

    with pytest.raises(ConflictError):
        await _register(service)

    assert db.organizations == []
    assert db.users == []
    assert db.user_roles == []
    assert factory.only.commit_calls == 0
    assert factory.only.rollback_calls == 1


async def test_unexpected_failure_propagates_and_rolls_back(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # Only integrity errors are translated; anything else is a bug and must not
    # be disguised as a conflict.
    failing = FakeUserRepository(db, raise_on_add=RuntimeError("boom"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = AuthService(factory, hasher, tokens)

    with pytest.raises(RuntimeError, match="boom"):
        await _register(service)

    assert db.organizations == []
    assert factory.only.commit_calls == 0


# --- Login ------------------------------------------------------------------


async def test_login_returns_a_token_pair(service: AuthService) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    assert isinstance(pair, TokenPair)
    assert pair.access_token
    assert pair.refresh_token
    assert pair.access_token != pair.refresh_token


async def test_login_issues_one_access_and_one_refresh_token(
    service: AuthService, tokens: FakeTokenService
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert [issued.claims.token_type for issued in tokens.issued] == [
        TokenType.ACCESS,
        TokenType.REFRESH,
    ]


async def test_login_puts_the_users_identity_in_the_token(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    claims = tokens.issued[0].claims
    assert claims.subject == db.users[0].public_id
    assert claims.organization_id == db.organizations[0].public_id
    assert claims.roles == frozenset({DEFAULT_ROLE})


async def test_login_accepts_a_differently_cased_email(service: AuthService) -> None:
    await _register(service)

    assert await service.login(email="  FOUNDER@EXAMPLE.com ", password=PASSWORD)


async def test_login_rejects_an_unknown_email(service: AuthService) -> None:
    with pytest.raises(AuthenticationError):
        await service.login(email="nobody@example.com", password=PASSWORD)


async def test_login_rejects_a_wrong_password(service: AuthService) -> None:
    await _register(service)

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password="not the password")


async def test_login_rejects_an_inactive_user(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)
    db.users[0].is_active = False

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password=PASSWORD)


async def test_every_login_failure_reports_the_same_message(
    service: AuthService, db: FakeDatabase
) -> None:
    # Distinguishable messages would turn the login form into an oracle for
    # which addresses have accounts.
    await _register(service)
    messages = set()

    for email, password in (
        ("nobody@example.com", PASSWORD),
        (EMAIL, "wrong"),
    ):
        with pytest.raises(AuthenticationError) as caught:
            await service.login(email=email, password=password)
        messages.add(caught.value.message)

    db.users[0].is_active = False
    with pytest.raises(AuthenticationError) as caught:
        await service.login(email=EMAIL, password=PASSWORD)
    messages.add(caught.value.message)

    assert len(messages) == 1


# --- Login: timing ----------------------------------------------------------


async def test_unknown_email_still_performs_a_verification(
    service: AuthService, hasher: FakePasswordHasher
) -> None:
    # Without this, "no such account" returns in microseconds while a real
    # attempt takes ~80 ms, and the difference is measurable over the network.
    with pytest.raises(AuthenticationError):
        await service.login(email="nobody@example.com", password=PASSWORD)

    assert hasher.verified == [(PASSWORD, _DUMMY_PASSWORD_HASH)]


async def test_inactive_user_is_checked_after_verifying_the_password(
    service: AuthService, db: FakeDatabase, hasher: FakePasswordHasher
) -> None:
    # Short-circuiting on is_active would make a disabled account cheaper than
    # an enabled one, leaking that the address exists.
    await _register(service)
    db.users[0].is_active = False
    hasher.verified.clear()

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password=PASSWORD)

    assert hasher.verified == [(PASSWORD, f"hashed::{PASSWORD}")]


def test_dummy_hash_is_a_real_argon2_hash() -> None:
    # If it were malformed, verification would fail instantly instead of doing
    # the work, defeating the whole point of the constant.
    assert Argon2PasswordHasher().verify_password("anything", _DUMMY_PASSWORD_HASH) is False


def test_dummy_hash_uses_current_parameters() -> None:
    # Guards the timing defence: a dummy built with cheaper parameters than the
    # library's current defaults would be faster than a real verification.
    assert Argon2PasswordHasher().needs_rehash(_DUMMY_PASSWORD_HASH) is False


# --- Login: rehash ----------------------------------------------------------


async def test_login_rehashes_when_the_stored_hash_is_outdated(
    factory: FakeUnitOfWorkFactory, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    upgrading = FakePasswordHasher(needs_rehash=True)
    service = AuthService(factory, upgrading, tokens)
    await _register(service)
    upgrading.hashed.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    # Login is the only moment the plaintext is available, so it is the only
    # chance to upgrade the stored hash.
    assert upgrading.hashed == [PASSWORD]
    assert db.users[0].password_hash == f"hashed::{PASSWORD}"


async def test_login_does_not_rehash_when_the_hash_is_current(
    service: AuthService, hasher: FakePasswordHasher
) -> None:
    await _register(service)
    hasher.hashed.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    assert hasher.hashed == []


# --- Login: refresh token persistence ---------------------------------------


async def test_login_persists_exactly_one_refresh_token(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert len(db.refresh_tokens) == 1


async def test_persisted_refresh_token_matches_the_issued_token(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    stored = db.refresh_tokens[0]
    refresh_claims = tokens.issued[1].claims
    assert stored.jti == refresh_claims.jti
    assert stored.user_id == db.users[0].id
    # The row and the credential must expire at the same instant, or one
    # outlives the other.
    assert stored.expires_at == refresh_claims.expires_at
    assert stored.token_hash == hash_token(pair.refresh_token)


async def test_the_refresh_token_itself_is_never_stored(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    stored = db.refresh_tokens[0]
    assert pair.refresh_token not in stored.token_hash
    assert stored.token_hash != pair.refresh_token


async def test_stored_refresh_token_starts_live(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert db.refresh_tokens[0].revoked_at is None


async def test_each_login_starts_a_new_token_family(service: AuthService, db: FakeDatabase) -> None:
    # Families exist so that revoking one compromised session does not end the
    # user's other sessions.
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)
    await service.login(email=EMAIL, password=PASSWORD)

    families = {token.family_id for token in db.refresh_tokens}
    assert len(families) == 2


# --- Login: transaction boundaries ------------------------------------------


async def test_login_uses_one_transaction_and_commits_once(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    await _register(service)
    factory.created.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    assert len(factory.created) == 1
    assert factory.created[0].commit_calls == 1


async def test_failed_login_commits_nothing(
    service: AuthService, factory: FakeUnitOfWorkFactory, db: FakeDatabase
) -> None:
    await _register(service)
    factory.created.clear()

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password="wrong")

    assert factory.created[0].commit_calls == 0
    assert factory.created[0].rollback_calls == 1
    assert db.refresh_tokens == []


# --- Layering ---------------------------------------------------------------


def test_service_module_imports_no_security_vendor() -> None:
    # Run in a fresh interpreter: another test importing the adapters would
    # otherwise make jwt/argon2 appear in sys.modules and hide a real leak.
    program = textwrap.dedent(
        """
        import sys
        import app.services.auth_service  # noqa: F401
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in {"jwt", "argon2"})
        print(",".join(leaked))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""


def test_service_knows_nothing_about_http() -> None:
    # Business code raises domain errors; only the API layer knows status codes.
    source = inspect.getsource(sys.modules[AuthService.__module__])

    assert "HTTPException" not in source
    assert "fastapi" not in source


# --- Refresh: rotation ------------------------------------------------------


async def _login(service: AuthService) -> TokenPair:
    await _register(service)
    return await service.login(email=EMAIL, password=PASSWORD)


async def test_refresh_returns_a_new_pair(service: AuthService) -> None:
    original = await _login(service)

    rotated = await service.refresh(original.refresh_token)

    assert isinstance(rotated, TokenPair)
    assert rotated.access_token != original.access_token
    assert rotated.refresh_token != original.refresh_token


async def test_refresh_revokes_the_presented_token(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    presented = db.refresh_tokens[0]

    await service.refresh(original.refresh_token)

    assert presented.revoked_at is not None


async def test_refresh_stores_a_successor_in_the_same_family(
    service: AuthService, db: FakeDatabase
) -> None:
    # The lineage is what lets one compromised session be revoked wholesale.
    original = await _login(service)
    family = db.refresh_tokens[0].family_id

    await service.refresh(original.refresh_token)

    assert len(db.refresh_tokens) == 2
    assert {token.family_id for token in db.refresh_tokens} == {family}
    assert db.refresh_tokens[1].revoked_at is None


async def test_successor_hash_matches_the_returned_token(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)

    rotated = await service.refresh(original.refresh_token)

    assert db.refresh_tokens[1].token_hash == hash_token(rotated.refresh_token)


async def test_refresh_picks_up_a_role_change(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    # Access tokens carry roles, so a revoked or granted role only takes effect
    # when a new one is minted. Refresh re-reads the user for exactly this.
    original = await _login(service)
    admin = db.add_role("admin")
    UserRole(user=db.users[0], role=admin)  # backref populates user.user_roles

    await service.refresh(original.refresh_token)

    assert tokens.issued[-1].claims.roles == frozenset({DEFAULT_ROLE, "admin"})


async def test_refresh_uses_one_transaction(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    original = await _login(service)
    factory.created.clear()

    await service.refresh(original.refresh_token)

    assert len(factory.created) == 1
    assert factory.created[0].commit_calls == 1


# --- Refresh: rejection -----------------------------------------------------


async def test_refresh_rejects_an_access_token(service: AuthService) -> None:
    # Signed by the same key, so only the token_type claim stops it.
    original = await _login(service)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.access_token)


async def test_refresh_rejects_an_unverifiable_token(service: AuthService) -> None:
    await _login(service)

    with pytest.raises(AuthenticationError):
        await service.refresh("not-a-token")


async def test_refresh_rejects_a_token_with_no_stored_row(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)
    db.refresh_tokens.clear()

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_rejects_a_hash_mismatch(service: AuthService, db: FakeDatabase) -> None:
    # Would mean the presented token is not the one this row recorded.
    original = await _login(service)
    db.refresh_tokens[0].token_hash = hash_token("a different token")

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_hash_mismatch_does_not_revoke_the_family(
    service: AuthService, db: FakeDatabase
) -> None:
    # Checked before the replay test precisely so a token merely claiming this
    # jti cannot trigger a family-wide revocation.
    original = await _login(service)
    db.refresh_tokens[0].token_hash = hash_token("a different token")

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert db.refresh_tokens[0].revoked_at is None


async def test_refresh_rejects_a_row_that_has_expired(
    service: AuthService, db: FakeDatabase
) -> None:
    # The database is authoritative even when the token itself still verifies.
    original = await _login(service)
    db.refresh_tokens[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_handles_a_naive_stored_expiry(
    service: AuthService, db: FakeDatabase
) -> None:
    # MySQL DATETIME returns naive values; comparing one against an aware `now`
    # would raise TypeError and surface as a 500 rather than a refusal.
    original = await _login(service)
    db.refresh_tokens[0].expires_at = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)

    assert await service.refresh(original.refresh_token)


async def test_refresh_rejects_a_deleted_user(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    db.users[0].deleted_at = datetime.now(UTC)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_rejects_an_inactive_user(service: AuthService, db: FakeDatabase) -> None:
    # Disabling an account must stop it extending itself.
    original = await _login(service)
    db.users[0].is_active = False

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_failed_refresh_issues_no_tokens(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    original = await _login(service)
    db.users[0].is_active = False
    issued_before = len(tokens.issued)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert len(tokens.issued) == issued_before
    assert len(db.refresh_tokens) == 1


async def test_every_refresh_failure_reports_the_same_message(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)
    messages = set()

    with pytest.raises(AuthenticationError) as caught:
        await service.refresh(original.access_token)
    messages.add(caught.value.message)

    with pytest.raises(AuthenticationError) as caught:
        await service.refresh("nonsense")
    messages.add(caught.value.message)

    db.users[0].is_active = False
    with pytest.raises(AuthenticationError) as caught:
        await service.refresh(original.refresh_token)
    messages.add(caught.value.message)

    assert len(messages) == 1


# --- Refresh: reuse detection -----------------------------------------------


async def test_replaying_a_rotated_token_is_rejected(service: AuthService) -> None:
    original = await _login(service)
    await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_replay_revokes_the_whole_family(service: AuthService, db: FakeDatabase) -> None:
    # The attacker's successor must die with the replayed parent, otherwise a
    # thief simply keeps rotating and the victim is silently logged out.
    original = await _login(service)
    await service.refresh(original.refresh_token)
    assert db.refresh_tokens[1].revoked_at is None

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert all(token.revoked_at is not None for token in db.refresh_tokens)


async def test_replay_commits_the_family_revocation(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    # The raise unwinds through the unit of work's rollback, so detection is
    # only durable if it commits first.
    original = await _login(service)
    await service.refresh(original.refresh_token)
    factory.created.clear()

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert factory.created[0].commit_calls == 1


async def test_replay_leaves_the_successor_unusable(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(rotated.refresh_token)


async def test_replay_does_not_touch_another_family(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    other = await service.login(email=EMAIL, password=PASSWORD)
    await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    # The user's other session is unaffected.
    assert await service.refresh(other.refresh_token)


# --- Logout -----------------------------------------------------------------


async def test_logout_revokes_the_whole_family(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    await service.refresh(original.refresh_token)

    await service.logout(original.refresh_token)

    assert all(token.revoked_at is not None for token in db.refresh_tokens)


async def test_logout_stops_further_refreshing(service: AuthService) -> None:
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    await service.logout(rotated.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(rotated.refresh_token)


async def test_logout_is_idempotent(service: AuthService) -> None:
    # A repeated logout asks for a state that already holds; unlike refresh, an
    # already-revoked token here is not evidence of a replay.
    original = await _login(service)

    await service.logout(original.refresh_token)
    await service.logout(original.refresh_token)


async def test_logout_of_an_unknown_token_succeeds(service: AuthService, db: FakeDatabase) -> None:
    # Reporting it would disclose which tokens the server has issued.
    original = await _login(service)
    db.refresh_tokens.clear()

    await service.logout(original.refresh_token)


async def test_logout_rejects_an_access_token(service: AuthService) -> None:
    original = await _login(service)

    with pytest.raises(AuthenticationError):
        await service.logout(original.access_token)


async def test_logout_rejects_an_unverifiable_token(service: AuthService) -> None:
    with pytest.raises(AuthenticationError):
        await service.logout("nonsense")


async def test_logout_leaves_other_sessions_alone(service: AuthService) -> None:
    first = await _login(service)
    second = await service.login(email=EMAIL, password=PASSWORD)

    await service.logout(first.refresh_token)

    assert await service.refresh(second.refresh_token)


async def test_logout_commits(service: AuthService, factory: FakeUnitOfWorkFactory) -> None:
    original = await _login(service)
    factory.created.clear()

    await service.logout(original.refresh_token)

    assert factory.created[0].commit_calls == 1
