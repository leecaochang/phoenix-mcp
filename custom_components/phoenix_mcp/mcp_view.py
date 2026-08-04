"""MCP Streamable HTTP endpoint for the Phoenix MCP integration."""

from __future__ import annotations

import asyncio
import functools
import dataclasses
import json
import logging
import time
from datetime import timedelta
from typing import Any, cast

import voluptuous as vol
from aiohttp import web

from .view_base import PhoenixView
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.dt import utcnow

from .audit import generate_request_id
from .const import AGENTCLI_CLIENT_IP, AI_TASK_CLIENT_IP, ASSIST_CLIENT_IP, PHOENIX_VERSION, BLOCKED_DOMAINS, VOICE_AGENT_CLIENT_IP, CAP_ALLOW, CAP_CONFIRM, CAP_DENY, CAPABILITY_NAMES, DOMAIN, DOMAIN_IMPORTANT_ATTRIBUTES, DUAL_GATE_SERVICES, LEAN_ALWAYS_ATTRS, HIGH_RISK_DOMAINS, MCP_DISCOVER_TTL_MS, MCP_PROTOCOL_VERSION_PREFERRED, MCP_PROTOCOL_VERSIONS, MCP_SSE_KEEPALIVE_SECONDS, MAX_BATCH_APPROVALS, MAX_BATCH_ITEMS, MAX_HISTORY_RANGE_DAYS, MAX_LOG_ENTRIES, MAX_SUBSCRIPTION_SECONDS, MAX_TOOL_NAME_LENGTH, MESA_APPROVED_EXECUTOR, MESA_MODE_OFF, NO_TARGET_SERVICES, PROXY_TIMEOUT_SECONDS
from .data import PhoenixData
from .mesa import async_apply_mesa_to_call, fire_mesa_blocked_event
from .ws_dispatch import WsDispatchError, async_ws_command
from .mesa_tools import MESA_TOOL_NAMES, async_call_mesa_tool, mesa_tool_defs
from .helpers import build_error_response as _error, build_permitted_states as _build_permitted_states, build_safe_config, collect_log_entries as _collect_log_entries, content_hash, diff_summary_fields as _summary, effective_cap, effective_caps, async_get_authenticated_token as _async_get_authenticated_token, get_client_ip as _get_client_ip, log_request as _log, parse_time_param as _parse_time_param, render_template_for_token as _render_template_for_token, sanitize_service_data as _sanitize_service_data, service_not_found_hint as _service_not_found_hint, str_arg, SystemLogUnavailableError, token_has_write_scope, validation_error_message as _validation_error_message
# Shared tool primitives live in tool_common (extracted so per-domain tool
# modules can use them without importing this transport module).
# Per-domain tools live under tools/; this module keeps the transport, the
# dispatch registry and the executor registry, and imports the names it
# registers or calls. The dependency runs one way: nothing under tools/
# imports this module.
from .tools.authoring import _execute_create_automation, _execute_create_scene, _execute_create_script, _execute_delete_automation, _execute_delete_scene, _execute_delete_script, _execute_edit_automation, _execute_edit_scene, _execute_edit_script, _tool_create_automation, _tool_create_scene, _tool_create_script, _tool_delete_automation, _tool_delete_scene, _tool_delete_script, _tool_edit_automation, _tool_edit_scene, _tool_edit_script, _tool_get_automation, _tool_get_automation_traces, _tool_get_scene, _tool_get_script, _tool_list_automations, _tool_list_scenes, _tool_list_scripts
from .tools.config_files import (
    _execute_patch_yaml_config,
    _execute_set_yaml_config,
    _execute_write_file,
    _tool_get_yaml_config,
    _tool_list_files,
    _tool_read_file,
    _tool_patch_yaml_config,
    _tool_set_yaml_config,
    _tool_write_file,
)
from .tools.helper import (
    _execute_create_helper,
    _execute_delete_helper,
    _execute_edit_helper,
    _read_helper_config,
    _tool_create_helper,
    _tool_delete_helper,
    _tool_edit_helper,
    _tool_list_helpers,
)
from .tools.radio import (
    _execute_permit_zigbee_join,
    _execute_reconfigure_zigbee_device,
    _execute_remove_zigbee_device,
    _tool_get_radio_device,
    _tool_get_radio_network,
    _tool_permit_zigbee_join,
    _tool_reconfigure_zigbee_device,
    _tool_remove_zigbee_device,
)
from .tools.energy import (
    _execute_edit_energy_config,
    _tool_edit_energy_config,
    _tool_get_energy_config,
    _tool_get_solar_forecast,
    async_restore_energy_prefs,
)
from .tools.discovery import _requires_satisfied, _requires_unavailable_reason, _tool_check_config, _tool_compare_state, _tool_describe_area, _tool_describe_entity, _tool_dry_run_service, _tool_find_available_actions, _tool_get_audit_summary, _tool_get_device, _tool_get_overview, _tool_get_relationships, _tool_get_system_health, _tool_list_areas, _tool_list_devices, _tool_list_floors, _tool_list_zones, _tool_recent_activity, _tool_search_entities, _tool_validate_config, _tool_whatif
from .tools.native import (
    _UNTRUSTED_DATA_BOUNDARY,
    _build_live_context,
    _execute_hass_set_position,
    _execute_hass_stop_moving,
    _execute_hass_turn_off,
    _execute_hass_turn_on,
    _tool_get_date_time,
    _tool_get_live_context,
    _tool_hass_broadcast,
    _tool_hass_cancel_all_timers,
    _tool_hass_climate_set_temperature,
    _tool_hass_fan_set_speed,
    _tool_hass_light_set,
    _tool_hass_media_next,
    _tool_hass_media_pause,
    _tool_hass_media_player_mute,
    _tool_hass_media_player_unmute,
    _tool_hass_media_previous,
    _tool_hass_media_search_and_play,
    _tool_hass_media_unpause,
    _tool_hass_set_position,
    _tool_hass_set_volume,
    _tool_hass_set_volume_relative,
    _tool_hass_stop_moving,
    _tool_hass_vacuum_clean_area,
    _tool_hass_vacuum_return_to_base,
    _tool_hass_vacuum_start,
    _tool_hass_turn_off,
    _tool_hass_turn_on,
)
from .tools.blueprint import (
    _execute_create_blueprint,
    _execute_delete_blueprint,
    _execute_edit_blueprint,
    _read_blueprint_source,
    _resolve_blueprint_path,
    _tool_create_blueprint,
    _tool_delete_blueprint,
    _tool_edit_blueprint,
    _tool_get_blueprint,
    _tool_list_blueprints,
)
from .tools.lovelace import (
    _execute_add_dashboard_card,
    _execute_create_dashboard,
    _execute_delete_dashboard,
    _execute_delete_dashboard_card,
    _execute_patch_dashboard,
    _execute_edit_dashboard,
    _execute_edit_dashboard_card,
    _execute_set_dashboard_config,
    _tool_create_dashboard,
    _tool_dashboard_card,
    _tool_delete_dashboard,
    _tool_edit_dashboard,
    _tool_get_dashboard_config,
    _tool_list_dashboard_cards,
    _tool_patch_dashboard,
    _tool_list_dashboards,
    _tool_set_dashboard_config,
)
from .tools.esphome import _ESPHOME_DOMAIN, _execute_delete_esphome_yaml, _execute_install_esphome_firmware, _execute_rename_esphome_device, _execute_set_esphome_yaml, _tool_cancel_esphome_job, _tool_clean_esphome_build, _tool_compile_esphome_firmware, _tool_decode_esphome_backtrace, _tool_delete_esphome_yaml, _tool_get_esphome_automations, _tool_get_esphome_board, _tool_get_esphome_component, _tool_get_esphome_device_logs, _tool_get_esphome_job, _tool_get_esphome_overview, _tool_get_esphome_yaml, _tool_install_esphome_firmware, _tool_rename_esphome_device, _tool_set_esphome_yaml, _tool_validate_esphome_yaml, _tool_wait_for_esphome_job
# The published tool catalog (schemas + MCP annotations) is declarative data
# and lives in tool_defs.py; this module reads it to answer tools/list, build
# the per-token gate map, and compute the catalog fingerprint. Re-exported
# here, so mcp_view._SYSTEM_TOOL_DEFS and friends keep resolving.
from .tool_defs import (
    _ENTITY_TOOL_DEFS,
    _NATIVE_TOOL_DEFS,
    _SYSTEM_TOOL_DEFS,
    _TOOL_ANNOTATIONS,
)
from .audit import Outcome
from .tool_common import _CAP_FORBIDDEN_MESSAGE, _resolve_area_id, _ProgressBus, _approved_exec_ctx, _approval_resource, _gate, _mesa_advisory_ctx, _mesa_confirm_annotation, _operator_accepted_result, _pending_or_inline, _progress_ctx, _record_version, _restore_ctx, _set_progress_status, _tool_error, _tool_success
from .policy_engine import (EntityCreationNotPermitted, Permission, call_needs_physical_gate, esphome_entry_writable, filter_entities_for_token, filter_service_response, get_effective_hint, resolve, resolve_esphome_user_service, resolve_service_targets, scrub_sensitive_attributes, scrub_state_dict as _scrub_state_dict)
from .rate_limiter import RateLimitResult
from .token_store import TokenRecord
from . import yaml_includes

_LOGGER = logging.getLogger(__name__)

# What this server can do, answered identically by initialize and
# server/discover. One definition because the two are the same claim made to
# two eras of client, and a client that reads only one of them must not learn
# something different from the one that reads the other.
#
# listChanged and subscribe are FALSE on purpose, not as a placeholder: this
# transport is stateless and holds no connection to push a notification down.
# The tool list moving under a connected client is instead reported in-band, on
# that client's next call, by the staleness advisories.
_SERVER_CAPABILITIES: dict[str, dict] = {
    "tools": {"listChanged": False},
    "resources": {"subscribe": False},
    "prompts": {},
}

# Stamped onto the defs at import: a tool added without an annotation raises
# KeyError here rather than shipping with the spec's unsafe defaults.
for _def in (*_ENTITY_TOOL_DEFS, *_NATIVE_TOOL_DEFS, *_SYSTEM_TOOL_DEFS):
    _def["annotations"] = _TOOL_ANNOTATIONS[_def["name"]]
del _def


def _catalog_fingerprint() -> str:
    """Hash of every tool NAME and inputSchema this build defines.

    The second staleness signal, alongside settings_version. That one tracks the
    operator changing a token; this one tracks the CODE changing underneath a
    client that already fetched its tool list, which the transport cannot push
    and settings_version never notices. A client enforces its cached schema
    before sending, so an argument added after that fetch is unusable to it: the
    omission is enforced on ITS side, which no amount of server-side leniency
    reaches, and advising a reconnect is the only remedy.

    Hashes the FULL static catalog, never the per-token announced subset, so a
    permission change does not masquerade as a deploy (settings_version already
    covers that and prescribes a different fix). Descriptions are deliberately
    excluded: reworded guidance is not something a client must refetch to keep
    calling tools correctly, so treating prose edits as staleness would fire the
    notice constantly and train operators to ignore it.
    """
    catalog = [
        (d["name"], d.get("inputSchema"))
        for d in (*_ENTITY_TOOL_DEFS, *_NATIVE_TOOL_DEFS, *_SYSTEM_TOOL_DEFS, *mesa_tool_defs())
    ]
    return content_hash(sorted(catalog, key=lambda row: row[0]))[:16]


_TOOL_CATALOG_FINGERPRINT = _catalog_fingerprint()


def tool_catalog_counts() -> dict[str, int]:
    """The tool count, from the registry itself. The ONLY place it is defined.

    Every other statement of the count derives from here: the docs-count test,
    the admin info endpoint, and scripts/count_tools.py. Native means HA's own
    MCP tool names (Hass*/Get*, uppercase-initial), which Phoenix implements
    1:1; additional is everything Phoenix defines beyond them, mesa_* included.
    """
    names = {
        d["name"]
        for d in (*_ENTITY_TOOL_DEFS, *_NATIVE_TOOL_DEFS, *_SYSTEM_TOOL_DEFS, *mesa_tool_defs())
    }
    native = {n for n in names if n[0].isupper()}
    return {"total": len(names), "native": len(native), "additional": len(names - native)}


# Tools that perform writes/actions. They are announced in tools/list only when
# the token has write scope (any GREEN grant or pass_through). Cap-tied tools
# (those with a "cap" key) gate on their capability instead; all remaining tools
# (reads, GetDateTime, get_approval_status) are always announced. The
# announce_all_tools token flag overrides this gating entirely.
_WRITE_GATED_TOOLS = frozenset({
    "call_service",
    "HassTurnOn", "HassTurnOff", "HassLightSet", "HassFanSetSpeed",
    "HassClimateSetTemperature", "HassSetPosition", "HassSetVolume",
    "HassSetVolumeRelative", "HassMediaPause", "HassMediaUnpause",
    "HassMediaNext", "HassMediaPrevious", "HassMediaSearchAndPlay",
    "HassMediaPlayerMute", "HassMediaPlayerUnmute", "HassCancelAllTimers",
    "HassStopMoving",
    "HassVacuumStart", "HassVacuumReturnToBase", "HassVacuumCleanArea",
})


def _tool_is_announced(
    tool_def: dict, token: TokenRecord, has_write: bool, hass: HomeAssistant
) -> bool:
    """Whether a tool should appear in tools/list for this token.

    cap-tied tools gate on their capability; write/action tools gate on write
    scope; a tool carrying a "requires" key additionally needs that system
    surface to exist; everything else (reads, GetDateTime, get_approval_status)
    is always announced. The caller applies the announce_all_tools override
    separately.

    THREE MIRRORS: this predicate, _tool_gate_map (get_capability_summary), and
    agentcli.build_mcp_tool_list (Agent Chat, Assist, voice, AI Task) each decide
    announcement independently. Changing the rules here means changing all three.
    """
    cap = tool_def.get("cap")
    if cap is not None and effective_cap(token, cap) == CAP_DENY:
        return False
    if cap is None and tool_def["name"] in _WRITE_GATED_TOOLS and not has_write:
        return False
    return _requires_satisfied(tool_def, hass)


def _tool_gate_map(
    token: TokenRecord, data: PhoenixData, hass: HomeAssistant
) -> dict[str, Any]:
    """Classify every tool by how it would behave for this token, at the tool level.

    Buckets (token's own data, no entity oracle):
      usable        - announced and executes directly.
      needs_approval - cap-tied tool whose cap is Confirm AND which actually gates:
                      returns pending_approval.
      unavailable   - not usable (cap denied, a write/action tool without write
                      scope, or a tool whose required system surface is absent).

    THE `_EXECUTOR_REGISTRY` CHECK IS LOAD-BEARING, not a belt-and-braces extra.
    A Confirm cap does NOT mean every tool tied to it gates: a cap-tied READ
    (get_automation, get_dashboard_config, get_yaml_config, read_file, and 50-odd
    others) checks `effective_cap(...) == CAP_DENY` and never calls `_gate`, so it
    executes directly no matter what mode the cap is in. Bucketing by cap mode
    alone therefore reported every one of those as needing approval. That is not
    a cosmetic mislabel: this map IS `get_capability_summary`, the call the server
    instructions tell an agent to make FIRST to orient, so it mis-plans the whole
    session downstream. Live-found when an agent avoided free reads for an entire
    migration because the summary said they would queue for approval.

    Registering an executor is the right proxy because it is exactly what the
    Confirm flow requires: `async_execute_approved_tool` dispatches through
    `_EXECUTOR_REGISTRY`, so a tool that gates without one could never be applied
    after approval. `tests/test_mcp_tool_catalog.py` pins the registry against the
    handlers that really call `_gate`, in both directions, so the proxy cannot
    drift into a lie of its own.

    A tool unavailable because the system lacks its surface (no ESPHome, no
    Device Builder) also gets an entry in unavailable_reasons, which is emitted
    only when non-empty. That distinction matters to an agent: a denied
    capability is something the operator can grant, a missing add-on is not.

    This is a static, tool-level view. call_service and the native Hass* action
    tools appear "usable" when the token has write scope even though a specific
    target may still hit a physical/dual gate or MESA confirm at call time; use
    dry_run_service to preview an individual call. Mirrors _tool_is_announced so
    the summary and tools/list agree.
    """
    has_write = token_has_write_scope(token)
    mesa_defs = mesa_tool_defs() if data.mesa is not None else []
    usable: list[str] = []
    needs_approval: list[str] = []
    unavailable: list[str] = []
    reasons: dict[str, str] = {}
    for tool_def in list(_ENTITY_TOOL_DEFS) + list(_NATIVE_TOOL_DEFS) + list(_SYSTEM_TOOL_DEFS) + mesa_defs:
        name = tool_def["name"]
        cap = tool_def.get("cap")
        if not _requires_satisfied(tool_def, hass):
            unavailable.append(name)
            reasons[name] = _requires_unavailable_reason(tool_def)
        elif cap is not None:
            mode = effective_cap(token, cap)
            if mode == CAP_DENY:
                unavailable.append(name)
            elif mode == CAP_CONFIRM and name in _EXECUTOR_REGISTRY:
                needs_approval.append(name)
            else:
                usable.append(name)
        elif name in _WRITE_GATED_TOOLS:
            (usable if has_write else unavailable).append(name)
        else:
            usable.append(name)
    result: dict[str, Any] = {
        "usable": sorted(usable),
        "needs_approval": sorted(needs_approval),
        "unavailable": sorted(unavailable),
    }
    if reasons:
        result["unavailable_reasons"] = dict(sorted(reasons.items()))
    return result


