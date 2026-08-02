"""Tests for the token-scoped mesa_* MCP tools.

The priority is the no-enumeration-oracle guarantee: query counts, get, and
explain must all be filtered to the token's permission scope, and an
out-of-scope entity must be byte-identical to a nonexistent one.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mesa import (
    _moment_example_config,
    _numeric_threshold_example,
    async_setup_mesa,
)
from custom_components.phoenix_mcp.mesa_core import ControlMode, MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.mesa_tools import (
    authored_restrictions,
    async_call_mesa_tool,
    mesa_tool_defs,
)
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _token(*, cap_config_read="allow", pass_through=False, domains=None) -> TokenRecord:
    return TokenRecord(
        id="tok",
        name="scoped",
        token_hash="x",
        created_at=utcnow(),
        created_by="admin",
        persona="read_only",
        cap_config_read=cap_config_read,
        pass_through=pass_through,
        permissions=PermissionTree(
            domains={k: PermissionNode(state=v) for k, v in (domains or {}).items()},
        ),
    )


async def _data_with_profiles(hass: HomeAssistant) -> PhoenixData:
    runtime = await async_setup_mesa(hass, "advisory")
    for eid in ("light.a", "switch.b"):
        runtime.store.set(
            eid,
            SemanticProfile.from_dict(
                eid,
                {"semantic_profile": {"semantic_tags": ["lighting.ambient"]}},
                default_origin=MetadataOrigin.USER,
            ),
        )
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
                   mesa=runtime)
    return data


@pytest.fixture
def env(hass: HomeAssistant):
    hass.states.async_set("light.a", "on", {})
    hass.states.async_set("switch.b", "off", {})
    # In scope (light GREEN) and existing, but no MESA profile: exercises
    # mesa-core's own not_found path through the scoped store.
    hass.states.async_set("light.unprofiled", "on", {})
    return hass


async def _call(tool, args, token, hass, data):
    result, outcome, resource = await async_call_mesa_tool(tool, args, token, hass, data, "sess")
    text = result["content"][0]["text"]
    return json.loads(text), outcome, result.get("isError", False)


@pytest.mark.asyncio
async def test_query_returns_only_in_scope_entities(env):
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})  # lights only; switch is out of scope
    payload, outcome, _ = await _call("mesa_query_profiles", {}, token, env, data)
    ids = [r["entity_id"] for r in payload["results"]]
    assert ids == ["light.a"]
    assert payload["total_matched"] == 1  # count is scope-relative, no oracle


@pytest.mark.asyncio
async def test_get_out_of_scope_is_identical_to_nonexistent(env):
    # switch.b exists and has a profile but is out of scope for this token. Its
    # response must be byte-identical to mesa-core's genuine not_found envelope,
    # so the token cannot tell the entity exists or carries a profile.
    from custom_components.phoenix_mcp.mesa_tools import _not_found_envelope

    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})

    out_of_scope, _, _ = await _call("mesa_get_profile", {"entity_id": "switch.b"}, token, env, data)
    # An in-scope but unprofiled entity hits mesa-core's own not_found path.
    in_scope_missing, _, _ = await _call("mesa_get_profile", {"entity_id": "light.unprofiled"}, token, env, data)

    assert out_of_scope == _not_found_envelope("switch.b")
    assert in_scope_missing == _not_found_envelope("light.unprofiled")


@pytest.mark.asyncio
async def test_explain_out_of_scope_is_not_found(env):
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, _ = await _call("mesa_explain_profile", {"entity_id": "switch.b"}, token, env, data)
    assert payload["error"] == "not_found"
    assert outcome == "not_found"


@pytest.mark.asyncio
async def test_get_in_scope_returns_profile(env):
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, is_error = await _call("mesa_get_profile", {"entity_id": "light.a"}, token, env, data)
    assert outcome == "allowed"
    assert is_error is False
    assert payload["entity_id"] == "light.a"


@pytest.mark.asyncio
async def test_cap_deny_forbids(env):
    data = await _data_with_profiles(env)
    token = _token(cap_config_read="deny", domains={"light": "GREEN"})
    result, outcome, resource = await async_call_mesa_tool(
        "mesa_query_profiles", {}, token, env, data, "sess"
    )
    assert outcome == "denied"
    assert result["isError"] is True


@pytest.mark.asyncio
async def test_pass_through_sees_all_non_phoenix(env):
    data = await _data_with_profiles(env)
    token = _token(pass_through=True)
    payload, _, _ = await _call("mesa_query_profiles", {}, token, env, data)
    ids = sorted(r["entity_id"] for r in payload["results"])
    assert ids == ["light.a", "switch.b"]


@pytest.mark.asyncio
async def test_caller_context_reports_token_identity(env):
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, _, _ = await _call("mesa_get_caller_context", {}, token, env, data)
    assert payload["caller_id"] == "tok"
    assert payload["roles"] == ["read_only"]


def test_mesa_tool_defs_carry_cap():
    defs = mesa_tool_defs()
    assert {d["name"] for d in defs} == {
        "mesa_query_profiles", "mesa_get_profile",
        "mesa_explain_profile", "mesa_get_caller_context",
        "mesa_request_lease", "mesa_release_lease",
    }
    assert all(d["cap"] == "cap_config_read" for d in defs)
    assert all("inputSchema" in d for d in defs)


# ---- authored_restrictions (get_overview MESA summary) -----------------------


async def _runtime_with_modes(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "enforced")
    profiles = {
        "lock.gun_safe": ("prohibited", "Never operate the gun safe."),
        "switch.sump_pump": ("read_only", "Observe only; never switch off."),
        "light.dimmer": ("confirm", "Ask before changing."),
        "fan.patio": ("autonomous", "Free to control."),
    }
    for eid, (mode, reason) in profiles.items():
        runtime.store.set(
            eid,
            SemanticProfile.from_dict(
                eid,
                {"semantic_profile": {"operational_boundaries": {
                    "control_mode": mode, "control_reason": reason}}},
                default_origin=MetadataOrigin.USER,
            ),
        )
    return runtime


@pytest.mark.asyncio
async def test_authored_restrictions_lists_only_authored_restrictive(hass: HomeAssistant):
    for eid in ("lock.gun_safe", "switch.sump_pump", "light.dimmer", "fan.patio"):
        hass.states.async_set(eid, "on", {})
    runtime = await _runtime_with_modes(hass)
    token = _token(domains={"lock": "GREEN", "switch": "GREEN", "light": "GREEN", "fan": "GREEN"})

    summary = authored_restrictions(runtime, token, hass)

    # Counts cover every authored profile; the list is only the hard restrictions.
    assert summary["authored_profile_count"] == 4
    assert summary["by_control_mode"] == {
        "autonomous": 1, "confirm": 1, "prohibited": 1, "read_only": 1}
    restricted = {e["entity_id"]: e for e in summary["restricted_entities"]}
    assert set(restricted) == {"lock.gun_safe", "switch.sump_pump"}  # not confirm/autonomous
    assert restricted["lock.gun_safe"]["control_mode"] == "prohibited"
    assert restricted["lock.gun_safe"]["reason"] == "Never operate the gun safe."


@pytest.mark.asyncio
async def test_authored_restrictions_is_scope_relative(hass: HomeAssistant):
    for eid in ("lock.gun_safe", "switch.sump_pump"):
        hass.states.async_set(eid, "on", {})
    runtime = await _runtime_with_modes(hass)
    token = _token(domains={"lock": "GREEN"})  # switch.sump_pump out of scope

    summary = authored_restrictions(runtime, token, hass)

    assert {e["entity_id"] for e in summary["restricted_entities"]} == {"lock.gun_safe"}
    assert "read_only" not in summary["by_control_mode"]  # hidden entity not even counted


@pytest.mark.asyncio
async def test_authored_restrictions_empty_without_authored_profiles(hass: HomeAssistant):
    hass.states.async_set("light.a", "on", {})
    runtime = await async_setup_mesa(hass, "enforced")  # no entity profiles authored
    token = _token(domains={"light": "GREEN"})

    summary = authored_restrictions(runtime, token, hass)

    # Baseline-derived modes are never counted; only authored profiles appear.
    assert summary["authored_profile_count"] == 0
    assert summary["restricted_entities"] == []


# ---------------------------------------------------------------------------
# Semantic moments (mesa-core 1.2, HA purpose-specific triggers)
# ---------------------------------------------------------------------------


def _patch_semantic_schema_lookups(
    monkeypatch, *, trigger_schemas=None, condition_schemas=None,
    trigger_error=None, condition_error=None,
):
    """Monkeypatch the schema-enrichment lookups async_semantic_moments makes.

    Separate from the id lookups (async_get_triggers/conditions_for_target),
    tested elsewhere: these back the per-moment "schema" field.
    """

    async def _triggers(hass):
        if trigger_error:
            raise trigger_error
        return trigger_schemas or {}

    async def _conditions(hass):
        if condition_error:
            raise condition_error
        return condition_schemas or {}

    monkeypatch.setattr(
        "homeassistant.helpers.trigger.async_get_all_descriptions", _triggers,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.condition.async_get_all_descriptions", _conditions,
    )


@pytest.mark.asyncio
async def test_get_profile_includes_semantic_moments(env, monkeypatch):
    """include_semantic_moments=True returns the live HA vocabulary, tagged by
    kind, with each moment's schema (target/fields) attached when HA has one."""

    async def _fake_triggers(hass, target, expand_group):
        assert target == {"entity_id": "light.a"}
        assert expand_group is False
        return {"light.turned_on", "light.turned_off"}

    async def _fake_conditions(hass, target, expand_group):
        return {"light.is_on"}

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _fake_triggers,
    )
    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_conditions_for_target",
        _fake_conditions,
    )
    # Only light.turned_on has a schema in this fake registry, to prove the
    # per-id lookup is independent (turned_off and is_on omit "schema").
    _patch_semantic_schema_lookups(
        monkeypatch,
        trigger_schemas={"light.turned_on": {"target": {"entity": {"domain": "light"}}}},
    )
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, is_error = await _call(
        "mesa_get_profile",
        {"entity_id": "light.a", "include_semantic_moments": True},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert payload["semantic_moments"] == [
        {"id": "light.turned_off", "kind": "trigger"},
        {
            "id": "light.turned_on",
            "kind": "trigger",
            "schema": {"target": {"entity": {"domain": "light"}}},
        },
        {"id": "light.is_on", "kind": "condition"},
    ]


@pytest.mark.asyncio
async def test_get_profile_semantic_moments_schema_lookup_failure_keeps_ids(env, monkeypatch):
    """A failure fetching schemas is independent of the id fetch succeeding:
    the moments still come back, just without a "schema" key on any of them."""

    async def _fake_triggers(hass, target, expand_group):
        return {"light.turned_on"}

    async def _fake_conditions(hass, target, expand_group):
        return set()

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _fake_triggers,
    )
    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_conditions_for_target",
        _fake_conditions,
    )
    _patch_semantic_schema_lookups(
        monkeypatch, trigger_error=RuntimeError("HA refactored the schema cache"),
    )
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, is_error = await _call(
        "mesa_get_profile",
        {"entity_id": "light.a", "include_semantic_moments": True},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert payload["semantic_moments"] == [{"id": "light.turned_on", "kind": "trigger"}]


@pytest.mark.asyncio
async def test_get_profile_semantic_moments_flag_off_no_lookup(env, monkeypatch):
    """Without the flag the HA lookup never runs and the key is absent."""

    async def _boom(hass, target, expand_group):
        raise AssertionError("HA lookup must not run when the flag is off")

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _boom,
    )
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, _, _ = await _call(
        "mesa_get_profile", {"entity_id": "light.a"}, token, env, data,
    )
    assert "semantic_moments" not in payload


