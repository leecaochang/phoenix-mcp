"""Tests for the mesa-core audit bridge.

The property under test is narrow and easy to get vacuously right, so each test
here is written to fail if the filter, the attribution, or the teardown is
removed: an "it recorded something" assertion would pass against a bridge that
recorded everything, which is the specific thing this must not do.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.mesa import async_apply_mesa_to_call, async_setup_mesa
from custom_components.phoenix_mcp.mesa_audit import (
    attach_mesa_audit_bridge,
    detach_mesa_audit_bridge,
    request_context,
)
from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile
from custom_components.phoenix_mcp.mesa_core.audit import (
    MesaAuditEvent,
    audit_logger,
    emit_audit_event,
)
from custom_components.phoenix_mcp.token_store import GlobalSettings, PermissionTree, TokenRecord


def _token(**kw) -> TokenRecord:
    return TokenRecord(
        id="tok", name="Road test", token_hash="x", created_at=utcnow(),
        created_by="admin", permissions=PermissionTree(), **kw,
    )


class _Store:
    def __init__(self, settings: GlobalSettings | None = None) -> None:
        self._settings = settings or GlobalSettings()

    def get_settings(self) -> GlobalSettings:
        return self._settings


class _Data:
    """The two attributes the bridge touches, and mesa for the live path."""

    def __init__(self, settings: GlobalSettings | None = None, mesa=None) -> None:
        self.audit = AuditLog()
        self.store = _Store(settings)
        self.mesa = mesa


@pytest.fixture
def bridge_data():
    """A bridge attached for one test, always detached again.

    Without the teardown a failing test would leave a handler on the process-wide
    mesa_core.audit logger and silently pollute every later test in the session.
    """
    data = _Data()
    bridge = attach_mesa_audit_bridge(data)
    # The logger's level is deliberately untouched by the bridge, so a test run
    # under a raised root level would see nothing; pin it here, not in source.
    previous = audit_logger.level
    audit_logger.setLevel(logging.INFO)
    try:
        yield data, bridge
    finally:
        detach_mesa_audit_bridge(bridge)
        audit_logger.setLevel(previous)


def _emit(decision: str, *, event_type: str = "enforcement_decision", **kw) -> None:
    emit_audit_event(MesaAuditEvent(
        event_type=event_type,
        action=kw.pop("action", "lock.unlock"),
        decision=decision,
        entity_id=kw.pop("entity_id", "lock.front"),
        rule_applied=kw.pop("rule_applied", "control_mode:prohibited"),
        **kw,
    ))


class TestFiltering:
    def test_a_block_is_recorded_against_the_request(self, bridge_data):
        data, _ = bridge_data
        with request_context(_token(), "req-1", "10.0.0.5"):
            _emit("blocked")
        entries = data.audit.query()
        assert len(entries) == 1
        e = entries[0]
        assert e.outcome == "denied"
        assert e.method == "mesa:enforcement_decision"
        assert e.resource == "lock.front"
        assert e.request_id == "req-1"
        assert e.token_id == "tok" and e.token_name == "Road test"
        assert e.client_ip == "10.0.0.5"
        # The rule is the whole point of the row: "denied" alone is what the
        # Audit tab already showed before this bridge existed.
        assert "control_mode:prohibited" in (e.payload or "")

    def test_a_privacy_denial_is_recorded_too(self, bridge_data):
        # Nothing in Phoenix MCP inspects the privacy path, so this is a case the
        # bridge reaches and a hand-rolled mirror of the verdict would not.
        data, _ = bridge_data
        with request_context(_token(), "req-2", None):
            _emit("denied", event_type="privacy_access", action="access", rule_applied="privacy:deny_for")
        entries = data.audit.query()
        assert len(entries) == 1
        assert entries[0].method == "mesa:privacy_access"

    def test_allowed_decisions_are_not_recorded(self, bridge_data):
        # mesa-core emits an INFO "allowed" for every access to a sensitive or
        # person entity. Recording those would evict real request history from a
        # fixed-size ring buffer.
        data, _ = bridge_data
        with request_context(_token(), "req-3", None):
            _emit("allowed")
            _emit("allowed", event_type="privacy_access", action="access")
            _emit("granted", event_type="lease", action="lease_request")
        assert data.audit.query() == []

    def test_an_event_outside_a_request_is_dropped(self, bridge_data, monkeypatch):
        # A dry_run_service preview runs the same enforcer; a predicted block is
        # not a block, and there is no request to attribute it to.
        #
        # The handleError assertion is load-bearing, not decoration: deleting the
        # ctx-is-None guard makes the handler dereference None and the class's
        # own catch-all swallows it, so "nothing was recorded" holds either way
        # and this test passed against the deliberately broken version until it
        # also insisted the skip was CLEAN.
        data, _ = bridge_data
        errors: list[object] = []
        monkeypatch.setattr(logging.Handler, "handleError", lambda self, record: errors.append(record))
        _emit("blocked")
        assert data.audit.query() == []
        assert errors == []

    def test_a_record_without_the_event_attribute_is_ignored(self, bridge_data, monkeypatch):
        data, _ = bridge_data
        errors: list[object] = []
        monkeypatch.setattr(logging.Handler, "handleError", lambda self, record: errors.append(record))
        with request_context(_token(), "req-4", None):
            audit_logger.info("an ordinary log line")
        assert data.audit.query() == []
        assert errors == []


class TestSettingsAndSafety:
    def test_the_operator_log_denied_toggle_governs_it(self):
        # Bridged rows go through AuditLog.record, so the existing per-outcome
        # toggles apply without the bridge knowing about them.
        data = _Data(GlobalSettings(log_denied=False))
        bridge = attach_mesa_audit_bridge(data)
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            with request_context(_token(), "req-5", None):
                _emit("blocked")
            assert data.audit.query() == []
        finally:
            detach_mesa_audit_bridge(bridge)
            audit_logger.setLevel(previous)

    def test_a_failure_inside_the_bridge_never_reaches_the_caller(self, bridge_data, monkeypatch):
        # This handler runs inside mesa-core's own decision path: an exception
        # here would turn an audit-plumbing bug into a failed service call.
        data, _ = bridge_data

        def boom(**kwargs):
            raise RuntimeError("audit exploded")

        monkeypatch.setattr(data.audit, "record", boom)
        monkeypatch.setattr(logging.Handler, "handleError", lambda self, record: None)
        with request_context(_token(), "req-6", None):
            _emit("blocked")  # must not raise


class TestLifecycle:
    def test_detach_stops_recording(self):
        data = _Data()
        bridge = attach_mesa_audit_bridge(data)
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            detach_mesa_audit_bridge(bridge)
            detach_mesa_audit_bridge(bridge)  # idempotent
            detach_mesa_audit_bridge(None)
            with request_context(_token(), "req-7", None):
                _emit("blocked")
            assert data.audit.query() == []
        finally:
            audit_logger.setLevel(previous)

    def test_a_reload_does_not_stack_handlers(self):
        # attach/detach pairs the way async_setup_entry and async_on_unload do.
        # Leaking one handler per reload would multiply every future row.
        data = _Data()
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            for _ in range(3):
                bridge = attach_mesa_audit_bridge(data)
                detach_mesa_audit_bridge(bridge)
            bridge = attach_mesa_audit_bridge(data)
            try:
                with request_context(_token(), "req-8", None):
                    _emit("blocked")
                assert len(data.audit.query()) == 1
            finally:
                detach_mesa_audit_bridge(bridge)
        finally:
            audit_logger.setLevel(previous)


class TestConfirmIsNotADenial:
    """A confirm entity Phoenix let through must not appear as a denial.

    mesa-core labels it "blocked" because the enforcer runs with
    interactive=False, but Phoenix reinterprets it host-side in BOTH
    modes: advisory allows with a warning, enforced raises a pending approval
    that gets its own record. Neither is a denial, and recording one made the
    Audit tab list actions MESA had not stopped.
    """

    @pytest.mark.asyncio
    async def test_advisory_confirm_files_no_denial_row(self, hass: HomeAssistant):
        runtime = await async_setup_mesa(hass, "advisory")
        runtime.store.set("timer.ok", SemanticProfile.from_dict(
            "timer.ok",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
            default_origin=MetadataOrigin.USER,
        ))
        data = _Data(GlobalSettings(mesa_mode="advisory"), mesa=runtime)
        bridge = attach_mesa_audit_bridge(data)
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            outcome = await async_apply_mesa_to_call(
                hass, data, _token(),
                domain="timer", service="cancel", service_data={},
                entities=["timer.ok"], request_id="req-confirm",
                client_ip="127.0.0.1", session_id="s",
            )
            # The action RAN, so nothing was denied.
            assert outcome.decision == "allow"
            assert outcome.entities == ["timer.ok"]
            assert data.audit.query() == []
        finally:
            detach_mesa_audit_bridge(bridge)
            audit_logger.setLevel(previous)

    @pytest.mark.asyncio
    async def test_only_the_real_block_is_recorded_in_a_mixed_call(self, hass: HomeAssistant):
        """The live shape: many confirm entities, one genuine block.

        This is the case that produced the defect. Six confirm timers and one
        read_only one filed seven denial rows for one actual block, so the count
        is the assertion that matters, not merely that the block appears.
        """
        runtime = await async_setup_mesa(hass, "advisory")
        for i in range(6):
            runtime.store.set(f"timer.ok{i}", SemanticProfile.from_dict(
                f"timer.ok{i}",
                {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
                default_origin=MetadataOrigin.USER,
            ))
        runtime.store.set("timer.leak", SemanticProfile.from_dict(
            "timer.leak",
            {"semantic_profile": {"operational_boundaries": {
                "control_mode": "read_only", "enforcement_mode": "enforced"}}},
            default_origin=MetadataOrigin.USER,
        ))
        data = _Data(GlobalSettings(mesa_mode="advisory"), mesa=runtime)
        bridge = attach_mesa_audit_bridge(data)
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            outcome = await async_apply_mesa_to_call(
                hass, data, _token(),
                domain="timer", service="cancel", service_data={},
                entities=[f"timer.ok{i}" for i in range(6)] + ["timer.leak"],
                request_id="req-mixed", client_ip="127.0.0.1", session_id="s",
            )
            assert outcome.decision == "allow"
            assert "timer.leak" not in outcome.entities
            assert len(outcome.entities) == 6

            entries = data.audit.query()
            assert [e.resource for e in entries] == ["timer.leak"], (
                "the audit log must name exactly the entity MESA stopped"
            )
        finally:
            detach_mesa_audit_bridge(bridge)
            audit_logger.setLevel(previous)

    def test_the_confirm_rule_is_filtered_at_the_bridge(self, bridge_data):
        """Mode-independent, pinned at the filter itself.

        Asserted here rather than end-to-end for the enforced case, because that
        path builds a real PendingApproval and the point is the filter, not the
        approval machinery. A neighbouring prohibited event proves the bridge is
        still recording, so this cannot pass by recording nothing at all.
        """
        data, _ = bridge_data
        with request_context(_token(), "req-confirm", None):
            _emit("blocked", rule_applied="control_mode:confirm_no_channel",
                  entity_id="timer.ok")
            _emit("blocked", rule_applied="control_mode:prohibited",
                  entity_id="lock.front")
        assert [e.resource for e in data.audit.query()] == ["lock.front"]


class TestLoggerLevelCoupling:
    """An empty MESA audit log does not mean MESA blocked nothing.

    mesa-core emits at INFO and the bridge deliberately never raises the
    logger's level (that would override the operator's logging config and put a
    line in home-assistant.log per event). The consequence is that raising the
    default log level, a common way to quiet the log FILE, silences the security
    audit trail too. Pinned here so the coupling is a known, tested property
    rather than a surprise.
    """

    @pytest.mark.asyncio
    async def test_a_real_block_files_nothing_when_the_logger_is_above_info(
            self, hass: HomeAssistant):
        runtime = await async_setup_mesa(hass, "enforced")
        runtime.store.set("lock.front", SemanticProfile.from_dict(
            "lock.front",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "prohibited"}}},
            default_origin=MetadataOrigin.USER,
        ))
        data = _Data(GlobalSettings(mesa_mode="enforced"), mesa=runtime)
        # Order matters and is the whole test: quiet the logger FIRST, then
        # attach. Attaching first would let the test's own setLevel overwrite a
        # bridge that forces INFO, so the assertion below could never fail and
        # the pin would be vacuous. (It was, until a mutation run said so.)
        previous = audit_logger.level
        audit_logger.setLevel(logging.WARNING)
        bridge = attach_mesa_audit_bridge(data)
        try:
            outcome = await async_apply_mesa_to_call(
                hass, data, _token(),
                domain="lock", service="unlock", service_data={},
                entities=["lock.front"], request_id="req-quiet",
                client_ip="127.0.0.1", session_id="s",
            )
            # The BLOCK still happens; only the audit row is lost.
            assert outcome.decision == "deny"
            assert data.audit.query() == [], (
                "if this now records, the bridge started forcing the logger level; "
                "that is a deliberate design change, not a bug fix"
            )
        finally:
            detach_mesa_audit_bridge(bridge)
            audit_logger.setLevel(previous)


class TestThroughTheRealEnforcer:
    """End to end: no hand-built event, a real prohibited entity."""

    @pytest.mark.asyncio
    async def test_a_real_prohibited_entity_lands_in_the_audit_log(self, hass: HomeAssistant):
        runtime = await async_setup_mesa(hass, "enforced")
        runtime.store.set("lock.front", SemanticProfile.from_dict(
            "lock.front",
            {"semantic_profile": {"operational_boundaries": {"control_mode": "prohibited"}}},
            default_origin=MetadataOrigin.USER,
        ))
        data = _Data(GlobalSettings(mesa_mode="enforced"), mesa=runtime)
        bridge = attach_mesa_audit_bridge(data)
        previous = audit_logger.level
        audit_logger.setLevel(logging.INFO)
        try:
            outcome = await async_apply_mesa_to_call(
                hass, data, _token(),
                domain="lock", service="unlock", service_data={},
                entities=["lock.front"], request_id="req-live",
                client_ip="127.0.0.1", session_id="s",
            )
            assert outcome.decision == "deny"
            entries = data.audit.query()
            assert [e.resource for e in entries] == ["lock.front"]
            assert entries[0].request_id == "req-live"
        finally:
            detach_mesa_audit_bridge(bridge)
            audit_logger.setLevel(previous)
