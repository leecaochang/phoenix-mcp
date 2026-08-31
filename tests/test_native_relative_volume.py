"""Whole-request semantics for numeric native relative-volume actions."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool
from custom_components.phoenix_mcp.policy_engine import Permission
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.tools.native import (
    _execute_hass_set_volume_relative,
    _tool_hass_set_volume_relative,
)

NATIVE = "custom_components.phoenix_mcp.tools.native"


def _token() -> TokenRecord:
    return TokenRecord(
        id="token",
        name="test",
        token_hash="x",
        created_at=utcnow(),
        created_by="admin",
        permissions=PermissionTree(),
    )


def _data(*, with_mesa: bool) -> SimpleNamespace:
    return SimpleNamespace(
        mesa=MagicMock() if with_mesa else None,
        mesa_setup_failed=False,
    )


def _hass(levels: dict[str, float]) -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)

    def state(entity_id: str) -> MagicMock:
        return MagicMock(attributes={
            "friendly_name": entity_id,
            "volume_level": levels[entity_id],
        })

    hass.states.get.side_effect = state
    return hass


def _mesa_outcome(
    decision: str,
    *,
    entities: list[str] | None = None,
    approval: object | None = None,
    blocked: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        decision=decision,
        entities=entities or [],
        approval=approval,
        blocked=blocked or [],
        warnings=[],
    )


def _plan() -> dict:
    return {
        "_relative_volume_plan_version": 1,
        "target_levels": [
            {"entity_id": "media_player.one", "volume_level": 0.45},
            {"entity_id": "media_player.two", "volume_level": 1.0},
        ],
        "volume_step": 25,
    }


async def test_confirmation_covers_the_complete_frozen_target_mapping():
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})
    data = _data(with_mesa=True)
    approval = SimpleNamespace(id="approval", expires_at=None)
    gate_result = _mesa_outcome("pending", approval=approval)
    pending = (
        {"content": []},
        "pending_approval",
        "approval:HassSetVolumeRelative:approval",
    )
    with patch(
        f"{NATIVE}.resolve_intent_entities",
        return_value=["media_player.one", "media_player.two"],
    ), patch(f"{NATIVE}.resolve", return_value=Permission.WRITE), patch(
        f"{NATIVE}.async_apply_mesa_to_call",
        AsyncMock(return_value=gate_result),
    ) as mesa_gate, patch(
        f"{NATIVE}._pending_or_inline", AsyncMock(return_value=pending)
    ):
        result = await _tool_hass_set_volume_relative(
            {"name": "Speakers", "volume_step": 25},
            _token(),
            hass,
            data,
            request_id="rid",
            client_ip="1.2.3.4",
        )

    assert result[1] == "pending_approval"
    mesa_gate.assert_awaited_once()
    kwargs = mesa_gate.await_args.kwargs
    assert kwargs["entities"] == ["media_player.one", "media_player.two"]
    assert kwargs["service_data_by_entity"] == {
        "media_player.one": {"volume_level": 0.45},
        "media_player.two": {"volume_level": 1.0},
    }
    assert kwargs["approval_args"] == _plan()
    assert kwargs["approval_tool_name"] == "HassSetVolumeRelative"
    assert kwargs["require_all"] is True
    hass.services.async_call.assert_not_awaited()


async def test_mesa_denial_refuses_the_whole_plan_before_dispatch():
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})
    data = _data(with_mesa=True)
    gate_result = _mesa_outcome(
        "deny",
        blocked=[("media_player.two", "control_mode:read_only", "read only")],
    )
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE), patch(
        f"{NATIVE}.async_apply_mesa_to_call",
        AsyncMock(return_value=gate_result),
    ), patch(f"{NATIVE}.fire_mesa_blocked_event"):
        _response, outcome, _resource = await _execute_hass_set_volume_relative(
            _plan(), _token(), hass, data,
        )

    assert outcome == "denied"
    hass.services.async_call.assert_not_awaited()


async def test_approved_execution_reuses_levels_instead_of_recomputing_state():
    hass = _hass({"media_player.one": 0.8, "media_player.two": 0.1})
    data = _data(with_mesa=True)
    gate_result = _mesa_outcome(
        "allow", entities=["media_player.one", "media_player.two"]
    )
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE), patch(
        f"{NATIVE}.async_apply_mesa_to_call",
        AsyncMock(return_value=gate_result),
    ) as mesa_gate:
        _response, outcome, _resource = await async_execute_approved_tool(
            "HassSetVolumeRelative", _plan(), _token(), hass, data,
        )

    assert outcome == "allowed"
    assert mesa_gate.await_args.kwargs["confirm_approved"] is True
    calls = [call.args[2] for call in hass.services.async_call.await_args_list]
    assert calls == [
        {"volume_level": 0.45, "entity_id": ["media_player.one"]},
        {"volume_level": 1.0, "entity_id": ["media_player.two"]},
    ]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (HomeAssistantError("offline"), "denied"),
        (vol.Invalid("invalid level"), "invalid_request"),
        (asyncio.TimeoutError(), "timeout"),
    ],
)
async def test_partial_group_failures_are_reported(failure, reason):
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})

    async def service_call(_domain, _service, call_data, **_kwargs):
        if call_data["volume_level"] == 1.0:
            raise failure

    hass.services.async_call.side_effect = service_call
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            _plan(), _token(), hass, _data(with_mesa=False),
        )

    assert outcome == "allowed"
    body = json.loads(response["content"][0]["text"])
    assert body["partial"] is True
    assert [entry["id"] for entry in body["data"]["success"]] == [
        "media_player.one"
    ]
    assert body["data"]["failed"][0]["id"] == "media_player.two"
    assert body["data"]["failed"][0]["reason"] == reason


async def test_distinct_volume_groups_dispatch_concurrently():
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})
    second_started = asyncio.Event()

    async def service_call(_domain, _service, call_data, **_kwargs):
        if call_data["volume_level"] == 0.45:
            await asyncio.wait_for(second_started.wait(), timeout=0.5)
        else:
            second_started.set()

    hass.services.async_call.side_effect = service_call
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            _plan(), _token(), hass, _data(with_mesa=False),
        )

    body = json.loads(response["content"][0]["text"])
    assert outcome == "allowed"
    assert body["data"]["failed"] == []
    assert {entry["id"] for entry in body["data"]["success"]} == {
        "media_player.one",
        "media_player.two",
    }


async def test_distinct_volume_groups_have_a_fixed_concurrency_ceiling():
    entity_count = 40
    levels = {
        f"media_player.player_{index}": index / entity_count
        for index in range(entity_count)
    }
    hass = _hass(levels)
    active = 0
    peak = 0

    async def service_call(_domain, _service, _call_data, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
        finally:
            active -= 1

    hass.services.async_call.side_effect = service_call
    plan = {
        "_relative_volume_plan_version": 1,
        "target_levels": [
            {"entity_id": entity_id, "volume_level": level}
            for entity_id, level in levels.items()
        ],
        "volume_step": 1,
    }
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            plan, _token(), hass, _data(with_mesa=False),
        )

    body = json.loads(response["content"][0]["text"])
    assert outcome == "allowed"
    assert len(body["data"]["success"]) == entity_count
    assert hass.services.async_call.await_count == entity_count
    assert peak == 8


async def test_relative_volume_uses_one_deadline_and_reports_undispatched_groups():
    entity_count = 20
    levels = {
        f"media_player.player_{index}": index / entity_count
        for index in range(entity_count)
    }
    hass = _hass(levels)
    cancelled = 0

    async def service_call(_domain, _service, _call_data, **_kwargs):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    hass.services.async_call.side_effect = service_call
    plan = {
        "_relative_volume_plan_version": 1,
        "target_levels": [
            {"entity_id": entity_id, "volume_level": level}
            for entity_id, level in levels.items()
        ],
        "volume_step": 1,
    }
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE), patch(
        f"{NATIVE}.PROXY_TIMEOUT_SECONDS", 0.02,
    ):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            plan, _token(), hass, _data(with_mesa=False),
        )

    body = json.loads(response["content"][0]["text"])
    reasons = [entry["reason"] for entry in body["data"]["failed"]]
    assert outcome == "allowed"
    assert body["partial"] is True
    assert hass.services.async_call.await_count == 8
    assert cancelled == 8
    assert reasons.count("timeout") == 8
    assert reasons.count("deadline_exceeded") == entity_count - 8


async def test_deadline_stops_queue_when_service_suppresses_cancellation():
    entity_count = 20
    levels = {
        f"media_player.player_{index}": index / entity_count
        for index in range(entity_count)
    }
    hass = _hass(levels)

    async def service_call(_domain, _service, _call_data, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    hass.services.async_call.side_effect = service_call
    plan = {
        "_relative_volume_plan_version": 1,
        "target_levels": [
            {"entity_id": entity_id, "volume_level": level}
            for entity_id, level in levels.items()
        ],
        "volume_step": 1,
    }
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE), patch(
        f"{NATIVE}.PROXY_TIMEOUT_SECONDS", 0.02,
    ):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            plan, _token(), hass, _data(with_mesa=False),
        )

    body = json.loads(response["content"][0]["text"])
    reasons = [entry["reason"] for entry in body["data"]["failed"]]
    assert outcome == "allowed"
    assert body["data"]["success"] == []
    assert hass.services.async_call.await_count == 8
    assert reasons.count("timeout") == 8
    assert reasons.count("deadline_exceeded") == entity_count - 8


async def test_relative_volume_cancellation_stops_workers_and_propagates():
    levels = {
        f"media_player.player_{index}": index / 20
        for index in range(20)
    }
    hass = _hass(levels)
    started = asyncio.Event()
    cancelled = 0

    async def service_call(_domain, _service, _call_data, **_kwargs):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    hass.services.async_call.side_effect = service_call
    plan = {
        "_relative_volume_plan_version": 1,
        "target_levels": [
            {"entity_id": entity_id, "volume_level": level}
            for entity_id, level in levels.items()
        ],
        "volume_step": 1,
    }
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
        task = asyncio.create_task(_execute_hass_set_volume_relative(
            plan, _token(), hass, _data(with_mesa=False),
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert hass.services.async_call.await_count == 8
    assert cancelled == 8


@pytest.mark.parametrize("reverse", [False, True])
async def test_all_failed_groups_keep_details_with_deterministic_outcome(reverse):
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})

    async def service_call(_domain, _service, call_data, **_kwargs):
        if call_data["volume_level"] == 0.45:
            raise HomeAssistantError("offline")
        raise vol.Invalid("invalid level")

    hass.services.async_call.side_effect = service_call
    plan = _plan()
    if reverse:
        plan["target_levels"].reverse()
    with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
        response, outcome, _resource = await _execute_hass_set_volume_relative(
            plan, _token(), hass, _data(with_mesa=False),
        )

    body = json.loads(response["content"][0]["text"])
    failed = {entry["id"]: entry["reason"] for entry in body["data"]["failed"]}
    assert outcome == "invalid_request"
    assert response["isError"] is True
    assert body["data"]["success"] == []
    assert failed == {
        "media_player.one": "denied",
        "media_player.two": "invalid_request",
    }
    assert "partial" not in body


async def test_unversioned_frozen_plan_is_refused_before_dispatch():
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})
    plan = _plan()
    plan.pop("_relative_volume_plan_version")

    response, outcome, _resource = await _execute_hass_set_volume_relative(
        plan, _token(), hass, _data(with_mesa=False),
    )

    assert outcome == "invalid_request"
    assert "invalid relative-volume target plan" in response["content"][0]["text"]
    hass.services.async_call.assert_not_awaited()


async def test_missing_current_volume_refuses_before_mesa_or_dispatch():
    hass = _hass({"media_player.one": 0.2, "media_player.two": 0.9})
    hass.states.get.side_effect = lambda entity_id: MagicMock(
        attributes={"volume_level": 0.2 if entity_id.endswith("one") else None}
    )
    mesa_gate = AsyncMock()
    with patch(
        f"{NATIVE}.resolve_intent_entities",
        return_value=["media_player.one", "media_player.two"],
    ), patch(f"{NATIVE}.async_apply_mesa_to_call", mesa_gate):
        response, outcome, _resource = await _tool_hass_set_volume_relative(
            {"name": "Speakers", "volume_step": 25},
            _token(),
            hass,
            _data(with_mesa=True),
        )

    assert outcome == "invalid_request"
    assert "current volume is unavailable" in response["content"][0]["text"]
    mesa_gate.assert_not_awaited()
    hass.services.async_call.assert_not_awaited()
