"""MESA semantic-safety runtime for Phoenix MCP.

This module wires the vendored mesa-core library (custom_components.phoenix_mcp.mesa_core)
into the Phoenix MCP integration. mesa-core is per-ENTITY policy (what is safe to touch
at all); Phoenix MCP tokens are per-CALLER policy (who may touch what). The two layer:
Phoenix MCP resolves and flattens targets first, then MESA evaluates the explicit entity
list. MESA is orthogonal to the token permission tree and applies even to
pass-through tokens.

Design notes that are easy to get wrong:

- The profile store is backed by an in-memory dict (PhoenixMesaBackend) persisted
  through HA's Store. Because the backend never blocks, mesa-core's synchronous
  APIs run directly on the event loop; the ``a*`` (to_thread) variants are never
  used here.
- The MesaEnforcer is constructed with ``interactive=False``. With no
  interaction channel, a ``control_mode: confirm`` entity is blocked with rule
  ``control_mode:confirm_no_channel`` BEFORE any confirmation challenge is
  issued, so mesa-core's ConfirmationManager stays empty and we never touch its
  private state. Phoenix MCP interprets that block itself: in advisory mode it becomes a
  warning, in enforced mode it routes to Phoenix MCP's admin approval gate.
- "enforced-ness" is recomputed host-side per entity from public data
  (``settings.mesa_mode == enforced`` or the effective profile's
  ``enforcement_mode``), mirroring MesaEnforcer._is_enforced.

mesa-core is a read-only dependency; never modify it and never reach into its
private state. Report any mesa-core bug to the maintainer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar_mod
from homeassistant.helpers import device_registry as dr_mod
from homeassistant.helpers import entity_registry as er_mod
from homeassistant.helpers import sun as sun_mod
from homeassistant.helpers.storage import Store
from homeassistant.util.yaml import load_yaml as _load_yaml

from homeassistant.util import dt as dt_util

from . import mesa_audit
from .helpers import diff_summary_fields
from .const import (
    MESA_APPROVED_EXECUTOR,
    MESA_CONFIRM_CAP,
    MESA_MODE_ENFORCED,
    MESA_MODE_OFF,
    MESA_STORAGE_KEY,
    MESA_STORAGE_VERSION,
)
from .mesa_core import (
    CallerContext,
    InheritanceResolver,
    LeaseManager,
    MesaEnforcer,
    ProfileStore,
    TriggerValidator,
    import_from_integration,
)
from .mesa_core.backends import StorageBackend
from .mesa_core.exceptions import MesaError

if TYPE_CHECKING:
    from .approvals import PendingApproval
    from .data import PhoenixData
    from .mesa_core import SemanticProfile, ValidationIssue
    from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)

# MESA's rule name for a call whose parameters name a target other than the
# entity the decision was made for. Unreachable once target resolution has
# consumed the selectors, so it is treated as a defect signal rather than a
# policy outcome.
_RULE_CONTRADICTORY_TARGET = "contradictory_target"

_AUTOMATION_YAML = "automations.yaml"


class PhoenixMesaBackend(StorageBackend):
    """In-memory dict storage backend persisted via HA's Store.

    All reads/writes/deletes operate on an in-process dict, so mesa-core's
    synchronous API stays non-blocking on the event loop. Durability is the
    caller's responsibility: mutate, then call MesaRuntime.async_save().
    """

    def __init__(self, initial: dict[str, dict[str, Any]] | None = None) -> None:
        self._data: dict[str, dict[str, Any]] = dict(initial or {})

    def read(self, key: str) -> dict[str, Any] | None:
        value = self._data.get(key)
        return dict(value) if value is not None else None

    def write(self, key: str, data: dict[str, Any]) -> None:
        self._data[key] = dict(data)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def list_keys(self, prefix: str | None = None) -> list[str]:
        keys = sorted(self._data)
        if prefix is not None:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Deep-enough copy for persistence (profile docs are plain JSON)."""
        return {k: dict(v) for k, v in self._data.items()}


@dataclass
class MesaRuntime:
    """Holds the constructed mesa-core objects and Phoenix-side caches."""

    hass: HomeAssistant
    backend: PhoenixMesaBackend
    store: ProfileStore
    resolver: InheritanceResolver
    enforcer: MesaEnforcer
    validator: TriggerValidator
    ha_store: Store
    # Advisory coordination leases (mesa-core 1.1, Enrichment Section 21).
    # In-memory only by design: leases cap at 30 seconds and never survive a
    # restart. Exposed to agents via the mesa_request_lease/mesa_release_lease
    # tools (mesa_tools.py); no admin UI or HA sensor in this version.
    lease_manager: LeaseManager
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    trigger_issues: list[ValidationIssue] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    orphan_areas: list[str] = field(default_factory=list)
    orphan_integrations: list[str] = field(default_factory=list)
    orphan_devices: list[str] = field(default_factory=list)
    # Profile suggestions (mesa_suggestions.py): cached like the orphan lists.
    # dismissed_suggestions persists in the phoenix_mcp_mesa store (Phoenix-owned sibling
    # key next to "profiles"; mesa-core never sees it).
    suggestions: list = field(default_factory=list)
    dismissed_suggestions: set[str] = field(default_factory=set)

    async def async_save(self) -> None:
        """Persist the current profile set to HA storage.

        Callers already hold ``self.lock`` for the mutate-then-save sequence.
        """
        await self.ha_store.async_save({
            "profiles": self.backend.snapshot(),
            "dismissed_suggestions": sorted(self.dismissed_suggestions),
        })

    async def async_wipe(self) -> None:
        """Delete every MESA profile and reset all Phoenix-side MESA caches.

        Callers already hold ``self.lock``. Clears the profile backend (all
        levels: entity/device/domain/integration/area/deployment), the dismissed-
        suggestion set, and the cached orphan/suggestion/trigger lists, then
        persists the emptied store.
        """
        for key in self.backend.list_keys():
            self.backend.delete(key)
        self.dismissed_suggestions.clear()
        self.trigger_issues = []
        self.orphans = []
        self.orphan_areas = []
        self.orphan_integrations = []
        self.orphan_devices = []
        self.suggestions = []
        await self.async_save()

    def set_mode(self, mesa_mode: str) -> None:
        """Update the enforcer's global mode after a settings change.

        ``off`` never calls the enforcer (the verdict helper short-circuits), so
        it maps to advisory here for safety if the enforcer is ever consulted.
        """
        self.enforcer.mode = "enforced" if mesa_mode == MESA_MODE_ENFORCED else "advisory"


