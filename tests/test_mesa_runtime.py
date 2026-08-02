"""Tests for MESA runtime startup tasks and host callbacks.

Covers sidecar developer-profile import (with the user-source skip), the
CallerContext mapping, and TriggerValidator wiring.
"""

from __future__ import annotations

import json
import os

import pytest
from homeassistant.core import HomeAssistant

from custom_components.phoenix_mcp.mesa import (
    async_import_sidecar_profiles,
    async_refresh_trigger_issues,
    async_setup_mesa,
    build_caller_context,
    build_expand_target,
)
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from homeassistant.util.dt import utcnow


def _write_sidecar(hass: HomeAssistant, domain: str, body: dict) -> None:
    base = hass.config.path("custom_components", domain)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "mesa_profile.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh)


def _token(persona: str = "voice_assistant") -> TokenRecord:
    return TokenRecord(
        id="tok-1",
        name="my_token",
        token_hash="x",
        created_at=utcnow(),
        created_by="admin",
        persona=persona,
        permissions=PermissionTree(),
    )


@pytest.mark.asyncio
async def test_sidecar_import_loads_developer_profile(hass: HomeAssistant):
    _write_sidecar(
        hass,
        "my_integration",
        {"semantic_profile": {"semantic_tags": ["climate.heating"]}},
    )
    runtime = await async_setup_mesa(hass, "advisory")
    count = await async_import_sidecar_profiles(hass, runtime)
    assert count == 1
    # Sidecars import as integration-scope profiles keyed by the component name.
    integration_profile = runtime.store.get_integration_profile("my_integration")
    assert integration_profile is not None
    # Sidecar without metadata_origin is stamped developer (Spec 5.3).
    assert integration_profile.metadata.source is MetadataOrigin.DEVELOPER


@pytest.mark.asyncio
async def test_sidecar_import_skips_user_authored_integration(hass: HomeAssistant):
    _write_sidecar(
        hass,
        "my_integration",
        {"semantic_profile": {"semantic_tags": ["climate.heating"]}},
    )
    runtime = await async_setup_mesa(hass, "advisory")
    # Operator already authored an integration profile: import must not clobber it.
    user_profile = SemanticProfile.from_dict(
        "my_integration",
        {"semantic_profile": {"semantic_tags": ["operator.custom"]}},
        default_origin=MetadataOrigin.USER,
    )
    runtime.store.set_integration_profile("my_integration", user_profile)

    count = await async_import_sidecar_profiles(hass, runtime)
    assert count == 0
    kept = runtime.store.get_integration_profile("my_integration")
    assert kept.semantic_tags == ["operator.custom"]


