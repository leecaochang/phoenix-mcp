"""Permission resolution engine for Phoenix MCP. No I/O."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow

from .const import (
    BLOCKED_DOMAINS,
    DOMAIN,
    ESPHOME_DOMAIN,
    PHYSICAL_GATE_DOMAINS,
    SENSITIVE_ATTRIBUTES,
    SENSITIVE_KEY_SUBSTRINGS,
)
from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)


class Permission(str, Enum):
    WRITE = "write"
    READ = "read"
    DENY = "deny"
    NO_ACCESS = "no_access"
    NOT_FOUND = "not_found"


class ConfigEntryRegistryContext(NamedTuple):
    """Exact registry membership owned by one Home Assistant config entry."""

    entry: Any
    entity_ids: tuple[str, ...]
    device_ids: tuple[str, ...]


class EntityCreationNotPermitted(Exception):
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(
            f"Entity {entity_id!r} is not in the entity registry; "
            "Phoenix MCP does not permit entity creation via service calls."
        )


_RELATIVE_TIME_RE = re.compile(r"^(\d+)(h|d|w|m)$")
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def canonical_entity_id(entity_id: str, hass: HomeAssistant) -> str:
    """Resolve an entity_id (or registry id) to its canonical entity_id.

    Mirrors the alias canonicalization resolve() does internally, so callers that
    fetch state after a permission check use the same id resolve() granted on
    (avoids a false 404 when a registry id or alias was supplied).
    """
    entry = er.async_get(hass).async_get(entity_id)
    return entry.entity_id if entry else entity_id


def resolve(entity_id: str, token: TokenRecord, hass: HomeAssistant) -> Permission:
    """Resolve effective permission for entity_id against a token.

    Return values:
        Permission.WRITE      - write access (unrestricted for pass-through tokens)
        Permission.READ       - read-only access
        Permission.DENY       - explicitly denied; audit outcome: denied
        Permission.NO_ACCESS  - no grant found; audit outcome: denied
        Permission.NOT_FOUND  - ghost entity (not in states or registry); audit outcome: not_found

    Pass-through tokens return WRITE for all entities (after Phoenix MCP blocklist check).
    The DUAL_GATE_SERVICES check is NOT performed here; it is the caller's responsibility.
    """
    # A non-string cannot name an entity, and a model does not always follow the
    # tool schema (a list where a string was declared is live-observed). Treated
    # as a ghost so the answer is byte-identical to any other absent entity and
    # fails closed, instead of raising out of the single choke point every
    # permission decision runs through.
    if not isinstance(entity_id, str):
        return Permission.NOT_FOUND

    registry = er.async_get(hass)

    # Resolve to canonical entity_id via entity registry
    entry = registry.async_get(entity_id)
    if entry:
        entity_id = entry.entity_id

    domain = entity_id.split(".")[0]

    # Ghost check (before pass-through short-circuit so ghosts are never accessible).
    # entry is already the registry lookup result; no second call needed.
    if hass.states.get(entity_id) is None and entry is None:
        return Permission.NOT_FOUND

    # Phoenix MCP blocklist - applies even in pass-through mode
    if domain in BLOCKED_DOMAINS:
        return Permission.NO_ACCESS

    # Block Phoenix MCP's own internal sensor entities regardless of pass_through or permission grants.
    # These live under the sensor domain so BLOCKED_DOMAINS does not catch them.
    if entry is not None and entry.platform == DOMAIN:
        return Permission.NO_ACCESS

    # Pass-through bypasses all entity permission resolution
    if token.pass_through:
        return Permission.WRITE

    permissions = token.permissions
    device_id = entry.device_id if entry else None

    entity_node = permissions.entities.get(entity_id)
    device_node = permissions.devices.get(device_id) if device_id else None
    domain_node = permissions.domains.get(domain)

    # Pass 1: RED check - walk entire ancestor chain before resolving any grant
    for node in (entity_node, device_node, domain_node):
        if node is not None and node.state == "RED":
            return Permission.DENY

    # Pass 2: most specific non-GREY grant
    for node in (entity_node, device_node, domain_node):
        if node is None:
            continue
        if node.state == "GREEN":
            return Permission.WRITE
        if node.state == "YELLOW":
            return Permission.READ

    return Permission.NO_ACCESS


def resolve_registry_access(
    entity_id: str,
    token: TokenRecord,
    hass: HomeAssistant,
    *,
    force_registry_only: bool = False,
) -> Permission:
    """Resolve access to an entity, requiring inherited scope when registry-only.

    A disabled entry, or an entry whose integration currently publishes no
    State, is manageable only through its owning device or domain.  An explicit
    entity grant may still *restrict* that inherited access (RED and YELLOW are
    preserved by the ordinary resolver), but it cannot create positive access
    after the entity has disappeared from the live permission tree.

    Live, enabled entities retain the existing resolve() semantics.  Pass-through
    tokens also retain WRITE after the ordinary blocklist/ghost checks; Assist
    exposure remains the caller's shared enumeration filter.
    """
    ordinary = resolve(entity_id, token, hass)
    if ordinary not in (Permission.READ, Permission.WRITE):
        return ordinary

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    canonical_id = entry.entity_id if entry is not None else entity_id
    if entry is None or (not force_registry_only and (
        entry.disabled_by is None and hass.states.get(canonical_id) is not None
    )):
        return ordinary
    if token.pass_through:
        return ordinary

    permissions = token.permissions
    domain = canonical_id.split(".", 1)[0]
    device_node = permissions.devices.get(entry.device_id) if entry.device_id else None
    domain_node = permissions.domains.get(domain)

    inherited = Permission.NO_ACCESS
    for node in (device_node, domain_node):
        if node is None:
            continue
        if node.state == "GREEN":
            inherited = Permission.WRITE
            break
        if node.state == "YELLOW":
            inherited = Permission.READ
            break

    if inherited == Permission.NO_ACCESS:
        return Permission.NO_ACCESS
    if ordinary == Permission.WRITE and inherited == Permission.WRITE:
        return Permission.WRITE
    return Permission.READ


def device_config_entry_ids(device: Any) -> list[str]:
    """Return a device's owning config-entry ids across supported HA versions."""
    config_entries = getattr(device, "config_entries", None)
    if config_entries is not None:
        return sorted(
            entry_id for entry_id in config_entries if isinstance(entry_id, str)
        )
    config_entry_id = getattr(device, "config_entry_id", None)
    return [config_entry_id] if isinstance(config_entry_id, str) else []


