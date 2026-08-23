"""Guard localized READMEs against silent drift from the English source.

The localized READMEs are full translations rather than independent documents.
This script splits them into matching Markdown blocks and records both the
English fingerprint each block was translated from and the translation's own
fingerprint. Tests then fail when either side changes without an explicit
restamp, when the document structure diverges, or when URLs, code literals,
numbers, and protected product names stop matching.

The normal stamp command refuses to accept changed English with an unchanged
translation. This catches the most dangerous shortcut: updating the baseline
without reviewing the locale. Use --allow-unchanged only when an English-only
edit genuinely requires no translated change; the source-stamp diff still makes
that decision visible in review.

Run: python scripts/readme_i18n_stamp.py --all
     python scripts/readme_i18n_stamp.py --check --all
     python scripts/readme_i18n_stamp.py ja
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = REPO / "README.md"
TRANSLATIONS = {
    "de": REPO / "README.de.md",
    "es": REPO / "README.es.md",
    "fr": REPO / "README.fr.md",
    "zh-CN": REPO / "README.zh-CN.md",
    "zh-Hant": REPO / "README.zh-Hant.md",
    "ja": REPO / "README.ja.md",
    "ko": REPO / "README.ko.md",
    "nl": REPO / "README.nl.md",
    "ru": REPO / "README.ru.md",
}
BASELINE = REPO / "tests" / "contract" / "readme_i18n_source_hashes.json"

COMMENT = (
    "Fingerprints of each English README block and its localized counterpart. "
    "Written by scripts/readme_i18n_stamp.py and checked by tests/test_readme_i18n.py. "
    "A source mismatch means English changed after translation."
)

LINK_TARGET = re.compile(r"\[[^]]*\]\(([^)]+)\)")
INLINE_CODE = re.compile(r"`[^`]+`")
# Keep locale counters such as Korean "159개" attached to the number while
# still avoiding numbers embedded in identifiers, versions, or URLs.
NUMBER = re.compile(r"(?<![A-Za-z0-9_.-])\d+(?:\.\d+)*(?![A-Za-z0-9_.-])")
ORDERED_ITEM = re.compile(r"^\d+\. ")
PROTECTED = (
    "Phoenix MCP",
    "Home Assistant",
    "MESA",
    "HACS",
    "Ollama",
    "Claude Code",
    "Cursor",
    "ChatGPT/Codex",
    "Gemini",
    "MCP",
    "Python",
    "API",
    "HA",
)
LOCALE_ONLY_START = "<!-- readme-i18n:locale-only:start -->"
LOCALE_ONLY_END = "<!-- readme-i18n:locale-only:end -->"
LOCALE_ONLY = re.compile(
    re.escape(LOCALE_ONLY_START) + r".*?" + re.escape(LOCALE_ONLY_END), re.DOTALL
)


def digest(value: str) -> str:
    """Short stable fingerprint for one exact Markdown block."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _chunks(text: str) -> list[str]:
    text = LOCALE_ONLY.sub("", text)
    return [chunk.strip() for chunk in re.split(r"\n\s*\n", text.strip()) if chunk.strip()]


def _is_metadata(chunk: str) -> bool:
    lines = chunk.splitlines()
    return (
        chunk == "# Phoenix MCP"
        or all(line.startswith("[![") for line in lines)
        or sum(
            name in chunk for name in ("README.md", "README.de.md", "README.es.md", "README.fr.md", "README.zh-CN.md", "README.zh-Hant.md", "README.ja.md", "README.ko.md", "README.nl.md", "README.ru.md")
        ) >= 2
    )


