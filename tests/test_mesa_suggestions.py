"""Tests for the MESA suggestion scanner (mesa_suggestions.py).

Both signals (blast_radius, naked_risky), coverage suppression via
has_profile, the cover device-class filter, domain consolidation, the
deployment-defaults guard, dismissal filtering/pruning, and persistence.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.phoenix_mcp import mesa_suggestions
from custom_components.phoenix_mcp.const import (
    MESA_SUGGEST_CONSOLIDATE_THRESHOLD,
    MESA_SUGGEST_FANOUT_THRESHOLD,
)
from custom_components.phoenix_mcp.mesa import async_setup_mesa
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.mesa_suggestions import refresh_suggestions


def _profile(key: str, mode: str = "confirm") -> SemanticProfile:
    return SemanticProfile.from_dict(
        key,
        {"semantic_profile": {"operational_boundaries": {"control_mode": mode}}},
        default_origin=MetadataOrigin.USER,
    )


def _keys(runtime) -> list[str]:
    return [s.key for s in runtime.suggestions]


def _no_refs(monkeypatch) -> None:
    """Silence the orchestrator signal so naked-risky tests run in isolation."""
    monkeypatch.setattr(mesa_suggestions, "_automation_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_script_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_scene_members", lambda h, e: None)


# ---------------------------------------------------------------------------
# naked_risky
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_naked_lock_suggests_prohibited(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == ["naked_risky:entity:lock.front"]
    s = runtime.suggestions[0]
    assert s.suggested_mode == "prohibited"
    assert s.scope == "entity"
    assert "physical security boundary" in s.reason
    assert "matches the built-in baseline" in s.reason


@pytest.mark.asyncio
async def test_non_lock_risky_domain_suggests_confirm(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("valve.main_water", "closed")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    s = runtime.suggestions[0]
    assert s.subject_id == "valve.main_water"
    assert s.suggested_mode == "confirm"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["entity", "domain", "integration"])
async def test_authored_coverage_suppresses(hass: HomeAssistant, monkeypatch, level):
    _no_refs(monkeypatch)
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get_or_create(
        "lock", "test_locks", "uid1", suggested_object_id="front"
    )
    hass.states.async_set(entry.entity_id, "locked")
    runtime = await async_setup_mesa(hass, "advisory")
    if level == "entity":
        runtime.store.set(entry.entity_id, _profile(entry.entity_id))
    elif level == "domain":
        runtime.store.set_domain_profile("lock", _profile("lock"))
    else:
        runtime.store.set_integration_profile("test_locks", _profile("test_locks"))

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == []


@pytest.mark.asyncio
async def test_cover_device_class_filter(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("cover.garage_door", "closed", {"device_class": "garage"})
    hass.states.async_set("cover.bedroom_blind", "open", {"device_class": "shade"})
    hass.states.async_set("cover.no_class", "open")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == ["naked_risky:entity:cover.garage_door"]


@pytest.mark.asyncio
async def test_consolidation_to_domain_suggestion(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    for i in range(MESA_SUGGEST_CONSOLIDATE_THRESHOLD):
        hass.states.async_set(f"update.dev_{i}", "off")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == ["naked_risky:domain:update"]
    s = runtime.suggestions[0]
    assert s.scope == "domain"
    assert s.evidence["uncovered_count"] == MESA_SUGGEST_CONSOLIDATE_THRESHOLD
    assert len(s.evidence["examples"]) == MESA_SUGGEST_CONSOLIDATE_THRESHOLD


@pytest.mark.asyncio
async def test_below_consolidation_threshold_stays_per_entity(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    for i in range(MESA_SUGGEST_CONSOLIDATE_THRESHOLD - 1):
        hass.states.async_set(f"update.dev_{i}", "off")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    assert all(s.scope == "entity" for s in runtime.suggestions)
    assert len(runtime.suggestions) == MESA_SUGGEST_CONSOLIDATE_THRESHOLD - 1


@pytest.mark.asyncio
async def test_cover_exempt_from_consolidation(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    for i in range(MESA_SUGGEST_CONSOLIDATE_THRESHOLD + 1):
        hass.states.async_set(f"cover.garage_{i}", "closed", {"device_class": "garage"})
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    # A cover domain profile would catch blinds too, so covers never consolidate.
    assert all(s.scope == "entity" for s in runtime.suggestions)
    assert len(runtime.suggestions) == MESA_SUGGEST_CONSOLIDATE_THRESHOLD + 1


# ---------------------------------------------------------------------------
# blast_radius
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_automation_touching_lock_suggests_confirm(hass: HomeAssistant, monkeypatch):
    hass.states.async_set("automation.night", "on")
    hass.states.async_set("lock.front", "locked")
    monkeypatch.setattr(
        mesa_suggestions, "_automation_refs",
        lambda h, e: ["lock.front", "light.hall"] if e == "automation.night" else None,
    )
    monkeypatch.setattr(mesa_suggestions, "_script_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_scene_members", lambda h, e: None)
    runtime = await async_setup_mesa(hass, "advisory")
    # Covering the referenced lock does NOT suppress the orchestrator suggestion:
    # automation actions run natively and bypass per-entity gates.
    runtime.store.set("lock.front", _profile("lock.front", "prohibited"))

    refresh_suggestions(hass, runtime)

    blast = [s for s in runtime.suggestions if s.signal == "blast_radius"]
    assert [s.subject_id for s in blast] == ["automation.night"]
    assert blast[0].suggested_mode == "confirm"
    assert "lock.front" in blast[0].reason
    assert blast[0].evidence["risky_referenced"] == ["lock.front"]


@pytest.mark.asyncio
async def test_fanout_threshold(hass: HomeAssistant, monkeypatch):
    hass.states.async_set("script.wide", "off")
    hass.states.async_set("script.narrow", "off")
    refs = {
        "script.wide": [f"light.l{i}" for i in range(MESA_SUGGEST_FANOUT_THRESHOLD)],
        "script.narrow": [f"light.l{i}" for i in range(MESA_SUGGEST_FANOUT_THRESHOLD - 1)],
    }
    monkeypatch.setattr(mesa_suggestions, "_script_refs", lambda h, e: refs.get(e))
    monkeypatch.setattr(mesa_suggestions, "_automation_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_scene_members", lambda h, e: None)
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    subjects = [s.subject_id for s in runtime.suggestions]
    assert "script.wide" in subjects
    assert "script.narrow" not in subjects
    wide = next(s for s in runtime.suggestions if s.subject_id == "script.wide")
    assert wide.evidence["over_fanout"] is True
    assert "wide blast radius" in wide.reason


@pytest.mark.asyncio
async def test_orchestrator_with_own_profile_suppressed(hass: HomeAssistant, monkeypatch):
    hass.states.async_set("automation.night", "on")
    monkeypatch.setattr(mesa_suggestions, "_automation_refs", lambda h, e: ["lock.front"])
    monkeypatch.setattr(mesa_suggestions, "_script_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_scene_members", lambda h, e: None)
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set("automation.night", _profile("automation.night"))

    refresh_suggestions(hass, runtime)

    assert all(s.signal != "blast_radius" for s in runtime.suggestions)


@pytest.mark.asyncio
async def test_empty_and_unavailable_refs_excluded(hass: HomeAssistant, monkeypatch):
    hass.states.async_set("automation.empty", "on")
    hass.states.async_set("automation.unavail", "on")
    monkeypatch.setattr(
        mesa_suggestions, "_automation_refs",
        lambda h, e: [] if e == "automation.empty" else None,
    )
    monkeypatch.setattr(mesa_suggestions, "_script_refs", lambda h, e: None)
    monkeypatch.setattr(mesa_suggestions, "_scene_members", lambda h, e: None)
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    assert runtime.suggestions == []


@pytest.mark.asyncio
async def test_scene_members_from_state_attribute(hass: HomeAssistant):
    # No monkeypatch: _scene_members reads the real state attribute.
    hass.states.async_set(
        "scene.goodnight", "scening", {"entity_id": ["lock.front", "light.hall"]}
    )
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")

    refresh_suggestions(hass, runtime)

    blast = [s for s in runtime.suggestions if s.signal == "blast_radius"]
    assert [s.subject_id for s in blast] == ["scene.goodnight"]


# ---------------------------------------------------------------------------
# Deployment-defaults guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_default_confirm_suppresses_all(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_deployment_defaults({"default_control_mode": "confirm"})

    refresh_suggestions(hass, runtime)

    assert runtime.suggestions == []


@pytest.mark.asyncio
async def test_deployment_default_autonomous_does_not_suppress(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_deployment_defaults({"default_control_mode": "autonomous"})

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == ["naked_risky:entity:lock.front"]


@pytest.mark.asyncio
async def test_domain_override_suppresses_just_that_domain(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("valve.main", "closed")
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_deployment_defaults({
        "default_control_mode": "autonomous",
        "domain_overrides": {"lock": {"control_mode": "prohibited"}},
    })

    refresh_suggestions(hass, runtime)

    assert _keys(runtime) == ["naked_risky:entity:valve.main"]


# ---------------------------------------------------------------------------
# Dismissals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismissed_key_filtered_and_pruned_when_subject_gone(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.dismissed_suggestions = {
        "naked_risky:entity:lock.front",
        "naked_risky:entity:lock.gone",  # subject no longer exists: pruned
        "garbage-key",                    # malformed: dropped
    }

    refresh_suggestions(hass, runtime)

    assert runtime.suggestions == []
    assert runtime.dismissed_suggestions == {"naked_risky:entity:lock.front"}


@pytest.mark.asyncio
async def test_dismissals_persist_round_trip(hass: HomeAssistant, monkeypatch):
    _no_refs(monkeypatch)
    hass.states.async_set("lock.front", "locked")
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.dismissed_suggestions.add("naked_risky:entity:lock.front")
    async with runtime.lock:
        await runtime.async_save()

    reloaded = await async_setup_mesa(hass, "advisory")
    assert reloaded.dismissed_suggestions == {"naked_risky:entity:lock.front"}
    refresh_suggestions(hass, reloaded)
    assert reloaded.suggestions == []


@pytest.mark.asyncio
async def test_a_device_profile_counts_as_coverage(hass: HomeAssistant):
    """An entity covered only by its device's profile stops being suggested.

    Coverage is "a stored profile at any inheritance level", so the device layer
    joins it automatically once the host callback is wired. That is intended and
    worth pinning: without it, an operator who profiled a whole physical device
    would keep being told its entities are unprofiled.
    """
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    from custom_components.phoenix_mcp.mesa import async_setup_mesa
    from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile

    entry = ConfigEntry(
        version=1, minor_version=1, domain="demo", title="demo", data={}, source="user",
        options={}, unique_id=None, discovery_keys={}, subentries_data=(),
    )
    hass.config_entries._entries[entry.entry_id] = entry
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "lock")}, name="Front Door",
    )
    lock = er.async_get(hass).async_get_or_create(
        "lock", "demo", "uid_cov", device_id=device.id, suggested_object_id="front"
    )
    hass.states.async_set(lock.entity_id, "locked", {})

    runtime = await async_setup_mesa(hass, "advisory")
    assert not runtime.resolver.has_profile(lock.entity_id)

    runtime.store.set_device_profile(
        device.id,
        SemanticProfile.from_dict(
            device.id,
            {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
            default_origin=MetadataOrigin.USER,
        ),
    )
    assert runtime.resolver.has_profile(lock.entity_id)
