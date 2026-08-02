"""Hold every shipped locale complete and consistent with the English catalog.

Phoenix is multilingual from here on, and the failure mode this guards is that
nothing else reports it. A key missing from a locale renders English in the
middle of a translated screen; a key whose {placeholder} set drifted is DELETED
by Home Assistant and falls back to English with only a log line. Both look like
"the translation is a bit patchy" rather than "someone forgot a step".

So adding an English string is not finished until every locale has it, or the
key is listed in the checker's KEEP_ENGLISH manifest with a reason. This test is
what makes that mechanical.

The checks themselves live in scripts/i18n_check_locale.py so the same logic
serves the command line and the suite.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _checker():
    """Load the script by path; scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "i18n_check_locale", REPO / "scripts" / "i18n_check_locale.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _locales() -> list[str]:
    """Every shipped locale, from the checker so there is ONE definition.

    The catalog is split across two directories (i18n_check_locale.SECTION_DIRS),
    so a glob written out here would be a second, drifting answer to "which
    locales ship".
    """
    return _checker().languages()


@pytest.mark.parametrize("language", _locales())
def test_locale_is_complete_and_consistent(language: str, capsys) -> None:
    """Run the full locale check; any problem fails with the checker's own report."""
    exit_code = _checker().check(language)
    if exit_code != 0:
        pytest.fail(f"{language}.json:\n{capsys.readouterr().out}")


def test_at_least_one_locale_is_shipped() -> None:
    """Guards the parametrize above from passing vacuously if the glob breaks."""
    assert _locales(), "no locale files found; did the translations directory move?"


# The four permission buttons are labelled with English letters (N R W D) that
# are never translated, because they are compact and the column is narrow. In
# English the tooltip happens to explain the letter for free, since it starts
# with the very word the letter comes from ("Read-only", "Deny (overrides...)").
# A fully translated tooltip severs that: a reader sees "R" and 只读 with nothing
# connecting them. So a translated tooltip must LEAD with the English word.
MNEMONICS = {
    "panel.perms.selGrey": "None",
    "panel.perms.selYellow": "Read",
    "panel.perms.selGreen": "Write",
    "panel.perms.selRed": "Deny",
}


@pytest.mark.parametrize("language", _locales())
def test_permission_button_tooltips_keep_the_english_mnemonic(language: str) -> None:
    """Each N/R/W/D tooltip starts with the word its letter stands for.

    Deliberate asymmetry with English, which needs no prefix. A translator
    tidying this away would silently orphan the button letters, which is exactly
    the kind of intent that does not survive in a catalog without a test.
    """
    checker = _checker()
    catalog = checker.flatten(checker.load(language), "")
    missing = [
        f"{key} should start with {word!r}, got {catalog[key]!r}"
        for key, word in MNEMONICS.items()
        if key in catalog and not catalog[key].startswith(word)
    ]
    assert missing == []
    # Not vacuous: these keys must actually be in the locale.
    assert all(key in catalog for key in MNEMONICS), f"{language} is missing tooltip keys"
