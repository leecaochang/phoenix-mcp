"""Tests for MESA per-entity enforcement classification.

Exercises evaluate_service_entities over a real MesaRuntime: the advisory vs
enforced vs off behaviour, the interactive=False confirm routing, per-profile
enforcement_mode override, and confirm-approved folding.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.mesa import (
    async_apply_mesa_to_call,
    async_setup_mesa,
    build_mesa_service_diff,
    evaluate_service_entities,
)
import custom_components.phoenix_mcp.mesa as mesa_module
from custom_components.phoenix_mcp.token_store import GlobalSettings
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord


def _token() -> TokenRecord:
    return TokenRecord(
        id="tok",
        name="t",
        token_hash="x",
        created_at=utcnow(),
        created_by="admin",
        persona="power_user",
        permissions=PermissionTree(),
    )


def _set_profile(runtime, entity_id, control_mode, *, enforcement_mode=None):
    # Stamp source: user, mirroring operator-authored profiles. An unknown-origin
    # autonomous declaration is clamped to confirm by MESA (untrusted loosening).
    ob = {"control_mode": control_mode}
    if enforcement_mode is not None:
        ob["enforcement_mode"] = enforcement_mode
    runtime.store.set(
        entity_id,
        SemanticProfile.from_dict(
            entity_id,
            {"semantic_profile": {"operational_boundaries": ob}},
            default_origin=MetadataOrigin.USER,
        ),
    )


def _evaluate(runtime, mode, entities, **kw):
    return evaluate_service_entities(
        runtime, mode, _token(), entities,
        domain="light", service="turn_on", service_data={}, session_id="s", **kw,
    )


@pytest.mark.asyncio
async def test_autonomous_allowed(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.a", "autonomous")
    verdict = _evaluate(runtime, "enforced", ["light.a"])
    assert verdict.allowed == ["light.a"]
    assert verdict.confirm == [] and verdict.blocked == []


@pytest.mark.asyncio
async def test_prohibited_blocks_under_enforced(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.a", "prohibited")
    verdict = _evaluate(runtime, "enforced", ["light.a"])
    assert verdict.allowed == []
    assert verdict.blocked and verdict.blocked[0][1] == "control_mode:prohibited"


@pytest.mark.asyncio
async def test_prohibited_warns_but_allows_under_advisory(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "advisory")
    _set_profile(runtime, "light.a", "prohibited")
    verdict = _evaluate(runtime, "advisory", ["light.a"])
    assert verdict.allowed == ["light.a"]
    assert verdict.blocked == []


@pytest.mark.asyncio
async def test_read_only_blocks_even_under_advisory(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "advisory")
    _set_profile(runtime, "light.a", "read_only")
    verdict = _evaluate(runtime, "advisory", ["light.a"])
    assert verdict.allowed == []
    assert verdict.blocked and verdict.blocked[0][1] == "control_mode:read_only"


@pytest.mark.asyncio
async def test_confirm_routes_to_confirm_under_enforced(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.a", "confirm")
    verdict = _evaluate(runtime, "enforced", ["light.a"])
    assert verdict.confirm == ["light.a"]
    assert verdict.allowed == [] and verdict.blocked == []


@pytest.mark.asyncio
async def test_confirm_allows_with_warning_under_advisory(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "advisory")
    _set_profile(runtime, "light.a", "confirm")
    verdict = _evaluate(runtime, "advisory", ["light.a"])
    assert verdict.allowed == ["light.a"]
    assert verdict.confirm == []
    # The agent must be told the action was confirm-gated but allowed because
    # MESA is advisory (otherwise advisory is indistinguishable from off).
    assert any("light.a" in w and "advisory" in w for w in verdict.warnings)


@pytest.mark.asyncio
async def test_confirm_approved_emits_no_advisory_warning(hass: HomeAssistant):
    # Re-execution after admin approval must not re-warn (it was approved, not
    # waved through by advisory mode).
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.a", "confirm")
    verdict = _evaluate(runtime, "enforced", ["light.a"], confirm_approved=True)
    assert verdict.allowed == ["light.a"]
    assert verdict.warnings == []


@pytest.mark.asyncio
async def test_per_profile_enforced_overrides_global_advisory(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "advisory")
    _set_profile(runtime, "light.a", "confirm", enforcement_mode="enforced")
    verdict = _evaluate(runtime, "advisory", ["light.a"])
    assert verdict.confirm == ["light.a"]


@pytest.mark.asyncio
async def test_confirm_approved_folds_into_allowed(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.a", "confirm")
    verdict = _evaluate(runtime, "enforced", ["light.a"], confirm_approved=True)
    assert verdict.allowed == ["light.a"]
    assert verdict.confirm == []


@pytest.mark.asyncio
async def test_mixed_entities_split_correctly(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.ok", "autonomous")
    _set_profile(runtime, "light.gate", "confirm")
    _set_profile(runtime, "light.no", "prohibited")
    verdict = _evaluate(runtime, "enforced", ["light.ok", "light.gate", "light.no"])
    assert verdict.allowed == ["light.ok"]
    assert verdict.confirm == ["light.gate"]
    assert [b[0] for b in verdict.blocked] == ["light.no"]


@pytest.mark.asyncio
async def test_diff_includes_mesa_block(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.gate", "confirm")
    _set_profile(runtime, "light.ok", "autonomous")
    verdict = _evaluate(runtime, "enforced", ["light.gate", "light.ok"])
    diff = build_mesa_service_diff("light", "turn_on", {"brightness_pct": 50}, verdict)
    assert diff["kind"] == "service_preview"
    assert diff["preview"]["mesa"]["confirm_entities"] == ["light.gate"]
    assert diff["preview"]["resolved_entity_ids"] == ["light.gate", "light.ok"]


@pytest.mark.asyncio
async def test_custom_mesa_approval_preserves_tool_args_and_diff(
    hass: HomeAssistant, monkeypatch
):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.gate", "confirm")
    create = AsyncMock(return_value=SimpleNamespace(id="approval"))
    monkeypatch.setattr(mesa_module, "async_create_mesa_approval", create)
    data = SimpleNamespace(
        mesa=runtime,
        mesa_setup_failed=False,
        store=SimpleNamespace(get_settings=lambda: GlobalSettings(mesa_mode="enforced")),
    )
    custom_diff = {
        "kind": "config_diff",
        "preview": {"property": "led_mode"},
    }
    outcome = await async_apply_mesa_to_call(
        hass,
        data,
        _token(),
        domain="zigbee",
        service="set_property",
        service_data={"property": "led_mode", "value": "on"},
        entities=["light.gate"],
        request_id="rid",
        client_ip="1.2.3.4",
        session_id="sid",
        approval_tool_name="set_zigbee_device_property",
        approval_args={"device_id": "dev", "property": "led_mode"},
        approval_diff=custom_diff,
        require_all=True,
    )
    assert outcome.decision == "pending"
    kwargs = create.await_args.kwargs
    assert kwargs["tool_name"] == "set_zigbee_device_property"
    assert kwargs["args"] == {"device_id": "dev", "property": "led_mode"}
    assert kwargs["diff"]["kind"] == "config_diff"
    assert kwargs["diff"]["preview"]["property"] == "led_mode"
    assert kwargs["diff"]["preview"]["mesa"]["confirm_entities"] == [
        "light.gate"
    ]


@pytest.mark.asyncio
async def test_require_all_mesa_denies_mixed_allowed_and_blocked(
    hass: HomeAssistant, monkeypatch
):
    runtime = await async_setup_mesa(hass, "enforced")
    _set_profile(runtime, "light.ok", "autonomous")
    _set_profile(runtime, "light.no", "prohibited")
    create = AsyncMock()
    monkeypatch.setattr(mesa_module, "async_create_mesa_approval", create)
    data = SimpleNamespace(
        mesa=runtime,
        mesa_setup_failed=False,
        store=SimpleNamespace(get_settings=lambda: GlobalSettings(mesa_mode="enforced")),
    )
    outcome = await async_apply_mesa_to_call(
        hass,
        data,
        _token(),
        domain="zigbee",
        service="set_property",
        service_data={"property": "led_mode", "value": "on"},
        entities=["light.ok", "light.no"],
        request_id="rid",
        client_ip=None,
        session_id="sid",
        require_all=True,
    )
    assert outcome.decision == "deny"
    assert [item[0] for item in outcome.blocked] == ["light.no"]
    create.assert_not_awaited()