# The numeric_threshold selector shapes below are the two real forms observed
# live: temperature carries the unit as a selector-level list, percentage carries
# it as a string inside the number spec. Both config outputs were verified valid
# against HA's own validate_config.
_TEMP_THRESHOLD_SCHEMA = {
    "fields": {
        "threshold": {
            "required": True,
            "selector": {
                "numeric_threshold": {
                    "mode": "crossed",
                    "number": {"mode": "box", "step": 1.0},
                    "unit_of_measurement": ["°C", "°F"],
                }
            },
        },
        "behavior": {"selector": {"automation_behavior": {"mode": "trigger"}}},
    }
}
_VOLUME_THRESHOLD_SCHEMA = {
    "fields": {
        "threshold": {
            "required": True,
            "selector": {
                "numeric_threshold": {
                    "mode": "crossed",
                    "number": {"min": 0.0, "max": 100.0, "unit_of_measurement": "%"},
                }
            },
        }
    }
}


def test_numeric_threshold_example_temperature_unit_from_list():
    # Unit is a selector-level list -> take the first; no min/max -> placeholder 20.
    assert _numeric_threshold_example(
        _TEMP_THRESHOLD_SCHEMA["fields"]["threshold"]["selector"]["numeric_threshold"]
    ) == {"type": "above", "value": {"number": 20, "unit_of_measurement": "°C"}}


