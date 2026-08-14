"""Integration tests for include-graph-aware YAML authoring (yaml_includes wiring).

Exercises the create/edit/delete automation/script/scene executors end-to-end on
split configurations (!include / !include_dir_* layouts): correct leaf routing,
byte-preservation of untouched files, pinned refusal messages, legacy fallback
parity when configuration.yaml is absent, tag handling in version capture, the
async_restore_version tag guard, and approval-replay determinism.
"""

from __future__ import annotations

import json
import textwrap
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp import yaml_includes as yi
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _call_tool,
    _execute_edit_automation,
    async_restore_version,
)
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.tools.authoring import (
    _build_diff_create_script,
    _build_diff_delete_automation,
)
from custom_components.phoenix_mcp.version_store import VersionStore


def _token(tree: PermissionTree | None = None, **caps) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=tree or PermissionTree(domains={}), **caps,
    )


def _data() -> tuple[PhoenixData, VersionStore]:
    versions = VersionStore()
    data = PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions,
    )
    return data, versions


def _text(content: dict) -> str:
    return content["content"][0]["text"]


AUTOMATION_CFG = {
    "alias": "Test",
    "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
    "action": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}],
}


@pytest.fixture
def env(hass: HomeAssistant, tmp_path):
    """Isolated config dir + no-op reload services for all three domains."""
    hass.config.config_dir = str(tmp_path)
    for domain in ("automation", "script", "scene"):
        hass.services.async_register(domain, "reload", lambda call: None)
    return tmp_path


def write(base, rel: str, content: str = "") -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