def _build_get_entity_area(hass: HomeAssistant) -> Callable[[str], str | None]:
    """Return a sync callback mapping entity_id to area_id (device fallback).

    Mirrors mcp_view._resolve_area_id but is defined here to avoid an import
    cycle (mcp_view imports this module for enforcement).
    """

    def _get_entity_area(entity_id: str) -> str | None:
        er = er_mod.async_get(hass)
        entry = er.async_get(entity_id)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        if entry.device_id:
            device = dr_mod.async_get(hass).async_get(entry.device_id)
            if device and device.area_id:
                return device.area_id
        return None

    return _get_entity_area


def _build_get_entity_device(hass: HomeAssistant) -> Callable[[str], str | None]:
    """Return a sync callback mapping entity_id to the device that owns it.

    This is the MESA "device" inheritance level, between entity and area: one
    profile against a device registry id governs every entity that device owns,
    including entities a firmware or integration update adds later, which is the
    part per-entity profiles can never cover.

    The lookup is UNCONDITIONAL, unlike the device read inside
    _build_get_entity_area, which consults the device only when the entity has
    no area of its own. Here the owning device is the answer whether or not the
    entity is separately placed.

    There is no fallback by design: without this callback the whole layer is
    inert, because guessing a device would silently apply policy nobody wrote.
    """

    def _get_entity_device(entity_id: str) -> str | None:
        entry = er_mod.async_get(hass).async_get(entity_id)
        return entry.device_id if entry is not None else None

    return _get_entity_device


def _build_get_entity_integration(hass: HomeAssistant) -> Callable[[str], str | None]:
    """Return a sync callback mapping entity_id to the integration that created it.

    This is the MESA "integration" inheritance level (between area and domain): the
    entity registry's ``platform`` is the component that created the entity (e.g.
    ``hue``, ``yale_access_bluetooth``), which is exactly the key a vendor sidecar
    is stored under. It lets a device/hub integration's sidecar govern the entities
    it created regardless of their entity domain (lock.*, sensor.*, ...).
    """

    def _get_entity_integration(entity_id: str) -> str | None:
        entry = er_mod.async_get(hass).async_get(entity_id)
        return entry.platform if entry is not None else None

    return _get_entity_integration


def build_expand_target(hass: HomeAssistant) -> Callable[[str, str], list[str]]:
    """Return the host callback resolving indirect automation targets to entity ids.

    mesa-core's trigger walk (entities_by_role / TriggerValidator) finds target
    selectors it cannot resolve itself: device triggers and the target blocks of
    purpose-specific triggers reference a device_id/area_id/floor_id/label_id,
    and only the host can expand those against the HA registries. Without this
    callback, an automation triggering on a device is invisible to the
    triggers_automations staleness check and to Phoenix MCP's reverse references.

    Deliberately INCLUSIVE, unlike service-target expansion: hidden, config,
    and diagnostic entities still fire triggers, so there is no
    primary-entities-only filtering here. Disabled registry entries are
    excluded (they produce no state or events). An entity assigned its own
    area does not count toward its device's area, matching HA's area
    semantics. Any failure returns [] (a resolution error must never break
    validation or a reverse-reference read).
    """

    def _device_entities(ent_reg: Any, device_id: str) -> list[str]:
        return [
            e.entity_id
            for e in er_mod.async_entries_for_device(ent_reg, device_id)
            if e.disabled_by is None
        ]

    def _area_entities(ent_reg: Any, area_id: str) -> list[str]:
        out = [
            e.entity_id
            for e in er_mod.async_entries_for_area(ent_reg, area_id)
            if e.disabled_by is None
        ]
        dev_reg = dr_mod.async_get(hass)
        for device in dr_mod.async_entries_for_area(dev_reg, area_id):
            out.extend(
                e.entity_id
                for e in er_mod.async_entries_for_device(ent_reg, device.id)
                if e.disabled_by is None and e.area_id is None
            )
        return out

    def _expand_target(kind: str, ref: str) -> list[str]:
        try:
            ent_reg = er_mod.async_get(hass)
            if kind == "device_id":
                return _device_entities(ent_reg, ref)
            if kind == "area_id":
                return _area_entities(ent_reg, ref)
            if kind == "floor_id":
                out: list[str] = []
                for area in ar_mod.async_entries_for_floor(ar_mod.async_get(hass), ref):
                    out.extend(_area_entities(ent_reg, area.id))
                return out
            if kind == "label_id":
                out = [
                    e.entity_id
                    for e in er_mod.async_entries_for_label(ent_reg, ref)
                    if e.disabled_by is None
                ]
                dev_reg = dr_mod.async_get(hass)
                for device in dr_mod.async_entries_for_label(dev_reg, ref):
                    out.extend(_device_entities(ent_reg, device.id))
                return out
        except Exception:  # noqa: BLE001 - fail-quiet per the docstring
            return []
        return []

    return _expand_target