@pytest.mark.asyncio
async def test_get_entity_integration_maps_to_platform(hass: HomeAssistant):
    """The host callback maps an entity to the integration (platform) that made it."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa import _build_get_entity_integration

    entry = er.async_get(hass).async_get_or_create(
        "light", "hue", "uid_x", suggested_object_id="hue_lamp"
    )
    get_integration = _build_get_entity_integration(hass)
    assert get_integration(entry.entity_id) == "hue"
    assert get_integration("light.nonexistent") is None


@pytest.mark.asyncio
async def test_integration_profile_resolves_for_its_entities(hass: HomeAssistant):
    """An integration profile governs the entities that integration created, even
    when those entities live under a different entity domain (where a domain
    profile keyed by the component name would not apply)."""
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get_or_create(
        "switch", "yale_access_bluetooth", "uid_lock", suggested_object_id="front"
    )
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_integration_profile(
        "yale_access_bluetooth",
        SemanticProfile.from_dict(
            "yale_access_bluetooth",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
        ),
    )
    assert runtime.resolver.has_profile(entry.entity_id)


@pytest.mark.asyncio
async def test_trigger_validator_flags_none_declared_entity(hass: HomeAssistant):
    runtime = await async_setup_mesa(hass, "advisory")
    # default_origin=USER mirrors what the admin profile API stamps on an
    # operator-authored profile. It matters here: an unconfirmed profile of
    # untrusted origin cannot assert triggers_automations: none on a helper
    # domain (mesa-core reads it as `likely`, Spec 5.4 Rule 9), so a
    # source-less fixture would resolve away the very declaration under test.
    runtime.store.set(
        "input_boolean.guest_mode",
        SemanticProfile.from_dict(
            "input_boolean.guest_mode",
            {
                "semantic_profile": {
                    "operational_boundaries": {"triggers_automations": "none"},
                }
            },
            default_origin=MetadataOrigin.USER,
        ),
    )
    # Author an automation that references the entity in a trigger.
    automations = hass.config.path("automations.yaml")
    with open(automations, "w", encoding="utf-8") as fh:
        fh.write(
            "- id: a1\n"
            "  trigger:\n"
            "    - platform: state\n"
            "      entity_id: input_boolean.guest_mode\n"
            "  action: []\n"
        )

    await async_refresh_trigger_issues(hass, runtime)
    assert any(
        issue.entity_id == "input_boolean.guest_mode" for issue in runtime.trigger_issues
    )


@pytest.mark.asyncio
async def test_trigger_validator_flags_entity_inheriting_none_from_domain(
    hass: HomeAssistant,
):
    """An entity with no profile of its own, reading as none via a domain
    profile, is still flagged. It is absent from the store's key set, so this
    only works because the host passes the entity registry as candidates."""
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_domain_profile(
        "binary_sensor",
        SemanticProfile.from_dict(
            "binary_sensor",
            {
                "semantic_profile": {
                    "operational_boundaries": {"triggers_automations": "none"},
                }
            },
            default_origin=MetadataOrigin.USER,
        ),
    )
    hass.states.async_set("binary_sensor.porch_motion", "off")
    automations = hass.config.path("automations.yaml")
    with open(automations, "w", encoding="utf-8") as fh:
        fh.write(
            "- id: a2\n"
            "  trigger:\n"
            "    - platform: state\n"
            "      entity_id: binary_sensor.porch_motion\n"
            "  action: []\n"
        )

    await async_refresh_trigger_issues(hass, runtime)
    assert any(
        issue.entity_id == "binary_sensor.porch_motion"
        for issue in runtime.trigger_issues
    )


def _registry_env(hass: HomeAssistant):
    """A device with two entities, in an area on a floor, plus label wiring.

    Returns (device, entity_on_device, second_entity, area, floor_id, label_id)
    for the expand_target tests.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import floor_registry as fr
    from homeassistant.helpers import label_registry as lr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="test_integration", entry_id="exp1")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    area = ar.async_get(hass).async_create("Server Room")
    floor = fr.async_get(hass).async_create("Upstairs")
    ar.async_get(hass).async_update(area.id, floor_id=floor.floor_id)
    label = lr.async_get(hass).async_create("Holiday")

    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test_integration", "dev1")})
    dev_reg.async_update_device(device.id, area_id=area.id)
    on_device = ent_reg.async_get_or_create(
        "light", "test_integration", "u1", config_entry=entry,
        suggested_object_id="rack", device_id=device.id)
    second = ent_reg.async_get_or_create(
        "sensor", "test_integration", "u2", config_entry=entry,
        suggested_object_id="rack_temp", device_id=device.id)
    return device, on_device, second, area, floor.floor_id, label.label_id


