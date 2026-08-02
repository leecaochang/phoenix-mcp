"""Tests for Phoenix MCP admin views."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminAuditView,
    PhoenixAdminEntityTreeView,
    PhoenixAdminPermissionDomainView,
    PhoenixAdminSettingsView,
    PhoenixAdminTokenAuditView,
    PhoenixAdminTokenStatsView,
    PhoenixAdminTokensView,
    PhoenixAdminTokenView,
    PhoenixAdminVoiceAgentPipelineView,
    PhoenixAdminAiTaskPreferredView,
    PhoenixAdminWipeView,
    ALL_ADMIN_VIEWS,
    _wipe_flag,
)
from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import (
    AGENTCLI_MAX_ITERATIONS_MAX,
    AGENTCLI_MAX_ITERATIONS_MIN,
    AGENTCLI_SCROLLBACK_MAX,
    AGENTCLI_SCROLLBACK_MIN,
    DOMAIN,
    MAX_CONFIRM_INLINE_WAIT_SECONDS,
    MIN_CONFIRM_INLINE_WAIT_SECONDS,
    TOKEN_PREFIX,
)
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.token_store import GlobalSettings, TokenRecord, TokenStore


def _make_active_token(name: str = "test-token", pass_through: bool = False) -> TokenRecord:
    from homeassistant.util.dt import utcnow

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return TokenRecord(
        id=str(uuid.uuid4()),
        name=name,
        token_hash=token_hash,
        created_at=utcnow(),
        created_by="user1",
        pass_through=pass_through,
    )


def _make_data(tokens: list[TokenRecord] | None = None) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.list_tokens.return_value = tokens or []
    store.list_archived.return_value = []
    store.get_settings.return_value = GlobalSettings()
    store.get_entity_hints.return_value = {}
    store.get_pending_approvals.return_value = []
    store.async_lock = asyncio.Lock()

    rate_limiter = MagicMock(spec=RateLimiter)
    audit = MagicMock(spec=AuditLog)
    audit.query.return_value = []
    audit.count.return_value = 0

    return PhoenixData(
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
    )


def _make_admin_request(is_admin: bool = True, body: bytes = b"", authenticated: bool = True) -> MagicMock:
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    user = MagicMock()
    user.is_admin = is_admin
    user.id = "admin-user-id"

    def _get(k, default=None):
        if k == KEY_HASS_USER:
            return user
        if k == KEY_AUTHENTICATED:
            return authenticated
        return default

    request = MagicMock()
    request.query = {}
    request.read = AsyncMock(return_value=body)
    request.content_length = len(body)
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=body)
    request.__getitem__ = MagicMock(side_effect=lambda k: user if k == KEY_HASS_USER else None)
    request.get = MagicMock(side_effect=_get)
    return request


def _make_hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))
    return hass


@pytest.mark.asyncio
async def test_unauthenticated_rejected_from_tokens_list():
    token = _make_active_token()
    data = _make_data([token])
    hass = _make_hass(data)

    view = PhoenixAdminTokensView()
    view.hass = hass

    request = _make_admin_request(authenticated=False)
    resp = await view.get(request)

    assert resp.status == 401
    body = json.loads(resp.text)
    assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_non_admin_rejected_from_tokens_list():
    token = _make_active_token()
    data = _make_data([token])
    hass = _make_hass(data)

    view = PhoenixAdminTokensView()
    view.hass = hass

    request = _make_admin_request(is_admin=False)
    resp = await view.get(request)

    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["error"] == "forbidden"


@pytest.mark.asyncio
async def test_admin_can_list_tokens():
    token = _make_active_token()
    data = _make_data([token])
    hass = _make_hass(data)

    view = PhoenixAdminTokensView()
    view.hass = hass

    request = _make_admin_request()
    resp = await view.get(request)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert len(body) == 1
    assert body[0]["id"] == token.id
    assert "token_hash" not in body[0]
    assert "token" not in body[0]


@pytest.mark.asyncio
async def test_create_token_returns_raw_token_once():
    data = _make_data()
    hass = _make_hass(data)

    token = _make_active_token()
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock(return_value=(token, "phx_rawtoken123"))

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "my-token"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.post(request)

    assert resp.status == 201
    result = json.loads(resp.text)
    assert result["token"] == "phx_rawtoken123"
    assert result["id"] == token.id


@pytest.mark.asyncio
async def test_create_token_naive_expires_at_normalized_to_aware():
    """A timezone-less expires_at must never be persisted naive.

    A naive datetime poisons the stored record: every later comparison against
    aware UTC raises TypeError, and the startup expiry sweep runs during
    async_setup_entry, so one bad record aborts the whole integration.
    """
    data = _make_data()
    hass = _make_hass(data)

    token = _make_active_token()
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock(return_value=(token, "phx_rawtoken123"))

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "my-token", "expires_at": "2030-01-01T12:00:00"}).encode()
    resp = await view.post(_make_admin_request(body=body))

    assert resp.status == 201
    stored = data.store.async_create_token.call_args.kwargs["expires_at"]
    assert stored is not None
    assert stored.tzinfo is not None

    # An offset-aware value is normalized to UTC, preserving the instant.
    data.store.async_create_token.reset_mock()
    body = json.dumps({"name": "my-token", "expires_at": "2030-01-01T12:00:00+02:00"}).encode()
    resp = await view.post(_make_admin_request(body=body))
    assert resp.status == 201
    stored = data.store.async_create_token.call_args.kwargs["expires_at"]
    assert stored.isoformat() == "2030-01-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_create_token_non_string_expires_at_rejected():
    data = _make_data()
    hass = _make_hass(data)
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock()

    view = PhoenixAdminTokensView()
    view.hass = hass

    for garbage in (12345, ["2030-01-01T12:00:00Z"], {"at": "2030"}, None):
        body = json.dumps({"name": "my-token", "expires_at": garbage}).encode()
        resp = await view.post(_make_admin_request(body=body))
        assert resp.status == 400
        assert json.loads(resp.text)["error"] == "invalid_request"
    data.store.async_create_token.assert_not_called()


@pytest.mark.asyncio
async def test_create_token_invalid_name_rejected():
    data = _make_data()
    hass = _make_hass(data)

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "bad name!"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.post(request)

    assert resp.status == 400
    body_parsed = json.loads(resp.text)
    assert body_parsed["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_create_token_slug_collision_rejected():
    data = _make_data()
    hass = _make_hass(data)
    data.store.name_slug_exists.return_value = True

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "my-token"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.post(request)

    assert resp.status == 409


@pytest.mark.asyncio
async def test_create_pass_through_requires_confirmation():
    data = _make_data()
    hass = _make_hass(data)
    data.store.name_slug_exists.return_value = False

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "pt-token", "pass_through": True}).encode()
    request = _make_admin_request(body=body)
    resp = await view.post(request)

    assert resp.status == 400
    body_parsed = json.loads(resp.text)
    assert "confirm_pass_through" in body_parsed["message"]


@pytest.mark.asyncio
async def test_create_pass_through_with_confirmation_succeeds():
    data = _make_data()
    hass = _make_hass(data)

    token = _make_active_token(pass_through=True)
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock(return_value=(token, "phx_rawtoken"))

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "pt-token", "pass_through": True, "confirm_pass_through": True}).encode()
    request = _make_admin_request(body=body)
    resp = await view.post(request)

    assert resp.status == 201


@pytest.mark.asyncio
async def test_create_non_boolean_pass_through_rejected():
    # bool("false") is True: a string must not enable pass-through. Require a
    # real JSON boolean, and require confirm_pass_through to be exactly true.
    data = _make_data()
    hass = _make_hass(data)
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock()

    view = PhoenixAdminTokensView()
    view.hass = hass

    for body_dict in (
        {"name": "pt", "pass_through": "false"},
        {"name": "pt", "pass_through": "true", "confirm_pass_through": True},
        {"name": "pt", "pass_through": 1, "confirm_pass_through": True},
        {"name": "pt", "pass_through": True, "confirm_pass_through": "true"},
        {"name": "pt", "pass_through": True, "use_assist_exposure": "yes"},
    ):
        resp = await view.post(_make_admin_request(body=json.dumps(body_dict).encode()))
        assert resp.status == 400, body_dict
    data.store.async_create_token.assert_not_called()


@pytest.mark.asyncio
async def test_patch_non_boolean_pass_through_rejected():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock()
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    for body_dict in (
        {"pass_through": "false"},
        {"pass_through": "true", "confirm_pass_through": True},
        {"use_assist_exposure": "true"},
    ):
        request = _make_admin_request(body=json.dumps(body_dict).encode())
        resp = await view.patch(request, token_id=token.id)
        assert resp.status == 400, body_dict
    data.store.async_patch_token.assert_not_called()


@pytest.mark.asyncio
async def test_patch_token_rename_succeeds():
    token = _make_active_token(name="old-name")
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.name_slug_exists.return_value = False
    renamed = _make_active_token(name="new-name")
    renamed.id = token.id
    data.store.async_patch_token = AsyncMock(return_value=renamed)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"name": "new-name"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 200
    data.store.async_patch_token.assert_awaited_once()
    assert data.store.async_patch_token.call_args.kwargs.get("name") == "new-name"


@pytest.mark.asyncio
async def test_patch_token_rename_clash_rejected():
    token = _make_active_token(name="old-name")
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.name_slug_exists.return_value = True  # another token already uses it
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"name": "taken-name"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_patch_token_rename_invalid_name_rejected():
    token = _make_active_token(name="old-name")
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.name_slug_exists.return_value = False
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"name": "no"}).encode()  # too short for TOKEN_NAME_REGEX
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_patch_token_rejects_expires_at_field():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"expires_at": "2030-01-01T00:00:00Z"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_patch_token_pass_through_requires_confirm():
    token = _make_active_token(pass_through=False)
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"pass_through": True}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 400
    body_parsed = json.loads(resp.text)
    assert "confirm_pass_through" in body_parsed["message"]


@pytest.mark.asyncio
async def test_patch_token_pass_through_already_enabled_no_confirm_needed():
    token = _make_active_token(pass_through=True)
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"pass_through": True}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id)

    assert resp.status == 200
    # Assert the actual patch payload, not just the status: re-affirming pass_through
    # on an already-enabled token must patch exactly {"pass_through": True} and must
    # NOT smuggle in a confirm_pass_through escape hatch or any other field.
    data.store.async_patch_token.assert_awaited_once()
    assert data.store.async_patch_token.await_args.args[0] == token.id
    assert data.store.async_patch_token.await_args.kwargs == {"pass_through": True}


@pytest.mark.asyncio
async def test_patch_token_announce_all_tools_accepted():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"announce_all_tools": True}).encode()
    resp = await view.patch(_make_admin_request(body=body), token_id=token.id)

    assert resp.status == 200
    assert data.store.async_patch_token.call_args.kwargs["announce_all_tools"] is True


@pytest.mark.asyncio
async def test_patch_token_announce_all_tools_rejects_non_bool():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"announce_all_tools": "yes"}).encode()
    resp = await view.patch(_make_admin_request(body=body), token_id=token.id)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_patch_token_inline_wait_accepted():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    # In-range durations and 0 (disable, the unattended-agent mode) all pass.
    for good in (30, 60, 180, 0):
        body = json.dumps({"confirm_inline_wait_seconds": good}).encode()
        resp = await view.patch(_make_admin_request(body=body), token_id=token.id)
        assert resp.status == 200, good
        assert data.store.async_patch_token.call_args.kwargs["confirm_inline_wait_seconds"] == good


@pytest.mark.asyncio
async def test_patch_token_inline_wait_rejects_out_of_range():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    # Below the floor (but nonzero), above the ceiling, non-int, and bool all fail.
    for bad in (29, 181, -1, "60", True):
        body = json.dumps({"confirm_inline_wait_seconds": bad}).encode()
        resp = await view.patch(_make_admin_request(body=body), token_id=token.id)
        assert resp.status == 400, bad


@pytest.mark.asyncio
async def test_patch_uses_async_lock():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)
    hass = _make_hass(data)

    lock_acquired = []
    original_acquire = data.store.async_lock.acquire

    async def tracking_acquire():
        lock_acquired.append(True)
        return await original_acquire()

    data.store.async_lock.acquire = tracking_acquire

    view = PhoenixAdminTokenView()
    view.hass = hass

    body = json.dumps({"cap_restart": "allow"}).encode()
    request = _make_admin_request(body=body)
    await view.patch(request, token_id=token.id)

    assert len(lock_acquired) == 1


@pytest.mark.asyncio
async def test_delete_token_fires_revoked_event():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    request = _make_admin_request()
    resp = await view.delete(request, token_id=token.id)

    assert resp.status == 204
    hass.bus.async_fire.assert_called_once()
    event_type, payload = hass.bus.async_fire.call_args[0]
    assert event_type == "phoenix_mcp_token_revoked"
    assert payload["token_id"] == token.id
    assert payload["revoked_by"] == "admin-user-id"


@pytest.mark.asyncio
async def test_delete_token_cancels_pending_approvals():
    """Revoking a token cancels its queued approvals, dismisses their
    notifications, and fires phoenix_mcp_approval_resolved; other tokens' approvals
    and mid-execution approvals are untouched."""
    from homeassistant.util.dt import utcnow as _utcnow

    token = _make_active_token()
    data = _make_data([token])
    now_iso = _utcnow().isoformat()

    def _entry(appr_id: str, token_id: str) -> dict:
        return {
            "id": appr_id, "token_id": token_id, "token_name": "t",
            "tool_name": "restart_ha", "cap_name": "cap_restart",
            "args": {}, "diff": {}, "status": "pending",
            "created_at": now_iso, "expires_at": now_iso, "request_id": "r",
        }

    pending = [
        _entry("appr_mine", token.id),
        _entry("appr_other", "other-token"),
        _entry("appr_running", token.id),
    ]
    data.store.get_pending_approvals.return_value = pending
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    data.approvals_in_progress.add("appr_running")
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    with patch(
        "custom_components.phoenix_mcp.approvals.dismiss_approval_notification"
    ) as dismiss:
        resp = await view.delete(_make_admin_request(), token_id=token.id)

    assert resp.status == 204
    by_id = {e["id"]: e for e in pending}
    assert by_id["appr_mine"]["status"] == "cancelled"
    assert by_id["appr_mine"]["rejected_reason"] == "token_revoked"
    assert by_id["appr_other"]["status"] == "pending"
    assert by_id["appr_running"]["status"] == "pending"
    dismiss.assert_called_once_with(hass, "appr_mine")
    resolved = [
        c.args[1] for c in hass.bus.async_fire.call_args_list
        if c.args[0] == "phoenix_mcp_approval_resolved"
    ]
    assert [p["approval_id"] for p in resolved] == ["appr_mine"]
    assert resolved[0]["rejected_reason"] == "token_revoked"


@pytest.mark.asyncio
async def test_delete_token_destroys_rate_limiter_state():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    request = _make_admin_request()
    await view.delete(request, token_id=token.id)

    data.rate_limiter.destroy.assert_called_once_with(token.id)


@pytest.mark.asyncio
async def test_delete_token_cleans_token_counters():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    data.token_counters[token.id] = {"request_count": 10, "denied_count": 1, "rate_limit_hits": 0}
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    request = _make_admin_request()
    await view.delete(request, token_id=token.id)

    assert token.id not in data.token_counters


@pytest.mark.asyncio
async def test_delete_nonexistent_token_returns_404():
    data = _make_data()
    data.store.get_token_by_id = MagicMock(return_value=None)
    hass = _make_hass(data)

    view = PhoenixAdminTokenView()
    view.hass = hass

    request = _make_admin_request()
    resp = await view.delete(request, token_id="nonexistent-id")

    assert resp.status == 404


@pytest.mark.asyncio
async def test_permission_domain_patch_valid_state():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_permission_node = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminPermissionDomainView()
    view.hass = hass

    body = json.dumps({"state": "GREEN"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id, node_id="light")

    assert resp.status == 200
    data.store.async_patch_permission_node.assert_called_once_with(
        token.id, "domains", "light", "GREEN", None
    )


@pytest.mark.asyncio
async def test_permission_patch_invalid_state_rejected():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminPermissionDomainView()
    view.hass = hass

    body = json.dumps({"state": "PURPLE"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id, node_id="light")

    assert resp.status == 400


@pytest.mark.asyncio
async def test_permission_patch_grey_removes_node():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_permission_node = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminPermissionDomainView()
    view.hass = hass

    body = json.dumps({"state": "GREY"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request, token_id=token.id, node_id="light")

    assert resp.status == 200
    data.store.async_patch_permission_node.assert_called_once_with(
        token.id, "domains", "light", "GREY", None
    )


@pytest.mark.asyncio
async def test_permission_patch_with_hint():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_permission_node = AsyncMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminPermissionDomainView()
    view.hass = hass

    body = json.dumps({"state": "YELLOW", "hint": "Living room lights only"}).encode()
    request = _make_admin_request(body=body)
    await view.patch(request, token_id=token.id, node_id="light")

    data.store.async_patch_permission_node.assert_called_once_with(
        token.id, "domains", "light", "YELLOW", "Living room lights only"
    )


@pytest.mark.asyncio
async def test_settings_patch_uses_async_lock():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings())
    hass = _make_hass(data)

    lock_acquired = []
    original_acquire = data.store.async_lock.acquire

    async def tracking_acquire():
        lock_acquired.append(True)
        return await original_acquire()

    data.store.async_lock.acquire = tracking_acquire

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"notify_on_rate_limit": True}).encode()
    request = _make_admin_request(body=body)
    await view.patch(request)

    assert len(lock_acquired) == 1


@pytest.mark.asyncio
async def test_settings_patch_rejects_unknown_fields_silently():
    data = _make_data()
    settings = GlobalSettings()
    data.store.async_patch_settings = AsyncMock(return_value=settings)
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"notify_on_rate_limit": True, "unknown_field": "ignored"}).encode()
    request = _make_admin_request(body=body)
    resp = await view.patch(request)

    assert resp.status == 200
    call_kwargs = data.store.async_patch_settings.call_args.kwargs
    assert "unknown_field" not in call_kwargs
    assert "notify_on_rate_limit" in call_kwargs


@pytest.mark.asyncio
async def test_settings_patch_agentcli_scrollback_persists_and_clamps():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings())
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    # A normal value reaches async_patch_settings (was silently dropped before).
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_scrollback_lines": 250}).encode()))
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs["agentcli_scrollback_lines"] == 250

    # Over-range is clamped to the maximum, not rejected.
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_scrollback_lines": 999999}).encode()))
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs["agentcli_scrollback_lines"] == 5000

    # A non-integer is rejected.
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_scrollback_lines": "lots"}).encode()))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_settings_patch_agentcli_max_iterations_persists_and_clamps():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings())
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_max_iterations": 40}).encode()))
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs["agentcli_max_iterations"] == 40

    # Over-range clamps to the max (100), under-range to the min (3).
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_max_iterations": 9999}).encode()))
    assert data.store.async_patch_settings.call_args.kwargs["agentcli_max_iterations"] == 100
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_max_iterations": 1}).encode()))
    assert data.store.async_patch_settings.call_args.kwargs["agentcli_max_iterations"] == 3

    # A non-integer is rejected.
    resp = await view.patch(_make_admin_request(body=json.dumps({"agentcli_max_iterations": "many"}).encode()))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_settings_patch_mesa_mode_valid_updates_enforcer():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings(mesa_mode="enforced"))
    data.mesa = MagicMock()
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"mesa_mode": "enforced"}).encode()
    resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 200
    assert "mesa_mode" in data.store.async_patch_settings.call_args.kwargs
    data.mesa.set_mode.assert_called_once_with("enforced")


@pytest.mark.asyncio
async def test_settings_patch_mesa_mode_invalid_rejected():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings())
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"mesa_mode": "yolo"}).encode()
    resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 400
    data.store.async_patch_settings.assert_not_called()


@pytest.mark.asyncio
async def test_settings_patch_mesa_inject_enables_and_syncs():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(
        return_value=GlobalSettings(mesa_inject_enabled=True)
    )
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"mesa_inject_enabled": True}).encode()
    with patch("custom_components.phoenix_mcp.panel.async_sync_mesa_inject", new=AsyncMock()) as sync:
        resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs["mesa_inject_enabled"] is True
    sync.assert_awaited_once_with(hass)


@pytest.mark.asyncio
async def test_settings_patch_flush_interval_reschedules_timer():
    """Changing audit_flush_interval re-registers the periodic flush immediately,
    instead of taking effect only after the previously pending interval elapses."""
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(
        return_value=GlobalSettings(audit_flush_interval=5)
    )
    data.reschedule_audit_flush = MagicMock()
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"audit_flush_interval": 5}).encode()
    resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 200
    data.reschedule_audit_flush.assert_called_once_with()


@pytest.mark.asyncio
async def test_settings_patch_mesa_inject_non_bool_rejected():
    data = _make_data()
    data.store.async_patch_settings = AsyncMock(return_value=GlobalSettings())
    hass = _make_hass(data)

    view = PhoenixAdminSettingsView()
    view.hass = hass

    body = json.dumps({"mesa_inject_enabled": "yes"}).encode()
    resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 400
    data.store.async_patch_settings.assert_not_called()


@pytest.mark.asyncio
async def test_token_stats_returns_zero_counters_for_new_token():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenStatsView()
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminTokenStatsView as V
    view = V()
    view.hass = hass

    request = _make_admin_request()
    resp = await view.get(request, token_id=token.id)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["request_count"] == 0
    assert body["denied_count"] == 0
    assert body["rate_limit_hits"] == 0


@pytest.mark.asyncio
async def test_token_stats_reflects_live_counters():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.token_counters[token.id] = {"request_count": 42, "denied_count": 5, "rate_limit_hits": 2}
    hass = _make_hass(data)

    from custom_components.phoenix_mcp.admin_view import PhoenixAdminTokenStatsView
    view = PhoenixAdminTokenStatsView()
    view.hass = hass

    request = _make_admin_request()
    resp = await view.get(request, token_id=token.id)

    body = json.loads(resp.text)
    assert body["request_count"] == 42
    assert body["denied_count"] == 5
    assert body["rate_limit_hits"] == 2


def _connection_view():
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminTokenConnectionView
    return PhoenixAdminTokenConnectionView()


@pytest.mark.asyncio
async def test_token_connection_no_session_zero_counters():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)
    view = _connection_view()
    view.hass = hass

    resp = await view.get(_make_admin_request(), token_id=token.id)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {"last_used_at": None, "request_count": 0}


@pytest.mark.asyncio
async def test_token_connection_reports_request_count():
    # Streamable HTTP clients are stateless; request_count is the "connected" signal.
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.token_counters[token.id] = {"request_count": 3, "denied_count": 0, "rate_limit_hits": 0}
    hass = _make_hass(data)
    view = _connection_view()
    view.hass = hass

    resp = await view.get(_make_admin_request(), token_id=token.id)
    body = json.loads(resp.text)
    assert body["request_count"] == 3


@pytest.mark.asyncio
async def test_token_connection_reports_last_used_iso():
    from homeassistant.util.dt import utcnow
    token = _make_active_token()
    token.last_used_at = utcnow()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)
    view = _connection_view()
    view.hass = hass

    resp = await view.get(_make_admin_request(), token_id=token.id)
    body = json.loads(resp.text)
    assert body["last_used_at"] == token.last_used_at.isoformat()


@pytest.mark.asyncio
async def test_token_connection_404_for_unknown_token():
    data = _make_data([])
    data.store.get_token_by_id = MagicMock(return_value=None)
    hass = _make_hass(data)
    view = _connection_view()
    view.hass = hass

    resp = await view.get(_make_admin_request(), token_id="nope")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_token_connection_requires_admin():
    token = _make_active_token()
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)
    view = _connection_view()
    view.hass = hass

    resp = await view.get(_make_admin_request(is_admin=False), token_id=token.id)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_audit_log_query_paginates():
    data = _make_data()
    token = _make_active_token()
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminTokenAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"limit": "50", "offset": "10"}
    await view.get(request, token_id=token.id)

    data.audit.query.assert_called_once_with(
        token_id=token.id,
        outcome=None,
        client_ip=None,
        limit=50,
        offset=10,
    )


@pytest.mark.asyncio
async def test_global_audit_query_all_tokens():
    data = _make_data()
    hass = _make_hass(data)

    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {}
    await view.get(request)

    data.audit.count.assert_called_once_with(
        token_id=None,
        outcome=None,
        client_ip=None,
        method=None,
        resource=None,
        since=None,
    )
    data.audit.query.assert_called_once_with(
        token_id=None,
        outcome=None,
        client_ip=None,
        method=None,
        resource=None,
        since=None,
        limit=100,
        offset=0,
    )


@pytest.mark.asyncio
async def test_global_audit_response_has_entries_and_total():
    data = _make_data()
    data.audit.count.return_value = 42
    hass = _make_hass(data)

    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {}
    resp = await view.get(request)

    body = json.loads(resp.text)
    assert body["entries"] == []
    assert body["total"] == 42


@pytest.mark.asyncio
async def test_global_audit_forwards_method_resource_since():
    data = _make_data()
    hass = _make_hass(data)

    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"method": "create_automation", "resource": "appr_", "since": "2026-01-01T00:00:00+00:00"}
    await view.get(request)

    data.audit.query.assert_called_once_with(
        token_id=None,
        outcome=None,
        client_ip=None,
        method="create_automation",
        resource="appr_",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        limit=100,
        offset=0,
    )


@pytest.mark.asyncio
async def test_global_audit_invalid_since_returns_400():
    data = _make_data()
    hass = _make_hass(data)

    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"since": "not-a-timestamp"}
    resp = await view.get(request)

    assert resp.status == 400
    data.audit.query.assert_not_called()


@pytest.mark.asyncio
async def test_global_audit_naive_since_normalized_to_aware():
    # Audit entry timestamps are aware UTC; forwarding a naive since would
    # raise TypeError inside the filter and 500 the query.
    data = _make_data()
    hass = _make_hass(data)

    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"since": "2026-01-01T00:00:00"}
    resp = await view.get(request)

    assert resp.status == 200
    forwarded = data.audit.query.call_args.kwargs["since"]
    assert forwarded is not None
    assert forwarded.tzinfo is not None


@pytest.mark.asyncio
async def test_entity_tree_uses_cache():
    data = _make_data()
    cached_tree = {"light": {"devices": {}, "deviceless_entities": [], "entity_details": {}}}
    data.entity_tree_cache = cached_tree
    data.entity_tree_cache_valid = True
    hass = _make_hass(data)
    hass.states = MagicMock()

    view = PhoenixAdminEntityTreeView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {}

    with patch("custom_components.phoenix_mcp.admin_view._build_entity_tree") as mock_build:
        resp = await view.get(request)

    mock_build.assert_not_called()
    assert resp.status == 200
    body = json.loads(resp.text)
    assert "light" in body


@pytest.mark.asyncio
async def test_entity_tree_rebuilds_when_invalid():
    data = _make_data()
    data.entity_tree_cache = None
    data.entity_tree_cache_valid = False
    fresh_tree = {"switch": {}}
    hass = _make_hass(data)

    view = PhoenixAdminEntityTreeView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {}

    with patch("custom_components.phoenix_mcp.admin_view._build_entity_tree", new=MagicMock(return_value=fresh_tree)):
        resp = await view.get(request)

    assert resp.status == 200
    assert data.entity_tree_cache_valid is True


@pytest.mark.asyncio
async def test_entity_tree_force_reload_bypasses_cache():
    data = _make_data()
    data.entity_tree_cache = {"light": {}}
    data.entity_tree_cache_valid = True
    fresh = {"sensor": {}}
    hass = _make_hass(data)

    view = PhoenixAdminEntityTreeView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"force_reload": "1"}

    with patch("custom_components.phoenix_mcp.admin_view._build_entity_tree", new=MagicMock(return_value=fresh)):
        resp = await view.get(request)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert "sensor" in body


def test_all_admin_views_exported():
    assert len(ALL_ADMIN_VIEWS) == 55


def test_archived_views_before_token_view():
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminArchivedTokensView,
        PhoenixAdminArchivedTokenView,
        PhoenixAdminTokenView,
    )
    archived_idx = ALL_ADMIN_VIEWS.index(PhoenixAdminArchivedTokensView)
    archived_single_idx = ALL_ADMIN_VIEWS.index(PhoenixAdminArchivedTokenView)
    token_idx = ALL_ADMIN_VIEWS.index(PhoenixAdminTokenView)
    assert archived_idx < token_idx
    assert archived_single_idx < token_idx


# --- global entity hints ---

def _entity_hints_view():
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminEntityHintsView
    return PhoenixAdminEntityHintsView()


def _entity_hint_view():
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminEntityHintView
    return PhoenixAdminEntityHintView()


@pytest.mark.asyncio
async def test_set_global_entity_hint():
    data = _make_data()
    data.store.async_set_entity_hint = AsyncMock()
    data.store.get_entity_hints.return_value = {"light.x": "note"}
    view = _entity_hint_view()
    view.hass = _make_hass(data)
    body = json.dumps({"hint": "note"}).encode()
    resp = await view.put(_make_admin_request(body=body), entity_id="light.x")
    assert resp.status == 200
    data.store.async_set_entity_hint.assert_awaited_once_with("light.x", "note")
    assert json.loads(resp.text)["entity_hints"] == {"light.x": "note"}


@pytest.mark.asyncio
async def test_clear_global_entity_hint():
    data = _make_data()
    data.store.async_set_entity_hint = AsyncMock()
    view = _entity_hint_view()
    view.hass = _make_hass(data)
    body = json.dumps({"hint": "   "}).encode()
    resp = await view.put(_make_admin_request(body=body), entity_id="light.x")
    assert resp.status == 200
    data.store.async_set_entity_hint.assert_awaited_once_with("light.x", None)


@pytest.mark.asyncio
async def test_set_global_entity_hint_invalid_entity_id():
    data = _make_data()
    view = _entity_hint_view()
    view.hass = _make_hass(data)
    body = json.dumps({"hint": "x"}).encode()
    resp = await view.put(_make_admin_request(body=body), entity_id="not-an-entity")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_set_global_entity_hint_too_long():
    data = _make_data()
    view = _entity_hint_view()
    view.hass = _make_hass(data)
    body = json.dumps({"hint": "z" * 201}).encode()
    resp = await view.put(_make_admin_request(body=body), entity_id="light.x")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_get_global_entity_hints():
    data = _make_data()
    data.store.get_entity_hints.return_value = {"light.x": "note"}
    view = _entity_hints_view()
    view.hass = _make_hass(data)
    resp = await view.get(_make_admin_request())
    assert resp.status == 200
    assert json.loads(resp.text)["entity_hints"] == {"light.x": "note"}


_ADMIN_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _admin_view_handlers() -> list[tuple[type, str]]:
    """(view_class, method_name) for every HTTP handler across ALL_ADMIN_VIEWS.

    Every one of these must reject unauthenticated and non-admin requests via
    @require_admin, regardless of the path parameters its route declares: the
    decorator checks auth before the wrapped method (and its path kwargs) ever run.
    """
    handlers = []
    for view_cls in ALL_ADMIN_VIEWS:
        for name in _ADMIN_HTTP_METHODS:
            fn = getattr(view_cls, name, None)
            if fn is not None and inspect.iscoroutinefunction(fn):
                handlers.append((view_cls, name))
    return handlers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "view_cls, method_name",
    _admin_view_handlers(),
    ids=lambda v: v.__name__ if isinstance(v, type) else v,
)
async def test_admin_handler_rejects_unauthenticated_and_non_admin(view_cls, method_name):
    """Regression guard: every admin HTTP handler must be @require_admin.

    Calls each handler with no path kwargs; @require_admin returns before the
    wrapped method (and any path params it needs) ever runs, so this works
    uniformly across the whole admin surface without per-route fixtures. A
    handler missing the decorator would instead hit the real method body and
    raise (e.g. a missing positional path argument) rather than return 401/403,
    which fails this test just as loudly.
    """
    data = _make_data()
    hass = _make_hass(data)
    view = view_cls()
    view.hass = hass
    handler = getattr(view, method_name)

    resp = await handler(_make_admin_request(authenticated=False))
    assert resp.status == 401, f"{view_cls.__name__}.{method_name} did not reject an unauthenticated request"
    assert json.loads(resp.text)["error"] == "unauthorized"

    resp = await handler(_make_admin_request(is_admin=False))
    assert resp.status == 403, f"{view_cls.__name__}.{method_name} did not reject a non-admin request"
    assert json.loads(resp.text)["error"] == "forbidden"


# --------------------------------------------------------------------------- #
# Selective wipe (Data Management)
# --------------------------------------------------------------------------- #


def _make_wipe_data(tokens: list[TokenRecord] | None = None) -> PhoenixData:
    """PhoenixData with the async wipe collaborators stubbed so the core path runs."""
    data = _make_data(tokens)
    data.store.async_wipe = AsyncMock()
    data.audit.async_wipe = AsyncMock()
    data.versions = MagicMock()
    data.versions.async_wipe = AsyncMock()
    data.rate_limiter.destroy_all = MagicMock()
    return data


def _wipe_body(**flags) -> bytes:
    return json.dumps({"confirm": "WIPE", **flags}).encode()


def test_wipe_flag_coercion():
    # Real bools honored; anything else falls back to the default.
    assert _wipe_flag(True, False) is True
    assert _wipe_flag(False, True) is False
    assert _wipe_flag(None, True) is True          # key absent
    assert _wipe_flag("false", True) is True       # garbage -> default
    assert _wipe_flag(1, False) is False           # non-bool -> default


@pytest.mark.asyncio
async def test_wipe_requires_confirm_token():
    data = _make_wipe_data()
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    request = _make_admin_request(body=json.dumps({"confirm": "nope"}).encode())
    resp = await view.delete(request)

    assert resp.status == 400
    data.store.async_wipe.assert_not_called()


@pytest.mark.asyncio
async def test_wipe_defaults_hit_core_and_providers_but_not_mesa():
    data = _make_wipe_data()
    data.mesa = MagicMock()
    data.mesa.lock = asyncio.Lock()
    data.mesa.async_wipe = AsyncMock()
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    with patch(
        "custom_components.phoenix_mcp.agentcli.async_wipe_agentcli_secrets",
        new=AsyncMock(),
    ) as wipe_secrets:
        resp = await view.delete(_make_admin_request(body=_wipe_body()))

    assert resp.status == 204
    data.store.async_wipe.assert_awaited_once()
    wipe_secrets.assert_awaited_once()
    data.mesa.async_wipe.assert_not_called()


@pytest.mark.asyncio
async def test_wipe_core_tears_down_pending_approvals():
    data = _make_wipe_data()
    # One pending approval (must be dismissed) + one already-terminal (must be
    # ignored). Only "pending" gets a notification dismiss + resolved event.
    data.store.get_pending_approvals = MagicMock(return_value=[
        {"id": "ap-pending", "token_id": "gone", "tool_name": "call_service",
         "status": "pending", "created_at": "2026-07-10T00:00:00+00:00",
         "expires_at": "2026-07-10T01:00:00+00:00"},
        {"id": "ap-old", "token_id": "gone", "tool_name": "call_service",
         "status": "approved", "created_at": "2026-07-10T00:00:00+00:00",
         "expires_at": "2026-07-10T01:00:00+00:00"},
    ])
    data.approvals_in_progress = {"ap-inflight"}
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.approvals.dismiss_approval_notification") as dismiss, \
         patch("custom_components.phoenix_mcp.approvals.fire_approval_resolved_event") as fire, \
         patch("custom_components.phoenix_mcp.agentcli.async_wipe_agentcli_secrets", new=AsyncMock()):
        resp = await view.delete(_make_admin_request(body=_wipe_body()))

    assert resp.status == 204
    dismiss.assert_called_once_with(hass, "ap-pending")
    assert fire.call_count == 1
    assert fire.call_args[0][1].id == "ap-pending"
    assert fire.call_args[0][1].status == "cancelled"
    # The in-progress guard set is cleared by the wipe.
    assert data.approvals_in_progress == set()


@pytest.mark.asyncio
async def test_wipe_providers_only_leaves_approvals_untouched():
    data = _make_wipe_data()
    data.store.get_pending_approvals = MagicMock(return_value=[
        {"id": "ap1", "token_id": "gone", "tool_name": "call_service",
         "status": "pending", "created_at": "2026-07-10T00:00:00+00:00",
         "expires_at": "2026-07-10T01:00:00+00:00"},
    ])
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.approvals.dismiss_approval_notification") as dismiss, \
         patch("custom_components.phoenix_mcp.agentcli.async_wipe_agentcli_secrets", new=AsyncMock()):
        resp = await view.delete(
            _make_admin_request(body=_wipe_body(wipe_core=False, wipe_providers=True))
        )

    assert resp.status == 204
    # wipe_core off: the approval teardown never runs.
    dismiss.assert_not_called()


@pytest.mark.asyncio
async def test_wipe_providers_only_skips_core_store():
    data = _make_wipe_data()
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    with patch(
        "custom_components.phoenix_mcp.agentcli.async_wipe_agentcli_secrets",
        new=AsyncMock(),
    ) as wipe_secrets:
        resp = await view.delete(
            _make_admin_request(body=_wipe_body(wipe_core=False, wipe_providers=True))
        )

    assert resp.status == 204
    data.store.async_wipe.assert_not_called()
    wipe_secrets.assert_awaited_once()


@pytest.mark.asyncio
async def test_wipe_mesa_flag_wipes_mesa_and_can_spare_others():
    data = _make_wipe_data()
    data.mesa = MagicMock()
    data.mesa.lock = asyncio.Lock()
    data.mesa.async_wipe = AsyncMock()
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    with patch(
        "custom_components.phoenix_mcp.agentcli.async_wipe_agentcli_secrets",
        new=AsyncMock(),
    ) as wipe_secrets:
        resp = await view.delete(
            _make_admin_request(
                body=_wipe_body(wipe_core=False, wipe_providers=False, wipe_mesa=True)
            )
        )

    assert resp.status == 204
    data.mesa.async_wipe.assert_awaited_once()
    data.store.async_wipe.assert_not_called()
    wipe_secrets.assert_not_called()


@pytest.mark.asyncio
async def test_wipe_mesa_without_runtime_clears_store_directly():
    data = _make_wipe_data()
    data.mesa = None  # runtime unavailable this session
    hass = _make_hass(data)
    view = PhoenixAdminWipeView()
    view.hass = hass

    saved = {}

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        async def async_save(self, payload):
            saved.update(payload)

    with patch("homeassistant.helpers.storage.Store", _FakeStore):
        resp = await view.delete(
            _make_admin_request(body=_wipe_body(wipe_core=False, wipe_providers=False, wipe_mesa=True))
        )

    assert resp.status == 204
    assert saved == {"profiles": {}, "dismissed_suggestions": []}


# ---------------------------------------------------------------------------
# Assist bridge: binding via settings PATCH + teardown on revoke
# ---------------------------------------------------------------------------

def _settings_data(tokens):
    """A _make_data whose settings are a mutable GlobalSettings holder."""
    data = _make_data(tokens)
    holder = {"s": GlobalSettings()}
    data.store.get_settings.side_effect = lambda: holder["s"]

    async def _patch(**kw):
        holder["s"] = GlobalSettings.from_dict({**holder["s"].to_dict(), **kw})
        return holder["s"]

    data.store.async_patch_settings = AsyncMock(side_effect=_patch)
    data.store.async_seed_default_presets = AsyncMock(return_value=0)
    data.store.get_token_by_id = MagicMock(
        side_effect=lambda tid: next((t for t in tokens if t.id == tid), None)
    )
    return data, holder


@pytest.mark.asyncio
async def test_settings_get_includes_assist_api_supported():
    data = _make_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    resp = await view.get(_make_admin_request())
    assert resp.status == 200
    assert "assist_api_supported" in json.loads(resp.text)


@pytest.mark.asyncio
async def test_settings_get_includes_esphome_availability_flags():
    """The panel greys out ESPHome controls from these, so all three must ride along."""
    data = _make_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    with patch(
        "custom_components.phoenix_mcp.tools.discovery.esphome_availability",
        return_value=SimpleNamespace(integration=True, builder=False, builder_live=False),
    ):
        resp = await view.get(_make_admin_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["esphome_integration"] is True
    assert body["esphome_builder"] is False
    assert body["esphome_builder_live"] is False


@pytest.mark.asyncio
async def test_settings_patch_binds_active_token():
    token = _make_active_token()
    data, holder = _settings_data([token])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    body = json.dumps({"assist_bound_token_id": token.id}).encode()
    resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 200
    assert holder["s"].assist_bound_token_id == token.id
    assert json.loads(resp.text)["assist_bound_token_id"] == token.id


@pytest.mark.asyncio
async def test_settings_patch_rejects_unknown_token():
    data, _ = _settings_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    body = json.dumps({"assist_bound_token_id": "does-not-exist"}).encode()
    resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_patch_null_unbinds():
    token = _make_active_token()
    data, holder = _settings_data([token])
    holder["s"] = GlobalSettings(assist_bound_token_id=token.id)
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    body = json.dumps({"assist_bound_token_id": None}).encode()
    resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 200
    assert holder["s"].assist_bound_token_id is None


@pytest.mark.asyncio
async def test_revoke_clears_assist_binding():
    token = _make_active_token()
    data, holder = _settings_data([token])
    holder["s"] = GlobalSettings(assist_bound_token_id=token.id)
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    view = PhoenixAdminTokenView()
    view.hass = _make_hass(data)
    resp = await view.delete(_make_admin_request(), token_id=token.id)
    assert resp.status == 204
    assert holder["s"].assist_bound_token_id is None


@pytest.mark.asyncio
async def test_revoke_clears_voice_agent_token_and_resyncs():
    token = _make_active_token()
    data, holder = _settings_data([token])
    holder["s"] = GlobalSettings(voice_agent_enabled=True, voice_agent_token_id=token.id,
                                 voice_agent_provider_id="p", voice_agent_model="m")
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    data.async_sync_voice_agent = MagicMock()
    view = PhoenixAdminTokenView()
    view.hass = _make_hass(data)
    resp = await view.delete(_make_admin_request(), token_id=token.id)
    assert resp.status == 204
    assert holder["s"].voice_agent_token_id is None
    data.async_sync_voice_agent.assert_called_once()


# ---------------------------------------------------------------------------
# Voice agent: settings PATCH validation + live re-sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_patch_voice_agent_config_resyncs():
    token = _make_active_token()
    data, holder = _settings_data([token])
    data.async_sync_voice_agent = MagicMock()
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)

    provider_store = MagicMock()
    provider_store.get.return_value = {"kind": "claude"}
    body = json.dumps({
        "voice_agent_enabled": True, "voice_agent_token_id": token.id,
        "voice_agent_provider_id": "i1", "voice_agent_model": "m",
    }).encode()
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=provider_store)):
        resp = await view.patch(_make_admin_request(body=body))

    assert resp.status == 200
    assert holder["s"].voice_agent_enabled is True
    assert holder["s"].voice_agent_provider_id == "i1"
    data.async_sync_voice_agent.assert_called_once()


@pytest.mark.asyncio
async def test_settings_patch_voice_agent_rejects_inactive_token():
    data, _ = _settings_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    body = json.dumps({"voice_agent_token_id": "does-not-exist"}).encode()
    resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_patch_voice_agent_rejects_unknown_provider():
    data, _ = _settings_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    provider_store = MagicMock()
    provider_store.get.return_value = None
    body = json.dumps({"voice_agent_provider_id": "gone"}).encode()
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=provider_store)):
        resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Voice agent: one-click Assist pipeline endpoint + disable teardown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_pipeline_post_creates_and_defaults_preferred_true():
    data, _ = _settings_data([])
    view = PhoenixAdminVoiceAgentPipelineView()
    view.hass = _make_hass(data)
    created = AsyncMock(return_value={"pipeline_id": "pl1", "name": "Phoenix MCP", "preferred": True})
    with patch("custom_components.phoenix_mcp.voice_agent.async_create_assist_pipeline", created):
        resp = await view.post(_make_admin_request(body=b"{}"))
    assert resp.status == 200
    assert json.loads(resp.text)["pipeline_id"] == "pl1"
    # body omitted "preferred" -> defaults to True
    assert created.call_args.kwargs["preferred"] is True


@pytest.mark.asyncio
async def test_voice_pipeline_post_surfaces_config_error():
    from custom_components.phoenix_mcp.voice_agent import VoicePipelineError

    data, _ = _settings_data([])
    view = PhoenixAdminVoiceAgentPipelineView()
    view.hass = _make_hass(data)
    boom = AsyncMock(side_effect=VoicePipelineError("Configure the Phoenix MCP voice agent first."))
    with patch("custom_components.phoenix_mcp.voice_agent.async_create_assist_pipeline", boom):
        resp = await view.post(_make_admin_request(body=b"{}"))
    assert resp.status == 400
    assert "Configure" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_voice_pipeline_post_rejects_non_bool_preferred():
    data, _ = _settings_data([])
    view = PhoenixAdminVoiceAgentPipelineView()
    view.hass = _make_hass(data)
    body = json.dumps({"preferred": "yes"}).encode()
    resp = await view.post(_make_admin_request(body=body))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_voice_pipeline_delete_removes():
    data, _ = _settings_data([])
    view = PhoenixAdminVoiceAgentPipelineView()
    view.hass = _make_hass(data)
    remove = AsyncMock()
    with patch("custom_components.phoenix_mcp.voice_agent.async_remove_assist_pipeline", remove):
        resp = await view.delete(_make_admin_request())
    assert resp.status == 200
    remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabling_voice_agent_removes_tracked_pipeline():
    token = _make_active_token()
    data, holder = _settings_data([token])
    holder["s"] = GlobalSettings(
        voice_agent_enabled=True, voice_agent_token_id=token.id,
        voice_agent_provider_id="p", voice_agent_model="m", voice_agent_pipeline_id="pl1",
    )
    data.async_sync_voice_agent = MagicMock()
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    remove = AsyncMock()
    body = json.dumps({"voice_agent_enabled": False}).encode()
    with patch("custom_components.phoenix_mcp.voice_agent.async_remove_assist_pipeline", remove):
        resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 200
    remove.assert_awaited_once()


# ---------------------------------------------------------------------------
# AI Task entity: settings PATCH validation + revoke teardown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_get_includes_ai_task_supported():
    data = _make_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    resp = await view.get(_make_admin_request())
    assert "ai_task_supported" in json.loads(resp.text)


@pytest.mark.asyncio
async def test_settings_patch_ai_task_config():
    token = _make_active_token()
    data, holder = _settings_data([token])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    provider_store = MagicMock()
    provider_store.get.return_value = object()
    body = json.dumps({
        "ai_task_enabled": True, "ai_task_token_id": token.id,
        "ai_task_provider_id": "i1", "ai_task_model": "m",
    }).encode()
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=provider_store)):
        resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 200
    assert holder["s"].ai_task_enabled is True
    assert holder["s"].ai_task_provider_id == "i1"


@pytest.mark.asyncio
async def test_settings_patch_ai_task_rejects_inactive_token():
    data, _ = _settings_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    body = json.dumps({"ai_task_token_id": "does-not-exist"}).encode()
    resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_patch_ai_task_rejects_unknown_provider():
    data, _ = _settings_data([])
    view = PhoenixAdminSettingsView()
    view.hass = _make_hass(data)
    provider_store = MagicMock()
    provider_store.get.return_value = None
    body = json.dumps({"ai_task_provider_id": "gone"}).encode()
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=provider_store)):
        resp = await view.patch(_make_admin_request(body=body))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_revoke_clears_ai_task_token():
    token = _make_active_token()
    data, holder = _settings_data([token])
    holder["s"] = GlobalSettings(
        ai_task_enabled=True, ai_task_token_id=token.id,
        ai_task_provider_id="p", ai_task_model="m",
    )
    data.store.async_archive_token = AsyncMock(return_value=MagicMock())
    view = PhoenixAdminTokenView()
    view.hass = _make_hass(data)
    resp = await view.delete(_make_admin_request(), token_id=token.id)
    assert resp.status == 204
    assert holder["s"].ai_task_token_id is None


@pytest.mark.asyncio
async def test_ai_task_preferred_get_reports_status():
    data = _make_data([])
    view = PhoenixAdminAiTaskPreferredView()
    view.hass = _make_hass(data)
    resp = await view.get(_make_admin_request())
    assert resp.status == 200
    assert "supported" in json.loads(resp.text)


@pytest.mark.asyncio
async def test_ai_task_preferred_post_sets_default():
    data = _make_data([])
    view = PhoenixAdminAiTaskPreferredView()
    view.hass = _make_hass(data)
    status = {"supported": True, "entity_id": "ai_task.phoenix_mcp_ai_task", "is_preferred": True,
              "gen_data_entity_id": "ai_task.phoenix_mcp_ai_task", "gen_data_name": "Phoenix MCP AI Task"}
    with patch("custom_components.phoenix_mcp.ai_task.set_ai_task_preferred", return_value=status):
        resp = await view.post(_make_admin_request())
    assert resp.status == 200
    assert json.loads(resp.text)["is_preferred"] is True


@pytest.mark.asyncio
async def test_ai_task_preferred_post_surfaces_setup_error():
    from custom_components.phoenix_mcp.ai_task import AiTaskSetupError

    data = _make_data([])
    view = PhoenixAdminAiTaskPreferredView()
    view.hass = _make_hass(data)
    with patch("custom_components.phoenix_mcp.ai_task.set_ai_task_preferred",
               side_effect=AiTaskSetupError("Configure the Phoenix MCP AI Task first.")):
        resp = await view.post(_make_admin_request())
    assert resp.status == 400
    assert "Configure" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_ai_task_preferred_delete_clears():
    data = _make_data([])
    view = PhoenixAdminAiTaskPreferredView()
    view.hass = _make_hass(data)
    status = {"supported": True, "entity_id": None, "is_preferred": False,
              "gen_data_entity_id": None, "gen_data_name": None}
    with patch("custom_components.phoenix_mcp.ai_task.clear_ai_task_preferred", return_value=status):
        resp = await view.delete(_make_admin_request())
    assert resp.status == 200
    assert json.loads(resp.text)["is_preferred"] is False


# ---------------------------------------------------------------------------
# Settings PATCH validation: characterization.
#
# The PATCH handler validates and applies every global setting, and several of
# its rejection branches had no test at all. These pin the exact status and
# message of each one so the validation can be restructured without changing
# what an operator (or the panel) sees. Every case here failed to be covered
# before this block existed; they are deliberately assertions about MESSAGES,
# not just status codes, because the panel surfaces the message verbatim.
# ---------------------------------------------------------------------------


async def _settings_patch(hass, body: dict):
    view = PhoenixAdminSettingsView()
    view.hass = hass
    return await view.patch(_make_admin_request(body=json.dumps(body).encode()))


def _patchable_hass():
    data, holder = _settings_data([])
    return _make_hass(data), data, holder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"kill_switch": "yes"}, "must be a boolean"),
        ({"notify_on_approval": 1}, "must be a boolean"),
        ({"mesa_mode": "sideways"}, "mesa_mode must be one of"),
        ({"audit_flush_interval": "soon"}, "audit_flush_interval must be an integer"),
        ({"audit_flush_interval": 7}, "audit_flush_interval must be one of"),
        ({"audit_log_maxlen": "big"}, "audit_log_maxlen must be an integer"),
        ({"audit_log_maxlen": 250}, "audit_log_maxlen must be one of"),
        ({"agentcli_scrollback_lines": "many"}, "agentcli_scrollback_lines must be an integer"),
        ({"agentcli_max_iterations": "lots"}, "agentcli_max_iterations must be an integer"),
        ({"assist_bound_token_id": 42}, "must be a token id string or null"),
        ({"assist_bound_token_id": "ghost"}, "must reference an active token"),
        ({"voice_agent_token_id": 7}, "voice_agent_token_id must be a string or null"),
        ({"ai_task_model": []}, "ai_task_model must be a string or null"),
        ({"voice_agent_token_id": "ghost"}, "voice_agent_token_id must reference an active token"),
        ({"ai_task_token_id": "ghost"}, "ai_task_token_id must reference an active token"),
    ],
)
async def test_settings_patch_rejects_invalid_values(payload, fragment):
    hass, data, _ = _patchable_hass()
    resp = await _settings_patch(hass, payload)
    assert resp.status == 400, payload
    assert fragment in json.loads(resp.text)["message"], payload
    # A rejected patch must not reach the store at all.
    data.store.async_patch_settings.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, ""])
@pytest.mark.parametrize(
    "key",
    [
        "assist_bound_token_id", "voice_agent_token_id", "voice_agent_provider_id",
        "voice_agent_model", "ai_task_token_id", "ai_task_provider_id", "ai_task_model",
    ],
)
async def test_settings_patch_blank_string_clears_to_none(key, blank):
    """None and "" both mean unbind/clear; the panel sends either."""
    hass, data, _ = _patchable_hass()
    resp = await _settings_patch(hass, {key: blank})
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs[key] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "sent", "stored"),
    [
        ("agentcli_scrollback_lines", 999999, AGENTCLI_SCROLLBACK_MAX),
        ("agentcli_scrollback_lines", -5, AGENTCLI_SCROLLBACK_MIN),
        ("agentcli_max_iterations", 999999, AGENTCLI_MAX_ITERATIONS_MAX),
        ("agentcli_max_iterations", 0, AGENTCLI_MAX_ITERATIONS_MIN),
    ],
)
async def test_settings_patch_clamps_rather_than_rejecting(key, sent, stored):
    """These two clamp; the four enum-ish integers reject. Do not blur them."""
    hass, data, _ = _patchable_hass()
    resp = await _settings_patch(hass, {key: sent})
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs[key] == stored


@pytest.mark.asyncio
async def test_settings_patch_accepts_a_numeric_string_for_int_settings():
    """int() coercion is deliberate: the panel posts form values as strings."""
    hass, data, _ = _patchable_hass()
    resp = await _settings_patch(hass, {"audit_log_maxlen": "1000"})
    assert resp.status == 200
    assert data.store.async_patch_settings.call_args.kwargs["audit_log_maxlen"] == 1000


@pytest.mark.asyncio
async def test_settings_patch_rejects_unknown_provider_account():
    hass, data, _ = _patchable_hass()
    with patch(
        "custom_components.phoenix_mcp.agentcli._get_secret_store",
        new=AsyncMock(return_value=MagicMock(get=MagicMock(return_value=None))),
    ):
        resp = await _settings_patch(hass, {"voice_agent_provider_id": "nope"})
    assert resp.status == 400
    assert "must reference a configured provider account" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_settings_patch_side_effects_fire_for_their_own_keys_only():
    """Each side effect is keyed on its own setting; an unrelated patch is inert."""
    hass, data, _ = _patchable_hass()
    data.audit.resize = MagicMock()
    data.reschedule_audit_flush = MagicMock()
    data.async_sync_voice_agent = MagicMock()
    data.async_sync_ai_task = MagicMock()

    resp = await _settings_patch(hass, {"log_allowed": True})
    assert resp.status == 200
    data.audit.resize.assert_not_called()
    data.reschedule_audit_flush.assert_not_called()
    data.async_sync_voice_agent.assert_not_called()
    data.async_sync_ai_task.assert_not_called()


@pytest.mark.asyncio
async def test_settings_patch_audit_side_effects_fire_for_their_keys():
    hass, data, _ = _patchable_hass()
    data.audit.resize = MagicMock()
    data.reschedule_audit_flush = MagicMock()

    resp = await _settings_patch(hass, {"audit_log_maxlen": 5000, "audit_flush_interval": 15})
    assert resp.status == 200
    data.audit.resize.assert_called_once_with(5000)
    data.reschedule_audit_flush.assert_called_once()


@pytest.mark.asyncio
async def test_settings_patch_ai_task_sync_fires_on_ai_task_keys():
    hass, data, _ = _patchable_hass()
    data.async_sync_ai_task = MagicMock()
    resp = await _settings_patch(hass, {"ai_task_enabled": True})
    assert resp.status == 200
    data.async_sync_ai_task.assert_called_once()


# ---------------------------------------------------------------------------
# Token PATCH validation: characterization.
#
# Same reasoning as the settings block above. This handler validates the whole
# per-token surface (name, pass-through acknowledgment, every capability, the
# persona, the rate limits, the inline-confirm wait) and several of its
# rejections had no test. Pinned before restructuring, message included.
#
# NOTE its validation deliberately runs INSIDE the store lock (rule 17): the
# name-uniqueness check reads store state the same critical section then writes,
# so it cannot be hoisted out.
# ---------------------------------------------------------------------------


def _token_patch_data(token):
    data = _make_data([token])
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.name_slug_exists.return_value = False
    data.store.async_patch_token = AsyncMock(return_value=token)
    return data


async def _token_patch(data, token_id, body: dict):
    view = PhoenixAdminTokenView()
    view.hass = _make_hass(data)
    # require_admin's wrapper forwards extras as keyword-only.
    return await view.patch(_make_admin_request(body=json.dumps(body).encode()), token_id=token_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "status", "fragment"),
    [
        ({"expires_at": "2030-01-01"}, 400, "immutable"),
        ({"name": ""}, 400, "name is required"),
        ({"name": 5}, 400, "name is required"),
        ({"name": "no"}, 400, "3-32 characters"),
        ({"pass_through": "yes"}, 400, "pass_through must be a boolean"),
        ({"pass_through": True}, 400, "confirm_pass_through: true is required"),
        ({"pass_through": True, "confirm_pass_through": "true"}, 400, "confirm_pass_through: true is required"),
        ({"announce_all_tools": "yes"}, 400, "announce_all_tools must be a boolean"),
        ({"use_assist_exposure": "yes"}, 400, "use_assist_exposure must be a boolean"),
        ({"use_assist_exposure": True}, 400, "only valid for pass_through tokens"),
        ({"persona": "wizard"}, 400, "Unknown persona"),
        ({"cap_backup": "maybe"}, 400, "must be one of: deny, allow, confirm"),
        ({"confirm_inline_wait_seconds": 5}, 400, "must be 0 (off) or an integer from"),
        ({"confirm_inline_wait_seconds": True}, 400, "must be 0 (off) or an integer from"),
        ({"rate_limit_requests": "10"}, 400, "rate_limit_requests must be an integer"),
        ({"rate_limit_requests": True}, 400, "rate_limit_requests must be an integer"),
        ({"rate_limit_burst": -1}, 400, "rate_limit_burst must be non-negative"),
        ({"rate_limit_burst": 100_001}, 400, "must not exceed 100000"),
    ],
)
async def test_token_patch_rejects_invalid_values(payload, status, fragment):
    token = _make_active_token(name="tok-a")
    data = _token_patch_data(token)
    resp = await _token_patch(data, token.id, payload)
    assert resp.status == status, payload
    assert fragment in json.loads(resp.text)["message"], payload
    data.store.async_patch_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_patch_unknown_token_is_404():
    token = _make_active_token()
    data = _token_patch_data(token)
    data.store.get_token_by_id = MagicMock(return_value=None)
    resp = await _token_patch(data, "ghost", {"name": "whatever"})
    assert resp.status == 404
    assert "Token not found" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_token_patch_confirm_mode_refused_for_a_cap_that_cannot_confirm():
    """CONFIRM_AVAILABLE_CAPS is the allowlist; a cap outside it must refuse."""
    from custom_components.phoenix_mcp.const import CAPABILITY_NAMES, CONFIRM_AVAILABLE_CAPS

    non_confirmable = sorted(set(CAPABILITY_NAMES) - set(CONFIRM_AVAILABLE_CAPS))
    assert non_confirmable, "expected at least one non-confirmable capability"
    token = _make_active_token()
    data = _token_patch_data(token)
    resp = await _token_patch(data, token.id, {non_confirmable[0]: "confirm"})
    assert resp.status == 400
    assert "does not support 'confirm' mode" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_token_patch_use_assist_exposure_allowed_when_enabling_pass_through_together():
    """The check reads the RESULTING pass_through, not just the stored one."""
    token = _make_active_token(pass_through=False)
    data = _token_patch_data(token)
    resp = await _token_patch(data, token.id, {
        "pass_through": True, "confirm_pass_through": True, "use_assist_exposure": True,
    })
    assert resp.status == 200
    assert data.store.async_patch_token.call_args.kwargs["use_assist_exposure"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [0, MIN_CONFIRM_INLINE_WAIT_SECONDS, MAX_CONFIRM_INLINE_WAIT_SECONDS])
async def test_token_patch_accepts_valid_inline_wait(wait):
    token = _make_active_token()
    data = _token_patch_data(token)
    resp = await _token_patch(data, token.id, {"confirm_inline_wait_seconds": wait})
    assert resp.status == 200
    assert data.store.async_patch_token.call_args.kwargs["confirm_inline_wait_seconds"] == wait


@pytest.mark.asyncio
async def test_token_patch_rename_sensor_rebuild_failure_never_fails_the_patch():
    """Sensor rebuild is best-effort; a registry problem must not 500 the rename."""
    token = _make_active_token(name="old-name")
    data = _token_patch_data(token)
    renamed = _make_active_token(name="new-name")
    renamed.id = token.id
    data.store.async_patch_token = AsyncMock(return_value=renamed)
    data.async_on_token_archived = AsyncMock(side_effect=RuntimeError("registry boom"))
    data.async_on_token_created = AsyncMock(side_effect=RuntimeError("registry boom"))

    resp = await _token_patch(data, token.id, {"name": "new-name"})
    assert resp.status == 200
    data.async_on_token_archived.assert_awaited_once()
    data.async_on_token_created.assert_awaited_once()
