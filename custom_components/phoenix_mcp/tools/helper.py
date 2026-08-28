"""Helper authoring and settings over Home Assistant's own helper machinery.

The storage-based helper domains only, listed in HELPER_TYPES: each publishes a
uniform {type}/create|update|delete WS command whose item id key is
"{type}_id", which is what makes one set of handlers cover all twelve.
Config-flow helpers use HA's config-entry flow manager instead. create_helper
can replay their cumulative form steps, returning the next real schema until a
last step is supplied; only that last step is approval-gated and submitted.

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

import copy
from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, cast

import voluptuous as vol
import voluptuous_serialize

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import slugify

from ..const import CAP_DENY
from ..data import PhoenixData
from ..mesa import (
    MesaRuntime,
    RegistryMesaDecision,
    async_create_mesa_approval,
    evaluate_registry_action,
    fire_mesa_blocked_event,
)
from ..ws_dispatch import WsDispatchError, async_ws_command
from ..helpers import content_hash, dict_arg, diff_summary_fields as _summary, effective_cap, version_summary_fields as _version_summary
from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _approved_exec_ctx,
    _cas_conflict,
    _gate,
    _mesa_advisory_ctx,
    _pending_or_inline,
    _record_version,
    _restore_ctx,
    redaction_sentinel_path,
    _tool_error,
    _tool_success,
    _truncate,
)
from ..policy_engine import (
    _ENTITY_ID_RE,
    Permission,
    filter_entities_for_token,
    resolve,
    resolve_registry_access,
)
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
    "input_select", "input_datetime", "input_button", "counter", "timer", "schedule", "zone", "tag", "person",
})

_ZONE_CONFIG_FIELDS = frozenset({
    "name", "latitude", "longitude", "radius", "passive", "icon",
})
_TAG_CREATE_CONFIG_FIELDS = frozenset({"tag_id", "name", "description"})
_TAG_UPDATE_CONFIG_FIELDS = frozenset({"name", "description"})
_PERSON_CONFIG_FIELDS = frozenset({"name", "device_trackers"})
_SCOPED_STORAGE_HELPER_TYPES = frozenset({"person", "tag", "zone"})
_HELPER_CONTEXT_FINGERPRINT = "_phoenix_helper_context_fingerprint"
_HELPER_MESA_FINGERPRINT = "_phoenix_helper_mesa_fingerprint"
_HELPER_RESTORE_ID = "_phoenix_helper_restore_id"

# A generated OTP cannot be created noninteractively: HA shows the secret and
# requires a current TOTP code in a later confirmation step. Keeping it out is a
# deliberate safety boundary, not a missing schema mapping.
_EXCLUDED_CONFIG_FLOW_HELPERS = frozenset({"otp"})
_MAX_HELPER_FLOW_STEPS = 8


# ---------------------------------------------------------------------------
# Helper CRUD (cap_helper_write) via in-process WS command dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HelperMesaContext:
    """One exact inherited-MESA snapshot for a helper mutation."""

    action: str
    decisions: tuple[tuple[str, RegistryMesaDecision], ...]
    decision: str
    fingerprint: str
    warnings: tuple[str, ...]

    @property
    def profile_fingerprints(self) -> frozenset[str]:
        return frozenset(
            decision.profile_fingerprint
            for _entity_id, decision in self.decisions
            if isinstance(decision.profile_fingerprint, str)
        )


def _helper_mesa_context(
    data: PhoenixData,
    token: TokenRecord,
    entity_ids: list[str] | tuple[str, ...],
    *,
    action: str,
    service_data: dict[str, Any] | None = None,
    session_id: str,
) -> _HelperMesaContext | tuple[dict, str, str]:
    """Resolve helper.<action> for every exact entity, failing closed."""
    entities = tuple(sorted(set(entity_ids)))
    if not entities:
        return (
            _tool_error("MESA could not resolve an entity for this helper; nothing changed."),
            "denied",
            f"helper:{action}",
        )
    decisions: list[tuple[str, RegistryMesaDecision]] = []
    try:
        for entity_id in entities:
            if not isinstance(data.mesa, MesaRuntime) and data.mesa_setup_failed is not True:
                decisions.append((
                    entity_id,
                    RegistryMesaDecision(
                        decision="allow",
                        rule="mesa:unavailable_in_test_context",
                        reason="MESA has no runtime in this host context.",
                    ),
                ))
                continue
            decisions.append((
                entity_id,
                evaluate_registry_action(
                    data,
                    token,
                    entity_id,
                    action=action,
                    registry_domain="helper",
                    service_data=dict(service_data or {}),
                    session_id=session_id,
                ),
            ))
    except Exception:  # noqa: BLE001 - a safety resolver failure must block
        _LOGGER.exception("MESA helper %s evaluation failed for %s", action, entities)
        return (
            _tool_error("MESA safety evaluation failed; no helper change was made."),
            "denied",
            f"helper:{action}",
        )
    aggregate = (
        "deny" if any(item.decision == "deny" for _eid, item in decisions)
        else "confirm" if any(item.decision == "confirm" for _eid, item in decisions)
        else "allow"
    )
    warnings = tuple(
        warning
        for _entity_id, decision in decisions
        for warning in decision.warnings
    )
    fingerprint_doc = {
        "action": action,
        "entities": [
            {
                "entity_id": entity_id,
                "decision": decision.decision,
                "rule": decision.rule,
                "reason": decision.reason,
                "effective_rule": decision.effective_rule,
                "profile_fingerprint": decision.profile_fingerprint,
            }
            for entity_id, decision in decisions
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_doc, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()
    return _HelperMesaContext(
        action=action,
        decisions=tuple(decisions),
        decision=aggregate,
        fingerprint=fingerprint,
        warnings=warnings,
    )


def _helper_mesa_preview(context: _HelperMesaContext) -> dict[str, Any]:
    return {
        "decision": context.decision,
        "action": f"helper.{context.action}",
        "confirm_entities": [
            entity_id
            for entity_id, decision in context.decisions
            if decision.decision == "confirm"
        ],
        "allowed_entities": [
            entity_id
            for entity_id, decision in context.decisions
            if decision.decision == "allow"
        ],
        "blocked": [
            {"entity_id": entity_id, "rule": decision.rule}
            for entity_id, decision in context.decisions
            if decision.decision == "deny"
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "decision": decision.decision,
                "rule": decision.rule,
                "reason": decision.reason,
                "effective_rule": decision.effective_rule,
            }
            for entity_id, decision in context.decisions
        ],
        "warnings": list(context.warnings),
        "fingerprint": context.fingerprint[:16],
    }


def _helper_mesa_error(
    context: _HelperMesaContext,
    *,
    changed: bool = False,
) -> tuple[dict, str, str]:
    message = (
        "MESA protection changed after this helper request was reviewed; nothing changed."
        if changed
        else "MESA blocked this helper change."
    )
    return (
        _tool_error(json.dumps({
            "error": message,
            "mesa": _helper_mesa_preview(context),
        }, default=str)),
        "denied",
        f"helper:{context.action}",
    )


def _fire_helper_mesa_blocks(
    hass: HomeAssistant,
    token: TokenRecord,
    context: _HelperMesaContext,
) -> None:
    blocked = [
        (entity_id, decision.rule, decision.reason)
        for entity_id, decision in context.decisions
        if decision.decision == "deny"
    ]
    if blocked:
        fire_mesa_blocked_event(hass, token, blocked)


def _helper_mesa_diff(diff: dict, context: _HelperMesaContext) -> dict:
    preview = diff.setdefault("preview", {})
    if isinstance(preview, dict):
        preview["mesa"] = _helper_mesa_preview(context)
    return diff


async def _helper_mesa_approval_gate(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    context: _HelperMesaContext,
    tool_name: str,
    request_id: str,
    client_ip: str | None,
    diff_builder: Any,
) -> tuple[dict, tuple[dict, str, str] | None]:
    """Merge inherited MESA with the ordinary helper capability approval."""
    if context.decision == "deny":
        _fire_helper_mesa_blocks(hass, token, context)
        return args, _helper_mesa_error(context)
    approval_args = {**args, _HELPER_MESA_FINGERPRINT: context.fingerprint}

    async def _diff() -> dict:
        value = diff_builder()
        if hasattr(value, "__await__"):
            value = await value
        return _helper_mesa_diff(value, context)

    blocked = await _gate(
        "cap_helper_write",
        token,
        hass,
        data,
        tool_name=tool_name,
        args=approval_args,
        request_id=request_id,
        client_ip=client_ip,
        diff=_diff,
    )
    if blocked is not None:
        return approval_args, blocked
    if context.decision == "confirm":
        approval = await async_create_mesa_approval(
            hass,
            data,
            token,
            args=approval_args,
            diff=await _diff(),
            request_id=request_id,
            client_ip=client_ip,
            tool_name=tool_name,
        )
        return approval_args, await _pending_or_inline(
            hass, data, token, approval
        )
    return approval_args, None


def _enforce_helper_mesa_context(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    entity_ids: list[str] | tuple[str, ...],
    *,
    action: str,
    service_data: dict[str, Any] | None = None,
    session_id: str,
) -> _HelperMesaContext | tuple[dict, str, str]:
    context = _helper_mesa_context(
        data,
        token,
        entity_ids,
        action=action,
        service_data=service_data,
        session_id=session_id,
    )
    if isinstance(context, tuple):
        return context
    saved = args.get(_HELPER_MESA_FINGERPRINT)
    if isinstance(saved, str) and saved != context.fingerprint:
        return _helper_mesa_error(context, changed=True)
    restoring = _restore_ctx.get() is not None
    confirmed = (
        _approved_exec_ctx.get()
        and isinstance(saved, str)
        and saved == context.fingerprint
    ) or restoring
    if context.decision == "deny" or (
        context.decision == "confirm" and not confirmed
    ):
        _fire_helper_mesa_blocks(hass, token, context)
        return _helper_mesa_error(context)
    if context.warnings:
        _mesa_advisory_ctx.set(True)
    return context


def _helper_create_mesa_entity(
    helper_type: str, args: dict, hass: HomeAssistant
) -> str:
    restore_id = args.get(_HELPER_RESTORE_ID)
    if isinstance(restore_id, str) and restore_id.strip():
        return f"{helper_type}.{slugify(restore_id)}"
    config = args.get("config")
    suggestion: str | None = None
    if isinstance(config, dict):
        if helper_type == "tag" and isinstance(config.get("tag_id"), str):
            suggestion = config["tag_id"]
        elif isinstance(config.get("name"), str):
            suggestion = config["name"]
    if suggestion is None:
        steps = args.get("flow_steps")
        if isinstance(steps, list):
            for step in reversed(steps):
                step_data = step.get("data") if isinstance(step, dict) else None
                if isinstance(step_data, dict) and isinstance(step_data.get("name"), str):
                    suggestion = step_data["name"]
                    break
    if suggestion is not None:
        base = slugify(suggestion) or "__phoenix_helper_create__"
        if helper_type in HELPER_TYPES and helper_type != "tag":
            used = {
                entry.unique_id
                for entry in er.async_get(hass).entities.values()
                if entry.domain == helper_type and entry.platform == helper_type
            }
            proposal = base
            attempt = 1
            while proposal in used:
                attempt += 1
                proposal = f"{base}_{attempt}"
            base = proposal
        return f"{helper_type}.{base}"
    return f"{helper_type}.__phoenix_helper_create__"


def _valid_helper_type(helper_type: Any) -> bool:
    return isinstance(helper_type, str) and helper_type in HELPER_TYPES


def _storage_items(helper_type: str, result: Any) -> list[dict[str, Any]] | None:
    """Normalize a storage collection list without admitting YAML people."""
    if helper_type == "person":
        if not isinstance(result, dict) or not isinstance(result.get("storage"), list):
            return None
        result = result["storage"]
    if not isinstance(result, list):
        return None
    return [item for item in result if isinstance(item, dict)]


def _person_trackers(item: dict[str, Any]) -> tuple[str, ...] | None:
    """Return a validated, duplicate-free person tracker relationship."""
    value = item.get("device_trackers", [])
    if not isinstance(value, list):
        return None
    trackers = tuple(value)
    if any(
        not isinstance(entity_id, str)
        or not _ENTITY_ID_RE.fullmatch(entity_id)
        or not entity_id.startswith("device_tracker.")
        for entity_id in trackers
    ) or len(set(trackers)) != len(trackers):
        return None
    return trackers


def _person_relationships_accessible(
    item: dict[str, Any], token: TokenRecord, hass: HomeAssistant, *, write: bool,
) -> bool:
    trackers = _person_trackers(item)
    if trackers is None:
        return False
    allowed = {Permission.WRITE} if write else {Permission.READ, Permission.WRITE}
    return all(
        resolve_registry_access(entity_id, token, hass) in allowed
        for entity_id in trackers
    )


def _public_helper_config(helper_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """Project stored helper data into the MCP/version-history privacy boundary."""
    if helper_type != "person":
        return dict(item)
    trackers = _person_trackers(item)
    return {
        **({"id": item["id"]} if isinstance(item.get("id"), str) else {}),
        **({"name": item["name"]} if isinstance(item.get("name"), str) else {}),
        "device_trackers": list(trackers or ()),
        "has_user_link": bool(item.get("user_id")),
        "has_picture": bool(item.get("picture")),
    }


async def _is_config_flow_helper(hass: HomeAssistant, helper_type: Any) -> bool:
    """Whether HA classifies this domain as a creatable helper config flow."""
    if (
        not isinstance(helper_type, str)
        or not helper_type.strip()
        or helper_type in HELPER_TYPES
        or helper_type in _EXCLUDED_CONFIG_FLOW_HELPERS
    ):
        return False
    try:
        integration = await async_get_integration(hass, helper_type)
    except (IntegrationNotFound, ValueError):
        return False
    return (
        integration.integration_type == "helper"
        and integration.manifest.get("config_flow") is True
    )


def _helper_flow_steps_error(steps: Any) -> str | None:
    """Cheap structural validation before a helper creation flow is opened."""
    if not isinstance(steps, list):
        return "flow_steps must be an array. Use an empty array to read the first form."
    if len(steps) > _MAX_HELPER_FLOW_STEPS:
        return f"flow_steps may contain at most {_MAX_HELPER_FLOW_STEPS} steps."
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return f"flow_steps[{index}] must be an object."
        if not isinstance(step.get("step_id"), str) or not step["step_id"].strip():
            return f"flow_steps[{index}].step_id must be a non-empty string."
        if not isinstance(step.get("data"), dict):
            return f"flow_steps[{index}].data must be an object."
    return None


def _flow_step_body(helper_type: str, result: Any) -> dict[str, Any] | None:
    """Public, serializable projection of one HA config-flow form."""
    if result.get("type") != FlowResultType.FORM:
        return None
    schema = _options_schema_json(result)
    if schema is None:
        return None
    return {
        "status": "needs_input",
        "helper_type": helper_type,
        "step": {
            "step_id": result.get("step_id"),
            "last_step": result.get("last_step"),
            "schema": schema,
            "errors": result.get("errors") or {},
            "description_placeholders": result.get("description_placeholders") or {},
        },
        "note": (
            "Append this step_id and its data to flow_steps, then call create_helper "
            "again. Phoenix replays and closes unfinished flows; approval is requested "
            "only when the final form is supplied."
        ),
    }


async def _abort_create_flow(hass: HomeAssistant, flow_id: str | None) -> None:
    """Close an unfinished helper creation flow without leaking a UI dialog."""
    if not flow_id:
        return
    try:
        hass.config_entries.flow.async_abort(flow_id)
    except Exception:  # noqa: BLE001 - already-finished and already-gone are fine
        _LOGGER.debug("Could not abort helper creation flow %s", flow_id, exc_info=True)


async def _prepare_config_flow_helper(
    hass: HomeAssistant,
    helper_type: str,
    steps: list[dict[str, Any]],
    token: TokenRecord,
) -> tuple[str, Any]:
    """Replay a helper flow without ever submitting its final form.

    Returns ("form", body) when more input is needed, ("ready", None) when the
    supplied final form is schema-valid and may go to approval, or ("error",
    message) on refusal. Every flow opened here is aborted in the finally block.
    """
    flow_id = None
    try:
        result = await hass.config_entries.flow.async_init(
            helper_type, context={"source": SOURCE_USER}
        )
        flow_id = result.get("flow_id")
        if result.get("type") == FlowResultType.ABORT:
            return "error", f"Home Assistant refused this helper flow: {result.get('reason')}."

        for index, supplied in enumerate(steps):
            body = _flow_step_body(helper_type, result)
            if body is None:
                return "error", "This helper's creation flow is not a supported form flow."
            current = body["step"]
            if supplied["step_id"] != current["step_id"]:
                return "error", (
                    f"flow_steps[{index}].step_id must be '{current['step_id']}', "
                    f"not '{supplied['step_id']}'."
                )
            denied = _unwritable_option_entities(supplied["data"], token, hass)
            if denied:
                return "denied", denied

            if current["last_step"] is True:
                if index != len(steps) - 1:
                    return "error", "The final helper form must be the last item in flow_steps."
                schema = result.get("data_schema")
                try:
                    if schema is not None:
                        schema(dict(supplied["data"]))
                except vol.Invalid as err:
                    return "error", f"The final helper form was rejected: {err}."
                return "ready", None

            # Submitting a form whose last_step is unknown could create an entry
            # during this side-effect-free preflight. Fail closed. HA's schema
            # config flows, including history_stats and mold_indicator, mark it.
            if current["last_step"] is not False:
                return "error", (
                    "This helper flow does not identify its final form, so Phoenix "
                    "cannot preflight it without risking an unapproved creation."
                )
            try:
                result = await hass.config_entries.flow.async_configure(
                    flow_id, dict(supplied["data"])
                )
            except vol.Invalid as err:
                return "error", f"flow_steps[{index}] was rejected: {err}."
            if result.get("type") == FlowResultType.ABORT:
                return "error", f"Home Assistant refused this helper flow: {result.get('reason')}."
            if result.get("type") == FlowResultType.CREATE_ENTRY:
                # last_step=False promised this could not happen. If an
                # integration violates that contract, creation already occurred;
                # surface it loudly rather than pretending preflight was clean.
                entry = result.get("result")
                if entry is not None:
                    await hass.config_entries.async_remove(entry.entry_id)
                return "error", "This helper flow finished before its declared final form."
            if result.get("errors"):
                error_body = _flow_step_body(helper_type, result)
                if error_body is None:
                    return "error", "This helper's rejected form could not be described."
                return "form", error_body

        body = _flow_step_body(helper_type, result)
        if body is None:
            return "error", "This helper's creation flow is not a supported form flow."
        return "form", body
    except Exception:  # noqa: BLE001 - integration flow failures become clean tool errors
        _LOGGER.exception("Could not preflight helper creation flow for %s", helper_type)
        return "error", "Could not open or validate this helper's creation flow."
    finally:
        await _abort_create_flow(hass, flow_id)


def _resolve_helper_entity_id(hass: HomeAssistant, helper_type: str, helper_id: str) -> str | None:
    """Map a storage-helper id back to its entity_id via the registry, or None.

    list_helpers exposes entry.unique_id as the editable helper_id, so the reverse
    lookup matches on (domain == helper_type, unique_id == helper_id). Used by
    edit/delete as an existence check (the helper must resolve to a real
    entity). Ordinary authoring is cap-gated; sensitive zone/tag/person writes add
    entity scope and storage-membership checks.
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
    scoped_storage_items: dict[str, dict[str, dict[str, Any]]] = {}
    for helper_type in _SCOPED_STORAGE_HELPER_TYPES:
        if type_filter not in (None, helper_type):
            continue
        if helper_type not in hass.config.components:
            continue
        try:
            items = await async_ws_command(hass, f"{helper_type}/list", {})
        except WsDispatchError:
            if type_filter == helper_type:
                return (
                    _tool_error(
                        f"{helper_type.capitalize()} helper storage is unavailable."
                    ),
                    "invalid_request",
                    "list_helpers",
                )
            continue
        storage_items = _storage_items(helper_type, items)
        if storage_items is None:
            if type_filter == helper_type:
                return (
                    _tool_error(
                        f"{helper_type.capitalize()} helper storage is unavailable."
                    ),
                    "invalid_request",
                    "list_helpers",
                )
            continue
        scoped_storage_items[helper_type] = {
            row["id"]: row for row in storage_items
            if isinstance(row.get("id"), str)
        }
    helpers: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        domain = e["entity_id"].split(".")[0]
        if domain not in HELPER_TYPES:
            continue
        if type_filter and domain != type_filter:
            continue
        entry = registry.async_get(e["entity_id"])
        stored_item = (
            scoped_storage_items.get(domain, {}).get(entry.unique_id)
            if entry is not None else None
        )
        if domain in _SCOPED_STORAGE_HELPER_TYPES and stored_item is None:
            continue
        if domain == "person" and (
            stored_item is None
            or not _person_relationships_accessible(
                stored_item, token, hass, write=False
            )
        ):
            continue
        helper = {
            "entity_id": e["entity_id"],
            "helper_type": domain,
            "name": e.get("attributes", {}).get("friendly_name"),
            "helper_id": entry.unique_id if entry is not None else None,
        }
        if domain == "person" and stored_item is not None:
            public = _public_helper_config(domain, stored_item)
            helper.update({
                "device_trackers": public["device_trackers"],
                "tracker_count": len(public["device_trackers"]),
                "has_user_link": public["has_user_link"],
            })
        helpers.append(helper)
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
    if args.get("helper_type") == "zone" and isinstance(config, dict):
        unknown = sorted(set(config) - _ZONE_CONFIG_FIELDS)
        if unknown:
            return (
                _tool_error(
                    "Unknown zone configuration fields: "
                    f"{', '.join(unknown)}. Allowed fields are: "
                    f"{', '.join(sorted(_ZONE_CONFIG_FIELDS))}."
                ),
                "invalid_request",
                tool_name,
            )
    if args.get("helper_type") == "tag" and isinstance(config, dict):
        allowed = _TAG_UPDATE_CONFIG_FIELDS if require_id else _TAG_CREATE_CONFIG_FIELDS
        unknown = sorted(set(config) - allowed)
        if unknown:
            return (
                _tool_error(
                    "Unknown tag configuration fields: "
                    f"{', '.join(unknown)}. Allowed fields are: "
                    f"{', '.join(sorted(allowed))}."
                ),
                "invalid_request",
                tool_name,
            )
        if tool_name == "edit_helper" and not config:
            return (
                _tool_error("Tag edit config must include name or description."),
                "invalid_request",
                tool_name,
            )
        if not require_id and (
            not isinstance(config.get("tag_id"), str)
            or not config["tag_id"].strip()
        ):
            return (
                _tool_error("Tag creation requires a non-empty string tag_id."),
                "invalid_request",
                tool_name,
            )
        if "name" in config and (
            not isinstance(config["name"], str) or not config["name"].strip()
        ):
            return (
                _tool_error("Tag name must be a non-empty string."),
                "invalid_request",
                tool_name,
            )
        if "description" in config and not isinstance(config["description"], str):
            return (
                _tool_error("Tag description must be a string."),
                "invalid_request",
                tool_name,
            )
    if args.get("helper_type") == "person" and isinstance(config, dict):
        unknown = sorted(set(config) - _PERSON_CONFIG_FIELDS)
        if unknown:
            return (
                _tool_error(
                    "Unknown person configuration fields: "
                    f"{', '.join(unknown)}. Phoenix accepts only name and "
                    "device_trackers; user links and pictures remain private "
                    "and must be managed in Home Assistant."
                ),
                "invalid_request",
                tool_name,
            )
        if not require_id and (
            not isinstance(config.get("name"), str) or not config["name"].strip()
        ):
            return (
                _tool_error("Person creation requires a non-empty string name."),
                "invalid_request",
                tool_name,
            )
        if "name" in config and (
            not isinstance(config["name"], str) or not config["name"].strip()
        ):
            return (
                _tool_error("Person name must be a non-empty string."),
                "invalid_request",
                tool_name,
            )
        if tool_name == "edit_helper" and not config:
            return (
                _tool_error("Person edit config must include name or device_trackers."),
                "invalid_request",
                tool_name,
            )
        if "device_trackers" in config and _person_trackers(config) is None:
            return (
                _tool_error(
                    "device_trackers must be a duplicate-free array of "
                    "device_tracker entity IDs."
                ),
                "invalid_request",
                tool_name,
            )
    return None


