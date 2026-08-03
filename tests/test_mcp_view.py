"""Tests for Phoenix MCP MCP endpoint."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
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
from custom_components.phoenix_mcp.mcp_view import (
    PhoenixMcpContextView,
    PhoenixMcpView,
    _build_context_json,
    _build_context_plain,
    _dispatch_mcp,
)
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord, TokenStore


def _raw_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(32)


def _make_token(
    pass_through: bool = False,
    cap_restart: str = "deny",
    cap_config_read: str = "deny",
    cap_template_render: str = "deny",
    cap_automation_write: str = "deny",
    cap_script_write: str = "deny",
    cap_log_read: str = "deny",
    rate_limit_requests: int = 60,
    rate_limit_burst: int = 10,
    revoked: bool = False,
    permissions: PermissionTree | None = None,
) -> tuple[TokenRecord, str]:
    from homeassistant.util.dt import utcnow

    raw = _raw_token()
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    record = TokenRecord(
        id=str(uuid.uuid4()),
        name="test-token",
        token_hash=token_hash,
        created_at=utcnow(),
        created_by="user1",
        pass_through=pass_through,
        cap_restart=cap_restart,
        cap_config_read=cap_config_read,
        cap_template_render=cap_template_render,
        cap_automation_write=cap_automation_write,
        cap_script_write=cap_script_write,
        cap_log_read=cap_log_read,
        rate_limit_requests=rate_limit_requests,
        rate_limit_burst=rate_limit_burst,
        revoked=revoked,
        permissions=permissions or PermissionTree(),
    )
    return record, raw


def _make_data(token: TokenRecord | None = None) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    if token is not None:
        store.get_token_by_hash.return_value = token
    else:
        store.get_token_by_hash.return_value = None
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
    store.get_entity_hints.return_value = {}

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
    body: bytes = b"",
) -> MagicMock:
    req = MagicMock()
    req.method = method
    req.remote = remote
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda k, default="": (headers or {}).get(k, default))
    req.query = query or {}
    req.content_length = None
    req.read = AsyncMock(return_value=body)
    req.content = MagicMock()
    req.content.read = AsyncMock(return_value=body)
    return req


def _make_hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.components = MagicMock()
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    return hass


def _make_mcp_view(data: PhoenixData, hass: MagicMock | None = None) -> PhoenixMcpView:
    view = PhoenixMcpView()
    view.hass = hass if hass is not None else _make_hass(data)
    return view


def _make_context_view(data: PhoenixData, hass: MagicMock | None = None) -> PhoenixMcpContextView:
    view = PhoenixMcpContextView()
    view.hass = hass if hass is not None else _make_hass(data)
    return view


@pytest.mark.asyncio
async def test_streamable_batch_dispatches_sequentially_and_isolates_failures():
    from custom_components.phoenix_mcp.mcp_view import _handle_streamable_batch

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=True, limit=60, remaining=59, reset=9999999999)

    call_order: list = []

    async def fake_dispatch(method, msg_id, params, *a, **k):
        call_order.append(msg_id)
        if method == "boom":
            raise RuntimeError("explode")
        return ({"jsonrpc": "2.0", "id": msg_id, "result": {"m": method}}, None, None, None)

    items = [
        {"jsonrpc": "2.0", "id": 1, "method": "a"},
        {"jsonrpc": "2.0", "id": 2, "method": "boom"},
        {"jsonrpc": "2.0", "id": 3, "method": "c"},
    ]
    with patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=fake_dispatch):
        resp = await _handle_streamable_batch(items, token, rl, hass, data, "rid", "127.0.0.1", "http://h")

    assert resp.status == 200
    body = json.loads(resp.text)
    # Sequential, in-order dispatch (no asyncio.gather concurrency).
    assert call_order == [1, 2, 3]
    assert [r["id"] for r in body] == [1, 2, 3]
    # One item's failure is isolated as a per-item internal error, not a batch failure.
    assert body[1]["error"]["code"] == -32603
    assert body[0]["result"]["m"] == "a"
    assert body[2]["result"]["m"] == "c"


# --- token validation on POST /api/phoenix-mcp (Streamable HTTP) ---

@pytest.mark.asyncio
async def test_mcp_401_missing_auth_header():
    token, _ = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    request = _make_request(method="POST", headers={})

    result = await view.post(request)

    from aiohttp import web
    assert isinstance(result, web.Response)
    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_401_token_missing_phx_prefix():
    token, _ = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    bad = "xxxx_" + secrets.token_hex(32)
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {bad}"})

    result = await view.post(request)

    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_401_token_wrong_length_short():
    token, _ = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    short = TOKEN_PREFIX + secrets.token_hex(32)[:-1]  # 67 chars total
    assert len(short) == TOKEN_LENGTH - 1
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {short}"})

    result = await view.post(request)

    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_401_token_wrong_length_long():
    token, _ = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    long_tok = TOKEN_PREFIX + secrets.token_hex(32) + "a"  # 69 chars total
    assert len(long_tok) == TOKEN_LENGTH + 1
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {long_tok}"})

    result = await view.post(request)

    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_401_llat_jwt_format_rejected():
    token, _ = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    llat = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {llat}"})

    result = await view.post(request)

    assert result.status == 401
    data.store.get_token_by_hash.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_401_token_in_query_param():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
        query={"token": raw},
    )

    result = await view.post(request)

    assert result.status == 401


@pytest.mark.asyncio
async def test_mcp_401_access_token_in_query_param():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
        query={"access_token": raw},
    )

    result = await view.post(request)

    assert result.status == 401


# --- POST /api/phoenix-mcp (Streamable HTTP transport) ---

@pytest.mark.asyncio
async def test_mcp_post_413_body_too_large():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
    )
    request.content_length = MAX_REQUEST_BODY_BYTES + 1

    result = await view.post(request)

    assert result.status == 413


@pytest.mark.asyncio
async def test_mcp_post_413_body_too_large_streaming():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    big_body = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
        body=big_body,
    )

    result = await view.post(request)

    assert result.status == 413


@pytest.mark.asyncio
async def test_mcp_post_initialize_returns_result_inline():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
        body=body,
    )

    result = await view.post(request)

    assert result.status == 200
    payload = json.loads(result.text)
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == "2025-03-26"
    assert payload["result"]["serverInfo"]["name"] == "Phoenix MCP"
    assert "tools" in payload["result"]["capabilities"]


@pytest.mark.asyncio
async def test_mcp_post_body_drains_partial_stream():
    """A tool-call body split across multiple reads (aiohttp's normal short-read
    behaviour) must still be fully assembled before parsing, not truncated."""
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"note": "x" * 300}}
    ).encode()

    class _ChunkedContent:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def read(self, _limit: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def at_eof(self) -> bool:
            return not self._chunks

    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"})
    request.content_length = len(body)
    request.content = _ChunkedContent([body[:19], body[19:150], body[150:]])

    result = await view.post(request)

    assert result.status == 200
    payload = json.loads(result.text)
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == "2025-03-26"


@pytest.mark.asyncio
async def test_mcp_post_notification_returns_202():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}"},
        body=body,
    )

    result = await view.post(request)

    assert result.status == 202


# --- malformed JSON-RPC envelope handling (Codex audit) ---

@pytest.mark.asyncio
async def test_mcp_post_non_dict_params_is_invalid_params_not_500():
    """A tools/call with a non-dict params (list/scalar) must return a clean
    JSON-RPC -32602, not escape post() as an AttributeError -> aiohttp 500."""
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}).encode()
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)

    result = await view.post(request)

    assert result.status == 200
    payload = json.loads(result.text)
    assert payload["error"]["code"] == -32602
    assert payload["id"] == 1


@pytest.mark.asyncio
async def test_mcp_post_non_string_method_is_invalid_request():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": ["bad"]}).encode()
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)

    result = await view.post(request)

    assert result.status == 200
    payload = json.loads(result.text)
    assert payload["error"]["code"] == -32600
    assert payload["id"] == 7


@pytest.mark.asyncio
async def test_mcp_post_response_object_accepted_202():
    """A JSON-RPC response object (no method, carries result) is accepted with no
    reply per the Streamable HTTP transport, not answered 'method not found'."""
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)

    result = await view.post(request)

    assert result.status == 202


@pytest.mark.asyncio
async def test_streamable_batch_bad_params_is_invalid_params_not_internal_error():
    from custom_components.phoenix_mcp.mcp_view import _handle_streamable_batch

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=True, limit=60, remaining=59, reset=9999999999)

    items = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]},
        {"jsonrpc": "2.0", "id": 2, "method": ["bad"]},
    ]
    resp = await _handle_streamable_batch(items, token, rl, hass, data, "rid", "127.0.0.1", "http://h")

    body = json.loads(resp.text)
    assert body[0]["error"]["code"] == -32602  # invalid params, not -32603
    assert body[1]["error"]["code"] == -32600  # invalid request


# --- Origin validation (DNS-rebinding defense, MCP transport spec) ---

@pytest.mark.asyncio
async def test_mcp_post_rejects_untrusted_origin():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}", "Origin": "https://evil.example"},
        body=body,
    )
    with patch("homeassistant.helpers.network.is_hass_url", return_value=False):
        result = await view.post(request)

    assert result.status == 403


@pytest.mark.asyncio
async def test_mcp_post_allows_trusted_origin():
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}", "Origin": "https://home.example"},
        body=body,
    )
    with patch("homeassistant.helpers.network.is_hass_url", return_value=True):
        result = await view.post(request)

    assert result.status == 200


@pytest.mark.asyncio
async def test_mcp_post_absent_origin_allowed():
    """Real (non-browser) MCP clients send no Origin; that must never be blocked."""
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)

    result = await view.post(request)  # is_hass_url never consulted (Origin absent)

    assert result.status == 200


@pytest.mark.asyncio
async def test_mcp_post_malformed_origin_is_403_not_500():
    # A malformed Origin (e.g. "http://[::1") makes HA's URL parser raise; it
    # must be a uniform 403, never an unauthenticated 500.
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    request = _make_request(
        method="POST",
        headers={"Authorization": f"Bearer {raw}", "Origin": "http://[::1"},
        body=body,
    )
    with patch("homeassistant.helpers.network.is_hass_url",
               side_effect=ValueError("Invalid IPv6 URL")):
        result = await view.post(request)

    assert result.status == 403


@pytest.mark.asyncio
async def test_mcp_post_rejects_non_finite_numbers():
    # json.loads accepts NaN/Infinity by default; those defeat range checks and
    # can reach HA services, so the body parse must reject them.
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": NaN}}'
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)
    result = await view.post(request)
    payload = json.loads(result.text)
    assert payload["error"]["code"] == -32700  # parse error


@pytest.mark.asyncio
async def test_mcp_notification_gets_no_response():
    # A JSON-RPC notification omits `id` and must receive no response body (202).
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    body = json.dumps({"jsonrpc": "2.0", "method": "ping"}).encode()  # no id
    request = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body)
    result = await view.post(request)
    assert result.status == 202
    assert not result.text

    # A request WITH id: null is NOT a notification, it still gets a response.
    body2 = json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}).encode()
    request2 = _make_request(method="POST", headers={"Authorization": f"Bearer {raw}"}, body=body2)
    result2 = await view.post(request2)
    assert result2.status == 200


@pytest.mark.asyncio
async def test_mcp_malformed_no_id_is_invalid_request_not_notification():
    # A malformed object without `id` is NOT a valid notification; it must get an
    # Invalid Request error with id null, not be silently swallowed with 202.
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)
    for bad in ({"jsonrpc": "2.0", "method": 7}, {"jsonrpc": "2.0", "params": {}}):
        request = _make_request(
            method="POST", headers={"Authorization": f"Bearer {raw}"},
            body=json.dumps(bad).encode())
        result = await view.post(request)
        assert result.status == 200
        payload = json.loads(result.text)
        assert payload["error"]["code"] == -32600
        assert payload["id"] is None


@pytest.mark.asyncio
async def test_mcp_positional_params_notification_gets_no_response():
    # JSON-RPC allows array (positional) params. A no-id request with array params
    # is a valid notification Phoenix MCP can't dispatch, so it must get NO response, but a
    # request (with id) still gets -32602 since Phoenix MCP only dispatches by-name.
    token, raw = _make_token()
    data = _make_data(token)
    view = _make_mcp_view(data)

    notif = {"jsonrpc": "2.0", "method": "ping", "params": []}  # no id
    result = await view.post(_make_request(
        method="POST", headers={"Authorization": f"Bearer {raw}"},
        body=json.dumps(notif).encode()))
    assert result.status == 202
    assert not result.text

    req = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}  # has id
    result = await view.post(_make_request(
        method="POST", headers={"Authorization": f"Bearer {raw}"},
        body=json.dumps(req).encode()))
    assert result.status == 200
    assert json.loads(result.text)["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_streamable_batch_positional_params_notification_dropped():
    from custom_components.phoenix_mcp.mcp_view import _handle_streamable_batch

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=True, limit=60, remaining=59, reset=9999999999)

    items = [
        {"jsonrpc": "2.0", "method": "ping", "params": []},          # notification: dropped
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": []},  # request: -32602
    ]
    resp = await _handle_streamable_batch(items, token, rl, hass, data, "rid", "127.0.0.1", "http://h")
    body = json.loads(resp.text)
    assert len(body) == 1
    assert body[0]["id"] == 2 and body[0]["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_streamable_batch_malformed_no_id_still_gets_error_entry():
    from custom_components.phoenix_mcp.mcp_view import _handle_streamable_batch

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    rl = RateLimitResult(allowed=True, rate_limiting_enabled=True, limit=60, remaining=59, reset=9999999999)

    items = [
        {"jsonrpc": "2.0", "method": 7},           # malformed, no id
        {"jsonrpc": "2.0", "method": "ping"},       # valid notification, no id
    ]
    resp = await _handle_streamable_batch(items, token, rl, hass, data, "rid", "127.0.0.1", "http://h")
    body = json.loads(resp.text)
    # The malformed item produces an Invalid Request (id null); the valid
    # notification produces no entry. So exactly one entry, and it is the error.
    assert len(body) == 1
    assert body[0]["error"]["code"] == -32600
    assert body[0]["id"] is None


def test_validate_number_range_rejects_nan_and_inf():
    from custom_components.phoenix_mcp.tools.native import _validate_number_range
    assert _validate_number_range("x", float("nan"), 0, 100) is not None
    assert _validate_number_range("x", float("inf"), 0, 100) is not None
    assert _validate_number_range("x", float("-inf"), 0, 100) is not None
    assert _validate_number_range("x", 50, 0, 100) is None


# --- tools/list filtering ---

@pytest.mark.asyncio
async def test_tools_list_includes_read_tools_always():
    token, _ = _make_token()  # no write scope
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("get_state", "get_states", "get_history", "get_statistics", "GetLiveContext", "GetDateTime", "get_approval_status"):
        assert name in tool_names


@pytest.mark.asyncio
async def test_initialize_includes_token_aware_instructions():
    # Channel A: the MCP initialize result carries a token-aware primer that
    # names the token's confirm-gated caps and links to the skill endpoint.
    token, _ = _make_token(cap_automation_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "initialize", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    instr = result["result"]["instructions"]
    assert "http://homeassistant.local/api/phoenix-mcp/skill" in instr
    assert "get_capability_summary" in instr
    assert "pending_approval" in instr
    assert "cap_automation_write" in instr  # confirm-gated cap is surfaced


@pytest.mark.asyncio
async def test_initialize_nudges_find_available_actions_when_authoring_and_search():
    # The authoring-discoverability nudge appears for a token that can author AND
    # search (find_available_actions is the entity-scoped, any-entity moments path).
    token, _ = _make_token(cap_automation_write="allow")
    token.cap_search = "allow"
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "initialize", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local",
    )
    instr = result["result"]["instructions"]
    assert "find_available_actions" in instr
    assert "Before authoring an automation" in instr


@pytest.mark.asyncio
async def test_initialize_omits_authoring_nudge_without_search():
    # Cannot search -> find_available_actions is unavailable -> no nudge (keeps the
    # always-on primer clean for tokens the guidance does not apply to).
    token, _ = _make_token(cap_automation_write="allow")  # cap_search defaults to deny
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "initialize", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local",
    )
    assert "Before authoring an automation" not in result["result"]["instructions"]


@pytest.mark.asyncio
async def test_skill_view_serves_markdown_unauthenticated():
    from custom_components.phoenix_mcp.skill_view import PhoenixSkillView

    view = PhoenixSkillView()
    assert view.requires_auth is False
    hass = MagicMock()
    data = MagicMock()
    data.shutting_down = False
    data.store.get_settings.return_value = MagicMock(kill_switch=False)
    hass.data = {DOMAIN: data}
    view.hass = hass
    resp = await view.get(MagicMock())
    assert resp.status == 200
    assert resp.content_type == "text/markdown"
    assert "Phoenix MCP" in resp.text
    assert "get_approval_status" in resp.text


@pytest.mark.asyncio
async def test_tools_list_hides_control_tools_without_write_scope():
    token, _ = _make_token()  # no GREEN grants, not pass_through
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("call_service", "HassTurnOn", "HassTurnOff", "HassLightSet", "HassStopMoving"):
        assert name not in tool_names


@pytest.mark.asyncio
async def test_tools_list_shows_control_tools_with_write_scope():
    token, _ = _make_token(permissions=PermissionTree(domains={"light": PermissionNode(state="GREEN")}))
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("call_service", "HassTurnOn", "HassTurnOff"):
        assert name in tool_names


@pytest.mark.asyncio
async def test_tools_list_pass_through_has_write_scope():
    token, _ = _make_token(pass_through=True)
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    assert "call_service" in tool_names
    assert "HassTurnOn" in tool_names


@pytest.mark.asyncio
async def test_tools_list_announce_all_overrides_gating():
    # A read-only token with announce_all_tools sees the full surface.
    token, _ = _make_token()
    token.announce_all_tools = True
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("call_service", "HassTurnOn", "get_config", "create_automation", "restart_ha"):
        assert name in tool_names


@pytest.mark.asyncio
async def test_tools_list_excludes_system_tools_when_flags_false():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("get_config", "render_template", "create_automation", "restart_ha"):
        assert name not in tool_names


@pytest.mark.asyncio
async def test_tools_list_includes_system_tools_when_flags_true():
    token, _ = _make_token(
        cap_config_read="allow",
        cap_template_render="allow",
        cap_restart="allow",
        cap_automation_write="allow",
    )
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("get_config", "render_template", "create_automation", "restart_ha"):
        assert name in tool_names


@pytest.mark.asyncio
async def test_tools_list_pass_through_non_exempt_flags_auto_granted():
    """Pass-through grants non-exempt flag tools automatically, but not exempt-flag tools."""
    token, _ = _make_token(pass_through=True)
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    # Non-exempt flags: pass-through grants these automatically
    for name in ("get_config", "render_template"):
        assert name in tool_names
    # Exempt flags: require explicit grant even for pass-through tokens
    for name in ("create_automation", "restart_ha"):
        assert name not in tool_names


@pytest.mark.asyncio
async def test_tools_list_pass_through_with_exempt_flags():
    """Pass-through + explicit exempt flags yields all system tools."""
    token, _ = _make_token(
        pass_through=True,
        cap_restart="allow",
        cap_automation_write="allow",
        cap_script_write="allow",
    )
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "tools/list", 1, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    tool_names = [t["name"] for t in result["result"]["tools"]]
    for name in ("get_config", "render_template", "create_automation", "restart_ha"):
        assert name in tool_names


# --- tools/call: get_state ---

@pytest.mark.asyncio
async def test_tools_call_get_state_denied_entity_not_accessible():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.NO_ACCESS

        result, _m, _r, outcome = await _dispatch_mcp(
            "tools/call",
            3,
            {"name": "get_state", "arguments": {"entity_id": "light.kitchen"}},
            token,
            hass,
            data,
            "127.0.0.1",
            base_url="http://homeassistant.local"
        )

    assert outcome == "denied"
    content = result["result"]["content"][0]["text"]
    assert "not found" in content.lower()
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_get_state_success():
    token, _ = _make_token()
    data = _make_data(token)

    state_mock = MagicMock()
    state_mock.entity_id = "light.kitchen"
    state_mock.state = "on"
    state_mock.attributes = {"brightness": 255}

    hass = _make_hass(data)
    hass.states.get.return_value = state_mock

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.WRITE

        with patch("custom_components.phoenix_mcp.mcp_view.scrub_sensitive_attributes") as mock_scrub:
            mock_scrub.return_value = {"entity_id": "light.kitchen", "state": "on", "attributes": {}}

            result, _m, _r, outcome = await _dispatch_mcp(
                "tools/call",
                3,
                {"name": "get_state", "arguments": {"entity_id": "light.kitchen"}},
                token,
                hass,
                data,
                "127.0.0.1",
                base_url="http://homeassistant.local"
            )

    assert outcome == "allowed"
    content = result["result"]["content"][0]["text"]
    payload = json.loads(content)
    assert payload["entity_id"] == "light.kitchen"


@pytest.mark.asyncio
async def test_tools_call_unhandled_exception_returns_clean_error_not_hang():
    # _call_tool is a bare dispatcher with no exception handling of its own, and
    # post() has none either. Before this safety net, an unhandled exception in a
    # tool handler (e.g. a type-confusion bug triggered by a model sending an
    # unexpected argument type) propagated all the way to aiohttp's default error
    # handling, which the MCP client apparently cannot parse cleanly - live-
    # observed as a multi-minute client hang, not just an ugly error. This must
    # instead come back as a normal, well-formed JSON-RPC result with a generic,
    # non-leaking message so the agent can react instead of hanging.
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    with patch(
        "custom_components.phoenix_mcp.mcp_view._call_tool",
        side_effect=AttributeError("'list' object has no attribute 'strip'"),
    ):
        result, _m, resource, outcome = await _dispatch_mcp(
            "tools/call",
            3,
            {"name": "HassTurnOn", "arguments": {"name": ["a", "b"]}},
            token,
            hass,
            data,
            "127.0.0.1",
            base_url="http://homeassistant.local",
        )

    assert outcome == "invalid_request"
    assert resource == "HassTurnOn"
    content = result["result"]["content"][0]["text"]
    assert result["result"].get("isError") is True
    # Never leak the raw exception text (could reveal internals) - generic only.
    assert "strip" not in content
    assert "AttributeError" not in content
    assert "internal error" in content.lower()


@pytest.mark.asyncio
async def test_tools_call_get_state_not_found_entity_same_response_as_denied():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.NOT_FOUND

        result_nf, _m, _r, outcome_nf = await _dispatch_mcp(
            "tools/call",
            3,
            {"name": "get_state", "arguments": {"entity_id": "light.ghost"}},
            token,
            hass,
            data,
            "127.0.0.1",
            base_url="http://homeassistant.local"
        )

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        mock_resolve.return_value = Permission.DENY

        result_denied, _m, _r, outcome_denied = await _dispatch_mcp(
            "tools/call",
            4,
            {"name": "get_state", "arguments": {"entity_id": "light.ghost"}},
            token,
            hass,
            data,
            "127.0.0.1",
            base_url="http://homeassistant.local"
        )

    nf_text = result_nf["result"]["content"][0]["text"]
    denied_text = result_denied["result"]["content"][0]["text"]
    assert nf_text == denied_text


# --- get_state / get_states field projection (v2.1) ---

from custom_components.phoenix_mcp.mcp_view import (  # noqa: E402
    _normalize_fields,
    _select_state_fields,
    _lean_state,
    _project_state,
)


def _full_light_dict():
    return {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {
            "friendly_name": "Kitchen",
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "supported_features": 44,   # not domain-important -> dropped in lean
            "icon": "mdi:lamp",         # not domain-important -> dropped in lean
        },
        "last_changed": "2026-06-29T00:00:00+00:00",
        "last_updated": "2026-06-29T00:00:00+00:00",
        "context": {"id": "abc", "user_id": None, "parent_id": None},
    }


def test_normalize_fields_accepts_list_csv_and_rejects_garbage():
    assert _normalize_fields(["state", " attr.brightness "]) == ["state", "attr.brightness"]
    assert _normalize_fields("state, attr.brightness") == ["state", "attr.brightness"]
    assert _normalize_fields(None) == []
    assert _normalize_fields(123) == []
    assert _normalize_fields([]) == []


def test_lean_state_keeps_base_plus_domain_attrs_only():
    lean = _lean_state(_full_light_dict())
    assert lean["entity_id"] == "light.kitchen"
    assert lean["state"] == "on"
    assert lean["attributes"]["friendly_name"] == "Kitchen"
    assert lean["attributes"]["brightness"] == 200
    assert lean["attributes"]["color_temp_kelvin"] == 3000
    assert "supported_features" not in lean["attributes"]
    assert "icon" not in lean["attributes"]
    # heavy top-level fields dropped in lean
    assert "context" not in lean
    assert "last_updated" not in lean


def test_lean_state_unknown_domain_base_only():
    d = {"entity_id": "weird.thing", "state": "x", "attributes": {"friendly_name": "W", "foo": 1}}
    lean = _lean_state(d)
    assert lean["attributes"] == {"friendly_name": "W"}
    assert "foo" not in lean["attributes"]


@pytest.mark.parametrize(
    "entity_id, keep_attr, drop_attr",
    [
        # Additions to existing domains
        ("light.k", "color_mode", "supported_features"),
        ("media_player.tv", "is_volume_muted", "entity_id"),
        ("cover.garage", "device_class", "assumed_state"),
        ("number.setpoint", "mode", "editable"),
        # New domains
        ("update.router", "installed_version", "release_summary"),
        ("valve.water", "current_position", "assumed_state"),
        ("timer.pasta", "remaining", "editable"),
        ("sun.sun", "next_dawn", "azimuth"),          # azimuth intentionally omitted
        ("automation.morning", "last_triggered", "id"),
        ("script.bedtime", "last_action", "friendly_name_extra"),
        ("input_select.mode", "options", "editable"),
        ("calendar.work", "start_time", "description"),  # description intentionally omitted (long)
        ("remote.tv", "current_activity", "activity_list"),  # the value kept, the *_list dropped
    ],
)
def test_lean_state_domain_allowlist_keeps_primary_drops_junk(entity_id, keep_attr, drop_attr):
    d = {
        "entity_id": entity_id,
        "state": "x",
        "attributes": {"friendly_name": "N", keep_attr: "v", drop_attr: "junk"},
    }
    lean = _lean_state(d)
    assert lean["attributes"].get(keep_attr) == "v"
    assert drop_attr not in lean["attributes"]
    assert lean["attributes"]["friendly_name"] == "N"


def test_lean_state_counter_uses_minimum_maximum_not_min_max():
    # counter's HA attributes are minimum/maximum/step (NOT min/max like number),
    # the exact name gotcha the allowlist was verified against.
    d = {
        "entity_id": "counter.visits",
        "state": "3",
        "attributes": {"friendly_name": "V", "minimum": 0, "maximum": 10, "step": 1, "min": 999},
    }
    lean = _lean_state(d)
    assert lean["attributes"]["minimum"] == 0
    assert lean["attributes"]["maximum"] == 10
    assert lean["attributes"]["step"] == 1
    assert "min" not in lean["attributes"]  # counter has no min/max, only minimum/maximum


def test_domain_important_attributes_are_string_tuples():
    from custom_components.phoenix_mcp.const import DOMAIN_IMPORTANT_ATTRIBUTES
    for domain, attrs in DOMAIN_IMPORTANT_ATTRIBUTES.items():
        assert isinstance(domain, str) and domain
        assert isinstance(attrs, tuple) and attrs
        assert all(isinstance(a, str) and a for a in attrs)
        assert len(set(attrs)) == len(attrs), f"{domain} has duplicate attrs"


def test_select_state_fields_topmost_attr_and_all():
    d = _full_light_dict()
    sel = _select_state_fields(d, ["state", "attr.brightness", "last_changed"])
    assert sel == {
        "entity_id": "light.kitchen",
        "state": "on",
        "last_changed": "2026-06-29T00:00:00+00:00",
        "attributes": {"brightness": 200},
    }
    sel_all = _select_state_fields(d, ["attributes"])
    assert sel_all["attributes"] == d["attributes"]
    sel_unknown = _select_state_fields(d, ["nope", "attr.nope"])
    assert sel_unknown == {"entity_id": "light.kitchen"}


def test_select_state_fields_cannot_resurrect_scrubbed_attr():
    # access_token is already scrubbed out before projection; requesting it returns nothing.
    d = {"entity_id": "camera.front", "state": "idle", "attributes": {"friendly_name": "Front"}}
    sel = _select_state_fields(d, ["attr.access_token"])
    assert sel == {"entity_id": "camera.front"}
    assert "attributes" not in sel


def test_project_state_modes():
    d = _full_light_dict()
    assert _project_state(d, None, True) is d  # detailed -> full as-is
    assert _project_state(d, ["state"], False) == {"entity_id": "light.kitchen", "state": "on"}
    lean = _project_state(d, None, False)
    assert "supported_features" not in lean["attributes"]


@pytest.mark.asyncio
async def test_tools_call_get_state_lean_default_and_detailed():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    hass.states.get.return_value = MagicMock()
    full = {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "K", "brightness": 5, "icon": "mdi:x"},
        "last_updated": "t",
        "context": {"id": "c"},
    }
    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.WRITE
        with patch("custom_components.phoenix_mcp.mcp_view.scrub_sensitive_attributes", return_value=full):
            res_lean, _m, _r, out_lean = await _dispatch_mcp(
                "tools/call", 3,
                {"name": "get_state", "arguments": {"entity_id": "light.kitchen"}},
                token, hass, data, "127.0.0.1", base_url="http://h",
            )
            res_full, _m2, _r2, out_full = await _dispatch_mcp(
                "tools/call", 4,
                {"name": "get_state", "arguments": {"entity_id": "light.kitchen", "detailed": True}},
                token, hass, data, "127.0.0.1", base_url="http://h",
            )
    assert out_lean == "allowed" and out_full == "allowed"
    lean = json.loads(res_lean["result"]["content"][0]["text"])
    assert lean["attributes"]["brightness"] == 5
    assert "icon" not in lean["attributes"]
    assert "context" not in lean
    full_out = json.loads(res_full["result"]["content"][0]["text"])
    assert full_out["attributes"]["icon"] == "mdi:x"
    assert "context" in full_out


# --- get_logbook / get_calendar_events (v2.1) ---

@pytest.mark.asyncio
async def test_get_logbook_forbidden_without_cap():
    token, _ = _make_token(cap_log_read="deny")
    data = _make_data(token)
    hass = _make_hass(data)
    res, _m, _r, outcome = await _dispatch_mcp(
        "tools/call", 3, {"name": "get_logbook", "arguments": {}},
        token, hass, data, "127.0.0.1", base_url="http://h",
    )
    assert outcome == "denied"
    assert res["result"].get("isError") is True


@pytest.mark.asyncio
async def test_get_logbook_scopes_to_accessible_entities():
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    from custom_components.phoenix_mcp.policy_engine import Permission

    def _res(eid, tok, h):
        return Permission.READ if eid == "light.ok" else Permission.NO_ACCESS

    entries = [
        {"entity_id": "light.ok", "message": "turned on"},
        {"entity_id": "light.secret", "message": "turned off"},
        {"name": "Some event", "message": "no entity id"},
    ]
    # _logbook_entry_visible (mcp_view.resolve) does the scoping under test; the
    # extra filter_service_response redaction pass has its own coverage, so stub it
    # to identity here (it would otherwise hit a real entity registry on the mock).
    with patch("custom_components.phoenix_mcp.mcp_view.resolve", side_effect=_res), \
         patch("custom_components.phoenix_mcp.mcp_view.filter_service_response", side_effect=lambda d, t, h: d), \
         patch("custom_components.phoenix_mcp.mcp_view.async_ws_command", new=AsyncMock(return_value=entries)):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3, {"name": "get_logbook", "arguments": {}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "allowed"
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["entries"][0]["entity_id"] == "light.ok"


@pytest.mark.asyncio
async def test_get_calendar_events_returns_events_for_accessible_calendar():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    hass.services.async_call = AsyncMock(return_value={"calendar.fam": {"events": [{"summary": "Dentist"}]}})
    from custom_components.phoenix_mcp.policy_engine import Permission
    with patch("custom_components.phoenix_mcp.mcp_view.resolve", return_value=Permission.READ):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3,
            {"name": "get_calendar_events", "arguments": {"calendar_id": "calendar.fam"}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "allowed"
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["calendar_id"] == "calendar.fam"
    assert payload["events"][0]["summary"] == "Dentist"


@pytest.mark.asyncio
async def test_get_calendar_events_rejects_non_calendar_entity():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    from custom_components.phoenix_mcp.policy_engine import Permission
    with patch("custom_components.phoenix_mcp.mcp_view.resolve", return_value=Permission.READ):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3,
            {"name": "get_calendar_events", "arguments": {"calendar_id": "light.kitchen"}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "invalid_request"


@pytest.mark.asyncio
async def test_get_calendar_events_not_found_when_inaccessible():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    from custom_components.phoenix_mcp.policy_engine import Permission
    with patch("custom_components.phoenix_mcp.mcp_view.resolve", return_value=Permission.NO_ACCESS):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3,
            {"name": "get_calendar_events", "arguments": {"calendar_id": "calendar.fam"}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "denied"


# --- list_blueprints (v2.1) ---

@pytest.mark.asyncio
async def test_list_blueprints_forbidden_without_cap():
    token, _ = _make_token(cap_config_read="deny")
    data = _make_data(token)
    hass = _make_hass(data)
    res, _m, _r, outcome = await _dispatch_mcp(
        "tools/call", 3, {"name": "list_blueprints", "arguments": {}},
        token, hass, data, "127.0.0.1", base_url="http://h",
    )
    assert outcome == "denied"
    assert res["result"].get("isError") is True


@pytest.mark.asyncio
async def test_list_blueprints_lists_with_inputs():
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    bp = MagicMock()
    bp.metadata = {"name": "Motion Light", "description": "d", "input": {"motion": {}}, "source_url": "u"}
    dom_bp = MagicMock()
    dom_bp.async_get_blueprints = AsyncMock(return_value={"author/motion.yaml": bp})
    with patch("homeassistant.components.automation.async_get_blueprints", return_value=dom_bp), \
         patch("homeassistant.components.script.async_get_blueprints", return_value=dom_bp):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3, {"name": "list_blueprints", "arguments": {"domain": "automation"}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "allowed"
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["count"] == 1
    row = payload["blueprints"][0]
    assert row["name"] == "Motion Light"
    assert row["domain"] == "automation"
    assert row["path"] == "author/motion.yaml"
    assert row["input"] == {"motion": {}}
    assert "warnings" not in payload  # clean read carries no warnings key


@pytest.mark.asyncio
async def test_list_blueprints_warns_when_a_domain_cannot_be_read():
    """A domain that fails to read must not vanish, or a short list looks complete."""
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    with patch("homeassistant.components.automation.async_get_blueprints",
               side_effect=RuntimeError("boom")), \
         patch("homeassistant.components.script.async_get_blueprints",
               side_effect=RuntimeError("boom")):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3, {"name": "list_blueprints", "arguments": {}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "allowed"
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["count"] == 0
    assert len(payload["warnings"]) == 2  # one per domain
    assert any("automation" in w for w in payload["warnings"])


@pytest.mark.asyncio
async def test_list_blueprints_warns_about_blueprints_that_failed_to_load():
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    ok = MagicMock()
    ok.metadata = {"name": "Fine"}
    dom_bp = MagicMock()
    # HA stores a blueprint that failed to parse as its own exception.
    dom_bp.async_get_blueprints = AsyncMock(
        return_value={"a/ok.yaml": ok, "a/broken.yaml": ValueError("bad yaml")})
    with patch("homeassistant.components.automation.async_get_blueprints", return_value=dom_bp):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3, {"name": "list_blueprints", "arguments": {"domain": "automation"}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "allowed"
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["warnings"] == ["1 automation blueprint(s) failed to load and were omitted."]


# --- get_blueprint ---

def _bp_env(hass, tmp_path, body="blueprint:\n  name: X\ntrigger:\n  - platform: state\n    entity_id: !input sensor\n"):
    """A blueprint HA has enumerated, backed by a real file on disk."""
    import os
    hass.config.config_dir = str(tmp_path)
    hass.config.path = lambda *parts: os.path.join(str(tmp_path), *parts)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    os.makedirs(os.path.join(str(tmp_path), "blueprints", "automation", "author"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "blueprints", "automation", "author", "motion.yaml"),
              "w", encoding="utf-8") as f:
        f.write(body)
    bp = MagicMock()
    bp.metadata = {"name": "Motion Light", "description": "d", "input": {"sensor": {}}, "source_url": "u"}
    dom_bp = MagicMock()
    dom_bp.async_get_blueprints = AsyncMock(return_value={"author/motion.yaml": bp})
    return dom_bp


async def _get_blueprint(args, token, hass, data, dom_bp):
    with patch("homeassistant.components.automation.async_get_blueprints", return_value=dom_bp), \
         patch("homeassistant.components.script.async_get_blueprints", return_value=dom_bp):
        return await _dispatch_mcp(
            "tools/call", 3, {"name": "get_blueprint", "arguments": args},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )


@pytest.mark.asyncio
async def test_get_blueprint_forbidden_without_cap(tmp_path):
    token, _ = _make_token(cap_config_read="deny")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": "author/motion.yaml"}, token, hass, data, dom_bp)
    assert outcome == "denied"
    assert res["result"].get("isError") is True


@pytest.mark.asyncio
async def test_get_blueprint_returns_source_with_inputs(tmp_path):
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": "author/motion.yaml"}, token, hass, data, dom_bp)
    assert outcome == "allowed"
    body = json.loads(res["result"]["content"][0]["text"])
    assert body["name"] == "Motion Light"
    assert body["input"] == {"sensor": {}}
    # Raw source: the !input placeholder survives verbatim, unlike bp.data.
    assert "!input sensor" in body["content"]


@pytest.mark.asyncio
async def test_get_blueprint_unknown_path_not_found(tmp_path):
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": "author/nope.yaml"}, token, hass, data, dom_bp)
    assert outcome == "not_found"
    assert res["result"].get("isError") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../../secrets.yaml", "/etc/passwd", "author/../../../x.yaml"])
async def test_get_blueprint_traversal_is_not_expressible(tmp_path, path):
    """The path must match a key HA enumerated, so traversal never reaches the disk."""
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    _res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": path}, token, hass, data, dom_bp)
    assert outcome == "not_found"


@pytest.mark.asyncio
async def test_get_blueprint_reports_a_blueprint_that_failed_to_load(tmp_path):
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = MagicMock()
    dom_bp.async_get_blueprints = AsyncMock(return_value={"a/bad.yaml": ValueError("bad yaml")})
    res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": "a/bad.yaml"}, token, hass, data, dom_bp)
    assert outcome == "invalid_request"
    assert "failed to load" in res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_blueprint_rejects_a_bad_domain(tmp_path):
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    _res, _m, _r, outcome = await _get_blueprint(
        {"domain": "light", "path": "author/motion.yaml"}, token, hass, data, dom_bp)
    assert outcome == "invalid_request"


@pytest.mark.asyncio
async def test_get_blueprint_missing_path_argument(tmp_path):
    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path)
    _res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation"}, token, hass, data, dom_bp)
    assert outcome == "invalid_request"


@pytest.mark.asyncio
async def test_get_blueprint_oversized_file_refused(tmp_path):
    from custom_components.phoenix_mcp.const import MAX_FILE_BYTES

    token, _ = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    dom_bp = _bp_env(hass, tmp_path, body="x" * (MAX_FILE_BYTES + 1))
    res, _m, _r, outcome = await _get_blueprint(
        {"domain": "automation", "path": "author/motion.yaml"}, token, hass, data, dom_bp)
    assert outcome == "invalid_request"
    assert "maximum readable size" in res["result"]["content"][0]["text"]


# --- get_logs: an unloaded system_log is not an empty log ---

@pytest.mark.asyncio
async def test_get_logs_errors_when_system_log_not_loaded():
    """Returning [] here said "no warnings or errors", the opposite of the truth."""
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    hass.data = {DOMAIN: data}  # no "system_log" key
    res, _m, _r, outcome = await _dispatch_mcp(
        "tools/call", 3, {"name": "get_logs", "arguments": {}},
        token, hass, data, "127.0.0.1", base_url="http://h",
    )
    assert outcome == "invalid_request"
    assert res["result"].get("isError") is True
    assert "system_log" in res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_logs_returns_entries_when_system_log_present():
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    record = MagicMock()
    record.level = "ERROR"
    record.name = "homeassistant.components.light"
    record.message = ["it broke"]
    record.source = ("light/__init__.py", 1)
    record.timestamp = 0
    record.exception = ""
    record.count = 1
    syslog = MagicMock()
    syslog.records = {"k": record}
    hass.data = {DOMAIN: data, "system_log": syslog}
    res, _m, _r, outcome = await _dispatch_mcp(
        "tools/call", 3, {"name": "get_logs", "arguments": {}},
        token, hass, data, "127.0.0.1", base_url="http://h",
    )
    assert outcome == "allowed"
    assert json.loads(res["result"]["content"][0]["text"])["count"] == 1


# --- get_logbook: an unexpected shape is not an empty logbook ---

@pytest.mark.asyncio
async def test_get_logbook_errors_on_non_list_result():
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    with patch("custom_components.phoenix_mcp.mcp_view.async_ws_command",
               new=AsyncMock(return_value={"unexpected": "shape"})):
        res, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 3, {"name": "get_logbook", "arguments": {}},
            token, hass, data, "127.0.0.1", base_url="http://h",
        )
    assert outcome == "invalid_request"
    assert res["result"].get("isError") is True


# --- tools/call: restart_ha dual-gate ---

@pytest.mark.asyncio
async def test_tools_call_restart_ha_denied_without_cap_restart():
    token, _ = _make_token(cap_restart="deny", pass_through=False)
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        5,
        {"name": "restart_ha", "arguments": {}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_restart_ha_denied_for_pass_through_without_cap_restart():
    token, _ = _make_token(cap_restart="deny", pass_through=True)
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        6,
        {"name": "restart_ha", "arguments": {}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"
    assert result["result"].get("isError") is True


# --- authoring: HA drift must not be reported as the caller's mistake ---

@pytest.mark.asyncio
async def test_authoring_validator_drift_is_not_reported_as_invalid_config():
    """An unexpected exception from HA's validator is an internal error, not bad input.

    HA calls its automation validator with raise_on_errors=True, so vol.Invalid and
    HomeAssistantError are the caller's own bad config. Anything else (an
    AttributeError after an HA refactor, say) used to land in the same broad
    catch and answer "check trigger, condition, and action fields", which sends
    an agent into rewriting a config that was never the problem. Regression:
    the message must NOT be the domain guidance, and the failure must be logged.
    """
    from custom_components.phoenix_mcp.tools import authoring

    token, _ = _make_token(cap_automation_write="allow")
    data = _make_data(token)
    hass = _make_hass(data)

    async def _boom(*_a, **_k):
        raise AttributeError("HA moved something")

    with patch.object(authoring, "_validate_automation_config", _boom):
        result, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 7,
            {"name": "create_automation", "arguments": {"config": {"alias": "x"}}},
            token, hass, data, "127.0.0.1", base_url="http://homeassistant.local",
        )

    assert outcome == "invalid_request"
    text = result["result"]["content"][0]["text"]
    assert "Internal error" in text
    assert "trigger, condition" not in text


@pytest.mark.asyncio
async def test_authoring_invalid_config_still_gets_the_domain_guidance():
    """The other half: a real vol.Invalid keeps the actionable message."""
    import voluptuous as vol

    from custom_components.phoenix_mcp.tools import authoring

    token, _ = _make_token(cap_automation_write="allow")
    data = _make_data(token)
    hass = _make_hass(data)

    async def _invalid(*_a, **_k):
        raise vol.Invalid("required key not provided @ data['triggers']")

    with patch.object(authoring, "_validate_automation_config", _invalid):
        result, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 7,
            {"name": "create_automation", "arguments": {"config": {"alias": "x"}}},
            token, hass, data, "127.0.0.1", base_url="http://homeassistant.local",
        )

    assert outcome == "invalid_request"
    text = result["result"]["content"][0]["text"]
    assert "trigger, condition, and action fields" in text
    assert "Internal error" not in text


@pytest.mark.asyncio
async def test_authoring_write_failure_is_not_audited_as_denied():
    """A failed write is an internal error, never outcome "denied".

    "denied" means Phoenix MCP's permission rules blocked the call, and the audit
    log is what an operator reads to see what policy stopped. A disk error or an
    HA service failure filed under that slug pollutes exactly that signal.
    """
    from custom_components.phoenix_mcp.tools import authoring

    token, _ = _make_token(cap_automation_write="allow")
    data = _make_data(token)
    hass = _make_hass(data)

    async def _ok(*_a, **_k):
        return {"alias": "x"}

    async def _fail(*_a, **_k):
        raise OSError("disk full")

    with patch.object(authoring, "_validate_automation_config", _ok), \
         patch.object(hass, "async_add_executor_job", _fail):
        result, _m, _r, outcome = await _dispatch_mcp(
            "tools/call", 7,
            {"name": "create_automation", "arguments": {"config": {"alias": "x"}}},
            token, hass, data, "127.0.0.1", base_url="http://homeassistant.local",
        )

    assert outcome == "invalid_request", "a write failure must not be audited as a policy denial"
    assert result["result"].get("isError") is True


# --- tools/call: automation stubs ---

@pytest.mark.asyncio
async def test_tools_call_create_automation_invalid_config_rejected():
    """Empty config dict fails HA validation and returns invalid_request."""
    token, _ = _make_token(cap_automation_write="allow")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "create_automation", "arguments": {"config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_create_automation_confirm_mode_invalid_config_rejected_before_pending():
    """An invalid config must fail fast even under confirm mode, not sail through
    as a false pending_approval that can only fail once an admin approves it."""
    token, _ = _make_token(cap_automation_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "create_automation", "arguments": {"config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_edit_automation_confirm_mode_invalid_config_rejected_before_pending():
    token, _ = _make_token(cap_automation_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "edit_automation", "arguments": {"automation_id": "abc", "config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_create_automation_denied_before_validation_no_leak():
    """A cap=deny token gets the uniform Forbidden message, never a validation
    detail about its own (invalid) config - deny must be checked before the
    precheck runs, not after."""
    token, _ = _make_token(cap_automation_write="deny")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "create_automation", "arguments": {"config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"
    text = result["result"]["content"][0]["text"]
    assert "not enabled for this token" in text
    assert "validation" not in text.lower()


@pytest.mark.asyncio
async def test_tools_call_automation_denied_without_flag():
    token, _ = _make_token(cap_automation_write="deny")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        8,
        {"name": "delete_automation", "arguments": {"automation_id": "abc"}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"
    assert result["result"].get("isError") is True


# --- tools/call: script stubs ---

@pytest.mark.asyncio
async def test_tools_call_create_script_confirm_mode_invalid_config_rejected_before_pending():
    """Same precheck as create_automation: an invalid script config must not
    become a pending approval that only fails after an admin approves it."""
    token, _ = _make_token(cap_script_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "create_script", "arguments": {"script_id": "s1", "config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_edit_script_confirm_mode_invalid_config_rejected_before_pending():
    token, _ = _make_token(cap_script_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "edit_script", "arguments": {"script_id": "s1", "config": {}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_create_script_bad_id_confirm_mode_rejected_before_pending():
    """The script_id format check is also structural, so it must fail fast too."""
    token, _ = _make_token(cap_script_write="confirm")
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": "create_script", "arguments": {"script_id": "Not Valid!", "config": {"sequence": []}}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "invalid_request"
    assert result["result"].get("isError") is True


@pytest.mark.asyncio
async def test_tools_call_garbled_tool_name_is_capped_before_dispatch_and_logging():
    """A malformed client (e.g. a local model emitting garbled tool-call syntax -
    stray tag debris or special tokens leaking into what should be a bare tool
    name) must not be able to write an unbounded string into the audit log's
    method/resource fields. Every real tool name is short, so the cap never
    affects a legitimate call."""
    from custom_components.phoenix_mcp.const import MAX_TOOL_NAME_LENGTH

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    garbled = "search_entities" + "<_key>domain</arg_key><arg_value>x</arg_value>" * 20
    assert len(garbled) > MAX_TOOL_NAME_LENGTH

    result, _m, resource, outcome = await _dispatch_mcp(
        "tools/call",
        7,
        {"name": garbled, "arguments": {}},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"  # unknown tool
    assert len(resource) <= MAX_TOOL_NAME_LENGTH
    logged_method = data.audit.record.call_args.kwargs["method"]
    logged_resource = data.audit.record.call_args.kwargs["resource"]
    assert len(logged_method) <= MAX_TOOL_NAME_LENGTH
    assert len(logged_resource) <= MAX_TOOL_NAME_LENGTH


# --- resources ---

@pytest.mark.asyncio
async def test_resources_list_returns_server_info():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "resources/list", 9, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "allowed"
    resources = result["result"]["resources"]
    assert any(r["uri"] == "phx://server-info" for r in resources)


@pytest.mark.asyncio
async def test_resources_read_server_info():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    hass.states.async_all.return_value = []

    result, _m, _r, outcome = await _dispatch_mcp(
        "resources/read", 10, {"uri": "phx://server-info"}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "allowed"
    contents = result["result"]["contents"]
    assert len(contents) == 1
    payload = json.loads(contents[0]["text"])
    assert payload["token_name"] == token.name
    assert "capability_flags" in payload


@pytest.mark.asyncio
async def test_resources_read_unknown_uri_returns_error():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "resources/read", 11, {"uri": "phx://nonexistent"}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "denied"
    assert "error" in result


# --- unknown method ---

@pytest.mark.asyncio
async def test_unknown_method_with_id_returns_jsonrpc_error():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, outcome = await _dispatch_mcp(
        "nonexistent/method", 99, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert outcome == "not_implemented"
    assert result is not None
    assert "error" in result
    assert result["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_notification_no_id_returns_no_response():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    result, _m, _r, _o = await _dispatch_mcp(
        "notifications/initialized", None, {}, token, hass, data, "127.0.0.1",
        base_url="http://homeassistant.local"
    )

    assert result is None


# --- context endpoint ---

def _make_token_with_permissions(entities: dict[str, PermissionNode]) -> tuple[TokenRecord, str]:
    tree = PermissionTree(entities=entities)
    return _make_token(permissions=tree)


def _make_hass_with_states(data: PhoenixData, entity_ids: list[str]) -> MagicMock:
    hass = _make_hass(data)
    states = []
    for eid in entity_ids:
        s = MagicMock()
        s.entity_id = eid
        states.append(s)
    hass.states.async_all.return_value = states
    hass.states.get = MagicMock(side_effect=lambda eid: next((s for s in states if s.entity_id == eid), None))
    return hass


@pytest.mark.asyncio
async def test_context_plain_entity_with_hint_appears():
    token, raw = _make_token_with_permissions({
        "light.kitchen": PermissionNode(state="GREEN", hint="The main kitchen light")
    })
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve, \
         patch("custom_components.phoenix_mcp.mcp_view.get_effective_hint", return_value="The main kitchen light"):
        from custom_components.phoenix_mcp.policy_engine import Permission

        def resolve_side_effect(eid, tok, hass):
            if eid == "light.kitchen":
                return Permission.WRITE
            return Permission.NO_ACCESS

        mock_resolve.side_effect = resolve_side_effect
        hass = _make_hass_with_states(data, ["light.kitchen"])
        text = _build_context_plain(token, hass)

    assert "light.kitchen" in text
    assert "The main kitchen light" in text
    assert "READ/WRITE" in text


@pytest.mark.asyncio
async def test_context_plain_entity_without_hint_renders_normally():
    token, raw = _make_token_with_permissions({
        "light.kitchen": PermissionNode(state="GREEN", hint=None)
    })
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve, \
         patch("custom_components.phoenix_mcp.mcp_view.get_effective_hint", return_value=None):
        from custom_components.phoenix_mcp.policy_engine import Permission

        def resolve_side_effect(eid, tok, hass):
            if eid == "light.kitchen":
                return Permission.WRITE
            return Permission.NO_ACCESS

        mock_resolve.side_effect = resolve_side_effect
        hass = _make_hass_with_states(data, ["light.kitchen"])
        text = _build_context_plain(token, hass)

    assert "light.kitchen" in text
    assert '"' not in text.split("light.kitchen")[1].split("\n")[0]


@pytest.mark.asyncio
async def test_context_plain_read_permission_shows_read_only():
    token, _ = _make_token_with_permissions({
        "sensor.temp": PermissionNode(state="YELLOW")
    })
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve, \
         patch("custom_components.phoenix_mcp.mcp_view.get_effective_hint", return_value=None):
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.READ
        hass = _make_hass_with_states(data, ["sensor.temp"])
        text = _build_context_plain(token, hass)

    assert "sensor.temp (READ)" in text
    assert "READ/WRITE" not in text


@pytest.mark.asyncio
async def test_context_json_entity_with_hint():
    token, _ = _make_token_with_permissions({
        "switch.relay_1": PermissionNode(state="GREEN", hint="Holiday lights power switch")
    })
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve, \
         patch("custom_components.phoenix_mcp.mcp_view.get_effective_hint", return_value="Holiday lights power switch"):
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.WRITE

        with patch("custom_components.phoenix_mcp.mcp_view.er") as mock_er_mod:
            with patch("custom_components.phoenix_mcp.mcp_view.dr") as mock_dr_mod:
                mock_registry = MagicMock()
                mock_registry.async_get.return_value = None
                mock_er_mod.async_get.return_value = mock_registry

                mock_dev_reg = MagicMock()
                mock_dr_mod.async_get.return_value = mock_dev_reg

                hass = _make_hass_with_states(data, ["switch.relay_1"])
                payload = _build_context_json(token, hass)

    entity_entry = next(e for e in payload["entities"] if e["entity_id"] == "switch.relay_1")
    assert entity_entry["hint"] == "Holiday lights power switch"
    assert entity_entry["permission"] == "READ/WRITE"


@pytest.mark.asyncio
async def test_context_json_entity_without_hint_has_no_hint_key():
    token, _ = _make_token_with_permissions({
        "light.hall": PermissionNode(state="GREEN", hint=None)
    })
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve, \
         patch("custom_components.phoenix_mcp.mcp_view.get_effective_hint", return_value=None):
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.WRITE

        with patch("custom_components.phoenix_mcp.mcp_view.er") as mock_er_mod:
            with patch("custom_components.phoenix_mcp.mcp_view.dr") as mock_dr_mod:
                mock_registry = MagicMock()
                mock_registry.async_get.return_value = None
                mock_er_mod.async_get.return_value = mock_registry

                mock_dev_reg = MagicMock()
                mock_dr_mod.async_get.return_value = mock_dev_reg

                hass = _make_hass_with_states(data, ["light.hall"])
                payload = _build_context_json(token, hass)

    entity_entry = next(e for e in payload["entities"] if e["entity_id"] == "light.hall")
    assert "hint" not in entity_entry


@pytest.mark.asyncio
async def test_context_json_includes_capability_flags():
    token, _ = _make_token(cap_config_read="allow", cap_restart="allow")
    data = _make_data(token)

    with patch("custom_components.phoenix_mcp.mcp_view.resolve") as mock_resolve:
        from custom_components.phoenix_mcp.policy_engine import Permission
        mock_resolve.return_value = Permission.NO_ACCESS

        with patch("custom_components.phoenix_mcp.mcp_view.er") as mock_er_mod:
            with patch("custom_components.phoenix_mcp.mcp_view.dr") as mock_dr_mod:
                mock_er_mod.async_get.return_value = MagicMock()
                mock_dr_mod.async_get.return_value = MagicMock()

                hass = _make_hass_with_states(data, [])
                payload = _build_context_json(token, hass)

    assert payload["capability_flags"]["cap_config_read"] == "allow"
    assert payload["capability_flags"]["cap_restart"] == "allow"
    assert payload["capability_flags"]["cap_template_render"] == "deny"


@pytest.mark.asyncio
async def test_context_endpoint_returns_plain_text_by_default():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    view = _make_context_view(data, hass)
    request = _make_request(headers={"Authorization": f"Bearer {raw}"})

    result = await view.get(request)

    assert result.status == 200
    assert "text/plain" in result.content_type


@pytest.mark.asyncio
async def test_context_endpoint_returns_json_with_format_param():
    token, raw = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    view = _make_context_view(data, hass)
    request = _make_request(
        headers={"Authorization": f"Bearer {raw}"},
        query={"format": "json"},
    )

    with patch("custom_components.phoenix_mcp.mcp_view.er") as mock_er_mod:
        with patch("custom_components.phoenix_mcp.mcp_view.dr") as mock_dr_mod:
            mock_er_mod.async_get.return_value = MagicMock()
            mock_dr_mod.async_get.return_value = MagicMock()
            result = await view.get(request)

    assert result.status == 200
    assert "application/json" in result.content_type
    payload = json.loads(result.text)
    assert "entities" in payload
    assert "capability_flags" in payload


@pytest.mark.asyncio
async def test_context_endpoint_401_without_auth():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    view = _make_context_view(data, hass)
    request = _make_request(headers={})

    result = await view.get(request)

    assert result.status == 401


# ---- HassTurnOn/Off physical-control gating ----------------------------------


def _make_physical_token(cap_physical_control: str) -> TokenRecord:
    from homeassistant.util.dt import utcnow

    raw = _raw_token()
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="phys-token",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        created_at=utcnow(),
        created_by="user1",
        cap_physical_control=cap_physical_control,
        permissions=PermissionTree(),
    )


def _fake_action_done(calls):
    async def _fake_intent_action(tool, domain, service, sd, entities, h, token=None, args=None):
        calls.append((domain, service, tuple(entities)))
        body = json.dumps({
            "speech": {}, "response_type": "action_done",
            "data": {"success": [{"id": e, "type": "entity"} for e in entities], "failed": []},
        })
        return ({"content": [{"type": "text", "text": body}]}, "allowed", tool)
    return _fake_intent_action


@pytest.mark.asyncio
async def test_call_service_cover_toggle_denied_without_physical_control(hass):
    # End-to-end through _tool_call_service: cover.toggle (a service the old
    # exact-name allowlist missed) must hit the cap_physical_control gate for a
    # token that has WRITE on the cover but physical control denied.
    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _tool_call_service
    from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree

    perms = PermissionTree(entities={"cover.g": PermissionNode(state="GREEN")})
    token, _ = _make_token(permissions=perms)
    token.cap_physical_control = "deny"
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _tool_call_service(
        {"domain": "cover", "service": "toggle", "entity_id": "cover.g"},
        token, hass, data, "rid", "1.2.3.4",
    )
    assert outcome == "denied"
    assert result.get("isError") is True


@pytest.mark.asyncio
async def test_turn_on_allow_includes_physical_entities():
    from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on

    token = _make_physical_token("allow")
    data = _make_data(token)
    hass = _make_hass(data)
    calls: list = []

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["light.kitchen", "lock.front_door"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_action_done(calls)), \
         patch("custom_components.phoenix_mcp.tools.native._gate", new=AsyncMock(return_value=None)) as gate:
        result = await _tool_hass_turn_on({"area": "Kitchen"}, token, hass, data, "rid", None)

    # The light goes via homeassistant.turn_on; the lock must go via lock.lock,
    # because homeassistant.turn_on cannot operate a lock on current HA.
    assert ("homeassistant", "turn_on", ("light.kitchen",)) in calls
    assert ("lock", "lock", ("lock.front_door",)) in calls
    merged = json.loads(result[0]["content"][0]["text"])
    assert {e["id"] for e in merged["data"]["success"]} == {"light.kitchen", "lock.front_door"}
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_turn_on_deny_strips_physical_entities():
    from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on

    token = _make_physical_token("deny")
    data = _make_data(token)
    hass = _make_hass(data)
    captured = {}

    async def _fake_intent_action(tool, domain, service, sd, entities, h, token=None, args=None):
        captured["entities"] = entities
        return ({"content": []}, "allowed", tool)

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["light.kitchen", "lock.front_door"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_intent_action), \
         patch("custom_components.phoenix_mcp.tools.native._gate", new=AsyncMock(return_value=None)) as gate:
        await _tool_hass_turn_on({"area": "Kitchen"}, token, hass, data, "rid", None)

    assert captured["entities"] == ["light.kitchen"]
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_turn_on_confirm_with_physical_creates_pending():
    from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on

    token = _make_physical_token("confirm")
    data = _make_data(token)
    hass = _make_hass(data)
    pending = ({"content": [], "_pending": True}, "pending_approval", "approval:HassTurnOn:x")
    intent = AsyncMock()

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["light.kitchen", "lock.front_door"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", new=intent), \
         patch("custom_components.phoenix_mcp.tools.native._gate", new=AsyncMock(return_value=pending)) as gate:
        result = await _tool_hass_turn_on({"area": "Kitchen"}, token, hass, data, "rid", None)

    assert result == pending
    gate.assert_awaited_once()
    # The action must NOT fire while the request is pending approval.
    intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_turn_on_confirm_without_physical_fires_immediately():
    from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on

    token = _make_physical_token("confirm")
    data = _make_data(token)
    hass = _make_hass(data)
    captured = {}

    async def _fake_intent_action(tool, domain, service, sd, entities, h, token=None, args=None):
        captured["entities"] = entities
        return ({"content": []}, "allowed", tool)

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["light.kitchen", "switch.fan"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_intent_action), \
         patch("custom_components.phoenix_mcp.tools.native._gate", new=AsyncMock(return_value=None)) as gate:
        await _tool_hass_turn_on({"area": "Kitchen"}, token, hass, data, "rid", None)

    assert captured["entities"] == ["light.kitchen", "switch.fan"]
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_execute_turn_on_includes_physical_under_confirm():
    """Approved executors must actuate physical locks through lock.lock."""
    from custom_components.phoenix_mcp.tools.native import _execute_hass_turn_on

    token = _make_physical_token("confirm")
    data = _make_data(token)
    hass = _make_hass(data)
    calls: list = []

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["light.kitchen", "lock.front_door"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_action_done(calls)):
        await _execute_hass_turn_on({"area": "Kitchen"}, token, hass, data)

    assert ("lock", "lock", ("lock.front_door",)) in calls


def test_turn_service_groups_routes_lock_and_cover():
    """Lock and cover turn actions route through domain services."""
    from custom_components.phoenix_mcp.tools.native import _turn_service_groups

    on = {(d, s): e for d, s, e in _turn_service_groups("turn_on", ["light.k", "lock.f", "cover.g"])}
    assert on[("homeassistant", "turn_on")] == ["light.k"]
    assert on[("lock", "lock")] == ["lock.f"]
    assert on[("cover", "open_cover")] == ["cover.g"]

    off = {(d, s): e for d, s, e in _turn_service_groups("turn_off", ["lock.f", "cover.g"])}
    assert off[("lock", "unlock")] == ["lock.f"]
    assert off[("cover", "close_cover")] == ["cover.g"]


def test_turn_service_groups_routes_vacuum():
    # Regression: homeassistant.turn_on/off silently no-ops on vacuum entities
    # (HA logs "does not support entities vacuum.X"), the same class of bug
    # already fixed for lock/cover. "off" maps to stop (halt in place), not
    # return_to_base, matching the word "stop" rather than "dock".
    from custom_components.phoenix_mcp.tools.native import _turn_service_groups

    on = {(d, s): e for d, s, e in _turn_service_groups("turn_on", ["vacuum.roomba"])}
    assert on[("vacuum", "start")] == ["vacuum.roomba"]

    off = {(d, s): e for d, s, e in _turn_service_groups("turn_off", ["vacuum.roomba"])}
    assert off[("vacuum", "stop")] == ["vacuum.roomba"]


def test_turn_service_groups_routes_valve():
    # Same silent no-op class as vacuum: the valve domain has open/close_valve,
    # no turn_on/off services, so homeassistant.turn_on/off cannot operate it.
    # "Turn on the water" -> open the valve, mirroring HA's own intent handling.
    from custom_components.phoenix_mcp.tools.native import _turn_service_groups

    on = {(d, s): e for d, s, e in _turn_service_groups("turn_on", ["valve.irrigation"])}
    assert on[("valve", "open_valve")] == ["valve.irrigation"]

    off = {(d, s): e for d, s, e in _turn_service_groups("turn_off", ["valve.irrigation"])}
    assert off[("valve", "close_valve")] == ["valve.irrigation"]


@pytest.mark.asyncio
async def test_set_position_routes_cover_and_valve_by_domain():
    """HassSetPosition dispatches cover.set_cover_position vs valve.set_valve_position
    per entity domain (the spec promises valve support; it was cover-hardcoded)."""
    from custom_components.phoenix_mcp.tools.native import _execute_hass_set_position

    token = _make_physical_token("allow")
    data = _make_data(token)
    hass = _make_hass(data)
    calls: list = []

    async def _fake_intent_action(tool, domain, service, sd, entities, h, token=None, args=None):
        calls.append((domain, service, tuple(entities), dict(sd)))
        body = json.dumps({
            "speech": {}, "response_type": "action_done",
            "data": {"success": [{"id": e, "type": "entity"} for e in entities], "failed": []},
        })
        return ({"content": [{"type": "text", "text": body}]}, "allowed", tool)

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["valve.irrigation"]) as resolver, \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_intent_action):
        result = await _execute_hass_set_position({"name": "Irrigation Valve", "position": 50}, token, hass, data)

    # Default resolution now spans both position-capable domains.
    assert resolver.call_args.kwargs["domains"] == ["cover", "valve"]
    assert calls == [("valve", "set_valve_position", ("valve.irrigation",), {"position": 50})]
    assert result[1] == "allowed"

    calls.clear()
    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["cover.garage"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_intent_action):
        await _execute_hass_set_position({"name": "Garage", "position": 30}, token, hass, data)
    assert calls == [("cover", "set_cover_position", ("cover.garage",), {"position": 30})]


@pytest.mark.asyncio
async def test_set_position_mixed_domains_merged():
    """An area-wide set-position spanning covers and valves issues one call per
    domain service and merges the action_done responses."""
    from custom_components.phoenix_mcp.tools.native import _execute_hass_set_position

    token = _make_physical_token("allow")
    data = _make_data(token)
    hass = _make_hass(data)
    calls: list = []

    async def _fake_intent_action(tool, domain, service, sd, entities, h, token=None, args=None):
        calls.append((domain, service, tuple(entities), dict(sd)))
        body = json.dumps({
            "speech": {}, "response_type": "action_done",
            "data": {"success": [{"id": e, "type": "entity"} for e in entities], "failed": []},
        })
        return ({"content": [{"type": "text", "text": body}]}, "allowed", tool)

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities",
               return_value=["cover.patio_awning", "valve.irrigation"]), \
         patch("custom_components.phoenix_mcp.tools.native._tool_intent_action", side_effect=_fake_intent_action):
        result = await _execute_hass_set_position({"area": "Patio", "position": 50}, token, hass, data)

    assert ("cover", "set_cover_position", ("cover.patio_awning",), {"position": 50}) in calls
    assert ("valve", "set_valve_position", ("valve.irrigation",), {"position": 50}) in calls
    merged = json.loads(result[0]["content"][0]["text"])
    assert {e["id"] for e in merged["data"]["success"]} == {"cover.patio_awning", "valve.irrigation"}


@pytest.mark.asyncio
async def test_turn_on_off_registered_as_executors():
    from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY

    assert "HassTurnOn" in _EXECUTOR_REGISTRY
    assert "HassTurnOff" in _EXECUTOR_REGISTRY


@pytest.mark.asyncio
async def test_turn_on_confirm_creates_real_approval_then_executor_actuates(hass):
    """End-to-end native confirm path with the REAL _gate (not mocked).

    The sibling confirm tests above mock _gate to a canned pending tuple; this one
    drives the real capability gate so we verify the parts those cannot: that a real
    PendingApproval is created carrying the saved args and the right cap, that the
    lock is held (not actuated) while pending, and that async_execute_approved_tool re-runs
    from those saved args and actuates lock.lock through the real _tool_intent_action.
    Intent resolution itself is stubbed (covered in test_policy_engine).
    """
    import asyncio as _asyncio
    from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool
    from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on
    from custom_components.phoenix_mcp.data import PhoenixData

    class _ApprStore:
        def __init__(self) -> None:
            self._p: list = []
            self.async_lock = _asyncio.Lock()
            self.async_save = AsyncMock()

        def get_pending_approvals(self) -> list:
            return self._p

        def set_pending_approvals(self, v: list) -> None:
            self._p = v

    token = _make_physical_token("confirm")
    store = _ApprStore()
    # mesa=None and no hass.data[DOMAIN] entry -> the MESA gate degrades to allow-all.
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    hass.states.async_set("lock.front_door", "unlocked", {})
    lock_calls: list = []

    async def _lock_handler(call):
        eid = call.data.get("entity_id")
        lock_calls.append(eid if isinstance(eid, list) else [eid])

    hass.services.async_register("lock", "lock", _lock_handler)

    with patch("custom_components.phoenix_mcp.tools.native.resolve_intent_entities", return_value=["lock.front_door"]):
        result = await _tool_hass_turn_on({"area": "Hall"}, token, hass, data, "rid-1", "1.2.3.4")
        # A real approval exists; the lock is held, not actuated.
        assert result[1] == "pending_approval"
        assert lock_calls == []
        assert len(store._p) == 1
        appr = store._p[0]
        assert appr["tool_name"] == "HassTurnOn"
        assert appr["cap_name"] == "cap_physical_control"
        assert appr["args"] == {"area": "Hall"}

        # Approving re-runs from the SAVED args and actuates lock.lock for real.
        out = await async_execute_approved_tool("HassTurnOn", appr["args"], token, hass, data)
        await hass.async_block_till_done()

    assert out[1] == "allowed"
    assert lock_calls and "lock.front_door" in lock_calls[0]


# --- optional inline wait on a confirm gate (token confirm_inline_wait_seconds) ---

def _inline_wait_data(pending_records: list) -> PhoenixData:
    store = MagicMock()
    store.get_pending_approvals.return_value = pending_records
    return PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)


def _approval_record(status: str, *, result=None, rejected_reason=None) -> dict:
    from homeassistant.util.dt import utcnow
    from datetime import timedelta
    return {
        "id": "appr-1", "token_id": "tid", "token_name": "t",
        "tool_name": "create_scene", "cap_name": "cap_scene_write",
        "args": {}, "diff": {}, "status": status,
        "created_at": utcnow().isoformat(),
        "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
        "request_id": "rid", "result": result, "rejected_reason": rejected_reason,
    }


@pytest.mark.asyncio
async def test_inline_confirm_returns_executed_result_on_approval(hass):
    """When a token opts into the inline wait and its approval resolves approved
    inside the window, the agent gets the executed tool result directly (no poll),
    while the original request's audit outcome stays pending_approval."""
    import asyncio
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _await_inline_confirm

    record = _approval_record(
        "approved",
        result={"tool_result": {"content": [{"type": "text", "text": "done"}]}, "outcome": "allowed"},
    )
    data = _inline_wait_data([record])
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)

    task = asyncio.create_task(_await_inline_confirm(hass, data, token, approval))
    await asyncio.sleep(0.01)  # let the bus listener register
    hass.bus.async_fire("phoenix_mcp_approval_resolved", {"approval_id": "appr-1"})
    content, outcome, resource = await task

    # The executed result comes back first, with the operator-accepted note
    # appended so the agent treats the approved change as settled.
    assert content["content"][0] == {"type": "text", "text": "done"}
    assert "reviewed this exact change and approved it" in content["content"][1]["text"]
    assert not content.get("isError")
    assert outcome == "pending_approval"
    assert resource == "approval:create_scene:appr-1"
    # The stored approval record's result is never mutated by the append.
    assert record["result"]["tool_result"] == {"content": [{"type": "text", "text": "done"}]}


