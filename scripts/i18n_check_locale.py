"""Check a translated locale file against the English catalog.

Every failure mode this guards is SILENT at runtime, which is why it exists.
Home Assistant compares each translated string's {placeholder} set against
English and deletes the key when they differ (helpers/translation.py), falling
back to English without telling anyone. A key the translator misspelled is
simply never used. A code payload that got translated along with the prose
around it produces instructions that cannot work. None of that shows up as an
error in the log the operator reads.

Run: python scripts/i18n_check_locale.py zh-Hans
Exits non-zero on any problem, and prints a coverage summary either way.
"""

from __future__ import annotations

import json
import pathlib
import re
import string
import sys
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "custom_components" / "phoenix_mcp"
CATALOGS = PACKAGE / "catalogs"
TRANSLATIONS = PACKAGE / "translations"

# Sections the panel and HA actually read. `entity` is deliberately absent from
# every translated locale (see KEEP_ENGLISH_SECTIONS).
SECTIONS = ("config", "issues", "panel", "notification", "voice")

# Which directory each section is stored in. To a translator, and to every check
# in this file, there is ONE catalog per language; it is split across two
# directories only because hassfest validates translations/ against a closed set
# of Home Assistant categories and errors on anything else, which fails the HACS
# submission. So Phoenix's own sections live in catalogs/ and HA's own stay put.
# Both halves are checked here: config is translated too, and dropping it from
# the sweep would have silently retired the guards that cover it.
SECTION_DIRS = {
    "config": TRANSLATIONS,
    "entity": TRANSLATIONS,
    "issues": TRANSLATIONS,
    "panel": CATALOGS,
    "notification": CATALOGS,
    "voice": CATALOGS,
}


def load(language: str) -> dict:
    """One language's whole catalog, both halves merged into one document.

    The section names are disjoint across the two files, so the merge is exact.
    A missing file contributes nothing (a locale need not translate every half).
    """
    doc: dict = {}
    for directory in (CATALOGS, TRANSLATIONS):
        path = directory / f"{language}.json"
        if path.exists():
            doc.update(json.loads(path.read_text(encoding="utf-8")))
    return doc


def languages() -> list[str]:
    """Every shipped locale, from either half. en is the source, not a locale."""
    stems = {p.stem for d in (CATALOGS, TRANSLATIONS) for p in d.glob("*.json")}
    return sorted(s for s in stems if s not in ("en", "strings"))

# Whole sections that stay English in every locale.
#
# entity: the six per-token sensor names. Their entity_id is slugified from the
# display name at first registration, so translating them would give a fresh
# install under a non-English locale different entity_ids from every other
# install. The names are also read back by anyone writing a template.
KEEP_ENGLISH_SECTIONS = ("entity",)

# Individual keys that stay English, with the reason grouped so the next person
# can tell a decision from an oversight. Absence from the locale file IS the
# mechanism: HA falls back per key.
KEEP_ENGLISH: dict[str, tuple[str, ...]] = {
    # NOTE (2026-07-30): the MESA enum values and the permission states
    # (READ/WRITE/DENY/INHERIT) USED to live here, on the argument
    # that the effective-resolution table prints the raw mesa-core value beside
    # them. That argument was wrong. Home Assistant itself translates entity
    # states for display while hass.states keeps the English slug, so
    # "translate the label, leave the raw value raw" is the HA-native pattern,
    # not an inconsistency. They are translated now; do not move them back.
    #
    # Proper nouns with nothing to translate.
    "proper nouns": (
        "panel.shell.tabMesa",
        "panel.audit.rowMesa",
        "notification.approval.title",
        "config.step.user.title",
    ),
    # A bare placeholder. Nothing to translate, and a byte-identical copy would
    # just be one more entry to keep in step for no benefit.
    "pure placeholder": (
        "panel.version.size",
        # A middot and a value that is already localized where it is built.
        "panel.approvals.cardTimeSuffix",
    ),
}

KEEP_ENGLISH_KEYS = {key for group in KEEP_ENGLISH.values() for key in group}

