"""Keep shipped locales and every public language list in lockstep."""

from __future__ import annotations

import importlib.util
import pathlib
import re

from custom_components.phoenix_mcp.locales import SHIPPED_LOCALES


REPO = pathlib.Path(__file__).resolve().parent.parent

# These are the English names that operators see in the public documentation.
# The locale codes are deliberately the keys so the test can require this list
# to be updated whenever the shipped catalog set changes.
PUBLIC_LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
}


def _module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shipped_codes() -> set[str]:
    checker = _module("i18n_check_locale", REPO / "scripts" / "i18n_check_locale.py")
    return {"en", *checker.languages()}


def _documented_names() -> str:
    names = [PUBLIC_LANGUAGE_NAMES[code] for code in (
        "en", "es", "fr", "de", "nl", "pl", "ru", "zh-Hans", "zh-Hant", "ko", "ja"
    )]
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def test_public_language_name_map_matches_shipped_catalogs() -> None:
    assert set(PUBLIC_LANGUAGE_NAMES) == _shipped_codes()


def test_backend_locale_map_matches_shipped_catalogs() -> None:
    assert set(SHIPPED_LOCALES) == _shipped_codes()


def test_language_picker_matches_shipped_catalogs() -> None:
    source = (REPO / "frontend_src" / "i18n" / "index.ts").read_text(encoding="utf-8")
    picker_codes = set(re.findall(r'\{ code: "([^"]+)", endonym:', source))
    assert picker_codes == _shipped_codes()


def test_readme_editions_and_switchers_match_shipped_catalogs() -> None:
    stamper = _module("readme_i18n_stamp", REPO / "scripts" / "readme_i18n_stamp.py")
    readme_codes = {"zh-CN" if code == "zh-Hans" else code for code in _shipped_codes() - {"en"}}
    assert set(stamper.TRANSLATIONS) == readme_codes
    for path in [stamper.SOURCE, *stamper.TRANSLATIONS.values()]:
        text = path.read_text(encoding="utf-8")
        assert stamper.language_link_problems(path, text) == []


def test_english_public_docs_list_every_shipped_language() -> None:
    names = _documented_names()
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    install = (REPO / "docs" / "install.html").read_text(encoding="utf-8")
    panel = (REPO / "docs" / "panel.html").read_text(encoding="utf-8")

    assert f"The panel currently supports {names}." in readme
    assert f"The panel currently supports {names}." in install
    assert (
        "Phoenix MCP ships English, Spanish, French, German, Dutch, Polish, Russian, "
        "Simplified Chinese (<code>zh-Hans</code>), Traditional Chinese "
        "(<code>zh-Hant</code>), Korean, and Japanese."
    ) in panel
