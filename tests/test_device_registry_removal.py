"""Integration-aware device removal, ownership races, and MESA safety."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _REMOVE_DEVICE_CONTEXT_FINGERPRINT,
    _REMOVE_DEVICE_MESA_FINGERPRINT,
    _build_diff_remove_device,
    _call_tool,
    _device_mesa_decision,
    _execute_remove_device,
    _resolve_device_removal_context,
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
        {"semantic_profile": {"operational_boundaries": {"control_mode": mode}}},
        default_origin=MetadataOrigin.USER,
    )


def _token(device_id: str, *, cap: str = "allow", state: str = "GREEN") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="device-remove-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_integration_write=cap,
        cap_registry_read="allow",
        permissions=PermissionTree(
            devices={device_id: PermissionNode(state=state)}
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


def _add_owner_entity(
    hass: HomeAssistant,
    owner: MockConfigEntry,
    device_id: str,
    unique_id: str,
) -> str:
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        owner.domain,
        unique_id,
        config_entry=owner,
        device_id=device_id,
        suggested_object_id=unique_id,
    )
    hass.states.async_set(entry.entity_id, "1")
    return entry.entity_id


@pytest.fixture
def removal_env(hass: HomeAssistant) -> dict:
    owner = MockConfigEntry(
        domain="test_integration", entry_id="remove-owner", title="Remove owner"
    )
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(owner.domain, "remove-device")},
        name="Removal test device",
    )
    entity_id = _add_owner_entity(hass, owner, device.id, "remove-owner-entity")
    return {"owner": owner, "device": device, "entity_id": entity_id}


async def test_unsupported_integration_is_refused_before_approval(
    hass: HomeAssistant, removal_env
):
    data = await _data(hass)
    gate = AsyncMock()
    with (
        patch(
            "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
            AsyncMock(return_value=None),
        ),
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device",
            {"device_id": removal_env["device"].id},
            _token(removal_env["device"].id, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "invalid_request"
    assert "does not support" in content["content"][0]["text"]
    gate.assert_not_awaited()


@pytest.mark.parametrize("failure", ["reject", "exception"])
async def test_hook_rejection_or_exception_leaves_registry_ownership(
    hass: HomeAssistant, removal_env, failure
):
    hook = (
        AsyncMock(return_value=False)
        if failure == "reject"
        else AsyncMock(side_effect=RuntimeError("hook failed"))
    )
    data = await _data(hass)
    device_id = removal_env["device"].id
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device_id}, _token(device_id), hass, data
        )
    assert outcome == "denied"
    assert "integration" in content["content"][0]["text"].lower()
    assert removal_env["owner"].entry_id in dr.async_get(hass).async_get(
        device_id
    ).config_entries
    assert data.versions.list_for("device", device_id) == []


async def test_integration_may_remove_device_itself(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id

    async def remove_in_hook(_hass, _entry, _device):
        dr.async_get(hass).async_remove_device(device_id)
        return True

    data = await _data(hass)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=remove_in_hook),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device_id}, _token(device_id), hass, data
        )
    assert outcome == "allowed"
    assert _body(content)["device_disappeared"] is True
    assert dr.async_get(hass).async_get(device_id) is None


async def test_multi_owner_requires_selection_and_removes_only_selected_owner(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id
    second = MockConfigEntry(
        domain="second_integration", entry_id="second-owner", title="Second owner"
    )
    second.add_to_hass(hass)
    second_entity = _add_owner_entity(hass, second, device_id, "second-owner-entity")
    data = await _data(hass)
    hook = AsyncMock(return_value=True)
    hook_resolver = AsyncMock(return_value=hook)
    registry = dr.async_get(hass)
    original_update = registry.async_update_device
    removed = False

    def legacy_owners(_device):
        return [second.entry_id] if removed else [removal_env["owner"].entry_id, second.entry_id]

    def legacy_update(target_id, **kwargs):
        nonlocal removed
        if kwargs.get("remove_config_entry_id") == removal_env["owner"].entry_id:
            removed = True
            return registry.async_get(target_id)
        return original_update(target_id, **kwargs)

    with (
        patch(
            "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
            hook_resolver,
        ),
        patch(
            "custom_components.phoenix_mcp.mcp_view.device_config_entry_ids",
            side_effect=legacy_owners,
        ),
        patch.object(registry, "async_update_device", side_effect=legacy_update),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device_id}, _token(device_id), hass, data
        )
        assert outcome == "invalid_request"
        assert "multiple owners" in content["content"][0]["text"]

        content, outcome, _ = await _call_tool(
            "remove_device",
            {"device_id": device_id, "config_entry_id": removal_env["owner"].entry_id},
            _token(device_id),
            hass,
            data,
        )
    assert outcome == "allowed"
    body = _body(content)
    assert body["device_disappeared"] is False
    assert body["remaining_owners"][0]["entry_id"] == second.entry_id
    current = dr.async_get(hass).async_get(device_id)
    assert current is not None
    assert er.async_get(hass).async_get(second_entity) is not None

    version = data.versions.list_for("device", device_id)[0]
    assert version.action == "delete"
    assert version.before["restorable"] is False
    assert version.before["device_disappeared"] is False
    content, outcome, _ = await async_restore_version(
        version, "admin-remove", hass, data
    )
    assert outcome == "invalid_request"
    assert "cannot be restored" in content["content"][0]["text"]


async def test_preview_contains_exact_affected_relationships_and_safety_context(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id
    data = await _data(hass)
    data.store.list_tokens.return_value = [_token(device_id)]
    hook = AsyncMock(return_value=True)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        context = await _resolve_device_removal_context(
            {"device_id": device_id}, _token(device_id), hass
        )
    assert not isinstance(context, tuple)
    mesa = _device_mesa_decision(
        data,
        _token(device_id),
        hass,
        context.device,
        actions=["remove"],
        service_data={},
        session_id="preview",
        entity_ids=context.affected_entity_ids,
    )
    assert not isinstance(mesa, tuple)
    relationships = {"consumers": [{"kind": "automation", "id": "consumer"}]}
    with patch(
        "custom_components.phoenix_mcp.mcp_view._registry_relationships_preview",
        AsyncMock(return_value=relationships),
    ):
        diff = await _build_diff_remove_device(
            context, _token(device_id), hass, data, mesa
        )
    preview = diff["preview"]
    assert preview["affected_entities"] == [removal_env["entity_id"]]
    assert preview["relationships"] == relationships
    assert preview["selected_owner"]["supports_remove_device"] is True
    assert preview["device_permission_references"]
    assert preview["device_mesa_profile"] is None


async def test_mesa_evaluates_exact_selected_owner_membership(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id
    second = MockConfigEntry(
        domain="second_integration", entry_id="mesa-second", title="MESA second"
    )
    second.add_to_hass(hass)
    dr.async_get(hass).async_update_device(
        device_id, add_config_entry_id=second.entry_id
    )
    unaffected = _add_owner_entity(hass, second, device_id, "mesa-unaffected")
    data = await _data(hass, "enforced")
    data.mesa.store.set(
        removal_env["entity_id"],
        _profile(removal_env["entity_id"], "autonomous"),
    )
    data.mesa.store.set(unaffected, _profile(unaffected, "read_only"))
    hook = AsyncMock(return_value=True)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device",
            {
                "device_id": device_id,
                "config_entry_id": removal_env["owner"].entry_id,
            },
            _token(device_id),
            hass,
            data,
        )
    assert outcome == "allowed"
    assert _body(content)["remaining_owners"][0]["entry_id"] == second.entry_id


async def test_affected_read_only_and_empty_membership_fail_closed(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id
    data = await _data(hass, "enforced")
    data.mesa.store.set(
        removal_env["entity_id"], _profile(removal_env["entity_id"], "read_only")
    )
    hook = AsyncMock(return_value=True)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device_id}, _token(device_id), hass, data
        )
    assert outcome == "denied"
    assert "read_only" in content["content"][0]["text"]
    hook.assert_not_awaited()

    data.mesa.store.delete(removal_env["entity_id"])
    er.async_get(hass).async_remove(removal_env["entity_id"])
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device_id}, _token(device_id), hass, data
        )
    assert outcome == "denied"
    assert "unresolved_device_context" in content["content"][0]["text"]
    hook.assert_not_awaited()


async def test_approval_revalidates_membership_permission_capability_and_support(
    hass: HomeAssistant, removal_env
):
    device_id = removal_env["device"].id
    data = await _data(hass)
    hook = AsyncMock(return_value=True)
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with (
        patch(
            "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
            AsyncMock(return_value=hook),
        ),
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
    ):
        _content, outcome, _ = await _call_tool(
            "remove_device",
            {"device_id": device_id},
            _token(device_id, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]
    assert isinstance(approved_args[_REMOVE_DEVICE_CONTEXT_FINGERPRINT], str)
    assert isinstance(approved_args[_REMOVE_DEVICE_MESA_FINGERPRINT], str)

    _add_owner_entity(
        hass, removal_env["owner"], device_id, "joined-while-remove-pending"
    )
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=hook),
    ):
        content, outcome, _ = await async_execute_approved_tool(
            "remove_device", approved_args, _token(device_id), hass, data
        )
    assert outcome == "denied"
    assert "changed after approval" in content["content"][0]["text"]
    hook.assert_not_awaited()

    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=None),
    ):
        _content, outcome, _ = await _execute_remove_device(
            approved_args, _token(device_id), hass, data
        )
    assert outcome == "invalid_request"

    _content, outcome, _ = await _execute_remove_device(
        approved_args, _token(device_id, cap="deny"), hass, data
    )
    assert outcome == "denied"
    _content, outcome, _ = await _execute_remove_device(
        approved_args, _token(device_id, state="RED"), hass, data
    )
    assert outcome == "denied"


async def test_phoenix_owned_entry_is_never_removable(hass: HomeAssistant):
    owner = MockConfigEntry(domain=DOMAIN, entry_id="phoenix-owner")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(DOMAIN, "phoenix-device")},
    )
    data = await _data(hass)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._async_device_removal_hook",
        AsyncMock(return_value=AsyncMock(return_value=True)),
    ):
        content, outcome, _ = await _call_tool(
            "remove_device", {"device_id": device.id}, _token(device.id), hass, data
        )
    assert outcome in ("denied", "not_found")
    assert content.get("isError") is True
