"""No native tool actuates Home Assistant without passing MESA.

MESA runs last, on the flattened entity list, on EVERY service-call path,
including the native Hass* tools. Two native tools cannot go through
_tool_intent_action: HassCancelAllTimers, whose native
contract requires a speech_slots count that function does not emit, and
HassBroadcast, which picks its own assist_satellite targets. Both called
hass.services.async_call directly, so a MESA read_only or prohibited entity was
actuated anyway.

Calling HA directly cancels a read_only timer through HassCancelAllTimers while
the identical call through _tool_intent_action is denied. The two tools now call
the same gate (_mesa_gate_native) directly.

Two kinds of test here, deliberately. The behavioural ones run a real
MesaRuntime and a real service registration, so they assert the service did not
fire rather than that some mock was not called. The structural one enumerates
every actuation site in the package, so a THIRD such tool added later fails here
instead of shipping the same gap again.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.mesa import async_setup_mesa
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord

NATIVE = "custom_components.phoenix_mcp.tools.native"
REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "custom_components" / "phoenix_mcp"


def _token() -> TokenRecord:
    return TokenRecord(
        id="tok", name="t", token_hash="x", created_at=utcnow(), created_by="admin",
        persona="power_user", permissions=PermissionTree(), cap_broadcast="allow",
    )


async def _mesa(hass: HomeAssistant, entity_id: str, control_mode: str, mode="enforced"):
    runtime = await async_setup_mesa(hass, mode)
    runtime.store.set(entity_id, SemanticProfile.from_dict(
        entity_id,
        {"semantic_profile": {"operational_boundaries": {"control_mode": control_mode}}},
        default_origin=MetadataOrigin.USER))
    data = MagicMock()
    data.mesa = runtime
    data.store.get_settings.return_value = MagicMock(mesa_mode=mode)
    hass.data[DOMAIN] = data
    return runtime


class TestCancelAllTimers:
    async def test_read_only_timer_is_not_cancelled(self, hass: HomeAssistant):
        """The bypass, pinned. Asserted on the real service, not a mock."""
        from custom_components.phoenix_mcp.tools.native import _tool_hass_cancel_all_timers

        await _mesa(hass, "timer.laundry", "read_only")
        fired: list = []
        hass.services.async_register("timer", "cancel", lambda c: fired.append(c))

        with patch(f"{NATIVE}.resolve_intent_entities", return_value=["timer.laundry"]):
            _content, outcome, _ = await _tool_hass_cancel_all_timers({}, _token(), hass)
        await hass.async_block_till_done()

        assert fired == [], "a MESA read_only timer was cancelled anyway"
        assert outcome == "denied"

    async def test_autonomous_timer_still_cancels(self, hass: HomeAssistant):
        """The keep side: the gate must not break the ordinary case."""
        from custom_components.phoenix_mcp.tools.native import _tool_hass_cancel_all_timers

        await _mesa(hass, "timer.laundry", "autonomous")
        fired: list = []
        hass.services.async_register("timer", "cancel", lambda c: fired.append(c))

        with patch(f"{NATIVE}.resolve_intent_entities", return_value=["timer.laundry"]):
            _content, outcome, _ = await _tool_hass_cancel_all_timers({}, _token(), hass)
        await hass.async_block_till_done()

        assert len(fired) == 1
        assert outcome == "allowed"

    async def test_reported_count_is_what_mesa_allowed(self, hass: HomeAssistant):
        """speech_slots must not claim timers MESA stopped.

        The count is what the caller acts on next. Reporting the resolved count
        after cancelling a subset is a lie about state, which is worse than the
        refusal it papers over.
        """
        import json

        from custom_components.phoenix_mcp.tools.native import _tool_hass_cancel_all_timers

        runtime = await _mesa(hass, "timer.blocked", "read_only")
        runtime.store.set("timer.ok", SemanticProfile.from_dict(
            "timer.ok",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "autonomous"}}},
            default_origin=MetadataOrigin.USER))
        hass.services.async_register("timer", "cancel", lambda c: None)

        with patch(f"{NATIVE}.resolve_intent_entities",
                   return_value=["timer.ok", "timer.blocked"]):
            content, outcome, _ = await _tool_hass_cancel_all_timers({}, _token(), hass)
        await hass.async_block_till_done()

        assert outcome == "allowed"
        body = json.loads(content["content"][0]["text"])
        assert body["speech_slots"]["canceled"] == 1

    async def test_zero_timers_still_reports_speech_slots(self, hass: HomeAssistant):
        """Native parity: speech_slots is unconditional, including N=0."""
        import json

        from custom_components.phoenix_mcp.tools.native import _tool_hass_cancel_all_timers

        with patch(f"{NATIVE}.resolve_intent_entities", return_value=[]):
            content, outcome, _ = await _tool_hass_cancel_all_timers({}, _token(), hass)

        body = json.loads(content["content"][0]["text"])
        assert body["speech_slots"] == {"canceled": 0}
        assert outcome == "allowed"


class TestBroadcast:
    def _satellite(self, hass, entity_id="assist_satellite.kitchen"):
        from custom_components.phoenix_mcp.const import ANNOUNCE_BIT
        hass.states.async_set(entity_id, "idle", {"supported_features": ANNOUNCE_BIT})
        return entity_id

    async def test_read_only_satellite_is_not_announced_to(self, hass: HomeAssistant):
        from custom_components.phoenix_mcp.tools.native import _tool_hass_broadcast

        eid = self._satellite(hass)
        await _mesa(hass, eid, "read_only")
        fired: list = []
        hass.services.async_register("assist_satellite", "announce", lambda c: fired.append(c))

        with patch(f"{NATIVE}.resolve", return_value=__import__(
                "custom_components.phoenix_mcp.policy_engine", fromlist=["Permission"]
        ).Permission.WRITE):
            _content, outcome, _ = await _tool_hass_broadcast({"message": "hi"}, _token(), hass)
        await hass.async_block_till_done()

        assert fired == [], "a MESA read_only satellite was announced to anyway"
        assert outcome == "denied"

    async def test_autonomous_satellite_still_announces(self, hass: HomeAssistant):
        from custom_components.phoenix_mcp.policy_engine import Permission
        from custom_components.phoenix_mcp.tools.native import _tool_hass_broadcast

        eid = self._satellite(hass)
        await _mesa(hass, eid, "autonomous")
        fired: list = []
        hass.services.async_register("assist_satellite", "announce", lambda c: fired.append(c))

        with patch(f"{NATIVE}.resolve", return_value=Permission.WRITE):
            _content, outcome, _ = await _tool_hass_broadcast({"message": "hi"}, _token(), hass)
        await hass.async_block_till_done()

        assert len(fired) == 1
        assert outcome == "allowed"


# --------------------------------------------------------------------------
# The structural guard: no NEW actuation site may skip MESA silently
# --------------------------------------------------------------------------

# Functions that call hass.services.async_call without a MESA gate, each with the
# reason it is genuinely exempt. Adding a name here is a deliberate act that has
# to be justified in review; forgetting to add one fails the test below.
#
# The categories are all "no entity for MESA to have an opinion about":
#   - no entity target at all (rule 15's no-target reloads, homeassistant.restart)
#   - a config-store write whose service call is the RELOAD, not an actuation
#   - a read that happens to use a service (calendar.get_events)
#   - radio coordinator operations, which act on the radio, not on an entity
#   - notifications Phoenix itself raises
MESA_EXEMPT_ACTUATORS = {
    "helpers.py::fire_rate_limit_events": "persistent_notification Phoenix raises about itself",
    "mcp_view.py::_tool_get_calendar_events": "calendar.get_events is a read with return_response",
    "mcp_view.py::_dispatch_no_target_tool_call": "NO_TARGET_SERVICES take no entity (rule 15)",
    "mcp_view.py::_execute_restart_ha": "homeassistant.restart has no entity target",
    "proxy_view.py::_dispatch_no_target_call": "NO_TARGET_SERVICES take no entity (rule 15)",
    "proxy_view.py::post": "gated via _mesa_gate; see test_post_routes_through_the_mesa_gate",
    "radio.py::async_permit_join": "Zigbee coordinator operation, not entity actuation",
    "radio.py::async_remove_device": "Zigbee coordinator operation, not entity actuation",
    "tools/authoring.py::_execute_create_automation": "automation.reload after a config write",
    "tools/authoring.py::_execute_edit_automation": "automation.reload after a config write",
    "tools/authoring.py::_execute_delete_automation": "automation.reload after a config write",
    "tools/authoring.py::_execute_create_script": "script.reload after a config write",
    "tools/authoring.py::_execute_edit_script": "script.reload after a config write",
    "tools/authoring.py::_execute_delete_script": "script.reload after a config write",
    "tools/authoring.py::_scene_write": "scene.reload after a config write",
    "tools/authoring.py::_execute_delete_scene": "scene.reload after a config write",
}

MESA_MARKERS = ("async_apply_mesa_to_call", "_mesa_gate_native", "_mesa_gate")


def _actuation_sites() -> dict[str, bool]:
    """{module::function: gates_on_mesa} for every hass.services.async_call site."""
    out: dict[str, bool] = {}
    for path in sorted(PKG.rglob("*.py")):
        if "mesa_core" in path.parts or "fastmcp" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if "services.async_call(" not in body:
                continue
            key = f"{path.relative_to(PKG)}::{node.name}"
            out[key] = any(m in body for m in MESA_MARKERS)
    return out


def test_every_actuation_site_gates_on_mesa_or_is_a_declared_exemption():
    sites = _actuation_sites()
    assert len(sites) > 10, f"the scan found only {len(sites)} actuation sites; it is not working"
    ungated = {k for k, gated in sites.items() if not gated} - set(MESA_EXEMPT_ACTUATORS)
    assert not ungated, (
        "These call hass.services.async_call without a MESA gate and are not "
        "declared exempt. Either route them through _mesa_gate_native (rule 26) "
        "or add them to MESA_EXEMPT_ACTUATORS with the reason:\n  "
        + "\n  ".join(sorted(ungated))
    )


def test_the_exemption_list_has_no_stale_entries():
    """A name left behind after a rename makes the list stop protecting anything."""
    sites = _actuation_sites()
    stale = set(MESA_EXEMPT_ACTUATORS) - set(sites)
    assert not stale, (
        "MESA_EXEMPT_ACTUATORS names functions that no longer actuate (renamed, "
        "moved, or the call was removed): " + ", ".join(sorted(stale))
    )


def test_the_two_fixed_tools_are_not_on_the_exemption_list():
    """They were the bug. Re-exempting them would silently reopen it."""
    for name in ("tools/native.py::_tool_hass_cancel_all_timers",
                 "tools/native.py::_tool_hass_broadcast"):
        assert name not in MESA_EXEMPT_ACTUATORS
        assert _actuation_sites().get(name) is True, f"{name} lost its MESA gate"


def test_the_scan_would_notice_an_ungated_actuator():
    """Mutation check in-file: prove the scan is not vacuous.

    A scan that unparses to an empty string, or looks for the wrong call, reports
    zero ungated sites exactly like a correctly gated codebase.
    """
    fake = ast.parse(
        "async def _tool_new_thing(args, token, hass):\n"
        "    await hass.services.async_call('lock', 'unlock', {})\n"
    ).body[0]
    body = ast.unparse(fake)
    assert "services.async_call(" in body, "the scan cannot see an actuation call"
    assert not any(m in body for m in MESA_MARKERS), (
        "the scan would count an ungated actuator as gated"
    )


@pytest.mark.parametrize("tool", ["_tool_hass_cancel_all_timers", "_tool_hass_broadcast"])
def test_the_fixed_tools_use_the_shared_gate_not_a_copy(tool):
    """One definition of the gate, as with physical_gate_applies."""
    tree = ast.parse((PKG / "tools" / "native.py").read_text())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == tool)
    body = ast.unparse(node)
    assert "_mesa_gate_native(" in body
    assert "async_apply_mesa_to_call(" not in body, (
        f"{tool} calls MESA directly instead of through _mesa_gate_native; that is "
        "a second copy of the gate's decision routing"
    )
