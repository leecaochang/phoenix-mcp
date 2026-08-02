"""Helper for the panel i18n string extraction.

Applies (old_code, new_code) rewrites to a source file while adding the matching
catalog entries, and refuses to do either unless two things hold:

  1. old_code appears in the file exactly as given, so the source side of a
     rewrite can never be approximated.
  2. every catalog value appears verbatim inside the old_code it came from, so
     the extracted English cannot drift from what the code actually rendered.

That second check is the point of this script. Retyping a 250-character
capability description into JSON by hand is exactly where a silent wording
change would slip through, and no test asserts on most of those strings.

Usage: import apply() and call it with the rewrites for one source file.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOG = REPO / "custom_components" / "phoenix_mcp" / "catalogs" / "en.json"


def _load() -> dict:
    return json.loads(CATALOG.read_text())


def _save(doc: dict) -> None:
    CATALOG.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _put(panel: dict, dotted: str, value: str) -> None:
    node = panel
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _fidelity_problems(key: str, value: str, old: str) -> list[str]:
    """Check that an extracted string really came out of the code it replaces.

    A plain string must appear in old_code outright. A template like "{m}m ago"
    cannot, because the source spelled it `${m}m ago`, so every literal run
    BETWEEN the placeholders has to appear instead. That still catches a
    reworded or invented sentence, which is what this guards against.
    """
    if "{" not in value:
        if value in old:
            return []
        return [f"{key}: {value[:60]!r} does not occur in its old_code"]
    problems = []
    for fragment in re.split(r"\{\w+\}", value):
        if fragment and fragment not in old:
            problems.append(f"{key}: fragment {fragment[:50]!r} not in its old_code")
    return problems


def apply(path: str, rewrites: list[tuple[str, str, dict[str, str]]],
          fragment: str | None = None) -> None:
    """Rewrite one file and record its catalog entries.

    Each rewrite is (old_code, new_code, {key: english}). Nothing is written
    unless every old_code is present and every english value traces back to the
    old_code it came from. With `fragment`, catalog entries go to that JSON file
    instead of en.json, so parallel workers never race on it.
    """
    src = REPO / path
    text = src.read_text()

    problems = []
    for old, _new, entries in rewrites:
        if old not in text:
            problems.append(f"not found in {path}: {old[:70]!r}")
            continue
        for key, value in entries.items():
            problems.extend(_fidelity_problems(key, value, old))
    if problems:
        for p in problems:
            print("FAIL", p)
        sys.exit(1)

    doc = None
    if fragment:
        frag = pathlib.Path(fragment)
        panel = json.loads(frag.read_text()) if frag.exists() else {}
    else:
        doc = _load()
        panel = doc["panel"]

    for old, new, entries in rewrites:
        text = text.replace(old, new)
        for key, value in entries.items():
            _put(panel, key, value)

    src.write_text(text)
    if fragment:
        pathlib.Path(fragment).write_text(json.dumps(panel, indent=2, ensure_ascii=False))
    else:
        _save(doc)
    total = sum(len(e) for _, _, e in rewrites)
    print(f"{path}: {len(rewrites)} rewrites, {total} keys")
