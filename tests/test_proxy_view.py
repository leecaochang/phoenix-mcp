"""Tests for Phoenix MCP proxy views."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import (
    DOMAIN,
    MAX_REQUEST_BODY_BYTES,
    TOKEN_LENGTH,
    TOKEN_PREFIX,
)
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.policy_engine import Permission
from custom_components.phoenix_mcp.proxy_view import (
    _error,
    _async_get_authenticated_token,
    _json_response,
)
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.token_store import TokenRecord, TokenStore


def _make_token(
    pass_through: bool = False,
    rate_limit_requests: int = 60,
    rate_limit_burst: int = 10,
    revoked: bool = False,
    **caps: str,
) -> tuple[TokenRecord, str]:
    """A token record plus its raw secret.

    Capabilities come through **caps rather than a fixed keyword list, so a test
    for a newly-gated service does not need this helper edited first. Unknown
    keys are rejected by TokenRecord itself, so a typo still fails loudly.
    """
    from homeassistant.util.dt import utcnow
    import uuid

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    record = TokenRecord(
        id=str(uuid.uuid4()),
        name="test-token",
        token_hash=token_hash,
        created_at=utcnow(),
        created_by="user1",
        pass_through=pass_through,
        rate_limit_requests=rate_limit_requests,
        rate_limit_burst=rate_limit_burst,
        revoked=revoked,
        **caps,
    )
    return record, raw


def _make_data(token: TokenRecord) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.get_token_by_hash.return_value = token
    store.get_settings.return_value = MagicMock(
        kill_switch=False,
        disable_all_logging=False,
        log_allowed=True,
        log_denied=True,
        log_rate_limited=True,
        log_entity_names=True,
        log_client_ip=True,
        notify_on_rate_limit=False,
    )
    store.update_last_used = MagicMock()
    store.get_pending_approvals.return_value = []
    store.async_lock = asyncio.Lock()

    rate_limiter = MagicMock(spec=RateLimiter)
    rate_limiter.check.return_value = RateLimitResult(
        allowed=True,
        rate_limiting_enabled=True,
        limit=60,
        remaining=59,
        reset=9999999999,
        retry_after=0,
    )

    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()

    return PhoenixData(
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
        rate_limit_notified={},
    )


def _make_request(
    headers: dict | None = None,
    query: dict | None = None,
    method: str = "GET",
    remote: str = "127.0.0.1",
) -> MagicMock:
    req = MagicMock()
    req.method = method
    req.remote = remote
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda k, default="": (headers or {}).get(k, default))
    req.query = query or {}
    req.content_length = None
    req.read = AsyncMock(return_value=b"")
    req.content = MagicMock()
    req.content.read = AsyncMock(return_value=b"")
    req.rel_url = MagicMock()
    req.rel_url.path = "/api/phoenix-mcp/health"
    return req


def _make_service_request(raw: str, body: bytes = b"{}") -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}
    request.content_length = None
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=body)
    return request


def _make_hass(data: PhoenixData, token: TokenRecord) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.components = MagicMock()
    return hass


@pytest.mark.asyncio
async def test_401_missing_authorization_header():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    request = _make_request(headers={})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    assert isinstance(result, MagicMock) or hasattr(result, "status")
    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    body = json.loads(result.text)
    assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_401_token_missing_phx_prefix():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    bad_token = "xxxx_" + secrets.token_hex(32)
    request = _make_request(headers={"Authorization": f"Bearer {bad_token}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_401_token_wrong_length_too_short():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    short_token = TOKEN_PREFIX + secrets.token_hex(31)
    assert len(short_token) == TOKEN_LENGTH - 2
    request = _make_request(headers={"Authorization": f"Bearer {short_token}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_401_token_wrong_length_too_long():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    long_token = TOKEN_PREFIX + secrets.token_hex(33)
    assert len(long_token) == TOKEN_LENGTH + 2
    request = _make_request(headers={"Authorization": f"Bearer {long_token}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_401_jwt_llat_rejected_immediately():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    request = _make_request(headers={"Authorization": f"Bearer {jwt}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_401_token_in_query_param():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    request = _make_request(
        headers={},
        query={"token": raw},
    )

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401


@pytest.mark.asyncio
async def test_401_token_in_access_token_query_param():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    request = _make_request(
        headers={},
        query={"access_token": raw},
    )

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401


@pytest.mark.asyncio
async def test_query_param_401_body_identical_to_missing_header_401():
    token, raw = _make_token()
    data1 = _make_data(token)
    data2 = _make_data(token)
    hass1 = _make_hass(data1, token)
    hass2 = _make_hass(data2, token)

    req_query = _make_request(headers={}, query={"token": raw})
    req_no_header = _make_request(headers={})

    from aiohttp import web
    r1 = await _async_get_authenticated_token(hass1, req_query, data1, "req-1", "/")
    r2 = await _async_get_authenticated_token(hass2, req_no_header, data2, "req-2", "/")

    assert isinstance(r1, web.Response)
    assert isinstance(r2, web.Response)
    assert r1.status == r2.status == 401
    assert json.loads(r1.text) == json.loads(r2.text)


@pytest.mark.asyncio
async def test_valid_token_returns_token_and_rl_result():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data, token)
    request = _make_request(headers={"Authorization": f"Bearer {raw}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    assert isinstance(result, tuple)
    returned_token, rl_result = result
    assert returned_token is token
    assert rl_result.allowed is True


@pytest.mark.asyncio
async def test_rate_limited_returns_429():
    token, raw = _make_token()
    data = _make_data(token)
    data.rate_limiter.check.return_value = RateLimitResult(
        allowed=False,
        rate_limiting_enabled=True,
        limit=60,
        remaining=0,
        reset=9999999999,
        retry_after=5,
    )
    hass = _make_hass(data, token)
    request = _make_request(headers={"Authorization": f"Bearer {raw}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/states")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 429
    assert result.headers.get("Retry-After") == "5"
    body = json.loads(result.text)
    assert body["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_rate_limited_logs_audit_entry():
    token, raw = _make_token()
    data = _make_data(token)
    data.rate_limiter.check.return_value = RateLimitResult(
        allowed=False,
        rate_limiting_enabled=True,
        limit=60,
        remaining=0,
        reset=9999999999,
        retry_after=5,
    )
    hass = _make_hass(data, token)
    request = _make_request(headers={"Authorization": f"Bearer {raw}"})

    await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/states")

    data.audit.record.assert_called_once()
    call_kwargs = data.audit.record.call_args.kwargs
    assert call_kwargs["outcome"] == "rate_limited"


@pytest.mark.asyncio
async def test_rate_limit_fires_event_once_per_minute():
    token, raw = _make_token()
    data = _make_data(token)
    data.rate_limiter.check.return_value = RateLimitResult(
        allowed=False,
        rate_limiting_enabled=True,
        limit=60,
        remaining=0,
        reset=9999999999,
        retry_after=1,
    )
    hass = _make_hass(data, token)

    request1 = _make_request(headers={"Authorization": f"Bearer {raw}"})
    request2 = _make_request(headers={"Authorization": f"Bearer {raw}"})

    await _async_get_authenticated_token(hass, request1, data, "req-1", "/api/phoenix-mcp/states")
    await _async_get_authenticated_token(hass, request2, data, "req-2", "/api/phoenix-mcp/states")

    assert hass.bus.async_fire.call_count == 2
    assert hass.bus.async_fire.call_args_list[0][0][0] == "phoenix_mcp_rate_limited"
    assert hass.bus.async_fire.call_args_list[1][0][0] == "phoenix_mcp_rate_limited"


@pytest.mark.asyncio
async def test_x_phoenix_request_id_present_in_error_response():
    resp = _error("unauthorized", "Unauthorized.", 401, "test-request-id-123")
    assert resp.headers.get("X-Phoenix-Request-ID") == "test-request-id-123"


@pytest.mark.asyncio
async def test_x_phoenix_request_id_present_in_json_response():
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=True, limit=60, remaining=59, reset=9999999999)
    resp = _json_response({"message": "ok"}, 200, "test-id-456", rl)
    assert resp.headers.get("X-Phoenix-Request-ID") == "test-id-456"


def test_rate_limit_headers_on_200():
    rl = RateLimitResult(
        allowed=True,
        rate_limiting_enabled=True,
        limit=60,
        remaining=42,
        reset=1234567890,
    )
    resp = _json_response({"ok": True}, 200, "req-1", rl)
    assert resp.headers.get("X-RateLimit-Limit") == "60"
    assert resp.headers.get("X-RateLimit-Remaining") == "42"
    assert resp.headers.get("X-RateLimit-Reset") == "1234567890"


def test_rate_limit_headers_absent_when_disabled():
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=False)
    resp = _json_response({"ok": True}, 200, "req-1", rl)
    assert "X-RateLimit-Limit" not in resp.headers
    assert "X-RateLimit-Remaining" not in resp.headers
    assert "X-RateLimit-Reset" not in resp.headers


@pytest.mark.asyncio
async def test_expired_token_archived_on_request():
    from datetime import timedelta
    from homeassistant.util.dt import utcnow
    import uuid

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expired_token = TokenRecord(
        id=str(uuid.uuid4()),
        name="expired",
        token_hash=token_hash,
        created_at=utcnow() - timedelta(days=2),
        created_by="user1",
        expires_at=utcnow() - timedelta(hours=1),
    )

    data = _make_data(expired_token)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    hass = _make_hass(data, expired_token)
    request = _make_request(headers={"Authorization": f"Bearer {raw}"})

    result = await _async_get_authenticated_token(hass, request, data, "req-1", "/api/phoenix-mcp/health")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.async_archive_token.assert_called_once()
    call_kwargs = data.store.async_archive_token.call_args
    assert call_kwargs.args[0] == expired_token.id
    assert call_kwargs.kwargs["revoked"] is False
    hass.bus.async_fire.assert_called_once()
    assert hass.bus.async_fire.call_args[0][0] == "phoenix_mcp_token_expired"
    data.rate_limiter.destroy.assert_called_once_with(expired_token.id)


@pytest.mark.asyncio
async def test_413_request_body_too_large():
    from custom_components.phoenix_mcp.proxy_view import _read_json_body

    request = MagicMock()
    request.content_length = MAX_REQUEST_BODY_BYTES + 1
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=b"x" * (MAX_REQUEST_BODY_BYTES + 1))

    result = await _read_json_body(request, "req-1")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 413
    body = json.loads(result.text)
    assert body["error"] == "request_too_large"


@pytest.mark.asyncio
async def test_413_body_too_large_via_content_length():
    from custom_components.phoenix_mcp.proxy_view import _read_json_body

    request = MagicMock()
    request.content_length = MAX_REQUEST_BODY_BYTES + 100
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=b"{}")

    result = await _read_json_body(request, "req-1")

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 413


@pytest.mark.asyncio
async def test_dual_gate_allowed_with_cap_restart(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token(cap_restart="allow")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    restart_called = []

    async def _fake_restart(call):
        restart_called.append(call)

    hass.services.async_register("homeassistant", "restart", _fake_restart)

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw)

    resp = await view.post(request, "homeassistant", "restart")

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["success"] is True
    assert len(restart_called) == 1


@pytest.mark.asyncio
async def test_dual_gate_denied_without_cap_restart(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token(cap_restart="deny")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw)

    resp = await view.post(request, "homeassistant", "restart")

    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["error"] == "forbidden"


@pytest.mark.asyncio
async def test_no_target_reload_allowed_with_cap_yaml_edit(hass, token_store):
    """A no-target reload (automation.reload) on the REST path bypasses entity
    flattening and calls HA with bare service_data when cap_yaml_edit is allow."""
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    token.cap_yaml_edit = "allow"
    data = _make_data(token)
    hass.data[DOMAIN] = data

    hass.states.async_set("automation.foo", "on", {})
    captured: dict = {}

    async def _reload_handler(call):
        captured["data"] = dict(call.data)

    hass.services.async_register("automation", "reload", _reload_handler)

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(_make_service_request(raw), "automation", "reload")

    assert resp.status == 200
    assert json.loads(resp.text)["success"] is True
    assert captured.get("data") == {}  # no entity_id target attached


@pytest.mark.asyncio
async def test_no_target_reload_denied_without_cap_yaml_edit(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    token.cap_yaml_edit = "deny"
    data = _make_data(token)
    hass.data[DOMAIN] = data

    called: list = []

    async def _reload_handler(call):
        called.append(1)

    hass.services.async_register("automation", "reload", _reload_handler)

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(_make_service_request(raw), "automation", "reload")

    assert resp.status == 403
    assert json.loads(resp.text)["error"] == "forbidden"
    assert called == []


@pytest.mark.asyncio
async def test_dual_gate_pass_through_still_requires_cap_restart(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token(pass_through=True, cap_restart="deny")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw)

    resp = await view.post(request, "homeassistant", "restart")

    assert resp.status == 403


@pytest.mark.asyncio
async def test_state_view_404_same_body_for_inaccessible_and_nonexistent(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixStateView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixStateView()
    view.hass = hass

    def make_req(entity_id):
        req = MagicMock()
        req.method = "GET"
        req.remote = "127.0.0.1"
        req.headers = MagicMock()
        req.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
        req.query = {}
        req.rel_url = MagicMock()
        req.rel_url.path = f"/api/phoenix-mcp/states/{entity_id}"
        return req

    # resolve() is mocked ON PURPOSE: this test isolates the response-formatting
    # contract (rule #12, identical 404 body for NOT_FOUND vs DENY), not whether
    # those verdicts are reached correctly. That verdict logic (ghost detection,
    # RED two-pass, alias-to-canonical) is exercised against the real implementation
    # in test_policy_engine.py (test_ghost_entity_returns_not_found,
    # test_red_entity_under_green_domain_is_deny, test_resolves_to_canonical_entity_id,
    # and ~90 others). Mocking here keeps the two concerns in separate unit layers.
    with patch("custom_components.phoenix_mcp.proxy_view.resolve") as mock_resolve:
        mock_resolve.return_value = Permission.NOT_FOUND
        resp_nonexistent = await view.get(make_req("light.does_not_exist"), "light.does_not_exist")

        mock_resolve.return_value = Permission.DENY
        resp_inaccessible = await view.get(make_req("light.secret_light"), "light.secret_light")

    assert resp_nonexistent.status == resp_inaccessible.status == 404
    assert json.loads(resp_nonexistent.text) == json.loads(resp_inaccessible.text)


@pytest.mark.asyncio
async def test_config_view_forbidden_without_flag(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixConfigView

    token, raw = _make_token(cap_config_read="deny")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixConfigView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}

    resp = await view.get(request)

    assert resp.status == 403


@pytest.mark.asyncio
async def test_template_view_forbidden_without_flag(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixTemplateView

    token, raw = _make_token(cap_template_render="deny")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixTemplateView()
    view.hass = hass

    request = _make_service_request(raw, b'{"template": "{{ 1 + 1 }}"}')

    resp = await view.post(request)

    assert resp.status == 403


@pytest.mark.asyncio
async def test_events_view_forbidden_without_flag(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixEventsView

    token, raw = _make_token(cap_config_read="deny")
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixEventsView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}

    resp = await view.get(request)

    assert resp.status == 403


@pytest.mark.asyncio
async def test_root_view_returns_api_running(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixRootView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data

    view = PhoenixRootView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}

    resp = await view.get(request)

    assert resp.status == 200
    assert json.loads(resp.text) == {"message": "API running."}


@pytest.mark.asyncio
async def test_states_view_returns_filtered_states(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixStatesView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []

    view = PhoenixStatesView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}

    with patch("custom_components.phoenix_mcp.proxy_view.filter_entities_for_token", return_value=[]) as mock_filter:
        resp = await view.get(request)

    assert resp.status == 200
    assert json.loads(resp.text) == []
    mock_filter.assert_called_once()


@pytest.mark.asyncio
async def test_states_view_negative_limit_clamped(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixStatesView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []

    view = PhoenixStatesView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {"limit": "-1"}

    three = [{"entity_id": f"light.l{i}"} for i in range(3)]
    with patch("custom_components.phoenix_mcp.proxy_view.filter_entities_for_token", return_value=three):
        resp = await view.get(request)

    assert resp.status == 200
    # Clamped to a minimum page of 1, not the old filtered[0:-1] (which returned 2).
    assert len(json.loads(resp.text)) == 1


@pytest.mark.asyncio
async def test_state_view_uses_canonical_id_for_fetch(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixStateView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states.async_set("light.real", "on", {})

    view = PhoenixStateView()
    view.hass = hass

    request = MagicMock()
    request.method = "GET"
    request.remote = "127.0.0.1"
    request.headers = MagicMock()
    request.headers.get = MagicMock(side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    request.query = {}
    request.rel_url = MagicMock()
    request.rel_url.path = "/api/phoenix-mcp/states/some_registry_id"

    # A registry id / alias canonicalizes to light.real; the state fetch must use
    # the canonical id, not the original arg (which would 404).
    with patch("custom_components.phoenix_mcp.proxy_view.canonical_entity_id", return_value="light.real"), \
         patch("custom_components.phoenix_mcp.proxy_view.resolve", return_value=Permission.WRITE):
        resp = await view.get(request, "some_registry_id")

    assert resp.status == 200
    assert json.loads(resp.text)["entity_id"] == "light.real"


@pytest.mark.asyncio
async def test_service_view_empty_permitted_returns_403(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "light.kitchen"}')

    # resolve_service_targets is mocked ON PURPOSE: this isolates the view's
    # handling of an empty resolution (-> 403). The real flattening it stands in
    # for (device/area/"all" expansion, RED-skip, ghost handling) is tested against
    # the real implementation in test_policy_engine.py (test_all_expands_to_domain_entities,
    # test_device_id_expands_to_service_domain_entities_only, test_area_id_red_entity_silently_skipped, etc.).
    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=([], 1)):
        resp = await view.post(request, "light", "turn_on")

    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["error"] == "forbidden"


@pytest.mark.asyncio
async def test_service_view_entity_creation_blocked(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView
    from custom_components.phoenix_mcp.policy_engine import EntityCreationNotPermitted

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "light.new_entity"}')

    # Mocked ON PURPOSE: isolates the view's translation of the creation-guard
    # exception into a 403. The guard itself (a service-named entity absent from
    # the registry raises EntityCreationNotPermitted) is tested against the real
    # implementation in test_policy_engine.py (test_nonexistent_entity_raises_entity_creation_not_permitted,
    # test_pass_through_entity_creation_still_blocked).
    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", side_effect=EntityCreationNotPermitted("light.new_entity")):
        resp = await view.post(request, "light", "turn_on")

    assert resp.status == 403


@pytest.mark.asyncio
async def test_service_view_timeout_returns_200_partial(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=asyncio.TimeoutError())

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "light.kitchen"}')

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["light.kitchen"], 1)):
        resp = await view.post(request, "light", "turn_on")

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["success"] is True
    assert body["partial"] is True


@pytest.mark.asyncio
async def test_service_view_entities_requested_and_affected_headers(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": ["light.a", "light.b", "light.c"]}')

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["light.a"], 3)):
        with patch("custom_components.phoenix_mcp.proxy_view.filter_service_response", return_value=None):
            resp = await view.post(request, "light", "turn_on")

    assert resp.status == 200
    assert resp.headers.get("X-Phoenix-Entities-Requested") == "3"
    assert resp.headers.get("X-Phoenix-Entities-Affected") == "1"


@pytest.mark.asyncio
async def test_service_view_advisory_flags_audit_entry(hass, token_store):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "light.kitchen"}')

    mesa_outcome = SimpleNamespace(
        blocked=[], decision="allow", approval=None,
        entities=["light.kitchen"], warnings=["light.kitchen is advisory-flagged by MESA"],
    )

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["light.kitchen"], 1)):
        with patch("custom_components.phoenix_mcp.proxy_view.async_apply_mesa_to_call", AsyncMock(return_value=mesa_outcome)):
            with patch("custom_components.phoenix_mcp.proxy_view.filter_service_response", return_value=None):
                resp = await view.post(request, "light", "turn_on")

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["mesa_advisory"] == ["light.kitchen is advisory-flagged by MESA"]
    # the audit entry for this allowed call carries the advisory flag
    call_kwargs = data.audit.record.call_args.kwargs
    assert call_kwargs["mesa_advisory"] is True


@pytest.mark.asyncio
async def test_service_view_service_not_found_hints_verbs_for_core_domain(hass, token_store):
    """REST call_service on a curated core domain (DOMAIN_SERVICE_HINTS) surfaces
    the valid core verbs as invalid_request (400) + suggestions, so a wrong-verb
    guess self-corrects. Leak-safe: post-authorization + hardcoded public list."""
    from homeassistant.exceptions import ServiceNotFound

    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    # valve is a physical actuator domain, so the verb-hint (post-authorization)
    # path is only reached once the physical-control gate passes; grant it.
    token.cap_physical_control = "allow"
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=ServiceNotFound("valve", "open"))

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "valve.irrigation"}')

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["valve.irrigation"], 1)):
        resp = await view.post(request, "valve", "open")

    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_request"
    assert "open_valve" in body["suggestions"]
    assert "open_valve" in body["message"]


@pytest.mark.asyncio
async def test_service_view_cover_toggle_denied_without_physical_control(hass, token_store):
    """REST call_service on cover.toggle (a service the old exact-name allowlist
    missed) is 403 for a token with physical control denied, before any HA call."""
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()  # cap_physical_control defaults to deny
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    view = PhoenixServiceView()
    view.hass = hass
    request = _make_service_request(raw, b'{"entity_id": "cover.g"}')

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["cover.g"], 1)):
        resp = await view.post(request, "cover", "toggle")

    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["error"] == "forbidden"
    # The gate fired before any service dispatch.
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_service_view_service_not_found_generic_for_unmapped_domain(hass, token_store):
    """A domain NOT in DOMAIN_SERVICE_HINTS keeps the generic no-oracle 403
    'Forbidden.' on ServiceNotFound (light is deliberately excluded)."""
    from homeassistant.exceptions import ServiceNotFound

    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=ServiceNotFound("light", "make_bright"))

    view = PhoenixServiceView()
    view.hass = hass

    request = _make_service_request(raw, b'{"entity_id": "light.kitchen"}')

    with patch("custom_components.phoenix_mcp.proxy_view.resolve_service_targets", return_value=(["light.kitchen"], 1)):
        resp = await view.post(request, "light", "make_bright")

    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["error"] == "forbidden"
    assert body["message"] == "Forbidden."
    assert "suggestions" not in body


def test_all_views_exported():
    from custom_components.phoenix_mcp.proxy_view import ALL_VIEWS
    assert len(ALL_VIEWS) == 11


@pytest.mark.asyncio
async def test_read_json_body_drains_partial_stream():
    """A body split across multiple reads (aiohttp's normal short-read
    behaviour) must still be fully assembled before parsing, not truncated."""
    from custom_components.phoenix_mcp.proxy_view import _read_json_body

    payload = {"entity_id": "light.kitchen", "note": "x" * 200}
    raw = json.dumps(payload).encode()

    class _ChunkedContent:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def read(self, _limit: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def at_eof(self) -> bool:
            return not self._chunks

    request = MagicMock()
    request.content_length = len(raw)
    request.content = _ChunkedContent([raw[:13], raw[13:97], raw[97:]])

    result = await _read_json_body(request, "req-1")
    assert result == payload


# ---------------------------------------------------------------------------
# REST confirm gates route through the approval flow (parity with MCP)
# ---------------------------------------------------------------------------


def _confirm_env(token: TokenRecord) -> tuple[PhoenixData, list]:
    """_make_data plus a live pending-approvals list, so async_evaluate_capability's
    real approval creation works against the MagicMock store."""
    data = _make_data(token)
    pending: list = []
    data.store.get_pending_approvals = MagicMock(side_effect=lambda: pending)

    def _set_pending(v: list) -> None:
        pending[:] = v

    data.store.set_pending_approvals = MagicMock(side_effect=_set_pending)
    data.store.async_save = AsyncMock()
    return data, pending


@pytest.mark.asyncio
async def test_rest_restart_confirm_routes_through_approval(hass):
    """cap_restart=confirm on the REST dual-gate must pend, not 403.

    A plain 403 made confirm behave as deny on the REST surface while the
    same call on MCP queued an approval."""
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token(cap_restart="confirm")
    data, pending = _confirm_env(token)
    hass.data[DOMAIN] = data

    calls: list = []

    async def _restart(call):
        calls.append(call)

    hass.services.async_register("homeassistant", "restart", _restart)

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(_make_service_request(raw), "homeassistant", "restart")

    assert resp.status == 202
    body = json.loads(resp.text)
    assert body["status"] == "pending_approval"
    assert len(pending) == 1
    appr = pending[0]
    assert body["approval_id"] == appr["id"]
    assert appr["cap_name"] == "cap_restart"
    assert appr["tool_name"] == "call_service"
    assert calls == []  # held for approval, not executed


@pytest.mark.asyncio
async def test_rest_physical_confirm_routes_through_approval(hass):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    token.cap_physical_control = "confirm"
    data, pending = _confirm_env(token)
    hass.data[DOMAIN] = data

    lock_calls: list = []

    async def _lock(call):
        lock_calls.append(call)

    hass.services.async_register("lock", "lock", _lock)

    view = PhoenixServiceView()
    view.hass = hass
    body = json.dumps({"entity_id": "lock.front_door"}).encode()
    resp = await view.post(_make_service_request(raw, body), "lock", "lock")

    assert resp.status == 202
    appr = pending[0]
    assert appr["cap_name"] == "cap_physical_control"
    assert appr["args"]["entity_id"] == "lock.front_door"
    assert lock_calls == []


@pytest.mark.asyncio
async def test_rest_reload_confirm_pends_and_approved_args_execute(hass):
    """The approval stores MCP-shaped call_service args, so the shared
    executor re-runs the identical call when the admin approves."""
    from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token()
    token.cap_yaml_edit = "confirm"
    data, pending = _confirm_env(token)
    hass.data[DOMAIN] = data

    captured: dict = {}

    async def _reload(call):
        captured["data"] = dict(call.data)

    hass.services.async_register("automation", "reload", _reload)

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(_make_service_request(raw), "automation", "reload")
    assert resp.status == 202
    assert captured == {}

    appr = pending[0]
    assert appr["cap_name"] == "cap_yaml_edit"
    assert appr["args"]["domain"] == "automation"
    assert appr["args"]["service"] == "reload"

    out = await async_execute_approved_tool("call_service", appr["args"], token, hass, data)
    assert out[1] == "allowed"
    assert captured["data"] == {}  # bare service_data, no entity target attached


@pytest.mark.asyncio
async def test_rest_confirm_queue_full_returns_429(hass):
    from custom_components.phoenix_mcp.const import MAX_PENDING_APPROVALS_PER_TOKEN
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    token, raw = _make_token(cap_restart="confirm")
    data, pending = _confirm_env(token)
    pending[:] = [
        {"token_id": token.id, "status": "pending"}
        for _ in range(MAX_PENDING_APPROVALS_PER_TOKEN)
    ]
    hass.data[DOMAIN] = data

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(_make_service_request(raw), "homeassistant", "restart")

    assert resp.status == 429
    assert json.loads(resp.text)["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_rest_logs_400_when_system_log_not_loaded(hass):
    """The REST logs endpoint mirrors get_logs: unloaded is an error, not an empty log."""
    from custom_components.phoenix_mcp.proxy_view import PhoenixLogsView

    token, raw = _make_token()
    token.cap_log_read = "allow"
    data = _make_data(token)
    hass.data[DOMAIN] = data
    hass.data.pop("system_log", None)

    view = PhoenixLogsView()
    view.hass = hass
    resp = await view.get(_make_request(headers={"Authorization": f"Bearer {raw}"}))

    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_request"
    assert "system_log" in body["message"]


# ---------------------------------------------------------------------------
# Gate invariant: pending WITHOUT an approval record must fail CLOSED.
#
# Both gates read result.approval straight after checking is_pending / decision
# == "pending". If that invariant ever broke, the tempting "and approval is not
# None" guard would make the branch fall through to the code that RUNS the
# service call, turning a held action into an unreviewed one. These pin the
# refusal so that shortcut cannot be reintroduced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_gate_pending_without_an_approval_refuses():
    from custom_components.phoenix_mcp.proxy_view import _cap_gate

    pending = SimpleNamespace(is_deny=False, is_pending=True, approval=None)
    data = MagicMock()
    data.audit = AuditLog()
    data.store.get_settings = MagicMock(return_value=MagicMock(disable_all_logging=True))
    token = MagicMock(id="t", name="t", pass_through=False)

    with patch("custom_components.phoenix_mcp.proxy_view.async_evaluate_capability",
               new=AsyncMock(return_value=pending)):
        resp = await _cap_gate(
            "cap_restart", token, MagicMock(), data,
            domain="homeassistant", service="restart", entity_id=None,
            device_id=None, area_id=None, service_data={}, body={},
            request_id="r", resource="/x", client_ip="127.0.0.1",
            rl_result=RateLimitResult(True, 0, 0, 0, False),
        )

    # Refused, NOT None: returning None means "gate passed, go run it".
    assert resp is not None
    assert resp.status == 403
    assert json.loads(resp.text)["error"] == "forbidden"


# ---------------------------------------------------------------------------
# No-target service dispatch: characterization.
#
# post() carries three near-identical dispatch blocks for the service families
# that take NO entity target: the dual-gate services (homeassistant/restart,
# /stop), the config-reload family (NO_TARGET_SERVICES), and ESPHome
# user-defined actions. Each handles timeout, HA failure and success itself, and
# their timeout/error branches were uncovered. These pin every branch of every
# family before the three are collapsed into one helper, because the families
# differ in ways that matter and must NOT be blurred:
#
#   - the timeout message wording ("Service" vs "Action" dispatched),
#   - ESPHome alone surfaces ServiceValidationError/vol.Invalid as a 400,
#   - ESPHome alone authorizes on the owning device's write scope.
# ---------------------------------------------------------------------------


def _no_target_view(hass, token):
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    data = _make_data(token)
    hass.data[DOMAIN] = data
    view = PhoenixServiceView()
    view.hass = hass
    return view, data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "service", "caps"),
    [
        ("homeassistant", "restart", {"cap_restart": "allow"}),
        ("automation", "reload", {"cap_yaml_edit": "allow"}),
    ],
)
async def test_no_target_dispatch_timeout_reports_partial_success(hass, token_store, domain, service, caps):
    """A slow service is NOT a failure: it reports success with partial=True."""
    token, raw = _make_token(**caps)
    view, _data = _no_target_view(hass, token)

    async def _slow(call):
        raise asyncio.TimeoutError

    hass.services.async_register(domain, service, _slow)
    resp = await view.post(_make_service_request(raw), domain, service)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["success"] is True and body["partial"] is True
    assert "Service dispatched" in body["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "service", "caps"),
    [
        ("homeassistant", "restart", {"cap_restart": "allow"}),
        ("automation", "reload", {"cap_yaml_edit": "allow"}),
    ],
)
async def test_no_target_dispatch_ha_error_is_a_uniform_403(hass, token_store, domain, service, caps):
    """HomeAssistantError stays a generic Forbidden: no service-existence oracle."""
    token, raw = _make_token(**caps)
    view, _data = _no_target_view(hass, token)

    from homeassistant.exceptions import HomeAssistantError

    async def _boom(call):
        raise HomeAssistantError("nope")

    hass.services.async_register(domain, service, _boom)
    resp = await view.post(_make_service_request(raw), domain, service)

    assert resp.status == 403
    assert json.loads(resp.text)["message"] == "Forbidden."


@pytest.mark.asyncio
async def test_no_target_reload_requires_cap_yaml_edit(hass, token_store):
    token, raw = _make_token(cap_yaml_edit="deny")
    view, _data = _no_target_view(hass, token)
    hass.services.async_register("automation", "reload", AsyncMock())

    resp = await view.post(_make_service_request(raw), "automation", "reload")
    assert resp.status == 403


@pytest.mark.asyncio
async def test_no_target_reload_surfaces_the_validation_message(hass, token_store):
    """REST now matches MCP: a bad reload argument reports WHY, not a bare 403.

    A generic "Forbidden." here, while mcp_view's call_service returns the
    message, is an accidental divergence. Both are post-cap_yaml_edit,
    so the message describes the caller's own reloadable config and never hidden
    state. A separate parity guard keeps the two surfaces aligned.
    """
    from homeassistant.exceptions import ServiceValidationError

    token, raw = _make_token(cap_yaml_edit="allow")
    view, _data = _no_target_view(hass, token)

    async def _bad_arg(call):
        raise ServiceValidationError("scene_id is not a valid scene")

    hass.services.async_register("scene", "reload", _bad_arg)
    resp = await view.post(_make_service_request(raw), "scene", "reload")

    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_request"
    assert "scene_id" in body["message"]


@pytest.mark.asyncio
async def test_dual_gate_still_hides_validation_detail(hass, token_store):
    """The complement: restart/stop stay a uniform 403.

    They take no caller arguments worth reporting, and a message there would
    describe a service the token cannot otherwise probe.
    """
    from homeassistant.exceptions import ServiceValidationError

    token, raw = _make_token(cap_restart="allow")
    view, _data = _no_target_view(hass, token)

    async def _bad_arg(call):
        raise ServiceValidationError("some internal detail")

    hass.services.async_register("homeassistant", "restart", _bad_arg)
    resp = await view.post(_make_service_request(raw), "homeassistant", "restart")

    assert resp.status == 403
    assert json.loads(resp.text)["message"] == "Forbidden."
    assert "internal detail" not in resp.text