def _build_get_state(hass: HomeAssistant) -> Callable[[str], str | None]:
    def _get_state(entity_id: str) -> str | None:
        state = hass.states.get(entity_id)
        return state.state if state is not None else None

    return _get_state


def _build_get_solar_elevation(hass: HomeAssistant) -> Callable[[datetime], float | None]:
    """Return a sync callback computing the sun's elevation (degrees) at a time.

    Backs mesa-core's solar_angle temporal conditions. HA's cached astral
    Location (helpers.sun.get_astral_location) computes elevation at ANY
    instant, so mesa-core's solar_offset_minutes sampling in the past is
    exact, not approximated from the current sun.sun state. Any failure
    returns None, which mesa-core treats as unevaluable (fail-closed).
    """

    def _get_solar_elevation(at: datetime) -> float | None:
        try:
            location, elevation = sun_mod.get_astral_location(hass)
            return float(location.solar_elevation(at, elevation))
        except Exception:  # noqa: BLE001 - fail-closed per the mesa-core contract
            return None

    return _get_solar_elevation


def _build_on_lease_event(hass: HomeAssistant) -> Callable[[dict[str, Any]], None]:
    """Bridge mesa-core lease events onto the HA event bus as phoenix_mcp_mesa_lease_expired.

    LeaseManager's sync methods run via asyncio.to_thread (the a* variants), so
    the callback fires on a worker thread; hass.add_job hops back to the event
    loop before touching the bus (async_fire is a @callback, loop-only).
    """
    from .const import DOMAIN  # noqa: PLC0415 - matches fire_mesa_blocked_event

    def _on_lease_event(payload: dict[str, Any]) -> None:
        hass.add_job(hass.bus.async_fire, f"{DOMAIN}_mesa_lease_expired", dict(payload))

    return _on_lease_event


def _numeric_threshold_example(nt_spec: dict[str, Any]) -> dict[str, Any]:
    """Serialize a numeric_threshold selector into its VALID config value.

    A numeric_threshold selector (used by *_crossed_threshold triggers) does not
    serialize the way its selector shape suggests: the config value is
    {type: above|below, value: {number, unit_of_measurement}}, not a bare number.
    The unit lives either at the selector level as a list (temperature) or inside
    the number spec as a string (percentage); handle both. type must be above or
    below (there is no bidirectional form). The number is a representative
    in-range placeholder the caller adapts to the user's real threshold.
    """
    number = nt_spec.get("number") or {}
    lo, hi = number.get("min"), number.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        example_number: float = round((lo + hi) / 2)
    elif isinstance(lo, (int, float)):
        example_number = round(lo)
    else:
        example_number = 20
    value: dict[str, Any] = {"number": example_number}
    unit_list = nt_spec.get("unit_of_measurement")
    if isinstance(unit_list, list) and unit_list:
        value["unit_of_measurement"] = unit_list[0]
    elif isinstance(number.get("unit_of_measurement"), str):
        value["unit_of_measurement"] = number["unit_of_measurement"]
    return {"type": "above", "value": value}


def _moment_example_config(
    moment_id: str, kind: str, entity_id: str, schema: dict[str, Any]
) -> dict[str, Any] | None:
    """A minimal, copy-adaptable VALID config for a semantic moment, or None.

    The moment's ``schema`` is the frontend FORM descriptor (selectors), not the
    config-YAML serialization, and a model handed only that cannot reliably
    author the trigger/condition: it is left reverse-engineering the config shape
    one field at a time against repeated validation failures. This emits a
    concrete example for the
    selector types whose serialization is known and verified, and returns None
    (example omitted, never guessed) for anything else so a wrong example is never
    shipped. Fields with defaults (behavior, for) are omitted from the minimal
    example; only required parameterized fields are serialized. Triggers and
    conditions share the numeric_threshold serialization; they differ only in the
    leading key (verified against HA's own validate_config for both).
    """
    options: dict[str, Any] = {}
    for field_name, field_spec in (schema.get("fields") or {}).items():
        selector = field_spec.get("selector") or {}
        if "numeric_threshold" in selector:
            options[field_name] = _numeric_threshold_example(selector["numeric_threshold"])
    if not options:
        return None
    key = "condition" if kind == "condition" else "trigger"
    return {key: moment_id, "target": {"entity_id": entity_id}, "options": options}


