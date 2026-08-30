"""The three native vacuum tools.

HassVacuumStart and HassVacuumReturnToBase are ordinary domain-locked action
tools. HassVacuumCleanArea is not, and that is what most of this file is about:
its `area` names the room to CLEAN and is passed to the service as
cleaning_area_id, so feeding it to entity resolution the way every other native
tool feeds `area` would both target the wrong entities and quietly widen the
call. The test that pins `area` out of the resolver is the one worth keeping.

The feature filter is the other load-bearing piece. HA's vacuum intents set
required_features, so a vacuum that cannot perform the operation is never
targeted; without the mirror the service would be called anyway and the tool
would report success for a vacuum that ignored it.

Patches target tools.native directly, never mcp_view's re-exported names: a
monkeypatch on a re-export fails silently and leaves these assertions passing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import (
    VACUUM_CLEAN_AREA_BIT,
    VACUUM_RETURN_HOME_BIT,
    VACUUM_START_BIT,
)
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.tools.native import (
    _areas_matching,
    _tool_hass_vacuum_clean_area,
    _tool_hass_vacuum_return_to_base,
    _tool_hass_vacuum_start,
    _vacuums_supporting,
)

NATIVE = "custom_components.phoenix_mcp.tools.native"


def _token() -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(),
    )


def _hass_with_features(features: dict[str, object]) -> MagicMock:
    """hass whose states.get reports the given supported_features per entity."""
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (
        SimpleNamespace(attributes={"supported_features": features[eid]})
        if eid in features else None
    )
    return hass


def _area(area_id: str, name: str, aliases: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(id=area_id, name=name, aliases=aliases)


def _patch_areas(areas: list) -> object:
    ar_mock = patch(f"{NATIVE}.ar")
    started = ar_mock.start()
    started.async_get.return_value.async_list_areas.return_value = areas
    return ar_mock


# ---------------------------------------------------------------------------
# Feature filtering
# ---------------------------------------------------------------------------


def test_vacuums_supporting_keeps_only_capable_entities():
    hass = _hass_with_features({
        "vacuum.capable": VACUUM_START_BIT | VACUUM_RETURN_HOME_BIT,
        "vacuum.dock_only": VACUUM_RETURN_HOME_BIT,
    })
    entities = ["vacuum.capable", "vacuum.dock_only"]
    assert _vacuums_supporting(entities, hass, VACUUM_START_BIT) == ["vacuum.capable"]
    assert _vacuums_supporting(entities, hass, VACUUM_RETURN_HOME_BIT) == entities


def test_vacuums_supporting_drops_unknown_and_malformed_states():
    # A missing state and a non-int supported_features both mean "cannot prove it
    # supports this", and the filter must fail closed rather than pass it through
    # to a service call that would error.
    hass = _hass_with_features({"vacuum.weird": "8192"})
    assert _vacuums_supporting(["vacuum.weird", "vacuum.ghost"], hass, VACUUM_START_BIT) == []


# ---------------------------------------------------------------------------
# Start / return to base
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "service", "bit"),
    [
        (_tool_hass_vacuum_start, "start", VACUUM_START_BIT),
        (_tool_hass_vacuum_return_to_base, "return_to_base", VACUUM_RETURN_HOME_BIT),
    ],
)
async def test_vacuum_action_pins_domain_and_filters_by_feature(handler, service, bit):
    hass = _hass_with_features({"vacuum.able": bit, "vacuum.unable": 0})
    resolve = MagicMock(return_value=["vacuum.able", "vacuum.unable"])
    action = AsyncMock(return_value=({}, "allowed", "x"))
    with patch(f"{NATIVE}.resolve_intent_entities", resolve), \
            patch(f"{NATIVE}._tool_intent_action", action):
        await handler({"area": "Kitchen"}, _token(), hass)

    # An area-only call is pinned to vacuum and must not resolve every writable
    # entity in that area. Wrong explicit domains are covered at the public
    # dispatcher boundary in test_native_selectors.py.
    assert resolve.call_args.kwargs["domains"] == ["vacuum"]
    assert resolve.call_args.kwargs["area"] == "Kitchen"
    called_domain, called_service, service_data, entities = action.call_args.args[1:5]
    assert (called_domain, called_service) == ("vacuum", service)
    assert service_data == {}
    assert entities == ["vacuum.able"]


# ---------------------------------------------------------------------------
# Clean area
# ---------------------------------------------------------------------------


async def test_clean_area_requires_an_area():
    _resp, outcome, _res = await _tool_hass_vacuum_clean_area({}, _token(), MagicMock())
    assert outcome == "invalid_request"


async def test_clean_area_refuses_an_unmatched_area_without_saying_so():
    # Same refusal an unmatched entity gets. Answering differently would tell a
    # token that cannot list areas whether a given area exists.
    patcher = _patch_areas([_area("kitchen", "Kitchen")])
    try:
        resp, outcome, _res = await _tool_hass_vacuum_clean_area(
            {"area": "Nowhere"}, _token(), MagicMock())
    finally:
        patcher.stop()
    assert outcome == "denied"
    assert "No accessible entities matched your request." in str(resp)


async def test_clean_area_passes_the_area_as_service_data_not_as_a_selector():
    """The whole point of this tool's shape.

    `area` must reach the service as cleaning_area_id and must NOT reach entity
    resolution: passing it as a selector would target only vacuums registered to
    that area (wrong: the vacuum lives elsewhere and drives there) and would drag
    in whatever else the area holds.
    """
    hass = _hass_with_features({"vacuum.rosie": VACUUM_CLEAN_AREA_BIT})
    resolve = MagicMock(return_value=["vacuum.rosie"])
    action = AsyncMock(return_value=({}, "allowed", "x"))
    patcher = _patch_areas([_area("study_id", "Study")])
    try:
        with patch(f"{NATIVE}.resolve_intent_entities", resolve), \
                patch(f"{NATIVE}._tool_intent_action", action):
            await _tool_hass_vacuum_clean_area(
                {"area": "study", "name": "Rosie"}, _token(), hass)
    finally:
        patcher.stop()

    assert resolve.call_args.kwargs.get("area") is None, "area must not scope entity resolution"
    assert resolve.call_args.kwargs["domains"] == ["vacuum"]
    assert resolve.call_args.kwargs["name"] == "Rosie"
    service_data = action.call_args.args[3]
    assert service_data == {"cleaning_area_id": ["study_id"]}


async def test_clean_area_filters_to_vacuums_that_support_it():
    hass = _hass_with_features({
        "vacuum.rosie": VACUUM_CLEAN_AREA_BIT,
        "vacuum.basic": VACUUM_START_BIT,
    })
    resolve = MagicMock(return_value=["vacuum.rosie", "vacuum.basic"])
    action = AsyncMock(return_value=({}, "allowed", "x"))
    patcher = _patch_areas([_area("study_id", "Study")])
    try:
        with patch(f"{NATIVE}.resolve_intent_entities", resolve), \
                patch(f"{NATIVE}._tool_intent_action", action):
            await _tool_hass_vacuum_clean_area({"area": "Study"}, _token(), hass)
    finally:
        patcher.stop()
    assert action.call_args.args[4] == ["vacuum.rosie"]


async def test_clean_area_rejects_a_wrong_shaped_area():
    # str_arg degrades a list to absent rather than stringifying it, so this is
    # the missing-argument path and not a lookup for the literal "['Study']".
    _resp, outcome, _res = await _tool_hass_vacuum_clean_area(
        {"area": ["Study"]}, _token(), MagicMock())
    assert outcome == "invalid_request"


# ---------------------------------------------------------------------------
# Area matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["Study", "study", "  STUDY  ", "study_id", "den"])
def test_areas_matching_accepts_name_id_and_alias_case_insensitively(query):
    patcher = _patch_areas([_area("study_id", "Study", aliases=("Den",))])
    try:
        assert [a.id for a in _areas_matching(MagicMock(), query)] == ["study_id"]
    finally:
        patcher.stop()


def test_areas_matching_is_exact_not_substring():
    # A substring match would send a vacuum to the wrong room on a near miss.
    patcher = _patch_areas([_area("study_id", "Study")])
    try:
        assert _areas_matching(MagicMock(), "stud") == []
        assert _areas_matching(MagicMock(), "") == []
    finally:
        patcher.stop()