@pytest.mark.asyncio
async def test_inline_confirm_reports_rejection_without_retry(hass):
    """A rejection inside the window returns an isError result telling the agent
    the action did not apply, not a stale pending stub."""
    import asyncio
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _await_inline_confirm

    data = _inline_wait_data([_approval_record("rejected", rejected_reason="admin_cancelled")])
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)

    task = asyncio.create_task(_await_inline_confirm(hass, data, token, approval))
    await asyncio.sleep(0.01)
    hass.bus.async_fire("phoenix_mcp_approval_resolved", {"approval_id": "appr-1"})
    content, outcome, _ = await task

    assert content.get("isError") is True
    body = json.loads(content["content"][0]["text"])
    assert body["status"] == "rejected"
    assert "admin_cancelled" in body["message"]
    assert outcome == "pending_approval"


@pytest.mark.asyncio
async def test_inline_confirm_falls_back_to_pending_on_timeout(hass):
    """No decision inside the window falls back to the normal immediate pending
    reply, identical to a token that never opted into the wait."""
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _await_inline_confirm

    data = _inline_wait_data([])
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)

    with patch("custom_components.phoenix_mcp.mcp_view.asyncio.wait_for", side_effect=TimeoutError):
        content, outcome, _ = await _await_inline_confirm(hass, data, token, approval)

    assert outcome == "pending_approval"
    assert json.loads(content["content"][0]["text"])["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_gate_routes_to_inline_wait_only_when_opted_in():
    """_gate calls the inline wait for a pending result iff the token opted in;
    otherwise it returns the immediate pending reply as before."""
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _gate

    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)
    pending = SimpleNamespace(is_deny=False, is_pending=True, approval=approval)
    token = _make_physical_token("confirm")
    data = _inline_wait_data([])
    hass = MagicMock()

    sentinel = ({"content": []}, "pending_approval", "approval:create_scene:appr-1")
    with patch("custom_components.phoenix_mcp.tool_common.async_evaluate_capability", new=AsyncMock(return_value=pending)), \
         patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm", new=AsyncMock(return_value=sentinel)) as inline:
        token.confirm_inline_wait_seconds = 0
        out_off = await _gate("cap_scene_write", token, hass, data,
                              tool_name="create_scene", args={}, request_id="r", client_ip=None, diff={})
        assert inline.await_count == 0
        assert out_off[1] == "pending_approval"

        token.confirm_inline_wait_seconds = 60
        out_on = await _gate("cap_scene_write", token, hass, data,
                             tool_name="create_scene", args={}, request_id="r", client_ip=None, diff={})
        assert inline.await_count == 1
        assert out_on is sentinel


