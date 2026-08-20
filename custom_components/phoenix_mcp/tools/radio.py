"""Radio tools: the Zigbee network and device surface (cap_radio_write).

The TOOL layer only. The backend adapter that talks to Zigbee2MQTT and ZHA is
the package-level radio.py, imported here as `radio_backend` so the two are
never confused inside a module that shares its name.

Every tool resolves through the token's permission tree before touching a
radio: `_resolve_radio_device` returns a device only when the token can see it,
so a device lookup cannot become an oracle for the rest of the mesh. The four
mutating tools are Confirm-eligible and each builds a diff naming the specific
device, because "remove device" on a mesh is not reversible from here.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from typing import Any
import asyncio
import dataclasses
import json
import logging
import re

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant

from ..const import CAP_CONFIRM, CAP_DENY, PROXY_TIMEOUT_SECONDS
from ..data import PhoenixData
from ..mesa import (
    async_apply_mesa_to_call,
    entity_control_mode,
    fire_mesa_blocked_event,
)
from ..radio import RadioError
from ..helpers import (
    diff_summary_fields as _summary,
    effective_cap,
    version_summary_fields as _version_summary,
)
from .discovery import _accessible_entity_ids
from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _approved_exec_ctx,
    _gate,
    _mesa_advisory_ctx,
    _mesa_confirm_annotation,
    _pending_or_inline,
    _record_version,
    _tool_error,
    _tool_success,
    _truncate,
)
from ..policy_engine import Permission, resolve
from ..token_store import TokenRecord
from .. import radio as radio_backend

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _ResolvedRadioDevice:
    """A radio device resolved under a token's scope: all three always present."""

    device: Any
    backend: str
    ieee: str


@dataclasses.dataclass(frozen=True)
class _ResolvedZ2MOptionChange:
    """One validated, conflict-checked Zigbee2MQTT option update."""

    device: Any
    ieee: str
    entry: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    changes: dict[str, Any]
    before_hash: str


@dataclasses.dataclass(frozen=True)
class _Z2MPropertyCoverage:
    """HA MQTT discovery ownership for one device's exposed properties."""

    owners: dict[str, tuple[str, ...]]
    ambiguous: frozenset[str]


@dataclasses.dataclass(frozen=True)
class _ResolvedZ2MPropertyChange:
    """One validated, conflict-checked direct exposed-property update."""

    device: Any
    ieee: str
    entry: dict[str, Any]
    property_name: str
    schema: dict[str, Any]
    before: Any
    after: Any
    before_hash: str
    mesa_entities: tuple[str, ...]


def _z2m_root_property_groups(entry: dict[str, Any]) -> dict[str, list[set[str]]]:
    """Group recursive property leaves by each top-level expose type."""
    definition = entry.get("definition") if isinstance(entry, dict) else None
    exposes = definition.get("exposes") if isinstance(definition, dict) else None
    groups: dict[str, list[set[str]]] = {}

    def _properties(item: Any, depth: int = 0) -> set[str]:
        if not isinstance(item, dict) or depth > 4:
            return set()
        found = set()
        prop = item.get("property")
        if isinstance(prop, str) and prop:
            found.add(prop)
        raw_features = item.get("features")
        features = raw_features if isinstance(raw_features, list) else []
        for feature in features:
            found.update(_properties(feature, depth + 1))
        return found

    for root in exposes if isinstance(exposes, list) else []:
        if not isinstance(root, dict) or not isinstance(root.get("type"), str):
            continue
        props = _properties(root)
        if props:
            groups.setdefault(root["type"], []).append(props)
    return groups


