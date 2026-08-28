"""Bounded driver for Home Assistant's official integration reconfigure flow.

This module deliberately does not expose or mutate ``ConfigEntry.data``.  The
caller supplies proposed values, Home Assistant's integration-owned flow
validates and applies them, and Phoenix only observes the resulting entry and
reload lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import voluptuous as vol
import voluptuous_serialize

from homeassistant.config_entries import ConfigEntryState, SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv

from ..policy_engine import is_sensitive_key

_LOGGER = logging.getLogger(__name__)

FLOW_STEP_BUDGET = 12
MENU_CHOICE_BUDGET = 10
RELOAD_VERIFY_TIMEOUT = 30.0
RECONFIGURE_SUCCESS_REASON = "reconfigure_successful"

STATUS_ABORTED = "flow_aborted_before_apply"
STATUS_APPLY_FAILED = "apply_failed"
STATUS_VERIFIED = "applied_and_verified"
STATUS_UNVERIFIED = "applied_but_unverified"
STATUS_INCOMPLETE = "applied_but_incomplete"

_UNSUPPORTED_TYPES = {
    FlowResultType.EXTERNAL_STEP,
    FlowResultType.EXTERNAL_STEP_DONE,
    FlowResultType.SHOW_PROGRESS,
    FlowResultType.SHOW_PROGRESS_DONE,
}
_FAILURE_STATES = {
    ConfigEntryState.SETUP_ERROR,
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.FAILED_UNLOAD,
}


@dataclass(frozen=True)
class ReconfigureFlowResult:
    """A truthful result from one bounded reconfigure attempt."""

    status: str
    applied: bool
    reason: str
    details: dict[str, Any]
    baseline_modified_at: datetime
    commit_modified_at: datetime | None


def _result_type(result: Mapping[str, Any]) -> Any:
    """Normalize old string flow types and current enum flow types."""
    raw = result.get("type")
    if not isinstance(raw, str):
        return raw
    try:
        return FlowResultType(raw)
    except (TypeError, ValueError):
        return raw


def _marker_name(marker: Any) -> str | None:
    raw = marker.schema if isinstance(marker, vol.Marker) else marker
    return raw if isinstance(raw, str) and raw else None


def _marker_description(marker: Any) -> Mapping[str, Any]:
    description = getattr(marker, "description", None)
    return description if isinstance(description, Mapping) else {}


def _is_form_section(value: Any) -> bool:
    """Recognize HA form sections without raising Phoenix's HA import floor."""
    value_type = type(value)
    return (
        value_type.__module__ == "homeassistant.data_entry_flow"
        and value_type.__name__ == "section"
        and isinstance(getattr(value, "schema", None), vol.Schema)
    )


def _step_owned_fallback(
    marker: Any, inherited_suggestions: Mapping[str, Any] | None = None
) -> tuple[bool, Any]:
    """Return only a current value the edit step itself supplies.

    A static voluptuous default is intentionally not returned here. Omitting
    that field lets Home Assistant apply the same default it applies to the
    frontend form, while submitting it would overwrite an existing value.
    """
    description = _marker_description(marker)
    if "suggested_value" in description and description["suggested_value"] is not None:
        return True, description["suggested_value"]
    name = _marker_name(marker)
    if (
        name is not None
        and inherited_suggestions is not None
        and name in inherited_suggestions
    ):
        return True, inherited_suggestions[name]
    return False, None


def _clears_by_omission(marker: Any) -> bool:
    """Whether omitting a caller-supplied null clears this optional field."""
    return not isinstance(marker, vol.Required) and getattr(
        marker, "default", vol.UNDEFINED
    ) is vol.UNDEFINED


