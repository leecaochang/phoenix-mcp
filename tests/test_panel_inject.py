"""Tests for the optional in-context profile injector registration (panel.py).

Covers async_sync_mesa_inject: it adds the extra ES-module URL only when the
mesa_inject_enabled setting is on AND the HA version meets the soft baseline, and
removes it when the setting is toggled off. The actual DOM injection is frontend
code (covered by vitest); this is just the registration gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp import panel as panel_mod
from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.token_store import GlobalSettings


def _make_hass(inject_enabled: bool) -> MagicMock:
    hass = MagicMock()
    store = MagicMock()
    store.get_settings.return_value = GlobalSettings(mesa_inject_enabled=inject_enabled)
    data = MagicMock()
    data.store = store
    hass.data = {DOMAIN: data}
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return hass


@pytest.mark.asyncio
async def test_sync_adds_module_when_enabled():
    hass = _make_hass(True)
    with patch.object(panel_mod, "add_extra_js_url") as add, \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_mesa_inject(hass)

    assert add.call_count == 1
    url = add.call_args.args[1]
    assert url.startswith("/local/phoenix-mcp/phoenix-mcp-inject.js")
    rem.assert_not_called()
    assert hass.data[panel_mod._INJECT_REGISTERED_URL_KEY] == url


@pytest.mark.asyncio
async def test_sync_does_nothing_when_disabled():
    hass = _make_hass(False)
    with patch.object(panel_mod, "add_extra_js_url") as add:
        await panel_mod.async_sync_mesa_inject(hass)
    add.assert_not_called()
    assert panel_mod._INJECT_REGISTERED_URL_KEY not in hass.data


@pytest.mark.asyncio
async def test_sync_skips_below_version_baseline():
    """Driven by raising the declared floor, not by inventing a hass attribute.

    This test used to set hass.config.version on a MagicMock, which accepts any
    attribute, so it created the value the gate was failing to read in
    production and proved only that the test's own world was consistent.
    """
    hass = _make_hass(True)
    with patch.object(panel_mod, "MESA_INJECT_MIN_HA", "9999.1.0"), \
         patch.object(panel_mod, "add_extra_js_url") as add:
        await panel_mod.async_sync_mesa_inject(hass)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_sync_removes_when_toggled_off():
    hass = _make_hass(True)
    with patch.object(panel_mod, "add_extra_js_url"), \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_mesa_inject(hass)  # add
        url = hass.data[panel_mod._INJECT_REGISTERED_URL_KEY]
        hass.data[DOMAIN].store.get_settings.return_value = GlobalSettings(
            mesa_inject_enabled=False
        )
        await panel_mod.async_sync_mesa_inject(hass)  # remove

    rem.assert_called_once_with(hass, url)
    assert panel_mod._INJECT_REGISTERED_URL_KEY not in hass.data


@pytest.mark.asyncio
async def test_remove_mesa_inject_clears_registration():
    hass = _make_hass(True)
    with patch.object(panel_mod, "add_extra_js_url"), \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_mesa_inject(hass)
        url = hass.data[panel_mod._INJECT_REGISTERED_URL_KEY]
        panel_mod.remove_mesa_inject(hass)

    rem.assert_called_once_with(hass, url)
    assert panel_mod._INJECT_REGISTERED_URL_KEY not in hass.data


# --------------------------------------------------------------------------- #
# Global Agent Chat window injection (async_sync_agentchat_inject)
# --------------------------------------------------------------------------- #

def _make_hass_agentchat(global_on: bool) -> MagicMock:
    hass = MagicMock()
    store = MagicMock()
    store.get_settings.return_value = GlobalSettings(agentcli_global=global_on)
    data = MagicMock()
    data.store = store
    hass.data = {DOMAIN: data}
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return hass


@pytest.mark.asyncio
async def test_agentchat_sync_adds_module_when_enabled():
    hass = _make_hass_agentchat(True)
    with patch.object(panel_mod, "add_extra_js_url") as add, \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_agentchat_inject(hass)

    assert add.call_count == 1
    url = add.call_args.args[1]
    assert url.startswith("/local/phoenix-mcp/phoenix-mcp-agentchat.js")
    rem.assert_not_called()
    assert hass.data[panel_mod._AGENTCHAT_REGISTERED_URL_KEY] == url


@pytest.mark.asyncio
async def test_agentchat_sync_does_nothing_when_disabled():
    hass = _make_hass_agentchat(False)
    with patch.object(panel_mod, "add_extra_js_url") as add:
        await panel_mod.async_sync_agentchat_inject(hass)
    add.assert_not_called()
    assert panel_mod._AGENTCHAT_REGISTERED_URL_KEY not in hass.data


@pytest.mark.asyncio
async def test_agentchat_sync_skips_below_version_baseline():
    """Same baseline, same correction as the injector's own version test."""
    hass = _make_hass_agentchat(True)
    with patch.object(panel_mod, "MESA_INJECT_MIN_HA", "9999.1.0"), \
         patch.object(panel_mod, "add_extra_js_url") as add:
        await panel_mod.async_sync_agentchat_inject(hass)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_agentchat_sync_removes_when_toggled_off():
    hass = _make_hass_agentchat(True)
    with patch.object(panel_mod, "add_extra_js_url"), \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_agentchat_inject(hass)
        url = hass.data[panel_mod._AGENTCHAT_REGISTERED_URL_KEY]
        hass.data[DOMAIN].store.get_settings.return_value = GlobalSettings(agentcli_global=False)
        await panel_mod.async_sync_agentchat_inject(hass)

    rem.assert_called_once_with(hass, url)
    assert panel_mod._AGENTCHAT_REGISTERED_URL_KEY not in hass.data