def _z2m_property_coverage(
    hass: HomeAssistant, device_id: str, entry: dict[str, Any]
) -> _Z2MPropertyCoverage:
    """Index exact HA MQTT discovery ownership, failing closed on ambiguity."""
    # Private MQTT runtime detail, kept lazy so a compatibility drift cannot
    # prevent Phoenix itself from loading. Any failure below becomes ambiguity.
    try:
        from homeassistant.components.mqtt.const import ATTR_DISCOVERY_PAYLOAD  # noqa: PLC0415
        from homeassistant.components.mqtt.models import DATA_MQTT  # noqa: PLC0415
    except ImportError:
        return _Z2MPropertyCoverage({}, frozenset(radio_backend.z2m_exposed_property_map(entry)))
    definitions = radio_backend.z2m_exposed_property_map(entry)
    all_properties = set(definitions)
    if not all_properties:
        return _Z2MPropertyCoverage({}, frozenset())
    owners: dict[str, set[str]] = {prop: set() for prop in definitions}
    ambiguous: set[str] = set()
    mqtt_data = hass.data.get(DATA_MQTT)
    debug_entities = getattr(mqtt_data, "debug_info_entities", None)
    if not isinstance(debug_entities, dict):
        return _Z2MPropertyCoverage({}, frozenset(all_properties))

    root_groups = _z2m_root_property_groups(entry)
    entries = er.async_entries_for_device(
        er.async_get(hass), device_id, include_disabled_entities=True
    )
    mqtt_entry_count = 0
    for registry_entry in entries:
        if registry_entry.platform != "mqtt":
            continue
        mqtt_entry_count += 1
        if registry_entry.disabled_by is not None:
            # Even if stale debug metadata remains, disabling an entity must
            # never unlock a lower-level control path.
            ambiguous.update(all_properties)
            continue
        entity_info = debug_entities.get(registry_entry.entity_id)
        discovery_data = (
            entity_info.get("discovery_data")
            if isinstance(entity_info, dict)
            else None
        )
        payload = (
            discovery_data.get(ATTR_DISCOVERY_PAYLOAD)
            if isinstance(discovery_data, dict)
            else None
        )
        if not isinstance(payload, dict):
            # Disabled/unloaded MQTT entities are deliberately a full block:
            # disabling an HA entity must never unlock a lower-level bypass.
            ambiguous.update(all_properties)
            continue
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        recognized = {
            prop
            for prop in all_properties
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(prop)}(?![A-Za-z0-9_])",
                serialized,
            )
        }
        for prop in recognized:
            owners[prop].add(registry_entry.entity_id)

        matching_roots = root_groups.get(registry_entry.domain, [])
        if len(matching_roots) == 1:
            for prop in matching_roots[0]:
                if prop in owners:
                    owners[prop].add(registry_entry.entity_id)
        elif len(matching_roots) > 1:
            for prop in set().union(*matching_roots) - recognized:
                ambiguous.add(prop)
        elif not recognized:
            # A device MQTT entity whose discovery payload cannot be related to
            # any expose makes ownership indeterminate; fail closed.
            ambiguous.update(all_properties)

    if mqtt_entry_count == 0:
        ambiguous.update(all_properties)

    return _Z2MPropertyCoverage(
        {prop: tuple(sorted(entity_ids)) for prop, entity_ids in owners.items() if entity_ids},
        frozenset(ambiguous),
    )


def _property_fallback_summary(
    hass: HomeAssistant, device_id: str, entry: dict[str, Any]
) -> dict[str, Any]:
    """Bounded inventory of exposes safe for direct fallback."""
    definitions = radio_backend.z2m_exposed_property_map(entry)
    coverage = _z2m_property_coverage(hass, device_id, entry)
    items = []
    for prop, schema in sorted(definitions.items()):
        access = schema.get("access")
        if (
            not isinstance(access, int)
            or isinstance(access, bool)
            or not access & 1
            or prop in coverage.owners
            or prop in coverage.ambiguous
        ):
            continue
        items.append(
            {
                "property": prop,
                "readable": True,
                "writable": bool(access & 2),
                "definition": radio_backend.project_z2m_property_definition(schema),
            }
        )
    return {
        "status": "available" if items else "unavailable",
        "properties": items,
        "note": (
            "Direct fallback is listed only when MQTT discovery proves no Home Assistant entity owns the property."
        ),
    }


def _resolve_radio_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str, *, require_write: bool
) -> _ResolvedRadioDevice | tuple[dict, str, str]:
    """Resolve a device_id to a _ResolvedRadioDevice, or the tool error to return.

    Returns one or the other, not an (error, device, backend, ieee) 4-tuple. The
    tuple form guaranteed the three values were non-None only via the FIRST
    element, so every caller then passed `str | None` into code needing a str.
    This is the same single-return shape as _read_body, _validate_preset_name and
    yaml_includes._layout_or_result; a frozen dataclass rather than a NamedTuple
    so `isinstance(..., tuple)` discriminates the two cases cleanly. Visibility mirrors get_device: the device must have at least one
    accessible entity, and a ghost device and an out-of-scope device return
    identical not_found bodies (no existence oracle). Write ops additionally
    require WRITE on at least one of the device's entities; a visible but
    READ-only device gets a specific message, since the token can already see
    it. A visible non-radio device (including the Z2M bridge itself, which
    never detects as a radio device, protecting the coordinator) is
    invalid_request.
    """
    device_id = str(args.get("device_id") or "").strip()
    if not device_id:
        return _tool_error("device_id is required."), "invalid_request", tool_name
    device = dr.async_get(hass).async_get(device_id)
    ent_reg = er.async_get(hass)
    accessible = [
        eid for eid in _accessible_entity_ids(token, hass)
        if (entry := ent_reg.async_get(eid)) is not None and entry.device_id == device_id
    ]
    if device is None or not accessible:
        return _tool_error("Device not found."), "not_found", device_id
    detected = radio_backend.zigbee_backend_for_device(device)
    if detected is None:
        return _tool_error("Not a radio-managed device."), "invalid_request", device_id
    if require_write and not any(
        resolve(eid, token, hass) == Permission.WRITE for eid in accessible
    ):
        return (
            _tool_error("Read-only access to this device; radio writes need write access."),
            "denied",
            device_id,
        )
    backend, ieee = detected
    return _ResolvedRadioDevice(device=device, backend=backend, ieee=ieee)


