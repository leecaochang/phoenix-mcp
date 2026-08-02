"""Shared test fixtures for Phoenix MCP integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def isolated_config_dir(hass, tmp_path):
    """Keep any device-YAML writes out of the shared testing config dir."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def esphome_entries(hass):
    """Fake esphome config entries at the point the tool enumerates them.

    The tool duck-types entry.state and entry.runtime_data, and the real esphome
    integration cannot load here (aioesphomeapi is an esphome requirement, not a
    Phoenix one, so a LOADED MockConfigEntry explodes on teardown unload). Only
    the enumeration is faked: the entity registry rows stay real, because
    registry-based scoping is the security-critical half and must not be mocked.
    """
    entries: list = []
    real_async_entries = hass.config_entries.async_entries
    real_async_get_entry = hass.config_entries.async_get_entry

    def _async_entries(domain=None, **kwargs):
        if domain == "esphome":
            return list(entries)
        return real_async_entries(domain, **kwargs)

    def _async_get_entry(entry_id):
        # Must agree with _async_entries: code reaching an entry by id (via an
        # entity's config_entry_id) has to see the same object as code that
        # enumerates the domain, or scoping and dispatch disagree.
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        return real_async_get_entry(entry_id)

    with (
        patch.object(hass.config_entries, "async_entries", _async_entries),
        patch.object(hass.config_entries, "async_get_entry", _async_get_entry),
    ):
        yield entries


@pytest.fixture
def esphome_dir(hass, tmp_path):
    """A realistic config/esphome directory, including the excluded entries."""
    # Imported in the body, not at module scope: the sample YAML is asserted
    # against by the module that owns it, and a conftest-level import would run
    # at collection time for every test session.
    from tests.test_mcp_esphome_tools import DEVICE_YAML, SECRETS_YAML

    root = tmp_path / "esphome"
    root.mkdir()
    (root / "rf-blaster1.yaml").write_text(DEVICE_YAML)
    (root / "secrets.yaml").write_text(SECRETS_YAML)
    (root / ".device-builder.json").write_text("{}")
    archive = root / "archive"
    archive.mkdir()
    (archive / "old-device.yaml").write_text("esphome:\n  name: old\n")
    return root


@pytest.fixture
def mock_store_data():
    return {
        "version": 1,
        "tokens": [],
        "archived_tokens": [],
        "settings": {},
    }


@pytest.fixture
def mock_store(mock_store_data):
    store = AsyncMock()
    store.async_load = AsyncMock(return_value=mock_store_data)
    store.async_save = AsyncMock()
    return store


@pytest.fixture
async def token_store(hass, mock_store):
    from custom_components.phoenix_mcp.token_store import TokenStore

    with patch(
        "custom_components.phoenix_mcp.token_store._PhoenixStore",
        return_value=mock_store,
    ):
        instance = await TokenStore.async_create(hass)
    return instance