def _scoped_helper_create_scope_error(
    helper_type: str, token: TokenRecord
) -> str | None:
    """Require inherited WRITE scope before creating a scoped helper entity.

    A new entity cannot already have an entity-specific grant. Requiring a
    GREEN domain grant (or pass-through scope) ensures the entity will be
    visible and manageable after it materializes. The executor still verifies
    the concrete entity because an entity-specific RED rule may override the
    inherited grant once the final entity_id is known.
    """
    if token.pass_through:
        return None
    domain = token.permissions.domains.get(helper_type)
    if domain is not None and domain.state == "GREEN":
        return None
    return (
        f"Creating a {helper_type} requires write access to the entire "
        f"{helper_type} domain, because the new {helper_type} does not have an "
        "entity ID that can be scoped yet."
    )


async def _tag_create_absence_error(
    args: dict, hass: HomeAssistant, tool_name: str
) -> tuple[dict, str, str] | None:
    """Require an authoritative absent tag id before create and approval."""
    if args.get("helper_type") != "tag":
        return None
    tag_id = str(dict_arg(args.get("config")).get("tag_id") or "").strip()
    try:
        items = await async_ws_command(hass, "tag/list", {})
    except WsDispatchError:
        return (
            _tool_error("Tag helper storage is unavailable; nothing changed."),
            "invalid_request",
            tool_name,
        )
    if not isinstance(items, list):
        return (
            _tool_error("Tag helper storage is unavailable; nothing changed."),
            "invalid_request",
            tool_name,
        )
    if any(
        isinstance(row, dict) and row.get("id") == tag_id for row in items
    ):
        return (
            _tool_error("This tag_id is unavailable."),
            "invalid_request",
            tool_name,
        )
    return None


