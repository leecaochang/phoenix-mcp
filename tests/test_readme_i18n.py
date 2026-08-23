"""Localized READMEs must remain full translations of the English source."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _stamper():
    spec = importlib.util.spec_from_file_location(
        "readme_i18n_stamp", REPO / "scripts" / "readme_i18n_stamp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("language", sorted(_stamper().TRANSLATIONS))
def test_localized_readme_matches_its_english_source(language: str, capsys) -> None:
    exit_code = _stamper().check(language)
    if exit_code:
        pytest.fail(capsys.readouterr().out)


def test_every_localized_readme_has_a_nonempty_stamp_set() -> None:
    stamp = _stamper()
    baseline = stamp.load_baseline()
    assert set(baseline) == {path.name for path in stamp.TRANSLATIONS.values()}
    assert all(baseline.values())


def test_source_rewording_is_reported_even_when_translation_still_exists() -> None:
    stamp = _stamper()
    previous = {
        "intro.block.1": {
            "source": stamp.digest("Old English"),
            "translation": stamp.digest("翻訳"),
        }
    }
    current = {
        "intro.block.1": {
            "source": stamp.digest("New English"),
            "translation": stamp.digest("翻訳"),
        }
    }
    assert stamp.audit(current, previous)["source_stale"] == ["intro.block.1"]
    assert stamp.unchanged_translation_keys(current, previous) == ["intro.block.1"]


def test_translation_edit_requires_an_explicit_new_stamp() -> None:
    stamp = _stamper()
    previous = {
        "intro.block.1": {
            "source": stamp.digest("English"),
            "translation": stamp.digest("旧訳"),
        }
    }
    current = {
        "intro.block.1": {
            "source": stamp.digest("English"),
            "translation": stamp.digest("新訳"),
        }
    }
    assert stamp.audit(current, previous)["translation_changed"] == ["intro.block.1"]


def test_translated_headings_keep_stable_structural_ids() -> None:
    stamp = _stamper()
    english = stamp.parse_text("# Phoenix MCP\n\nIntro\n\n## Documentation\n\nRead this.")
    japanese = stamp.parse_text("# Phoenix MCP\n\n概要\n\n## ドキュメント\n\nこちらをご覧ください。")
    assert [(b["key"], b["kind"]) for b in english] == [
        (b["key"], b["kind"]) for b in japanese
    ]


@pytest.mark.parametrize(
    ("source", "translation", "needle"),
    [
        ("Use 148 tools.", "ツールを使用します。", "numbers differ"),
        ("See [docs](https://example.test).", "[文書](https://wrong.test)を参照。", "link targets differ"),
        ("Copy `config.json`.", "設定をコピーします。", "inline code differ"),
        ("Restart Home Assistant.", "システムを再起動します。", "protected names differ"),
    ],
)
def test_machine_readable_content_cannot_drift(
    source: str, translation: str, needle: str
) -> None:
    problems = _stamper().parity_problems(source, translation)
    assert any(needle in problem for problem in problems)


def test_list_structure_cannot_silently_lose_an_item() -> None:
    stamp = _stamper()
    source = stamp.parse_text("## Requirements\n\n- One\n- Two")
    translated = stamp.parse_text("## 要件\n\n- 一つ")
    assert stamp.structure_problems(source, translated)


def test_normal_restamp_refuses_unchanged_translation(
    tmp_path: pathlib.Path, monkeypatch, capsys
) -> None:
    stamp = _stamper()
    source = tmp_path / "README.md"
    translated = tmp_path / "README.ja.md"
    chinese = tmp_path / "README.zh-CN.md"
    baseline = tmp_path / "stamps.json"
    nav_en = "**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)"
    nav_ja = "[English](README.md) | [简体中文](README.zh-CN.md) | **日本語**"
    source.write_text(f"# Phoenix MCP\n\n{nav_en}\n\nOriginal wording.\n", encoding="utf-8")
    translated.write_text(f"# Phoenix MCP\n\n{nav_ja}\n\n翻訳です。\n", encoding="utf-8")
    chinese.write_text("", encoding="utf-8")
    monkeypatch.setattr(stamp, "SOURCE", source)
    monkeypatch.setattr(stamp, "TRANSLATIONS", {"zh-CN": chinese, "ja": translated})
    monkeypatch.setattr(stamp, "BASELINE", baseline)

    assert stamp.stamp("ja") == 0
    before = baseline.read_text(encoding="utf-8")
    source.write_text(f"# Phoenix MCP\n\n{nav_en}\n\nReworded text.\n", encoding="utf-8")
    assert stamp.stamp("ja") == 1
    assert "English changed but these translations did not" in capsys.readouterr().out
    assert baseline.read_text(encoding="utf-8") == before
    assert stamp.stamp("ja", allow_unchanged=True) == 0


def test_language_switcher_cannot_drop_a_locale(tmp_path: pathlib.Path, monkeypatch) -> None:
    stamp = _stamper()
    source = tmp_path / "README.md"
    chinese = tmp_path / "README.zh-CN.md"
    japanese = tmp_path / "README.ja.md"
    monkeypatch.setattr(stamp, "SOURCE", source)
    monkeypatch.setattr(stamp, "TRANSLATIONS", {"zh-CN": chinese, "ja": japanese})
    text = "# Phoenix MCP\n\n**English** | [日本語](README.ja.md)\n"
    assert stamp.language_link_problems(source, text)


def test_marked_locale_only_appendix_does_not_break_translation_alignment() -> None:
    stamp = _stamper()
    text = (
        "# Phoenix MCP\n\n翻訳。\n\n"
        f"{stamp.LOCALE_ONLY_START}\n\n地域固有の注記。\n\n{stamp.LOCALE_ONLY_END}\n\n"
        "## 文書\n\n本文。"
    )
    assert [block["key"] for block in stamp.parse_text(text)] == [
        "intro.block.1",
        "section.1.heading",
        "section.1.block.1",
    ]
