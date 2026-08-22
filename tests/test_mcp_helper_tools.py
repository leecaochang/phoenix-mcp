"""Tests for helper CRUD MCP tools (create/edit/delete_helper, list_helpers).

Helper writes are Confirm-gated on cap_helper_write and execute via the
in-process WS command dispatcher (ws_dispatch). list_helpers is a
cap_registry_read read scoped to accessible helper entities.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.tools.helper import _build_diff_delete_helper


def _token(**caps) -> TokenRecord:
    tree = PermissionTree(domains={
        "input_boolean": PermissionNode(state="GREEN"),
        "input_button": PermissionNode(state="GREEN"),
        "schedule": PermissionNode(state="GREEN"),
    })
    base = {"cap_helper_write": "allow", "cap_registry_read": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=tree, **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass):
    return await _call_tool(name, args, token, hass, MagicMock())


@pytest.fixture
async def helper_env(hass: HomeAssistant):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {}})
    return hass


class TestExecutorRegistration:
    def test_helper_executors_registered(self):
        for name in ("create_helper", "edit_helper", "delete_helper"):
            assert name in _EXECUTOR_REGISTRY


class TestCreateHelper:
    async def test_deny_without_cap(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "X"}},
            _token(cap_helper_write="deny"), hass)
        assert outcome == "denied"

    async def test_create(self, hass, helper_env, hass_admin_user):
        content, outcome, _ = await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "Guest mode"}}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["helper"]["name"] == "Guest mode"
        helper_id = body["helper"]["id"]
        await hass.async_block_till_done()
        # The returned id must map to a real entity, not just "some input_boolean
        # exists": resolve the registry entry by its unique_id and confirm the live
        # entity_id is what got created.
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        entity_id = reg.async_get_entity_id("input_boolean", "input_boolean", helper_id)
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None
        assert hass.states.get(entity_id).attributes.get("friendly_name") == "Guest mode"

    async def test_bad_type(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call(
            "create_helper", {"helper_type": "light", "config": {"name": "X"}}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_empty_config(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {}}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_empty_config_confirm_mode_rejected_before_pending(self, hass, helper_env, hass_admin_user):
        """An invalid helper config must fail before a pending approval is
        created, even under confirm mode, otherwise it sails through as a false
        pending that can only fail once an admin approves it."""
        _, outcome, _ = await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {}},
            _token(cap_helper_write="confirm"), hass)
        assert outcome == "invalid_request"

    async def test_bad_type_confirm_mode_rejected_before_pending(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call(
            "create_helper", {"helper_type": "light", "config": {"name": "X"}},
            _token(cap_helper_write="confirm"), hass)
        assert outcome == "invalid_request"


class TestEditDeleteHelper:
    async def test_delete_diff_uses_live_friendly_name(self, hass, helper_env, hass_admin_user):
        created = _json((await _call(
            "create_helper",
            {"helper_type": "input_boolean", "config": {"name": "Approval helper"}},
            _token(), hass,
        ))[0])
        await hass.async_block_till_done()
        diff = _build_diff_delete_helper(
            {
                "helper_type": "input_boolean",
                "helper_id": created["helper"]["id"],
            },
            _token(), hass,
        )
        assert diff["target"]["label"] == "Approval helper"

    async def test_edit(self, hass, helper_env, hass_admin_user):
        created = _json((await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "A"}}, _token(), hass))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()  # let the helper entity register
        content, outcome, _ = await _call(
            "edit_helper", {"helper_type": "input_boolean", "helper_id": hid, "config": {"name": "B"}}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["helper"]["name"] == "B"
        await hass.async_block_till_done()
        # Registry integrity: the same id still maps to a single live entity, whose
        # state now reflects the edit (not a duplicate or orphaned entry).
        from homeassistant.helpers import entity_registry as er
        entity_id = er.async_get(hass).async_get_entity_id("input_boolean", "input_boolean", hid)
        assert entity_id is not None
        assert hass.states.get(entity_id).attributes.get("friendly_name") == "B"

    async def test_delete(self, hass, helper_env, hass_admin_user):
        from homeassistant.helpers import entity_registry as er
        created = _json((await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "Temp"}}, _token(), hass))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()  # let the helper entity register
        entity_id = er.async_get(hass).async_get_entity_id("input_boolean", "input_boolean", hid)
        assert entity_id is not None
        _, outcome, _ = await _call(
            "delete_helper", {"helper_type": "input_boolean", "helper_id": hid}, _token(), hass)
        assert outcome == "allowed"
        await hass.async_block_till_done()
        # Registry integrity: the entry and its live state are both gone.
        assert er.async_get(hass).async_get_entity_id("input_boolean", "input_boolean", hid) is None
        assert hass.states.get(entity_id) is None

    async def test_edit_invalid_config_confirm_mode_rejected_before_pending(self, hass, helper_env, hass_admin_user):
        created = _json((await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "A"}}, _token(), hass))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()
        _, outcome, _ = await _call(
            "edit_helper", {"helper_type": "input_boolean", "helper_id": hid, "config": "not-a-dict"},
            _token(cap_helper_write="confirm"), hass)
        assert outcome == "invalid_request"

    async def test_delete_unknown(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call(
            "delete_helper", {"helper_type": "input_boolean", "helper_id": "does_not_exist"}, _token(), hass)
        assert outcome == "not_found"

    async def test_edit_missing_not_found(self, hass, helper_env, hass_admin_user):
        # A non-existent helper is not_found regardless of scope (existence check).
        _, outcome, _ = await _call(
            "edit_helper", {"helper_type": "input_boolean", "helper_id": "does_not_exist", "config": {"name": "X"}},
            _token(), hass)
        assert outcome == "not_found"

    async def test_edit_cap_only_not_entity_scoped(self, hass, helper_env, hass_admin_user):
        # Helper authoring is cap-gated, not entity-scoped.
        created = _json((await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "Owned"}}, _token(), hass))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()
        unscoped = TokenRecord(
            id=str(uuid.uuid4()), name="a", token_hash="x", created_at=utcnow(),
            created_by="u", permissions=PermissionTree(domains={}),
            cap_helper_write="allow", cap_registry_read="allow",
        )
        content, outcome, _ = await _call(
            "edit_helper", {"helper_type": "input_boolean", "helper_id": hid, "config": {"name": "Renamed"}},
            unscoped, hass)
        assert outcome == "allowed"
        assert _json(content)["helper"]["name"] == "Renamed"

    async def test_delete_cap_only_not_entity_scoped(self, hass, helper_env, hass_admin_user):
        # Helper deletion is cap-gated, not entity-scoped.
        created = _json((await _call(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "Owned"}}, _token(), hass))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()
        unscoped = TokenRecord(
            id=str(uuid.uuid4()), name="a", token_hash="x", created_at=utcnow(),
            created_by="u", permissions=PermissionTree(domains={}),
            cap_helper_write="allow", cap_registry_read="allow",
        )
        _, outcome, _ = await _call(
            "delete_helper", {"helper_type": "input_boolean", "helper_id": hid}, unscoped, hass)
        assert outcome == "allowed"


class TestVersionCapture:
    """create/edit/delete_helper record version history.

    The other helper tests pass a MagicMock for data, so capture is a no-op there;
    here a real VersionStore is supplied so the helper read-before path that
    populates `before` for edit and delete is actually exercised end-to-end.
    """

    @staticmethod
    def _data():
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions,
        )
        return data, versions

    async def test_create_edit_delete_record_history(self, hass, helper_env, hass_admin_user):
        data, versions = self._data()
        token = _token()

        created = _json((await _call_tool(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "A"}},
            token, hass, data))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()

        await _call_tool(
            "edit_helper",
            {"helper_type": "input_boolean", "helper_id": hid, "config": {"name": "B"}},
            token, hass, data)
        await hass.async_block_till_done()

        await _call_tool(
            "delete_helper", {"helper_type": "input_boolean", "helper_id": hid},
            token, hass, data)

        history = versions.list_for("helper", f"input_boolean:{hid}")
        assert [v.action for v in history] == ["delete", "edit", "create"]  # newest first
        delete_rec, edit_rec, create_rec = history

        assert create_rec.before is None
        assert create_rec.after == {"name": "A"}
        assert create_rec.token_name == token.name
        # edit_helper reads the prior config into `before`.
        assert edit_rec.before is not None and edit_rec.before.get("name") == "A"
        assert edit_rec.after == {"name": "B"}
        # delete_helper read the prior config; no `after`
        assert delete_rec.before is not None and delete_rec.before.get("name") == "B"
        assert delete_rec.after is None

    async def test_restore_existing_helper_reapplies_as_rollback(self, hass, helper_env, hass_admin_user):
        from custom_components.phoenix_mcp.mcp_view import async_restore_version

        data, versions = self._data()
        token = _token()
        created = _json((await _call_tool(
            "create_helper", {"helper_type": "input_boolean", "config": {"name": "A"}},
            token, hass, data))[0])
        hid = created["helper"]["id"]
        await hass.async_block_till_done()
        await _call_tool(
            "edit_helper", {"helper_type": "input_boolean", "helper_id": hid, "config": {"name": "B"}},
            token, hass, data)
        await hass.async_block_till_done()

        rkey = f"input_boolean:{hid}"
        create_ver = versions.list_for("helper", rkey)[-1]  # the create (name A)
        _r, outcome, _res = await async_restore_version(create_ver, "admin-9", hass, data)
        assert outcome == "allowed"

        latest = versions.list_for("helper", rkey)[0]
        assert latest.action == "rollback"
        assert latest.approved_by_user_id == "admin-9"
        assert latest.after.get("name") == "A"


class TestListHelpers:
    async def test_lists_accessible(self, hass, helper_env, hass_admin_user):
        await _call("create_helper", {"helper_type": "input_boolean", "config": {"name": "Listed"}}, _token(), hass)
        await hass.async_block_till_done()
        content, outcome, _ = await _call("list_helpers", {}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert any(h["helper_type"] == "input_boolean" and h["helper_id"] for h in body["helpers"])

    async def test_deny_without_cap(self, hass, helper_env, hass_admin_user):
        _, outcome, _ = await _call("list_helpers", {}, _token(cap_registry_read="deny"), hass)
        assert outcome == "denied"


class TestInputButtonLifecycle:
    """Prove input_button fits the shared storage-helper CRUD contract."""

    async def test_crud_and_deleted_restore(self, hass, hass_admin_user):
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.mcp_view import async_restore_version

        assert await async_setup_component(hass, "input_button", {"input_button": {}})
        await hass.async_block_till_done()
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions,
        )
        token = _token()

        created_result, outcome, _ = await _call_tool(
            "create_helper",
            {
                "helper_type": "input_button",
                "config": {"name": "Doorbell test", "icon": "mdi:gesture-tap-button"},
            },
            token, hass, data,
        )
        assert outcome == "allowed"
        created = _json(created_result)["helper"]
        helper_id = created["id"]
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("input_button", "input_button", helper_id)
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None
        assert hass.states.get(entity_id).attributes["friendly_name"] == "Doorbell test"
        assert hass.states.get(entity_id).attributes["icon"] == "mdi:gesture-tap-button"

        listed_result, outcome, _ = await _call_tool(
            "list_helpers", {"helper_type": "input_button"}, token, hass, data,
        )
        assert outcome == "allowed"
        assert _json(listed_result)["helpers"] == [{
            "entity_id": entity_id,
            "helper_type": "input_button",
            "name": "Doorbell test",
            "helper_id": helper_id,
        }]

        edited_result, outcome, _ = await _call_tool(
            "edit_helper",
            {
                "helper_type": "input_button",
                "helper_id": helper_id,
                "config": {"name": "Doorbell renamed", "icon": "mdi:radiobox-marked"},
            },
            token, hass, data,
        )
        assert outcome == "allowed"
        assert _json(edited_result)["helper"]["name"] == "Doorbell renamed"
        assert registry.async_get_entity_id("input_button", "input_button", helper_id) == entity_id
        assert hass.states.get(entity_id).attributes["friendly_name"] == "Doorbell renamed"
        assert hass.states.get(entity_id).attributes["icon"] == "mdi:radiobox-marked"

        _, outcome, _ = await _call_tool(
            "delete_helper",
            {"helper_type": "input_button", "helper_id": helper_id},
            token, hass, data,
        )
        assert outcome == "allowed"
        assert registry.async_get_entity_id("input_button", "input_button", helper_id) is None
        assert hass.states.get(entity_id) is None

        history = versions.list_for("helper", f"input_button:{helper_id}")
        assert [record.action for record in history] == ["delete", "edit", "create"]
        assert history[0].before["name"] == "Doorbell renamed"
        assert history[1].before["name"] == "Doorbell test"
        assert history[1].after["name"] == "Doorbell renamed"

        restored_result, outcome, _ = await async_restore_version(
            history[0], "admin-input-button", hass, data,
        )
        assert outcome == "allowed"
        restored = _json(restored_result)["helper"]
        restored_id = restored["id"]
        assert restored_id != helper_id
        restored_entity_id = registry.async_get_entity_id(
            "input_button", "input_button", restored_id
        )
        assert restored_entity_id is not None
        assert hass.states.get(restored_entity_id).attributes["friendly_name"] == "Doorbell renamed"
        restored_history = versions.list_for("helper", f"input_button:{restored_id}")
        assert restored_history[0].action == "rollback"
        assert restored_history[0].approved_by_user_id == "admin-input-button"

        # A restore creates a new storage-helper id, so remove that new helper too.
        _, outcome, _ = await _call_tool(
            "delete_helper",
            {"helper_type": "input_button", "helper_id": restored_id},
            token, hass, data,
        )
        assert outcome == "allowed"

class TestScheduleLifecycle:
    """Prove schedule CRUD preserves HA's nested time-range contract."""

    async def test_schema_crud_and_deleted_restore(self, hass, hass_admin_user):
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.mcp_view import async_restore_version

        assert await async_setup_component(hass, "schedule", {"schedule": {}})
        await hass.async_block_till_done()
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions,
        )
        token = _token()
        initial_config = {
            "name": "Weekly test",
            "icon": "mdi:calendar-clock",
            "monday": [{
                "from": "08:00:00",
                "to": "09:30:00",
                "data": {"mode": "office", "enabled": True},
            }],
            "sunday": [{"from": "23:00:00", "to": "24:00:00"}],
        }

        created_result, outcome, _ = await _call_tool(
            "create_helper",
            {"helper_type": "schedule", "config": initial_config},
            token, hass, data,
        )
        assert outcome == "allowed"
        created = _json(created_result)["helper"]
        helper_id = created["id"]
        assert created["monday"] == initial_config["monday"]
        assert created["sunday"] == initial_config["sunday"]
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("schedule", "schedule", helper_id)
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None
        assert hass.states.get(entity_id).attributes["friendly_name"] == "Weekly test"
        assert hass.states.get(entity_id).attributes["icon"] == "mdi:calendar-clock"

        listed_result, outcome, _ = await _call_tool(
            "list_helpers", {"helper_type": "schedule"}, token, hass, data,
        )
        assert outcome == "allowed"
        assert _json(listed_result)["helpers"] == [{
            "entity_id": entity_id,
            "helper_type": "schedule",
            "name": "Weekly test",
            "helper_id": helper_id,
        }]

        invalid_result, outcome, _ = await _call_tool(
            "create_helper",
            {
                "helper_type": "schedule",
                "config": {
                    "name": "Overlapping test",
                    "monday": [
                        {"from": "08:00:00", "to": "10:00:00"},
                        {"from": "09:00:00", "to": "11:00:00"},
                    ],
                },
            },
            token, hass, data,
        )
        assert outcome == "invalid_request"
        assert invalid_result["isError"] is True
        assert registry.async_get_entity_id(
            "schedule", "schedule", "overlapping_test"
        ) is None

        edited_config = {
            "name": "Weekly renamed",
            "icon": "mdi:calendar-refresh",
            "tuesday": [{
                "from": "12:15:00",
                "to": "13:45:00",
                "data": {"mode": "lunch", "priority": 2},
            }],
        }
        edited_result, outcome, _ = await _call_tool(
            "edit_helper",
            {
                "helper_type": "schedule",
                "helper_id": helper_id,
                "config": edited_config,
            },
            token, hass, data,
        )
        assert outcome == "allowed"
        edited = _json(edited_result)["helper"]
        assert edited["tuesday"] == edited_config["tuesday"]
        assert registry.async_get_entity_id("schedule", "schedule", helper_id) == entity_id
        assert hass.states.get(entity_id).attributes["friendly_name"] == "Weekly renamed"
        assert hass.states.get(entity_id).attributes["icon"] == "mdi:calendar-refresh"

        _, outcome, _ = await _call_tool(
            "delete_helper",
            {"helper_type": "schedule", "helper_id": helper_id},
            token, hass, data,
        )
        assert outcome == "allowed"
        assert registry.async_get_entity_id("schedule", "schedule", helper_id) is None
        assert hass.states.get(entity_id) is None

        history = versions.list_for("helper", f"schedule:{helper_id}")
        assert [record.action for record in history] == ["delete", "edit", "create"]
        assert history[0].before["tuesday"] == edited_config["tuesday"]
        assert history[1].before["monday"] == initial_config["monday"]
        assert history[1].after == edited_config

        restored_result, outcome, _ = await async_restore_version(
            history[0], "admin-schedule", hass, data,
        )
        assert outcome == "allowed"
        restored = _json(restored_result)["helper"]
        restored_id = restored["id"]
        assert restored_id != helper_id
        restored_entity_id = registry.async_get_entity_id("schedule", "schedule", restored_id)
        assert restored_entity_id is not None
        assert restored["tuesday"] == edited_config["tuesday"]
        restored_history = versions.list_for("helper", f"schedule:{restored_id}")
        assert restored_history[0].action == "rollback"
        assert restored_history[0].approved_by_user_id == "admin-schedule"

        _, outcome, _ = await _call_tool(
            "delete_helper",
            {"helper_type": "schedule", "helper_id": restored_id},
            token, hass, data,
        )
        assert outcome == "allowed"
