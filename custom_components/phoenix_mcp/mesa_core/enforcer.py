"""MesaEnforcer: service call evaluation (Module 4.4; Spec 4, 5.8, 6.4-6.6).

Evaluation order: resolve effective profile -> apply temporal constraints (so a
temporally tightened control_mode is honoured) -> privacy -> control_mode
(including the enforced-mode confirmation round-trip) -> declared limits.

The server-level ``mode`` interacts with per-profile ``enforcement_mode``: a
call is enforced when either is "enforced". ``read_only`` blocks regardless of
mode because it describes entity nature, not policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from custom_components.phoenix_mcp.mesa_core.audit import MesaAuditEvent, emit_audit_event
from custom_components.phoenix_mcp.mesa_core.exceptions import MesaError
from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver
from custom_components.phoenix_mcp.mesa_core.privacy import CallerContext, PrivacyEnforcer
from custom_components.phoenix_mcp.mesa_core.profile import (
    DOMAIN_SAFETY_BASELINE,
    HA_TARGET_SELECTOR_KEYS,
    ControlMode,
    MetadataOrigin,
    SemanticProfile,
)
from custom_components.phoenix_mcp.mesa_core.store import ProfileStore
from custom_components.phoenix_mcp.mesa_core.temporal import TemporalEvaluator

__all__ = [
    "DOMAIN_SAFETY_BASELINE",
    "ConfirmationManager",
    "EnforcementResult",
    "MesaEnforcer",
]

CHALLENGE_TTL_SECONDS = 120  # Spec 6.6: challenges SHOULD expire within 120 seconds.
INFERRED_CONFIDENCE_FLOOR = 0.7  # Spec 5.4 Rule 3.
VALID_MODES = ("enforced", "advisory")

# HA reports an entity it cannot read as one of these rather than as absent, so
# they are unevaluable states, not values to compare against (Spec 6.5).
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})


@dataclass
class EnforcementResult:
    allowed: bool
    reason: str
    rule_applied: str | None
    entity_id: str
    effective_profile: SemanticProfile
    warnings: list[str] = field(default_factory=list)
    confirmation_challenge: dict[str, Any] | None = None


def _target_conflict(entity_id: str, service_params: dict[str, Any]) -> str | None:
    """Why these parameters target something other than ``entity_id``, or None.

    A decision covers exactly the entity it was evaluated for, but Home
    Assistant lets an action name its target several other ways: an
    ``entity_id`` in the service data, a ``device_id``/``area_id``/
    ``floor_id``/``label_id`` selector, or a nested ``target`` block carrying
    any of those. Each can reach entities this evaluation never considered, and
    only the host can resolve a selector to entities, so a call carrying one is
    refused rather than evaluated against the single entity it was handed:
    otherwise a challenge approved for a light could execute against a lock, a
    whole area, or a floor.
    """

    def conflict_in(mapping: dict[str, Any], where: str) -> str | None:
        if "entity_id" in mapping and mapping["entity_id"] != entity_id:
            return (
                f"{where} names entity_id {mapping['entity_id']!r}, but policy was "
                f"evaluated for {entity_id!r}"
            )
        for key in HA_TARGET_SELECTOR_KEYS:
            if key in mapping:
                return (
                    f"{where} carries the target selector {key}={mapping[key]!r}, which "
                    f"can select entities other than {entity_id!r} and can only be "
                    "resolved by the host"
                )
        return None

    direct = conflict_in(service_params, "service_params")
    if direct is not None:
        return direct
    if "target" in service_params:
        target = service_params["target"]
        if not isinstance(target, dict):
            return (
                f"service_params carries a target of type {type(target).__name__}, "
                "which names no resolvable entity"
            )
        return conflict_in(target, "service_params.target")
    return None


def _canonical_params(params: dict[str, Any] | None) -> str:
    return json.dumps(params or {}, sort_keys=True, default=str)


class ConfirmationManager:
    """Issues and redeems single-use confirmation challenges (Spec 6.6).

    A token is valid only for the exact entity, service, and parameters of the
    original challenge; expired or reused tokens are rejected.
    """

    def __init__(self, ttl_seconds: int = CHALLENGE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._challenges: dict[str, dict[str, Any]] = {}

    def _evict_expired(self, now: datetime) -> None:
        """Drop challenges past their TTL.

        Redemption is the only other removal path, and an unconfirmed call is
        never redeemed, so without this every challenge a user declined or
        ignored would be retained for the life of the process.
        """
        for challenge_id in [
            cid for cid, record in self._challenges.items() if now > record["expires_at"]
        ]:
            del self._challenges[challenge_id]

    def issue(
        self,
        entity_id: str,
        service: str,
        params: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        self._evict_expired(now)
        challenge_id = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        self._challenges[challenge_id] = {
            "entity_id": entity_id,
            "service": service,
            "params": _canonical_params(params),
            "expires_at": expires_at,
            "used": False,
        }
        return {
            "challenge_id": challenge_id,
            "entity_id": entity_id,
            "service": service,
            "parameters": dict(params or {}),
            "expires_at": expires_at.isoformat(),
        }

    def redeem(
        self,
        token: dict[str, Any],
        entity_id: str,
        service: str,
        params: dict[str, Any] | None,
        now: datetime,
    ) -> tuple[bool, str]:
        challenge_id = token.get("challenge_id")
        if not isinstance(challenge_id, str):
            return False, "confirmation token missing challenge_id"
        # The token records the approval (Spec 6.6 token schema: challenge_id,
        # approved_by, approved_at). A token without them produces no audit
        # trail, which is the protocol's stated purpose, so it is rejected rather
        # than accepted with a warning.
        if not isinstance(token.get("approved_by"), str) or not token["approved_by"]:
            return False, "confirmation token missing approved_by (Spec 6.6)"
        if not isinstance(token.get("approved_at"), str) or not token["approved_at"]:
            return False, "confirmation token missing approved_at (Spec 6.6)"
        record = self._challenges.get(challenge_id)
        if record is None:
            return False, "unknown or expired confirmation challenge"
        if record["used"]:
            return False, "confirmation token already used (single-use)"
        if now > record["expires_at"]:
            del self._challenges[challenge_id]
            return False, "confirmation challenge expired"
        if (
            record["entity_id"] != entity_id
            or record["service"] != service
            or record["params"] != _canonical_params(params)
        ):
            return False, (
                "confirmation token does not match this request: a token is valid "
                "only for the exact entity, service, and parameters challenged"
            )
        record["used"] = True
        return True, "confirmed"


def _finite_float(value: Any) -> float | None:
    """``float(value)`` when it yields a finite number, else None (not comparable).

    Catches OverflowError so an arbitrarily large JSON integer cannot crash
    enforcement, and rejects NaN/Infinity so a non-finite bound fails closed:
    every comparison with NaN is false, which would otherwise silently disable
    a declared limit.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _compare(operator: str, state: str, value: Any) -> bool | None:
    """Evaluate a canonical predicate operator. None = unevaluable.

    Numeric operands go through _finite_float: a state of "nan"/"inf" (or an
    oversized integer value) is unevaluable, not a clean False. Every comparison
    with NaN is false, which would otherwise read the predicate as inactive and
    silently drop the limit instead of failing closed (Spec 6.5).
    """
    try:
        if operator in ("gt", "gte", "lt", "lte"):
            s, v = _finite_float(state), _finite_float(value)
            if s is None or v is None:
                return None
            return {
                "gt": s > v,
                "gte": s >= v,
                "lt": s < v,
                "lte": s <= v,
            }[operator]
        if operator in ("eq", "neq"):
            if isinstance(value, bool):
                # HA states are strings; booleans map onto on/off conventions.
                matched = state.lower() in (("on", "true") if value else ("off", "false"))
            elif isinstance(value, int | float):
                s, v = _finite_float(state), _finite_float(value)
                if s is None or v is None:
                    return None
                matched = s == v
            else:
                matched = state == str(value)
            return matched if operator == "eq" else not matched
        if operator == "in":
            return any(state == str(item) for item in value)
        if operator == "contains":
            return str(value) in state
    except (TypeError, ValueError, OverflowError):
        return None
    return None


