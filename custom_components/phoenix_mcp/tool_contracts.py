"""Normalization for the breaking, structured Tool Catalog v2 inputs."""

from __future__ import annotations

from typing import Any


_RETIRED_REPLACEMENTS: dict[str, dict[str, str]] = {
    "get_state": {
        "detailed": 'projection: {"kind":"full"} (or {"kind":"compact"})',
        "fields": 'projection: {"kind":"fields","fields":[...]}',
    },
    "get_states": {
        "detailed": 'projection: {"kind":"full"} (or {"kind":"compact"})',
        "fields": 'projection: {"kind":"fields","fields":[...]}',
    },
    "get_calendar_events": {"calendar_id": "entity_id"},
    "wait_for_approval": {"approval_id": 'approval_ids: ["<approval_id>"]'},
    "call_service": {
        "domain": 'service: {"domain":"...","name":"...","data":{...}}',
        "service_data": "service.data",
        "entity_id": 'targets: [{"kind":"entity","ids":[...]}]',
        "device_id": 'targets: [{"kind":"device","ids":[...]}]',
        "area_id": 'targets: [{"kind":"area","ids":[...]}]',
    },
    "dry_run_service": {
        "domain": 'service: {"domain":"...","name":"...","data":{...}}',
        "service_data": "service.data",
        "entity_id": 'targets: [{"kind":"entity","ids":[...]}]',
        "device_id": 'targets: [{"kind":"device","ids":[...]}]',
        "area_id": 'targets: [{"kind":"area","ids":[...]}]',
    },
    "get_relationships": {
        "entity_id": 'scope: {"kind":"entity","id":"..."}',
        "device_id": 'scope: {"kind":"device","id":"..."}',
        "integration": 'scope: {"kind":"integration","id":"..."}',
        "area": 'scope: {"kind":"area","id":"..."}',
        "label": 'scope: {"kind":"label","id":"..."}',
    },
    "get_logbook": {
        "entity_ids": 'target: {"kind":"resources","entity_ids":[...]}',
        "device_ids": 'target: {"kind":"resources","device_ids":[...]}',
        "context_id": 'target: {"kind":"context","id":"..."}',
    },
    "get_esphome_job": {
        "job_id": 'lookup: {"kind":"job","id":"..."}',
        "file": 'lookup: {"kind":"file","id":"..."}',
    },
    "edit_energy_config": {
        "op": "operation",
        "statistic": 'target: {"kind":"statistic","id":"..."}',
        "device_name": 'target: {"kind":"device_name","name":"..."}',
        "source_type": 'target: {"kind":"source","source_type":"..."}',
        "new_statistic": "changes.new_statistic",
        "name": "changes.name",
        "stat_energy_from": "changes.stat_energy_from",
        "stat_energy_to": "changes.stat_energy_to",
        "number_energy_price": "changes.number_energy_price",
        "number_energy_price_export": "changes.number_energy_price_export",
        "entity_energy_price": "changes.entity_energy_price",
        "entity_energy_price_export": "changes.entity_energy_price_export",
        "stat_cost": "changes.stat_cost",
        "stat_compensation": "changes.stat_compensation",
    },
    "patch_yaml_config": {
        "key": 'address: {"kind":"key","value":"..."}',
        "path": 'address: {"kind":"path","value":[...]}',
        "op": "change.kind",
        "content": "change.content",
    },
    "patch_dashboard": {
        "url_path": "target.url_path",
        "path": "target.path",
        "op": "change.kind",
        "value": "change.value",
    },
    "set_entity": {
        field: f"changes.{field}" for field in (
            "name", "icon", "area_id", "device_class", "new_entity_id", "enabled",
            "hidden", "add_aliases", "remove_aliases", "add_labels", "remove_labels",
            "categories",
        )
    },
    "set_device": {
        field: f"changes.{field}"
        for field in ("name", "area_id", "enabled", "add_labels", "remove_labels")
    },
    "set_integration": {
        field: f"changes.{field}"
        for field in ("title", "pref_disable_new_entities", "pref_disable_polling")
    },
    "compare_states": {"entity_id": "entity_ids: [...]"},
}


_SERVICE_OBJECT_SYNTAX = 'service: {"domain":"...","name":"...","data":{...}}'


def _migration_error(
    tool: str, retired: set[str], extra: dict[str, str] | None = None
) -> str:
    """Actionable field-by-field replacement syntax for a retired v1 shape."""
    replacements = {**_RETIRED_REPLACEMENTS[tool], **(extra or {})}
    fields = sorted(retired)
    mapping = "; ".join(f"{field} -> {replacements[field]}" for field in fields)
    return (
        f"Catalog v2 no longer accepts {', '.join(fields)} for {tool}. "
        f"Replace with: {mapping}."
    )