@pytest.mark.asyncio
async def test_remove_agentchat_inject_clears_registration():
    hass = _make_hass_agentchat(True)
    with patch.object(panel_mod, "add_extra_js_url"), \
         patch.object(panel_mod, "remove_extra_js_url") as rem:
        await panel_mod.async_sync_agentchat_inject(hass)
        url = hass.data[panel_mod._AGENTCHAT_REGISTERED_URL_KEY]
        panel_mod.remove_agentchat_inject(hass)

    rem.assert_called_once_with(hass, url)
    assert panel_mod._AGENTCHAT_REGISTERED_URL_KEY not in hass.data


# --- the HA version baseline ------------------------------------------------


def test_version_gate_actually_compares():
    """The baseline must decide something, not merely be declared.

    This gate read a Config attribute that does not exist, and its fail-open
    catch swallowed the resulting error, so it returned True on every version and
    the declared baseline was never applied. A test that only asserted "True on
    current HA" would have passed throughout, so the assertion that matters is
    the one below it: raising the floor above the running version must flip it.
    """
    from custom_components.phoenix_mcp import panel as panel_mod

    assert panel_mod._inject_version_ok() is True

    with patch.object(panel_mod, "MESA_INJECT_MIN_HA", "9999.1.0"):
        assert panel_mod._inject_version_ok() is False


def test_version_gate_fails_open_on_an_unparseable_baseline():
    """A version that cannot be parsed leaves the feature enabled.

    The in-page feature detection self-disables on a DOM it does not recognise,
    so the worst case of a wrong answer here is a script that adds nothing;
    withholding it on a parse failure would disable a working feature instead.
    """
    from custom_components.phoenix_mcp import panel as panel_mod

    with patch.object(panel_mod, "MESA_INJECT_MIN_HA", "not-a-version"):
        assert panel_mod._inject_version_ok() is True


def test_the_baseline_is_below_the_home_assistant_under_test():
    """Otherwise the suite would be exercising the withheld path everywhere.

    A floor above the pinned Home Assistant would silently turn every injector
    test into a test of the disabled branch.
    """
    from awesomeversion import AwesomeVersion
    from homeassistant.const import __version__ as ha_version

    from custom_components.phoenix_mcp.const import MESA_INJECT_MIN_HA

    assert AwesomeVersion(ha_version) >= AwesomeVersion(MESA_INJECT_MIN_HA)
