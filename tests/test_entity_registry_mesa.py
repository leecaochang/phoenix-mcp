"""Entity-registry rename/delete safety through inherited MESA profiles."""

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
    _REGISTRY_MESA_FINGERPRINT,
    _call_tool,
    _entity_identity_references,
    async_execute_approved_tool,
)
from custom_components.phoenix_mcp.mesa import (
    async_setup_mesa,
    evaluate_registry_action,
)
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.token_store import (
    GlobalSettings,
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
    snapshot_preset,
)


def _profile(key: str, mode: str, reason: str = "Registry safety test") -> SemanticProfile:
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


def _token(*, cap: str = "allow", state: str = "GREEN") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="registry-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_registry_write=cap,
        permissions=PermissionTree(domains={"light": PermissionNode(state=state)}),
    )


def _body(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _data(hass: HomeAssistant, mesa_mode: str) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.async_lock = asyncio.Lock()
    store.async_save = AsyncMock()
    store.get_settings.return_value = GlobalSettings(mesa_mode=mesa_mode)
    store.list_tokens.return_value = []
    store.get_entity_hints.return_value = {}
    store.get_pending_approvals.return_value = []
    runtime = await async_setup_mesa(hass, mesa_mode)
    data = PhoenixData(
        store=store,
        rate_limiter=MagicMock(),
        audit=MagicMock(),
        mesa=runtime,
    )
    hass.data[DOMAIN] = data
    return data


@pytest.fixture
def registry_entity(hass: HomeAssistant) -> tuple[str, str]:
    config_entry = MockConfigEntry(domain="test_integration", entry_id="registry-mesa")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test_integration", "registry-device")},
    )
    entry = er.async_get(hass).async_get_or_create(
        "light",
        "test_integration",
        "registry-light",
        config_entry=config_entry,
        device_id=device.id,
        suggested_object_id="registry_light",
    )
    hass.states.async_set(entry.entity_id, "off")
    return entry.entity_id, device.id


@pytest.mark.parametrize("profile_scope", ["domain", "device"])
async def test_inherited_read_only_blocks_rename_and_delete_before_approval(
    hass: HomeAssistant, registry_entity, profile_scope
):
    entity_id, device_id = registry_entity
    data = await _data(hass, "enforced")
    if profile_scope == "domain":
        data.mesa.store.set_domain_profile(
            "light", _profile("light", "read_only", "Domain protects registry identity")
        )
    else:
        data.mesa.store.set_device_profile(
            device_id,
            _profile(device_id, "read_only", "Device protects registry identity"),
        )

    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        for tool_name, args in (
            (
                "set_entity",
                {
                    "entity_id": entity_id,
                    "changes": {"new_entity_id": "light.registry_light_renamed"},
                },
            ),
            ("delete_entity", {"entity_id": entity_id}),
        ):
            content, outcome, _resource = await _call_tool(
                tool_name, args, _token(cap="confirm"), hass, data
            )
            assert outcome == "denied"
            denial = _body(content)
            assert denial["mesa"]["effective_rule"]["control_mode"] == "read_only"
            assert denial["mesa"]["effective_rule"]["provided_by_level"] == profile_scope

    gate.assert_not_awaited()
    assert er.async_get(hass).async_get(entity_id) is not None
    assert er.async_get(hass).async_get("light.registry_light_renamed") is None


async def test_read_only_always_blocks_but_advisory_prohibited_warns(
    hass: HomeAssistant, registry_entity
):
    entity_id, _device_id = registry_entity
    data = await _data(hass, "advisory")
    data.mesa.store.set_domain_profile("light", _profile("light", "read_only"))
    _content, outcome, _ = await _call_tool(
        "set_entity",
        {"entity_id": entity_id, "changes": {"new_entity_id": "light.read_only_refused"}},
        _token(),
        hass,
        data,
    )
    assert outcome == "denied"

    data.mesa.store.set_domain_profile(
        "light", _profile("light", "prohibited", "Advisory prohibition")
    )
    content, outcome, _ = await _call_tool(
        "set_entity",
        {"entity_id": entity_id, "changes": {"new_entity_id": "light.advisory_rename"}},
        _token(),
        hass,
        data,
    )
    assert outcome == "allowed"
    assert _body(content)["mesa_advisory"]
    assert er.async_get(hass).async_get("light.advisory_rename") is not None