def test_numeric_threshold_example_percent_unit_from_number_and_midpoint():
    # Unit is a string inside number; min/max present -> midpoint number.
    assert _numeric_threshold_example(
        _VOLUME_THRESHOLD_SCHEMA["fields"]["threshold"]["selector"]["numeric_threshold"]
    ) == {"type": "above", "value": {"number": 50, "unit_of_measurement": "%"}}


def test_moment_example_config_builds_valid_shape_for_numeric_threshold_trigger():
    example = _moment_example_config(
        "climate.target_temperature_crossed_threshold",
        "trigger",
        "climate.home_office_ac",
        _TEMP_THRESHOLD_SCHEMA,
    )
    # The config shape HA actually accepts: options.threshold.{type, value:{...}},
    # NOT a bare threshold value (which is what the selector schema misleadingly
    # suggests and what a model fails to reverse-engineer).
    assert example == {
        "trigger": "climate.target_temperature_crossed_threshold",
        "target": {"entity_id": "climate.home_office_ac"},
        "options": {
            "threshold": {"type": "above", "value": {"number": 20, "unit_of_measurement": "°C"}}
        },
    }


def test_moment_example_config_condition_uses_condition_key():
    # A condition moment serializes identically but under a "condition" key (both
    # verified valid against HA's validate_config); the is_* conditions are
    # above/below threshold checks, not exact-equality, so they take a type too.
    example = _moment_example_config(
        "media_player.is_volume",
        "condition",
        "media_player.living_room_tv",
        _VOLUME_THRESHOLD_SCHEMA,
    )
    assert example == {
        "condition": "media_player.is_volume",
        "target": {"entity_id": "media_player.living_room_tv"},
        "options": {
            "threshold": {"type": "above", "value": {"number": 50, "unit_of_measurement": "%"}}
        },
    }