@pytest.mark.asyncio
async def test_a_mesa_confirm_honours_the_inline_wait_like_a_capability_confirm():
    """The two kinds of confirm gate must hold the request the same way.

    A MESA confirm used to return `_tool_pending` directly and never consult
    `confirm_inline_wait_seconds`, so an operator who configured an inline wait
    got it for a capability confirm and not for a MESA one. Both now go through
    the single `_pending_or_inline` definition.
    """
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _pending_or_inline

    approval = SimpleNamespace(id="appr-9", tool_name="call_service", expires_at=None)
    token = _make_physical_token("confirm")
    data = _inline_wait_data([])
    hass = MagicMock()

    sentinel = ({"content": []}, "pending_approval", "approval:call_service:appr-9")
    with patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm",
               new=AsyncMock(return_value=sentinel)) as inline:
        token.confirm_inline_wait_seconds = 0
        out_off = await _pending_or_inline(hass, data, token, approval)
        assert inline.await_count == 0
        assert out_off[1] == "pending_approval"

        token.confirm_inline_wait_seconds = 60
        out_on = await _pending_or_inline(hass, data, token, approval)
        assert inline.await_count == 1
        assert out_on is sentinel


def _resolved_approval(status, *, tool_result=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        id="appr-7", status=status, tool_name="call_service",
        created_at=None, expires_at=None, resolved_at=None,
        result={"tool_result": tool_result} if tool_result is not None else None,
        rejected_reason=None,
    )


