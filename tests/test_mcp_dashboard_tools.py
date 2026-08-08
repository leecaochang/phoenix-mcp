"""Tests for the dashboard config tools (get/set_dashboard_config) + versioning.

set_dashboard_config writes a storage-mode dashboard's view/card layout (Confirm-
gated on cap_lovelace_write) and is versioned; get_dashboard_config reads it back
with out-of-scope entity ids redacted. Both go through the ws_dispatch lovelace
helpers, which use the lovelace integration's LovelaceConfig objects directly.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import REDACTION_SENTINEL
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.mcp_view import _call_tool, async_restore_version
from custom_components.phoenix_mcp.tools import lovelace as lovelace_tools
from custom_components.phoenix_mcp.tools.lovelace import (
    _build_diff_set_dashboard_config,
    _execute_set_dashboard_config,
)
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import (
    WsDashboardNotFoundError,
    WsDispatchError,
    async_get_lovelace_config,
)


def _token(tree: PermissionTree | None = None, **caps) -> TokenRecord:
    base = {"cap_lovelace_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=tree or PermissionTree(domains={}), **base,
    )


def _data() -> tuple[PhoenixData, VersionStore]:
    versions = VersionStore()
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)
    return data, versions


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass, data=None):
    return await _call_tool(name, args, token, hass, data if data is not None else MagicMock())


@pytest.fixture
async def lovelace_env(hass: HomeAssistant):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
    entry = MockConfigEntry(domain="test_integration", entry_id="e1")
    entry.add_to_hass(hass)
    e = er.async_get(hass).async_get_or_create(
        "light", "test_integration", "uid_k", config_entry=entry, suggested_object_id="kitchen")
    hass.states.async_set(e.entity_id, "on", {})
    hass.states.async_set("sensor.secret", "1", {})  # out of scope
    return hass, e.entity_id


class TestGetDashboardConfig:
    async def test_deny_without_cap(self, hass, lovelace_env):
        h, _light = lovelace_env
        _, outcome, _ = await _call("get_dashboard_config", {}, _token(cap_lovelace_write="deny"), h)
        assert outcome == "denied"

    async def test_autogen_returns_not_found(self, hass, lovelace_env):
        h, _light = lovelace_env
        # The default dashboard has no stored config until something is saved.
        _, outcome, _ = await _call("get_dashboard_config", {}, _token(), h)
        assert outcome == "not_found"

    async def test_reads_and_redacts_out_of_scope_entities(self, hass, lovelace_env):
        h, light = lovelace_env
        token = _token(tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}))
        data, _v = _data()
        cfg = {"views": [{"cards": [{"type": "entities", "entities": [light, "sensor.secret"]}]}]}
        await _call("set_dashboard_config", {"config": cfg}, token, h, data)

        content, outcome, _ = await _call("get_dashboard_config", {}, token, h)
        assert outcome == "allowed"
        ents = _json(content)["config"]["views"][0]["cards"][0]["entities"]
        assert light in ents  # in scope, kept
        assert "sensor.secret" not in ents and "<redacted>" in ents  # out of scope, redacted


class TestSetDashboardConfig:
    async def test_deny_without_cap(self, hass, lovelace_env):
        h, _light = lovelace_env
        _, outcome, _ = await _call(
            "set_dashboard_config", {"config": {"views": []}}, _token(cap_lovelace_write="deny"), h)
        assert outcome == "denied"

    async def test_writes_and_records_version(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, versions = _data()
        cfg = {"views": [{"title": "A"}]}
        _c, outcome, _ = await _call("set_dashboard_config", {"config": cfg}, token, h, data)
        assert outcome == "allowed"
        assert await async_get_lovelace_config(h, None) == cfg  # persisted

        hist = versions.list_for("dashboard", "lovelace")  # default dashboard keyed "lovelace"
        assert len(hist) == 1
        assert hist[0].action == "create" and hist[0].before is None and hist[0].after == cfg

    async def test_invalid_config_confirm_mode_rejected_before_pending(self, hass, lovelace_env):
        """A non-dict config must fail before a pending approval is created,
        even under confirm mode, otherwise it sails through as a false pending
        that can only fail once an admin approves it."""
        h, _light = lovelace_env
        _, outcome, _ = await _call(
            "set_dashboard_config", {"config": "not-a-dict"}, _token(cap_lovelace_write="confirm"), h)
        assert outcome == "invalid_request"

    async def test_redacted_read_cannot_be_written_back(self, hass, lovelace_env):
        """The full-layout writer must not persist a lossy read's placeholder."""
        h, light = lovelace_env
        token = _token(tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}))
        data, _versions = _data()
        original = {
            "views": [{"cards": [{"type": "entities", "entities": [light, "sensor.secret"]}]}]
        }
        await _call("set_dashboard_config", {"config": original}, token, h, data)
        read = _json((await _call("get_dashboard_config", {}, token, h))[0])
        assert REDACTION_SENTINEL in read["config"]["views"][0]["cards"][0]["entities"]

        content, outcome, _ = await _call(
            "set_dashboard_config",
            {"config": read["config"], "expected_hash": read["content_hash"]},
            token, h, data,
        )

        assert outcome == "invalid_request"
        assert "views[0].cards[0].entities[1]" in content["content"][0]["text"]
        assert "patch_dashboard" in content["content"][0]["text"]
        assert "individual card tools" in content["content"][0]["text"]
        assert await async_get_lovelace_config(h, None) == original

    async def test_redaction_guard_runs_before_confirm_gate(self, hass, lovelace_env):
        """A lossy payload must fail instead of creating a doomed approval."""
        h, _light = lovelace_env
        config = {"views": [{"cards": [{"entity": REDACTION_SENTINEL}]}]}
        with patch.object(lovelace_tools, "_gate", new=AsyncMock()) as gate:
            _, outcome, _ = await _call(
                "set_dashboard_config", {"config": config},
                _token(cap_lovelace_write="confirm"), h,
            )
        assert outcome == "invalid_request"
        gate.assert_not_awaited()

    async def test_executor_rechecks_redaction_guard(self, hass, lovelace_env):
        """Approval-time execution must reject a sentinel introduced after gating."""
        h, _light = lovelace_env
        token = _token()
        data, _versions = _data()
        original = {"views": [{"title": "A"}]}
        await _call("set_dashboard_config", {"config": original}, token, h, data)

        _, outcome, _ = await _execute_set_dashboard_config(
            {"config": {"views": [{"title": REDACTION_SENTINEL}]}}, token, h, data,
        )

        assert outcome == "invalid_request"
        assert await async_get_lovelace_config(h, None) == original

    async def test_second_set_is_an_edit(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, versions = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        await _call("set_dashboard_config", {"config": {"views": [{"title": "B"}]}}, token, h, data)
        hist = versions.list_for("dashboard", "lovelace")
        assert [v.action for v in hist] == ["edit", "create"]
        assert hist[0].before == {"views": [{"title": "A"}]} and hist[0].after == {"views": [{"title": "B"}]}


class TestDashboardContentHash:
    """Optimistic-concurrency (compare-and-swap) guard on set_dashboard_config."""

    async def test_get_reports_content_hash(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        cfg = {"views": [{"title": "A"}]}
        await _call("set_dashboard_config", {"config": cfg}, token, h, data)
        content, _, _ = await _call("get_dashboard_config", {}, token, h)
        assert _json(content)["content_hash"] == content_hash(cfg)

    async def test_matching_hash_writes(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        got = _json((await _call("get_dashboard_config", {}, token, h))[0])
        _, outcome, _ = await _call(
            "set_dashboard_config",
            {"config": {"views": [{"title": "B"}]}, "expected_hash": got["content_hash"]},
            token, h, data)
        assert outcome == "allowed"
        assert await async_get_lovelace_config(h, None) == {"views": [{"title": "B"}]}

    async def test_stale_hash_conflicts_and_does_not_write(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        _, outcome, _ = await _call(
            "set_dashboard_config",
            {"config": {"views": [{"title": "B"}]}, "expected_hash": content_hash({"views": ["stale"]})},
            token, h, data)
        assert outcome == "invalid_request"
        assert await async_get_lovelace_config(h, None) == {"views": [{"title": "A"}]}  # untouched

    async def test_hash_is_key_order_independent(self, hass, lovelace_env):
        # Canonical (sorted-key) hashing: a reordered-key config must produce the
        # same hash, so re-serialization never causes a false conflict.
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    async def test_expected_hash_on_absent_dashboard_conflicts(self, hass, lovelace_env):
        # A fresh default dashboard has no stored config (get returns not_found).
        # An expected_hash for it therefore cannot match: refuse rather than write.
        h, _light = lovelace_env
        _, outcome, _ = await _call(
            "set_dashboard_config",
            {"config": {"views": [{"title": "X"}]}, "expected_hash": content_hash({"views": []})},
            _token(), h, _data()[0])
        assert outcome == "invalid_request"
        assert await async_get_lovelace_config(h, None) is None  # nothing stored

    async def test_executor_stale_hash_conflict_at_apply_time(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        _, outcome, _ = await _execute_set_dashboard_config(
            {"config": {"views": [{"title": "B"}]}, "expected_hash": content_hash({"views": ["stale"]})},
            token, h, data)
        assert outcome == "invalid_request"
        assert await async_get_lovelace_config(h, None) == {"views": [{"title": "A"}]}


class TestDashboardReadFailureIsNotAbsence:
    """An unreadable lovelace must never be mistaken for an absent dashboard.

    Absence is a legitimate state the write paths act on (before=None, recorded as
    a "create", and any expected_hash conflicts). A transport or lovelace failure
    reaching the same branch would let a write land against a phantom empty prior.
    """

    def _patch_read(self, exc):
        # Patch the module that CALLS it: the dashboard tools live in
        # tools/lovelace.py and never read mcp_view's binding.
        return patch.object(
            lovelace_tools, "async_get_lovelace_config", new=AsyncMock(side_effect=exc))

    async def test_cas_pregate_errors_on_read_failure(self, hass, lovelace_env):
        h, _light = lovelace_env
        with self._patch_read(WsDispatchError("lovelace is not loaded")):
            content, outcome, _ = await _call(
                "set_dashboard_config",
                {"config": {"views": []}, "expected_hash": content_hash({"views": []})},
                _token(), h, _data()[0])
        assert outcome == "invalid_request"
        assert "Could not read dashboard" in content["content"][0]["text"]

    async def test_cas_pregate_treats_not_found_as_absence(self, hass, lovelace_env):
        # Unknown dashboard: absence is real, so the CAS guard conflicts (as before).
        h, _light = lovelace_env
        with self._patch_read(WsDashboardNotFoundError("unknown dashboard")):
            _, outcome, _ = await _call(
                "set_dashboard_config",
                {"config": {"views": []}, "expected_hash": content_hash({"views": []})},
                _token(), h, _data()[0])
        assert outcome == "invalid_request"

    async def test_executor_errors_on_read_failure_and_does_not_write(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        with self._patch_read(WsDispatchError("socket gone")):
            content, outcome, _ = await _execute_set_dashboard_config(
                {"config": {"views": [{"title": "B"}]}}, token, h, data)
        assert outcome == "invalid_request"
        assert "Could not read dashboard" in content["content"][0]["text"]
        assert await async_get_lovelace_config(h, None) == {"views": [{"title": "A"}]}

    async def test_executor_still_creates_when_genuinely_absent(self, hass, lovelace_env):
        h, _light = lovelace_env
        data, versions = _data()
        with self._patch_read(WsDashboardNotFoundError("unknown dashboard")):
            _, outcome, _ = await _execute_set_dashboard_config(
                {"config": {"views": [{"title": "New"}]}}, _token(), h, data)
        assert outcome == "allowed"
        assert versions.list_recent()[0].action == "create"


class TestSetDashboardDiff:
    async def test_before_empty_for_new_dashboard(self, hass, lovelace_env):
        h, _light = lovelace_env
        diff = await _build_diff_set_dashboard_config(
            {"config": {"views": [{"title": "A"}]}}, _token(), h)
        assert diff["kind"] == "yaml_diff"
        assert diff["before"] is None  # nothing stored yet
        assert "A" in diff["after"]

    async def test_before_shows_current_layout_on_edit(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, _v = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        # The approval diff for a subsequent set must show the CURRENT layout as
        # "before" (regression: it used to hard-code before=None).
        diff = await _build_diff_set_dashboard_config(
            {"config": {"views": [{"title": "B"}]}}, token, h)
        assert diff["before"] is not None and "A" in diff["before"]
        assert "B" in diff["after"]

    async def test_before_redacts_out_of_scope_entities(self, hass, lovelace_env):
        h, light = lovelace_env
        token = _token(tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}))
        data, _v = _data()
        cfg = {"views": [{"cards": [{"type": "entities", "entities": [light, "sensor.secret"]}]}]}
        await _call("set_dashboard_config", {"config": cfg}, token, h, data)
        diff = await _build_diff_set_dashboard_config({"config": {"views": []}}, token, h)
        assert diff["before"] is not None
        assert "sensor.secret" not in diff["before"] and "<redacted>" in diff["before"]


class TestDashboardRestore:
    async def test_restore_reapplies_as_rollback(self, hass, lovelace_env):
        h, _light = lovelace_env
        token = _token()
        data, versions = _data()
        await _call("set_dashboard_config", {"config": {"views": [{"title": "A"}]}}, token, h, data)
        await _call("set_dashboard_config", {"config": {"views": [{"title": "B"}]}}, token, h, data)

        create_ver = versions.list_for("dashboard", "lovelace")[-1]  # the create (A)
        _r, outcome, _res = await async_restore_version(create_ver, "admin-1", h, data)
        assert outcome == "allowed"
        assert await async_get_lovelace_config(h, None) == {"views": [{"title": "A"}]}  # config restored

        latest = versions.list_for("dashboard", "lovelace")[0]
        assert latest.action == "rollback" and latest.approved_by_user_id == "admin-1"
