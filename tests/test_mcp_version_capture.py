"""Integration tests for configuration version capture.

Exercises the executor capture sites end-to-end with a real VersionStore for the
YAML-backed resources (automation, script, scene): create -> edit -> delete must
record one version each, with correct before/after. The other tool-test files pass
a MagicMock for data, so capture is a no-op there; here a real PhoenixData is supplied.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import MAX_DIFF_INLINE_BYTES, MAX_REQUEST_BODY_BYTES
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool, async_restore_version
from custom_components.phoenix_mcp.tools.authoring import _read_automations_yaml
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore


# The seeded configuration.yaml. set_yaml_config refuses a write that drops a
# top-level key without declaring it, so every write here stays a superset.
_SEED_YAML = (
    "automation: !include automations.yaml\n"
    "script: !include scripts.yaml\n"
    "scene: !include scenes.yaml\n"
)


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    """Authoring executors resolve configuration.yaml (yaml_includes); isolate
    from the shared testing config dir (which other tests pollute) and
    use the real HA-default include layout."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(_SEED_YAML, encoding="utf-8")
    return tmp_path


def _data() -> tuple[PhoenixData, VersionStore]:
    versions = VersionStore()
    data = PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions,
    )
    return data, versions


def _token(tree: PermissionTree | None = None, **caps) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=tree or PermissionTree(domains={}), **caps,
    )


def _text(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


class TestAutomationCapture:
    @pytest.fixture
    def env(self, hass: HomeAssistant):
        hass.services.async_register("automation", "reload", lambda call: None)
        return hass

    async def test_create_edit_delete_record_history(self, hass, env):
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        cfg = {
            "alias": "A",
            "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
            "action": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}],
        }
        created = _text((await _call_tool("create_automation", {"config": cfg}, token, hass, data))[0])
        aid = created["id"]

        await _call_tool(
            "edit_automation", {"automation_id": aid, "config": dict(cfg, alias="A2")}, token, hass, data)
        await _call_tool("delete_automation", {"automation_id": aid}, token, hass, data)

        history = versions.list_for("automation", aid)
        assert [v.action for v in history] == ["delete", "edit", "create"]  # newest first
        delete_rec, edit_rec, create_rec = history
        assert create_rec.before is None and create_rec.after.get("alias") == "A"
        assert create_rec.token_name == token.name
        assert edit_rec.before.get("alias") == "A" and edit_rec.after.get("alias") == "A2"
        assert delete_rec.before.get("alias") == "A2" and delete_rec.after is None


class TestScriptCapture:
    @pytest.fixture
    def env(self, hass: HomeAssistant):
        hass.services.async_register("script", "reload", lambda call: None)
        return hass

    async def test_create_edit_delete_record_history(self, hass, env):
        data, versions = _data()
        token = _token(cap_script_write="allow")
        sid = "phx_test_script"
        cfg = {"alias": "S", "sequence": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}]}

        await _call_tool("create_script", {"script_id": sid, "config": cfg}, token, hass, data)
        await _call_tool(
            "edit_script", {"script_id": sid, "config": dict(cfg, alias="S2")}, token, hass, data)
        await _call_tool("delete_script", {"script_id": sid}, token, hass, data)

        history = versions.list_for("script", sid)
        assert [v.action for v in history] == ["delete", "edit", "create"]
        delete_rec, edit_rec, create_rec = history
        assert create_rec.before is None and create_rec.after.get("alias") == "S"
        assert edit_rec.before.get("alias") == "S" and edit_rec.after.get("alias") == "S2"
        assert delete_rec.before.get("alias") == "S2" and delete_rec.after is None