def test_moment_example_config_omitted_when_no_serializable_field():
    # A simple state trigger (no numeric_threshold field) gets no example rather
    # than a guessed one - the fields all have defaults or unknown selectors.
    schema = {"fields": {"behavior": {"selector": {"automation_behavior": {"mode": "trigger"}}}}}
    assert _moment_example_config("lock.jammed", "trigger", "lock.smart_lock", schema) is None
    assert _moment_example_config("lock.jammed", "trigger", "lock.smart_lock", {}) is None


@pytest.mark.asyncio
async def test_get_profile_semantic_moments_degrades_on_ha_failure(env, monkeypatch):
    """An HA-side failure omits the field; the profile response still succeeds."""

    async def _boom(hass, target, expand_group):
        raise RuntimeError("HA refactored the trigger API again")

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _boom,
    )
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, is_error = await _call(
        "mesa_get_profile",
        {"entity_id": "light.a", "include_semantic_moments": True},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert "semantic_moments" not in payload
    assert payload["semantic_profile"]["semantic_tags"] == ["lighting.ambient"]


@pytest.mark.asyncio
async def test_get_profile_semantic_moments_skips_lookup_when_out_of_scope(env, monkeypatch):
    """An out-of-scope entity must never reach the HA trigger/condition lookup.

    async_call_mesa_tool's visibility check runs before the semantic-moments lookup
    specifically so an out-of-scope or ghost entity never pays for (or leaks
    through) the HA call; this pins that ordering so a future refactor cannot
    silently swap it. Uses a call counter, not a raising mock: async_semantic_moments
    catches every exception broadly (degrade to None, never break the tool), so a
    raising mock would be silently swallowed there and this test would pass even
    if the ordering regressed and the wasted call happened.
    """
    calls = []

    async def _track(hass, target, expand_group):
        calls.append(target)
        return set()

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _track,
    )
    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_conditions_for_target",
        _track,
    )
    from custom_components.phoenix_mcp.mesa_tools import _not_found_envelope

    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})  # switch.b is out of scope

    payload, outcome, _ = await _call(
        "mesa_get_profile",
        {"entity_id": "switch.b", "include_semantic_moments": True},
        token, env, data,
    )
    assert outcome == "not_found"
    assert payload == _not_found_envelope("switch.b")
    assert calls == []


