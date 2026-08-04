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

from ..const import CAP_ALLOW, CAP_CONFIRM, CAP_DENY, DUAL_GATE_SERVICES, MAX_COMPARE_LIST_VALUES, MAX_COMPARE_VALUE_CHARS, MAX_DANGLING_PATHS, MAX_SEARCH_QUERY_LEN, MESA_MODE_OFF, NO_TARGET_SERVICES, PHYSICAL_GATE_DOMAINS, SEARCH_FUZZY_MATCH_CUTOFF
from ..data import PhoenixData
from ..mesa import async_semantic_moments, build_expand_target, entity_control_mode, evaluate_service_entities
from ..mesa_core.trigger_validator import entities_by_role
from ..mesa_tools import MESA_TOOLS_CAP, authored_restrictions
from ..helpers import effective_cap, parse_time_param as _parse_time_param, redact_diagnostics as _redact_diagnostics, sanitize_service_data as _sanitize_service_data, str_arg, str_list_arg
from .authoring import _AUTOMATION_YAML, _SCRIPT_CONFIG_PATH, _read_automations_yaml, _read_scripts_yaml
from .esphome import _ESPHOME_DOMAIN, _esphome_action_signature, _esphome_actions_for_entity, esphome_availability
from ..tool_common import _resolve_area_id, _tool_error, _tool_success
from ..ws_dispatch import WsDispatchError, async_get_lovelace_config, async_ws_command
from ..policy_engine import _ENTITY_ID_RE, EntityCreationNotPermitted, Permission, esphome_entry_writable, filter_entities_for_token, physical_gate_applies, resolve, resolve_esphome_user_service, resolve_service_targets, scrub_sensitive_attributes
from ..token_store import TokenRecord
from .. import yaml_includes

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


def _domain_entries(hass: HomeAssistant, domain: str) -> tuple[Any, list[dict]]:
    """Every entry a domain defines, plus the branches that could not be read.

    NOT a plain read of automations.yaml and its siblings, which is what both
    reverse-reference walks used to do: an installation defining a domain inline
    in configuration.yaml, or routing it through !include_dir_list, read as
    EMPTY, and the caller reported that nothing referenced the entity. Nothing
    distinguished that from a genuinely unused entity, which is the worst answer
    this surface can give. Anything still unreadable (a package tree, a broken
    leaf) is returned so a caller can say so rather than quietly omitting it.
    """
    found = yaml_includes.read_all_entries(hass.config.config_dir, domain)
    return found.entries, [{"kind": domain, "reason": r} for r in found.unreadable]


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

    for cfg in _domain_entries(hass, "automation")[0]:
        if not isinstance(cfg, dict):
            continue
        by_role = entities_by_role(cfg, expand)
        roles = sorted(role for role, ents in by_role.items() if entity_id in ents)
        if roles:
            refs.append({"kind": "automation", "id": str(cfg.get("id", "")), "name": cfg.get("alias"), "roles": roles})

    for script_id, cfg in _domain_entries(hass, "script")[0].items():
        if not isinstance(cfg, dict):
            continue
        found: set[str] = set()
        _collect_entity_id_values(cfg, found)
        if entity_id in found:
            refs.append({"kind": "script", "id": script_id, "name": cfg.get("alias"), "roles": ["sequence"]})

    for scene in _domain_entries(hass, "scene")[0]:
        if not isinstance(scene, dict):
            continue
        members = scene.get("entities")
        if isinstance(members, dict) and entity_id in members:
            refs.append({"kind": "scene", "id": str(scene.get("id", "")), "name": scene.get("name"), "roles": ["member"]})

    return refs


# ---------------------------------------------------------------------------
# Relationship scanning (get_relationships)
# ---------------------------------------------------------------------------


