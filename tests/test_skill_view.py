"""Tests for the unauthenticated skill route (skill_view.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.skill_view import PHOENIX_SKILL_MARKDOWN, PhoenixSkillView


def _make_view(*, kill_switch: bool = False, shutting_down: bool = False, has_data: bool = True) -> PhoenixSkillView:
    view = PhoenixSkillView()
    hass = MagicMock()
    if has_data:
        data = MagicMock()
        data.shutting_down = shutting_down
        settings = MagicMock()
        settings.kill_switch = kill_switch
        data.store.get_settings.return_value = settings
        hass.data = {DOMAIN: data}
    else:
        hass.data = {}
    view.hass = hass
    return view


@pytest.mark.asyncio
async def test_serves_markdown_when_active():
    resp = await _make_view().get(MagicMock())
    assert resp.status == 200
    assert resp.content_type == "text/markdown"
    assert resp.text == PHOENIX_SKILL_MARKDOWN


@pytest.mark.asyncio
async def test_503_when_kill_switch_on():
    # Runtime-enabled kill switch: the route is already registered (HA cannot
    # unregister it), so it must refuse service rather than keep serving.
    resp = await _make_view(kill_switch=True).get(MagicMock())
    assert resp.status == 503


@pytest.mark.asyncio
async def test_503_when_shutting_down():
    resp = await _make_view(shutting_down=True).get(MagicMock())
    assert resp.status == 503


@pytest.mark.asyncio
async def test_503_when_data_missing():
    resp = await _make_view(has_data=False).get(MagicMock())
    assert resp.status == 503


def test_skill_directs_card_discovery_before_authoring():
    """Pin the wording, not just the tool name.

    Wording here is load-bearing and has measurably changed behaviour before: an
    alternative reads as permission and an early return reads as success. Three
    things must survive a reword. The instruction has to be to call the tool
    BEFORE building, or a model discovers cards only after it has already picked
    one. It has to say an uninstalled type is not refused at write time, since a
    model reasonably assumes a bad type would be rejected like any other bad
    argument and so treats guessing as safe. And an un-harvested catalog has to
    read as UNKNOWN, because "no custom cards" makes a model avoid every custom
    card on the instance with no way to find out otherwise.
    """
    assert "Call `list_dashboard_cards` before building a card" in PHOENIX_SKILL_MARKDOWN
    assert "renders as a broken card" in PHOENIX_SKILL_MARKDOWN
    assert "UNKNOWN, not absent" in PHOENIX_SKILL_MARKDOWN
    # The choice-or-offer instruction is the user's stated workflow; keep both halves.
    assert "pick the best-suited card yourself" in PHOENIX_SKILL_MARKDOWN
    assert "with a recommendation" in PHOENIX_SKILL_MARKDOWN
    # The old blanket discouragement must not come back: it steered models away
    # from custom cards entirely, which is the opposite of the intent now that
    # they can check.
    assert "Do not assume custom cards" not in PHOENIX_SKILL_MARKDOWN


def test_skill_includes_domain_authoring_recipes():
    # The modular domain-authoring recipes (v2.1) are the skill's authoring value;
    # guard their headers against accidental removal.
    for header in (
        "### Automations",
        "### Scripts and scenes",
        "### Dashboards and cards",
        "### Conditional and visibility",
        "### Climate",
    ):
        assert header in PHOENIX_SKILL_MARKDOWN
