"""Energy dashboard tools: the preferences read and the addressed write.

get_energy_config (cap_config_read) and edit_energy_config (cap_energy_write).

The Energy dashboard's preferences are the mapping from a statistic to its role
(grid / solar / battery / gas / water, or an individual device). They live in
.storage/energy and are reachable ONLY through the energy/get_prefs WebSocket
command, so this module is the single path to them; no state read, registry
read or filesystem tool can see them.

The gap that motivated it: nothing in Home Assistant records that an Energy
source depends on an integration. Removing that integration orphans the source
silently, the dashboard keeps its configuration pointing at a statistic that no
longer produces data, and no other read Phoenix MCP offers would have shown the
dependency beforehand.

Scoping is the whole reason this is not a passthrough. energy/get_prefs runs
under the resolved admin user (ws_dispatch has no other mode), so the raw result
names every statistic on the instance regardless of the calling token. The
result is therefore passed through filter_service_response before it is
returned, which replaces an entity the token cannot resolve with the redaction
sentinel and drops entity-keyed entries outright.

THE WRITE IS ADDRESSED, NEVER WHOLESALE, and that shape is the whole design.
energy/save_prefs full-replaces each top-level key it receives, so a caller that
composed "device_consumption" from a read would delete every device the read had
redacted. edit_energy_config therefore never accepts a preferences blob. It takes
ONE addressed operation, re-reads the current preferences UNREDACTED inside the
executor, applies that operation, checks the result differs from the original in
exactly the intended way (`_verify_structure`), and sends only the top-level keys
that actually changed. The agent's redacted view is never a write input, so the
rule 32 hazard cannot arise from the config at all, only from a literal value the
caller supplied, which is refused explicitly.

Every write records a version, and a restore writes the snapshot back wholesale.
That is the one legitimate whole-prefs write: reproducing the operator's chosen
snapshot IS the operation, the same exemption rule 31 grants a YAML restore.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from typing import Any
import copy
import json
import logging

from homeassistant.core import HomeAssistant, valid_entity_id

from ..const import CAP_DENY, REDACTION_SENTINEL
from ..data import PhoenixData
from ..tool_contracts import normalize_tool_args
from ..helpers import (
    diff_summary_fields as _summary,
    effective_cap,
    str_arg,
    version_summary_fields as _version_summary,
)
from ..policy_engine import Permission, filter_service_response, resolve
from ..token_store import TokenRecord
from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _gate,
    _record_version,
    _tool_error,
    _tool_success,
    _truncate,
    redaction_sentinel_path,
)
from ..ws_dispatch import WsDispatchError, async_ws_command

_LOGGER = logging.getLogger(__name__)


# The three top-level keys of HA's EnergyPreferences. device_consumption_water is
# NotRequired upstream, so an instance configured before it existed returns two
# keys rather than three; every one of them is emitted here so a caller never has
# to distinguish "no water devices" from "this HA is too old to have the key".
_PREFS_KEYS = ("energy_sources", "device_consumption", "device_consumption_water")

# The two dispatch failures that both mean "there is no Energy configuration to
# look at", which is an ANSWER (configured: false) rather than a failed read.
# Reporting either as a tool error would tell an agent checking for a dependency
# that the check itself did not run, which is the opposite of what happened.
#
#   not_found      energy/get_prefs answers ERR_NOT_FOUND "No prefs" on an
#                  instance whose Energy dashboard was never set up, rather than
#                  returning empty lists. ws_dispatch formats a handler error as
#                  "<code>: <message>", so the code is what is matched.
#   not available  the energy component is not set up, so the command is not in
#                  hass.data["websocket_api"] and ws_dispatch refuses before any
#                  handler lookup. An HA without the component has no Energy
#                  dashboard for anything to depend on.
#
# Substring matching is deliberate but narrow: both strings are ws_dispatch's own
# formatting, one function away, not HA's. Any OTHER failure still surfaces.
_NO_CONFIG_ERRORS = ("not_found", "not available")


def _default_prefs() -> dict[str, Any]:
    """Return the empty preferences structure for a never-configured instance.

    Mirrors EnergyManager.default_preferences() in HA's energy/data.py. Reported
    with configured=False so a caller can tell "the operator has not set Energy
    up" from "they set it up and then removed everything", which are different
    situations and would otherwise both arrive as three empty lists.
    """
    return {key: [] for key in _PREFS_KEYS}


def _referenced_entities(prefs: Any, _depth: int = 0) -> list[str]:
    """Collect every entity id the preferences reference, sorted and deduplicated.

    Walks the whole structure rather than reading a fixed list of key names
    (stat_energy_from, stat_consumption, entity_energy_price, ...) because those
    differ per source type and HA adds to them; a name list would go stale
    silently, reporting no dependency where one exists, which is the exact
    failure this tool is meant to prevent.

    Call this on the REDACTED preferences, never the raw ones: the sentinel is
    skipped here, so an entity the token cannot resolve is absent from the result
    instead of being named in it. External statistics ("domain:name") are not
    entity ids and are correctly not collected.
    """
    if _depth > 10:
        return []
    if isinstance(prefs, str):
        if prefs != REDACTION_SENTINEL and valid_entity_id(prefs):
            return [prefs]
        return []
    if isinstance(prefs, dict):
        found: list[str] = []
        for value in prefs.values():
            found.extend(_referenced_entities(value, _depth + 1))
        return found
    if isinstance(prefs, list):
        found = []
        for item in prefs:
            found.extend(_referenced_entities(item, _depth + 1))
        return found
    return []


async def _tool_get_energy_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read the Energy dashboard preferences (cap_config_read)."""
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_energy_config"

    configured = True
    try:
        prefs = await async_ws_command(hass, "energy/get_prefs", {})
    except WsDispatchError as err:
        message = str(err).lower()
        if any(marker in message for marker in _NO_CONFIG_ERRORS):
            prefs, configured = _default_prefs(), False
        else:
            _LOGGER.exception("get_energy_config: energy/get_prefs failed")
            return (
                _tool_error("Could not read the Energy dashboard configuration."),
                "invalid_request",
                "get_energy_config",
            )

    if not isinstance(prefs, dict):
        # Defensive: HA has always sent a mapping here, but a shape change would
        # otherwise reach filter_service_response and be returned as a bare value.
        _LOGGER.warning("get_energy_config: unexpected prefs type %s", type(prefs).__name__)
        prefs, configured = _default_prefs(), False

    body = _scoped_body(prefs, configured, token, hass)
    # Included on every read rather than behind a flag: the caller asking what the
    # Energy dashboard depends on is exactly the caller who needs to know which of
    # those dependencies is ALREADY dead, and a flag defaulting off would have to
    # be discovered before it could help.
    body["issues"] = await _energy_issues(prefs, token, hass) if configured else []
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_energy_config"