def _resolve_area(hass: HomeAssistant, query: str) -> Any | None:
    """An area by id, then by name, then by alias. One definition, two callers.

    describe_area and get_relationships both take an operator-typed area, and an
    agent that reaches one with a name expects the other to accept the same
    string. Two copies of the fallback order is how that stops being true.
    """
    area_reg = ar.async_get(hass)
    target = area_reg.async_get_area(query)
    if target is not None:
        return target
    wanted = query.lower()
    for area in area_reg.async_list_areas():
        aliases = {alias.lower() for alias in (area.aliases or [])}
        if (area.name or "").lower() == wanted or wanted in aliases:
            return area
    return None


def _entity_id_strings(node: Any, path: list, out: list[tuple[list, str]]) -> None:
    """Collect every entity-id-SHAPED string value in a structure, with its path.

    Deliberately matches the VALUE, never the key name, which is the opposite of
    _collect_entity_id_values above and is not a style difference. That one runs
    on script configs, where HA defines the key names, so keying on `entity_id`
    is exact. This one runs on dashboard cards and config-entry payloads, where
    the key is whatever the card author or the integration chose: an
    attribute_as_sensor helper stores its source under `entity_id`, a derivative
    under `source`, a group under `entity_ids`, and a custom card under anything
    at all. A key-name list would go stale silently and report NO dependency
    where one exists, which is exactly the failure this tool exists to prevent
    (it would have cleared an integration for removal while helpers were still
    built on its entities). Over-reporting a coincidental string is the safe
    direction here; under-reporting is not.

    The path is recorded in patch_dashboard's own addressing form (a list of
    mapping keys and list indexes), so a dashboard hit is directly actionable
    rather than something the caller has to go and find.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _entity_id_strings(value, [*path, key], out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _entity_id_strings(value, [*path, index], out)
    elif isinstance(node, str) and _ENTITY_ID_RE.match(node):
        out.append((path, node))


_TEMPLATE_MARKERS = ("{{", "}}", "{%", "|")


_SERVICE_FIELDS = frozenset({"service", "perform_action", "action"})


def _in_service_field(path: list) -> bool:
    """Whether a walked value sits where a SERVICE name goes rather than an id."""
    return bool(path) and path[-1] in _SERVICE_FIELDS


def _dangling_candidates(
    refs: dict[str, list[dict]], hass: HomeAssistant
) -> dict[str, list[dict]]:
    """Narrow referenced ids to those that were ever meant to BE an entity id.

    `dangling_references` is a list an operator is meant to ACT on, so a false
    positive is not harmless noise, it is what teaches them to ignore the field.
    Live first run: 26 entries of which about 20 were junk (`1.3em` from a card's
    CSS, `3.4` from a version string, `attributes.device_class` from a template,
    `deye_p3.yaml` from a filename, HA's `all` wildcard, and several `{{ }}`
    script targets) with a handful of genuinely dead references buried in it.

    THE FILTER STRENGTH MATCHES WHAT THE HOLDER KNEW, which is the whole design,
    and a value referenced from several places is kept if ANY holder vouches for
    it. A TYPED holder came from somewhere HA defines the shape: mesa-core's role
    walk, a script's `entity_id:` key, a scene's entity keys. Those strings were
    MEANT to be entity ids, so they are trusted outright. An untyped holder found
    the value by shape alone in a dashboard card or config-entry payload, where
    the key is whatever the author chose, so it also has to name a domain this
    instance actually has.

    A DOMAIN CHECK ON TYPED HOLDERS WOULD BE BACKWARDS and was tried first: a
    reference into a domain that no longer exists at all can never resolve, which
    makes it MORE dangling, not less. Its own test caught that.

    An untyped holder in a SERVICE-CALL POSITION is excluded unless the domain is
    `script`. A card's `perform_action: button.press` names a verb, not an entity,
    and has the exact shape of an id in the exact place the walk reads; running a
    script, though, IS its entity id, so a card pointing at a deleted script is
    the most valuable find here and must survive.

    THE FIRST ATTEMPT AT THAT USED SERVICE REGISTRATION and it was wrong, which
    only the live data showed: it dropped anything `hass.services.has_service`
    recognised, reasoning that a live script's service is registered and a dead
    one's is not. Home Assistant can keep a script's service registered after the
    script is gone, so two references whose entities did not exist were filtered
    away, i.e. the rule suppressed exactly the class it was written to preserve.
    POSITION distinguishes a verb from an id and depends on no runtime state that
    can go stale.

    Template syntax is excluded everywhere: a value carrying `{{ }}` is an id
    computed at runtime, not one Phoenix can resolve. HA's `all` wildcard goes
    with it, having no dot to split on.
    """
    domains = {state.entity_id.split(".", 1)[0] for state in hass.states.async_all()}
    domains |= {entry.entity_id.split(".", 1)[0] for entry in er.async_get(hass).entities.values()}
    domains |= set(hass.services.async_services())

    def _vouches(value: str, holder: dict) -> bool:
        if holder["typed"]:
            return True
        if value.split(".", 1)[0] not in domains:
            return False
        return value.startswith("script.") if holder["service"] else True

    out: dict[str, list[dict]] = {}
    for value, holders in refs.items():
        if "." not in value or any(m in value for m in _TEMPLATE_MARKERS):
            continue
        if any(_vouches(value, holder) for holder in holders):
            out[value] = holders
    return out


def _dangling_report(value: str, holders: list[dict]) -> dict:
    """One dangling entry: the dead id and what still points at it.

    Holders are GROUPED by consumer rather than listed one per hit, which is not
    cosmetic. Live: a single deleted script was called from 18 sub-buttons on one
    dashboard, and one automation referencing a dead entity in two roles appeared
    twice, so an ungrouped list spent most of the response repeating one
    dashboard's identity. Grouping answers the operator's actual question, which
    is WHICH THING to open, and the paths say where to look once inside.

    Paths are capped at const.MAX_DANGLING_PATHS with the true total alongside,
    the same report-total-and-truncated contract get_logs and list_backups use: a
    clipped list must never read as a complete one.
    """
    grouped: dict[tuple[str, str], dict] = {}
    for holder in holders:
        key = (holder["kind"], holder["id"])
        entry = grouped.setdefault(key, {
            "kind": holder["kind"], "id": holder["id"], "name": holder["name"], "paths": [],
        })
        if "path" in holder:
            entry["paths"].append(holder["path"])
    out = []
    for entry in grouped.values():
        paths = entry.pop("paths")
        if paths:
            entry["path_count"] = len(paths)
            entry["paths"] = paths[:MAX_DANGLING_PATHS]
            entry["truncated"] = len(paths) > MAX_DANGLING_PATHS
        out.append(entry)
    return {"entity_id": value, "referenced_by": out}


def _consumer(kind: str, cid: str, name: Any) -> dict:
    return {"kind": kind, "id": cid, "name": name if isinstance(name, str) else None,
            "entities": {}, "roles": set()}


def _record(consumer: dict, entity_id: str, role: str) -> None:
    consumer["entities"].setdefault(entity_id, set()).add(role)
    consumer["roles"].add(role)


def _finish(consumer: dict) -> dict:
    """Render one consumer's accumulated sets as the sorted lists a caller reads."""
    return {
        "kind": consumer["kind"],
        "id": consumer["id"],
        "name": consumer["name"],
        "roles": sorted(consumer["roles"]),
        "entities": [
            {"entity_id": eid, "roles": sorted(roles)}
            for eid, roles in sorted(consumer["entities"].items())
        ],
        **({"paths": consumer["paths"]} if consumer.get("paths") else {}),
    }


