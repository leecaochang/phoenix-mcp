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

import voluptuous as vol
import voluptuous_serialize

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.loader import IntegrationNotFound, async_get_integration

from ..const import CAP_DENY
from ..data import PhoenixData
from ..ws_dispatch import WsDispatchError, async_ws_command
from ..helpers import content_hash, dict_arg, diff_summary_fields as _summary, effective_cap, version_summary_fields as _version_summary
from ..tool_common import _CAP_FORBIDDEN_MESSAGE, _cas_conflict, _gate, _record_version, _tool_error, _tool_success, _truncate
from ..policy_engine import _ENTITY_ID_RE, Permission, filter_entities_for_token, resolve
from ..token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)

# One message for "no such entry" and "not a helper" (rule 12): whether an
# entry_id names a real integration is not something this tool should disclose.
_NOT_A_HELPER_ENTRY = (
    "No helper configuration entry with that id. This tool edits HELPER entries only "
    "(the ones Home Assistant classifies as helpers, such as threshold, derivative, "
    "switch_as_x or min_max); integration entries cannot be reconfigured here."
)


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


# ---------------------------------------------------------------------------
# Config-entry helper reconfigure (cap_helper_write)
# ---------------------------------------------------------------------------


async def _helper_config_entry(hass: HomeAssistant, entry_id: Any) -> Any | None:
    """A config entry that is a HELPER, or None. The gate for both tools here.

    "Is this a helper" is HA's OWN classification, `integration_type: helper` in
    the integration's manifest, not a list of domain names maintained here. Every
    core helper declares it (threshold, derivative, switch_as_x, min_max,
    utility_meter, group, template) and a hub declares something else, so a
    third-party helper installed tomorrow is covered without a code change and
    cannot be mistaken for an integration.

    That distinction carries the whole security argument for this pair of tools.
    A helper's options are entity references and numbers, so every entity in a
    write can be checked against the token's tree before it lands. An arbitrary
    integration's options flow can carry a HOSTNAME AND CREDENTIALS, and
    repointing one at a server the operator does not control is a
    data-exfiltration primitive that no scope check would catch. Widening past
    helpers is therefore a separate decision with its own capability, not
    something to reach by relaxing this predicate.
    """
    if not isinstance(entry_id, str) or not entry_id.strip():
        return None
    entry = hass.config_entries.async_get_entry(entry_id.strip())
    if entry is None:
        return None
    try:
        integration = await async_get_integration(hass, entry.domain)
    except IntegrationNotFound:
        return None
    return entry if integration.integration_type == "helper" else None


def _options_schema_json(result: Any) -> list:
    """Serialize a flow step's data_schema the way HA's own frontend receives it.

    voluptuous_serialize with HA's custom_serializer is exactly what
    helpers/data_entry_flow._prepare_result_json uses, so an agent reads the same
    field names, defaults and selectors the UI does; an entity field even carries
    the domains it accepts. Reimplementing this would drift from what the flow
    actually validates against.
    """
    schema = result.get("data_schema")
    if schema is None:
        return []
    converted = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    # convert() types as dict-or-list; a vol.Schema over a mapping always yields
    # the per-field list, and anything else is not a form an agent can fill.
    return converted if isinstance(converted, list) else []


async def _abort_flow(hass: HomeAssistant, flow_id: str | None) -> None:
    """Drop an options flow we opened but will not finish.

    Every early return between async_init and a successful configure has to come
    through here: the flow lives in HA's own manager, not in the request, so
    abandoning one leaks it into config_entries/flow/progress where it shows up
    in the operator's UI as a half-finished dialog.
    """
    if not flow_id:
        return
    try:
        hass.config_entries.options.async_abort(flow_id)
    except Exception:  # noqa: BLE001 - best effort; an already-gone flow is fine
        _LOGGER.debug("Could not abort options flow %s", flow_id, exc_info=True)


