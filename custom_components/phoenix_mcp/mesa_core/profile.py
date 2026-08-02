"""MESA profile data model (Spec Sections 4-7; Module Proposal Section 4.1).

``raw`` retains the original document (root form). ``to_dict`` serialises the
typed fields and merges back the entries of ``raw`` this version does not model,
so unknown fields survive round-trips (Spec Section 23 forward compatibility)
while edits to the typed fields are never silently dropped.

``declared_paths`` records which fields the source document actually declared,
which is what Rule E reads (absence is inherited, never defaulted). A field is
serialised only when it was declared or has been set away from its default, so
a parse/serialise round-trip neither invents declarations nor loses them.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from custom_components.phoenix_mcp.mesa_core import validation
from custom_components.phoenix_mcp.mesa_core.exceptions import MesaValidationError


class ControlMode(StrEnum):
    AUTONOMOUS = "autonomous"
    CONFIRM = "confirm"
    READ_ONLY = "read_only"
    PROHIBITED = "prohibited"


class TriggersAutomations(StrEnum):
    LIKELY = "likely"
    NONE = "none"
    UNKNOWN = "unknown"
    DEPLOYMENT_DEFINED = "deployment_defined"


class PrivacyLevel(StrEnum):
    PUBLIC = "public"
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class MetadataOrigin(StrEnum):
    DEVELOPER = "developer"
    USER = "user"
    HYBRID = "hybrid"
    INFERRED_AI = "inferred_ai"
    UNKNOWN = "unknown"


# Rule A restrictiveness ranking. read_only ties with prohibited but wins the tie
# because it describes entity nature rather than operator policy (Spec Section 4).
CONTROL_MODE_RANK: dict[ControlMode, int] = {
    ControlMode.AUTONOMOUS: 0,
    ControlMode.CONFIRM: 1,
    ControlMode.PROHIBITED: 2,
    ControlMode.READ_ONLY: 2,
}

PRIVACY_RANK: dict[PrivacyLevel, int] = {
    PrivacyLevel.PUBLIC: 0,
    PrivacyLevel.NORMAL: 1,
    PrivacyLevel.SENSITIVE: 2,
    PrivacyLevel.RESTRICTED: 3,
}

# Rule D authority within equal scope (Spec 5.7).
ORIGIN_AUTHORITY: dict[MetadataOrigin, int] = {
    MetadataOrigin.DEVELOPER: 4,
    MetadataOrigin.USER: 3,
    MetadataOrigin.HYBRID: 2,
    MetadataOrigin.INFERRED_AI: 1,
    MetadataOrigin.UNKNOWN: 0,
}

TRUSTED_ORIGINS: frozenset[MetadataOrigin] = frozenset(
    {MetadataOrigin.DEVELOPER, MetadataOrigin.USER, MetadataOrigin.HYBRID}
)

HELPER_DOMAINS: frozenset[str] = frozenset(
    {
        "input_boolean",
        "input_select",
        "input_number",
        "input_text",
        "input_datetime",
        "counter",
        "timer",
    }
)

# Home Assistant service-action targets: keys that name what an action acts on
# instead of how. Each can reach entities other than the one being evaluated,
# and only the host can resolve them, so the enforcer refuses a call carrying
# one (mesa-core decides policy per entity).
HA_TARGET_SELECTOR_KEYS: tuple[str, ...] = (
    "area_id",
    "config_entry_id",
    "device_id",
    "floor_id",
    "label_id",
)

# The subset an automation config can use to reference entities indirectly
# (device triggers and conditions, and the target blocks of purpose-specific
# triggers). A config entry is a service-action target, not something an
# automation references, so it is absent here. Kept beside the set above so the
# difference is deliberate and visible rather than a drift.
HA_AUTOMATION_SELECTOR_KEYS: tuple[str, ...] = tuple(
    key for key in HA_TARGET_SELECTOR_KEYS if key != "config_entry_id"
)

# Built-in domain safety baseline (Spec 5.8). Applies only when an entity has no
# profile at any inheritance level and no deployment_defaults are configured.
DOMAIN_SAFETY_BASELINE: dict[str, ControlMode] = {
    "light": ControlMode.AUTONOMOUS,
    "media_player": ControlMode.CONFIRM,
    "input_select": ControlMode.CONFIRM,
    "switch": ControlMode.CONFIRM,
    "cover": ControlMode.CONFIRM,
    "climate": ControlMode.CONFIRM,
    "lock": ControlMode.PROHIBITED,
    "alarm_control_panel": ControlMode.PROHIBITED,
    "input_boolean": ControlMode.CONFIRM,
    "script": ControlMode.CONFIRM,
    "scene": ControlMode.CONFIRM,
}


def baseline_control_mode(domain: str) -> ControlMode:
    return DOMAIN_SAFETY_BASELINE.get(domain, ControlMode.CONFIRM)


def baseline_triggers_automations(domain: str) -> TriggersAutomations:
    if domain in HELPER_DOMAINS:
        return TriggersAutomations.LIKELY
    return TriggersAutomations.UNKNOWN


@dataclass
class PrivacyClassification:
    level: PrivacyLevel = PrivacyLevel.NORMAL
    contains_presence_data: bool = False
    contains_audio_capture: bool = False
    contains_visual_capture: bool = False
    contains_biometric_data: bool = False
    contains_behavioural_data: bool = False
    data_retention_local: bool | None = None
    access_logging_recommended: bool | None = None
    access_roles: dict[str, list[str]] | None = None
    deny_response_mode: str = "omit"
    privacy_note: str | None = None


@dataclass
class OperationalBoundaries:
    control_mode: ControlMode = ControlMode.CONFIRM
    triggers_automations: TriggersAutomations = TriggersAutomations.UNKNOWN
    # Absence is not a permissive default (Spec 5.7 Rule E): None means "not declared".
    reversible: bool | None = None
    reversibility_cost: str | None = None
    reversibility_note: str | None = None
    reversibility_window_seconds: float | None = None
    idempotent: bool | None = None
    state_persistence: str | None = None
    expected_latency_ms: float | None = None
    side_effect_scope: str | None = None
    state_volatility: str | None = None
    enforcement_mode: str = "advisory"
    control_reason: str | None = None
    declared_limits: list[dict[str, Any]] = field(default_factory=list)
    temporal_constraints: list[dict[str, Any]] = field(default_factory=list)
    override_triggers_automations: bool = False
    override_control_mode: bool = False
    human_reason: str | None = None


@dataclass
class PersonTraits:
    """People semantics for person entities (Enrichment Section 17).

    None (or an empty list) means "not declared" (Rule E); ``is_minor: true``
    at any trusted layer forces restricted privacy behaviour at enforcement.
    """

    household_role: str | None = None
    display_name: str | None = None
    is_minor: bool | None = None
    associated_zones: list[str] = field(default_factory=list)
    associated_automations: list[str] = field(default_factory=list)
    presence_entity: str | None = None


@dataclass
class FreshnessReport:
    """One evaluation of a profile's freshness (Spec 5.4, 5.5).

    ``status`` is the Spec 5.4 ``staleness_status`` value; ``warnings`` are the
    Spec 5.5 invalidation findings, including triggers that could not be
    evaluated. Both come from a single pass over ``profile_valid_for``, so they
    can never disagree about whether a trigger fired.
    """

    status: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProfileMetadata:
    # Programmatically built profiles are authored by this library, so they
    # carry the current format version; from_dict keeps "1.0" for unversioned
    # stored documents (Spec 23: never silently migrated).
    schema_version: str = "1.1"
    profile_version: str | None = None
    source: MetadataOrigin = MetadataOrigin.UNKNOWN
    confidence: float | None = None
    generated_at: str | None = None
    # number per Spec 5.4; int or float is preserved as declared, never truncated
    staleness_window_days: float = 60
    confirmed_fields: list[str] = field(default_factory=list)
    last_updated: str | None = None
    profile_valid_for: dict[str, Any] | None = None


# Keys this version models at each level. Everything else in a source document
# is unknown-but-preserved (Spec 23).
_ROOT_KEYS = frozenset({"semantic_profile", "privacy_classification", "diagnostic_profile"})
_SP_KEYS = frozenset(
    {
        "schema_version",
        "profile_version",
        "metadata_origin",
        "semantic_tags",
        "operational_boundaries",
        "privacy_classification",
        "person_traits",
        "inheritance_scope",
        "last_updated",
        "profile_valid_for",
    }
)
_OB_KEYS = frozenset(
    {
        "control_mode",
        "triggers_automations",
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
        "declared_limits",
        "temporal_constraints",
        "override_triggers_automations",
        "override_control_mode",
        "human_reason",
    }
)
_PC_KEYS = frozenset(
    {
        "level",
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
    }
)
_PT_KEYS = frozenset(
    {
        "household_role",
        "display_name",
        "is_minor",
        "associated_zones",
        "associated_automations",
        "presence_entity",
    }
)


def _unmodelled(source: Any, known: frozenset[str]) -> dict[str, Any]:
    """The entries of ``source`` this version does not model (Spec 23)."""
    if not isinstance(source, dict):
        return {}
    return {k: copy.deepcopy(v) for k, v in source.items() if k not in known}


def _sub(source: Any, key: str) -> dict[str, Any]:
    """``source[key]`` when it is an object, else an empty one."""
    if not isinstance(source, dict):
        return {}
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def path_segment(key: str) -> str:
    """Render a property name as one dot-notation field-path segment.

    Dot-notation cannot address a property name that itself contains a dot (or
    a backslash): ``x_vendor.a.b`` always parses as nested ``a`` then ``b``.
    Escaping keeps the rendered path unique for explanations, and trust
    matching refuses escaped segments, so such a name is never individually
    confirmable, though it is still covered by a confirmed ancestor.
    """
    return key.replace("\\", "\\\\").replace(".", "\\.")


def iter_unmodelled(raw: dict[str, Any]) -> Iterator[tuple[tuple[str, ...], str, Any]]:
    """Yield ``(write_path, confirmed_path, value)`` for each unmodelled top-level entry.

    ``write_path`` is the tuple of dict keys locating the entry in the root
    document, ``confirmed_path`` is the dotted path used in ``confirmed_fields``
    and ``declared()`` (with unaddressable characters escaped, see
    ``path_segment``), and ``value`` is the entry value, which may be a nested
    object. Whole subtrees are yielded, not pre-flattened leaves: the resolver
    merges them per subfield so disjoint inherited subfields compose (Rule E)
    while a scalar-versus-object shape collision is resolved atomically under
    Rule D, which per-leaf flattening cannot distinguish from composition.
    """
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        if key not in _ROOT_KEYS:
            yield (key,), path_segment(key), value
    sp = _sub(raw, "semantic_profile")
    for key, value in sp.items():
        if key not in _SP_KEYS:
            yield ("semantic_profile", key), path_segment(key), value
    for container, known in (("operational_boundaries", _OB_KEYS), ("person_traits", _PT_KEYS)):
        for key, value in _sub(sp, container).items():
            if key not in known:
                yield (
                    ("semantic_profile", container, key),
                    f"{container}.{path_segment(key)}",
                    value,
                )
    # Privacy reads the location the parser canonicalises to: the sibling when
    # present, else the nested copy (Spec 7).
    pc = raw.get("privacy_classification")
    if not isinstance(pc, dict):
        pc = sp.get("privacy_classification")
    if isinstance(pc, dict):
        for key, value in pc.items():
            if key not in _PC_KEYS:
                yield (
                    ("privacy_classification", key),
                    f"privacy_classification.{path_segment(key)}",
                    value,
                )


@dataclass
class SemanticProfile:
    entity_id: str
    semantic_tags: list[str] = field(default_factory=list)
    metadata: ProfileMetadata = field(default_factory=ProfileMetadata)
    operational_boundaries: OperationalBoundaries = field(default_factory=OperationalBoundaries)
    privacy_classification: PrivacyClassification = field(default_factory=PrivacyClassification)
    person_traits: PersonTraits = field(default_factory=PersonTraits)
    inheritance_scope: str = "entity"
    diagnostic_profile: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    parse_warnings: list[str] = field(default_factory=list)
    # Dotted paths the source document explicitly declared (Rule E). Empty for
    # a programmatically constructed profile: it declares nothing until a field
    # is set away from its default.
    declared_paths: set[str] = field(default_factory=set)

    # -- identity helpers ---------------------------------------------------

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def origin(self) -> MetadataOrigin:
        return self.metadata.source

    def is_inferred(self) -> bool:
        return self.metadata.source == MetadataOrigin.INFERRED_AI

    def is_trusted(self) -> bool:
        return self.metadata.source in TRUSTED_ORIGINS

    def declared(self, path: str) -> bool:
        """Whether a dotted field path was explicitly declared (Rule E)."""
        return path in self.declared_paths

    @staticmethod
    def _declared_paths_of(root: dict[str, Any]) -> set[str]:
        """Dotted paths a source document explicitly declares.

        A key present with a null value is not a declaration. ``privacy_
        classification`` is read at the canonical sibling location and the
        nested fallback location (Spec 7).
        """
        sp = _sub(root, "semantic_profile")
        paths: set[str] = set()
        for container in ("operational_boundaries", "person_traits"):
            for key, value in _sub(sp, container).items():
                if value is not None:
                    paths.add(f"{container}.{key}")
        for source in (root.get("privacy_classification"), sp.get("privacy_classification")):
            if isinstance(source, dict):
                for key, value in source.items():
                    if value is not None:
                        paths.add(f"privacy_classification.{key}")
        return paths

    def effective_confidence(self) -> float:
        if self.metadata.confidence is not None:
            return self.metadata.confidence
        return 1.0 if self.is_trusted() else 0.0

    def freshness(
        self,
        now: datetime | None = None,
        *,
        known_entity_ids: Collection[str] | None = None,
        integration_version: str | None = None,
        ha_version: str | None = None,
    ) -> FreshnessReport:
        """Staleness status and invalidation warnings from ONE evaluation.

        ``staleness_status`` and ``validity_warnings`` read the same
        ``profile_valid_for`` triggers, so evaluating them separately can
        report a status and a warning set that disagree. Callers that want
        both (the retrieval tools do) call this instead, and the two answers
        are guaranteed consistent because they come from a single pass.
        """
        findings = self._validity_findings(
            now=now,
            known_entity_ids=known_entity_ids,
            integration_version=integration_version,
            ha_version=ha_version,
        )
        return FreshnessReport(
            status=self._staleness_from(findings, now),
            warnings=[message for _, message in findings],
        )

    def staleness_status(
        self,
        now: datetime | None = None,
        *,
        known_entity_ids: Collection[str] | None = None,
        integration_version: str | None = None,
        ha_version: str | None = None,
    ) -> str:
        """``current`` / ``stale`` / ``unknown`` (Spec 5.4). Trusted profiles do not decay.

        Spec 5.4 defines ``stale`` as age exceeding ``staleness_window_days``
        **or** an invalidation trigger having fired, so the ``profile_valid_for``
        triggers of Spec 5.5 are evaluated here as well. ``review_after_days``
        needs no host input; the registry and version triggers are evaluated
        only when the host supplies the matching argument, exactly as in
        :meth:`validity_warnings`. A fired trigger is definite evidence of
        staleness, so it outranks ``unknown``; a trigger that cannot be
        evaluated leaves the status alone and is surfaced as a warning instead.

        Use :meth:`freshness` when the warnings are wanted as well, so both
        answers come from one evaluation of the triggers.
        """
        return self._staleness_from(
            self._validity_findings(
                now=now,
                known_entity_ids=known_entity_ids,
                integration_version=integration_version,
                ha_version=ha_version,
            ),
            now,
        )

    def _staleness_from(
        self, findings: list[tuple[bool, str]], now: datetime | None
    ) -> str:
        if not self.is_inferred():
            return "current"
        if any(fired for fired, _ in findings):
            return "stale"
        if not self.metadata.generated_at:
            return "unknown"
        try:
            generated = datetime.fromisoformat(self.metadata.generated_at)
        except ValueError:
            return "unknown"
        now = now or datetime.now(tz=generated.tzinfo)
        # Naive and aware datetimes cannot be subtracted; when only one side
        # carries a timezone, compare wall clocks (strip the aware side).
        if now.tzinfo is None and generated.tzinfo is not None:
            generated = generated.replace(tzinfo=None)
        elif now.tzinfo is not None and generated.tzinfo is None:
            now = now.replace(tzinfo=None)
        # Compared in seconds rather than through timedelta(days=...), which
        # raises OverflowError for a validated but astronomically large window;
        # int/float comparison is exact and cannot overflow.
        age_seconds = (now - generated).total_seconds()
        return "stale" if age_seconds > self.metadata.staleness_window_days * 86400 else "current"

    def validity_warnings(
        self,
        *,
        now: datetime | None = None,
        known_entity_ids: Collection[str] | None = None,
        integration_version: str | None = None,
        ha_version: str | None = None,
    ) -> list[str]:
        """Advisory warnings from the profile_valid_for triggers (Spec 5.5).

        Each check runs only when the host supplies its input: pass the entity
        registry to evaluate ``invalidated_by_entities`` and the current
        versions to evaluate the version pins. Version comparison is exact
        string inequality: versions are implementor-defined strings, so an
        ordering guess would be dishonest. Advisory only: a fired trigger
        flags the profile for review and never discards it. Typically called
        on the effective profile, whose ``profile_valid_for`` has been merged
        per subfield across the inheritance layers. Applies to profiles of
        every origin, unlike ``staleness_status``, which Spec 5.4 scopes to
        inferred profiles.
        """
        return [
            message
            for _, message in self._validity_findings(
                now=now,
                known_entity_ids=known_entity_ids,
                integration_version=integration_version,
                ha_version=ha_version,
            )
        ]

    def _validity_findings(
        self,
        *,
        now: datetime | None = None,
        known_entity_ids: Collection[str] | None = None,
        integration_version: str | None = None,
        ha_version: str | None = None,
    ) -> list[tuple[bool, str]]:
        """``(fired, message)`` for each profile_valid_for finding.

        ``fired`` separates a trigger that actually fired, which makes an
        inferred profile stale (Spec 5.4), from one that could not be
        evaluated, which is reported but leaves the status alone. Shared by
        ``staleness_status`` and ``validity_warnings`` so the two cannot drift.
        """
        pvf = self.metadata.profile_valid_for
        if not isinstance(pvf, dict):
            return []
        warnings: list[tuple[bool, str]] = []
        prefix = f"{self.entity_id}: "

        days = pvf.get("review_after_days")
        if isinstance(days, int | float) and not isinstance(days, bool):
            anchor_raw = self.metadata.last_updated or self.metadata.generated_at
            anchor: datetime | None = None
            if anchor_raw:
                try:
                    anchor = datetime.fromisoformat(anchor_raw)
                except ValueError:
                    anchor = None
            if anchor is None:
                # Unevaluable is surfaced, never silent: the operator declared
                # a review window that nothing anchors. It has not fired, so it
                # does not make the profile stale.
                warnings.append(
                    (
                        False,
                        prefix + "review_after_days is declared but cannot be evaluated: "
                        "neither last_updated nor metadata_origin.generated_at provides "
                        "a parseable anchor timestamp",
                    )
                )
            else:
                current = now or datetime.now(tz=anchor.tzinfo)
                # Mixed tz-awareness: compare wall clocks (as staleness_status).
                if current.tzinfo is None and anchor.tzinfo is not None:
                    anchor = anchor.replace(tzinfo=None)
                elif current.tzinfo is not None and anchor.tzinfo is None:
                    current = current.replace(tzinfo=None)
                # Seconds, not timedelta(days=...): exact and cannot overflow.
                if (current - anchor).total_seconds() > days * 86400:
                    warnings.append(
                        (
                            True,
                            prefix + f"profile is due for review: review_after_days ({days}) "
                            f"has elapsed since {anchor_raw}",
                        )
                    )

        if known_entity_ids is not None:
            known = set(known_entity_ids)
            declared = pvf.get("invalidated_by_entities")
            if isinstance(declared, list):
                for ref in declared:
                    if isinstance(ref, str) and ref not in known:
                        warnings.append(
                            (
                                True,
                                prefix + f"invalidated: entity {ref} named in "
                                "invalidated_by_entities is no longer in the registry",
                            )
                        )

        for key, current_version in (
            ("integration_version", integration_version),
            ("ha_version", ha_version),
        ):
            if current_version is None:
                continue
            declared_version = pvf.get(key)
            if isinstance(declared_version, str) and declared_version != current_version:
                warnings.append(
                    (
                        True,
                        prefix + f"{key} mismatch: profile authored against "
                        f"{declared_version}, current is {current_version}",
                    )
                )
        return warnings

    # -- parsing ------------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        entity_id: str,
        data: dict[str, Any],
        *,
        default_origin: MetadataOrigin = MetadataOrigin.UNKNOWN,
    ) -> SemanticProfile:
        """Parse a profile document (root form, or bare semantic_profile contents).

        ``default_origin`` applies when ``metadata_origin`` is absent: UNKNOWN
        everywhere except integration sidecar imports, which pass DEVELOPER
        (Spec 5.3 location-based provenance defaults).
        """
        validation.validate_or_raise(data, entity_id)

        if "semantic_profile" in data:
            root = copy.deepcopy(data)
        else:
            root = {"semantic_profile": copy.deepcopy(data)}
        sp = root.get("semantic_profile") or {}

        # Canonicalise privacy_classification to the sibling location (Spec 7).
        pc_raw = root.get("privacy_classification")
        if pc_raw is None and isinstance(sp.get("privacy_classification"), dict):
            pc_raw = sp["privacy_classification"]
            root["privacy_classification"] = pc_raw

        mo = sp.get("metadata_origin") or {}
        source = MetadataOrigin(mo["source"]) if "source" in mo else default_origin
        metadata = ProfileMetadata(
            schema_version=sp.get("schema_version", "1.0"),
            profile_version=sp.get("profile_version"),
            source=source,
            confidence=mo.get("confidence"),
            generated_at=mo.get("generated_at"),
            staleness_window_days=mo.get("staleness_window_days", 60),
            confirmed_fields=list(mo.get("confirmed_fields") or []),
            last_updated=sp.get("last_updated"),
            profile_valid_for=sp.get("profile_valid_for"),
        )

        ob_raw = sp.get("operational_boundaries") or {}
        boundaries = OperationalBoundaries(
            control_mode=ControlMode(ob_raw.get("control_mode", "confirm")),
            triggers_automations=TriggersAutomations(
                ob_raw.get("triggers_automations", "unknown")
            ),
            reversible=ob_raw.get("reversible"),
            reversibility_cost=ob_raw.get("reversibility_cost"),
            reversibility_note=ob_raw.get("reversibility_note"),
            reversibility_window_seconds=ob_raw.get("reversibility_window_seconds"),
            idempotent=ob_raw.get("idempotent"),
            state_persistence=ob_raw.get("state_persistence"),
            expected_latency_ms=ob_raw.get("expected_latency_ms"),
            side_effect_scope=ob_raw.get("side_effect_scope"),
            state_volatility=ob_raw.get("state_volatility"),
            enforcement_mode=ob_raw.get("enforcement_mode", "advisory"),
            control_reason=ob_raw.get("control_reason"),
            declared_limits=list(ob_raw.get("declared_limits") or []),
            temporal_constraints=list(ob_raw.get("temporal_constraints") or []),
            override_triggers_automations=bool(ob_raw.get("override_triggers_automations", False)),
            override_control_mode=bool(ob_raw.get("override_control_mode", False)),
            human_reason=ob_raw.get("human_reason"),
        )

        # NOTE: untrusted-origin safety coercions (Spec 5.4 Rules 3, 8, 9) are NOT
        # applied here. Parsing is faithful to the document; trust policy is applied
        # at resolution time (mesa_core.conflict), which every consumption path uses.

        if isinstance(pc_raw, dict):
            privacy = PrivacyClassification(
                level=PrivacyLevel(pc_raw.get("level", "normal")),
                contains_presence_data=bool(pc_raw.get("contains_presence_data", False)),
                contains_audio_capture=bool(pc_raw.get("contains_audio_capture", False)),
                contains_visual_capture=bool(pc_raw.get("contains_visual_capture", False)),
                contains_biometric_data=bool(pc_raw.get("contains_biometric_data", False)),
                contains_behavioural_data=bool(pc_raw.get("contains_behavioural_data", False)),
                data_retention_local=pc_raw.get("data_retention_local"),
                access_logging_recommended=pc_raw.get("access_logging_recommended"),
                access_roles=pc_raw.get("access_roles"),
                deny_response_mode=pc_raw.get("deny_response_mode", "omit"),
                privacy_note=pc_raw.get("privacy_note"),
            )
        else:
            privacy = PrivacyClassification()

        pt_raw = sp.get("person_traits") or {}
        person = PersonTraits(
            household_role=pt_raw.get("household_role"),
            display_name=pt_raw.get("display_name"),
            is_minor=pt_raw.get("is_minor"),
            associated_zones=list(pt_raw.get("associated_zones") or []),
            associated_automations=list(pt_raw.get("associated_automations") or []),
            presence_entity=pt_raw.get("presence_entity"),
        )

        return cls(
            entity_id=entity_id,
            semantic_tags=list(sp.get("semantic_tags") or []),
            metadata=metadata,
            operational_boundaries=boundaries,
            privacy_classification=privacy,
            person_traits=person,
            inheritance_scope=sp.get("inheritance_scope", "entity"),
            diagnostic_profile=root.get("diagnostic_profile"),
            raw=root,
            declared_paths=cls._declared_paths_of(root),
        )

    # -- serialisation ------------------------------------------------------

    def _emit(self, path: str, value: Any, default: Any) -> bool:
        """Whether a modelled field belongs in the serialised document.

        Declared fields round-trip verbatim; undeclared fields appear only once
        set away from their default, so serialising never invents a declaration
        Rule E would then honour, and never drops an edit.
        """
        return path in self.declared_paths or value != default

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the root document form.

        The typed fields are the source of truth; entries of ``raw`` this
        version does not model are merged back around them (Spec 23).
        """
        sp_raw = _sub(self.raw, "semantic_profile")

        ob = self.operational_boundaries
        ob_dict: dict[str, Any] = _unmodelled(sp_raw.get("operational_boundaries"), _OB_KEYS)
        if self._emit("operational_boundaries.control_mode", ob.control_mode, ControlMode.CONFIRM):
            ob_dict["control_mode"] = ob.control_mode.value
        if self._emit(
            "operational_boundaries.triggers_automations",
            ob.triggers_automations,
            TriggersAutomations.UNKNOWN,
        ):
            ob_dict["triggers_automations"] = ob.triggers_automations.value
        for key in (
            "reversible",
            "reversibility_cost",
            "reversibility_note",
            "reversibility_window_seconds",
            "idempotent",
            "state_persistence",
            "expected_latency_ms",
            "side_effect_scope",
            "state_volatility",
            "control_reason",
            "human_reason",
        ):
            value = getattr(ob, key)
            if value is not None:
                ob_dict[key] = value
        if self._emit("operational_boundaries.enforcement_mode", ob.enforcement_mode, "advisory"):
            ob_dict["enforcement_mode"] = ob.enforcement_mode
        if ob.declared_limits:
            ob_dict["declared_limits"] = copy.deepcopy(ob.declared_limits)
        if ob.temporal_constraints:
            ob_dict["temporal_constraints"] = copy.deepcopy(ob.temporal_constraints)
        if ob.override_triggers_automations:
            ob_dict["override_triggers_automations"] = True
        if ob.override_control_mode:
            ob_dict["override_control_mode"] = True

        mo_dict: dict[str, Any] = {"source": self.metadata.source.value}
        if self.metadata.confidence is not None:
            mo_dict["confidence"] = self.metadata.confidence
        if self.metadata.generated_at is not None:
            mo_dict["generated_at"] = self.metadata.generated_at
        if self.metadata.staleness_window_days != 60:
            mo_dict["staleness_window_days"] = self.metadata.staleness_window_days
        if self.metadata.confirmed_fields or self.metadata.source == MetadataOrigin.HYBRID:
            # REQUIRED for hybrid (Spec 5.3), so it survives a round-trip even
            # when empty; dropping it as falsy would make a stored hybrid
            # profile fail to reload.
            mo_dict["confirmed_fields"] = list(self.metadata.confirmed_fields)

        sp_dict: dict[str, Any] = _unmodelled(sp_raw, _SP_KEYS)
        sp_dict["schema_version"] = self.metadata.schema_version
        sp_dict["metadata_origin"] = mo_dict
        if ob_dict:
            sp_dict["operational_boundaries"] = ob_dict
        if self.metadata.profile_version is not None:
            sp_dict["profile_version"] = self.metadata.profile_version
        if self.semantic_tags:
            sp_dict["semantic_tags"] = list(self.semantic_tags)
        if self.metadata.last_updated is not None:
            sp_dict["last_updated"] = self.metadata.last_updated
        if self.metadata.profile_valid_for is not None:
            sp_dict["profile_valid_for"] = copy.deepcopy(self.metadata.profile_valid_for)
        if self.inheritance_scope != "entity":
            sp_dict["inheritance_scope"] = self.inheritance_scope

        pc = self.privacy_classification
        pc_dict: dict[str, Any] = _unmodelled(self.raw.get("privacy_classification"), _PC_KEYS)
        for key in (
            "contains_presence_data",
            "contains_audio_capture",
            "contains_visual_capture",
            "contains_biometric_data",
            "contains_behavioural_data",
        ):
            if getattr(pc, key):
                pc_dict[key] = True
        for key in ("data_retention_local", "access_logging_recommended", "privacy_note"):
            value = getattr(pc, key)
            if value is not None:
                pc_dict[key] = value
        if pc.access_roles is not None:
            pc_dict["access_roles"] = copy.deepcopy(pc.access_roles)
        if pc.deny_response_mode != "omit":
            pc_dict["deny_response_mode"] = pc.deny_response_mode
        if self._emit("privacy_classification.level", pc.level, PrivacyLevel.NORMAL) or pc_dict:
            # level is REQUIRED whenever the object is present (Spec 7.1), so a
            # classification carrying any other field carries its level too.
            pc_dict["level"] = pc.level.value

        pt = self.person_traits
        pt_dict: dict[str, Any] = _unmodelled(sp_raw.get("person_traits"), _PT_KEYS)
        for key in ("household_role", "display_name", "is_minor", "presence_entity"):
            value = getattr(pt, key)
            if value is not None:
                pt_dict[key] = value
        for key in ("associated_zones", "associated_automations"):
            values = getattr(pt, key)
            if values:
                pt_dict[key] = list(values)
        if pt_dict:
            sp_dict["person_traits"] = pt_dict

        root: dict[str, Any] = _unmodelled(self.raw, _ROOT_KEYS)
        root["semantic_profile"] = sp_dict
        if pc_dict:
            root["privacy_classification"] = pc_dict
        if self.diagnostic_profile is not None:
            root["diagnostic_profile"] = copy.deepcopy(self.diagnostic_profile)
        return root


def parse_control_mode(value: str) -> ControlMode:
    try:
        return ControlMode(value)
    except ValueError as err:
        raise MesaValidationError(f"invalid control_mode: {value!r}") from err
