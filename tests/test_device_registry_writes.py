"""Device registry metadata writes, approval revalidation, and MESA safety."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _DEVICE_MESA_FINGERPRINT,
    _build_diff_set_device,
    _call_tool,
    _execute_set_device,
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


def _profile(key: str, mode: str, reason: str = "Device safety test") -> SemanticProfile:
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
    device_id: str,
    *,
    cap: str = "allow",
    device_state: str = "GREEN",
    entities: dict[str, PermissionNode] | None = None,
) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="device-write-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_registry_write=cap,
        cap_registry_read="allow",
        permissions=PermissionTree(
            devices={device_id: PermissionNode(state=device_state)},
            entities=entities or {},
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
def device_env(hass: HomeAssistant) -> dict:
    owner = MockConfigEntry(
        domain="test_integration", entry_id="device-owner", title="Device owner"
    )
    owner.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Device office")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("test_integration", "device-write")},
        name="Original device",
    )
    entities: list[str] = []
    for domain, unique_id in (("light", "device-light"), ("sensor", "device-sensor")):
        entry = er.async_get(hass).async_get_or_create(
            domain,
            "test_integration",
            unique_id,
            config_entry=owner,
            device_id=device.id,
            suggested_object_id=unique_id,
        )
        hass.states.async_set(entry.entity_id, "off" if domain == "light" else "1")
        entities.append(entry.entity_id)
    return {
        "owner": owner,
        "area_id": area.id,
        "device_id": device.id,
        "entities": entities,
    }


async def test_whole_device_write_requires_explicit_device_green(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    inherited = TokenRecord(
        id=str(uuid.uuid4()),
        name="inherited-only",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_registry_write="allow",
        permissions=PermissionTree(
            domains={"light": PermissionNode(state="GREEN")}
        ),
    )
    data = await _data(hass)
    content, outcome, _ = await _call_tool(
        "set_device", {"device_id": device_id, "changes": {"add_labels": []}}, inherited, hass, data
    )
    assert outcome == "denied"
    assert "explicit WRITE" in content["content"][0]["text"]


async def test_nullable_fields_labels_and_version_restore(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    token = _token(device_id)
    data = await _data(hass)
    label = lr.async_get(hass).async_create("Device registry test")

    content, outcome, _ = await _call_tool(
        "set_device",
        {
            "device_id": device_id,
            "changes": {
                "name": "Disposable Ping",
                "area_id": device_env["area_id"],
                "add_labels": [label.label_id],
            },
        },
        token,
        hass,
        data,
    )
    assert outcome == "allowed"
    assert _body(content)["labels_added"] == [label.label_id]
    desired = data.versions.list_for("device", device_id)[0]
    assert desired.after == {
        "device_id": device_id,
        "name": "Disposable Ping",
        "area_id": device_env["area_id"],
        "disabled_by": None,
        "labels": [label.label_id],
    }

    _content, outcome, _ = await _call_tool(
        "set_device",
        {
            "device_id": device_id,
            "changes": {
                "name": None,
                "area_id": None,
                "remove_labels": [label.label_id],
            },
        },
        token,
        hass,
        data,
    )
    assert outcome == "allowed"
    current = dr.async_get(hass).async_get(device_id)
    assert current.name_by_user is None
    assert current.area_id is None
    assert current.labels == set()

    _content, outcome, _ = await async_restore_version(
        desired, "admin-device", hass, data, side="after"
    )
    assert outcome == "allowed"
    restored = dr.async_get(hass).async_get(device_id)
    assert restored.name_by_user == "Disposable Ping"
    assert restored.area_id == device_env["area_id"]
    assert restored.labels == {label.label_id}
    newest = data.versions.list_for("device", device_id)[0]
    assert newest.action == "rollback"


async def test_unknown_label_is_rejected_before_approval(
    hass: HomeAssistant, device_env
):
    data = await _data(hass)
    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        content, outcome, _ = await _call_tool(
            "set_device",
            {"device_id": device_env["device_id"], "changes": {"add_labels": ["missing"]}},
            _token(device_env["device_id"], cap="confirm"),
            hass,
            data,
        )
    assert outcome == "invalid_request"
    assert "Unknown label" in content["content"][0]["text"]
    gate.assert_not_awaited()


async def test_label_reference_is_revalidated_at_execution(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass)
    label_registry = lr.async_get(hass)
    label = label_registry.async_create("Approval window device label")
    args = {"device_id": device_id, "add_labels": [label.label_id]}
    label_registry.async_delete(label.label_id)
    content, outcome, _ = await _execute_set_device(
        args, _token(device_id), hass, data
    )
    assert outcome == "invalid_request"
    assert "Unknown label" in content["content"][0]["text"]


async def test_only_user_disabled_state_can_be_toggled(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    registry = dr.async_get(hass)
    registry.async_update_device(
        device_id, disabled_by=dr.DeviceEntryDisabler.INTEGRATION
    )
    data = await _data(hass)
    content, outcome, _ = await _call_tool(
        "set_device",
        {"device_id": device_id, "changes": {"enabled": True}},
        _token(device_id),
        hass,
        data,
    )
    assert outcome == "invalid_request"
    assert "disabled by integration" in content["content"][0]["text"]
    assert registry.async_get(device_id).disabled_by == dr.DeviceEntryDisabler.INTEGRATION


async def test_enable_refuses_disabled_owner(hass: HomeAssistant):
    owner = MockConfigEntry(
        domain="test_integration",
        entry_id="disabled-owner",
        disabled_by=ConfigEntryDisabler.USER,
    )
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("test_integration", "disabled-owner-device")},
        disabled_by=dr.DeviceEntryDisabler.CONFIG_ENTRY,
    )
    data = await _data(hass)
    content, outcome, _ = await _call_tool(
        "set_device",
        {"device_id": device.id, "changes": {"enabled": True}},
        _token(device.id),
        hass,
        data,
    )
    assert outcome == "invalid_request"
    assert "owning config entry" in content["content"][0]["text"]


async def test_child_restriction_blocks_name_area_and_enabled_but_not_labels(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    blocked_entity = device_env["entities"][0]
    token = _token(
        device_id,
        entities={blocked_entity: PermissionNode(state="YELLOW")},
    )
    data = await _data(hass)
    label = lr.async_get(hass).async_create("Restriction-safe label")
    for update in (
        {"name": "Blocked"},
        {"area_id": device_env["area_id"]},
        {"enabled": False},
    ):
        content, outcome, _ = await _call_tool(
            "set_device", {"device_id": device_id, "changes": update}, token, hass, data
        )
        assert outcome == "denied"
        assert blocked_entity in _body(content)["blocked_entities"]

    _content, outcome, _ = await _call_tool(
        "set_device",
        {"device_id": device_id, "changes": {"add_labels": [label.label_id]}},
        token,
        hass,
        data,
    )
    assert outcome == "allowed"


async def test_disable_preview_includes_membership_and_relationships(
    hass: HomeAssistant, device_env
):
    expected = {"consumers": [{"kind": "automation", "id": "uses-device"}]}
    relationship_preview = AsyncMock(return_value=expected)
    with patch(
        "custom_components.phoenix_mcp.mcp_view._registry_relationships_preview",
        relationship_preview,
    ):
        diff = await _build_diff_set_device(
            {"device_id": device_env["device_id"], "enabled": False},
            _token(device_env["device_id"]),
            hass,
        )
    assert diff["preview"]["affected_entities"] == sorted(device_env["entities"])
    assert diff["preview"]["relationships"] == expected
    relationship_preview.assert_awaited_once_with(hass, sorted(device_env["entities"]))


@pytest.mark.parametrize(
    "mesa_mode,control_mode,expected",
    [
        ("enforced", "read_only", "denied"),
        ("enforced", "prohibited", "denied"),
        ("advisory", "prohibited", "allowed"),
    ],
)
async def test_device_mesa_modes_apply_to_name_and_disable(
    hass: HomeAssistant, device_env, mesa_mode, control_mode, expected
):
    device_id = device_env["device_id"]
    data = await _data(hass, mesa_mode)
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, control_mode, "Device profile controls writes")
    )
    for update in ({"name": "MESA name"}, {"enabled": False}):
        content, outcome, _ = await _call_tool(
            "set_device",
            {"device_id": device_id, "changes": update},
            _token(device_id),
            hass,
            data,
        )
        assert outcome == expected
        if expected == "allowed":
            assert _body(content)["mesa_advisory"]
            if update.get("enabled") is False:
                await _call_tool(
                    "set_device",
                    {"device_id": device_id, "changes": {"enabled": True}},
                    _token(device_id),
                    hass,
                    data,
                )


async def test_area_and_labels_have_no_mesa_gate(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass, "enforced")
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "read_only")
    )
    label = lr.async_get(hass).async_create("No MESA metadata")
    content, outcome, _ = await _call_tool(
        "set_device",
        {
            "device_id": device_id,
            "changes": {
                "area_id": device_env["area_id"],
                "add_labels": [label.label_id],
            },
        },
        _token(device_id),
        hass,
        data,
    )
    assert outcome == "allowed"
    assert "mesa_advisory" not in _body(content)


async def test_restrictive_device_profile_cannot_be_loosened_by_entity_profile(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass, "enforced")
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "read_only", "Device is immutable")
    )
    for entity_id in device_env["entities"]:
        data.mesa.store.set(entity_id, _profile(entity_id, "autonomous"))
    content, outcome, _ = await _call_tool(
        "set_device",
        {"device_id": device_id, "changes": {"name": "Still blocked"}},
        _token(device_id),
        hass,
        data,
    )
    assert outcome == "denied"
    blocked = _body(content)["blocked"]
    assert blocked
    assert all(item["rule"] == "control_mode:read_only" for item in blocked)


async def test_enforced_confirm_joins_normal_approval_and_covers_membership(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass, "enforced")
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "confirm", "Confirm whole device rename")
    )
    gate = AsyncMock(return_value=({}, "pending_approval", "cap-approval"))
    create = AsyncMock()
    with (
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
        patch(
            "custom_components.phoenix_mcp.mcp_view._create_registry_mesa_approval",
            create,
        ),
    ):
        _content, outcome, _ = await _call_tool(
            "set_device",
            {"device_id": device_id, "changes": {"name": "Confirmed device"}},
            _token(device_id, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    saved = gate.await_args.kwargs["args"]
    assert isinstance(saved[_DEVICE_MESA_FINGERPRINT], str)
    create.assert_not_awaited()


async def test_capability_approval_pins_membership_even_when_mesa_is_off(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass, "off")
    gate = AsyncMock(return_value=({}, "pending_approval", "cap-approval"))
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _ = await _call_tool(
            "set_device",
            {"device_id": device_id, "changes": {"name": "Pinned membership"}},
            _token(device_id, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]
    assert isinstance(approved_args[_DEVICE_MESA_FINGERPRINT], str)

    owner = device_env["owner"]
    joined = er.async_get(hass).async_get_or_create(
        "switch",
        "test_integration",
        "off-mode-joined",
        config_entry=owner,
        device_id=device_id,
        suggested_object_id="off_mode_joined",
    )
    hass.states.async_set(joined.entity_id, "off")
    _content, outcome, _ = await async_execute_approved_tool(
        "set_device", approved_args, _token(device_id), hass, data
    )
    assert outcome == "denied"
    assert dr.async_get(hass).async_get(device_id).name_by_user is None


async def test_membership_and_mesa_changes_are_rechecked_for_approved_execution(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass, "enforced")
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "confirm", "Initial confirmation")
    )
    create = AsyncMock(return_value=({}, "pending_approval", "mesa-approval"))
    with patch(
        "custom_components.phoenix_mcp.mcp_view._create_registry_mesa_approval",
        create,
    ):
        _content, outcome, _ = await _call_tool(
            "set_device",
            {"device_id": device_id, "changes": {"name": "Pending name"}},
            _token(device_id),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = create.await_args.kwargs["args"]
    token = _token(device_id)

    owner = device_env["owner"]
    new_entity = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test_integration",
        "joined-during-approval",
        config_entry=owner,
        device_id=device_id,
        suggested_object_id="joined_during_approval",
    )
    hass.states.async_set(new_entity.entity_id, "off")
    _content, outcome, _ = await async_execute_approved_tool(
        "set_device", approved_args, token, hass, data
    )
    assert outcome == "denied"
    assert dr.async_get(hass).async_get(device_id).name_by_user is None

    er.async_get(hass).async_remove(new_entity.entity_id)
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "read_only", "Tightened while pending")
    )
    _content, outcome, _ = await async_execute_approved_tool(
        "set_device", approved_args, token, hass, data
    )
    assert outcome == "denied"
    assert dr.async_get(hass).async_get(device_id).name_by_user is None


async def test_permission_change_is_rechecked_for_approved_execution(
    hass: HomeAssistant, device_env
):
    device_id = device_env["device_id"]
    data = await _data(hass)
    token = _token(device_id)
    token.permissions.devices[device_id] = PermissionNode(state="RED")
    _content, outcome, _ = await async_execute_approved_tool(
        "set_device", {"device_id": device_id, "name": "Denied"}, token, hass, data
    )
    assert outcome == "denied"
    assert dr.async_get(hass).async_get(device_id).name_by_user is None


async def test_entityless_device_fails_closed_only_for_mesa_actions(
    hass: HomeAssistant
):
    owner = MockConfigEntry(domain="test_integration", entry_id="entityless-owner")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("test_integration", "entityless-device")},
    )
    data = await _data(hass, "enforced")
    token = _token(device.id)
    for update in ({"name": "Blocked"}, {"enabled": False}):
        content, outcome, _ = await _call_tool(
            "set_device", {"device_id": device.id, "changes": update}, token, hass, data
        )
        assert outcome == "denied"
        assert "unresolved_device_context" in content["content"][0]["text"]

    label = lr.async_get(hass).async_create("Entityless metadata")
    _content, outcome, _ = await _call_tool(
        "set_device",
        {"device_id": device.id, "changes": {"add_labels": [label.label_id]}},
        token,
        hass,
        data,
    )
    assert outcome == "allowed"
