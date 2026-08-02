"""Standard audit event schema (Module Proposal Section 8; Spec 7.1).

Every audit record mesa-core emits on the ``mesa_core.audit`` logger carries a
``mesa_audit_event`` attribute holding the standard event dict. Hosts attach a
logging handler and read ``record.mesa_audit_event`` for structured
consumption; the record message stays human-readable. The Core specification
requires logging for restricted entities and person entities; this schema is
the RECOMMENDED shape, not a conformance requirement on third parties.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

audit_logger = logging.getLogger("mesa_core.audit")


@dataclass
class MesaAuditEvent:
    """One audit event: a privacy access, an enforcement decision, or lease
    activity. ``details`` carries event-type-specific fields; the named fields
    are common to every event type (Module Proposal Section 8)."""

    event_type: str  # "privacy_access" | "enforcement_decision" | "lease"
    action: str  # "access", the service called, or the lease operation
    decision: str  # "allowed" | "denied" | "blocked" | "granted" | expiry reason
    entity_id: str | None = None
    caller_id: str | None = None
    roles: list[str] = field(default_factory=list)
    profile_version: str | None = None
    rule_applied: str | None = None
    redaction_mode: str | None = None
    timestamp: str = ""  # ISO 8601; stamped at emission when empty
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "caller_id": self.caller_id,
            "roles": list(self.roles),
            "entity_id": self.entity_id,
            "action": self.action,
            "decision": self.decision,
            "profile_version": self.profile_version,
            "rule_applied": self.rule_applied,
            "redaction_mode": self.redaction_mode,
        }
        if self.details:
            out["details"] = dict(self.details)
        return out


def emit_audit_event(
    event: MesaAuditEvent,
    *,
    logger: logging.Logger | None = None,
    level: int = logging.INFO,
) -> None:
    if not event.timestamp:
        event.timestamp = datetime.now().isoformat()
    (logger or audit_logger).log(
        level,
        "mesa audit: %s %s entity=%s decision=%s",
        event.event_type,
        event.action,
        event.entity_id or "-",
        event.decision,
        extra={"mesa_audit_event": event.to_dict()},
    )