# Substrings that must survive verbatim into the translation because something
# outside the catalog compares against them or a user is meant to type or read
# them literally. <code> spans are checked automatically and are not listed.
LITERALS: dict[str, tuple[str, ...]] = {
    # WipeConfirmModal compares typed !== "WIPE" and its placeholder attribute
    # is a hardcoded literal, so a translated WIPE tells the operator to type
    # something the button will never accept.
    "panel.settings.wipeTypeConfirm": ("WIPE",),
    "panel.caps.cap_restart.description": ("homeassistant.restart", "homeassistant.stop"),
    "panel.caps.cap_filesystem.description": ("www/", "themes/", "custom_templates/"),
    "panel.caps.cap_service_response.description": ("conversation.process",),
    "panel.caps.cap_esphome_yaml.description": ("esphome/",),
    "panel.caps.cap_yaml_edit.description": ("configuration.yaml", "secrets.yaml", "!secret"),
    # panel.diff.set_yaml_config no longer names configuration.yaml: the tool
    # writes any include target, so the summary carries a {file} placeholder and
    # pinning the literal would force every locale to name the wrong file.
    # These echo an API field name back at the caller; translating it would name
    # a field that does not exist in the request body.
    "panel.adminError.expiresImmutable": ("expires_at",),
    "panel.adminError.badExpiresAt": ("expires_at",),
    "panel.adminError.nameRequired": ("name",),
    "panel.perms.simEntityPlaceholder": ("entity_id", "light.kitchen"),
    "panel.tokens.namePlaceholder": ("my_token",),
    # ai_task.py hardcodes _attr_name = "Phoenix MCP AI Task"; the entity is not
    # localized, so a translated mention would name something the operator
    # cannot find in Home Assistant.
    "panel.settings.aiTaskDefaultModalBody": ("Phoenix MCP AI Task",),
}

# Values a locale may legitimately leave byte-identical to English. Anything
# else that matches English exactly is almost certainly a copy that was never
# translated, which nothing at runtime would ever report.
ALLOW_IDENTICAL = frozenset({
    "panel.perms.colId",              # "ID"
    "panel.tokens.namePlaceholder",   # "my_token", a literal example value
    "panel.changes.typeEsphomeYaml",  # "ESPHome YAML": a product name plus a format name
    # Product and plan names are proper nouns rather than translatable prose.
    "panel.settings.agentcliZaiCoding",
    "panel.settings.agentcliZaiStandard",
    "panel.settings.providerAnthropic",
    "panel.settings.providerCerebras",
    "panel.settings.providerDeepSeek",
    "panel.settings.providerFireworks",
    "panel.settings.providerGemini",
    "panel.settings.providerGrok",
    "panel.settings.providerGroq",
    "panel.settings.providerKimi",
    "panel.settings.providerMeta",
    "panel.settings.providerMiniMax",
    "panel.settings.providerMistral",
    "panel.settings.providerNvidia",
    "panel.settings.providerOpenAI",
    "panel.settings.providerOpenRouter",
    "panel.settings.providerQwen",
    "panel.settings.providerTogether",
    "panel.settings.providerZai",
})

# Keys whose leading or trailing ASCII space is allowed to disappear, because
# the punctuation it spaces gets replaced by a full-width or ideographic form
# that carries its own spacing. Everywhere else that whitespace is load-bearing
# glue between fragments and losing it is a real defect.
WHITESPACE_EXEMPT = frozenset({
    "panel.common.listSeparator",   # ", " becomes the ideographic comma
    "panel.mesa.currentFilter",     # "Current filter: " becomes a full-width colon
    "panel.perms.simResult",        # "Result: " likewise
    "panel.agentchat.approvalReason",  # leading " - " becomes a full-width comma
    # Appended after a sentence: English needs the separating space, Chinese
    # does not, because the preceding full stop is already full-width.
    "panel.mesaSuggestion.baseline_note",
})

# CLAUDE.md forbids these everywhere, catalog values included. The ideographic
# characters that look similar in Chinese text are not the same code points and
# are fine.
FORBIDDEN = re.compile(r"[—–→]|[\U0001F300-\U0001FAFF☀-➿]")

TAG = re.compile(r"<(/?)(\w+)>")
CODE_SPAN = re.compile(r"<code>(.*?)</code>", re.DOTALL)

