"""LeaseManager: advisory coordination leases (Enrichment Section 21).

The lease protocol is an advisory signal between MESA-aware components, not a
concurrency lock (Section 21.1). mesa-core owns no event loop, so automatic
expiry is lazy: an expired lease never grants anything, but it is removed, and
its ``mesa_lease_expired`` event emitted, only when a lifecycle operation
sweeps. ``request``, ``release``, ``release_session``, ``expire``, and
``sensor_state`` sweep; ``active_leases`` filters expired entries out of its
result without removing them or emitting, so it stays a side-effect-free read.
Hosts SHOULD therefore call ``expire()`` periodically for timely events rather
than relying on reads to produce them, and MUST call ``release_session()`` when
a session disconnects, since nothing else releases a session's holds early.
Events are delivered through the ``on_lease_event`` callback with the
Section 21.4 payload (``lease_id``, ``entities``, ``reason``, ``timestamp``);
the host bridges them onto the HA event bus.

Multi-agent priority preemption (Section 21.6) ships in v2. For overlapping
requests the existing holder takes precedence, which is 21.6 Rule 3, the
no-priority baseline; ``caller_priority`` is accepted but unused.

Default clocks are timezone-aware UTC. A caller-supplied ``now`` is used
as-is: inject consistently aware or consistently naive datetimes for one
manager's lifetime, since lease expiry compares them against each other.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.phoenix_mcp.mesa_core.audit import MesaAuditEvent, emit_audit_event
from custom_components.phoenix_mcp.mesa_core.exceptions import LeaseNotFoundError, MesaValidationError
from custom_components.phoenix_mcp.mesa_core.lease.registry import Lease, LeaseRegistry
from custom_components.phoenix_mcp.mesa_core.store import ProfileStore

logger = logging.getLogger("mesa_core.lease")

MAX_LEASE_DURATION_SECONDS = 30.0

_PRIORITY_LEVELS = ("deferential", "cooperative", "assertive")
_PREEMPTION_HANDLING = ("rollback_abort", "continue_ignore")
# Automation cooperative_priority levels that deny leases (Spec 21.5).
_DENYING_LEVELS = ("protected", "critical")
_CONFLICT_LEVELS = ("cooperative", "assertive")
# "deferential" is valid but has no coordination effect (Enrichment 11.2).
_INERT_LEVELS = ("deferential",)


def _entity_set(value: Any, where: str, warnings: list[str]) -> set[str] | None:
    """Parse an Enrichment Section 11 entity list; None means unevaluable.

    A non-list value, or a list containing non-strings, is malformed as a
    whole: salvaging the readable items could silently narrow a protection
    scope, so the field is unevaluable and the caller treats it as covering
    every requested entity (fail-closed).
    """
    if value is None:
        return set()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    warnings.append(
        f"{where} is malformed; treated as covering every requested entity (fail-closed)"
    )
    return None


@dataclass
class LeaseResponse:
    """The Section 21.3 lease response. ``lease_id`` and ``expires_at`` are
    always present, even for full denials (the lease is simply never
    registered)."""

    lease_id: str
    granted: bool
    entities_granted: list[str]
    entities_denied: list[str]
    expires_at: str
    granted_duration_seconds: float
    denial_reasons: dict[str, str] = field(default_factory=dict)
    active_conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Entities denied because of protected/critical automations, so the tool
    # layer can distinguish the lease_conflict envelope (Spec 9.6) from a
    # plain existing-holder denial.
    automation_denials: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lease_id": self.lease_id,
            "granted": self.granted,
            "entities_granted": list(self.entities_granted),
            "entities_denied": list(self.entities_denied),
            "expires_at": self.expires_at,
            "granted_duration_seconds": self.granted_duration_seconds,
        }
        if self.denial_reasons:
            out["denial_reasons"] = dict(self.denial_reasons)
        if self.active_conflicts:
            out["active_conflicts"] = list(self.active_conflicts)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


class LeaseManager:
    """Grant, track, and expire advisory coordination leases."""

    def __init__(
        self,
        store: ProfileStore | None = None,
        *,
        get_state: Callable[[str], str | None] | None = None,
        on_lease_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """``store`` supplies automation profiles for the protected/critical
        denial check (Spec 21.5); without it no automation profiles exist to
        check. ``get_state`` reports automation entity state for the
        protected "while active" test; absent, protected automations are
        treated as active (fail-closed). ``on_lease_event`` receives the
        ``mesa_lease_expired`` payload for every ended lease.
        """
        self.store = store
        self.get_state = get_state
        self.on_lease_event = on_lease_event
        self._registry = LeaseRegistry()
        # The registry is reached from threads (arequest/arelease offload with
        # asyncio.to_thread), and granting is a check-then-act across holding()
        # and add(). Reentrant because the lifecycle methods sweep as they go.
        self._lock = threading.RLock()

    # -- events ------------------------------------------------------------------

    def _emit(self, lease: Lease, reason: str, now: datetime) -> None:
        emit_audit_event(
            MesaAuditEvent(
                event_type="lease",
                action="lease_ended",
                decision=reason,
                caller_id=lease.caller_id,
                timestamp=now.isoformat(),
                details={"lease_id": lease.lease_id, "entities": list(lease.entities)},
            )
        )
        if self.on_lease_event is None:
            return
        try:
            self.on_lease_event(
                {
                    "event_type": "mesa_lease_expired",
                    "lease_id": lease.lease_id,
                    "entities": list(lease.entities),
                    "reason": reason,
                    "timestamp": now.isoformat(),
                }
            )
        except Exception:
            # The lease is already released; a host event-bus failure must not
            # abandon the remaining releases or fail the caller's own request.
            logger.exception("on_lease_event callback failed for lease %s", lease.lease_id)

    def _sweep(self, now: datetime) -> None:
        for lease in self._registry.sweep_expired(now):
            self._emit(lease, "natural_expiry", now)

    def _supersede(self, session_id: str, entities: list[str]) -> None:
        """Drop a session's prior hold on entities it is re-acquiring.

        A same-session re-request is a refresh (Spec 21.4). Leaving the old
        lease in place would double-count the hold, publish the superseded
        lease's expiry as ``earliest_expiry``, and fire ``mesa_lease_expired``
        for an entity the session still holds, which tells native automations
        to resume normal operation mid-operation. No event is emitted here: the
        hold is continuous, so nothing ended.
        """
        taken = set(entities)
        for lease in self._registry.by_session(session_id):
            remaining = [entity for entity in lease.entities if entity not in taken]
            if len(remaining) == len(lease.entities):
                continue
            if remaining:
                lease.entities = remaining
            else:
                self._registry.remove(lease.lease_id)

    # -- automation interaction (Spec 21.5) ----------------------------------------

    def _automation_active(self, automation_id: str, warnings: list[str]) -> bool:
        if self.get_state is None:
            warnings.append(
                f"protected automation {automation_id}: no get_state callback; "
                "treated as active (fail-closed)"
            )
            return True
        try:
            state = self.get_state(automation_id)
        except Exception as err:
            warnings.append(
                f"protected automation {automation_id}: get_state failed ({err!r}); "
                "treated as active (fail-closed)"
            )
            return True
        if state is None or state in ("unavailable", "unknown"):
            warnings.append(
                f"protected automation {automation_id}: state unavailable; "
                "treated as active (fail-closed)"
            )
            return True
        return state == "on"

    def _automation_conflicts(
        self, requested: set[str], warnings: list[str]
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Denials and advisory conflicts from stored automation profiles.

        Reads the unmodeled Section 11 fields from ``raw``: monitored entities
        are trigger + condition entities (11.3); for ``critical`` the scope
        additionally includes ``intent_archetype.affected_entities``, since
        21.5 denies for "entities in this automation's scope".
        """
        denials: dict[str, str] = {}
        conflicts: list[dict[str, Any]] = []
        if self.store is None:
            return denials, conflicts
        for key in self.store.entity_keys():
            if not key.startswith("automation."):
                continue
            try:
                profile = self.store.get(key)
            except MesaValidationError as err:
                warnings.append(f"skipped malformed automation profile {key}: {err}")
                continue
            if profile is None:
                continue
            sp = profile.raw.get("semantic_profile", {})
            cp = sp.get("cooperative_priority")
            if cp is None:
                continue
            # Malformed Section 11 data must fail closed: a typo'd level or a
            # wrong-typed entity list on the one automation guarding a lock
            # would otherwise silently disable its protection.
            level: Any
            if not isinstance(cp, dict):
                warnings.append(
                    f"cooperative_priority on {key} is not an object; "
                    "treated as protected (fail-closed)"
                )
                level = "protected"
            else:
                level = cp.get("level")
                if level is None:
                    continue
                if (
                    level not in _DENYING_LEVELS
                    and level not in _CONFLICT_LEVELS
                    and level not in _INERT_LEVELS
                ):
                    warnings.append(
                        f"unrecognized cooperative_priority.level {level!r} on {key}; "
                        "treated as protected (fail-closed)"
                    )
                    level = "protected"
            if level in _INERT_LEVELS:
                continue
            env = sp.get("environmental_dependencies")
            monitored: set[str] | None
            if env is None:
                monitored = set()
            elif not isinstance(env, dict):
                warnings.append(
                    f"environmental_dependencies on {key} is malformed; "
                    "treated as covering every requested entity (fail-closed)"
                )
                monitored = None
            else:
                trig = _entity_set(env.get("trigger_entities"), f"{key} trigger_entities", warnings)
                cond = _entity_set(
                    env.get("condition_entities"), f"{key} condition_entities", warnings
                )
                monitored = None if trig is None or cond is None else trig | cond
            scope = monitored
            if level == "critical" and scope is not None:
                ia = sp.get("intent_archetype")
                if ia is None:
                    affected: set[str] | None = set()
                elif not isinstance(ia, dict):
                    warnings.append(
                        f"intent_archetype on {key} is malformed; "
                        "treated as covering every requested entity (fail-closed)"
                    )
                    affected = None
                else:
                    affected = _entity_set(
                        ia.get("affected_entities"),
                        f"{key} intent_archetype.affected_entities",
                        warnings,
                    )
                scope = None if affected is None else scope | affected
            relevant = scope if level == "critical" else monitored
            # Unevaluable scope covers everything requested (fail-closed).
            overlap = set(requested) if relevant is None else requested & relevant
            if not overlap:
                continue
            if level == "critical":
                for entity in overlap:
                    denials.setdefault(
                        entity, f"entity is in the scope of critical automation {key} (Spec 21.5)"
                    )
            elif level == "protected":
                if self._automation_active(key, warnings):
                    for entity in overlap:
                        denials.setdefault(
                            entity,
                            f"entity is monitored by active protected automation {key} "
                            "(Spec 21.5)",
                        )
            else:
                conflicts.append(
                    {"automation_id": key, "level": level, "entities": sorted(overlap)}
                )
                if level == "assertive":
                    warnings.append(
                        f"assertive automation {key} may counteract actions on "
                        f"{sorted(overlap)} (Spec 21.5)"
                    )
        return denials, conflicts

    # -- lifecycle (Spec 21.4) ------------------------------------------------------

    def request(
        self,
        entities: list[str],
        duration_seconds: float,
        *,
        session_id: str,
        caller_id: str = "unknown",
        intent: str | None = None,
        priority_level: str = "cooperative",
        preemption_handling: str = "rollback_abort",
        caller_priority: float | None = None,
        now: datetime | None = None,
    ) -> LeaseResponse:
        with self._lock:
            return self._request_locked(
                entities,
                duration_seconds,
                session_id=session_id,
                caller_id=caller_id,
                intent=intent,
                priority_level=priority_level,
                preemption_handling=preemption_handling,
                caller_priority=caller_priority,
                now=now,
            )

    def _request_locked(
        self,
        entities: list[str],
        duration_seconds: float,
        *,
        session_id: str,
        caller_id: str = "unknown",
        intent: str | None = None,
        priority_level: str = "cooperative",
        preemption_handling: str = "rollback_abort",
        caller_priority: float | None = None,
        now: datetime | None = None,
    ) -> LeaseResponse:
        now = now or datetime.now(UTC)
        self._sweep(now)
        if not entities:
            raise ValueError("entities must be a non-empty list")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if priority_level not in _PRIORITY_LEVELS:
            raise ValueError(f"invalid priority_level: {priority_level!r}")
        if preemption_handling not in _PREEMPTION_HANDLING:
            raise ValueError(f"invalid preemption_handling: {preemption_handling!r}")

        warnings: list[str] = []
        granted_duration = min(duration_seconds, MAX_LEASE_DURATION_SECONDS)
        if granted_duration < duration_seconds:
            warnings.append(
                f"duration_seconds clamped to the {MAX_LEASE_DURATION_SECONDS:.0f}s "
                "maximum (Spec 21.2)"
            )
        if caller_priority is not None:
            warnings.append(
                "caller_priority is accepted but unused: multi-agent priority "
                "preemption (Spec 21.6) ships in a future version; existing "
                "holders take precedence"
            )

        denial_reasons, active_conflicts = self._automation_conflicts(set(entities), warnings)
        automation_denials = sorted(denial_reasons)

        # Existing holder takes precedence (21.6 Rule 3 baseline). Same-session
        # overlap is a refresh and is granted.
        for entity in entities:
            if entity in denial_reasons:
                continue
            holder = self._registry.holding(entity, now)
            if holder is not None and holder.session_id != session_id:
                denial_reasons[entity] = (
                    "entity is under an active lease held by another session"
                )

        entities_granted = [e for e in entities if e not in denial_reasons]
        entities_denied = [e for e in entities if e in denial_reasons]
        lease_id = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=granted_duration)

        if entities_granted:
            self._supersede(session_id, entities_granted)
            self._registry.add(
                Lease(
                    lease_id=lease_id,
                    session_id=session_id,
                    caller_id=caller_id,
                    entities=list(entities_granted),
                    granted_at=now,
                    expires_at=expires_at,
                    intent=intent,
                    priority_level=priority_level,
                    preemption_handling=preemption_handling,
                )
            )
        emit_audit_event(
            MesaAuditEvent(
                event_type="lease",
                action="lease_request",
                decision="granted" if entities_granted else "denied",
                caller_id=caller_id,
                timestamp=now.isoformat(),
                details={
                    "lease_id": lease_id,
                    "entities_granted": list(entities_granted),
                    "entities_denied": list(entities_denied),
                    "intent": intent,
                },
            )
        )
        return LeaseResponse(
            lease_id=lease_id,
            granted=bool(entities_granted),
            entities_granted=entities_granted,
            entities_denied=entities_denied,
            expires_at=expires_at.isoformat(),
            granted_duration_seconds=granted_duration,
            denial_reasons=denial_reasons,
            active_conflicts=active_conflicts,
            warnings=warnings,
            automation_denials=automation_denials,
        )

    def release(
        self, lease_id: str, *, session_id: str | None = None, now: datetime | None = None
    ) -> Lease:
        """Release a lease early. ``session_id``, when provided, must match the
        holder's; a mismatch reads as not-found so other sessions' leases are
        never disclosed (Spec 21.6)."""
        now = now or datetime.now(UTC)
        with self._lock:
            self._sweep(now)
            lease = self._registry.get(lease_id)
            if lease is None or (session_id is not None and lease.session_id != session_id):
                raise LeaseNotFoundError(f"lease {lease_id!r} does not exist or has expired")
            self._registry.remove(lease_id)
            self._emit(lease, "early_release", now)
            return lease

    def release_session(self, session_id: str, *, now: datetime | None = None) -> int:
        """Release all leases of a terminated session (Spec 21.4). Returns count."""
        now = now or datetime.now(UTC)
        with self._lock:
            self._sweep(now)
            leases = self._registry.by_session(session_id)
            for lease in leases:
                self._registry.remove(lease.lease_id)
                self._emit(lease, "session_terminated", now)
            return len(leases)

    def expire(self, now: datetime | None = None) -> None:
        """Sweep expired leases, emitting their events. Hosts SHOULD call this
        periodically for timely events; correctness does not depend on it."""
        with self._lock:
            self._sweep(now or datetime.now(UTC))

    def active_leases(self, now: datetime | None = None) -> list[Lease]:
        with self._lock:
            return self._registry.active(now or datetime.now(UTC))

    def sensor_state(self, now: datetime | None = None) -> dict[str, Any]:
        """The ``binary_sensor.mesa_lease_active`` state and attributes
        (Spec 21.4), for hosts that expose the sensor natively."""
        now = now or datetime.now(UTC)
        with self._lock:
            self._sweep(now)
            active = self._registry.active(now)
            leased: set[str] = set()
            for lease in active:
                leased.update(lease.entities)
            earliest = min((lease.expires_at for lease in active), default=None)
        return {
            "state": "on" if active else "off",
            "active_lease_count": len(active),
            "leased_entities": sorted(leased),
            "earliest_expiry": earliest.isoformat() if earliest else None,
            "last_lease_holder": self._registry.last_lease_holder,
        }

    # -- async variants ---------------------------------------------------------------

    async def arequest(
        self, entities: list[str], duration_seconds: float, **kwargs: Any
    ) -> LeaseResponse:
        return await asyncio.to_thread(
            lambda: self.request(entities, duration_seconds, **kwargs)
        )

    async def arelease(self, lease_id: str, **kwargs: Any) -> Lease:
        return await asyncio.to_thread(lambda: self.release(lease_id, **kwargs))

    async def arelease_session(self, session_id: str, **kwargs: Any) -> int:
        return await asyncio.to_thread(lambda: self.release_session(session_id, **kwargs))

    async def aexpire(self, now: datetime | None = None) -> None:
        await asyncio.to_thread(self.expire, now)