async def _resolve_z2m_option_change(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> _ResolvedZ2MOptionChange | tuple[dict, str, str]:
    """Resolve, validate, and conflict-check a converter-option update."""
    tool = "set_zigbee_device_options"
    resolved = _resolve_radio_device(
        args, token, hass, tool, require_write=True
    )
    if isinstance(resolved, tuple):
        return resolved
    if resolved.backend != radio_backend.BACKEND_Z2M:
        return (
            _tool_error("Device options are available only for Zigbee2MQTT devices."),
            "invalid_request",
            resolved.device.id,
        )
    expected_hash = args.get("expected_hash")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(char not in "0123456789abcdef" for char in expected_hash)
    ):
        return (
            _tool_error(
                "expected_hash must be the lowercase content_hash returned by get_radio_device."
            ),
            "invalid_request",
            resolved.device.id,
        )
    try:
        entry, before, before_hash = await radio_backend.async_z2m_device_snapshot(
            hass, resolved.ieee
        )
    except RadioError as exc:
        return (
            _tool_error(f"Failed to read Zigbee2MQTT device options: {exc}"),
            "invalid_request",
            resolved.device.id,
        )
    if entry is None:
        return (
            _tool_error("Device is not on the Zigbee network."),
            "invalid_request",
            resolved.device.id,
        )
    if expected_hash != before_hash:
        return (
            _tool_error(
                "Zigbee2MQTT device options changed after they were read. "
                "Call get_radio_device again and retry with its new content_hash."
            ),
            "invalid_request",
            resolved.device.id,
        )
    try:
        changes = radio_backend.validate_z2m_option_changes(
            entry, args.get("options")
        )
    except radio_backend.Z2MOptionValidationError as exc:
        return _tool_error(str(exc)), "invalid_request", resolved.device.id
    after = {**before, **changes}
    if after == before:
        return (
            _tool_error("The requested Zigbee2MQTT options are already current."),
            "invalid_request",
            resolved.device.id,
        )
    return _ResolvedZ2MOptionChange(
        device=resolved.device,
        ieee=resolved.ieee,
        entry=entry,
        before=before,
        after=after,
        changes=changes,
        before_hash=before_hash,
    )


async def _resolve_z2m_property_change(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> _ResolvedZ2MPropertyChange | tuple[dict, str, str]:
    """Resolve, ownership-check, validate, and conflict-check a direct write."""
    tool = "set_zigbee_device_property"
    resolved = _resolve_radio_device(args, token, hass, tool, require_write=True)
    if isinstance(resolved, tuple):
        return resolved
    if resolved.backend != radio_backend.BACKEND_Z2M:
        return (
            _tool_error("Direct exposed properties are available only for Zigbee2MQTT devices."),
            "invalid_request",
            resolved.device.id,
        )
    property_name = args.get("property")
    if not isinstance(property_name, str) or not property_name:
        return (
            _tool_error("property must be a non-empty exposed property name."),
            "invalid_request",
            resolved.device.id,
        )
    expected_hash = args.get("expected_hash")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(char not in "0123456789abcdef" for char in expected_hash)
    ):
        return (
            _tool_error(
                "expected_hash must be the lowercase content_hash returned by get_radio_device's property_read."
            ),
            "invalid_request",
            resolved.device.id,
        )
    try:
        entry, _options, _options_hash = await radio_backend.async_z2m_device_snapshot(
            hass, resolved.ieee
        )
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", resolved.device.id
    if entry is None:
        return (
            _tool_error("Device is not on the Zigbee network."),
            "invalid_request",
            resolved.device.id,
        )
    definitions = radio_backend.z2m_exposed_property_map(entry)
    schema = definitions.get(property_name)
    if not isinstance(schema, dict):
        return (
            _tool_error("Unknown, ambiguous, or non-readable/writable exposed property."),
            "invalid_request",
            resolved.device.id,
        )
    access = schema.get("access")
    if isinstance(access, bool) or not isinstance(access, int) or access & 3 != 3:
        return (
            _tool_error("Unknown, ambiguous, or non-readable/writable exposed property."),
            "invalid_request",
            resolved.device.id,
        )
    coverage = _z2m_property_coverage(hass, resolved.device.id, entry)
    if property_name in coverage.owners:
        accessible = _accessible_entity_ids(token, hass)
        visible = sorted(set(coverage.owners[property_name]) & accessible)
        message = (
            f"Use Home Assistant entity {visible[0]} for this property."
            if visible
            else "Use the Home Assistant entity that owns this property."
        )
        return _tool_error(message), "invalid_request", resolved.device.id
    if property_name in coverage.ambiguous:
        return (
            _tool_error(
                "Direct fallback is unavailable because Home Assistant MQTT discovery ownership is ambiguous."
            ),
            "invalid_request",
            resolved.device.id,
        )
    try:
        before = await radio_backend.async_read_z2m_exposed_property(
            hass, entry, property_name, schema
        )
        after = radio_backend.validate_z2m_exposed_value(
            property_name, args.get("value"), schema
        )
    except (RadioError, radio_backend.Z2MPropertyValidationError) as exc:
        return _tool_error(str(exc)), "invalid_request", resolved.device.id
    before_hash = radio_backend.z2m_property_hash(property_name, before)
    if before_hash != expected_hash:
        return (
            _tool_error(
                "The Zigbee2MQTT property changed after it was read. Call get_radio_device again and retry with its new content_hash."
            ),
            "invalid_request",
            resolved.device.id,
        )
    if type(before) is type(after) and before == after:
        return (
            _tool_error("The requested Zigbee2MQTT property value is already current."),
            "invalid_request",
            resolved.device.id,
        )
    mesa_entities = tuple(
        sorted(
            registry_entry.entity_id
            for registry_entry in er.async_entries_for_device(
                er.async_get(hass),
                resolved.device.id,
                include_disabled_entities=True,
            )
            if resolve(registry_entry.entity_id, token, hass) == Permission.WRITE
        )
    )
    return _ResolvedZ2MPropertyChange(
        device=resolved.device,
        ieee=resolved.ieee,
        entry=entry,
        property_name=property_name,
        schema=schema,
        before=before,
        after=after,
        before_hash=before_hash,
        mesa_entities=mesa_entities,
    )