class TestAutomationSplitLayout:
    async def test_delete_diff_uses_live_friendly_name(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", "- id: 1734362394008\n  alias: ~test\n")
        hass.states.async_set(
            "automation.test", "off",
            {"id": "1734362394008", "friendly_name": "~test"},
        )
        diff = await _build_diff_delete_automation(
            {"automation_id": "1734362394008"}, _token(), hass,
        )
        assert diff["target"] == {
            "type": "automation", "id": "1734362394008", "label": "~test",
        }

    async def test_edit_touches_only_the_leaf(self, hass, env):
        write(env, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
        write(env, "automations/one.yaml", "# keep me\n- id: aaa\n  alias: A\n")
        write(env, "automations/two.yaml", "- id: bbb\n  alias: B  # inline note\n")
        other_before = (env / "automations/two.yaml").read_text(encoding="utf-8")

        token = _token(cap_automation_write="allow")
        _, outcome, _ = await _call_tool(
            "edit_automation",
            {"automation_id": "aaa", "config": dict(AUTOMATION_CFG, alias="A2")},
            token, hass, MagicMock(),
        )
        assert outcome == "allowed"
        assert (env / "automations/two.yaml").read_text(encoding="utf-8") == other_before
        text = (env / "automations/one.yaml").read_text(encoding="utf-8")
        assert text.startswith("# keep me\n")
        assert "alias: A2" in text

    async def test_create_routes_to_dir_flavor(self, hass, env):
        write(env, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
        (env / "automations").mkdir()
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "allowed"
        aid = json.loads(_text(content))["id"]
        new_file = env / "automations" / f"{aid}.yaml"
        assert new_file.exists()

    async def test_delete_preserves_other_entries_bytes(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", """\
            - id: aaa
              alias: A
            # bbb heading
            - id: bbb
              alias: B
        """)
        token = _token(cap_automation_write="allow")
        _, outcome, _ = await _call_tool(
            "delete_automation", {"automation_id": "aaa"}, token, hass, MagicMock())
        assert outcome == "allowed"
        assert (env / "automations.yaml").read_text(encoding="utf-8") == textwrap.dedent("""\
            # bbb heading
            - id: bbb
              alias: B
        """)

    async def test_create_refuses_when_unrouted(self, hass, env):
        """The silent no-op bug is fixed: an unroutable create refuses honestly."""
        write(env, "configuration.yaml", "default_config:\nfrontend:\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == yi.MSG_CREATE_NO_ROUTE.format(domain="automation")
        assert not (env / "automations.yaml").exists()

    async def test_duplicate_id_refusal_message(self, hass, env):
        write(env, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
        write(env, "automations/a.yaml", "- id: dup\n  alias: A\n")
        write(env, "automations/b.yaml", "- id: dup\n  alias: B\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "edit_automation", {"automation_id": "dup", "config": AUTOMATION_CFG},
            token, hass, MagicMock())
        assert outcome == "denied"
        assert "more than one included file" in _text(content)

    async def test_multi_branch_create_ambiguity(self, hass, env):
        write(env, "configuration.yaml", """\
            automation a: !include_dir_merge_list one
            automation b: !include_dir_merge_list two
        """)
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == yi.MSG_CREATE_MULTI.format(domain="automation")


class TestScriptSplitLayout:
    def test_create_diff_persists_alias(self, hass):
        diff = _build_diff_create_script(
            {
                "script_id": "approval_summary_smoke_test",
                "config": {"alias": "Approval Summary Smoke Test", "sequence": []},
            },
            _token(), hass,
        )
        assert diff["target"] == {
            "type": "script",
            "id": "approval_summary_smoke_test",
            "label": "Approval Summary Smoke Test",
        }

    async def test_create_edit_delete_dir_named(self, hass, env):
        write(env, "configuration.yaml", "script: !include_dir_named scripts\n")
        (env / "scripts").mkdir()
        token = _token(cap_script_write="allow")
        cfg = {"alias": "S", "sequence": []}

        _, outcome, _ = await _call_tool(
            "create_script", {"script_id": "hello", "config": cfg}, token, hass, MagicMock())
        assert outcome == "allowed"
        assert (env / "scripts/hello.yaml").exists()

        _, outcome, _ = await _call_tool(
            "edit_script", {"script_id": "hello", "config": dict(cfg, alias="S2")},
            token, hass, MagicMock())
        assert outcome == "allowed"
        assert "alias: S2" in (env / "scripts/hello.yaml").read_text(encoding="utf-8")

        _, outcome, _ = await _call_tool(
            "delete_script", {"script_id": "hello"}, token, hass, MagicMock())
        assert outcome == "allowed"
        # dir_named: an empty leftover file would resurrect as {stem: {}}, so
        # delete removes the file itself.
        assert not (env / "scripts/hello.yaml").exists()

    async def test_duplicate_create_graph_wide(self, hass, env):
        write(env, "configuration.yaml", "script: !include_dir_merge_named scripts\n")
        write(env, "scripts/pack.yaml", "hello:\n  sequence: []\n")
        token = _token(cap_script_write="allow")
        content, outcome, _ = await _call_tool(
            "create_script", {"script_id": "hello", "config": {"sequence": []}},
            token, hass, MagicMock())
        assert outcome == "invalid_request"
        assert "already exists" in _text(content)


class TestSceneSplitLayout:
    @pytest.fixture
    def scene_env(self, hass: HomeAssistant, env):
        entry = MockConfigEntry(domain="test_integration", entry_id="e1")
        entry.add_to_hass(hass)
        e = er.async_get(hass).async_get_or_create(
            "light", "test_integration", "uid_k", config_entry=entry,
            suggested_object_id="kitchen")
        hass.states.async_set(e.entity_id, "on", {})
        return env

    def _scene_token(self) -> TokenRecord:
        tree = PermissionTree(domains={
            "light": PermissionNode(state="GREEN"),
            "scene": PermissionNode(state="GREEN"),
        })
        return _token(tree=tree, cap_scene_write="allow", cap_registry_read="allow")

    async def test_edit_and_delete_in_merge_list_dir(self, hass, scene_env):
        env = scene_env
        write(env, "configuration.yaml", "scene: !include_dir_merge_list scenes\n")
        write(env, "scenes/pack.yaml", """\
            - id: movie
              name: Movie
              entities:
                light.kitchen: off
            - id: other
              name: Other
              entities:
                light.kitchen: on
        """)
        token = self._scene_token()
        cfg = {"name": "Movie v2", "entities": {"light.kitchen": "on"}}
        _, outcome, _ = await _call_tool(
            "edit_scene", {"scene_id": "movie", "config": cfg}, token, hass, MagicMock())
        assert outcome == "allowed"
        text = (env / "scenes/pack.yaml").read_text(encoding="utf-8")
        assert "Movie v2" in text and "name: Other" in text

        _, outcome, _ = await _call_tool(
            "delete_scene", {"scene_id": "movie"}, token, hass, MagicMock())
        assert outcome == "allowed"
        remaining = (env / "scenes/pack.yaml").read_text(encoding="utf-8")
        assert "movie" not in remaining and "name: Other" in remaining

    async def test_oracle_error_for_missing_scene_on_split_layout(self, hass, scene_env):
        env = scene_env
        write(env, "configuration.yaml", "scene: !include_dir_merge_list scenes\n")
        (env / "scenes").mkdir()
        token = self._scene_token()
        content, outcome, _ = await _call_tool(
            "delete_scene", {"scene_id": "ghost"}, token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == (
            "No scene found with id 'ghost', or it controls entities outside your write scope.")

    async def test_delete_scene_registry_cleanup_on_split_layout(self, hass, scene_env):
        env = scene_env
        write(env, "configuration.yaml", "scene: !include_dir_merge_list scenes\n")
        write(env, "scenes/pack.yaml", """\
            - id: movie
              name: Movie
              entities:
                light.kitchen: off
        """)
        registry = er.async_get(hass)
        scene_entity = registry.async_get_or_create("scene", "homeassistant", "movie")
        token = self._scene_token()
        _, outcome, _ = await _call_tool(
            "delete_scene", {"scene_id": "movie"}, token, hass, MagicMock())
        assert outcome == "allowed"
        assert registry.async_get(scene_entity.entity_id) is None


class TestLegacyFallbackParity:
    async def test_no_configuration_yaml_uses_legacy_path(self, hass, env):
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "allowed"
        aid = json.loads(_text(content))["id"]
        assert (env / "automations.yaml").exists()

        _, outcome, _ = await _call_tool(
            "edit_automation", {"automation_id": aid, "config": dict(AUTOMATION_CFG, alias="X")},
            token, hass, MagicMock())
        assert outcome == "allowed"

    async def test_legacy_include_guard_still_fires(self, hass, env):
        """No configuration.yaml + !include text in automations.yaml = legacy refusal."""
        write(env, "automations.yaml", "- !include extra.yaml\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == (
            "automations.yaml uses !include directives. Phoenix MCP cannot safely edit it "
            "without destroying the include structure.")

    async def test_create_into_bare_empty_list_file(self, hass, env):
        """Regression: HA's default empty automations.yaml is a bare '[]'. Creating
        the first automation must produce a loadable file (not '[]' + block list)."""
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", "[]\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, MagicMock())
        assert outcome == "allowed", _text(content)
        text = (env / "automations.yaml").read_text(encoding="utf-8")
        assert "[]" not in text
        import yaml as _yaml
        loaded = _yaml.safe_load(text)
        assert isinstance(loaded, list) and len(loaded) == 1

    async def test_not_found_message_parity(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", "- id: aaa\n  alias: A\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "edit_automation", {"automation_id": "ghost", "config": AUTOMATION_CFG},
            token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == "No automation found with id 'ghost'."


class TestTagHandling:
    async def test_secret_edit_records_encoded_before(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", """\
            - id: aaa
              alias: A
              password: !secret db_pass
        """)
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        _, outcome, _ = await _call_tool(
            "edit_automation", {"automation_id": "aaa", "config": dict(AUTOMATION_CFG, alias="A2")},
            token, hass, data)
        assert outcome == "allowed"
        record = versions.list_for("automation", "aaa")[0]
        assert record.before == {"id": "aaa", "alias": "A", "password": "!secret db_pass"}

    async def test_restore_refuses_tag_bearing_version(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", """\
            - id: aaa
              alias: A
              password: !secret db_pass
        """)
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        await _call_tool("delete_automation", {"automation_id": "aaa"}, token, hass, data)
        record = versions.list_for("automation", "aaa")[0]
        assert record.before["password"] == "!secret db_pass"

        content, outcome, _ = await async_restore_version(record, "admin-1", hass, data)
        assert outcome == "invalid_request"
        assert "cannot be restored automatically" in _text(content)
        # Nothing was written back.
        assert "!secret" not in (env / "automations.yaml").read_text(encoding="utf-8")

    async def test_tag_free_restore_round_trips(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        data, versions = _data()
        token = _token(cap_automation_write="allow")
        content, _, _ = await _call_tool(
            "create_automation", {"config": AUTOMATION_CFG}, token, hass, data)
        aid = json.loads(_text(content))["id"]
        await _call_tool("delete_automation", {"automation_id": aid}, token, hass, data)

        record = versions.list_for("automation", aid)[0]  # the delete, before=config
        _, outcome, _ = await async_restore_version(record, "admin-1", hass, data)
        assert outcome == "allowed"
        restored = yi.read_entry(str(env), "automation", aid)
        assert restored["alias"] == AUTOMATION_CFG["alias"]

    async def test_edit_refuses_include_bearing_entry(self, hass, env):
        write(env, "configuration.yaml", "automation: !include automations.yaml\n")
        write(env, "automations.yaml", "- id: aaa\n  alias: A\n  action: !include actions.yaml\n")
        token = _token(cap_automation_write="allow")
        content, outcome, _ = await _call_tool(
            "edit_automation", {"automation_id": "aaa", "config": AUTOMATION_CFG},
            token, hass, MagicMock())
        assert outcome == "denied"
        assert _text(content) == yi.MSG_INCLUDE_REFUSAL.format(name="automations.yaml")


class TestApprovalReplay:
    async def test_execute_edit_automation_standalone(self, hass, env):
        """Approval replay runs _execute_X directly; it must behave identically."""
        write(env, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
        write(env, "automations/one.yaml", "- id: aaa\n  alias: A\n")
        token = _token(cap_automation_write="confirm")
        _, outcome, _ = await _execute_edit_automation(
            {"automation_id": "aaa", "config": dict(AUTOMATION_CFG, alias="Approved")},
            token, hass, MagicMock())
        assert outcome == "allowed"
        assert "alias: Approved" in (env / "automations/one.yaml").read_text(encoding="utf-8")