async def _tool_get_solar_forecast(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read solar production forecasts (cap_config_read).

    Empty when no solar source names a forecast config entry, which is the normal
    state without a forecast integration installed; that is an answer, not an
    error. Forecast values are per config entry and name no entities, so there is
    nothing here to scope, but the result is filtered anyway rather than trusted to
    stay that shape.
    """
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_solar_forecast"
    try:
        forecast = await async_ws_command(hass, "energy/solar_forecast", {})
    except WsDispatchError as err:
        if any(marker in str(err).lower() for marker in _NO_CONFIG_ERRORS):
            forecast = {}
        else:
            _LOGGER.exception("get_solar_forecast: energy/solar_forecast failed")
            return (
                _tool_error("Could not read the solar forecast."),
                "invalid_request",
                "get_solar_forecast",
            )
    if not isinstance(forecast, dict):
        forecast = {}
    body = {
        "configured": bool(forecast),
        "forecasts": filter_service_response(_plain(forecast), token, hass),
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_solar_forecast"


def _scoped_body(prefs: dict, configured: bool, token: TokenRecord, hass: HomeAssistant) -> dict:
    """The token-scoped response body. Shared by the read and the write's result."""
    scoped = filter_service_response(prefs, token, hass)
    return {
        "configured": configured,
        **{key: scoped.get(key) or [] for key in _PREFS_KEYS},
        "referenced_entities": sorted(set(_referenced_entities(scoped))),
    }


def _plain(value: Any) -> Any:
    """Recursively turn sets and tuples into lists.

    Two things need this, and both fail SILENTLY without it. HA's ValidationIssue
    carries `affected_entities` as a set of TUPLES: json.dumps cannot serialize a
    set at all, and filter_service_response walks dicts, lists and strings only, so
    a tuple falls straight through its recursion and would return entity ids the
    token cannot see. Normalising first is what puts those ids somewhere the
    redactor actually looks.
    """
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _entry_label(key: str, index: int, prefs: dict) -> str:
    """Name the configuration entry an issue is about, in the operator's terms.

    HA reports issues as three arrays positionally parallel to the preferences
    lists, which is unreadable on its own; this pairs each back with the thing the
    operator recognises (a device's own label, or the source's type).
    """
    entries = prefs.get(key) or []
    entry = entries[index] if index < len(entries) else None
    if not isinstance(entry, dict):
        return f"{key}[{index}]"
    if key == "energy_sources":
        return str(entry.get("name") or entry.get("type") or f"{key}[{index}]")
    return str(entry.get("name") or entry.get("stat_consumption") or f"{key}[{index}]")


async def _energy_issues(
    prefs: dict, token: TokenRecord, hass: HomeAssistant
) -> list[dict] | None:
    """Flatten energy/validate into one scoped, labelled list of problems.

    Returns None when validation could not run, which is reported as a null field
    rather than an empty list: "nothing is wrong" and "nobody checked" must not
    look the same to a caller deciding whether it is safe to delete something.

    Best-effort by design. This rides on a plain config READ, so a validation
    failure degrades the extra field instead of failing the read the caller
    actually asked for.
    """
    try:
        report = await async_ws_command(hass, "energy/validate", {})
    except WsDispatchError:
        _LOGGER.debug("energy/validate unavailable; reporting issues as unknown", exc_info=True)
        return None
    if not isinstance(report, dict):
        return None
    issues: list[dict] = []
    for key in _PREFS_KEYS:
        for index, per_entry in enumerate(report.get(key) or []):
            for issue in per_entry or []:
                if not isinstance(issue, dict):
                    continue
                issues.append({
                    "where": key,
                    "index": index,
                    "label": _entry_label(key, index, prefs),
                    "type": issue.get("type"),
                    "affected_entities": _plain(issue.get("affected_entities") or []),
                })
    # Scoped last, over plain containers, so an issue naming an entity this token
    # cannot resolve reports the redaction sentinel rather than the id.
    return filter_service_response(issues, token, hass)


# ---------------------------------------------------------------------------
# edit_energy_config (cap_energy_write)
# ---------------------------------------------------------------------------

_OPS = ("replace_statistic", "add_device", "remove_device", "rename_device", "set_source", "remove_source")
_SOURCE_TYPES = ("grid", "solar", "battery", "gas", "water")

# The fields this tool will write per source type, mirrored from HA's own
# *_SOURCE_SCHEMA in components/energy/data.py. Deliberately a SUBSET: stat_rate,
# power_config, stat_soc and config_entry_solar_forecast are omitted because they
# are set up by HA's own flows and a hand-written value is more likely to break a
# working source than to fix one. A field absent here is refused by name, so a
# caller learns the field is not writable rather than having it silently dropped.
# HA-VERSION-SENSITIVE: re-verify the schemas on upgrades.
#
# The price fields come in two shapes and both are offered, because a tariff is
# either fixed or it is not: number_* is a constant, entity_energy_price* names a
# sensor publishing the current rate (time-of-use), and stat_cost / stat_compensation
# name an already-existing cost statistic instead of letting HA derive one. HA
# refuses a price on a source whose meter is an EXTERNAL statistic
# (_reject_price_for_external_stat), so that combination fails at save with its own
# message rather than being second-guessed here.
_SOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "grid": (
        "stat_energy_from", "stat_energy_to",
        "stat_cost", "entity_energy_price", "number_energy_price",
        "stat_compensation", "entity_energy_price_export", "number_energy_price_export",
    ),
    "solar": ("stat_energy_from",),
    "battery": ("stat_energy_from", "stat_energy_to"),
    "gas": ("stat_energy_from", "stat_cost", "entity_energy_price", "number_energy_price"),
    "water": ("stat_energy_from", "stat_cost", "entity_energy_price", "number_energy_price"),
}