def device_registry_entity_ids(hass: HomeAssistant, device_id: str) -> list[str]:
    """Return every registry entity currently attached to a device."""
    return sorted(
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.device_id == device_id
    )


def _phoenix_owned_device(device: Any, hass: HomeAssistant) -> bool:
    """Whether a device belongs to Phoenix MCP and must remain unreachable."""
    for config_entry_id in device_config_entry_ids(device):
        config_entry = hass.config_entries.async_get_entry(config_entry_id)
        if config_entry is not None and config_entry.domain == DOMAIN:
            return True
    registry = er.async_get(hass)
    return any(
        (entry := registry.async_get(entity_id)) is not None
        and entry.platform == DOMAIN
        for entity_id in device_registry_entity_ids(hass, device.id)
    )


def resolve_device_registry_access(
    device_id: str, token: TokenRecord, hass: HomeAssistant
) -> Permission:
    """Resolve read access to a device-registry entry.

    An explicit device node authorizes the device even when it has no entities.
    Otherwise one attached entity must pass the ordinary registry-aware entity
    resolver. Pass-through tokens retain their Assist-exposure filter: a device
    with no exposed attached entity is not an alternate enumeration path.
    """
    if not isinstance(device_id, str):
        return Permission.NOT_FOUND
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return Permission.NOT_FOUND
    if _phoenix_owned_device(device, hass):
        return Permission.NO_ACCESS

    entity_ids = device_registry_entity_ids(hass, device_id)
    if token.pass_through:
        expose = assist_expose_check(token, hass)
        if expose is not None and not any(
            expose(entity_id)
            and resolve_registry_access(entity_id, token, hass)
            in (Permission.READ, Permission.WRITE)
            for entity_id in entity_ids
        ):
            return Permission.NO_ACCESS
        return Permission.WRITE

    device_node = token.permissions.devices.get(device_id)
    if device_node is not None:
        if device_node.state == "RED":
            return Permission.DENY
        if device_node.state == "GREEN":
            return Permission.WRITE
        if device_node.state == "YELLOW":
            return Permission.READ

    inherited = [
        resolve_registry_access(
            entity_id,
            token,
            hass,
            force_registry_only=device.disabled_by is not None,
        )
        for entity_id in entity_ids
    ]
    if Permission.WRITE in inherited:
        return Permission.WRITE
    if Permission.READ in inherited:
        return Permission.READ
    if Permission.DENY in inherited:
        return Permission.DENY
    return Permission.NO_ACCESS


def resolve_device_registry_write(
    device_id: str, token: TokenRecord, hass: HomeAssistant
) -> Permission:
    """Resolve the explicit device permission required for whole-device writes.

    Attached entities may make a device readable, but they never authorize a
    registry mutation that affects the whole device. Scoped callers therefore
    need GREEN on the exact device node; pass-through remains unrestricted.
    """
    if not isinstance(device_id, str):
        return Permission.NOT_FOUND
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return Permission.NOT_FOUND
    if _phoenix_owned_device(device, hass):
        return Permission.NO_ACCESS
    if token.pass_through:
        return Permission.WRITE
    node = token.permissions.devices.get(device_id)
    if node is None:
        return Permission.NO_ACCESS
    if node.state == "GREEN":
        return Permission.WRITE
    if node.state == "YELLOW":
        return Permission.READ
    if node.state == "RED":
        return Permission.DENY
    return Permission.NO_ACCESS


def config_entry_registry_context(
    entry_id: str, hass: HomeAssistant
) -> ConfigEntryRegistryContext | None:
    """Collect exact entity and device membership for a config entry.

    Devices can have multiple owners.  Membership therefore comes from the
    registry's owner set rather than from an entity sample, while entities use
    their exact config_entry_id.  Phoenix MCP's own entry is deliberately
    indistinguishable from an absent entry to callers.
    """
    if not isinstance(entry_id, str):
        return None
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain == DOMAIN:
        return None
    entity_ids = tuple(sorted(
        item.entity_id
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry_id
    ))
    device_ids = tuple(sorted(
        device.id
        for device in dr.async_get(hass).devices.values()
        if entry_id in device_config_entry_ids(device)
        and not _phoenix_owned_device(device, hass)
    ))
    return ConfigEntryRegistryContext(entry, entity_ids, device_ids)