def _person_relationship_scope_error(
    config: dict[str, Any], token: TokenRecord, hass: HomeAssistant,
) -> str | None:
    """Require WRITE for every person tracker without identifying hidden links."""
    if not _person_relationships_accessible(config, token, hass, write=True):
        return (
            "This person references an unavailable or out-of-scope device "
            "tracker. Write access to every linked tracker is required; nothing changed."
        )
    return None


def _scoped_helper_approval_context(
    helper_type: str, entity_id: str, item: dict[str, Any]
) -> dict[str, Any]:
    """Return the stable configuration that an approval actually reviews."""
    if helper_type == "tag":
        item = {
            key: item[key] for key in ("id", "name", "description") if key in item
        }
    elif helper_type == "person":
        # The returned value is immediately hashed into the approval arguments.
        # Keeping the raw private binding inside the hash detects user-link and
        # picture drift while never exposing either value in a diff or response.
        item = {
            key: item[key]
            for key in ("id", "name", "user_id", "device_trackers", "picture")
            if key in item
        }
    return {"entity_id": entity_id, "config": item}


async def _scoped_helper_context(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str,
) -> tuple[dict[str, Any] | None, tuple[dict, str, str] | None]:
    """Resolve one writable scoped storage helper and its stored config.

    The storage list proves the item belongs to the editable collection rather
    than merely sharing its entity domain. A list failure is reported as
    unavailable, never collapsed into an authoritative not-found result.
    """
    helper_type = str(args.get("helper_type") or "")
    helper_id = str(args.get("helper_id") or "").strip()
    entity_id = _resolve_helper_entity_id(hass, helper_type, helper_id)
    if entity_id is None or resolve(entity_id, token, hass) != Permission.WRITE:
        return None, (
            _tool_error("Helper not found."), "not_found",
            f"helper:{helper_type}:{helper_id}",
        )
    try:
        items = await async_ws_command(hass, f"{helper_type}/list", {})
    except WsDispatchError:
        return None, (
            _tool_error(
                f"{helper_type.capitalize()} helper storage is unavailable; "
                "nothing changed."
            ),
            "invalid_request",
            tool_name,
        )
    storage_items = _storage_items(helper_type, items)
    if storage_items is None:
        return None, (
            _tool_error(
                f"{helper_type.capitalize()} helper storage is unavailable; "
                "nothing changed."
            ),
            "invalid_request",
            tool_name,
        )
    item = next(
        (row for row in storage_items if row.get("id") == helper_id),
        None,
    )
    if item is None:
        return None, (
            _tool_error("Helper not found."), "not_found",
            f"helper:{helper_type}:{helper_id}",
        )
    if helper_type == "person" and not _person_relationships_accessible(
        item, token, hass, write=True
    ):
        return None, (
            _tool_error(
                "Helper not found or one of its relationships is outside this "
                "token's write scope."
            ),
            "not_found",
            f"helper:{helper_type}:{helper_id}",
        )
    return {"entity_id": entity_id, "config": item}, None


