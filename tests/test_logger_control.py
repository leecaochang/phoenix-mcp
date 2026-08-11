"""Scoped integration logger inspection, control, and restoration contracts."""

from __future__ import annotations

import json
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp.logger_control import (
    IntegrationOverride,
    LoggerCompatibilityAdapter,
    LoggerControlUnavailable,
    LoggerOverrideManager,
)
from custom_components.phoenix_mcp.mcp_view import _dispatch_mcp
from tests.test_mcp_view import _make_data, _make_hass, _make_token


async def _call(args, *, tool="set_integration_log_level", token=None, data=None, hass=None):
    token = token or _make_token(cap_log_read="allow", cap_log_control="allow")[0]
    data = data or _make_data(token)
    hass = hass or _make_hass(data)
    response, _method, _resource, outcome = await _dispatch_mcp(
        "tools/call", 7, {"name": tool, "arguments": args}, token, hass, data,
        "127.0.0.1", base_url="http://homeassistant.local",
    )
    text = response["result"]["content"][0]["text"]
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = text
    return body, response["result"], outcome


@pytest.mark.asyncio
async def test_capabilities_are_checked_before_arguments():
    token = _make_token(cap_log_read="deny", cap_log_control="allow")[0]
    body, result, outcome = await _call({"integration": "SECRET.MARKER"}, token=token)
    assert outcome == "denied"
    assert result["isError"] is True
    assert body.startswith("Forbidden:")
    assert "MARKER" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"integration": "mqtt", "level": "TRACE", "persistence": "none"},
    {"integration": "mqtt", "level": "NOTSET", "persistence": "once"},
    {"integration": "mqtt", "level": "INFO", "persistence": "once", "duration_minutes": 5},
    {"integration": "mqtt", "level": "INFO", "persistence": "none", "duration_minutes": True},
    {"integration": "mqtt", "level": "INFO", "persistence": "none", "duration_minutes": 121},
])
async def test_invalid_shapes_fail_before_logger_access(args):
    data = _make_data(_make_token()[0])
    data.logger_control = MagicMock()
    with patch("custom_components.phoenix_mcp.mcp_view._visible_logger_domains") as visible:
        _body, result, outcome = await _call(args, data=data)
    assert outcome == "invalid_request"
    assert result["isError"] is True
    visible.assert_not_called()


@pytest.mark.asyncio
async def test_noop_skips_approval_when_setting_matches():
    token = _make_token(cap_log_read="allow", cap_log_control="confirm")[0]
    data = _make_data(token)
    manager = MagicMock()
    manager.adapter.declared_loggers = AsyncMock(return_value={"homeassistant.components.mqtt"})
    manager.adapter.get_override.return_value = IntegrationOverride("WARNING", "none")
    manager.active.return_value = None
    data.logger_control = manager
    with patch("custom_components.phoenix_mcp.mcp_view._visible_logger_domains", return_value={"mqtt"}), patch(
        "custom_components.phoenix_mcp.mcp_view.loader.async_get_integration", new=AsyncMock()
    ), patch("custom_components.phoenix_mcp.mcp_view._gate", new=AsyncMock()) as gate:
        body, _result, outcome = await _call(
            {"integration": "mqtt", "level": "WARNING", "persistence": "none"},
            token=token, data=data,
        )
    assert outcome == "allowed"
    assert body["changed"] is False
    gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_uses_integration_logger_set_and_returns_verbose_warnings():
    token = _make_token(cap_log_read="allow", cap_log_control="allow")[0]
    data = _make_data(token)
    manager = MagicMock()
    manager.adapter.declared_loggers = AsyncMock(return_value={"custom_components.demo", "homeassistant.components.demo"})
    manager.adapter.get_override.side_effect = [
        None, None, IntegrationOverride("DEBUG", "none")
    ]
    manager.adapter.effective_levels.return_value = {
        "custom_components.demo": "DEBUG", "homeassistant.components.demo": "DEBUG"
    }
    manager.async_set = AsyncMock()
    manager.active.return_value = None
    data.logger_control = manager
    with patch("custom_components.phoenix_mcp.mcp_view._visible_logger_domains", return_value={"demo"}), patch(
        "custom_components.phoenix_mcp.mcp_view.loader.async_get_integration", new=AsyncMock()
    ):
        body, _result, outcome = await _call(
            {"integration": "demo", "level": "DEBUG", "persistence": "none"},
            token=token, data=data,
        )
    assert outcome == "allowed"
    assert body["affected_loggers"] == ["custom_components.demo", "homeassistant.components.demo"]
    assert len(body["warnings"]) == 2
    manager.async_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_reports_source_and_only_scoped_domains():
    token = _make_token(cap_log_read="allow")[0]
    data = _make_data(token)
    manager = MagicMock()
    manager.adapter.source_status.return_value = {"status": "available"}
    manager.adapter.declared_loggers = AsyncMock(return_value={"homeassistant.components.demo"})
    manager.adapter.get_override.return_value = IntegrationOverride("WARNING", "once")
    manager.adapter.effective_levels.return_value = {"homeassistant.components.demo": "WARNING"}
    manager.active.return_value = None
    data.logger_control = manager
    with patch("custom_components.phoenix_mcp.mcp_view._visible_logger_domains", return_value={"demo"}):
        body, _result, outcome = await _call(
            {}, tool="list_integration_log_levels", token=token, data=data
        )
    assert outcome == "allowed"
    assert body["source"] == {
        "status": "available", "timed_restoration": "available"
    }
    assert [row["integration"] for row in body["integrations"]] == ["demo"]
    assert body["integrations"][0]["override"]["setting"] == {
        "level": "WARNING", "persistence": "once"
    }


