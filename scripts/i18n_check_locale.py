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

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "custom_components" / "phoenix_mcp"
CATALOGS = PACKAGE / "catalogs"
TRANSLATIONS = PACKAGE / "translations"

# Sections the panel and HA actually read. `entity` is deliberately absent from
# every translated locale (see KEEP_ENGLISH_SECTIONS).
SECTIONS = ("config", "panel", "notification", "voice")

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
    "panel.diff.set_yaml_config": ("configuration.yaml",),
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
    "panel.audit.rowIp",              # "IP"
    "panel.perms.colId",              # "ID"
    "panel.tokens.namePlaceholder",   # "my_token", a literal example value
    "panel.changes.typeEsphomeYaml",  # "ESPHome YAML": a product name plus a format name
})

# Keys whose leading or trailing ASCII space is allowed to disappear, because
# the punctuation it spaces gets replaced by a full-width or ideographic form
# that carries its own spacing. Everywhere else that whitespace is load-bearing
# glue between fragments and losing it is a real defect.
WHITESPACE_EXEMPT = frozenset({
    "panel.common.listSeparator",   # ", " becomes the ideographic comma
    "panel.mesa.currentFilter",     # "Current filter: " becomes a full-width colon
    "panel.perms.simResult",        # "Result: " likewise
    "panel.mesa.effEnforcement",    # leading ", " becomes a full-width comma
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