async def _bind_scoped_helper_context(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str,
) -> tuple[dict, tuple[dict, str, str] | None]:
    """Attach an exact stable snapshot to edit/delete approval arguments."""
    helper_type = str(args.get("helper_type") or "")
    if helper_type not in _SCOPED_STORAGE_HELPER_TYPES:
        return args, None
    context, error = await _scoped_helper_context(args, token, hass, tool_name)
    if error is not None:
        return args, error
    assert context is not None
    bound = dict(args)
    bound[_HELPER_CONTEXT_FINGERPRINT] = content_hash(
        _scoped_helper_approval_context(
            helper_type, context["entity_id"], context["config"]
        )
    )
    return bound, None


async def _revalidate_scoped_helper_context(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str,
) -> tuple[dict[str, Any] | None, tuple[dict, str, str] | None]:
    """Re-check helper scope, storage ownership, and any approval snapshot."""
    helper_type = str(args.get("helper_type") or "")
    if helper_type not in _SCOPED_STORAGE_HELPER_TYPES:
        return None, None
    context, error = await _scoped_helper_context(args, token, hass, tool_name)
    if error is not None:
        return None, error
    assert context is not None
    saved = args.get(_HELPER_CONTEXT_FINGERPRINT)
    current = _scoped_helper_approval_context(
        helper_type, context["entity_id"], context["config"]
    )
    if saved is not None and content_hash(current) != saved:
        return None, (
            _tool_error(
                f"This {helper_type} changed after the request was reviewed; "
                "nothing changed. "
                "Read it again and submit a new request."
            ),
            "invalid_request",
            tool_name,
        )
    return context, None


