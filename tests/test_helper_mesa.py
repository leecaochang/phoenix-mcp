"""Inherited MESA safety for helper authoring and restoration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _call_tool,
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
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import async_ws_command


def _profile(key: str, mode: str, reason: str = "Helper safety test") -> SemanticProfile:
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


def _token(domain: str, *, cap: str = "confirm") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="helper-mesa-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_helper_write=cap,
        cap_registry_read="allow",
        permissions=PermissionTree(domains={domain: PermissionNode(state="GREEN")}),
    )


async def _data(hass: HomeAssistant, mesa_mode: str = "enforced") -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.async_lock = asyncio.Lock()
    store.async_save = AsyncMock()
    store.get_settings.return_value = GlobalSettings(mesa_mode=mesa_mode)
    store.get_pending_approvals.return_value = []
    runtime = await async_setup_mesa(hass, mesa_mode)
    data = PhoenixData(
        store=store,
        rate_limiter=MagicMock(),
        audit=MagicMock(),
        mesa=runtime,
        versions=VersionStore(),
    )
    hass.data[DOMAIN] = data
    return data


_HELPERS = {
    "input_button": {"name": "MESA Button"},
    "schedule": {
        "name": "MESA Schedule",
        "monday": [{"from": "08:00:00", "to": "09:00:00"}],
    },
    "zone": {
        "name": "MESA Zone",
        "latitude": 10.0,
        "longitude": 20.0,
        "radius": 100,
    },
    "tag": {"tag_id": "mesa-tag", "name": "MESA Tag"},
}


async def _create_raw_helper(
    hass: HomeAssistant, helper_type: str
) -> tuple[str, str, dict]:
    assert await async_setup_component(hass, helper_type, {helper_type: {}})
    item = await async_ws_command(
        hass, f"{helper_type}/create", dict(_HELPERS[helper_type])
    )
    helper_id = item["id"]
    entity_id = er.async_get(hass).async_get_entity_id(
        helper_type, helper_type, helper_id
    )
    assert entity_id is not None
    return helper_id, entity_id, item


@pytest.mark.parametrize("helper_type", tuple(_HELPERS))
async def test_read_only_blocks_recent_helper_edit_and_delete_before_approval(
    hass: HomeAssistant, hass_admin_user, helper_type: str
):
    helper_id, entity_id, _item = await _create_raw_helper(hass, helper_type)
    data = await _data(hass)
    data.mesa.store.set(entity_id, _profile(entity_id, "read_only"))
    gate = AsyncMock()

    with patch("custom_components.phoenix_mcp.tools.helper._gate", gate):
        for tool_name, args in (
            (
                "edit_helper",
                {
                    "helper_type": helper_type,
                    "helper_id": helper_id,
                    "config": {"name": "Must Not Change"},
                },
            ),
            (
                "delete_helper",
                {"helper_type": helper_type, "helper_id": helper_id},
            ),
        ):
            content, outcome, _resource = await _call_tool(
                tool_name, args, _token(helper_type), hass, data
            )
            assert outcome == "denied"
            denial = json.loads(content["content"][0]["text"])
            assert denial["mesa"]["decision"] == "deny"
            assert denial["mesa"]["entities"][0]["effective_rule"]["control_mode"] == "read_only"

    gate.assert_not_awaited()
    assert er.async_get(hass).async_get(entity_id) is not None
    await async_ws_command(
        hass,
        f"{helper_type}/delete",
        {f"{helper_type}_id": helper_id},
    )


@pytest.mark.parametrize("helper_type", tuple(_HELPERS))
async def test_domain_read_only_blocks_helper_create_before_approval(
    hass: HomeAssistant, hass_admin_user, helper_type: str
):
    assert await async_setup_component(hass, helper_type, {helper_type: {}})
    data = await _data(hass)
    data.mesa.store.set_domain_profile(
        helper_type, _profile(helper_type, "read_only", "No helper creation")
    )
    gate = AsyncMock()
    with patch("custom_components.phoenix_mcp.tools.helper._gate", gate):
        content, outcome, _resource = await _call_tool(
            "create_helper",
            {"helper_type": helper_type, "config": dict(_HELPERS[helper_type])},
            _token(helper_type),
            hass,
            data,
        )
    assert outcome == "denied"
    assert "read_only" in content["content"][0]["text"]
    gate.assert_not_awaited()
    assert await async_ws_command(hass, f"{helper_type}/list", {}) == []


async def test_new_read_only_profile_invalidates_pending_tag_delete(
    hass: HomeAssistant, hass_admin_user
):
    helper_id, entity_id, _item = await _create_raw_helper(hass, "tag")
    data = await _data(hass)
    data.mesa.store.set(entity_id, _profile(entity_id, "confirm"))
    gate = AsyncMock(return_value=({}, "pending_approval", "approval-1"))
    with patch("custom_components.phoenix_mcp.tools.helper._gate", gate):
        _content, outcome, _resource = await _call_tool(
            "delete_helper",
            {"helper_type": "tag", "helper_id": helper_id},
            _token("tag"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    saved_args = gate.await_args.kwargs["args"]
    assert isinstance(saved_args["_phoenix_helper_mesa_fingerprint"], str)

    data.mesa.store.set(
        entity_id,
        _profile(entity_id, "read_only", "Protected while approval waited"),
    )
    content, outcome, _resource = await async_execute_approved_tool(
        "delete_helper", saved_args, _token("tag"), hass, data
    )
    assert outcome == "denied"
    assert "changed after this helper request was reviewed" in content["content"][0]["text"]
    assert er.async_get(hass).async_get(entity_id) is not None


async def test_capability_approval_covers_unchanged_domain_confirm_on_create(
    hass: HomeAssistant, hass_admin_user
):
    assert await async_setup_component(hass, "input_button", {"input_button": {}})
    data = await _data(hass)
    data.mesa.store.set_domain_profile(
        "input_button", _profile("input_button", "confirm")
    )
    gate = AsyncMock(return_value=({}, "pending_approval", "approval-1"))
    token = _token("input_button")
    with patch("custom_components.phoenix_mcp.tools.helper._gate", gate):
        _content, outcome, _resource = await _call_tool(
            "create_helper",
            {
                "helper_type": "input_button",
                "config": {"name": "Confirmed helper"},
            },
            token,
            hass,
            data,
        )
    assert outcome == "pending_approval"
    saved_args = gate.await_args.kwargs["args"]
    diff = await gate.await_args.kwargs["diff"]()
    assert diff["preview"]["mesa"]["decision"] == "confirm"

    content, outcome, _resource = await async_execute_approved_tool(
        "create_helper", saved_args, token, hass, data
    )
    assert outcome == "allowed", content
    helper = json.loads(content["content"][0]["text"])["helper"]
    await async_ws_command(
        hass,
        "input_button/delete",
        {"input_button_id": helper["id"]},
    )


async def test_mesa_confirm_uses_one_helper_approval_when_capability_allows(
    hass: HomeAssistant, hass_admin_user
):
    helper_id, entity_id, _item = await _create_raw_helper(hass, "tag")
    data = await _data(hass)
    data.mesa.store.set(entity_id, _profile(entity_id, "confirm"))
    approval = SimpleNamespace(id="mesa-helper-approval")
    gate = AsyncMock(return_value=None)
    create_approval = AsyncMock(return_value=approval)
    pending = AsyncMock(return_value=({}, "pending_approval", "approval-1"))
    with (
        patch("custom_components.phoenix_mcp.tools.helper._gate", gate),
        patch(
            "custom_components.phoenix_mcp.tools.helper.async_create_mesa_approval",
            create_approval,
        ),
        patch(
            "custom_components.phoenix_mcp.tools.helper._pending_or_inline",
            pending,
        ),
    ):
        _content, outcome, _resource = await _call_tool(
            "delete_helper",
            {"helper_type": "tag", "helper_id": helper_id},
            _token("tag", cap="allow"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    gate.assert_awaited_once()
    create_approval.assert_awaited_once()
    assert create_approval.await_args.kwargs["tool_name"] == "delete_helper"
    mesa = create_approval.await_args.kwargs["diff"]["preview"]["mesa"]
    assert mesa["confirm_entities"] == [entity_id]
    assert pending.await_count == 1
    assert pending.await_args.args[0] is hass
    assert pending.await_args.args[1] is data
    assert pending.await_args.args[3] is create_approval.return_value
    assert er.async_get(hass).async_get(entity_id) is not None
    await async_ws_command(hass, "tag/delete", {"tag_id": helper_id})


async def test_deleted_tag_restore_honors_orphan_read_only_but_accepts_confirm(
    hass: HomeAssistant, hass_admin_user
):
    helper_id, entity_id, item = await _create_raw_helper(hass, "tag")
    data = await _data(hass)
    await async_ws_command(hass, "tag/delete", {"tag_id": helper_id})
    assert er.async_get(hass).async_get(entity_id) is None
    before = {"name": item["name"]}
    if item.get("description") is not None:
        before["description"] = item["description"]
    record = SimpleNamespace(
        resource_type="helper",
        resource_id=f"tag:{helper_id}",
        before=before,
        after=None,
    )

    data.mesa.store.set(entity_id, _profile(entity_id, "read_only"))
    content, outcome, _resource = await async_restore_version(
        record, hass_admin_user.id, hass, data
    )
    assert outcome == "denied"
    assert "read_only" in content["content"][0]["text"]
    assert await async_ws_command(hass, "tag/list", {}) == []

    data.mesa.store.set(entity_id, _profile(entity_id, "confirm"))
    _content, outcome, _resource = await async_restore_version(
        record, hass_admin_user.id, hass, data
    )
    assert outcome == "allowed"
    assert er.async_get(hass).async_get(entity_id) is not None
    await async_ws_command(hass, "tag/delete", {"tag_id": helper_id})


async def test_read_only_blocks_config_flow_helper_settings_before_approval(
    hass: HomeAssistant, hass_admin_user
):
    entry = MockConfigEntry(
        domain="threshold",
        entry_id="mesa-helper-settings",
        title="MESA threshold",
        options={"entity_id": "sensor.source", "hysteresis": 0.0},
    )
    entry.add_to_hass(hass)
    object.__setattr__(entry, "_supports_options", True)
    registry_entry = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "threshold",
        "mesa-helper-settings",
        config_entry=entry,
        suggested_object_id="mesa_threshold",
    )
    hass.states.async_set(registry_entry.entity_id, "off")
    hass.states.async_set("sensor.source", "20")
    data = await _data(hass)
    data.mesa.store.set(
        registry_entry.entity_id,
        _profile(registry_entry.entity_id, "read_only"),
    )
    token = _token("sensor")
    gate = AsyncMock()

    async def _helper_integration(_hass, _domain):
        return MagicMock(integration_type="helper")

    with (
        patch(
            "custom_components.phoenix_mcp.tools.helper.async_get_integration",
            _helper_integration,
        ),
        patch("custom_components.phoenix_mcp.tools.helper._gate", gate),
    ):
        content, outcome, _resource = await _call_tool(
            "set_helper_settings",
            {
                "entry_id": entry.entry_id,
                "settings": {"entity_id": "sensor.source", "hysteresis": 1.0},
            },
            token,
            hass,
            data,
        )
    assert outcome == "denied"
    assert "read_only" in content["content"][0]["text"]
    gate.assert_not_awaited()
    assert entry.options["hysteresis"] == 0.0
