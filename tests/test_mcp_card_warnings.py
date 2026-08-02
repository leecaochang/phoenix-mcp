"""Tests for the uninstalled-custom-card advisory on dashboard writes.

Advisory, never a refusal. The catalog is a cache of what one browser could see,
so failing a write closed would block a legitimate card whenever the cache is
cold, one plugin install stale, or harvested from a page that saw a partial
registry. A warning next to a card that does render is a far cheaper mistake
than refusing a write the operator wanted with no way to override.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.phoenix_mcp.card_catalog import CardCatalogStore
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.tools.lovelace import _card_warnings, _collect_card_types


def _data(catalog: CardCatalogStore | None = None) -> PhoenixData:
    return PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(),
        card_catalog=catalog or CardCatalogStore(),
    )


async def _catalog() -> CardCatalogStore:
    store = CardCatalogStore()
    await store.async_replace(
        entries=[
            {"type": "mushroom-light-card", "name": "Mushroom Light"},
            {"type": "tiktoktts-card", "name": "TikTok TTS", "available": False},
        ],
        resource_count=31,
        failed_imports=[],
    )
    return store


def test_collects_nested_custom_types() -> None:
    """A stack card hides its children's types several levels down."""
    types: set[str] = set()
    _collect_card_types(
        {
            "type": "custom:vertical-stack-in-card",
            "cards": [
                {"type": "tile", "entity": "light.a"},
                {"type": "custom:mushroom-light-card", "entity": "light.b"},
                {"type": "custom:stack-in-card", "cards": [{"type": "custom:mini-graph-card"}]},
            ],
        },
        types,
    )

    assert types == {
        "vertical-stack-in-card", "mushroom-light-card", "stack-in-card", "mini-graph-card",
    }
    # A built-in type is not a custom card and must not be collected.
    assert "tile" not in types


async def test_installed_card_warns_nothing() -> None:
    data = _data(await _catalog())
    assert _card_warnings({"type": "custom:mushroom-light-card"}, data) == []


async def test_uninstalled_card_warns() -> None:
    data = _data(await _catalog())
    (warning,) = _card_warnings({"type": "custom:not-installed-card"}, data)

    assert "not installed" in warning
    assert "list_dashboard_cards" in warning


async def test_registered_but_unavailable_card_warns() -> None:
    data = _data(await _catalog())
    (warning,) = _card_warnings({"type": "custom:tiktoktts-card"}, data)

    assert "did not load" in warning


async def test_builtin_types_never_warn() -> None:
    data = _data(await _catalog())
    assert _card_warnings({"type": "tile", "entity": "light.a"}, data) == []
    assert _card_warnings({"type": "entities", "entities": ["light.a"]}, data) == []


async def test_unharvested_catalog_stays_silent() -> None:
    """With nothing to compare against, every custom card would look unknown.

    Warning on all of them would train the reader to ignore the field exactly
    when it starts being accurate.
    """
    data = _data()  # never harvested
    assert _card_warnings({"type": "custom:anything-at-all"}, data) == []


async def test_warns_once_per_type_sorted() -> None:
    data = _data(await _catalog())
    config = {
        "views": [{"cards": [
            {"type": "custom:zzz-card"},
            {"type": "custom:aaa-card"},
            {"type": "custom:zzz-card"},
        ]}]
    }

    warnings = _card_warnings(config, data)

    assert len(warnings) == 2
    assert "aaa-card" in warnings[0]
    assert "zzz-card" in warnings[1]


async def test_malformed_payload_does_not_raise() -> None:
    data = _data(await _catalog())
    for payload in (None, "string", 42, [], {"type": 7}, {"cards": None}):
        assert _card_warnings(payload, data) == []
