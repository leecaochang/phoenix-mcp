"""Tests for the Assist bridge (assist_api.py).

Covers registration lifecycle, per-turn instance building (scoping mirrors
tools/list gating), tool dispatch through _dispatch_mcp with the "assist"
sentinel, confirm-gate degradation to a queued reply, and the unbound/killed
degradations.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.phoenix_mcp import assist_api
from custom_components.phoenix_mcp.assist_api import (
    PhoenixAssistAPI,
    PhoenixAssistTool,
    async_probe_assist_api,
    assist_api_supported,
    async_register_assist_api,
    async_unregister_assist_api,
)
from custom_components.phoenix_mcp.const import ASSIST_CLIENT_IP, DOMAIN, TOKEN_PREFIX
from custom_components.phoenix_mcp.token_store import GlobalSettings, PermissionTree, TokenRecord


def _make_token(*, announce_all: bool = False, cap_config_read: str = "allow") -> TokenRecord:
    from homeassistant.util.dt import utcnow

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="voice-token",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        created_at=utcnow(),
        created_by="admin",
        cap_config_read=cap_config_read,
        announce_all_tools=announce_all,
        permissions=PermissionTree(),
    )


def _make_data(token: TokenRecord | None, *, kill_switch: bool = False, bound_id=None) -> SimpleNamespace:
    settings = GlobalSettings(kill_switch=kill_switch, assist_bound_token_id=bound_id)
    store = MagicMock()
    store.get_settings.return_value = settings
    store.get_token_by_id.side_effect = lambda tid: token if (token and tid == token.id) else None
    return SimpleNamespace(
        store=store,
        mesa=None,
        ready=True,
        shutting_down=False,
        assist_api_unregister=None,
    )


# --------------------------------------------------------------------------- #
# Support / registration lifecycle
# --------------------------------------------------------------------------- #

def test_assist_api_supported_in_test_env():
    # The pinned HA test env ships helpers.llm and the legacy converter.
    assert assist_api_supported() is True


@pytest.mark.asyncio
async def test_schema_converter_probe_runs_off_the_event_loop(hass):
    executor = MagicMock()

    async def _run_in_executor(func, *args):
        executor()
        return func(*args)

    with patch.object(hass, "async_add_executor_job", side_effect=_run_in_executor):
        assert await async_probe_assist_api(hass) is True
    executor.assert_called_once_with()


def test_schema_converter_falls_back_to_probatio_when_legacy_module_is_absent(monkeypatch):
    assist_api._schema_converter.cache_clear()
    legacy_error = ModuleNotFoundError(
        "No module named 'voluptuous_openapi'", name="voluptuous_openapi"
    )
    probatio = SimpleNamespace(from_openapi=lambda schema: {"probatio": schema})

    def _import_module(name):
        if name == "voluptuous_openapi":
            raise legacy_error
        assert name == "probatio"
        return probatio

    monkeypatch.setattr(assist_api.importlib, "import_module", _import_module)
    assert assist_api._convert_to_voluptuous({"type": "object"}) == {
        "probatio": {"type": "object"}
    }
    assist_api._schema_converter.cache_clear()


def test_schema_converter_preserves_nested_module_not_found(monkeypatch):
    assist_api._schema_converter.cache_clear()

    def _import_module(name):
        assert name == "voluptuous_openapi"
        raise ModuleNotFoundError("No module named 'dependency'", name="dependency")

    monkeypatch.setattr(assist_api.importlib, "import_module", _import_module)
    with pytest.raises(ModuleNotFoundError, match="dependency"):
        assist_api._convert_to_voluptuous({"type": "object"})
    assist_api._schema_converter.cache_clear()


@pytest.mark.asyncio
async def test_register_and_unregister_lifecycle(hass):
    data = _make_data(None)
    async_register_assist_api(hass, data)
    assert data.assist_api_unregister is not None
    first = data.assist_api_unregister

    # Idempotent: a second register while up is a no-op (same callback).
    async_register_assist_api(hass, data)
    assert data.assist_api_unregister is first

    async_unregister_assist_api(data)
    assert data.assist_api_unregister is None
    # Idempotent unregister.
    async_unregister_assist_api(data)
    assert data.assist_api_unregister is None


# --------------------------------------------------------------------------- #
# Instance building
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_unbound_instance_has_no_tools_and_never_raises(hass):
    data = _make_data(_make_token(), bound_id=None)
    api = PhoenixAssistAPI(hass, data)
    instance = await api.async_get_api_instance(_ctx())
    assert instance.tools == []
    assert "bind a token" in instance.api_prompt.lower()


@pytest.mark.asyncio
async def test_kill_switch_yields_no_tools(hass):
    token = _make_token()
    data = _make_data(token, kill_switch=True, bound_id=token.id)
    api = PhoenixAssistAPI(hass, data)
    instance = await api.async_get_api_instance(_ctx())
    assert instance.tools == []


@pytest.mark.asyncio
async def test_bound_instance_tools_mirror_tools_list_gating(hass):
    from custom_components.phoenix_mcp.agentcli import build_mcp_tool_list

    token = _make_token(cap_config_read="allow")
    data = _make_data(token, bound_id=token.id)
    api = PhoenixAssistAPI(hass, data)
    instance = await api.async_get_api_instance(_ctx())

    expected = {d["name"] for d in build_mcp_tool_list(token, data)}
    assert {t.name for t in instance.tools} == expected
    assert expected  # non-empty for a read-capable token


@pytest.mark.asyncio
async def test_announce_all_widens_the_tool_set(hass):
    scoped = _make_token(announce_all=False)
    wide = _make_token(announce_all=True)
    d_scoped = _make_data(scoped, bound_id=scoped.id)
    d_wide = _make_data(wide, bound_id=wide.id)

    scoped_tools = {t.name for t in (await PhoenixAssistAPI(hass, d_scoped).async_get_api_instance(_ctx())).tools}
    wide_tools = {t.name for t in (await PhoenixAssistAPI(hass, d_wide).async_get_api_instance(_ctx())).tools}
    assert scoped_tools < wide_tools


@pytest.mark.asyncio
async def test_one_unconvertible_schema_is_skipped_not_fatal(hass):
    from custom_components.phoenix_mcp.agentcli import build_mcp_tool_list

    token = _make_token()
    data = _make_data(token, bound_id=token.id)
    names = [d["name"] for d in build_mcp_tool_list(token, data)]
    victim = names[0]
    real = assist_api._convert_to_voluptuous

    def flaky(schema):
        # Fail for the victim tool's schema only; convert the rest normally.
        if isinstance(schema, dict) and schema.get("__victim__"):
            raise ValueError("boom")
        return real(schema)

    def tagged_list(tok, dat):
        out = build_mcp_tool_list(tok, dat)
        for d in out:
            if d["name"] == victim:
                d["inputSchema"] = {"__victim__": True}
        return out

    with patch.object(assist_api, "_convert_to_voluptuous", side_effect=flaky), \
         patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", side_effect=tagged_list):
        instance = await PhoenixAssistAPI(hass, data).async_get_api_instance(_ctx())

    got = {t.name for t in instance.tools}
    assert victim not in got
    assert got == set(names) - {victim}


# --------------------------------------------------------------------------- #
# Tool dispatch
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tool_call_dispatches_with_assist_sentinel(hass):
    token = _make_token()
    data = _make_data(token, bound_id=token.id)
    hass.data[DOMAIN] = data

    captured = {}

    async def fake_dispatch(method, msg_id, params, tok, h, d, client_ip, base_url, *a, **k):
        captured["client_ip"] = client_ip
        captured["name"] = params["name"]
        return ({"result": {"content": [{"type": "text", "text": '{"state": "on"}'}]}}, "get_state", "get_state", "allowed")

    tool = PhoenixAssistTool("get_state", "d", MagicMock(), token.id)
    with patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=fake_dispatch):
        result = await tool.async_call(hass, _tool_input({"entity_id": "light.x"}), _ctx())

    assert captured["client_ip"] == ASSIST_CLIENT_IP
    assert captured["name"] == "get_state"
    assert result == {"state": "on"}


@pytest.mark.asyncio
async def test_confirm_gated_call_returns_queued(hass):
    token = _make_token()
    data = _make_data(token, bound_id=token.id)
    hass.data[DOMAIN] = data

    pending = {
        "result": {
            "content": [{"type": "text", "text": '{"status": "pending_approval", "approval_id": "ap-1"}'}],
        }
    }

    async def fake_dispatch(*a, **k):
        return (pending, "call_service", "call_service", "pending_approval")

    tool = PhoenixAssistTool("call_service", "d", MagicMock(), token.id)
    with patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=fake_dispatch):
        result = await tool.async_call(hass, _tool_input({}), _ctx())

    assert result["status"] == "queued_for_approval"
    assert result["approval_id"] == "ap-1"


@pytest.mark.asyncio
async def test_tool_call_on_revoked_token_returns_error(hass):
    token = _make_token()
    data = _make_data(token, bound_id=token.id)
    data.store.get_token_by_id.side_effect = lambda tid: None  # gone since instance built
    hass.data[DOMAIN] = data

    tool = PhoenixAssistTool("get_state", "d", MagicMock(), token.id)
    result = await tool.async_call(hass, _tool_input({}), _ctx())
    assert "error" in result


@pytest.mark.asyncio
async def test_error_result_is_surfaced_not_raised(hass):
    token = _make_token()
    data = _make_data(token, bound_id=token.id)
    hass.data[DOMAIN] = data

    async def fake_dispatch(*a, **k):
        return ({"result": {"content": [{"type": "text", "text": "Forbidden."}], "isError": True}}, "x", "x", "denied")

    tool = PhoenixAssistTool("get_state", "d", MagicMock(), token.id)
    with patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=fake_dispatch):
        result = await tool.async_call(hass, _tool_input({}), _ctx())
    assert result == {"error": "Forbidden."}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ctx():
    return SimpleNamespace(platform="conversation", context=None, language="en", assistant="conversation", device_id=None)


def _tool_input(args):
    return SimpleNamespace(id="ti-1", tool_name="t", tool_args=args, external=False)