class TestSceneCapture:
    @pytest.fixture
    def light_entity(self, hass: HomeAssistant) -> str:
        entry = MockConfigEntry(domain="test_integration", entry_id="e1")
        entry.add_to_hass(hass)
        e = er.async_get(hass).async_get_or_create(
            "light", "test_integration", "uid_k", config_entry=entry, suggested_object_id="kitchen")
        hass.states.async_set(e.entity_id, "on", {})
        hass.services.async_register("scene", "reload", lambda call: None)
        return e.entity_id

    async def test_create_edit_delete_record_history(self, hass, light_entity):
        data, versions = _data()
        tree = PermissionTree(domains={
            "light": PermissionNode(state="GREEN"), "scene": PermissionNode(state="GREEN"),
        })
        token = _token(tree=tree, cap_scene_write="allow", cap_registry_read="allow")

        created = _text((await _call_tool(
            "create_scene", {"config": {"name": "Movie", "entities": {light_entity: "on"}}},
            token, hass, data))[0])
        sid = created["id"]
        await _call_tool(
            "edit_scene", {"scene_id": sid, "config": {"name": "Movie2", "entities": {light_entity: "off"}}},
            token, hass, data)
        await _call_tool("delete_scene", {"scene_id": sid}, token, hass, data)

        history = versions.list_for("scene", sid)
        assert [v.action for v in history] == ["delete", "edit", "create"]
        delete_rec, edit_rec, create_rec = history
        assert create_rec.before is None and create_rec.after.get("name") == "Movie"
        assert edit_rec.before.get("name") == "Movie" and edit_rec.after.get("name") == "Movie2"
        assert delete_rec.before.get("name") == "Movie2" and delete_rec.after is None


class TestAutomationRestore:
    @pytest.fixture
    def env(self, hass: HomeAssistant):
        hass.services.async_register("automation", "reload", lambda call: None)
        return hass

    @staticmethod
    def _cfg(alias: str) -> dict:
        return {
            "alias": alias,
            "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
            "action": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}],
        }

    async def test_restore_existing_reapplies_as_rollback(self, hass, env):
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        created = _text((await _call_tool("create_automation", {"config": self._cfg("A")}, token, hass, data))[0])
        aid = created["id"]
        await _call_tool("edit_automation", {"automation_id": aid, "config": self._cfg("B")}, token, hass, data)

        create_ver = versions.list_for("automation", aid)[-1]  # oldest is the create
        _result, outcome, _r = await async_restore_version(create_ver, "admin-1", hass, data)
        assert outcome == "allowed"

        items = _read_automations_yaml(os.path.join(hass.config.config_dir, "automations.yaml"))
        assert next(a for a in items if a.get("id") == aid)["alias"] == "A"  # config restored

        latest = versions.list_for("automation", aid)[0]
        assert latest.action == "rollback"
        assert latest.approved_by_user_id == "admin-1"
        assert latest.after.get("alias") == "A"

    async def test_restore_before_side_undoes_change(self, hass, env):
        # An edit version holds before=A, after=B. Restoring side="before" re-applies
        # the prior config (A); side="after" re-applies the change (B).
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        created = _text((await _call_tool("create_automation", {"config": self._cfg("A")}, token, hass, data))[0])
        aid = created["id"]
        await _call_tool("edit_automation", {"automation_id": aid, "config": self._cfg("B")}, token, hass, data)

        edit_ver = versions.list_for("automation", aid)[0]  # newest is the edit (before A / after B)
        assert edit_ver.before.get("alias") == "A"
        assert edit_ver.after.get("alias") == "B"

        _result, outcome, _r = await async_restore_version(edit_ver, "admin-1", hass, data, side="before")
        assert outcome == "allowed"
        items = _read_automations_yaml(os.path.join(hass.config.config_dir, "automations.yaml"))
        assert next(a for a in items if a.get("id") == aid)["alias"] == "A"  # undone to before

    async def test_restore_missing_side_errors(self, hass, env):
        # Restoring the "before" of a create (before is None) is rejected cleanly.
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        created = _text((await _call_tool("create_automation", {"config": self._cfg("A")}, token, hass, data))[0])
        aid = created["id"]
        create_ver = versions.list_for("automation", aid)[0]
        result, outcome, _r = await async_restore_version(create_ver, "admin-1", hass, data, side="before")
        assert outcome == "invalid_request"
        assert result.get("isError") is True

    async def test_restore_deleted_recreates_in_place(self, hass, env):
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        created = _text((await _call_tool("create_automation", {"config": self._cfg("A")}, token, hass, data))[0])
        aid = created["id"]
        await _call_tool("delete_automation", {"automation_id": aid}, token, hass, data)

        delete_ver = versions.list_for("automation", aid)[0]  # newest is the delete
        result, outcome, _r = await async_restore_version(delete_ver, "admin-1", hass, data)
        assert outcome == "allowed"

        # Deleted automations are restored under their original ID.
        assert _text(result)["id"] == aid
        items = _read_automations_yaml(os.path.join(hass.config.config_dir, "automations.yaml"))
        assert sum(1 for a in items if a.get("id") == aid) == 1  # exactly one, in place

        latest = versions.list_for("automation", aid)[0]
        assert latest.action == "rollback"
        assert latest.approved_by_user_id == "admin-1"

    async def test_restore_deleted_is_idempotent(self, hass, env):
        # Repeated restores must update the original automation, not duplicate it.
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        created = _text((await _call_tool("create_automation", {"config": self._cfg("A")}, token, hass, data))[0])
        aid = created["id"]
        await _call_tool("delete_automation", {"automation_id": aid}, token, hass, data)

        delete_ver = versions.list_for("automation", aid)[0]
        await async_restore_version(delete_ver, "admin-1", hass, data)
        await async_restore_version(delete_ver, "admin-1", hass, data)

        items = _read_automations_yaml(os.path.join(hass.config.config_dir, "automations.yaml"))
        assert sum(1 for a in items if a.get("id") == aid) == 1  # still exactly one

    async def test_restore_version_with_no_config_errors(self, hass, env):
        # A record whose before and after are both None has nothing to restore.
        data, _versions = _data()

        class _Rec:
            resource_type = "automation"
            resource_id = "x"
            before = None
            after = None

        result, outcome, _r = await async_restore_version(_Rec(), "admin-1", hass, data)
        assert outcome == "invalid_request"
        assert result.get("isError") is True