@pytest.mark.asyncio
async def test_get_profile_semantic_moments_empty_result_is_present_not_omitted(env, monkeypatch):
    """A real lookup that finds nothing is distinct from a lookup that failed.

    mesa-core only omits the field when the callback returns None; an empty
    list (a successful lookup, entity just has no purpose-specific triggers or
    conditions) must still appear as semantic_moments: [].
    """

    async def _empty(hass, target, expand_group):
        return set()

    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_triggers_for_target",
        _empty,
    )
    monkeypatch.setattr(
        "homeassistant.components.websocket_api.automation.async_get_conditions_for_target",
        _empty,
    )
    # No ids means the schema lookups' results are never consulted, but the
    # calls still fire unconditionally; patch them so the test does not depend
    # on real HA trigger/condition platforms being loaded.
    _patch_semantic_schema_lookups(monkeypatch)
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})
    payload, outcome, is_error = await _call(
        "mesa_get_profile",
        {"entity_id": "light.a", "include_semantic_moments": True},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert payload["semantic_moments"] == []


# ---------------------------------------------------------------------------
# Advisory lease tools (mesa-core 1.1, Enrichment Section 21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_request_release_round_trip_across_calls(env):
    """A token can release its own lease in a LATER stateless call.

    Proves the token-id-as-session design: the two async_call_mesa_tool invocations
    carry different transport session ids ("sess" both times here, but a fresh
    request id in production), yet the release matches because leases key on
    the token.
    """
    events = []
    env.bus.async_listen("phoenix_mcp_mesa_lease_expired", lambda e: events.append(e.data))

    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})

    granted, outcome, is_error = await _call(
        "mesa_request_lease",
        {"entities": ["light.a"], "duration_seconds": 10},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert granted["granted"] is True
    assert granted["entities_granted"] == ["light.a"]

    released, outcome, is_error = await _call(
        "mesa_release_lease", {"lease_id": granted["lease_id"]}, token, env, data,
    )
    assert outcome == "allowed" and not is_error
    assert released["released"] is True
    assert released["entities"] == ["light.a"]

    # The early release surfaced on the HA bus via the thread-safe bridge.
    await env.async_block_till_done()
    assert any(e.get("reason") == "early_release" for e in events)


@pytest.mark.asyncio
async def test_lease_request_scope_filters_silently(env):
    """Out-of-scope entities are dropped before mesa-core ever sees them."""
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "GREEN"})  # switch.b is out of scope

    granted, outcome, is_error = await _call(
        "mesa_request_lease",
        {"entities": ["light.a", "switch.b", "light.ghost"], "duration_seconds": 5},
        token, env, data,
    )
    assert outcome == "allowed" and not is_error
    # Only the in-scope WRITE entity was leased; the dropped ids appear in
    # neither list, indistinguishable between out-of-scope and nonexistent.
    assert granted["entities_granted"] == ["light.a"]
    assert "switch.b" not in granted.get("entities_denied", [])
    assert "light.ghost" not in granted.get("entities_denied", [])


