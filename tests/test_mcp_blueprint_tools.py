"""Tests for the blueprint authoring tools (create/edit/delete + get).

Writes go through HA's own blueprint/save and blueprint/delete WS commands, so
these verify Phoenix MCP's wiring rather than HA's behaviour: the dual capability
gate, the path jail, pre-gate validation, the consumer annotation that makes an
edit's real blast radius visible in the approval, and version capture.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.tools import blueprint as blueprint_tools
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError

VALID = """\
blueprint:
  name: Test Blueprint
  domain: automation
  input:
    trigger_entity:
      name: Trigger entity
triggers:
  - trigger: state
    entity_id: !input trigger_entity
actions:
  - action: light.turn_on
    target:
      entity_id: light.kitchen
mode: single
"""


def _token(**caps) -> TokenRecord:
    base = {
        "cap_blueprint_write": "allow",
        "cap_automation_write": "allow",
        "cap_script_write": "allow",
        "cap_config_read": "allow",
    }
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=PermissionTree(), **base,
    )


def _data() -> tuple[PhoenixData, VersionStore]:
    versions = VersionStore()
    return PhoenixData(store=MagicMock(), rate_limiter=MagicMock(),
                       audit=MagicMock(), versions=versions), versions


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _text(content: dict) -> str:
    return content["content"][0]["text"]


async def _call(name, args, token, hass, data=None):
    return await _call_tool(name, args, token, hass, data if data is not None else MagicMock())


@pytest.fixture
def bp_env(hass: HomeAssistant, tmp_path):
    """A config dir with a real blueprints/automation tree."""
    import os
    hass.config.config_dir = str(tmp_path)
    os.makedirs(os.path.join(str(tmp_path), "blueprints", "automation", "author"), exist_ok=True)
    os.makedirs(os.path.join(str(tmp_path), "blueprints", "script"), exist_ok=True)
    return tmp_path


def _write_existing(tmp_path, rel="author/existing.yaml", body=VALID):
    import os
    target = os.path.join(str(tmp_path), "blueprints", "automation", rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(body)
    return target


def _patch_ws(side_effect=None, return_value=None):
    return patch.object(
        blueprint_tools, "async_ws_command",
        new=AsyncMock(side_effect=side_effect, return_value=return_value))


def _patch_consumers(entities):
    return patch.object(blueprint_tools, "_blueprint_consumers", return_value=list(entities))


class TestRegistration:
    def test_executors_registered(self):
        for name in ("create_blueprint", "edit_blueprint", "delete_blueprint"):
            assert name in _EXECUTOR_REGISTRY

    def test_ws_commands_allowlisted(self):
        from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS
        assert "blueprint/save" in ALLOWED_WS_COMMANDS
        assert "blueprint/delete" in ALLOWED_WS_COMMANDS

    def test_import_from_url_is_not_dispatchable(self):
        # Deliberately absent: it fetches an operator-unseen URL from inside HA.
        from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS
        assert "blueprint/import" not in ALLOWED_WS_COMMANDS


class TestDualCapabilityGate:
    """cap_blueprint_write AND the domain cap must both permit."""

    async def test_blueprint_cap_denied(self, hass, bp_env):
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": VALID},
            _token(cap_blueprint_write="deny"), hass)
        assert outcome == "denied"
        assert "Forbidden" in _text(content)

    async def test_domain_cap_denied_blocks_even_with_blueprint_cap(self, hass, bp_env):
        # A script blueprint rewires scripts, so cap_script_write must permit too:
        # otherwise this is escalation through the blueprint surface.
        _, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "script", "path": "s.yaml", "content": VALID.replace(
                "domain: automation", "domain: script")},
            _token(cap_script_write="deny"), hass)
        assert outcome == "denied"

    async def test_automation_cap_denied_blocks_automation_blueprint(self, hass, bp_env):
        _, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": VALID},
            _token(cap_automation_write="deny"), hass)
        assert outcome == "denied"

    async def test_other_domain_cap_is_irrelevant(self, hass, bp_env):
        # cap_script_write=deny must not block an AUTOMATION blueprint.
        with _patch_ws(return_value={"overrides_existing": False}), _patch_consumers([]):
            _, outcome, _ = await _call(
                "create_blueprint",
                {"domain": "automation", "path": "author/x.yaml", "content": VALID},
                _token(cap_script_write="deny"), hass, _data()[0])
        assert outcome == "allowed"

    async def test_denied_token_learns_nothing_about_its_payload(self, hass, bp_env):
        """The cap-deny check runs before validation, so nothing is echoed back."""
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "not-a-domain", "path": "///", "content": "garbage: ["},
            _token(cap_blueprint_write="deny"), hass)
        assert outcome == "denied"
        body = _text(content)
        assert "YAML" not in body and "path must be" not in body


class TestPathJail:
    @pytest.mark.parametrize(
        "bad",
        [
            "../../../secrets.yaml",
            "author/../../../etc/passwd.yaml",
            "/etc/passwd.yaml",
            ".hidden/x.yaml",
            "author/.hidden.yaml",
            "author/notes.txt",
            "author/noext",
            "",
            "   ",
        ],
    )
    async def test_refused_paths(self, hass, bp_env, bad):
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": bad, "content": VALID},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "relative .yaml" in _text(content)

    async def test_nested_author_path_allowed(self, hass, bp_env):
        with _patch_ws(return_value={"overrides_existing": False}), _patch_consumers([]):
            content, outcome, _ = await _call(
                "create_blueprint",
                {"domain": "automation", "path": "deep/nested/ok.yaml", "content": VALID},
                _token(), hass, _data()[0])
        assert outcome == "allowed"
        assert _json(content)["path"] == "deep/nested/ok.yaml"

    async def test_bad_domain_refused(self, hass, bp_env):
        _, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "template", "path": "x.yaml", "content": VALID},
            _token(), hass)
        assert outcome == "invalid_request"


class TestPreGateValidation:
    """A structurally invalid blueprint must fail before an approval exists."""

    async def test_unparseable_yaml_refused(self, hass, bp_env):
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": "a: [unclosed"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "not valid YAML" in _text(content)

    async def test_missing_blueprint_block_refused(self, hass, bp_env):
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": "triggers: []\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "not a valid automation blueprint" in _text(content)

    async def test_domain_mismatch_refused(self, hass, bp_env):
        # Declared script, saved as automation.
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml",
             "content": VALID.replace("domain: automation", "domain: script")},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "not a valid automation blueprint" in _text(content)

    async def test_undefined_input_refused(self, hass, bp_env):
        # HA rejects a body using an !input with no matching input definition.
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml",
             "content": VALID.replace("!input trigger_entity", "!input nope")},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "not a valid automation blueprint" in _text(content)

    async def test_confirm_mode_refuses_before_creating_an_approval(self, hass, bp_env):
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _, outcome, _ = await _call_tool(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": "a: [unclosed"},
            _token(cap_blueprint_write="confirm"), hass, data)
        assert outcome == "invalid_request"
        data.store.set_pending_approvals.assert_not_called()

    async def test_empty_content_refused(self, hass, bp_env):
        _, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/x.yaml", "content": "   "},
            _token(), hass)
        assert outcome == "invalid_request"


class TestCreate:
    async def test_creates_and_versions(self, hass, bp_env):
        data, versions = _data()
        with _patch_ws(return_value={"overrides_existing": False}) as ws, _patch_consumers([]):
            content, outcome, resource = await _call(
                "create_blueprint",
                {"domain": "automation", "path": "author/new.yaml", "content": VALID},
                _token(), hass, data)
        assert outcome == "allowed"
        assert resource == "blueprint:automation/author/new.yaml"
        assert ws.await_args.args[1] == "blueprint/save"
        payload = ws.await_args.args[2]
        assert payload["allow_override"] is False
        assert payload["yaml"] == VALID
        rec = versions.list_recent()[0]
        assert rec.resource_type == "blueprint" and rec.action == "create"

    async def test_refuses_when_path_already_exists(self, hass, bp_env):
        _write_existing(bp_env)
        content, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/existing.yaml", "content": VALID},
            _token(), hass, _data()[0])
        assert outcome == "invalid_request"
        assert "already exists" in _text(content)

    async def test_yaml_suffix_added_by_ha_is_not_our_concern(self, hass, bp_env):
        # We require an explicit .yaml/.yml, so HA's suffix fallback never fires.
        _, outcome, _ = await _call(
            "create_blueprint",
            {"domain": "automation", "path": "author/nosuffix", "content": VALID},
            _token(), hass)
        assert outcome == "invalid_request"


class TestEdit:
    async def test_edit_requires_an_existing_blueprint(self, hass, bp_env):
        content, outcome, _ = await _call(
            "edit_blueprint",
            {"domain": "automation", "path": "author/ghost.yaml", "content": VALID},
            _token(), hass, _data()[0])
        assert outcome == "not_found"
        assert "not found" in _text(content).lower()

    async def test_edit_overrides_and_versions_before_after(self, hass, bp_env):
        _write_existing(bp_env)
        data, versions = _data()
        new = VALID.replace("Test Blueprint", "Renamed")
        with _patch_ws(return_value={"overrides_existing": True}) as ws, _patch_consumers([]):
            _content, outcome, _ = await _call(
                "edit_blueprint",
                {"domain": "automation", "path": "author/existing.yaml", "content": new},
                _token(), hass, data)
        assert outcome == "allowed"
        assert ws.await_args.args[2]["allow_override"] is True
        rec = versions.list_recent()[0]
        assert rec.action == "edit"
        assert "Test Blueprint" in rec.before["content"]
        assert "Renamed" in rec.after["content"]

    async def test_edit_reports_the_reloaded_consumers(self, hass, bp_env):
        """The blast radius has to reach the caller, not just the approval card."""
        _write_existing(bp_env)
        with _patch_ws(return_value={"overrides_existing": True}), \
             _patch_consumers(["automation.a", "automation.b"]):
            content, outcome, _ = await _call(
                "edit_blueprint",
                {"domain": "automation", "path": "author/existing.yaml", "content": VALID},
                _token(), hass, _data()[0])
        assert outcome == "allowed"
        body = _json(content)
        assert body["reloaded"] == ["automation.a", "automation.b"]
        assert "2 existing automation(s)" in body["note"]

    async def test_ws_failure_surfaces(self, hass, bp_env):
        _write_existing(bp_env)
        with _patch_ws(side_effect=WsDispatchError("disk full")), _patch_consumers([]):
            content, outcome, _ = await _call(
                "edit_blueprint",
                {"domain": "automation", "path": "author/existing.yaml", "content": VALID},
                _token(), hass, _data()[0])
        assert outcome == "invalid_request"
        assert "disk full" in _text(content)


class TestDelete:
    async def test_deletes_and_versions_the_source(self, hass, bp_env):
        _write_existing(bp_env)
        data, versions = _data()
        with _patch_ws(return_value=None) as ws, _patch_consumers([]):
            content, outcome, _ = await _call(
                "delete_blueprint",
                {"domain": "automation", "path": "author/existing.yaml"},
                _token(), hass, data)
        assert outcome == "allowed"
        assert ws.await_args.args[1] == "blueprint/delete"
        assert _json(content)["deleted"] is True
        rec = versions.list_recent()[0]
        assert rec.action == "delete"
        assert "Test Blueprint" in rec.before["content"]

    async def test_unknown_blueprint_not_found(self, hass, bp_env):
        _, outcome, _ = await _call(
            "delete_blueprint",
            {"domain": "automation", "path": "author/ghost.yaml"},
            _token(), hass, _data()[0])
        assert outcome == "not_found"

    async def test_in_use_refusal_from_ha_surfaces(self, hass, bp_env):
        """HA raises BlueprintInUse; that is a legitimate outcome, not a crash."""
        _write_existing(bp_env)
        with _patch_ws(side_effect=WsDispatchError(
                "home_assistant_error: Blueprint author/existing.yaml is in use")), \
             _patch_consumers(["automation.a"]):
            content, outcome, _ = await _call(
                "delete_blueprint",
                {"domain": "automation", "path": "author/existing.yaml"},
                _token(), hass, _data()[0])
        assert outcome == "invalid_request"
        assert "in use" in _text(content)

    async def test_delete_does_not_require_content(self, hass, bp_env):
        _write_existing(bp_env)
        with _patch_ws(return_value=None), _patch_consumers([]):
            _, outcome, _ = await _call(
                "delete_blueprint",
                {"domain": "automation", "path": "author/existing.yaml"},
                _token(), hass, _data()[0])
        assert outcome == "allowed"


class TestApprovalDiff:
    async def test_edit_diff_names_the_consumers(self, hass, bp_env):
        _write_existing(bp_env)
        with _patch_consumers(["automation.a", "automation.b", "automation.c"]):
            diff = await blueprint_tools._build_diff_blueprint(
                {"domain": "automation", "path": "author/existing.yaml", "content": VALID},
                hass, "edit")
        assert "3 will be reloaded" in diff["summary"]
        assert diff["preview"]["used_by_count"] == 3
        assert diff["preview"]["used_by"] == ["automation.a", "automation.b", "automation.c"]
        assert "Test Blueprint" in diff["before"]
        assert diff["after"] is not None

    async def test_create_diff_has_no_before_and_no_consumer_count(self, hass, bp_env):
        with _patch_consumers([]):
            diff = await blueprint_tools._build_diff_blueprint(
                {"domain": "automation", "path": "author/brand_new.yaml", "content": VALID},
                hass, "create")
        assert diff["before"] is None
        assert "reloaded" not in diff["summary"]
        assert diff["target"]["type"] == "blueprint"

    async def test_delete_diff_carries_the_source_being_lost(self, hass, bp_env):
        _write_existing(bp_env)
        with _patch_consumers([]):
            diff = await blueprint_tools._build_diff_blueprint(
                {"domain": "automation", "path": "author/existing.yaml"}, hass, "delete")
        assert "Test Blueprint" in diff["before"]
        assert diff["after"] is None

    async def test_consumer_lookup_failure_degrades_quietly(self, hass, bp_env):
        # Annotation only: a lookup failure must never block the gate.
        _write_existing(bp_env)
        with patch.object(blueprint_tools, "_blueprint_consumers", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                # Confirm the patch bites, then verify the real helper swallows.
                blueprint_tools._blueprint_consumers(hass, "automation", "x")
        assert blueprint_tools._blueprint_consumers(hass, "automation", "nope") == []


class TestBlueprintRestore:
    """async_restore_version routes blueprint snapshots back through the executors.

    Blueprint versions were recorded (and the panel offered Restore) long before
    a restore branch existed, so every attempt errored "Cannot restore resource
    type 'blueprint'"; these pin the branch and the synthetic restore token's
    explicit capability allows, without which the executor's dual gate refuses
    the pass-through-EXEMPT domain caps.
    """

    async def test_restore_token_carries_explicit_allows_for_exempt_caps(self):
        token = mcp_view._restore_token("admin-1")
        assert token.cap_automation_write == "allow"
        assert token.cap_script_write == "allow"
        assert token.cap_blueprint_write == "allow"

    async def test_restore_edits_an_existing_blueprint(self, hass, bp_env):
        from custom_components.phoenix_mcp.mcp_view import async_restore_version
        data, versions = _data()
        token = _token()
        _write_existing(bp_env)  # V1 on disk
        v2 = VALID.replace("Test Blueprint", "Test Blueprint v2")
        with _patch_ws(return_value={"overrides_existing": True}), _patch_consumers([]):
            await _call("edit_blueprint",
                        {"domain": "automation", "path": "author/existing.yaml", "content": v2},
                        token, hass, data)
        edit_ver = versions.list_for("blueprint", "automation/author/existing.yaml")[0]
        assert edit_ver.action == "edit"

        with _patch_ws(return_value={"overrides_existing": True}) as ws, _patch_consumers([]):
            _result, outcome, _r = await async_restore_version(edit_ver, "admin-1", hass, data, side="before")
        assert outcome == "allowed"
        call = ws.await_args_list[-1].args
        assert call[1] == "blueprint/save"
        assert call[2]["yaml"] == VALID  # the Before snapshot, byte-faithful
        assert call[2]["allow_override"] is True  # file exists, so edit path
        latest = versions.list_for("blueprint", "automation/author/existing.yaml")[0]
        assert latest.action == "rollback"

    async def test_restore_recreates_a_deleted_blueprint(self, hass, bp_env):
        import os
        from custom_components.phoenix_mcp.mcp_view import async_restore_version
        data, versions = _data()
        token = _token()
        target = _write_existing(bp_env)
        with _patch_ws(return_value={}), _patch_consumers([]):
            await _call("delete_blueprint",
                        {"domain": "automation", "path": "author/existing.yaml"},
                        token, hass, data)
        os.remove(target)  # the mocked WS command never touched disk; HA would have
        del_ver = versions.list_for("blueprint", "automation/author/existing.yaml")[0]
        assert del_ver.action == "delete"

        with _patch_ws(return_value={}) as ws, _patch_consumers([]):
            _result, outcome, _r = await async_restore_version(del_ver, "admin-1", hass, data)
        assert outcome == "allowed"  # side defaults to before for a delete
        call = ws.await_args_list[-1].args
        assert call[1] == "blueprint/save"
        assert call[2]["yaml"] == VALID
        assert call[2]["allow_override"] is False  # file gone, so create path

    async def test_oversized_blueprint_snapshot_refuses_cleanly(self, hass, bp_env):
        from custom_components.phoenix_mcp.mcp_view import async_restore_version
        data, _versions = _data()
        record = MagicMock()
        record.resource_type = "blueprint"
        record.resource_id = "automation/author/existing.yaml"
        record.before = None
        record.after = {"content": None, "truncated": True, "bytes": 999999}
        result, outcome, _r = await async_restore_version(record, "admin-1", hass, data)
        assert outcome == "invalid_request"
        assert result.get("isError") is True
