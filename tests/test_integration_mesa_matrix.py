"""Exhaustive five-scope MESA matrix for config-entry lifecycle actions."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _call_tool,
    _config_entry_action_decision,
    async_execute_approved_tool,
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


SCOPES = ("integration", "domain", "area", "device", "entity")
ACTIONS = ("rename", "reload", "enable", "disable", "remove")
NON_EMPTY_SCOPE_SUBSETS = tuple(
    subset
    for size in range(1, len(SCOPES) + 1)
    for subset in itertools.combinations(SCOPES, size)
)
SCOPE_PAIRS = tuple(itertools.combinations(SCOPES, 2))


def _profile(key: str, mode: str) -> SemanticProfile:
    return SemanticProfile.from_dict(
        key,
        {
            "semantic_profile": {
                "operational_boundaries": {
                    "control_mode": mode,
                    "control_reason": f"Matrix {mode} at {key}",
                }
            }
        },
        default_origin=MetadataOrigin.USER,
    )


def _token(env: dict, *, cap: str = "allow") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="integration-mesa-matrix",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_integration_write=cap,
        permissions=PermissionTree(
            devices={env["device_id"]: PermissionNode(state="GREEN")},
            domains={env["entity_domain"]: PermissionNode(state="GREEN")},
        ),
    )


async def _data(hass: HomeAssistant, mesa_mode: str = "enforced") -> PhoenixData:
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


def _environment(hass: HomeAssistant) -> dict:
    entry = MockConfigEntry(
        domain="matrix_integration",
        entry_id="matrix-entry",
        title="MESA matrix integration",
    )
    entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("MESA matrix area")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(entry.domain, "matrix-device")},
        name="MESA matrix device",
    )
    device = dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        entry.domain,
        "matrix-entity",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="matrix_entity",
    )
    hass.states.async_set(entity.entity_id, "1")
    return {
        "entry": entry,
        "entry_id": entry.entry_id,
        "entry_domain": entry.domain,
        "entity_id": entity.entity_id,
        "entity_domain": "sensor",
        "device_id": device.id,
        "area_id": area.id,
    }


def _set_scope(data: PhoenixData, env: dict, scope: str, mode: str) -> None:
    store = data.mesa.store
    if scope == "integration":
        store.set_integration_profile(
            env["entry_domain"], _profile(env["entry_domain"], mode)
        )
    elif scope == "domain":
        store.set_domain_profile(
            env["entity_domain"], _profile(env["entity_domain"], mode)
        )
    elif scope == "area":
        store.set_area_profile(env["area_id"], _profile(env["area_id"], mode))
    elif scope == "device":
        store.set_device_profile(
            env["device_id"], _profile(env["device_id"], mode)
        )
    elif scope == "entity":
        store.set(env["entity_id"], _profile(env["entity_id"], mode))
    else:  # pragma: no cover - the parameter set above is closed
        raise AssertionError(scope)


def _decision(
    data: PhoenixData,
    env: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    action: str,
):
    result = _config_entry_action_decision(
        data,
        token,
        hass,
        env["entry"],
        actions=[action],
        service_data={},
        session_id=f"matrix-{action}",
    )
    assert not isinstance(result, tuple)
    return result


@pytest.mark.parametrize("scopes", NON_EMPTY_SCOPE_SUBSETS)
@pytest.mark.parametrize("action", ACTIONS)
async def test_every_non_empty_read_only_scope_subset_denies(
    hass: HomeAssistant, scopes: tuple[str, ...], action: str
):
    env = _environment(hass)
    data = await _data(hass)
    for scope in scopes:
        _set_scope(data, env, scope, "read_only")
    decision = _decision(data, env, _token(env), hass, action)
    assert decision.decision == "deny"
    assert decision.blocked
    effective_rule = decision.entities[0]["effective_rule"]
    assert effective_rule["action"] == f"config_entry.{action}"
    assert effective_rule["control_mode"] == "read_only"
    assert effective_rule["provided_by_level"] in scopes
    explanation = decision.entities[0]["explanation"]["explanation"]
    assert any(
        item["field_path"] == "operational_boundaries.control_mode"
        and item["provided_by_level"] in scopes
        for item in explanation
    )


@pytest.mark.parametrize("pair", SCOPE_PAIRS)
@pytest.mark.parametrize("restrictive_index", (0, 1))
@pytest.mark.parametrize("action", ACTIONS)
async def test_pairwise_autonomous_cannot_loosen_read_only(
    hass: HomeAssistant,
    pair: tuple[str, str],
    restrictive_index: int,
    action: str,
):
    env = _environment(hass)
    data = await _data(hass)
    restrictive = pair[restrictive_index]
    permissive = pair[1 - restrictive_index]
    _set_scope(data, env, restrictive, "read_only")
    _set_scope(data, env, permissive, "autonomous")
    decision = _decision(data, env, _token(env), hass, action)
    assert decision.decision == "deny"
    effective_rule = decision.entities[0]["effective_rule"]
    assert effective_rule["control_mode"] == "read_only"
    assert effective_rule["provided_by_level"] == restrictive


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "mesa_mode,control_mode,expected",
    (
        ("enforced", "prohibited", "deny"),
        ("advisory", "prohibited", "allow"),
        ("enforced", "confirm", "confirm"),
    ),
)
async def test_each_scope_covers_prohibited_advisory_and_confirm(
    hass: HomeAssistant,
    scope: str,
    action: str,
    mesa_mode: str,
    control_mode: str,
    expected: str,
):
    env = _environment(hass)
    data = await _data(hass, mesa_mode)
    _set_scope(data, env, scope, control_mode)
    decision = _decision(data, env, _token(env), hass, action)
    assert decision.decision == expected
    if mesa_mode == "advisory":
        assert decision.warnings
    effective_rule = decision.entities[0]["effective_rule"]
    assert effective_rule["provided_by_level"] == scope
    assert effective_rule["control_mode"] == control_mode


@pytest.mark.parametrize("scope", SCOPES)
async def test_each_confirm_scope_creates_one_merged_approval(
    hass: HomeAssistant, scope: str
):
    env = _environment(hass)
    data = await _data(hass, "enforced")
    _set_scope(data, env, scope, "confirm")
    gate = AsyncMock(return_value=None)
    create = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with (
        patch("custom_components.phoenix_mcp.mcp_view._gate", gate),
        patch(
            "custom_components.phoenix_mcp.mcp_view._create_registry_mesa_approval",
            create,
        ),
        patch(
            "custom_components.phoenix_mcp.mcp_view._registry_relationships_preview",
            AsyncMock(return_value={"consumers": []}),
        ),
    ):
        _content, outcome, _ = await _call_tool(
            "remove_integration",
            {"entry_id": env["entry_id"]},
            _token(env),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    gate.assert_awaited_once()
    create.assert_awaited_once()
    preview = create.await_args.kwargs["diff"]["preview"]
    assert preview["mesa"]["decision"] == "confirm"
    assert preview["mesa"]["entities"][0]["effective_rule"]["provided_by_level"] == scope


@pytest.mark.parametrize(
    "race", ("permission", "membership", "entry_state", "mesa")
)
async def test_all_scope_pending_approval_revalidates_every_race(
    hass: HomeAssistant, race: str
):
    env = _environment(hass)
    data = await _data(hass, "enforced")
    for scope in SCOPES:
        _set_scope(data, env, scope, "confirm")
    approving_token = _token(env, cap="confirm")
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _ = await _call_tool(
            "reload_integration",
            {"entry_id": env["entry_id"]},
            approving_token,
            hass,
            data,
        )
    assert outcome == "pending_approval"
    approved_args = gate.await_args.kwargs["args"]

    execution_token = _token(env)
    expected = "denied"
    if race == "permission":
        execution_token.permissions.devices[env["device_id"]] = PermissionNode(
            state="RED"
        )
        expected = "not_found"
    elif race == "membership":
        joined = er.async_get(hass).async_get_or_create(
            "sensor",
            env["entry_domain"],
            "joined-after-all-scope-approval",
            config_entry=env["entry"],
            device_id=env["device_id"],
        )
        hass.states.async_set(joined.entity_id, "2")
    elif race == "entry_state":
        object.__setattr__(env["entry"], "state", ConfigEntryState.SETUP_ERROR)
    else:
        _set_scope(data, env, "entity", "read_only")

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_mock:
        content, outcome, _ = await async_execute_approved_tool(
            "reload_integration", approved_args, execution_token, hass, data
        )
    assert outcome == expected
    reload_mock.assert_not_awaited()
    if race != "permission":
        assert "changed after approval" in content["content"][0]["text"]
