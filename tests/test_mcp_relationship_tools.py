"""Tests for get_relationships and describe_entity (reverse/forward references).

Automation references go through mesa-core's public entities_by_role; script and
scene references are extracted by Phoenix MCP. Forward references are scoped to entities
the token can access; reverse references describe the accessible entity itself.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock

import pytest
import yaml
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _token(cap_search: str = "allow", **caps) -> TokenRecord:
    tree = PermissionTree(domains={
        "light": PermissionNode(state="GREEN"),
        "automation": PermissionNode(state="GREEN"),
    })
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u",
        cap_search=cap_search, permissions=tree, **caps,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _dangling(body: dict) -> list[str]:
    """Just the dead ids. Most assertions are about WHICH ids survive the
    filter; the holder detail has its own test."""
    return [d["entity_id"] for d in body["dangling_references"]]


async def _call(name, args, token, hass):
    args = dict(args)
    if name == "get_relationships":
        selectors = {
            "entity_id": "entity",
            "device_id": "device",
            "integration": "integration",
            "area": "area",
            "label": "label",
        }
        for legacy, kind in selectors.items():
            if legacy in args:
                args["scope"] = {"kind": kind, "id": args.pop(legacy)}
                break
    data = MagicMock()
    data.mesa = None  # deterministic: describe_entity skips the MESA block
    return await _call_tool(name, args, token, hass, data)


def _write(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


@pytest.fixture
def rel_env(hass: HomeAssistant):
    entry = MockConfigEntry(domain="test_integration", entry_id="e1")
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    for slug, uid in (("kitchen", "uid_k"), ("bedroom", "uid_b")):
        e = ent_reg.async_get_or_create("light", "test_integration", uid, config_entry=entry, suggested_object_id=slug)
        hass.states.async_set(e.entity_id, "on", {})
    auto = ent_reg.async_get_or_create(
        "automation", "test_integration", "auto1", config_entry=entry, suggested_object_id="morning")
    hass.states.async_set(auto.entity_id, "on", {})
    # sensor.secret stays ungranted/denied.
    hass.states.async_set("sensor.secret", "1", {})

    _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
        {
            "id": "auto1", "alias": "Morning",
            "trigger": [{"platform": "state", "entity_id": "light.kitchen"}],
            "action": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}],
        },
    ])
    _write(hass.config.path("scripts.yaml"), {
        "greet": {"alias": "Greet", "sequence": [
            {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]},
    })
    _write(hass.config.path("scenes.yaml"), [
        {"id": "s1", "name": "Evening", "entities": {"light.kitchen": "on"}},
    ])
    _write(hass.config.path("groups.yaml"), {
        "downstairs_lights": {
            "name": "Downstairs lights",
            "entities": ["light.kitchen", "light.bedroom"],
        },
    })
    return {"automation": auto.entity_id}


class TestGetRelationships:
    async def test_deny_without_cap(self, hass, rel_env):
        _, outcome, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(cap_search="deny"), hass)
        assert outcome == "denied"

    async def test_consumers_are_grouped_by_consumer(self, hass, rel_env):
        """The grouping IS the point: entity -> consumers made a caller union N
        results by hand to learn which automations to edit."""
        content, outcome, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        by_kind = {c["kind"]: c for c in body["consumers"]}
        assert by_kind["automation"]["name"] == "Morning"
        assert by_kind["automation"]["roles"] == ["trigger"]
        assert by_kind["automation"]["entities"] == [
            {"entity_id": "light.kitchen", "roles": ["trigger"]}]
        assert by_kind["script"]["id"] == "greet"
        assert by_kind["scene"]["name"] == "Evening"
        assert by_kind["group"]["id"] == "downstairs_lights"
        assert by_kind["group"]["roles"] == ["member"]
        assert body["scope"] == {
            "selector": "entity_id", "value": "light.kitchen",
            "entity_ids": ["light.kitchen"], "count": 1}

    async def test_a_consumer_reports_each_entity_in_its_own_role(self, hass, rel_env):
        """One automation can use A as a trigger and B as an action; a single
        roles list would say both entities do both."""
        content, _, _ = await _call(
            "get_relationships", {"integration": "test_integration"}, _token(), hass)
        auto = next(c for c in _json(content)["consumers"] if c["kind"] == "automation")
        roles = {e["entity_id"]: e["roles"] for e in auto["entities"]}
        assert roles["light.kitchen"] == ["trigger"]
        assert roles["light.bedroom"] == ["action"]
        assert auto["roles"] == ["action", "trigger"]

    async def test_forward_references_scoped(self, hass, rel_env):
        content, _, _ = await _call("get_relationships", {"entity_id": rel_env["automation"]}, _token(), hass)
        body = _json(content)
        # The automation references both lights; both are accessible.
        assert set(body["references"]) == {"light.bedroom", "light.kitchen"}

    async def test_forward_references_follow_include_layout(self, hass, rel_env):
        """Forward references must use the same include-aware walk as consumers."""
        os.remove(os.path.join(hass.config.config_dir, "automations.yaml"))
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("automation: !include_dir_list automations/\n")
        os.makedirs(os.path.join(hass.config.config_dir, "automations"), exist_ok=True)
        _write(os.path.join(hass.config.config_dir, "automations", "one.yaml"), {
            "id": "auto1", "alias": "Split Out",
            "trigger": [],
            "action": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
        })

        content, _, _ = await _call(
            "get_relationships", {"entity_id": rel_env["automation"]}, _token(), hass)
        body = _json(content)
        assert body["references"] == ["light.kitchen"]
        assert "references_not_searched" not in body

    async def test_forward_script_references_follow_include_layout(self, hass, rel_env):
        """Scripts use the named-entry form of the same include-aware loader."""
        from custom_components.phoenix_mcp.tools.discovery import _forward_references_details

        os.remove(hass.config.path("scripts.yaml"))
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("script: !include_dir_named scripts/\n")
        os.makedirs(hass.config.path("scripts"), exist_ok=True)
        _write(hass.config.path("scripts/greet.yaml"), {
            "alias": "Greet",
            "sequence": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
        })

        references, not_searched = await hass.async_add_executor_job(
            _forward_references_details, hass, _token(), "script.greet",
        )
        assert references == ["light.kitchen"]
        assert not not_searched

    async def test_forward_references_only_for_a_single_entity(self, hass, rel_env):
        """"What does it reference" has no meaning for a set of entities."""
        content, _, _ = await _call(
            "get_relationships", {"integration": "test_integration"}, _token(), hass)
        assert "references" not in _json(content)

    async def test_forward_excludes_out_of_scope(self, hass, rel_env):
        # Add an action targeting sensor.secret (denied); it must not appear.
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {
                "id": "auto1", "alias": "Morning",
                "trigger": [{"platform": "state", "entity_id": "light.kitchen"}],
                "action": [{"service": "homeassistant.update_entity", "target": {"entity_id": "sensor.secret"}}],
            },
        ])
        content, _, _ = await _call("get_relationships", {"entity_id": rel_env["automation"]}, _token(), hass)
        assert "sensor.secret" not in _json(content)["references"]

    async def test_inaccessible_not_found(self, hass, rel_env):
        _, outcome, _ = await _call("get_relationships", {"entity_id": "sensor.secret"}, _token(), hass)
        assert outcome == "not_found"

    async def test_an_out_of_scope_entity_never_appears_as_a_consumer_entity(self, hass, rel_env):
        """Scope is built from resolve() before any scan, so a denied entity
        cannot be named even when a consumer in scope also touches it."""
        content, _, _ = await _call(
            "get_relationships", {"integration": "test_integration"}, _token(), hass)
        named = {e["entity_id"] for c in _json(content)["consumers"] for e in c["entities"]}
        assert "sensor.secret" not in named

    async def test_uses_executor_for_file_io(self, hass, rel_env, monkeypatch):
        """_scan_relationships/_forward_references_details read YAML files synchronously;
        they must run via async_add_executor_job rather than blocking the loop."""
        from custom_components.phoenix_mcp.tools.discovery import _forward_references_details, _scan_relationships

        seen_fns = []
        orig = hass.async_add_executor_job

        async def spy(func, *args):
            seen_fns.append(func)
            return await orig(func, *args)

        monkeypatch.setattr(hass, "async_add_executor_job", spy)
        await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert _scan_relationships in seen_fns
        assert _forward_references_details in seen_fns

    async def test_reverse_references_follow_device_triggers(self, hass, rel_env):
        """An automation referencing this entity only through a device trigger
        (no entity_id anywhere in its config) still shows up, via mesa-core's
        expand_target host callback."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er_mod

        entry = MockConfigEntry(domain="test_integration", entry_id="dev_e")
        entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test_integration", "d1")})
        dev_light = er_mod.async_get(hass).async_get_or_create(
            "light", "test_integration", "uid_dev", config_entry=entry,
            suggested_object_id="rack", device_id=device.id)
        hass.states.async_set(dev_light.entity_id, "on", {})

        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {
                "id": "auto_dev", "alias": "Device trig",
                "trigger": [{"platform": "device", "device_id": device.id,
                             "domain": "light", "type": "turned_on"}],
                "action": [],
            },
        ])
        content, outcome, _ = await _call(
            "get_relationships", {"entity_id": dev_light.entity_id}, _token(), hass)
        assert outcome == "allowed"
        autos = [c for c in _json(content)["consumers"] if c["kind"] == "automation"]
        assert autos and autos[0]["name"] == "Device trig"
        assert autos[0]["roles"] == ["trigger"]