# Every writable field across all types, so an unknown-field refusal can name what
# the caller sent instead of only what the type accepts. Derived, never a copy.
_ALL_SOURCE_FIELDS = frozenset(f for fields in _SOURCE_FIELDS.values() for f in fields)

# What HA's schema marks vol.Required, so a CREATE that omits one would be
# refused by the dispatch schema with a message about a key the caller never saw.
_SOURCE_REQUIRED: dict[str, tuple[str, ...]] = {
    "grid": (),  # only cost_adjustment_day, which _new_source supplies
    "solar": ("stat_energy_from",),
    "battery": ("stat_energy_from", "stat_energy_to"),
    "gas": ("stat_energy_from",),
    "water": ("stat_energy_from",),
}

# Fields naming a statistic or an entity rather than holding a number. Only these
# are checked against the token's scope, and only when being WRITTEN.
_STATISTIC_FIELDS = frozenset({
    "stat_energy_from", "stat_energy_to", "stat_cost", "stat_compensation",
    "entity_energy_price", "entity_energy_price_export",
})


class _OpError(Exception):
    """An operation that cannot be applied to the current preferences."""


def _is_external_statistic(value: str) -> bool:
    """Whether a statistic id belongs to an external source ("domain:name").

    External statistics are not entities, so they never resolve through the
    permission tree and must not be rejected for failing to.
    """
    return ":" in value


# The prefs keys holding device_consumption entries. Both are searched when
# addressing or replacing, so a water device is reachable; add_device writes to
# the electricity list, which is what "individual device" means on the dashboard.
_DEVICE_KEYS = ("device_consumption", "device_consumption_water")