@pytest.mark.asyncio
async def test_lease_request_all_out_of_scope_is_generic_forbidden(env):
    data = await _data_with_profiles(env)
    token = _token(domains={"light": "YELLOW"})  # READ only: no lease

    result, outcome, resource = await async_call_mesa_tool(
        "mesa_request_lease",
        {"entities": ["light.a", "switch.b"], "duration_seconds": 5},
        token, env, data, "sess",
    )
    assert outcome == "denied"
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Forbidden."


@pytest.mark.asyncio
async def test_lease_cross_token_isolation(env):
    """Another token can neither release nor see a holder's lease."""
    data = await _data_with_profiles(env)
    holder = _token(domains={"light": "GREEN"})
    other = _token(domains={"light": "GREEN"})
    other.id = "tok-other"

    granted, _, _ = await _call(
        "mesa_request_lease",
        {"entities": ["light.a"], "duration_seconds": 10},
        holder, env, data,
    )
    assert granted["granted"] is True

    stolen, _, _ = await _call(
        "mesa_release_lease", {"lease_id": granted["lease_id"]}, other, env, data,
    )
    assert stolen["error"] == "lease_not_found"

    # The other token's own request on the leased entity is denied by the
    # existing-holder-wins rule, without naming the holder.
    blocked, _, _ = await _call(
        "mesa_request_lease",
        {"entities": ["light.a"], "duration_seconds": 5},
        other, env, data,
    )
    assert blocked["granted"] is False
    assert "light.a" in blocked["entities_denied"]


# --- retrieval payload shape ------------------------------------------------


@pytest.mark.asyncio
async def test_component_type_is_derived_from_the_entity_domain(env):
    """An automation, a helper and a person are not reported as plain entities.

    The field is agent-facing and was a hardcoded "entity" for every row, so an
    agent could not tell an orchestrator from the thing it orchestrates.
    """
    for eid in ("automation.morning", "input_boolean.guest", "person.sam", "light.a"):
        env.states.async_set(eid, "on", {})
    runtime = await async_setup_mesa(env, "advisory")
    for eid in ("automation.morning", "input_boolean.guest", "person.sam", "light.a"):
        runtime.store.set(
            eid,
            SemanticProfile.from_dict(
                eid, {"semantic_profile": {"semantic_tags": []}},
                default_origin=MetadataOrigin.USER,
            ),
        )
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
                       mesa=runtime)
    token = _token(pass_through=True)

    seen = {}
    for eid in ("automation.morning", "input_boolean.guest", "person.sam", "light.a"):
        payload, _, _ = await _call("mesa_get_profile", {"entity_id": eid}, token, env, data)
        seen[eid] = payload["component_type"]

    assert seen == {
        "automation.morning": "automation",
        "input_boolean.guest": "helper",
        "person.sam": "person",
        "light.a": "entity",
    }


# --- integration capability hints -------------------------------------------


def _integration_hint_profile(mode: str) -> SemanticProfile:
    """A vendor sidecar declaring only a capability hint, no operational boundary.

    Shaped like a real shipped sidecar: an inferred profile must carry its
    confidence and generation time, so a fixture without them is rejected before
    it can be stored.
    """
    return SemanticProfile.from_dict(
        "demo",
        {
            "semantic_profile": {
                "capability_semantics": {"control_mode": mode},
                "metadata_origin": {"source": "developer"},
            }
        },
        default_origin=MetadataOrigin.DEVELOPER,
    )