class TestRestoreEndpoint:
    @pytest.fixture
    def env(self, hass: HomeAssistant):
        hass.services.async_register("automation", "reload", lambda call: None)
        return hass

    @staticmethod
    def _request(body: bytes = b"", content_length: int | None = None):
        from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

        user = MagicMock()
        user.is_admin = True
        user.id = "admin-7"
        state = {KEY_HASS_USER: user, KEY_AUTHENTICATED: True, "phoenix_mcp_rid": "rid"}
        req = MagicMock()
        req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
        req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
        req.content_length = content_length if content_length is not None else (len(body) or None)
        req.content = MagicMock()
        req.content.read = AsyncMock(return_value=body)
        return req

    async def _seed(self, hass):
        """Create automation alias A, edit to B; return (data, versions, aid)."""
        from custom_components.phoenix_mcp.const import DOMAIN

        data, versions = _data()
        hass.data[DOMAIN] = data
        token = _token(cap_automation_write="allow")
        cfg = {
            "alias": "A",
            "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
            "action": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}],
        }
        created = _text((await _call_tool("create_automation", {"config": cfg}, token, hass, data))[0])
        aid = created["id"]
        await _call_tool("edit_automation", {"automation_id": aid, "config": dict(cfg, alias="B")}, token, hass, data)
        return data, versions, aid

    def _alias(self, hass, aid):
        items = _read_automations_yaml(os.path.join(hass.config.config_dir, "automations.yaml"))
        return next(a for a in items if a.get("id") == aid)["alias"]

    async def test_post_restore_happy_path(self, hass, env):
        from custom_components.phoenix_mcp.admin_view import PhoenixAdminVersionRestoreView

        data, versions, aid = await self._seed(hass)
        create_ver = versions.list_for("automation", aid)[-1]

        view = PhoenixAdminVersionRestoreView()
        view.hass = hass
        resp = await view.post(self._request(), version_id=create_ver.id)
        assert resp.status == 200
        assert json.loads(resp.body)["restored"] is True

        assert self._alias(hass, aid) == "A"
        assert versions.list_for("automation", aid)[0].approved_by_user_id == "admin-7"

    async def test_post_restore_side_from_body_is_honored(self, hass, env):
        from custom_components.phoenix_mcp.admin_view import PhoenixAdminVersionRestoreView

        data, versions, aid = await self._seed(hass)
        edit_ver = versions.list_for("automation", aid)[0]  # before=A, after=B

        view = PhoenixAdminVersionRestoreView()
        view.hass = hass
        resp = await view.post(
            self._request(json.dumps({"side": "before"}).encode()), version_id=edit_ver.id,
        )
        assert resp.status == 200
        # Default side is "after" (B); only a parsed body lands on A.
        assert self._alias(hass, aid) == "A"

    async def test_post_restore_malformed_json_is_400_and_does_not_restore(self, hass, env):
        # A malformed body must not be treated as "no body": that silently
        # restored the default side, the opposite of what the admin asked for.
        from custom_components.phoenix_mcp.admin_view import PhoenixAdminVersionRestoreView

        data, versions, aid = await self._seed(hass)
        create_ver = versions.list_for("automation", aid)[-1]

        view = PhoenixAdminVersionRestoreView()
        view.hass = hass
        resp = await view.post(self._request(b'{"side": "befo'), version_id=create_ver.id)
        assert resp.status == 400
        assert self._alias(hass, aid) == "B"  # untouched

        resp = await view.post(self._request(b'["side", "before"]'), version_id=create_ver.id)
        assert resp.status == 400
        assert self._alias(hass, aid) == "B"

    async def test_post_restore_oversized_body_is_413(self, hass, env):
        from custom_components.phoenix_mcp.admin_view import PhoenixAdminVersionRestoreView

        data, versions, aid = await self._seed(hass)
        create_ver = versions.list_for("automation", aid)[-1]

        view = PhoenixAdminVersionRestoreView()
        view.hass = hass
        resp = await view.post(
            self._request(b"{}", content_length=MAX_REQUEST_BODY_BYTES + 1),
            version_id=create_ver.id,
        )
        assert resp.status == 413
        assert self._alias(hass, aid) == "B"