def _jsonrpc_result(msg_id: Any, result: Any) -> dict:
    """Wrap a result in a JSON-RPC 2.0 success envelope."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _sanitize_jsonrpc_id(raw_id: Any) -> str | int | None:
    """Coerce a JSON-RPC id to a valid type (string, number, or null).

    JSON-RPC 2.0 requires id to be a string, number, or null. If the client
    sends a dict, list, or other non-conforming type, coerce to None rather
    than echoing it back.
    """
    if raw_id is None or isinstance(raw_id, (str, int, float)):
        return raw_id  # type: ignore[return-value]  # float is accepted on the wire; see the id contract above
    return None


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict:
    """Wrap an error in a JSON-RPC 2.0 error envelope."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _classify_jsonrpc_message(body: dict) -> tuple[str, Any]:
    """Validate one JSON-RPC message envelope (jsonrpc == "2.0" already checked).

    A malformed client (or a browser cross-origin probe) can send a `params`
    that is not an object or a `method` that is not a string; the tools/call
    dispatcher does `params.get(...)` and would raise before the per-call
    exception net, escaping to an aiohttp 500 rather than a clean JSON-RPC error.
    This gate runs before dispatch in BOTH the single-request and batch paths so
    the shape is validated once. Returns one of:
      ("dispatch", (method, params)) - a well-formed request to hand to _dispatch_mcp.
      ("accepted", None)             - a JSON-RPC response/notification object (no
                                       method, carries result/error): accept with no
                                       reply (HTTP 202 / no batch entry), per the MCP
                                       Streamable HTTP transport.
      ("unsupported", (code, message)) - a STRUCTURALLY VALID request Phoenix MCP cannot
                                       dispatch (positional/array params). A caller
                                       must error a real request but STAY SILENT for
                                       a notification, since the envelope is valid.
      ("error", (code, message))     - a malformed envelope; the caller emits the
                                       JSON-RPC error with the message's own id.
    """
    method = body.get("method")
    # A response/notification object carries result/error and no method: the MCP
    # transport says accept these (no reply), rather than answering "method not found".
    if method is None and ("result" in body or "error" in body):
        return "accepted", None
    if not isinstance(method, str) or not method:
        return "error", (-32600, "Invalid Request.")
    raw_params = body.get("params")
    # JSON-RPC allows params to be an Array (positional) or Object (by-name). Phoenix MCP
    # only dispatches by-name (its handlers index params as a dict). A list is thus
    # STRUCTURALLY VALID but unsupported ("unsupported"), which matters because a
    # valid positional-params NOTIFICATION must still get no response; a scalar is
    # not a structured value at all, so it is a malformed envelope ("error").
    if raw_params is not None and not isinstance(raw_params, dict):
        if isinstance(raw_params, list):
            return "unsupported", (-32602, "Positional parameters are not supported.")
        return "error", (-32602, "Invalid params.")
    return "dispatch", (method, raw_params or {})



















# One-time in-band advisory appended to a tools/call response when this token's
# settings changed after its last tools/list (the stateless transport cannot
# push notifications/tools/list_changed). Once per staleness epoch.
_STALE_TOOLS_ADVISORY = (
    "Notice: this token's permissions or settings were changed by the operator "
    "after your client last fetched the tool list, so your tool list and "
    "capability summary may be stale. Call get_capability_summary for the "
    "current state and ask the user to reconnect or refresh this MCP server's "
    "tools."
)

# The deploy-side sibling of the advisory above. Deliberately does NOT mention
# get_capability_summary: that reports permissions, which have not changed here,
# and following it would waste a call and answer the wrong question. What changed
# is the SCHEMAS, and only a reconnect can fix that, because a client enforces
# its own cached schema before the request is ever sent.
_STALE_CATALOG_ADVISORY = (
    "Notice: Phoenix MCP was updated after your client last fetched the tool "
    "list, so some tools may have gained or changed arguments your copy of their "
    "schemas does not have. If a call fails on an argument you believe is valid, "
    "that is the likely cause. Ask the user to reconnect or refresh this MCP "
    "server's tools; re-reading the capability summary will not fix it."
)









def _normalize_fields(fields: Any) -> list[str]:
    """Coerce the get_state/get_states `fields` arg to a list of field names.

    Accepts a list of strings or a single comma-separated string (a common agent
    mistake); anything else yields an empty list, which selects the lean default.
    """
    if isinstance(fields, str):
        return [f for f in (p.strip() for p in fields.split(",")) if f]
    if isinstance(fields, list):
        return [str(f).strip() for f in fields if str(f).strip()]
    return []


def _select_state_fields(scrubbed: dict, fields: list[str]) -> dict:
    """Return only the requested fields from an already-scrubbed state dict.

    Top-level keys (state, last_changed, last_updated, ...) are taken as-is;
    'attr.<name>' selects one attribute; 'attributes' selects all (scrubbed)
    attributes. entity_id is always included. Unknown fields are ignored. Runs
    AFTER scrubbing, so a scrubbed attribute can never be selected back in.
    """
    attrs = scrubbed.get("attributes", {})
    out: dict = {"entity_id": scrubbed.get("entity_id")}
    picked_attrs: dict = {}
    for f in fields:
        if f == "attributes":
            picked_attrs.update(attrs)
        elif f.startswith("attr."):
            name = f[len("attr."):]
            if name in attrs:
                picked_attrs[name] = attrs[name]
        elif f in scrubbed and f != "attributes":
            out[f] = scrubbed[f]
    if picked_attrs:
        out["attributes"] = picked_attrs
    return out


def _lean_state(scrubbed: dict) -> dict:
    """Return the domain-aware lean view of an already-scrubbed state dict."""
    eid = scrubbed.get("entity_id", "") or ""
    domain = eid.split(".")[0] if "." in eid else ""
    attrs = scrubbed.get("attributes", {})
    keep = set(LEAN_ALWAYS_ATTRS) | set(DOMAIN_IMPORTANT_ATTRIBUTES.get(domain, ()))
    lean_attrs = {k: v for k, v in attrs.items() if k in keep}
    out: dict = {"entity_id": eid, "state": scrubbed.get("state")}
    if lean_attrs:
        out["attributes"] = lean_attrs
    return out


def _project_state(scrubbed: dict, fields: Any, detailed: bool) -> dict:
    """Apply presentation-only field projection to a scrubbed state dict.

    detailed -> full dict; explicit fields -> requested subset; otherwise the
    domain-aware lean view. Never bypasses scrubbing (the input is already scrubbed).
    """
    if detailed:
        return scrubbed
    norm = _normalize_fields(fields)
    if norm:
        return _select_state_fields(scrubbed, norm)
    return _lean_state(scrubbed)