def _sanitize_public_schema(value: Any) -> Any:
    """Recursively remove values from serialized forms, including sections."""
    if isinstance(value, list):
        return [_sanitize_public_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    public = {
        key: _sanitize_public_schema(item)
        for key, item in value.items()
        if key not in {"default", "suggested_value"}
    }
    if is_sensitive_key(public.get("name")):
        # Selectors and constraints are useful; submitted/current values never are.
        public["sensitive"] = True
    return public


def _public_schema(result: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Serialize a form without leaking stored suggested/default values."""
    schema = result.get("data_schema")
    if schema is None:
        return []
    try:
        converted = voluptuous_serialize.convert(
            schema, custom_serializer=cv.custom_serializer
        )
    except Exception:  # an integration-defined schema outside HA serialization
        _LOGGER.debug("Could not serialize reconfigure schema", exc_info=True)
        return None
    if not isinstance(converted, list):
        return None
    sanitized = _sanitize_public_schema(converted)
    return sanitized if isinstance(sanitized, list) else None


def _form_payload(
    result: Mapping[str, Any], config: Mapping[str, Any], encountered: set[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Match flat caller values to one form, filling required HA fallbacks."""
    schema = result.get("data_schema")
    if schema is None:
        return {}, []

    def _walk(
        nested_schema: Any,
        inherited_suggestions: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        raw_schema = (
            nested_schema.schema
            if isinstance(nested_schema, vol.Schema)
            else None
        )
        if not isinstance(raw_schema, Mapping):
            return None, [
                "The integration returned a form schema Phoenix cannot drive."
            ]
        payload: dict[str, Any] = {}
        missing: list[str] = []
        for marker, validator in raw_schema.items():
            name = _marker_name(marker)
            if name is None:
                return None, ["The integration returned a non-string form field."]
            if _is_form_section(validator):
                found, section_values = _step_owned_fallback(
                    marker, inherited_suggestions
                )
                nested_suggestions = (
                    section_values
                    if found and isinstance(section_values, Mapping)
                    else None
                )
                section_payload, section_missing = _walk(
                    validator.schema, nested_suggestions
                )
                if section_payload is None:
                    return None, section_missing
                payload[name] = section_payload
                missing.extend(section_missing)
                continue

            # Phoenix's public contract is flat even when HA groups fields into
            # visual sections. A repeated leaf name therefore deliberately reuses
            # the same caller value across every form/section encounter.
            encountered.add(name)
            if name in config:
                if config[name] is None and _clears_by_omission(marker):
                    continue
                payload[name] = config[name]
                continue
            found, fallback = _step_owned_fallback(marker, inherited_suggestions)
            if found:
                payload[name] = fallback
            elif isinstance(marker, vol.Required):
                if getattr(marker, "default", vol.UNDEFINED) is vol.UNDEFINED:
                    missing.append(name)
        return payload, missing

    return _walk(schema)


async def _abort_flow(hass: HomeAssistant, flow_id: str | None) -> None:
    if not flow_id:
        return
    try:
        hass.config_entries.flow.async_abort(flow_id)
    except Exception:  # already completed/aborted is harmless
        _LOGGER.debug("Could not abort reconfigure flow %s", flow_id, exc_info=True)


def _modified(entry: Any) -> datetime:
    value = getattr(entry, "modified_at", None)
    if not isinstance(value, datetime):
        raise RuntimeError("Home Assistant did not expose config-entry modified_at")
    return value


async def _verify_reload(
    entry: Any,
    baseline: datetime,
    commit: datetime,
    events: list[tuple[datetime, ConfigEntryState]],
    changed: asyncio.Event,
    submit_boundary: int,
) -> bool:
    """Attribute a terminal reload to this commit's modified_at boundary."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RELOAD_VERIFY_TIMEOUT
    while True:
        if commit != baseline:
            eligible = [state for modified, state in events if modified == commit]
        else:
            # Identical accepted updates do not bump modified_at. Only state
            # transitions observed after Phoenix's first submission may count.
            eligible = [state for _, state in events[submit_boundary:]]
        if ConfigEntryState.LOADED in eligible:
            return True
        if any(state in _FAILURE_STATES for state in eligible):
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        changed.clear()
        try:
            await asyncio.wait_for(changed.wait(), timeout=remaining)
        except TimeoutError:
            return False


async def async_run_reconfigure_flow(
    hass: HomeAssistant,
    entry: Any,
    config: Mapping[str, Any],
    menu_choices: list[str],
) -> ReconfigureFlowResult:
    """Drive bounded FORM/MENU steps and observe the integration's reload."""
    baseline = _modified(entry)
    events: list[tuple[datetime, ConfigEntryState]] = []
    state_changed = asyncio.Event()

    def _on_state_change(*_: Any) -> None:
        try:
            events.append((_modified(entry), entry.state))
        except Exception:
            _LOGGER.debug("Could not record reconfigure state transition", exc_info=True)
        state_changed.set()

    on_state_change = getattr(entry, "async_on_state_change", None)
    listener_supported = callable(on_state_change)
    remove_listener = (
        cast(Any, on_state_change)(_on_state_change)
        if listener_supported
        else lambda: None
    )
    flow_id: str | None = None
    encountered: set[str] = set()
    menu_index = 0
    submitted = False
    submit_boundary = 0
    result: Mapping[str, Any] | None = None

    def _observed_apply() -> tuple[bool, datetime | None]:
        current = hass.config_entries.async_get_entry(entry.entry_id)
        commit = _modified(current) if current is not None else None
        return submitted and (commit is None or commit != baseline), commit

    try:
        try:
            result = await hass.config_entries.flow.async_init(
                entry.domain,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            flow_id = result.get("flow_id")
        except Exception as err:  # integration import/initialization failure
            return ReconfigureFlowResult(
                STATUS_APPLY_FAILED,
                False,
                "The integration's reconfigure flow could not be started.",
                {"error": type(err).__name__},
                baseline,
                None,
            )

        for _step in range(FLOW_STEP_BUDGET):
            result_type = _result_type(result)
            if result_type is FlowResultType.FORM:
                if submitted and result.get("errors"):
                    await _abort_flow(hass, flow_id)
                    current = hass.config_entries.async_get_entry(entry.entry_id)
                    commit = _modified(current) if current is not None else None
                    applied = commit is None or commit != baseline
                    return ReconfigureFlowResult(
                        STATUS_UNVERIFIED if applied else STATUS_ABORTED,
                        applied,
                        "The integration rejected the submitted values.",
                        {
                            "validation_errors": result.get("errors") or {},
                            "schema": _public_schema(result),
                            "step_id": result.get("step_id"),
                        },
                        baseline,
                        commit,
                    )
                payload, missing = _form_payload(result, config, encountered)
                if payload is None or missing:
                    await _abort_flow(hass, flow_id)
                    applied, commit = _observed_apply()
                    return ReconfigureFlowResult(
                        STATUS_INCOMPLETE if applied else STATUS_ABORTED,
                        applied,
                        (
                            missing[0]
                            if payload is None
                            else "Required values are missing from config."
                        ),
                        {
                            "missing_fields": missing,
                            "schema": _public_schema(result),
                            "step_id": result.get("step_id"),
                        },
                        baseline,
                        commit,
                    )
                submit_boundary = len(events) if not submitted else submit_boundary
                submitted = True
                try:
                    result = await hass.config_entries.flow.async_configure(
                        flow_id, payload
                    )
                except vol.Invalid as err:
                    await _abort_flow(hass, flow_id)
                    current = hass.config_entries.async_get_entry(entry.entry_id)
                    commit = _modified(current) if current is not None else None
                    applied = commit is None or commit != baseline
                    return ReconfigureFlowResult(
                        STATUS_UNVERIFIED if applied else STATUS_ABORTED,
                        applied,
                        "The integration rejected the submitted values.",
                        {
                            "validation_error": str(err),
                            "schema": _public_schema(result),
                            "step_id": result.get("step_id"),
                        },
                        baseline,
                        commit,
                    )
                continue

            if result_type is FlowResultType.MENU:
                raw_options = result.get("menu_options")
                options = (
                    list(raw_options)
                    if isinstance(raw_options, (list, tuple))
                    else []
                )
                if menu_index >= len(menu_choices):
                    await _abort_flow(hass, flow_id)
                    applied, commit = _observed_apply()
                    return ReconfigureFlowResult(
                        STATUS_INCOMPLETE if applied else STATUS_ABORTED,
                        applied,
                        "The reconfigure flow requires another menu choice.",
                        {
                            "step_id": result.get("step_id"),
                            "menu_options": options,
                            "menu_choice_index": menu_index,
                        },
                        baseline,
                        commit,
                    )
                choice = menu_choices[menu_index]
                menu_index += 1
                if options and choice not in options:
                    await _abort_flow(hass, flow_id)
                    applied, commit = _observed_apply()
                    return ReconfigureFlowResult(
                        STATUS_INCOMPLETE if applied else STATUS_ABORTED,
                        applied,
                        "A menu choice is not offered by the integration.",
                        {
                            "step_id": result.get("step_id"),
                            "menu_options": options,
                            "menu_choice_index": menu_index - 1,
                        },
                        baseline,
                        commit,
                    )
                result = await hass.config_entries.flow.async_configure(
                    flow_id, {"next_step_id": choice}
                )
                continue

            if result_type in _UNSUPPORTED_TYPES:
                await _abort_flow(hass, flow_id)
                applied, commit = _observed_apply()
                return ReconfigureFlowResult(
                    STATUS_INCOMPLETE if applied else STATUS_ABORTED,
                    applied,
                    "Browser/OAuth and asynchronous progress steps are unsupported; use Home Assistant's frontend.",
                    {"step_type": getattr(result_type, "value", result_type)},
                    baseline,
                    commit,
                )

            current = hass.config_entries.async_get_entry(entry.entry_id)
            commit = _modified(current) if current is not None else None
            applied = submitted and (commit is None or commit != baseline)
            if (
                result_type is FlowResultType.ABORT
                and result.get("reason") == RECONFIGURE_SUCCESS_REASON
            ):
                # The official helper updates before returning this abort. A
                # missing entry after success is an applied identity failure,
                # classified by the caller after it re-captures identity.
                commit = commit or baseline
                verified = (
                    await _verify_reload(
                        entry,
                        baseline,
                        commit,
                        events,
                        state_changed,
                        submit_boundary,
                    )
                    if listener_supported
                    else False
                )
                unused = sorted(set(config) - encountered)
                unused_menus = menu_choices[menu_index:]
                incomplete = bool(unused or unused_menus)
                return ReconfigureFlowResult(
                    STATUS_INCOMPLETE
                    if incomplete
                    else STATUS_VERIFIED
                    if verified
                    else STATUS_UNVERIFIED,
                    True,
                    (
                        "Home Assistant accepted the reconfigure flow, but some supplied values were unused."
                        if incomplete
                        else "Home Assistant accepted the reconfigure flow."
                    ),
                    {
                        "unused_config_fields": unused,
                        "unused_menu_choices": unused_menus,
                        "reload_verified": verified,
                    },
                    baseline,
                    commit,
                )

            await _abort_flow(hass, flow_id)
            if result_type is FlowResultType.FORM and result.get("errors"):
                reason = "The integration rejected the submitted values."
            elif result_type is FlowResultType.ABORT:
                reason = "The integration aborted the reconfigure flow before applying it."
            else:
                reason = "The integration ended its reconfigure flow without the required success result."
            return ReconfigureFlowResult(
                STATUS_UNVERIFIED if applied else STATUS_ABORTED,
                applied,
                reason,
                {
                    "flow_result_type": getattr(result_type, "value", result_type),
                    "flow_reason": result.get("reason"),
                    "validation_errors": result.get("errors") or {},
                    "schema": _public_schema(result)
                    if result_type is FlowResultType.FORM
                    else None,
                    "step_id": result.get("step_id"),
                },
                baseline,
                commit,
            )

        await _abort_flow(hass, flow_id)
        current = hass.config_entries.async_get_entry(entry.entry_id)
        commit = _modified(current) if current is not None else None
        applied = submitted and (commit is None or commit != baseline)
        return ReconfigureFlowResult(
            STATUS_INCOMPLETE if applied else STATUS_ABORTED,
            applied,
            "The reconfigure flow exceeded Phoenix's bounded step budget.",
            {"step_budget": FLOW_STEP_BUDGET},
            baseline,
            commit,
        )
    except Exception as err:  # third-party flow boundary; retain ambiguity
        _LOGGER.exception("Reconfigure flow failed for %s", entry.entry_id)
        await _abort_flow(hass, flow_id)
        current = hass.config_entries.async_get_entry(entry.entry_id)
        commit = _modified(current) if current is not None else None
        applied = submitted and (commit is None or commit != baseline)
        return ReconfigureFlowResult(
            STATUS_UNVERIFIED if applied else STATUS_APPLY_FAILED,
            applied,
            (
                "The reconfigure flow failed after the integration may have applied the change."
                if applied
                else "The reconfigure flow failed before Phoenix observed an applied change."
            ),
            {"error": type(err).__name__},
            baseline,
            commit,
        )
    finally:
        remove_listener()
