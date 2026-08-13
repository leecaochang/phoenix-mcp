"""Pin the counts the documentation states against the code.

Counts embedded in prose are the part of the docs that rots silently: nothing
breaks, the sentence still reads fine, and the number is only wrong. The
capability count on the overview page went stale twice, the tool count drifted
in two places at once, and then a written-out "Eighty-six tools" sat invisible
to this very test because its word dictionary only knew nine through
twenty-five, so the checks are mechanical and the word list is generated.

The counts themselves come from mcp_view.tool_catalog_counts(), the single
source of truth (also served by the admin info endpoint and printed by
scripts/count_tools.py). Nothing here re-derives them from the def lists.

The localized READMEs restate the same headline counts and are scanned by their
own patterns, because the English one cannot match a CJK noun (see NUMBER_ZH and
NUMBER_JA). A translated count that nothing checks is the original defect in a
second language.

Word-numbers below nine are deliberately unchecked: prose legitimately counts
subsets that small ("four retrieval tools", "Three panel tools"). Every real
catalog count is far above it, and "hundred"/"thousand" anywhere near a counted
noun fails outright with instructions to use digits, so a written-out total can
never slip past again.

`docs/` at the repo root is the only copy of the documentation. It is published
as a hosted site; the integration ships no copy of it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from custom_components.phoenix_mcp.const import AGENTCLI_PROVIDERS, CAPABILITY_NAMES
from custom_components.phoenix_mcp.mcp_view import tool_catalog_counts
from custom_components.phoenix_mcp.personas import PERSONA_DEFINITIONS

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"

# Every written-out number nine through ninety-nine, generated so no gap like
# "eighty-six" can exist. One through eight stay unlisted on purpose (subset
# counts); anything larger than ninety-nine must be written in digits, enforced
# by the hundred/thousand tripwire below.
_UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
          "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
WORDS: dict[str, int] = {"nine": 9}
for _i, _teen in enumerate(_TEENS):
    WORDS[_teen] = 10 + _i
for _ti, _ten in enumerate(_TENS):
    WORDS[_ten] = 20 + 10 * _ti
    for _ui, _unit in enumerate(_UNITS, start=1):
        WORDS[f"{_ten}-{_unit}"] = 20 + 10 * _ti + _ui

_NOUNS = r"(tools?|capabilit\w*|personas?|switches|providers?)"
# Longest first: a regex alternation is leftmost-first, so "twenty" would
# otherwise shadow "twenty-five".
_WORD_ALT = "|".join(sorted(WORDS, key=len, reverse=True))
# The lookbehind keeps digits embedded in identifiers (a model name like
# claude-opus-4-8, a version like 1.0.23) from reading as prose counts. The
# window excludes quotes as well as sentence/tag boundaries: a real count and
# its noun never straddle an HTML attribute boundary.
NUMBER = re.compile(
    r'(?<![\w.-])\b(' + _WORD_ALT + r'|\d{1,3})\b[^.<"]{0,45}?\b' + _NOUNS + r"\b",
    re.IGNORECASE,
)
# The same count with the noun FIRST ("tools: 139", "capabilities (26)"). The
# pattern above reads number-then-noun only, so this grammar was invisible to it.
# Its window is DELIBERATELY far tighter than NUMBER's 45 characters: reading
# right-to-left across a sentence sweeps up every number that merely follows the
# noun, and the first attempt read "capability returns 400" as a capability count
# of 400. Only light punctuation is allowed between the two, which is the whole
# difference between a stated count and a number that happens to come later.
NUMBER_AFTER_NOUN = re.compile(
    r'(?<![\w.-])\b' + _NOUNS + r"\b[\s:=(\[]{0,3}(" + _WORD_ALT + r"|\d{1,3})\b",
    re.IGNORECASE,
)
# A count with its noun ELIDED: "plus 116 more", "and 23 others". Neither pattern
# above can see one, since there is no noun on either side to anchor to. This is
# the exact shape the stale 108 hid in, so it is matched on the quantifier
# instead and the VALUE is checked against every count the page may legitimately
# state. Deliberately narrow: only the few phrasings that always mean a count,
# so an ordinary sentence containing a number cannot trip it.
ELIDED_COUNT = re.compile(
    r"(?<![\w.-])\b(?:plus|and|another|a further)\s+(" + _WORD_ALT + r"|\d{1,3})\b\s+"
    r"(?:more|others|further|additional)\b",
    re.IGNORECASE,
)
# A count too large for the word list must be digits; "one hundred and
# twenty-seven tools" is unparseable to this test and therefore forbidden.
BIG_WORD = re.compile(
    r"\b(hundred|thousand)\b[^.<]{0,45}?\b" + _NOUNS + r"\b", re.IGNORECASE
)

# The same counts, restated in the README's Simplified Chinese section. It needs
# its own pattern rather than another alternation branch: `\b` is defined
# between a word and a non-word character, and every CJK character is a word
# character, so "131 个工具" has no boundary before the noun and the pattern
# above matches nothing at all. The digits are the only part that can drift (a
# translated count is written in digits, never in characters), so the window
# between the number and its noun is deliberately tight.
_NOUNS_ZH = r"(工具|能力|角色|服务商)"
NUMBER_ZH = re.compile(r"(?<![\w.-])(\d{1,3})\s*[个种项]?\s*" + _NOUNS_ZH)
# Maps each Chinese noun onto the English key its allowed values are filed under.
_ZH_KIND = {"工具": "tool", "能力": "capabilit", "角色": "persona", "服务商": "provider"}

_NOUNS_JA = r"(ツール|機能|ペルソナ|プロバイダー)"
NUMBER_JA = re.compile(r"(?<![\w.-])(\d{1,3})\s*個?の?\s*" + _NOUNS_JA)
_JA_KIND = {"ツール": "tool", "機能": "capabilit", "ペルソナ": "persona", "プロバイダー": "provider"}


def _pages() -> list[pathlib.Path]:
    # README carries the same headline counts as the docs site and drifted the
    # same way, so it is scanned with them.
    return sorted(DOCS.glob("*.html")) + sorted(DOCS.parent.glob("README*.md"))


def test_docs_directory_is_present() -> None:
    """So the sweep below cannot pass by finding nothing to read."""
    assert len(_pages()) > 5


def test_word_list_has_no_gaps() -> None:
    """The 86 blind spot: every value 9..99 must be representable."""
    assert sorted(WORDS.values()) == list(range(9, 100))


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_no_stale_counts_in_prose(page: pathlib.Path) -> None:
    counts = tool_catalog_counts()
    allowed = {
        "tool": {counts["native"], counts["additional"], counts["total"]},
        "capabilit": {len(CAPABILITY_NAMES)},
        "persona": {len(PERSONA_DEFINITIONS)},
        "switches": {len(CAPABILITY_NAMES)},
        "provider": {len(AGENTCLI_PROVIDERS)},
    }

    stale: list[str] = []
    for lineno, line in enumerate(page.read_text(encoding="utf-8").split("\n"), 1):
        for big in BIG_WORD.finditer(line):
            stale.append(
                f"{page.name}:{lineno} says {big.group(0)!r};"
                " write the count in digits so this test can check it"
            )
        for pattern, num_group, noun_group in (
            (NUMBER, 1, 2),
            (NUMBER_AFTER_NOUN, 2, 1),
        ):
            for match in pattern.finditer(line):
                raw = match.group(num_group).lower()
                noun = match.group(noun_group).lower()
                value = WORDS.get(raw)
                if value is None:
                    value = int(raw)
                kind = next(k for k in allowed if noun.startswith(k))
                if value not in allowed[kind]:
                    stale.append(
                        f"{page.name}:{lineno} says {match.group(0)!r};"
                        f" {kind} should be one of {sorted(allowed[kind])}"
                    )
        for match in ELIDED_COUNT.finditer(line):
            raw = match.group(1).lower()
            value = WORDS.get(raw, None)
            if value is None:
                value = int(raw)
            # No noun to key on, so any count the page could legitimately be
            # stating is accepted; a value matching none of them is stale.
            every = {v for values in allowed.values() for v in values}
            if value not in every:
                stale.append(
                    f"{page.name}:{lineno} says {match.group(0)!r} with the noun left"
                    f" out; that count matches none of {sorted(every)}"
                )
        for match in NUMBER_ZH.finditer(line):
            kind = _ZH_KIND[match.group(2)]
            if int(match.group(1)) not in allowed[kind]:
                stale.append(
                    f"{page.name}:{lineno} says {match.group(0)!r};"
                    f" {kind} should be one of {sorted(allowed[kind])}"
                )
        for match in NUMBER_JA.finditer(line):
            kind = _JA_KIND[match.group(2)]
            if int(match.group(1)) not in allowed[kind]:
                stale.append(
                    f"{page.name}:{lineno} says {match.group(0)!r};"
                    f" {kind} should be one of {sorted(allowed[kind])}"
                )
    assert stale == []
