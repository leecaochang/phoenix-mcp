"""Hand-rolled profile validation, kept in agreement with schemas/mesa_profile.schema.json.

The JSON Schema file is the canonical machine-readable artifact for third parties;
this module is the zero-dependency implementation mesa-core uses internally. The
test suite asserts both reject the same documents (tests/test_validation_schema_agreement.py).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from custom_components.phoenix_mcp.mesa_core import vocabulary
from custom_components.phoenix_mcp.mesa_core.exceptions import MesaValidationError

VALID_CONTROL_MODES = {"autonomous", "confirm", "read_only", "prohibited"}
VALID_TRIGGERS = {"likely", "none", "unknown", "deployment_defined"}
VALID_PRIVACY_LEVELS = {"public", "normal", "sensitive", "restricted"}
VALID_ORIGINS = {"developer", "user", "hybrid", "inferred_ai", "unknown"}
VALID_ENFORCEMENT_MODES = {"advisory", "enforced"}
VALID_REVERSIBILITY_COSTS = {"none", "trivial", "moderate", "high"}
VALID_SIDE_EFFECT_SCOPES = {
    "entity_only",
    "device_localized",
    "room_localized",
    "zone_wide",
    "deployment_wide",
}
VALID_STATE_VOLATILITY = {"static", "low", "medium", "high", "realtime"}
VALID_STATE_PERSISTENCE = {"permanent", "temporary", "session", "transient"}
VALID_DENY_RESPONSE_MODES = {"omit", "redact", "error"}
VALID_HOUSEHOLD_ROLES = {
    "primary_resident",
    "secondary_resident",
    "child",
    "regular_guest",
    "temporary_guest",
    "caregiver",
}
VALID_INHERITANCE_SCOPES = {"entity", "domain", "integration", "area", "device"}
PREDICATE_OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "contains"}
VALID_TEMPORAL_TYPES = {
    "time_range",
    "day_of_week",
    "calendar_entity",
    "solar_angle",
    "duration",
    "relative_to_event",
}
VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
VALID_SOLAR_EVENTS = {
    "sunrise",
    "sunset",
    "civil_twilight_start",
    "civil_twilight_end",
    "nautical_twilight_start",
    "nautical_twilight_end",
}
VALID_ACCESS_ROLE_KEYS = {"unrestricted_for", "restricted_for", "deny_for"}

# Fields each temporal condition type requires (Spec 6.5). A condition missing
# them cannot be evaluated, and an unevaluable constraint is applied as active,
# so a malformed one silently costs the author nothing at authoring time.
TEMPORAL_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "time_range": ("start_time", "end_time"),
    "day_of_week": ("days",),
    "calendar_entity": ("calendar_entity",),
    "solar_angle": ("solar_event",),
    "duration": ("duration_seconds",),
    "relative_to_event": ("anchor_event", "offset_seconds"),
}

# time_range bounds are 24-hour HH:MM (Spec 6.5). The evaluator parses them with
# time.fromisoformat, which also accepts HH:MM:SS ("12:00:00" reads as noon) and
# fails closed on garbage, so the author never hears about a wrong format unless
# it is rejected here.
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass
class ValidationReport:
    """Result of validating a profile document."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _semantic_profile_of(data: dict[str, Any]) -> dict[str, Any]:
    sp = data.get("semantic_profile", data)
    return sp if isinstance(sp, dict) else {}


def _privacy_of(data: dict[str, Any]) -> Any:
    # Canonical location is a sibling of semantic_profile; nested is accepted (Spec 7).
    if "privacy_classification" in data:
        return data["privacy_classification"]
    return _semantic_profile_of(data).get("privacy_classification")


def _check_enum(
    container: Any, key: str, valid: set[str], where: str, report: ValidationReport
) -> None:
    """Validate an enum-valued key. Absent is fine; present-but-wrong is not.

    Takes the container rather than the value so an explicit null is rejected
    rather than read as absent, and so an unhashable value is reported instead
    of raising out of the validator.
    """
    if not isinstance(container, dict) or key not in container:
        return
    value = container[key]
    if not isinstance(value, str) or value not in valid:
        report.errors.append(f"{where}: invalid value {value!r} (valid: {sorted(valid)})")


