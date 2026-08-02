"""Discovery and analysis tools: the read-only half of the tool surface.

Everything here answers "what is there and what would happen", never "change
it". Three groups, sharing one property that is the reason they live together:
each one enumerates, and every enumeration is scoped to the token's permission
tree before it is returned, so a listing never becomes an oracle for entities
the token cannot see.

  - Registry reads: areas, floors, zones, devices, and the entity search /
    overview / describe family.
  - Relationship analysis: which automations, scripts and scenes reference an
    entity (`_references_for_entity`) and what it drives in turn
    (`_forward_references`), both walked out of the stored YAML configs.
  - Preview tools: whatif, compare_state, dry_run_service, validate_config,
    find_available_actions. These run the same gates a real call would and
    report the verdict without actuating, which is why dry_run_service and
    find_available_actions mirror the physical-gate and no-target-service rules
    rather than approximating them.

`_tool_get_capability_summary` deliberately stayed in mcp_view: it reports on
the PUBLISHED TOOL SURFACE through _tool_gate_map, so it belongs with the
registry that owns that surface, not with the tools that read Home Assistant.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import dataclasses
import difflib
import functools
import json
import logging
import math
import os

from homeassistant.components.automation.config import async_validate_config_item as _validate_automation_config
from homeassistant.components.script.config import async_validate_config_item as _validate_script_config
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import parse_datetime, utcnow

from ..const import CAP_ALLOW, CAP_CONFIRM, CAP_DENY, DUAL_GATE_SERVICES, MAX_SEARCH_QUERY_LEN, MESA_MODE_OFF, NO_TARGET_SERVICES, PHYSICAL_GATE_DOMAINS, SEARCH_FUZZY_MATCH_CUTOFF
from ..data import PhoenixData
from ..mesa import async_semantic_moments, build_expand_target, entity_control_mode, evaluate_service_entities
from ..mesa_core.trigger_validator import entities_by_role
from ..mesa_tools import MESA_TOOLS_CAP, authored_restrictions
from ..helpers import effective_cap, parse_time_param as _parse_time_param, redact_diagnostics as _redact_diagnostics, sanitize_service_data as _sanitize_service_data, str_arg, str_list_arg
from .authoring import _AUTOMATION_YAML, _SCENE_CONFIG_PATH, _SCRIPT_CONFIG_PATH, _read_automations_yaml, _read_scenes_yaml, _read_scripts_yaml
from .esphome import _ESPHOME_DOMAIN, _esphome_action_signature, _esphome_actions_for_entity, esphome_availability
from ..tool_common import _resolve_area_id, _tool_error, _tool_success
from ..policy_engine import EntityCreationNotPermitted, Permission, esphome_entry_writable, filter_entities_for_token, physical_gate_applies, resolve, resolve_esphome_user_service, resolve_service_targets, scrub_sensitive_attributes
from ..token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)


def _collect_entity_id_values(node: Any, found: set[str]) -> None:
    """Collect entity_id values from a config subtree.

    Used only for scripts, which mesa-core does not model. Automations go
    through mesa-core's public entities_by_role instead, so the canonical
    HA-format knowledge (singular/plural section keys) stays single-sourced.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, list):
                    found.update(v for v in value if isinstance(v, str))
            else:
                _collect_entity_id_values(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_entity_id_values(item, found)


def _references_for_entity(hass: HomeAssistant, entity_id: str) -> list[dict]:
    """Scope-agnostic reverse index: automations/scripts/scenes referencing entity_id.

    Automations use mesa-core's canonical entities_by_role (by trigger/condition/
    action role), with the expand_target host callback so an automation that
    targets this entity's device, area, floor, or label (device triggers,
    purpose-specific trigger target blocks) is found too, not just direct
    entity_id references. Scripts and scenes are extracted by Phoenix MCP (mesa-core
    does not model them): scripts via entity_id collection, scenes via the
    entity keys under their `entities` mapping. Callers apply their own token
    scoping.
    """
    refs: list[dict] = []
    expand = build_expand_target(hass)

    auto_path = os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
    for cfg in _read_automations_yaml(auto_path):
        if not isinstance(cfg, dict):
            continue
        by_role = entities_by_role(cfg, expand)
        roles = sorted(role for role, ents in by_role.items() if entity_id in ents)
        if roles:
            refs.append({"kind": "automation", "id": str(cfg.get("id", "")), "name": cfg.get("alias"), "roles": roles})

    scripts = _read_scripts_yaml(hass.config.path(_SCRIPT_CONFIG_PATH))
    for script_id, cfg in scripts.items():
        if not isinstance(cfg, dict):
            continue
        found: set[str] = set()
        _collect_entity_id_values(cfg, found)
        if entity_id in found:
            refs.append({"kind": "script", "id": script_id, "name": cfg.get("alias"), "roles": ["sequence"]})

    for scene in _read_scenes_yaml(hass.config.path(_SCENE_CONFIG_PATH)):
        if not isinstance(scene, dict):
            continue
        members = scene.get("entities")
        if isinstance(members, dict) and entity_id in members:
            refs.append({"kind": "scene", "id": str(scene.get("id", "")), "name": scene.get("name"), "roles": ["member"]})

    return refs


def _forward_references(hass: HomeAssistant, token: TokenRecord, entity_id: str) -> list[str]:
    """Entities referenced by entity_id when it is an automation or script.

    Scoped to entities the token can access, so an automation never reveals
    targets outside the token's permission tree. Returns [] for other domains.
    """
    domain = entity_id.split(".")[0]
    found: set[str] = set()
    if domain == "automation":
        entry = er.async_get(hass).async_get(entity_id)
        unique_id = entry.unique_id if entry is not None else None
        if unique_id is not None:
            auto_path = os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
            for cfg in _read_automations_yaml(auto_path):
                if isinstance(cfg, dict) and str(cfg.get("id", "")) == unique_id:
                    # expand_target resolves device/area/floor/label references
                    # too; the scope filter below still gates what is revealed.
                    for ents in entities_by_role(cfg, build_expand_target(hass)).values():
                        found.update(ents)
                    break
    elif domain == "script":
        script_id = entity_id.split(".", 1)[1] if "." in entity_id else ""
        cfg = _read_scripts_yaml(hass.config.path(_SCRIPT_CONFIG_PATH)).get(script_id)
        if isinstance(cfg, dict):
            _collect_entity_id_values(cfg, found)
    return sorted(e for e in found if resolve(e, token, hass) in (Permission.READ, Permission.WRITE))


def _accessible_entity_ids(token: TokenRecord, hass: HomeAssistant) -> set[str]:
    """Return the set of entity IDs the token can read.

    Uses the same scoping/scrubbing path as get_states so registry views never
    reveal entities, areas, or devices outside the token's permission tree.
    """
    accessible = filter_entities_for_token(hass.states.async_all(), token, hass)
    return {e["entity_id"] for e in accessible}


async def _tool_list_areas(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list areas containing at least one accessible entity."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_areas"

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    counts: dict[str, int] = {}
    for eid in _accessible_entity_ids(token, hass):
        area_id = _resolve_area_id(registry.async_get(eid), dev_reg)
        if area_id:
            counts[area_id] = counts.get(area_id, 0) + 1

    areas: list[dict] = []
    for area_id, count in counts.items():
        area = area_reg.async_get_area(area_id)
        if area is None:
            continue
        areas.append({
            "area_id": area.id,
            "name": area.name,
            "floor_id": area.floor_id,
            "aliases": sorted(area.aliases) if area.aliases else [],
            "accessible_entity_count": count,
        })
    areas.sort(key=lambda a: (a["name"] or a["area_id"]).lower())
    return _tool_success(json.dumps({"count": len(areas), "areas": areas}, default=str)), "allowed", "list_areas"


async def _tool_list_floors(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list floors containing at least one accessible entity."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_floors"

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    floor_reg = fr.async_get(hass)
    floor_entity_counts: dict[str, int] = {}
    floor_area_ids: dict[str, set[str]] = {}
    for eid in _accessible_entity_ids(token, hass):
        area_id = _resolve_area_id(registry.async_get(eid), dev_reg)
        if not area_id:
            continue
        area = area_reg.async_get_area(area_id)
        if area is None or not area.floor_id:
            continue
        floor_entity_counts[area.floor_id] = floor_entity_counts.get(area.floor_id, 0) + 1
        floor_area_ids.setdefault(area.floor_id, set()).add(area_id)

    floors: list[dict] = []
    for floor_id, count in floor_entity_counts.items():
        floor = floor_reg.async_get_floor(floor_id)
        if floor is None:
            continue
        floors.append({
            "floor_id": floor.floor_id,
            "name": floor.name,
            "level": floor.level,
            "accessible_area_count": len(floor_area_ids[floor_id]),
            "accessible_entity_count": count,
        })
    floors.sort(key=lambda f: (f["level"] if f["level"] is not None else 0, (f["name"] or f["floor_id"]).lower()))
    return _tool_success(json.dumps({"count": len(floors), "floors": floors}, default=str)), "allowed", "list_floors"


async def _tool_list_zones(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list accessible zone.* entities."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_zones"

    accessible = filter_entities_for_token(hass.states.async_all(), token, hass)
    zones: list[dict] = []
    for e in accessible:
        if not e["entity_id"].startswith("zone."):
            continue
        attrs = e.get("attributes", {})
        zones.append({
            "entity_id": e["entity_id"],
            "name": attrs.get("friendly_name"),
            "latitude": attrs.get("latitude"),
            "longitude": attrs.get("longitude"),
            "radius": attrs.get("radius"),
        })
    zones.sort(key=lambda z: z["entity_id"])
    return _tool_success(json.dumps({"count": len(zones), "zones": zones}, default=str)), "allowed", "list_zones"


async def _tool_list_devices(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list devices with at least one accessible entity."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_devices"

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    counts: dict[str, int] = {}
    for eid in _accessible_entity_ids(token, hass):
        entry = registry.async_get(eid)
        if entry is not None and entry.device_id:
            counts[entry.device_id] = counts.get(entry.device_id, 0) + 1

    devices: list[dict] = []
    for device_id, count in counts.items():
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        devices.append({
            "device_id": device.id,
            "name": device.name_by_user or device.name,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "area_id": device.area_id,
            "accessible_entity_count": count,
        })
    devices.sort(key=lambda d: ((d["name"] or d["device_id"]).lower()))
    return _tool_success(json.dumps({"count": len(devices), "devices": devices}, default=str)), "allowed", "list_devices"


async def _tool_get_device(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: return one device plus its accessible entities.

    Returns not_found for both a nonexistent device and a device with no
    accessible entities, so there is no existence oracle across the device set.
    """
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_device"

    device_id = str_arg(args.get("device_id"))
    if not device_id:
        return _tool_error("Missing required argument: device_id"), "invalid_request", "get_device"

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    device_entities = sorted(
        eid for eid in _accessible_entity_ids(token, hass)
        if (entry := registry.async_get(eid)) is not None and entry.device_id == device_id
    )
    if device is None or not device_entities:
        return _tool_error("Device not found."), "not_found", device_id

    return _tool_success(json.dumps({
        "device_id": device.id,
        "name": device.name_by_user or device.name,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "sw_version": device.sw_version,
        "area_id": device.area_id,
        "entities": device_entities,
    }, default=str)), "allowed", device_id


def _area_name_for_entity(eid: str, registry: Any, dev_reg: Any, area_reg: Any) -> str | None:
    """Return the area NAME for an entity, or None if it has no area."""
    area_id = _resolve_area_id(registry.async_get(eid), dev_reg)
    if not area_id:
        return None
    area = area_reg.async_get_area(area_id)
    return area.name if area is not None else area_id


def _rank_search_matches(matches: list[dict], query: str) -> list[dict]:
    """Token-AND filter + relevance rank for a search_entities query.

    Keeps only rows whose entity_id/friendly_name contains every query token (so
    multi-word queries work, unlike a single substring test), and orders by an
    IDF-weighted score so the most relevant rows lead and survive truncation.
    Rarer query terms weigh more; exact and prefix/word-start matches get a bonus.
    """
    tokens = [t for t in query.split() if t]
    if not tokens:
        return matches
    docs = [f"{m['entity_id']} {(m.get('friendly_name') or '')}".lower() for m in matches]
    n = len(docs) or 1
    df = {t: sum(1 for d in docs if t in d) for t in set(tokens)}
    full = " ".join(tokens)
    scored: list[tuple[float, str, dict]] = []
    for m, doc in zip(matches, docs):
        if any(t not in doc for t in tokens):
            continue
        eid = m["entity_id"].lower()
        obj = eid.split(".")[-1]
        fname = (m.get("friendly_name") or "").lower()
        score = 0.0
        for t in tokens:
            score += math.log(1 + n / (1 + df.get(t, 0)))
            if t == eid or t == fname:
                score += 5.0
            elif obj.startswith(t) or any(w.startswith(t) for w in fname.split()):
                score += 2.0
        if full in (eid, fname, obj):
            score += 10.0
        scored.append((score, m["entity_id"], m))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [m for _, _, m in scored]


def _fuzzy_search_matches(matches: list[dict], query: str) -> list[dict]:
    """Typo-tolerant fallback rank, only run when exact matching found nothing.

    Scores each candidate by the best difflib similarity ratio of the query
    against its entity_id, object_id, or friendly_name, keeps those at or above
    SEARCH_FUZZY_MATCH_CUTOFF, and orders best-first. Because this is reached
    only after _rank_search_matches returns empty, it never alters results for a
    query that already matches exactly (a typo'd "kitchne" still surfaces
    light.kitchen instead of an empty result).
    """
    scored: list[tuple[float, str, dict]] = []
    for m in matches:
        eid = m["entity_id"].lower()
        obj = eid.split(".")[-1]
        fname = (m.get("friendly_name") or "").lower()
        ratio = max(
            difflib.SequenceMatcher(None, query, eid).ratio(),
            difflib.SequenceMatcher(None, query, obj).ratio(),
            difflib.SequenceMatcher(None, query, fname).ratio() if fname else 0.0,
        )
        if ratio >= SEARCH_FUZZY_MATCH_CUTOFF:
            scored.append((ratio, m["entity_id"], m))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [m for _, _, m in scored]


@dataclasses.dataclass(frozen=True)
class _SearchFilters:
    """Normalized search_entities arguments.

    Parsing is separated from filtering because every field here is defensive
    against a model that does not follow the schema: a comma-joined domain
    string, a numeric string where a number was declared, an empty string
    meaning "unset". Keeping that in one place makes the filter loop read as the
    policy it is rather than as argument handling.
    """

    query: str
    domains: set[str] | None
    device_class: Any
    state: Any
    area: str
    want_unavailable: bool
    stale_threshold: float | None
    limit: int


def _parse_search_args(args: dict) -> _SearchFilters:
    """Coerce raw search_entities arguments into normalized filters."""
    # Cap the query before ranking: _rank_search_matches tokenizes on whitespace
    # and the fuzzy fallback runs difflib (O(n*m)) against every accessible
    # entity, so an unbounded query (up to the 1 MiB body) could contain hundreds
    # of thousands of tokens and stall the event loop. A real query is short; the
    # cap only ever clips a pathological one.
    query = str(args.get("query") or args.get("name") or "").strip().lower()[:MAX_SEARCH_QUERY_LEN]

    raw_domains = args.get("domain")
    if isinstance(raw_domains, str):
        # A model that wants several domains sometimes joins them into one
        # comma-separated string instead of an array (live-observed).
        domains = [d.strip() for d in raw_domains.split(",") if d.strip()]
    else:
        domains = str_list_arg(raw_domains)

    stale_threshold: float | None = None
    stale_hours = args.get("stale_hours")
    if stale_hours is not None:
        try:
            stale_threshold = float(stale_hours)
        except (TypeError, ValueError):
            stale_threshold = None

    try:
        limit = int(args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100

    return _SearchFilters(
        query=query,
        domains=set(domains) or None,
        device_class=args.get("device_class") or None,
        state=args.get("state") or None,
        area=str(args.get("area") or "").strip().lower(),
        want_unavailable=bool(args.get("unavailable")),
        stale_threshold=stale_threshold,
        limit=max(1, min(limit, 500)),
    )


def _annotate_search_rows(
    rows: list[dict], registry: Any, data: PhoenixData, token: TokenRecord
) -> None:
    """Add MESA control_mode and entity_category to the returned rows, in place.

    Both run on the CAPPED result page rather than every match, to bound cost.
    control_mode is omitted when autonomous (the default) so it does not bloat
    every row; entity_category is omitted when unset.
    """
    settings = data.store.get_settings()
    if data.mesa is not None and settings.mesa_mode != MESA_MODE_OFF:
        for row in rows:
            mode = entity_control_mode(data.mesa, token, row["entity_id"])
            if mode is not None and mode != "autonomous":
                row["control_mode"] = mode

    # Discovery deliberately keeps config/diagnostic entities VISIBLE (a battery
    # or connectivity question is legitimate), unlike bulk service targeting
    # which excludes them; the tag lets an agent skip noise without losing access.
    for row in rows:
        entry = registry.async_get(row["entity_id"])
        if entry is not None and entry.entity_category is not None:
            row["entity_category"] = entry.entity_category.value


async def _tool_search_entities(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: filter the token's accessible entities by name/domain/area/etc."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "search_entities"

    f = _parse_search_args(args)

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    now = utcnow()

    matches: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        eid = e["entity_id"]
        domain = eid.split(".")[0]
        if f.domains is not None and domain not in f.domains:
            continue
        attrs = e.get("attributes", {})
        fname = attrs.get("friendly_name") or ""
        if f.device_class is not None and attrs.get("device_class") != f.device_class:
            continue
        state_val = e.get("state")
        if f.state is not None and state_val != f.state:
            continue
        if f.want_unavailable and state_val not in ("unavailable", "unknown"):
            continue
        area_name = _area_name_for_entity(eid, registry, dev_reg, area_reg)
        if f.area:
            area_id = _resolve_area_id(registry.async_get(eid), dev_reg)
            if not area_id:
                continue
            if f.area != area_id.lower() and f.area != (area_name or "").lower():
                continue
        if f.stale_threshold is not None:
            # State.as_dict() (scrub_sensitive_attributes' input) serializes
            # last_changed as an ISO string, not a datetime; parse before
            # subtracting or every stale_hours search crashes on every entity.
            last_changed = parse_datetime(e.get("last_changed") or "")
            if last_changed is None or (now - last_changed).total_seconds() < f.stale_threshold * 3600:
                continue
        matches.append({
            "entity_id": eid,
            "state": state_val,
            "friendly_name": fname or None,
            "domain": domain,
            "area": area_name,
            "device_class": attrs.get("device_class"),
        })

    fuzzy_fallback = False
    if f.query:
        ranked = _rank_search_matches(matches, f.query)
        if not ranked:
            # Exact token-AND matching found nothing; try a typo-tolerant pass so
            # a misspelled query returns the closest entities instead of empty.
            # difflib is O(query*name) per candidate, so run it off the event loop:
            # a large-scope token with a long query would otherwise stall HA.
            ranked = await hass.async_add_executor_job(_fuzzy_search_matches, matches, f.query)
            fuzzy_fallback = bool(ranked)
        matches = ranked

    truncated = len(matches) > f.limit
    results = matches[:f.limit]

    _annotate_search_rows(results, registry, data, token)

    body = {"count": len(results), "truncated": truncated, "entities": results}
    if fuzzy_fallback:
        # Signal that these are approximate (typo-tolerant) matches, not exact,
        # so the agent knows the query did not match any entity literally.
        body["fuzzy_fallback"] = True
    return _tool_success(json.dumps(body, default=str)), "allowed", "search_entities"


async def _tool_get_overview(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: compact home summary scoped to the token."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_overview"

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    accessible = filter_entities_for_token(hass.states.async_all(), token, hass)
    by_domain: dict[str, int] = {}
    by_area: dict[str, int] = {}
    unavailable = 0
    for e in accessible:
        eid = e["entity_id"]
        by_domain[eid.split(".")[0]] = by_domain.get(eid.split(".")[0], 0) + 1
        if e.get("state") in ("unavailable", "unknown"):
            unavailable += 1
        area_name = _area_name_for_entity(eid, registry, dev_reg, area_reg) or "(no area)"
        by_area[area_name] = by_area.get(area_name, 0) + 1

    body = {
        "total_accessible_entities": len(accessible),
        "unavailable_count": unavailable,
        "by_domain": dict(sorted(by_domain.items())),
        "by_area": dict(sorted(by_area.items())),
    }
    # Deployment-wide MESA posture: a cheap one-field orientation signal so the
    # agent knows whether to expect confirm/read-only gates (off | advisory |
    # enforced), plus the operator-authored prohibited/read-only entities the agent
    # should avoid. The rollup lists ONLY admin-authored profiles (never baseline-
    # derived modes), so it reflects operator intent, and it folds a safety-audit
    # ("what's off-limits?") into this one call instead of paginated mesa_* lookups.
    # Gated on cap_config_read (the profile-data cap) and only when MESA governs.
    if data.mesa is not None:
        mesa_mode = data.store.get_settings().mesa_mode
        body["mesa_mode"] = mesa_mode
        if mesa_mode != MESA_MODE_OFF and effective_cap(token, MESA_TOOLS_CAP) != CAP_DENY:
            body["mesa_restrictions"] = authored_restrictions(data.mesa, token, hass)
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_overview"


async def _tool_describe_area(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: describe one area and its accessible entities.

    Returns not_found for both a nonexistent area and an area with no accessible
    entities, so there is no existence oracle across the area set.
    """
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "describe_area"

    area_query = str(args.get("area") or args.get("area_id") or "").strip()
    if not area_query:
        return _tool_error("Missing required argument: area"), "invalid_request", "describe_area"

    area_reg = ar.async_get(hass)
    target = area_reg.async_get_area(area_query)
    if target is None:
        ql = area_query.lower()
        for a in area_reg.async_list_areas():
            aliases = {al.lower() for al in (a.aliases or [])}
            if (a.name or "").lower() == ql or ql in aliases:
                target = a
                break

    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entities_by_domain: dict[str, list[dict]] = {}
    count = 0
    if target is not None:
        for e in filter_entities_for_token(hass.states.async_all(), token, hass):
            eid = e["entity_id"]
            if _resolve_area_id(registry.async_get(eid), dev_reg) != target.id:
                continue
            entities_by_domain.setdefault(eid.split(".")[0], []).append({
                "entity_id": eid,
                "state": e.get("state"),
                "friendly_name": e.get("attributes", {}).get("friendly_name"),
            })
            count += 1

    if target is None or count == 0:
        return _tool_error("Area not found."), "not_found", area_query

    floor_name = None
    if target.floor_id:
        floor = fr.async_get(hass).async_get_floor(target.floor_id)
        floor_name = floor.name if floor is not None else None

    body = {
        "area_id": target.id,
        "name": target.name,
        "floor_id": target.floor_id,
        "floor_name": floor_name,
        "accessible_entity_count": count,
        "entities_by_domain": entities_by_domain,
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", target.id


async def _tool_find_available_actions(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: which services in an entity's domain this token can invoke now.

    Availability reflects Phoenix MCP scope (write access + physical/dual gate caps).
    MESA still enforces per-entity nature at call time; the entity's control_mode
    is surfaced as an advisory hint when MESA is active.
    """
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "find_available_actions"

    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "find_available_actions"

    perm = resolve(entity_id, token, hass)
    if perm not in (Permission.READ, Permission.WRITE):
        # nonexistent and inaccessible both look identical (no oracle).
        return _tool_error("Entity not found."), "not_found", entity_id

    domain = entity_id.split(".")[0]
    writable = token.pass_through or perm == Permission.WRITE
    physical_ok = effective_cap(token, "cap_physical_control") != CAP_DENY
    restart_ok = effective_cap(token, "cap_restart") != CAP_DENY
    yaml_edit_ok = effective_cap(token, "cap_yaml_edit") != CAP_DENY

    actions: list[dict] = []
    for svc in sorted(hass.services.async_services().get(domain, {}).keys()):
        key = f"{domain}/{svc}"
        available = writable
        reason: str | None = None
        # No-target reload services gate on cap_yaml_edit, not the entity's WRITE
        # permission, so they are annotated before the writable-based branches.
        if key in NO_TARGET_SERVICES:
            available = yaml_edit_ok
            reason = None if yaml_edit_ok else "requires yaml edit capability"
        elif not writable:
            reason = "read-only access to this entity"
        elif domain in PHYSICAL_GATE_DOMAINS and not physical_ok:
            available, reason = False, "requires physical control capability"
        elif key in DUAL_GATE_SERVICES and not restart_ok:
            available, reason = False, "requires restart capability"
        entry = {"service": f"{domain}.{svc}", "available": available}
        if reason:
            entry["reason"] = reason
        actions.append(entry)

    # An ESPHome device's user-defined actions live in the esphome domain, not the
    # entity's own, so the loop above can never surface them: without this, the
    # actions a device explicitly published are invisible on every entity it owns.
    # They are authorized by the device's write scope, matching _execute_call_service.
    actions.extend(_esphome_actions_for_entity(hass, token, entity_id, writable))

    body: dict = {
        "entity_id": entity_id,
        "domain": domain,
        "writable": writable,
        "actions": actions,
    }

    # Purpose-built automation triggers/conditions for this entity, with worked
    # config examples. HA-level authoring data (independent of MESA), so it is
    # exposed here for ANY READ-accessible entity, not only profiled ones: this is
    # the authoring-discovery path, distinct from mesa_get_profile's MESA-context
    # path (which stays profile-gated). Best-effort: any HA failure omits the key.
    moments = await async_semantic_moments(hass, entity_id)
    if moments:
        body["semantic_moments"] = moments

    settings = data.store.get_settings()
    if data.mesa is not None and settings.mesa_mode != MESA_MODE_OFF:
        control_mode = entity_control_mode(data.mesa, token, entity_id)
        if control_mode is not None:
            body["mesa_control_mode"] = control_mode
            body["mesa_note"] = (
                "MESA enforces this entity's nature at call time; "
                "read_only and prohibited block writes, confirm may require admin approval."
            )

    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


async def _tool_get_system_health(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: HA version + per-integration system health info."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_system_health"

    from homeassistant.const import __version__ as ha_version  # noqa: PLC0415
    integrations: dict = {}
    try:
        from homeassistant.components import system_health  # noqa: PLC0415
        integrations = await system_health.get_info(hass)
    except Exception:  # noqa: BLE001 - system_health may be unavailable
        integrations = {}

    # Per-integration health values are arbitrary and can carry embedded tokens,
    # credentials, URL-embedded secrets, AND network topology (LAN IPs, hostnames in
    # URLs, filesystem paths). The keys are integration-defined, so a
    # build_safe_config-style allowlist does not apply; redact_diagnostics scrubs
    # secret-keyed and credential values (redact_structure) and additionally the
    # topology that Phoenix MCP already withholds from agents elsewhere (get_config), while
    # preserving the diagnostic shape.
    body = {
        "home_assistant_version": ha_version,
        "integrations": _redact_diagnostics(integrations),
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_system_health"


# ---------------------------------------------------------------------------
# ESPHome status (cap_diagnostics)
# ---------------------------------------------------------------------------










def _requires_satisfied(tool_def: dict, hass: HomeAssistant) -> bool:
    """Whether a tool's "requires" precondition holds on this system.

    Only computed for the handful of defs that carry the key, so tools/list does
    not pay a dashboard lookup per tool.
    """
    requires = tool_def.get("requires")
    if requires is None:
        return True
    avail = esphome_availability(hass)
    if requires == "esphome_builder":
        return avail.builder
    return avail.integration or avail.builder


def _requires_unavailable_reason(tool_def: dict) -> str:
    """Explain, for get_capability_summary, why a required surface is missing."""
    if tool_def.get("requires") == "esphome_builder":
        return "The ESPHome Device Builder add-on is not available."
    return "The ESPHome integration is not set up."
















async def _tool_check_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: validate HA config files and return errors/warnings."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "check_config"

    from homeassistant.helpers import check_config  # noqa: PLC0415
    try:
        result = await check_config.async_check_ha_config_file(hass)
    except Exception:  # noqa: BLE001 - surface as a tool error, never 500
        _LOGGER.warning("MCP check_config failed", exc_info=True)
        return _tool_error("Config check failed."), "invalid_request", "check_config"

    errors = [{"message": e.message, "domain": e.domain} for e in result.errors]
    warnings = [{"message": e.message, "domain": e.domain} for e in result.warnings]
    body = {"valid": not errors, "errors": errors, "warnings": warnings}
    return _tool_success(json.dumps(body, default=str)), "allowed", "check_config"


async def _tool_get_relationships(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: reverse and forward references for an accessible entity."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_relationships"

    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "get_relationships"
    if resolve(entity_id, token, hass) not in (Permission.READ, Permission.WRITE):
        return _tool_error("Entity not found."), "not_found", entity_id

    body = {
        "entity_id": entity_id,
        "referenced_by": await hass.async_add_executor_job(_references_for_entity, hass, entity_id),
        "references": await hass.async_add_executor_job(_forward_references, hass, token, entity_id),
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


async def _tool_describe_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: comprehensive summary of one accessible entity."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "describe_entity"

    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "describe_entity"

    perm = resolve(entity_id, token, hass)
    if perm not in (Permission.READ, Permission.WRITE):
        return _tool_error("Entity not found."), "not_found", entity_id
    state = hass.states.get(entity_id)
    if state is None:
        return _tool_error("Entity not found."), "not_found", entity_id

    domain = entity_id.split(".")[0]
    scrubbed = scrub_sensitive_attributes(state)
    body: dict = {
        "entity_id": entity_id,
        "domain": domain,
        "state": scrubbed.get("state"),
        "attributes": scrubbed.get("attributes"),
        "area": _area_name_for_entity(entity_id, er.async_get(hass), dr.async_get(hass), ar.async_get(hass)),
        "writable": token.pass_through or perm == Permission.WRITE,
        "domain_services": sorted(hass.services.async_services().get(domain, {}).keys()),
        "referenced_by": await hass.async_add_executor_job(_references_for_entity, hass, entity_id),
    }

    # entity_category (config/diagnostic) when set: tells the agent this is a
    # setup/health entity, not a primary control. Bulk device/area service calls
    # exclude these; describe_entity keeps them visible and just labels them.
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.entity_category is not None:
        body["entity_category"] = entry.entity_category.value

    settings = data.store.get_settings()
    if data.mesa is not None and settings.mesa_mode != MESA_MODE_OFF:
        control_mode = entity_control_mode(data.mesa, token, entity_id)
        if control_mode is not None:
            body["mesa_control_mode"] = control_mode
            body["mesa_note"] = (
                "Call mesa_get_profile (requires cap_config_read) for the full semantic profile."
            )

    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


async def _tool_get_audit_summary(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: the token's own recent audit entries. No cap required; own data only."""
    try:
        limit = int(args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    outcome = args.get("outcome")
    entries = data.audit.query(token_id=token.id, outcome=outcome, limit=limit)
    if entries is None:
        return _tool_error("Invalid outcome filter."), "invalid_request", "get_audit_summary"

    items = []
    for e in entries:
        item = {
            "request_id": e.request_id,
            "timestamp": e.timestamp,
            "method": e.method,
            "resource": e.resource,
            "outcome": e.outcome,
        }
        if e.mesa_advisory:
            item["mesa_advisory"] = True
        items.append(item)
    body = {"token_name": token.name, "count": len(items), "entries": items}
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_audit_summary"


# ---------------------------------------------------------------------------
# Analysis tools (whatif / compare_state / recent_activity / dry_run / validate)
# ---------------------------------------------------------------------------


def _state_matches(constraint: Any, value: Any) -> bool | None:
    """True/False if value matches a state-trigger constraint, None if no constraint."""
    if constraint is None:
        return None
    if isinstance(constraint, list):
        return value in [str(c) for c in constraint]
    return str(constraint) == value


def _whatif_trigger(trig: dict, entity_id: str, current_state: str | None, hypothetical: str) -> bool | str:
    """Best-effort: would this trigger fire if entity_id became `hypothetical`?

    Evaluates state and numeric_state platforms; returns "unknown" for triggers
    that cannot be judged from a single state change (template, time, event, etc.).
    """
    platform = trig.get("platform") or trig.get("trigger")
    if platform == "state":
        if _state_matches(trig.get("from"), current_state) is False:
            return False
        if _state_matches(trig.get("not_from"), current_state) is True:
            return False
        to_match = _state_matches(trig.get("to"), hypothetical)
        if to_match is False:
            return False
        if _state_matches(trig.get("not_to"), hypothetical) is True:
            return False
        if to_match is True:
            return True
        if trig.get("to") is None and trig.get("not_to") is None:
            return hypothetical != current_state  # "any change" trigger
        return True
    if platform == "numeric_state":
        try:
            val = float(hypothetical)
        except (TypeError, ValueError):
            return "unknown"
        try:
            if trig.get("above") is not None and not val > float(trig["above"]):
                return False
            if trig.get("below") is not None and not val < float(trig["below"]):
                return False
        except (TypeError, ValueError):
            return "unknown"
        return True
    return "unknown"


async def _tool_whatif(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: which automations would fire if an entity changed to a hypothetical state."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "whatif"

    entity_id = str_arg(args.get("entity_id"))
    hypothetical = args.get("hypothetical_state")
    if not entity_id or hypothetical is None:
        return _tool_error("Missing required arguments: entity_id and hypothetical_state"), "invalid_request", "whatif"
    hypothetical = str(hypothetical)
    if resolve(entity_id, token, hass) not in (Permission.READ, Permission.WRITE):
        return _tool_error("Entity not found."), "not_found", entity_id

    current = hass.states.get(entity_id)
    current_state = current.state if current is not None else None

    # Read + parse automations.yaml off the event loop (the reverse-reference
    # tools already wrap this same read in the executor; whatif is the one path
    # that had not).
    automations = await hass.async_add_executor_job(
        _read_automations_yaml, os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
    )

    candidates: list[dict] = []
    expand = build_expand_target(hass)
    for cfg in automations:
        if not isinstance(cfg, dict):
            continue
        triggers = cfg.get("trigger") or cfg.get("triggers") or []
        if isinstance(triggers, dict):
            triggers = [triggers]
        matched: list[dict] = []
        for trig in triggers:
            if not isinstance(trig, dict):
                continue
            tents: set[str] = set()
            _collect_entity_id_values(trig, tents)
            if entity_id not in tents:
                # A device/area/floor/label reference (device triggers, target
                # blocks) can still cover this entity: reuse the canonical walk
                # on a one-trigger config with the expand_target callback. Such
                # coverage cannot be simulated (device trigger types are
                # integration-specific), so it reports would_fire "unknown"
                # instead of the automation being invisible entirely.
                covered = entities_by_role({"trigger": [trig]}, expand)["trigger"]
                if entity_id not in covered:
                    continue
                matched.append({
                    "platform": trig.get("platform") or trig.get("trigger"),
                    "would_fire": "unknown",
                })
                continue
            matched.append({
                "platform": trig.get("platform") or trig.get("trigger"),
                "would_fire": _whatif_trigger(trig, entity_id, current_state, hypothetical),
            })
        if not matched:
            continue
        if any(m["would_fire"] is True for m in matched):
            verdict: bool | str = True
        elif any(m["would_fire"] == "unknown" for m in matched):
            verdict = "unknown"
        else:
            verdict = False
        candidates.append({
            "automation_id": str(cfg.get("id", "")),
            "name": cfg.get("alias"),
            "would_fire": verdict,
            "triggers": matched,
        })

    body = {
        "entity_id": entity_id,
        "current_state": current_state,
        "hypothetical_state": hypothetical,
        "candidates": candidates,
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


async def _history_states(hass: HomeAssistant, start: Any, end: Any, entity_ids: list[str], *, include_start: bool = True) -> dict:
    """Recorder significant-states for a set of entities, no attributes."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder import history as rec_history  # noqa: PLC0415
    fn = functools.partial(
        rec_history.get_significant_states,
        hass, start, end, entity_ids, None, include_start, True, False, True,
    )
    return await get_instance(hass).async_add_executor_job(fn)


async def _tool_compare_state(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: compare accessible entity states between two times."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "compare_state"

    ids = str_list_arg(args.get("entity_id"))
    if not ids:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "compare_state"
    if not args.get("t1"):
        return _tool_error("Missing required argument: t1"), "invalid_request", "compare_state"
    try:
        t1 = _parse_time_param(args["t1"])
    except ValueError:
        return _tool_error("Invalid t1 format."), "invalid_request", "compare_state"
    t2 = utcnow()
    if args.get("t2"):
        try:
            t2 = _parse_time_param(args["t2"])
        except ValueError:
            return _tool_error("Invalid t2 format."), "invalid_request", "compare_state"

    accessible = [e for e in ids if resolve(e, token, hass) in (Permission.READ, Permission.WRITE)]
    comparisons: list[dict] = []
    if accessible:
        try:
            result = await _history_states(hass, t1, t2, accessible)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("compare_state history failed", exc_info=True)
            return _tool_error("History call failed."), "invalid_request", "compare_state"
        for eid in accessible:
            dicts = [s.as_dict() if hasattr(s, "as_dict") else s for s in result.get(eid, [])]
            s1 = dicts[0].get("state") if dicts else None
            s2 = dicts[-1].get("state") if dicts else None
            comparisons.append({"entity_id": eid, "state_at_t1": s1, "state_at_t2": s2, "changed": s1 != s2})

    body = {"t1": t1, "t2": t2, "comparisons": comparisons}
    return _tool_success(json.dumps(body, default=str)), "allowed", "compare_state"


async def _tool_recent_activity(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: which accessible entities changed in the last N minutes."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "recent_activity"

    try:
        minutes = int(args.get("minutes", 30))
    except (TypeError, ValueError):
        minutes = 30
    minutes = max(1, min(minutes, 1440))
    try:
        limit = int(args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    end = utcnow()
    start = end - timedelta(minutes=minutes)
    accessible_ids = list(_accessible_entity_ids(token, hass))
    changes: list[dict] = []
    if accessible_ids:
        try:
            result = await _history_states(hass, start, end, accessible_ids, include_start=False)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("recent_activity history failed", exc_info=True)
            return _tool_error("History call failed."), "invalid_request", "recent_activity"
        for eid, states in result.items():
            dicts = [s.as_dict() if hasattr(s, "as_dict") else s for s in states]
            if not dicts:
                continue
            last = dicts[-1]
            changes.append({
                "entity_id": eid,
                "state": last.get("state"),
                "when": last.get("last_changed") or last.get("last_updated"),
                "changes_in_window": len(dicts),
            })
    changes.sort(key=lambda c: str(c["when"] or ""), reverse=True)
    body = {
        "window_minutes": minutes,
        "count": min(len(changes), limit),
        "truncated": len(changes) > limit,
        "changes": changes[:limit],
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "recent_activity"


def _cap_outcome(mode: str) -> str:
    """Map an effective cap mode to a predicted call outcome string."""
    if mode == CAP_DENY:
        return "denied"
    if mode == CAP_CONFIRM:
        return "pending_approval"
    return "allowed"


async def _tool_dry_run_service(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: preview a service call (resolved targets + MESA verdict) without executing."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "dry_run_service"

    domain = str_arg(args.get("domain"))
    service = str_arg(args.get("service"))
    if not domain or not service:
        return _tool_error("Missing required arguments: domain and service"), "invalid_request", "dry_run_service"
    # Sanitised exactly as the real call sanitises it, so the preview predicts
    # the call that would actually run rather than the one the caller wrote.
    service_data = _sanitize_service_data(args.get("service_data"))
    service_key = f"{domain}/{service}"

    if service_key in DUAL_GATE_SERVICES:
        system_predicted = _cap_outcome(effective_cap(token, "cap_restart"))
        body = {
            "domain": domain, "service": service, "system_service": True,
            "resolved_entities": [],
            "predicted_outcome": system_predicted,
            "would_execute": system_predicted == "allowed",
        }
        return _tool_success(json.dumps(body, default=str)), "allowed", "dry_run_service"

    if service_key in NO_TARGET_SERVICES:
        no_target_predicted = _cap_outcome(effective_cap(token, "cap_yaml_edit"))
        body = {
            "domain": domain, "service": service, "system_service": True,
            "resolved_entities": [],
            "predicted_outcome": no_target_predicted,
            "would_execute": no_target_predicted == "allowed",
        }
        return _tool_success(json.dumps(body, default=str)), "allowed", "dry_run_service"

    # ESPHome user-defined actions: no entity target, authorized by the owning
    # device's write scope. Reporting the declared argument signature is the
    # point of a dry run here, since the schema is device-defined and a wrong
    # argument name is the likeliest failure.
    if domain == _ESPHOME_DOMAIN:
        esphome_entry = resolve_esphome_user_service(hass, service)
        if esphome_entry is not None:
            allowed = esphome_entry_writable(hass, esphome_entry, token)
            signature = _esphome_action_signature(esphome_entry, service)
            declared = {a["name"] for a in signature}
            body = {
                "domain": domain, "service": service, "device_action": True,
                "resolved_entities": [],
                "declared_args": signature,
                "unknown_args": sorted(k for k in service_data if k not in declared),
                "missing_args": sorted(declared - set(service_data)),
                "predicted_outcome": "allowed" if allowed else "denied",
                "would_execute": allowed,
            }
            return _tool_success(json.dumps(body, default=str)), "allowed", "dry_run_service"

    try:
        permitted, requested = resolve_service_targets(
            entity_id=args.get("entity_id"), device_id=args.get("device_id"),
            area_id=args.get("area_id"), service_domain=domain, token=token, hass=hass,
        )
    except EntityCreationNotPermitted:
        permitted, requested = [], 0

    # Predict the outcome in the same order call_service applies its gates: the
    # physical-control cap gate runs first (before target resolution and MESA),
    # then empty resolution denies, then MESA (confirm -> pending, nothing
    # allowed -> deny, else allow). A confirm at any layer surfaces as pending.
    # The same predicate the enforcing path uses, not a copy of it: a preview
    # that encodes the rule separately can drift from the call it predicts.
    physical_gate = physical_gate_applies(domain, service, permitted)
    predicted: str
    if physical_gate and effective_cap(token, "cap_physical_control") != CAP_ALLOW:
        predicted = _cap_outcome(effective_cap(token, "cap_physical_control"))
    elif not permitted:
        predicted = "denied"
    else:
        predicted = "allowed"

    mesa: dict | None = None
    settings = data.store.get_settings()
    if data.mesa is not None and settings.mesa_mode != MESA_MODE_OFF and permitted:
        verdict = evaluate_service_entities(
            data.mesa, settings.mesa_mode, token, permitted,
            domain=domain, service=service, service_data=service_data, session_id="dry_run",
        )
        mesa = {
            "allowed": verdict.allowed,
            "confirm": verdict.confirm,
            "blocked": [{"entity_id": e, "rule": r, "reason": reason} for e, r, reason in verdict.blocked],
            "warnings": verdict.warnings,
        }
        # MESA only narrows the outcome, and only when the cap gate did not
        # already deny/pend (mirrors call_service: the cap gate returns first).
        if predicted == "allowed":
            if verdict.confirm:
                predicted = "pending_approval"
            elif not verdict.allowed:
                predicted = "denied"

    body = {
        "domain": domain,
        "service": service,
        "requested_target_count": requested,
        "resolved_entities": permitted,
        "dropped_count": max(requested - len(permitted), 0),
        "mesa": mesa,
        "physical_gate": physical_gate,
        "predicted_outcome": predicted,
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "dry_run_service"


async def _tool_validate_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: validate an automation or script config without saving it."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "validate_config"

    cfg_type = args.get("type")
    config = args.get("config")
    if cfg_type not in ("automation", "script") or not isinstance(config, dict):
        return _tool_error("Provide type ('automation' or 'script') and a config object."), "invalid_request", "validate_config"

    valid = True
    errors: list[str] = []
    referenced: set[str] = set()
    try:
        if cfg_type == "automation":
            result = await _validate_automation_config(hass, "phoenix_mcp_validate", config)
            # Indirect targets (device/area/floor/label selectors) expand to
            # their entities so the accessibility report below covers them;
            # the no-oracle collapse already handles out-of-scope ids.
            for ents in entities_by_role(config, build_expand_target(hass)).values():
                referenced.update(ents)
        else:
            script_result = await _validate_script_config(hass, "phoenix_mcp_validate", config)
            result = script_result  # type: ignore[assignment]  # one local, two validator result types
            _collect_entity_id_values(config, referenced)
        if result is None:
            valid = False
            errors.append("Config failed schema validation.")
    except Exception as exc:  # noqa: BLE001 - HA validators raise various types
        valid = False
        errors.append(str(exc))

    # Never reveal existence for entities the token cannot access: a hidden real
    # entity must be indistinguishable from a typo (no existence oracle). Since
    # resolve() ghost-checks, READ/WRITE already implies the entity exists, so
    # exists is reported as the accessible flag and collapses for everything else.
    refs = []
    for eid in sorted(referenced):
        accessible = resolve(eid, token, hass) in (Permission.READ, Permission.WRITE)
        refs.append({"entity_id": eid, "exists": accessible, "accessible": accessible})
    body = {"type": cfg_type, "valid": valid, "errors": errors, "referenced_entities": refs}
    return _tool_success(json.dumps(body, default=str)), "allowed", "validate_config"