class TestRawWriteCapture:
    async def test_yaml_config_records_create_then_edit(self, hass):
        data, versions = _data()
        token = _token(cap_yaml_edit="allow")
        first_content = _SEED_YAML + "default_config:\n"
        second_content = first_content + "foo: bar\n"
        await _call_tool("set_yaml_config", {"content": first_content}, token, hass, data)
        await _call_tool("set_yaml_config", {"content": second_content}, token, hass, data)

        # The test env may or may not ship a configuration.yaml, so assert on the
        # before/after content chain rather than on create-vs-edit of the first write.
        history = versions.list_for("yaml_config", "configuration.yaml")
        assert len(history) == 2
        second, first = history  # newest first
        assert first.after["content"] == first_content
        assert second.before["content"] == first_content
        assert second.after["content"] == second_content

    async def test_patch_yaml_config_records_a_restorable_whole_file(self, hass):
        """A patch is a partial write; its version must not be a partial record.

        Asserted against the STORE and then round-tripped through restore rather
        than against the _record_version call, because that function swallows a
        failed record by design (history must never break a write), so a wiring
        mistake would otherwise look exactly like a passing test.
        """
        data, versions = _data()
        token = _token(cap_yaml_edit="allow")
        await _call_tool(
            "patch_yaml_config", {"key": "recorder", "content": "purge_keep_days: 10\n"},
            token, hass, data)
        rec = versions.list_for("yaml_config", "configuration.yaml")[0]
        assert rec.before["content"] == _SEED_YAML
        assert "purge_keep_days: 10" in rec.after["content"]
        assert "recorder" in rec.summary

        _r, outcome, _res = await async_restore_version(rec, "admin-1", hass, data, side="before")
        assert outcome == "allowed"
        with open(hass.config.path("configuration.yaml"), encoding="utf-8") as f:
            assert f.read() == _SEED_YAML

    async def test_write_file_records_create_then_edit(self, hass):
        data, versions = _data()
        token = _token(cap_filesystem="allow")
        rel = "www/phx_cap_test.js"
        target = os.path.join(hass.config.config_dir, "www", "phx_cap_test.js")
        if os.path.exists(target):  # the test config dir can persist across runs
            os.remove(target)
        await _call_tool("write_file", {"path": rel, "content": "v1"}, token, hass, data)
        await _call_tool("write_file", {"path": rel, "content": "v2"}, token, hass, data)

        history = versions.list_for("file", rel)
        assert [v.action for v in history] == ["edit", "create"]
        edit_rec, create_rec = history
        assert create_rec.before is None and create_rec.after["content"] == "v1"
        assert edit_rec.before["content"] == "v1" and edit_rec.after["content"] == "v2"

    async def test_oversized_content_stored_as_truncated_marker(self, hass):
        data, versions = _data()
        token = _token(cap_yaml_edit="allow")
        big = _SEED_YAML + "# " + "x" * (MAX_DIFF_INLINE_BYTES + 50)
        await _call_tool("set_yaml_config", {"content": big}, token, hass, data)
        rec = versions.list_for("yaml_config", "configuration.yaml")[0]
        assert rec.after["content"] is None
        assert rec.after["truncated"] is True
        assert rec.after["bytes"] == len(big.encode("utf-8"))