def _find_device(prefs: dict, statistic: str, device_name: str) -> tuple[str, int]:
    """Locate one device entry by statistic id or by its display name.

    Addressing by NAME is not a convenience: a device whose statistic the token
    cannot resolve comes back as the redaction sentinel, so its id is genuinely
    unavailable to the caller and the name is the only handle left. That is the
    common case for a dead entry, which is exactly what an operator wants to fix.
    Raises _OpError when nothing or more than one thing matches.
    """
    hits = [
        (key, index)
        for key in _DEVICE_KEYS
        for index, entry in enumerate(prefs.get(key) or [])
        if isinstance(entry, dict)
        and (
            (statistic and entry.get("stat_consumption") == statistic)
            or (device_name and str(entry.get("name") or "").strip().lower() == device_name.lower())
        )
    ]
    if not hits:
        addressed = statistic or device_name
        raise _OpError(f"No Energy device entry matches {addressed!r}. Call get_energy_config to see the current entries.")
    if len(hits) > 1:
        raise _OpError(
            f"{len(hits)} Energy device entries match {(statistic or device_name)!r}. "
            "Address the one you mean by its statistic id."
        )
    return hits[0]


def _replace_everywhere(prefs: dict, old: str, new: str) -> int:
    """Point every occurrence of one statistic id at another. Returns the count.

    Walks sources AND both device lists, because a statistic can legitimately
    appear in several places (a battery's charge meter, a device entry for the
    same appliance) and a migration that fixed only one would leave the rest
    pointing at an integration that is about to be removed.
    """
    changed = 0
    for source in prefs.get("energy_sources") or []:
        if not isinstance(source, dict):
            continue
        for field in _STATISTIC_FIELDS | {"stat_cost", "stat_compensation", "entity_energy_price", "stat_rate"}:
            if source.get(field) == old:
                source[field] = new
                changed += 1
    for key in _DEVICE_KEYS:
        for entry in prefs.get(key) or []:
            if isinstance(entry, dict):
                for field in ("stat_consumption", "stat_rate", "included_in_stat"):
                    if entry.get(field) == old:
                        entry[field] = new
                        changed += 1
    return changed


# HA's per-source CROSS-FIELD rules, mirrored from the validators wired onto
# *_SOURCE_SCHEMA in components/energy/data.py. Each is (import-side, export-side)
# key groups: the meter, its price-entity, its price-number, and its cost stat.
# Only grid has an export side; gas and water carry the import group alone, and
# solar/battery have no cost fields at all.
#
# WHY THESE ARE MIRRORED rather than left to HA: per-field validation cannot see
# them, so a call that sets both a price entity AND a price number passes every
# check here, becomes a PendingApproval, waits for an admin to review it, and only
# then fails at save. Live-hit exactly that way on 2026-08-03, costing a real
# approval click. Rule 29 is about not spending a human's attention on a write
# that was never going to land.
# HA-VERSION-SENSITIVE: re-verify the validators on upgrades.
_PRICE_GROUPS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "grid": (
        ("stat_energy_from", "entity_energy_price", "number_energy_price", "stat_cost"),
        ("stat_energy_to", "entity_energy_price_export", "number_energy_price_export", "stat_compensation"),
    ),
    "gas": (("stat_energy_from", "entity_energy_price", "number_energy_price", "stat_cost"),),
    "water": (("stat_energy_from", "entity_energy_price", "number_energy_price", "stat_cost"),),
}


def _check_source_cross_fields(source: dict, source_type: str) -> None:
    """Refuse a source HA's own schema would reject for a cross-field reason.

    Called on the MERGED source (existing values plus the caller's), because every
    rule here reads more than one field and an update supplies only some of them.
    Raises _OpError with HA's own wording, so the message a caller sees pre-gate is
    the message they would have seen at save.
    """
    for meter, entity_price, number_price, cost_stat in _PRICE_GROUPS.get(source_type, ()):
        side = "export" if meter == "stat_energy_to" else "import"
        if source.get(entity_price) is not None and source.get(number_price) is not None:
            raise _OpError(
                f"Define either an entity or a fixed number for {side} price, not both "
                f"({entity_price} and {number_price}). Set the one you do not want to null."
            )
        stat_id = source.get(meter)
        # A price cannot be derived for an EXTERNAL statistic; HA wants a ready-made
        # cost statistic instead, and skips this rule when one is already set.
        if (
            stat_id is not None
            and not valid_entity_id(stat_id)
            and source.get(cost_stat) is None
            and (source.get(entity_price) is not None or source.get(number_price) is not None)
        ):
            raise _OpError(
                f"{meter} is an external statistic, so a price cannot be applied to it. "
                f"Set {cost_stat} to an existing cost statistic instead."
            )
    if source_type == "grid" and not any(
        source.get(k) is not None for k in ("stat_energy_from", "stat_energy_to", "stat_rate", "power_config")
    ):
        raise _OpError(
            "A grid source must keep at least one of an import meter, an export meter, "
            "or a power sensor."
        )


