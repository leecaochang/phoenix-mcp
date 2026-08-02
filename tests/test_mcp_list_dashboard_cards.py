"""Tests for list_dashboard_cards, the installed-custom-card discovery tool.

The invariant that matters most here is the cold-start one: an un-harvested
catalog must never read as "this instance has no custom cards". An agent told
that avoids every custom card on the system, which is both wrong and something
it has no way to recover from, whereas "unknown, prefer built-ins" degrades to
exactly the behaviour that existed before the tool.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.card_catalog import CardCatalogStore
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord


def _token(**caps) -> TokenRecord:
    base = {"cap_lovelace_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(domains={}), **base,
    )


def _data(catalog: CardCatalogStore | None = None) -> PhoenixData:
    return PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
        card_catalog=catalog or CardCatalogStore(),
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _harvested(**over) -> CardCatalogStore:
    store = CardCatalogStore()
    entries = over.pop("entries", [
        {
            "type": "mushroom-light-card",
            "name": "Mushroom Light Card",
            "description": "A light card",
            "documentation_url": "https://example.invalid/light",
            "stub_config": {"type": "custom:mushroom-light-card", "entity": "light.kitchen"},
            "has_visual_editor": True,
        },
        {"type": "broken-card", "name": "Broken", "available": False},
    ])
    await store.async_replace(entries=entries, resource_count=31, failed_imports=[])
    return store


async def _call(args, token, hass, data):
    return await _call_tool("list_dashboard_cards", args, token, hass, data=data)


async def test_unharvested_is_reported_as_unknown_not_empty(hass: HomeAssistant) -> None:
    """The whole point: never let a cold catalog read as 'no custom cards'."""
    content, outcome, _ = await _call({}, _token(), hass, _data())
    body = _json(content)

    assert outcome == "allowed"
    assert body["harvested"] is False
    assert body["cards"] == []
    # The notice must say UNKNOWN, and must not be phrased as an absence.
    notice = body["notice"].lower()
    assert "unknown" in notice
    assert "not the same as none" in notice
    assert "built-in" in notice


async def test_denied_cap_returns_uniform_forbidden(hass: HomeAssistant) -> None:
    content, outcome, resource = await _call({}, _token(cap_lovelace_write="deny"), hass, _data())

    assert outcome == "denied"
    assert resource == "list_dashboard_cards"
    assert content["content"][0]["text"] == "Forbidden."


async def test_lean_listing_omits_examples(hass: HomeAssistant) -> None:
    """A discovery call must not spend its budget on examples for unused cards."""
    content, outcome, _ = await _call({}, _token(), hass, _data(await _harvested()))
    body = _json(content)

    assert outcome == "allowed"
    assert body["harvested"] is True
    assert body["count"] == 2
    card = next(c for c in body["cards"] if c["type"] == "custom:mushroom-light-card")
    assert card["name"] == "Mushroom Light Card"
    assert card["description"] == "A light card"
    assert card["documentation_url"] == "https://example.invalid/light"
    assert "example_config" not in card


async def test_types_carry_the_custom_prefix(hass: HomeAssistant) -> None:
    """The catalog stores bare types; a card config needs the prefixed form."""
    content, _, _ = await _call({}, _token(), hass, _data(await _harvested()))
    for card in _json(content)["cards"]:
        assert card["type"].startswith("custom:")


async def test_unavailable_card_is_flagged_and_available_ones_are_not(hass: HomeAssistant) -> None:
    """A picker entry whose element never loaded must not look usable."""
    content, _, _ = await _call({}, _token(), hass, _data(await _harvested()))
    cards = {c["type"]: c for c in _json(content)["cards"]}

    broken = cards["custom:broken-card"]
    assert broken["available"] is False
    assert "do not use" in broken["note"].lower()

    # The common case stays quiet rather than repeating "available: true" per row.
    assert "available" not in cards["custom:mushroom-light-card"]


async def test_single_type_lookup_returns_the_example(hass: HomeAssistant) -> None:
    content, outcome, _ = await _call(
        {"type": "mushroom-light-card"}, _token(), hass, _data(await _harvested())
    )
    body = _json(content)

    assert outcome == "allowed"
    assert body["found"] is True
    assert body["card"]["example_config"]["entity"] == "light.kitchen"
    assert body["card"]["has_visual_editor"] is True


async def test_single_type_lookup_accepts_custom_prefix(hass: HomeAssistant) -> None:
    content, _, _ = await _call(
        {"type": "custom:mushroom-light-card"}, _token(), hass, _data(await _harvested())
    )
    assert _json(content)["found"] is True


async def test_missing_type_reports_not_installed(hass: HomeAssistant) -> None:
    content, outcome, _ = await _call(
        {"type": "custom:nope-card"}, _token(), hass, _data(await _harvested())
    )
    body = _json(content)

    assert outcome == "allowed"
    assert body["found"] is False
    assert "not installed" in body["notice"].lower()


async def test_detailed_includes_every_example(hass: HomeAssistant) -> None:
    content, _, _ = await _call({"detailed": True}, _token(), hass, _data(await _harvested()))
    cards = {c["type"]: c for c in _json(content)["cards"]}

    assert cards["custom:mushroom-light-card"]["example_config"]["entity"] == "light.kitchen"
    # A card with no stub simply carries no example rather than a null.
    assert "example_config" not in cards["custom:broken-card"]


async def test_wrong_shaped_type_arg_degrades_to_a_listing(hass: HomeAssistant) -> None:
    """str_arg degrades a bad shape to absent instead of inventing an argument."""
    content, outcome, _ = await _call(
        {"type": ["mushroom-light-card"]}, _token(), hass, _data(await _harvested())
    )
    body = _json(content)

    assert outcome == "allowed"
    assert body["count"] == 2
    assert "found" not in body