def resolve_config_entry_registry_access(
    entry_id: str, token: TokenRecord, hass: HomeAssistant
) -> Permission:
    """Resolve read visibility inherited from an entry's owned resources."""
    context = config_entry_registry_context(entry_id, hass)
    if context is None:
        return Permission.NOT_FOUND
    if token.pass_through:
        expose = assist_expose_check(token, hass)
        if expose is None or (not context.entity_ids and not context.device_ids):
            return Permission.WRITE
        if any(
            expose(entity_id)
            and resolve_registry_access(entity_id, token, hass)
            in (Permission.READ, Permission.WRITE)
            for entity_id in context.entity_ids
        ) or any(
            resolve_device_registry_access(device_id, token, hass)
            in (Permission.READ, Permission.WRITE)
            for device_id in context.device_ids
        ):
            return Permission.WRITE
        return Permission.NO_ACCESS

    expose = assist_expose_check(token, hass)
    entity_results = [
        resolve_registry_access(entity_id, token, hass)
        for entity_id in context.entity_ids
        if expose is None or expose(entity_id)
    ]
    device_results = [
        resolve_device_registry_access(device_id, token, hass)
        for device_id in context.device_ids
    ]
    results = entity_results + device_results
    if Permission.WRITE in results:
        return Permission.WRITE
    if Permission.READ in results:
        return Permission.READ
    if Permission.DENY in results:
        return Permission.DENY
    return Permission.NO_ACCESS


def resolve_config_entry_registry_write(
    entry_id: str,
    token: TokenRecord,
    hass: HomeAssistant,
    *,
    force_registry_only: bool = False,
) -> Permission:
    """Require complete entity WRITE and explicit device WRITE coverage.

    A scoped token cannot manage a resource-less entry.  For disabling,
    force_registry_only proves that authorization survives after live states
    disappear instead of relying on a direct entity grant that will self-lock.
    """
    context = config_entry_registry_context(entry_id, hass)
    if context is None:
        return Permission.NOT_FOUND
    if token.pass_through:
        return Permission.WRITE
    if not context.entity_ids and not context.device_ids:
        return Permission.NO_ACCESS

    entity_results = [
        resolve_registry_access(
            entity_id,
            token,
            hass,
            force_registry_only=force_registry_only,
        )
        for entity_id in context.entity_ids
    ]
    device_results = [
        resolve_device_registry_write(device_id, token, hass)
        for device_id in context.device_ids
    ]
    results = entity_results + device_results
    if all(result == Permission.WRITE for result in results):
        return Permission.WRITE
    if Permission.DENY in results:
        return Permission.DENY
    if Permission.READ in results:
        return Permission.READ
    return Permission.NO_ACCESS


def is_sensitive_key(key: Any) -> bool:
    """Whether an attribute/response key name marks its value as sensitive.

    True when the key is in the fixed SENSITIVE_ATTRIBUTES list or contains any
    SENSITIVE_KEY_SUBSTRINGS marker (case-insensitive). Used to drop secrets that
    third-party integrations surface under arbitrary keys.
    """
    if not isinstance(key, str):
        return False
    if key in SENSITIVE_ATTRIBUTES:
        return True
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_SUBSTRINGS)


def scrub_sensitive_attributes(state: State) -> dict[str, Any]:
    """Return a state dict with sensitive attributes removed."""
    d = state.as_dict()
    # HA types as_dict()'s values as a union wide enough to include datetime, so
    # the attributes mapping does not narrow on its own. It is always a mapping.
    attributes: Mapping[str, Any] = d.get("attributes") or {}  # type: ignore[assignment]
    clean_attrs = {k: v for k, v in attributes.items() if not is_sensitive_key(k)}
    return {**d, "attributes": clean_attrs}


def scrub_state_dict(d: dict) -> dict:
    """Return a copy of a raw state dict with sensitive attributes removed.

    Use this when the input is already a dict (e.g. from state history results).
    Use scrub_sensitive_attributes when working with State objects directly.
    """
    attrs = {k: v for k, v in d.get("attributes", {}).items() if not is_sensitive_key(k)}
    return {**d, "attributes": attrs}


def assist_expose_check(token: TokenRecord, hass: HomeAssistant) -> Callable[[str], bool] | None:
    """Return the Assist-exposure predicate for a pass-through token, or None.

    Non-None only for pass_through tokens with use_assist_exposure;
    scoped tokens are filtered by their permission tree alone. This is the one
    shared implementation for every entity-filter path (filter_entities_for_token,
    build_permitted_states/entity_ids, resolve_intent_entities). The rest of the
    filter predicate (Phoenix MCP domain blocklist, Phoenix-platform sensors, pass-through
    WRITE) lives in resolve() itself, so those paths need only this plus resolve().
    """
    if not (token.pass_through and token.use_assist_exposure):
        return None
    from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
        async_should_expose,
    )

    def _exposed(entity_id: str) -> bool:
        return async_should_expose(hass, "conversation", entity_id)

    return _exposed


