"""Tests for backup (list/create) and Lovelace dashboard CRUD MCP tools.

These dispatch through ws_dispatch.async_ws_command (covered end-to-end in
test_ws_dispatch). Here it is patched so we verify Phoenix MCP's wiring: the gates, the
command names/payloads, defaults, and validation. restore_backup does not exist.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.tools import lovelace as lovelace_tools
from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError, async_ws_command


def _token(**caps) -> TokenRecord:
    base = {"cap_backup": "allow", "cap_lovelace_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=PermissionTree(), **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass):
    return await _call_tool(name, args, token, hass, MagicMock())


def _patch_ws(return_value):
    return patch.object(mcp_view, "async_ws_command", new=AsyncMock(return_value=return_value))


def _patch_ws_map(mapping, default=None):
    """Patch async_ws_command to return per-command values."""
    async def _fn(_hass, command, payload=None, **_kw):
        return mapping.get(command, default)
    return patch.object(mcp_view, "async_ws_command", new=AsyncMock(side_effect=_fn))


def _patch_lovelace_ws(return_value):
    """Patch the dashboard tools' own async_ws_command.

    The dashboard surface lives in tools/lovelace.py, so patching mcp_view's
    binding would apply to a name these handlers never read - and the mock
    would simply go unused rather than failing.
    """
    return patch.object(lovelace_tools, "async_ws_command", new=AsyncMock(return_value=return_value))


class TestRestoreRemoved:
    def test_no_restore_backup_tool(self):
        names = {d["name"] for d in mcp_view._SYSTEM_TOOL_DEFS}
        assert "restore_backup" not in names
        assert "restore_backup" not in _EXECUTOR_REGISTRY

    def test_backup_dashboard_executors_registered(self):
        for name in ("create_backup", "create_dashboard", "edit_dashboard", "delete_dashboard"):
            assert name in _EXECUTOR_REGISTRY


class TestBackup:
    async def test_list_deny(self, hass):
        _, outcome, _ = await _call("list_backups", {}, _token(cap_backup="deny"), hass)
        assert outcome == "denied"

    async def test_list_projects_and_sorts(self, hass):
        # Backup lists use compact dicts, newest first.
        mapping = {
            "backup/info": {"backups": [
                {"backup_id": "b1", "name": "Old", "date": "2025-01-01T00:00:00+00:00",
                 "database_included": True, "homeassistant_version": "2026.1.0",
                 "agents": {"hassio.local": {"protected": False, "size": 100}}},
                {"backup_id": "b2", "name": "New", "date": "2026-06-01T00:00:00+00:00",
                 "database_included": False, "homeassistant_version": None,
                 "agents": {"hassio.local": {"protected": False, "size": 200}}},
            ]},
            "backup/agents/info": {"agents": [{"agent_id": "hassio.local"}]},
        }
        with _patch_ws_map(mapping) as m:
            content, outcome, _ = await _call("list_backups", {}, _token(), hass)
        assert outcome == "allowed"
        assert "backup/info" in [c.args[1] for c in m.await_args_list]
        body = _json(content)
        assert body["total"] == 2
        assert body["backups"][0]["backup_id"] == "b2"  # newest first
        assert body["backups"][0]["size"] == 200
        assert isinstance(body["backups"][0], dict)  # not a repr string
        assert body["available_agents"] == ["hassio.local"]

    async def test_list_limit(self, hass):
        # Large backup lists are capped.
        backs = [{"backup_id": f"b{i}", "name": str(i), "date": f"2026-01-{i:02d}T00:00:00+00:00", "agents": {}}
                 for i in range(1, 6)]
        mapping = {"backup/info": {"backups": backs}, "backup/agents/info": {"agents": []}}
        with _patch_ws_map(mapping):
            content, outcome, _ = await _call("list_backups", {"limit": 2}, _token(), hass)
        body = _json(content)
        assert body["total"] == 5
        assert body["returned"] == 2
        assert len(body["backups"]) == 2

    async def test_create_default_agent_autodetected(self, hass):
        # Default backup agent comes from backup/agents/info.
        mapping = {
            "backup/agents/info": {"agents": [{"agent_id": "hassio.local"}]},
            "backup/generate": {"backup_job_id": "abc"},
        }
        with _patch_ws_map(mapping) as m:
            content, outcome, _ = await _call("create_backup", {"name": "Nightly"}, _token(), hass)
        assert outcome == "allowed"
        gen = [c for c in m.await_args_list if c.args[1] == "backup/generate"][0]
        assert gen.args[2]["agent_ids"] == ["hassio.local"]  # not backup.local
        assert gen.args[2]["name"] == "Nightly"
        assert _json(content)["backup_job_id"] == "abc"  # clean field, not a NewBackup repr

    async def test_create_no_agents_errors(self, hass):
        mapping = {"backup/agents/info": {"agents": []}, "backup/generate": {"backup_job_id": "x"}}
        with _patch_ws_map(mapping):
            _, outcome, _ = await _call("create_backup", {}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_create_explicit_agent_honored(self, hass):
        with _patch_ws({"backup_job_id": "z"}) as m:
            _, outcome, _ = await _call("create_backup", {"agent_ids": ["my.agent"]}, _token(), hass)
        assert outcome == "allowed"
        gen = [c for c in m.await_args_list if c.args[1] == "backup/generate"][0]
        assert gen.args[2]["agent_ids"] == ["my.agent"]

    async def test_create_deny(self, hass):
        _, outcome, _ = await _call("create_backup", {}, _token(cap_backup="deny"), hass)
        assert outcome == "denied"


class TestBackupAgentReadFailure:
    """A failed backup/agents/info must not read as "this install has no agents"."""

    def _patch_agents_fail(self):
        async def _fn(_hass, command, payload=None, **_kw):
            if command == "backup/agents/info":
                raise WsDispatchError("socket gone")
            if command == "backup/info":
                return {"backups": []}
            return {"backup_job_id": "x"}
        return patch.object(mcp_view, "async_ws_command", new=AsyncMock(side_effect=_fn))

    async def test_list_backups_warns_and_still_succeeds(self, hass):
        with self._patch_agents_fail():
            content, outcome, _ = await _call("list_backups", {}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["available_agents"] == []
        assert any("backup agents" in w for w in body["warnings"])

    async def test_list_backups_clean_read_has_no_warnings_key(self, hass):
        mapping = {
            "backup/info": {"backups": []},
            "backup/agents/info": {"agents": [{"agent_id": "backup.local"}]},
        }
        with _patch_ws_map(mapping):
            content, outcome, _ = await _call("list_backups", {}, _token(), hass)
        assert outcome == "allowed"
        assert "warnings" not in _json(content)

    async def test_create_backup_reports_the_read_failure_not_no_agents(self, hass):
        with self._patch_agents_fail():
            content, outcome, _ = await _call("create_backup", {}, _token(), hass)
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "Failed to determine available backup agents" in text
        assert "No backup agents are available" not in text

    async def test_create_backup_never_dispatches_generate_on_read_failure(self, hass):
        with self._patch_agents_fail() as m:
            await _call("create_backup", {}, _token(), hass)
        assert not [c for c in m.await_args_list if c.args[1] == "backup/generate"]


class TestDashboard:
    async def test_list(self, hass):
        with _patch_lovelace_ws([{"url_path": "lovelace-x", "title": "X"}]) as m:
            content, outcome, _ = await _call("list_dashboards", {}, _token(), hass)
        assert outcome == "allowed"
        assert m.await_args.args[1] == "lovelace/dashboards/list"

    async def test_create(self, hass):
        with _patch_lovelace_ws({"id": "d1", "url_path": "lovelace-new", "title": "New"}) as m:
            content, outcome, _ = await _call(
                "create_dashboard", {"config": {"url_path": "lovelace-new", "title": "New"}}, _token(), hass)
        assert outcome == "allowed"
        assert m.await_args.args[1] == "lovelace/dashboards/create"
        assert m.await_args.args[2]["url_path"] == "lovelace-new"

    async def test_create_empty_config(self, hass):
        _, outcome, _ = await _call("create_dashboard", {"config": {}}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_edit(self, hass):
        with _patch_lovelace_ws({"id": "d1", "title": "Renamed"}) as m:
            content, outcome, _ = await _call(
                "edit_dashboard", {"dashboard_id": "d1", "config": {"title": "Renamed"}}, _token(), hass)
        assert outcome == "allowed"
        assert m.await_args.args[1] == "lovelace/dashboards/update"
        assert m.await_args.args[2] == {"dashboard_id": "d1", "title": "Renamed"}

    async def test_edit_existing_hyphenless_dashboard(self, hass, hass_admin_user):
        """An existing single-word dashboard remains updateable."""
        assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
        seeded = await async_ws_command(
            hass,
            "lovelace/dashboards/create",
            {"url_path": "map", "title": "Map", "allow_single_word": True},
        )
        assert seeded["url_path"] == "map"

        content, outcome, _ = await _call(
            "edit_dashboard",
            {"dashboard_id": seeded["id"], "config": {"title": "Updated map"}},
            _token(),
            hass,
        )

        assert outcome == "allowed"
        assert "Updated map" in content["content"][0]["text"]
        dashboards = await async_ws_command(hass, "lovelace/dashboards/list", {})
        updated = next(item for item in dashboards if item["url_path"] == "map")
        assert updated["title"] == "Updated map"

    async def test_edit_missing_id(self, hass):
        _, outcome, _ = await _call("edit_dashboard", {"dashboard_id": "", "config": {"title": "x"}}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_delete(self, hass):
        with _patch_lovelace_ws(None) as m:
            content, outcome, _ = await _call("delete_dashboard", {"dashboard_id": "d1"}, _token(), hass)
        assert outcome == "allowed"
        assert m.await_args.args[1] == "lovelace/dashboards/delete"
        assert m.await_args.args[2] == {"dashboard_id": "d1"}

    async def test_deny(self, hass):
        _, outcome, _ = await _call("create_dashboard", {"config": {"url_path": "x"}}, _token(cap_lovelace_write="deny"), hass)
        assert outcome == "denied"