def test_private_logger_state_shape_fails_closed():
    hass = MagicMock()
    hass.data = {"logger": MagicMock(settings=MagicMock(_stored_config={"unexpected": {}}))}
    adapter = LoggerCompatibilityAdapter(hass)
    assert adapter.source_status()["status"] == "degraded"
    with pytest.raises(LoggerControlUnavailable):
        adapter.get_override("mqtt")


@pytest.mark.asyncio
async def test_adapter_writes_only_through_integration_aware_command():
    hass = MagicMock()
    hass.data = {"logger": MagicMock(settings=MagicMock(_stored_config={"logs": {}}))}
    adapter = LoggerCompatibilityAdapter(hass)
    with patch(
        "custom_components.phoenix_mcp.ws_dispatch.async_ws_command",
        new=AsyncMock(),
    ) as command:
        await adapter.apply("mqtt", IntegrationOverride("WARNING", "none"))
    command.assert_awaited_once_with(
        hass,
        "logger/integration_log_level",
        {"integration": "mqtt", "level": "WARNING", "persistence": "none"},
    )


@pytest.mark.asyncio
async def test_timed_expiry_never_overwrites_a_superseding_change():
    hass = MagicMock()
    manager = LoggerOverrideManager(hass)
    manager._store = AsyncMock()
    manager._store.async_save = AsyncMock()
    manager.adapter = MagicMock()
    manager.adapter.get_override.return_value = IntegrationOverride("ERROR", "permanent")
    manager.adapter.declared_loggers = AsyncMock(return_value={"homeassistant.components.demo"})
    manager.adapter.apply = AsyncMock()
    from custom_components.phoenix_mcp.logger_control import TimedOverride
    from homeassistant.util.dt import utcnow
    now = utcnow()
    manager.records["demo"] = TimedOverride(
        "demo", IntegrationOverride("WARNING", "none"), None,
        ["homeassistant.components.demo"], "token", now, now,
    )
    manager._timers["demo"] = MagicMock()
    await manager.async_expire("demo")
    manager.adapter.apply.assert_not_awaited()
    assert "demo" not in manager.records


def test_installed_logger_compatibility_surface_still_has_required_state():
    from homeassistant.components.logger.helpers import (
        LoggerSettings, LoggerSetting, get_integration_loggers,
    )
    assert {"level", "persistence", "type"} <= LoggerSetting.__annotations__.keys()
    source = inspect.getsource(LoggerSettings)
    assert '_stored_config[STORAGE_LOG_KEY]' in source
    assert "async def async_update" in source
    assert "get_integration_loggers" in inspect.getsource(get_integration_loggers)
    from homeassistant.components.logger.websocket_api import handle_integration_log_level
    schema = handle_integration_log_level._ws_schema
    if hasattr(schema, "schema"):
        schema = schema.schema
    assert {"integration", "level", "persistence"} <= set(schema)
    command_source = inspect.getsource(handle_integration_log_level.__wrapped__)
    assert "async_get_integration" in command_source
    assert "LogSettingsType.INTEGRATION" in command_source


