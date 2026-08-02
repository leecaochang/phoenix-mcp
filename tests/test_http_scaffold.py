"""Real-HTTP scaffold smoke tests.

Most view tests drive handlers with MagicMock requests, which skips aiohttp's
routing, path-variable parsing, request middleware, and real response headers.
These tests register the Phoenix MCP views on HA's real aiohttp app and hit them through
an actual client, covering what the MagicMock path structurally cannot: routing,
the {entity_id} path variable, token auth context, body-size (413) enforcement,
and the X-Phoenix-Request-ID response header on both success and error paths.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN, MAX_REQUEST_BODY_BYTES, TOKEN_PREFIX
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
)


def _make_token() -> tuple[TokenRecord, str]:
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return (
        TokenRecord(
            id=str(uuid.uuid4()), name="http-token",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=utcnow(), created_by="user1", pass_through=True,
        ),
        raw,
    )


def _make_scoped_token() -> tuple[TokenRecord, str]:
    """A scoped (non-pass-through) token granting READ on exactly sensor.allowed."""
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return (
        TokenRecord(
            id=str(uuid.uuid4()), name="scoped-token",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=utcnow(), created_by="user1", pass_through=False,
            permissions=PermissionTree(
                entities={"sensor.allowed": PermissionNode(state="YELLOW")},
            ),
        ),
        raw,
    )


def _make_data(token: TokenRecord) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.get_token_by_hash.return_value = token
    store.get_settings.return_value = MagicMock(
        kill_switch=False, disable_all_logging=False, log_allowed=True,
        log_denied=True, log_rate_limited=True, log_entity_names=True,
        log_client_ip=True, notify_on_rate_limit=False,
    )
    store.update_last_used = MagicMock()
    rate_limiter = MagicMock(spec=RateLimiter)
    rate_limiter.check.return_value = RateLimitResult(
        allowed=True, rate_limiting_enabled=True, limit=60,
        remaining=59, reset=9999999999, retry_after=0,
    )
    audit = MagicMock(spec=AuditLog)
    return PhoenixData(store=store, rate_limiter=rate_limiter, audit=audit, rate_limit_notified={})


@pytest.fixture
async def http_client(hass: HomeAssistant, hass_client_no_auth):
    """Set up HA http, register the Phoenix MCP views, and return (client, raw_token).

    Uses hass_client_no_auth (a TestClient over HA's real aiohttp app that does not
    inject an HA Authorization header) so we control the Phoenix MCP Bearer token ourselves;
    the HA test client also avoids binding a real socket.
    """
    assert await async_setup_component(hass, "http", {})
    token, raw = _make_token()
    hass.data[DOMAIN] = _make_data(token)

    from custom_components.phoenix_mcp.proxy_view import PhoenixStatesView, PhoenixStateView, PhoenixTemplateView
    from custom_components.phoenix_mcp.skill_view import PhoenixSkillView

    for view_cls in (PhoenixStatesView, PhoenixStateView, PhoenixTemplateView, PhoenixSkillView):
        view = view_cls()
        view.hass = hass
        hass.http.register_view(view)

    client = await hass_client_no_auth()
    return client, raw


async def test_states_requires_auth_and_sets_request_id(http_client):
    client, _raw = http_client
    resp = await client.get("/api/phoenix-mcp/states")
    assert resp.status == 401
    # The request-id header is present even on the auth-failure path.
    assert resp.headers.get("X-Phoenix-Request-ID")


async def test_states_with_token_returns_list_and_request_id(http_client):
    client, raw = http_client
    resp = await client.get("/api/phoenix-mcp/states", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status == 200
    assert resp.headers.get("X-Phoenix-Request-ID")
    assert isinstance(await resp.json(), list)


async def test_state_path_variable_routes(hass: HomeAssistant, http_client):
    client, raw = http_client
    hass.states.async_set("sensor.scaffold", "42", {})
    # The {entity_id} path variable must route to the single-state handler.
    resp = await client.get(
        "/api/phoenix-mcp/states/sensor.scaffold", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["entity_id"] == "sensor.scaffold"


async def test_body_size_413_enforced(http_client):
    client, raw = http_client
    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 100)
    resp = await client.post(
        "/api/phoenix-mcp/template", data=oversized,
        headers={"Authorization": f"Bearer {raw}"})
    assert resp.status == 413
    assert (await resp.json())["error"] == "request_too_large"


async def test_skill_route_unauthenticated(http_client):
    client, _raw = http_client
    # The skill guide is intentionally unauthenticated.
    resp = await client.get("/api/phoenix-mcp/skill")
    assert resp.status == 200
    assert "Phoenix MCP" in await resp.text()


@pytest.fixture
async def scoped_client(hass: HomeAssistant, hass_client_no_auth):
    """Same scaffold as http_client but with a scoped (non-pass-through) token."""
    assert await async_setup_component(hass, "http", {})
    token, raw = _make_scoped_token()
    hass.data[DOMAIN] = _make_data(token)

    from custom_components.phoenix_mcp.proxy_view import PhoenixStatesView, PhoenixStateView

    for view_cls in (PhoenixStatesView, PhoenixStateView):
        view = view_cls()
        view.hass = hass
        hass.http.register_view(view)

    client = await hass_client_no_auth()
    return client, raw


async def test_scoped_list_excludes_out_of_scope_entity(hass: HomeAssistant, scoped_client):
    client, raw = scoped_client
    hass.states.async_set("sensor.allowed", "1", {})
    hass.states.async_set("sensor.secret", "2", {})
    resp = await client.get("/api/phoenix-mcp/states", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status == 200
    ids = {e["entity_id"] for e in await resp.json()}
    # Entity filtering runs through the real HTTP layer: only the granted entity is returned.
    assert "sensor.allowed" in ids
    assert "sensor.secret" not in ids


async def test_scoped_single_state_in_scope_vs_out_of_scope(hass: HomeAssistant, scoped_client):
    client, raw = scoped_client
    hass.states.async_set("sensor.allowed", "1", {})
    hass.states.async_set("sensor.secret", "2", {})
    auth = {"Authorization": f"Bearer {raw}"}

    in_scope = await client.get("/api/phoenix-mcp/states/sensor.allowed", headers=auth)
    assert in_scope.status == 200
    assert (await in_scope.json())["entity_id"] == "sensor.allowed"

    # An out-of-scope (but real) entity returns the same 404 as a nonexistent one.
    out_of_scope = await client.get("/api/phoenix-mcp/states/sensor.secret", headers=auth)
    ghost = await client.get("/api/phoenix-mcp/states/sensor.ghost_xyz", headers=auth)
    assert out_of_scope.status == ghost.status == 404
    assert await out_of_scope.json() == await ghost.json()