# Machine-readable content a reader must be able to type, search for, or find
# on disk. Bare words stay out because a translated UI label can legitimately
# replace one. Files come first so `tool_policy.json` is not split into a
# snake_case token plus an extension. Absolute paths require more than one
# segment, which keeps prose units such as `/min` out; relative directories are
# recognized by their trailing slash.
FILE_PATTERN = r"[A-Za-z0-9_.~*/-]+\.(?:yaml|yml|json|py|md|txt)"
TECH_LITERAL = re.compile(
    rf"(?:"
    rf"{FILE_PATTERN}"
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"
    r"|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|(?<![\w.])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?![\w])"
    r"|[a-z][a-z0-9+.-]*://[^\s<]*"
    r"|(?<!\w)~?/[^\s<]+/[^\s<]*"
    r"|(?<![\w/])[A-Za-z_.-][A-Za-z0-9_.-]*/(?![A-Za-z0-9/])"
    r")"
)
FILE = re.compile(FILE_PATTERN)
NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,\u00a0\u202f ]\d+)*")
NUMBER_SEPARATOR = re.compile(r"[.,\u00a0\u202f ]")
PERCENT = re.compile(r"(?<![A-Za-z0-9])(\d+)\s?[%％]")
MAGNITUDE = re.compile(r"(?<![A-Za-z0-9])(\d+)([KMGT])(?![A-Za-z])")
STORAGE_UNIT = re.compile(r"(?<![A-Za-z0-9])(\d+)\s*([KMGT]B)(?![A-Za-z])", re.I)
COMPARISON = re.compile(r"([A-Za-z_]\w*)\s*([<>]=?)\s*(\d+)")
ORDERED_PAIR = re.compile(
    rf"({NUMBER.pattern})\s*([-\u2010-\u2015:\uff1a])\s*({NUMBER.pattern})(?![A-Za-z0-9])"
)
SENTENCE_PUNCTUATION = ".,;:!?)]}\"'\u00bb\u201d"
PROSE_ABBREVIATIONS = frozenset({"e.g", "i.e"})


def _canonical_number(token: str) -> tuple[str, ...]:
    """Normalize locale punctuation without flattening decimals or versions."""
    groups = NUMBER_SEPARATOR.split(token)
    if (
        len(groups) > 1
        and len(groups[0]) <= 3
        and all(len(group) == 3 for group in groups[1:])
    ):
        return ("".join(groups),)
    return tuple(groups)


def _numbers(value: str) -> Counter[tuple[str, ...]]:
    """Numeric claims as a multiset, so dropped duplicates remain visible."""
    return Counter(_canonical_number(token) for token in NUMBER.findall(value))


def _literal_counts(value: str) -> Counter[str]:
    """Conservative machine-readable tokens with sentence punctuation removed."""
    tokens: list[str] = []
    for match in TECH_LITERAL.findall(value):
        token = match if match.endswith("...") else match.rstrip(SENTENCE_PUNCTUATION)
        if token and token not in PROSE_ABBREVIATIONS:
            tokens.append(token)
    return Counter(tokens)