def parse_text(text: str) -> list[dict[str, str | int]]:
    """Return stable, structural block IDs independent of translated headings."""
    blocks: list[dict[str, str | int]] = []
    section = 0
    subsection = 0
    sequence = 0

    for chunk in _chunks(text):
        if _is_metadata(chunk):
            continue
        if chunk.startswith("## ") and not chunk.startswith("### "):
            section += 1
            subsection = 0
            sequence = 0
            key = f"section.{section}.heading"
            kind = "heading-2"
        elif chunk.startswith("### "):
            subsection += 1
            sequence = 0
            key = f"section.{section}.subsection.{subsection}.heading"
            kind = "heading-3"
        else:
            sequence += 1
            scope = "intro" if section == 0 else f"section.{section}"
            if subsection:
                scope += f".subsection.{subsection}"
            key = f"{scope}.block.{sequence}"
            lines = chunk.splitlines()
            if all(line.startswith("- ") for line in lines):
                kind = "unordered-list"
            elif all(ORDERED_ITEM.match(line) for line in lines):
                kind = "ordered-list"
            else:
                kind = "paragraph"
        blocks.append({"key": key, "kind": kind, "text": chunk, "lines": len(chunk.splitlines())})
    return blocks


def parse(path: pathlib.Path) -> list[dict[str, str | int]]:
    return parse_text(path.read_text(encoding="utf-8"))


def block_map(blocks: list[dict[str, str | int]]) -> dict[str, str]:
    return {str(block["key"]): str(block["text"]) for block in blocks}


def structure_problems(
    source: list[dict[str, str | int]], translated: list[dict[str, str | int]]
) -> list[str]:
    """Require the localized README to remain a full structural translation."""
    problems: list[str] = []
    source_shape = [(b["key"], b["kind"], b["lines"]) for b in source]
    translated_shape = [(b["key"], b["kind"], b["lines"]) for b in translated]
    if source_shape != translated_shape:
        problems.append(
            "Markdown block structure differs from README.md\n"
            f"    source: {source_shape}\n"
            f"    locale: {translated_shape}"
        )
    return problems


