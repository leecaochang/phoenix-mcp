"""In-memory active lease registry (Enrichment Section 21.4).

Leases are deliberately NOT persisted to a StorageBackend: the maximum
duration is 30 seconds and leases are scoped to sessions, which do not survive
a server restart. Persisting them would resurrect stale locks on startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Lease:
    """One active coordination lease (Enrichment Section 21.3, 21.4)."""

    lease_id: str
    session_id: str
    caller_id: str
    entities: list[str]
    granted_at: datetime
    expires_at: datetime
    intent: str | None = None
    priority_level: str = "cooperative"
    preemption_handling: str = "rollback_abort"

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class LeaseRegistry:
    """Holds active leases; sweeps return what was removed for event emission."""

    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self.last_lease_holder: str | None = None

    def add(self, lease: Lease) -> None:
        self._leases[lease.lease_id] = lease
        self.last_lease_holder = lease.caller_id

    def get(self, lease_id: str) -> Lease | None:
        return self._leases.get(lease_id)

    def remove(self, lease_id: str) -> Lease | None:
        return self._leases.pop(lease_id, None)

    def active(self, now: datetime) -> list[Lease]:
        return [lease for lease in self._leases.values() if not lease.is_expired(now)]

    def sweep_expired(self, now: datetime) -> list[Lease]:
        expired = [lease for lease in self._leases.values() if lease.is_expired(now)]
        for lease in expired:
            self._leases.pop(lease.lease_id, None)
        return expired

    def by_session(self, session_id: str) -> list[Lease]:
        return [lease for lease in self._leases.values() if lease.session_id == session_id]

    def holding(self, entity_id: str, now: datetime) -> Lease | None:
        """The active lease covering an entity, or None."""
        for lease in self.active(now):
            if entity_id in lease.entities:
                return lease
        return None
