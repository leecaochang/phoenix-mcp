"""Config-entry metadata, lifecycle, approval, and MESA safety tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _CONFIG_ENTRY_CONTEXT_FINGERPRINT,
    _CONFIG_ENTRY_METADATA_SNAPSHOT,
    _call_tool,
    async_execute_approved_tool,
    async_restore_version,
)
from custom_components.phoenix_mcp.policy_engine import (
    config_entry_registry_context,
    resolve_config_entry_registry_write,
)
from custom_components.phoenix_mcp.mesa import async_setup_mesa
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.token_store import (
    GlobalSettings,
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
)


def _body(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _profile(key: str, mode: str, reason: str = "Integration safety test") -> SemanticProfile:
    return SemanticProfile.from_dict(
        key,
        {
            "semantic_profile": {
                "operational_boundaries": {
                    "control_mode": mode,
                    "control_reason": reason,
                }
            }
        },
        default_origin=MetadataOrigin.USER,
    )


def _token(
    env: dict,
    *,
    cap: str = "allow",
    device_state: str = "GREEN",
    domain_state: str = "GREEN",
    entity_states: dict[str, str] | None = None,
) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="integration-write-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_integration_write=cap,
        permissions=PermissionTree(
            devices={env["device_id"]: PermissionNode(state=device_state)},
            domains={
                "light": PermissionNode(state=domain_state),
                "sensor": PermissionNode(state=domain_state),
            },
            entities={
                entity_id: PermissionNode(state=state)
                for entity_id, state in (entity_states or {}).items()
            },
        ),
    )


async def _data(hass: HomeAssistant, mesa_mode: str = "off") -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.async_lock = asyncio.Lock()
    store.async_save = AsyncMock()
    store.get_settings.return_value = GlobalSettings(mesa_mode=mesa_mode)
    store.list_tokens.return_value = []
    store.get_entity_hints.return_value = {}
    store.get_pending_approvals.return_value = []
    data = PhoenixData(
        store=store,
        rate_limiter=MagicMock(),
        audit=MagicMock(),
        hass=hass,
        mesa=await async_setup_mesa(hass, mesa_mode),
    )
    hass.data[DOMAIN] = data
    return data


@pytest.fixture
def integration_env(hass: HomeAssistant) -> dict:
    entry = MockConfigEntry(
        domain="test_integration",
        entry_id="integration-write-owner",
        title="Original integration",
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test_integration", "integration-write-device")},
        name="Integration write device",
    )
    entity_ids = []
    for domain in ("light", "sensor"):
        registry_entry = er.async_get(hass).async_get_or_create(
            domain,
            "test_integration",
            f"integration-{domain}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"integration_{domain}",
        )
        hass.states.async_set(registry_entry.entity_id, "off")
        entity_ids.append(registry_entry.entity_id)
    return {
        "entry": entry,
        "entry_id": entry.entry_id,
        "device_id": device.id,
        "entity_ids": sorted(entity_ids),
    }


async def test_scoped_write_requires_every_entity_and_exact_device(
    hass: HomeAssistant, integration_env
):
    data = await _data(hass)
    for token in (
        _token(integration_env, device_state="YELLOW"),
        _token(
            integration_env,
            entity_states={integration_env["entity_ids"][0]: "YELLOW"},
        ),
    ):
        context = config_entry_registry_context(integration_env["entry_id"], hass)
        assert context is not None
        assert context.device_ids == (integration_env["device_id"],)
        assert token.pass_through is False
        assert resolve_config_entry_registry_write(
            integration_env["entry_id"], token, hass
        ).value != "write"
        content, outcome, _ = await _call_tool(
            "set_integration",
            {"entry_id": integration_env["entry_id"], "changes": {"title": "Blocked"}},
            token,
            hass,
            data,
        )
        assert outcome == "not_found"
        assert "Integration not found" in content["content"][0]["text"]


async def test_resource_less_scoped_entry_fails_closed(hass: HomeAssistant):
    entry = MockConfigEntry(domain="empty_integration", entry_id="empty-entry")
    entry.add_to_hass(hass)
    data = await _data(hass)
    env = {"device_id": "unused"}
    _content, outcome, _ = await _call_tool(
        "set_integration",
        {"entry_id": entry.entry_id, "changes": {"title": "No resources"}},
        _token(env),
        hass,
        data,
    )
    assert outcome == "not_found"


async def test_metadata_noop_skips_gate_and_version(
    hass: HomeAssistant, integration_env
):
    data = await _data(hass)
    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        content, outcome, _ = await _call_tool(
            "set_integration",
            {
                "entry_id": integration_env["entry_id"],
                "changes": {"title": integration_env["entry"].title},
            },
            _token(integration_env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "allowed"
    assert _body(content)["changed"] is False
    assert data.versions.list_for("config_entry", integration_env["entry_id"]) == []
    gate.assert_not_awaited()


async def test_metadata_update_polling_reload_and_version_restore(
    hass: HomeAssistant, integration_env
):
    entry = integration_env["entry"]
    entry.mock_state(hass, ConfigEntryState.LOADED)
    data = await _data(hass)
    token = _token(integration_env)
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_mock:
        content, outcome, _ = await _call_tool(
            "set_integration",
            {
                "entry_id": entry.entry_id,
                "changes": {
                    "title": "Updated integration",
                    "pref_disable_new_entities": True,
                    "pref_disable_polling": True,
                },
            },
            token,
            hass,
            data,
        )
    assert outcome == "allowed"
    assert _body(content)["reload_succeeded"] is True
    reload_mock.assert_awaited_once_with(entry.entry_id)
    version = data.versions.list_for("config_entry", entry.entry_id)[0]
    assert version.before["snapshot_type"] == _CONFIG_ENTRY_METADATA_SNAPSHOT
    assert version.after["title"] == "Updated integration"

    hass.config_entries.async_update_entry(entry, title="Changed again")
    restored_content, restored_outcome, _ = await async_restore_version(
        version, "admin-integration", hass, data, side="before"
    )
    assert restored_outcome == "allowed", restored_content
    assert entry.title == "Original integration"
    assert data.versions.list_for("config_entry", entry.entry_id)[0].action == "rollback"


async def test_admin_restore_satisfies_confirm_but_not_read_only(
    hass: HomeAssistant, integration_env
):
    entry = integration_env["entry"]
    data = await _data(hass, "enforced")
    current = {
        "snapshot_type": _CONFIG_ENTRY_METADATA_SNAPSHOT,
        "title": entry.title,
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "disabled_by": None,
    }
    restore_confirmed = SimpleNamespace(
        resource_type="config_entry",
        resource_id=entry.entry_id,
        before={**current, "title": "Admin confirmed restore"},
        after=current,
    )
    content, outcome, _ = await async_restore_version(
        restore_confirmed, "admin-integration", hass, data, side="before"
    )
    assert outcome == "allowed", content
    assert entry.title == "Admin confirmed restore"

    data.mesa.store.set_integration_profile(
        entry.domain,
        _profile(entry.domain, "read_only", "Restore remains prohibited"),
    )
    restore_blocked = SimpleNamespace(
        resource_type="config_entry",
        resource_id=entry.entry_id,
        before={**current, "title": "Must not restore"},
        after={**current, "title": entry.title},
    )
    content, outcome, _ = await async_restore_version(
        restore_blocked, "admin-integration", hass, data, side="before"
    )
    assert outcome == "denied"
    assert "read_only" in content["content"][0]["text"]
    assert entry.title == "Admin confirmed restore"


async def test_title_only_does_not_reload(hass: HomeAssistant, integration_env):
    data = await _data(hass)
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_mock:
        content, outcome, _ = await _call_tool(
            "set_integration",
            {"entry_id": integration_env["entry_id"], "changes": {"title": "Title only"}},
            _token(integration_env),
            hass,
            data,
        )
    assert outcome == "allowed"
    assert _body(content)["reload_attempted"] is False
    reload_mock.assert_not_awaited()


async def test_enable_disable_user_only_and_truthful_reload(
    hass: HomeAssistant, integration_env
):
    entry = integration_env["entry"]
    data = await _data(hass)
    with patch.object(
        hass.config_entries, "async_set_disabled_by", new=AsyncMock(return_value=False)
    ):
        content, outcome, _ = await _call_tool(
            "set_integration_enabled",
            {"entry_id": entry.entry_id, "enabled": False},
            _token(integration_env),
            hass,
            data,
        )
    assert outcome == "allowed"
    assert _body(content)["reload_succeeded"] is False
    assert _body(content)["requires_restart"] is True

    entry.disabled_by = MagicMock(value="integration")
    _content, outcome, _ = await _call_tool(
        "set_integration_enabled",
        {"entry_id": entry.entry_id, "enabled": True},
        _token(integration_env),
        hass,
        data,
    )
    assert outcome == "denied"


async def test_reload_refuses_disabled_and_records_no_version(
    hass: HomeAssistant, integration_env
):
    entry = integration_env["entry"]
    entry.disabled_by = ConfigEntryDisabler.USER
    data = await _data(hass)
    _content, outcome, _ = await _call_tool(
        "reload_integration",
        {"entry_id": entry.entry_id},
        _token(integration_env),
        hass,
        data,
    )
    assert outcome == "denied"
    assert data.versions.list_for("config_entry", entry.entry_id) == []


@pytest.mark.parametrize(
    "mesa_mode,control_mode,expected",
    [
        ("enforced", "read_only", "denied"),
        ("enforced", "prohibited", "denied"),
        ("advisory", "prohibited", "allowed"),
    ],
)
async def test_integration_scope_mesa_controls_rename_and_reload(
    hass: HomeAssistant,
    integration_env,
    mesa_mode,
    control_mode,
    expected,
):
    data = await _data(hass, mesa_mode)
    data.mesa.store.set_integration_profile(
        integration_env["entry"].domain,
        _profile(integration_env["entry"].domain, control_mode),
    )
    token = _token(integration_env)
    for tool_name, args in (
        (
            "set_integration",
            {"entry_id": integration_env["entry_id"], "changes": {"title": "MESA title"}},
        ),
        ("reload_integration", {"entry_id": integration_env["entry_id"]}),
    ):
        with patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ):
            content, outcome, _ = await _call_tool(
                tool_name, args, token, hass, data
            )
        assert outcome == expected
        if expected == "allowed":
            assert _body(content)["mesa_advisory"]


async def test_integration_scope_read_only_blocks_disable_and_enable(
    hass: HomeAssistant, integration_env
):
    data = await _data(hass, "enforced")
    data.mesa.store.set_integration_profile(
        integration_env["entry"].domain,
        _profile(integration_env["entry"].domain, "read_only"),
    )
    token = _token(integration_env)
    set_disabled = AsyncMock(return_value=True)
    with patch.object(
        hass.config_entries, "async_set_disabled_by", new=set_disabled
    ):
        _content, outcome, _ = await _call_tool(
            "set_integration_enabled",
            {"entry_id": integration_env["entry_id"], "enabled": False},
            token,
            hass,
            data,
        )
    assert outcome == "denied"
    set_disabled.assert_not_awaited()

    integration_env["entry"].disabled_by = ConfigEntryDisabler.USER
    with patch.object(
        hass.config_entries, "async_set_disabled_by", new=set_disabled
    ):
        _content, outcome, _ = await _call_tool(
            "set_integration_enabled",
            {"entry_id": integration_env["entry_id"], "enabled": True},
            token,
            hass,
            data,
        )
    assert outcome == "denied"
    set_disabled.assert_not_awaited()


async def test_entityless_entry_fails_closed_when_mesa_is_active(
    hass: HomeAssistant
):
    entry = MockConfigEntry(
        domain="entityless_integration",
        entry_id="entityless-mesa-entry",
        title="Entity-less",
    )
    entry.add_to_hass(hass)
    data = await _data(hass, "enforced")
    token = TokenRecord(
        id=str(uuid.uuid4()),
        name="entityless-pass-through",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        pass_through=True,
        cap_integration_write="allow",
    )
    content, outcome, _ = await _call_tool(
        "set_integration",
        {"entry_id": entry.entry_id, "changes": {"title": "Still blocked"}},
        token,
        hass,
        data,
    )
    assert outcome == "denied"
    assert "unresolved_config_entry_context" in content["content"][0]["text"]
    assert entry.title == "Entity-less"


async def test_capability_approval_pins_membership_and_mesa(
    hass: HomeAssistant, integration_env
):
    data = await _data(hass, "enforced")
    data.mesa.store.set_integration_profile(
        integration_env["entry"].domain,
        _profile(integration_env["entry"].domain, "confirm"),
    )
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _ = await _call_tool(
            "reload_integration",
            {"entry_id": integration_env["entry_id"]},
            _token(integration_env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]
    assert isinstance(approved_args[_CONFIG_ENTRY_CONTEXT_FINGERPRINT], str)

    joined = er.async_get(hass).async_get_or_create(
        "switch",
        "test_integration",
        "approval-window-member",
        config_entry=integration_env["entry"],
        device_id=integration_env["device_id"],
        suggested_object_id="approval_window_member",
    )
    hass.states.async_set(joined.entity_id, "off")
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_mock:
        content, outcome, _ = await async_execute_approved_tool(
            "reload_integration",
            approved_args,
            _token(integration_env),
            hass,
            data,
        )
    assert outcome == "denied"
    assert "changed after approval" in content["content"][0]["text"]
    reload_mock.assert_not_awaited()