async def test_enforced_prohibited_blocks_registry_identity_actions(
    hass: HomeAssistant, registry_entity
):
    entity_id, _device_id = registry_entity
    data = await _data(hass, "enforced")
    data.mesa.store.set_domain_profile("light", _profile("light", "prohibited"))
    for tool_name, args in (
        ("set_entity", {"entity_id": entity_id, "changes": {"new_entity_id": "light.blocked"}}),
        ("delete_entity", {"entity_id": entity_id}),
    ):
        _content, outcome, _ = await _call_tool(
            tool_name, args, _token(), hass, data
        )
        assert outcome == "denied"
    assert er.async_get(hass).async_get(entity_id) is not None


@pytest.mark.parametrize(
    "tool_name,args_factory",
    [
        (
            "set_entity",
            lambda entity_id: {
                "entity_id": entity_id,
                "changes": {"new_entity_id": "light.confirmed_rename"},
            },
        ),
        ("delete_entity", lambda entity_id: {"entity_id": entity_id}),
    ],
)
async def test_enforced_confirm_uses_one_normal_registry_approval(
    hass: HomeAssistant, registry_entity, tool_name, args_factory
):
    entity_id, _device_id = registry_entity
    data = await _data(hass, "enforced")
    data.mesa.store.set_domain_profile(
        "light", _profile("light", "confirm", "Operator must confirm registry identity")
    )
    gate = AsyncMock(return_value=None)
    create = AsyncMock(return_value=({}, "pending_approval", "approval-1"))
    relationship_preview = AsyncMock(
        return_value={"consumers": [], "consumer_count": 0, "searched": []}
    )
    with (
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
        patch(
            "custom_components.phoenix_mcp.mcp_view._create_registry_mesa_approval",
            create,
        ),
        patch(
            "custom_components.phoenix_mcp.mcp_view._registry_relationship_preview",
            relationship_preview,
        ),
    ):
        _content, outcome, _ = await _call_tool(
            tool_name, args_factory(entity_id), _token(), hass, data
        )

    assert outcome == "pending_approval"
    gate.assert_awaited_once()
    create.assert_awaited_once()
    saved_args = create.await_args.kwargs["args"]
    assert isinstance(saved_args[_REGISTRY_MESA_FINGERPRINT], str)
    mesa_preview = create.await_args.kwargs["diff"]["preview"]["mesa"]
    assert mesa_preview["decision"] == "confirm"
    assert mesa_preview["effective_rule"]["provided_by_level"] == "domain"
    assert mesa_preview["effective_rule"]["control_reason"].startswith("Operator")
    assert er.async_get(hass).async_get(entity_id) is not None


async def test_capability_confirmation_already_satisfies_mesa_confirmation(
    hass: HomeAssistant, registry_entity
):
    entity_id, _device_id = registry_entity
    data = await _data(hass, "enforced")
    data.mesa.store.set_domain_profile("light", _profile("light", "confirm"))
    gate = AsyncMock(return_value=({}, "pending_approval", "approval-capability"))
    create = AsyncMock()
    with (
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
        patch(
            "custom_components.phoenix_mcp.mcp_view._create_registry_mesa_approval",
            create,
        ),
    ):
        _content, outcome, _ = await _call_tool(
            "set_entity",
            {"entity_id": entity_id, "changes": {"new_entity_id": "light.one_approval"}},
            _token(cap="confirm"),
            hass,
            data,
        )

    assert outcome == "pending_approval"
    gate.assert_awaited_once()
    assert isinstance(
        gate.await_args.kwargs["args"][_REGISTRY_MESA_FINGERPRINT], str
    )
    create.assert_not_awaited()
    assert er.async_get(hass).async_get(entity_id) is not None


async def test_restrictive_parent_cannot_be_loosened_by_entity_profile(
    hass: HomeAssistant, registry_entity
):
    entity_id, device_id = registry_entity
    data = await _data(hass, "enforced")
    data.mesa.store.set_device_profile(
        device_id, _profile(device_id, "read_only", "Device is immutable")
    )
    data.mesa.store.set(entity_id, _profile(entity_id, "autonomous"))

    decision = evaluate_registry_action(
        data, _token(), entity_id, action="delete"
    )
    assert decision.decision == "deny"
    assert decision.effective_rule["control_mode"] == "read_only"
    assert decision.effective_rule["provided_by_level"] == "device"