class MesaEnforcer:
    def __init__(
        self,
        store: ProfileStore,
        resolver: InheritanceResolver | None = None,
        *,
        mode: str = "enforced",
        interactive: bool = True,
        privacy_enforcer: PrivacyEnforcer | None = None,
        get_state: Callable[[str], str | None] | None = None,
        get_calendar_events: Callable[[str], list[Any]] | None = None,
        get_solar_elevation: Callable[[datetime], float | None] | None = None,
        challenge_ttl_seconds: int = CHALLENGE_TTL_SECONDS,
    ) -> None:
        if mode not in VALID_MODES:
            # Anything unrecognised would read as advisory and silently disable
            # enforcement deployment-wide, so a typo fails closed at wiring time
            # rather than at the first prohibited call.
            raise MesaError(f"invalid mode {mode!r}: expected one of {list(VALID_MODES)}")
        self.store = store
        self.resolver = resolver or InheritanceResolver(store=store)
        self.mode = mode
        self.interactive = interactive
        self.privacy = privacy_enforcer or PrivacyEnforcer()
        self.get_state = get_state
        self.temporal = TemporalEvaluator(
            get_state=get_state,
            get_calendar_events=get_calendar_events,
            get_solar_elevation=get_solar_elevation,
        )
        self.confirmations = ConfirmationManager(ttl_seconds=challenge_ttl_seconds)

    # -- helpers -----------------------------------------------------------------

    def _is_enforced(self, profile_mode: str) -> bool:
        return self.mode == "enforced" or profile_mode == "enforced"

    def _evaluate_predicate(
        self, predicate: dict[str, Any], warnings: list[str], limit_id: str
    ) -> bool:
        """True when the predicate (and therefore the limit) is active.

        Unevaluable predicates are treated as active, mirroring the temporal
        fail-closed rule: an evaluation failure must not disable a limit.
        """
        if predicate.get("type") == "ha_condition":
            warnings.append(
                f"declared limit {limit_id!r}: ha_condition predicates require host "
                "evaluation; treated as active (fail-closed)"
            )
            return True
        entity = predicate.get("entity")
        if self.get_state is None or not entity:
            warnings.append(
                f"declared limit {limit_id!r}: predicate cannot be evaluated without "
                "a get_state callback; treated as active (fail-closed)"
            )
            return True
        try:
            state = self.get_state(str(entity))
        except Exception as err:
            # A host callback failure is an evaluation failure, not a licence to
            # drop the limit.
            warnings.append(
                f"declared limit {limit_id!r}: get_state({entity!r}) failed ({err!r}); "
                "treated as active (fail-closed)"
            )
            return True
        if state is None or state.strip().lower() in UNAVAILABLE_STATES:
            # HA reports an unreadable entity as the strings "unavailable" or
            # "unknown" rather than as absent. Comparing against them would
            # evaluate cleanly to False and silently disable the limit (Spec 6.5).
            warnings.append(
                f"declared limit {limit_id!r}: entity {entity!r} is unavailable "
                f"(state={state!r}); treated as active (fail-closed)"
            )
            return True
        outcome = _compare(str(predicate.get("operator")), state, predicate.get("value"))
        if outcome is None:
            warnings.append(
                f"declared limit {limit_id!r}: predicate could not be evaluated; "
                "treated as active (fail-closed)"
            )
            return True
        return outcome

    def _check_limit(
        self,
        limit: dict[str, Any],
        service: str,
        service_params: dict[str, Any],
    ) -> str | None:
        """Returns a violation description, or None when the call is within limits."""
        spec = limit.get("limit") or {}
        if spec.get("service") != service:
            return None
        parameter = spec.get("parameter")
        if parameter not in service_params:
            return None
        value = service_params[parameter]
        human_reason = limit.get("human_reason") or "declared limit"
        if "max_value" in spec:
            observed = _finite_float(value)
            bound = _finite_float(spec["max_value"])
            if observed is None or bound is None:
                # Non-numeric, non-finite, or oversized: fail closed. A NaN bound
                # would make every comparison false and silently disable the limit.
                return f"{parameter}={value!r} is not comparable to max_value: {human_reason}"
            if observed > bound:
                return (
                    f"{parameter}={value} exceeds max_value {spec['max_value']}: "
                    f"{human_reason}"
                )
        if "min_value" in spec:
            observed = _finite_float(value)
            bound = _finite_float(spec["min_value"])
            if observed is None or bound is None:
                return f"{parameter}={value!r} is not comparable to min_value: {human_reason}"
            if observed < bound:
                return (
                    f"{parameter}={value} is below min_value {spec['min_value']}: "
                    f"{human_reason}"
                )
        if "permitted_values" in spec:
            permitted = spec["permitted_values"]
            if not any(str(value) == str(item) for item in permitted):
                return f"{parameter}={value!r} is not a permitted value: {human_reason}"
        return None

    # -- evaluation -----------------------------------------------------------------

    def evaluate(
        self,
        entity_id: str,
        service: str,
        service_params: dict[str, Any] | None = None,
        caller_context: CallerContext | None = None,
        current_time: datetime | None = None,
        confirmation_token: dict[str, Any] | None = None,
    ) -> EnforcementResult:
        now = current_time or datetime.now()
        service_params = service_params or {}
        explanation = self.resolver.explain(entity_id)
        profile = explanation.effective_profile
        warnings = list(explanation.warnings)

        # A target named twice, differently, is contradictory input: policy is
        # selected from entity_id while the executed call and the confirmation
        # challenge carry the parameters, so accepting it would evaluate one
        # entity and act on another. Home Assistant's REST API takes entity_id
        # inside the service data, which is exactly how a caller-supplied
        # parameter reaches the wire. Denied rather than reconciled: there is
        # no safe way to guess which target the operator meant.
        conflict = _target_conflict(entity_id, service_params)
        if conflict is not None:
            reason = (
                f"contradictory target: {conflict}. Separate target data from service "
                "data, resolve every selector to entities yourself, and evaluate each "
                "one; a decision made here covers only the entity it was given"
            )
            audit_result = EnforcementResult(
                allowed=False,
                reason=reason,
                rule_applied="contradictory_target",
                entity_id=entity_id,
                effective_profile=profile,
                warnings=[*warnings, reason],
            )
            emit_audit_event(
                MesaAuditEvent(
                    event_type="enforcement_decision",
                    action=service,
                    decision="denied",
                    entity_id=entity_id,
                    caller_id=caller_context.caller_id if caller_context else None,
                    roles=caller_context.effective_roles() if caller_context else [],
                    profile_version=profile.metadata.profile_version,
                    rule_applied="contradictory_target",
                    details={"conflict": conflict},
                ),
                level=logging.WARNING,
            )
            return audit_result
        # Set when a confirmation token is redeemed, so the approval reaches the
        # audit trail the protocol promises (Spec 6.6).
        approved: dict[str, Any] | None = None

        def audit(
            decision: str,
            rule: str | None,
            level: int = logging.INFO,
            details: dict[str, Any] | None = None,
        ) -> None:
            emit_audit_event(
                MesaAuditEvent(
                    event_type="enforcement_decision",
                    action=service,
                    decision=decision,
                    entity_id=entity_id,
                    caller_id=caller_context.caller_id if caller_context else None,
                    roles=caller_context.effective_roles() if caller_context else [],
                    profile_version=profile.metadata.profile_version,
                    rule_applied=rule,
                    details=details or {},
                ),
                level=level,
            )

        def blocked(reason: str, rule: str) -> EnforcementResult:
            audit("blocked", rule)
            return EnforcementResult(
                allowed=False,
                reason=reason,
                rule_applied=rule,
                entity_id=entity_id,
                effective_profile=profile,
                warnings=warnings,
            )

        # 1. Temporal constraints first, so a temporally tightened control_mode
        #    is what gets evaluated below.
        temporal = self.temporal.apply(profile.operational_boundaries, now)
        boundaries = temporal.boundaries
        warnings.extend(temporal.warnings)

        # 2. Privacy.
        is_person = profile.domain == "person"
        decision = self.privacy.evaluate(
            profile.privacy_classification,
            caller_context,
            entity_id=entity_id,
            is_person=is_person,
            # From the resolved profile, so is_minor declared at any inheritance
            # level is honoured (Spec 17 Rule 2 cannot be scoped away).
            is_minor=profile.person_traits.is_minor is True,
        )
        if not decision.allowed:
            return blocked(decision.reason, "privacy:deny_for")
        if (
            decision.effective_level.value == "restricted"
            and boundaries.control_mode == ControlMode.AUTONOMOUS
        ):
            # Restricted entities may not be acted on autonomously (Spec 7.1).
            boundaries.control_mode = ControlMode.CONFIRM
            warnings.append(
                "privacy level restricted: autonomous action not permitted; "
                "confirmation required (Spec 7.1)"
            )

        # 3. Low-confidence inferred profiles are surfaced (Spec 5.4 Rule 3).
        if (
            profile.metadata.source == MetadataOrigin.INFERRED_AI
            and profile.effective_confidence() < INFERRED_CONFIDENCE_FLOOR
        ):
            warnings.append(
                f"effective profile is inferred_ai with confidence "
                f"{profile.effective_confidence():.2f} < {INFERRED_CONFIDENCE_FLOOR} "
                "(Spec 5.4 Rule 3)"
            )

        # 4. control_mode.
        mode = boundaries.control_mode
        reason_suffix = boundaries.control_reason or entity_id
        enforced = self._is_enforced(boundaries.enforcement_mode)
        if mode == ControlMode.READ_ONLY:
            # Entity nature, not policy: blocks regardless of enforcement mode.
            return blocked(
                f"Entity is read-only by nature: {reason_suffix}", "control_mode:read_only"
            )
        if mode == ControlMode.PROHIBITED:
            if enforced:
                return blocked(
                    f"Entity is prohibited by policy: {reason_suffix}",
                    "control_mode:prohibited",
                )
            warnings.append(
                f"advisory: entity is prohibited by MESA policy ({reason_suffix}); "
                "the call is not blocked because enforcement is advisory"
            )
        if mode == ControlMode.CONFIRM:
            if not self.interactive:
                # No interaction channel: confirm is blocked for all domains
                # (Spec 4). Operators pre-authorise via deployment_defaults or
                # the Rule A loosening override.
                return blocked(
                    f"Entity requires confirmation but no interaction channel exists: "
                    f"{reason_suffix}",
                    "control_mode:confirm_no_channel",
                )
            if enforced:
                if confirmation_token is not None:
                    ok, message = self.confirmations.redeem(
                        confirmation_token, entity_id, service, service_params, now
                    )
                    if not ok:
                        return blocked(message, "control_mode:confirm")
                    # redeem() guarantees both fields are present strings.
                    approved = {
                        "approved_by": confirmation_token["approved_by"],
                        "approved_at": confirmation_token["approved_at"],
                    }
                    warnings.append(
                        f"confirmation accepted (approved_by={approved['approved_by']})"
                    )
                else:
                    challenge = self.confirmations.issue(entity_id, service, service_params, now)
                    result = blocked(
                        f"Confirmation required: {reason_suffix}. Present this action to "
                        "the user and re-submit with the confirmation_token.",
                        "control_mode:confirm",
                    )
                    result.confirmation_challenge = challenge
                    return result
            else:
                warnings.append(
                    f"confirmation required before acting (advisory): {reason_suffix}"
                )

        # 5. Declared limits (profile limits plus active temporal value constraints).
        all_limits = list(boundaries.declared_limits) + temporal.active_limits
        for limit in all_limits:
            limit_id = str(limit.get("id", "<unnamed>"))
            if "predicate" in limit and not self._evaluate_predicate(
                limit["predicate"], warnings, limit_id
            ):
                continue
            violation = self._check_limit(limit, service, service_params)
            if violation is not None:
                if enforced:
                    return blocked(violation, f"declared_limit:{limit_id}")
                warnings.append(f"advisory: {violation}")

        # Allowed calls are audited at DEBUG: full trails opt in via log level.
        # A confirmed write is the exception: it is the record of a human
        # approving a restricted action, so it is always audited (Spec 6.6).
        if approved is not None:
            audit("allowed", "control_mode:confirm", level=logging.INFO, details=approved)
        else:
            audit("allowed", None, level=logging.DEBUG)
        return EnforcementResult(
            allowed=True,
            reason="permitted",
            rule_applied=None,
            entity_id=entity_id,
            effective_profile=profile,
            warnings=warnings,
        )

    async def aevaluate(
        self,
        entity_id: str,
        service: str,
        service_params: dict[str, Any] | None = None,
        caller_context: CallerContext | None = None,
        current_time: datetime | None = None,
        confirmation_token: dict[str, Any] | None = None,
    ) -> EnforcementResult:
        return await asyncio.to_thread(
            self.evaluate,
            entity_id,
            service,
            service_params,
            caller_context,
            current_time,
            confirmation_token,
        )