@dataclasses.dataclass(frozen=True)
class _ResolvedPermitJoin:
    """Validated permit_zigbee_join args; backend and duration always present."""

    backend: str
    duration: int
    router_ieee: str | None = None
    router_label: str | None = None


def _resolve_permit_join(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> _ResolvedPermitJoin | tuple[dict, str, str]:
    """Validate permit_zigbee_join args to (backend, duration, router_ieee, router_label).

    Shared by the pre-gate check, the diff builder, and the executor so all
    three see the same resolution. The optional router device needs only READ
    visibility (permitting via a router does not mutate the router, and the
    no-router variant needs no device at all). With both backends present and
    no router, the backend arg is required.
    """
    duration_raw = args.get("duration", 60)
    if isinstance(duration_raw, str) and duration_raw.strip().lstrip("-").isdigit():
        duration_raw = int(duration_raw)
    if isinstance(duration_raw, bool) or not isinstance(duration_raw, int) or not 0 <= duration_raw <= 254:
        return _tool_error("duration must be an integer between 0 and 254."), "invalid_request", "permit_zigbee_join"
    backend = args.get("backend")
    if backend is not None and backend not in radio_backend.ZIGBEE_BACKENDS:
        return _tool_error("backend must be one of: z2m, zha."), "invalid_request", "permit_zigbee_join"
    router_ieee = None
    router_label = None
    if args.get("device_id"):
        resolved = _resolve_radio_device(
            args, token, hass, "permit_zigbee_join", require_write=False
        )
        if isinstance(resolved, tuple):
            return resolved
        device, dev_backend, ieee = resolved.device, resolved.backend, resolved.ieee
        if backend is not None and backend != dev_backend:
            return _tool_error("The router device belongs to the other Zigbee backend."), "invalid_request", "permit_zigbee_join"
        backend = dev_backend
        router_ieee = ieee
        router_label = device.name_by_user or device.name or ieee
    present = radio_backend.present_zigbee_backends(hass)
    if backend is None:
        if not present:
            return _tool_error("No Zigbee network is available."), "invalid_request", "permit_zigbee_join"
        if len(present) > 1:
            return _tool_error("Both Zigbee backends are present; pass backend: z2m or zha."), "invalid_request", "permit_zigbee_join"
        backend = present[0]
    elif backend not in present:
        return _tool_error("That Zigbee backend is not available."), "invalid_request", "permit_zigbee_join"
    return _ResolvedPermitJoin(backend, duration_raw, router_ieee, router_label)


async def _build_diff_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> dict:
    permit = _resolve_permit_join(args, token, hass)
    if isinstance(permit, tuple):
        return {"kind": "system_action", **_summary("zigbee_permit"), "preview": {}}
    backend, duration, router_label = permit.backend, permit.duration, permit.router_label
    if duration == 0:
        fields = _summary("zigbee_permit.close")
        warning = "Closes the join window immediately."
    else:
        fields = _summary("zigbee_permit.open", duration=duration)
        warning = "Any nearby Zigbee device can join while the network is open."
    preview: dict[str, Any] = {"duration": duration, "backend": backend, "warning": warning}
    if router_label:
        preview["via_device"] = router_label
    return {
        "kind": "system_action",
        **fields,
        "target": {"type": "system", "id": f"zigbee:{backend}", "label": "Zigbee network"},
        "preview": preview,
    }


def _radio_device_diff_preview(device: Any, ieee: str) -> dict:
    return {
        "device": device.name_by_user or device.name or ieee,
        "ieee": ieee,
        "manufacturer": device.manufacturer,
        "model": device.model,
    }


async def _build_diff_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> dict:
    resolved = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return {"kind": "system_action", **_summary("zigbee_reconfigure"), "preview": {}}
    device, ieee = resolved.device, resolved.ieee
    preview = _radio_device_diff_preview(device, ieee)
    preview["note"] = "Re-interviews the device; it may be briefly unresponsive. Battery devices must be awake."
    label = preview["device"]
    return {
        "kind": "system_action",
        **_summary("zigbee_reconfigure.device", label=label),
        "target": {"type": "device", "id": device.id, "label": label},
        "preview": preview,
    }


async def _build_diff_set_zigbee_device_options(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    *,
    change: _ResolvedZ2MOptionChange | None = None,
) -> dict:
    if change is None:
        resolved_change = await _resolve_z2m_option_change(args, token, hass)
    else:
        resolved_change = change
    if isinstance(resolved_change, tuple):
        return {
            "kind": "config_diff",
            **_summary("zigbee_options"),
            "before": "{}",
            "after": "{}",
            "preview": {},
        }
    change = resolved_change
    label = change.device.name_by_user or change.device.name or change.ieee
    keys = sorted(change.changes)
    return {
        "kind": "config_diff",
        **_summary(
            "zigbee_options.device", label=label, keys=", ".join(keys)
        ),
        "target": {
            "type": "device",
            "id": change.device.id,
            "label": label,
        },
        "before": _truncate(json.dumps(change.before, indent=2, default=str)),
        "after": _truncate(json.dumps(change.after, indent=2, default=str)),
        "preview": {
            "backend": radio_backend.BACKEND_Z2M,
            "changed_keys": keys,
            "content_hash": change.before_hash,
            "warning": (
                "Some options require a Zigbee2MQTT restart. Phoenix reports that "
                "requirement but does not restart Zigbee2MQTT automatically."
            ),
        },
    }


async def _build_diff_set_zigbee_device_property(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    *,
    change: _ResolvedZ2MPropertyChange | None = None,
) -> dict:
    """Approval preview for one exact direct exposed-property change."""
    resolved_change = change or await _resolve_z2m_property_change(args, token, hass)
    if isinstance(resolved_change, tuple):
        return {
            "kind": "config_diff",
            **_summary("zigbee_property"),
            "before": "null",
            "after": "null",
            "preview": {},
        }
    change = resolved_change
    label = change.device.name_by_user or change.device.name or change.ieee
    preview: dict[str, Any] = {
        "backend": radio_backend.BACKEND_Z2M,
        "property": change.property_name,
        "content_hash": change.before_hash,
        "warning": (
            "This bypasses Home Assistant's entity layer only because MQTT discovery proves no entity owns the property."
        ),
    }
    mesa_note = _mesa_confirm_annotation(
        token,
        hass,
        [("zigbee", "set_property", list(change.mesa_entities))],
    )
    if mesa_note:
        preview["mesa"] = mesa_note
    return {
        "kind": "config_diff",
        **_summary(
            "zigbee_property.device",
            label=label,
            property=change.property_name,
        ),
        "target": {"type": "device", "id": change.device.id, "label": label},
        "before": _truncate(json.dumps(change.before, indent=2, default=str)),
        "after": _truncate(json.dumps(change.after, indent=2, default=str)),
        "preview": preview,
    }


async def _build_diff_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> dict:
    resolved = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return {"kind": "system_action", **_summary("zigbee_remove"), "preview": {}}
    device, ieee = resolved.device, resolved.ieee
    preview = _radio_device_diff_preview(device, ieee)
    # The approval diff is admin-only, so it lists all of the device's registry
    # entities (not just the token-visible ones) plus any MESA control modes:
    # the reviewer should see the full blast radius of dropping the device.
    entries = er.async_entries_for_device(er.async_get(hass), device.id, include_disabled_entities=True)
    preview["entities"] = sorted(entry.entity_id for entry in entries)
    mesa_modes = {}
    for entry in entries:
        mode = entity_control_mode(data.mesa, token, entry.entity_id)
        if mode is not None:
            mesa_modes[entry.entity_id] = mode
    if mesa_modes:
        preview["mesa_control_modes"] = mesa_modes
    preview["warning"] = "Device must be re-paired to rejoin; its entities become unavailable."
    label = preview["device"]
    return {
        "kind": "system_action",
        **_summary("zigbee_remove.device", label=label),
        "target": {"type": "device", "id": device.id, "label": label},
        "preview": preview,
    }


async def _tool_get_radio_network(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: report every radio network present (safe projection only)."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_radio_network"
    networks = await radio_backend.async_radio_networks(hass)
    return (
        _tool_success(json.dumps({"networks": networks}, default=str)),
        "allowed",
        "get_radio_network",
    )


async def _tool_get_radio_device(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: radio-level diagnostics for one visible device."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_radio_device"
    resolved = _resolve_radio_device(
        args, token, hass, "get_radio_device", require_write=False
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        if backend == radio_backend.BACKEND_Z2M:
            entry, current_options, _options_hash = (
                await radio_backend.async_z2m_device_snapshot(hass, ieee)
            )
            if entry is None:
                # Registered in HA but unknown to Z2M (a stale discovery
                # leftover). The token already sees the device, so no oracle.
                return _tool_error("Device is not on the Zigbee network."), "invalid_request", device.id
            projected = radio_backend.project_z2m_device(
                entry, current_options=current_options
            )
            projected["direct_property_fallback"] = _property_fallback_summary(
                hass, device.id, entry
            )
            requested_property = args.get("property")
            if requested_property is not None:
                if not isinstance(requested_property, str) or not requested_property:
                    return (
                        _tool_error("property must be a non-empty exposed property name."),
                        "invalid_request",
                        device.id,
                    )
                definitions = radio_backend.z2m_exposed_property_map(entry)
                schema = definitions.get(requested_property)
                if not isinstance(schema, dict):
                    return (
                        _tool_error("Unknown, ambiguous, or unreadable exposed property."),
                        "invalid_request",
                        device.id,
                    )
                access = schema.get("access")
                if isinstance(access, bool) or not isinstance(access, int) or not access & 1:
                    return (
                        _tool_error("Unknown, ambiguous, or unreadable exposed property."),
                        "invalid_request",
                        device.id,
                    )
                coverage = _z2m_property_coverage(hass, device.id, entry)
                owners = coverage.owners.get(requested_property, ())
                if owners:
                    accessible = _accessible_entity_ids(token, hass)
                    visible_owners = sorted(set(owners) & accessible)
                    message = (
                        f"Use Home Assistant entity {visible_owners[0]} for this property."
                        if visible_owners
                        else "Use the Home Assistant entity that owns this property."
                    )
                    return _tool_error(message), "invalid_request", device.id
                if requested_property in coverage.ambiguous:
                    return (
                        _tool_error(
                            "Direct fallback is unavailable because Home Assistant MQTT discovery ownership is ambiguous."
                        ),
                        "invalid_request",
                        device.id,
                    )
                value = await radio_backend.async_read_z2m_exposed_property(
                    hass, entry, requested_property, schema
                )
                projected["property_read"] = {
                    "property": requested_property,
                    "value": value,
                    "content_hash": radio_backend.z2m_property_hash(
                        requested_property, value
                    ),
                    "definition": radio_backend.project_z2m_property_definition(schema),
                    "source": "zigbee2mqtt_direct_fallback",
                }
        else:
            raw = await radio_backend.async_zha_device_info(hass, ieee)
            accessible = _accessible_entity_ids(token, hass)
            projected = radio_backend.project_zha_device(
                raw,
                accessible_entity_ids=accessible,
                visible_ieee_map=radio_backend.visible_zigbee_device_map(hass, accessible),
                pass_through=token.pass_through,
            )
    except RadioError as exc:
        return _tool_error(f"Failed to read Zigbee device: {exc}"), "invalid_request", device.id
    projected["device_id"] = device.id
    projected["name"] = device.name_by_user or device.name or projected.get("friendly_name")
    if device.area_id:
        projected["area_id"] = device.area_id
    return _tool_success(json.dumps(projected, default=str)), "allowed", device.id


async def _tool_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: open/close the Zigbee join window (Confirm-eligible)."""
    # Capability gate first: a denied token gets a uniform Forbidden with no
    # device work, so the tool can never be a scope/existence oracle.
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "permit_zigbee_join"
    pre = _resolve_permit_join(args, token, hass)
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="permit_zigbee_join", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_permit_zigbee_join(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_permit_zigbee_join(args, token, hass, data)


async def _execute_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    permit = _resolve_permit_join(args, token, hass)
    if isinstance(permit, tuple):
        return permit
    backend, duration = permit.backend, permit.duration
    router_ieee, router_label = permit.router_ieee, permit.router_label
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await radio_backend.async_permit_join(hass, backend, duration, router_ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", "permit_zigbee_join"
    except TimeoutError:
        return _tool_error("Timed out talking to the Zigbee network."), "invalid_request", "permit_zigbee_join"
    except HomeAssistantError as exc:
        _LOGGER.error("permit_zigbee_join failed: %s", exc)
        return _tool_error("Failed to change the Zigbee join window."), "denied", "permit_zigbee_join"
    body: dict[str, Any] = {"success": True, "backend": backend, "duration": duration}
    if duration == 0:
        body["message"] = "Join window closed."
    else:
        body["message"] = f"Network open for joining for {duration} seconds."
    if router_label:
        body["via_device"] = router_label
    return _tool_success(json.dumps(body)), "allowed", "permit_zigbee_join"


async def _tool_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: re-interview a Zigbee device (Confirm-eligible)."""
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "reconfigure_zigbee_device"
    pre = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="reconfigure_zigbee_device", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_reconfigure_zigbee_device(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_reconfigure_zigbee_device(args, token, hass, data)


async def _execute_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    resolved = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            result = await radio_backend.async_reconfigure_device(hass, backend, ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", device.id
    except TimeoutError:
        # Z2M answers device/configure only when the interview finishes; no
        # answer inside the window means it is still working (a battery
        # device may be asleep, but a mains device can just be slow to
        # complete its full endpoint/binding re-interview).
        body = {
            "success": True,
            "partial": True,
            "message": "Reconfigure started; it did not finish within the timeout and is still running.",
        }
        return _tool_success(json.dumps(body)), "allowed", device.id
    except HomeAssistantError as exc:
        _LOGGER.error("reconfigure_zigbee_device failed for %s: %s", ieee, exc)
        return _tool_error("Failed to reconfigure the device."), "denied", device.id
    if result.get("completed"):
        message = "Reconfigure completed."
    else:
        message = "Reconfigure started; it completes in the background (check get_radio_device later)."
    return (
        _tool_success(json.dumps({"success": True, "ieee": ieee, "backend": backend, "message": message})),
        "allowed",
        device.id,
    )


async def _tool_set_zigbee_device_options(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: change Z2M converter options (Confirm-eligible)."""
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_zigbee_device_options"
    pre = await _resolve_z2m_option_change(args, token, hass)
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write",
        token,
        hass,
        data,
        tool_name="set_zigbee_device_options",
        args=args,
        request_id=request_id,
        client_ip=client_ip,
        diff=lambda: _build_diff_set_zigbee_device_options(
            args, token, hass, change=pre
        ),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_zigbee_device_options(args, token, hass, data)


async def _execute_set_zigbee_device_options(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Apply one validated option update and record its bounded history."""
    change = await _resolve_z2m_option_change(args, token, hass)
    if isinstance(change, tuple):
        return change
    confirmation = "response"
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            result = await radio_backend.async_set_z2m_device_options(
                hass, change.ieee, change.changes
            )
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", change.device.id
    except TimeoutError:
        try:
            _entry, retained_after, _retained_hash, restart_required = (
                await radio_backend.async_confirm_z2m_device_options(
                    hass, change.ieee, change.changes
                )
            )
        except (RadioError, TimeoutError):
            return (
                _tool_error(
                    "Timed out waiting for Zigbee2MQTT to confirm the option change, "
                    "and the requested values were not visible in retained state. "
                    "Call get_radio_device before retrying because the change may apply later."
                ),
                "invalid_request",
                change.device.id,
            )
        result = {
            "to": retained_after,
            "restart_required": restart_required,
        }
        confirmation = "retained_state"

    raw_to = result.get("to")
    try:
        after = radio_backend.project_z2m_option_values(change.entry, raw_to)
        if not isinstance(raw_to, dict) or any(
            key not in raw_to for key in change.changes
        ):
            raise radio_backend.Z2MOptionValidationError(
                "Zigbee2MQTT returned no value for a requested option."
            )
        confirmed = radio_backend.validate_z2m_option_changes(
            change.entry,
            {key: raw_to[key] for key in change.changes},
        )
    except radio_backend.Z2MOptionValidationError as exc:
        _LOGGER.error(
            "set_zigbee_device_options received an unsafe result for %s: %s",
            change.ieee,
            exc,
        )
        return (
            _tool_error(
                "Zigbee2MQTT applied the request but returned an unsafe or incomplete result. "
                "Call get_radio_device to verify the current options."
            ),
            "invalid_request",
            change.device.id,
        )
    if confirmed != change.changes:
        return (
            _tool_error(
                "Zigbee2MQTT did not confirm every requested option value. "
                "Call get_radio_device to verify the current options."
            ),
            "invalid_request",
            change.device.id,
        )
    after_hash = radio_backend.z2m_options_hash(after)
    label = change.device.name_by_user or change.device.name or change.ieee
    snapshot_before = {
        "snapshot_type": "zigbee_device_options",
        "restorable": False,
        "options": change.before,
        "content_hash": change.before_hash,
    }
    snapshot_after = {
        "snapshot_type": "zigbee_device_options",
        "restorable": False,
        "options": after,
        "content_hash": after_hash,
    }
    await _record_version(
        data,
        token,
        resource_type="device",
        resource_id=change.device.id,
        action="edit",
        before=snapshot_before,
        after=snapshot_after,
        alias=label,
        summary=_version_summary(
            "device.zigbee_options", keys=", ".join(sorted(change.changes))
        ),
    )
    restart_required = bool(result.get("restart_required"))
    return (
        _tool_success(
            json.dumps(
                {
                    "success": True,
                    "backend": radio_backend.BACKEND_Z2M,
                    "device_id": change.device.id,
                    "changed": change.changes,
                    "options": after,
                    "content_hash": after_hash,
                    "restart_required": restart_required,
                    "confirmation": confirmation,
                    "message": (
                        "Options changed. Restart Zigbee2MQTT to apply every change."
                        if restart_required
                        else "Options changed."
                    ),
                }
            )
        ),
        "allowed",
        change.device.id,
    )


async def _tool_set_zigbee_device_property(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: direct Z2M exposed-property fallback (dual-gated)."""
    caps = ("cap_radio_write", "cap_physical_control")
    if any(effective_cap(token, cap) == CAP_DENY for cap in caps):
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_zigbee_device_property"
    pre = await _resolve_z2m_property_change(args, token, hass)
    if isinstance(pre, tuple):
        return pre
    pre_diff = await _build_diff_set_zigbee_device_property(
        args, token, hass, change=pre
    )
    mesa_outcome = await async_apply_mesa_to_call(
        hass,
        data,
        token,
        domain="zigbee",
        service="set_property",
        service_data={"property": pre.property_name, "value": pre.after},
        entities=list(pre.mesa_entities),
        request_id=request_id or "zigbee_property",
        client_ip=client_ip,
        session_id=request_id or "zigbee_property",
        confirm_approved=False,
        approval_tool_name="set_zigbee_device_property",
        approval_args=args,
        approval_diff=pre_diff,
        require_all=True,
    )
    if mesa_outcome.blocked:
        fire_mesa_blocked_event(hass, token, mesa_outcome.blocked)
    if mesa_outcome.decision == "pending":
        return await _pending_or_inline(hass, data, token, mesa_outcome.approval)
    if mesa_outcome.decision == "deny":
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", pre.device.id
    if mesa_outcome.warnings:
        _mesa_advisory_ctx.set(True)
    if not _approved_exec_ctx.get():
        for cap in caps:
            if effective_cap(token, cap) != CAP_CONFIRM:
                continue
            blocked = await _gate(
                cap,
                token,
                hass,
                data,
                tool_name="set_zigbee_device_property",
                args=args,
                request_id=request_id,
                client_ip=client_ip,
                diff=pre_diff,
            )
            if blocked is not None:
                return blocked
    return await _execute_set_zigbee_device_property(
        args, token, hass, data, request_id=request_id, client_ip=client_ip
    )


async def _execute_set_zigbee_device_property(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """Apply one exact direct property write after all gates are satisfied."""
    if any(
        effective_cap(token, cap) == CAP_DENY
        for cap in ("cap_radio_write", "cap_physical_control")
    ):
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_zigbee_device_property"
    change = await _resolve_z2m_property_change(args, token, hass)
    if isinstance(change, tuple):
        return change

    diff = await _build_diff_set_zigbee_device_property(
        args, token, hass, change=change
    )
    mesa_outcome = await async_apply_mesa_to_call(
        hass,
        data,
        token,
        domain="zigbee",
        service="set_property",
        service_data={
            "property": change.property_name,
            "value": change.after,
        },
        entities=list(change.mesa_entities),
        request_id=request_id or "zigbee_property",
        client_ip=client_ip,
        session_id=request_id or "zigbee_property",
        confirm_approved=_approved_exec_ctx.get(),
        approval_tool_name="set_zigbee_device_property",
        approval_args=args,
        approval_diff=diff,
        require_all=True,
    )
    if mesa_outcome.blocked:
        fire_mesa_blocked_event(hass, token, mesa_outcome.blocked)
    if mesa_outcome.decision == "pending":
        return await _pending_or_inline(
            hass, data, token, mesa_outcome.approval
        )
    if mesa_outcome.decision == "deny":
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", change.device.id
    if mesa_outcome.warnings:
        _mesa_advisory_ctx.set(True)

    try:
        confirmed = await radio_backend.async_set_z2m_exposed_property(
            hass,
            change.entry,
            change.property_name,
            change.after,
            change.schema,
        )
    except (RadioError, radio_backend.Z2MPropertyValidationError) as exc:
        return _tool_error(str(exc)), "invalid_request", change.device.id

    after_hash = radio_backend.z2m_property_hash(change.property_name, confirmed)
    label = change.device.name_by_user or change.device.name or change.ieee
    await _record_version(
        data,
        token,
        resource_type="device",
        resource_id=change.device.id,
        action="edit",
        before={
            "snapshot_type": "zigbee_exposed_property",
            "restorable": False,
            "property": change.property_name,
            "value": change.before,
            "content_hash": change.before_hash,
        },
        after={
            "snapshot_type": "zigbee_exposed_property",
            "restorable": False,
            "property": change.property_name,
            "value": confirmed,
            "content_hash": after_hash,
        },
        alias=label,
        summary=_version_summary(
            "device.zigbee_property", property=change.property_name
        ),
    )
    body: dict[str, Any] = {
        "success": True,
        "backend": radio_backend.BACKEND_Z2M,
        "device_id": change.device.id,
        "property": change.property_name,
        "previous": change.before,
        "value": confirmed,
        "content_hash": after_hash,
        "confirmation": "device_state",
        "message": "Property changed and confirmed by Zigbee2MQTT device state.",
    }
    if mesa_outcome.warnings:
        body["mesa_advisory"] = mesa_outcome.warnings
    return _tool_success(json.dumps(body)), "allowed", change.device.id


async def _tool_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: remove a device from the Zigbee network (Confirm-eligible)."""
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "remove_zigbee_device"
    pre = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="remove_zigbee_device", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_remove_zigbee_device(args, token, hass, data),
    )
    if blocked is not None:
        return blocked
    return await _execute_remove_zigbee_device(args, token, hass, data)


async def _execute_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    resolved = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await radio_backend.async_remove_device(hass, backend, ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", device.id
    except TimeoutError:
        # Removing an unreachable device can outlast the window; the leave
        # request stays queued, so report partial rather than failure.
        body = {
            "success": True,
            "partial": True,
            "message": "Removal started; the network drops the device when it responds or times out.",
        }
        return _tool_success(json.dumps(body)), "allowed", device.id
    except HomeAssistantError as exc:
        _LOGGER.error("remove_zigbee_device failed for %s: %s", ieee, exc)
        return _tool_error("Failed to remove the device."), "denied", device.id
    return (
        _tool_success(json.dumps({
            "success": True,
            "removed": True,
            "ieee": ieee,
            "backend": backend,
            "message": "Device removed. Re-pair it to rejoin the network.",
        })),
        "allowed",
        device.id,
    )