def test_polling_an_approved_action_carries_the_operator_accepted_note():
    """The note has to reach agents that POLL, not just those that wait inline.

    Every approval outliving the inline window resolves by polling, and those
    are the slow ones an agent is most likely to re-propose. Before this, only
    `_await_inline_confirm` and Agent Chat appended it.
    """
    from custom_components.phoenix_mcp.mcp_view import _approval_status_result
    from custom_components.phoenix_mcp.tool_common import _OPERATOR_ACCEPTED_NOTE
    from custom_components.phoenix_mcp.approvals import STATUS_APPROVED

    record = _resolved_approval(STATUS_APPROVED, tool_result={"content": [{"type": "text", "text": "{}"}]})
    out = _approval_status_result(record, resolved=True)

    texts = [c.get("text") for c in out["content"]]
    assert _OPERATOR_ACCEPTED_NOTE in texts
    # The status body is still the first item, so existing readers are unaffected.
    assert json.loads(texts[0])["status"] == STATUS_APPROVED


def test_polling_a_rejection_carries_no_acceptance_note():
    """Nothing was accepted, so saying otherwise would be a lie the agent acts on."""
    from custom_components.phoenix_mcp.mcp_view import _approval_status_result
    from custom_components.phoenix_mcp.tool_common import _OPERATOR_ACCEPTED_NOTE
    from custom_components.phoenix_mcp.approvals import STATUS_REJECTED

    out = _approval_status_result(_resolved_approval(STATUS_REJECTED), resolved=True)

    assert all(c.get("text") != _OPERATOR_ACCEPTED_NOTE for c in out["content"])