@pytest.mark.asyncio
async def test_integration_capability_hint_tightens_an_entity_with_no_profile(env):
    """A vendor sidecar's declared capability now participates in resolution.

    It contributes only where the same profile declares no operational
    control_mode of its own, and it enters as an ordinary most-restrictive-wins
    candidate, so it can tighten an entity and can never loosen one.
    """
    runtime = await async_setup_mesa(env, "advisory")
    runtime.store.set_integration_profile("demo", _integration_hint_profile("read_only"))

    with patch.object(runtime.store, "get_entity_integration", lambda _eid: "demo"):
        runtime.resolver.get_entity_integration = lambda _eid: "demo"
        effective = runtime.resolver.resolve("light.a")

    assert effective.operational_boundaries.control_mode is ControlMode.READ_ONLY


@pytest.mark.asyncio
async def test_a_restrictive_hint_defeats_an_operator_loosening_override(env):
    """The case worth knowing by name: a sidecar can override an operator.

    A read_only or prohibited hint populates the never-loosened branch, so an
    operator's deliberate entity-scope loosening override is ignored. It still
    fails closed, and a resolution warning names it, which is the only signal an
    operator gets that their decision did not take effect.
    """
    runtime = await async_setup_mesa(env, "advisory")
    runtime.store.set_integration_profile("demo", _integration_hint_profile("read_only"))
    runtime.store.set(
        "light.a",
        SemanticProfile.from_dict(
            "light.a",
            {
                "semantic_profile": {
                    "operational_boundaries": {
                        "control_mode": "autonomous",
                        "override_control_mode": True,
                        "control_reason": "operator accepts the risk",
                    }
                }
            },
            default_origin=MetadataOrigin.USER,
        ),
    )

    runtime.resolver.get_entity_integration = lambda _eid: "demo"
    explanation = runtime.resolver.explain("light.a")

    assert explanation.effective_profile.operational_boundaries.control_mode is ControlMode.READ_ONLY
    assert any("cannot loosen" in w for w in explanation.warnings), explanation.warnings


# --- device layer under token scoping ---------------------------------------