def _new_source(source_type: str) -> dict:
    """A minimal valid source of one type, ready for the caller's fields.

    cost_adjustment_day is HA's only vol.Required field on a grid source and has
    no sensible caller-facing meaning, so it is supplied rather than asked for.
    """
    source: dict[str, Any] = {"type": source_type}
    if source_type == "grid":
        source["cost_adjustment_day"] = 0.0
    return source


def _check_writable_statistic(value: Any, field: str, token: TokenRecord, hass: HomeAssistant) -> str:
    """Validate one statistic id the caller wants WRITTEN into the preferences.

    Applied only to values being written, never to the ones used to ADDRESS an
    existing entry: a dead entry is addressed by the very id that no longer
    resolves, and refusing that would make the broken entries unfixable, which is
    half the reason this tool exists.
    """
    statistic = str_arg(value).strip()
    if not statistic:
        raise _OpError(f"{field} must be an entity id or an external statistic id.")
    if _is_external_statistic(statistic):
        return statistic
    if not valid_entity_id(statistic):
        raise _OpError(f"{field}: {statistic!r} is not a valid entity id or external statistic id.")
    if resolve(statistic, token, hass) in (Permission.NO_ACCESS, Permission.DENY, Permission.NOT_FOUND):
        # Byte-identical for out-of-scope and nonexistent (rule 12). Naming the
        # field is safe: it is the caller's own argument, not hidden state.
        raise _OpError(
            f"{field}: {statistic!r} is not an entity this token can use. "
            "Check the entity id with search_entities."
        )
    return statistic


def _source_fields_arg(args: dict, source_type: str, token: TokenRecord, hass: HomeAssistant) -> dict:
    """The caller's source fields, validated against the type's own schema.

    An explicit null is kept rather than skipped: clearing a price or a cost
    statistic is a real edit, and dropping it would silently turn "unset this" into
    a no-op the caller has no way to notice.
    """
    supplied = {field: args[field] for field in _SOURCE_FIELDS[source_type] if field in args}
    unknown = [f for f in _ALL_SOURCE_FIELDS if f in args and f not in _SOURCE_FIELDS[source_type]]
    if unknown:
        raise _OpError(
            f"A {source_type} source does not take {', '.join(sorted(unknown))}. "
            f"It accepts: {', '.join(_SOURCE_FIELDS[source_type])}."
        )
    if not supplied:
        raise _OpError(
            f"set_source needs at least one field to set. A {source_type} source accepts: "
            f"{', '.join(_SOURCE_FIELDS[source_type])}."
        )
    out: dict[str, Any] = {}
    for field, value in supplied.items():
        if value is None:
            out[field] = None
            continue
        if field in _STATISTIC_FIELDS:
            out[field] = _check_writable_statistic(value, field, token, hass)
            continue
        try:
            out[field] = float(value)
        except (TypeError, ValueError):
            raise _OpError(f"{field} must be a number or null.") from None
    return out