async def _tool_get_config_entry_options(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read one helper config entry's options and the schema for them.

    A read, so never approval-gated even when cap_helper_write is confirm (the
    get_dashboard_config precedent). It opens an options flow to obtain the
    schema, because the schema is what the flow declares rather than something
    stored, and aborts it immediately: this must not leave a dialog open in the
    operator's UI just because something asked what the fields are.
    """
    tool = "get_config_entry_options"
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", tool
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    body: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "options": dict(entry.options),
        "content_hash": content_hash(dict(entry.options)),
        "supports_options": entry.supports_options,
    }
    if not entry.supports_options:
        body["schema"] = []
        body["note"] = "This helper does not expose an options flow, so its settings cannot be changed here."
        return _tool_success(json.dumps(body, default=str)), "allowed", tool
    flow_id = None
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        flow_id = result.get("flow_id")
        body["schema"] = _options_schema_json(result)
        body["step_id"] = result.get("step_id")
    except Exception:  # noqa: BLE001 - a flow that will not start is not a Phoenix bug
        _LOGGER.exception("Could not open the options flow for %s", entry.entry_id)
        return _tool_error("Could not read this helper's settings."), "invalid_request", tool
    finally:
        await _abort_flow(hass, flow_id)
    return _tool_success(json.dumps(body, default=str)), "allowed", tool


def _entities_in_options(options: Any) -> set[str]:
    """Every entity-id-shaped string value anywhere in an options payload.

    Matched by VALUE, never by key name, for the reason the relationship walk
    learned the hard way: each helper names its source differently (`entity_id`
    on threshold, `source` on derivative, `entity_ids` on min_max, whatever a
    third-party helper chose), so a key list would go stale silently and let an
    unchecked entity through. Over-matching a coincidental string only costs a
    scope check the caller would pass anyway.
    """
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)
        elif isinstance(node, str) and _ENTITY_ID_RE.match(node):
            found.add(node)

    _walk(options)
    return found


def _unwritable_option_entities(
    options: Any, token: TokenRecord, hass: HomeAssistant
) -> list[str]:
    """Entities in an options payload the token may not WRITE.

    WRITE rather than READ, matching _unwritable_scene_members, and the strictness
    is deliberate. A helper both EXPOSES its source (a token that cannot read
    sensor.secret could otherwise point a helper at it and read the helper's own
    entity instead, which is in scope) and can ACTUATE it (switch_as_x wraps a
    switch so the new entity turns the old one on), and nothing in the serialized
    schema says which of the two a given field does. The failure direction is
    refusing a legitimate edit rather than handing over a scope escape.
    """
    return sorted(
        eid for eid in _entities_in_options(options)
        if resolve(eid, token, hass) != Permission.WRITE
    )


def _config_entry_options_precheck(
    options: Any, entry: Any, token: TokenRecord, hass: HomeAssistant, tool: str
) -> tuple[dict, str, str] | None:
    """Rule 29: refuse a doomed reconfigure before an approval exists."""
    if not isinstance(options, dict) or not options:
        return _tool_error("options must be a non-empty object of the settings to apply."), "invalid_request", tool
    if not entry.supports_options:
        return (
            _tool_error("This helper does not expose an options flow, so its settings cannot be changed here."),
            "invalid_request", tool,
        )
    denied = _unwritable_option_entities(options, token, hass)
    if denied:
        return (
            _tool_error(
                f"These entities are outside this token's write scope: {', '.join(denied)}. "
                "A helper exposes and can actuate the entity it points at, so it may only be "
                "pointed at entities this token could already control."
            ),
            "denied", tool,
        )
    return None


async def _build_diff_set_config_entry_options(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> dict:
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    before = dict(entry.options) if entry is not None else {}
    after = dict_arg(args.get("options"))
    label = (entry.title if entry is not None else None) or str(args.get("entry_id"))
    # The keys the write actually changes, computed from the payload rather than
    # taken on trust, so the History line an admin reads cannot misreport itself.
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return {
        "kind": "config_diff",
        **_summary("config_entry.options", label=label, keys=", ".join(changed) or "(nothing)"),
        "target": {"type": "config_entry", "id": args.get("entry_id"), "label": label},
        "before": _truncate(json.dumps(before, indent=2, default=str)),
        "after": _truncate(json.dumps(after, indent=2, default=str)),
        "preview": {
            "domain": entry.domain if entry is not None else None,
            "changed_keys": changed,
            "warning": "Replaces this helper's settings; the options flow validates them and reloads it.",
        },
    }


async def _tool_set_config_entry_options(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: change one helper config entry's settings (Confirm-gated).

    The tool that lets an agent finish a migration. When the entity a helper was
    built on goes away, nothing else on this surface can repoint it: the helper
    keeps existing, keeps its own entity, and quietly produces nothing.

    `options` REPLACES the entry's options, because that is what an options flow
    does; read them first with get_config_entry_options and send the whole set
    back with your change applied.
    """
    tool = "set_config_entry_options"
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    pre = _config_entry_options_precheck(args.get("options"), entry, token, hass, tool)
    if pre is not None:
        return pre
    conflict = _cas_conflict(args.get("expected_hash"), dict(entry.options), tool)
    if conflict is not None:
        return conflict
    blocked = await _gate(
        "cap_helper_write", token, hass, data,
        tool_name=tool, args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_config_entry_options(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_config_entry_options(args, token, hass, data)


async def _execute_set_config_entry_options(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    tool = "set_config_entry_options"
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    options = args.get("options")
    # Re-validated at apply time, not only pre-gate: the entry, the token's tree
    # and the entities named can all move while an approval waits.
    pre = _config_entry_options_precheck(options, entry, token, hass, tool)
    if pre is not None:
        return pre
    before = dict(entry.options)
    conflict = _cas_conflict(args.get("expected_hash"), before, tool)
    if conflict is not None:
        return conflict

    flow_id = None
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        flow_id = result.get("flow_id")
        result = await hass.config_entries.options.async_configure(
            flow_id, dict(options) if isinstance(options, dict) else {})
    except vol.Invalid as err:
        await _abort_flow(hass, flow_id)
        return _tool_error(f"The settings were rejected: {err}"), "invalid_request", tool
    except Exception:  # noqa: BLE001 - a flow failure is not a Phoenix bug, but must not leak
        _LOGGER.exception("Options flow failed for %s", entry.entry_id)
        await _abort_flow(hass, flow_id)
        return _tool_error("Could not apply these settings."), "invalid_request", tool

    if result.get("type") != FlowResultType.CREATE_ENTRY:
        # Either the flow wants another step, or it refused with errors. Neither
        # is applied, and a multi-step flow is not something one call can drive.
        await _abort_flow(hass, flow_id)
        errors = result.get("errors") or {}
        if errors:
            detail = ", ".join(f"{field}: {msg}" for field, msg in errors.items())
            message = f"The settings were rejected ({detail}). Nothing was changed."
        else:
            message = (
                "Nothing was changed. This helper's options flow asks for more than one "
                "step, which this tool cannot drive; change it in the Home Assistant UI."
            )
        return _tool_error(message), "invalid_request", tool

    # Re-read rather than trusting the payload: the flow's own validators can
    # normalise what they were given (threshold sets absent bounds to None).
    applied = hass.config_entries.async_get_entry(entry.entry_id)
    after = dict(applied.options) if applied is not None else dict_arg(options)
    await _record_version(
        data, token, resource_type="config_entry", resource_id=entry.entry_id,
        action="edit", before=before, after=after, alias=entry.title,
        summary=_version_summary("config_entry.options", subject=entry.title),
    )
    return (
        _tool_success(json.dumps({
            "entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title,
            "options": after, "content_hash": content_hash(after),
            "note": "The helper reloaded with these settings; no restart is needed.",
        }, default=str)),
        "allowed", f"config_entry:{entry.entry_id}",
    )