class TestRelationshipSelectors:
    """One call per SCOPE, not per entity. Sweeping six devices' worth of
    entities took ~74 calls against a 60/min rate limit, 63 of them empty."""

    async def test_exactly_one_selector_is_required(self, hass, rel_env):
        for args in ({}, {"entity_id": "light.kitchen", "integration": "test_integration"}):
            content, outcome, _ = await _call("get_relationships", args, _token(), hass)
            assert outcome == "invalid_request"
            assert any(
                phrase in content["content"][0]["text"]
                for phrase in ("scope must", "no longer accepts")
            )

    async def test_integration_scope_covers_every_entity_at_once(self, hass, rel_env):
        content, outcome, _ = await _call(
            "get_relationships", {"integration": "test_integration"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert set(body["scope"]["entity_ids"]) >= {"light.kitchen", "light.bedroom"}
        assert {c["kind"] for c in body["consumers"]} == {
            "automation", "script", "scene", "group"
        }

    async def test_device_scope(self, hass, rel_env):
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er_mod

        entry = MockConfigEntry(domain="test_integration", entry_id="dev_scope")
        entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test_integration", "dscope")})
        light = er_mod.async_get(hass).async_get_or_create(
            "light", "test_integration", "uid_scope", config_entry=entry,
            suggested_object_id="desk", device_id=device.id)
        hass.states.async_set(light.entity_id, "on", {})
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "a", "alias": "Desk", "trigger": [], "action": [
                {"service": "light.turn_on", "target": {"entity_id": light.entity_id}}]},
        ])
        content, outcome, _ = await _call(
            "get_relationships", {"device_id": device.id}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["scope"]["entity_ids"] == [light.entity_id]
        assert _json(content)["consumers"][0]["name"] == "Desk"

    async def test_area_scope_accepts_a_name_as_well_as_an_id(self, hass, rel_env):
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import entity_registry as er_mod

        area = ar.async_get(hass).async_get_or_create("Kitchen")
        er_mod.async_get(hass).async_update_entity("light.kitchen", area_id=area.id)
        for value in (area.id, "Kitchen", "kitchen"):
            content, outcome, _ = await _call(
                "get_relationships", {"area": value}, _token(), hass)
            assert outcome == "allowed", value
            assert _json(content)["scope"]["entity_ids"] == ["light.kitchen"]

    async def test_label_scope(self, hass, rel_env):
        from homeassistant.helpers import entity_registry as er_mod

        er_mod.async_get(hass).async_update_entity("light.kitchen", labels={"critical"})
        content, outcome, _ = await _call(
            "get_relationships", {"label": "critical"}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["scope"]["entity_ids"] == ["light.kitchen"]

    async def test_an_empty_scope_is_the_same_answer_however_it_got_there(self, hass, rel_env):
        """Rule 12: a device that does not exist and one whose entities are out
        of this token's tree must not be distinguishable."""
        missing, out1, _ = await _call(
            "get_relationships", {"device_id": "no_such_device"}, _token(), hass)
        unknown_platform, out2, _ = await _call(
            "get_relationships", {"integration": "not_installed"}, _token(), hass)
        assert out1 == out2 == "not_found"
        assert missing["content"][0]["text"] == unknown_platform["content"][0]["text"]


class TestRelationshipCoverage:
    """A fast call with silent blind spots is worse than a slow one: it produces
    a confident wrong answer to "is anything still using this"."""

    async def test_missing_caps_are_reported_not_hidden(self, hass, rel_env):
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        body = _json(content)
        assert body["searched"] == ["automation", "script", "scene", "group"]
        assert {n["kind"] for n in body["not_searched"]} == {
            "dashboard", "config_entry", "reference_graph",
        }
        cap_gaps = [n for n in body["not_searched"] if n["kind"] != "reference_graph"]
        assert all("requires cap_" in n["reason"] for n in cap_gaps)

    async def test_reference_graph_adds_a_consumer_absent_from_yaml(
        self, hass, rel_env, monkeypatch,
    ):
        """HA's loaded graph covers consumers Phoenix cannot read from YAML."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        registry = er.async_get(hass)
        graph_only = registry.async_get_or_create(
            "automation",
            "test_integration",
            "graph-only-id",
            suggested_object_id="graph_only",
        )
        hass.states.async_set(graph_only.entity_id, "on", {"friendly_name": "Graph only"})

        async def _graph(hass_, command, payload):
            assert command == "search/related"
            assert payload == {"item_type": "entity", "item_id": "light.kitchen"}
            return {"automation": {graph_only.entity_id}}

        monkeypatch.setattr(disc, "async_ws_command", _graph)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"}, _token(), hass,
        )
        body = _json(content)
        consumer = next(c for c in body["consumers"] if c["name"] == "Graph only")
        assert consumer == {
            "kind": "automation",
            "id": "graph-only-id",
            "name": "Graph only",
            "roles": ["reference"],
            "entities": [{"entity_id": "light.kitchen", "roles": ["reference"]}],
        }
        assert "person" in body["searched"]
        assert not [n for n in body.get("not_searched", []) if n["kind"] == "reference_graph"]

    async def test_real_home_assistant_graph_reaches_the_tool(
        self, hass, hass_admin_user,
    ):
        """Contract test: drive HA's real graph through Phoenix end to end."""
        from homeassistant.setup import async_setup_component

        hass.states.async_set("light.graph_target", "off")
        assert await async_setup_component(hass, "automation", {
            "automation": [{
                "id": "real-graph-contract",
                "alias": "Real graph contract",
                "trigger": [{"platform": "state", "entity_id": "light.graph_target"}],
                "action": [],
            }],
        })
        await hass.async_block_till_done()
        assert await async_setup_component(hass, "search", {"search": {}})

        content, outcome, _ = await _call(
            "get_relationships", {"entity_id": "light.graph_target"}, _token(), hass,
        )
        assert outcome == "allowed"
        consumer = next(
            item for item in _json(content)["consumers"]
            if item["name"] == "Real graph contract"
        )
        assert consumer["id"] == "real-graph-contract"
        assert consumer["roles"] == ["reference"]
        assert consumer["entities"] == [
            {"entity_id": "light.graph_target", "roles": ["reference"]},
        ]

    async def test_reference_graph_merges_without_weakening_yaml_roles(
        self, hass, rel_env, monkeypatch,
    ):
        """A graph hit must not duplicate a YAML hit or replace trigger/action detail."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        async def _graph(hass_, command, payload):
            return {"automation": {rel_env["automation"]}}

        monkeypatch.setattr(disc, "async_ws_command", _graph)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"}, _token(), hass,
        )
        automations = [
            c for c in _json(content)["consumers"]
            if c["kind"] == "automation" and c["id"] == "auto1"
        ]
        assert len(automations) == 1
        assert automations[0]["roles"] == ["trigger"]
        assert automations[0]["entities"] == [
            {"entity_id": "light.kitchen", "roles": ["trigger"]},
        ]

    async def test_reference_graph_failure_keeps_yaml_and_discloses_the_gap(
        self, hass, rel_env, monkeypatch,
    ):
        """An unavailable graph is partial evidence, never an empty graph."""
        import custom_components.phoenix_mcp.tools.discovery as disc
        from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError

        async def _unavailable(hass_, command, payload):
            raise WsDispatchError("WebSocket command not available: search/related")

        monkeypatch.setattr(disc, "async_ws_command", _unavailable)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"}, _token(), hass,
        )
        body = _json(content)
        assert {c["kind"] for c in body["consumers"]} >= {
            "automation", "script", "scene", "group",
        }
        gap = next(n for n in body["not_searched"] if n["kind"] == "reference_graph")
        assert "UI-loaded" in gap["reason"]

    async def test_reference_graph_queries_every_entity_in_a_broad_scope(
        self, hass, rel_env, monkeypatch,
    ):
        """The broad selector must not regain the exact-entity graph blind spot."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        queried = []

        async def _graph(hass_, command, payload):
            queried.append(payload["item_id"])
            return {}

        monkeypatch.setattr(disc, "async_ws_command", _graph)
        content, _, _ = await _call(
            "get_relationships", {"integration": "test_integration"}, _token(), hass,
        )
        body = _json(content)
        assert queried == sorted(body["scope"]["entity_ids"])
        assert "person" in body["searched"]

    async def test_reference_graph_unknown_consumer_type_is_a_visible_gap(
        self, hass, rel_env, monkeypatch,
    ):
        """A future HA relation must not be silently misread as completeness."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        async def _future(hass_, command, payload):
            return {"future_consumer": {"future.example"}}

        monkeypatch.setattr(disc, "async_ws_command", _future)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"}, _token(), hass,
        )
        gap = next(
            item for item in _json(content)["not_searched"]
            if item["kind"] == "future_consumer"
        )
        assert "does not model" in gap["reason"]

    async def test_registry_write_preview_uses_the_same_reference_graph(
        self, hass, rel_env, monkeypatch,
    ):
        """Rename/delete approval previews need the same safety coverage."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        registry = er.async_get(hass)
        graph_only = registry.async_get_or_create(
            "automation",
            "test_integration",
            "preview-graph-id",
            suggested_object_id="preview_graph",
        )
        hass.states.async_set(graph_only.entity_id, "on", {"friendly_name": "Preview graph"})

        async def _dispatch(hass_, command, payload):
            if command == "search/related":
                return {"automation": {graph_only.entity_id}}
            assert command == "lovelace/dashboards/list"
            return []

        async def _dashboard(hass_, url_path):
            return None

        monkeypatch.setattr(disc, "async_ws_command", _dispatch)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _dashboard)
        preview = await disc._registry_relationships_preview(hass, ["light.kitchen"])
        assert any(
            consumer["id"] == "preview-graph-id"
            for consumer in preview["consumers"]
        )
        assert "person" in preview["searched"]

    async def test_dashboards_are_searched_with_a_patchable_path(self, hass, rel_env, monkeypatch):
        """The path is patch_dashboard's own addressing form, so a hit is
        directly actionable rather than something to go and find."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        layout = {"views": [{"cards": [
            {"type": "tile", "entity": "light.kitchen"},
            {"type": "tile", "entity": "light.elsewhere"},
        ]}]}

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return layout if url_path == "home" else None

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        body = _json(content)
        assert "dashboard" in body["searched"]
        dash = next(c for c in body["consumers"] if c["kind"] == "dashboard")
        assert dash["id"] == "home" and dash["name"] == "Home"
        assert dash["paths"] == [["views", 0, "cards", 0, "entity"]]

    async def test_an_unreadable_dashboard_is_reported_not_skipped(self, hass, rel_env, monkeypatch):
        """A YAML-mode dashboard cannot be searched. Skipping it while still
        claiming "searched: dashboard" is the confident-wrong-answer this whole
        tool exists to avoid, so the skip has to be visible."""
        import custom_components.phoenix_mcp.tools.discovery as disc
        from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError

        async def _list(hass_, cmd, payload):
            return [{"url_path": "yamlmode", "title": "Legacy"}]

        async def _config(hass_, url_path):
            raise WsDispatchError("dashboard is not in storage mode")

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        body = _json(content)
        skipped = [n for n in body["not_searched"] if n.get("id") == "yamlmode"]
        assert skipped and "YAML-mode" in skipped[0]["reason"]

    async def test_a_failed_dashboard_list_says_none_were_searched(self, hass, rel_env, monkeypatch):
        import custom_components.phoenix_mcp.tools.discovery as disc
        from custom_components.phoenix_mcp.ws_dispatch import WsDispatchError

        async def _list(hass_, cmd, payload):
            raise WsDispatchError("lovelace not loaded")

        async def _config(hass_, url_path):
            return None

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        reasons = [n["reason"] for n in _json(content)["not_searched"]]
        assert any("none were searched" in r for r in reasons)

    async def test_an_absent_default_dashboard_is_not_reported_as_a_gap(self, hass, rel_env, monkeypatch):
        """An auto-generated default has nothing stored to search, and saying so
        on every call would train the reader to ignore the field."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        async def _list(hass_, cmd, payload):
            return []

        async def _config(hass_, url_path):
            return None

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        body = _json(content)
        assert "dashboard" in body["searched"]
        assert not [n for n in body.get("not_searched", []) if n["kind"] == "dashboard"]

    async def test_a_config_entry_built_on_an_entity_is_found(self, hass, rel_env):
        """The live gap this closes: a helper config entry keeps an integration
        alive, and nothing in HA records that dependency."""
        helper = MockConfigEntry(
            domain="attribute_as_sensor", entry_id="helper1", title="Kitchen brightness",
            options={"entity_id": "light.kitchen", "attribute": "brightness"},
        )
        helper.add_to_hass(hass)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_integration_write="allow"), hass)
        body = _json(content)
        assert "config_entry" in body["searched"]
        entry = next(c for c in body["consumers"] if c["kind"] == "config_entry")
        assert entry["id"] == "helper1" and entry["name"] == "Kitchen brightness"
        assert entry["roles"] == ["attribute_as_sensor"]

    async def test_a_config_entry_key_name_is_never_assumed(self, hass, rel_env):
        """Integrations store their source entity under whatever key they like
        (entity_id, source, entity_ids); matching the VALUE is what keeps this
        from silently reporting no dependency."""
        helper = MockConfigEntry(
            domain="derivative", entry_id="helper2", title="Rate",
            options={"source": "light.kitchen"},
        )
        helper.add_to_hass(hass)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_integration_write="allow"), hass)
        assert any(c["id"] == "helper2" for c in _json(content)["consumers"])

    async def test_dangling_references_come_free_with_the_walk(self, hass, rel_env):
        """An id referenced by something but present in neither hass.states nor
        the registry can never resolve again, for anyone."""
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "a", "alias": "Dead", "trigger": [
                {"platform": "state", "entity_id": "light.kitchen"}], "action": [
                {"service": "lock.lock", "target": {"entity_id": "lock.front_door"}}]},
        ])
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert "lock.front_door" in _dangling(_json(content))

    async def test_a_dead_reference_in_a_domain_that_is_gone_is_still_reported(self, hass, rel_env):
        """The first filter tried was a domain-existence check on everything,
        and this is what it got wrong: a reference into a domain the instance no
        longer has at all can never resolve, which makes it MORE dangling.
        `lock` has no entities and no services here, and lock.front_door must
        still be reported."""
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "a", "alias": "Dead", "trigger": [
                {"platform": "state", "entity_id": "light.kitchen"}], "action": [
                {"service": "lock.lock", "target": {"entity_id": "lock.front_door"}}]},
        ])
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert "lock.front_door" in _dangling(_json(content))

    @pytest.mark.parametrize("value", [
        "{{ trigger.entity_id }}", "input_boolean.{{ fan | lower }}_state", "all",
    ])
    async def test_a_runtime_computed_target_is_not_a_dead_reference(self, hass, rel_env, value):
        """Live-found: scripts targeting a templated entity_id, and HA's `all`
        wildcard, filled most of the first real dangling list."""
        _write(hass.config.path("scripts.yaml"), {
            "greet": {"alias": "Greet", "sequence": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}},
                {"service": "light.turn_off", "target": {"entity_id": value}}]},
        })
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert _dangling(_json(content)) == []

    @pytest.mark.parametrize("junk", ["1.3em", "3.4", "attributes.device_class", "deye_p3.yaml"])
    async def test_a_dashboard_string_that_merely_looks_like_an_id_is_not_reported(
        self, hass, rel_env, monkeypatch, junk,
    ):
        """A card's CSS, a version string, a template fragment and a filename all
        match the entity-id shape. The value walk has no type information, so it
        gets the domain check the typed sources deliberately do not."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [
                {"type": "tile", "entity": "light.kitchen", "height": junk}]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert _dangling(_json(content)) == []

    async def test_a_dashboard_reference_to_a_removed_entity_is_still_reported(
        self, hass, rel_env, monkeypatch,
    ):
        """The domain check must not cost the case it exists to serve: a
        dashboard pointing at an entity that no longer exists is exactly the
        ghost that makes a layout unwritable."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [
                {"type": "tile", "entity": "light.kitchen"},
                {"type": "tile", "entity": "light.removed_last_year"}]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert _dangling(_json(content)) == ["light.removed_last_year"]

    async def test_a_domain_with_services_but_no_entities_still_counts(
        self, hass, rel_env, monkeypatch,
    ):
        """The domain set unions services precisely for this: an integration is
        loaded but its last entity has been removed, so a dashboard still
        pointing at one is a real dead reference and the domain must not read as
        made-up. Without the services union the check falls back on entities
        alone and this reference disappears."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        hass.services.async_register("lock", "lock", lambda call: None)

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [
                {"type": "tile", "entity": "light.kitchen"},
                {"type": "tile", "entity": "lock.front_door"}]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert "lock.front_door" in _dangling(_json(content))

    async def test_a_card_calling_a_service_is_not_a_dead_entity(self, hass, rel_env, monkeypatch):
        """Live-found. A card's perform_action holds `button.press`, which has
        the exact shape of an entity id in the exact position the value walk
        reads. Registration is what separates it from a real dead reference."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        hass.services.async_register("button", "press", lambda call: None)

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [{
                "type": "button", "entity": "light.kitchen",
                "tap_action": {"action": "perform-action", "perform_action": "button.press"},
            }]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert _dangling(_json(content)) == []

    async def test_a_card_calling_a_script_that_is_gone_is_still_reported(
        self, hass, rel_env, monkeypatch,
    ):
        """The reason the rule is registration and not the field name: running a
        script IS its service name, so a card pointing at a deleted script looks
        exactly like the service call above and is the most valuable find here.
        A field-name rule would discard it with the noise."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        # Any instance that can have a dead script reference has the script
        # integration loaded, which is what puts `script` in the domain set.
        hass.services.async_register("script", "turn_on", lambda call: None)

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [{
                "type": "button", "entity": "light.kitchen",
                "tap_action": {"action": "perform-action",
                               "perform_action": "script.deleted_last_year"},
            }]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert _dangling(_json(content)) == ["script.deleted_last_year"]

    async def test_a_script_targeting_a_service_name_is_still_a_bad_reference(self, hass, rel_env):
        """The service exclusion is scoped to the value walk, and this is why.
        An `entity_id:` key states that its value was MEANT to be an entity, so
        one holding a service name is a config error worth surfacing, not a
        service call to filter away."""
        hass.services.async_register("button", "press", lambda call: None)
        _write(hass.config.path("scripts.yaml"), {
            "greet": {"alias": "Greet", "sequence": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}},
                {"service": "button.press", "target": {"entity_id": "button.press"}}]},
        })
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert _dangling(_json(content)) == ["button.press"]

    async def test_a_dangling_reference_names_what_still_points_at_it(
        self, hass, rel_env, monkeypatch,
    ):
        """"script.x is dead" sends someone hunting; naming the dashboard and
        the card path is a repair. The walk already had the holder in hand."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "auto1", "alias": "Morning", "trigger": [
                {"platform": "state", "entity_id": "light.kitchen"}], "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.gone"}}]},
        ])

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [
                {"type": "tile", "entity": "light.kitchen"},
                {"type": "tile", "entity": "light.gone"}]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        entry = next(d for d in _json(content)["dangling_references"]
                     if d["entity_id"] == "light.gone")
        holders = {h["kind"]: h for h in entry["referenced_by"]}
        assert holders["automation"]["name"] == "Morning"
        assert holders["dashboard"]["id"] == "home"
        assert holders["dashboard"]["paths"] == [["views", 0, "cards", 1, "entity"]]
        assert holders["dashboard"]["path_count"] == 1
        assert holders["dashboard"]["truncated"] is False
        # The provenance flags are how the filter decides; they are not a caller's
        # business and must not leak into the response.
        assert "typed" not in holders["automation"] and "service" not in holders["automation"]

    async def test_one_vouching_holder_is_enough(self, hass, rel_env, monkeypatch):
        """A dead id can be referenced from several places at once, and the
        holders disagree about how much they knew. An automation's `entity_id:`
        key says this was meant to be an entity; a card's perform_action for the
        same string says nothing (it is where a verb goes). The typed holder
        settles it, so requiring every holder to vouch would lose the finding."""
        import custom_components.phoenix_mcp.tools.discovery as disc

        hass.services.async_register("button", "press", lambda call: None)
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "auto1", "alias": "Morning", "trigger": [
                {"platform": "state", "entity_id": "light.kitchen"}], "action": [
                {"service": "button.press", "target": {"entity_id": "button.gone"}}]},
        ])

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            return {"views": [{"cards": [{
                "type": "button", "entity": "light.kitchen",
                "tap_action": {"action": "perform-action", "perform_action": "button.gone"},
            }]}]}

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        assert _dangling(_json(content)) == ["button.gone"]

    async def test_many_hits_on_one_consumer_group_into_one_holder(
        self, hass, rel_env, monkeypatch,
    ):
        """Live: one deleted script was called from 18 sub-buttons on a single
        dashboard, so an ungrouped list spent most of the response repeating that
        dashboard's identity. The operator's question is WHICH thing to open."""
        import custom_components.phoenix_mcp.tools.discovery as disc
        from custom_components.phoenix_mcp.const import MAX_DANGLING_PATHS

        cards = [{"type": "tile", "entity": "light.kitchen"}] + [
            {"type": "button", "tap_action": {"perform_action": "script.gone"}}
            for _ in range(18)
        ]

        async def _list(hass_, cmd, payload):
            return [{"url_path": "home", "title": "Home"}]

        async def _config(hass_, url_path):
            # None is the default dashboard; only "home" carries the layout.
            return {"views": [{"cards": cards}]} if url_path == "home" else None

        monkeypatch.setattr(disc, "async_ws_command", _list)
        monkeypatch.setattr(disc, "async_get_lovelace_config", _config)
        hass.services.async_register("script", "turn_on", lambda call: None)
        content, _, _ = await _call(
            "get_relationships", {"entity_id": "light.kitchen"},
            _token(cap_lovelace_write="allow"), hass)
        entry = next(d for d in _json(content)["dangling_references"]
                     if d["entity_id"] == "script.gone")
        assert len(entry["referenced_by"]) == 1
        holder = entry["referenced_by"][0]
        # The clipped list reports its own true size, so it cannot read as whole.
        assert holder["path_count"] == 18
        assert len(holder["paths"]) == MAX_DANGLING_PATHS
        assert holder["truncated"] is True

    async def test_one_consumer_referencing_a_dead_id_twice_is_listed_once(self, hass, rel_env):
        """An automation naming the same dead entity in two roles appeared twice."""
        _write(os.path.join(hass.config.config_dir, "automations.yaml"), [
            {"id": "auto1", "alias": "Morning",
             "trigger": [{"platform": "state", "entity_id": "light.gone"},
                         {"platform": "state", "entity_id": "light.kitchen"}],
             "action": [{"service": "light.turn_on", "target": {"entity_id": "light.gone"}}]},
        ])
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        entry = next(d for d in _json(content)["dangling_references"]
                     if d["entity_id"] == "light.gone")
        assert len(entry["referenced_by"]) == 1
        assert entry["referenced_by"][0]["name"] == "Morning"

    async def test_a_non_default_yaml_layout_is_still_read(self, hass, rel_env):
        """Both walks used to open automations.yaml and nothing else, so an
        install routing the domain through !include_dir_list read as EMPTY and
        the tool reported that nothing referenced the entity. Indistinguishable
        from a genuinely unused entity, which is the worst answer here."""
        os.remove(os.path.join(hass.config.config_dir, "automations.yaml"))
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("automation: !include_dir_list automations/\n")
        os.makedirs(os.path.join(hass.config.config_dir, "automations"), exist_ok=True)
        _write(os.path.join(hass.config.config_dir, "automations", "one.yaml"), {
            "id": "split1", "alias": "Split Out",
            "trigger": [{"platform": "state", "entity_id": "light.kitchen"}], "action": [],
        })
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        autos = [c for c in _json(content)["consumers"] if c["kind"] == "automation"]
        assert autos and autos[0]["name"] == "Split Out"

    async def test_a_package_tree_is_named_rather_than_silently_skipped(self, hass, rel_env):
        """HA merges packages into the domain and this does not resolve them, so
        the answer is incomplete and has to say so."""
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("homeassistant:\n  packages: !include_dir_named packages/\n"
                    "automation: !include automations.yaml\n")
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        body = _json(content)
        # Still searched, just not exhaustively; both facts are reported.
        assert "automation" in body["searched"]
        assert any("packages" in n["reason"] for n in body["not_searched"])

    async def test_a_live_entity_is_never_reported_dangling(self, hass, rel_env):
        content, _, _ = await _call("get_relationships", {"entity_id": "light.kitchen"}, _token(), hass)
        assert _dangling(_json(content)) == []


