"""Tests for the in-process WebSocket command dispatcher (ws_dispatch)."""

from __future__ import annotations

import pytest
from homeassistant.setup import async_setup_component

import custom_components.phoenix_mcp.ws_dispatch as wd_module
from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError, async_ws_command
from custom_components.phoenix_mcp.tools.helper import HELPER_TYPES


def test_storage_helper_domains_have_all_allowlisted_commands():
    """The helper catalog and privileged WS allowlist must move together."""
    assert set(wd_module._HELPER_DOMAINS) == HELPER_TYPES
    assert {
        f"{domain}/{operation}"
        for domain in HELPER_TYPES
        for operation in ("create", "update", "delete", "list")
    } <= wd_module.ALLOWED_WS_COMMANDS


async def test_create_input_boolean_in_process(hass, hass_admin_user):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})

    result = await async_ws_command(hass, "input_boolean/create", {"name": "Phoenix MCP Test"})
    assert result["name"] == "Phoenix MCP Test"
    assert "id" in result

    await hass.async_block_till_done()
    ids = [s.entity_id for s in hass.states.async_all() if s.entity_id.startswith("input_boolean.")]
    assert ids, "the created input_boolean should appear as an entity"


async def test_delete_input_boolean_in_process(hass, hass_admin_user):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    created = await async_ws_command(hass, "input_boolean/create", {"name": "Temp"})
    await hass.async_block_till_done()

    await async_ws_command(hass, "input_boolean/delete", {"input_boolean_id": created["id"]})
    await hass.async_block_till_done()
    # The deleted helper should no longer exist as an entity (verified via states).
    ids = [s.entity_id for s in hass.states.async_all() if s.entity_id.startswith("input_boolean.")]
    assert not ids, "the deleted input_boolean should no longer appear as an entity"


async def test_unknown_command_raises(hass):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    with pytest.raises(WsDispatchError):
        await async_ws_command(hass, "nonexistent/command", {})


async def test_command_not_on_allowlist_rejected(hass, hass_admin_user):
    # A real, registered HA command that Phoenix MCP never dispatches must still be
    # refused by the allowlist guard, before any handler lookup or execution.
    # (input_boolean/list is now allowlisted for version-history capture, so a
    # freshly registered throwaway command stands in for "registered but blocked".)
    from homeassistant.components import websocket_api

    @websocket_api.websocket_command({"type": "phx_test/registered_but_blocked"})
    def _handler(hass, connection, msg):
        connection.send_result(msg["id"])

    websocket_api.async_register_command(hass, _handler)
    with pytest.raises(WsDispatchError, match="not allowed"):
        await async_ws_command(hass, "phx_test/registered_but_blocked", {})


async def test_invalid_payload_raises(hass, hass_admin_user):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    # input_boolean/create requires a name; omitting it should fail schema validation.
    with pytest.raises(WsDispatchError):
        await async_ws_command(hass, "input_boolean/create", {})


async def test_handler_side_error_wrapped(hass, hass_admin_user):
    # Deleting a nonexistent item makes the handler fail; that must surface as a
    # clean WsDispatchError, not a raw HA exception (the broad-wrap hardening).
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    with pytest.raises(WsDispatchError):
        await async_ws_command(hass, "input_boolean/delete", {"input_boolean_id": "missing"})


async def test_result_via_send_message_bytes_is_captured(hass, hass_admin_user, monkeypatch):
    # Some handlers (notably logbook/get_events) deliver their result through
    # connection.send_message with a pre-serialized JSON result message, not via
    # send_result. _CapturingConnection must capture that too, or the dispatch
    # times out. Regression for the get_logbook timeout.
    import json

    import custom_components.phoenix_mcp.ws_dispatch as wd
    from homeassistant.components import websocket_api
    from homeassistant.components.websocket_api import messages

    @websocket_api.websocket_command({"type": "phx_test/send_message_result"})
    @websocket_api.async_response
    async def _handler(hass, connection, msg):
        payload = json.dumps(messages.result_message(msg["id"], {"ok": True})).encode()
        connection.send_message(payload)

    websocket_api.async_register_command(hass, _handler)
    monkeypatch.setattr(
        wd, "ALLOWED_WS_COMMANDS", wd.ALLOWED_WS_COMMANDS | {"phx_test/send_message_result"}
    )

    result = await wd.async_ws_command(hass, "phx_test/send_message_result", {})
    assert result == {"ok": True}