@pytest.mark.asyncio
async def test_timed_expiry_restores_exact_baseline_when_still_current():
    hass = MagicMock()
    manager = LoggerOverrideManager(hass)
    manager._store = AsyncMock()
    manager._store.async_save = AsyncMock()
    manager.adapter = MagicMock()
    applied = IntegrationOverride("WARNING", "none")
    prior = IntegrationOverride("ERROR", "permanent")
    manager.adapter.get_override.return_value = applied
    manager.adapter.declared_loggers = AsyncMock(return_value={"homeassistant.components.demo"})
    manager.adapter.apply = AsyncMock()
    from custom_components.phoenix_mcp.logger_control import TimedOverride
    from homeassistant.util.dt import utcnow
    now = utcnow()
    manager.records["demo"] = TimedOverride(
        "demo", applied, prior, ["homeassistant.components.demo"], "token", now, now,
    )
    manager._timers["demo"] = MagicMock()
    await manager.async_expire("demo")
    manager.adapter.apply.assert_awaited_once_with("demo", prior)
    assert "demo" not in manager.records


@pytest.mark.asyncio
async def test_replacing_timed_override_preserves_original_baseline():
    hass = MagicMock()
    manager = LoggerOverrideManager(hass)
    manager._store = AsyncMock()
    manager._store.async_save = AsyncMock()
    manager.adapter = MagicMock()
    manager.adapter.apply = AsyncMock()
    manager._schedule = MagicMock()
    from custom_components.phoenix_mcp.logger_control import TimedOverride
    from homeassistant.util.dt import utcnow
    now = utcnow()
    original = IntegrationOverride("ERROR", "permanent")
    manager.records["demo"] = TimedOverride(
        "demo", IntegrationOverride("WARNING", "none"), original,
        ["homeassistant.components.demo"], "first", now, now,
    )
    await manager.async_set(
        domain="demo", desired=IntegrationOverride("INFO", "none"),
        prior=IntegrationOverride("WARNING", "none"),
        loggers={"homeassistant.components.demo"}, owner_token_id="second",
        duration_minutes=10,
    )
    assert manager.records["demo"].prior == original


@pytest.mark.asyncio
async def test_storage_failure_disables_timers_without_blocking_initialization():
    hass = MagicMock()
    manager = LoggerOverrideManager(hass)
    manager._store = AsyncMock()
    manager._store.async_load.side_effect = OSError("disk unavailable")
    await manager.async_initialize()
    assert manager.storage_available is False
    manager.adapter = MagicMock()
    manager.adapter.apply = AsyncMock()
    with pytest.raises(LoggerControlUnavailable):
        await manager.async_set(
            domain="demo", desired=IntegrationOverride("WARNING", "none"),
            prior=None, loggers={"homeassistant.components.demo"},
            owner_token_id="token", duration_minutes=5,
        )
    manager.adapter.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_execution_rejects_logger_context_race():
    token = _make_token(cap_log_read="allow", cap_log_control="allow")[0]
    data = _make_data(token)
    manager = MagicMock()
    manager.adapter.declared_loggers = AsyncMock(side_effect=[
        {"homeassistant.components.demo"},
        {"homeassistant.components.demo", "custom_components.demo"},
    ])
    manager.adapter.get_override.return_value = None
    manager.async_set = AsyncMock()
    data.logger_control = manager
    with patch("custom_components.phoenix_mcp.mcp_view._visible_logger_domains", return_value={"demo"}), patch(
        "custom_components.phoenix_mcp.mcp_view.loader.async_get_integration", new=AsyncMock()
    ):
        body, result, outcome = await _call(
            {"integration": "demo", "level": "WARNING", "persistence": "none"},
            token=token, data=data,
        )
    assert outcome == "denied"
    assert result["isError"] is True
    assert "changed after approval" in body
    manager.async_set.assert_not_awaited()