@pytest.mark.asyncio
async def test_expand_target_resolves_device_area_floor_label(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    device, on_device, second, area, floor_id, label_id = _registry_env(hass)
    ent_reg = er.async_get(hass)
    expand = build_expand_target(hass)

    # device_id: every enabled entity of the device.
    assert set(expand("device_id", device.id)) == {on_device.entity_id, second.entity_id}

    # area_id: entities of devices in the area count, UNLESS the entity is
    # assigned its own (different) area; a directly-assigned entity counts too.
    ent_reg.async_update_entity(second.entity_id, area_id="somewhere_else")
    direct = ent_reg.async_get_or_create(
        "switch", "test_integration", "u3", suggested_object_id="rack_plug")
    ent_reg.async_update_entity(direct.entity_id, area_id=area.id)
    assert set(expand("area_id", area.id)) == {on_device.entity_id, direct.entity_id}

    # floor_id covers the areas on the floor.
    assert set(expand("floor_id", floor_id)) == {on_device.entity_id, direct.entity_id}

    # label_id: labeled entities plus entities of labeled devices.
    from homeassistant.helpers import device_registry as dr
    ent_reg.async_update_entity(direct.entity_id, labels={label_id})
    dr.async_get(hass).async_update_device(device.id, labels={label_id})
    assert set(expand("label_id", label_id)) == {
        direct.entity_id, on_device.entity_id, second.entity_id}

    # Disabled entities never expand (they produce no state or events).
    ent_reg.async_update_entity(
        on_device.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    assert on_device.entity_id not in expand("device_id", device.id)

    # Unknown refs and kinds resolve to nothing, never raise.
    assert expand("device_id", "nope") == []
    assert expand("area_id", "nope") == []
    assert expand("floor_id", "nope") == []
    assert expand("label_id", "nope") == []
    assert expand("service_id", "whatever") == []


@pytest.mark.asyncio
async def test_trigger_validator_flags_none_entity_behind_device_trigger(
    hass: HomeAssistant,
):
    """The headline expand_target case: an entity declared triggers_automations
    none that an automation references ONLY through a device trigger (no
    entity_id anywhere in the config) is still flagged as stale."""
    device, on_device, _second, _area, _floor, _label = _registry_env(hass)
    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set(
        on_device.entity_id,
        SemanticProfile.from_dict(
            on_device.entity_id,
            {
                "semantic_profile": {
                    "operational_boundaries": {"triggers_automations": "none"},
                }
            },
            default_origin=MetadataOrigin.USER,
        ),
    )
    automations = hass.config.path("automations.yaml")
    with open(automations, "w", encoding="utf-8") as fh:
        fh.write(
            "- id: a_dev\n"
            "  trigger:\n"
            "    - platform: device\n"
            f"      device_id: {device.id}\n"
            "      domain: light\n"
            "      type: turned_on\n"
            "  action: []\n"
        )

    await async_refresh_trigger_issues(hass, runtime)
    assert any(
        issue.entity_id == on_device.entity_id and issue.role == "trigger"
        for issue in runtime.trigger_issues
    )


@pytest.mark.asyncio
async def test_refresh_orphans_covers_entity_area_integration(hass: HomeAssistant):
    """refresh_orphans flags entity, area, and integration profiles whose target
    is gone, and leaves live ones alone (mesa-core's find_orphans covers only the
    entity level, so Phoenix MCP checks area + integration host-side)."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa import refresh_orphans

    # A live entity from the "hue" integration in a live area.
    area = ar.async_get(hass).async_create("Real Area")
    entry = er.async_get(hass).async_get_or_create(
        "light", "hue", "uid_live", suggested_object_id="live"
    )

    runtime = await async_setup_mesa(hass, "advisory")

    def _profile(key: str) -> SemanticProfile:
        return SemanticProfile.from_dict(
            key, {"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}
        )

    # Live targets must NOT be flagged.
    runtime.store.set(entry.entity_id, _profile(entry.entity_id))
    runtime.store.set_area_profile(area.id, _profile(area.id))
    runtime.store.set_integration_profile("hue", _profile("hue"))
    # Dead targets must be flagged.
    runtime.store.set("light.ghost", _profile("light.ghost"))
    runtime.store.set_area_profile("ghost_area", _profile("ghost_area"))
    runtime.store.set_integration_profile("removed_integration", _profile("removed_integration"))

    refresh_orphans(hass, runtime)

    assert runtime.orphans == ["light.ghost"]
    assert runtime.orphan_areas == ["ghost_area"]
    assert runtime.orphan_integrations == ["removed_integration"]


@pytest.mark.asyncio
async def test_async_wipe_clears_all_profiles_and_caches(hass: HomeAssistant):
    """MesaRuntime.async_wipe empties every profile level and the cached lists."""
    runtime = await async_setup_mesa(hass, "advisory")

    def _profile(key: str) -> SemanticProfile:
        return SemanticProfile.from_dict(
            key, {"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}
        )

    runtime.store.set("light.kitchen", _profile("light.kitchen"))
    runtime.store.set_domain_profile("lock", _profile("lock"))
    runtime.store.set_area_profile("kitchen", _profile("kitchen"))
    runtime.store.set_integration_profile("hue", _profile("hue"))
    runtime.dismissed_suggestions.add("some-suggestion")
    runtime.orphans = ["light.ghost"]
    runtime.suggestions = ["s1"]
    assert runtime.backend.list_keys()  # something is stored

    async with runtime.lock:
        await runtime.async_wipe()

    assert runtime.backend.list_keys() == []
    assert runtime.store.get("light.kitchen") is None
    assert runtime.store.get_domain_profile("lock") is None
    assert runtime.store.get_area_profile("kitchen") is None
    assert runtime.store.get_integration_profile("hue") is None
    assert runtime.dismissed_suggestions == set()
    assert runtime.orphans == [] and runtime.suggestions == []


def test_build_caller_context_maps_token():
    ctx = build_caller_context(_token("automation_builder"), session_id="sess-9")
    assert ctx.caller_id == "tok-1"
    assert ctx.display_name == "my_token"
    assert ctx.roles == ["automation_builder"]
    assert ctx.is_authenticated is True
    assert ctx.session_id == "sess-9"


@pytest.mark.asyncio
async def test_solar_elevation_callback_wired_and_matches_astral(hass: HomeAssistant):
    """The enforcer's temporal evaluator gets a working get_solar_elevation.

    The value is oracle-checked against HA's own astral Location so the test
    holds for any test-harness lat/long, not a hardcoded expectation.
    """
    from datetime import datetime, timezone

    from homeassistant.helpers import sun as sun_mod

    runtime = await async_setup_mesa(hass, "advisory")
    callback = runtime.enforcer.temporal.get_solar_elevation
    assert callback is not None

    at = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    got = callback(at)
    location, elevation = sun_mod.get_astral_location(hass)
    assert got == pytest.approx(float(location.solar_elevation(at, elevation)))


@pytest.mark.asyncio
async def test_solar_elevation_callback_fails_closed(hass: HomeAssistant, monkeypatch):
    """Any astral failure returns None (mesa-core treats it as unevaluable)."""
    from datetime import datetime, timezone

    from custom_components.phoenix_mcp.mesa import _build_get_solar_elevation

    def _boom(_hass):
        raise RuntimeError("no location")

    monkeypatch.setattr("custom_components.phoenix_mcp.mesa.sun_mod.get_astral_location", _boom)
    callback = _build_get_solar_elevation(hass)
    assert callback(datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)) is None


# --- device inheritance level -----------------------------------------------


def _make_device(hass: HomeAssistant, name: str = "Front Door Lock"):
    """A device registry entry with a config entry to hang entities on."""
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers import device_registry as dr

    entry = ConfigEntry(
        version=1, minor_version=1, domain="demo", title="demo", data={}, source="user",
        options={}, unique_id=None, discovery_keys={}, subentries_data=(),
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", name)},
        name=name,
    )


@pytest.mark.asyncio
async def test_get_entity_device_maps_to_the_owning_device(hass: HomeAssistant):
    """The callback answers with the owning device unconditionally.

    Unlike the device read inside the area callback, which fires only when the
    entity has no area of its own, the owning device is the answer whether or not
    the entity is separately placed.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa import _build_get_entity_device

    device = _make_device(hass)
    entry = er.async_get(hass).async_get_or_create(
        "lock", "demo", "uid_lock", device_id=device.id, suggested_object_id="front"
    )
    er.async_get(hass).async_update_entity(entry.entity_id, area_id="hallway")

    get_device = _build_get_entity_device(hass)
    assert get_device(entry.entity_id) == device.id
    assert get_device("lock.nonexistent") is None


@pytest.mark.asyncio
async def test_one_device_profile_governs_every_entity_the_device_owns(hass: HomeAssistant):
    """The point of the layer: author once, cover the whole physical device.

    Per-entity profiles can express the same policy only by being written for
    each entity, and cannot cover an entity the device grows later.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa_core import ControlMode

    device = _make_device(hass)
    registry = er.async_get(hass)
    owned = [
        registry.async_get_or_create(
            domain, "demo", f"uid_{domain}", device_id=device.id,
            suggested_object_id="front",
        ).entity_id
        for domain in ("lock", "sensor", "binary_sensor")
    ]

    runtime = await async_setup_mesa(hass, "advisory")
    runtime.store.set_device_profile(
        device.id,
        SemanticProfile.from_dict(
            device.id,
            {"semantic_profile": {"operational_boundaries": {"control_mode": "read_only"}}},
            default_origin=MetadataOrigin.USER,
        ),
    )

    for entity_id in owned:
        effective = runtime.resolver.resolve(entity_id)
        assert effective.operational_boundaries.control_mode is ControlMode.READ_ONLY, entity_id

    # An entity the device does not own is untouched by it.
    other = registry.async_get_or_create(
        "light", "demo", "uid_elsewhere", suggested_object_id="hall"
    ).entity_id
    assert runtime.resolver.resolve(other).operational_boundaries.control_mode is not ControlMode.READ_ONLY


@pytest.mark.asyncio
async def test_device_layer_tightens_and_scope_only_breaks_ties(hass: HomeAssistant):
    """How the device layer actually composes, which is easy to state wrongly.

    Device sits between entity and area in scope rank, but on control_mode
    RESTRICTIVENESS DOMINATES SCOPE: the most restrictive declared value wins and
    scope only breaks a tie between equally restrictive ones. So a device profile
    is not "overridden" by a more permissive entity profile; layers only tighten,
    which is the same property that makes hiding one from a caller unsafe.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.phoenix_mcp.mesa_core import ControlMode

    device = _make_device(hass)
    entry = er.async_get(hass).async_get_or_create(
        "lock", "demo", "uid_rank", device_id=device.id, suggested_object_id="rank"
    )
    er.async_get(hass).async_update_entity(entry.entity_id, area_id="hallway")
    runtime = await async_setup_mesa(hass, "advisory")

    def _profile(key: str, mode: str) -> SemanticProfile:
        return SemanticProfile.from_dict(
            key,
            {"semantic_profile": {"operational_boundaries": {"control_mode": mode}}},
            default_origin=MetadataOrigin.USER,
        )

    # The device tightens the area's confirm to read_only.
    runtime.store.set_area_profile("hallway", _profile("hallway", "confirm"))
    runtime.store.set_device_profile(device.id, _profile(device.id, "read_only"))
    assert runtime.resolver.resolve(entry.entity_id).operational_boundaries.control_mode is (
        ControlMode.READ_ONLY
    )

    # A MORE PERMISSIVE entity profile does not win, because it is not a scope
    # contest: read_only outranks confirm regardless of which layer declared it.
    runtime.store.set(entry.entity_id, _profile(entry.entity_id, "confirm"))
    assert runtime.resolver.resolve(entry.entity_id).operational_boundaries.control_mode is (
        ControlMode.READ_ONLY
    )

    # Scope decides only when the values are equally restrictive, and then the
    # more specific layer is credited.
    runtime.store.set(entry.entity_id, _profile(entry.entity_id, "read_only"))
    explanation = runtime.resolver.explain(entry.entity_id)
    control = next(
        f for f in explanation.explanation
        if f.field_path == "operational_boundaries.control_mode"
    )
    assert control.provided_by_level == "entity"


@pytest.mark.asyncio
async def test_the_enforcer_resolves_over_the_unfiltered_store(hass: HomeAssistant):
    """Enforcement must never see a per-token view of the profile store.

    Every inheritance layer only tightens, so a store that hides layers yields a
    MORE permissive effective profile than the operator authored, and nothing in
    a resolution marks a layer as missing: a scoped resolution is
    indistinguishable from a deployment that genuinely has no such profile.
    Backing the enforcer with a scoped view would therefore convert reduced
    visibility into reduced policy. Scoping belongs to the retrieval tools, which
    filter which ENTITIES a caller may address, never which layers apply to one.
    """
    from custom_components.phoenix_mcp.mesa_tools import ScopedProfileStore

    runtime = await async_setup_mesa(hass, "enforced")

    assert runtime.enforcer.store is runtime.store
    assert runtime.enforcer.resolver.store is runtime.store
    assert not isinstance(runtime.enforcer.store, ScopedProfileStore)
    assert not isinstance(runtime.enforcer.resolver.store, ScopedProfileStore)