def _scan_relationships(
    hass: HomeAssistant,
    scope: set[str],
    dashboards: list[tuple[str, str | None, Any]],
    entries: list[dict],
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """Every consumer touching an entity in `scope`, and every entity id seen.

    ONE pass per source, not one pass per entity. The tool it replaces asked
    about a single entity and re-read all three YAML files each time, so
    sweeping six devices' worth of entities took ~74 calls against a 60/min rate
    limit, 63 of them returning nothing. Scanning a SET collapses that to one.

    The second return value maps every entity id referenced by any consumer, in
    scope or not, to the consumers holding it. Naming the holder is what makes a
    dangling reference actionable: "script.x is dead" sends someone hunting,
    while "the Fridge dashboard calls it at views[0].cards[3]" is a repair. Each
    holder also records how much its source knew (`typed` when HA defines the
    shape, e.g. a role walk or an `entity_id:` key; `service` when the value sat
    where a service name goes), which is what _dangling_candidates filters on.
    All of it is free, since the walk has already visited every consumer.

    Runs in an executor because it reads the automation/script/scene YAML.
    Dashboards and config entries are snapshotted by the caller in the event
    loop, since obtaining them needs loop access, and are passed in.
    """
    consumers: list[dict] = []
    refs: dict[str, list[dict]] = {}
    unreadable: list[dict] = []
    expand = build_expand_target(hass)

    def _entries(domain: str) -> Any:
        found, gaps = _domain_entries(hass, domain)
        unreadable.extend(gaps)
        return found

    def _note(entity_id: str, holder: dict) -> None:
        refs.setdefault(entity_id, []).append(holder)

    for cfg in _entries("automation"):
        if not isinstance(cfg, dict):
            continue
        by_role = entities_by_role(cfg, expand)
        entry = _consumer("automation", str(cfg.get("id", "")), cfg.get("alias"))
        for role, ents in by_role.items():
            for eid in ents:
                _note(eid, {"kind": "automation", "id": entry["id"],
                            "name": entry["name"], "typed": True, "service": False})
                if eid in scope:
                    _record(entry, eid, role)
        if entry["entities"]:
            consumers.append(entry)

    for script_id, cfg in _entries("script").items():
        if not isinstance(cfg, dict):
            continue
        found: set[str] = set()
        _collect_entity_id_values(cfg, found)
        entry = _consumer("script", script_id, cfg.get("alias"))
        for eid in found:
            _note(eid, {"kind": "script", "id": script_id, "name": entry["name"],
                        "typed": True, "service": False})
        for eid in found & scope:
            _record(entry, eid, "sequence")
        if entry["entities"]:
            consumers.append(entry)

    for scene in _entries("scene"):
        if not isinstance(scene, dict):
            continue
        members = scene.get("entities")
        if not isinstance(members, dict):
            continue
        entry = _consumer("scene", str(scene.get("id", "")), scene.get("name"))
        for member in members:
            if isinstance(member, str):
                _note(member, {"kind": "scene", "id": entry["id"], "name": entry["name"],
                               "typed": True, "service": False})
        for eid in set(members) & scope:
            _record(entry, eid, "member")
        if entry["entities"]:
            consumers.append(entry)

    for url_path, title, config in dashboards:
        hits: list[tuple[list, str]] = []
        _entity_id_strings(config, [], hits)
        entry = _consumer("dashboard", url_path, title)
        for path, eid in hits:
            _note(eid, {"kind": "dashboard", "id": url_path, "name": title, "path": path,
                        "typed": False, "service": _in_service_field(path)})
        entry["paths"] = []
        for path, eid in hits:
            if eid in scope:
                _record(entry, eid, "card")
                entry["paths"].append(path)
        if entry["entities"]:
            consumers.append(entry)

    for record in entries:
        hits = []
        _entity_id_strings(record["payload"], [], hits)
        entry = _consumer("config_entry", record["entry_id"], record["title"])
        for path, eid in hits:
            _note(eid, {"kind": "config_entry", "id": record["entry_id"],
                        "name": record["title"], "typed": False,
                        "service": _in_service_field(path)})
        for _path, eid in hits:
            if eid in scope:
                _record(entry, eid, record["domain"])
        if entry["entities"]:
            consumers.append(entry)

    return [_finish(c) for c in consumers], refs, unreadable


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

    target = _resolve_area(hass, area_query)

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


_RELATIONSHIP_SELECTORS = ("entity_id", "device_id", "integration", "area", "label")

_SCOPE_NOT_FOUND = (
    "Nothing accessible matched that scope. Check the selector value, or the entities "
    "it names may be outside this token's permission tree."
)


@dataclasses.dataclass(frozen=True)
class _Scope:
    """One resolved selector and the accessible entity ids it names."""

    selector: str
    value: str
    entity_ids: set[str]


def _relationship_scope(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> _Scope | tuple[dict, str, str]:
    """Resolve exactly one selector to the accessible entity ids it names.

    Every branch ends in the same filter, so the scope can never contain an
    entity the token cannot read, and a selector naming nothing accessible is
    byte-identical to one naming nothing at all (rule 12): a device id, area,
    label, or integration that does not exist must not be distinguishable from
    one whose entities are simply out of scope.

    device/area/label go through mesa.build_expand_target, the one definition of
    "which entities does this indirect reference name", rather than a second
    copy of the registry walk. `integration` has no expander because it is not a
    target selector HA understands; it reads the registry's own platform field.
    """
    given = [s for s in _RELATIONSHIP_SELECTORS if str_arg(args.get(s)).strip()]
    if len(given) != 1:
        return (
            _tool_error(
                "Pass exactly one of: " + ", ".join(_RELATIONSHIP_SELECTORS)
                + ". entity_id asks what references one entity; the others ask the same "
                "question about everything a device, integration, area, or label covers."
            ),
            "invalid_request", "get_relationships",
        )
    selector = given[0]
    value = str_arg(args.get(selector)).strip()

    if selector == "entity_id":
        candidates = [value]
    elif selector == "integration":
        candidates = [
            e.entity_id for e in er.async_get(hass).entities.values()
            if e.platform == value and e.disabled_by is None
        ]
    elif selector == "area":
        area = _resolve_area(hass, value)
        candidates = build_expand_target(hass)("area_id", area.id) if area is not None else []
    else:
        kind = "device_id" if selector == "device_id" else "label_id"
        candidates = build_expand_target(hass)(kind, value)

    scope = {
        eid for eid in candidates
        if resolve(eid, token, hass) in (Permission.READ, Permission.WRITE)
    }
    if not scope:
        # Byte-identical for "does not exist" and "not in your tree", and for the
        # single-entity form this is the same "Entity not found." the tool has
        # always returned.
        message = "Entity not found." if selector == "entity_id" else _SCOPE_NOT_FOUND
        return _tool_error(message), "not_found", f"{selector}:{value}"
    return _Scope(selector, value, scope)


async def _relationship_dashboards(
    hass: HomeAssistant,
) -> tuple[list[tuple[str, str | None, Any]], list[dict]]:
    """(url_path, title, config) per readable dashboard, plus what was NOT read.

    Fail-quiet per dashboard, because one unreadable dashboard must not cost the
    caller the whole answer. But quiet is not the same as silent: a YAML-mode
    dashboard is rejected by async_get_lovelace_config and an auto-generated one
    has no stored config, so skipping either while the response still said
    "searched: dashboard" would be the exact confident-wrong-answer this tool
    exists to avoid. Every skip is returned and surfaces in `not_searched`.

    A failed LIST is the worse case and says so differently: it means no named
    dashboard was examined at all, rather than one being missed.
    """
    out: list[tuple[str, str | None, Any]] = []
    skipped: list[dict] = []
    listed: list[dict] = []
    try:
        result = await async_ws_command(hass, "lovelace/dashboards/list", {})
        listed = [d for d in result if isinstance(d, dict)] if isinstance(result, list) else []
    except WsDispatchError as exc:
        _LOGGER.debug("get_relationships could not list dashboards", exc_info=True)
        skipped.append({"kind": "dashboard",
                        "reason": f"could not list dashboards, so none were searched: {exc}"})
    # None is the default dashboard, which the list command does not report. It
    # is skipped QUIETLY when absent: an auto-generated default has no stored
    # config to search and reporting that on every call would be noise, whereas
    # a NAMED dashboard that cannot be read is a real hole in the answer.
    for url_path, title in [(None, None)] + [
        (d.get("url_path"), d.get("title")) for d in listed if d.get("url_path")
    ]:
        try:
            config = await async_get_lovelace_config(hass, url_path)
        except WsDispatchError as exc:
            if url_path is not None:
                skipped.append({"kind": "dashboard", "id": url_path,
                                "reason": f"not readable (YAML-mode dashboards cannot be searched): {exc}"})
            continue
        if isinstance(config, dict):
            out.append((url_path or "lovelace", title, config))
        elif url_path is not None:
            skipped.append({"kind": "dashboard", "id": url_path,
                            "reason": "no stored configuration to search (auto-generated)"})
    return out, skipped


def _relationship_config_entries(hass: HomeAssistant) -> list[dict]:
    """Snapshot config-entry payloads in the loop for the executor to walk.

    Only entity ids are ever read back out of `payload`; the entries themselves
    hold credentials, so nothing from here reaches a response except an id that
    was already in the caller's scope.
    """
    return [
        {
            "entry_id": entry.entry_id,
            "domain": entry.domain,
            "title": entry.title,
            "payload": {"data": dict(entry.data), "options": dict(entry.options)},
        }
        for entry in hass.config_entries.async_entries()
    ]


async def _tool_get_relationships(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: what consumes the entities a selector names.

    Grouped by CONSUMER, not by entity. The entity-keyed shape made a caller
    union N results by hand to learn which automations to edit; a consumer
    carrying the in-scope entities it touches and their roles IS the edit work
    list.

    Coverage is reported rather than assumed. Dashboards and config entries need
    their own capabilities to name, so a token without them gets a narrower
    answer, and `not_searched` says so: a fast call with silent blind spots is
    worse than a slow one, because it produces a CONFIDENT wrong answer. This
    tool's whole job is answering "is anything still using these", and the one
    unacceptable reply is a wrong no.
    """
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_relationships"

    resolved = _relationship_scope(args, token, hass)
    if isinstance(resolved, tuple):
        return resolved
    selector, value, scope = resolved.selector, resolved.value, resolved.entity_ids

    searched = ["automation", "script", "scene"]
    not_searched: list[dict] = []
    dashboards: list[tuple[str, str | None, Any]] = []
    entries: list[dict] = []
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        not_searched.append({"kind": "dashboard", "reason": "requires cap_lovelace_write"})
    else:
        dashboards, skipped = await _relationship_dashboards(hass)
        not_searched.extend(skipped)
        searched.append("dashboard")
    if effective_cap(token, "cap_integration_write") == CAP_DENY:
        not_searched.append({"kind": "config_entry", "reason": "requires cap_integration_write"})
    else:
        entries = _relationship_config_entries(hass)
        searched.append("config_entry")

    consumers, refs, unreadable = await hass.async_add_executor_job(
        _scan_relationships, hass, scope, dashboards, entries,
    )
    # A YAML branch this cannot read is the same class of gap as a capability it
    # does not hold, so it lands in the same field rather than a second one.
    not_searched.extend(unreadable)
    # Free, because the walk already visited every consumer: an id referenced by
    # something but present in neither hass.states nor the registry is rule 8's
    # ghost, i.e. a reference that can never resolve again for anyone. Reported
    # for the whole walk rather than for the scope, since a dead reference is
    # worth surfacing wherever it was found.
    registry = er.async_get(hass)
    dangling = [
        _dangling_report(eid, holders)
        for eid, holders in sorted(_dangling_candidates(refs, hass).items())
        if hass.states.get(eid) is None and registry.async_get(eid) is None
    ]

    body: dict[str, Any] = {
        "scope": {"selector": selector, "value": value,
                  "entity_ids": sorted(scope), "count": len(scope)},
        "consumers": consumers,
        "consumer_count": len(consumers),
        "dangling_references": dangling,
        "searched": searched,
    }
    if not_searched:
        body["not_searched"] = not_searched
    if selector == "entity_id":
        # Only meaningful for a single automation or script: what IT references.
        body["references"] = await hass.async_add_executor_job(
            _forward_references, hass, token, value,
        )
    return _tool_success(json.dumps(body, default=str)), "allowed", f"{selector}:{value}"


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
        # This field covers automations, scripts and scenes; get_relationships
        # additionally reaches dashboards and config entries. The difference is
        # deliberate (this is a cheap one-entity summary, and scanning dashboards
        # costs a WS round trip per dashboard), but an unqualified "what
        # references it" invites reading an empty or short list as PROOF nothing
        # does, which is the confident wrong answer get_relationships exists to
        # avoid. Naming the limit costs a line; the mesa_note precedent below.
        "referenced_by_note": (
            "Automations, scripts and scenes only. Call get_relationships for the "
            "full picture, including dashboards and config entries such as helpers "
            "built on this entity, and for entity IDs that no longer resolve."
        ),
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


_SCALAR_TYPES = (str, int, float, bool, type(None))


def _scalar_list(value: Any) -> list | None:
    """The value as a list of scalars, or None when it is not one.

    A list of mappings (a weather forecast, a media browse tree) has no useful
    member-wise reading: differencing it produces two near-identical blobs
    rather than the one-line "this option was renamed" a caller acts on, and it
    would dominate the response. Those are reported by type instead.
    """
    if not isinstance(value, (list, tuple)):
        return None
    items = list(value)
    return items if all(isinstance(v, _SCALAR_TYPES) for v in items) else None


def _bounded_value(value: Any) -> Any:
    """One attribute value, bounded and JSON-safe.

    Scalars and short scalar lists pass through, since the value itself is the
    answer. A long string is clipped and a larger structure is replaced by a
    marker naming its type, because the question this tool answers is whether
    two entities are interchangeable, not what either one currently holds.
    """
    if isinstance(value, str) and len(value) > MAX_COMPARE_VALUE_CHARS:
        return value[:MAX_COMPARE_VALUE_CHARS] + f"... ({len(value) - MAX_COMPARE_VALUE_CHARS} more characters)"
    if isinstance(value, _SCALAR_TYPES):
        return value
    scalars = _scalar_list(value)
    if scalars is not None and len(scalars) <= MAX_COMPARE_LIST_VALUES:
        return scalars
    size = len(value) if isinstance(value, (list, tuple, dict)) else None
    marker = type(value).__name__ if size is None else f"{type(value).__name__} with {size} entries"
    return {"omitted": marker}


def _changed_attribute(name: str, value: Any, other: Any) -> dict:
    """One entry in the `changed` array, differenced by value shape.

    Two scalar LISTS are differenced member-wise, and that case is the reason
    this tool exists: an option set renamed between two integrations
    (preset_modes boost -> speed) reads as a removal beside an addition, which
    is directly actionable, while the two full lists side by side leave the
    caller to spot it. Everything else reports both values, so a narrowed range
    (min_temp 7 -> 16) is visible as the pair it is.
    """
    entry: dict[str, Any] = {"attribute": name}
    values = _scalar_list(value)
    others = _scalar_list(other)
    if values is None or others is None:
        entry["value"] = _bounded_value(value)
        entry["compare_value"] = _bounded_value(other)
        return entry
    removed = [v for v in values if v not in others]
    added = [v for v in others if v not in values]
    entry["removed"] = removed[:MAX_COMPARE_LIST_VALUES]
    entry["removed_total"] = len(removed)
    entry["added"] = added[:MAX_COMPARE_LIST_VALUES]
    entry["added_total"] = len(added)
    entry["truncated"] = (
        len(removed) > MAX_COMPARE_LIST_VALUES or len(added) > MAX_COMPARE_LIST_VALUES
    )
    if not removed and not added:
        # Same members in a different order. Saying so is the whole point of the
        # flag: an entry carrying two empty arrays otherwise reads as a
        # difference the caller failed to understand, when it is one they can
        # almost always ignore.
        entry["reordered"] = True
    return entry


_UNPUBLISHED_WARNING = (
    "{entity_id} is {state}, so it is publishing few or none of its attributes. "
    "The differences below describe what it reports right now, not what it "
    "supports; do not read an attribute as absent until it is available again."
)


def _publishes_nothing(state: dict) -> bool:
    """Whether this entity's attribute set is too degraded to compare against.

    An offline entity keeps its registry entry and answers every read, so
    nothing about the comparison FAILS: it just silently becomes a list of
    attributes the other side has, which is a description of the outage and
    reads exactly like a description of the replacement. Live-hit on the first
    smoke test, where an unavailable unit reported five attributes "missing"
    that it publishes perfectly well when it is up. `restored` is HA's own
    marker for a state it rebuilt from the registry because the integration
    supplied none, and it is the precise signal; `unknown` is deliberately NOT
    included, since an entity with no value yet still publishes its attributes
    and warning there would fire on the ordinary case.
    """
    return state.get("state") == "unavailable" or (state.get("attributes") or {}).get("restored") is True


async def _tool_compare_entities(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: what differs between two accessible entities' current shapes."""
    if effective_cap(token, "cap_search") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "compare_entities"

    entity_id = str_arg(args.get("entity_id"))
    compare_to = str_arg(args.get("compare_to"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "compare_entities"
    if not compare_to:
        return _tool_error("Missing required argument: compare_to"), "invalid_request", "compare_entities"

    states = []
    for eid in (entity_id, compare_to):
        # Both sides answer with the same body whether the entity is absent or
        # merely out of scope, and the resource names the tool rather than the
        # id, so a refusal cannot report which of the two was the problem.
        if resolve(eid, token, hass) not in (Permission.READ, Permission.WRITE):
            return _tool_error("Entity not found."), "not_found", "compare_entities"
        state = hass.states.get(eid)
        if state is None:
            return _tool_error("Entity not found."), "not_found", "compare_entities"
        states.append(scrub_sensitive_attributes(state))

    # Scrubbing runs before the diff, so an attribute Phoenix strips from a
    # state read can never be reintroduced here as a difference between two.
    attrs, compare_attrs = (s.get("attributes") or {} for s in states)
    warnings = [
        _UNPUBLISHED_WARNING.format(entity_id=eid, state=state.get("state"))
        for eid, state in ((entity_id, states[0]), (compare_to, states[1]))
        if _publishes_nothing(state)
    ]
    missing = sorted(set(attrs) - set(compare_attrs))
    added = sorted(set(compare_attrs) - set(attrs))
    shared = sorted(set(attrs) & set(compare_attrs))
    changed = [
        _changed_attribute(name, attrs[name], compare_attrs[name])
        for name in shared
        if attrs[name] != compare_attrs[name]
    ]

    body = {
        "entity_id": entity_id,
        "compare_to": compare_to,
        "domain": entity_id.split(".")[0],
        "compare_domain": compare_to.split(".")[0],
        "state": states[0].get("state"),
        "compare_state": states[1].get("state"),
        # Named for the direction that breaks a substitution: something reading
        # entity_id's attribute finds nothing on compare_to. The other two
        # arrays are informational by comparison.
        "missing_in_compare_to": [
            {"attribute": name, "value": _bounded_value(attrs[name])} for name in missing
        ],
        "added_in_compare_to": [
            {"attribute": name, "value": _bounded_value(compare_attrs[name])} for name in added
        ],
        "changed": changed,
        "identical_count": len(shared) - len(changed),
        # Conditional, per the package's convention for a read that degraded but
        # still succeeded: a field that is usually absent gets read when it does
        # appear, where a permanently-present "degraded": false trains the
        # reader to skip it.
        **({"warnings": warnings} if warnings else {}),
        "note": (
            "Current attributes only. A value that varies over time (an enum "
            "sensor's state) can differ without appearing here; use get_history "
            "on both entities across the same window to compare those."
        ),
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "compare_entities"


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