async def _tool_get_state(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: return the current state of a single entity."""
    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "denied", "get_state"

    perm = resolve(entity_id, token, hass)
    if perm == Permission.NOT_FOUND:
        return _tool_error("Entity not found."), "not_found", entity_id
    if perm in (Permission.NO_ACCESS, Permission.DENY):
        return _tool_error("Entity not found."), "denied", entity_id

    state = hass.states.get(entity_id)
    if state is None:
        return _tool_error("Entity not found."), "not_found", entity_id

    scrubbed = scrub_sensitive_attributes(state)
    result = _project_state(scrubbed, args.get("fields"), bool(args.get("detailed")))
    return _tool_success(json.dumps(result, default=str)), "allowed", entity_id


async def _tool_get_states(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: return all entities accessible to the token."""
    states = hass.states.async_all()
    filtered = filter_entities_for_token(states, token, hass)
    fields = args.get("fields")
    detailed = bool(args.get("detailed"))
    projected = [_project_state(d, fields, detailed) for d in filtered]
    return _tool_success(json.dumps(projected, default=str)), "allowed", "get_states"


async def _tool_get_history(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: fetch state history for a single permitted entity."""
    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "denied", "get_history"

    perm = resolve(entity_id, token, hass)
    if perm == Permission.NOT_FOUND:
        return _tool_error("Entity not found."), "not_found", entity_id
    if perm in (Permission.NO_ACCESS, Permission.DENY):
        return _tool_error("Entity not found."), "denied", entity_id

    mode = str(args.get("mode") or "transitions").strip().lower()
    if mode not in ("transitions", "raw"):
        mode = "transitions"

    start_time_raw = args.get("start_time", "")
    if not start_time_raw:
        return _tool_error("Missing required argument: start_time"), "denied", entity_id

    try:
        start_time = _parse_time_param(start_time_raw)
    except ValueError:
        return _tool_error("Invalid start_time format."), "denied", entity_id

    end_time = None
    end_time_raw = args.get("end_time")
    if end_time_raw:
        try:
            end_time = _parse_time_param(end_time_raw)
        except ValueError:
            return _tool_error("Invalid end_time format."), "denied", entity_id

    effective_end = end_time or utcnow()
    max_start = effective_end - timedelta(days=MAX_HISTORY_RANGE_DAYS)
    if start_time < max_start:
        start_time = max_start

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder import history as rec_history

        fn = functools.partial(
            rec_history.get_significant_states,
            hass,
            start_time,
            end_time,
            [entity_id],
            None,
            False,
            True,
            False,
            False,
        )
        result = await get_instance(hass).async_add_executor_job(fn)
    except Exception:
        _LOGGER.warning("MCP history call failed for entity %s", entity_id, exc_info=True)
        return _tool_error("History call failed."), "denied", entity_id

    states_list = result.get(entity_id, [])
    dicts = [s.as_dict() if hasattr(s, "as_dict") else s for s in states_list]

    if mode == "raw":
        history = [_scrub_state_dict(d) for d in dicts]
    else:
        # Transitions: one entry per state-value change, dropping attribute noise
        # and consecutive duplicates. Far more compact than the raw per-sample dump.
        history = []
        last_state = None
        for d in dicts:
            state_val = d.get("state")
            if state_val == last_state:
                continue
            history.append({"state": state_val, "when": d.get("last_changed") or d.get("last_updated")})
            last_state = state_val

    body = {"entity_id": entity_id, "mode": mode, "count": len(history), "history": history}
    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


async def _tool_get_statistics(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: fetch long-term statistics for a single permitted entity."""
    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "denied", "get_statistics"

    perm = resolve(entity_id, token, hass)
    if perm == Permission.NOT_FOUND:
        return _tool_error("Entity not found."), "not_found", entity_id
    if perm in (Permission.NO_ACCESS, Permission.DENY):
        return _tool_error("Entity not found."), "denied", entity_id

    start_time_raw = args.get("start_time", "")
    if not start_time_raw:
        return _tool_error("Missing required argument: start_time"), "denied", entity_id

    try:
        start_time = _parse_time_param(start_time_raw)
    except ValueError:
        return _tool_error("Invalid start_time format."), "denied", entity_id

    end_time = None
    end_time_raw = args.get("end_time")
    if end_time_raw:
        try:
            end_time = _parse_time_param(end_time_raw)
        except ValueError:
            return _tool_error("Invalid end_time format."), "denied", entity_id

    effective_end = end_time or utcnow()
    max_start = effective_end - timedelta(days=MAX_HISTORY_RANGE_DAYS)
    if start_time < max_start:
        start_time = max_start

    period = args.get("period", "hour")
    if period not in ("5minute", "hour", "day", "week", "month"):
        return _tool_error("Invalid period. Must be one of: 5minute, hour, day, week, month."), "denied", entity_id

    valid_types = {"mean", "min", "max", "sum", "state", "change"}
    raw_types = args.get("statistic_types")
    type_set: set | None = None
    if raw_types:
        type_set = {t for t in raw_types if t in valid_types} or None

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder import statistics as recorder_stats

        fn = functools.partial(
            recorder_stats.statistics_during_period,
            hass,
            start_time,
            end_time,
            {entity_id},
            period,
            None,
            # types became non-optional in HA 2026.4; default to all types when not specified.
            type_set or {"mean", "min", "max", "sum", "state", "change"},
        )
        result = await get_instance(hass).async_add_executor_job(fn)
    except Exception:
        _LOGGER.warning("MCP statistics call failed for entity %s", entity_id, exc_info=True)
        return _tool_error("Statistics call failed."), "denied", entity_id

    return _tool_success(json.dumps(result, default=str)), "allowed", entity_id


async def _tool_get_calendar_events(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list events from one accessible calendar entity (entity-scoped)."""
    calendar_id = str(args.get("calendar_id") or args.get("entity_id") or "").strip()
    if not calendar_id:
        return _tool_error("Missing required argument: calendar_id"), "invalid_request", "get_calendar_events"

    perm = resolve(calendar_id, token, hass)
    if perm == Permission.NOT_FOUND:
        return _tool_error("Calendar not found."), "not_found", calendar_id
    if perm in (Permission.NO_ACCESS, Permission.DENY):
        return _tool_error("Calendar not found."), "denied", calendar_id
    if not calendar_id.startswith("calendar."):
        return _tool_error("Not a calendar entity."), "invalid_request", calendar_id

    start_time = utcnow()
    if args.get("start_time"):
        try:
            start_time = _parse_time_param(args["start_time"])
        except ValueError:
            return _tool_error("Invalid start_time format."), "invalid_request", calendar_id
    if args.get("end_time"):
        try:
            end_time = _parse_time_param(args["end_time"])
        except ValueError:
            return _tool_error("Invalid end_time format."), "invalid_request", calendar_id
    else:
        end_time = start_time + timedelta(days=7)
    if end_time <= start_time:
        return _tool_error("end_time must be after start_time."), "invalid_request", calendar_id

    try:
        # calendar.get_events is SupportsResponse.ONLY, so return_response=True is
        # required here (the usual Phoenix MCP rule against it is for response-less services).
        resp = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": calendar_id,
             "start_date_time": start_time.isoformat(),
             "end_date_time": end_time.isoformat()},
            blocking=True, return_response=True,
        )
    except Exception:
        _LOGGER.debug("calendar get_events failed for %s", calendar_id, exc_info=True)
        return _tool_error("Failed to read calendar events."), "invalid_request", calendar_id

    entry = resp.get(calendar_id, {}) if isinstance(resp, dict) else {}
    events = entry.get("events", []) if isinstance(entry, dict) else []
    return _tool_success(json.dumps({"calendar_id": calendar_id, "events": events}, default=str)), "allowed", calendar_id


async def _tool_call_service(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: call a HA service with entity targets filtered to WRITE-permitted entities."""
    # Coerced before anything gates on them: service_key and the physical-gate
    # domain check are set-membership tests, so a list-valued domain raises
    # instead of being refused.
    domain = str_arg(args.get("domain"))
    service = str_arg(args.get("service"))
    if not domain or not service:
        return _tool_error("Missing required arguments: domain and service"), "denied", "call_service"

    service_key = f"{domain}/{service}"

    if service_key in DUAL_GATE_SERVICES:
        blocked = await _gate(
            "cap_restart", token, hass, data,
            tool_name="call_service", args=args, request_id=request_id,
            client_ip=client_ip, diff=lambda: _build_diff_call_service(args, token, hass),
        )
        if blocked is not None:
            return blocked
    elif call_needs_physical_gate(
        domain=domain, service=service,
        entity_id=args.get("entity_id"), device_id=args.get("device_id"),
        area_id=args.get("area_id"), token=token, hass=hass,
    ):
        blocked = await _gate(
            "cap_physical_control", token, hass, data,
            tool_name="call_service", args=args, request_id=request_id,
            client_ip=client_ip, diff=lambda: _build_diff_call_service(args, token, hass),
        )
        if blocked is not None:
            return blocked
    elif service_key in NO_TARGET_SERVICES:
        blocked = await _gate(
            "cap_yaml_edit", token, hass, data,
            tool_name="call_service", args=args, request_id=request_id,
            client_ip=client_ip, diff=lambda: _build_diff_call_service(args, token, hass),
        )
        if blocked is not None:
            return blocked
    return await _execute_call_service(
        args, token, hass, data, request_id=request_id, client_ip=client_ip,
    )


async def _dispatch_no_target_tool_call(
    hass: HomeAssistant,
    token: TokenRecord,
    *,
    domain: str,
    service: str,
    service_data: dict,
    resource: str,
    timeout_noun: str,
    surface_validation_errors: bool,
) -> tuple[dict, Outcome, str]:
    """Call a service that takes NO entity target, and map the outcome.

    The MCP-side twin of proxy_view._dispatch_no_target_call, for the same three
    families: the dual-gate services (no entities in hass.states), the
    config-reload family (whose schemas reject an entity_id), and ESPHome
    user-defined actions (schema built from only the arguments the device
    declared). Each ran its own near-identical copy.

    Authorization has already happened by the time this runs; it decides nothing.

    NOTE the two surfaces deliberately differ on one flag, so do not "unify"
    them by changing a default here. For NO_TARGET_SERVICES, MCP passes
    surface_validation_errors=True (the message describes the caller's own
    reloadable config, post-cap_yaml_edit) while the REST proxy passes False.
    Changing either is a product decision, not a cleanup.
    """
    if domain in HIGH_RISK_DOMAINS:
        _LOGGER.info(
            "High-risk service call %s/%s by token %s",
            domain, service, token.name,
        )
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await hass.services.async_call(
                domain, service, service_data, blocking=True, return_response=False,
            )
    except asyncio.TimeoutError:
        subject = "the device" if timeout_noun == "Action" else "HA"
        return (
            _tool_success(json.dumps({
                "success": True,
                "partial": True,
                "message": f"{timeout_noun} dispatched but {subject} did not respond within the timeout window.",
            })),
            "allowed",
            resource,
        )
    except ServiceNotFound:
        # Generic: never confirm or deny service existence.
        return _tool_error("Forbidden."), "denied", resource
    except (ServiceValidationError, vol.Invalid) as err:
        if not surface_validation_errors:
            return _tool_error("Forbidden."), "denied", resource
        return _tool_error(_validation_error_message(err)), "invalid_request", resource
    except HomeAssistantError:
        return _tool_error("Forbidden."), "denied", resource
    return _tool_success(json.dumps({"success": True})), "allowed", resource


async def _execute_call_service(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    request_id: str = "",
    client_ip: str | None = None,
    mesa_approved: bool = False,
) -> tuple[dict, str, str]:
    # Coerced before anything gates on them: service_key and the physical-gate
    # domain check are set-membership tests, so a list-valued domain raises
    # instead of being refused.
    domain = str_arg(args.get("domain"))
    service = str_arg(args.get("service"))
    if not domain or not service:
        return _tool_error("Missing required arguments: domain and service"), "denied", "call_service"

    resource = f"service:{domain}/{service}"
    service_key = f"{domain}/{service}"

    entity_id = args.get("entity_id")
    device_id = args.get("device_id")
    area_id = args.get("area_id")
    # A target selector inside service_data is stripped rather than honoured: the
    # explicit targets above are what this call may reach, and HA would union a
    # leftover selector with them. See helpers.sanitize_service_data.
    service_data = _sanitize_service_data(args.get("service_data"))

    # DUAL_GATE_SERVICES have no entities in hass.states; routing them through
    # resolve_service_targets always produces an empty list and a spurious 403.
    # The cap_restart gate above is the only permission check required.
    if service_key in DUAL_GATE_SERVICES:
        return await _dispatch_no_target_tool_call(
            hass, token, domain=domain, service=service, service_data=service_data,
            resource=resource, timeout_noun="Service",
            surface_validation_errors=False,
        )

    # NO_TARGET_SERVICES are domain-wide config reloads that take no entity target.
    # The normal path below always attaches an explicit entity_id list (rule 15),
    # which these services' schemas reject, so call them with bare service_data.
    # The cap_yaml_edit gate above is the only permission check required; any
    # entity/device/area the caller passed is ignored (these services have no
    # target semantics). MESA does not apply (no entities to govern).
    if service_key in NO_TARGET_SERVICES:
        # surface_validation_errors=True: post-authorization (cap_yaml_edit is
        # already granted) and the message describes the caller's own reloadable
        # config, not hidden state. The REST proxy deliberately differs here.
        return await _dispatch_no_target_tool_call(
            hass, token, domain=domain, service=service, service_data=service_data,
            resource=resource, timeout_noun="Service",
            surface_validation_errors=True,
        )

    # ESPHome user-defined actions (esphome.<device>_<action>) take NO entity
    # target: HA builds their schema from only the arguments the device declared,
    # so attaching the flattened entity_id list (rule 15) makes voluptuous reject
    # the call outright, and routing them through resolve_service_targets yields
    # an empty list and a spurious 403. Authorization is the owning DEVICE's write
    # scope rather than a capability: the action is defined by that device's own
    # firmware and can only act on it, so a token that may already actuate the
    # device's entities is exactly the token that may invoke its actions. MESA
    # does not apply (no entity target to govern). A service no LOADED entry
    # claims falls through to the normal path and refuses uniformly, so a stale
    # registration left behind by an unload reveals nothing.
    if domain == _ESPHOME_DOMAIN:
        esphome_entry = resolve_esphome_user_service(hass, service)
        if esphome_entry is not None:
            if not esphome_entry_writable(hass, esphome_entry, token):
                return _tool_error("Forbidden."), "denied", resource
            # A device-defined schema makes a wrong argument NAME the likeliest
            # failure, and the caller can fix it, so validation errors surface.
            return await _dispatch_no_target_tool_call(
                hass, token, domain=domain, service=service, service_data=service_data,
                resource=resource, timeout_noun="Action",
                surface_validation_errors=True,
            )

    try:
        permitted_entities, _requested_count = resolve_service_targets(
            entity_id=entity_id,
            device_id=device_id,
            area_id=area_id,
            service_domain=domain,
            token=token,
            hass=hass,
        )
    except EntityCreationNotPermitted:
        return _tool_error("Forbidden."), "denied", resource

    if not permitted_entities:
        return _tool_error("Forbidden."), "denied", resource

    # MESA enforcement runs last, on the flattened entity list Phoenix MCP already
    # permitted (rule 15: never pass device_id/area_id/"all" to HA). MESA never
    # sees entities Phoenix MCP denied; it can drop entities, gate the whole call for
    # confirmation, or block outright.
    mesa_outcome = await async_apply_mesa_to_call(
        hass, data, token,
        domain=domain, service=service, service_data=service_data,
        entities=permitted_entities,
        request_id=request_id, client_ip=client_ip, session_id=request_id,
        confirm_approved=mesa_approved or _approved_exec_ctx.get(),
    )
    if mesa_outcome.blocked:
        fire_mesa_blocked_event(hass, token, mesa_outcome.blocked)
    if mesa_outcome.decision == "pending":
        # Same holding behaviour as a capability confirm (tool_common._gate):
        # one definition, so the two kinds of gate cannot answer differently.
        return await _pending_or_inline(hass, data, token, mesa_outcome.approval)
    if mesa_outcome.decision == "deny":
        return _tool_error("Forbidden."), "denied", resource
    permitted_entities = mesa_outcome.entities

    if domain in HIGH_RISK_DOMAINS:
        _LOGGER.info(
            "High-risk service call %s/%s by token %s",
            domain, service, token.name,
        )

    call_data = dict(service_data)
    call_data["entity_id"] = permitted_entities

    use_return_response = False
    if effective_cap(token, "cap_service_response") != CAP_DENY:
        try:
            from homeassistant.core import SupportsResponse as _SR
            handler = hass.services.async_services().get(domain, {}).get(service)
            use_return_response = (
                handler is not None and
                getattr(handler, "supports_response", None) not in (None, _SR.NONE)
            )
        except Exception:
            _LOGGER.debug(
                "supports_response probe failed for %s/%s; calling without return_response",
                domain, service, exc_info=True,
            )

    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            svc_response = await hass.services.async_call(
                domain,
                service,
                call_data,
                blocking=True,
                return_response=use_return_response,
            )
    except asyncio.TimeoutError:
        return (
            _tool_success(json.dumps({
                "success": True,
                "partial": True,
                "message": "Service dispatched but HA did not respond within the timeout window.",
            })),
            "allowed",
            resource,
        )
    except ServiceNotFound:
        # For most domains, return a generic error: never confirm or deny
        # service existence (no enumeration oracle). But for a curated set of
        # core actuator domains (DOMAIN_SERVICE_HINTS), name the valid core service
        # verbs so an agent that guessed the wrong one (e.g. valve.open instead of
        # valve.open_valve) can self-correct. This is leak-safe: the catch is
        # post-authorization (the token already proved WRITE on an entity in this
        # domain) and the verbs come from a hardcoded public HA-core list, not a
        # live hass.services lookup, so nothing hidden is revealed.
        hint = _service_not_found_hint(domain, service)
        if hint is not None:
            return _tool_error(hint[0]), "invalid_request", resource
        return _tool_error("Forbidden."), "denied", resource
    except ServiceValidationError as err:
        # The token already holds WRITE on this entity and invoked a real service;
        # a validation error is about the caller's own argument (e.g. an
        # out-of-range setpoint), not about hidden entities or services, so it is
        # safe to surface and lets the agent self-correct instead of reading it as
        # a permission denial. (ServiceNotFound subclasses ServiceValidationError
        # but is caught above and stays generic, preserving the no-oracle rule.)
        return _tool_error(_validation_error_message(err)), "invalid_request", resource
    except vol.Invalid as err:
        # hass.services.async_call validates service_data against the target
        # service's own schema with plain voluptuous, and re-raises vol.Invalid/
        # MultipleInvalid unwrapped rather than as a HomeAssistantError (e.g. a
        # service whose schema does not accept entity_id at all, called with a
        # resolved entity target still attached). Not caught by the
        # ServiceValidationError/HomeAssistantError handlers above since
        # vol.Invalid does not subclass either; without this it fell through to
        # the dispatch-level safety net as a bare "Internal error", losing the
        # actionable detail voluptuous provides. Safe to surface for the same
        # reason as ServiceValidationError: describes the caller's own argument
        # shape, not hidden state.
        return _tool_error(_validation_error_message(err)), "invalid_request", resource
    except HomeAssistantError:
        return _tool_error("Forbidden."), "denied", resource

    filtered_response = filter_service_response(svc_response, token, hass) if svc_response is not None else None

    body: dict[str, Any] = {"success": True}
    if filtered_response is not None:
        body["service_response"] = filtered_response
    if mesa_outcome.warnings:
        body["mesa_advisory"] = mesa_outcome.warnings
        _mesa_advisory_ctx.set(True)

    return _tool_success(json.dumps(body, default=str)), "allowed", resource


async def _execute_call_service_mesa_approved(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Re-run a MESA-gated service call after admin approval.

    Registered under MESA_APPROVED_EXECUTOR but never dispatchable from the tool
    router, so a token cannot reach the confirm-approved path itself. Re-runs
    Phoenix MCP scope resolution and MESA evaluation; only control_mode:confirm blocks
    are treated as satisfied, so an entity that became prohibited or read_only
    since the request is still rejected.
    """
    return await _execute_call_service(args, token, hass, data, mesa_approved=True)


async def _tool_get_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: return HA config (requires cap_config_read)."""
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_config"
    return _tool_success(json.dumps(build_safe_config(hass), default=str)), "allowed", "get_config"




async def _tool_get_logs(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read system_log entries (requires cap_log_read)."""
    if effective_cap(token, "cap_log_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_logs"

    raw_level = str(args.get("level") or "WARNING").strip().upper()
    if raw_level not in ("INFO", "WARNING", "ERROR"):
        raw_level = "WARNING"

    integration = str(args.get("integration") or "").strip() or None

    # Default matches _DEFAULT_LOG_LIMIT in proxy_view.py. Both are 50 intentionally;
    # they are not shared via a constant to avoid coupling the two view modules.
    limit = 50
    raw_limit = args.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
            if not (1 <= limit <= MAX_LOG_ENTRIES):
                limit = max(1, min(limit, MAX_LOG_ENTRIES))
        except (TypeError, ValueError):
            limit = 50

    try:
        page = _collect_log_entries(hass, raw_level, integration, limit)
    except SystemLogUnavailableError:
        return _tool_error(
            "The system_log integration is not loaded; logs are unavailable."
        ), "invalid_request", "get_logs"
    # total and truncated, not just count: this is the tool an agent uses to decide
    # whether the instance is healthy, and a clipped page read as the whole log
    # answers that question wrongly in the direction that matters.
    body = {
        "count": len(page.entries),
        "total": page.total,
        "truncated": page.total > len(page.entries),
        "entries": page.entries,
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_logs"


def _logbook_entry_visible(entry: dict, token: TokenRecord, hass: HomeAssistant) -> bool:
    """A logbook entry is visible only if its entity is accessible to the token.

    Entries without an entity_id cannot be scope-checked, so they are dropped
    (conservative: never reveal activity the token has no entity-level access to).
    """
    eid = entry.get("entity_id")
    if not isinstance(eid, str) or not eid:
        return False
    return resolve(eid, token, hass) in (Permission.READ, Permission.WRITE)


async def _tool_get_logbook(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read the human-readable logbook (requires cap_log_read)."""
    if effective_cap(token, "cap_log_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_logbook"

    try:
        start_time = _parse_time_param(str(args.get("start_time") or "24h"))
    except ValueError:
        return _tool_error("Invalid start_time format."), "invalid_request", "get_logbook"
    payload: dict[str, Any] = {"start_time": start_time.isoformat()}
    if args.get("end_time"):
        try:
            payload["end_time"] = _parse_time_param(args["end_time"]).isoformat()
        except ValueError:
            return _tool_error("Invalid end_time format."), "invalid_request", "get_logbook"

    entity_id = str(args.get("entity_id") or "").strip()
    if entity_id:
        perm = resolve(entity_id, token, hass)
        if perm == Permission.NOT_FOUND:
            return _tool_error("Entity not found."), "not_found", entity_id
        if perm in (Permission.NO_ACCESS, Permission.DENY):
            return _tool_error("Entity not found."), "denied", entity_id
        payload["entity_ids"] = [entity_id]

    try:
        result = await async_ws_command(hass, "logbook/get_events", payload)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to read logbook: {exc}"), "invalid_request", "get_logbook"

    if not isinstance(result, list):
        # An unexpected shape used to fall through as an empty logbook, which reads
        # as "nothing happened" rather than "the read did not work".
        return _tool_error(
            "Unexpected logbook response from Home Assistant."
        ), "invalid_request", "get_logbook"
    entries = result
    # Scope: keep only entries for entities this token can read, then redact any
    # remaining out-of-scope ids (e.g. a context_entity_id) defensively.
    scoped = [e for e in entries if isinstance(e, dict) and _logbook_entry_visible(e, token, hass)]
    scoped = filter_service_response(scoped, token, hass)

    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    scoped = scoped[-limit:]  # logbook is chronological; keep the most recent
    return _tool_success(json.dumps({"count": len(scoped), "entries": scoped}, default=str)), "allowed", "get_logbook"


def _entity_meta_snapshot(entry: Any) -> dict:
    """The registry metadata fields set_entity can change (for version capture)."""
    return {"name": entry.name, "icon": entry.icon, "area_id": entry.area_id}


def _registry_write_perm_error(perm: Any, entity_id: str) -> tuple[dict, str, str] | None:
    """Shared scope check for registry writes; None means WRITE-accessible.

    READ-only access returns a specific forbidden (the token can already see the
    entity, so no existence oracle); anything else returns the not_found body.
    """
    if perm == Permission.WRITE:
        return None
    if perm == Permission.READ:
        return _tool_error("Read-only access to this entity; registry edits need write access."), "denied", entity_id
    if perm == Permission.NOT_FOUND:
        return _tool_error("Entity not found."), "not_found", entity_id
    return _tool_error("Entity not found."), "denied", entity_id


def _registry_write_precheck(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str
) -> tuple[dict, str, str] | None:
    """Pre-gate validation for registry writes; None means OK to proceed.

    Checks entity_id presence and write scope so a doomed request is rejected
    before a pending approval is created. The executor re-validates at apply time.
    """
    entity_id = str(args.get("entity_id") or "").strip()
    if not entity_id:
        return _tool_error("entity_id is required."), "invalid_request", tool_name
    return _registry_write_perm_error(resolve(entity_id, token, hass), entity_id)


async def _build_diff_set_entity(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    entity_id = str(args.get("entity_id") or "")
    entry = er.async_get(hass).async_get(entity_id)
    before = _entity_meta_snapshot(entry) if entry else {}
    fields = [f for f in ("name", "icon", "area_id") if f in args]
    preview = {f: {"before": before.get(f), "after": args.get(f)} for f in fields}
    return {
        "kind": "system_action",
        **_summary("set_entity", fields=", ".join(fields) or "nothing", entity_id=entity_id),
        "target": {"type": "entity", "id": entity_id, "label": before.get("name") or entity_id},
        "preview": preview,
    }


async def _build_diff_delete_entity(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    entity_id = str(args.get("entity_id") or "")
    entry = er.async_get(hass).async_get(entity_id)
    snap = _entity_meta_snapshot(entry) if entry else {}
    return {
        "kind": "system_action",
        **_summary("delete_entity", entity_id=entity_id),
        "target": {"type": "entity", "id": entity_id, "label": snap.get("name") or entity_id},
        "preview": {
            "name": snap.get("name"),
            "area_id": snap.get("area_id"),
            "warning": "Removes the entity's registry entry. A live entity's integration may re-create it; an orphan stays gone. Not re-creatable through Phoenix MCP.",
        },
    }


async def _tool_set_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: edit an entity's registry metadata (Confirm-eligible)."""
    # Capability gate first: a denied token gets a uniform Forbidden with no entity
    # work, so the tool can never be a scope/existence oracle when the cap is off.
    if effective_cap(token, "cap_registry_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_entity"
    # Then validate entity scope before gating so an out-of-scope or typo'd target
    # is rejected up front instead of creating a pending approval that can only
    # fail after the admin approves it. The executor re-validates at approval time.
    pre = _registry_write_precheck(args, token, hass, "set_entity")
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_registry_write", token, hass, data,
        tool_name="set_entity", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_entity(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_entity(args, token, hass, data)


async def _execute_set_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    entity_id = str(args.get("entity_id") or "").strip()
    if not entity_id:
        return _tool_error("entity_id is required."), "invalid_request", "set_entity"
    err = _registry_write_perm_error(resolve(entity_id, token, hass), entity_id)
    if err is not None:
        return err
    reg = er.async_get(hass)
    if reg.async_get(entity_id) is None:
        return _tool_error("Entity has no registry entry to edit."), "invalid_request", entity_id

    updates: dict = {}
    if "name" in args:
        updates["name"] = args["name"] or None
    if "icon" in args:
        updates["icon"] = args["icon"] or None
    if "area_id" in args:
        area_id = args["area_id"]
        if area_id and ar.async_get(hass).async_get_area(area_id) is None:
            return _tool_error("Unknown area_id."), "invalid_request", entity_id
        updates["area_id"] = area_id or None
    if not updates:
        return _tool_error("Provide at least one of name, icon, area_id."), "invalid_request", entity_id

    before = _entity_meta_snapshot(reg.async_get(entity_id))
    try:
        reg.async_update_entity(entity_id, **updates)
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error
        _LOGGER.error("set_entity failed for %s: %s", entity_id, exc)
        return _tool_error("Failed to update entity."), "denied", entity_id
    after = _entity_meta_snapshot(reg.async_get(entity_id))
    await _record_version(
        data, token, resource_type="entity", resource_id=entity_id,
        action="edit", before=before, after=after, alias=after.get("name") or entity_id,
    )
    return _tool_success(json.dumps({"entity_id": entity_id, "updated": after}, default=str)), "allowed", entity_id


async def _tool_delete_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete an entity's registry entry (Confirm-eligible)."""
    # Capability gate first: a denied token gets a uniform Forbidden with no entity
    # work, so the tool can never be a scope/existence oracle when the cap is off.
    if effective_cap(token, "cap_registry_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "delete_entity"
    pre = _registry_write_precheck(args, token, hass, "delete_entity")
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_registry_write", token, hass, data,
        tool_name="delete_entity", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_entity(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_entity(args, token, hass, data)


async def _execute_delete_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    entity_id = str(args.get("entity_id") or "").strip()
    if not entity_id:
        return _tool_error("entity_id is required."), "invalid_request", "delete_entity"
    err = _registry_write_perm_error(resolve(entity_id, token, hass), entity_id)
    if err is not None:
        return err
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    if entry is None:
        return _tool_error("Entity has no registry entry to delete."), "invalid_request", entity_id
    before = _entity_meta_snapshot(entry)
    try:
        reg.async_remove(entity_id)
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error
        _LOGGER.error("delete_entity failed for %s: %s", entity_id, exc)
        return _tool_error("Failed to delete entity."), "denied", entity_id
    await _record_version(
        data, token, resource_type="entity", resource_id=entity_id,
        action="delete", before=before, after=None, alias=before.get("name") or entity_id,
    )
    return _tool_success(json.dumps({"entity_id": entity_id, "deleted": True})), "allowed", entity_id


async def _tool_render_template(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: render a Jinja2 template against permitted entity state."""
    if effective_cap(token, "cap_template_render") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "render_template"

    template_str = args.get("template", "")
    if not template_str:
        return _tool_error("Missing required argument: template"), "invalid_request", "render_template"

    try:
        rendered = _render_template_for_token(template_str, token, hass)
    except Exception:
        return _tool_error("Template rendering failed. Check your template syntax."), "invalid_request", "render_template"

    return _tool_success(rendered), "allowed", "render_template"


async def _tool_restart_ha(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: restart HA (gated by cap_restart, supports Confirm)."""
    blocked = await _gate(
        "cap_restart", token, hass, data,
        tool_name="restart_ha", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_restart_ha(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_restart_ha(args, token, hass, data)


async def _execute_restart_ha(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Side-effect path for restart_ha. Assumes capability is already satisfied."""
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await hass.services.async_call(
                "homeassistant",
                "restart",
                {},
                blocking=True,
            )
    except asyncio.TimeoutError:
        return (
            _tool_success(json.dumps({"success": True, "partial": True, "message": "Restart dispatched."})),
            "allowed",
            "restart_ha",
        )
    except ServiceNotFound:
        return _tool_error("Restart failed."), "denied", "restart_ha"
    except HomeAssistantError:
        return _tool_error("Restart failed."), "denied", "restart_ha"
    return _tool_success(json.dumps({"success": True})), "allowed", "restart_ha"


async def _tool_get_approval_status(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: poll an approval the token previously created, or list the
    token's own outstanding (pending) approvals when no approval_id is given.

    Cross-token reads return 404 (matching the missing-record response) to avoid
    a token-existence oracle.
    """
    from .approvals import STATUS_PENDING, get_approval, list_approvals  # noqa: PLC0415

    approval_id = args.get("approval_id")
    if approval_id is None:
        # No id: enumerate this token's own pending approvals (own data only).
        pending = list_approvals(data.store, status=STATUS_PENDING, token_id=token.id)
        items = [
            {
                "approval_id": a.id,
                "status": a.status,
                "tool_name": a.tool_name,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a in pending
        ]
        body = {"count": len(items), "pending_approvals": items}
        return _tool_success(json.dumps(body, default=str)), "allowed", "get_approval_status"

    if not isinstance(approval_id, str) or not approval_id:
        return _tool_error("Missing approval_id."), "invalid_request", "get_approval_status"
    record = get_approval(data.store, approval_id)
    if record is None or record.token_id != token.id:
        return _tool_error("Approval not found."), "not_found", "get_approval_status"
    payload = {
        "approval_id": record.id,
        "status": record.status,
        "tool_name": record.tool_name,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "result": record.result,
        "rejected_reason": record.rejected_reason,
    }
    return _tool_success(json.dumps(payload, default=str)), "allowed", _approval_resource(record)


def _approval_status_result(record: Any, *, resolved: bool) -> dict:
    """The status body, plus the operator-accepted note when the action ran.

    The note tells an agent that the operator reviewed and accepted this exact
    change, so it stops proposing variations of it. Until now it reached ONLY
    the two INTERACTIVE paths (`_await_inline_confirm` and Agent Chat), and a
    poll returned the bare status. That left the loop the note exists to prevent
    fully reachable by the agents most likely to hit it: every approval that
    outlived the inline window, which is to say the slow ones, and every MESA
    confirm before this change gave them an inline wait at all.

    Only an APPROVED record with a stored `tool_result` qualifies. An
    approved-but-execution-failed record transitions to rejected with the
    `execution_failed` slug, so it is not this branch, and a plain rejection has
    no stored result: neither is something the operator accepted.

    Never double-appends: the inline path builds its own copy from the stored
    record and never writes the note back, so the record read here is always the
    unannotated one.
    """
    from .approvals import STATUS_APPROVED  # noqa: PLC0415

    payload = _approval_status_payload(record, resolved=resolved)
    result = _tool_success(json.dumps(payload, default=str))
    if record.status == STATUS_APPROVED and (record.result or {}).get("tool_result") is not None:
        return _operator_accepted_result(result)
    return result


def _approval_status_payload(record: Any, *, resolved: bool) -> dict:
    """Status body shared by wait_for_approval (same fields as get_approval_status)."""
    return {
        "approval_id": record.id,
        "status": record.status,
        "resolved": resolved,
        "tool_name": record.tool_name,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "result": record.result,
        "rejected_reason": record.rejected_reason,
    }


async def _wait_for_many_approvals(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Block until every named approval resolves, or the timeout expires.

    ONE listener for the whole set rather than a wait per id: the ids resolve in
    whatever order the operator works through them (and a batch approve resolves
    several inside one request), so waiting on them in sequence would block on
    #1 while #2 and #3 were already done.

    Returns a per-approval list plus the ids still outstanding, so a timeout is
    partial information rather than a failure: the caller learns exactly which
    ones landed and can wait again for the rest.
    """
    from .approvals import STATUS_APPROVED, STATUS_PENDING, get_approval  # noqa: PLC0415

    raw = args.get("approval_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(i, str) and i for i in raw):
        return (
            _tool_error("approval_ids must be a non-empty array of approval id strings."),
            "invalid_request", "wait_for_approval",
        )
    # Order-preserving dedupe: a repeated id would otherwise be reported twice and
    # inflate the outstanding count against itself.
    ids = list(dict.fromkeys(raw))
    if len(ids) > MAX_BATCH_APPROVALS:
        return (
            _tool_error(f"wait_for_approval accepts at most {MAX_BATCH_APPROVALS} approval_ids."),
            "invalid_request", "wait_for_approval",
        )

    records = {}
    for approval_id in ids:
        record = get_approval(data.store, approval_id)
        # Unknown and belonging-to-another-token answer identically, so this
        # cannot become an oracle for another token's queue (rule 12).
        if record is None or record.token_id != token.id:
            return _tool_error("Approval not found."), "not_found", "wait_for_approval"
        records[approval_id] = record

    outstanding = {i for i, r in records.items() if r.status == STATUS_PENDING}
    if outstanding:
        timeout = _clamp_timeout(args.get("timeout", MAX_SUBSCRIPTION_SECONDS))
        future: asyncio.Future = hass.loop.create_future()

        @callback
        def _on_resolved(event: Any) -> None:
            outstanding.discard(event.data.get("approval_id"))
            if not outstanding and not future.done():
                future.set_result(True)

        unsub = hass.bus.async_listen(f"{DOMAIN}_approval_resolved", _on_resolved)
        _set_progress_status(
            f"Waiting for operator approval: {len(outstanding)} pending", total=float(timeout))
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            pass  # partial result below; the re-read decides what actually landed
        finally:
            unsub()
            _set_progress_status(None)

    # Re-read every record: some may have resolved without an event reaching us
    # (the expiry sweep), and a batch approve resolves several at once.
    latest = {i: (get_approval(data.store, i) or records[i]) for i in ids}
    still_pending = [i for i, r in latest.items() if r.status == STATUS_PENDING]
    payload = {
        "approvals": [
            _approval_status_payload(r, resolved=r.status != STATUS_PENDING)
            for r in latest.values()
        ],
        "resolved": not still_pending,
        "pending": still_pending,
    }
    result = _tool_success(json.dumps(payload, default=str))
    # The accepted-note applies as soon as ANY of them landed as an operator
    # approval, for the same reason it does on a single one: those changes are
    # final and must not be revised.
    if any(
        r.status == STATUS_APPROVED and (r.result or {}).get("tool_result") is not None
        for r in latest.values()
    ):
        result = _operator_accepted_result(result)
    return result, "allowed", f"approvals:{len(ids)}"


async def _tool_wait_for_approval(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: block until the token's own approval(s) resolve, or until timeout.

    A bounded server-side wait (not a stream): returns immediately if already
    resolved, else waits on the phoenix_mcp_approval_resolved event filtered to this
    approval_id. Own-data only (cross-token lookups 404, matching
    get_approval_status); no capability required.

    Accepts `approval_ids` (a list) as well as a single `approval_id`, and that
    plural form is what makes staged approvals workable. Confirm-gated calls
    return pending_approval immediately by default (see
    DEFAULT_CONFIRM_INLINE_WAIT_SECONDS), so an agent making a run of writes ends
    up holding N approval ids and wanting ONE place to block until the operator
    has dealt with them, however they choose to: one at a time, or in a batch. A
    caller waiting on each id in turn would serialise itself right back into the
    stall the staged model exists to remove.
    """
    from .approvals import STATUS_PENDING, get_approval  # noqa: PLC0415

    if "approval_ids" in args:
        return await _wait_for_many_approvals(args, token, hass, data)
    approval_id = args.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return _tool_error("Missing approval_id."), "invalid_request", "wait_for_approval"
    record = get_approval(data.store, approval_id)
    if record is None or record.token_id != token.id:
        return _tool_error("Approval not found."), "not_found", "wait_for_approval"

    if record.status != STATUS_PENDING:
        return _approval_status_result(record, resolved=True), "allowed", _approval_resource(record)

    timeout = _clamp_timeout(args.get("timeout", MAX_SUBSCRIPTION_SECONDS))
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_resolved(event: Any) -> None:
        if event.data.get("approval_id") == approval_id and not future.done():
            future.set_result(True)

    unsub = hass.bus.async_listen(f"{DOMAIN}_approval_resolved", _on_resolved)
    _set_progress_status(
        f"Waiting for operator approval: {record.tool_name}", total=float(timeout))
    try:
        await asyncio.wait_for(future, timeout)
    except TimeoutError:
        # Re-read in case it resolved without an event (e.g. the expiry sweep).
        latest = get_approval(data.store, approval_id) or record
        resolved = latest.status != STATUS_PENDING
        return _approval_status_result(latest, resolved=resolved), "allowed", _approval_resource(latest)
    finally:
        unsub()
        _set_progress_status(None)

    latest = get_approval(data.store, approval_id) or record
    return _approval_status_result(latest, resolved=True), "allowed", _approval_resource(latest)


# Executor registry for the admin-approval gate. When an admin approves a pending
# request, the approve handler looks up the saved tool_name here and invokes the
# corresponding _execute_X function with the saved args.
_EXECUTOR_REGISTRY: dict[str, Any] = {}


def _register_executor(tool_name: str, fn: Any) -> None:
    """Record an executor function for a tool. Called once at module import."""
    _EXECUTOR_REGISTRY[tool_name] = fn


# Tool dispatch registry: name -> (handler, context params it accepts, bound kwargs).
# Populated by the _register_tool block at the foot of this module, once every
# handler is defined.
_TOOL_HANDLERS: dict[str, tuple[Any, tuple[str, ...], dict[str, Any]]] = {}

# Request context a handler may opt into simply by naming the parameter. Reads
# come as (args, token, hass); gated writes add all three of these.
_TOOL_CONTEXT_PARAMS = ("data", "request_id", "client_ip")


def _register_tool(name: str, handler: Any, /, **bound: Any) -> None:
    """Bind a tool name to its handler. Called once per tool at module import.

    The handler's parameter names are read from its code object (never inspect,
    per the ws_dispatch rule) so the dispatcher can pass just the context each
    one takes, instead of every handler having to accept a uniform signature it
    mostly ignores. bound supplies per-registration constants, which is how the
    three dashboard card ops and the six mesa_* tools share one handler.

    name and handler are positional-only: a bound kwarg is itself called
    tool_name (the card ops pass their own), which would otherwise collide with
    this function's own parameter.
    """
    if name in _TOOL_HANDLERS:
        raise ValueError(f"duplicate tool registration: {name}")
    code = handler.__code__
    accepted = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
    wanted = tuple(p for p in _TOOL_CONTEXT_PARAMS if p in accepted)
    _TOOL_HANDLERS[name] = (handler, wanted, bound)


def _restore_token(user_id: str) -> TokenRecord:
    """Synthetic pass-through token used to re-apply a config under admin authority.

    pass_through makes policy_engine.resolve() return WRITE for every entity, so the
    per-entity scope checks inside the scene/helper executors pass for an admin who
    has full authority. Every capability is set to allow explicitly: pass_through
    only promotes deny for non-exempt caps, and the blueprint executors check the
    pass-through-EXEMPT domain caps (cap_automation_write / cap_script_write)
    inside the executor, which would refuse an admin restore on the defaults.
    It is never persisted and never authenticates a request.
    """
    return TokenRecord(
        id=f"__restore__:{user_id}",
        name="(admin restore)",
        token_hash="",
        created_at=utcnow(),
        created_by=user_id,
        pass_through=True,
        **{cap: CAP_ALLOW for cap in CAPABILITY_NAMES},  # type: ignore[arg-type]  # every CAPABILITY_NAMES entry is a cap_* str field
    )


async def _resource_exists(hass: HomeAssistant, resource_type: str, resource_id: str) -> bool:
    """Whether a versioned resource currently exists (picks restore edit vs recreate)."""
    try:
        if resource_type in ("automation", "script", "scene"):
            current = await hass.async_add_executor_job(
                yaml_includes.read_entry, hass.config.config_dir, resource_type, resource_id,
            )
            return current is not None
        if resource_type == "helper":
            ht, _, hid = resource_id.partition(":")
            return await _read_helper_config(hass, ht, hid) is not None
    except Exception:  # noqa: BLE001 - existence probe is best-effort
        return False
    return False


async def async_restore_version(
    record: Any, admin_user_id: str, hass: HomeAssistant, data: PhoenixData, side: str | None = None
) -> tuple[dict, str, str]:
    """Re-apply a stored config version under admin authority."""
    if side == "before":
        target = record.before
    elif side == "after":
        target = record.after
    else:
        target = record.after if record.after is not None else record.before
    if not isinstance(target, dict):
        return _tool_error("This version has no configuration to restore on that side."), "invalid_request", "async_restore_version"
    # The executors manage ids themselves; a stored config may carry one (e.g. a
    # deleted helper's full item), so drop it before re-applying.
    target = {k: v for k, v in target.items() if k != "id"}

    resource_type = record.resource_type
    resource_id = record.resource_id
    if resource_type in ("automation", "script", "scene") and yaml_includes.contains_tag_strings(target):
        # The stored snapshot flattened YAML tags to display strings ("!secret x");
        # writing them back as quoted strings would silently break the config.
        # Fail closed, like the oversized-snapshot marker below.
        return _tool_error(
            "This version contains YAML tags such as !secret or !include and "
            "cannot be restored automatically. Re-create it manually."
        ), "invalid_request", "async_restore_version"
    token = _restore_token(admin_user_id)
    exists = await _resource_exists(hass, resource_type, resource_id)

    ctx = _restore_ctx.set({"user_id": admin_user_id})
    try:
        if resource_type == "automation":
            if exists:
                return await _execute_edit_automation({"automation_id": resource_id, "config": target}, token, hass, data)
            # Recreate in place under the original id so the rollback lands on the
            # same timeline and a second restore just edits it.
            return await _execute_create_automation({"config": target, "automation_id": resource_id}, token, hass, data)
        if resource_type == "script":
            if exists:
                return await _execute_edit_script({"script_id": resource_id, "config": target}, token, hass, data)
            return await _execute_create_script({"script_id": resource_id, "config": target}, token, hass, data)
        if resource_type == "scene":
            if exists:
                return await _execute_edit_scene({"scene_id": resource_id, "config": target}, token, hass, data)
            return await _execute_create_scene({"config": target, "scene_id": resource_id}, token, hass, data)
        if resource_type == "helper":
            ht, _, hid = resource_id.partition(":")
            if exists:
                return await _execute_edit_helper({"helper_type": ht, "helper_id": hid, "config": target}, token, hass, data)
            return await _execute_create_helper({"helper_type": ht, "config": target}, token, hass, data)
        if resource_type == "dashboard":
            # Dashboards are edit-only: re-apply the layout to the existing dashboard
            # (resource_id "lovelace" is the default dashboard, url_path None).
            return await _execute_set_dashboard_config(
                {"url_path": None if resource_id == "lovelace" else resource_id, "config": target},
                token, hass, data,
            )
        if resource_type == "energy":
            # Energy is restore-only and deliberately WHOLESALE: the snapshot IS
            # the operator's chosen state, so reproducing it is the operation. The
            # addressed-write rule edit_energy_config enforces does not apply here,
            # exactly as rule 31 exempts a YAML restore from its removal check.
            return await async_restore_energy_prefs(target, token, hass, data)
        if resource_type in ("yaml_config", "file", "esphome_yaml", "blueprint"):
            restorable = target.get("content")
            if not isinstance(restorable, str):
                # Oversized snapshots are stored as a non-restorable marker.
                return _tool_error("This version is too large to restore inline."), "invalid_request", "async_restore_version"
            if resource_type == "blueprint":
                # resource_id is "<domain>/<config-relative path>". Route through
                # the executors so the path jail, schema validation, and consumer
                # reload all apply; edit when the file exists, recreate otherwise.
                bp_domain, _, bp_rel = resource_id.partition("/")
                resolved = _resolve_blueprint_path(hass, bp_domain, bp_rel)
                bp_exists = (
                    resolved is not None
                    and await _read_blueprint_source(hass, resolved[0]) is not None
                )
                bp_args = {"domain": bp_domain, "path": bp_rel, "content": restorable}
                if bp_exists:
                    return await _execute_edit_blueprint(bp_args, token, hass, data)
                return await _execute_create_blueprint(bp_args, token, hass, data)
            if resource_type == "yaml_config":
                return await _execute_set_yaml_config({"content": restorable}, token, hass, data)
            if resource_type == "esphome_yaml":
                # Snapshots hold the RAW file, so a restore reproduces it exactly.
                # It routes through the executor, whose splice is a no-op on raw
                # content (no placeholders). The executor reads _restore_ctx (set
                # by the caller below) and waives the three LITERAL credential
                # refusals for this path only: raw text re-writes the file's inline
                # credentials verbatim, which those rules read as an agent changing
                # a frozen value, so every restore of a file with any inline
                # credential used to fail. See splice_esphome_text.
                return await _execute_set_esphome_yaml(
                    {"file": resource_id, "content": restorable}, token, hass, data)
            return await _execute_write_file({"path": resource_id, "content": restorable}, token, hass, data)
        if resource_type == "entity":
            # Re-apply the registry metadata (name/icon/area). Restoring a deleted
            # entry's snapshot lands here too and fails cleanly in set_entity if the
            # entity no longer exists (a deleted registry entry cannot be recreated).
            fields = {k: target[k] for k in ("name", "icon", "area_id") if k in target}
            if not fields:
                return _tool_error("This version has no entity metadata to restore."), "invalid_request", "async_restore_version"
            return await _execute_set_entity({"entity_id": resource_id, **fields}, token, hass, data)
        return _tool_error(f"Cannot restore resource type '{resource_type}'."), "invalid_request", "async_restore_version"
    finally:
        _restore_ctx.reset(ctx)


async def async_execute_approved_tool(
    tool_name: str,
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
) -> tuple[dict, str, str]:
    """Run the side-effect path for a previously-gated tool. Returns the tool result tuple.

    Raises KeyError if no executor is registered for the tool_name.
    """
    fn = _EXECUTOR_REGISTRY.get(tool_name)
    if fn is None:
        raise KeyError(f"No executor registered for tool {tool_name!r}")
    # The admin's approval covers the whole action, so the MESA gate inside the
    # executor runs under confirm-approved semantics (see _approved_exec_ctx).
    ctx = _approved_exec_ctx.set(True)
    try:
        return await fn(args, token, hass, data)
    finally:
        _approved_exec_ctx.reset(ctx)


def _build_diff_restart_ha(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Diff payload for restart_ha approvals."""
    return {
        "kind": "system_action",
        **_summary("restart_ha"),
        "target": {"type": "system", "id": "homeassistant", "label": "Home Assistant"},
        "preview": {
            "warning": "Home Assistant will restart and be briefly unavailable.",
        },
    }


# ---------------------------------------------------------------------------
# Discovery and registry read tools (cap_registry_read)
# ---------------------------------------------------------------------------


async def _tool_get_capability_summary(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: the token introspecting its own caps/persona/limits. No cap required."""
    caps = effective_caps(token)
    body = {
        "token_name": token.name,
        "persona": token.persona,
        "pass_through": token.pass_through,
        "write_scope": token_has_write_scope(token),
        "capabilities": caps,
        "allowed": sorted(c for c, m in caps.items() if m == CAP_ALLOW),
        "confirm_gated": sorted(c for c, m in caps.items() if m == CAP_CONFIRM),
        "denied": sorted(c for c, m in caps.items() if m == CAP_DENY),
        "tools": _tool_gate_map(token, data, hass),
        "rate_limit": (
            {"requests_per_min": token.rate_limit_requests, "burst_per_sec": token.rate_limit_burst}
            if token.rate_limit_requests > 0 else "none"
        ),
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_capability_summary"


# ---------------------------------------------------------------------------
# Bounded subscription (cap_config_read): watch_entity
# ---------------------------------------------------------------------------


def _clamp_timeout(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = MAX_SUBSCRIPTION_SECONDS
    return max(1, min(seconds, MAX_SUBSCRIPTION_SECONDS))


async def _tool_watch_entity(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: block until an accessible entity changes state, or until timeout."""
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "watch_entity"

    entity_id = str_arg(args.get("entity_id"))
    if not entity_id:
        return _tool_error("Missing required argument: entity_id"), "invalid_request", "watch_entity"
    if resolve(entity_id, token, hass) not in (Permission.READ, Permission.WRITE):
        return _tool_error("Entity not found."), "not_found", entity_id

    timeout = _clamp_timeout(args.get("timeout", MAX_SUBSCRIPTION_SECONDS))
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_change(event: Any) -> None:
        if not future.done():
            future.set_result(event.data.get("new_state"))

    unsub = async_track_state_change_event(hass, [entity_id], _on_change)
    try:
        new_state = await asyncio.wait_for(future, timeout)
    except TimeoutError:
        return (
            _tool_success(json.dumps({"entity_id": entity_id, "changed": False, "timeout_seconds": timeout})),
            "allowed", entity_id,
        )
    finally:
        unsub()

    if new_state is None:
        body = {"entity_id": entity_id, "changed": True, "removed": True}
    else:
        scrubbed = scrub_sensitive_attributes(new_state)
        body = {
            "entity_id": entity_id,
            "changed": True,
            "state": scrubbed.get("state"),
            "attributes": scrubbed.get("attributes"),
            "when": getattr(new_state, "last_changed", None),
        }
    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


# ---------------------------------------------------------------------------
# ESPHome device YAML (cap_esphome_yaml)
# ---------------------------------------------------------------------------

































# ---------------------------------------------------------------------------
# ESPHome Device Builder reads (cap_esphome_yaml)
#
# Validation and reference lookups run against the add-on's WebSocket API. All
# four are reads, so they are never approval-gated even when the capability is
# set to confirm, matching the get_automation/get_dashboard_config precedent.
# ---------------------------------------------------------------------------



























# ---------------------------------------------------------------------------
# ESPHome firmware jobs (cap_esphome_yaml to build, cap_esphome_flash to flash)
#
# A build takes minutes and the admin Approve POST runs its executor inline, so
# nothing here ever waits for one: compile and install ENQUEUE a job in the
# add-on and return its id, and get_esphome_job polls it afterwards. The jobs are
# the add-on's, durable and connection-independent, so Phoenix MCP keeps no job
# state of its own and stays as stateless as the rest of the transport.
# ---------------------------------------------------------------------------















































































# ---------------------------------------------------------------------------
# Integration enable/disable (cap_integration_write)
# ---------------------------------------------------------------------------


async def _tool_list_integrations(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list config entries (integrations)."""
    if effective_cap(token, "cap_integration_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_integrations"
    integrations = [
        {
            "entry_id": entry.entry_id,
            "domain": entry.domain,
            "title": entry.title,
            "state": str(entry.state),
            "disabled_by": str(entry.disabled_by) if entry.disabled_by else None,
        }
        for entry in hass.config_entries.async_entries()
        if entry.domain != DOMAIN  # never expose Phoenix MCP's own entry as a target
    ]
    integrations.sort(key=lambda e: (e["domain"], e["title"] or ""))
    return _tool_success(json.dumps({"count": len(integrations), "integrations": integrations}, default=str)), "allowed", "list_integrations"


async def _tool_set_integration_enabled(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: enable/disable an integration (Confirm-gated)."""
    blocked = await _gate(
        "cap_integration_write", token, hass, data,
        tool_name="set_integration_enabled", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_integration_enabled(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_integration_enabled(args, token, hass, data)


async def _execute_set_integration_enabled(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    entry_id = str(args.get("entry_id") or "").strip()
    enabled = args.get("enabled")
    if not entry_id:
        return _tool_error("entry_id is required."), "invalid_request", "set_integration_enabled"
    if not isinstance(enabled, bool):
        return _tool_error("enabled must be a boolean."), "invalid_request", "set_integration_enabled"
    entry = hass.config_entries.async_get_entry(entry_id)
    # Phoenix MCP's own entry is never a valid target (no self-lockout); treat as not found.
    if entry is None or entry.domain == DOMAIN:
        return _tool_error("Integration not found."), "not_found", entry_id
    try:
        await hass.config_entries.async_set_disabled_by(
            entry_id, None if enabled else ConfigEntryDisabler.USER
        )
    except Exception as exc:  # noqa: BLE001 - OperationNotAllowed etc. -> clean error
        _LOGGER.error("set_integration_enabled failed: %s", exc)
        return _tool_error("Failed to change integration state."), "denied", entry_id
    return (
        _tool_success(json.dumps({"entry_id": entry_id, "domain": entry.domain, "enabled": enabled})),
        "allowed", f"integration:{entry_id}",
    )


# ---------------------------------------------------------------------------
# Backup (cap_backup) - create + list only; restore is intentionally unsupported
# ---------------------------------------------------------------------------


async def _backup_agent_ids(hass: HomeAssistant) -> list[str]:
    """Available backup agent ids (e.g. hassio.local on HAOS, backup.local on Core).

    Raises WsDispatchError rather than degrading to []: an empty list means "this
    install has no backup agents", which callers act on (create_backup refuses,
    list_backups reports none). Swallowing a dispatch failure into the same value
    turned a transport error into that wrong diagnosis.
    """
    info = await async_ws_command(hass, "backup/agents/info", {})
    agents = info.get("agents") if isinstance(info, dict) else None
    if not isinstance(agents, list):
        return []
    return [str(a["agent_id"]) for a in agents if isinstance(a, dict) and a.get("agent_id")]


def _backup_to_summary(b: Any) -> dict:
    """Project one backup (a ManagerBackup dataclass or dict) to compact JSON.

    The raw backup/info result holds dataclass instances; serializing them with
    json default=str produces unparseable repr strings, so flatten to fields.
    """
    if isinstance(b, dict):
        d = b
    elif dataclasses.is_dataclass(b) and not isinstance(b, type):
        try:
            d = dataclasses.asdict(b)
        except Exception:  # noqa: BLE001 - fall back to attribute access
            d = {}
    else:
        d = {}
    fields = ("backup_id", "name", "date", "database_included", "homeassistant_version")
    out: dict = {f: (d.get(f) if d else getattr(b, f, None)) for f in fields}
    agents = d.get("agents") if d else getattr(b, "agents", None)
    size = None
    agent_ids: list = []
    if isinstance(agents, dict):
        agent_ids = list(agents.keys())
        for a in agents.values():
            sz = a.get("size") if isinstance(a, dict) else getattr(a, "size", None)
            if sz is not None:
                size = sz
                break
    out["size"] = size
    out["agents"] = agent_ids
    return out


async def _tool_list_backups(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list existing backups (compact, newest first) and available agents."""
    if effective_cap(token, "cap_backup") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_backups"
    try:
        result = await async_ws_command(hass, "backup/info", {})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to list backups: {exc}"), "invalid_request", "list_backups"
    raw = result.get("backups") if isinstance(result, dict) else None
    backups = raw if isinstance(raw, list) else []
    summaries = [_backup_to_summary(b) for b in backups]
    summaries.sort(key=lambda s: s.get("date") or "", reverse=True)
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    warnings: list[str] = []
    try:
        available_agents = await _backup_agent_ids(hass)
    except WsDispatchError as exc:
        available_agents = []
        warnings.append(f"Failed to list backup agents: {exc}")
    body = {
        "total": len(summaries),
        "returned": min(len(summaries), limit),
        # Explicit, even though total vs returned already implies it: every other
        # paginating read states truncation as a boolean, and a reader that has to
        # compare two numbers to notice will sometimes not compare them.
        "truncated": len(summaries) > limit,
        "backups": summaries[:limit],
        "available_agents": available_agents,
    }
    if warnings:
        body["warnings"] = warnings
    return _tool_success(json.dumps(body, default=str)), "allowed", "list_backups"


async def _tool_create_backup(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a backup (Confirm-gated)."""
    blocked = await _gate(
        "cap_backup", token, hass, data,
        tool_name="create_backup", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_create_backup(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_backup(args, token, hass, data)


async def _execute_create_backup(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    agent_ids = args.get("agent_ids")
    if not isinstance(agent_ids, list) or not agent_ids:
        # Auto-detect: the default agent is install-type dependent (hassio.local on
        # HAOS/supervised, backup.local on Core). Prefer a local one.
        try:
            available = await _backup_agent_ids(hass)
        except WsDispatchError as exc:
            return _tool_error(
                f"Failed to determine available backup agents: {exc}. Pass agent_ids explicitly."
            ), "invalid_request", "create_backup"
        agent_ids = next(([a] for a in ("hassio.local", "backup.local") if a in available), available[:1])
        if not agent_ids:
            return _tool_error("No backup agents are available; pass agent_ids explicitly."), "invalid_request", "create_backup"
    payload: dict = {"agent_ids": agent_ids}
    name = args.get("name")
    if isinstance(name, str) and name.strip():
        payload["name"] = name
    try:
        result = await async_ws_command(hass, "backup/generate", payload, timeout=60)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to create backup: {exc}"), "invalid_request", "create_backup"
    job_id = getattr(result, "backup_job_id", None)
    if job_id is None and isinstance(result, dict):
        job_id = result.get("backup_job_id")
    body = {"created": True, "backup_job_id": job_id, "agent_ids": agent_ids}
    return _tool_success(json.dumps(body, default=str)), "allowed", "create_backup"


async def _tool_mesa(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    *,
    mesa_tool_name: str,
) -> tuple[dict, str, str]:
    """Adapter for the mesa_* tools, whose entry point takes the name first."""
    return await async_call_mesa_tool(mesa_tool_name, args, token, hass, data, request_id)


async def _call_tool(
    tool_name: str,
    arguments: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, Outcome, str]:
    """Route a tools/call request to the appropriate tool handler.

    The middle element is the audit outcome, narrowed to audit.Outcome so the
    _dispatch_mcp call site that feeds it straight into log_request type-checks.
    Note this does NOT statically constrain the handlers: _TOOL_HANDLERS stores
    them as Any, so their own returns are erased here. A valid outcome per tool
    is enforced by an adversarial sweep over the whole surface instead.
    """
    entry = _TOOL_HANDLERS.get(tool_name)
    if entry is None:
        return _tool_error(f"Unknown tool: {tool_name}"), "denied", tool_name
    handler, wanted, bound = entry
    context = {"data": data, "request_id": request_id, "client_ip": client_ip}
    return await handler(
        arguments, token, hass, **{k: context[k] for k in wanted}, **bound
    )


async def _get_ha_assist_api(hass: HomeAssistant) -> Any:
    """Return HA's Assist LLM APIInstance, or raise if unavailable.

    HA-COUPLING POINT. LLMContext's fields have changed across HA versions:
    `user_prompt` existed when this was written and has since been removed, so
    passing it raised TypeError and BOTH callers (prompts/list and prompts/get,
    pass-through tokens only) swallowed it in a bare `except Exception` and
    silently reported no prompts. Nothing failed loudly; the feature just went
    quiet. Found by type checking, not by anyone noticing.

    So the kwargs are now built against the live dataclass and filtered to the
    fields it actually declares, the same "supply the intersection with the live
    signature" approach ws_dispatch uses for ActiveConnection. Old and new HA
    both work, and a future field removal degrades to omitting it rather than
    to a silent empty result.
    """
    import dataclasses  # noqa: PLC0415

    from homeassistant.helpers import llm as _ha_llm  # noqa: PLC0415

    wanted = {
        "platform": DOMAIN,
        "context": None,
        "user_prompt": None,
        "language": "en",
        "assistant": "conversation",
        "device_id": None,
    }
    declared = {f.name for f in dataclasses.fields(_ha_llm.LLMContext)}
    kwargs: dict[str, Any] = {k: v for k, v in wanted.items() if k in declared}
    llm_context = _ha_llm.LLMContext(**kwargs)
    return await _ha_llm.async_get_api(hass, _ha_llm.LLM_API_ASSIST, llm_context)


def _build_server_info(token: TokenRecord, hass: HomeAssistant, base_url: str) -> dict:
    """Build the phx://server-info resource payload for the MCP resources/read endpoint."""
    states = hass.states.async_all()
    if token.pass_through:
        # Use build_permitted_states to get the same set the token actually sees,
        # including the Phoenix-platform entity filter (sensor.phoenix_mcp_* telemetry sensors).
        count = len(_build_permitted_states(token, hass))
    else:
        filtered = filter_entities_for_token(states, token, hass)
        count = len(filtered)

    return {
        "name": "Phoenix MCP Scoped Proxy",
        "version": PHOENIX_VERSION,
        "token_name": token.name,
        "permitted_entity_count": count,
        "capability_flags": effective_caps(token),
        "persona": token.persona,
        "native_ha_mcp_endpoint": f"{base_url}/api/mcp",
        "phoenix_mcp_context_endpoint": f"{base_url}/api/phoenix-mcp/context",
    }


def _build_context_plain(token: TokenRecord, hass: HomeAssistant) -> str:
    """Build the plain-text context document listing accessible entities and capabilities."""
    lines: list[str] = []

    if token.pass_through:
        # Use build_permitted_states for an accurate count that respects Phoenix-platform
        # entity filtering and use_assist_exposure (same set the token actually sees).
        count = len(_build_permitted_states(token, hass))
        lines.append("This token operates in pass-through mode.")
        lines.append(
            f"It has unrestricted access to all {count} accessible Home Assistant entities and services."
        )
        lines.append("")
        lines.append("The phoenix_mcp domain is always blocked regardless of token type.")
    else:
        states = hass.states.async_all()
        entity_hints = hass.data[DOMAIN].store.get_entity_hints()
        accessible: list[tuple[str, str, str | None]] = []
        for state in states:
            perm = resolve(state.entity_id, token, hass)
            if perm == Permission.WRITE:
                accessible.append((state.entity_id, "READ/WRITE", get_effective_hint(token, state.entity_id, hass, entity_hints)))
            elif perm == Permission.READ:
                accessible.append((state.entity_id, "READ", get_effective_hint(token, state.entity_id, hass, entity_hints)))

        accessible.sort(key=lambda x: x[0])
        lines.append("You have access to the following Home Assistant entities:")
        if accessible:
            for eid, perm_str, hint in accessible:
                hint_part = f' - "{hint}"' if hint else ""
                lines.append(f"- {eid} ({perm_str}){hint_part}")
        else:
            lines.append("(none)")
        lines.append("")
        lines.append(
            "You cannot access any other entities. "
            "Do not attempt to call services on entities not listed above."
        )

    lines.append("")
    caps = effective_caps(token)
    lines.append("Capabilities (deny / allow / confirm; confirm requires admin approval per request):")
    label_map = (
        ("cap_config_read", "Config read"),
        ("cap_automation_write", "Automation write"),
        ("cap_script_write", "Script write"),
        ("cap_template_render", "Template render"),
        ("cap_restart", "Restart"),
        ("cap_physical_control", "Physical control (locks/alarms/covers)"),
        ("cap_broadcast", "Broadcast"),
        ("cap_log_read", "Log read"),
        ("cap_service_response", "Service response"),
    )
    for cap_key, label in label_map:
        lines.append(f"- {label}: {caps.get(cap_key, 'deny')}")
    lines.append("")
    if token.rate_limit_requests > 0:
        lines.append(
            f"Rate limit: {token.rate_limit_requests} requests/min, burst {token.rate_limit_burst}/sec"
        )
    else:
        lines.append("Rate limit: none")

    return "\n".join(lines)


def _build_context_json(token: TokenRecord, hass: HomeAssistant) -> dict:
    """Build the structured JSON context document for the ?format=json context endpoint."""
    registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)

    entities: list[dict] = []
    states = hass.states.async_all()

    if token.pass_through:
        _expose_check = None
        if token.use_assist_exposure:
            from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
                async_should_expose as _should_expose,
            )
            _expose_check = lambda eid: _should_expose(hass, "conversation", eid)
        for state in states:
            eid = state.entity_id
            if eid.split(".")[0] in BLOCKED_DOMAINS:
                continue
            entry = registry.async_get(eid)
            # Exclude Phoenix MCP telemetry sensors (registered to the phoenix_mcp platform) so
            # pass_through tokens see the same entity set as build_permitted_states().
            if entry is not None and entry.platform == DOMAIN:
                continue
            if _expose_check is not None and not _expose_check(eid):
                continue
            area_id = _resolve_area_id(entry, dev_registry)
            entities.append({
                "entity_id": eid,
                "permission": "READ/WRITE",
                "area_id": area_id,
            })
    else:
        entity_hints = hass.data[DOMAIN].store.get_entity_hints()
        for state in states:
            perm = resolve(state.entity_id, token, hass)
            if perm not in (Permission.READ, Permission.WRITE):
                continue
            entry = registry.async_get(state.entity_id)
            area_id = _resolve_area_id(entry, dev_registry)
            perm_str = "READ/WRITE" if perm == Permission.WRITE else "READ"
            e: dict = {"entity_id": state.entity_id, "permission": perm_str, "area_id": area_id}
            hint = get_effective_hint(token, state.entity_id, hass, entity_hints)
            if hint:
                e["hint"] = hint
            entities.append(e)

    entities.sort(key=lambda e: e["entity_id"])

    return {
        "token_name": token.name,
        "pass_through": token.pass_through,
        "persona": token.persona,
        "entities": entities,
        "capability_flags": effective_caps(token),
        "rate_limit": {
            "requests_per_minute": token.rate_limit_requests,
            "burst_per_second": token.rate_limit_burst,
        },
    }


def _build_instructions(token: TokenRecord, data: PhoenixData, base_url: str) -> str:
    """Token-aware MCP `instructions` primer (skills Channel A).

    Short, injected every session: the etiquette that prevents the common
    failure modes (treating pending_approval as an error, retrying, ignoring the
    per-entity safety layer) plus this token's gated capabilities and a link to
    the full guide at /api/phoenix-mcp/skill.
    """
    caps = effective_caps(token)
    confirm_gated = sorted(c for c, m in caps.items() if m == CAP_CONFIRM)
    lines = [
        "You are connected to Home Assistant through Phoenix MCP, a scoped gateway. This token "
        "sees only the entities and tools an operator granted it, and some actions are gated.",
        "",
        "- Call get_capability_summary first to see what you can read, control, and what "
        "needs approval. Use get_overview or search_entities to discover entities; you only "
        "see entities in this token's scope.",
        # Must agree with tool_common._tool_pending, which the agent reads at the
        # moment it matters; a primer that says something else is a second story
        # about the same event. Both say: continue, then collect the outcomes
        # together, because approvals queue and are cleared in one action.
        "- Some actions return status \"pending_approval\". That is normal, not an error: a "
        "human must approve them. Do not retry, and do not stop after one: further gated "
        "calls queue alongside it and the operator clears the queue in a single action. If "
        "you need the outcomes, call wait_for_approval ONCE with approval_ids listing all of "
        "them (or get_approval_status for a one-shot check); otherwise tell the user what is "
        "awaiting approval.",
        "- Before a risky or bulk service call, preview it with dry_run_service (and whatif to "
        "see what automations it would trigger).",
        "- If a tool is not in the tool list, this token cannot use it; ask the operator to "
        "grant the capability rather than attempting the call.",
        "- If a tool response includes a notice that this token's permissions changed, your "
        "tool list is stale: call get_capability_summary for the current state and ask the "
        "user to reconnect or refresh this MCP server's tools.",
    ]
    if confirm_gated:
        lines.append(
            "- As of this connection, these capabilities require admin approval per call: "
            + ", ".join(confirm_gated)
            + ". Capabilities can change mid-session; call get_capability_summary for the current state."
        )
    if data.store.get_settings().mesa_mode != MESA_MODE_OFF:
        lines.append(
            "- A per-entity safety layer (MESA) is active: some entities are read-only or "
            "require confirmation by nature regardless of capabilities. describe_entity and "
            "find_available_actions show an entity's control_mode."
        )
    if (
        caps.get("cap_automation_write") != CAP_DENY
        and caps.get("cap_search") != CAP_DENY
    ):
        lines.append(
            "- Before authoring an automation, call find_available_actions on the trigger "
            "or condition entity: Home Assistant may define a purpose-built trigger/condition "
            "for it (for example a threshold crossing), and the response lists those with a "
            "ready-to-use config example, which is more reliable than hand-building a generic "
            "state or numeric_state equivalent."
        )
    lines.append("")
    lines.append(
        f"Full Phoenix MCP and Home Assistant usage guide: {base_url}/api/phoenix-mcp/skill (fetch it before "
        "complex automation, scene, or configuration work)."
    )
    return "\n".join(lines)


def _maybe_append_stale_advisory(
    tool_result: dict, token: TokenRecord, data: PhoenixData, client_ip: str
) -> bool:
    """Append the one-time stale-tools/catalog notices to a result, in place.

    Returns whether an advisory was added, which the caller records on the audit
    row. This is Phoenix's "soft tools/list_changed": the stateless transport
    cannot push notifications, so the notice rides in-band on the next
    tools/call, once per staleness epoch (a fresh tools/list resets it).

    TWO independent axes, and the epoch key spans both so resolving one does not
    re-fire the other:
      - the OPERATOR changed this token (settings_version moved past the version
        echoed at the last tools/list), and
      - the BUILD changed under a client that already fetched its schemas
        (catalog fingerprint differs). An empty stored fingerprint means never
        recorded, so never stale.

    Skipped entirely for Phoenix's own internal surfaces (the sentinel client
    IPs): they rebuild their tool list every turn from the live token, so it is
    never stale, and there is no external MCP server to reconnect. Deliberately
    NOT marking the epoch for them either, so a real external client sharing the
    token still gets advised.
    """
    internal = client_ip in (
        AGENTCLI_CLIENT_IP, ASSIST_CLIENT_IP, VOICE_AGENT_CLIENT_IP, AI_TASK_CLIENT_IP)
    notices: list[str] = []
    if not internal:
        if token.settings_version > token.tools_list_version:
            notices.append(_STALE_TOOLS_ADVISORY)
        if (
            token.tools_catalog_fingerprint
            and token.tools_catalog_fingerprint != _TOOL_CATALOG_FINGERPRINT
        ):
            notices.append(_STALE_CATALOG_ADVISORY)
    epoch = f"{token.settings_version}:{token.tools_catalog_fingerprint}"
    if not notices or data.stale_tools_advised.get(token.id) == epoch:
        return False
    data.stale_tools_advised[token.id] = epoch
    content = tool_result.get("content")
    if isinstance(content, list):
        content.extend({"type": "text", "text": n} for n in notices)
    return True


async def _dispatch_tools_call(
    params: dict,
    msg_id: Any,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str,
    client_ip: str,
) -> tuple[dict, str, str, Outcome]:
    """Run one tools/call and build its JSON-RPC response and audit row.

    This is the single choke point for every tools/call, including each item of a
    JSON-RPC batch, which is why the safety net lives here.
    """
    # Capped before dispatch or logging: this is client-supplied and otherwise
    # unbounded. A malformed client (a local model emitting garbled tool-call
    # syntax) can send garbage as the "name" field; every real tool name is a
    # short identifier, so a generous cap only ever clips pathological input and
    # keeps the audit log (method and resource both derive from it) readable.
    tool_name = str(params.get("name") or "")[:MAX_TOOL_NAME_LENGTH]
    arguments = params.get("arguments") or {}
    _mesa_advisory_ctx.set(False)
    try:
        tool_result, outcome, resource = await _call_tool(
            tool_name, arguments, token, hass, data,
            request_id=request_id, client_ip=client_ip,
        )
    except Exception:
        # _call_tool is a bare dispatcher with no exception handling of its own,
        # and post() has none either: an unhandled exception here previously
        # propagated to aiohttp's default error handling, which the MCP client
        # cannot parse cleanly. It manifests as a multi-minute client HANG rather
        # than a fast error. Log the real exception server-side for diagnosis,
        # never leak it to the client, return something self-correctable.
        _LOGGER.exception(
            "Unhandled exception in tool %s for token %s rid=%s",
            tool_name, token.name, request_id,
        )
        tool_result = _tool_error("Internal error processing this tool call.")
        outcome = "invalid_request"
        resource = tool_name

    stale = _maybe_append_stale_advisory(tool_result, token, data, client_ip)
    _log(data, token, request_id=request_id, method=tool_name or "tools/call",
         resource=resource, outcome=outcome, client_ip=client_ip,
         payload={"name": tool_name, "arguments": arguments},
         mesa_advisory=_mesa_advisory_ctx.get(),
         stale_tools_advisory=stale)
    return _jsonrpc_result(msg_id, tool_result), tool_name or "tools/call", resource, outcome


# Every JSON-RPC method _dispatch_mcp answers. The transport reads it to choose
# an unknown method's HTTP STATUS, which the spec makes meaningful: 404 with a
# -32601 body says "this IS an MCP endpoint and it does not implement that
# method", which is how a client distinguishes it from a 404 by a server that
# hosts no MCP endpoint at all and decides whether to fall back. The status has
# to be decided BEFORE the response framing is picked, because preparing an SSE
# stream puts a 200 on the wire; an unknown method has nothing to stream anyway.
#
# This mirrors the dispatcher's own branches, so test_mcp_protocol.py reads them
# back off its AST and fails on any drift. A method added to one and not the
# other is the failure that matters: answering a real method with 404, or
# streaming an unknown one.
_MCP_METHODS: frozenset[str] = frozenset({
    "server/discover",
    "initialize",
    "notifications/initialized",
    "initialized",
    "ping",
    "tools/list",
    "tools/call",
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
})


async def _dispatch_mcp(
    method: str,
    msg_id: Any,
    params: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    client_ip: str,
    base_url: str,
) -> tuple[dict | None, str, str, str]:
    """Dispatch one MCP method call.

    Returns (response_msg, log_method, log_resource, outcome).
    response_msg is None for notifications that require no response.
    """
    request_id = generate_request_id()

    if method == "server/discover":
        # The stateless-era replacement for initialize: identity, versions and
        # capabilities in one call, with no handshake to establish. It matters
        # more than a duplicate of initialize looks, because a client speaking a
        # revision that HAS no initialize would otherwise never receive the
        # token-aware primer at all, and that primer is the only place an agent
        # is told which of its capabilities are approval-gated.
        resp = _jsonrpc_result(msg_id, {
            "resultType": "complete",
            "supportedVersions": list(MCP_PROTOCOL_VERSIONS),
            "capabilities": _SERVER_CAPABILITIES,
            "instructions": _build_instructions(token, data, base_url),
            "ttlMs": MCP_DISCOVER_TTL_MS,
            # NEVER "public". The instructions name THIS token's confirm-gated
            # capabilities, so a shared intermediary caching one token's answer
            # would serve it to the next token reaching it through that proxy.
            "cacheScope": "private",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "Phoenix MCP", "version": PHOENIX_VERSION,
                },
            },
        })
        _log(data, token, request_id=request_id, method="server/discover",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "server/discover", "/api/phoenix-mcp", "allowed"

    if method == "initialize":
        # Echo the client's version when it is one we implement, otherwise name
        # the one we prefer and let the client decide. Membership on a tuple
        # compares rather than hashes, so a client sending a non-string here
        # (live-observed shapes are not always what a schema declares) falls to
        # the preferred version instead of raising.
        requested = params.get("protocolVersion")
        version = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION_PREFERRED
        resp = _jsonrpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": _SERVER_CAPABILITIES,
            "serverInfo": {"name": "Phoenix MCP", "version": PHOENIX_VERSION},
            "instructions": _build_instructions(token, data, base_url),
        })
        _log(data, token, request_id=request_id, method="initialize",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "initialize", "/api/phoenix-mcp", "allowed"

    if method in ("notifications/initialized", "initialized"):
        _log(data, token, request_id=request_id, method=method,
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return None, method, "/api/phoenix-mcp", "allowed"

    if method == "ping":
        resp = _jsonrpc_result(msg_id, {})
        _log(data, token, request_id=request_id, method="ping",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "ping", "/api/phoenix-mcp", "allowed"

    if method == "tools/list":
        # Announce only the tools this token can use, unless announce_all_tools
        # is set. Cap-tied tools gate on their cap; write/action tools gate on
        # write scope; tools needing a system surface (ESPHome, the Device
        # Builder) gate on it being present; reads are always announced.
        # announce_all_tools overrides all of it, including "requires": it is a
        # debugging affordance, and the call path still refuses.
        announce_all = getattr(token, "announce_all_tools", False)
        has_write = token_has_write_scope(token)
        mesa_defs = mesa_tool_defs() if data.mesa is not None else []
        tools = []
        for tool_def in list(_ENTITY_TOOL_DEFS) + list(_NATIVE_TOOL_DEFS) + list(_SYSTEM_TOOL_DEFS) + mesa_defs:
            if announce_all or _tool_is_announced(tool_def, token, has_write, hass):
                tools.append({k: v for k, v in tool_def.items() if k not in ("cap", "requires")})
        resp = _jsonrpc_result(msg_id, {"tools": tools})
        # The client's tool list is now current: echo the settings version (in
        # memory; persisted by the periodic flush, like last_used_at) and clear
        # the advised marker so a later change advises again.
        token.tools_list_version = token.settings_version
        token.tools_catalog_fingerprint = _TOOL_CATALOG_FINGERPRINT
        data.stale_tools_advised.pop(token.id, None)
        _log(data, token, request_id=request_id, method="tools/list",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "tools/list", "/api/phoenix-mcp", "allowed"

    if method == "tools/call":
        return await _dispatch_tools_call(
            params, msg_id, token, hass, data, request_id, client_ip,
        )

    if method == "resources/list":
        resp = _jsonrpc_result(msg_id, {
            "resources": [
                {
                    "uri": "homeassistant://assist/context-snapshot",
                    "name": "Assist Context Snapshot",
                    "description": "A snapshot of the current Assist context, matching the existing GetLiveContext tool output",
                    "mimeType": "text/plain",
                },
                {
                    "uri": "phx://server-info",
                    "name": "Phoenix MCP Server Info",
                    "mimeType": "application/json",
                },
            ]
        })
        _log(data, token, request_id=request_id, method="resources/list",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "resources/list", "/api/phoenix-mcp", "allowed"

    if method == "resources/read":
        uri = params.get("uri", "")
        if uri == "homeassistant://assist/context-snapshot":
            context_text = _build_live_context(token, hass)
            resp = _jsonrpc_result(msg_id, {
                "contents": [{
                    "uri": "homeassistant://assist/context-snapshot",
                    "mimeType": "text/plain",
                    "text": context_text,
                }]
            })
            _log(data, token, request_id=request_id, method="resources/read",
                 resource="homeassistant://assist/context-snapshot", outcome="allowed", client_ip=client_ip)
            return resp, "resources/read", "homeassistant://assist/context-snapshot", "allowed"
        if uri != "phx://server-info":
            if msg_id is not None:
                _log(data, token, request_id=request_id, method="resources/read",
                     resource=uri or "/api/phoenix-mcp", outcome="denied", client_ip=client_ip)
                return _jsonrpc_error(msg_id, -32602, "Unknown resource URI."), "resources/read", uri, "denied"
            return None, "resources/read", uri, "denied"
        server_info = _build_server_info(token, hass, base_url)
        resp = _jsonrpc_result(msg_id, {
            "contents": [{
                "uri": "phx://server-info",
                "mimeType": "application/json",
                "text": json.dumps(server_info, default=str),
            }]
        })
        _log(data, token, request_id=request_id, method="resources/read",
             resource="phx://server-info", outcome="allowed", client_ip=client_ip)
        return resp, "resources/read", "phx://server-info", "allowed"

    if method == "prompts/list":
        if token.pass_through:
            try:
                api_inst = await _get_ha_assist_api(hass)
                prompt_name = f"Default prompt for Home Assistant {api_inst.api.name}"
                prompts = [{"name": prompt_name, "description": f"Default prompt for Home Assistant {api_inst.api.name} API"}]
            except Exception:
                # Never silent: this same catch hid a real defect for months when HA
                # removed LLMContext.user_prompt, because an empty prompt list is a
                # valid answer and nothing distinguished "no prompts" from "the
                # Assist lookup is broken". Degrading is still right (a missing
                # prompt must not fail the request), but the operator gets a
                # traceback for it.
                _LOGGER.exception("prompts/list: the HA Assist API lookup failed")
                prompts = []
        else:
            prompts = [{
                "name": "Phoenix MCP access context",
                "description": "Describes the Home Assistant entities and capabilities accessible to this token",
            }]
        resp = _jsonrpc_result(msg_id, {"prompts": prompts})
        _log(data, token, request_id=request_id, method="prompts/list",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "prompts/list", "/api/phoenix-mcp", "allowed"

    if method == "prompts/get":
        name = params.get("name", "")
        if token.pass_through:
            try:
                api_inst = await _get_ha_assist_api(hass)
                expected_name = f"Default prompt for Home Assistant {api_inst.api.name}"
                if name != expected_name:
                    _log(data, token, request_id=request_id, method="prompts/get",
                         resource="/api/phoenix-mcp", outcome="denied", client_ip=client_ip)
                    return _jsonrpc_error(msg_id, -32602, "Unknown prompt."), "prompts/get", "/api/phoenix-mcp", "denied"
                resp = _jsonrpc_result(msg_id, {
                    "description": f"Default prompt for Home Assistant {api_inst.api.name} API",
                    "messages": [{"role": "user", "content": {"type": "text",
                        "text": _UNTRUSTED_DATA_BOUNDARY + "\n\n" + api_inst.api_prompt}}],
                })
            except Exception:
                # See prompts/list above: "Prompt unavailable" reads as a normal
                # absence, so without this the failure that produced it is
                # invisible. The audit outcome stays denied because that is what
                # the caller was told; the traceback is for the operator.
                _LOGGER.exception("prompts/get: the HA Assist API lookup failed")
                _log(data, token, request_id=request_id, method="prompts/get",
                     resource="/api/phoenix-mcp", outcome="denied", client_ip=client_ip)
                return _jsonrpc_error(msg_id, -32603, "Prompt unavailable."), "prompts/get", "/api/phoenix-mcp", "denied"
        else:
            if name != "Phoenix MCP access context":
                _log(data, token, request_id=request_id, method="prompts/get",
                     resource="/api/phoenix-mcp", outcome="denied", client_ip=client_ip)
                return _jsonrpc_error(msg_id, -32602, "Unknown prompt."), "prompts/get", "/api/phoenix-mcp", "denied"
            prompt_text = _UNTRUSTED_DATA_BOUNDARY + "\n\n" + _build_context_plain(token, hass)
            resp = _jsonrpc_result(msg_id, {
                "description": "Describes the Home Assistant entities and capabilities accessible to this token",
                "messages": [{"role": "user", "content": {"type": "text", "text": prompt_text}}],
            })
        _log(data, token, request_id=request_id, method="prompts/get",
             resource="/api/phoenix-mcp", outcome="allowed", client_ip=client_ip)
        return resp, "prompts/get", "/api/phoenix-mcp", "allowed"

    if msg_id is not None:
        _log(data, token, request_id=request_id, method=method or "unknown",
             resource="/api/phoenix-mcp", outcome="not_implemented", client_ip=client_ip)
        return _jsonrpc_error(msg_id, -32601, "Method not found."), method or "unknown", "/api/phoenix-mcp", "not_implemented"

    return None, method or "unknown", "/api/phoenix-mcp", "not_implemented"


async def _handle_streamable_batch(
    items: list,
    token: TokenRecord,
    rl_result: RateLimitResult,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str,
    client_ip: str,
    base_url: str,
) -> web.Response:
    """Dispatch a JSON-RPC batch array per MCP 2025-03-26.

    Each item is dispatched independently. Failed items produce per-item error objects
    rather than failing the whole batch. Notifications (no id) produce no response entry.
    Returns 202 when all items are notifications; 200 with a results array otherwise.
    """
    if not items:
        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(_jsonrpc_error(None, -32600, "Empty batch.")),
            headers={"X-Phoenix-Request-ID": request_id},
        )

    # Batch rate limiting design: each batch consumes ONE rate-limit token, not one
    # per item. Per-item counting would let a single 50-item batch exhaust a token's
    # entire 60 req/min budget, making batching worse than sequential calls. The
    # MAX_BATCH_ITEMS cap bounds the multiplier to 50x, an acceptable tradeoff
    # for MCP batch usability.
    # Items are dispatched sequentially (not via asyncio.gather) so a batch can never
    # run up to MAX_BATCH_ITEMS side-effecting tools concurrently and interleave
    # writes; order is preserved and one item's failure stays isolated.
    if len(items) > MAX_BATCH_ITEMS:
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps(_jsonrpc_error(None, -32600, f"Batch too large. Maximum {MAX_BATCH_ITEMS} items.")),
            headers={"X-Phoenix-Request-ID": request_id},
        )

    responses = await _dispatch_streamable_batch(
        items, token, hass, data, client_ip, base_url=base_url)

    if not responses:
        return web.Response(status=202, headers={"X-Phoenix-Request-ID": request_id})

    resp = web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps(responses, default=str),
        headers={"X-Phoenix-Request-ID": request_id},
    )
    resp.headers.update(_rate_limit_headers(token, rl_result))
    return resp


def _batch_expects_response(items: list) -> bool:
    """Whether a batch produces any reply entry, without dispatching anything.

    Mirrors the per-item suppression rules in _dispatch_streamable_batch so the
    SSE path can decide between a stream and a bare 202 BEFORE preparing a
    response. Any change to those rules must change this too; the agreement is
    pinned by test.
    """
    for item in items:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
            return True  # malformed: gets an Invalid Request entry
        kind, _payload = _classify_jsonrpc_message(item)
        if kind == "accepted":
            continue  # a response/notification object never gets a reply
        if kind == "unsupported" and "id" not in item:
            continue  # valid positional-params notification: no entry
        if kind == "error" or "id" in item:
            return True
    return False


async def _dispatch_streamable_batch(
    items: list,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    client_ip: str,
    *,
    base_url: str,
) -> list[dict]:
    """Dispatch each batch item in order, returning only the entries that reply.

    Items are dispatched sequentially (not via asyncio.gather) so a batch can
    never run up to MAX_BATCH_ITEMS side-effecting tools concurrently and
    interleave writes; order is preserved and one item's failure stays isolated.
    """
    bus = _progress_ctx.get()

    async def _dispatch_one(item: Any) -> dict | None:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
            msg_id = _sanitize_jsonrpc_id(item.get("id")) if isinstance(item, dict) else None
            return _jsonrpc_error(msg_id, -32600, "Invalid Request.")
        msg_id = _sanitize_jsonrpc_id(item.get("id"))
        kind, payload = _classify_jsonrpc_message(item)
        is_notification = "id" not in item
        if kind == "accepted":
            return None  # response/notification object: no reply entry
        if kind in ("error", "unsupported"):
            # A structurally valid but undispatchable item (positional params) with
            # no id is a valid notification: no entry. A malformed item is an
            # Invalid Request (id null) and still gets an error entry.
            if kind == "unsupported" and is_notification:
                return None
            return _jsonrpc_error(msg_id, *payload)
        method, params = payload
        if bus is not None:
            # Progress is per-request in MCP, and each batch item carries its own
            # token; items run one at a time, so one field is enough.
            bus.token = _progress_token(method, params)
        try:
            response_msg, _, _, _ = await _dispatch_mcp(
                method, msg_id, params, token, hass, data, client_ip,
                base_url=base_url,
            )
        finally:
            if bus is not None:
                bus.token = None
                bus.status = None
        return None if is_notification else response_msg

    responses: list[dict] = []
    for item in items:
        try:
            r = await _dispatch_one(item)
        except Exception:  # noqa: BLE001 - isolate one item's failure from the batch
            msg_id = _sanitize_jsonrpc_id(item.get("id")) if isinstance(item, dict) else None
            responses.append(_jsonrpc_error(msg_id, -32603, "Internal error."))
            continue
        if r is not None:
            responses.append(r)
    return responses


async def _dispatch_mcp_result(
    method: str,
    msg_id: Any,
    params: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    client_ip: str,
    *,
    base_url: str,
) -> dict | None:
    """_dispatch_mcp reduced to just the response message, for the SSE writer."""
    response_msg, _method, _resource, _outcome = await _dispatch_mcp(
        method, msg_id, params, token, hass, data, client_ip,
        base_url=base_url,
    )
    return response_msg


def _rate_limit_headers(token: TokenRecord, rl_result: Any) -> dict[str, str]:
    """The X-RateLimit-* trio, or nothing when rate limiting is off for this token."""
    if token.rate_limit_requests <= 0:
        return {}
    return {
        "X-RateLimit-Limit": str(token.rate_limit_requests),
        "X-RateLimit-Remaining": str(rl_result.remaining),
        "X-RateLimit-Reset": str(rl_result.reset),
    }


def _progress_token(method: str, params: dict) -> str | int | None:
    """The client's progressToken for this request, if it asked for progress.

    Per the MCP spec a token is a string or integer in `params._meta`, and a
    server may only send progress notifications for a request that carried one.
    Anything else (absent, wrong type) means no progress: never an error, since a
    malformed _meta must not fail an otherwise valid call.
    """
    if method != "tools/call" or not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    tok = meta.get("progressToken")
    if isinstance(tok, str) and tok:
        return tok
    if isinstance(tok, int) and not isinstance(tok, bool):
        return tok
    return None


def _sse_frame(payload: dict) -> bytes:
    """One SSE `message` event carrying a JSON-RPC object."""
    return f"event: message\ndata: {json.dumps(payload, default=str)}\n\n".encode()


def _progress_notification(bus: _ProgressBus, elapsed: float) -> dict:
    """A JSON-RPC notifications/progress message for the current wait.

    `progress` is elapsed seconds, which satisfies the spec's requirement that the
    value increase with every notification for a given token.
    """
    params: dict[str, Any] = {
        "progressToken": bus.token,
        "progress": round(elapsed, 1),
    }
    if bus.total is not None:
        params["total"] = bus.total
    if bus.status:
        params["message"] = bus.status
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": params}


async def _mcp_sse_response(
    request: web.Request,
    hass: HomeAssistant,
    request_id: str,
    extra_headers: dict[str, str],
    bus: _ProgressBus,
    dispatch: Any,
) -> web.StreamResponse:
    """Run `dispatch` and deliver its JSON-RPC result as a single SSE message.

    Same response, different framing: the client gets exactly the payload the
    plain JSON path would have returned, but the connection carries a frame every
    MCP_SSE_KEEPALIVE_SECONDS while the work is in flight. That matters because a
    confirm gate can hold a tools/call for minutes with nothing on the wire, which
    is what an intermediary drops.

    Only this function writes to the stream. Handler code deep in a tool sets
    bus.status instead, so two coroutines can never interleave frames.
    """
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
            "X-Phoenix-Request-ID": request_id,
            **extra_headers,
        },
    )
    await resp.prepare(request)

    task = hass.loop.create_task(dispatch)
    # The dispatch may have already applied a side effect, so it is never
    # cancelled when the client goes away; it runs to completion either way.
    task.add_done_callback(_log_dispatch_task_error)
    started = time.monotonic()
    writable = True
    while True:
        done, _pending = await asyncio.wait({task}, timeout=MCP_SSE_KEEPALIVE_SECONDS)
        if done:
            break
        if not writable:
            continue
        elapsed = time.monotonic() - started
        frame = (
            _sse_frame(_progress_notification(bus, elapsed))
            if bus.token is not None
            else b": keepalive\n\n"
        )
        try:
            await resp.write(frame)
        except Exception:  # noqa: BLE001 - client is gone; keep awaiting the work
            writable = False

    try:
        response_msg = task.result()
    except Exception:
        # Defense in depth: _dispatch_mcp already nets tools/call exceptions, and
        # the batch path isolates per item. Never surface the text to the client.
        _LOGGER.exception("Unhandled exception on the MCP SSE path rid=%s", request_id)
        response_msg = _jsonrpc_error(None, -32603, "Internal error.")

    if writable and response_msg is not None:
        try:
            await resp.write(_sse_frame(response_msg))
        except Exception:  # noqa: BLE001 - client disconnected mid-flight
            pass
    try:
        await resp.write_eof()
    except Exception:  # noqa: BLE001
        pass
    return resp


def _log_dispatch_task_error(task: asyncio.Task) -> None:
    """Never let a detached dispatch task's exception vanish silently."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.error("MCP dispatch task failed: %s", exc, exc_info=exc)


def _reject_non_finite(_value: str) -> None:
    """json.loads parse_constant hook: reject NaN / Infinity / -Infinity.

    Python's json.loads accepts these by default, and they defeat range checks
    (every comparison with NaN is False, so NaN passes _validate_number_range's
    min/max) and can flow through to HA services and device integrations. Raising
    here rejects them at ingestion; the caller's ValueError handler turns it into
    a clean JSON-RPC parse error.
    """
    raise ValueError("Non-finite JSON numbers are not allowed.")


def _origin_rejected(hass: HomeAssistant, request: web.Request, request_id: str) -> web.Response | None:
    """Refuse a browser cross-origin (DNS-rebinding) request per the MCP transport spec.

    The MCP Streamable HTTP transport says a server MUST validate the Origin
    header to prevent DNS-rebinding: a malicious web page resolving a hostname to
    127.0.0.1 could otherwise drive a locally-bound MCP server from the victim's
    browser. Only browsers send Origin, so an ABSENT Origin (every real,
    non-browser MCP client) is allowed; a PRESENT Origin is accepted only when it
    matches one of Home Assistant's own configured URLs. It is validated against
    HA's configured URLs via is_hass_url, never the request's own Host header,
    which a rebinding attacker also forges. This is defense in depth: Phoenix MCP already
    requires its bearer token, which such a page cannot know, so a rebound request
    still gets the uniform 401; the Origin check refuses it one step earlier.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return None
    from homeassistant.helpers.network import is_hass_url  # noqa: PLC0415
    try:
        allowed = is_hass_url(hass, origin)
    except ValueError:
        # A malformed Origin (e.g. "http://[::1") makes HA's URL parser raise;
        # treat any unparseable Origin as not allowed rather than letting it
        # escape as an unauthenticated 500.
        allowed = False
    if allowed:
        return None
    return _error("forbidden", "Origin not allowed.", 403, request_id)


class PhoenixMcpView(PhoenixView):
    """POST /api/phoenix-mcp - MCP Streamable HTTP transport.

    The revisions implemented are const.MCP_PROTOCOL_VERSIONS; read the comment
    there before adding one, since the list is a compliance claim.

    Served from the integration's base path so the URL an operator pastes into
    an MCP client config is as short as possible. The trailing-slash form is
    registered too: aiohttp treats it as a distinct resource and does not
    redirect, so without it a stray slash in a client config would 404.
    """

    url = "/api/phoenix-mcp"
    extra_urls = ["/api/phoenix-mcp/"]
    name = "api:phoenix-mcp:mcp"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Handle one Streamable HTTP request."""
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        client_ip = _get_client_ip(request)

        rejected = _origin_rejected(hass, request, request_id)
        if rejected is not None:
            return rejected

        result = await _async_get_authenticated_token(
            hass, request, data, request_id, "/api/phoenix-mcp"
        )
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        from .const import MAX_REQUEST_BODY_BYTES as _MAX_BODY
        if request.content_length is not None and request.content_length > _MAX_BODY:
            return _error("request_too_large", "Request body too large.", 413, request_id)
        # aiohttp's StreamReader.read(n) is a SHORT read: it returns as soon as
        # the buffer has ANY data, up to n bytes, not once n bytes have
        # arrived. A single read(_MAX_BODY + 1) call could therefore silently
        # truncate a larger tool-call body arriving across multiple
        # TCP/chunked segments. Loop until EOF instead, bailing as soon as the
        # cap is exceeded.
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = await request.content.read(_MAX_BODY - total + 1)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_BODY:
                    return _error("request_too_large", "Request body too large.", 413, request_id)
                at_eof = getattr(request.content, "at_eof", None)
                if callable(at_eof) and at_eof():
                    break
        except Exception:
            return _error("invalid_request", "Failed to read request body.", 400, request_id)
        body_bytes = b"".join(chunks)
        if not body_bytes:
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(_jsonrpc_error(None, -32700, "Parse error.")),
                headers={"X-Phoenix-Request-ID": request_id},
            )
        try:
            parsed = json.loads(body_bytes, parse_constant=_reject_non_finite)
        except ValueError:  # JSONDecodeError, or non-finite rejected above
            _LOGGER.warning(
                "MCP POST: invalid JSON body (%d bytes read, Content-Length %s) rid=%s",
                len(body_bytes), request.content_length, request_id,
            )
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(_jsonrpc_error(None, -32700, "Parse error.")),
                headers={"X-Phoenix-Request-ID": request_id},
            )

        # Response framing, not a session: an SSE-capable client gets the SAME
        # single JSON-RPC response, delivered as one `message` event, with
        # keepalive frames while the work runs. Per the Streamable HTTP spec
        # clients advertise both types, so this is the normal path for real MCP
        # clients; a client asking only for JSON keeps byte-identical behavior.
        wants_sse = "text/event-stream" in request.headers.get("Accept", "")
        base_url = str(request.url.origin())
        rl_headers = _rate_limit_headers(token, rl_result)

        if isinstance(parsed, list):
            if wants_sse and _batch_expects_response(parsed) and 0 < len(parsed) <= MAX_BATCH_ITEMS:
                bus = _ProgressBus()
                _progress_ctx.set(bus)
                return cast(web.Response, await _mcp_sse_response(
                    request, hass, request_id, rl_headers, bus,
                    _dispatch_streamable_batch(
                        parsed, token, hass, data, client_ip, base_url=base_url),
                ))
            return await _handle_streamable_batch(parsed, token, rl_result, hass, data, request_id, client_ip, base_url=base_url)

        if not isinstance(parsed, dict):
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(_jsonrpc_error(None, -32600, "Invalid Request.")),
                headers={"X-Phoenix-Request-ID": request_id},
            )

        body = parsed
        if body.get("jsonrpc") != "2.0":
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(_jsonrpc_error(_sanitize_jsonrpc_id(body.get("id")), -32600, "Invalid Request.")),
                headers={"X-Phoenix-Request-ID": request_id},
            )

        msg_id = _sanitize_jsonrpc_id(body.get("id"))
        # A JSON-RPC notification is a VALID request that omits `id`; it must
        # never receive a response. But `id`-absence alone is not enough: a
        # malformed envelope (bad method, non-object params) is an Invalid
        # Request and MUST get an error with `id: null`, not be silently
        # swallowed. So classify FIRST and only suppress the dispatch path.
        # (`id: null` is a request, not a notification, hence membership, not value.)
        kind, payload = _classify_jsonrpc_message(body)
        is_notification = "id" not in body
        if kind == "accepted":
            return web.Response(status=202, headers={"X-Phoenix-Request-ID": request_id})
        if kind in ("error", "unsupported"):
            # A structurally valid but undispatchable request (positional params)
            # is still a valid notification when it has no id, so it gets no reply;
            # a malformed envelope always gets an error (id null for a no-id body).
            if kind == "unsupported" and is_notification:
                return web.Response(status=202, headers={"X-Phoenix-Request-ID": request_id})
            code, message = payload
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(_jsonrpc_error(msg_id, code, message)),
                headers={"X-Phoenix-Request-ID": request_id},
            )
        method, params = payload

        if method not in _MCP_METHODS:
            # Still routed through the dispatcher rather than answered here, so
            # the error body and the audit row keep one definition; only the
            # HTTP status is decided at this layer. A notification for a method
            # we do not implement is accepted and dropped (202), not refused:
            # the envelope is valid and the sender is not owed a reply.
            response_msg, _m, _r, _o = await _dispatch_mcp(
                method, msg_id, params, token, hass, data, client_ip,
                base_url=base_url,
            )
            if is_notification or response_msg is None:
                return web.Response(status=202, headers={"X-Phoenix-Request-ID": request_id})
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps(response_msg),
                headers={"X-Phoenix-Request-ID": request_id},
            )

        if wants_sse and not is_notification:
            bus = _ProgressBus(token=_progress_token(method, params))
            _progress_ctx.set(bus)
            return cast(web.Response, await _mcp_sse_response(
                request, hass, request_id, rl_headers, bus,
                _dispatch_mcp_result(
                    method, msg_id, params, token, hass, data, client_ip,
                    base_url=base_url),
            ))

        response_msg, _log_method, _log_resource, _outcome = await _dispatch_mcp(
            method, msg_id, params, token, hass, data, client_ip,
            base_url=base_url,
        )

        if is_notification or response_msg is None:
            return web.Response(status=202, headers={"X-Phoenix-Request-ID": request_id})

        resp = web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(response_msg, default=str),
            headers={"X-Phoenix-Request-ID": request_id},
        )
        resp.headers.update(rl_headers)
        return resp


class PhoenixMcpContextView(PhoenixView):
    """GET /api/phoenix-mcp/context - context document listing accessible entities and capability flags.

    Returns plain text by default; pass ?format=json for a structured JSON response.
    """

    url = "/api/phoenix-mcp/context"
    name = "api:phoenix-mcp:context"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(
            hass, request, data, request_id, "/api/phoenix-mcp/context"
        )
        if isinstance(result, web.Response):
            return result
        token, _rl = result

        _log(data, token, request_id=request_id, method="GET", resource="/api/phoenix-mcp/context",
             outcome="allowed", client_ip=client_ip)

        fmt = request.query.get("format", "")
        if fmt == "json":
            body = _build_context_json(token, hass)
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(body, default=str),
                headers={"X-Phoenix-Request-ID": request_id},
            )

        text = _build_context_plain(token, hass)
        return web.Response(
            status=200,
            content_type="text/plain",
            text=text,
            headers={"X-Phoenix-Request-ID": request_id},
        )


ALL_MCP_VIEWS: list[type[PhoenixView]] = [
    PhoenixMcpView,
    PhoenixMcpContextView,
]


# --- Diff builders for Confirm-eligible tools ---------------------------------
# Each builder produces the structured payload shown in the admin Approvals UI.
# Diffs are best-effort: anything missing or non-fatal renders an empty preview
# rather than blocking creation of the pending approval.




def _build_diff_call_service(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Diff for call_service. Resolves entity targets read-only and lists service params."""
    domain = args.get("domain", "")
    service = args.get("service", "")
    # Sanitised like the executor's, so the approver reviews the call that will
    # run. Showing a selector the executor strips would describe a call reaching
    # a whole floor and invite a rejection of something that never had that reach.
    service_data = _sanitize_service_data(args.get("service_data"))
    entity_arg = args.get("entity_id")
    if isinstance(entity_arg, str):
        entity_ids: list[str] = [entity_arg]
    elif isinstance(entity_arg, list):
        entity_ids = [e for e in entity_arg if isinstance(e, str)]
    else:
        entity_ids = []
    preview = {
        "domain": domain,
        "service": service,
        "service_data": service_data,
        "requested_entity_ids": entity_ids,
        "device_id": args.get("device_id"),
        "area_id": args.get("area_id"),
    }
    # Read-only target resolution for the MESA note; the executor re-resolves
    # at approve time. Dual-gate and no-target reloads have no targets and
    # resolve to nothing, so they never carry the note.
    try:
        resolved, _count = resolve_service_targets(
            entity_id=args.get("entity_id"), device_id=args.get("device_id"),
            area_id=args.get("area_id"), service_domain=domain,
            token=token, hass=hass,
        )
    except EntityCreationNotPermitted:
        resolved = []
    mesa_note = _mesa_confirm_annotation(token, hass, [(domain, service, resolved)])
    if mesa_note:
        preview["mesa"] = mesa_note
    return {
        "kind": "service_preview",
        **_summary("call_service.mesa" if mesa_note else "call_service",
                   domain=domain, service=service),
        "target": {"type": "service", "id": f"{domain}/{service}", "label": f"{domain}/{service}"},
        "preview": preview,
    }


def _build_diff_set_integration_enabled(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    entry_id = str(args.get("entry_id") or "").strip()
    enabled = bool(args.get("enabled"))
    entry = hass.config_entries.async_get_entry(entry_id)
    label = f"{entry.domain} ({entry.title})" if entry is not None else entry_id
    return {
        "kind": "system_action",
        **_summary("integration.enable" if enabled else "integration.disable", label=label),
        "target": {"type": "integration", "id": entry_id, "label": label},
        "preview": {
            "domain": entry.domain if entry is not None else None,
            "enabled": enabled,
            "warning": None if enabled else "Disabling unloads the integration and its entities.",
        },
    }


def _build_diff_create_backup(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    name = args.get("name") if isinstance(args.get("name"), str) else None
    agent_ids = args.get("agent_ids") if isinstance(args.get("agent_ids"), list) else None
    return {
        "kind": "system_action",
        **(_summary("create_backup.named", name=name) if name else _summary("create_backup")),
        "target": {"type": "backup", "id": None, "label": name},
        "preview": {"name": name, "agent_ids": agent_ids or "(auto-detected local agent)",
                    "note": "Creates a backup; Phoenix MCP cannot restore backups."},
    }


# Register executors for tools that support the admin-approval gate.
# Each entry maps an MCP tool name to its side-effect-only _execute_X function.
# When an admin approves a pending request, async_execute_approved_tool() invokes the
# matching executor with the saved args.
_register_executor("restart_ha", _execute_restart_ha)
_register_executor("call_service", _execute_call_service)
_register_executor("create_automation", _execute_create_automation)
_register_executor("edit_automation", _execute_edit_automation)
_register_executor("delete_automation", _execute_delete_automation)
_register_executor("create_script", _execute_create_script)
_register_executor("edit_script", _execute_edit_script)
_register_executor("delete_script", _execute_delete_script)
_register_executor("create_scene", _execute_create_scene)
_register_executor("edit_scene", _execute_edit_scene)
_register_executor("delete_scene", _execute_delete_scene)
_register_executor("create_helper", _execute_create_helper)
_register_executor("edit_helper", _execute_edit_helper)
_register_executor("delete_helper", _execute_delete_helper)
_register_executor("write_file", _execute_write_file)
_register_executor("create_blueprint", _execute_create_blueprint)
_register_executor("edit_blueprint", _execute_edit_blueprint)
_register_executor("delete_blueprint", _execute_delete_blueprint)
_register_executor("set_yaml_config", _execute_set_yaml_config)
_register_executor("patch_yaml_config", _execute_patch_yaml_config)
_register_executor("set_esphome_yaml", _execute_set_esphome_yaml)
_register_executor("delete_esphome_yaml", _execute_delete_esphome_yaml)
_register_executor("rename_esphome_device", _execute_rename_esphome_device)
_register_executor("install_esphome_firmware", _execute_install_esphome_firmware)
_register_executor("set_integration_enabled", _execute_set_integration_enabled)
_register_executor("create_backup", _execute_create_backup)
_register_executor("create_dashboard", _execute_create_dashboard)
_register_executor("edit_dashboard", _execute_edit_dashboard)
_register_executor("delete_dashboard", _execute_delete_dashboard)
_register_executor("set_dashboard_config", _execute_set_dashboard_config)
_register_executor("add_dashboard_card", _execute_add_dashboard_card)
_register_executor("edit_dashboard_card", _execute_edit_dashboard_card)
_register_executor("delete_dashboard_card", _execute_delete_dashboard_card)
_register_executor("patch_dashboard", _execute_patch_dashboard)
_register_executor("edit_energy_config", _execute_edit_energy_config)
_register_executor("set_entity", _execute_set_entity)
_register_executor("delete_entity", _execute_delete_entity)
_register_executor("permit_zigbee_join", _execute_permit_zigbee_join)
_register_executor("reconfigure_zigbee_device", _execute_reconfigure_zigbee_device)
_register_executor("remove_zigbee_device", _execute_remove_zigbee_device)
_register_executor("HassSetPosition", _execute_hass_set_position)
_register_executor("HassStopMoving", _execute_hass_stop_moving)
_register_executor("HassTurnOn", _execute_hass_turn_on)
_register_executor("HassTurnOff", _execute_hass_turn_off)
# MESA control_mode:confirm re-execution. Registered but intentionally NOT
# dispatchable from _call_tool, so only the admin approve path can reach it.
_register_executor(MESA_APPROVED_EXECUTOR, _execute_call_service_mesa_approved)
# ---------------------------------------------------------------------------
# Tool dispatch registry.
#
# Replaces a 127-branch if/elif chain whose agreement with the published tool
# catalog was maintained purely by hand. Handlers differ in which pieces of
# request context they need, so _register_tool reads each one's parameter names
# once at import (from the code object, never inspect: the ws_dispatch
# precedent) and _call_tool passes exactly those by keyword.
# ---------------------------------------------------------------------------

_register_tool("get_state", _tool_get_state)
_register_tool("get_states", _tool_get_states)
_register_tool("get_history", _tool_get_history)
_register_tool("get_statistics", _tool_get_statistics)
_register_tool("get_calendar_events", _tool_get_calendar_events)
_register_tool("call_service", _tool_call_service)
_register_tool("get_config", _tool_get_config)
_register_tool("render_template", _tool_render_template)
_register_tool("list_automations", _tool_list_automations)
_register_tool("get_automation", _tool_get_automation)
_register_tool("list_scripts", _tool_list_scripts)
_register_tool("get_script", _tool_get_script)
_register_tool("create_automation", _tool_create_automation)
_register_tool("edit_automation", _tool_edit_automation)
_register_tool("delete_automation", _tool_delete_automation)
_register_tool("restart_ha", _tool_restart_ha)
_register_tool("get_approval_status", _tool_get_approval_status)
_register_tool("wait_for_approval", _tool_wait_for_approval)
_register_tool("get_capability_summary", _tool_get_capability_summary)
_register_tool("get_audit_summary", _tool_get_audit_summary)
_register_tool("GetLiveContext", _tool_get_live_context)
_register_tool("GetDateTime", _tool_get_date_time)
_register_tool("HassTurnOn", _tool_hass_turn_on)
_register_tool("HassTurnOff", _tool_hass_turn_off)
_register_tool("HassLightSet", _tool_hass_light_set)
_register_tool("HassFanSetSpeed", _tool_hass_fan_set_speed)
_register_tool("HassClimateSetTemperature", _tool_hass_climate_set_temperature)
_register_tool("HassSetPosition", _tool_hass_set_position)
_register_tool("HassSetVolume", _tool_hass_set_volume)
_register_tool("HassSetVolumeRelative", _tool_hass_set_volume_relative)
_register_tool("HassMediaPause", _tool_hass_media_pause)
_register_tool("HassMediaUnpause", _tool_hass_media_unpause)
_register_tool("HassMediaNext", _tool_hass_media_next)
_register_tool("HassMediaPrevious", _tool_hass_media_previous)
_register_tool("HassMediaSearchAndPlay", _tool_hass_media_search_and_play)
_register_tool("HassMediaPlayerMute", _tool_hass_media_player_mute)
_register_tool("HassMediaPlayerUnmute", _tool_hass_media_player_unmute)
_register_tool("HassCancelAllTimers", _tool_hass_cancel_all_timers)
_register_tool("HassStopMoving", _tool_hass_stop_moving)
_register_tool("HassVacuumStart", _tool_hass_vacuum_start)
_register_tool("HassVacuumReturnToBase", _tool_hass_vacuum_return_to_base)
_register_tool("HassVacuumCleanArea", _tool_hass_vacuum_clean_area)
_register_tool("HassBroadcast", _tool_hass_broadcast)
_register_tool("get_logs", _tool_get_logs)
_register_tool("get_logbook", _tool_get_logbook)
_register_tool("list_blueprints", _tool_list_blueprints)
_register_tool("get_blueprint", _tool_get_blueprint)
_register_tool("create_blueprint", _tool_create_blueprint)
_register_tool("edit_blueprint", _tool_edit_blueprint)
_register_tool("delete_blueprint", _tool_delete_blueprint)
_register_tool("set_entity", _tool_set_entity)
_register_tool("delete_entity", _tool_delete_entity)
_register_tool("get_radio_network", _tool_get_radio_network)
_register_tool("get_radio_device", _tool_get_radio_device)
_register_tool("permit_zigbee_join", _tool_permit_zigbee_join)
_register_tool("reconfigure_zigbee_device", _tool_reconfigure_zigbee_device)
_register_tool("remove_zigbee_device", _tool_remove_zigbee_device)
_register_tool("create_script", _tool_create_script)
_register_tool("edit_script", _tool_edit_script)
_register_tool("delete_script", _tool_delete_script)
_register_tool("list_areas", _tool_list_areas)
_register_tool("list_floors", _tool_list_floors)
_register_tool("list_zones", _tool_list_zones)
_register_tool("list_devices", _tool_list_devices)
_register_tool("get_device", _tool_get_device)
_register_tool("search_entities", _tool_search_entities)
_register_tool("get_overview", _tool_get_overview)
_register_tool("describe_area", _tool_describe_area)
_register_tool("find_available_actions", _tool_find_available_actions)
_register_tool("get_automation_traces", _tool_get_automation_traces)
_register_tool("get_system_health", _tool_get_system_health)
_register_tool("get_esphome_overview", _tool_get_esphome_overview)
_register_tool("get_esphome_yaml", _tool_get_esphome_yaml)
_register_tool("set_esphome_yaml", _tool_set_esphome_yaml)
_register_tool("delete_esphome_yaml", _tool_delete_esphome_yaml)
_register_tool("validate_esphome_yaml", _tool_validate_esphome_yaml)
_register_tool("get_esphome_board", _tool_get_esphome_board)
_register_tool("get_esphome_component", _tool_get_esphome_component)
_register_tool("get_esphome_automations", _tool_get_esphome_automations)
_register_tool("clean_esphome_build", _tool_clean_esphome_build)
_register_tool("rename_esphome_device", _tool_rename_esphome_device)
_register_tool("compile_esphome_firmware", _tool_compile_esphome_firmware)
_register_tool("install_esphome_firmware", _tool_install_esphome_firmware)
_register_tool("get_esphome_job", _tool_get_esphome_job)
_register_tool("wait_for_esphome_job", _tool_wait_for_esphome_job)
_register_tool("cancel_esphome_job", _tool_cancel_esphome_job)
_register_tool("get_esphome_device_logs", _tool_get_esphome_device_logs)
_register_tool("decode_esphome_backtrace", _tool_decode_esphome_backtrace)
_register_tool("check_config", _tool_check_config)
_register_tool("get_relationships", _tool_get_relationships)
_register_tool("describe_entity", _tool_describe_entity)
_register_tool("whatif", _tool_whatif)
_register_tool("compare_state", _tool_compare_state)
_register_tool("recent_activity", _tool_recent_activity)
_register_tool("dry_run_service", _tool_dry_run_service)
_register_tool("validate_config", _tool_validate_config)
_register_tool("list_scenes", _tool_list_scenes)
_register_tool("get_scene", _tool_get_scene)
_register_tool("create_scene", _tool_create_scene)
_register_tool("edit_scene", _tool_edit_scene)
_register_tool("delete_scene", _tool_delete_scene)
_register_tool("list_helpers", _tool_list_helpers)
_register_tool("create_helper", _tool_create_helper)
_register_tool("edit_helper", _tool_edit_helper)
_register_tool("delete_helper", _tool_delete_helper)
_register_tool("watch_entity", _tool_watch_entity)
_register_tool("list_files", _tool_list_files)
_register_tool("read_file", _tool_read_file)
_register_tool("write_file", _tool_write_file)
_register_tool("get_energy_config", _tool_get_energy_config)
_register_tool("get_solar_forecast", _tool_get_solar_forecast)
_register_tool("edit_energy_config", _tool_edit_energy_config)
_register_tool("get_yaml_config", _tool_get_yaml_config)
_register_tool("set_yaml_config", _tool_set_yaml_config)
_register_tool("patch_yaml_config", _tool_patch_yaml_config)
_register_tool("list_integrations", _tool_list_integrations)
_register_tool("set_integration_enabled", _tool_set_integration_enabled)
_register_tool("list_backups", _tool_list_backups)
_register_tool("create_backup", _tool_create_backup)
_register_tool("list_dashboards", _tool_list_dashboards)
_register_tool("list_dashboard_cards", _tool_list_dashboard_cards)
_register_tool("create_dashboard", _tool_create_dashboard)
_register_tool("edit_dashboard", _tool_edit_dashboard)
_register_tool("delete_dashboard", _tool_delete_dashboard)
_register_tool("get_dashboard_config", _tool_get_dashboard_config)
_register_tool("set_dashboard_config", _tool_set_dashboard_config)
_register_tool("add_dashboard_card", _tool_dashboard_card, op="add", tool_name="add_dashboard_card")
_register_tool("edit_dashboard_card", _tool_dashboard_card, op="edit", tool_name="edit_dashboard_card")
_register_tool("delete_dashboard_card", _tool_dashboard_card, op="delete", tool_name="delete_dashboard_card")
_register_tool("patch_dashboard", _tool_patch_dashboard)

# The mesa_* tools take (tool_name, args, ...) and are scoped by mesa_tools.
for _mesa_tool_name in sorted(MESA_TOOL_NAMES):
    _register_tool(_mesa_tool_name, _tool_mesa, mesa_tool_name=_mesa_tool_name)
