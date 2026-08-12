"""The panel's string catalog, and the endpoint that serves it.

Phoenix serves these strings itself. They used to arrive over HA's own
frontend/get_translations websocket command, which reads translations/ and
nothing else, and they had to leave that directory: hassfest validates
translations/en.json against a CLOSED set of Home Assistant categories and
errors on any other top-level key, which fails the HACS submission. So
catalogs/ holds Phoenix's own sections and translations/ holds only HA's.

That move cost two behaviours HA was providing for free, and helpers.panel_catalog
reimplements both. They are what this file pins, because both fail SILENTLY:

  - English backing. A key a translator has not reached must resolve to the
    English string, not to a raw dotted key rendered in the panel.
  - Placeholder mismatch drops the translation. The panel interpolates by name,
    so a translation that renamed a placeholder renders a broken sentence and one
    that invented a placeholder prints a token no caller passes. HA compared the
    sets and dropped the translated key; so does this.

scripts/i18n_check_locale.py refuses to let a mismatched locale ship in the first
place, so the drop rule is the runtime backstop for a hand-edited install rather
than the primary guard. It is tested here anyway: a backstop that never runs in a
test is a backstop nobody knows is broken.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.phoenix_mcp import helpers
from custom_components.phoenix_mcp.admin_view import PhoenixAdminCatalogView
from custom_components.phoenix_mcp.const import CATALOGS_DIR


@pytest.fixture(autouse=True)
def _clear_catalog_caches():
    """Both readers are lru_cached, so a test that swaps the directory must reset."""
    helpers._catalog_section.cache_clear()
    helpers.panel_catalog.cache_clear()
    yield
    helpers._catalog_section.cache_clear()
    helpers.panel_catalog.cache_clear()


def _fake_catalogs(tmp_path, monkeypatch, **languages: dict) -> None:
    for language, doc in languages.items():
        (tmp_path / f"{language}.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(helpers, "CATALOGS_DIR", tmp_path)


def test_english_is_flattened_to_dotted_keys() -> None:
    catalog = helpers.panel_catalog("en")
    assert catalog, "the shipped English catalog is empty"
    assert catalog["common.loading"]
    assert all("." in key for key in catalog), "keys must be dotted, not nested"


def test_shipped_locale_is_backed_by_english() -> None:
    """Every English key resolves in a translated locale, translated or not."""
    english = helpers.panel_catalog("en")
    chinese = helpers.panel_catalog("zh-Hans")
    assert set(english) <= set(chinese)
    translated = [k for k in english if chinese[k] != english[k]]
    # Not vacuous: zh-Hans is a real translation, so most keys must differ.
    assert len(translated) > len(english) // 2


def test_missing_key_falls_back_to_english(tmp_path, monkeypatch) -> None:
    _fake_catalogs(
        tmp_path,
        monkeypatch,
        en={"panel": {"a": "Alpha", "b": "Beta"}},
        xx={"panel": {"a": "translated"}},
    )
    assert helpers.panel_catalog("xx") == {"a": "translated", "b": "Beta"}


@pytest.mark.parametrize(
    "translated",
    [
        "缺少占位符",              # dropped the placeholder
        "重命名了 {other}",        # renamed it
        "多了一个 {name} {extra}",  # invented a second one
    ],
)
def test_placeholder_mismatch_keeps_the_english(tmp_path, monkeypatch, translated) -> None:
    _fake_catalogs(
        tmp_path,
        monkeypatch,
        en={"panel": {"greet": "Hello {name}"}},
        xx={"panel": {"greet": translated}},
    )
    assert helpers.panel_catalog("xx")["greet"] == "Hello {name}"


def test_matching_placeholders_are_kept(tmp_path, monkeypatch) -> None:
    """The guard above must not be refusing everything."""
    _fake_catalogs(
        tmp_path,
        monkeypatch,
        en={"panel": {"greet": "Hello {name}"}},
        xx={"panel": {"greet": "你好 {name}"}},
    )
    assert helpers.panel_catalog("xx")["greet"] == "你好 {name}"


def test_unknown_language_is_english(tmp_path, monkeypatch) -> None:
    """A language with no file at all is not an error; it is English."""
    _fake_catalogs(tmp_path, monkeypatch, en={"panel": {"a": "Alpha"}})
    assert helpers.panel_catalog("kl") == {"a": "Alpha"}


def test_only_the_panel_section_is_served() -> None:
    """notification and voice are backend-rendered and never reach the browser."""
    catalog = helpers.panel_catalog("en")
    assert not any(key.startswith(("notification.", "voice.")) for key in catalog)


def test_catalogs_dir_is_not_the_translations_dir() -> None:
    """The whole point of the split: hassfest must never see these sections.

    A future edit that pointed CATALOGS_DIR back at translations/ would pass
    every other test in this file and fail the HACS submission instead.
    """
    assert CATALOGS_DIR.name == "catalogs"
    translations = CATALOGS_DIR.parent / "translations"
    shipped = json.loads((translations / "en.json").read_text(encoding="utf-8"))
    allowed = {"config", "entity", "issues"}
    assert set(shipped) <= allowed, (
        f"translations/en.json carries {sorted(set(shipped) - allowed)},"
        " which hassfest rejects as an unknown category"
    )


def _admin_request(is_admin: bool = True) -> MagicMock:
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    user = MagicMock()
    user.is_admin = is_admin
    request = MagicMock()
    request.get = MagicMock(
        side_effect=lambda k, default=None: {
            KEY_HASS_USER: user,
            KEY_AUTHENTICATED: True,
        }.get(k, default)
    )
    request.__getitem__ = MagicMock(side_effect=lambda k: "rid-1")
    return request


async def _get(language: str):
    view = PhoenixAdminCatalogView()
    view.hass = MagicMock()
    view.hass.async_add_executor_job = AsyncMock(
        side_effect=lambda fn, *args: fn(*args)
    )
    # Keyword, not positional: require_admin forwards **kwargs only, which is
    # how aiohttp hands a path parameter to the handler.
    return await view.get(_admin_request(), language=language)


async def test_endpoint_serves_the_catalog() -> None:
    resp = await _get("en")
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["language"] == "en"
    assert body["resources"]["common.loading"]


async def test_endpoint_is_admin_only() -> None:
    view = PhoenixAdminCatalogView()
    view.hass = MagicMock()
    resp = await view.get(_admin_request(is_admin=False), language="en")
    assert resp.status == 403


async def test_endpoint_404s_when_the_catalog_cannot_be_read(tmp_path, monkeypatch) -> None:
    """An empty catalog is a broken install, not a language the panel should render."""
    monkeypatch.setattr(helpers, "CATALOGS_DIR", tmp_path)
    resp = await _get("en")
    assert resp.status == 404