def test_polling_an_approval_that_never_ran_carries_no_note():
    """Approved but with no stored result: there is no landed change to accept."""
    from custom_components.phoenix_mcp.mcp_view import _approval_status_result
    from custom_components.phoenix_mcp.tool_common import _OPERATOR_ACCEPTED_NOTE
    from custom_components.phoenix_mcp.approvals import STATUS_APPROVED

    out = _approval_status_result(_resolved_approval(STATUS_APPROVED), resolved=True)

    assert all(c.get("text") != _OPERATOR_ACCEPTED_NOTE for c in out["content"])


# --- stale-tool-list advisory (soft tools/list_changed) ---

async def _dt_call(token, hass, data, msg_id=1):
    """One benign tools/call; returns the result content list."""
    result, _m, _r, _o = await _dispatch_mcp(
        "tools/call", msg_id, {"name": "GetDateTime", "arguments": {}},
        token, hass, data, "127.0.0.1", base_url="http://homeassistant.local",
    )
    return result["result"]["content"]


def _has_advisory(content) -> bool:
    return any(
        c.get("type") == "text" and "tool list" in c.get("text", "") and "changed" in c.get("text", "")
        for c in content
    )


@pytest.mark.asyncio
async def test_stale_advisory_fires_once_per_epoch_and_resets_on_list():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    # Fresh fetch of the tool list echoes the settings version.
    await _dispatch_mcp("tools/list", 1, {}, token, hass, data, "127.0.0.1",
                        base_url="http://homeassistant.local")
    assert token.tools_list_version == token.settings_version
    assert not _has_advisory(await _dt_call(token, hass, data, 2))

    # Operator changes the token's settings after the fetch: advise ONCE.
    token.settings_version += 1
    assert _has_advisory(await _dt_call(token, hass, data, 3))
    assert not _has_advisory(await _dt_call(token, hass, data, 4))

    # A further change starts a new staleness epoch: advise once more.
    token.settings_version += 1
    assert _has_advisory(await _dt_call(token, hass, data, 5))
    assert not _has_advisory(await _dt_call(token, hass, data, 6))

    # A fresh tools/list clears everything.
    await _dispatch_mcp("tools/list", 7, {}, token, hass, data, "127.0.0.1",
                        base_url="http://homeassistant.local")
    assert token.tools_list_version == token.settings_version
    assert token.id not in data.stale_tools_advised
    assert not _has_advisory(await _dt_call(token, hass, data, 8))