def normalize_tool_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Translate a v2 public request to the established executor shape.

    Unknown keys intentionally remain untouched. Only fields that were publicly
    retired by Catalog v2 receive a migration error.
    """
    replacements = _RETIRED_REPLACEMENTS.get(tool, {})
    retired = set(replacements) & set(args)
    service_string = tool in {"call_service", "dry_run_service"} and isinstance(
        args.get("service"), str
    )
    if retired or service_string:
        extra = {"service": _SERVICE_OBJECT_SYNTAX} if service_string else None
        return args, _migration_error(
            tool, retired | ({"service"} if service_string else set()), extra
        )
    out = dict(args)
    if tool in {"get_state", "get_states"}:
        projection = out.pop("projection", None)
        if not isinstance(projection, dict):
            return out, "projection is required and must be an object."
        kind = projection.get("kind")
        if kind == "full":
            out["detailed"] = True
        elif kind == "fields" and isinstance(projection.get("fields"), list):
            out["fields"] = projection["fields"]
        elif kind != "compact":
            return out, "projection.kind must be compact, full, or fields."
    elif tool == "get_calendar_events":
        pass
    elif tool in {"call_service", "dry_run_service"}:
        service = out.pop("service", None)
        targets = out.pop("targets", None)
        if not isinstance(service, dict) or not isinstance(service.get("domain"), str) or not isinstance(service.get("name"), str):
            return out, "service must contain string domain and name."
        out["domain"] = service["domain"]
        out["service"] = service["name"]
        if "data" in service:
            out["service_data"] = service["data"]
        if targets is not None:
            if not isinstance(targets, list) or not targets:
                return out, "targets must be a non-empty array when supplied."
            for target in targets:
                if not isinstance(target, dict):
                    return out, "Each target must be an object."
                kind, ids = target.get("kind"), target.get("ids")
                if kind == "all":
                    out["entity_id"] = "all"
                elif kind in {"entity", "device", "area"} and isinstance(ids, list) and ids:
                    key = f"{kind}_id"
                    out[key] = ids
                else:
                    return out, "Each target needs kind all, or entity/device/area with non-empty ids."
    elif tool == "get_relationships":
        scope = out.pop("scope", None)
        if not isinstance(scope, dict) or scope.get("kind") not in {"entity", "device", "integration", "area", "label"} or not isinstance(scope.get("id"), str):
            return out, "scope must contain a supported kind and string id."
        out[f"{scope['kind']}_id" if scope["kind"] in {"entity", "device"} else scope["kind"]] = scope["id"]
    elif tool == "get_logbook":
        target = out.pop("target", None)
        if target is None:
            return out, None
        if not isinstance(target, dict):
            return out, "target must be an object."
        if target.get("kind") == "context" and isinstance(target.get("id"), str):
            out["context_id"] = target["id"]
        elif target.get("kind") == "resources":
            for key in ("entity_ids", "device_ids"):
                if key in target:
                    out[key] = target[key]
        else:
            return out, "target must be a context id or resource selector."
    elif tool == "get_esphome_job":
        lookup = out.pop("lookup", None)
        if not isinstance(lookup, dict) or lookup.get("kind") not in {"job", "file"} or not isinstance(lookup.get("id"), str):
            return out, "lookup must contain kind job or file and a string id."
        out["job_id" if lookup["kind"] == "job" else "file"] = lookup["id"]
    elif tool == "edit_energy_config":
        operation, target, changes = out.pop("operation", None), out.pop("target", None), out.pop("changes", None)
        if not isinstance(operation, str) or not isinstance(target, dict) or not isinstance(changes, dict):
            return out, "operation, target, and changes must be objects in the Catalog v2 shape."
        out["op"] = operation
        out.update(changes)
        if target.get("kind") == "statistic" and isinstance(target.get("id"), str):
            out["statistic"] = target["id"]
        elif target.get("kind") == "device_name" and isinstance(target.get("name"), str):
            out["device_name"] = target["name"]
        elif target.get("kind") == "source" and isinstance(target.get("source_type"), str):
            out["source_type"] = target["source_type"]
        else:
            return out, "target must identify a statistic, device_name, or source."
    elif tool == "patch_yaml_config":
        address, change = out.pop("address", None), out.pop("change", None)
        if not isinstance(address, dict) or not isinstance(change, dict):
            return out, "address and change must be objects."
        if address.get("kind") == "key" and isinstance(address.get("value"), str):
            out["key"] = address["value"]
        elif address.get("kind") == "path" and isinstance(address.get("value"), list):
            out["path"] = address["value"]
        else:
            return out, "address must be a key or path selector."
        out["op"] = change.get("kind")
        if "content" in change:
            out["content"] = change["content"]
    elif tool == "patch_dashboard":
        target, change = out.pop("target", None), out.pop("change", None)
        if not isinstance(target, dict) or not isinstance(change, dict) or not isinstance(target.get("path"), list):
            return out, "target.path and change must be supplied."
        out.update({key: target[key] for key in ("url_path", "path") if key in target})
        out["op"] = change.get("kind")
        if "value" in change:
            out["value"] = change["value"]
    elif tool in {"set_entity", "set_device", "set_integration"}:
        changes = out.pop("changes", None)
        if not isinstance(changes, dict) or not changes:
            return out, "changes must be a non-empty object."
        out.update(changes)
    elif tool == "compare_states":
        ids = out.pop("entity_ids", None)
        if not isinstance(ids, list) or not ids:
            return out, "entity_ids must be a non-empty array."
        out["entity_id"] = ids
    return out, None
