"""Tests for the diagnostics/traces MCP tools and paginated Recorder history."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _token(permissions: PermissionTree | None = None, **caps) -> TokenRecord:
    tree = permissions or PermissionTree(domains={"light": PermissionNode(state="GREEN")})
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=tree, **caps,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass):
    return await _call_tool(name, args, token, hass, MagicMock())


# --- get_history ---

class _FakeState:
    def __init__(self, state, when):
        self._s, self._w = state, when

    def as_dict(self):
        return {
            "entity_id": "light.kitchen", "state": self._s,
            "last_changed": self._w, "last_updated": self._w, "attributes": {"x": 1},
        }


class TestGetHistoryTransitions:
    def _patched(self, states):
        inst = MagicMock()
        inst.keep_days = 10
        inst.async_add_executor_job = AsyncMock(return_value={"light.kitchen": states})
        self.executor = inst.async_add_executor_job
        return patch("homeassistant.components.recorder.get_instance", return_value=inst)

    async def test_state_changes_are_bounded_and_cursor_paginated(self, hass):
        hass.states.async_set("light.kitchen", "on", {})
        now = utcnow()
        states = [
            _FakeState("on", now - timedelta(hours=3)),
            _FakeState("off", now - timedelta(hours=1)),
            _FakeState("on", now),
        ]
        with self._patched(states):
            content, outcome, _ = await _call(
                "get_history",
                {"entity_id": "light.kitchen", "start_time": "24h", "limit": 2},
                _token(), hass,
            )
        assert outcome == "allowed"
        body = _json(content)
        assert body["mode"] == "state_changes"
        assert [h["state"] for h in body["history"]] == ["on", "off"]
        assert body["count"] == 2
        assert body["has_more"] is True
        assert body["next_cursor"] == (now - timedelta(hours=1)).isoformat()
        assert body["effective_limit"] == 2
        assert body["retention"] == {"kind": "short_term", "configured_days": 10}
        assert body["data_range"] == {
            "start": (now - timedelta(hours=3)).isoformat(),
            "end": (now - timedelta(hours=1)).isoformat(),
        }
        job = self.executor.await_args.args[0]
        assert job.func.__name__ == "state_changes_during_period"
        assert job.args[4] is True
        assert job.args[6] == 3
        assert job.args[7] is False

    async def test_significant_states_returns_scrubbed_full_dicts(self, hass):
        hass.states.async_set("light.kitchen", "on", {})
        now = utcnow()
        states = [_FakeState("on", now), _FakeState("off", now)]
        with self._patched(states):
            content, _, _ = await _call(
                "get_history",
                {"entity_id": "light.kitchen", "start_time": "24h", "mode": "significant_states"},
                _token(), hass,
            )
        body = _json(content)
        assert body["mode"] == "significant_states"
        assert len(body["history"]) == 2
        assert "attributes" in body["history"][0]

    async def test_significant_states_refuses_more_than_seven_days_before_query(self, hass):
        hass.states.async_set("light.kitchen", "on", {})
        with patch("homeassistant.components.recorder.get_instance") as get_instance:
            content, outcome, _ = await _call(
                "get_history",
                {"entity_id": "light.kitchen", "start_time": "8d", "mode": "significant_states"},
                _token(), hass,
            )
        assert outcome == "invalid_request"
        assert "at most 7 days" in content["content"][0]["text"]
        get_instance.assert_not_called()

    async def test_legacy_modes_are_breaking_errors(self, hass):
        hass.states.async_set("light.kitchen", "on", {})
        content, outcome, _ = await _call(
            "get_history", {"entity_id": "light.kitchen", "mode": "raw"}, _token(), hass,
        )
        assert outcome == "invalid_request"
        assert "Invalid mode" in content["content"][0]["text"]

    async def test_inaccessible_entity_not_found(self, hass):
        content, outcome, _ = await _call(
            "get_history", {"entity_id": "sensor.secret", "start_time": "24h"}, _token(), hass)
        assert outcome in ("not_found", "denied")


# --- get_automation_traces ---

class _FakeTrace:
    def __init__(self, run_id="run1", start="2026-01-01T00:00:00+00:00", not_triggered=False):
        self.run_id = run_id
        self._start = start
        self.not_triggered = not_triggered

    def as_short_dict(self):
        short = {
            "run_id": self.run_id, "state": "stopped", "script_execution": "finished",
            "last_step": "action/0", "timestamp": {"start": self._start},
        }
        if self.not_triggered:
            short["not_triggered"] = True
        return short

    def as_dict(self):
        return {**self.as_short_dict(), "trace": {"trigger/0": [{"path": "trigger/0"}]}}


def _trace_buckets(runs=None, not_triggered=None):
    """A real TraceBuckets, the shape HA 2026.7+ stores per automation.

    Built from the installed HA rather than hand-rolled: the previous fixture
    hard-coded the pre-2026.7 dict shape, so the suite passed while every live
    automation that had actually run returned an internal error.
    """
    from homeassistant.components.trace.models import TraceBuckets
    from homeassistant.util.limited_size_dict import LimitedSizeDict

    run_bucket = LimitedSizeDict(size_limit=10)
    run_bucket.update({t.run_id: t for t in (runs or [])})
    nt_bucket = LimitedSizeDict(size_limit=10)
    nt_bucket.update({t.run_id: t for t in (not_triggered or [])})
    return TraceBuckets(runs=run_bucket, not_triggered=nt_bucket)


@pytest.fixture
def auto_env(hass: HomeAssistant):
    from homeassistant.components.trace.const import DATA_TRACE
    entry = MockConfigEntry(domain="test_integration", entry_id="e1")
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    auto = ent_reg.async_get_or_create(
        "automation", "test_integration", "auto_uid_1",
        config_entry=entry, suggested_object_id="morning",
    )
    hass.states.async_set(auto.entity_id, "on", {})
    hass.data[DATA_TRACE] = {"automation.auto_uid_1": _trace_buckets(runs=[_FakeTrace()])}
    return {"entity_id": auto.entity_id}


def _auto_token(cap_traces="allow"):
    tree = PermissionTree(domains={"automation": PermissionNode(state="GREEN")})
    return _token(permissions=tree, cap_traces=cap_traces)


class TestGetAutomationTraces:
    async def test_deny_without_cap(self, hass, auto_env):
        _, outcome, _ = await _call("get_automation_traces", {"automation_id": "automation.morning"}, _auto_token("deny"), hass)
        assert outcome == "denied"

    async def test_list_traces(self, hass, auto_env):
        content, outcome, _ = await _call("get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["count"] == 1
        assert body["traces"][0]["run_id"] == "run1"

    async def test_specific_run_summary(self, hass, auto_env):
        content, _, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning", "run_id": "run1", "summary": True}, _auto_token(), hass)
        body = _json(content)
        assert body["script_execution"] == "finished"
        assert "trace" not in body  # summary drops the heavy step tree

    async def test_unknown_automation_not_found(self, hass, auto_env):
        _, outcome, _ = await _call("get_automation_traces", {"automation_id": "automation.ghost"}, _auto_token(), hass)
        assert outcome == "not_found"

    async def test_list_traces_carries_no_warnings_when_trace_loaded(self, hass, auto_env):
        content, _, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert "warnings" not in _json(content)

    async def test_unknown_run_id_is_not_found_not_an_error(self, hass, auto_env):
        # Live-found: this raised AttributeError before the TraceBuckets fix,
        # surfacing as "Internal error" for every automation that had ever run.
        _, outcome, _ = await _call(
            "get_automation_traces",
            {"automation_id": "automation.morning", "run_id": "no-such-run"},
            _auto_token(), hass)
        assert outcome == "not_found"


class TestTraceStorageShapes:
    """HA 2026.7 replaced the per-automation dict with TraceBuckets.

    Phoenix supports HA 2025.2+, so both shapes must read correctly, and the
    not-triggered bucket (new in the same change) must be visible: "why did it
    not fire" is the usual reason to read traces at all.
    """

    def _set_bucket(self, hass, bucket):
        from homeassistant.components.trace.const import DATA_TRACE
        hass.data[DATA_TRACE] = {"automation.auto_uid_1": bucket}

    async def test_not_triggered_traces_are_listed_and_labeled(self, hass, auto_env):
        self._set_bucket(hass, _trace_buckets(
            runs=[_FakeTrace(run_id="ran", start="2026-01-01T00:00:00+00:00")],
            not_triggered=[_FakeTrace(run_id="skipped", start="2026-01-02T00:00:00+00:00",
                                      not_triggered=True)],
        ))
        content, outcome, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["count"] == 2
        by_id = {t["run_id"]: t for t in body["traces"]}
        assert by_id["skipped"]["not_triggered"] is True
        assert "not_triggered" not in by_id["ran"]

    async def test_run_id_lookup_finds_a_not_triggered_trace(self, hass, auto_env):
        self._set_bucket(hass, _trace_buckets(
            not_triggered=[_FakeTrace(run_id="skipped", not_triggered=True)]))
        content, outcome, _ = await _call(
            "get_automation_traces",
            {"automation_id": "automation.morning", "run_id": "skipped"},
            _auto_token(), hass)
        assert outcome == "allowed"
        assert _json(content)["run_id"] == "skipped"

    async def test_legacy_dict_shape_still_reads(self, hass, auto_env):
        # Pre-2026.7 HA stored a plain LimitedSizeDict of run_id -> trace.
        self._set_bucket(hass, {"run1": _FakeTrace(run_id="run1")})
        content, outcome, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert outcome == "allowed"
        assert _json(content)["traces"][0]["run_id"] == "run1"

    async def test_legacy_dict_shape_run_id_lookup(self, hass, auto_env):
        self._set_bucket(hass, {"run1": _FakeTrace(run_id="run1")})
        content, outcome, _ = await _call(
            "get_automation_traces",
            {"automation_id": "automation.morning", "run_id": "run1"},
            _auto_token(), hass)
        assert outcome == "allowed"
        assert _json(content)["run_id"] == "run1"

    async def test_warns_when_trace_component_not_loaded(self, hass, auto_env):
        """No traces because the component is absent reads identically to never-ran."""
        from homeassistant.components.trace.const import DATA_TRACE
        hass.data.pop(DATA_TRACE)
        content, outcome, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["count"] == 0
        assert body["warnings"] == ["The trace component is not loaded; trace history is unavailable."]

    async def test_no_warning_when_loaded_but_automation_never_ran(self, hass, auto_env):
        from homeassistant.components.trace.const import DATA_TRACE
        hass.data[DATA_TRACE] = {}
        content, outcome, _ = await _call(
            "get_automation_traces", {"automation_id": "automation.morning"}, _auto_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["count"] == 0
        assert "warnings" not in body


# --- get_system_health / check_config ---

class TestDiagnostics:
    async def test_system_health_deny(self, hass):
        _, outcome, _ = await _call("get_system_health", {}, _token(cap_diagnostics="deny"), hass)
        assert outcome == "denied"

    async def test_system_health_returns_version(self, hass):
        content, outcome, _ = await _call("get_system_health", {}, _token(cap_diagnostics="allow"), hass)
        assert outcome == "allowed"
        assert _json(content)["home_assistant_version"]

    async def test_system_health_redacts_integration_secrets(self, hass):
        # Per-integration health values are arbitrary; secret-keyed values and
        # URL-embedded credentials must be scrubbed before reaching the model.
        info = {
            "cloud": {"api_key": "supersecret", "can_reach_server": "ok"},
            "broker": {"url": "https://admin:hunter2@mqtt.local/x"},
        }
        with patch("homeassistant.components.system_health.get_info", AsyncMock(return_value=info)):
            content, outcome, _ = await _call("get_system_health", {}, _token(cap_diagnostics="allow"), hass)
        assert outcome == "allowed"
        body = _json(content)
        text = json.dumps(body)
        assert "supersecret" not in text          # sensitive-keyed value redacted
        assert "hunter2" not in text              # URL credentials scrubbed
        # Benign diagnostic values are preserved.
        assert body["integrations"]["cloud"]["can_reach_server"] == "ok"

    async def test_system_health_scrubs_network_topology(self, hass):
        # Integration diagnostics can disclose LAN IPs, hostnames-in-URLs, and
        # filesystem paths that Phoenix MCP withholds from agents elsewhere (get_config).
        info = {
            "router": {"host": "192.168.1.50", "gateway": "10.0.0.1"},
            "service": {"endpoint": "http://homeassistant.local:8123/api"},
            "store": {"path": "/config/.storage/secret_store", "status": "ok"},
            "version": {"installed": "4.8.0.1"},  # public-IP-shaped; must NOT scrub
        }
        with patch("homeassistant.components.system_health.get_info", AsyncMock(return_value=info)):
            content, outcome, _ = await _call("get_system_health", {}, _token(cap_diagnostics="allow"), hass)
        assert outcome == "allowed"
        text = json.dumps(_json(content))
        assert "192.168.1.50" not in text         # private IP scrubbed
        assert "10.0.0.1" not in text              # private IP scrubbed
        assert "homeassistant.local" not in text   # URL host scrubbed
        assert "/config/.storage" not in text      # filesystem path scrubbed
        assert "ok" in text                         # benign value preserved
        assert "4.8.0.1" in text                    # version string NOT a private IP, preserved

    async def test_check_config_deny(self, hass):
        _, outcome, _ = await _call("check_config", {}, _token(cap_diagnostics="deny"), hass)
        assert outcome == "denied"

    async def test_check_config_valid(self, hass):
        fake = MagicMock(errors=[], warnings=[])
        with patch("homeassistant.helpers.check_config.async_check_ha_config_file", AsyncMock(return_value=fake)):
            content, outcome, _ = await _call("check_config", {}, _token(cap_diagnostics="allow"), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["valid"] is True
        assert body["errors"] == []

    async def test_check_config_reports_errors(self, hass):
        err = MagicMock(message="bad yaml", domain="light")
        fake = MagicMock(errors=[err], warnings=[])
        with patch("homeassistant.helpers.check_config.async_check_ha_config_file", AsyncMock(return_value=fake)):
            content, _, _ = await _call("check_config", {}, _token(cap_diagnostics="allow"), hass)
        body = _json(content)
        assert body["valid"] is False
        assert body["errors"][0]["message"] == "bad yaml"