class TestDescribeEntity:
    async def test_deny_without_cap(self, hass, rel_env):
        _, outcome, _ = await _call("describe_entity", {"entity_id": "light.kitchen"}, _token(cap_search="deny"), hass)
        assert outcome == "denied"

    async def test_describe(self, hass, rel_env):
        content, outcome, _ = await _call("describe_entity", {"entity_id": "light.kitchen"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["domain"] == "light"
        assert body["state"] == "on"
        assert body["writable"] is True
        kinds = {r["kind"] for r in body["referenced_by"]}
        assert kinds == {"automation", "script", "scene"}
        assert "mesa_control_mode" not in body  # MagicMock data => skipped via mode check below

    async def test_describe_reports_unreadable_reference_branches(self, hass, rel_env):
        """A short reverse-reference list must not hide package coverage gaps."""
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("homeassistant:\n  packages: !include_dir_named packages/\n"
                    "automation: !include automations.yaml\n"
                    "script: !include scripts.yaml\n"
                    "scene: !include scenes.yaml\n")

        content, _, _ = await _call(
            "describe_entity", {"entity_id": "light.kitchen"}, _token(), hass)
        body = _json(content)
        assert {r["kind"] for r in body["referenced_by"]} == {"automation", "script", "scene"}
        assert any("packages" in item["reason"] for item in body["referenced_by_not_searched"])

    async def test_inaccessible_not_found(self, hass, rel_env):
        _, outcome, _ = await _call("describe_entity", {"entity_id": "sensor.secret"}, _token(), hass)
        assert outcome == "not_found"

    async def test_referenced_by_states_its_own_limit(self, hass, rel_env):
        """This field covers three consumer kinds; get_relationships reaches five.
        Unqualified, a short list here reads as PROOF nothing else uses the
        entity, which is the confident wrong answer get_relationships exists to
        avoid. The difference in coverage is fine; hiding it is not."""
        content, _, _ = await _call("describe_entity", {"entity_id": "light.kitchen"}, _token(), hass)
        note = _json(content)["referenced_by_note"]
        assert "get_relationships" in note
        assert "dashboards" in note and "config entries" in note

    async def test_entity_category_annotated(self, hass, rel_env):
        # describe_entity labels a config/diagnostic entity with its category
        # (a setup/health entity, not a primary control), independent of MESA.
        ent_reg = er.async_get(hass)
        diag = ent_reg.async_get_or_create(
            "light", "test_integration", "uid_diag",
            suggested_object_id="firmware",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        hass.states.async_set(diag.entity_id, "1.0", {})
        content, outcome, _ = await _call("describe_entity", {"entity_id": diag.entity_id}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["entity_category"] == "diagnostic"

    async def test_entity_category_omitted_for_primary(self, hass, rel_env):
        content, _, _ = await _call("describe_entity", {"entity_id": "light.kitchen"}, _token(), hass)
        assert "entity_category" not in _json(content)