async def test_zha_reads_are_allowlisted():
    from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS

    assert "zha/device" in ALLOWED_WS_COMMANDS
    assert "zha/network/settings" in ALLOWED_WS_COMMANDS


async def test_only_integration_aware_logger_control_is_allowlisted():
    from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS

    assert "logger/integration_log_level" in ALLOWED_WS_COMMANDS
    assert "logger/set_level" not in ALLOWED_WS_COMMANDS


async def test_search_related_is_allowlisted_and_matches_home_assistant(hass, hass_admin_user):
    """The relationship graph is a real HA command with a stable result shape."""
    from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS

    hass.states.async_set("light.kitchen", "off")
    assert await async_setup_component(hass, "automation", {
        "automation": [{
            "id": "graph-contract",
            "alias": "Graph contract",
            "trigger": [{"platform": "state", "entity_id": "light.kitchen"}],
            "action": [],
        }],
    })
    await hass.async_block_till_done()

    assert "search/related" in ALLOWED_WS_COMMANDS
    assert await async_setup_component(hass, "search", {"search": {}})

    result = await async_ws_command(
        hass,
        "search/related",
        {"item_type": "entity", "item_id": "light.kitchen"},
    )
    assert isinstance(result, dict)
    assert result["automation"] == {"automation.graph_contract"}


async def test_zha_leaky_write_commands_are_not_allowlisted():
    # zha/devices/permit enables process-wide ZHA debug logging and parks its
    # cleanup in connection.subscriptions; zha/devices/reconfigure parks a
    # dispatcher listener the same way and never calls send_result;
    # zha/topology/update never calls send_result. A synthetic capturing
    # connection can release none of that, so these must never be dispatched
    # (permit/remove go through zha services, reconfigure through
    # async_zha_reconfigure_device).
    from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS

    assert "zha/devices/permit" not in ALLOWED_WS_COMMANDS
    assert "zha/devices/reconfigure" not in ALLOWED_WS_COMMANDS
    assert "zha/topology/update" not in ALLOWED_WS_COMMANDS


async def test_zha_reconfigure_unavailable_without_zha(hass):
    # zigpy/zha are not installed in the test env, so the import seam inside
    # async_zha_reconfigure_device must degrade to a clean WsDispatchError.
    from custom_components.phoenix_mcp.ws_dispatch import async_zha_reconfigure_device

    with pytest.raises(WsDispatchError, match="ZHA is not available"):
        await async_zha_reconfigure_device(hass, "00:11:22:33:44:55:66:77")


async def test_zha_reconfigure_dispatches_task_via_gateway(hass, monkeypatch):
    # With the gateway seam faked, the reconfigure starts the reinterview task
    # for a known device and raises not_found for an unknown one.
    import sys
    import types

    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _Device:
        ieee = "eui:known"

    class _Gateway:
        def __init__(self):
            self.reinterviewed = []

        def get_device(self, ieee):
            return _Device() if str(ieee) == "eui:00:11" else None

        async def async_reinterview_device(self, ieee):
            self.reinterviewed.append(str(ieee))

    gateway = _Gateway()
    zha_helpers = types.ModuleType("homeassistant.components.zha.helpers")
    zha_helpers.get_zha_gateway = lambda hass: gateway
    zha_pkg = types.ModuleType("homeassistant.components.zha")
    zha_pkg.helpers = zha_helpers
    monkeypatch.setitem(sys.modules, "homeassistant.components.zha", zha_pkg)
    monkeypatch.setitem(sys.modules, "homeassistant.components.zha.helpers", zha_helpers)

    class _EUI64(str):
        @classmethod
        def convert(cls, value):
            return cls(f"eui:{value}")

    zigpy_types = types.ModuleType("zigpy.types")
    zigpy_types.EUI64 = _EUI64
    zigpy_pkg = types.ModuleType("zigpy")
    zigpy_pkg.types = zigpy_types
    monkeypatch.setitem(sys.modules, "zigpy", zigpy_pkg)
    monkeypatch.setitem(sys.modules, "zigpy.types", zigpy_types)

    await wd.async_zha_reconfigure_device(hass, "00:11")
    await hass.async_block_till_done()
    assert gateway.reinterviewed == ["eui:known"]

    with pytest.raises(WsDispatchError, match="not_found"):
        await wd.async_zha_reconfigure_device(hass, "99:99")


