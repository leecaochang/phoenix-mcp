"""Tests for the automation/script/scene configuration READ tools.

get_automation / get_script / get_scene close a read-modify-write gap: the edit
tools replace a whole config, so without a read an agent had to reconstruct one
blind and silently destroyed anything it did not resend. Each read returns the
current config plus a content_hash the matching edit accepts as expected_hash,
the same compare-and-swap contract get_dashboard_config already used.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.tools.authoring import _read_automations_yaml
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    """The authoring paths resolve configuration.yaml; isolate from the shared
    shared testing config dir and use the real HA-default include layout."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n"
        "script: !include scripts.yaml\n"
        "scene: !include scenes.yaml\n",
        encoding="utf-8",
    )
    return tmp_path


def _data() -> PhoenixData:
    return PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=VersionStore(),
    )


def _token(tree: PermissionTree | None = None, **caps) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=tree or PermissionTree(domains={}), **caps,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _text(content: dict) -> str:
    return content["content"][0]["text"]


def _cfg(**overrides) -> dict:
    """A fresh automation config per call.

    Deliberately a factory, not a shared constant: HA's config validator
    normalizes in place (trigger -> triggers and so on), so tests sharing one
    dict would leak a mutated nested list into each other.
    """
    base = {
        "alias": "Porch",
        "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
        "condition": [{"condition": "state", "entity_id": "input_boolean.x", "state": "on"}],
        "action": [{"service": "homeassistant.toggle", "entity_id": "input_boolean.x"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
def automation_env(hass: HomeAssistant):
    hass.services.async_register("automation", "reload", lambda call: None)
    return hass


class TestGetAutomation:
    async def test_round_trip_returns_full_config_and_hash(self, hass, automation_env):
        data = _data()
        token = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": _cfg()}, token, hass, data))[0])
        aid = created["id"]

        result, outcome, _res = await _call_tool("get_automation", {"automation_id": aid}, token, hass, data)
        assert outcome == "allowed"
        body = _json(result)
        assert body["automation_id"] == aid
        # Every authored part comes back, which is the whole point: an edit that
        # omits one of these would destroy it. HA normalizes trigger -> triggers
        # and action -> actions when it saves, so the stored form differs from
        # what was sent: another reason an agent must read before replacing.
        cfg = body["config"]
        assert cfg["alias"] == "Porch"
        assert cfg.get("triggers") or cfg.get("trigger")
        assert cfg.get("actions") or cfg.get("action")
        assert cfg.get("conditions") or cfg.get("condition")
        assert isinstance(body["content_hash"], str) and len(body["content_hash"]) == 64

    async def test_missing_automation_is_not_found(self, hass, automation_env):
        data = _data()
        token = _token(cap_automation_write="allow")
        result, outcome, _res = await _call_tool(
            "get_automation", {"automation_id": "nope"}, token, hass, data)
        assert outcome == "not_found"
        assert result.get("isError") is True

    async def test_denied_cap_is_forbidden(self, hass, automation_env):
        data = _data()
        token = _token(cap_automation_write="deny")
        result, outcome, _res = await _call_tool(
            "get_automation", {"automation_id": "x"}, token, hass, data)
        assert outcome == "denied"
        assert result.get("isError") is True

    async def test_confirm_cap_still_reads_without_approval(self, hass, automation_env):
        # A read is never approval-gated; Confirm on the write cap must not
        # block reading (mirrors get_dashboard_config under cap_lovelace_write).
        data = _data()
        writer = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": _cfg()}, writer, hass, data))[0])

        reader = _token(cap_automation_write="confirm")
        result, outcome, _res = await _call_tool(
            "get_automation", {"automation_id": created["id"]}, reader, hass, data)
        assert outcome == "allowed"
        assert _json(result)["config"]["alias"] == "Porch"

    async def test_secret_tags_render_as_display_strings(self, hass, automation_env, tmp_path):
        # A !secret must surface as its display string, never resolved, and the
        # config must stay JSON-serializable.
        (tmp_path / "automations.yaml").write_text(
            "- id: sec1\n"
            "  alias: Secret\n"
            "  trigger:\n"
            "    - platform: state\n"
            "      entity_id: input_boolean.x\n"
            "  action:\n"
            "    - service: notify.notify\n"
            "      data:\n"
            "        message: !secret my_message\n",
            encoding="utf-8",
        )
        data = _data()
        token = _token(cap_automation_write="allow")
        result, outcome, _res = await _call_tool("get_automation", {"automation_id": "sec1"}, token, hass, data)
        assert outcome == "allowed"
        body = _json(result)
        assert body["config"]["action"][0]["data"]["message"] == "!secret my_message"


class TestAutomationCas:
    async def test_matching_hash_allows_edit_stale_hash_refuses(self, hass, automation_env):
        data = _data()
        token = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": _cfg()}, token, hass, data))[0])
        aid = created["id"]
        read = _json((await _call_tool("get_automation", {"automation_id": aid}, token, hass, data))[0])
        good_hash = read["content_hash"]

        # The hash from the read is accepted.
        _r, outcome, _res = await _call_tool(
            "edit_automation",
            {"automation_id": aid, "config": _cfg(alias="Renamed"), "expected_hash": good_hash},
            token, hass, data,
        )
        assert outcome == "allowed"

        # That edit changed the automation, so the ORIGINAL hash is now stale:
        # a second edit carrying it is refused and must not write.
        result, outcome, _res = await _call_tool(
            "edit_automation",
            {"automation_id": aid, "config": _cfg(alias="Clobbered"), "expected_hash": good_hash},
            token, hass, data,
        )
        assert outcome == "invalid_request"
        assert "changed since you last read it" in _text(result)

        items = _read_automations_yaml(str(hass.config.config_dir) + "/automations.yaml")
        assert next(a for a in items if a.get("id") == aid)["alias"] == "Renamed"

    async def test_omitted_hash_skips_the_check(self, hass, automation_env):
        # expected_hash is optional: agents that do not read first still work.
        data = _data()
        token = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": _cfg()}, token, hass, data))[0])
        _r, outcome, _res = await _call_tool(
            "edit_automation",
            {"automation_id": created["id"], "config": _cfg(alias="NoHash")},
            token, hass, data,
        )
        assert outcome == "allowed"


class TestApprovalDiffCompleteness:
    async def test_big_automation_diff_is_not_display_truncated(self, hass, automation_env):
        # The approval diff is what an admin reads to decide, so clipping it
        # mid-config defeats the Confirm gate. A real automation easily exceeds
        # the old 4000-char display default; both sides must stay whole up to
        # the version-snapshot bound, so Approvals shows the same content the
        # Changes tab shows afterwards.
        from custom_components.phoenix_mcp.tools.authoring import _build_diff_edit_automation

        # Built literal here rather than derived from the shared module-level
        # config: HA's validator normalizes in place, so a shallow copy would
        # inherit another test's mutated nested lists.
        big_cfg = {
            "alias": "Long",
            "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
            "action": [
                {"service": "homeassistant.toggle", "entity_id": f"input_boolean.x_{i}",
                 "data": {"note": f"step {i} of a long automation body"}}
                for i in range(60)
            ],
        }
        assert len(json.dumps(big_cfg, indent=2)) > 4000

        data = _data()
        token = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": big_cfg}, token, hass, data))[0])

        diff = await _build_diff_edit_automation(
            {"automation_id": created["id"], "config": big_cfg}, token, hass)
        assert "more characters)" not in diff["after"]
        assert "more characters)" not in diff["before"]
        # Whole JSON on both sides: the last step survives, not just the first.
        assert "step 59 of a long automation body" in diff["after"]
        assert "step 59 of a long automation body" in diff["before"]

    async def test_diff_truncation_never_affected_what_was_written(self, hass, automation_env):
        """Truncation was always display-only, never data loss.

        The approval record keeps `args` (what the executor re-runs) and `diff`
        (what the panel renders) as SEPARATE fields, and the approve path
        executes from args. So even a clipped diff approved under the old cap
        still wrote the complete automation. Pinned here so no future change
        makes the rendered diff the source of truth for execution.
        """
        import asyncio as _asyncio
        from unittest.mock import AsyncMock as _AsyncMock

        from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool
        from custom_components.phoenix_mcp.tools.authoring import _truncate

        class _ApprStore:
            def __init__(self) -> None:
                self._p: list = []
                self.async_lock = _asyncio.Lock()
                self.async_save = _AsyncMock()

            def get_pending_approvals(self) -> list:
                return self._p

            def set_pending_approvals(self, v: list) -> None:
                self._p = v

        big_cfg = {
            "alias": "Long",
            "trigger": [{"platform": "state", "entity_id": "input_boolean.x"}],
            "action": [
                {"service": "homeassistant.toggle", "entity_id": f"input_boolean.x_{i}",
                 "data": {"note": f"step {i} of a long automation body"}}
                for i in range(60)
            ],
        }
        store = _ApprStore()
        data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(),
                       versions=VersionStore(), mesa=None)
        token = _token(cap_automation_write="allow")
        created = _json((await _call_tool("create_automation", {"config": big_cfg}, token, hass, data))[0])
        aid = created["id"]

        # Gate an edit carrying a LARGE config so a real pending approval is
        # created with content that a display cap would have clipped.
        edit_cfg = dict(big_cfg, alias="Replaced")
        confirm_token = _token(cap_automation_write="confirm")
        confirm_token.id = token.id
        _r, outcome, _res = await _call_tool(
            "edit_automation", {"automation_id": aid, "config": edit_cfg},
            confirm_token, hass, data,
        )
        assert outcome == "pending_approval"
        appr = store._p[0]

        # The stored args hold the config in FULL. (HA's validator normalizes
        # keys in place, action -> actions, so assert on content, not shape.)
        args_json = json.dumps(appr["args"]["config"])
        assert appr["args"]["config"]["alias"] == "Replaced"
        assert "step 59 of a long automation body" in args_json

        # A clipped RENDERING of that same config loses the tail; the args the
        # executor runs from are untouched by any such clipping. This is the
        # display-only property, made explicit.
        clipped = _truncate(args_json, max_chars=50)
        assert "more characters)" in clipped
        assert "step 59 of a long automation body" not in clipped

        # Approving executes from args, so the complete automation is written.
        out = await async_execute_approved_tool("edit_automation", appr["args"], token, hass, data)
        assert out[1] == "allowed"
        items = _read_automations_yaml(str(hass.config.config_dir) + "/automations.yaml")
        written = next(a for a in items if a.get("id") == aid)
        assert written["alias"] == "Replaced"
        assert len(written.get("actions") or written.get("action")) == 60
        assert "step 59 of a long automation body" in json.dumps(written)


class TestListTools:
    async def test_list_automations_reports_id_for_reading(self, hass):
        data = _data()
        token = _token(
            PermissionTree(domains={"automation": PermissionNode(state="GREEN")}),
            cap_registry_read="allow",
        )
        hass.states.async_set("automation.porch", "on", {"friendly_name": "Porch", "id": "auto-1"})
        result, outcome, _res = await _call_tool("list_automations", {}, token, hass, data)
        assert outcome == "allowed"
        body = _json(result)
        assert body["count"] == 1
        row = body["automations"][0]
        assert row["entity_id"] == "automation.porch"
        assert row["automation_id"] == "auto-1"
        assert row["alias"] == "Porch"

    async def test_list_scripts_derives_id_from_object_id(self, hass):
        data = _data()
        token = _token(
            PermissionTree(domains={"script": PermissionNode(state="GREEN")}),
            cap_registry_read="allow",
        )
        hass.states.async_set("script.morning", "off", {"friendly_name": "Morning"})
        body = _json((await _call_tool("list_scripts", {}, token, hass, data))[0])
        assert body["scripts"][0]["script_id"] == "morning"

    async def test_lists_are_scoped_to_the_token(self, hass):
        data = _data()
        token = _token(
            PermissionTree(entities={"automation.visible": PermissionNode(state="GREEN")}),
            cap_registry_read="allow",
        )
        hass.states.async_set("automation.visible", "on", {"id": "a1"})
        hass.states.async_set("automation.hidden", "on", {"id": "a2"})
        body = _json((await _call_tool("list_automations", {}, token, hass, data))[0])
        assert [a["entity_id"] for a in body["automations"]] == ["automation.visible"]

    async def test_list_denied_without_registry_read(self, hass):
        data = _data()
        token = _token(cap_registry_read="deny")
        _r, outcome, _res = await _call_tool("list_automations", {}, token, hass, data)
        assert outcome == "denied"


class TestGetScript:
    async def test_round_trip(self, hass):
        hass.services.async_register("script", "reload", lambda call: None)
        data = _data()
        token = _token(cap_script_write="allow")
        cfg = {"alias": "Morning", "sequence": [{"delay": {"seconds": 1}}]}
        await _call_tool("create_script", {"script_id": "morning", "config": cfg}, token, hass, data)

        result, outcome, _res = await _call_tool("get_script", {"script_id": "morning"}, token, hass, data)
        assert outcome == "allowed"
        body = _json(result)
        assert body["script_id"] == "morning"
        assert body["config"]["alias"] == "Morning"
        assert body["config"]["sequence"]
        assert len(body["content_hash"]) == 64

    async def test_missing_script_is_not_found(self, hass):
        data = _data()
        token = _token(cap_script_write="allow")
        _r, outcome, _res = await _call_tool("get_script", {"script_id": "ghost"}, token, hass, data)
        assert outcome == "not_found"


class TestGetScene:
    @pytest.fixture
    def scene_env(self, hass: HomeAssistant):
        entry = MockConfigEntry(domain="test_integration", entry_id="e1")
        entry.add_to_hass(hass)
        reg = er.async_get(hass)
        reg.async_get_or_create("light", "test_integration", "u1", config_entry=entry,
                                suggested_object_id="lamp")
        reg.async_get_or_create("light", "test_integration", "u2", config_entry=entry,
                                suggested_object_id="secret_lamp")
        hass.states.async_set("light.lamp", "on", {})
        hass.states.async_set("light.secret_lamp", "on", {})
        hass.services.async_register("scene", "reload", lambda call: None)
        return hass

    async def test_round_trip(self, hass, scene_env):
        data = _data()
        token = _token(
            PermissionTree(domains={"light": PermissionNode(state="GREEN"),
                                    "scene": PermissionNode(state="GREEN")}),
            cap_scene_write="allow",
        )
        created = _json((await _call_tool(
            "create_scene",
            {"config": {"name": "Movie", "entities": {"light.lamp": "on"}}},
            token, hass, data))[0])
        scene_id = created["id"]

        result, outcome, _res = await _call_tool("get_scene", {"scene_id": scene_id}, token, hass, data)
        assert outcome == "allowed"
        body = _json(result)
        assert body["config"]["name"] == "Movie"
        assert body["config"]["entities"] == {"light.lamp": "on"}
        assert len(body["content_hash"]) == 64

    async def test_scene_with_unwritable_member_is_refused_like_a_missing_scene(self, hass, scene_env):
        # The scene id must not be an existence oracle: a scene controlling an
        # out-of-scope entity gets the same refusal as one that does not exist.
        data = _data()
        writer = _token(
            PermissionTree(domains={"light": PermissionNode(state="GREEN"),
                                    "scene": PermissionNode(state="GREEN")}),
            cap_scene_write="allow",
        )
        created = _json((await _call_tool(
            "create_scene",
            {"config": {"name": "Secret", "entities": {"light.secret_lamp": "on"}}},
            writer, hass, data))[0])
        scene_id = created["id"]

        narrow = _token(
            PermissionTree(entities={"light.lamp": PermissionNode(state="GREEN")}),
            cap_scene_write="allow",
        )
        blocked, outcome, _res = await _call_tool("get_scene", {"scene_id": scene_id}, narrow, hass, data)
        missing, missing_outcome, _r2 = await _call_tool("get_scene", {"scene_id": "no-such"}, narrow, hass, data)
        # Same outcome and same message template either way. Each message echoes
        # the caller's own scene_id, which reveals nothing it did not supply; what
        # matters is that "exists but out of scope" is indistinguishable from
        # "does not exist".
        assert outcome == missing_outcome == "denied"
        suffix = ", or it controls entities outside your write scope."
        assert _text(blocked) == f"No scene found with id '{scene_id}'{suffix}"
        assert _text(missing) == f"No scene found with id 'no-such'{suffix}"
