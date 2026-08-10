"""Config-entry removal previews, cleanup observations, and safety races."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _CONFIG_ENTRY_CONTEXT_FINGERPRINT,
    _CONFIG_ENTRY_DELETE_SNAPSHOT,
    _build_diff_remove_integration,
    _call_tool,
    _config_entry_action_decision,
    async_execute_approved_tool,
    async_restore_version,
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


def _profile(key: str, mode: str) -> SemanticProfile:
    return SemanticProfile.from_dict(
        key,
        {
            "semantic_profile": {
                "operational_boundaries": {
                    "control_mode": mode,
                    "control_reason": "Integration removal safety test",
                }
            }
        },
        default_origin=MetadataOrigin.USER,
    )


def _token(env: dict, *, cap: str = "allow", device_state: str = "GREEN") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="integration-remove-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_integration_write=cap,
        permissions=PermissionTree(
            devices={env["device_id"]: PermissionNode(state=device_state)},
            domains={"sensor": PermissionNode(state="GREEN")},
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


def _environment(hass: HomeAssistant, *, entry_id: str = "remove-entry") -> dict:
    entry = MockConfigEntry(
        domain="test_integration",
        entry_id=entry_id,
        title="Removal integration",
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(entry.domain, f"{entry_id}-device")},
        name="Removal integration device",
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        entry.domain,
        f"{entry_id}-entity",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=f"{entry_id}_entity",
    )
    hass.states.async_set(entity.entity_id, "1")
    return {
        "entry": entry,
        "entry_id": entry.entry_id,
        "device_id": device.id,
        "entity_id": entity.entity_id,
    }


async def test_preview_reports_exact_consumers_shared_owners_and_phoenix_refs(
    hass: HomeAssistant,
):
    env = _environment(hass)
    second = MockConfigEntry(
        domain="second_integration", entry_id="remaining-owner", title="Remaining owner"
    )
    second.add_to_hass(hass)
    dr.async_get(hass).async_update_device(
        env["device_id"], add_config_entry_id=second.entry_id
    )
    data = await _data(hass, "enforced")
    token = _token(env)
    data.store.list_tokens.return_value = [token]
    data.store.get_entity_hints.return_value = {env["entity_id"]: "Retained hint"}
    data.store.get_pending_approvals.return_value = [{
        "id": "pending-ref",
        "status": "pending",
        "tool_name": "set_entity",
        "args": {"entity_id": env["entity_id"]},
    }]
    data.mesa.store.set(env["entity_id"], _profile(env["entity_id"], "autonomous"))
    data.mesa.store.set_device_profile(
        env["device_id"], _profile(env["device_id"], "autonomous")
    )
    data.mesa.store.set_integration_profile(
        env["entry"].domain, _profile(env["entry"].domain, "autonomous")
    )
    decision = _config_entry_action_decision(
        data,
        token,
        hass,
        env["entry"],
        actions=["remove"],
        service_data={},
        session_id="preview",
    )
    assert not isinstance(decision, tuple)
    relationships = {"consumers": [{"kind": "automation", "id": "consumer"}]}
    with (
        patch(
            "custom_components.phoenix_mcp.mcp_view._registry_relationships_preview",
            AsyncMock(return_value=relationships),
        ),
        patch(
            "custom_components.phoenix_mcp.mcp_view.device_config_entry_ids",
            return_value=[env["entry_id"], second.entry_id],
        ),
    ):
        diff = await _build_diff_remove_integration(
            {"entry_id": env["entry_id"]}, token, hass, data, decision
        )
    preview = diff["preview"]
    assert preview["affected_entities"] == [env["entity_id"]]
    assert preview["relationships"] == relationships
    assert preview["shared_devices"][0]["remaining_owners"][0]["entry_id"] == second.entry_id
    assert preview["permission_references"]["entities"] == []
    assert preview["permission_references"]["devices"][0]["device_id"] == env["device_id"]
    assert preview["global_hints"] == [env["entity_id"]]
    assert preview["mesa_profiles"]["entities"][0]["entity_id"] == env["entity_id"]
    assert preview["mesa_profiles"]["devices"][0]["device_id"] == env["device_id"]
    assert preview["mesa_profiles"]["integration"] is not None
    assert preview["pending_approvals"] == [
        {"approval_id": "pending-ref", "tool_name": "set_entity"}
    ]


async def test_success_uses_ha_remove_and_records_non_restorable_cleanup(
    hass: HomeAssistant,
):
    env = _environment(hass)
    data = await _data(hass)
    original_remove = hass.config_entries.async_remove
    with patch.object(
        hass.config_entries,
        "async_remove",
        new=AsyncMock(wraps=original_remove),
    ) as remove_mock:
        content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env),
            hass,
            data,
        )
    assert outcome == "allowed", content
    remove_mock.assert_awaited_once_with(env["entry_id"])
    body = _body(content)
    assert body["removed"] is True
    assert body["observed_cleanup"]["entities_removed"] == [env["entity_id"]]
    assert body["observed_cleanup"]["devices_removed"] == [env["device_id"]]
    assert hass.config_entries.async_get_entry(env["entry_id"]) is None

    version = data.versions.list_for("config_entry", env["entry_id"])[0]
    assert version.action == "delete"
    assert version.before["snapshot_type"] == _CONFIG_ENTRY_DELETE_SNAPSHOT
    assert version.before["restorable"] is False
    assert "data" not in version.before
    assert "options" not in version.before
    restore_content, restore_outcome, _ = await async_restore_version(
        version, "admin-remove", hass, data
    )
    assert restore_outcome == "invalid_request"
    assert "cannot be restored" in restore_content["content"][0]["text"]


async def test_shared_device_survives_with_remaining_owner(hass: HomeAssistant):
    env = _environment(hass)
    second = MockConfigEntry(
        domain="second_integration", entry_id="shared-owner", title="Shared owner"
    )
    second.add_to_hass(hass)
    dr.async_get(hass).async_update_device(
        env["device_id"], add_config_entry_id=second.entry_id
    )
    data = await _data(hass)
    original_private_remove = hass.config_entries._async_remove

    async def legacy_shared_remove(entry_id: str):
        await original_private_remove(entry_id)
        return {"require_restart": False}

    def legacy_owners(_device):
        if hass.config_entries.async_get_entry(env["entry_id"]) is None:
            return [second.entry_id]
        return [env["entry_id"], second.entry_id]

    with (
        patch.object(hass.config_entries, "async_remove", new=legacy_shared_remove),
        patch(
            "custom_components.phoenix_mcp.mcp_view.device_config_entry_ids",
            side_effect=legacy_owners,
        ),
    ):
        content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env),
            hass,
            data,
        )
    assert outcome == "allowed", content
    observed = _body(content)["observed_cleanup"]
    assert observed["devices_removed"] == []
    assert observed["devices_remaining"][0]["remaining_owners"][0]["entry_id"] == second.entry_id
    assert observed["device_ownership_anomalies"] == []


async def test_exception_reports_observed_unchanged_state_without_version(
    hass: HomeAssistant,
):
    env = _environment(hass)
    data = await _data(hass)
    with patch.object(
        hass.config_entries,
        "async_remove",
        new=AsyncMock(side_effect=RuntimeError("remove failed")),
    ):
        content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env),
            hass,
            data,
        )
    assert outcome == "denied"
    error = _body(content)
    assert error["home_assistant_raised"] is True
    assert error["observed_cleanup"]["entry_removed"] is False
    assert error["observed_cleanup"]["entities_remaining"] == [env["entity_id"]]
    assert data.versions.list_for("config_entry", env["entry_id"]) == []


async def test_exception_after_removal_reports_actual_success(hass: HomeAssistant):
    env = _environment(hass)
    data = await _data(hass)
    original_remove = hass.config_entries.async_remove

    async def remove_then_raise(entry_id: str):
        await original_remove(entry_id)
        raise RuntimeError("late failure")

    with patch.object(hass.config_entries, "async_remove", new=remove_then_raise):
        content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env),
            hass,
            data,
        )
    assert outcome == "allowed", content
    body = _body(content)
    assert body["home_assistant_raised"] is True
    assert body["removed"] is True
    assert body["warning"]


async def test_pending_removal_revalidates_membership_and_permission(
    hass: HomeAssistant,
):
    env = _environment(hass)
    data = await _data(hass)
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]
    assert isinstance(approved_args[_CONFIG_ENTRY_CONTEXT_FINGERPRINT], str)

    joined = er.async_get(hass).async_get_or_create(
        "sensor",
        env["entry"].domain,
        "joined-pending-removal",
        config_entry=env["entry"],
        device_id=env["device_id"],
    )
    hass.states.async_set(joined.entity_id, "2")
    with patch.object(hass.config_entries, "async_remove", new=AsyncMock()) as remove_mock:
        content, outcome, _ = await async_execute_approved_tool(
            "remove_integration", approved_args, _token(env), hass, data
        )
    assert outcome == "denied"
    assert "changed after approval" in content["content"][0]["text"]
    remove_mock.assert_not_awaited()

    _content, outcome, _ = await async_execute_approved_tool(
        "remove_integration",
        approved_args,
        _token(env, device_state="RED"),
        hass,
        data,
    )
    assert outcome == "not_found"


async def test_new_read_only_profile_invalidates_pending_removal(
    hass: HomeAssistant,
):
    env = _environment(hass)
    data = await _data(hass, "enforced")
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]
    data.mesa.store.set_integration_profile(
        env["entry"].domain, _profile(env["entry"].domain, "read_only")
    )
    with patch.object(hass.config_entries, "async_remove", new=AsyncMock()) as remove_mock:
        content, outcome, _ = await async_execute_approved_tool(
            "remove_integration", approved_args, _token(env), hass, data
        )
    assert outcome == "denied"
    assert "changed after approval" in content["content"][0]["text"]
    remove_mock.assert_not_awaited()


async def test_entity_less_and_phoenix_entries_fail_closed(hass: HomeAssistant):
    empty = MockConfigEntry(domain="empty_integration", entry_id="empty-removal")
    empty.add_to_hass(hass)
    data = await _data(hass, "enforced")
    env = {"device_id": "unused"}
    content, outcome, _ = await _call_tool(
        "remove_integration",
        {"entry_id": empty.entry_id},
        _token(env),
        hass,
        data,
    )
    assert outcome == "not_found"
    assert "Integration not found" in content["content"][0]["text"]

    pass_through = TokenRecord(
        id=str(uuid.uuid4()),
        name="entityless-remove-pass-through",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        pass_through=True,
        cap_integration_write="allow",
    )
    content, outcome, _ = await _call_tool(
        "remove_integration",
        {"entry_id": empty.entry_id},
        pass_through,
        hass,
        data,
    )
    assert outcome == "denied"
    assert "unresolved_config_entry_context" in content["content"][0]["text"]
    assert hass.config_entries.async_get_entry(empty.entry_id) is not None

    phoenix = MockConfigEntry(domain=DOMAIN, entry_id="phoenix-removal")
    phoenix.add_to_hass(hass)
    content, outcome, _ = await _call_tool(
        "remove_integration",
        {"entry_id": phoenix.entry_id},
        _token(env),
        hass,
        data,
    )
    assert outcome == "not_found"
    assert content.get("isError") is True