async def test_compat_probe_passes_on_current_ha(hass):
    from custom_components.phoenix_mcp.ws_dispatch import check_ws_dispatch_compat

    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    assert check_ws_dispatch_compat(hass) is None


async def test_compat_probe_reports_unsupported_required_param(hass, monkeypatch):
    # Drift detection: a NEW required constructor param Phoenix MCP cannot supply (the
    # exact class of break that HA 2026.6 caused by adding `remote`).
    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _Extra:
        def __init__(self, logger, hass, send_message, user, refresh_token, remote, mystery):
            ...

        def send_result(self): ...
        def send_error(self): ...
        def async_handle_exception(self): ...

    monkeypatch.setattr(wd, "ActiveConnection", _Extra)
    reason = wd.check_ws_dispatch_compat(hass)
    assert reason is not None and "mystery" in reason


async def test_compat_probe_tolerates_added_param_we_supply(hass, monkeypatch):
    # A new param we DO know how to supply (like `remote`) is not flagged.
    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _WithRemote:
        def __init__(self, logger, hass, send_message, user, refresh_token, remote):
            ...

        def send_result(self): ...
        def send_error(self): ...
        def async_handle_exception(self): ...

    monkeypatch.setattr(wd, "ActiveConnection", _WithRemote)
    assert wd.check_ws_dispatch_compat(hass) is None


async def test_compat_probe_reports_unsupported_required_kwonly_param(hass, monkeypatch):
    # Blind-spot regression: a required KEYWORD-ONLY constructor param Phoenix MCP cannot
    # supply fails construction per-call exactly like a positional one, so the
    # probe must flag it too (it previously derived requiredness from positional
    # args + __defaults__ only and reported such an HA compatible).
    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _KwOnlyRequired:
        def __init__(self, logger, hass, send_message, user, refresh_token, *, mystery_kw):
            ...

        def send_result(self): ...
        def send_error(self): ...
        def async_handle_exception(self): ...

    monkeypatch.setattr(wd, "ActiveConnection", _KwOnlyRequired)
    reason = wd.check_ws_dispatch_compat(hass)
    assert reason is not None and "mystery_kw" in reason


async def test_compat_probe_tolerates_kwonly_param_with_default_or_supplied(hass, monkeypatch):
    # A kwonly param with a default needs nothing from Phoenix MCP; a required kwonly
    # param Phoenix MCP already supplies (e.g. remote) is satisfied at construction.
    # Neither is drift.
    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _KwOnlyBenign:
        def __init__(self, logger, hass, send_message, user, refresh_token, *, remote, extra=None):
            ...

        def send_result(self): ...
        def send_error(self): ...
        def async_handle_exception(self): ...

    monkeypatch.setattr(wd, "ActiveConnection", _KwOnlyBenign)
    assert wd.check_ws_dispatch_compat(hass) is None


async def test_compat_probe_never_raises_on_introspection_failure(hass, monkeypatch):
    # Regression: on Python 3.14, inspect.signature(ActiveConnection.__init__)
    # raised NameError while evaluating a TYPE_CHECKING-only annotation, which
    # aborted Phoenix MCP setup. The advisory probe must degrade to a string, never raise.
    import custom_components.phoenix_mcp.ws_dispatch as wd

    class _BadCode:
        co_argcount = 1
        co_kwonlyargcount = 0

        @property
        def co_varnames(self):
            raise NameError("WebSocketAdapter")

    class _Init:
        __code__ = _BadCode()

    class _Pathological:
        __init__ = _Init()

        def send_result(self): ...
        def send_error(self): ...
        def async_handle_exception(self): ...

    monkeypatch.setattr(wd, "ActiveConnection", _Pathological)
    reason = wd.check_ws_dispatch_compat(hass)
    assert isinstance(reason, str)