def filter_entities_for_token(
    entities: list[State],
    token: TokenRecord,
    hass: HomeAssistant,
) -> list[dict[str, Any]]:
    """Filter a list of State objects to those accessible by token, scrub sensitive attributes.

    resolve() is the single filter predicate: it blocks the Phoenix MCP domain and Phoenix MCP's
    own platform entities and grants pass-through tokens WRITE, so scoped and
    pass-through tokens share one path here. The only pass-through-specific
    extra is the use_assist_exposure check. Sensitive attributes are
    always scrubbed.
    """
    expose = assist_expose_check(token, hass)
    return [
        scrub_sensitive_attributes(e)
        for e in entities
        if resolve(e.entity_id, token, hass) in (Permission.READ, Permission.WRITE)
        and (expose is None or expose(e.entity_id))
    ]


def _eligible_for_indirect_expansion(entry: er.RegistryEntry) -> bool:
    """Whether an entity may be reached by INDIRECT service-target expansion.

    Mirrors HA's helpers.target primary_entities_only=True default: config and
    diagnostic entities (entity_category set) and hidden entities are excluded
    when a call targets a device, area, or floor, so a bulk action like "turn
    off every switch in the bedroom" does not fan out to a same-domain config
    entity (e.g. a child-lock switch) the operator never meant to actuate.

    Not applied to explicit entity_id targets or the entity_id:"all"/whole-domain
    paths: HA does not filter those either (an operator naming an entity directly
    means it), so Phoenix MCP keeps them as the deliberate escape hatch.
    """
    return entry.entity_category is None and entry.hidden_by is None