def _has_catalog_advisory(content) -> bool:
    return any(
        c.get("type") == "text" and "Phoenix MCP was updated" in c.get("text", "")
        for c in content
    )


@pytest.mark.asyncio
async def test_a_deploy_advises_that_tool_schemas_may_have_changed():
    """The second staleness axis: the BUILD changed, not the token.

    A client that cached a tool's schema before an argument was added cannot
    send that argument at all, because its own copy of the schema forbids it.
    settings_version never moves on a deploy, so nothing else would advise it,
    and the transport cannot push tools/list_changed.
    """
    from custom_components.phoenix_mcp import mcp_view

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    await _dispatch_mcp("tools/list", 1, {}, token, hass, data, "127.0.0.1",
                        base_url="http://homeassistant.local")
    assert token.tools_catalog_fingerprint == mcp_view._TOOL_CATALOG_FINGERPRINT
    assert not _has_catalog_advisory(await _dt_call(token, hass, data, 2))

    # A deploy: what this client fetched no longer matches what the build serves.
    token.tools_catalog_fingerprint = "0000deadbeef0000"
    assert _has_catalog_advisory(await _dt_call(token, hass, data, 3))
    assert not _has_catalog_advisory(await _dt_call(token, hass, data, 4))

    # Reconnecting is the only fix, and it clears the notice.
    await _dispatch_mcp("tools/list", 5, {}, token, hass, data, "127.0.0.1",
                        base_url="http://homeassistant.local")
    assert not _has_catalog_advisory(await _dt_call(token, hass, data, 6))