def _apply_op(prefs: dict, args: dict, token: TokenRecord, hass: HomeAssistant) -> tuple[dict, dict]:
    """Apply ONE addressed operation to a copy of prefs.

    Returns (new_prefs, summary), where summary carries the fields the diff and
    version templates render. Raises _OpError with a caller-facing message for
    anything that cannot be applied; the caller maps that to invalid_request.
    """
    op = str_arg(args.get("op")).strip().lower()
    if op not in _OPS:
        raise _OpError(f"op must be one of: {', '.join(_OPS)}.")
    new = copy.deepcopy(prefs)
    statistic = str_arg(args.get("statistic")).strip()
    device_name = str_arg(args.get("device_name")).strip()

    if op == "replace_statistic":
        new_statistic = _check_writable_statistic(args.get("new_statistic"), "new_statistic", token, hass)
        old = statistic
        label = device_name or statistic
        if not old:
            if not device_name:
                raise _OpError("replace_statistic needs either statistic or device_name to say what to repoint.")
            key, index = _find_device(new, "", device_name)
            old = str_arg(new[key][index].get("stat_consumption")).strip()
            label = str(new[key][index].get("name") or device_name)
            if not old:
                raise _OpError(f"The Energy device entry {device_name!r} has no statistic to replace.")
        if old == new_statistic:
            raise _OpError(f"{new_statistic} is already what {label!r} points at.")
        if _replace_everywhere(new, old, new_statistic) == 0:
            raise _OpError(f"{old!r} is not referenced anywhere in the Energy configuration.")
        return new, {"op": op, "label": label, "old_statistic": old, "new_statistic": new_statistic}

    if op == "add_device":
        new_statistic = _check_writable_statistic(args.get("statistic"), "statistic", token, hass)
        for key in _DEVICE_KEYS:
            for entry in new.get(key) or []:
                if isinstance(entry, dict) and entry.get("stat_consumption") == new_statistic:
                    raise _OpError(f"{new_statistic} is already tracked as an individual device.")
        name = str_arg(args.get("name")).strip()
        added: dict[str, Any] = {"stat_consumption": new_statistic}
        if name:
            added["name"] = name
        new.setdefault("device_consumption", []).append(added)
        return new, {"op": op, "label": name or new_statistic, "new_statistic": new_statistic}

    if op == "remove_device":
        if not statistic and not device_name:
            raise _OpError("remove_device needs either statistic or device_name to say what to remove.")
        key, index = _find_device(new, statistic, device_name)
        removed = new[key].pop(index)
        return new, {
            "op": op,
            "label": str(removed.get("name") or removed.get("stat_consumption") or device_name or statistic),
            "old_statistic": str(removed.get("stat_consumption") or ""),
        }

    if op == "rename_device":
        if not statistic and not device_name:
            raise _OpError("rename_device needs either statistic or device_name to say which entry to rename.")
        name = str_arg(args.get("name")).strip()
        if not name:
            raise _OpError("rename_device needs a name.")
        key, index = _find_device(new, statistic, device_name)
        entry = new[key][index]
        previous = str(entry.get("name") or entry.get("stat_consumption") or "")
        if previous == name:
            raise _OpError(f"That entry is already called {name!r}.")
        entry["name"] = name
        return new, {"op": op, "label": name, "old_label": previous}

    if op == "remove_source":
        source_type = str_arg(args.get("source_type")).strip().lower()
        if source_type not in _SOURCE_TYPES:
            raise _OpError(f"source_type must be one of: {', '.join(_SOURCE_TYPES)}.")
        sources = new.get("energy_sources") or []
        matches = [i for i, s in enumerate(sources) if isinstance(s, dict) and s.get("type") == source_type]
        if not matches:
            raise _OpError(f"There is no {source_type} source to remove.")
        if len(matches) > 1:
            # HA allows several of a type (multiple grid connections, several
            # batteries). Removing "the first one" would be a coin flip on which of
            # the operator's meters stops being counted, so refuse and say so.
            raise _OpError(
                f"There are {len(matches)} {source_type} sources. Removing one by type would be "
                "ambiguous; remove it in the Energy dashboard UI."
            )
        removed = sources.pop(matches[0])
        return new, {
            "op": op,
            "source_type": source_type,
            "label": source_type,
            "old_statistic": str(removed.get("stat_energy_from") or ""),
        }

    source_type = str_arg(args.get("source_type")).strip().lower()
    if source_type not in _SOURCE_TYPES:
        raise _OpError(f"source_type must be one of: {', '.join(_SOURCE_TYPES)}.")
    fields = _source_fields_arg(args, source_type, token, hass)
    sources = new.setdefault("energy_sources", [])
    existing = next(
        (s for s in sources if isinstance(s, dict) and s.get("type") == source_type), None
    )
    if existing is None:
        missing = [f for f in _SOURCE_REQUIRED[source_type] if f not in fields]
        if missing:
            raise _OpError(
                f"There is no {source_type} source yet, and creating one requires "
                f"{', '.join(missing)}."
            )
        source = _new_source(source_type)
        source.update(fields)
        _check_source_cross_fields(source, source_type)
        sources.append(source)
        action = "create"
    else:
        # Checked on the MERGED result, not the caller's fields alone: an update
        # supplying only a price has to be judged against the meter already there.
        merged = {**existing, **fields}
        _check_source_cross_fields(merged, source_type)
        existing.update(fields)
        action = "update"
    return new, {
        "op": f"set_source.{action}",
        "source_type": source_type,
        "fields": ", ".join(sorted(fields)),
        "label": source_type,
    }


def _verify_structure(before: dict, after: dict, op: str) -> None:
    """Refuse a result that changed more than the operation is allowed to change.

    The backstop for HA's full-replace write semantics: this is what would catch a
    bug in _apply_op before it reaches the wire, rather than after the operator's
    other entries are already gone. Counts only, because every op here has an
    exact, knowable effect on the list lengths.
    """
    def counts(prefs: dict) -> tuple[int, int, int]:
        return tuple(len(prefs.get(key) or []) for key in _PREFS_KEYS)  # type: ignore[return-value]

    sources_before, elec_before, water_before = counts(before)
    sources_after, elec_after, water_after = counts(after)
    devices_before, devices_after = elec_before + water_before, elec_after + water_after

    if op in ("replace_statistic", "set_source.update", "rename_device"):
        allowed = sources_after == sources_before and devices_after == devices_before
    elif op == "set_source.create":
        allowed = sources_after == sources_before + 1 and devices_after == devices_before
    elif op == "add_device":
        allowed = sources_after == sources_before and devices_after == devices_before + 1
    elif op == "remove_device":
        allowed = sources_after == sources_before and devices_after == devices_before - 1
    elif op == "remove_source":
        allowed = sources_after == sources_before - 1 and devices_after == devices_before
    else:  # pragma: no cover - _apply_op already rejected an unknown op
        allowed = False
    if not allowed:
        raise _OpError(
            "Refusing to write: the change would alter the Energy configuration beyond "
            f"what '{op}' should touch "
            f"({sources_before} sources / {devices_before} devices before, "
            f"{sources_after} / {devices_after} after)."
        )