def _check_bool(container: dict[str, Any], key: str, where: str, report: ValidationReport) -> None:
    if key in container and not isinstance(container[key], bool):
        report.errors.append(f"{where}: must be a boolean (got {container[key]!r})")


def _check_number(
    container: dict[str, Any], key: str, where: str, report: ValidationReport
) -> None:
    if key not in container:
        return
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        report.errors.append(f"{where}: must be a number (got {value!r})")
    elif isinstance(value, float) and not math.isfinite(value):
        # NaN/Infinity are Python floats (json.loads accepts the non-standard
        # tokens), but a NaN safety bound disables its limit because every
        # comparison with NaN is false, and an infinite staleness window raises
        # at parse. Standard JSON forbids non-finite numbers; reject them here.
        # Only floats carry NaN/Infinity: a Python int is always finite, and
        # passing an arbitrarily large JSON integer to math.isfinite (which
        # converts to float) would raise OverflowError on hostile input, so int
        # is accepted as a finite number without conversion.
        report.errors.append(f"{where}: must be a finite number (got {value!r})")


def _check_string(
    container: dict[str, Any], key: str, where: str, report: ValidationReport
) -> None:
    if key in container and not isinstance(container[key], str):
        report.errors.append(f"{where}: must be a string (got {container[key]!r})")