def _ordered_pairs(value: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Ordered numeric pairs, independent of dash or colon punctuation."""
    return {
        (_canonical_number(low), _canonical_number(high))
        for low, _, high in ORDERED_PAIR.findall(value)
    }


def machine_parity_problems(source: str, value: str) -> list[str]:
    """Language-independent contradictions between English and a translation."""
    problems: list[str] = []
    source_numbers, value_numbers = _numbers(source), _numbers(value)
    lost_numbers = source_numbers - value_numbers
    added_numbers = value_numbers - source_numbers
    if lost_numbers:
        problems.append(f"numbers lost {sorted('.'.join(n) for n in lost_numbers.elements())}")
    if added_numbers:
        problems.append(f"numbers added {sorted('.'.join(n) for n in added_numbers.elements())}")

    source_literals, value_literals = _literal_counts(source), _literal_counts(value)
    lost_literals = source_literals - value_literals
    added_literals = value_literals - source_literals
    if lost_literals:
        problems.append(f"technical content dropped {sorted(lost_literals.elements())}")
    if added_literals:
        problems.append(f"technical content invented {sorted(added_literals.elements())}")

    for digits in PERCENT.findall(source):
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(digits)}\s?[%％]", value):
            problems.append(f"percentage sign dropped from {digits}%")
    for digits, suffix in MAGNITUDE.findall(source):
        attached = {
            match.group(1).upper()
            for match in re.finditer(
                rf"(?<![A-Za-z0-9]){re.escape(digits)}([A-Za-z]+)", value
            )
        }
        if attached and suffix.upper() not in attached:
            problems.append(f"magnitude changed from {digits}{suffix}")
    for digits, unit in STORAGE_UNIT.findall(source):
        attached = {
            match.group(1).upper()
            for match in re.finditer(
                rf"(?<![A-Za-z0-9]){re.escape(digits)}\s*([KMGT]B)(?![A-Za-z])",
                value,
                re.I,
            )
        }
        # A spelled-out localized unit is valid. Only contradict an English
        # abbreviation when the translation also chose an abbreviation.
        if attached and unit.upper() not in attached:
            problems.append(f"storage unit changed from {digits} {unit.upper()}")
    for name, operator, digits in COMPARISON.findall(source):
        if not re.search(
            rf"(?<!\w){re.escape(name)}\s*{re.escape(operator)}\s*{re.escape(digits)}(?!\d)",
            value,
        ):
            problems.append(f"comparison changed from {name} {operator} {digits}")

    translated_pairs = _ordered_pairs(value)
    for low, separator, high in ORDERED_PAIR.findall(source):
        pair = (_canonical_number(low), _canonical_number(high))
        if pair[::-1] in translated_pairs and pair not in translated_pairs:
            joiner = ":" if separator in ":\uff1a" else "-"
            problems.append(f"ordered pair reversed from {low}{joiner}{high}")
    return problems


def exception_manifest_problems(
    en_flat: dict[str, str], locale_flats: list[dict[str, str]]
) -> list[str]:
    """Stale or no-longer-needed entries in every exception manifest."""
    problems: list[str] = []
    for key in sorted(KEEP_ENGLISH_KEYS):
        if key not in en_flat:
            problems.append(f"KEEP_ENGLISH references missing key {key}")
    for key, literals in sorted(LITERALS.items()):
        if key not in en_flat:
            problems.append(f"LITERALS references missing key {key}")
            continue
        for literal in literals:
            if literal not in en_flat[key]:
                problems.append(f"LITERALS {key} references absent English literal {literal!r}")
    for key in sorted(ALLOW_IDENTICAL):
        if key not in en_flat:
            problems.append(f"ALLOW_IDENTICAL references missing key {key}")
        elif not any(
            key in locale and locale[key].strip() == en_flat[key].strip()
            for locale in locale_flats
        ):
            problems.append(f"ALLOW_IDENTICAL {key} is no longer used by any locale")
    for key in sorted(WHITESPACE_EXEMPT):
        if key not in en_flat:
            problems.append(f"WHITESPACE_EXEMPT references missing key {key}")
        elif not any(
            key in locale
            and (
                locale[key].startswith(" ") != en_flat[key].startswith(" ")
                or locale[key].endswith(" ") != en_flat[key].endswith(" ")
            )
            for locale in locale_flats
        ):
            problems.append(f"WHITESPACE_EXEMPT {key} is no longer used by any locale")
    return problems


def flatten(node: object, prefix: str = "") -> dict[str, str]:
    """Dotted-key view of a catalog, matching how HA and the panel look keys up."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(flatten(value, f"{prefix}{key}." if prefix or True else key))
    elif isinstance(node, str):
        out[prefix[:-1]] = node
    return out


def placeholders(value: str) -> set[str] | None:
    """The {name} set HA will compare, or None if the string will not parse."""
    try:
        return {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
    except ValueError:
        return None


def tag_shape(value: str) -> list[str]:
    """Ordered inline tags, so tRich renders the same structure in both languages."""
    return [f"{slash}{name}" for slash, name in TAG.findall(value)]


def check(language: str) -> int:
    english = load("en")
    if not any((d / f"{language}.json").exists() for d in (CATALOGS, TRANSLATIONS)):
        print(f"no {language}.json in {CATALOGS} or {TRANSLATIONS}")
        return 1
    other = load(language)
    # Named for the messages below, which point at a file the reader must open.
    # Phoenix's own sections are the bulk of every locale, so that half is the
    # useful thing to name when a problem is not section-specific.
    path = CATALOGS / f"{language}.json"

    problems: list[str] = []
    en_flat: dict[str, str] = {}
    for section in SECTIONS + KEEP_ENGLISH_SECTIONS:
        en_flat.update(flatten(english.get(section, {}), f"{section}."))
    tr_flat: dict[str, str] = {}
    for section in other:
        tr_flat.update(flatten(other[section], f"{section}."))

    locale_flats = []
    for shipped_language in languages():
        shipped = load(shipped_language)
        locale_flat: dict[str, str] = {}
        for section in shipped:
            locale_flat.update(flatten(shipped[section], f"{section}."))
        locale_flats.append(locale_flat)
    problems.extend(exception_manifest_problems(en_flat, locale_flats))

    for section in KEEP_ENGLISH_SECTIONS:
        if section in other:
            problems.append(
                f"section '{section}' must stay English; remove it from {path.name}"
            )

    # The source catalog is not exempt from the house style. Judging only what a
    # translation "introduces" quietly grandfathered whatever English already had.
    for key in sorted(en_flat):
        bad = set(FORBIDDEN.findall(en_flat[key]))
        if bad:
            problems.append(f"en.json {key}: contains {''.join(sorted(bad))!r} (forbidden in every catalog)")

    for key in sorted(tr_flat):
        value = tr_flat[key]
        if key not in en_flat:
            problems.append(f"{key}: not a key in en.json (typo, or a stale rename)")
            continue
        if key in KEEP_ENGLISH_KEYS:
            problems.append(f"{key}: deliberately stays English; remove it")
            continue

        source = en_flat[key]
        want, got = placeholders(source), placeholders(value)
        if got is None:
            problems.append(f"{key}: unbalanced brace, HA cannot parse it")
        elif want != got:
            problems.append(
                f"{key}: placeholders {sorted(got)} != English {sorted(want)}"
                " (HA drops this key silently)"
            )

        if tag_shape(source) != tag_shape(value):
            problems.append(
                f"{key}: inline tags {tag_shape(value)} != English {tag_shape(source)}"
            )

        for span in CODE_SPAN.findall(source):
            if span and "{" not in span and span not in value:
                problems.append(f"{key}: <code> payload {span!r} must pass through verbatim")

        for literal in LITERALS.get(key, ()):
            if literal not in value:
                problems.append(f"{key}: must contain {literal!r} verbatim")

        for problem in machine_parity_problems(source, value):
            problems.append(f"{key}: {problem}")

        bad = set(FORBIDDEN.findall(value))
        if bad:
            problems.append(
                f"{key}: contains {''.join(sorted(bad))!r}"
                " (forbidden dash, arrow, or emoji)"
            )

        # An untranslated copy renders English inside a translated screen and is
        # invisible to every other check, since it is a perfectly valid string.
        if key not in ALLOW_IDENTICAL and value.strip() == source.strip() and any(
            ch.isalpha() and ch.isascii() for ch in source
        ):
            problems.append(f"{key}: identical to English (untranslated, or add it to ALLOW_IDENTICAL)")

        if key not in WHITESPACE_EXEMPT and (
            source.startswith(" ") != value.startswith(" ")
            or source.endswith(" ") != value.endswith(" ")
        ):
            problems.append(f"{key}: leading/trailing space differs from English (it is load-bearing)")

    translatable = {k for k in en_flat if k not in KEEP_ENGLISH_KEYS}
    translatable = {k for k in translatable if not k.startswith(KEEP_ENGLISH_SECTIONS)}
    done = translatable & set(tr_flat)
    print(f"{language}: {len(done)}/{len(translatable)} translatable keys covered")

    # An untranslated key is not a crash, it just silently renders English in the
    # middle of a translated screen, so nothing else will ever report it. Phoenix
    # is multilingual from here on: a new English string is only finished once
    # every locale has it, or the key is in KEEP_ENGLISH with a reason.
    for key in sorted(translatable - set(tr_flat)):
        problems.append(f"{key}: missing from {path.name} (translate it, or add it to KEEP_ENGLISH)")
    by_section: dict[str, list[int]] = {}
    for key in sorted(translatable):
        top = key.split(".")[1] if key.startswith("panel.") else key.split(".")[0]
        counts = by_section.setdefault(top, [0, 0])
        counts[1] += 1
        if key in tr_flat:
            counts[0] += 1
    for top, (have, total) in sorted(by_section.items(), key=lambda kv: -kv[1][1]):
        flag = "" if have == total else "  <-- incomplete"
        print(f"  {have:5d}/{total:<5d} {top}{flag}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nno problems")
    return 0


if __name__ == "__main__":
    sys.exit(check(sys.argv[1] if len(sys.argv) > 1 else "zh-Hans"))
