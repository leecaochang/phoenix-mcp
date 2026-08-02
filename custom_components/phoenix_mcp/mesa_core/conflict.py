"""Global profile conflict resolution: Rules A-E (Spec 5.7).

Operates on declared fields only (``SemanticProfile.declared``): absence is
inherited, never defaulted here (Rule E). Defaults for undeclared kernel fields
are applied by the InheritanceResolver from deployment defaults or the built-in
baseline.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any

from custom_components.phoenix_mcp.mesa_core.profile import (
    CONTROL_MODE_RANK,
    HELPER_DOMAINS,
    ORIGIN_AUTHORITY,
    PRIVACY_RANK,
    ControlMode,
    MetadataOrigin,
    PrivacyLevel,
    SemanticProfile,
    TriggersAutomations,
    iter_unmodelled,
    path_segment,
)

SCOPE_RANK = {"entity": 5, "device": 4, "area": 3, "integration": 2, "domain": 1}

# Rule D fields on operational_boundaries (everything not covered by Rules A/B).
_OB_RULE_D_FIELDS = (
    "reversible",
    "reversibility_cost",
    "reversibility_note",
    "reversibility_window_seconds",
    "idempotent",
    "state_persistence",
    "expected_latency_ms",
    "side_effect_scope",
    "state_volatility",
    "enforcement_mode",
    "control_reason",
    "human_reason",
)

# Array-valued Rule D fields are unioned across layers, not replaced (see
# ConflictResolver.resolve_limit_union), so they are handled separately.
_OB_UNION_FIELDS = (
    "declared_limits",
    "temporal_constraints",
)

_PRIVACY_RULE_D_FIELDS = (
    "contains_presence_data",
    "contains_audio_capture",
    "contains_visual_capture",
    "contains_biometric_data",
    "contains_behavioural_data",
    "data_retention_local",
    "access_logging_recommended",
    "access_roles",
    "deny_response_mode",
    "privacy_note",
)

_PERSON_RULE_D_FIELDS = (
    "household_role",
    "display_name",
    "is_minor",
    "associated_zones",
    "associated_automations",
    "presence_entity",
)

# Fields that gate an access decision, so an unconfirmed inferred value must be
# excluded entirely rather than fall back to (Spec 5.4 Rule 3, Spec 17):
# access_roles grants/denies/relaxes by role, and is_minor forces restricted.
# The remaining privacy and person fields are informational (they do not gate
# access), and an inferred value there only ever adds caution, so they keep the
# normal Rule D trusted-tier-then-fallback resolution.
_ACCESS_DECISION_FIELDS = frozenset(
    {"privacy_classification.access_roles", "person_traits.is_minor"}
)


@dataclass
class Layer:
    """One inheritance level's profile.

    ``level`` is 'entity', 'device', 'area', 'integration', or 'domain'.
    """

    level: str
    profile: SemanticProfile


@dataclass
class FieldExplanation:
    """One entry of the mesa_explain_profile output (Spec 9.5)."""

    field_path: str
    effective_value: Any
    provided_by_level: str
    provided_by_origin: str
    conflict: bool = False
    conflict_resolution: str | None = None
    competing_values: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field_path": self.field_path,
            "effective_value": self.effective_value,
            "provided_by_level": self.provided_by_level,
            "provided_by_origin": self.provided_by_origin,
            "conflict": self.conflict,
        }
        if self.conflict_resolution is not None:
            out["conflict_resolution"] = self.conflict_resolution
        if self.competing_values is not None:
            out["competing_values"] = self.competing_values
        return out


@dataclass
class Resolution:
    """Outcome of merging the declared layers (before default filling)."""

    explanations: list[FieldExplanation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def conflicts_detected(self) -> bool:
        return any(e.conflict for e in self.explanations)


@dataclass
class _Candidate:
    layer: Layer
    value: Any
    capability_hint: bool = False

    @property
    def scope_rank(self) -> int:
        # The Spec 4 capability hint ranks below every operational_boundaries
        # declaration regardless of its layer's scope.
        if self.capability_hint:
            return 0
        # An unknown level ranks 0, below domain: it can never displace a known
        # level, so a missed enumeration site fails closed rather than winning.
        return SCOPE_RANK.get(self.layer.level, 0)

    @property
    def origin(self) -> MetadataOrigin:
        return self.layer.profile.metadata.source

    @property
    def origin_authority(self) -> int:
        return ORIGIN_AUTHORITY[self.origin]

    def describe(self) -> dict[str, Any]:
        value = self.value
        if hasattr(value, "value"):
            value = value.value
        # Copied so the explanation owns its payload rather than aliasing the
        # input layer's mutable value.
        return {
            "level": self.layer.level,
            "origin": self.origin.value,
            "value": copy.deepcopy(value),
        }


def _normalize_numbers(value: Any) -> Any:
    """Collapse equivalent numeric encodings before canonical serialisation.

    JSON has one number type, so ``1`` and ``1.0`` (or ``0`` and ``-0.0``) are
    the same value and must not read as differing declarations. A finite,
    integral float becomes the exact int; converting the other way (int to
    float) would falsely merge distinct integers beyond 2**53. Booleans are a
    distinct type and are never numbers.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_numbers(sub) for key, sub in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(sub) for sub in value]
    return value


def _canonical(value: Any) -> str:
    """Value-semantic serialisation, for detecting whether entries differ.

    Order-insensitive for object keys, and numerically normalised so equal
    JSON numbers with different encodings compare equal.
    """
    return json.dumps(_normalize_numbers(value), sort_keys=True, default=str)


