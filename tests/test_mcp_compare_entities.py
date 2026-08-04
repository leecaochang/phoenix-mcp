"""Tests for compare_entities: can one entity stand in for another.

The tool exists because of four real substitution traps found while migrating a
live instance from one integration to another, and each one has a test here
named after it. Three shapes matter and they fail differently: an option set
RENAMED between two integrations (preset_modes boost -> speed) breaks every
automation naming the old value while both entities still look like climate
entities; a range NARROWED (min_temp 7 -> 16) breaks only the calls outside the
new bounds; and an attribute one side declares and the other does not
(target_temp_step) silently changes how every value is rendered. The fourth,
an attribute with no counterpart at all, is the missing_in_compare_to case.

The security-shaped tests are the two that matter most to keep: scrubbing runs
BEFORE the diff, so a sensitive attribute cannot reappear as a difference
between two entities, and a refusal names the tool rather than the entity, so
comparing against an id the token cannot see never reveals which side was the
problem.
"""

from __future__ import annotations

import json
import uuid

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import MAX_COMPARE_LIST_VALUES, MAX_COMPARE_VALUE_CHARS
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from unittest.mock import MagicMock


def _token(domains=("climate", "sensor"), **caps) -> TokenRecord:
    tree = PermissionTree(domains={d: PermissionNode(state="GREEN") for d in domains})
    base = {"cap_search": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=tree, **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _compare(hass, token, entity_id="climate.old", compare_to="climate.new"):
    return await _call_tool(
        "compare_entities",
        {"entity_id": entity_id, "compare_to": compare_to},
        token, hass, MagicMock(),
    )


class TestGating:
    async def test_cap_search_denied(self, hass):
        _, outcome, resource = await _compare(hass, _token(cap_search="deny"))
        assert outcome == "denied"
        assert resource == "compare_entities"

    @pytest.mark.parametrize("args", [
        {"compare_to": "climate.new"},
        {"entity_id": "climate.old"},
        {"entity_id": "climate.old", "compare_to": ""},
        # A model that sends a list where a string is declared has its argument
        # degraded to ABSENT by str_arg, so it is refused as missing rather than
        # stringified into an id nobody typed.
        {"entity_id": ["climate.old"], "compare_to": "climate.new"},
    ])
    async def test_missing_or_wrong_shaped_arguments(self, hass, args):
        content, outcome, _ = await _call_tool("compare_entities", args, _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "Missing required argument" in content["content"][0]["text"]


class TestScopeRefusalsAreIndistinguishable:
    """Rule 12: an out-of-scope entity and a nonexistent one answer identically."""

    async def test_out_of_scope_and_nonexistent_match_byte_for_byte(self, hass):
        hass.states.async_set("climate.old", "cool", {})
        hass.states.async_set("lock.secret", "locked", {})
        token = _token()  # climate + sensor only, no lock
        out_of_scope, o1, r1 = await _compare(hass, token, compare_to="lock.secret")
        nonexistent, o2, r2 = await _compare(hass, token, compare_to="lock.absent")
        assert (o1, o2) == ("not_found", "not_found")
        assert out_of_scope == nonexistent

    async def test_refusal_never_names_which_side_failed(self, hass):
        """The resource lands in the audit log, so naming the id there would make
        the log itself the oracle the response body refuses to be."""
        hass.states.async_set("climate.old", "cool", {})
        _, _, resource = await _compare(hass, _token(), compare_to="lock.secret")
        assert resource == "compare_entities"

    async def test_registry_only_entity_with_no_state_is_not_found(self, hass):
        hass.states.async_set("climate.old", "cool", {})
        _, outcome, _ = await _compare(hass, _token(), compare_to="climate.never_set")
        assert outcome == "not_found"


class TestTheFourMigrationTraps:
    async def test_renamed_option_set_reads_as_a_removal_beside_an_addition(self, hass):
        """Trap 1, and the reason the list branch exists: both entities declare
        preset_modes with three values, so a caller reading the two lists side by
        side has to spot the rename itself."""
        hass.states.async_set("climate.old", "cool", {
            "preset_modes": ["boost", "wind_free", "wind_free_sleep", "eco"],
        })
        hass.states.async_set("climate.new", "cool", {
            "preset_modes": ["speed", "nano", "nanosleep", "eco"],
        })
        content, outcome, _ = await _compare(hass, _token())
        assert outcome == "allowed"
        entry = next(c for c in _json(content)["changed"] if c["attribute"] == "preset_modes")
        assert entry["removed"] == ["boost", "wind_free", "wind_free_sleep"]
        assert entry["added"] == ["speed", "nano", "nanosleep"]
        assert entry["removed_total"] == 3
        assert entry["added_total"] == 3
        assert entry["truncated"] is False
        assert "eco" not in entry["removed"] and "eco" not in entry["added"]

    async def test_narrowed_range_reports_both_bounds(self, hass):
        """Trap 2: a scalar pair, so the caller sees the direction of the move."""
        hass.states.async_set("climate.old", "cool", {"min_temp": 7, "max_temp": 35})
        hass.states.async_set("climate.new", "cool", {"min_temp": 16.0, "max_temp": 30.0})
        content, _, _ = await _compare(hass, _token())
        changed = {c["attribute"]: c for c in _json(content)["changed"]}
        assert changed["min_temp"]["value"] == 7
        assert changed["min_temp"]["compare_value"] == 16.0
        assert changed["max_temp"]["value"] == 35
        assert changed["max_temp"]["compare_value"] == 30.0

    async def test_attribute_only_the_replacement_declares(self, hass):
        """Trap 3, the one a human caught and the agent did not: nothing about
        target_temp_step looks wrong, it just makes every rendered value change
        from 26.0 to 26."""
        hass.states.async_set("climate.old", "cool", {"temperature": 26.0})
        hass.states.async_set("climate.new", "cool", {"temperature": 26.0, "target_temp_step": 1.0})
        content, _, _ = await _compare(hass, _token())
        body = _json(content)
        assert body["added_in_compare_to"] == [{"attribute": "target_temp_step", "value": 1.0}]
        assert body["missing_in_compare_to"] == []

    async def test_attribute_with_no_counterpart_is_the_breaking_direction(self, hass):
        """Trap 4: the replacement has nothing standing in for job_state, so
        anything reading it breaks outright rather than reading a wrong value."""
        hass.states.async_set("sensor.old_job", "finish", {
            "options": ["idle", "run", "finish"], "friendly_name": "Job State",
        })
        hass.states.async_set("sensor.new_machine", "idle", {
            "options": ["idle", "active", "pause"], "friendly_name": "Machine State",
        })
        content, _, _ = await _compare(
            hass, _token(), entity_id="sensor.old_job", compare_to="sensor.new_machine",
        )
        body = _json(content)
        assert body["state"] == "finish"
        assert body["compare_state"] == "idle"
        entry = next(c for c in body["changed"] if c["attribute"] == "options")
        assert entry["removed"] == ["run", "finish"]
        assert entry["added"] == ["active", "pause"]


class TestReportShape:
    async def test_identical_entities_report_no_differences(self, hass):
        attrs = {"min_temp": 7, "hvac_modes": ["off", "cool"], "friendly_name": "A/C"}
        hass.states.async_set("climate.old", "cool", attrs)
        hass.states.async_set("climate.new", "cool", dict(attrs))
        body = _json((await _compare(hass, _token()))[0])
        assert body["changed"] == []
        assert body["missing_in_compare_to"] == []
        assert body["added_in_compare_to"] == []
        assert body["identical_count"] == 3

    async def test_domains_and_states_are_reported_for_a_cross_domain_compare(self, hass):
        """A split (one old entity replaced by two of a different domain) is a
        real migration shape, so a domain mismatch is reported, never refused."""
        hass.states.async_set("sensor.old", "42", {})
        hass.states.async_set("climate.new", "cool", {})
        body = _json((await _compare(hass, _token(), "sensor.old", "climate.new"))[0])
        assert body["domain"] == "sensor"
        assert body["compare_domain"] == "climate"
        assert body["state"] == "42"
        assert body["compare_state"] == "cool"

    async def test_note_points_at_history_for_values_that_vary(self, hass):
        """The washer trap was only resolvable by comparing histories across one
        wash cycle. A snapshot tool that does not say so invites reading a clean
        report as proof the two entities behave the same."""
        hass.states.async_set("climate.old", "cool", {})
        hass.states.async_set("climate.new", "cool", {})
        assert "get_history" in _json((await _compare(hass, _token()))[0])["note"]

    async def test_reordered_list_says_so_rather_than_showing_two_empty_arrays(self, hass):
        hass.states.async_set("climate.old", "cool", {"hvac_modes": ["off", "cool", "heat"]})
        hass.states.async_set("climate.new", "cool", {"hvac_modes": ["heat", "off", "cool"]})
        body = _json((await _compare(hass, _token()))[0])
        entry = next(c for c in body["changed"] if c["attribute"] == "hvac_modes")
        assert entry["reordered"] is True
        assert entry["removed"] == [] and entry["added"] == []

    async def test_membership_change_is_not_flagged_as_a_reorder(self, hass):
        hass.states.async_set("climate.old", "cool", {"hvac_modes": ["off", "cool"]})
        hass.states.async_set("climate.new", "cool", {"hvac_modes": ["cool", "heat"]})
        entry = next(
            c for c in _json((await _compare(hass, _token()))[0])["changed"]
            if c["attribute"] == "hvac_modes"
        )
        assert "reordered" not in entry


class TestValuesAreBounded:
    async def test_long_option_lists_report_totals_beside_the_clipped_page(self, hass):
        """A light's effect_list runs to hundreds of entries. Clipping without a
        total would let a caller read a partial rename as the whole of it."""
        old = [f"effect_{i}" for i in range(MAX_COMPARE_LIST_VALUES + 10)]
        new = [f"scene_{i}" for i in range(MAX_COMPARE_LIST_VALUES + 10)]
        hass.states.async_set("climate.old", "cool", {"effect_list": old})
        hass.states.async_set("climate.new", "cool", {"effect_list": new})
        entry = next(
            c for c in _json((await _compare(hass, _token()))[0])["changed"]
            if c["attribute"] == "effect_list"
        )
        assert len(entry["removed"]) == MAX_COMPARE_LIST_VALUES
        assert entry["removed_total"] == MAX_COMPARE_LIST_VALUES + 10
        assert len(entry["added"]) == MAX_COMPARE_LIST_VALUES
        assert entry["added_total"] == MAX_COMPARE_LIST_VALUES + 10
        assert entry["truncated"] is True

    async def test_a_long_string_value_is_clipped_and_says_how_much_is_missing(self, hass):
        hass.states.async_set("climate.old", "cool", {"note": "a" * (MAX_COMPARE_VALUE_CHARS + 50)})
        hass.states.async_set("climate.new", "cool", {"note": "b"})
        entry = next(
            c for c in _json((await _compare(hass, _token()))[0])["changed"]
            if c["attribute"] == "note"
        )
        assert entry["value"].startswith("a" * MAX_COMPARE_VALUE_CHARS)
        assert "50 more characters" in entry["value"]

    @pytest.mark.parametrize("value,marker", [
        ([{"temp": 1}, {"temp": 2}], "list with 2 entries"),
        ({"a": 1, "b": 2, "c": 3}, "dict with 3 entries"),
    ])
    async def test_structured_values_are_named_by_type_never_echoed(self, hass, value, marker):
        """A weather forecast or a nested mapping has no member-wise reading a
        caller acts on, and echoing both sides would dominate the response."""
        hass.states.async_set("climate.old", "cool", {"payload": value})
        hass.states.async_set("climate.new", "cool", {"payload": []})
        entry = next(
            c for c in _json((await _compare(hass, _token()))[0])["changed"]
            if c["attribute"] == "payload"
        )
        assert entry["value"] == {"omitted": marker}

    async def test_a_scalar_list_longer_than_the_cap_is_named_by_type_on_the_absent_side(self, hass):
        """missing_in_compare_to carries the value for context, so it needs the
        same bound the changed entries have."""
        hass.states.async_set("climate.old", "cool", {
            "big": [f"v{i}" for i in range(MAX_COMPARE_LIST_VALUES + 1)],
        })
        hass.states.async_set("climate.new", "cool", {})
        body = _json((await _compare(hass, _token()))[0])
        assert body["missing_in_compare_to"][0]["value"] == {
            "omitted": f"list with {MAX_COMPARE_LIST_VALUES + 1} entries"
        }


class TestAnOfflineEntityIsNotReportedAsAPoorReplacement:
    """Live-found on the first smoke test against a real pair.

    An offline entity answers every read and simply publishes almost nothing, so
    the comparison does not fail: it silently turns into a list of attributes the
    other side has, which describes the OUTAGE and reads exactly like a
    description of the replacement's limits. Five attributes the unit publishes
    perfectly well when it is up were reported as missing from it.
    """

    async def test_an_unavailable_side_is_warned_about_by_name(self, hass):
        hass.states.async_set("climate.old", "cool", {
            "temperature": 22, "fan_mode": "auto", "min_temp": 7,
        })
        hass.states.async_set("climate.new", "unavailable", {})
        body = _json((await _compare(hass, _token()))[0])
        assert len(body["warnings"]) == 1
        assert "climate.new" in body["warnings"][0]
        assert "unavailable" in body["warnings"][0]
        # The warning has to reach the same reader the misleading list does.
        assert {e["attribute"] for e in body["missing_in_compare_to"]} == {
            "temperature", "fan_mode", "min_temp",
        }

    async def test_a_restored_state_is_warned_about_even_when_it_is_not_unavailable(self, hass):
        """HA's own marker for a state it rebuilt from the registry because the
        integration supplied none. It is the precise signal, and it can carry a
        real-looking state value while the attributes are still not the
        integration's."""
        hass.states.async_set("climate.old", "cool", {"min_temp": 7})
        hass.states.async_set("climate.new", "off", {"restored": True})
        body = _json((await _compare(hass, _token()))[0])
        assert len(body["warnings"]) == 1
        assert "climate.new" in body["warnings"][0]

    async def test_both_sides_offline_warns_about_both(self, hass):
        hass.states.async_set("climate.old", "unavailable", {})
        hass.states.async_set("climate.new", "unavailable", {})
        body = _json((await _compare(hass, _token()))[0])
        assert len(body["warnings"]) == 2
        assert "climate.old" in body["warnings"][0]
        assert "climate.new" in body["warnings"][1]

    async def test_two_healthy_entities_carry_no_warnings_key_at_all(self, hass):
        """Conditional, not a permanent "degraded": false. A field that is always
        present is a field the reader learns to skip."""
        hass.states.async_set("climate.old", "cool", {"min_temp": 7})
        hass.states.async_set("climate.new", "cool", {"min_temp": 7})
        assert "warnings" not in _json((await _compare(hass, _token()))[0])

    async def test_unknown_is_not_warned_about(self, hass):
        """An entity with no value yet still publishes its attributes, so
        warning here would fire on the ordinary case and teach the reader to
        ignore the field."""
        hass.states.async_set("climate.old", "cool", {"min_temp": 7})
        hass.states.async_set("climate.new", "unknown", {"min_temp": 7, "max_temp": 30})
        assert "warnings" not in _json((await _compare(hass, _token()))[0])


class TestSensitiveAttributesCannotReappearAsADifference:
    async def test_scrubbed_attributes_are_absent_from_every_array(self, hass):
        """Scrubbing runs before the diff, not after. Two cameras differing only
        in their access_token would otherwise have this tool print both."""
        hass.states.async_set("climate.old", "cool", {
            "access_token": "OLD-SECRET", "entity_picture": "/api/old", "min_temp": 7,
        })
        hass.states.async_set("climate.new", "cool", {
            "access_token": "NEW-SECRET", "entity_picture": "/api/new", "min_temp": 7,
        })
        content, outcome, _ = await _compare(hass, _token())
        assert outcome == "allowed"
        raw = content["content"][0]["text"]
        assert "OLD-SECRET" not in raw and "NEW-SECRET" not in raw
        body = json.loads(raw)
        assert body["changed"] == []
        assert [e["attribute"] for e in body["missing_in_compare_to"]] == []