class TestRawWriteRestore:
    async def test_restore_yaml_reapplies_and_records_rollback(self, hass):
        data, versions = _data()
        token = _token(cap_yaml_edit="allow")
        content_a = _SEED_YAML + "a: 1\n"
        content_b = _SEED_YAML + "b: 2\n"
        await _call_tool("set_yaml_config", {"content": content_a}, token, hass, data)
        await _call_tool("set_yaml_config", {"content": content_b}, token, hass, data)
        create_ver = versions.list_for("yaml_config", "configuration.yaml")[-1]  # after == content_a

        result, _outcome, _res = await async_restore_version(create_ver, "admin-1", hass, data)
        assert result.get("isError") is not True

        # Restoring content_a over content_b DROPS the top-level "b", which an
        # ordinary write would be refused for; the restore path is exempt.
        with open(hass.config.path("configuration.yaml"), encoding="utf-8") as f:
            assert f.read() == content_a
        newest = versions.list_for("yaml_config", "configuration.yaml")[0]
        assert newest.action == "rollback"
        assert newest.approved_by_user_id == "admin-1"

    async def test_restore_file_reapplies(self, hass):
        data, versions = _data()
        token = _token(cap_filesystem="allow")
        rel = "www/phx_restore_test.js"
        await _call_tool("write_file", {"path": rel, "content": "first"}, token, hass, data)
        await _call_tool("write_file", {"path": rel, "content": "second"}, token, hass, data)
        create_ver = versions.list_for("file", rel)[-1]  # after == "first"

        await async_restore_version(create_ver, "admin-2", hass, data)
        with open(os.path.join(hass.config.config_dir, "www", "phx_restore_test.js"), encoding="utf-8") as f:
            assert f.read() == "first"
        assert versions.list_for("file", rel)[0].action == "rollback"

    async def test_restore_truncated_version_refused(self, hass):
        data, versions = _data()
        token = _token(cap_yaml_edit="allow")
        big = _SEED_YAML + "# " + "y" * (MAX_DIFF_INLINE_BYTES + 50)
        await _call_tool("set_yaml_config", {"content": big}, token, hass, data)
        rec = versions.list_for("yaml_config", "configuration.yaml")[0]
        result, outcome, _res = await async_restore_version(rec, "admin-3", hass, data)
        assert outcome == "invalid_request"
        assert "too large" in result["content"][0]["text"].lower()


