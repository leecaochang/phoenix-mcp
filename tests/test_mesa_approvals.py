"""Tests for the MESA confirm-to-approval adapter.

Covers async_apply_mesa_to_call decision routing (allow/deny/pending), the saved
approval record shape (sentinel cap, non-dispatchable executor, explicit entity
list), confirm-approved re-execution folding, and the admin approve sentinel
skip (the MESA cap must not be auto-rejected by effective_cap).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import (
    MESA_APPROVED_EXECUTOR,
    MESA_CONFIRM_CAP,
)
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mesa import async_apply_mesa_to_call, async_setup_mesa
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.token_store import (
    GlobalSettings,
    PermissionTree,
    TokenRecord,
    TokenStore,
)


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


def _make_store(mesa_mode: str) -> MagicMock:
    store = MagicMock(spec=TokenStore)
    store._pending = []
    store.async_save = AsyncMock()
    store.async_lock = asyncio.Lock()
    store.get_pending_approvals = MagicMock(side_effect=lambda: store._pending)
    store.set_pending_approvals = MagicMock(
        side_effect=lambda lst: setattr(store, "_pending", lst)
    )
    store.get_settings = MagicMock(return_value=GlobalSettings(mesa_mode=mesa_mode))
    return store


async def _make_data(hass: HomeAssistant, mesa_mode: str) -> PhoenixData:
    runtime = await async_setup_mesa(hass, mesa_mode)
    data = PhoenixData(
        store=_make_store(mesa_mode),
        rate_limiter=MagicMock(spec=RateLimiter),
        audit=MagicMock(spec=AuditLog),
        mesa=runtime,
    )
    return data


def _set_profile(data, entity_id, control_mode):
    data.mesa.store.set(
        entity_id,
        SemanticProfile.from_dict(
            entity_id,
            {"semantic_profile": {"operational_boundaries": {"control_mode": control_mode}}},
            default_origin=MetadataOrigin.USER,
        ),
    )


async def _apply(hass, data, entities, **kw):
    return await async_apply_mesa_to_call(
        hass, data, _token(),
        domain="light", service="turn_on", service_data={},
        entities=entities, request_id="rid", client_ip=None, session_id="rid", **kw,
    )


@pytest.mark.asyncio
async def test_off_mode_allows_all(hass: HomeAssistant):
    data = await _make_data(hass, "off")
    _set_profile(data, "light.a", "prohibited")
    outcome = await _apply(hass, data, ["light.a"])
    assert outcome.decision == "allow"
    assert outcome.entities == ["light.a"]


@pytest.mark.asyncio
async def test_all_blocked_denies(hass: HomeAssistant):
    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.a", "prohibited")
    outcome = await _apply(hass, data, ["light.a"])
    assert outcome.decision == "deny"
    assert outcome.blocked


@pytest.mark.asyncio
async def test_confirm_creates_pending_approval(hass: HomeAssistant):
    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.gate", "confirm")
    _set_profile(data, "light.ok", "autonomous")

    with patch("homeassistant.components.persistent_notification.async_create"):
        outcome = await _apply(hass, data, ["light.gate", "light.ok"])

    assert outcome.decision == "pending"
    approval = outcome.approval
    assert approval.cap_name == MESA_CONFIRM_CAP
    assert approval.tool_name == MESA_APPROVED_EXECUTOR
    # The saved args carry the explicit confirm + allowed entity list so the
    # executor re-runs exactly what was reviewed.
    assert approval.args["entity_id"] == ["light.gate", "light.ok"]
    assert approval.args["domain"] == "light"
    assert data.store._pending  # persisted


@pytest.mark.asyncio
async def test_confirm_approved_folds_to_allow(hass: HomeAssistant):
    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.gate", "confirm")
    outcome = await _apply(hass, data, ["light.gate"], confirm_approved=True)
    assert outcome.decision == "allow"
    assert outcome.entities == ["light.gate"]


@pytest.mark.asyncio
async def test_advisory_confirm_allows_and_surfaces_warning(hass: HomeAssistant):
    # Under advisory, a confirm entity is allowed through and the outcome carries
    # a warning for the caller to surface (the mesa_advisory / native speech field).
    data = await _make_data(hass, "advisory")
    _set_profile(data, "light.a", "confirm")
    outcome = await _apply(hass, data, ["light.a"])
    assert outcome.decision == "allow"
    assert outcome.entities == ["light.a"]
    assert any("light.a" in w and "advisory" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_confirm_approved_reexec_emits_no_advisory_warning(hass: HomeAssistant):
    # The approved re-execution path must not re-warn (the action was approved,
    # not waved through by advisory mode).
    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.a", "confirm")
    outcome = await _apply(hass, data, ["light.a"], confirm_approved=True)
    assert outcome.decision == "allow"
    assert outcome.warnings == []


@pytest.mark.asyncio
async def test_missing_runtime_allows_all(hass: HomeAssistant):
    data = await _make_data(hass, "enforced")
    data.mesa = None
    outcome = await _apply(hass, data, ["light.a"])
    assert outcome.decision == "allow"
    assert outcome.entities == ["light.a"]


@pytest.mark.asyncio
async def test_admin_approval_satisfies_mesa_confirm_in_one_step(hass: HomeAssistant):
    """Approving a capability-gated call must not queue a second MESA approval.

    An action gated by BOTH a confirm-mode capability and a MESA confirm-mode
    entity would otherwise need two sequential admin approvals: approving the
    capability gate re-runs the call, which queues a separate MESA approval for
    the exact action the admin just reviewed. The admin-approved re-execution
    (async_execute_approved_tool) runs the MESA gate under confirm-approved
    semantics, so one approval completes the action; the same call outside that
    path still pends (the context flag must reset).
    """
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.phoenix_mcp.mcp_view import _execute_call_service, async_execute_approved_tool

    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.gate", "confirm")
    # An explicit entity_id must exist in the entity registry (the entity-creation
    # block), not just in states.
    er.async_get(hass).async_get_or_create("light", "test", "uniq-gate", suggested_object_id="gate")
    hass.states.async_set("light.gate", "off")
    calls = async_mock_service(hass, "light", "turn_on")
    token = _token()
    token.pass_through = True  # scope trivially satisfied; MESA applies regardless
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.gate"}

    # A live (non-approved) call still hits the MESA confirm gate.
    with patch("homeassistant.components.persistent_notification.async_create"):
        _result, outcome, _resource = await _execute_call_service(args, token, hass, data)
    assert outcome == "pending_approval"
    assert not calls
    pending_before = len(data.store._pending)

    # The admin-approved re-execution completes in one step: the service fires
    # and no follow-on MESA approval is created.
    _result, outcome, _resource = await async_execute_approved_tool(
        "call_service", args, token, hass, data,
    )
    assert outcome == "allowed"
    assert len(calls) == 1
    assert len(data.store._pending) == pending_before

    # The confirm-approved context must not leak past the approved execution.
    with patch("homeassistant.components.persistent_notification.async_create"):
        _result, outcome, _resource = await _execute_call_service(args, token, hass, data)
    assert outcome == "pending_approval"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gate_diff_marks_mesa_confirm_involvement(hass: HomeAssistant):
    """The capability-gate approval diff must say MESA is part of the approval.

    One approval now satisfies both gates, so the record the admin reviews (and
    History keeps) is the only place MESA involvement can be seen: the summary
    gains a permanent marker and the preview carries the mesa block the panel
    already renders.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.const import DOMAIN
    from custom_components.phoenix_mcp.mcp_view import _build_diff_call_service
    from custom_components.phoenix_mcp.tools.native import _build_diff_hass_turn

    data = await _make_data(hass, "enforced")
    hass.data[DOMAIN] = data
    _set_profile(data, "lock.front", "confirm")
    er.async_get(hass).async_get_or_create("lock", "test", "uniq-front", suggested_object_id="front")
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front Door Lock"})
    token = _token()
    token.pass_through = True

    diff = _build_diff_call_service(
        {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, token, hass,
    )
    assert diff["summary"].endswith("(includes MESA confirmation)")
    assert diff["preview"]["mesa"]["confirm_entities"] == ["lock.front"]
    assert diff["preview"]["mesa"]["warnings"]

    # The native HassTurnOn/Off physical diff gets the same marker, evaluated
    # per the actuating domain service (lock.lock for turn_on).
    diff = _build_diff_hass_turn("turn_on", ["lock.front"], {}, token, hass)
    assert diff["summary"].endswith("(includes MESA confirmation)")
    assert diff["preview"]["mesa"]["confirm_entities"] == ["lock.front"]


@pytest.mark.asyncio
async def test_gate_diff_unmarked_when_mesa_not_involved(hass: HomeAssistant):
    """No MESA marker when nothing is MESA-confirm (advisory mode: no gating)."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.const import DOMAIN
    from custom_components.phoenix_mcp.mcp_view import _build_diff_call_service

    data = await _make_data(hass, "advisory")
    hass.data[DOMAIN] = data
    _set_profile(data, "lock.front", "confirm")
    er.async_get(hass).async_get_or_create("lock", "test", "uniq-front", suggested_object_id="front")
    hass.states.async_set("lock.front", "locked")
    token = _token()
    token.pass_through = True

    diff = _build_diff_call_service(
        {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, token, hass,
    )
    assert "(includes MESA confirmation)" not in diff["summary"]
    assert "mesa" not in diff["preview"]


@pytest.mark.asyncio
async def test_approved_execution_still_rejects_prohibited(hass: HomeAssistant):
    """Confirm-approved semantics never loosen prohibited (or read_only) entities."""
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool

    data = await _make_data(hass, "enforced")
    _set_profile(data, "light.bad", "prohibited")
    er.async_get(hass).async_get_or_create("light", "test", "uniq-bad", suggested_object_id="bad")
    hass.states.async_set("light.bad", "off")
    calls = async_mock_service(hass, "light", "turn_on")
    token = _token()
    token.pass_through = True
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bad"}

    _result, outcome, _resource = await async_execute_approved_tool(
        "call_service", args, token, hass, data,
    )
    assert outcome == "denied"
    assert not calls