async def _read_prefs_raw(hass: HomeAssistant) -> dict:
    """The current preferences, UNREDACTED, defaulting when nothing is configured.

    Unredacted is the point: this is the base a write is applied to, so an entry
    the calling token cannot resolve must survive as its real statistic id rather
    than being rewritten to the redaction sentinel.
    """
    try:
        prefs = await async_ws_command(hass, "energy/get_prefs", {})
    except WsDispatchError as err:
        if any(marker in str(err).lower() for marker in _NO_CONFIG_ERRORS):
            return _default_prefs()
        raise
    return prefs if isinstance(prefs, dict) else _default_prefs()


def _sentinel_refusal(args: dict) -> str | None:
    """Refuse a caller-supplied value carrying the redaction placeholder (rule 32).

    Only the caller's own arguments can introduce one, because no part of the
    stored configuration is ever resent through this tool.
    """
    for field in ("statistic", "new_statistic", "name", "device_name", "stat_energy_from", "stat_energy_to"):
        if field in args and redaction_sentinel_path(args[field]) is not None:
            return (
                f"{field} contains the redaction placeholder {REDACTION_SENTINEL!r}. That placeholder is "
                "what a read substitutes for an entity this token cannot resolve, so it names nothing. "
                "Address the entry by device_name instead, or send the real entity id."
            )
    return None


