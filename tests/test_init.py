"""Tests for integration setup/unload orchestration in __init__.py.

These cover async_setup_entry's wiring decisions, not the HA primitives it drives:
the kill-switch gate on proxy/MCP route registration (admin routes + panel always
register), MESA degrading to off without failing setup, and async_unload_entry
cleaning up hass.data. Real timers and background tasks are neutralized so the test
asserts the orchestration without scheduling anything that would linger past it.
The end-to-end route wiring is covered separately by the real-HTTP scaffold test.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, callback
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.phoenix_mcp as phoenix_init
from custom_components.phoenix_mcp.const import DOMAIN


def _settings(kill_switch: bool = False):
    return SimpleNamespace(
        kill_switch=kill_switch,
        audit_log_maxlen=1000,
        mesa_mode="off",
        audit_flush_interval=0,
        voice_agent_enabled=False,
    )


def _mock_store(kill_switch: bool = False) -> MagicMock:
    store = MagicMock()
    store.get_settings.return_value = _settings(kill_switch)
    store.list_tokens.return_value = []
    store.get_pending_approvals.return_value = []
    store.async_flush_last_used = AsyncMock()
    store.async_lock = asyncio.Lock()
    return store


def _fake_bg(coro, name=None):
    # Don't schedule background loops in the test; close the coroutine so there is
    # no "never awaited" warning, and hand back a cancel-able stand-in.
    coro.close()
    return MagicMock(cancel=MagicMock())


async def _run_setup(hass: HomeAssistant, *, kill_switch: bool = False, mesa_fail: bool = False):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    hass.http = MagicMock()
    store = _mock_store(kill_switch)

    mesa = AsyncMock(side_effect=RuntimeError("boom")) if mesa_fail else AsyncMock(return_value=None)

    with patch.object(phoenix_init.TokenStore, "async_create", AsyncMock(return_value=store)), \
         patch("custom_components.phoenix_mcp.mesa.async_setup_mesa", mesa), \
         patch("custom_components.phoenix_mcp.panel.async_register_phoenix_panel", AsyncMock()), \
         patch("custom_components.phoenix_mcp.panel.async_sync_mesa_inject", AsyncMock()), \
         patch("custom_components.phoenix_mcp.panel.async_sync_agentchat_inject", AsyncMock()), \
         patch("custom_components.phoenix_mcp.ws_dispatch.check_ws_dispatch_compat", return_value=None), \
         patch.object(phoenix_init, "async_track_time_interval", MagicMock(return_value=MagicMock())), \
         patch.object(hass, "async_create_background_task", _fake_bg), \
         patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        result = await phoenix_init.async_setup_entry(hass, entry)

    return result, entry, hass.http.register_view.call_count


async def test_setup_registers_routes_when_kill_switch_off(hass: HomeAssistant):
    result, _entry, view_count = await _run_setup(hass, kill_switch=False)
    assert result is True
    data = hass.data[DOMAIN]
    assert data.routes_registered is True
    # Admin views plus the proxy/MCP/skill views were all registered.
    from custom_components.phoenix_mcp.admin_view import ALL_ADMIN_VIEWS
    assert view_count > len(ALL_ADMIN_VIEWS)


async def test_setup_skips_client_routes_when_kill_switch_on(hass: HomeAssistant):
    result, _entry, view_count = await _run_setup(hass, kill_switch=True)
    assert result is True
    data = hass.data[DOMAIN]
    # Client routes are NOT registered, but the helper is wired for later re-enable.
    assert data.routes_registered is False
    assert callable(data.async_register_routes)
    # Admin views are still registered (kill switch never hides the admin surface).
    # This includes the agentCLI provider-config admin views, which are also
    # kill-switch-immune; only the agentCLI streaming chat view is gated.
    from custom_components.phoenix_mcp.admin_view import ALL_ADMIN_VIEWS
    from custom_components.phoenix_mcp.agentcli import ALL_AGENTCLI_ADMIN_VIEWS
    assert view_count == len(ALL_ADMIN_VIEWS) + len(ALL_AGENTCLI_ADMIN_VIEWS)


async def test_setup_degrades_when_mesa_fails(hass: HomeAssistant):
    result, _entry, _ = await _run_setup(hass, mesa_fail=True)
    assert result is True
    # MESA setup raising must not block startup; the runtime degrades to off.
    assert hass.data[DOMAIN].mesa is None


async def test_unload_removes_data(hass: HomeAssistant):
    await _run_setup(hass, kill_switch=False)
    assert DOMAIN in hass.data
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    with patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel"), \
         patch("custom_components.phoenix_mcp.panel.remove_mesa_inject"), \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject"), \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        ok = await phoenix_init.async_unload_entry(hass, entry)

    assert ok is True
    assert DOMAIN not in hass.data


async def test_setup_reconciles_approvals_interrupted_by_the_last_shutdown(hass: HomeAssistant):
    """Startup must resolve approvals whose executor never reported back.

    An approval marked as executing and still pending is one whose side effect
    may already have applied. Leaving it pending offers the admin a button that
    could apply a service call or a config write a second time, which is the one
    outcome with no recovery. Pinned at the wiring level because the reconcile
    function passing its own unit tests says nothing about setup calling it.
    """
    with patch(
        "custom_components.phoenix_mcp.approvals.async_reconcile_interrupted_approvals",
        AsyncMock(return_value=[]),
    ) as reconcile:
        await _run_setup(hass, kill_switch=False)

    reconcile.assert_awaited_once()


# --- one-shot event listeners ------------------------------------------------


async def test_a_fired_one_shot_listener_is_not_removed_again_on_unload(hass: HomeAssistant, caplog):
    """LIVE-FOUND on a config-entry reload.

    `hass.bus.async_listen_once` removes its own listener when the event fires,
    so passing its remove-callback to `entry.async_on_unload` asks HA to remove
    the same listener twice. The second call raises
    `ValueError: list.remove(x): x not in list`, which HA logs as "Unable to
    remove unknown job listener" with a Phoenix traceback.

    Only reproducible in the reload-after-a-cold-start order: the event has to
    have FIRED before the unload. A happy-path setup/unload test never fires it,
    which is why this survived. Both `homeassistant_started` (suggestion priming)
    and `homeassistant_stop` (the final flush) had the shape.
    """
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    seen: list[str] = []

    @callback
    def _listener(_event=None) -> None:
        seen.append("fired")

    phoenix_init._listen_once_until_unload(
        hass, entry, EVENT_HOMEASSISTANT_STARTED, _listener)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert seen == ["fired"]

    # Runs the on-unload callbacks, which is the mechanism under test.
    #
    # Asserted on the LOG, not on a raised exception: HA catches the ValueError
    # inside _async_remove_listener and logs it, so an exception-based assertion
    # passes whether or not the bug is present. That is how the first version of
    # this test survived the mutation that restores the defect.
    caplog.clear()
    await entry._async_process_on_unload(hass)
    await hass.async_block_till_done()

    assert "Unable to remove unknown job listener" not in caplog.text, (
        "the already-fired listener was removed a second time on unload; this is "
        "the ERROR + Phoenix traceback seen in the operator's log on every reload"
    )


async def test_an_unfired_one_shot_listener_is_still_removed_on_unload(hass: HomeAssistant):
    # The other half: if the event never arrives, the listener must NOT be left
    # behind, or a reload would stack a second one and prime twice.
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    seen: list[str] = []

    @callback
    def _listener(_event=None) -> None:
        seen.append("fired")

    phoenix_init._listen_once_until_unload(
        hass, entry, EVENT_HOMEASSISTANT_STARTED, _listener)

    await entry._async_process_on_unload(hass)
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert seen == [], "the listener outlived the entry that registered it"


async def test_a_coroutine_one_shot_listener_is_awaited(hass: HomeAssistant):
    # _on_stop is async. Wrapping it in a sync @callback would leave a coroutine
    # unawaited and silently skip the shutdown flush.
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    seen: list[str] = []

    async def _listener(_event=None) -> None:
        seen.append("awaited")

    phoenix_init._listen_once_until_unload(
        hass, entry, EVENT_HOMEASSISTANT_STOP, _listener)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert seen == ["awaited"]


# --- unload is failure-tolerant ---------------------------------------------
#
# Every step before the platform unload is best-effort. These pin that, because
# the failure they prevent is silent and total: shutting_down is set FIRST and
# helpers gates every token request on it, so an exception on the way out used to
# leave the entry loaded with every route registered and every request answering
# 503 until the next restart. Happy-path unload tests cannot see any of it.


async def test_unload_survives_an_unwritable_audit_store(hass: HomeAssistant):
    await _run_setup(hass, kill_switch=False)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    data = hass.data[DOMAIN]
    data.audit.async_save = AsyncMock(side_effect=OSError("disk full"))

    with patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel"), \
         patch("custom_components.phoenix_mcp.panel.remove_mesa_inject"), \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject"), \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)) as unload:
        ok = await phoenix_init.async_unload_entry(hass, entry)

    assert ok is True
    # The teardown ran to completion rather than aborting at the save.
    assert unload.await_count == 1
    assert DOMAIN not in hass.data


async def test_unload_survives_a_failing_panel_removal(hass: HomeAssistant):
    await _run_setup(hass, kill_switch=False)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    with patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel",
               side_effect=RuntimeError("panel gone")), \
         patch("custom_components.phoenix_mcp.panel.remove_mesa_inject"), \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject"), \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)) as unload:
        ok = await phoenix_init.async_unload_entry(hass, entry)

    assert ok is True
    assert unload.await_count == 1
    assert DOMAIN not in hass.data


async def test_one_failing_frontend_removal_does_not_skip_the_others(hass: HomeAssistant):
    # They are three unrelated removals. Sharing one try meant a failure in the
    # first silently skipped the rest, leaving modules injected into every HA
    # page with nothing left to service them.
    await _run_setup(hass, kill_switch=False)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    with patch("custom_components.phoenix_mcp.panel.remove_mesa_inject",
               side_effect=RuntimeError("stuck")) as mesa, \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject") as chat, \
         patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel") as panel, \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await phoenix_init.async_unload_entry(hass, entry) is True

    mesa.assert_called_once()
    chat.assert_called_once()
    panel.assert_called_once()


async def test_a_failed_unload_keeps_the_frontend(hass: HomeAssistant):
    """HA keeps a failed unload LOADED, so its admin UI has to survive it.

    Removing the panel before knowing the outcome left a still-running
    integration with no administrative surface, recoverable only by a later
    successful reload or a restart. The token routes stay live either way, so
    this is the operator losing the one control that could fix the situation.
    """
    await _run_setup(hass, kill_switch=False)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    with patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel") as panel, \
         patch("custom_components.phoenix_mcp.panel.remove_mesa_inject") as mesa, \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject") as chat, \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert await phoenix_init.async_unload_entry(hass, entry) is False

    panel.assert_not_called()
    mesa.assert_not_called()
    chat.assert_not_called()
    assert hass.data[DOMAIN].shutting_down is False


async def test_a_failed_unload_leaves_the_integration_usable(hass: HomeAssistant):
    # HA keeps a failed unload LOADED. Leaving shutting_down set would 503 every
    # token request against an integration Home Assistant still considers up.
    await _run_setup(hass, kill_switch=False)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    with patch("custom_components.phoenix_mcp.panel.remove_phoenix_panel"), \
         patch("custom_components.phoenix_mcp.panel.remove_mesa_inject"), \
         patch("custom_components.phoenix_mcp.panel.remove_agentchat_inject"), \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        ok = await phoenix_init.async_unload_entry(hass, entry)

    assert ok is False
    assert DOMAIN in hass.data
    assert hass.data[DOMAIN].shutting_down is False


# --- startup compat probes (system_log shape, template env construction) -----


def test_system_log_probe_skips_when_absent(hass: HomeAssistant):
    # Absent = not loaded / not enabled / loads after Phoenix MCP. None of those are
    # drift, so the probe must stay silent rather than false-positive on order.
    from custom_components.phoenix_mcp.helpers import check_system_log_compat

    hass.data.pop("system_log", None)
    assert check_system_log_compat(hass) is None


def test_system_log_probe_passes_on_current_shape(hass: HomeAssistant):
    from custom_components.phoenix_mcp.helpers import check_system_log_compat

    hass.data["system_log"] = SimpleNamespace(records={})
    assert check_system_log_compat(hass) is None


def test_system_log_probe_reports_shape_drift(hass: HomeAssistant):
    from custom_components.phoenix_mcp.helpers import check_system_log_compat

    hass.data["system_log"] = SimpleNamespace(handler=object())  # no .records
    reason = check_system_log_compat(hass)
    assert reason is not None and "records" in reason

    hass.data["system_log"] = SimpleNamespace(records=[1, 2])  # not a mapping
    reason = check_system_log_compat(hass)
    assert reason is not None and "mapping" in reason


def test_template_sandbox_audit_warns_when_env_construction_fails(caplog, monkeypatch):
    # If HA's TemplateEnvironment changes shape, every render_template call will
    # fail; the audit must surface that once at startup as a warning, not debug.
    import custom_components.phoenix_mcp.helpers as helpers_mod

    monkeypatch.setattr(helpers_mod, "safe_template_env", MagicMock(side_effect=TypeError("shape")))
    phoenix_init._audit_template_sandbox()
    assert any(
        "template sandbox environment failed to construct" in r.message
        for r in caplog.records if r.levelname == "WARNING"
    )