def _comparison_view(path: str, value: Any) -> Any:
    """Field-aware view of a value for the distinct-declarations check.

    The role arrays inside ``access_roles`` are consumed as sets by the
    privacy enforcer, so their order and duplicates carry no semantics:
    reordered or duplicated role lists are the same declaration, not a
    conflict. Arrays elsewhere stay order-sensitive; only this field is
    normalised, and only for comparison, never for the effective value.
    """
    if path == "privacy_classification.access_roles" and isinstance(value, dict):
        return {
            key: sorted(set(sub))
            if isinstance(sub, list) and all(isinstance(item, str) for item in sub)
            else sub
            for key, sub in value.items()
        }
    return value


def _is_confirmed(profile: SemanticProfile, path: str) -> bool:
    """Whether a human has confirmed this field path.

    Only ``hybrid`` profiles carry authoritative confirmations: confirming a
    field of an inferred_ai profile promotes that field to hybrid (Spec 5.4
    Rule 6), so ``confirmed_fields`` on any other origin is self-contradictory
    and is never honoured. Without this check an inferred_ai profile could
    self-certify and escape Rules 8 and 9.

    A confirmed path covers the whole value declared at it: confirming
    ``x_vendor.a`` confirms everything under ``x_vendor.a``, so a descendant
    path produced by recursive composition is confirmed by its ancestor.

    Dot-notation is the path grammar, so a property name containing a dot (or
    a backslash) is not path-addressable: a ``confirmed_fields`` entry always
    parses as nested segments and never names such a key, and a rendered path
    containing an escaped segment (see ``path_segment``) matches only through
    a confirmed clean ancestor, never exactly. Without this, a confirmation
    intended for a nested path would also trust a literal dotted key that
    happens to render identically.
    """
    if profile.metadata.source != MetadataOrigin.HYBRID:
        return False
    for confirmed in profile.metadata.confirmed_fields:
        if "\\" in confirmed:
            # Not a parseable dot-notation path; it can name no field.
            continue
        if path.startswith(f"{confirmed}."):
            return True
        if path == confirmed and "\\" not in path:
            return True
    return False


def _is_trusted_for(profile: SemanticProfile, path: str) -> bool:
    """Field-level trust tier for Rule D (Spec 5.4 Rule 6).

    developer and user profiles are trusted for every field they declare. A
    hybrid profile is trusted only for the fields a human actually confirmed:
    its unconfirmed fields remain inferred and stay in the lower tier, so they
    cannot displace a trusted declaration.
    """
    source = profile.metadata.source
    if source in (MetadataOrigin.DEVELOPER, MetadataOrigin.USER):
        return True
    if source == MetadataOrigin.HYBRID:
        return _is_confirmed(profile, path)
    return False


def _shape_trusted(layer: Layer, path: str, value: Any) -> bool:
    """Trust used when layers disagree on a node's SHAPE (object vs atomic).

    Same as _is_trusted_for, except a hybrid object also counts as trusted when
    a confirmed path lies under this node AND resolves to a field the object
    actually contains: its children resolve individually afterwards, so a
    confirmed child must be able to keep the node an object. A confirmation
    naming a nonexistent or stale descendant confirms nothing, so it must not
    promote the object's unconfirmed fields past a trusted declaration. An
    atomic hybrid value still needs the node path itself confirmed.
    """
    if _is_trusted_for(layer.profile, path):
        return True
    if (
        isinstance(value, dict)
        and value
        and layer.profile.metadata.source == MetadataOrigin.HYBRID
    ):
        prefix = f"{path}."
        for confirmed in layer.profile.metadata.confirmed_fields:
            if "\\" in confirmed:
                # Not a parseable dot-notation path (see _is_confirmed).
                continue
            if not confirmed.startswith(prefix):
                continue
            node = value
            for segment in confirmed[len(prefix):].split("."):
                if not (isinstance(node, dict) and segment in node):
                    break
                node = node[segment]
            else:
                return True
    return False


def _coerced_control_mode(layer: Layer) -> tuple[ControlMode, str | None]:
    """Apply the untrusted-origin coercion of Spec 5.4 Rules 3/8.

    Profiles of inferred_ai/unknown origin, and unconfirmed hybrid fields, may
    not assert autonomous: it is read as confirm.
    """
    profile = layer.profile
    mode = profile.operational_boundaries.control_mode
    human_authored = profile.metadata.source in (
        MetadataOrigin.DEVELOPER,
        MetadataOrigin.USER,
    )
    if (
        not human_authored
        and mode == ControlMode.AUTONOMOUS
        and not _is_confirmed(profile, "operational_boundaries.control_mode")
    ):
        return ControlMode.CONFIRM, (
            f"{profile.entity_id}: unconfirmed {profile.metadata.source.value} profile asserted "
            "control_mode: autonomous; read as confirm (Spec 5.4 Rule 8)"
        )
    return mode, None