@pytest.mark.asyncio
async def test_a_token_that_never_recorded_a_fingerprint_is_never_catalog_stale():
    """Empty means no baseline, not staleness.

    token_store cannot import mcp_view to learn the current fingerprint (the
    dependency runs the other way), so a record written before this field
    existed has nothing to compare against. Advising on empty would fire once
    for every token on upgrade, for nothing.
    """
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    assert token.tools_catalog_fingerprint == ""
    assert not _has_catalog_advisory(await _dt_call(token, hass, data, 1))


def test_the_catalog_advisory_does_not_send_the_agent_to_the_capability_summary():
    """The two advisories prescribe DIFFERENT fixes, and that is the point.

    Permissions did not change on a deploy, so get_capability_summary reports
    the wrong thing and costs a call; only a reconnect replaces a schema the
    client already cached.
    """
    from custom_components.phoenix_mcp import mcp_view

    assert "get_capability_summary" in mcp_view._STALE_TOOLS_ADVISORY
    assert "get_capability_summary" not in mcp_view._STALE_CATALOG_ADVISORY
    assert "reconnect" in mcp_view._STALE_CATALOG_ADVISORY


def test_the_fingerprint_tracks_arguments_and_ignores_descriptions():
    """Argument changes force a reconnect; rewordings must not.

    A client enforces its cached schema before sending, so a new argument is
    unusable until it refetches. A reworded description costs it nothing, and
    treating prose edits as staleness would fire the notice constantly and train
    operators to ignore it.
    """
    from custom_components.phoenix_mcp import mcp_view

    original = mcp_view._catalog_fingerprint()
    target = next(
        d for d in mcp_view._SYSTEM_TOOL_DEFS if d["name"] == "get_esphome_job")

    old_desc = target["description"]
    target["description"] = old_desc + " Reworded guidance."
    try:
        assert mcp_view._catalog_fingerprint() == original
    finally:
        target["description"] = old_desc

    old_schema = target["inputSchema"]
    target["inputSchema"] = {
        **old_schema,
        "properties": {**old_schema["properties"], "brand_new": {"type": "string"}},
    }
    try:
        assert mcp_view._catalog_fingerprint() != original
    finally:
        target["inputSchema"] = old_schema

    assert mcp_view._catalog_fingerprint() == original


@pytest.mark.asyncio
async def test_stale_advisory_suppressed_for_agentcli():
    """agentCLI rebuilds the tool list every turn, so it never gets the advisory,
    and its call does not consume the staleness epoch for a real client."""
    from custom_components.phoenix_mcp.const import AGENTCLI_CLIENT_IP

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)

    token.settings_version += 1  # stale relative to tools_list_version (0)

    # An agentCLI-sentinel call gets no advisory and does not mark the epoch.
    res_ac, _m, _r, _o = await _dispatch_mcp(
        "tools/call", 1, {"name": "GetDateTime", "arguments": {}},
        token, hass, data, AGENTCLI_CLIENT_IP, base_url="http://homeassistant.local",
    )
    assert not _has_advisory(res_ac["result"]["content"])
    assert token.id not in data.stale_tools_advised

    # A real external client on the same token still gets advised once.
    assert _has_advisory(await _dt_call(token, hass, data, 2))


@pytest.mark.asyncio
async def test_stale_advisory_suppressed_for_assist():
    """The Assist bridge rebuilds its tool set every turn, so like agentCLI it
    never gets the advisory and does not consume the staleness epoch."""
    from custom_components.phoenix_mcp.const import ASSIST_CLIENT_IP

    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    token.settings_version += 1

    res, _m, _r, _o = await _dispatch_mcp(
        "tools/call", 1, {"name": "GetDateTime", "arguments": {}},
        token, hass, data, ASSIST_CLIENT_IP, base_url="http://homeassistant.local",
    )
    assert not _has_advisory(res["result"]["content"])
    assert token.id not in data.stale_tools_advised
    assert _has_advisory(await _dt_call(token, hass, data, 2))


@pytest.mark.asyncio
async def test_stale_advisory_tagged_in_audit():
    token, _ = _make_token()
    data = _make_data(token)
    hass = _make_hass(data)
    token.settings_version += 1  # never listed since the change

    await _dt_call(token, hass, data)
    kwargs = data.audit.record.call_args.kwargs
    assert kwargs["stale_tools_advisory"] is True

    await _dt_call(token, hass, data, 2)
    kwargs = data.audit.record.call_args.kwargs
    assert kwargs["stale_tools_advisory"] is False


@pytest.mark.asyncio
async def test_legacy_token_never_stale_on_upgrade():
    # Records written before the counters existed load with
    # tools_list_version == settings_version, so no spurious advisory.
    token, _ = _make_token()
    raw = token.to_storage_dict()
    del raw["settings_version"]
    del raw["tools_list_version"]
    loaded = TokenRecord.from_dict(raw)
    data = _make_data(loaded)
    hass = _make_hass(data)
    assert not _has_advisory(await _dt_call(loaded, hass, data))


@pytest.mark.asyncio
async def test_call_service_surfaces_validation_error_as_invalid_request(hass):
    """A ServiceValidationError from HA (e.g. an out-of-range setpoint) is surfaced
    to the agent as invalid_request with its message, not masked as 'Forbidden.'.

    The token already holds WRITE on the entity, so the argument-level message
    leaks nothing about hidden entities/services (no oracle regression).
    """
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        "climate", "demo", "office_ac", suggested_object_id="office_ac"
    )
    hass.states.async_set(entry.entity_id, "cool", {})

    async def _raise(call):
        raise ServiceValidationError(
            "Provided temperature 22.0 is not valid. Accepted range is 45 to 95."
        )

    hass.services.async_register("climate", "set_temperature", _raise)

    token, _ = _make_token(pass_through=True)
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _execute_call_service(
        {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": entry.entity_id,
            "service_data": {"temperature": 22},
        },
        token,
        hass,
        data,
    )
    assert outcome == "invalid_request"
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert text.startswith("Invalid request:")
    assert "45 to 95" in text


@pytest.mark.asyncio
async def test_call_service_surfaces_schema_rejection_as_invalid_request(hass):
    """hass.services.async_call validates service_data against the target
    service's OWN voluptuous schema and re-raises vol.Invalid/MultipleInvalid
    unwrapped (not as a HomeAssistantError), so without its own catch a service
    whose schema does not accept entity_id at all falls through the
    ServiceValidationError/HomeAssistantError catches entirely and hits the
    dispatch-level safety net as a bare 'Internal error', losing the
    actionable voluptuous detail. The
    triggering shape is a model calling a service that does not target entities,
    with a resolved entity attached anyway."""
    import voluptuous as vol
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        "light", "demo", "kitchen", suggested_object_id="kitchen"
    )
    hass.states.async_set(entry.entity_id, "on", {})

    async def _handler(call):
        pass  # unreachable: schema validation rejects before this runs

    hass.services.async_register(
        "notify", "persistent_notification", _handler,
        schema=vol.Schema({vol.Required("message"): str}),
    )

    token, _ = _make_token(pass_through=True)
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _execute_call_service(
        {
            "domain": "notify",
            "service": "persistent_notification",
            "entity_id": entry.entity_id,
            "service_data": {"message": "hi"},
        },
        token,
        hass,
        data,
    )
    assert outcome == "invalid_request"
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert text.startswith("Invalid request:")
    assert "entity_id" in text


@pytest.mark.asyncio
async def test_intent_action_surfaces_schema_rejection_as_invalid_request(hass):
    """Same fix as test_call_service_surfaces_schema_rejection_as_invalid_request,
    for the native Hass* tool path (_tool_intent_action), which has its own
    parallel ServiceNotFound/ServiceValidationError/HomeAssistantError catch."""
    import voluptuous as vol

    from custom_components.phoenix_mcp.tools.native import _tool_intent_action

    async def _handler(call):
        pass  # unreachable: schema validation rejects before this runs

    hass.services.async_register(
        "notify", "persistent_notification", _handler,
        schema=vol.Schema({vol.Required("message"): str}),
    )

    token, _ = _make_token(pass_through=True)

    result, outcome, _res = await _tool_intent_action(
        "HassBroadcast", "notify", "persistent_notification",
        {"message": "hi"}, ["light.kitchen"], hass, token,
    )
    assert outcome == "invalid_request"
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert text.startswith("Invalid request:")
    assert "entity_id" in text


@pytest.mark.asyncio
async def test_call_service_service_not_found_stays_generic_forbidden(hass):
    """ServiceNotFound subclasses ServiceValidationError but must stay generic
    ('Forbidden.'/denied) so service existence is never confirmed (no oracle),
    for a domain NOT in DOMAIN_SERVICE_HINTS (light is deliberately excluded)."""
    from homeassistant.exceptions import ServiceNotFound
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        "light", "demo", "desk1", suggested_object_id="desk1"
    )
    hass.states.async_set(entry.entity_id, "off", {})

    async def _raise(call):
        raise ServiceNotFound("light", "make_bright")

    hass.services.async_register("light", "make_bright", _raise)

    token, _ = _make_token(pass_through=True)
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _execute_call_service(
        {
            "domain": "light",
            "service": "make_bright",
            "entity_id": entry.entity_id,
            "service_data": {},
        },
        token,
        hass,
        data,
    )
    assert outcome == "denied"
    assert result["content"][0]["text"] == "Forbidden."


@pytest.mark.asyncio
async def test_call_service_service_not_found_hints_verbs_for_core_domain(hass):
    """For a curated core actuator domain (DOMAIN_SERVICE_HINTS), a ServiceNotFound
    surfaces the valid core service verbs as invalid_request so the agent can
    self-correct a wrong-verb guess (valve.open -> valve.open_valve). Leak-safe:
    post-authorization + a hardcoded public verb list, no live services lookup."""
    from homeassistant.exceptions import ServiceNotFound
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        "valve", "demo", "irrigation", suggested_object_id="irrigation"
    )
    hass.states.async_set(entry.entity_id, "closed", {})

    async def _raise(call):
        raise ServiceNotFound("valve", "open")

    # Register under the wrong verb the agent guessed, raising ServiceNotFound as
    # HA would for a service that is not actually registered.
    hass.services.async_register("valve", "open", _raise)

    token, _ = _make_token(pass_through=True)
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _execute_call_service(
        {
            "domain": "valve",
            "service": "open",
            "entity_id": entry.entity_id,
            "service_data": {},
        },
        token,
        hass,
        data,
    )
    assert outcome == "invalid_request"
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "open_valve" in text
    assert "close_valve" in text


# --- call_service: no-target reload services (NO_TARGET_SERVICES) ---

@pytest.mark.asyncio
async def test_no_target_reload_executes_bare_without_entity_flattening(hass):
    """A no-target reload (automation.reload) skips entity flattening and
    calls HA with bare service_data. Without the NO_TARGET branch it would fan out
    to every automation.* entity and the service schema would reject the attached
    entity_id ('extra keys not allowed @ data['entity_id']')."""
    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    # A live automation entity: the old flatten path would attach it as a target.
    hass.states.async_set("automation.foo", "on", {})

    captured: dict = {}

    async def _reload_handler(call):
        captured["data"] = dict(call.data)

    hass.services.async_register("automation", "reload", _reload_handler)

    token, _ = _make_token()
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _execute_call_service(
        {"domain": "automation", "service": "reload"},
        token, hass, data,
    )
    assert outcome == "allowed"
    assert result.get("isError") is not True
    assert captured.get("data") == {}  # called with no entity_id target


@pytest.mark.asyncio
async def test_no_target_reload_denied_without_cap_yaml_edit(hass):
    """automation.reload gates on cap_yaml_edit; a token without it is denied at
    the tool gate and the service is never called."""
    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _tool_call_service

    called: list = []

    async def _reload_handler(call):
        called.append(1)

    hass.services.async_register("automation", "reload", _reload_handler)

    token, _ = _make_token()
    token.cap_yaml_edit = "deny"
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _tool_call_service(
        {"domain": "automation", "service": "reload"},
        token, hass, data, "rid-nt", "1.2.3.4",
    )
    assert outcome == "denied"
    assert result.get("isError") is True
    assert called == []