async def async_semantic_moments(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]] | None:
    """Live purpose-specific trigger/condition vocabulary for one entity.

    Backs mesa-core 1.2's ``mesa_get_profile(include_semantic_moments=True)``.
    HA 2026.7 graduated purpose-specific triggers/conditions to default;
    ``async_get_triggers_for_target``/``async_get_conditions_for_target``
    resolve which apply to an entity from each integration's triggers.yaml/
    conditions.yaml target descriptors (verified identical between 2026.6.3
    and the final 2026.7.1). On 2026.6 with the Labs flag off the sets are
    simply smaller or empty, still a valid answer.

    HA-COUPLING POINT alongside ws_dispatch/the injector/mesa_suggestions:
    import-inside-function and ANY failure (including the module or functions
    not existing on older HA) degrades to None, which mesa-core renders as the
    field being omitted. Re-verify on HA upgrades. Consumed live at request
    time, never stored, never consulted by enforcement.

    Each moment also carries its ``schema`` (target/fields, straight from the
    owning integration's triggers.yaml/conditions.yaml) when available, via
    ``homeassistant.helpers.trigger``/``condition``'s own ``async_get_all_descriptions``
    (HA's frontend automation editor uses the same call to build its trigger/
    condition picker forms; the result is cached by HA itself, so this is not
    a re-parse per request). That schema is the frontend FORM descriptor
    (selectors), which does NOT map trivially to the config YAML an agent must
    author; for the selector types whose serialization is known (numeric_threshold
    triggers), a trigger moment also carries an ``example``: a concrete, valid,
    copy-adaptable config snippet built by ``_moment_example_config``. Both are
    SEPARATE, best-effort lookups from the id fetch above: if the ids resolve but
    the schema/example lookup fails, the moments are still returned without those
    keys rather than losing the whole field.
    """
    try:
        from homeassistant.components.websocket_api.automation import (  # noqa: PLC0415
            async_get_conditions_for_target,
            async_get_triggers_for_target,
        )

        target = {"entity_id": entity_id}
        triggers = await async_get_triggers_for_target(hass, target, False)
        conditions = await async_get_conditions_for_target(hass, target, False)
    except Exception:  # noqa: BLE001 - degrade to field-omitted, never break the tool
        _LOGGER.debug("semantic moments lookup failed for %s", entity_id, exc_info=True)
        return None

    trigger_schemas: dict[str, Any] = {}
    try:
        from homeassistant.helpers.trigger import (  # noqa: PLC0415
            async_get_all_descriptions as async_get_all_trigger_descriptions,
        )

        trigger_schemas = await async_get_all_trigger_descriptions(hass)
    except Exception:  # noqa: BLE001 - schema is best-effort; ids above still stand
        _LOGGER.debug("semantic moments trigger schema lookup failed for %s", entity_id, exc_info=True)

    condition_schemas: dict[str, Any] = {}
    try:
        from homeassistant.helpers.condition import (  # noqa: PLC0415
            async_get_all_descriptions as async_get_all_condition_descriptions,
        )

        condition_schemas = await async_get_all_condition_descriptions(hass)
    except Exception:  # noqa: BLE001 - schema is best-effort; ids above still stand
        _LOGGER.debug("semantic moments condition schema lookup failed for %s", entity_id, exc_info=True)

    moments: list[dict[str, Any]] = []
    for key in sorted(triggers):
        entry: dict[str, Any] = {"id": key, "kind": "trigger"}
        if schema := trigger_schemas.get(key):
            # A worked example supersedes the selector schema: the schema is the
            # frontend FORM descriptor and is not directly authorable (it misled a
            # model into 14 failed validate attempts), so when we can emit a valid
            # example we return that alone and drop the schema, both to shrink the
            # payload and to avoid a confusing double representation of each field.
            # The schema is kept only as the fallback when no example is available.
            if example := _moment_example_config(key, "trigger", entity_id, schema):
                entry["example"] = example
            else:
                entry["schema"] = schema
        moments.append(entry)
    for key in sorted(conditions):
        entry = {"id": key, "kind": "condition"}
        if schema := condition_schemas.get(key):
            if example := _moment_example_config(key, "condition", entity_id, schema):
                entry["example"] = example
            else:
                entry["schema"] = schema
        moments.append(entry)
    return moments