def _capability_hint_mode(layer: Layer) -> tuple[ControlMode | None, str | None]:
    """Read the Spec 4 capability hint from an integration profile's raw form.

    ``capability_semantics.control_mode`` contributes to Rule A only when the
    integration profile declares no ``operational_boundaries.control_mode``. A
    malformed hint value is ignored with a warning: a hint asserts nothing, so
    dropping garbage is the fail-closed reading (stored documents cannot carry
    one past validation; programmatically built layers can). Rule 8 coercion
    applies exactly as it would to a declared control_mode.
    """
    sp = layer.profile.raw.get("semantic_profile")
    container = sp if isinstance(sp, dict) else layer.profile.raw
    cs = container.get("capability_semantics")
    if not isinstance(cs, dict) or "control_mode" not in cs:
        return None, None
    value = cs["control_mode"]
    try:
        mode = ControlMode(value)
    except (ValueError, TypeError):
        return None, (
            f"{layer.profile.entity_id}: capability_semantics.control_mode {value!r} "
            "is not a valid control_mode; capability hint ignored (Spec 4)"
        )
    human_authored = layer.profile.metadata.source in (
        MetadataOrigin.DEVELOPER,
        MetadataOrigin.USER,
    )
    if (
        not human_authored
        and mode == ControlMode.AUTONOMOUS
        and not _is_confirmed(layer.profile, "capability_semantics.control_mode")
    ):
        return ControlMode.CONFIRM, (
            f"{layer.profile.entity_id}: unconfirmed {layer.profile.metadata.source.value} "
            "profile asserted capability_semantics.control_mode: autonomous; read as "
            "confirm (Spec 5.4 Rule 8)"
        )
    return mode, None


def _coerced_triggers(layer: Layer, domain: str) -> tuple[TriggersAutomations, str | None]:
    """Apply the helper-domain coercion of Spec 5.4 Rule 9."""
    profile = layer.profile
    value = profile.operational_boundaries.triggers_automations
    human_authored = profile.metadata.source in (MetadataOrigin.DEVELOPER, MetadataOrigin.USER)
    if (
        not human_authored
        and domain in HELPER_DOMAINS
        and value == TriggersAutomations.NONE
        and not _is_confirmed(profile, "operational_boundaries.triggers_automations")
    ):
        return TriggersAutomations.LIKELY, (
            f"{profile.entity_id}: unconfirmed {profile.metadata.source.value} helper profile "
            "asserted triggers_automations: none; read as likely (Spec 5.4 Rule 9)"
        )
    return value, None


