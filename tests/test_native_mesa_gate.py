"""MESA enforcement on the native Hass* tool path (_tool_intent_action).

MESA runs last, on the flattened entity list, on every service-call path
including the native tools. _tool_intent_action is the single choke point for
the native tools that map onto one service call, so a regression here is a
regression in all of them at once. That is why the gate is tested at this
function rather than once per tool, and why every branch of it (block event,
pending, deny, advisory warnings) is exercised.

The MESA runtime itself is stubbed. What is under test is Phoenix's ROUTING of
each MesaOutcome decision, not mesa-core's verdicts, which have their own tests.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.tools.native import _tool_intent_action

NATIVE = "custom_components.phoenix_mcp.tools.native"


def _token() -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(),
    )


def _hass(*, with_mesa: bool) -> MagicMock:
    hass = MagicMock()
    data = MagicMock()
    data.mesa = MagicMock() if with_mesa else None
    hass.data = {DOMAIN: data}
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    hass.states = MagicMock()
    hass.states.get.return_value = None
    return hass


def _outcome(decision="allow", *, entities=None, warnings=None, blocked=None, approval=None):
    return SimpleNamespace(
        decision=decision,
        entities=entities if entities is not None else ["light.kitchen"],
        warnings=warnings or [],
        blocked=blocked or [],
        approval=approval,
    )


async def _run(hass, outcome=None, entities=("light.kitchen",)):
    ctx = patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock(return_value=outcome)) \
        if outcome is not None else patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock())
    with ctx:
        return await _tool_intent_action(
            "HassTurnOn", "homeassistant", "turn_on", {}, list(entities), hass, _token(), {})


class TestMesaDecisionRouting:
    async def test_deny_returns_the_same_message_as_no_match(self, hass_unused=None):
        """A MESA denial must be indistinguishable from "nothing matched".

        Saying "MESA blocked lock.front" would confirm the entity exists and is
        in scope, which is exactly the leak the collapsed refusal prevents.
        """
        hass = _hass(with_mesa=True)
        content, outcome, resource = await _run(hass, _outcome("deny"))

        assert outcome == "denied"
        assert content["content"][0]["text"] == "No accessible entities matched your request."
        # And the service call never happened.
        hass.services.async_call.assert_not_awaited()

    async def test_pending_returns_the_approval_and_does_not_actuate(self):
        """A MESA confirm hands the approval to the SHARED pending helper.

        Patched at `_pending_or_inline` rather than at `_tool_pending`, because
        that helper is the seam now: it decides between returning immediately and
        holding the request open for `confirm_inline_wait_seconds`. Patching the
        inner `_tool_pending` would assert only the no-wait branch and would go
        on passing if this path stopped consulting the token's wait setting at
        all, which is the defect that made this change necessary.
        """
        approval = SimpleNamespace(id="ap-1", tool_name="HassTurnOn", expires_at=None)
        hass = _hass(with_mesa=True)

        with patch(
            f"{NATIVE}._pending_or_inline",
            new=AsyncMock(return_value=(
                {"content": [], "_p": True}, "pending_approval", "approval:HassTurnOn:ap-1")),
        ) as pend:
            content, outcome, resource = await _run(
                hass, _outcome("pending", approval=approval))

        assert outcome == "pending_approval"
        assert resource == "approval:HassTurnOn:ap-1"
        assert pend.await_args.args[3] is approval
        hass.services.async_call.assert_not_awaited()

    async def test_blocked_entities_fire_the_mesa_event_even_when_allowed(self):
        """A partial block still has to be observable on the event bus."""
        hass = _hass(with_mesa=True)
        blocked = [{"entity_id": "lock.front", "reason": "read_only"}]

        with patch(f"{NATIVE}.fire_mesa_blocked_event") as fire:
            await _run(hass, _outcome("allow", entities=["light.kitchen"], blocked=blocked))

        fire.assert_called_once()
        assert fire.call_args.args[2] == blocked

    async def test_allow_actuates_only_the_entities_mesa_returned(self):
        """MESA's filtered list is what reaches HA, not the list it was given."""
        hass = _hass(with_mesa=True)

        await _run(hass, _outcome("allow", entities=["light.kitchen"]),
                   entities=("light.kitchen", "lock.front"))

        call_data = hass.services.async_call.await_args.args[2]
        assert call_data["entity_id"] == ["light.kitchen"]
        assert "lock.front" not in call_data["entity_id"]

    async def test_advisory_warnings_reach_the_agent_and_set_the_audit_flag(self):
        """Advisory mode must be SURFACED, not silently swallowed."""
        hass = _hass(with_mesa=True)
        warn = ["light.kitchen is flagged by MESA"]

        with patch(f"{NATIVE}._mesa_advisory_ctx") as ctx:
            content, outcome, _ = await _run(hass, _outcome("allow", warnings=warn))

        assert outcome == "allowed"
        body = json.loads(content["content"][0]["text"])
        assert body["speech"]["plain"]["speech"] == "light.kitchen is flagged by MESA"
        ctx.set.assert_called_once_with(True)

    async def test_no_warnings_leaves_speech_empty_and_the_flag_unset(self):
        """The drop side of the previous test: no advisory, no speech, no flag."""
        hass = _hass(with_mesa=True)

        with patch(f"{NATIVE}._mesa_advisory_ctx") as ctx:
            content, outcome, _ = await _run(hass, _outcome("allow", warnings=[]))

        body = json.loads(content["content"][0]["text"])
        assert body["speech"] == {}
        ctx.set.assert_not_called()

    async def test_confirm_approved_context_is_threaded_into_the_gate(self):
        """An admin-approved execution runs MESA under confirm-approved semantics."""
        hass = _hass(with_mesa=True)

        with patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock(return_value=_outcome())) as gate, \
             patch(f"{NATIVE}._approved_exec_ctx") as ctx:
            ctx.get.return_value = True
            await _tool_intent_action(
                "HassTurnOn", "homeassistant", "turn_on", {}, ["light.kitchen"],
                hass, _token(), {})

        assert gate.await_args.kwargs["confirm_approved"] is True