def read_automation_configs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Read automations.yaml as a list of HA automation config dicts.

    Performs file I/O; run via hass.async_add_executor_job. Returns [] when the
    file is missing or malformed (the TriggerValidator tolerates an empty set).
    """
    path = hass.config.path(_AUTOMATION_YAML)
    if not os.path.isfile(path):
        return []
    try:
        data = _load_yaml(path)
    except Exception:  # noqa: BLE001 - a broken YAML file must not crash setup
        _LOGGER.warning("MESA: could not parse %s for trigger validation", path, exc_info=True)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def build_caller_context(token: TokenRecord, session_id: str) -> CallerContext:
    """Map a Phoenix MCP token to a MESA CallerContext.

    Phoenix MCP has no per-user roles; the token's persona is its only role-like
    attribute, so MESA access_roles rules are persona-granular. caller_id uses
    the token id (stable across renames); the name is surfaced as display_name.
    """
    return CallerContext(
        caller_id=token.id,
        roles=[token.persona] if token.persona else [],
        is_authenticated=True,
        session_id=session_id,
        display_name=token.name,
    )


async def async_setup_mesa(hass: HomeAssistant, mesa_mode: str) -> MesaRuntime:
    """Construct the MESA runtime, loading any persisted profiles.

    Built even when the kill switch is on: the admin profile API must work
    regardless, and the enforcement gate is simply never reached when no client
    routes are registered.
    """
    ha_store: Store[dict] = Store(hass, MESA_STORAGE_VERSION, MESA_STORAGE_KEY)
    raw = await ha_store.async_load() or {}
    backend = PhoenixMesaBackend(raw.get("profiles") or {})

    get_entity_area = _build_get_entity_area(hass)
    get_entity_integration = _build_get_entity_integration(hass)
    get_entity_device = _build_get_entity_device(hass)
    store = ProfileStore(
        backend=backend,
        get_entity_area=get_entity_area,
        get_entity_integration=get_entity_integration,
        get_entity_device=get_entity_device,
    )
    resolver = InheritanceResolver(
        store=store,
        get_entity_area=get_entity_area,
        get_entity_integration=get_entity_integration,
        get_entity_device=get_entity_device,
    )
    store.attach_resolver(resolver)
    # The enforcer resolves through whatever store it is handed, so it takes the
    # UNFILTERED one. Backing it with a per-token scoped view would turn reduced
    # visibility into reduced policy: every inheritance layer only ever tightens,
    # so a hidden layer yields a MORE permissive effective profile than the
    # operator authored, and nothing in a resolution marks a layer as missing.
    # Scoping belongs to the retrieval tools, which filter entities, never layers.
    enforcer = MesaEnforcer(
        store=store,
        resolver=resolver,
        mode="enforced" if mesa_mode == MESA_MODE_ENFORCED else "advisory",
        interactive=False,
        get_state=_build_get_state(hass),
        get_solar_elevation=_build_get_solar_elevation(hass),
    )
    validator = TriggerValidator(store=store, expand_target=build_expand_target(hass))
    lease_manager = LeaseManager(
        store=store,
        get_state=_build_get_state(hass),
        on_lease_event=_build_on_lease_event(hass),
    )

    return MesaRuntime(
        hass=hass,
        backend=backend,
        store=store,
        resolver=resolver,
        enforcer=enforcer,
        validator=validator,
        ha_store=ha_store,
        lease_manager=lease_manager,
        dismissed_suggestions={
            k for k in (raw.get("dismissed_suggestions") or []) if isinstance(k, str)
        },
    )


async def async_import_sidecar_profiles(hass: HomeAssistant, runtime: MesaRuntime) -> int:
    """Import developer mesa_profile.json sidecars from installed integrations.

    Domains that already carry an operator-authored (source: user) domain
    profile are skipped so restarts never clobber manual edits. Returns the
    number of domain profiles imported.
    """
    components_dir = hass.config.path("custom_components")

    def _scan() -> list[SemanticProfile]:
        found: list[SemanticProfile] = []
        if not os.path.isdir(components_dir):
            return found
        for name in sorted(os.listdir(components_dir)):
            path = os.path.join(components_dir, name)
            if not os.path.isdir(path):
                continue
            try:
                profile = import_from_integration(path)
            except MesaError:
                _LOGGER.warning("MESA: malformed sidecar in %s; skipping", name, exc_info=True)
                continue
            if profile is not None:
                found.append(profile)
        return found

    profiles = await hass.async_add_executor_job(_scan)
    imported = 0
    async with runtime.lock:
        for profile in profiles:
            existing = runtime.store.get_integration_profile(profile.entity_id)
            if existing is not None and existing.metadata.source.value == "user":
                continue
            runtime.store.set_integration_profile(profile.entity_id, profile)
            imported += 1
        if imported:
            await runtime.async_save()
    return imported


async def async_refresh_trigger_issues(hass: HomeAssistant, runtime: MesaRuntime) -> None:
    """Re-run the TriggerValidator and cache the results on the runtime.

    The validator checks the EFFECTIVE triggers_automations, so an entity can
    read as ``none`` by inheriting it from a domain, integration, or area
    profile while carrying no profile of its own. Such an entity is not in the
    store's key set, so it is only checkable when the host names it.

    ``entity_ids`` REPLACES the store's key set rather than extending it, so the
    candidates are the union: the live entities (registry plus states, mirroring
    refresh_orphans) and every entity carrying its own stored profile. Passing
    the live set alone would stop checking a profile whose entity is currently
    absent, which is exactly the stale declaration the check exists to catch.
    """
    configs = await hass.async_add_executor_job(read_automation_configs, hass)
    entity_ids = (
        set(er_mod.async_get(hass).entities)
        | set(hass.states.async_entity_ids())
        | set(runtime.store.entity_keys())
    )
    runtime.trigger_issues = await hass.async_add_executor_job(
        lambda: runtime.validator.validate(lambda: configs, entity_ids=entity_ids)
    )


def refresh_orphans(hass: HomeAssistant, runtime: MesaRuntime) -> None:
    """Recompute stored profiles whose target no longer exists.

    Four independent kinds, computed here rather than through mesa-core's own
    ``find_orphans``: entity profiles whose entity left the registry, device
    profiles whose device was removed, area profiles whose area was deleted, and
    integration profiles whose integration is neither producing entities nor
    loaded as a component. MESA never auto-deletes a profile (a rename or
    temporary removal must not lose authored intent); this only surfaces them for
    manual cleanup.

    mesa-core can check the scoped levels too, but it returns those orphans as
    their full reserved key (``__device__:abc``) for the caller to split apart,
    which would change the shape every consumer here already reads. The lists
    stay bare and stay separate.
    """
    er = er_mod.async_get(hass)
    known = set(er.entities) | set(hass.states.async_entity_ids())
    runtime.orphans = runtime.store.find_orphans(known)

    known_devices = set(dr_mod.async_get(hass).devices)
    runtime.orphan_devices = [d for d in runtime.store.device_keys() if d not in known_devices]

    known_areas = set(ar_mod.async_get(hass).areas)
    runtime.orphan_areas = [a for a in runtime.store.area_keys() if a not in known_areas]

    known_integrations = {e.platform for e in er.entities.values() if e.platform} | set(
        hass.config.components
    )
    runtime.orphan_integrations = [
        i for i in runtime.store.integration_keys() if i not in known_integrations
    ]


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


@dataclass
class MesaVerdict:
    """Per-entity MESA outcome for one flattened service call.

    allowed: entities that may proceed now. confirm: entities whose profile
    requires admin confirmation (enforced mode). blocked: (entity, rule, reason)
    for entities MESA refuses outright (read_only, prohibited, declared limit,
    privacy). warnings: advisory messages collected across all entities.
    """

    allowed: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)
    blocked: list[tuple[str, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RegistryMesaDecision:
    """MESA's fully resolved verdict for one entity-registry identity action."""

    decision: str  # "allow" | "confirm" | "deny"
    rule: str
    reason: str
    warnings: list[str] = field(default_factory=list)
    effective_rule: dict[str, Any] | None = None
    profile_fingerprint: str | None = None