class ConflictResolver:
    """Implements Rules A-E over an ordered set of inheritance layers."""

    # -- Rule A: control_mode -------------------------------------------------

    def resolve_control_mode(
        self, layers: list[Layer], resolution: Resolution
    ) -> ControlMode | None:
        candidates: list[_Candidate] = []
        for layer in layers:
            if layer.profile.declared("operational_boundaries.control_mode"):
                mode, warning = _coerced_control_mode(layer)
                if warning:
                    resolution.warnings.append(warning)
                candidates.append(_Candidate(layer, mode))
                continue
            # Spec 4 capability hint: an integration-scoped profile declaring
            # capability_semantics.control_mode but no operational_boundaries
            # control_mode contributes the hint as its Rule A declaration. Other
            # scopes never consult capability_semantics.
            if layer.level == "integration":
                hint, warning = _capability_hint_mode(layer)
                if warning:
                    resolution.warnings.append(warning)
                if hint is not None:
                    candidates.append(_Candidate(layer, hint, capability_hint=True))
        if not candidates:
            return None

        # Rule A exception: the operator loosening override. Valid only at entity
        # scope, user origin, control_mode autonomous, with control_reason.
        override: _Candidate | None = None
        for cand in candidates:
            ob = cand.layer.profile.operational_boundaries
            if not ob.override_control_mode:
                continue
            valid = (
                cand.layer.level == "entity"
                and cand.origin == MetadataOrigin.USER
                and cand.value == ControlMode.AUTONOMOUS
                and bool(ob.control_reason)
            )
            if valid:
                override = cand
            else:
                resolution.warnings.append(
                    f"{cand.layer.profile.entity_id}: override_control_mode is malformed "
                    "(requires entity scope, user origin, control_mode: autonomous, and "
                    "control_reason); ignored (Spec 5.7 Rule A)"
                )

        hard = [c for c in candidates if CONTROL_MODE_RANK[c.value] >= 2]
        if hard:
            # prohibited / read_only can never be loosened; read_only wins the tie.
            winner = next(
                (c for c in hard if c.value == ControlMode.READ_ONLY),
                max(hard, key=lambda c: (c.scope_rank, c.origin_authority)),
            )
            effective: ControlMode = winner.value
            reason = "Rule A: prohibited/read_only is never loosened"
            if override is not None:
                resolution.warnings.append(
                    f"{override.layer.profile.entity_id}: loosening override cannot loosen "
                    f"{effective.value}; ignored (Spec 5.7 Rule A)"
                )
        elif override is not None:
            winner = override
            effective = ControlMode.AUTONOMOUS
            reason = "Rule A exception: operator loosening override applied"
        else:
            winner = max(
                candidates, key=lambda c: (CONTROL_MODE_RANK[c.value], c.scope_rank)
            )
            effective = winner.value
            reason = f"Rule A: most restrictive value wins ({effective.value})"

        distinct = {c.value for c in candidates}
        if winner.capability_hint:
            note = (
                "value from capability_semantics.control_mode on the integration "
                "profile (capability hint, Spec 4)"
            )
            if len(distinct) > 1:
                reason = f"{reason}; {note}"
            else:
                # Spec 9.5 attaches conflict_resolution only to genuine
                # conflicts, so a hint winning uncontested is surfaced as a
                # warning instead of silently reading as an operational_
                # boundaries declaration.
                resolution.warnings.append(
                    f"{winner.layer.profile.entity_id}: control_mode {note}"
                )
        resolution.explanations.append(
            FieldExplanation(
                field_path="operational_boundaries.control_mode",
                effective_value=effective.value,
                provided_by_level=winner.layer.level,
                provided_by_origin=winner.origin.value,
                conflict=len(distinct) > 1,
                conflict_resolution=reason if len(distinct) > 1 else None,
                competing_values=(
                    [c.describe() for c in candidates] if len(distinct) > 1 else None
                ),
            )
        )
        return effective

    # -- Rule B: triggers_automations ------------------------------------------

    def resolve_triggers(
        self, layers: list[Layer], domain: str, resolution: Resolution
    ) -> TriggersAutomations | None:
        candidates: list[_Candidate] = []
        for layer in layers:
            if not layer.profile.declared("operational_boundaries.triggers_automations"):
                continue
            value, warning = _coerced_triggers(layer, domain)
            if warning:
                resolution.warnings.append(warning)
            candidates.append(_Candidate(layer, value))
        if not candidates:
            return None

        # Entity-level override of a sticky likely (Spec 6.1: requires human_reason;
        # value must be none or deployment_defined).
        override: _Candidate | None = None
        for cand in candidates:
            ob = cand.layer.profile.operational_boundaries
            if not ob.override_triggers_automations:
                continue
            valid = (
                cand.layer.level == "entity"
                and cand.value
                in (TriggersAutomations.NONE, TriggersAutomations.DEPLOYMENT_DEFINED)
                and bool(ob.human_reason)
            )
            if valid:
                override = cand
            else:
                resolution.warnings.append(
                    f"{cand.layer.profile.entity_id}: override_triggers_automations is "
                    "malformed (requires entity scope, human_reason, and a value of none "
                    "or deployment_defined); ignored (Spec 6.1)"
                )

        likely = [c for c in candidates if c.value == TriggersAutomations.LIKELY]
        if override is not None:
            winner = override
            effective: TriggersAutomations = override.value
            reason = "Rule B: entity-level override with human_reason"
        elif likely:
            winner = max(likely, key=lambda c: (c.scope_rank, c.origin_authority))
            effective = TriggersAutomations.LIKELY
            reason = "Rule B: likely is sticky upward"
        else:
            winner = max(candidates, key=lambda c: (c.scope_rank, c.origin_authority))
            effective = winner.value
            reason = "Rule B: most specific declaration wins (no likely present)"

        distinct = {c.value for c in candidates}
        resolution.explanations.append(
            FieldExplanation(
                field_path="operational_boundaries.triggers_automations",
                effective_value=effective.value,
                provided_by_level=winner.layer.level,
                provided_by_origin=winner.origin.value,
                conflict=len(distinct) > 1,
                conflict_resolution=reason if len(distinct) > 1 else None,
                competing_values=(
                    [c.describe() for c in candidates] if len(distinct) > 1 else None
                ),
            )
        )
        return effective

    # -- Rule C: privacy level ---------------------------------------------------

    def resolve_privacy_level(
        self, layers: list[Layer], resolution: Resolution
    ) -> PrivacyLevel | None:
        candidates = [
            _Candidate(layer, layer.profile.privacy_classification.level)
            for layer in layers
            if layer.profile.declared("privacy_classification.level")
        ]
        if not candidates:
            return None
        winner = max(
            candidates, key=lambda c: (PRIVACY_RANK[c.value], c.scope_rank, c.origin_authority)
        )
        effective_level: PrivacyLevel = winner.value
        distinct = {c.value for c in candidates}
        resolution.explanations.append(
            FieldExplanation(
                field_path="privacy_classification.level",
                effective_value=winner.value.value,
                provided_by_level=winner.layer.level,
                provided_by_origin=winner.origin.value,
                conflict=len(distinct) > 1,
                conflict_resolution=(
                    "Rule C: most restrictive privacy level wins" if len(distinct) > 1 else None
                ),
                competing_values=(
                    [c.describe() for c in candidates] if len(distinct) > 1 else None
                ),
            )
        )
        return effective_level

    # -- Rule D: everything else ---------------------------------------------------

    def resolve_rule_d(
        self,
        layers: list[Layer],
        path: str,
        getter_attr: str,
        container: str,
        resolution: Resolution,
        *,
        trusted_only: bool = False,
    ) -> tuple[bool, Any]:
        """Resolve one Rule D field. Returns (was_declared, effective_value).

        ``trusted_only`` excludes untrusted candidates entirely rather than
        falling back to them when no trusted layer declares the field. It is set
        for the fields that drive access decisions (privacy access_roles, person
        is_minor): Spec 5.4 Rule 3 forbids using an unconfirmed inferred privacy
        value for an access decision at all, so an untrusted-only declaration
        must leave the field at its safe default, not select the untrusted value.
        """
        candidates: list[_Candidate] = []
        for layer in layers:
            if not layer.profile.declared(path):
                continue
            obj = getattr(layer.profile, container)
            candidates.append(_Candidate(layer, getattr(obj, getter_attr)))
        if not candidates:
            return False, None

        trusted = [c for c in candidates if _is_trusted_for(c.layer.profile, path)]
        if trusted_only and not trusted:
            # Only untrusted layers declare an access-decision field: it cannot be
            # used (Rule 3), so it resolves as undeclared and keeps its default.
            resolution.warnings.append(
                f"{path}: declared only by an unconfirmed inferred profile; ignored for "
                "access decisions (Spec 5.4 Rule 3)"
            )
            return False, None
        pool = trusted if trusted else candidates
        winner = max(pool, key=lambda c: (c.scope_rank, c.origin_authority))

        def _comparable(v: Any) -> Any:
            return v.value if hasattr(v, "value") else v

        # _canonical, not repr: repr(dict) preserves insertion order, so two
        # semantically identical objects would read as differing values and
        # report a false conflict (Spec 9.5 erratum: conflict = differing).
        # _comparison_view additionally normalises set-valued role arrays.
        distinct_values = {
            _canonical(_comparison_view(path, _comparable(c.value))) for c in candidates
        }
        conflict = len(distinct_values) > 1
        if conflict:
            if trusted and len(trusted) < len(candidates):
                reason = "Rule D: lower-tier declaration never overrides trusted tier"
            elif len({c.scope_rank for c in pool}) > 1:
                reason = "Rule D: most specific scope wins among trusted origins"
            else:
                reason = "Rule D: origin authority tiebreak at equal scope"
        else:
            reason = None
        resolution.explanations.append(
            FieldExplanation(
                field_path=path,
                effective_value=copy.deepcopy(_comparable(winner.value)),
                provided_by_level=winner.layer.level,
                provided_by_origin=winner.origin.value,
                conflict=conflict,
                conflict_resolution=reason,
                competing_values=[c.describe() for c in candidates] if conflict else None,
            )
        )
        # Deep-copied so the caller's effective profile owns its value: the
        # winner is otherwise a live reference into the input layer, and a
        # read-modify-write of the merged profile would mutate the source.
        return True, copy.deepcopy(winner.value)

    # -- Rule D (array union): declared_limits / temporal_constraints -----------

    def resolve_limit_union(
        self,
        layers: list[Layer],
        path: str,
        attr: str,
        resolution: Resolution,
    ) -> tuple[bool, list[dict[str, Any]] | None]:
        """Union an array-valued safety field across layers instead of replacing it.

        ``declared_limits`` and ``temporal_constraints`` are additive: each entry
        is an independent constraint, and both consumers (the enforcer and the
        temporal evaluator) apply every entry, so a union is automatically
        tightest-wins at evaluation time. Entries are keyed by ``id``; when the
        same ``id`` is declared at more than one layer the winner is chosen the
        same way scalar Rule D chooses, trusted tier first, then most specific
        scope, then origin authority, so a lower-tier profile can never displace
        a trusted entry and an operator override of one entry stays deliberate.
        Distinct ids simply compose and are not reported as a conflict.
        """
        contributing: list[Layer] = []
        keyed: dict[Any, list[_Candidate]] = {}
        for layer in layers:
            if not layer.profile.declared(path):
                continue
            contributing.append(layer)
            entries = getattr(layer.profile.operational_boundaries, attr)
            for entry in entries:
                key = entry.get("id") if isinstance(entry, dict) else None
                if not key:
                    # id is REQUIRED (Spec 6.4); an id-less entry never dedups.
                    key = object()
                keyed.setdefault(key, []).append(_Candidate(layer, entry))
        if not contributing:
            return False, None

        # Winning entries are deep-copied so the effective profile owns its
        # limits: editing a merged entry must not write through to the layer.
        merged: list[dict[str, Any]] = []
        collision = False
        for cands in keyed.values():
            if len(cands) == 1:
                merged.append(copy.deepcopy(cands[0].value))
                continue
            trusted = [c for c in cands if _is_trusted_for(c.layer.profile, path)]
            pool = trusted if trusted else cands
            winner = max(pool, key=lambda c: (c.scope_rank, c.origin_authority))
            merged.append(copy.deepcopy(winner.value))
            if len({_canonical(c.value) for c in cands}) > 1:
                collision = True

        most_specific = max(contributing, key=lambda layer: SCOPE_RANK.get(layer.level, 0))
        resolution.explanations.append(
            FieldExplanation(
                field_path=path,
                effective_value=copy.deepcopy(merged),
                provided_by_level=most_specific.level,
                provided_by_origin=most_specific.profile.metadata.source.value,
                conflict=collision,
                conflict_resolution=(
                    "Rule D (union): entries merged across layers; same-id collision "
                    "resolved by trusted tier, then scope, then origin"
                    if collision
                    else None
                ),
                competing_values=(
                    [
                        {
                            "level": layer.level,
                            "origin": layer.profile.metadata.source.value,
                            "value": copy.deepcopy(
                                getattr(layer.profile.operational_boundaries, attr)
                            ),
                        }
                        for layer in contributing
                    ]
                    if collision
                    else None
                ),
            )
        )
        return True, merged

    # -- full merge -------------------------------------------------------------

    def resolve(
        self, entity_id: str, layers: list[Layer]
    ) -> tuple[SemanticProfile, Resolution]:
        """Merge declared layers into a single effective profile (no default filling)."""
        resolution = Resolution()
        domain = entity_id.split(".", 1)[0]
        effective = SemanticProfile(entity_id=entity_id)

        mode = self.resolve_control_mode(layers, resolution)
        if mode is not None:
            effective.operational_boundaries.control_mode = mode
        triggers = self.resolve_triggers(layers, domain, resolution)
        if triggers is not None:
            effective.operational_boundaries.triggers_automations = triggers
        level = self.resolve_privacy_level(layers, resolution)
        if level is not None:
            effective.privacy_classification.level = level

        for attr in _OB_RULE_D_FIELDS:
            declared, value = self.resolve_rule_d(
                layers,
                f"operational_boundaries.{attr}",
                attr,
                "operational_boundaries",
                resolution,
            )
            if declared:
                setattr(effective.operational_boundaries, attr, value)

        for attr in _OB_UNION_FIELDS:
            declared, value = self.resolve_limit_union(
                layers, f"operational_boundaries.{attr}", attr, resolution
            )
            if declared:
                setattr(effective.operational_boundaries, attr, value)

        for attr in _PRIVACY_RULE_D_FIELDS:
            path = f"privacy_classification.{attr}"
            declared, value = self.resolve_rule_d(
                layers,
                path,
                attr,
                "privacy_classification",
                resolution,
                trusted_only=path in _ACCESS_DECISION_FIELDS,
            )
            if declared:
                setattr(effective.privacy_classification, attr, value)

        for attr in _PERSON_RULE_D_FIELDS:
            path = f"person_traits.{attr}"
            declared, value = self.resolve_rule_d(
                layers,
                path,
                attr,
                "person_traits",
                resolution,
                trusted_only=path in _ACCESS_DECISION_FIELDS,
            )
            if declared:
                setattr(effective.person_traits, attr, value)

        # Effective tags are the union across levels (Spec 9.2). The explanation
        # names the most specific contributing layer (Spec 9.5 wants a per-field
        # entry for every effective field, not only the kernel).
        tags: list[str] = []
        tag_layers: list[Layer] = []
        for layer in sorted(layers, key=lambda layer: -SCOPE_RANK.get(layer.level, 0)):
            if layer.profile.semantic_tags:
                tag_layers.append(layer)
            for tag in layer.profile.semantic_tags:
                if tag not in tags:
                    tags.append(tag)
        effective.semantic_tags = tags
        if tags:
            contributor = max(tag_layers, key=lambda layer: SCOPE_RANK.get(layer.level, 0))
            # Spec 9.5 (as clarified by the erratum): conflict is true when the
            # levels declared DIFFERING tag sets. Tags union rather than replace,
            # so no contribution is lost, but disagreeing declarations are still
            # reported; identical declarations agree and are not a conflict.
            conflict = (
                len({_canonical(sorted(layer.profile.semantic_tags)) for layer in tag_layers})
                > 1
            )
            resolution.explanations.append(
                FieldExplanation(
                    field_path="semantic_tags",
                    effective_value=list(tags),
                    provided_by_level=contributor.level,
                    provided_by_origin=contributor.profile.metadata.source.value,
                    conflict=conflict,
                    conflict_resolution=(
                        "Rule (union, Spec 9.2): tags from every declaring level are retained"
                        if conflict
                        else None
                    ),
                    competing_values=(
                        [
                            {
                                "level": layer.level,
                                "origin": layer.profile.metadata.source.value,
                                "value": list(layer.profile.semantic_tags),
                            }
                            for layer in tag_layers
                        ]
                        if conflict
                        else None
                    ),
                )
            )

        # diagnostic_profile: Rule D-style pick (most specific declaring layer).
        diag_layers = [
            Layer(layer.level, layer.profile)
            for layer in layers
            if layer.profile.diagnostic_profile is not None
        ]
        if diag_layers:
            # Trusted tier first, then scope, then authority: the same order as
            # Rule D, so an untrusted entity-scope diagnostic profile cannot
            # displace a trusted broader one.
            trusted_diag = [
                layer for layer in diag_layers
                if _is_trusted_for(layer.profile, "diagnostic_profile")
            ]
            best = max(
                trusted_diag or diag_layers,
                key=lambda layer: (
                    SCOPE_RANK.get(layer.level, 0),
                    ORIGIN_AUTHORITY[layer.profile.metadata.source],
                ),
            )
            # Deep-copied: the effective profile owns its diagnostics, so a
            # read-modify-write of the merged result cannot mutate the layer.
            effective.diagnostic_profile = copy.deepcopy(best.profile.diagnostic_profile)
            # Spec 9.5: conflict is true when more than one level declared a
            # distinct diagnostic_profile (the others were passed over by Rule D).
            conflict = (
                len({_canonical(layer.profile.diagnostic_profile) for layer in diag_layers}) > 1
            )
            if conflict and trusted_diag and len(trusted_diag) < len(diag_layers):
                reason: str | None = "Rule D: lower-tier declaration never overrides trusted tier"
            elif conflict:
                reason = "Rule D: most specific scope wins, then origin authority"
            else:
                reason = None
            resolution.explanations.append(
                FieldExplanation(
                    field_path="diagnostic_profile",
                    effective_value=copy.deepcopy(best.profile.diagnostic_profile),
                    provided_by_level=best.level,
                    provided_by_origin=best.profile.metadata.source.value,
                    conflict=conflict,
                    conflict_resolution=reason,
                    competing_values=(
                        [
                            {
                                "level": layer.level,
                                "origin": layer.profile.metadata.source.value,
                                "value": copy.deepcopy(layer.profile.diagnostic_profile),
                            }
                            for layer in diag_layers
                        ]
                        if conflict
                        else None
                    ),
                )
            )

        # Effective metadata: the most specific contributing layer's provenance,
        # except profile_valid_for. That field is an invalidation trigger, not
        # provenance, so it is resolved per subfield under Rule D and Rule E
        # (trusted tier, then scope) rather than taken wholesale: a domain-level
        # subfield is inherited when a more-specific profile omits it, and an
        # unconfirmed hybrid subfield cannot overwrite a trusted one.
        if layers:
            most_specific = max(layers, key=lambda layer: SCOPE_RANK.get(layer.level, 0))
            # Deep-copied unconditionally: without the copy the effective profile
            # holds the source layer's own metadata object (and its mutable
            # confirmed_fields list), so a read-modify-write of the merged
            # result would mutate the input profile.
            effective.metadata = replace(
                copy.deepcopy(most_specific.profile.metadata),
                profile_valid_for=self._resolve_valid_for(layers, resolution),
            )
            for layer in layers:
                resolution.warnings.extend(layer.profile.parse_warnings)

        # Carry forward the enrichment and vendor fields this version does not
        # model, so a complete effective profile stays complete (Spec 23),
        # resolving each field under Rule D rather than letting a whole profile
        # win by scope. Trust is per field: a hybrid layer wins an unmodelled
        # field only when that field's path is in its confirmed_fields, so an
        # unconfirmed hybrid enrichment field cannot overwrite a developer one.
        self._carry_unmodelled(layers, effective, resolution)

        # The effective profile declares exactly the fields resolution settled.
        effective.declared_paths = {e.field_path for e in resolution.explanations}

        return effective, resolution

    @staticmethod
    def _resolve_valid_for(
        layers: list[Layer], resolution: Resolution
    ) -> dict[str, Any] | None:
        """Resolve ``profile_valid_for`` per subfield (Rule D + Rule E).

        Each subfield (the four typed members of Spec 5.5 and any forward-
        compatible extra) is resolved independently: a subfield unique to one
        level is inherited, and a subfield declared at several levels is picked
        trusted tier first (a hybrid layer is trusted for the specific path in
        its confirmed_fields, e.g. ``profile_valid_for.review_after_days``), then
        most specific scope, then origin authority. Resolving the object as one
        field would drop a disjoint inherited subfield and recognise confirmation
        only of the whole object.
        """
        keyed: dict[str, list[_Candidate]] = {}
        order: list[str] = []
        declared = False
        for layer in layers:
            value = layer.profile.metadata.profile_valid_for
            if not isinstance(value, dict):
                continue
            declared = True
            for key, sub in value.items():
                if key not in keyed:
                    order.append(key)
                keyed.setdefault(key, []).append(_Candidate(layer, sub))
        if not declared:
            # No layer declared the object at all; a declared-empty {} is kept.
            return None
        merged: dict[str, Any] = {}
        for key in order:
            cands = keyed[key]
            # path_segment, as in unmodelled resolution: a literal dotted key
            # is not path-addressable (Spec 5.7), so it must not be trusted by
            # a confirmed_fields entry that parses as nested segments, and the
            # explanation renders it escaped.
            path = f"profile_valid_for.{path_segment(key)}"
            trusted = [c for c in cands if _is_trusted_for(c.layer.profile, path)]
            winner = max(trusted or cands, key=lambda c: (c.scope_rank, c.origin_authority))
            merged[key] = copy.deepcopy(winner.value)
            conflict = len({_canonical(c.value) for c in cands}) > 1
            if conflict and trusted and len(trusted) < len(cands):
                reason: str | None = "Rule D: lower-tier declaration never overrides trusted tier"
            elif conflict:
                reason = "Rule D: most specific scope wins, then origin authority"
            else:
                reason = None
            resolution.explanations.append(
                FieldExplanation(
                    field_path=path,
                    effective_value=copy.deepcopy(winner.value),
                    provided_by_level=winner.layer.level,
                    provided_by_origin=winner.origin.value,
                    conflict=conflict,
                    conflict_resolution=reason,
                    competing_values=[c.describe() for c in cands] if conflict else None,
                )
            )
        return merged

    @staticmethod
    def _carry_unmodelled(
        layers: list[Layer], effective: SemanticProfile, resolution: Resolution
    ) -> None:
        """Resolve the unmodelled enrichment and vendor fields onto ``effective``.

        Each top-level unmodelled entry is merged across the layers that declare
        it. See ``_merge_unmodelled`` for the per-node rule; the top-level entries
        are gathered here in first-seen order so a stable, order-independent result
        is produced regardless of layer iteration order.
        """
        roots: dict[tuple[str, ...], list[tuple[Layer, str, Any]]] = {}
        order: list[tuple[str, ...]] = []
        for layer in layers:
            for write_path, confirmed_path, value in iter_unmodelled(layer.profile.raw):
                if write_path not in roots:
                    order.append(write_path)
                roots.setdefault(write_path, []).append((layer, confirmed_path, value))
        for write_path in order:
            ConflictResolver._merge_unmodelled(roots[write_path], write_path, effective, resolution)

    @staticmethod
    def _merge_unmodelled(
        cands: list[tuple[Layer, str, Any]],
        write_path: tuple[str, ...],
        effective: SemanticProfile,
        resolution: Resolution,
        suppress_untrusted: bool = False,
    ) -> None:
        """Resolve one unmodelled node across layers and write it onto ``effective.raw``.

        All candidates share the same ``confirmed_path`` (they are the same node
        seen at different layers). When every declaring layer supplies a non-empty
        object here, the node's subfields are composed: keys unique to one layer
        compose (Rule E), and a key declared at several layers recurses. When the
        layers disagree on shape (some objects, some scalars/arrays/empty
        objects), which SHAPE wins is itself a Rule D decision, taken over the
        trusted tier first: a lower-tier scalar or empty object therefore cannot
        force two trusted objects to resolve atomically and erase their disjoint
        subfields, but a trusted, more specific scalar still overrides a broader
        trusted object. If the winning shape is an object, the object candidates
        compose below and the atomic declarations are recorded as the losing
        side of the conflict; if it is atomic, the node resolves atomically.

        ``suppress_untrusted`` is set for the subtree under an object that beat a
        TRUSTED atomic declaration: that declaration is a trusted competitor for
        every child, so a child with no trusted declaration of its own is
        discarded rather than carried, and the object's unconfirmed fields
        cannot piggyback past it on a confirmed sibling.
        """
        confirmed_path = cands[0][1]
        object_cands = [c for c in cands if isinstance(c[2], dict) and c[2]]
        if len(object_cands) == len(cands):
            ConflictResolver._compose_children(
                cands, write_path, effective, resolution, suppress_untrusted
            )
            return

        trusted = [c for c in cands if _shape_trusted(c[0], c[1], c[2])]
        pool = trusted or cands
        winner = max(
            pool,
            key=lambda c: (
                SCOPE_RANK.get(c[0].level, 0),
                ORIGIN_AUTHORITY[c[0].profile.metadata.source],
            ),
        )

        if isinstance(winner[2], dict) and winner[2]:
            # The object shape wins: the objects compose per subfield, and the
            # atomic declarations lose as one conflict at this node (Spec 9.5).
            atomic_trusted = any(
                _is_trusted_for(c[0].profile, c[1])
                for c in cands
                if c not in object_cands
            )
            ConflictResolver._compose_children(
                object_cands,
                write_path,
                effective,
                resolution,
                suppress_untrusted or atomic_trusted,
            )
            composed: Any = effective.raw
            for key in write_path:
                if not isinstance(composed, dict) or key not in composed:
                    # Every child was discarded (each faced the trusted atomic
                    # competitor without a trusted declaration of its own), so
                    # nothing effective exists at this node to explain.
                    return
                composed = composed[key]
            resolution.explanations.append(
                FieldExplanation(
                    field_path=confirmed_path,
                    effective_value=copy.deepcopy(composed),
                    provided_by_level=winner[0].level,
                    provided_by_origin=winner[0].profile.metadata.source.value,
                    conflict=True,
                    conflict_resolution=(
                        "Rule D: layers disagree on this node's shape; the trusted "
                        "tier's objects compose and the atomic declarations lose"
                    ),
                    competing_values=[
                        {
                            "level": c[0].level,
                            "origin": c[0].profile.metadata.source.value,
                            "value": copy.deepcopy(c[2]),
                        }
                        for c in cands
                    ],
                )
            )
            return
        if suppress_untrusted and not trusted:
            # An ancestor of this node was declared atomically by a trusted
            # layer; with no trusted declaration of its own, this value loses
            # to that competitor rather than being carried (Spec 5.4 Rule 6).
            resolution.warnings.append(
                f"{confirmed_path}: unconfirmed value discarded; a trusted layer "
                "declared this subtree atomically (Rule D)"
            )
            return
        target: dict[str, Any] = effective.raw
        for key in write_path[:-1]:
            nxt = target.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                target[key] = nxt
            target = nxt
        target[write_path[-1]] = copy.deepcopy(winner[2])

        # Spec 9.5: every effective field gets an explanation keyed by its
        # confirmed_fields path, and conflict is true when the levels declared
        # DIFFERING values for this node (identical multi-level declarations
        # agree, they do not compete; see the Spec 9.5 erratum).
        conflict = len({_canonical(value) for _, _, value in cands}) > 1
        if conflict and trusted and len(trusted) < len(cands):
            reason: str | None = "Rule D: lower-tier declaration never overrides trusted tier"
        elif conflict:
            reason = "Rule D: most specific scope wins, then origin authority"
        else:
            reason = None
        resolution.explanations.append(
            FieldExplanation(
                field_path=confirmed_path,
                effective_value=copy.deepcopy(winner[2]),
                provided_by_level=winner[0].level,
                provided_by_origin=winner[0].profile.metadata.source.value,
                conflict=conflict,
                conflict_resolution=reason,
                competing_values=(
                    [
                        {
                            "level": c[0].level,
                            "origin": c[0].profile.metadata.source.value,
                            "value": copy.deepcopy(c[2]),
                        }
                        for c in cands
                    ]
                    if conflict
                    else None
                ),
            )
        )

    @staticmethod
    def _compose_children(
        cands: list[tuple[Layer, str, Any]],
        write_path: tuple[str, ...],
        effective: SemanticProfile,
        resolution: Resolution,
        suppress_untrusted: bool = False,
    ) -> None:
        """Compose object candidates per subfield (Rule E), recursing shared keys.

        Every candidate is a non-empty dict. Keys unique to one layer are carried;
        keys declared by several layers resolve as their own node.
        ``suppress_untrusted`` propagates a trusted atomic competitor declared at
        an ancestor (see ``_merge_unmodelled``).
        """
        keys: list[str] = []
        for _, _, value in cands:
            for key in value:
                if key not in keys:
                    keys.append(key)
        for key in keys:
            child = [
                (layer, f"{cpath}.{path_segment(key)}", value[key])
                for layer, cpath, value in cands
                if key in value
            ]
            ConflictResolver._merge_unmodelled(
                child, (*write_path, key), effective, resolution, suppress_untrusted
            )

    def merge(
        self, higher_authority_profile: SemanticProfile, lower_authority_profile: SemanticProfile
    ) -> SemanticProfile:
        """Merge two profiles, treating the first as the more specific scope."""
        layers = [
            Layer("entity", higher_authority_profile),
            Layer("domain", lower_authority_profile),
        ]
        effective, _ = self.resolve(higher_authority_profile.entity_id, layers)
        return effective
