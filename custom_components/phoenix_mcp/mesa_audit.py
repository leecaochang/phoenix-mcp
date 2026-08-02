"""Tail mesa-core's audit events into Phoenix MCP's own audit log.

mesa-core emits an event dict on the ``mesa_core.audit`` logger, one per
decision, with the structured payload on the record's ``mesa_audit_event``
attribute. Phoenix MCP records its OWN outcome for a call, so without this an
operator reading the Audit tab sees that a service call was denied but never
which entity MESA stopped or under which rule. Attaching a logging handler,
rather than keeping a second copy of mesa-core's decision logic, also captures
privacy denials and lease refusals that nothing in Phoenix MCP inspects.

TWO DELIBERATE LIMITS, both about keeping the Audit tab worth reading:

1. DENIALS ONLY, and a denial means one PHOENIX honored. Recording allowed
   traffic would put a row in the ring buffer for each in-scope entity of each
   call, evicting the real request history this log exists for; a
   confirm-approved allow is skipped for the further reason that it already has
   an approval record. The subtle case is ``control_mode:confirm_no_channel``,
   filtered by ``_NOT_A_DENIAL_RULES``: mesa-core labels it "blocked" because
   the enforcer runs with interactive=False, but Phoenix always reinterprets it
   host-side (advisory allows with a warning, enforced raises its own approval),
   so it is never a Phoenix denial. Recording it makes the tab list actions MESA
   did not stop, one row per confirm entity, for no block at all.

2. THE LOGGER'S LEVEL IS NOT TOUCHED. Forcing ``mesa_core.audit`` to INFO would
   override an operator's explicit logging config AND put a line in
   home-assistant.log for every event, which is the exact noise this bridge
   exists to avoid. HA's default root level is INFO, so it works out of the box.
   The consequence, worth knowing before trusting an empty log: where the
   ``logger:`` default sits above INFO (a common way to quiet the log FILE),
   mesa-core emits nothing, so the tab shows no MESA rows at all, which is
   indistinguishable from "MESA blocked nothing". See docs/mesa.html for the
   one-line entry that restores it.

Rows are attributed to the request that provoked them through a ContextVar set
by mesa.async_apply_mesa_to_call, i.e. only on real actuation. The preview tools
run the same enforcer to predict a verdict, and a preview is not a block: with
no context set, an event is dropped rather than logged against a request that
never happened.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from .mesa_core.audit import audit_logger

if TYPE_CHECKING:
    from .data import PhoenixData
    from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)

# Decisions worth an operator's attention. mesa-core's vocabulary: enforcement
# uses "blocked", privacy uses "denied", leases use "granted"/"denied" for a
# request and an expiry reason for an ending.
_RECORDED_DECISIONS = frozenset({"blocked", "denied"})

# NOT a denial, however mesa-core labels it. The enforcer runs with
# interactive=False, so a confirm entity comes back "blocked" with this rule
# before any challenge is issued (rule 27), and Phoenix ALWAYS reinterprets it
# host-side: advisory lets it through with a warning, enforced turns it into a
# pending approval that gets an approval record of its own. Recording it here
# put a "denied" row in the operator's log for an action that actually ran,
# which is worse than saying nothing: the audit tab is where you go to find out
# what MESA stopped, and this was listing things it did not stop.
_NOT_A_DENIAL_RULES = frozenset({"control_mode:confirm_no_channel"})

# Prefix on the audit row's method, so one "mesa" filter in the panel finds
# every kind while each kind stays distinguishable.
_METHOD_PREFIX = "mesa:"


@dataclass(frozen=True)
class MesaAuditContext:
    """The request a mesa-core event should be attributed to."""

    token_id: str
    token_name: str
    request_id: str
    client_ip: str
    pass_through: bool
    preset: str | None


_context: ContextVar[MesaAuditContext | None] = ContextVar("phoenix_mesa_audit", default=None)


@contextlib.contextmanager
def request_context(
    token: TokenRecord, request_id: str, client_ip: str | None
) -> Iterator[None]:
    """Attribute any mesa-core event raised in this block to one request."""
    # Resolved once here rather than per event: a call can raise one event per
    # targeted entity, and the preset lookup is a scan of the token's presets.
    preset: str | None = None
    active = getattr(token, "active_preset_id", None)
    if active:
        preset = next((p.name for p in getattr(token, "presets", []) if p.id == active), None)
    reset = _context.set(MesaAuditContext(
        token_id=token.id,
        token_name=token.name,
        request_id=request_id,
        client_ip=client_ip or "",
        pass_through=bool(getattr(token, "pass_through", False)),
        preset=preset,
    ))
    try:
        yield
    finally:
        _context.reset(reset)


def _resource(event: dict[str, Any]) -> str:
    """What the decision was about: the entity, else the action."""
    return str(event.get("entity_id") or event.get("action") or "-")


class MesaAuditBridge(logging.Handler):
    """Records mesa-core denials into Phoenix MCP's audit log.

    A logging handler runs inside the caller's ``log()`` call, so it must never
    raise: an exception here would surface inside mesa-core's decision path and
    turn an audit-plumbing bug into a failed service call.
    """

    def __init__(self, data: PhoenixData) -> None:
        super().__init__(level=logging.NOTSET)
        self._data = data

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = getattr(record, "mesa_audit_event", None)
            if not isinstance(event, dict):
                return
            if str(event.get("decision")) not in _RECORDED_DECISIONS:
                return
            if str(event.get("rule_applied")) in _NOT_A_DENIAL_RULES:
                return
            ctx = _context.get()
            if ctx is None:
                # No request to attribute it to: a preview, or a lease expiring
                # on a worker thread long after its caller went away. An
                # unattributed row is noise in a per-token log.
                return
            self._data.audit.record(
                request_id=ctx.request_id,
                token_id=ctx.token_id,
                token_name=ctx.token_name,
                method=_METHOD_PREFIX + str(event.get("event_type") or "event"),
                resource=_resource(event),
                outcome="denied",
                client_ip=ctx.client_ip,
                settings=self._data.store.get_settings(),
                pass_through=ctx.pass_through,
                # The rule, the roles and the service live here; record() runs
                # it through redact_structure and caps the size.
                payload=event,
                preset=ctx.preset,
            )
        except Exception:  # noqa: BLE001 - see the class docstring
            self.handleError(record)


def attach_mesa_audit_bridge(data: PhoenixData) -> MesaAuditBridge:
    """Start recording mesa-core denials. Returns the handler, for teardown."""
    bridge = MesaAuditBridge(data)
    audit_logger.addHandler(bridge)
    return bridge


def detach_mesa_audit_bridge(bridge: MesaAuditBridge | None) -> None:
    """Stop recording. Safe to call twice; a config-entry reload must not stack
    handlers, which would multiply every future row."""
    if bridge is None:
        return
    audit_logger.removeHandler(bridge)
    bridge.close()