def _protected_counts(value: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for literal in PROTECTED:
        pattern = re.compile(
            rf"(?<![A-Za-z]){re.escape(literal)}(?![A-Za-z])"
        )
        counts[literal] = len(pattern.findall(value))
    return +counts


def parity_problems(source: str, translated: str) -> list[str]:
    """Machine-readable content that must survive a prose translation."""
    problems: list[str] = []
    comparisons = (
        ("link targets", Counter(LINK_TARGET.findall(source)), Counter(LINK_TARGET.findall(translated))),
        ("inline code", Counter(INLINE_CODE.findall(source)), Counter(INLINE_CODE.findall(translated))),
        ("numbers", Counter(NUMBER.findall(source)), Counter(NUMBER.findall(translated))),
        ("protected names", _protected_counts(source), _protected_counts(translated)),
        ("bold markers", Counter({"**": source.count("**")}), Counter({"**": translated.count("**")})),
    )
    for label, expected, actual in comparisons:
        if expected != actual:
            problems.append(f"{label} differ: source={dict(expected)}, locale={dict(actual)}")
    return problems


def language_link_problems(path: pathlib.Path, text: str) -> list[str]:
    """Every README links to both of the other language editions."""
    expected = {
        candidate.name
        for candidate in [SOURCE, *TRANSLATIONS.values()]
        if candidate != path
    }
    actual = {
        target
        for target in LINK_TARGET.findall(text)
        if not target.startswith(("http://", "https://"))
    }
    if actual == expected:
        return []
    return [
        f"language links differ in {path.name}: expected={sorted(expected)}, "
        f"actual={sorted(actual)}"
    ]


def document_problems(
    source_path: pathlib.Path, translated_path: pathlib.Path
) -> tuple[list[dict[str, str | int]], list[dict[str, str | int]], list[str]]:
    source_text = source_path.read_text(encoding="utf-8")
    translated_text = translated_path.read_text(encoding="utf-8")
    source_blocks = parse_text(source_text)
    translated_blocks = parse_text(translated_text)
    problems = structure_problems(source_blocks, translated_blocks)
    problems.extend(language_link_problems(source_path, source_text))
    problems.extend(language_link_problems(translated_path, translated_text))

    if translated_text.count(LOCALE_ONLY_START) != translated_text.count(LOCALE_ONLY_END):
        problems.append("unbalanced locale-only appendix markers")

    if not problems:
        for source_block, translated_block in zip(source_blocks, translated_blocks, strict=True):
            for problem in parity_problems(
                str(source_block["text"]), str(translated_block["text"])
            ):
                problems.append(f"{source_block['key']}: {problem}")

    source_badges = [line for line in source_text.splitlines() if line.startswith("[![")]
    translated_badges = [line for line in translated_text.splitlines() if line.startswith("[![")]
    if source_badges != translated_badges:
        problems.append("badge markup differs from README.md")

    return source_blocks, translated_blocks, problems


def load_baseline() -> dict[str, dict[str, dict[str, str]]]:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8")).get("locales", {})


def current_stamps(
    source_blocks: list[dict[str, str | int]],
    translated_blocks: list[dict[str, str | int]],
) -> dict[str, dict[str, str]]:
    source = block_map(source_blocks)
    translated = block_map(translated_blocks)
    return {
        key: {"source": digest(source[key]), "translation": digest(translated[key])}
        for key in sorted(source)
    }


def audit(
    current: dict[str, dict[str, str]], stamped: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """Classify exact source and translation drift against the contract."""
    return {
        "source_stale": sorted(
            key for key in current.keys() & stamped.keys()
            if current[key]["source"] != stamped[key].get("source")
        ),
        "translation_changed": sorted(
            key for key in current.keys() & stamped.keys()
            if current[key]["translation"] != stamped[key].get("translation")
        ),
        "unstamped": sorted(current.keys() - stamped.keys()),
        "orphaned": sorted(stamped.keys() - current.keys()),
    }


def unchanged_translation_keys(
    current: dict[str, dict[str, str]], stamped: dict[str, dict[str, str]]
) -> list[str]:
    """English changed, but the locale block is byte-for-byte untouched."""
    return sorted(
        key for key in current.keys() & stamped.keys()
        if current[key]["source"] != stamped[key].get("source")
        and current[key]["translation"] == stamped[key].get("translation")
    )


def check(language: str) -> int:
    path = TRANSLATIONS[language]
    source_blocks, translated_blocks, problems = document_problems(SOURCE, path)
    current = current_stamps(source_blocks, translated_blocks) if not problems else {}
    drift = audit(current, load_baseline().get(path.name, {})) if not problems else {}
    if not problems and not any(drift.values()):
        return 0

    print(f"{path.name} is not synchronized with README.md:")
    for problem in problems:
        print(f"  {problem}")
    labels = {
        "source_stale": "English changed after translation",
        "translation_changed": "translation changed without a new stamp",
        "unstamped": "block has no source stamp",
        "orphaned": "stamp has no matching block",
    }
    for kind, keys in drift.items():
        if keys:
            print(f"  {labels[kind]}: {', '.join(keys)}")
    print(f"\nReview the translation, then run: python scripts/readme_i18n_stamp.py {language}")
    return 1


def stamp(language: str, allow_unchanged: bool = False) -> int:
    path = TRANSLATIONS[language]
    source_blocks, translated_blocks, problems = document_problems(SOURCE, path)
    if problems:
        print(f"REFUSED {path.name}:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    current = current_stamps(source_blocks, translated_blocks)
    baseline = load_baseline()
    unchanged = unchanged_translation_keys(current, baseline.get(path.name, {}))
    if unchanged and not allow_unchanged:
        print(
            f"REFUSED {path.name}: English changed but these translations did not:"
        )
        for key in unchanged:
            print(f"  {key}")
        print("Review and translate them, or use --allow-unchanged for an English-only edit.")
        return 1

    baseline[path.name] = current
    BASELINE.write_text(
        json.dumps(
            {"_comment": COMMENT, "locales": dict(sorted(baseline.items()))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"stamped {len(current)} README blocks for {path.name}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    checking = "--check" in args
    allow_unchanged = "--allow-unchanged" in args
    names = [arg for arg in args if not arg.startswith("--")]
    targets = list(TRANSLATIONS) if "--all" in args else names
    if not targets or any(name not in TRANSLATIONS for name in targets):
        print(__doc__)
        return 2
    return max(
        check(name) if checking else stamp(name, allow_unchanged)
        for name in targets
    )


if __name__ == "__main__":
    sys.exit(main())