async def _tool_create_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a helper (Confirm-gated)."""
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "create_helper"
    helper_type = args.get("helper_type")
    if _valid_helper_type(helper_type):
        if "flow_steps" in args:
            return (
                _tool_error("Storage helpers use config, not flow_steps."),
                "invalid_request", "create_helper",
            )
        pre = _helper_write_precheck(args, "create_helper", require_id=False)
        if pre is not None:
            return pre
        if helper_type in _SCOPED_STORAGE_HELPER_TYPES:
            if scope_error := _scoped_helper_create_scope_error(
                str(helper_type), token
            ):
                return _tool_error(scope_error), "denied", "create_helper"
            if absence_error := await _tag_create_absence_error(
                args, hass, "create_helper"
            ):
                return absence_error
            if helper_type == "person" and (
                relationship_error := _person_relationship_scope_error(
                    dict_arg(args.get("config")), token, hass
                )
            ):
                return _tool_error(relationship_error), "denied", "create_helper"
    else:
        if not await _is_config_flow_helper(hass, helper_type):
            extra = (
                " OTP is deliberately excluded because its generated-token flow "
                "requires a live confirmation code."
                if helper_type == "otp" else ""
            )
            return (
                _tool_error(
                    "helper_type must be a supported storage helper or a Home Assistant "
                    f"integration classified as a helper with a config flow.{extra}"
                ),
                "invalid_request", "create_helper",
            )
        if "config" in args:
            return (
                _tool_error("Config-flow helpers use flow_steps, not config."),
                "invalid_request", "create_helper",
            )
        steps = args.get("flow_steps")
        if error := _helper_flow_steps_error(steps):
            return _tool_error(error), "invalid_request", "create_helper"
        flow_steps = cast(list[dict[str, Any]], steps)
        status, detail = await _prepare_config_flow_helper(
            hass, str(helper_type), flow_steps, token
        )
        if status == "form":
            return (
                _tool_success(json.dumps(detail, default=str)),
                "allowed", f"helper_flow:{helper_type}",
            )
        if status == "denied":
            return (
                _tool_error(
                    "These entities are outside this token's write scope: "
                    f"{', '.join(detail)}. A helper may expose or actuate every entity "
                    "it references, so all of them must already be writable."
                ),
                "denied", "create_helper",
            )
        if status != "ready":
            return _tool_error(str(detail)), "invalid_request", "create_helper"
    create_entity = _helper_create_mesa_entity(str(helper_type), args, hass)
    mesa_context = _helper_mesa_context(
        data,
        token,
        [create_entity],
        action="create",
        service_data={
            "helper_type": helper_type,
            "config": args.get("config"),
            "flow_steps": args.get("flow_steps"),
        },
        session_id=request_id or "create_helper",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    approval_args, blocked = await _helper_mesa_approval_gate(
        args,
        token,
        hass,
        data,
        context=mesa_context,
        tool_name="create_helper",
        request_id=request_id,
        client_ip=client_ip,
        diff_builder=lambda: _build_diff_create_helper(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_helper(approval_args, token, hass, data)


async def _read_helper_config(hass: HomeAssistant, helper_type: str, helper_id: str) -> dict | None:
    """Return a helper's current stored config (for version-history `before`), or None.

    Best-effort: a failure to read the prior config must not block the edit/delete,
    so any dispatch error degrades to no `before` rather than raising.
    """
    try:
        result = await async_ws_command(hass, f"{helper_type}/list", {})
    except WsDispatchError:
        return None
    items = _storage_items(helper_type, result)
    if items is None:
        return None
    return next(
        (it for it in items if isinstance(it, dict) and it.get("id") == helper_id), None
    )


async def _execute_create_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    helper_type = args.get("helper_type")
    config = args.get("config")
    create_entity = _helper_create_mesa_entity(str(helper_type), args, hass)
    mesa_context = _enforce_helper_mesa_context(
        args,
        token,
        hass,
        data,
        [create_entity],
        action="create",
        service_data={
            "helper_type": helper_type,
            "config": config,
            "flow_steps": args.get("flow_steps"),
        },
        session_id="create_helper_execute",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    if not _valid_helper_type(helper_type):
        if not await _is_config_flow_helper(hass, helper_type):
            return _tool_error("This is not a creatable helper config flow."), "invalid_request", "create_helper"
        steps = args.get("flow_steps")
        if error := _helper_flow_steps_error(steps):
            return _tool_error(error), "invalid_request", "create_helper"
        flow_steps = cast(list[dict[str, Any]], steps)
        status, detail = await _prepare_config_flow_helper(
            hass, str(helper_type), flow_steps, token
        )
        if status == "denied":
            return (
                _tool_error(
                    "These entities are outside this token's write scope: "
                    f"{', '.join(detail)}."
                ),
                "denied", "create_helper",
            )
        if status != "ready":
            message = (
                "The helper flow no longer reaches its final form with these steps. "
                "Call create_helper again to inspect the current form."
                if status == "form" else str(detail)
            )
            return _tool_error(message), "invalid_request", "create_helper"
        return await _execute_config_flow_helper(
            str(helper_type), flow_steps, token, hass, data,
            create_mesa_context=mesa_context,
        )
    if not isinstance(config, dict) or not config:
        return _tool_error("config must be a non-empty object (at least 'name')."), "invalid_request", "create_helper"
    pre = _helper_write_precheck(args, "create_helper", require_id=False)
    if pre is not None:
        return pre
    if helper_type in _SCOPED_STORAGE_HELPER_TYPES:
        if scope_error := _scoped_helper_create_scope_error(str(helper_type), token):
            return _tool_error(scope_error), "denied", "create_helper"
        if absence_error := await _tag_create_absence_error(
            args, hass, "create_helper"
        ):
            return absence_error
        if helper_type == "person" and (
            relationship_error := _person_relationship_scope_error(
                config, token, hass
            )
        ):
            return _tool_error(relationship_error), "denied", "create_helper"
    create_payload = dict(config)
    restore_id = args.get(_HELPER_RESTORE_ID)
    if helper_type == "person" and isinstance(restore_id, str):
        # person/create offers no explicit id. Seed the name with the original
        # storage id, then apply the restored friendly name after HA allocates
        # that exact id.
        create_payload["name"] = restore_id
    try:
        item = await async_ws_command(hass, f"{helper_type}/create", create_payload)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to create helper: {exc}"), "invalid_request", "create_helper"
    new_id = item.get("id") if isinstance(item, dict) else None
    if (
        helper_type == "person"
        and isinstance(restore_id, str)
        and new_id == restore_id
        and config.get("name") != restore_id
    ):
        try:
            item = await async_ws_command(
                hass,
                "person/update",
                {"person_id": restore_id, **config},
            )
        except WsDispatchError as exc:
            try:
                await async_ws_command(
                    hass, "person/delete", {"person_id": restore_id}
                )
            except WsDispatchError:
                return (
                    _tool_error(
                        "Failed to finish restoring the person and automatic cleanup "
                        "failed. Remove it in Home Assistant."
                    ),
                    "invalid_request",
                    "create_helper",
                )
            return (
                _tool_error(f"Failed to finish restoring the person: {exc}"),
                "invalid_request",
                "create_helper",
            )
    entity_id = (
        _resolve_helper_entity_id(hass, str(helper_type), str(new_id))
        if new_id is not None else None
    )
    if helper_type in _SCOPED_STORAGE_HELPER_TYPES:
        if entity_id is None or resolve(entity_id, token, hass) != Permission.WRITE:
            if new_id is not None:
                try:
                    await async_ws_command(
                        hass,
                        f"{helper_type}/delete",
                        {f"{helper_type}_id": str(new_id)},
                    )
                except WsDispatchError:
                    return (
                        _tool_error(
                            f"The new {helper_type} did not enter this token's write "
                            "scope, and "
                            "automatic cleanup failed. Remove it in Home Assistant."
                        ),
                        "invalid_request",
                        "create_helper",
                    )
            return (
                _tool_error(
                    f"The new {helper_type} did not enter this token's write scope, "
                    "so it was removed and nothing changed."
                ),
                "denied",
                "create_helper",
            )
        if helper_type == "person" and (
            not isinstance(item, dict)
            or not _person_relationships_accessible(item, token, hass, write=True)
        ):
            if new_id is not None:
                try:
                    await async_ws_command(
                        hass, "person/delete", {"person_id": str(new_id)}
                    )
                except WsDispatchError:
                    return (
                        _tool_error(
                            "The new person has an inaccessible relationship and "
                            "automatic cleanup failed. Remove it in Home Assistant."
                        ),
                        "invalid_request",
                        "create_helper",
                    )
            return (
                _tool_error(
                    "The new person has an inaccessible relationship, so it was "
                    "removed and nothing changed."
                ),
                "denied",
                "create_helper",
            )
    if (
        helper_type == "person"
        and isinstance(restore_id, str)
        and new_id != restore_id
    ):
        if new_id is not None:
            try:
                await async_ws_command(
                    hass, "person/delete", {"person_id": str(new_id)}
                )
            except WsDispatchError:
                return (
                    _tool_error(
                        "Home Assistant assigned a different person ID and automatic "
                        "cleanup failed. Remove it in Home Assistant."
                    ),
                    "invalid_request",
                    "create_helper",
                )
        return (
            _tool_error(
                "The original person ID is unavailable, so the restore was rolled back."
            ),
            "invalid_request",
            "create_helper",
        )
    post_mesa = (
        _helper_mesa_context(
            data,
            token,
            [entity_id] if entity_id is not None else [],
            action="create",
            service_data={"helper_type": helper_type, "config": config},
            session_id="create_helper_materialized",
        )
    )
    post_error: tuple[dict, str, str] | None = None
    if isinstance(post_mesa, tuple):
        post_error = post_mesa
    elif post_mesa.decision == "deny":
        _fire_helper_mesa_blocks(hass, token, post_mesa)
        post_error = _helper_mesa_error(post_mesa)
    elif post_mesa.decision == "confirm" and not (
        _restore_ctx.get() is not None
        or (
            _approved_exec_ctx.get()
            and post_mesa.profile_fingerprints.issubset(
                mesa_context.profile_fingerprints
            )
        )
    ):
        post_error = _helper_mesa_error(post_mesa, changed=True)
    if post_error is not None:
        if new_id is not None:
            try:
                await async_ws_command(
                    hass,
                    f"{helper_type}/delete",
                    {f"{helper_type}_id": str(new_id)},
                )
            except WsDispatchError:
                return (
                    _tool_error(
                        "MESA blocked the new helper, but automatic cleanup failed. "
                        "Remove it in Home Assistant."
                    ),
                    "invalid_request",
                    "create_helper",
                )
        return post_error
    assert isinstance(post_mesa, _HelperMesaContext)
    if post_mesa.warnings:
        _mesa_advisory_ctx.set(True)
    public_item = (
        _public_helper_config(str(helper_type), item)
        if helper_type == "person" and isinstance(item, dict)
        else item if isinstance(item, dict) else dict(config)
    )
    version_after = public_item if helper_type == "person" else config
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{new_id}",
        action="create", before=None, after=version_after, alias=config.get("name"),
    )
    return _tool_success(json.dumps({"helper_type": helper_type, "helper": public_item}, default=str)), "allowed", f"helper:{helper_type}"


async def _execute_config_flow_helper(
    helper_type: str,
    steps: list[dict[str, Any]],
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    create_mesa_context: _HelperMesaContext,
) -> tuple[dict, str, str]:
    """Run the approved helper flow through its final form exactly once."""
    flow_id = None
    entry = None
    try:
        result = await hass.config_entries.flow.async_init(
            helper_type, context={"source": SOURCE_USER}
        )
        flow_id = result.get("flow_id")
        for index, supplied in enumerate(steps):
            body = _flow_step_body(helper_type, result)
            if body is None or body["step"]["step_id"] != supplied["step_id"]:
                return (
                    _tool_error("The helper flow changed after approval; nothing was created."),
                    "invalid_request", "create_helper",
                )
            denied = _unwritable_option_entities(supplied["data"], token, hass)
            if denied:
                return (
                    _tool_error(
                        "These entities are outside this token's write scope: "
                        f"{', '.join(denied)}."
                    ),
                    "denied", "create_helper",
                )
            result = await hass.config_entries.flow.async_configure(
                flow_id, dict(supplied["data"])
            )
            if result.get("type") == FlowResultType.CREATE_ENTRY:
                if index != len(steps) - 1:
                    entry = result.get("result")
                    if entry is not None:
                        await hass.config_entries.async_remove(entry.entry_id)
                        entry = None
                    return (
                        _tool_error("The helper flow finished before all approved steps were used."),
                        "invalid_request", "create_helper",
                    )
                entry = result.get("result")
                break
            if result.get("type") == FlowResultType.ABORT:
                return (
                    _tool_error(
                        f"Home Assistant refused this helper: {result.get('reason')}. "
                        "Nothing was created."
                    ),
                    "invalid_request", "create_helper",
                )
            if result.get("errors"):
                detail = ", ".join(
                    f"{field}: {message}"
                    for field, message in (result.get("errors") or {}).items()
                )
                return (
                    _tool_error(
                        f"The helper form was rejected ({detail}). Nothing was created."
                    ),
                    "invalid_request", "create_helper",
                )
        if entry is None:
            return (
                _tool_error("The approved steps did not create a helper."),
                "invalid_request", "create_helper",
            )
    except vol.Invalid as err:
        return (
            _tool_error(f"The helper form was rejected: {err}. Nothing was created."),
            "invalid_request", "create_helper",
        )
    except Exception:  # noqa: BLE001 - integration failures become clean tool errors
        _LOGGER.exception("Helper creation flow failed for %s", helper_type)
        return (
            _tool_error("Could not create this helper."),
            "invalid_request", "create_helper",
        )
    finally:
        if entry is None:
            await _abort_create_flow(hass, flow_id)

    registry = er.async_get(hass)
    entity_ids = sorted(
        registry_entry.entity_id
        for registry_entry in registry.entities.values()
        if registry_entry.config_entry_id == entry.entry_id
    )
    post_mesa = _helper_mesa_context(
        data,
        token,
        entity_ids,
        action="create",
        service_data={"helper_type": helper_type},
        session_id="create_helper_flow_materialized",
    )
    post_error: tuple[dict, str, str] | None = None
    if isinstance(post_mesa, tuple):
        post_error = post_mesa
    elif post_mesa.decision == "deny":
        _fire_helper_mesa_blocks(hass, token, post_mesa)
        post_error = _helper_mesa_error(post_mesa)
    elif post_mesa.decision == "confirm" and not (
        _restore_ctx.get() is not None
        or (
            _approved_exec_ctx.get()
            and post_mesa.profile_fingerprints.issubset(
                create_mesa_context.profile_fingerprints
            )
        )
    ):
        post_error = _helper_mesa_error(post_mesa, changed=True)
    if post_error is not None:
        try:
            await hass.config_entries.async_remove(entry.entry_id)
        except Exception:  # noqa: BLE001 - report an unsafe partial create loudly
            _LOGGER.exception(
                "MESA blocked new helper entry %s but cleanup failed", entry.entry_id
            )
            return (
                _tool_error(
                    "MESA blocked the new helper, but automatic cleanup failed. "
                    "Remove its configuration entry in Home Assistant."
                ),
                "invalid_request",
                "create_helper",
            )
        return post_error
    assert isinstance(post_mesa, _HelperMesaContext)
    if post_mesa.warnings:
        _mesa_advisory_ctx.set(True)
    await _record_version(
        data,
        token,
        resource_type="config_entry",
        resource_id=entry.entry_id,
        action="create",
        before=None,
        after={
            "restorable": False,
            "reason": "Config-flow helper creation cannot be replayed safely.",
        },
        alias=entry.title,
    )
    return (
        _tool_success(json.dumps({
            "helper_type": helper_type,
            "config_entry": {
                "entry_id": entry.entry_id,
                "domain": entry.domain,
                "title": entry.title,
                "state": str(entry.state),
            },
            "entity_ids": entity_ids,
            "note": "The helper is materialized; no restart is needed.",
        }, default=str)),
        "allowed",
        f"config_entry:{entry.entry_id}",
    )


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
    approval_args, context_error = await _bind_scoped_helper_context(
        args, token, hass, "edit_helper"
    )
    if context_error is not None:
        return context_error
    if args.get("helper_type") == "person" and "device_trackers" in dict_arg(
        args.get("config")
    ) and (
        relationship_error := _person_relationship_scope_error(
            dict_arg(args.get("config")), token, hass
        )
    ):
        return _tool_error(relationship_error), "denied", "edit_helper"
    helper_type = str(args.get("helper_type"))
    helper_id = str(args.get("helper_id") or "").strip()
    entity_id = _resolve_helper_entity_id(hass, helper_type, helper_id)
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    mesa_context = _helper_mesa_context(
        data,
        token,
        [entity_id],
        action="edit",
        service_data={"helper_type": helper_type, "config": args.get("config")},
        session_id=request_id or "edit_helper",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    approval_args, blocked = await _helper_mesa_approval_gate(
        approval_args,
        token,
        hass,
        data,
        context=mesa_context,
        tool_name="edit_helper",
        request_id=request_id,
        client_ip=client_ip,
        diff_builder=lambda: _build_diff_edit_helper(approval_args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_helper(approval_args, token, hass, data)


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
    pre = _helper_write_precheck(args, "edit_helper", require_id=True)
    if pre is not None:
        return pre
    scoped_context, context_error = await _revalidate_scoped_helper_context(
        args, token, hass, "edit_helper"
    )
    if context_error is not None:
        return context_error
    # Ordinary helper authoring is cap-gated rather than entity-scoped. Zone,
    # tag, and person are the narrow exceptions because their records carry
    # sensitive location, scan, user, or tracker relationships.
    # Every path still requires a real storage-backed helper entity.
    entity_id = _resolve_helper_entity_id(hass, str(helper_type), str(helper_id))
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    mesa_context = _enforce_helper_mesa_context(
        args,
        token,
        hass,
        data,
        [entity_id],
        action="edit",
        service_data={"helper_type": helper_type, "config": config},
        session_id="edit_helper_execute",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    before_cfg = (
        scoped_context["config"]
        if scoped_context is not None
        else await _read_helper_config(hass, str(helper_type), str(helper_id))
    )
    if helper_type == "person":
        if "device_trackers" in config and (
            relationship_error := _person_relationship_scope_error(
                config, token, hass
            )
        ):
            return _tool_error(relationship_error), "denied", "edit_helper"
        # HA's person/update schema defaults an omitted tracker list to [], so a
        # name-only edit must explicitly merge the stored relationship. user_id
        # and picture are omitted intentionally: HA preserves both on update.
        current = scoped_context["config"] if scoped_context is not None else {}
        config = {
            **config,
            "device_trackers": list(
                config.get("device_trackers", current.get("device_trackers", []))
            ),
        }
    payload = {f"{helper_type}_id": helper_id, **config}
    try:
        item = await async_ws_command(hass, f"{helper_type}/update", payload)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to edit helper: {exc}"), "invalid_request", "edit_helper"
    public_before = (
        _public_helper_config(str(helper_type), before_cfg)
        if helper_type == "person" and isinstance(before_cfg, dict)
        else before_cfg
    )
    public_item = (
        _public_helper_config(str(helper_type), item)
        if helper_type == "person" and isinstance(item, dict)
        else item if isinstance(item, dict) else dict(config)
    )
    version_after = public_item if helper_type == "person" else config
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{helper_id}",
        action="edit", before=public_before, after=version_after, alias=config.get("name"),
    )
    return _tool_success(json.dumps({"helper_type": helper_type, "helper": public_item}, default=str)), "allowed", f"helper:{helper_type}:{helper_id}"


async def _tool_delete_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete a helper (Confirm-gated)."""
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "delete_helper"
    pre = _helper_write_precheck(
        {**args, "config": {}}, "delete_helper", require_id=True
    )
    if pre is not None:
        return pre
    approval_args, context_error = await _bind_scoped_helper_context(
        args, token, hass, "delete_helper"
    )
    if context_error is not None:
        return context_error
    helper_type = str(args.get("helper_type"))
    helper_id = str(args.get("helper_id") or "").strip()
    entity_id = _resolve_helper_entity_id(hass, helper_type, helper_id)
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    mesa_context = _helper_mesa_context(
        data,
        token,
        [entity_id],
        action="delete",
        service_data={"helper_type": helper_type},
        session_id=request_id or "delete_helper",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    approval_args, blocked = await _helper_mesa_approval_gate(
        approval_args,
        token,
        hass,
        data,
        context=mesa_context,
        tool_name="delete_helper",
        request_id=request_id,
        client_ip=client_ip,
        diff_builder=lambda: _build_diff_delete_helper(approval_args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_helper(approval_args, token, hass, data)


async def _execute_delete_helper(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    helper_type = args.get("helper_type")
    helper_id = str(args.get("helper_id") or "").strip()
    if not _valid_helper_type(helper_type):
        return _tool_error(f"helper_type must be one of: {', '.join(sorted(HELPER_TYPES))}."), "invalid_request", "delete_helper"
    if not helper_id:
        return _tool_error("helper_id is required."), "invalid_request", "delete_helper"
    scoped_context, context_error = await _revalidate_scoped_helper_context(
        args, token, hass, "delete_helper"
    )
    if context_error is not None:
        return context_error
    # Ordinary helpers are cap-gated rather than entity-scoped. Zone, tag, and
    # person have passed stricter storage ownership and WRITE-scope checks above.
    entity_id = _resolve_helper_entity_id(hass, str(helper_type), str(helper_id))
    if entity_id is None:
        return _tool_error("Helper not found."), "not_found", f"helper:{helper_type}:{helper_id}"
    mesa_context = _enforce_helper_mesa_context(
        args,
        token,
        hass,
        data,
        [entity_id],
        action="delete",
        service_data={"helper_type": helper_type},
        session_id="delete_helper_execute",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    before_cfg = (
        scoped_context["config"]
        if scoped_context is not None
        else await _read_helper_config(hass, str(helper_type), str(helper_id))
    )
    try:
        await async_ws_command(hass, f"{helper_type}/delete", {f"{helper_type}_id": helper_id})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to delete helper: {exc}"), "invalid_request", "delete_helper"
    public_before = (
        _public_helper_config(str(helper_type), before_cfg)
        if helper_type == "person" and isinstance(before_cfg, dict)
        else before_cfg
    )
    await _record_version(
        data, token, resource_type="helper", resource_id=f"{helper_type}:{helper_id}",
        action="delete", before=public_before, after=None,
        alias=public_before.get("name") if isinstance(public_before, dict) else None,
    )
    return _tool_success(f"Helper '{helper_id}' deleted successfully."), "allowed", f"helper:{helper_type}:{helper_id}"


def _build_diff_create_helper(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    helper_type = args.get("helper_type")
    config = dict_arg(args.get("config"))
    flow_steps = args.get("flow_steps") if isinstance(args.get("flow_steps"), list) else []
    if flow_steps:
        config = {"flow_steps": flow_steps}
        for step in flow_steps:
            step_data = step.get("data") if isinstance(step, dict) else None
            if isinstance(step_data, dict) and isinstance(step_data.get("name"), str):
                config["name"] = step_data["name"]
                break
    return {
        "kind": "config_diff",
        **_summary(
            "create_helper", helper_type=helper_type,
            name=config.get("name") or config.get("tag_id") or "<no name>",
        ),
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
    entity_id = _resolve_helper_entity_id(hass, str(helper_type), helper_id)
    state = hass.states.get(entity_id) if entity_id is not None else None
    label = (
        state.attributes.get("friendly_name")
        if state is not None else None
    ) or helper_id
    return {
        "kind": "system_action",
        **_summary("delete_helper", helper_type=helper_type, helper_id=helper_id),
        "target": {"type": "helper", "id": helper_id, "label": label},
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


def _config_entry_helper_entity_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Every exact entity owned by one helper config entry."""
    return sorted(
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.config_entry_id == entry_id
    )


def _config_entry_helper_mesa_entities(
    hass: HomeAssistant, entry: Any, data: PhoenixData
) -> list[str] | tuple[dict, str, str]:
    """Exact owners, or a harmless synthetic anchor only while MESA is off."""
    entity_ids = _config_entry_helper_entity_ids(hass, entry.entry_id)
    if entity_ids:
        return entity_ids
    if data.mesa_setup_failed is True:
        return (
            _tool_error(
                "MESA is configured but unavailable, so helper ownership cannot be checked."
            ),
            "denied",
            "set_helper_settings",
        )
    if isinstance(data.mesa, MesaRuntime):
        try:
            if data.store.get_settings().mesa_mode != "off":
                return (
                    _tool_error(
                        "MESA cannot protect this helper because it has no registry entities; nothing changed."
                    ),
                    "denied",
                    "set_helper_settings",
                )
        except Exception:  # noqa: BLE001 - unreadable safety mode fails closed
            return (
                _tool_error("MESA safety state could not be read; nothing changed."),
                "denied",
                "set_helper_settings",
            )
    return [f"{entry.domain}.__phoenix_helper_settings__"]


# The two ways HA lets an entry be reconfigured, and they are NOT interchangeable.
# An options flow writes entry.options and finishes with CREATE_ENTRY; a
# reconfigure flow writes entry.data and finishes with ABORT carrying
# reason "reconfigure_successful", because it updates an entry rather than making
# one. Reading a success as a failure (or the reverse) is the whole hazard here,
# so every mechanism-specific difference is funnelled through the four helpers
# below rather than spread across the tools.
_OPTIONS = "options"
_RECONFIGURE = "reconfigure"
_RECONFIGURE_OK = "reconfigure_successful"


def _settings_mechanism(entry: Any) -> str | None:
    """Which flow, if any, can change this entry's settings.

    Options first: a helper that offers both is offering the options flow for
    ordinary settings, which is what this tool is for.
    """
    if entry.supports_options:
        return _OPTIONS
    if entry.supports_reconfigure:
        return _RECONFIGURE
    return None


def _settings_store(entry: Any, mechanism: str | None) -> dict:
    """The entry's current settings, from whichever store its flow writes."""
    return dict(entry.data if mechanism == _RECONFIGURE else entry.options)


def _settings_marker_name(marker: Any) -> str | None:
    """Return a string field name from a voluptuous marker."""
    raw = marker.schema if isinstance(marker, vol.Marker) else marker
    return raw if isinstance(raw, str) and raw else None


def _settings_marker_description(marker: Any) -> dict[str, Any]:
    """Return a marker description without trusting third-party shapes."""
    description = getattr(marker, "description", None)
    return description if isinstance(description, dict) else {}


def _settings_is_section(value: Any) -> bool:
    """Recognize Home Assistant form sections without importing private APIs."""
    value_type = type(value)
    return (
        value_type.__module__ == "homeassistant.data_entry_flow"
        and value_type.__name__ == "section"
        and isinstance(getattr(value, "schema", None), vol.Schema)
    )


def _settings_suggested_value(
    marker: Any, inherited: dict[str, Any] | None
) -> tuple[bool, Any]:
    """Return a current value supplied by the edit step, if any."""
    description = _settings_marker_description(marker)
    if "suggested_value" in description and description["suggested_value"] is not None:
        return True, copy.deepcopy(description["suggested_value"])
    name = _settings_marker_name(marker)
    if inherited is not None and name in inherited:
        return True, copy.deepcopy(inherited[name])
    return False, None


def _settings_clear_by_omission(marker: Any) -> bool:
    """Return whether an optional field has no default to replace omission."""
    return not isinstance(marker, vol.Required) and getattr(
        marker, "default", vol.UNDEFINED
    ) is vol.UNDEFINED


def _settings_form_payload(result: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Build an edit-flow payload without resetting omitted stored values.

    Home Assistant's edit forms arrive with current values as suggested values.
    The frontend submits those values back. Phoenix must do the same for fields
    the caller did not name, but must not submit a field's static schema default
    as though it were the current value.
    """
    schema = result.get("data_schema")
    if not isinstance(schema, vol.Schema) or not isinstance(schema.schema, dict):
        return dict(settings)

    def _walk(
        nested_schema: vol.Schema,
        inherited: dict[str, Any] | None = None,
        explicit: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], set[str]]:
        payload: dict[str, Any] = {}
        consumed: set[str] = set()
        for marker, validator in nested_schema.schema.items():
            name = _settings_marker_name(marker)
            if name is None:
                continue
            if _settings_is_section(validator):
                section_value = settings.get(name)
                section_named = isinstance(section_value, dict)
                found, suggested = _settings_suggested_value(marker, inherited)
                nested_inherited = suggested if found and isinstance(suggested, dict) else None
                child, child_consumed = _walk(
                    validator.schema,
                    nested_inherited,
                    section_value if section_named else None,
                )
                section_has_flat_input = any(
                    child_name in settings for child_name in child_consumed
                )
                if (
                    section_named
                    or section_has_flat_input
                    or isinstance(marker, vol.Required)
                    or found
                ):
                    payload[name] = child
                consumed.add(name)
                consumed.update(child_consumed)
                continue

            consumed.add(name)
            if name in settings:
                value = settings[name]
            elif explicit is not None and name in explicit:
                value = explicit[name]
            else:
                found, value = _settings_suggested_value(marker, inherited)
                if not found or redaction_sentinel_path(value) is not None:
                    if isinstance(marker, vol.Required) and getattr(
                        marker, "default", vol.UNDEFINED
                    ) is vol.UNDEFINED:
                        # Let the flow report a genuinely missing required value.
                        continue
                    continue
            if value is None and _settings_clear_by_omission(marker):
                continue
            payload[name] = copy.deepcopy(value)

        if explicit is not None:
            for name, value in explicit.items():
                if name not in consumed:
                    payload[name] = copy.deepcopy(value)
        return payload, consumed

    payload, consumed = _walk(schema)
    for name, value in settings.items():
        if name not in consumed:
            # Preserve unknown keys so Home Assistant can reject them instead
            # of Phoenix silently turning an invalid request into a no-op.
            payload[name] = copy.deepcopy(value)
    return payload


async def _init_settings_flow(hass: HomeAssistant, entry: Any, mechanism: str) -> Any:
    """Open the entry's settings flow and return its first step."""
    if mechanism == _RECONFIGURE:
        return await hass.config_entries.flow.async_init(
            entry.domain,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
    return await hass.config_entries.options.async_init(entry.entry_id)


async def _configure_settings_flow(
    hass: HomeAssistant, mechanism: str, flow_id: str, user_input: dict
) -> Any:
    manager = hass.config_entries.flow if mechanism == _RECONFIGURE else hass.config_entries.options
    return await manager.async_configure(flow_id, user_input)


def _settings_applied(mechanism: str, result: Any) -> bool:
    """Whether the flow actually wrote the settings.

    THE ASYMMETRY IS THE POINT. An options flow signals success with
    CREATE_ENTRY. A reconfigure flow signals it with ABORT + reason
    "reconfigure_successful", since HA's async_update_reload_and_abort updates
    the entry and then aborts the flow, so a single "type != CREATE_ENTRY means
    nothing happened" test reports every successful reconfigure as a failure.
    An abort with any OTHER reason (already_configured, and whatever an
    integration raises) is a real refusal and must keep reading as one.
    """
    if mechanism == _RECONFIGURE:
        return (result.get("type") == FlowResultType.ABORT
                and result.get("reason") == _RECONFIGURE_OK)
    return result.get("type") == FlowResultType.CREATE_ENTRY


def _options_schema_json(result: Any) -> list | None:
    """Serialize a flow step's data_schema the way HA's own frontend receives it.

    voluptuous_serialize with HA's custom_serializer is exactly what
    helpers/data_entry_flow._prepare_result_json uses, so an agent reads the same
    field names, defaults and selectors the UI does; an entity field even carries
    the domains it accepts. Reimplementing this would drift from what the flow
    actually validates against.
    """
    schema = result.get("data_schema")
    if schema is None:
        # A step with no schema at all is a confirm-only step: it genuinely has
        # no fields, which is NOT the same as a schema this cannot describe, so
        # it returns the empty list rather than the "unknown" None below.
        return []
    try:
        converted = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    except Exception:  # noqa: BLE001 - a schema shape this cannot describe
        # Returning [] here would be a lie the caller acts on: an empty field
        # list reads as "this helper accepts nothing", and the caller would then
        # send nothing and clear every optional setting. None means "could not be
        # described", which the caller is told rather than left to infer.
        _LOGGER.debug("Could not serialize a settings schema", exc_info=True)
        return None
    # convert() types as dict-or-list; a vol.Schema over a mapping always yields
    # the per-field list, and anything else is not a form an agent can fill.
    return converted if isinstance(converted, list) else None


async def _abort_flow(hass: HomeAssistant, mechanism: str | None, flow_id: str | None) -> None:
    """Drop a settings flow we opened but will not finish.

    Every early return between async_init and a successful configure has to come
    through here: the flow lives in HA's own manager, not in the request, so
    abandoning one leaks it into config_entries/flow/progress where it shows up
    in the operator's UI as a half-finished dialog. The two mechanisms have
    separate managers, so aborting on the wrong one silently leaves the flow open.
    """
    if not flow_id:
        return
    manager = hass.config_entries.flow if mechanism == _RECONFIGURE else hass.config_entries.options
    try:
        manager.async_abort(flow_id)
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
    tool = "get_helper_settings"
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", tool
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    mechanism = _settings_mechanism(entry)
    settings = _settings_store(entry, mechanism)
    body: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "mechanism": mechanism,
        "settings": settings,
        "content_hash": content_hash(settings),
    }
    if mechanism is None:
        body["schema"] = []
        body["note"] = "This helper exposes no way to change its settings, so they cannot be changed here."
        return _tool_success(json.dumps(body, default=str)), "allowed", tool
    flow_id = None
    try:
        result = await _init_settings_flow(hass, entry, mechanism)
        flow_id = result.get("flow_id")
        schema = _options_schema_json(result)
        body["step_id"] = result.get("step_id")
        if schema is None:
            # Settings still readable and writable; only the field description is
            # missing, so say that instead of implying there are no fields.
            body["note"] = (
                "This helper's settings form could not be described, so no field list is "
                "available. You can still change settings by sending the values you want; "
                "send the whole of settings, since the fields it accepts are unknown here."
            )
            return _tool_success(json.dumps(body, default=str)), "allowed", tool
        body["schema"] = schema
        # THE FIELD A CALLER ACTUALLY SENDS BACK, taken from the SCHEMA's own
        # suggested values rather than from the stored settings.
        #
        # Intersecting stored keys with field names was the first attempt and it
        # is wrong whenever a flow TRANSFORMS its input, which is common: a real
        # time_off helper stores {"entities": [id]} while its form field is
        # "entity" (singular, one id), so the intersection was EMPTY and the tool
        # reported that nothing could be changed on a helper that reconfigures
        # fine. The stored shape is the integration's business; the form's shape
        # is the caller's.
        #
        # suggested_value is authoritative because the FLOW puts it there: HA's
        # schema flows seed it from the stored options, and a hand-written flow
        # sets it deliberately. A field carrying none has no current value to
        # report and appears in `schema` alone, so the caller decides.
        editable = {}
        for field in schema:
            if not isinstance(field, dict):
                continue
            described = field.get("description")
            if isinstance(described, dict) and "suggested_value" in described:
                editable[field.get("name")] = described["suggested_value"]
        body["editable_settings"] = editable
        body["note"] = (
            "Send editable_settings back with your change applied; those are the form's "
            "own fields and current values, which may be named or shaped differently from "
            "settings above (that is what this helper stores, and it is left alone). A "
            "field in schema but not here has no current value. Omitting an OPTIONAL field "
            "the form offers clears it."
        )
    except Exception:  # noqa: BLE001 - a flow that will not start is not a Phoenix bug
        _LOGGER.exception("Could not open the settings flow for %s", entry.entry_id)
        return _tool_error("Could not read this helper's settings."), "invalid_request", tool
    finally:
        await _abort_flow(hass, mechanism, flow_id)
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


def _config_entry_settings_precheck(
    settings: Any, entry: Any, mechanism: str | None, token: TokenRecord,
    hass: HomeAssistant, tool: str,
) -> tuple[dict, str, str] | None:
    """Rule 29: refuse a doomed reconfigure before an approval exists."""
    if not isinstance(settings, dict) or not settings:
        return _tool_error("settings must be a non-empty object of the values to apply."), "invalid_request", tool
    if mechanism is None:
        return (
            _tool_error("This helper exposes no way to change its settings, so they cannot be changed here."),
            "invalid_request", tool,
        )
    denied = _unwritable_option_entities(settings, token, hass)
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
    mechanism = _settings_mechanism(entry) if entry is not None else None
    before = _settings_store(entry, mechanism) if entry is not None else {}
    after = dict_arg(args.get("settings"))
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
            "mechanism": mechanism,
            "changed_keys": changed,
            "warning": "Replaces this helper's settings; its flow validates them and reloads it.",
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

    `options` is MERGED over the entry's stored options by HA's own schema flow,
    not swapped for them, and the difference matters in both directions: a key
    the flow does not declare is left untouched (and is REJECTED if sent, since
    the step validates against its schema), while an OPTIONAL key the flow does
    declare and the caller omits is CLEARED, which is how a field gets emptied.
    So the set to send is `editable_options` from get_config_entry_options, which
    is exactly the stored options intersected with what the flow offers.
    """
    tool = "set_helper_settings"
    if effective_cap(token, "cap_helper_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    mechanism = _settings_mechanism(entry)
    pre = _config_entry_settings_precheck(
        args.get("settings"), entry, mechanism, token, hass, tool)
    if pre is not None:
        return pre
    conflict = _cas_conflict(
        args.get("expected_hash"), _settings_store(entry, mechanism), tool)
    if conflict is not None:
        return conflict
    entity_ids = _config_entry_helper_mesa_entities(hass, entry, data)
    if isinstance(entity_ids, tuple):
        return entity_ids
    mesa_context = _helper_mesa_context(
        data,
        token,
        entity_ids,
        action="settings",
        service_data={"helper_type": entry.domain, "settings": args.get("settings")},
        session_id=request_id or tool,
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    approval_args, blocked = await _helper_mesa_approval_gate(
        args,
        token,
        hass,
        data,
        context=mesa_context,
        tool_name=tool,
        request_id=request_id,
        client_ip=client_ip,
        diff_builder=lambda: _build_diff_set_config_entry_options(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_config_entry_options(approval_args, token, hass, data)


async def _execute_set_config_entry_options(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    tool = "set_helper_settings"
    entry = await _helper_config_entry(hass, args.get("entry_id"))
    if entry is None:
        return _tool_error(_NOT_A_HELPER_ENTRY), "not_found", tool
    mechanism = _settings_mechanism(entry)
    settings = args.get("settings")
    # Re-validated at apply time, not only pre-gate: the entry, the token's tree
    # and the entities named can all move while an approval waits.
    pre = _config_entry_settings_precheck(settings, entry, mechanism, token, hass, tool)
    if pre is not None:
        return pre
    before = _settings_store(entry, mechanism)
    conflict = _cas_conflict(args.get("expected_hash"), before, tool)
    if conflict is not None:
        return conflict
    entity_ids = _config_entry_helper_mesa_entities(hass, entry, data)
    if isinstance(entity_ids, tuple):
        return entity_ids
    mesa_context = _enforce_helper_mesa_context(
        args,
        token,
        hass,
        data,
        entity_ids,
        action="settings",
        service_data={"helper_type": entry.domain, "settings": settings},
        session_id="set_helper_settings_execute",
    )
    if isinstance(mesa_context, tuple):
        return mesa_context
    if mechanism is None:  # refused by the precheck above; narrows for the type checker
        return _tool_error("This helper exposes no way to change its settings."), "invalid_request", tool

    flow_id = None
    try:
        result = await _init_settings_flow(hass, entry, mechanism)
        flow_id = result.get("flow_id")
        result = await _configure_settings_flow(
            hass,
            mechanism,
            flow_id,
            _settings_form_payload(result, settings)
            if isinstance(settings, dict)
            else {},
        )
    except vol.Invalid as err:
        await _abort_flow(hass, mechanism, flow_id)
        return _tool_error(f"The settings were rejected: {err}"), "invalid_request", tool
    except Exception:  # noqa: BLE001 - a flow failure is not a Phoenix bug, but must not leak
        _LOGGER.exception("Settings flow failed for %s", entry.entry_id)
        await _abort_flow(hass, mechanism, flow_id)
        return _tool_error("Could not apply these settings."), "invalid_request", tool

    if not _settings_applied(mechanism, result):
        # Either the flow wants another step, or it refused. Neither applied
        # anything, and a multi-step flow is not something one call can drive.
        await _abort_flow(hass, mechanism, flow_id)
        errors = result.get("errors") or {}
        if errors:
            detail = ", ".join(f"{field}: {msg}" for field, msg in errors.items())
            message = f"The settings were rejected ({detail}). Nothing was changed."
        elif result.get("type") == FlowResultType.ABORT:
            message = f"Nothing was changed; the helper refused: {result.get('reason')}."
        else:
            message = (
                "Nothing was changed. This helper's settings flow asks for more than one "
                "step, which this tool cannot drive; change it in the Home Assistant UI."
            )
        return _tool_error(message), "invalid_request", tool

    # Re-read rather than trusting the payload: the flow's own validators can
    # normalise what they were given (threshold sets absent bounds to None).
    applied_entry = hass.config_entries.async_get_entry(entry.entry_id)
    after = (_settings_store(applied_entry, mechanism)
             if applied_entry is not None else dict_arg(settings))
    await _record_version(
        data, token, resource_type="config_entry", resource_id=entry.entry_id,
        action="edit", before=before, after=after, alias=entry.title,
        summary=_version_summary("config_entry.options", subject=entry.title),
    )
    return (
        _tool_success(json.dumps({
            "entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title,
            "mechanism": mechanism, "settings": after, "content_hash": content_hash(after),
            "note": "The helper reloaded with these settings; no restart is needed.",
        }, default=str)),
        "allowed", f"config_entry:{entry.entry_id}",
    )