def _preview_rows(prefs: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """A compact, scoped view of the preferences for the panel's visual preview.

    STRUCTURED, not a JSON string, deliberately. The panel could reparse
    diff.before/after instead, but those go through _truncate, and a truncated
    JSON string does not parse: exactly the bug the card preview hit when a large
    card exceeded the bound. These rows are small enough that the question never
    arises, and they are the shape the preview actually renders.

    Scoped through filter_service_response like every other read here, so a
    statistic the token cannot resolve previews as the sentinel rather than being
    disclosed to whoever opens the approval.
    """
    scoped = filter_service_response(prefs, token, hass)
    sources = []
    for source in scoped.get("energy_sources") or []:
        if not isinstance(source, dict):
            continue
        sources.append({
            "type": source.get("type"),
            "name": source.get("name"),
            # Ordered pairs rather than a dict so the panel renders import before
            # export without needing to know the field names itself.
            "meters": [
                [field, source[field]]
                for field in ("stat_energy_from", "stat_energy_to")
                if source.get(field)
            ],
            "price": source.get("number_energy_price") if source.get("number_energy_price") is not None
                     else source.get("entity_energy_price"),
        })
    devices = []
    for key in _DEVICE_KEYS:
        for entry in scoped.get(key) or []:
            if isinstance(entry, dict):
                devices.append({
                    "name": entry.get("name"),
                    "statistic": entry.get("stat_consumption"),
                    "water": key.endswith("_water"),
                })
    return {"sources": sources, "devices": devices}


async def _build_diff_edit_energy_config(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Approval diff for one Energy edit, in the operator's terms.

    Best-effort and never raises: the precheck has already validated, and a diff
    that failed to build must not turn an allowed call into an error.
    """
    diff: dict[str, Any] = {
        "kind": "config_diff",
        "target": {"type": "energy", "id": "preferences", "label": "Energy dashboard"},
    }
    try:
        before = await _read_prefs_raw(hass)
        after, summary = _apply_op(before, args, token, hass)
        diff.update(_summary(f"edit_energy_config.{summary['op']}", **{
            k: v for k, v in summary.items() if k != "op"
        }))
        diff["before"] = _truncate(json.dumps(filter_service_response(before, token, hass), indent=2, default=str))
        diff["after"] = _truncate(json.dumps(filter_service_response(after, token, hass), indent=2, default=str))
        diff["preview"] = {
            **{k: v for k, v in summary.items() if k != "op"},
            "energy": {"before": _preview_rows(before, token, hass), "after": _preview_rows(after, token, hass)},
        }
    except Exception:  # noqa: BLE001 - diagnostic only
        diff.setdefault("summary", "Change the Energy dashboard configuration")
    return diff


async def _tool_edit_energy_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: change ONE addressed part of the Energy dashboard (Confirm-gated).

    Never takes a preferences blob. See the module docstring for why that is the
    whole design rather than a stylistic choice. Validated against the CURRENT
    preferences pre-gate (rule 29, after the cap-deny check so a denied token
    learns nothing about its own payload), and re-validated at apply time so a
    change during the approval window is caught.
    """
    tool = "edit_energy_config"
    args, error = normalize_tool_args(tool, args)
    if error:
        return _tool_error(error), "invalid_request", tool
    if effective_cap(token, "cap_energy_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool
    refusal = _sentinel_refusal(args)
    if refusal is not None:
        return _tool_error(refusal), "invalid_request", tool
    try:
        before = await _read_prefs_raw(hass)
        after, summary = _apply_op(before, args, token, hass)
        _verify_structure(before, after, summary["op"])
    except _OpError as err:
        return _tool_error(str(err)), "invalid_request", tool
    except WsDispatchError:
        _LOGGER.exception("edit_energy_config: could not read the current preferences")
        return _tool_error("Could not read the Energy dashboard configuration."), "invalid_request", tool

    blocked = await _gate(
        "cap_energy_write", token, hass, data,
        tool_name=tool, args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_edit_energy_config(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_energy_config(args, token, hass, data)


async def _execute_edit_energy_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Side-effect path for edit_energy_config. Assumes the capability is satisfied."""
    tool = "edit_energy_config"
    try:
        before = await _read_prefs_raw(hass)
        after, summary = _apply_op(before, args, token, hass)
        _verify_structure(before, after, summary["op"])
    except _OpError as err:
        return _tool_error(str(err)), "invalid_request", tool
    except WsDispatchError:
        _LOGGER.exception("edit_energy_config: could not read the current preferences")
        return _tool_error("Could not read the Energy dashboard configuration."), "invalid_request", tool

    # Send ONLY the top-level keys this operation actually changed. HA's
    # EnergyManager.async_update copies through any key the update omits, so an
    # untouched key is never exposed to the full-replace semantics at all.
    payload = {key: after[key] for key in _PREFS_KEYS if key in after and after.get(key) != before.get(key)}
    if not payload:
        return _tool_error("That change would leave the Energy configuration unchanged."), "invalid_request", tool
    try:
        await async_ws_command(hass, "energy/save_prefs", payload)
    except WsDispatchError as err:
        # HA re-validates the payload against its own per-source-type schema, so
        # this is where a field combination Phoenix allowed but HA rejects lands.
        _LOGGER.exception("edit_energy_config: energy/save_prefs failed")
        return _tool_error(f"Home Assistant rejected the Energy configuration: {err}"), "invalid_request", tool

    await _record_version(
        data, token, resource_type="energy", resource_id="preferences",
        action="edit", before=before, after=after, alias="Energy dashboard",
        summary=_version_summary(
            "energy.sources",
            count=len(after.get("energy_sources") or []),
            devices=sum(len(after.get(key) or []) for key in _DEVICE_KEYS),
        ),
    )
    body = {"saved": True, **{k: v for k, v in summary.items()}, **_scoped_body(after, True, token, hass)}
    return _tool_success(json.dumps(body, default=str)), "allowed", "energy:preferences"


async def async_restore_energy_prefs(
    snapshot: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Restore a whole Energy preferences snapshot (admin version rollback only).

    The ONE legitimate wholesale write: reproducing the operator's chosen snapshot
    byte for byte IS the operation, so the addressed-write rule the rest of this
    module enforces does not apply, exactly as rule 31 exempts a YAML restore from
    its removal check. Reached only from async_restore_version, never from a tool.
    """
    tool = "edit_energy_config"
    if not isinstance(snapshot, dict):
        return _tool_error("This version is not a restorable Energy configuration."), "invalid_request", tool
    payload = {key: snapshot[key] for key in _PREFS_KEYS if isinstance(snapshot.get(key), list)}
    if not payload:
        return _tool_error("This version holds no Energy configuration to restore."), "invalid_request", tool
    try:
        before = await _read_prefs_raw(hass)
        await async_ws_command(hass, "energy/save_prefs", payload)
    except WsDispatchError as err:
        _LOGGER.exception("restore energy preferences failed")
        return _tool_error(f"Home Assistant rejected the Energy configuration: {err}"), "invalid_request", tool
    await _record_version(
        data, token, resource_type="energy", resource_id="preferences",
        action="edit", before=before, after=snapshot, alias="Energy dashboard",
        summary=_version_summary(
            "energy.sources",
            count=len(snapshot.get("energy_sources") or []),
            devices=sum(len(snapshot.get(key) or []) for key in _DEVICE_KEYS),
        ),
    )
    return (
        _tool_success(json.dumps({"saved": True, **_scoped_body(snapshot, True, token, hass)}, default=str)),
        "allowed",
        "energy:preferences",
    )