def evaluate_registry_action(
    data: PhoenixData,
    token: TokenRecord,
    entity_id: str,
    *,
    action: str,
    registry_domain: str = "entity_registry",
    service_data: dict[str, Any] | None = None,
    session_id: str = "entity_registry",
) -> RegistryMesaDecision:
    """Evaluate rename/delete through the normal inheritance resolver and enforcer.

    The synthetic service name defaults to ``entity_registry.<action>`` and may
    be switched to another registry domain such as ``device_registry``. The
    enforcer still resolves the entity's exact
    entity/device/area/integration/domain layers, so a permissive entity profile
    cannot bypass a restrictive ancestor. The returned fingerprint covers the
    whole resolved explanation, not only the final control-mode string; an
    approval can therefore satisfy only the MESA state it actually previewed.
    """
    runtime = data.mesa
    settings = data.store.get_settings()
    if runtime is None or settings.mesa_mode == MESA_MODE_OFF:
        return RegistryMesaDecision(
            decision="allow",
            rule="mesa:off",
            reason="MESA is off.",
        )

    explanation = runtime.resolver.explain(entity_id)
    explanation_doc = explanation.to_dict()
    fingerprint = hashlib.sha256(
        json.dumps(
            explanation_doc,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    control = next(
        (
            item
            for item in explanation.explanation
            if item.field_path == "operational_boundaries.control_mode"
        ),
        None,
    )
    boundaries = explanation.effective_profile.operational_boundaries
    control_mode = getattr(boundaries.control_mode, "value", boundaries.control_mode)
    effective_rule = {
        "action": f"{registry_domain}.{action}",
        "control_mode": control_mode,
        "control_reason": boundaries.control_reason,
        "enforcement_mode": (
            MESA_MODE_ENFORCED
            if settings.mesa_mode == MESA_MODE_ENFORCED
            else boundaries.enforcement_mode
        ),
        "provided_by_level": control.provided_by_level if control is not None else None,
        "provided_by_origin": control.provided_by_origin if control is not None else None,
        "profile_fingerprint": fingerprint[:16],
    }
    verdict = evaluate_service_entities(
        runtime,
        settings.mesa_mode,
        token,
        [entity_id],
        domain=registry_domain,
        service=action,
        service_data=dict(service_data or {}),
        session_id=session_id,
    )
    reason = boundaries.control_reason or entity_id
    if verdict.confirm:
        return RegistryMesaDecision(
            decision="confirm",
            rule="control_mode:confirm",
            reason=f"{registry_domain} {action} requires confirmation: {reason}",
            warnings=list(verdict.warnings),
            effective_rule=effective_rule,
            profile_fingerprint=fingerprint,
        )
    if verdict.blocked:
        _blocked_entity, rule, blocked_reason = verdict.blocked[0]
        return RegistryMesaDecision(
            decision="deny",
            rule=rule,
            reason=blocked_reason,
            warnings=list(verdict.warnings),
            effective_rule=effective_rule,
            profile_fingerprint=fingerprint,
        )
    return RegistryMesaDecision(
        decision="allow",
        rule=f"control_mode:{control_mode}",
        reason=f"{registry_domain} {action} is permitted: {reason}",
        warnings=list(verdict.warnings),
        effective_rule=effective_rule,
        profile_fingerprint=fingerprint,
    )


def _host_enforced(settings_mode: str, result: Any) -> bool:
    """Recompute MesaEnforcer._is_enforced host-side from public result data."""
    if settings_mode == MESA_MODE_ENFORCED:
        return True
    boundaries = result.effective_profile.operational_boundaries
    return getattr(boundaries, "enforcement_mode", "advisory") == "enforced"


def evaluate_service_entities(
    runtime: MesaRuntime,
    settings_mode: str,
    token: TokenRecord,
    entities: list[str],
    *,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    session_id: str,
    confirm_approved: bool = False,
) -> MesaVerdict:
    """Evaluate every flattened entity through the MesaEnforcer.

    The enforcer runs with interactive=False, so a confirm entity surfaces as a
    ``control_mode:confirm_no_channel`` block. We interpret that host-side: under
    enforcement it becomes a confirm (routed to the admin gate); under advisory
    it becomes an allowed-with-warning. ``confirm_approved=True`` (re-execution
    after admin approval) folds confirm entities into allowed.
    """
    verdict = MesaVerdict()
    caller = build_caller_context(token, session_id)
    service_str = f"{domain}.{service}"
    now = dt_util.now()
    for entity_id in entities:
        result = runtime.enforcer.evaluate(
            entity_id=entity_id,
            service=service_str,
            # The parameters this call will execute, not a filtered view of them:
            # a decision made about a different payload than the one sent is the
            # divergence the whole target check exists to catch, and the audit
            # record would describe a call that never happened. service_data has
            # already had its target selectors CONSUMED by target resolution, so
            # they are absent from the outgoing call too, which is why nothing
            # here needs withholding. The resolved entity goes last so no caller
            # value can displace it.
            service_params={**service_data, "entity_id": entity_id},
            caller_context=caller,
            current_time=now,
        )
        verdict.warnings.extend(result.warnings)
        if result.allowed:
            verdict.allowed.append(entity_id)
            continue
        rule = result.rule_applied or "control_mode:blocked"
        if rule == "control_mode:confirm_no_channel":
            if confirm_approved or not _host_enforced(settings_mode, result):
                verdict.allowed.append(entity_id)
                # Advisory mode lets a confirm entity through; tell the agent why,
                # since otherwise the call looks identical to an unrestricted one.
                if not confirm_approved:
                    verdict.warnings.append(
                        f"{entity_id}: this action is marked 'confirm' in its MESA profile; "
                        "proceeding because MESA is in advisory mode (it would require admin "
                        "approval under enforced mode)."
                    )
            else:
                verdict.confirm.append(entity_id)
        else:
            if rule == _RULE_CONTRADICTORY_TARGET:
                # Not a policy decision about the entity: it means the payload
                # named a target other than the one the decision was made for,
                # which cannot happen once target resolution has consumed the
                # selectors. Reaching it is a Phoenix MCP bug, and the caller
                # only ever sees the uniform refusal, so log it or the cause is
                # invisible.
                _LOGGER.error(
                    "MESA refused %s on %s as a contradictory target, which means the "
                    "evaluated parameters diverged from the resolved targets: %s",
                    service_str, entity_id, result.reason,
                )
            verdict.blocked.append((entity_id, rule, result.reason))
    return verdict


def entity_control_mode(
    runtime: MesaRuntime | None, token: TokenRecord, entity_id: str, session_id: str = "phoenix_mcp"
) -> str | None:
    """Best-effort effective MESA control_mode for one entity (advisory display).

    control_mode is an entity-level property, so the representative service used
    to drive the evaluation does not change it. Returns the control_mode string
    (autonomous, confirm, read_only, prohibited) or None if it cannot be
    determined. Callers only pass entities the token can already access, so this
    is not an enumeration oracle.

    runtime is optional because every caller passes data.mesa, which is None when
    MESA never started; the catch-all below already returns None in that case,
    and the annotation says so rather than pushing the check to every caller.
    """
    if runtime is None:
        return None
    try:
        result = runtime.enforcer.evaluate(
            entity_id=entity_id,
            service=f"{entity_id.split('.')[0]}.turn_on",
            service_params={"entity_id": entity_id},
            caller_context=build_caller_context(token, session_id),
            current_time=dt_util.now(),
        )
        cm = result.effective_profile.operational_boundaries.control_mode
        return getattr(cm, "value", cm) if cm is not None else None
    except Exception:  # noqa: BLE001 - advisory display must never break the caller
        return None


def mesa_confirm_preview(
    data: PhoenixData,
    token: TokenRecord,
    *,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    entities: list[str],
) -> list[str]:
    """Which of these entities MESA would route to admin confirmation right now.

    Read-only companion to async_apply_mesa_to_call, used by the capability-gate
    approval diff builders: the admin's approval of a gated call also satisfies
    MESA's confirm (async_execute_approved_tool runs confirm-approved), so the
    approval record names the entities that confirmation covers. Best-effort by
    contract: returns [] when MESA is off or absent, and on ANY failure, because
    an approval diff must never fail to build over an annotation.
    """
    try:
        runtime = data.mesa
        settings = data.store.get_settings()
        if runtime is None or settings.mesa_mode == MESA_MODE_OFF or not entities:
            return []
        verdict = evaluate_service_entities(
            runtime, settings.mesa_mode, token, list(entities),
            domain=domain, service=service, service_data=service_data,
            session_id="approval_diff",
        )
        return list(verdict.confirm)
    except Exception:  # noqa: BLE001 - advisory annotation only
        return []


def build_mesa_service_diff(
    domain: str,
    service: str,
    service_data: dict[str, Any],
    verdict: MesaVerdict,
) -> dict[str, Any]:
    """Build a service_preview diff for a MESA-gated approval (admin review UI)."""
    return {
        "kind": "service_preview",
        **diff_summary_fields("mesa_service", domain=domain, service=service),
        "target": {"type": "service", "id": f"{domain}.{service}", "label": f"{domain}.{service}"},
        "preview": {
            "domain": domain,
            "service": service,
            "resolved_entity_ids": list(verdict.confirm + verdict.allowed),
            "service_data": dict(service_data),
            "mesa": {
                "confirm_entities": list(verdict.confirm),
                "allowed_entities": list(verdict.allowed),
                "blocked": [
                    {"entity_id": e, "rule": r, "reason": reason}
                    for e, r, reason in verdict.blocked
                ],
                "warnings": list(verdict.warnings),
            },
        },
    }


def _mesa_call_args(
    domain: str, service: str, service_data: dict[str, Any], entities: list[str]
) -> dict[str, Any]:
    """Saved-args payload for a MESA approval, re-runnable by the executor.

    Saves the explicit flattened entity list (the confirm + already-allowed
    entities) rather than the original area/name targets, so re-execution fires
    on exactly what was reviewed. The executor re-resolves scope per entity and
    re-runs MESA under confirm-approved semantics; entities that became
    prohibited or read_only since the request are still rejected.
    """
    return {
        "domain": domain,
        "service": service,
        "service_data": dict(service_data),
        "entity_id": list(entities),
    }


@dataclass
class MesaGateOutcome:
    """Result of applying the MESA gate to one service call."""

    decision: str  # "allow" | "deny" | "pending"
    entities: list[str] = field(default_factory=list)
    approval: PendingApproval | None = None
    blocked: list[tuple[str, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def async_apply_mesa_to_call(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    *,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    entities: list[str],
    request_id: str,
    client_ip: str | None,
    session_id: str,
    confirm_approved: bool = False,
) -> MesaGateOutcome:
    """Apply MESA enforcement to an already-flattened, Phoenix-permitted entity list.

    Returns an outcome the caller maps to its own response shape:
    - allow: proceed with ``outcome.entities`` (a subset of the input).
    - deny: every entity was blocked; caller returns its standard Forbidden.
    - pending: at least one entity needs confirmation; an approval was created.

    ``mesa_mode == off`` or a missing runtime short-circuits to allow-all.
    """
    runtime = data.mesa
    settings = data.store.get_settings()
    if runtime is None or settings.mesa_mode == MESA_MODE_OFF:
        return MesaGateOutcome(decision="allow", entities=list(entities))

    # Only this path sets the audit context: the same enforcer runs from
    # dry_run_service / find_available_actions to PREVIEW a verdict, and a
    # preview is not a denial worth a row in the operator's log.
    with mesa_audit.request_context(token, request_id, client_ip):
        verdict = evaluate_service_entities(
            runtime,
            settings.mesa_mode,
            token,
            entities,
            domain=domain,
            service=service,
            service_data=service_data,
            session_id=session_id,
            confirm_approved=confirm_approved,
        )

    if verdict.confirm and not confirm_approved:
        diff = build_mesa_service_diff(domain, service, service_data, verdict)
        approval = await async_create_mesa_approval(
            hass,
            data,
            token,
            args=_mesa_call_args(
                domain, service, service_data, verdict.confirm + verdict.allowed
            ),
            diff=diff,
            request_id=request_id,
            client_ip=client_ip,
        )
        return MesaGateOutcome(
            decision="pending",
            approval=approval,
            blocked=verdict.blocked,
            warnings=verdict.warnings,
        )

    if not verdict.allowed:
        return MesaGateOutcome(
            decision="deny", blocked=verdict.blocked, warnings=verdict.warnings
        )

    # NOTE (deferred, low severity): warnings flow to the token via mesa_advisory /
    # native speech regardless of cap_config_read, so an advisory warning reveals an
    # in-scope entity's control_mode even to a token that lacks profile-read access.
    # Not an enumeration oracle (the token targeted an entity it already has WRITE on).
    # If we ever want to hide it, gate warnings on cap_config_read at the surfacing sites.
    return MesaGateOutcome(
        decision="allow",
        entities=verdict.allowed,
        blocked=verdict.blocked,
        warnings=verdict.warnings,
    )


async def async_create_mesa_approval(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    *,
    args: dict[str, Any],
    diff: dict[str, Any],
    request_id: str,
    client_ip: str | None,
) -> PendingApproval:
    """Create a PendingApproval for a MESA confirm, mirroring async_evaluate_capability.

    The record carries the MESA sentinel cap (skips the effective_cap recheck on
    approve) and the non-dispatchable executor key (re-runs the call under MESA
    confirm-approved semantics).
    """
    from .approvals import (
        create_approval_notification,
        async_create_pending_approval,
        fire_approval_requested_event,
    )

    async with data.store.async_lock:
        approval = await async_create_pending_approval(
            data.store,
            token_id=token.id,
            token_name=token.name,
            tool_name=MESA_APPROVED_EXECUTOR,
            cap_name=MESA_CONFIRM_CAP,
            args=args,
            diff=diff,
            request_id=request_id,
            client_ip=client_ip,
        )
    create_approval_notification(hass, approval)
    fire_approval_requested_event(hass, approval)
    return approval


def fire_mesa_blocked_event(
    hass: HomeAssistant, token: TokenRecord, blocked: list[tuple[str, str, str]]
) -> None:
    """Fire phoenix_mcp_mesa_blocked for each entity MESA refused (automation hooks)."""
    from .const import DOMAIN

    for entity_id, rule, _reason in blocked:
        hass.bus.async_fire(
            f"{DOMAIN}_mesa_blocked",
            {
                "token_id": token.id,
                "token_name": token.name,
                "entity_id": entity_id,
                "rule_applied": rule,
                "timestamp": dt_util.utcnow().isoformat(),
            },
        )


def release_token_leases(data: Any, token_id: str) -> int:
    """Drop any advisory leases a token still holds. Returns how many.

    Called when a token is revoked or expires. Bookkeeping rather than a safety
    mechanism, and worth being clear about which: the action that actually stops
    a dead token is refusing its next call at the tool boundary, which
    authentication already does. A lease it left behind blocks other sessions for
    at most MAX_LEASE_DURATION_SECONDS, after which mesa-core drops it during the
    next request's own sweep, so this only shortens that window.

    Leases are keyed by session, and Phoenix runs the lease tools with the token
    id as the session, so releasing the token's session releases exactly its own.

    Fails quiet: leases are in-memory and advisory, so a failure here must never
    interrupt a revocation, which has real teardown to finish.
    """
    runtime = getattr(data, "mesa", None)
    if runtime is None:
        return 0
    try:
        return runtime.lease_manager.release_session(token_id)
    except Exception:  # noqa: BLE001 - advisory cleanup never blocks a revocation
        _LOGGER.debug("Phoenix MCP: releasing leases for token %s failed", token_id, exc_info=True)
        return 0