class TestEntityRegistryWrite:
    @pytest.fixture
    def env(self, hass: HomeAssistant):
        entry = MockConfigEntry(domain="test_integration", entry_id="e_reg")
        entry.add_to_hass(hass)
        e = er.async_get(hass).async_get_or_create(
            "light", "test_integration", "uid_lamp", config_entry=entry, suggested_object_id="lamp")
        hass.states.async_set(e.entity_id, "on", {})
        area = ar.async_get(hass).async_create("Office")
        return e.entity_id, area.id

    @staticmethod
    def _token_rw():
        tree = PermissionTree(domains={"light": PermissionNode(state="GREEN")})
        return _token(tree=tree, cap_registry_write="allow", cap_registry_read="allow")

    async def test_set_entity_updates_and_versions(self, hass, env):
        eid, area_id = env
        data, versions = _data()
        res = _text((await _call_tool(
            "set_entity", {"entity_id": eid, "name": "Desk Lamp", "area_id": area_id},
            self._token_rw(), hass, data))[0])
        assert res["updated"]["name"] == "Desk Lamp"
        assert res["updated"]["area_id"] == area_id

        entry = er.async_get(hass).async_get(eid)
        assert entry.name == "Desk Lamp" and entry.area_id == area_id
        hist = versions.list_for("entity", eid)
        assert hist[0].action == "edit"
        assert hist[0].after["name"] == "Desk Lamp"
        assert hist[0].before["name"] != "Desk Lamp"

    async def test_set_entity_forbidden_without_cap(self, hass, env):
        eid, _area = env
        data, _ = _data()
        tree = PermissionTree(domains={"light": PermissionNode(state="GREEN")})
        token = _token(tree=tree, cap_registry_write="deny")
        _, outcome, _ = await _call_tool("set_entity", {"entity_id": eid, "name": "X"}, token, hass, data)
        assert outcome == "denied"

    async def test_deny_token_is_not_a_scope_oracle(self, hass, env):
        # A cap-denied token must get the byte-identical Forbidden body regardless
        # of whether the named entity is writable, out of scope, or nonexistent.
        eid, _area = env  # WRITE-accessible light entity
        hass.states.async_set("lock.front_door", "locked", {})  # exists, out of scope
        data, _ = _data()
        tree = PermissionTree(domains={"light": PermissionNode(state="GREEN")})
        token = _token(tree=tree, cap_registry_write="deny")
        bodies = []
        for target in (eid, "lock.front_door", "light.ghost_nonexistent"):
            content, outcome, _ = await _call_tool(
                "set_entity", {"entity_id": target, "name": "X"}, token, hass, data)
            assert outcome == "denied"
            bodies.append(content["content"][0]["text"])
        assert len(set(bodies)) == 1

    async def test_set_entity_read_only_denied(self, hass, env):
        eid, _area = env
        data, _ = _data()
        # YELLOW => READ only; registry edits require WRITE.
        tree = PermissionTree(domains={"light": PermissionNode(state="YELLOW")})
        token = _token(tree=tree, cap_registry_write="allow")
        _, outcome, _ = await _call_tool("set_entity", {"entity_id": eid, "name": "X"}, token, hass, data)
        assert outcome == "denied"

    async def test_set_entity_unknown_area_rejected(self, hass, env):
        eid, _area = env
        data, _ = _data()
        before = er.async_get(hass).async_get(eid).area_id
        content, outcome, _ = await _call_tool(
            "set_entity", {"entity_id": eid, "area_id": "no_such_area"}, self._token_rw(), hass, data)
        assert outcome == "invalid_request"
        assert "Unknown area_id" in content["content"][0]["text"]
        assert er.async_get(hass).async_get(eid).area_id == before

    @pytest.mark.parametrize(
        "area_id,message",
        [
            ("no_such_area", "Unknown area_id"),
            (["office"], "area_id must be a string"),
            ({"area": "office"}, "area_id must be a string"),
            (True, "area_id must be a string"),
        ],
    )
    async def test_set_entity_invalid_area_rejected_before_pending(
        self, hass, env, area_id, message
    ):
        """Rule 29: invalid registry IDs fail before an approval exists."""
        eid, _area = env
        data, _ = _data()
        gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
        with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
            content, outcome, _ = await _call_tool(
                "set_entity",
                {"entity_id": eid, "area_id": area_id},
                _token(
                    tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}),
                    cap_registry_write="confirm",
                ),
                hass,
                data,
            )

        assert outcome == "invalid_request"
        assert message in content["content"][0]["text"]
        gate.assert_not_awaited()

    @pytest.mark.parametrize(
        "unsupported",
        [
            {"label_id": "kitchen"},
            {"labels": ["kitchen"]},
            {"floor_id": "ground_floor"},
            {"device_id": "device-1"},
            {"category_id": "category-1"},
            {"categories": {"scope": "category-1"}},
            {"area_ids": ["office"]},
        ],
    )
    async def test_set_entity_unknown_only_arguments_rejected_before_pending(
        self, hass, env, unsupported
    ):
        """Unknown-only requests must not become no-op approvals."""
        eid, _area = env
        data, _ = _data()
        gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
        with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
            content, outcome, _ = await _call_tool(
                "set_entity",
                {"entity_id": eid, **unsupported},
                _token(
                    tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}),
                    cap_registry_write="confirm",
                ),
                hass,
                data,
            )

        assert outcome == "invalid_request"
        assert "Provide at least one of" in content["content"][0]["text"]
        gate.assert_not_awaited()

    async def test_set_entity_unknown_only_argument_revalidated_at_apply_time(self, hass, env):
        """Persisted approval arguments receive the same no-op check."""
        from custom_components.phoenix_mcp.mcp_view import _execute_set_entity

        eid, _area = env
        data, _ = _data()
        content, outcome, _ = await _execute_set_entity(
            {"entity_id": eid, "label_id": "kitchen"},
            self._token_rw(),
            hass,
            data,
        )

        assert outcome == "invalid_request"
        assert "Provide at least one of" in content["content"][0]["text"]

    async def test_set_entity_unknown_argument_is_ignored_with_supported_edit(self, hass, env):
        """Stray fields do not block a valid supported update."""
        eid, _area = env
        data, _ = _data()
        content, outcome, _ = await _call_tool(
            "set_entity",
            {"entity_id": eid, "name": "Desk Lamp", "label_id": "kitchen"},
            self._token_rw(),
            hass,
            data,
        )

        assert outcome == "allowed"
        assert json.loads(content["content"][0]["text"])["updated"]["name"] == "Desk Lamp"
        assert er.async_get(hass).async_get(eid).name == "Desk Lamp"

    async def test_set_entity_area_revalidated_after_precheck(self, hass, env):
        """An area deleted during an approval window is not written back."""
        from custom_components.phoenix_mcp.mcp_view import (
            _execute_set_entity,
            _registry_write_precheck,
        )

        eid, area_id = env
        token = self._token_rw()
        args = {"entity_id": eid, "area_id": area_id}
        assert _registry_write_precheck(args, token, hass, "set_entity") is None

        ar.async_get(hass).async_delete(area_id)
        data, _ = _data()
        _, outcome, _ = await _execute_set_entity(args, token, hass, data)

        assert outcome == "invalid_request"
        assert er.async_get(hass).async_get(eid).area_id is None

    async def test_delete_entity_removes_and_versions(self, hass, env):
        eid, _area = env
        data, versions = _data()
        res = _text((await _call_tool("delete_entity", {"entity_id": eid}, self._token_rw(), hass, data))[0])
        assert res["deleted"] is True
        assert er.async_get(hass).async_get(eid) is None
        hist = versions.list_for("entity", eid)
        assert hist[0].action == "delete"
        assert hist[0].after is None and hist[0].before is not None


