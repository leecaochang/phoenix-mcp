"""Hold every locale against the English it was actually translated from.

The completeness, placeholder and identical-to-English checks all judge a locale
against en.json as it is now, so a translation of a since-reworded English
string passes all of them and keeps stating the old behaviour. This is the guard
for that class; the stamps live in tests/contract/i18n_source_hashes.json and
scripts/i18n_source_stamp.py writes them.

The synthetic cases matter as much as the real one: an audit that silently
classified nothing would leave this file green forever.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _stamper():
    """Load the script by path; scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "i18n_source_stamp", REPO / "scripts" / "i18n_source_stamp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _locales() -> list[str]:
    return _stamper().locales()


@pytest.mark.parametrize("language", _locales())
def test_locale_matches_the_english_it_was_translated_from(language: str, capsys) -> None:
    """Any drift fails with the script's own report, naming the keys."""
    exit_code = _stamper().check(language)
    if exit_code != 0:
        pytest.fail(f"{language}.json:\n{capsys.readouterr().out}")


def test_every_shipped_locale_is_stamped() -> None:
    """Guards the parametrize above, and a baseline that lost a locale wholesale."""
    baseline = _stamper().load_baseline()
    assert _locales(), "no locale files found; did the translations directory move?"
    for language in _locales():
        assert baseline.get(language), f"{language} has no stamps at all"


def test_audit_reports_a_reworded_english_string() -> None:
    stamp = _stamper()
    problems = stamp.audit(
        english={"a": "Revoking a token also clears the Assist binding."},
        locale={"a": "撤销令牌还会清除 Assist 绑定。"},
        stamped={"a": stamp.digest("Revoking a token cancels its pending approvals.")},
    )
    assert problems["stale"] == ["a"]


def test_audit_is_quiet_when_the_english_is_unchanged() -> None:
    stamp = _stamper()
    problems = stamp.audit(
        english={"a": "Pending"},
        locale={"a": "待处理"},
        stamped={"a": stamp.digest("Pending")},
    )
    assert problems == {"stale": [], "unstamped": [], "orphaned": []}


def test_audit_reports_a_translation_with_no_stamp() -> None:
    stamp = _stamper()
    problems = stamp.audit(english={"a": "Pending"}, locale={"a": "待处理"}, stamped={})
    assert problems["unstamped"] == ["a"]


def test_audit_reports_a_stamp_whose_key_is_gone() -> None:
    stamp = _stamper()
    problems = stamp.audit(english={}, locale={}, stamped={"a": stamp.digest("Pending")})
    assert problems["orphaned"] == ["a"]


def test_keys_a_locale_deliberately_leaves_english_need_no_stamp() -> None:
    """The keep-English keys are absent from the locale, so they are not drift."""
    stamp = _stamper()
    problems = stamp.audit(english={"entity.sensor.status.name": "Status"}, locale={}, stamped={})
    assert problems == {"stale": [], "unstamped": [], "orphaned": []}
