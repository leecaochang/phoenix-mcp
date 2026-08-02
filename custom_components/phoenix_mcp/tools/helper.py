"""Helper tools: input_* / counter / timer CRUD over the in-process WS dispatch.

The storage-based helper domains only, listed in HELPER_TYPES: each publishes a
uniform {type}/create|update|delete WS command whose item id key is
"{type}_id", which is what makes one set of handlers cover all seven.
Config-entry helper types (template, group, utility_meter) do not work that way
and are deliberately out of scope.

The precheck here hoists only the cheap structural checks (type known, id
present, config a mapping). A helper write validates by way of the WS command
that IS the write, so there is no side-effect-free validator to run pre-gate;
the rest stays at execution time.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from typing import Any
import json
import logging

from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant

from ..const import CAP_DENY
from ..data import PhoenixData
from ..ws_dispatch import WsDispatchError, async_ws_command
from ..helpers import dict_arg, diff_summary_fields as _summary, effective_cap
from ..tool_common import _CAP_FORBIDDEN_MESSAGE, _gate, _record_version, _tool_error, _tool_success, _truncate
from ..policy_engine import filter_entities_for_token
from ..token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)


# Storage-based helper domains managed via the in-process WS command dispatch
# ({type}/create|update|delete, item id key = "{type}_id"). Config-entry helper
# types (template, group, utility_meter, etc.) are out of scope for now.
HELPER_TYPES = frozenset({
    "input_boolean", "input_number", "input_text",
    "input_select", "input_datetime", "counter", "timer",
})


# ---------------------------------------------------------------------------
# Helper CRUD (cap_helper_write) via in-process WS command dispatch
# ---------------------------------------------------------------------------


def _valid_helper_type(helper_type: Any) -> bool:
    return isinstance(helper_type, str) and helper_type in HELPER_TYPES


def _resolve_helper_entity_id(hass: HomeAssistant, helper_type: str, helper_id: str) -> str | None:
    """Map a storage-helper id back to its entity_id via the registry, or None.

    list_helpers exposes entry.unique_id as the editable helper_id, so the reverse
    lookup matches on (domain == helper_type, unique_id == helper_id). Used by
    edit/delete as an existence check (the helper must resolve to a real
    entity); authoring itself is cap-gated, not entity-scoped.
    """
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == helper_type and entry.unique_id == helper_id:
            return entry.entity_id
    return None


async def _tool_list_helpers(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list accessible helper entities with their editable helper id."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_helpers"
    type_filter = args.get("helper_type")
    registry = er.async_get(hass)
    helpers: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        domain = e["entity_id"].split(".")[0]
        if domain not in HELPER_TYPES:
            continue
        if type_filter and domain != type_filter:
            continue
        entry = registry.async_get(e["entity_id"])
        helpers.append({
            "entity_id": e["entity_id"],
            "helper_type": domain,
            "name": e.get("attributes", {}).get("friendly_name"),
            "helper_id": entry.unique_id if entry is not None else None,
        })
    helpers.sort(key=lambda h: h["entity_id"])
    return _tool_success(json.dumps({"count": len(helpers), "helpers": helpers}, default=str)), "allowed", "list_helpers"


def _helper_write_precheck(args: dict, tool_name: str, *, require_id: bool) -> tuple[dict, str, str] | None:
    """Pre-gate validation for helper writes; None means OK to proceed.

    Checks helper_type/helper_id/config shape so a doomed request is rejected
    before a pending approval is created. HA's create/update WS command is the
    only place that validates the per-type schema, and that call is itself the
    write, so it cannot be pre-checked further here. The executor re-validates
    at apply time.
    """
    if not _valid_helper_type(args.get("helper_type")):
        return _tool_error(f"helper_type must be one of: {', '.join(sorted(HELPER_TYPES))}."), "invalid_request", tool_name
    if require_id and not str(args.get("helper_id") or "").strip():
        return _tool_error("helper_id is required."), "invalid_request", tool_name
    config = args.get("config")
    if require_id:
        if not isinstance(config, dict):
            return _tool_error("config must be an object."), "invalid_request", tool_name
    elif not isinstance(config, dict) or not config:
        return _tool_error("config must be a non-empty object (at least 'name')."), "invalid_request", tool_name
    return None


async def _tool_create_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a helper (Confirm-gated)."""
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "create_helper"
    pre = _helper_write_precheck(args, "create_helper", require_id=False)
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_helper_write", token, hass, data,
        tool_name="create_helper", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_create_helper(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_helper(args, token, hass, data)


async def _read_helper_config(hass: HomeAssistant, helper_type: str, helper_id: str) -> dict | None:
    """Return a helper's current stored config (for version-history `before`), or None.

    Best-effort: a failure to read the prior config must not block the edit/delete,
    so any dispatch error degrades to no `before` rather than raising.
    """
    try:
        items = await async_ws_command(hass, f"{helper_type}/list", {})
    except WsDispatchError:
        return None
    if not isinstance(items, list):
        return None
    return next(
        (it for it in items if isinstance(it, dict) and it.get("id") == helper_id), None
    )


async def _execute_create_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    helper_type = args.get("helper_type")
    config = args.get("config")
    if not _valid_helper_type(helper_type):
        return _tool_error(f"helper_type must be one of: {', '.join(sorted(HELPER_TYPES))}."), "invalid_request", "create_helper"
    if not isinstance(config, dict) or not config:
        return _tool_error("config must be a non-empty object (at least 'name')."), "invalid_request", "create_helper"
    try:
        item = await async_ws_command(hass, f"{helper_type}/create", dict(config))
    except WsDispatchError as exc:
        return _tool_error(f"Failed to create helper: {exc}"), "invalid_request", "create_helper"
    new_id = item.get("id") if isinstance(item, dict) else None
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{new_id}",
        action="create", before=None, after=config, alias=config.get("name"),
    )
    return _tool_success(json.dumps({"helper_type": helper_type, "helper": item}, default=str)), "allowed", f"helper:{helper_type}"