class TestMesaRuntimeAbsent:
    async def test_missing_runtime_degrades_to_allow_all(self):
        """Tests and a failed setup both leave data.mesa None; that must not block."""
        hass = _hass(with_mesa=False)

        with patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock()) as gate:
            content, outcome, _ = await _tool_intent_action(
                "HassTurnOn", "homeassistant", "turn_on", {}, ["light.kitchen"],
                hass, _token(), {})

        gate.assert_not_awaited()
        assert outcome == "allowed"
        hass.services.async_call.assert_awaited_once()

    async def test_missing_domain_data_degrades_to_allow_all(self):
        hass = _hass(with_mesa=False)
        hass.data = {}

        with patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock()) as gate:
            _content, outcome, _ = await _tool_intent_action(
                "HassTurnOn", "homeassistant", "turn_on", {}, ["light.kitchen"],
                hass, _token(), {})

        gate.assert_not_awaited()
        assert outcome == "allowed"


class TestServiceCallFailures:
    """The error taxonomy on the native path, which was also uncovered."""

    async def _call(self, exc):
        hass = _hass(with_mesa=False)
        hass.services.async_call = AsyncMock(side_effect=exc)
        return await _tool_intent_action(
            "HassTurnOn", "homeassistant", "turn_on", {}, ["light.kitchen"],
            hass, _token(), {})

    async def test_timeout_reports_partial_success_not_an_error(self):
        """A slow service is dispatched, not failed (the ha-mcp lesson)."""
        import asyncio

        content, outcome, _ = await self._call(asyncio.TimeoutError())
        assert outcome == "allowed"
        body = json.loads(content["content"][0]["text"])
        assert body["success"] is True and body["partial"] is True

    async def test_service_not_found_stays_opaque(self):
        """A native ServiceNotFound is an internal mapping bug, never a hint."""
        from homeassistant.exceptions import ServiceNotFound

        content, outcome, _ = await self._call(ServiceNotFound("homeassistant", "turn_on"))
        assert outcome == "denied"
        assert content["content"][0]["text"] == "Service call failed."

    async def test_validation_error_message_is_surfaced(self):
        """Safe: the entities are already WRITE-permitted and the complaint is
        about the caller's own argument, not hidden state."""
        from homeassistant.exceptions import ServiceValidationError

        content, outcome, _ = await self._call(ServiceValidationError("brightness out of range"))
        assert outcome == "invalid_request"
        assert "brightness out of range" in content["content"][0]["text"]

    async def test_vol_invalid_is_surfaced_too(self):
        """hass.services.async_call re-raises the target schema's vol.Invalid
        unwrapped, so it never reaches the HomeAssistantError catch."""
        import voluptuous as vol

        content, outcome, _ = await self._call(vol.Invalid("extra keys not allowed"))
        assert outcome == "invalid_request"
        assert "extra keys not allowed" in content["content"][0]["text"]

    async def test_bare_home_assistant_error_stays_generic(self):
        from homeassistant.exceptions import HomeAssistantError

        content, outcome, _ = await self._call(HomeAssistantError("device offline"))
        assert outcome == "denied"
        assert content["content"][0]["text"] == "Service call failed."
        assert "device offline" not in content["content"][0]["text"]


async def test_empty_entity_list_never_reaches_mesa_or_ha():
    """The zero-selector fail-closed case, asserted at both downstream calls."""
    hass = _hass(with_mesa=True)

    with patch(f"{NATIVE}.async_apply_mesa_to_call", AsyncMock()) as gate:
        content, outcome, _ = await _tool_intent_action(
            "HassTurnOn", "homeassistant", "turn_on", {}, [], hass, _token(), {})

    assert outcome == "denied"
    assert content["content"][0]["text"] == "No accessible entities matched your request."
    gate.assert_not_awaited()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("selector,expected_type", [("area", "area"), ("floor", "floor")])
async def test_target_context_leads_the_success_list(selector, expected_type):
    """The native envelope names the area/floor the caller asked for."""
    hass = _hass(with_mesa=False)

    with patch(f"{NATIVE}._area_id_from_name", return_value="area_1"):
        content, _outcome, _ = await _tool_intent_action(
            "HassTurnOn", "homeassistant", "turn_on", {}, ["light.kitchen"],
            hass, _token(), {selector: "Kitchen"})

    body = json.loads(content["content"][0]["text"])
    assert body["data"]["success"][0]["type"] == expected_type
