"""The node catalogue endpoint, driven through a real application.

This payload is the frontend's whole dependency for rendering the palette and
the configuration forms, so these tests pin its *shape* rather than only its
status code — a field quietly renamed here breaks a client that cannot be fixed
by redeploying the backend.

No database is involved, which is why the milestone has no integration suite:
the endpoint reads a registry assembled from code.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.main import create_app

SECRET = "node-types-endpoint-secret-long-enough"
PATH = "/api/v1/node-types"

EXPECTED_ORDER = [
    "trigger.manual@1",
    "trigger.webhook@1",
    "trigger.schedule@1",
    "core.constant@1",
    "core.noop@1",
    "core.log@1",
    "core.wait@1",
    "core.condition@1",
    "core.merge@1",
    "ai.agent@1",
]

ITEM_KEYS = {
    "type",
    "version",
    "qualified_name",
    "category",
    "deprecated",
    "display",
    "config_schema",
    "inputs",
    "outputs",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _auth(app: FastAPI, *roles: str) -> dict[str, str]:
    user = AuthenticatedUser(
        public_id="01USERUSERUSERUSERUSERUSER",
        organization_id="01ORGORGORGORGORGORGORGORG",
        roles=frozenset(roles or {"member"}),
    )
    token = app.state.container.token_service.create_access_token(user).token
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def catalogue(app: FastAPI, client: TestClient) -> list[dict[str, object]]:
    response = client.get(PATH, headers=_auth(app))
    assert response.status_code == 200
    items: list[dict[str, object]] = response.json()["items"]
    return items


# --- Access -----------------------------------------------------------------


def test_requires_authentication(client: TestClient) -> None:
    response = client.get(PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_rejects_a_refresh_token(app: FastAPI, client: TestClient) -> None:
    user = AuthenticatedUser(public_id="01U", organization_id="01O", roles=frozenset())
    refresh = app.state.container.token_service.create_refresh_token(user).token

    response = client.get(PATH, headers={"Authorization": f"Bearer {refresh}"})

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_any_role_may_read_the_catalogue(app: FastAPI, client: TestClient, role: str) -> None:
    # The catalogue is identical for everyone and reveals nothing about an
    # organization's data, so it carries no role requirement.
    assert client.get(PATH, headers=_auth(app, role)).status_code == 200


# --- Contents ---------------------------------------------------------------


def test_returns_every_registered_node_type(catalogue: list[dict[str, object]]) -> None:
    assert [item["qualified_name"] for item in catalogue] == EXPECTED_ORDER


def test_each_item_has_exactly_the_contract_keys(catalogue: list[dict[str, object]]) -> None:
    for item in catalogue:
        assert set(item) == ITEM_KEYS, item["qualified_name"]


def test_no_internal_field_leaks(catalogue: list[dict[str, object]]) -> None:
    # The wire says `type`; `node_type` is an internal spelling forced by a
    # builtin clash. `config_model` is a Python class and must never appear.
    for item in catalogue:
        assert "node_type" not in item
        assert "config_model" not in item


def test_qualified_name_is_type_and_version(catalogue: list[dict[str, object]]) -> None:
    for item in catalogue:
        assert item["qualified_name"] == f"{item['type']}@{item['version']}"


def test_every_item_carries_a_renderable_display(catalogue: list[dict[str, object]]) -> None:
    for item in catalogue:
        display = item["display"]
        assert isinstance(display, dict)
        assert display["label"]
        assert display["description"]
        assert set(display) == {"label", "description", "icon", "color"}


def test_deprecated_is_reported_not_hidden(catalogue: list[dict[str, object]]) -> None:
    # Nothing is deprecated yet, but the flag must be present so the builder can
    # grey an entry out rather than have it vanish from under a workflow.
    assert all(item["deprecated"] is False for item in catalogue)


# --- Config schemas ---------------------------------------------------------


def test_every_config_schema_is_a_json_schema_object(
    catalogue: list[dict[str, object]],
) -> None:
    for item in catalogue:
        schema = item["config_schema"]
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert schema["title"]


def test_config_schemas_forbid_unknown_keys(catalogue: list[dict[str, object]]) -> None:
    # Flows from `extra="forbid"` on each config model; the builder can use it
    # to reject a stale field rather than silently sending it.
    for item in catalogue:
        schema = item["config_schema"]
        assert isinstance(schema, dict)
        assert schema["additionalProperties"] is False


def test_constant_publishes_its_configurable_field(catalogue: list[dict[str, object]]) -> None:
    constant = next(i for i in catalogue if i["type"] == "core.constant")
    schema = constant["config_schema"]
    assert isinstance(schema, dict)

    assert schema["properties"]["value"]["type"] == "string"
    assert schema["properties"]["value"]["maxLength"] == 10_000


def test_log_publishes_its_level_choices(catalogue: list[dict[str, object]]) -> None:
    # The builder renders a dropdown from this; without the enum it would have
    # to hardcode the levels.
    log = next(i for i in catalogue if i["type"] == "core.log")
    schema = log["config_schema"]
    assert isinstance(schema, dict)

    assert "$defs" in schema or "enum" in str(schema)
    assert "debug" in str(schema)


def test_the_schedule_trigger_publishes_its_expression(
    catalogue: list[dict[str, object]],
) -> None:
    # The builder renders the cron field from this, and its default is what a
    # freshly dropped schedule node shows before anyone configures it.
    schedule = next(i for i in catalogue if i["type"] == "trigger.schedule")
    schema = schedule["config_schema"]
    assert isinstance(schema, dict)

    assert schema["properties"]["cron"]["type"] == "string"
    assert schema["properties"]["cron"]["default"] == "0 0 * * *"
    assert schema["properties"]["cron"]["maxLength"] == 128
    # No timezone: the platform is UTC throughout, and a field here would
    # promise a capability the schema does not have.
    assert set(schema["properties"]) == {"cron"}


# --- Handles ----------------------------------------------------------------


def test_handle_types_are_rendered_as_strings(catalogue: list[dict[str, object]]) -> None:
    # The same rendering validation messages quote, so a user reading "cannot
    # connect Json to Text" sees words the builder already showed them.
    trigger = next(i for i in catalogue if i["type"] == "trigger.manual")
    log = next(i for i in catalogue if i["type"] == "core.log")

    assert trigger["outputs"] == [{"name": "main", "type": "Json"}]
    inputs = log["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "Text"


def test_input_handles_carry_their_connection_rules(
    catalogue: list[dict[str, object]],
) -> None:
    # `required` drives whether the builder marks a socket mandatory; `arity`
    # whether it accepts a second edge.
    noop = next(i for i in catalogue if i["type"] == "core.noop")
    inputs = noop["inputs"]
    assert isinstance(inputs, list)

    assert inputs[0] == {
        "name": "main",
        "type": "Any",
        "arity": "single",
        "join": "all",
        "required": True,
    }


def test_a_trigger_publishes_no_inputs(catalogue: list[dict[str, object]]) -> None:
    trigger = next(i for i in catalogue if i["type"] == "trigger.manual")

    assert trigger["inputs"] == []


def test_a_terminal_node_publishes_no_outputs(catalogue: list[dict[str, object]]) -> None:
    log = next(i for i in catalogue if i["type"] == "core.log")

    assert log["outputs"] == []


# --- Stability --------------------------------------------------------------


def test_repeated_calls_are_byte_identical(app: FastAPI, client: TestClient) -> None:
    # Frontend snapshot tests diff this payload; a non-deterministic ordering or
    # schema would churn them for no reason.
    first = client.get(PATH, headers=_auth(app))
    second = client.get(PATH, headers=_auth(app))

    assert first.content == second.content


def test_a_fresh_application_serves_the_same_catalogue(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    # The catalogue comes from code, so it cannot vary between processes.
    original = client.get(PATH, headers=_auth(app)).json()

    other = create_app(settings)
    with TestClient(other) as other_client:
        assert other_client.get(PATH, headers=_auth(other)).json() == original


# --- Wiring -----------------------------------------------------------------


def test_the_endpoint_is_published_in_openapi(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    assert "get" in paths[PATH]


def test_the_catalogue_needs_no_database(app: FastAPI, client: TestClient) -> None:
    # The settings fixture pins database_url to None; a 200 here proves the
    # endpoint never reaches for a session.
    assert app.state.container.settings.database_url is None
    assert client.get(PATH, headers=_auth(app)).status_code == 200