async def _tool_edit_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: edit a helper (Confirm-gated)."""
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "edit_helper"
    pre = _helper_write_precheck(args, "edit_helper", require_id=True)
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_helper_write", token, hass, data,
        tool_name="edit_helper", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_edit_helper(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_helper(args, token, hass, data)


async def _execute_edit_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    helper_type = args.get("helper_type")
    helper_id = str(args.get("helper_id") or "").strip()
    config = args.get("config")
    if not _valid_helper_type(helper_type):
        return _tool_error(f"helper_type must be one of: {', '.join(sorted(HELPER_TYPES))}."), "invalid_request", "edit_helper"
    if not helper_id:
        return _tool_error("helper_id is required."), "invalid_request", "edit_helper"
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "edit_helper"
    # Helper authoring is cap-gated (cap_helper_write), not entity-scoped: like
    # scripts and automations, a token that may write helpers may edit any helper
    # entity-scoped. We still require the helper to exist.
    entity_id = _resolve_helper_entity_id(hass, str(helper_type), str(helper_id))
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    before_cfg = await _read_helper_config(hass, str(helper_type), str(helper_id))
    payload = {f"{helper_type}_id": helper_id, **config}
    try:
        item = await async_ws_command(hass, f"{helper_type}/update", payload)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to edit helper: {exc}"), "invalid_request", "edit_helper"
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{helper_id}",
        action="edit", before=before_cfg, after=config, alias=config.get("name"),
    )
    return _tool_success(json.dumps({"helper_type": helper_type, "helper": item}, default=str)), "allowed", f"helper:{helper_type}:{helper_id}"


async def _tool_delete_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete a helper (Confirm-gated)."""
    blocked = await _gate(
        "cap_helper_write", token, hass, data,
        tool_name="delete_helper", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_helper(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_helper(args, token, hass, data)


async def _execute_delete_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    helper_type = args.get("helper_type")
    helper_id = str(args.get("helper_id") or "").strip()
    if not _valid_helper_type(helper_type):
        return _tool_error(f"helper_type must be one of: {', '.join(sorted(HELPER_TYPES))}."), "invalid_request", "delete_helper"
    if not helper_id:
        return _tool_error("helper_id is required."), "invalid_request", "delete_helper"
    # Cap-gated, not entity-scoped; existence still required.
    entity_id = _resolve_helper_entity_id(hass, str(helper_type), str(helper_id))
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    before_cfg = await _read_helper_config(hass, str(helper_type), str(helper_id))
    try:
        await async_ws_command(hass, f"{helper_type}/delete", {f"{helper_type}_id": helper_id})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to delete helper: {exc}"), "invalid_request", "delete_helper"
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{helper_id}",
        action="delete", before=before_cfg, after=None,
        alias=before_cfg.get("name") if isinstance(before_cfg, dict) else None,
    )
    return _tool_success(f"Helper '{helper_id}' deleted successfully."), "allowed", f"helper:{helper_type}:{helper_id}"


def _build_diff_create_helper(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    helper_type = args.get("helper_type")
    config = dict_arg(args.get("config"))
    return {
        "kind": "config_diff",
        **_summary("create_helper", helper_type=helper_type, name=config.get("name", "<no name>")),
        "target": {"type": "helper", "id": None, "label": config.get("name")},
        "before": None,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"helper_type": helper_type},
    }


def _build_diff_edit_helper(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    helper_type = args.get("helper_type")
    helper_id = str(args.get("helper_id") or "").strip()
    config = dict_arg(args.get("config"))
    return {
        "kind": "yaml_diff",
        **_summary("edit_helper", helper_type=helper_type, helper_id=helper_id),
        "target": {"type": "helper", "id": helper_id, "label": config.get("name")},
        "before": None,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"helper_type": helper_type},
    }


def _build_diff_delete_helper(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    helper_type = args.get("helper_type")
    helper_id = str(args.get("helper_id") or "").strip()
    return {
        "kind": "system_action",
        **_summary("delete_helper", helper_type=helper_type, helper_id=helper_id),
        "target": {"type": "helper", "id": helper_id, "label": helper_id},
        "before": None,
        "preview": {"helper_type": helper_type, "warning": "This helper will be removed permanently."},
    }