def expand_service_targets(
    *,
    entity_id: str | list[str] | None,
    device_id: str | list[str] | None,
    area_id: str | list[str] | None,
    service_domain: str,
    hass: HomeAssistant,
) -> tuple[set[str], list[str]]:
    """Expand service call targets without permission filtering or entity creation checks.

    Returns (device_area_candidates, explicit_entity_ids) where:
    - device_area_candidates: entity IDs from device/area/'all' expansion (deduplicated set)
    - explicit_entity_ids: entity IDs specified directly (not via 'all') that callers must
      validate against the entity registry before use

    Use resolve_service_targets for the full filtered result. Use this directly only when
    a raw count is needed (e.g. X-Phoenix-Entities-Requested header).
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    candidates: set[str] = set()
    explicit_ids: list[str] = []

    if entity_id is not None:
        if isinstance(entity_id, str):
            ids = [entity_id]
        elif isinstance(entity_id, (list, tuple)):
            # Non-string members are dropped rather than stringified: they
            # cannot name an entity, and inventing one only defers the refusal.
            ids = [e for e in entity_id if isinstance(e, str)]
        else:
            ids = []
        for eid in ids:
            if eid == "all":
                for state in hass.states.async_all():
                    if state.entity_id.split(".")[0] == service_domain:
                        candidates.add(state.entity_id)
            else:
                explicit_ids.append(eid)

    if device_id is not None:
        if isinstance(device_id, str):
            dids = [device_id]
        elif isinstance(device_id, (list, tuple)):
            dids = [d for d in device_id if isinstance(d, str)]
        else:
            dids = []
        for did in dids:
            for entry in entity_registry.entities.values():
                if (
                    entry.device_id == did
                    and entry.domain == service_domain
                    and not entry.disabled_by
                    and _eligible_for_indirect_expansion(entry)
                ):
                    candidates.add(entry.entity_id)

    if area_id is not None:
        if isinstance(area_id, str):
            aids = [area_id]
        elif isinstance(area_id, (list, tuple)):
            aids = [a for a in area_id if isinstance(a, str)]
        else:
            aids = []
        # Build indexes once to avoid O(A*E) and O(D*E) nested iteration.
        device_entity_index: dict[str, list[str]] = {}
        area_entity_index: dict[str, list[str]] = {}
        for entry in entity_registry.entities.values():
            if (
                entry.domain == service_domain
                and not entry.disabled_by
                and _eligible_for_indirect_expansion(entry)
            ):
                if entry.device_id:
                    device_entity_index.setdefault(entry.device_id, []).append(entry.entity_id)
                if entry.area_id:
                    area_entity_index.setdefault(entry.area_id, []).append(entry.entity_id)
        for aid in aids:
            for eid in area_entity_index.get(aid, []):
                candidates.add(eid)
            for device in device_registry.devices.values():
                if device.area_id == aid:
                    for eid in device_entity_index.get(device.id, []):
                        candidates.add(eid)

    # No explicit targets: expand to all domain entities. This mirrors native HA
    # behavior (a service call with no targets affects all entities in the domain).
    # Phoenix MCP still filters through resolve_service_targets before calling HA, so only
    # WRITE-permitted entities are included in the final call.
    if entity_id is None and device_id is None and area_id is None:
        for state in hass.states.async_all():
            if state.entity_id.split(".")[0] == service_domain:
                candidates.add(state.entity_id)

    return candidates, explicit_ids


def resolve_service_targets(
    *,
    entity_id: str | list[str] | None = None,
    device_id: str | list[str] | None = None,
    area_id: str | list[str] | None = None,
    service_domain: str,
    token: TokenRecord,
    hass: HomeAssistant,
) -> tuple[list[str], int]:
    """Resolve service call targets to a WRITE-permitted, deduplicated entity_id list.

    Returns (permitted_entities, raw_count) where raw_count is the number of
    candidate entities before permission filtering. This avoids a second call to
    expand_service_targets just to populate X-Phoenix-Entities-Requested.

    Raises:
        EntityCreationNotPermitted: if an explicit entity_id is not in the entity registry.

    Returns an empty list when no entities pass permission filtering; the caller must
    return 403 in that case.

    Phoenix MCP never passes device_id, area_id, or 'all' through to HA. This function
    always returns an explicit entity list.
    """
    entity_registry = er.async_get(hass)

    candidates, explicit_ids = expand_service_targets(
        entity_id=entity_id,
        device_id=device_id,
        area_id=area_id,
        service_domain=service_domain,
        hass=hass,
    )

    # Entity creation check for explicit entity_ids; raises immediately if any are not in registry
    for eid in explicit_ids:
        if entity_registry.async_get(eid) is None:
            raise EntityCreationNotPermitted(eid)
        candidates.add(eid)

    raw_count = len(candidates)

    # Deduplicate while preserving order, then filter to WRITE-permitted entities
    seen: set[str] = set()
    permitted: list[str] = []
    for eid in candidates:
        if eid in seen:
            continue
        seen.add(eid)
        if resolve(eid, token, hass) == Permission.WRITE:
            permitted.append(eid)

    return permitted, raw_count


def physical_gate_applies(domain: str, service: str, targets: list[str]) -> bool:
    """The physical-gate predicate over an ALREADY-RESOLVED target list.

    Split out of call_needs_physical_gate so the enforcing path and the PREVIEW
    path (dry_run_service) share one definition instead of encoding it twice.
    dry_run_service has already resolved its targets and must not resolve them
    again, but its prediction has to be the same verdict the real call will
    reach; when this lived in two places, a new case added to one would have
    made the preview quietly disagree with reality, which is the single thing a
    preview tool must never do.

    Any service on a physical actuator domain (lock/alarm_control_panel/cover/
    valve) qualifies: every service those domains expose is an actuation, so
    gating the whole domain is safe and future-proof against new services an
    exact service-name list would miss (cover.toggle, the *_cover_tilt family,
    valve.toggle, alarm_arm_custom_bypass). The generic homeassistant.toggle
    redispatches to the target domain's own toggle (cover.toggle/valve.toggle
    exist), so it qualifies when a resolved WRITE-target is physical;
    homeassistant.turn_on/off verifiably no-op on those domains and are excluded.
    HA-version-sensitive: re-verify the redispatch behavior on HA upgrades.
    """
    if domain in PHYSICAL_GATE_DOMAINS:
        return True
    if domain == "homeassistant" and service == "toggle":
        return any(e.split(".")[0] in PHYSICAL_GATE_DOMAINS for e in targets)
    return False


def call_needs_physical_gate(
    *,
    domain: str,
    service: str,
    entity_id: str | list[str] | None = None,
    device_id: str | list[str] | None = None,
    area_id: str | list[str] | None = None,
    token: TokenRecord,
    hass: HomeAssistant,
) -> bool:
    """True if a call_service must gate on cap_physical_control.

    Resolves the target list, then applies physical_gate_applies. The rule
    itself lives there; this is the enforcing path's entry point, which has to
    do the resolution first.
    """
    if domain in PHYSICAL_GATE_DOMAINS:
        return True
    if domain == "homeassistant" and service == "toggle":
        try:
            targets, _ = resolve_service_targets(
                entity_id=entity_id, device_id=device_id, area_id=area_id,
                service_domain=domain, token=token, hass=hass,
            )
        except EntityCreationNotPermitted:
            return False
        return physical_gate_applies(domain, service, targets)
    return False


def filter_service_response(
    response_data: Any,
    token: TokenRecord,
    hass: HomeAssistant,
    _depth: int = 0,
) -> Any:
    """Recursively redact secrets and inaccessible entity IDs from service response data.

    Three redactions are applied: any string that is an entity ID the token cannot
    access becomes "<redacted>", any dict value whose key name is sensitive
    (is_sensitive_key) becomes "<redacted>" without recursing into it, and any dict
    ENTRY whose KEY is an inaccessible entity ID is dropped entirely.

    Keys are dropped rather than replaced because dict keys must be unique: two
    denied keys replaced by the same "<redacted>" string would silently collapse
    into one entry, and a disambiguating suffix would disclose how many entities
    were denied. Dropping also matches the no-oracle convention used elsewhere
    (get_logbook drops out-of-scope entries rather than marking them). Services
    that key their response by entity_id (weather.get_forecasts, calendar
    .get_events) are the reason this matters.
    """
    if _depth > 10:
        # Depth limit reached. Still redact entity ID strings, but truncate
        # containers to empty rather than returning their raw contents - a dict
        # or list at this depth could contain entity IDs at deeper levels that
        # would bypass redaction if returned as-is.
        if isinstance(response_data, str) and _ENTITY_ID_RE.match(response_data):
            perm = resolve(response_data, token, hass)
            if perm in (Permission.NO_ACCESS, Permission.DENY, Permission.NOT_FOUND):
                return "<redacted>"
            return response_data
        if isinstance(response_data, (dict, list)):
            _LOGGER.warning(
                "filter_service_response: depth limit reached, truncating %s to empty",
                type(response_data).__name__,
            )
            return {} if isinstance(response_data, dict) else []
        return response_data
    if isinstance(response_data, str):
        if _ENTITY_ID_RE.match(response_data):
            perm = resolve(response_data, token, hass)
            if perm in (Permission.NO_ACCESS, Permission.DENY, Permission.NOT_FOUND):
                return "<redacted>"
        return response_data
    if isinstance(response_data, dict):
        out: dict = {}
        for k, v in response_data.items():
            if isinstance(k, str) and _ENTITY_ID_RE.match(k):
                perm = resolve(k, token, hass)
                if perm in (Permission.NO_ACCESS, Permission.DENY, Permission.NOT_FOUND):
                    continue
            out[k] = (
                "<redacted>" if is_sensitive_key(k)
                else filter_service_response(v, token, hass, _depth + 1)
            )
        return out
    if isinstance(response_data, list):
        return [filter_service_response(item, token, hass, _depth + 1) for item in response_data]
    return response_data


def get_effective_hint(
    token: TokenRecord,
    entity_id: str,
    hass: HomeAssistant,
    entity_hints: dict[str, str] | None = None,
) -> str | None:
    """Return the most specific hint for an entity.

    Checks the per-token entity, device, then domain nodes. If none is set, falls
    back to the global entity_hints map (entity_id -> hint) when provided. The
    per-token node hint always wins over the global hint.
    """
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    permissions = token.permissions

    entity_node = permissions.entities.get(entity_id)
    if entity_node and entity_node.hint:
        return entity_node.hint

    if entry and entry.device_id:
        device_node = permissions.devices.get(entry.device_id)
        if device_node and device_node.hint:
            return device_node.hint

    domain = entity_id.split(".")[0]
    domain_node = permissions.domains.get(domain)
    if domain_node and domain_node.hint:
        return domain_node.hint

    if entity_hints:
        return entity_hints.get(entity_id)
    return None


def template_blocklist_vars() -> dict:
    """Return a variables dict that shadows HA template globals to block entity enumeration.

    Pass as **template_blocklist_vars() when building the variables dict for
    Template.async_render(). Jinja2 local variables shadow globals of the same name,
    so these stubs override HA's built-in functions that could bypass Phoenix MCP filtering.

    Blocked here:
    - 'secrets' removed entirely (shadowed with None; attribute access raises AttributeError)
    - 'hass' replaced with None (safe subset is impractical to implement; None causes
      AttributeError on any access, which HA's template engine surfaces as a template error)
    - 'config_entry_attr()' blocked (returns None, preventing integration metadata leaks)
    - 'device_attr()' blocked (already present; prevents device metadata enumeration)
    - Enumeration helpers blocked (return empty iterables)
    """
    return {
        # Remove secrets and replace hass with a safe (non-functional) substitute.
        "hass": None,
        "secrets": None,
        # Block config_entry_attr() to prevent integration metadata enumeration.
        "config_entry_attr": lambda *a, **kw: None,
        "integration_entities": lambda *a, **kw: [],
        "area_entities": lambda *a, **kw: [],
        "area_devices": lambda *a, **kw: [],
        "device_entities": lambda *a, **kw: [],
        "expand": lambda *a, **kw: [],
        "label_entities": lambda *a, **kw: [],
        "label_areas": lambda *a, **kw: [],
        "floor_entities": lambda *a, **kw: [],
        "floor_areas": lambda *a, **kw: [],
        "device_attr": lambda *a, **kw: None,
        "device_id": lambda *a, **kw: None,
        "areas": lambda *a, **kw: [],
        "labels": lambda *a, **kw: [],
        "label_id": lambda *a, **kw: None,
        "label_name": lambda *a, **kw: None,
        "floors": lambda *a, **kw: [],
        "floor_id": lambda *a, **kw: None,
        "floor_name": lambda *a, **kw: None,
        "closest": lambda *a, **kw: None,
        "is_device_attr": lambda *a, **kw: False,
        "area_id": lambda *a, **kw: None,
        # State access - can reveal state/attributes of out-of-scope entities
        "state_translated": lambda *a, **kw: None,
        "state_attr_translated": lambda *a, **kw: None,
        "entity_name": lambda *a, **kw: None,
        "distance": lambda *a, **kw: None,
        "is_hidden_entity": lambda *a, **kw: False,
        # Device/area/config metadata enumeration
        "config_entry_id": lambda *a, **kw: None,
        "device_name": lambda *a, **kw: None,
        "area_name": lambda *a, **kw: None,
        "label_description": lambda *a, **kw: None,
        "label_devices": lambda *a, **kw: [],
        # HA issues (could reveal integration problems or installed components)
        "issues": lambda *a, **kw: [],
        "issue": lambda *a, **kw: None,
    }


def parse_relative_time(value: str) -> datetime:
    """Parse a relative time string to a UTC datetime.

    Supported formats: '24h' (hours), '7d' (days), '2w' (weeks), '1m' (30-day months).
    Raises ValueError for unrecognized formats.
    """
    if not isinstance(value, str):
        raise ValueError(f"Unrecognized relative time format: {value!r}")
    match = _RELATIVE_TIME_RE.match(value.strip())
    if not match:
        raise ValueError(f"Unrecognized relative time format: {value!r}")
    n = int(match.group(1))
    unit = match.group(2)
    _MAX_DAYS = 366
    if unit == "h":
        if n > _MAX_DAYS * 24:
            raise ValueError(f"Relative time {value!r} exceeds the maximum allowed range of {_MAX_DAYS} days.")
        delta = timedelta(hours=n)
    elif unit == "d":
        if n > _MAX_DAYS:
            raise ValueError(f"Relative time {value!r} exceeds the maximum allowed range of {_MAX_DAYS} days.")
        delta = timedelta(days=n)
    elif unit == "w":
        if n > 52:
            raise ValueError(f"Relative time {value!r} exceeds the maximum allowed range of 52 weeks.")
        delta = timedelta(weeks=n)
    else:
        if n > 12:
            raise ValueError(f"Relative time {value!r} exceeds the maximum allowed range of 12 months.")
        delta = timedelta(days=30 * n)
    return utcnow() - delta


def _selector_list(value: object) -> list[str] | None:
    """A list-valued intent selector, coerced. None means "not provided"."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        cleaned = [v for v in value if isinstance(v, str)]
        return cleaned or None
    return None


class IntentSelectors(NamedTuple):
    """The five native targeting selectors, coerced to the shapes HA assumes.

    A model does not always follow the tool schema, and HA's async_match_targets
    assumes name/area/floor are plain strings and that domains/device_classes are
    iterable. A wrong-shaped value is degraded to "not provided" rather than
    stringified, for the reason in helpers.str_arg: str(["a"]) invents an
    argument nobody sent, which then has to be refused with a message about a
    value nobody typed.
    """

    name: str | None
    area: str | None
    floor: str | None
    domains: list[str] | None
    device_classes: list[str] | None

    @property
    def none_usable(self) -> bool:
        """True when nothing usable survived coercion.

        A native action with zero constraints must match nothing rather than fan
        out to the token's whole writable scope, so resolve_intent_entities fails
        closed on this. It is exposed as a property because the CALLER needs to
        tell that case apart from "matched nothing" and "denied", which are the
        same empty list: only this one is the caller's own doing and can be
        reported as such without disclosing anything.
        """
        return not (self.name or self.area or self.floor or self.domains or self.device_classes)


def normalize_intent_selectors(
    *,
    name: object = None,
    area: object = None,
    floor: object = None,
    domains: object = None,
    device_classes: object = None,
) -> IntentSelectors:
    """Coerce the five targeting selectors. ONE definition, two callers.

    resolve_intent_entities normalises through this, and the native tools that
    can be left with no selector at all call it first to answer the caller
    properly. A second copy of the coercion in the caller would be a mirror of
    this policy with nothing keeping the two in agreement, which is the pattern
    physical_gate_applies was split out to remove.
    """
    return IntentSelectors(
        name=name if isinstance(name, str) else None,
        area=area if isinstance(area, str) else None,
        floor=floor if isinstance(floor, str) else None,
        domains=_selector_list(domains),
        device_classes=_selector_list(device_classes),
    )


def resolve_intent_entities(
    hass: HomeAssistant,
    token: TokenRecord,
    *,
    domains: list[str] | None = None,
    device_classes: list[str] | None = None,
    name: str | None = None,
    area: str | None = None,
    floor: str | None = None,
) -> list[str]:
    """Resolve intent-based targeting (area/name/floor/domain/device_class) to entity_id list.

    Uses HA's async_match_targets for name/area/floor resolution to match native HA
    intent scoring behavior. Silently drops entities the token cannot WRITE. Never
    acknowledges blocked or inaccessible entities. Returns an empty list when nothing matches.
    """
    from homeassistant.helpers.intent import (  # noqa: PLC0415
        MatchTargetsConstraints,
        async_match_targets,
    )

    # A model does not always follow the tool schema exactly (observed live: a
    # local model targeting two locks by name sent name=["Front Door Lock",
    # "Rear door lock"], a list, instead of a single string). HA's own
    # async_match_targets assumes name/area/floor are plain strings and calls
    # .strip() on them; a list raises an unhandled AttributeError that, before
    # the dispatch-level safety net in mcp_view._dispatch_mcp existed, escaped
    # all the way to the transport and manifested as a multi-minute hang for
    # the MCP client rather than a clean error. The same applies to the LIST
    # selectors, which HA iterates. A wrong-shaped value degrades to "not
    # provided" and falls through to the zero-selector fail-closed check below.
    #
    # The coercion itself lives in normalize_intent_selectors so the native tools
    # can ask whether anything usable was given WITHOUT keeping a second copy of
    # these rules that could drift out of agreement with this one.
    selectors = normalize_intent_selectors(
        name=name, area=area, floor=floor,
        domains=domains, device_classes=device_classes,
    )
    name, area, floor = selectors.name, selectors.area, selectors.floor
    domains, device_classes = selectors.domains, selectors.device_classes

    registry = er.async_get(hass)
    expose = assist_expose_check(token, hass)

    def _intent_eligible(entity_id: str) -> bool:
        # Exclude config/diagnostic and hidden entities from native Hass* intent
        # matching, matching HA's primary_entities_only default. Phoenix MCP passes
        # assistant=None (bypassing Assist exposure), so without this an area or
        # domain match would sweep in same-domain config entities native HA never
        # targets. Applied to name matches too (decision A): an operator who
        # really wants one drives it via call_service with an explicit entity_id,
        # the direct path that stays unfiltered. Unregistered states have no
        # category, so they are kept.
        entry = registry.async_get(entity_id)
        return entry is None or _eligible_for_indirect_expansion(entry)

    permitted: list[State] = [
        s for s in hass.states.async_all()
        if resolve(s.entity_id, token, hass) == Permission.WRITE
        and (expose is None or expose(s.entity_id))
        and _intent_eligible(s.entity_id)
    ]

    if not name and not area and not floor:
        if not domains and not device_classes:
            # No selector at all (no name/area/floor/domain/device_class). A native
            # intent action with zero constraints must match nothing, not fan out to
            # the token's entire writable scope. An unrecognized argument (e.g. a
            # model passing entity_id, which these tools do not accept) leaves every
            # recognized selector empty; without this guard the call would silently
            # actuate every entity the token can write. Fail closed.
            return []
        if domains:
            domain_set = set(domains)
            permitted = [s for s in permitted if s.entity_id.split(".")[0] in domain_set]
        if device_classes:
            dc_set = set(device_classes)
            permitted = [s for s in permitted if s.attributes.get("device_class") in dc_set]
        return [s.entity_id for s in permitted]

    constraints = MatchTargetsConstraints(
        name=name,
        area_name=area,
        floor_name=floor,
        domains=domains,
        device_classes=device_classes,
        assistant=None,
    )
    result = async_match_targets(
        hass,
        constraints,
        states=permitted,
    )
    if not result.is_match:
        return []
    return [s.entity_id for s in result.states]


# ---------------------------------------------------------------------------
# ESPHome user-defined actions
# ---------------------------------------------------------------------------
#
# A device's firmware can declare actions, which HA registers as
# esphome.<device_name_with_underscores>_<action_name>. These are the only
# services the esphome domain has (its services.yaml is deliberately empty
# upstream) and their schema is built from ONLY the arguments the device
# declared, so they take no entity target. That makes them unreachable through
# the ordinary target-flattening path, which would both attach an entity_id the
# schema rejects and resolve to an empty target list.
#
# Authorization is the owning DEVICE's write scope rather than a capability: the
# action is defined by that device's own firmware and can act on nothing else, so
# a token that may already actuate the device's entities is exactly the token
# that may invoke its actions.


def esphome_entry_for_entity(hass: HomeAssistant, entity_id: str) -> Any | None:
    """Return the LOADED ESPHome config entry owning entity_id, or None."""
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None or not reg_entry.config_entry_id:
        return None
    entry = hass.config_entries.async_get_entry(reg_entry.config_entry_id)
    if entry is None or entry.domain != ESPHOME_DOMAIN:
        return None
    return entry if _esphome_entry_loaded(entry) else None


def _esphome_entry_loaded(entry: Any) -> bool:
    """True when the entry is loaded, so reading runtime_data is safe.

    HA deletes runtime_data on unload (object.__delattr__), so this guard is what
    keeps every caller from reading a deleted attribute or, worse, reporting the
    stale runtime a partially-failed setup left behind.
    """
    from homeassistant.config_entries import ConfigEntryState  # noqa: PLC0415

    return entry.state is ConfigEntryState.LOADED


def resolve_esphome_user_service(hass: HomeAssistant, service: str) -> Any | None:
    """Find the LOADED ESPHome config entry that owns a user-defined action.

    Matching keeps the LONGEST device-name prefix that also declares the
    remaining action name, so a device whose name is a prefix of another's
    cannot shadow it.

    Returns None for any service no loaded entry claims, which deliberately
    includes the stale registrations the esphome integration leaves behind in
    hass.services: it does not remove them on disconnect or unload, so trusting
    the service registry would dispatch at a device that is gone.
    """
    best: Any | None = None
    best_len = -1
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        if not _esphome_entry_loaded(entry):
            continue
        runtime = getattr(entry, "runtime_data", None)
        device_info = getattr(runtime, "device_info", None)
        name = getattr(device_info, "name", None)
        if not name:
            continue
        prefix = f"{name.replace('-', '_')}_"
        if not service.startswith(prefix) or len(prefix) <= best_len:
            continue
        declared = {
            getattr(svc, "name", None)
            for svc in (getattr(runtime, "services", None) or {}).values()
        }
        if service[len(prefix):] in declared:
            best, best_len = entry, len(prefix)
    return best


def esphome_entry_writable(hass: HomeAssistant, entry: Any, token: TokenRecord) -> bool:
    """True when the token holds WRITE on at least one of the entry's entities."""
    registry = er.async_get(hass)
    return any(
        resolve(reg_entry.entity_id, token, hass) is Permission.WRITE
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
    )
