"""Prove the locale checker REJECTS each defect it claims to catch.

tests/test_i18n_locales.py only shows that the checker approves the files that
happen to be in the repo right now. That passes just as happily if a check is
silently broken, which is the failure mode worth guarding: every problem this
tool exists for is invisible at runtime, so a guard that stopped working would
not be noticed by anything else.

Each test here builds a minimal two-file catalog in a tmp dir, points the
checker at it, and asserts on the specific problem reported.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location(
        "i18n_check_locale", REPO / "scripts" / "i18n_check_locale.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def catalogs(tmp_path, checker, monkeypatch):
    """Write an en.json / xx.json pair and aim the checker at them.

    BOTH directory constants are redirected. The real catalog is split across
    catalogs/ and translations/ and the checker reads both, so patching one
    would leave it merging these fixtures with the shipped strings from the
    other and judging a document no test ever wrote.
    """
    monkeypatch.setattr(checker, "CATALOGS", tmp_path)
    monkeypatch.setattr(checker, "TRANSLATIONS", tmp_path)
    monkeypatch.setattr(checker, "KEEP_ENGLISH", {})
    monkeypatch.setattr(checker, "KEEP_ENGLISH_KEYS", set())
    monkeypatch.setattr(checker, "LITERALS", {})
    monkeypatch.setattr(checker, "ALLOW_IDENTICAL", frozenset())
    monkeypatch.setattr(checker, "ALLOW_IDENTICAL_BY_LANGUAGE", {})
    monkeypatch.setattr(checker, "WHITESPACE_EXEMPT", frozenset())

    def write(english: dict, other: dict) -> None:
        (tmp_path / "en.json").write_text(
            json.dumps({"panel": english}), encoding="utf-8"
        )
        (tmp_path / "xx.json").write_text(
            json.dumps({"panel": other}), encoding="utf-8"
        )

    return write


def run(checker, capsys) -> tuple[int, str]:
    code = checker.check("xx")
    return code, capsys.readouterr().out


def test_clean_pair_passes(checker, catalogs, capsys):
    """The happy path, so every negative below is a real signal."""
    catalogs({"greet": "Hello {name}"}, {"greet": "你好 {name}"})
    code, out = run(checker, capsys)
    assert code == 0, out
    assert "no problems" in out


def test_missing_key_is_rejected(checker, catalogs, capsys):
    catalogs({"greet": "Hello", "bye": "Goodbye"}, {"greet": "你好"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "panel.bye: missing" in out


def test_untranslated_copy_is_rejected(checker, catalogs, capsys):
    catalogs({"greet": "Hello there"}, {"greet": "Hello there"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "identical to English" in out


def test_placeholder_drift_is_rejected(checker, catalogs, capsys):
    """HA deletes the key and falls back to English, logging nothing useful."""
    catalogs({"greet": "Hello {name}"}, {"greet": "你好"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "placeholders" in out


def test_unbalanced_brace_is_rejected(checker, catalogs, capsys):
    catalogs({"greet": "Hello {name}"}, {"greet": "你好 {name"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "unbalanced brace" in out


def test_inline_tag_drift_is_rejected(checker, catalogs, capsys):
    """tRich renders by tag, so a dropped tag changes the rendered structure."""
    catalogs({"note": "<strong>Careful</strong> now"}, {"note": "请注意"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "inline tags" in out


def test_code_payload_must_pass_through(checker, catalogs, capsys):
    catalogs(
        {"note": "Run <code>hass --script check</code> first"},
        {"note": "请先运行 <code>检查脚本</code>"},
    )
    code, out = run(checker, capsys)
    assert code == 1
    assert "must pass through verbatim" in out


def test_literal_guard_rejects_a_translated_token(checker, catalogs, capsys, monkeypatch):
    monkeypatch.setitem(checker.LITERALS, "panel.confirm", ("WIPE",))
    catalogs({"confirm": "Type WIPE to confirm"}, {"confirm": "输入“清除”以确认"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "must contain 'WIPE' verbatim" in out


@pytest.mark.parametrize(
    ("english", "translated", "message"),
    [
        ("Keep 4.5:1", "45:1 を維持", "numbers added"),
        ("Open configuration.yaml", "設定ファイルを開く", "technical content dropped"),
        ("Open the settings", "other.yaml を開く", "technical content invented"),
        ("Save 90%", "90 を保存", "percentage sign dropped"),
        ("Use 5K tokens", "5M トークンを使用", "magnitude changed"),
        ("Limit 256 MB", "上限 256 GB", "storage unit changed"),
        ("Require N > 0", "N < 0 が必要", "comparison changed"),
        ("Range 1-600", "範囲 600-1", "ordered pair reversed"),
    ],
)
def test_machine_parity_defects_are_rejected(
    checker, catalogs, capsys, english, translated, message
):
    catalogs({"instruction": english}, {"instruction": translated})
    code, out = run(checker, capsys)
    assert code == 1
    assert message in out


def test_locale_number_punctuation_and_localized_units_pass(checker, catalogs, capsys):
    catalogs(
        {"instruction": "Keep 4.5:1, 10,000 requests, and 256 MB"},
        {"instruction": "4,5:1、10 000 件のリクエスト、256 メガバイトを維持"},
    )
    code, out = run(checker, capsys)
    assert code == 0, out


def test_slash_unit_is_not_mistaken_for_a_path(checker, catalogs, capsys):
    catalogs({"rate": "50/min"}, {"rate": "毎分 50 回"})
    code, out = run(checker, capsys)
    assert code == 0, out


def test_forbidden_character_is_rejected_in_a_locale(checker, catalogs, capsys):
    catalogs({"greet": "Hello there"}, {"greet": "你好 — 朋友"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "forbidden dash" in out


def test_forbidden_character_is_rejected_in_english_too(checker, catalogs, capsys):
    """English is not grandfathered; the house style applies to every catalog."""
    catalogs({"greet": "Hello — there"}, {"greet": "你好，朋友"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "en.json panel.greet" in out


def test_keep_english_key_present_is_rejected(checker, catalogs, capsys, monkeypatch):
    monkeypatch.setattr(checker, "KEEP_ENGLISH_KEYS", {"panel.brand"})
    catalogs({"brand": "Phoenix MCP"}, {"brand": "凤凰"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "deliberately stays English" in out


def test_unknown_key_is_rejected(checker, catalogs, capsys):
    catalogs({"greet": "Hello"}, {"greet": "你好", "gret": "错字"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "not a key in en.json" in out


def test_load_bearing_whitespace_is_enforced(checker, catalogs, capsys):
    catalogs({"prefix": "Result: "}, {"prefix": "结果："})
    code, out = run(checker, capsys)
    assert code == 1
    assert "leading/trailing space" in out


def test_entity_section_must_not_be_translated(checker, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(checker, "TRANSLATIONS", tmp_path)
    (tmp_path / "en.json").write_text(
        json.dumps({"panel": {"a": "A thing"}, "entity": {"sensor": {"status": {"name": "Status"}}}}),
        encoding="utf-8",
    )
    (tmp_path / "xx.json").write_text(
        json.dumps({"panel": {"a": "一个东西"}, "entity": {"sensor": {"status": {"name": "状态"}}}}),
        encoding="utf-8",
    )
    code, out = run(checker, capsys)
    assert code == 1
    assert "must stay English" in out


def test_keep_english_manifest_rejects_a_missing_key(checker, catalogs, capsys, monkeypatch):
    monkeypatch.setattr(checker, "KEEP_ENGLISH_KEYS", {"panel.retired"})
    catalogs({"greet": "Hello"}, {"greet": "こんにちは"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "KEEP_ENGLISH references missing key panel.retired" in out


def test_literals_manifest_rejects_an_absent_source_literal(
    checker, catalogs, capsys, monkeypatch
):
    monkeypatch.setattr(checker, "LITERALS", {"panel.greet": ("WIPE",)})
    catalogs({"greet": "Hello"}, {"greet": "こんにちは"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "references absent English literal 'WIPE'" in out


def test_allow_identical_manifest_must_remain_used(
    checker, catalogs, capsys, monkeypatch
):
    monkeypatch.setattr(checker, "ALLOW_IDENTICAL", frozenset({"panel.greet"}))
    catalogs({"greet": "Hello"}, {"greet": "こんにちは"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "ALLOW_IDENTICAL panel.greet is no longer used" in out


def test_language_scoped_identical_allowlist_does_not_leak_to_other_locales(
    checker, catalogs, capsys, monkeypatch
):
    monkeypatch.setattr(
        checker,
        "ALLOW_IDENTICAL_BY_LANGUAGE",
        {"es": frozenset({"panel.greet"})},
    )
    catalogs({"greet": "Hello"}, {"greet": "Hello"})
    code, out = run(checker, capsys)
    assert code == 1
    assert "identical to English" in out


def test_whitespace_manifest_must_remain_used(
    checker, catalogs, capsys, monkeypatch
):
    monkeypatch.setattr(checker, "WHITESPACE_EXEMPT", frozenset({"panel.prefix"}))
    catalogs({"prefix": "Result: "}, {"prefix": "結果: "})
    code, out = run(checker, capsys)
    assert code == 1
    assert "WHITESPACE_EXEMPT panel.prefix is no longer used" in out