@pytest.mark.asyncio
async def test_no_target_reload_pending_under_confirm(hass):
    """cap_yaml_edit=confirm routes automation.reload through the approval gate: a
    PendingApproval is created (cap_name cap_yaml_edit, tool_name call_service) and
    the service is held, not called."""
    import asyncio as _asyncio

    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _tool_call_service

    class _ApprStore:
        def __init__(self) -> None:
            self._p: list = []
            self.async_lock = _asyncio.Lock()
            self.async_save = AsyncMock()

        def get_pending_approvals(self) -> list:
            return self._p

        def set_pending_approvals(self, v: list) -> None:
            self._p = v

    called: list = []

    async def _reload_handler(call):
        called.append(1)

    hass.services.async_register("automation", "reload", _reload_handler)

    token, _ = _make_token()
    token.cap_yaml_edit = "confirm"
    store = _ApprStore()
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _tool_call_service(
        {"domain": "automation", "service": "reload"},
        token, hass, data, "rid-nt2", "1.2.3.4",
    )
    assert outcome == "pending_approval"
    assert called == []
    assert len(store._p) == 1
    assert store._p[0]["cap_name"] == "cap_yaml_edit"
    assert store._p[0]["tool_name"] == "call_service"


@pytest.mark.asyncio
async def test_dry_run_no_target_reload_predicts_cap_yaml_edit(hass):
    """dry_run_service predicts a no-target reload from cap_yaml_edit (not from
    entity flattening), reporting system_service with no resolved entities."""
    from custom_components.phoenix_mcp.data import PhoenixData
    from custom_components.phoenix_mcp.mcp_view import _tool_dry_run_service

    token, _ = _make_token()
    token.cap_search = "allow"
    token.cap_yaml_edit = "confirm"
    store = MagicMock()
    store.get_settings.return_value = MagicMock(mesa_mode="off")
    data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)

    result, outcome, _res = await _tool_dry_run_service(
        {"domain": "automation", "service": "reload"}, token, hass, data,
    )
    assert outcome == "allowed"
    body = json.loads(result["content"][0]["text"])
    assert body["system_service"] is True
    assert body["resolved_entities"] == []
    assert body["predicted_outcome"] == "pending_approval"
    assert body["would_execute"] is False


def test_inline_resolved_reports_execution_failure_with_executor_error():
    """An admin APPROVED but the executor failed: the inline-wait result must
    read as approved-but-failed with the executor's error, never as a plain
    rejection, which an agent treats as a refusal and retries."""
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _tool_inline_resolved

    approval = SimpleNamespace(
        id="appr-1", status="rejected", rejected_reason="execution_failed",
        result={"tool_result": {"isError": True, "content": [{"type": "text", "text": (
            "This configuration changed since you last read it (expected_hash no "
            "longer matches). Re-read it and reapply your change."
        )}]}, "outcome": "invalid_request"},
    )
    out = _tool_inline_resolved(approval)
    assert out["isError"] is True
    body = json.loads(out["content"][0]["text"])
    assert body["status"] == "execution_failed"
    assert "APPROVED" in body["message"]
    assert "expected_hash no longer matches" in body["message"]

    # A real admin rejection (no stored result) keeps the plain rejected shape.
    plain = SimpleNamespace(id="appr-2", status="rejected", rejected_reason="not now", result=None)
    body2 = json.loads(_tool_inline_resolved(plain)["content"][0]["text"])
    assert body2["status"] == "rejected"
    assert "not now" in body2["message"]


# ---------------------------------------------------------------------------
# prompts/list + prompts/get for pass-through tokens.
#
# These had NO tests, and both call sites wrap _get_ha_assist_api in a bare
# `except Exception`. HA removed LLMContext.user_prompt, so constructing it
# raised TypeError, the except swallowed it, and both endpoints silently
# reported no prompts. Nothing failed loudly; the feature just went quiet.
# Found by type checking. These pin both halves: that the context is built
# against the LIVE dataclass, and that the endpoint actually returns a prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ha_assist_api_builds_a_context_the_live_ha_accepts():
    """Constructing LLMContext must not raise on the installed HA."""
    import dataclasses
    from types import SimpleNamespace

    from homeassistant.helpers import llm as ha_llm

    from custom_components.phoenix_mcp.mcp_view import _get_ha_assist_api

    captured = {}

    async def _fake_get_api(hass, api_id, llm_context):
        captured["ctx"] = llm_context
        return SimpleNamespace(api=SimpleNamespace(name="Assist"))

    with patch.object(ha_llm, "async_get_api", new=_fake_get_api):
        await _get_ha_assist_api(MagicMock())

    ctx = captured["ctx"]
    assert isinstance(ctx, ha_llm.LLMContext)
    # Every field it was given is one the live dataclass declares. A field that
    # HA has since removed (user_prompt) must simply not be passed.
    declared = {f.name for f in dataclasses.fields(ha_llm.LLMContext)}
    assert "platform" in declared and ctx.platform == "phoenix_mcp"
    assert "user_prompt" not in declared or hasattr(ctx, "user_prompt")


@pytest.mark.asyncio
async def test_prompts_list_returns_the_assist_prompt_for_a_pass_through_token():
    """The regression itself: this returned [] because the construction raised."""
    from types import SimpleNamespace

    from homeassistant.helpers import llm as ha_llm

    token, _ = _make_token()
    token.pass_through = True
    data = _make_data(token)
    hass = _make_hass(data)

    async def _fake_get_api(hass_, api_id, llm_context):
        return SimpleNamespace(api=SimpleNamespace(name="Assist"))

    with patch.object(ha_llm, "async_get_api", new=_fake_get_api):
        res, _m, _r, _o = await _dispatch_mcp(
            "prompts/list", 1, {}, token, hass, data, "127.0.0.1",
            base_url="http://homeassistant.local",
        )

    prompts = res["result"]["prompts"]
    assert prompts, "pass-through prompts/list returned nothing; the Assist lookup is failing again"
    assert "Assist" in prompts[0]["name"]


# --- the inline wait steps aside once a backlog exists (batch approval) --------


def _pending_for(approval_id: str, token_id: str = "tid") -> dict:
    """A stored pending record, shaped like _approval_record but addressable."""
    rec = _approval_record("pending")
    rec["id"] = approval_id
    rec["token_id"] = token_id
    return rec


@pytest.mark.asyncio
async def test_inline_wait_is_skipped_when_the_token_already_has_one_pending():
    """The change that makes batch approval usable.

    The hold blocks a whole request and tool calls arrive one at a time, so
    approval N+1 cannot be CREATED until approval N stops waiting. Live-measured
    at a 60s wait, consecutive approvals landed 62.8s apart, i.e. twenty writes
    would take twenty minutes to fill a queue meant to be reviewed in one go.
    """
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _pending_or_inline

    approval = SimpleNamespace(id="appr-2", tool_name="create_scene", expires_at=None)
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    token.id = "tid"
    # Something else from this token is already waiting on the operator.
    data = _inline_wait_data([_pending_for("appr-1")])

    with patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm",
               new=AsyncMock()) as inline:
        _content, outcome, _resource = await _pending_or_inline(MagicMock(), data, token, approval)

    assert inline.await_count == 0
    assert outcome == "pending_approval"


@pytest.mark.asyncio
async def test_the_first_confirm_still_waits():
    """A lone confirm keeps the interactive feel; only a backlog suppresses it.

    Also covers the instant-approve workflow: there each approval resolves in
    seconds, so the queue is empty again by the next call and every call waits.
    """
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _pending_or_inline

    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    token.id = "tid"
    data = _inline_wait_data([])

    sentinel = ({"content": []}, "pending_approval", "approval:create_scene:appr-1")
    with patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm",
               new=AsyncMock(return_value=sentinel)) as inline:
        out = await _pending_or_inline(MagicMock(), data, token, approval)

    assert inline.await_count == 1
    assert out is sentinel


@pytest.mark.asyncio
async def test_the_calls_own_approval_does_not_count_as_a_backlog():
    """async_evaluate_capability stores this approval BEFORE we get here.

    Counting it would make the queue never look empty, so no call would ever
    wait and the inline feature would be silently dead for every token.
    """
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _pending_or_inline

    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    token.id = "tid"
    # The ONLY pending record is this call's own.
    data = _inline_wait_data([_pending_for("appr-1")])

    with patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm",
               new=AsyncMock(return_value=("x", "pending_approval", "r"))) as inline:
        await _pending_or_inline(MagicMock(), data, token, approval)

    assert inline.await_count == 1


@pytest.mark.asyncio
async def test_another_tokens_backlog_does_not_suppress_this_tokens_wait():
    """The queue is per token: one agent's backlog must not change another's."""
    from types import SimpleNamespace
    from custom_components.phoenix_mcp.tool_common import _pending_or_inline

    approval = SimpleNamespace(id="appr-2", tool_name="create_scene", expires_at=None)
    token = _make_physical_token("confirm")
    token.confirm_inline_wait_seconds = 60
    token.id = "tid"
    data = _inline_wait_data([_pending_for("appr-9", token_id="someone-else")])

    with patch("custom_components.phoenix_mcp.tool_common._await_inline_confirm",
               new=AsyncMock(return_value=("x", "pending_approval", "r"))) as inline:
        await _pending_or_inline(MagicMock(), data, token, approval)

    assert inline.await_count == 1


# --- wait_for_approval over several ids (the staged-approval companion) -------


@pytest.mark.asyncio
async def test_wait_for_many_returns_at_once_when_all_already_resolved(hass):
    """No listener, no block: the common case after a batch approve."""
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    a = _approval_record("approved", result={"tool_result": {"content": []}})
    a["id"] = "appr-1"
    b = _approval_record("rejected", rejected_reason="nope")
    b["id"] = "appr-2"
    data = _inline_wait_data([a, b])
    token = _make_physical_token("confirm")
    token.id = "tid"

    content, outcome, resource = await _tool_wait_for_approval(
        {"approval_ids": ["appr-1", "appr-2"]}, token, hass, data)

    body = json.loads(content["content"][0]["text"])
    assert outcome == "allowed" and resource == "approvals:2"
    assert body["resolved"] is True and body["pending"] == []
    assert [x["status"] for x in body["approvals"]] == ["approved", "rejected"]
    # One approved-with-result in the set is enough: those changes are final.
    assert any("operator reviewed" in c["text"] for c in content["content"])


@pytest.mark.asyncio
async def test_wait_for_many_reports_which_ones_are_still_outstanding(hass):
    """A timeout is partial information, not a failure.

    The caller learns exactly which landed, so it can wait again for the rest
    instead of re-waiting the whole set.
    """
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    done = _approval_record("approved", result={"tool_result": {"content": []}})
    done["id"] = "appr-1"
    still = _approval_record("pending")
    still["id"] = "appr-2"
    data = _inline_wait_data([done, still])
    token = _make_physical_token("confirm")
    token.id = "tid"

    content, outcome, _r = await _tool_wait_for_approval(
        {"approval_ids": ["appr-1", "appr-2"], "timeout": 0}, token, hass, data)

    body = json.loads(content["content"][0]["text"])
    assert outcome == "allowed"
    assert body["resolved"] is False and body["pending"] == ["appr-2"]


@pytest.mark.asyncio
async def test_wait_for_many_refuses_another_tokens_approval(hass):
    """Byte-identical to an unknown id, so it cannot enumerate another queue."""
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    other = _approval_record("pending")
    other["id"] = "appr-1"
    other["token_id"] = "someone-else"
    data = _inline_wait_data([other])
    token = _make_physical_token("confirm")
    token.id = "tid"

    theirs, outcome_theirs, _r1 = await _tool_wait_for_approval(
        {"approval_ids": ["appr-1"]}, token, hass, data)
    ghost, outcome_ghost, _r2 = await _tool_wait_for_approval(
        {"approval_ids": ["appr-nope"]}, token, hass, data)

    assert outcome_theirs == outcome_ghost == "not_found"
    assert theirs == ghost


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[], "appr-1", ["appr-1", ""], ["appr-1", 7]])
async def test_wait_for_many_rejects_a_malformed_list(hass, bad):
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    data = _inline_wait_data([])
    token = _make_physical_token("confirm")
    token.id = "tid"
    _content, outcome, _r = await _tool_wait_for_approval(
        {"approval_ids": bad}, token, hass, data)
    assert outcome == "invalid_request"


@pytest.mark.asyncio
async def test_wait_for_many_dedupes_repeated_ids(hass):
    """A repeated id would otherwise be reported twice and count against itself."""
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    rec = _approval_record("approved", result={"tool_result": {"content": []}})
    rec["id"] = "appr-1"
    data = _inline_wait_data([rec])
    token = _make_physical_token("confirm")
    token.id = "tid"

    content, _o, resource = await _tool_wait_for_approval(
        {"approval_ids": ["appr-1", "appr-1"]}, token, hass, data)

    body = json.loads(content["content"][0]["text"])
    assert len(body["approvals"]) == 1 and resource == "approvals:1"


@pytest.mark.asyncio
async def test_single_id_form_is_untouched(hass):
    """The plural form is additive; existing callers keep the object shape."""
    from custom_components.phoenix_mcp.mcp_view import _tool_wait_for_approval

    rec = _approval_record("approved", result={"tool_result": {"content": []}})
    data = _inline_wait_data([rec])
    token = _make_physical_token("confirm")
    token.id = "tid"

    content, outcome, _r = await _tool_wait_for_approval(
        {"approval_id": "appr-1"}, token, hass, data)

    body = json.loads(content["content"][0]["text"])
    assert outcome == "allowed"
    assert body["approval_id"] == "appr-1" and "approvals" not in body