def _summary_text(fields):
    """The English out of a _version_change_summary triple.

    The function returns summary/summary_key/summary_params now (the panel needs
    the key to localize it), so these tests read the English out. Asserting on
    that English is still the point: it is what a pre-upgrade record shows and
    what the generated catalog entry has to reproduce.
    """
    return None if fields is None else fields["summary"]


class TestVersionChangeSummary:
    """The one-line what-changed description stored on version records for
    resource types whose alias says nothing about the change itself."""

    def test_dashboard_card_count_movement(self):
        from custom_components.phoenix_mcp.tool_common import _version_change_summary

        before = {"views": [
            {"cards": [{"type": "a"}]},
            {"sections": [{"cards": [{"type": "b"}, {"type": "c"}]}]},
        ]}
        after = {"views": [
            {"cards": [{"type": "a"}, {"type": "x"}]},
            {"sections": [{"cards": [{"type": "b"}, {"type": "c"}]}]},
        ]}
        assert _summary_text(_version_change_summary("dashboard", before, after)) == "4 cards (was 3)"
        assert _version_change_summary("dashboard", before, after)["summary_key"] == "version.cards.was"
        assert _summary_text(_version_change_summary("dashboard", after, after)) == "4 cards"
        assert _version_change_summary("dashboard", None, {"strategy": {}}) is None

    def test_raw_content_size_movement(self):
        from custom_components.phoenix_mcp.tool_common import _version_change_summary

        small = {"content": "a" * 512}
        big = {"content": None, "truncated": True, "bytes": 250_000}
        assert _summary_text(_version_change_summary("yaml_config", small, big)) == "244.1 KB (was 512 B)"
        assert _summary_text(_version_change_summary("file", None, small)) == "512 B"
        assert _summary_text(_version_change_summary("file", small, small)) == "512 B"

    def test_entity_changed_fields_and_removal(self):
        from custom_components.phoenix_mcp.tool_common import _version_change_summary

        before = {"name": "Old", "icon": "mdi:a", "area_id": "kitchen"}
        after = {"name": "New", "icon": "mdi:a", "area_id": "office"}
        assert _summary_text(_version_change_summary("entity", before, after)) == "changed: area_id, name"
        assert _summary_text(_version_change_summary("entity", before, None)) == "registry entry removed"
        assert _version_change_summary("entity", before, before) is None

    def test_alias_carrying_types_get_no_summary(self):
        from custom_components.phoenix_mcp.tool_common import _version_change_summary

        cfg = {"alias": "Morning", "trigger": [], "action": []}
        for rtype in ("automation", "script", "scene", "helper"):
            assert _version_change_summary(rtype, None, cfg) is None
