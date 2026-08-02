"""Two things the native action tools must not report as the same refusal.

`resolve_intent_entities` returns an empty list for three unrelated reasons: the
caller gave nothing usable to select on, the selectors matched nothing, and the
token may not touch what matched. All three came back as
"No accessible entities matched your request.", so an agent that had sent a
malformed argument read its own mistake as a permission decision and retried it
unchanged.

Only the FIRST is separated, and only because it is decided entirely from the
caller's own arguments before anything is resolved, so it can never become an
oracle. The other two stay byte-identical, and the test that pins that is the
one that must never be relaxed.

The second half covers the mixed-target drop: a call resolving a light and a
lock under cap_physical_control deny actuates the light and silently discarded
the lock, which is indistinguishable from a room with no lock in it.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import CAP_ALLOW, CAP_DENY
from custom_components.phoenix_mcp.policy_engine import normalize_intent_selectors
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.tools.native import (
    _NO_SELECTOR_MESSAGE,
    _with_declined_targets,
    _tool_hass_turn_off,
    _tool_hass_turn_on,
)

NATIVE = "custom_components.phoenix_mcp.tools.native"
COLLAPSED = "No accessible entities matched your request."


def _token(**caps) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(), **caps,
    )


def _text(response: dict) -> str:
    return response["content"][0]["text"]


# ---------------------------------------------------------------------------
# The shared normaliser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"name": ["Front Door Lock", "Rear door lock"]},  # the observed shape
        {"name": True, "area": 3, "floor": {"a": 1}},
        {"domain": "not-a-list-but-accepted-as-one"},     # counter-case, see below
    ],
)
def test_normalizer_reports_whether_anything_usable_survived(args):
    selectors = normalize_intent_selectors(
        name=args.get("name"), area=args.get("area"), floor=args.get("floor"),
        domains=args.get("domain"), device_classes=args.get("device_class"),
    )
    # A bare string domain is a shape a model plainly meant, so it is accepted as
    # a one-element list and IS usable; everything else above degrades to absent.
    expected_usable = "domain" in args
    assert selectors.none_usable is not expected_usable


def test_normalizer_never_stringifies_a_wrong_shape():
    # str(["a"]) would invent an argument nobody sent, which then has to be
    # refused with a message about a value nobody typed.
    selectors = normalize_intent_selectors(name=["Front Door Lock"], area=7)
    assert selectors.name is None
    assert selectors.area is None


# ---------------------------------------------------------------------------
# Item 1: the caller-argument case is separated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler", [_tool_hass_turn_on, _tool_hass_turn_off])
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"name": ["Front Door Lock", "Rear door lock"]},
        {"entity_id": "light.kitchen"},  # a parameter these tools do not accept
    ],
)
async def test_no_usable_selector_is_reported_as_a_bad_argument(handler, args):
    response, outcome, _resource = await handler(args, _token(), MagicMock(), MagicMock())
    assert outcome == "invalid_request"
    assert _text(response) == _NO_SELECTOR_MESSAGE
    # It must name the parameters and their shapes: the commonest way to land
    # here is a shape error, and "give me a selector" does not fix that.
    for param in ("name", "area", "floor", "domain", "device_class"):
        assert param in _text(response)


@pytest.mark.parametrize("handler", [_tool_hass_turn_on, _tool_hass_turn_off])
async def test_a_real_selector_that_matches_nothing_stays_collapsed(handler):
    """The leak boundary. Never relax this.

    Matched-nothing and denied must remain byte-identical to each other, and
    must NOT borrow the argument-shaped message: that would turn the pair into
    an oracle for what this token can see.
    """
    with patch(f"{NATIVE}.resolve_intent_entities", MagicMock(return_value=[])):
        response, outcome, _resource = await handler(
            {"area": "Kitchen"}, _token(), MagicMock(), MagicMock())
    assert outcome == "denied"
    assert _text(response) == COLLAPSED
    assert _NO_SELECTOR_MESSAGE not in _text(response)


@pytest.mark.parametrize("handler", [_tool_hass_turn_on, _tool_hass_turn_off])
async def test_one_real_selector_is_enough_to_proceed(handler):
    action = AsyncMock(return_value=({}, "allowed", "x"))
    with patch(f"{NATIVE}.resolve_intent_entities", MagicMock(return_value=["light.k"])), \
            patch(f"{NATIVE}._tool_intent_action", action):
        await handler({"domain": ["light"]}, _token(), MagicMock(), MagicMock())
    assert action.await_count == 1


# ---------------------------------------------------------------------------
# Item 3: a capability-dropped target is named
# ---------------------------------------------------------------------------


def _action_result(entity_ids: list[str]):
    payload = {
        "speech": {},
        "response_type": "action_done",
        "data": {"success": [{"name": e, "type": "entity", "id": e} for e in entity_ids], "failed": []},
    }
    return ({"content": [{"type": "text", "text": json.dumps(payload)}]}, "allowed", "HassTurnOff")


async def _turn_off_mixed(token: TokenRecord) -> dict:
    hass = MagicMock()
    hass.states.get.return_value = None
    resolved = ["light.hall", "lock.front_door"]
    with patch(f"{NATIVE}.resolve_intent_entities", MagicMock(return_value=resolved)), \
            patch(f"{NATIVE}._tool_intent_action",
                  AsyncMock(return_value=_action_result(["light.hall"]))):
        response, outcome, _resource = await _tool_hass_turn_off(
            {"area": "Hall"}, token, hass, MagicMock())
    assert outcome == "allowed"
    return json.loads(_text(response))


async def test_a_dropped_physical_target_is_named_with_its_reason():
    payload = await _turn_off_mixed(_token(cap_physical_control=CAP_DENY))
    assert [e["id"] for e in payload["data"]["success"]] == ["light.hall"]
    assert payload["not_permitted"] == [
        {"name": "lock.front_door", "type": "entity", "id": "lock.front_door",
         "reason": "cap_physical_control"},
    ]


async def test_nothing_is_added_when_the_capability_permits():
    # The field is conditional, the mesa_advisory convention: a caller must not
    # have to distinguish an empty list from an absent one.
    payload = await _turn_off_mixed(_token(cap_physical_control=CAP_ALLOW))
    assert "not_permitted" not in payload


async def test_the_declined_list_never_rides_on_a_refusal():
    """A denied call must stay exactly as opaque as it was.

    Everything resolved is physical and the capability is deny, so nothing
    survives and the tool refuses. Naming the dropped entities on THAT response
    would disclose the very targets the refusal exists to say nothing about.
    """
    hass = MagicMock()
    hass.states.get.return_value = None
    with patch(f"{NATIVE}.resolve_intent_entities", MagicMock(return_value=["lock.front_door"])):
        response, outcome, _resource = await _tool_hass_turn_off(
            {"area": "Hall"}, _token(cap_physical_control=CAP_DENY), hass, MagicMock())
    assert outcome == "denied"
    assert _text(response) == COLLAPSED
    assert "lock.front_door" not in _text(response)


def test_declined_targets_are_never_added_to_a_non_allowed_result():
    """Pins the outcome check itself, not the JSON guard standing behind it.

    A refusal body is plain text today, so a mistakenly annotated refusal would
    fail to parse and be left alone by accident. That accident is not the
    guarantee being made: only an ALLOWED action names what it dropped, and that
    has to hold for a non-allowed result that parses perfectly well.
    """
    parseable = (
        {"content": [{"type": "text", "text": json.dumps({"speech": {}})}]},
        "denied",
        "HassTurnOff",
    )
    assert _with_declined_targets(parseable, ["lock.front_door"], MagicMock()) is parseable
