"""Tests for the card-level dashboard tools (add/edit/delete_dashboard_card).

These edit ONE card of a storage-mode dashboard without resending the whole
layout, which blows weaker providers' output-token limits. Confirm-gated on
cap_lovelace_write like set_dashboard_config, validated pre-gate against the
current layout, re-validated plus CAS-checked in the executor, and versioned
with full before/after configs so the Changes tab and async_restore_version
work unchanged.
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

from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.tools.lovelace import (
    _build_diff_dashboard_card,
    _execute_dashboard_card,
)
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import async_get_lovelace_config, async_save_lovelace_config


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


CARD_A = {"type": "markdown", "content": "a"}
CARD_B = {"type": "button", "entity": "light.kitchen"}
NEW_CARD = {"type": "history-graph", "entities": ["sensor.solar"]}


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


async def _seed(hass, config=None):
    await async_save_lovelace_config(hass, None, config or {"views": [{"title": "V", "cards": [dict(CARD_A), dict(CARD_B)]}]})


class TestAddDashboardCard:
    async def test_deny_without_cap(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD},
            _token(cap_lovelace_write="deny"), h)
        assert outcome == "denied"

    async def test_appends_and_records_full_version(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        token = _token()
        data, versions = _data()
        content, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, token, h, data)
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["views"][0]["cards"] == [CARD_A, CARD_B, NEW_CARD]
        body = _json(content)
        assert body["saved"] is True and body["op"] == "add"
        # The result hash matches the new layout, so card ops can be chained
        # without re-reading.
        assert body["content_hash"] == content_hash(stored)
        hist = versions.list_for("dashboard", "lovelace")
        assert len(hist) == 1 and hist[0].action == "edit"
        assert hist[0].before["views"][0]["cards"] == [CARD_A, CARD_B]  # full configs
        assert hist[0].after == stored
        # The full-layout snapshot cannot say which card moved, so the record's
        # summary carries the op context for the Changes list.
        assert hist[0].summary == f"added {NEW_CARD['type']} (view 0)"

    async def test_position_inserts(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "position": 1, "card": NEW_CARD},
            _token(), h, _data()[0])
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["views"][0]["cards"] == [CARD_A, NEW_CARD, CARD_B]

    async def test_add_to_view_with_no_cards_yet(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h, {"views": [{"title": "Empty"}]})
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, _token(), h, _data()[0])
        assert outcome == "allowed"
        assert (await async_get_lovelace_config(h, None))["views"][0]["cards"] == [NEW_CARD]

    async def test_sections_view_requires_section_index(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h, {"views": [{"type": "sections", "sections": [{"title": "S", "cards": [dict(CARD_A)]}]}]})
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, _token(), h, _data()[0])
        assert outcome == "invalid_request"
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "section_index": 0, "card": NEW_CARD},
            _token(), h, _data()[0])
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["views"][0]["sections"][0]["cards"] == [CARD_A, NEW_CARD]

    async def test_section_index_on_plain_view_rejected(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "section_index": 0, "card": NEW_CARD},
            _token(), h, _data()[0])
        assert outcome == "invalid_request"


class TestCardOpValidationBeforeGate:
    """A structurally doomed op must fail pre-gate even under confirm, never
    becoming a pending approval that dies once an admin approves it."""

    @pytest.mark.parametrize("args", [
        {"view_index": 5, "card": NEW_CARD},                       # view out of range
        {"view_index": 0, "position": 9, "card": NEW_CARD},        # position out of range
        {"view_index": 0, "card": "not-a-dict"},                   # card not an object
        {"view_index": 0, "card": {"content": "no type"}},         # card without type
        {"view_index": True, "card": NEW_CARD},                    # bool is not an index
    ])
    async def test_add_invalid_under_confirm_is_invalid_request(self, hass, lovelace_env, args):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "add_dashboard_card", args, _token(cap_lovelace_write="confirm"), h)
        assert outcome == "invalid_request"

    async def test_edit_and_delete_index_out_of_range(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        for name, args in (
            ("edit_dashboard_card", {"view_index": 0, "card_index": 7, "card": NEW_CARD}),
            ("delete_dashboard_card", {"view_index": 0, "card_index": 7}),
        ):
            _, outcome, _ = await _call(name, args, _token(cap_lovelace_write="confirm"), h)
            assert outcome == "invalid_request"

    async def test_strategy_view_and_dashboard_rejected(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h, {"views": [{"strategy": {"type": "area"}}]})
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, _token(), h, _data()[0])
        assert outcome == "invalid_request"
        await _seed(h, {"strategy": {"type": "original-states"}})
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, _token(), h, _data()[0])
        assert outcome == "invalid_request"

    async def test_autogen_dashboard_steers_to_set_dashboard_config(self, hass, lovelace_env):
        # Card ops need a stored base layout; a fresh default dashboard has none.
        # The message must NAME the addressed dashboard: live-found, an agent
        # that dropped url_path was told "this dashboard has no stored config"
        # about the default dashboard, read it as contradicting the named
        # dashboard it had just successfully read, and spiraled.
        h, _light = lovelace_env
        content, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD}, _token(), h, _data()[0])
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "set_dashboard_config" in text
        assert "DEFAULT dashboard" in text and "url_path" in text and "list_dashboards" in text

    def test_no_stored_config_message_names_a_specific_dashboard(self):
        from custom_components.phoenix_mcp.tools.lovelace import _no_stored_config_message
        named = _no_stored_config_message("dashboard-steve")
        assert "'dashboard-steve'" in named and "set_dashboard_config" in named
        assert "DEFAULT" not in named

    async def test_valid_op_under_confirm_is_pending(self, hass, lovelace_env):
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        class _ApprStore:
            def __init__(self) -> None:
                self._p: list = []
                self.async_lock = _asyncio.Lock()
                self.async_save = AsyncMock()

            def get_pending_approvals(self) -> list:
                return self._p

            def set_pending_approvals(self, v: list) -> None:
                self._p = v

        h, _light = lovelace_env
        await _seed(h)
        data, _v = _data()
        data.store = _ApprStore()
        _content, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 0, "card": NEW_CARD},
            _token(cap_lovelace_write="confirm"), h, data)
        assert outcome == "pending_approval"
        assert len(data.store._p) == 1
        assert data.store._p[0]["tool_name"] == "add_dashboard_card"
        assert data.store._p[0]["cap_name"] == "cap_lovelace_write"
        # Nothing written until an admin approves.
        assert (await async_get_lovelace_config(h, None))["views"][0]["cards"] == [CARD_A, CARD_B]


class TestEditDeleteDashboardCard:
    async def test_edit_replaces_card(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        data, versions = _data()
        _, outcome, _ = await _call(
            "edit_dashboard_card", {"view_index": 0, "card_index": 1, "card": NEW_CARD},
            _token(), h, data)
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["views"][0]["cards"] == [CARD_A, NEW_CARD]
        hist = versions.list_for("dashboard", "lovelace")
        assert hist[0].before["views"][0]["cards"][1] == CARD_B

    async def test_delete_removes_card(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "delete_dashboard_card", {"view_index": 0, "card_index": 0}, _token(), h, _data()[0])
        assert outcome == "allowed"
        assert (await async_get_lovelace_config(h, None))["views"][0]["cards"] == [CARD_B]

    async def test_untouched_layout_bytes_preserved(self, hass, lovelace_env):
        # A card op must never disturb the rest of the layout (other views,
        # view options, section titles).
        h, _light = lovelace_env
        cfg = {
            "title": "Home",
            "views": [
                {"title": "V0", "icon": "mdi:home", "cards": [dict(CARD_A)]},
                {"type": "sections", "title": "V1",
                 "sections": [{"title": "S0", "cards": [dict(CARD_B)]}, {"title": "S1", "cards": []}]},
            ],
        }
        await _seed(h, cfg)
        _, outcome, _ = await _call(
            "add_dashboard_card", {"view_index": 1, "section_index": 1, "card": NEW_CARD},
            _token(), h, _data()[0])
        assert outcome == "allowed"
        stored = await async_get_lovelace_config(h, None)
        assert stored["title"] == "Home"
        assert stored["views"][0] == {"title": "V0", "icon": "mdi:home", "cards": [CARD_A]}
        assert stored["views"][1]["sections"][0] == {"title": "S0", "cards": [CARD_B]}
        assert stored["views"][1]["sections"][1]["cards"] == [NEW_CARD]


class TestCardOpCas:
    async def test_matching_hash_writes_and_chains(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        token = _token()
        data, _v = _data()
        current = await async_get_lovelace_config(h, None)
        content, outcome, _ = await _call(
            "add_dashboard_card",
            {"view_index": 0, "card": NEW_CARD, "expected_hash": content_hash(current)},
            token, h, data)
        assert outcome == "allowed"
        # Chain the returned hash into a second op without re-reading.
        _, outcome, _ = await _call(
            "delete_dashboard_card",
            {"view_index": 0, "card_index": 0, "expected_hash": _json(content)["content_hash"]},
            token, h, data)
        assert outcome == "allowed"

    async def test_stale_hash_refused_pre_gate(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        _, outcome, _ = await _call(
            "add_dashboard_card",
            {"view_index": 0, "card": NEW_CARD, "expected_hash": content_hash({"views": ["stale"]})},
            _token(cap_lovelace_write="confirm"), h)
        assert outcome == "invalid_request"
        assert (await async_get_lovelace_config(h, None))["views"][0]["cards"] == [CARD_A, CARD_B]

    async def test_executor_catches_approval_window_drift(self, hass, lovelace_env):
        # The layout changes between gate time and admin approval: the executor's
        # own CAS check (and bounds re-validation) must refuse at apply time.
        h, _light = lovelace_env
        await _seed(h)
        token = _token()
        data, _v = _data()
        stale_hash = content_hash(await async_get_lovelace_config(h, None))
        await async_save_lovelace_config(h, None, {"views": [{"cards": []}]})  # drift
        _, outcome, _ = await _execute_dashboard_card(
            {"view_index": 0, "card": NEW_CARD, "expected_hash": stale_hash},
            "add", "add_dashboard_card", token, h, data)
        assert outcome == "invalid_request"

    async def test_executor_revalidates_bounds_without_hash(self, hass, lovelace_env):
        # No expected_hash: the executor still re-resolves the target, so an
        # index that no longer exists after drift fails cleanly.
        h, _light = lovelace_env
        await _seed(h)
        token = _token()
        data, _v = _data()
        await async_save_lovelace_config(h, None, {"views": [{"cards": []}]})  # drift: cards gone
        _, outcome, _ = await _execute_dashboard_card(
            {"view_index": 0, "card_index": 1}, "delete", "delete_dashboard_card", token, h, data)
        assert outcome == "invalid_request"


class TestCardOpDiff:
    async def test_add_diff_shows_card_and_location(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        diff = await _build_diff_dashboard_card(
            {"view_index": 0, "card": NEW_CARD}, "add", _token(), h)
        assert diff["kind"] == "yaml_diff"
        assert "history-graph" in diff["summary"]
        assert diff["before"] is None and "history-graph" in diff["after"]
        assert diff["preview"]["view_index"] == 0 and diff["preview"]["position"] == "append"

    async def test_delete_diff_shows_prior_card_redacted(self, hass, lovelace_env):
        h, light = lovelace_env
        token = _token(tree=PermissionTree(domains={"light": PermissionNode(state="GREEN")}))
        await _seed(h, {"views": [{"cards": [
            {"type": "entities", "entities": [light, "sensor.secret"]}]}]})
        diff = await _build_diff_dashboard_card(
            {"view_index": 0, "card_index": 0}, "delete", token, h)
        assert diff["after"] is None
        assert diff["before"] is not None
        assert light in diff["before"]
        assert "sensor.secret" not in diff["before"] and "<redacted>" in diff["before"]

    async def test_edit_diff_shows_both_sides(self, hass, lovelace_env):
        h, _light = lovelace_env
        await _seed(h)
        diff = await _build_diff_dashboard_card(
            {"view_index": 0, "card_index": 1, "card": NEW_CARD}, "edit", _token(), h)
        assert "button" in diff["before"]  # the replaced card
        assert "history-graph" in diff["after"]

    async def test_big_card_diff_is_not_display_truncated(self, hass, lovelace_env):
        # A real single card (a multi-series chart) can exceed the 4000-char
        # display default; the panel PARSES diff.before/diff.after to render
        # the live preview, so a card diff must stay whole JSON up to the
        # version-snapshot bound (live-found: a truncated ~5.7KB apexcharts
        # card made the preview toggle vanish).
        big_card = {"type": "custom:apexcharts-card", "series": [
            {"entity": f"sensor.temperature_{i}", "name": f"Room {i}", "color": "#ff6b6b",
             "stroke_width": 1, "curve": "smooth", "group_by": {"func": "avg", "duration": "30m"},
             "show": {"legend_value": False}} for i in range(30)
        ]}
        assert len(json.dumps(big_card, indent=2)) > 4000
        h, _light = lovelace_env
        await _seed(h, {"views": [{"cards": [dict(big_card)]}]})
        diff = await _build_diff_dashboard_card(
            {"view_index": 0, "card_index": 0, "card": big_card}, "edit", _token(), h)
        assert "more characters)" not in diff["after"]
        assert "more characters)" not in diff["before"]
        # Whole JSON on both sides: parses, all 30 series intact. (The before
        # side's entity ids are redacted to scope as usual - these are ghosts -
        # so assert on non-entity fields.)
        assert json.loads(diff["after"])["series"][29]["name"] == "Room 29"
        before = json.loads(diff["before"])
        assert len(before["series"]) == 30 and before["series"][29]["name"] == "Room 29"
