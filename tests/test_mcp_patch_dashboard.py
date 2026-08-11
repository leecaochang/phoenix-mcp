"""Tests for patch_dashboard, the path-addressed single-value dashboard write.

It exists because set_dashboard_config demands the WHOLE layout while a read of
that layout is lossy: rule 12 redacts entities the token cannot resolve, and rule
8 redacts GHOSTS (absent from both hass.states and the registry) before any
permission resolution runs. A dashboard carrying a few dead references therefore
cannot be written back by any token at any permission level without persisting
the placeholder over them. Patching never resends untouched bytes, so redaction
stops being a write barrier, and the sentinel guard makes the failure that
motivated this tool structurally impossible rather than merely avoidable.

Also covers what the card ops cannot address at all: views[].badges[], which is
where the live migration that produced this tool actually got stuck.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import REDACTION_SENTINEL
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.tool_common import resolve_json_path, redaction_sentinel_path
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import async_get_lovelace_config, async_save_lovelace_config


def _token(**caps) -> TokenRecord:
    base = {"cap_lovelace_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(domains={}), **base,
    )


def _data() -> tuple[PhoenixData, VersionStore]:
    versions = VersionStore()
    data = PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)
    return data, versions


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _text(content: dict) -> str:
    return content["content"][0]["text"]


async def _call(args, token=None, hass=None, data=None):
    args = dict(args)
    target = {"path": args.pop("path")}
    if "url_path" in args:
        target["url_path"] = args.pop("url_path")
    change = {"kind": args.pop("op", "set")}
    if "value" in args:
        change["value"] = args.pop("value")
    args["target"] = target
    args["change"] = change
    return await _call_tool(
        "patch_dashboard", args, token, hass, data if data is not None else MagicMock())


# A layout shaped like the one that motivated the tool: a view-level badge (no
# card op can reach it) whose visibility names an entity that no longer exists.
LAYOUT = {
    "views": [
        {
            "title": "Home",
            "badges": [
                {"type": "entity", "entity": "sensor.old_remaining", "name": "Laundry"},
            ],
            "cards": [{"type": "markdown", "content": "a"}],
        }
    ]
}


@pytest.fixture
async def lovelace_env(hass: HomeAssistant):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {}})
    entry = MockConfigEntry(domain="test_integration", entry_id="e1")
    entry.add_to_hass(hass)
    e = er.async_get(hass).async_get_or_create(
        "light", "test_integration", "uid_k", config_entry=entry, suggested_object_id="kitchen")
    hass.states.async_set(e.entity_id, "on", {})
    return hass, e.entity_id


async def _seed(hass, config=None):
    await async_save_lovelace_config(hass, None, config or json.loads(json.dumps(LAYOUT)))


class TestPathResolution:
    """resolve_json_path is surface-agnostic, so it is tested directly."""

    def test_resolves_a_nested_leaf(self):
        root = {"views": [{"badges": [{"entity": "x"}]}]}
        container, key, reason = resolve_json_path(root, ["views", 0, "badges", 0, "entity"])
        assert reason is None and container["entity"] == "x" and key == "entity"

    def test_missing_parent_is_refused_not_created(self):
        root = {"views": []}
        container, _key, reason = resolve_json_path(root, ["views", 0, "badges", 0])
        assert container is None and "segment 1" in reason
        assert root == {"views": []}  # nothing was built along the way

    def test_negative_index_refused(self):
        """Python would accept -1; a stale read computing it edits the wrong end."""
        root = {"views": [{"badges": [{"entity": "x"}]}]}
        container, _key, reason = resolve_json_path(root, ["views", -1, "badges", 0])
        assert container is None and reason is not None

    def test_bool_is_not_an_index(self):
        """bool subclasses int, so True must not silently address element 1."""
        root = {"views": [{"a": 1}, {"b": 2}]}
        container, _key, reason = resolve_json_path(root, ["views", True, "b"])
        assert container is None and reason is not None

    def test_empty_or_wrong_shaped_path(self):
        for bad in ([], None, "views.0", {"a": 1}):
            container, _key, reason = resolve_json_path({"views": []}, bad)
            assert container is None and reason is not None

    def test_type_mismatch_is_named(self):
        root = {"views": [{"badges": []}]}
        _c, _k, reason = resolve_json_path(root, ["views", "0", "badges"])
        assert "integer" in reason


class TestRedactionSentinelDetection:
    def test_finds_nested_sentinel_and_reports_its_path(self):
        value = {"visibility": [{"condition": "state", "entity": REDACTION_SENTINEL}]}
        assert redaction_sentinel_path(value) == ["visibility", 0, "entity"]

    def test_finds_a_sentinel_used_as_a_key(self):
        assert redaction_sentinel_path({REDACTION_SENTINEL: 1}) == [REDACTION_SENTINEL]

    def test_clean_value_returns_none(self):
        assert redaction_sentinel_path({"entity": "sensor.real", "n": [1, 2]}) is None

    def test_bare_string_at_the_root(self):
        assert redaction_sentinel_path(REDACTION_SENTINEL) == []


class TestPatchDashboard:
    async def test_deny_without_cap_never_echoes_the_payload(self, hass, lovelace_env):
        """Rule 29(a): a denied token learns nothing about its own arguments."""
        h, _l = lovelace_env
        await _seed(h)
        content, outcome, _ = await _call(
            {"path": ["views", 0, "badges", 0, "entity"], "value": "sensor.new"},
            _token(cap_lovelace_write="deny"), h)
        assert outcome == "denied"
        assert "sensor.new" not in _text(content)

    async def test_sets_a_view_level_badge(self, hass, lovelace_env):
        """The exact thing no card op can reach."""
        h, _l = lovelace_env
        await _seed(h)
        data, versions = _data()
        content, outcome, _ = await _call(
            {"path": ["views", 0, "badges", 0, "entity"], "value": "sensor.new_remaining"},
            _token(), h, data)
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["views"][0]["badges"][0]["entity"] == "sensor.new_remaining"
        # Everything else is byte-identical: nothing untouched was resent.
        assert stored["views"][0]["cards"] == LAYOUT["views"][0]["cards"]
        assert stored["views"][0]["badges"][0]["name"] == "Laundry"
        body = _json(content)
        assert body["saved"] is True and body["op"] == "set"
        assert body["path"] == "views[0].badges[0].entity"
        assert body["content_hash"] == content_hash(stored)
        hist = versions.list_for("dashboard", "lovelace")
        assert len(hist) == 1 and hist[0].summary == "set views[0].badges[0].entity"

    async def test_refuses_a_value_carrying_the_redaction_sentinel(self, hass, lovelace_env):
        """The failure this whole tool exists to make impossible.

        A caller that reads a layout and hands part of it back would otherwise
        persist the placeholder over real configuration, silently and
        irreversibly (the original entity id is gone from the stored config).
        """
        h, _l = lovelace_env
        await _seed(h)
        before = await async_get_lovelace_config(h, None)
        content, outcome, _ = await _call(
            {"path": ["views", 0, "badges", 0],
             "value": {"type": "entity", "entity": "sensor.ok",
                       "visibility": [{"condition": "state", "entity": REDACTION_SENTINEL}]}},
            _token(), h, _data()[0])
        assert outcome == "invalid_request"
        message = _text(content)
        assert REDACTION_SENTINEL in message
        assert "visibility[0].entity" in message  # names where to look
        assert await async_get_lovelace_config(h, None) == before  # nothing written

    async def test_append_to_a_list(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        _content, outcome, _ = await _call(
            {"path": ["views", 0, "badges"], "op": "append",
             "value": {"type": "entity", "entity": "sensor.second"}},
            _token(), h, _data()[0])
        assert outcome == "allowed"
        badges = (await async_get_lovelace_config(h, None))["views"][0]["badges"]
        assert len(badges) == 2 and badges[1]["entity"] == "sensor.second"

    async def test_append_requires_a_list(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        content, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "op": "append", "value": "x"},
            _token(), h, _data()[0])
        assert outcome == "invalid_request" and "list" in _text(content)

    async def test_remove_drops_the_leaf(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        _content, outcome, _ = await _call(
            {"path": ["views", 0, "badges", 0], "op": "remove"}, _token(), h, _data()[0])
        assert outcome == "allowed"
        assert (await async_get_lovelace_config(h, None))["views"][0]["badges"] == []

    async def test_remove_needs_no_value(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "op": "remove"}, _token(), h, _data()[0])
        assert outcome == "allowed"
        assert "title" not in (await async_get_lovelace_config(h, None))["views"][0]

    async def test_set_requires_a_value(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        content, outcome, _ = await _call(
            {"path": ["views", 0, "title"]}, _token(), h, _data()[0])
        assert outcome == "invalid_request" and "value is required" in _text(content)

    async def test_setting_null_is_not_the_same_as_omitting_value(self, hass, lovelace_env):
        """An explicit null is a real value; only an ABSENT key is 'no value'."""
        h, _l = lovelace_env
        await _seed(h)
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "value": None}, _token(), h, _data()[0])
        assert outcome == "allowed"
        assert (await async_get_lovelace_config(h, None))["views"][0]["title"] is None

    async def test_bad_path_writes_nothing(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        before = await async_get_lovelace_config(h, None)
        content, outcome, _ = await _call(
            {"path": ["views", 0, "sections", 3, "cards"], "value": []}, _token(), h, _data()[0])
        assert outcome == "invalid_request" and "does not exist" in _text(content)
        assert await async_get_lovelace_config(h, None) == before

    async def test_stale_expected_hash_is_refused(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "value": "New", "expected_hash": "0" * 64},
            _token(), h, _data()[0])
        assert outcome == "invalid_request"
        assert (await async_get_lovelace_config(h, None))["views"][0]["title"] == "Home"

    async def test_matching_expected_hash_applies(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        current = await async_get_lovelace_config(h, None)
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "value": "New",
             "expected_hash": content_hash(current)},
            _token(), h, _data()[0])
        assert outcome == "allowed"

    async def test_chained_hash_lets_a_second_patch_skip_the_re_read(self, hass, lovelace_env):
        h, _l = lovelace_env
        await _seed(h)
        content, _o, _r = await _call(
            {"path": ["views", 0, "title"], "value": "One"}, _token(), h, _data()[0])
        chained = _json(content)["content_hash"]
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "badges", 0, "name"], "value": "Two",
             "expected_hash": chained},
            _token(), h, _data()[0])
        assert outcome == "allowed"

    async def test_auto_generated_dashboard_is_refused(self, hass, lovelace_env):
        """No stored config to patch; steer to set_dashboard_config instead."""
        h, _l = lovelace_env
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "value": "x"}, _token(), h, _data()[0])
        assert outcome == "invalid_request"

    async def test_wrong_shaped_op_falls_back_to_set(self, hass, lovelace_env):
        """str_arg degrades a wrong-shaped argument to absent, never to str(value)."""
        h, _l = lovelace_env
        await _seed(h)
        _c, outcome, _ = await _call(
            {"path": ["views", 0, "title"], "op": ["remove"], "value": "Kept"},
            _token(), h, _data()[0])
        assert outcome == "allowed"
        assert (await async_get_lovelace_config(h, None))["views"][0]["title"] == "Kept"