def _check_string_array(
    container: dict[str, Any], key: str, where: str, report: ValidationReport
) -> None:
    if key not in container:
        return
    value = container[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        report.errors.append(f"{where}: must be an array of strings (got {value!r})")


def _check_predicate(pred: Any, where: str, report: ValidationReport) -> None:
    if not isinstance(pred, dict):
        report.errors.append(f"{where}: predicate must be an object")
        return
    if pred.get("type") == "ha_condition":
        if not isinstance(pred.get("condition"), dict):
            report.errors.append(f"{where}: ha_condition predicate requires a 'condition' object")
        return
    op = pred.get("operator")
    # isinstance first: an unhashable operator would raise out of the set test.
    if not isinstance(op, str) or op not in PREDICATE_OPERATORS:
        report.errors.append(
            f"{where}: unrecognised predicate operator {op!r} "
            f"(canonical tokens: {sorted(PREDICATE_OPERATORS)}; Spec 6.3)"
        )
    if "entity" not in pred:
        report.errors.append(f"{where}: predicate requires 'entity'")
    else:
        _check_string(pred, "entity", f"{where}.entity", report)
    if "value" not in pred:
        report.errors.append(f"{where}: predicate requires 'value'")


def _object_at(
    container: dict[str, Any], key: str, where: str, report: ValidationReport
) -> dict[str, Any] | None:
    """The object at ``key``, or None when absent.

    Reads presence rather than truthiness, so an explicit null is reported as
    the wrong type rather than read as absent and skipping every check below it.
    """
    if key not in container:
        return None
    value = container[key]
    if not isinstance(value, dict):
        report.errors.append(f"{where} must be an object (got {value!r})")
        return None
    return value


def _array_at(container: dict[str, Any], key: str) -> list[Any]:
    """The array at ``key``, or an empty one. Type errors are reported by the caller."""
    value = container.get(key)
    return value if isinstance(value, list) else []


def _check_metadata_origin(sp: dict[str, Any], report: ValidationReport) -> None:
    mo = _object_at(sp, "metadata_origin", "metadata_origin", report)
    if mo is None:
        return
    for key in ("generated_at", "last_updated"):
        _check_string(mo, key, f"metadata_origin.{key}", report)
    _check_number(mo, "staleness_window_days", "metadata_origin.staleness_window_days", report)
    source = mo.get("source")
    _check_enum(mo, "source", VALID_ORIGINS, "metadata_origin.source", report)
    _check_string_array(mo, "confirmed_fields", "metadata_origin.confirmed_fields", report)
    if source == "hybrid" and "confirmed_fields" not in mo:
        # REQUIRED for hybrid (Spec 5.3), the same way confidence and
        # generated_at are required for inferred_ai: a profile claiming partial
        # human confirmation must say what was confirmed.
        report.errors.append(
            "hybrid profile is malformed: missing 'confirmed_fields' (Spec 5.3)"
        )
    if source != "hybrid" and mo.get("confirmed_fields"):
        # Confirming a field promotes it to hybrid (Spec 5.4 Rule 6), so
        # confirmed_fields on any other origin is self-contradictory. Rejected
        # rather than ignored: honouring it would let an inferred profile
        # self-certify past Rules 8 and 9.
        report.errors.append(
            f"'confirmed_fields' is only meaningful for source 'hybrid', not {source!r}: "
            "human confirmation promotes the confirmed fields to hybrid (Spec 5.4 Rule 6)"
        )
    confidence = mo.get("confidence")
    if "confidence" in mo and (
        # isinstance(True, int) is True and float(True) is 1.0, so without the
        # bool guard `confidence: true` reads as full confidence and clears the
        # Rule 3 threshold.
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        # Compared without float() coercion: converting an arbitrarily large
        # JSON integer to float raises OverflowError, and Python compares
        # int/float exactly without conversion.
        or not 0.0 <= confidence <= 1.0
    ):
        report.errors.append(
            f"metadata_origin.confidence must be a number between 0.0 and 1.0 "
            f"(got {confidence!r})"
        )
    if source == "inferred_ai":
        # Inferred Rule 1 (Spec 5.4): missing either field makes the profile malformed.
        if confidence is None:
            report.errors.append("inferred_ai profile is malformed: missing 'confidence' (Rule 1)")
        if mo.get("generated_at") is None:
            report.errors.append(
                "inferred_ai profile is malformed: missing 'generated_at' (Rule 1)"
            )
    if source in ("developer", "user") and "generated_at" in mo:
        report.warnings.append(
            f"trust laundering suspected: source {source!r} but profile carries "
            "'generated_at', an AI-inference marker. AI-generated content must be "
            "marked 'hybrid' or 'inferred_ai' (Getting Started Guide)."
        )


def _check_boundaries(sp: dict[str, Any], report: ValidationReport) -> None:
    ob = _object_at(sp, "operational_boundaries", "operational_boundaries", report)
    if ob is None:
        return
    _check_enum(ob, "control_mode", VALID_CONTROL_MODES, "control_mode", report)
    _check_enum(ob, "triggers_automations", VALID_TRIGGERS, "triggers_automations", report)
    _check_enum(ob, "enforcement_mode", VALID_ENFORCEMENT_MODES, "enforcement_mode", report)
    _check_enum(ob, "reversibility_cost", VALID_REVERSIBILITY_COSTS, "reversibility_cost", report)
    _check_enum(ob, "side_effect_scope", VALID_SIDE_EFFECT_SCOPES, "side_effect_scope", report)
    _check_enum(ob, "state_volatility", VALID_STATE_VOLATILITY, "state_volatility", report)
    _check_enum(ob, "state_persistence", VALID_STATE_PERSISTENCE, "state_persistence", report)
    for key in (
        "reversible",
        "idempotent",
        "override_control_mode",
        "override_triggers_automations",
    ):
        _check_bool(ob, key, key, report)
    for key in ("reversibility_window_seconds", "expected_latency_ms"):
        _check_number(ob, key, key, report)
    for key in ("reversibility_note", "control_reason", "human_reason"):
        _check_string(ob, key, key, report)

    # Override flags: malformed overrides are ignored at resolution (Spec 5.7 Rule A, 6.1);
    # surfaced here as warnings so authors find out why.
    if ob.get("override_control_mode") is True and not ob.get("control_reason"):
        report.warnings.append(
            "override_control_mode: true without control_reason is malformed "
            "and will be ignored (Spec 5.7 Rule A)"
        )
    if ob.get("override_triggers_automations") is True and not ob.get("human_reason"):
        report.warnings.append(
            "override_triggers_automations: true without human_reason is malformed "
            "and will be ignored (Spec 6.1)"
        )

    # Presence, not truthiness: `or []` would read a malformed declared_limits
    # (an object, 0, "") as "no limits declared" and silently drop every safety
    # limit in it, and a truthy scalar would raise out of enumerate().
    for key in ("declared_limits", "temporal_constraints"):
        if key in ob and not isinstance(ob[key], list):
            report.errors.append(f"{key} must be an array (got {ob[key]!r})")

    for i, limit in enumerate(_array_at(ob, "declared_limits")):
        where = f"declared_limits[{i}]"
        if not isinstance(limit, dict):
            report.errors.append(f"{where}: must be an object")
            continue
        if not isinstance(limit.get("id"), str) or not limit["id"]:
            report.errors.append(f"{where}: 'id' is required and must be a string (Spec 6.4)")
        _check_string(limit, "human_reason", f"{where}.human_reason", report)
        _check_predicate(limit.get("predicate"), where, report)
        lim = limit.get("limit")
        if not isinstance(lim, dict) or "service" not in lim or "parameter" not in lim:
            report.errors.append(f"{where}: 'limit' requires 'service' and 'parameter'")
        else:
            _check_value_constraint(lim, f"{where}.limit", report)

    for i, tc in enumerate(_array_at(ob, "temporal_constraints")):
        _check_temporal_constraint(tc, f"temporal_constraints[{i}]", report)


def _check_value_constraint(spec: dict[str, Any], where: str, report: ValidationReport) -> None:
    """Validate the value-constraint fields shared by limits and temporal effects.

    Bounds must be numeric: a non-numeric bound is not comparable to the service
    parameter at enforcement time, and an incomparable bound cannot constrain
    anything (Spec 6.4).
    """
    _check_string(spec, "service", f"{where}.service", report)
    _check_string(spec, "parameter", f"{where}.parameter", report)
    for key in ("max_value", "min_value"):
        _check_number(spec, key, f"{where}.{key}", report)
    if "permitted_values" in spec and not isinstance(spec["permitted_values"], list):
        report.errors.append(f"{where}.permitted_values: must be an array")


def _check_temporal_condition(cond: Any, where: str, report: ValidationReport) -> None:
    if not isinstance(cond, dict):
        report.errors.append(f"{where}: 'condition' object is required (Spec 6.5)")
        return
    if "type" not in cond:
        report.errors.append(f"{where}.condition: 'type' is required (Spec 6.5)")
        return
    _check_enum(cond, "type", VALID_TEMPORAL_TYPES, f"{where}.condition.type", report)
    _check_bool(cond, "negate", f"{where}.condition.negate", report)
    cond_type = cond.get("type")
    if not isinstance(cond_type, str) or cond_type not in TEMPORAL_REQUIRED_FIELDS:
        return
    for required in TEMPORAL_REQUIRED_FIELDS[cond_type]:
        if required not in cond:
            report.errors.append(
                f"{where}.condition: {cond_type!r} requires {required!r} (Spec 6.5)"
            )
    if cond_type == "time_range":
        for key in ("start_time", "end_time"):
            _check_string(cond, key, f"{where}.condition.{key}", report)
            value = cond.get(key)
            if isinstance(value, str) and not _HHMM_RE.match(value):
                report.errors.append(
                    f"{where}.condition.{key}: must be 24-hour HH:MM (got {value!r})"
                )
    elif cond_type == "day_of_week":
        days = cond.get("days")
        if not isinstance(days, list) or not days:
            report.errors.append(f"{where}.condition.days: must be a non-empty array")
        else:
            invalid = [d for d in days if not isinstance(d, str) or d not in VALID_WEEKDAYS]
            if invalid:
                report.errors.append(
                    f"{where}.condition.days: invalid weekday(s) {invalid!r} "
                    f"(valid: {sorted(VALID_WEEKDAYS)})"
                )
    elif cond_type == "calendar_entity":
        _check_string(cond, "calendar_entity", f"{where}.condition.calendar_entity", report)
    elif cond_type == "solar_angle":
        _check_enum(
            cond, "solar_event", VALID_SOLAR_EVENTS, f"{where}.condition.solar_event", report
        )
        _check_number(
            cond, "solar_offset_minutes", f"{where}.condition.solar_offset_minutes", report
        )
    elif cond_type == "duration":
        _check_number(cond, "duration_seconds", f"{where}.condition.duration_seconds", report)
    elif cond_type == "relative_to_event":
        _check_string(cond, "anchor_event", f"{where}.condition.anchor_event", report)
        _check_number(cond, "offset_seconds", f"{where}.condition.offset_seconds", report)


def _check_temporal_constraint(tc: Any, where: str, report: ValidationReport) -> None:
    if not isinstance(tc, dict):
        report.errors.append(f"{where}: must be an object")
        return
    if not isinstance(tc.get("id"), str) or not tc["id"]:
        report.errors.append(f"{where}: 'id' is required and must be a string (Spec 6.5)")
    _check_string(tc, "human_reason", f"{where}.human_reason", report)
    _check_temporal_condition(tc.get("condition"), where, report)

    effect = tc.get("effect")
    if not isinstance(effect, dict) or not effect:
        report.errors.append(f"{where}: 'effect' must be a non-empty object (Spec 6.5)")
        return
    _check_enum(effect, "control_mode", VALID_CONTROL_MODES, f"{where}.effect.control_mode", report)
    _check_value_constraint(effect, f"{where}.effect", report)
    # An effect's value constraint needs a service and parameter to apply to,
    # or it silently constrains nothing (Spec 6.5).
    bounds = [key for key in ("max_value", "min_value", "permitted_values") if key in effect]
    if bounds and "service" not in effect:
        report.errors.append(
            f"{where}.effect: {bounds[0]!r} requires 'service' (Spec 6.5)"
        )
    if "service" in effect and "parameter" not in effect:
        report.errors.append(f"{where}.effect: 'service' requires 'parameter' (Spec 6.5)")
    if "control_mode" not in effect and not bounds:
        report.errors.append(
            f"{where}.effect: must declare a 'control_mode' or a value constraint; "
            "an effect that declares neither is silently ignored (Spec 6.5)"
        )


def _check_person_traits(sp: dict[str, Any], report: ValidationReport) -> None:
    pt = _object_at(sp, "person_traits", "person_traits", report)
    if pt is None:
        return
    _check_enum(
        pt, "household_role", VALID_HOUSEHOLD_ROLES, "person_traits.household_role", report
    )
    if "is_minor" in pt and not isinstance(pt["is_minor"], bool):
        # A non-boolean is_minor would silently fail the mandatory restricted
        # check (Spec 17 Rule 2): reject loudly rather than fail open.
        report.errors.append("person_traits.is_minor must be a boolean")
    for key in ("display_name", "presence_entity"):
        _check_string(pt, key, f"person_traits.{key}", report)
    for key in ("associated_zones", "associated_automations"):
        _check_string_array(pt, key, f"person_traits.{key}", report)


def _check_access_roles(pc: dict[str, Any], report: ValidationReport) -> None:
    """Validate access_roles (Spec 7.2).

    Each entry must be an array of role names. A bare string would be read as a
    set of its characters at enforcement, so ``deny_for: "guest"`` would deny
    nobody named guest while denying anyone named "g": rejected, not coerced.
    """
    roles = _object_at(pc, "access_roles", "privacy_classification.access_roles", report)
    if roles is None:
        return
    for key, value in roles.items():
        where = f"privacy_classification.access_roles.{key}"
        if key not in VALID_ACCESS_ROLE_KEYS:
            report.errors.append(
                f"{where}: unknown key (valid: {sorted(VALID_ACCESS_ROLE_KEYS)}; Spec 7.2)"
            )
            continue
        if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
            report.errors.append(f"{where}: must be an array of role names (got {value!r})")


def validate_document(data: dict[str, Any], entity_id: str = "") -> ValidationReport:
    """Validate a profile document (root form or bare semantic_profile contents)."""
    report = ValidationReport()
    if not isinstance(data, dict):
        report.errors.append("profile document must be an object")
        return report
    if "semantic_profile" in data and not isinstance(data["semantic_profile"], dict):
        # Nothing below can be checked, and coercing this to an empty object
        # would report the document clean and then fail to parse it.
        report.errors.append(
            f"semantic_profile must be an object (got {data['semantic_profile']!r})"
        )
        return report

    sp = _semantic_profile_of(data)
    _check_metadata_origin(sp, report)
    _check_boundaries(sp, report)
    _check_person_traits(sp, report)

    if "semantic_tags" in sp:
        tags = sp["semantic_tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            report.errors.append(f"semantic_tags must be an array of strings (got {tags!r})")
        else:
            report.errors.extend(vocabulary.check_tags(tags))
    for key in ("schema_version", "profile_version", "last_updated"):
        _check_string(sp, key, key, report)

    _check_enum(sp, "inheritance_scope", VALID_INHERITANCE_SCOPES, "inheritance_scope", report)

    # capability_semantics is an integration-profile section (Spec 8.2). Only its
    # control_mode member participates in resolution (the Spec 4 capability hint),
    # so only that member is typed; the rest stays unmodelled (Spec 23).
    _object_at(sp, "capability_semantics", "capability_semantics", report)
    cs = sp.get("capability_semantics")
    if isinstance(cs, dict):
        _check_enum(
            cs, "control_mode", VALID_CONTROL_MODES, "capability_semantics.control_mode", report
        )

    # profile_valid_for is a semantic_profile field; diagnostic_profile is a root
    # sibling. Both are opaque to this version (Spec 23) but must be objects: a
    # scalar in either slot is malformed, not unmodelled content the schema allows.
    _object_at(sp, "profile_valid_for", "profile_valid_for", report)
    _object_at(data, "diagnostic_profile", "diagnostic_profile", report)

    # profile_valid_for's four known members are typed (Spec 5.5); validate them
    # while leaving unknown members untouched for forward compatibility (Spec 23).
    pvf = sp.get("profile_valid_for")
    if isinstance(pvf, dict):
        _check_string(pvf, "integration_version", "profile_valid_for.integration_version", report)
        _check_string(pvf, "ha_version", "profile_valid_for.ha_version", report)
        _check_number(pvf, "review_after_days", "profile_valid_for.review_after_days", report)
        _check_string_array(
            pvf, "invalidated_by_entities", "profile_valid_for.invalidated_by_entities", report
        )

    pc = _privacy_of(data)
    if pc is not None or "privacy_classification" in data:
        if not isinstance(pc, dict):
            report.errors.append(f"privacy_classification must be an object (got {pc!r})")
        else:
            if "level" not in pc:
                report.errors.append("privacy_classification requires 'level' (Spec 7.1)")
            _check_enum(pc, "level", VALID_PRIVACY_LEVELS, "privacy_classification.level", report)
            _check_enum(
                pc, "deny_response_mode", VALID_DENY_RESPONSE_MODES, "deny_response_mode", report
            )
            for key in (
                "contains_presence_data",
                "contains_audio_capture",
                "contains_visual_capture",
                "contains_biometric_data",
                "contains_behavioural_data",
                "data_retention_local",
                "access_logging_recommended",
            ):
                _check_bool(pc, key, f"privacy_classification.{key}", report)
            _check_string(pc, "privacy_note", "privacy_classification.privacy_note", report)
            _check_access_roles(pc, report)

    return report


def validate_or_raise(data: dict[str, Any], entity_id: str = "") -> ValidationReport:
    """Validate and raise MesaValidationError when the document is malformed."""
    report = validate_document(data, entity_id)
    if not report.ok:
        raise MesaValidationError(
            f"profile for {entity_id or '<unkeyed>'} is malformed: {'; '.join(report.errors)}",
            errors=report.errors,
        )
    return report