@pytest.mark.asyncio
async def test_device_layer_applies_through_the_scoped_store(env):
    """A device profile reaches an in-scope entity through the scoped view.

    The scoped store filters which ENTITIES a caller may address, never which
    layers contribute to one. Dropping a layer could only relax policy, since
    every layer tightens, so the store method and the host callback have to be
    present together: wiring the callback without the method makes resolution
    call a store that cannot answer.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa_tools import ScopedProfileStore

    device = er.async_get(env).async_get_or_create(
        "light", "demo", "uid_scoped", suggested_object_id="scoped"
    )
    runtime = await async_setup_mesa(env, "advisory")
    scoped = ScopedProfileStore(runtime.store, _token(domains={"light": "GREEN"}), env)

    # Both halves of the pairing are present.
    assert scoped.get_entity_device is runtime.store.get_entity_device
    assert scoped.get_device_profile("nonexistent-device") is None

    runtime.store.set_device_profile(
        "dev-1",
        SemanticProfile.from_dict(
            "dev-1",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "read_only"}}},
            default_origin=MetadataOrigin.USER,
        ),
    )
    assert scoped.get_device_profile("dev-1") is not None
    assert device.entity_id  # the registry entry exists for the scoped read above


def test_scoped_store_exposes_no_scope_enumeration():
    """Enumeration is the confidentiality surface, not resolution.

    A caller already receives the merged effective profile for entities it can
    see, so the device layer's contribution discloses nothing new. What would
    leak is EXISTENCE: the key listings name devices, areas and integrations a
    caller may have no entity access to, so none of them is exposed here.
    """
    from custom_components.phoenix_mcp.mesa_tools import ScopedProfileStore

    for name in ("device_keys", "area_keys", "integration_keys", "domain_keys"):
        assert not hasattr(ScopedProfileStore, name), (
            f"{name} would let a token enumerate objects it has no entity access to"
        )


# --- profile freshness ------------------------------------------------------


@pytest.mark.asyncio
async def test_validity_context_entity_set_is_complete_and_reusable(env):
    """The two shapes that fail silently rather than loudly.

    A one-shot iterable is drained by the first row read, after which every later
    row sees an empty set and reports real entities as removed. An incomplete set
    has the same effect for whatever it omits, and neither source alone is
    complete: the registry omits entities with no unique ID, the state machine
    omits disabled ones.
    """
    from collections.abc import Collection

    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa_tools import _build_validity_context

    registry = er.async_get(env)
    registered = registry.async_get_or_create(
        "light", "demo", "uid_reg", suggested_object_id="registered"
    ).entity_id
    # A state with no registry entry: the union's other half.
    env.states.async_set("light.stateful_only", "on", {})

    context_for = _build_validity_context(env)
    first = context_for("light.a")["known_entity_ids"]
    second = context_for("light.a")["known_entity_ids"]

    assert isinstance(first, Collection) and not isinstance(first, str)
    assert registered in first
    assert "light.stateful_only" in first
    # Reusable: reading it once must not empty it for the next entity.
    assert list(first) and list(first)
    assert set(first) == set(second)


@pytest.mark.asyncio
async def test_validity_context_omits_a_version_it_cannot_know(env):
    """A core integration has no manifest version, so the key is left out.

    Home Assistant requires ``version`` only for custom integrations. Filling the
    gap with a stand-in would either never match, invalidating every such
    profile, or always match, invalidating none; absent means the trigger is
    simply not evaluated, which is the honest answer.
    """
    from custom_components.phoenix_mcp.mesa_tools import _build_validity_context

    context = _build_validity_context(env)("light.a")
    assert "integration_version" not in context
    assert context["ha_version"]


@pytest.mark.asyncio
async def test_an_invalidated_profile_reports_stale_rather_than_current(env):
    """The end-to-end point of the callback.

    A profile naming an entity that has since left the registry is invalidated by
    its own declaration. Without the host context those triggers are parsed and
    stored but never evaluated, so the profile reports itself current forever.
    """
    runtime = await async_setup_mesa(env, "advisory")
    runtime.store.set(
        "light.a",
        SemanticProfile.from_dict(
            "light.a",
            {
                "semantic_profile": {
                    "semantic_tags": ["lighting.ambient"],
                    "profile_valid_for": {"invalidated_by_entities": ["light.long_gone"]},
                    "metadata_origin": {
                        "source": "inferred_ai",
                        "confidence": 0.9,
                        "generated_at": utcnow().isoformat(),
                    },
                }
            },
            default_origin=MetadataOrigin.INFERRED_AI,
        ),
    )
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
                       mesa=runtime)
    token = _token(pass_through=True)

    payload, _, _ = await _call("mesa_get_profile", {"entity_id": "light.a"}, token, env, data)

    assert payload["staleness_status"] == "stale"
    assert any("light.long_gone" in w for w in payload.get("warnings", [])), payload


# --- lease teardown on revocation -------------------------------------------


@pytest.mark.asyncio
async def test_revoking_a_token_releases_the_leases_it_held(env):
    """A dead token stops holding entities other tokens want.

    Bookkeeping, not safety: the token's next call is refused by authentication
    regardless, and mesa-core drops the lease on its own within the duration cap.
    This only shortens the window in which another token is told an entity is
    busy on behalf of a token that no longer exists.
    """
    from custom_components.phoenix_mcp.mesa import release_token_leases

    runtime = await async_setup_mesa(env, "advisory")
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
                       mesa=runtime)
    holder = _token(domains={"light": "GREEN"})

    granted, _, _ = await _call(
        "mesa_request_lease",
        {"entities": ["light.a"], "duration_seconds": 30},
        holder, env, data,
    )
    assert granted["granted"] is True

    assert release_token_leases(data, holder.id) == 1

    # Another token can now take the entity the revoked one was holding.
    other = _token(domains={"light": "GREEN"})
    other.id = "tok-other"
    retaken, _, _ = await _call(
        "mesa_request_lease",
        {"entities": ["light.a"], "duration_seconds": 30},
        other, env, data,
    )
    assert retaken["granted"] is True


@pytest.mark.asyncio
async def test_releasing_leases_is_safe_when_mesa_is_not_loaded(env):
    """Revocation must finish even with no MESA runtime to clean up."""
    from custom_components.phoenix_mcp.mesa import release_token_leases

    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)
    assert release_token_leases(data, "tok") == 0