# ---------------------------------------------------------------------------
# The probe also has to cover the CALL async_ws_command makes, not just the
# connection object it builds: the registry entry unpacking as (handler, schema)
# and the handler taking (hass, connection, msg). Neither is visible to the
# constructor/method checks above, so an arity change would have reported
# "compatible" at startup and then failed every dispatch at runtime.
# ---------------------------------------------------------------------------


async def test_compat_probe_reports_registry_entry_shape_change(hass, monkeypatch):
    import custom_components.phoenix_mcp.ws_dispatch as wd
    from homeassistant.components.websocket_api import const as ws_const

    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    # A 3-tuple would blow up the `handler, schema = handlers[command]` unpack.
    registry = dict(hass.data[ws_const.DOMAIN])
    registry["input_boolean/create"] = (lambda h, c, m: None, None, "extra")
    monkeypatch.setitem(hass.data, ws_const.DOMAIN, registry)
    reason = wd.check_ws_dispatch_compat(hass)
    assert reason is not None and "handler, schema" in reason


async def test_compat_probe_reports_handler_arity_change(hass, monkeypatch):
    import custom_components.phoenix_mcp.ws_dispatch as wd
    from homeassistant.components.websocket_api import const as ws_const

    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})

    def _four_args(hass_, connection, msg, surprise):  # noqa: ANN001
        ...

    registry = dict(hass.data[ws_const.DOMAIN])
    # Every allowlisted command is replaced: the probe samples the first one it
    # finds, so patching a single entry could be skipped over.
    for cmd in list(registry):
        if cmd in wd.ALLOWED_WS_COMMANDS:
            registry[cmd] = (_four_args, None)
    monkeypatch.setitem(hass.data, ws_const.DOMAIN, registry)
    reason = wd.check_ws_dispatch_compat(hass)
    assert reason is not None and "positional" in reason


async def test_compat_probe_tolerates_decorated_varargs_handler(hass, monkeypatch):
    # A handler wrapped by a decorator forwards *args and tells us nothing about
    # the real arity, so it must not be reported as drift.
    import custom_components.phoenix_mcp.ws_dispatch as wd
    from homeassistant.components.websocket_api import const as ws_const

    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})

    def _wrapper(*args, **kwargs):
        ...

    registry = dict(hass.data[ws_const.DOMAIN])
    for cmd in list(registry):
        if cmd in wd.ALLOWED_WS_COMMANDS:
            registry[cmd] = (_wrapper, None)
    monkeypatch.setitem(hass.data, ws_const.DOMAIN, registry)
    assert wd.check_ws_dispatch_compat(hass) is None


async def test_compat_probe_quiet_when_nothing_registered_yet(hass):
    # websocket_api can register after Phoenix MCP sets up. Nothing to sample is
    # not drift, so the probe must stay silent rather than guess.
    import custom_components.phoenix_mcp.ws_dispatch as wd

    assert wd._handler_contract_error({}) is None


# ---------------------------------------------------------------------------
# Lovelace helpers: "not loaded" and "unknown dashboard" are different failures.
#
# Both used to return None, so an unreadable lovelace looked like a missing
# dashboard and the write paths treated it as "nothing to preserve".
# ---------------------------------------------------------------------------


def test_dashboard_not_found_is_a_ws_dispatch_error():
    assert issubclass(wd_module.WsDashboardNotFoundError, WsDispatchError)


async def test_lovelace_not_loaded_raises(hass):
    # lovelace is not set up in this hass, so LOVELACE_DATA is absent.
    with pytest.raises(WsDispatchError) as exc:
        wd_module._lovelace_dashboard(hass, None)
    assert "not loaded" in str(exc.value)
    assert not isinstance(exc.value, wd_module.WsDashboardNotFoundError)


async def test_unknown_dashboard_raises_not_found(hass):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
    with pytest.raises(wd_module.WsDashboardNotFoundError):
        await wd_module.async_get_lovelace_config(hass, "no-such-dashboard")


async def test_known_dashboard_with_no_stored_config_returns_none(hass):
    # Distinct from both errors: the dashboard exists but is auto-generated.
    assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
    assert await wd_module.async_get_lovelace_config(hass, None) is None


async def test_save_to_unknown_dashboard_raises_not_found(hass):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
    with pytest.raises(wd_module.WsDashboardNotFoundError):
        await wd_module.async_save_lovelace_config(hass, "no-such-dashboard", {"views": []})