async def test_permission_and_mesa_are_rechecked_for_approved_execution(
    hass: HomeAssistant, registry_entity
):
    entity_id, _device_id = registry_entity
    data = await _data(hass, "enforced")
    token = _token()
    data.mesa.store.set_domain_profile("light", _profile("light", "confirm"))
    initial = evaluate_registry_action(data, token, entity_id, action="rename")
    approved_args = {
        "entity_id": entity_id,
        "new_entity_id": "light.pending_rename",
        _REGISTRY_MESA_FINGERPRINT: initial.profile_fingerprint,
    }

    data.mesa.store.set_domain_profile(
        "light", _profile("light", "read_only", "Policy tightened while pending")
    )
    _content, outcome, _ = await async_execute_approved_tool(
        "set_entity", approved_args, token, hass, data
    )
    assert outcome == "denied"
    assert er.async_get(hass).async_get(entity_id) is not None

    data.mesa.store.set_domain_profile(
        "light", _profile("light", "confirm", "Confirmation reason changed")
    )
    _content, outcome, _ = await async_execute_approved_tool(
        "set_entity", approved_args, token, hass, data
    )
    assert outcome == "denied"
    assert er.async_get(hass).async_get(entity_id) is not None

    data.store.get_settings.return_value = GlobalSettings(mesa_mode="off")
    token.permissions.domains["light"] = PermissionNode(state="RED")
    _content, outcome, _ = await async_execute_approved_tool(
        "set_entity",
        {"entity_id": entity_id, "new_entity_id": "light.permission_denied"},
        token,
        hass,
        data,
    )
    assert outcome == "denied"
    assert er.async_get(hass).async_get(entity_id) is not None


async def test_rename_blockers_cover_identity_keyed_phoenix_configuration(
    hass: HomeAssistant, registry_entity
):
    entity_id, device_id = registry_entity
    data = await _data(hass, "enforced")
    configured = _token()
    configured.permissions.entities[entity_id] = PermissionNode(state="GREEN")
    configured.presets.append(snapshot_preset(configured, "Entity preset"))
    data.store.list_tokens.return_value = [configured]
    data.store.get_entity_hints.return_value = {entity_id: {"hint": "Keep identity"}}
    data.store.get_pending_approvals.return_value = [
        {
            "id": "approval-other",
            "status": "pending",
            "tool_name": "call_service",
            "args": {"entity_id": entity_id},
        }
    ]
    data.mesa.store.set(entity_id, _profile(entity_id, "confirm"))
    # Inherited profiles govern the operation but are not identity blockers.
    data.mesa.store.set_domain_profile("light", _profile("light", "confirm"))
    data.mesa.store.set_device_profile(device_id, _profile(device_id, "confirm"))

    references = _entity_identity_references(data, entity_id)
    assert {item["kind"] for item in references} == {
        "token_permission",
        "preset_permission",
        "global_hint",
        "mesa_entity_profile",
        "pending_approval",
    }

    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        content, outcome, _ = await _call_tool(
            "set_entity",
            {"entity_id": entity_id, "changes": {"new_entity_id": "light.blocked_by_config"}},
            _token(cap="confirm"),
            hass,
            data,
        )
    assert outcome == "invalid_request"
    assert {item["kind"] for item in _body(content)["blockers"]} == {
        item["kind"] for item in references
    }
    gate.assert_not_awaited()


@pytest.mark.parametrize(
    "new_entity_id,message",
    [
        ("switch.other_domain", "stay in the light domain"),
        ("not-an-entity-id", "valid entity ID"),
        ("light.occupied", "already occupied"),
    ],
)
async def test_invalid_or_occupied_rename_is_rejected_before_approval(
    hass: HomeAssistant, registry_entity, new_entity_id, message
):
    entity_id, _device_id = registry_entity
    er.async_get(hass).async_get_or_create(
        "light", "test_integration", "occupied", suggested_object_id="occupied"
    )
    data = await _data(hass, "off")
    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        content, outcome, _ = await _call_tool(
            "set_entity",
            {"entity_id": entity_id, "changes": {"new_entity_id": new_entity_id}},
            _token(cap="confirm"),
            hass,
            data,
        )
    assert outcome == "invalid_request"
    assert message in content["content"][0]["text"]
    gate.assert_not_awaited()
