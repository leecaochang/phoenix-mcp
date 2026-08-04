"""Key-addressed splicing for a mapping-shaped YAML document.

Replaces, adds, or removes ONE dotted key inside a YAML file while leaving every
other byte exactly as it was. Pure functions, no hass, following esphome_yaml.py
and yaml_includes.py; the tool that calls it lives in tools/config_files.py.

The approach is span-surgical for the same reason esphome_yaml's is. A
parse-and-re-dump round trip produces a semantically identical file and destroys
everything a human put there: comments, blank-line grouping, key order, quoting
style. configuration.yaml is hand-maintained and heavily commented, so rewriting
it wholesale to change one key is not an acceptable cost. `yaml.compose` gives
every node its exact source position, so the new text is spliced in at the
addressed key's own lines and nothing else moves.

Spans are computed in LINES rather than byte offsets, mirroring
yaml_includes._FileScan: PyYAML's end_mark for a block collection runs to the
start of the next token, which swallows the blank lines and comments that sit
between two keys and belong to the SECOND one. Each key's span therefore ends
where its next sibling begins, with trailing blank and comment lines trimmed back
out, so a comment written above a key stays with that key.

Two refusals are structural rather than cautious. Nothing is created along the
way, so a path whose parent is missing is a typo rather than an instruction to
build the tree (the resolve_json_path rule); and a flow-style mapping ({...} on
one line) has no per-key line span to address, so a key inside one is refused
rather than guessed at.

Every patch is VERIFIED before it is returned: the spliced text is re-parsed and
compared against the old document with only the addressed path changed. A span
bug therefore surfaces as a refusal rather than as a quietly mangled
configuration.yaml, which is the same self-check discipline yaml_includes applies
to its own splices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import copy

import yaml

from .yaml_includes import PhoenixLenientLoader, YamlParseError, load_tagged_lenient

PATCH_OPS = ("set", "remove")

_MISSING = object()


class PatchError(Exception):
    """A patch that cannot be applied. The message is shown to the caller."""


@dataclass(frozen=True)
class PatchResult:
    """A verified patch: the new file text and the key's previous source text."""

    text: str
    before: str | None


def split_key(key: Any) -> list[str]:
    """Split a dotted key path into mapping keys, refusing an empty segment.

    Keys containing a literal dot cannot be addressed, which is the same limit
    get_yaml_config's `key` argument already has; both stop at the mapping whose
    keys are entity ids (homeassistant.customize) and edit it whole.
    """
    if not isinstance(key, str) or not key.strip():
        raise PatchError("key must be a dotted path of mapping keys, e.g. 'recorder.include'.")
    parts = [part.strip() for part in key.split(".")]
    if any(not part for part in parts):
        raise PatchError(f"key '{key}' has an empty path segment.")
    return parts


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError as err:
        raise PatchError(f"configuration.yaml is not valid YAML, so no key can be located in it: {err}") from err


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _child_spans(node: yaml.nodes.MappingNode, lines: list[str], limit: int) -> list[tuple[int, int]]:
    """Line span of each pair in a block mapping, trailing blanks trimmed.

    A pair runs from its own key line to the start of the next pair's, so a
    comment above a key belongs to that key and survives an edit to the one
    before it. `limit` bounds the LAST pair: the file length at the top level,
    and the enclosing pair's own end further down.
    """
    starts = [key_node.start_mark.line for key_node, _ in node.value]
    spans: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else limit
        while end > start + 1 and _is_blank_or_comment(lines[end - 1]):
            end -= 1
        spans.append((start, end))
    return spans


def _index_of(node: yaml.nodes.MappingNode, part: str) -> int | None:
    """Index of the LAST pair whose key matches, mirroring PyYAML's own reading
    of a duplicate key."""
    found = None
    for index, (key_node, _) in enumerate(node.value):
        if str(key_node.value) == part:
            found = index
    return found


@dataclass(frozen=True)
class _Target:
    """Where a key lives, or where a new one would be written."""

    found: bool
    indent: int
    start_line: int
    end_line: int
    limit: int


def _locate(text: str, path: list[str]) -> _Target:
    """Find the addressed key's line span, or the point a new one goes."""
    lines = text.splitlines(keepends=True)
    root = _compose(text)
    if root is None:
        if len(path) > 1:
            raise PatchError(
                f"'{path[0]}' does not exist and the file is empty. Nothing is created along "
                f"the way, so set '{path[0]}' with the whole subtree instead."
            )
        return _Target(found=False, indent=0, start_line=len(lines), end_line=len(lines), limit=len(lines))
    if not isinstance(root, yaml.nodes.MappingNode):
        raise PatchError("configuration.yaml's top level is not a mapping of keys, so no key can be addressed in it.")

    node: yaml.nodes.Node = root
    limit = len(lines)
    for depth, part in enumerate(path):
        walked = ".".join(path[: depth + 1])
        parent = ".".join(path[:depth])
        # At the top level there is no enclosing key to rewrite instead, so the
        # only way forward is the whole-file tool.
        instead = (
            f"Set '{parent}' with the whole subtree instead."
            if parent
            else "Use set_yaml_config to write the file instead."
        )
        named = f"'{parent}'" if parent else "the file's top level"
        if not isinstance(node, yaml.nodes.MappingNode) or not node.value:
            raise PatchError(
                f"{named} does not hold a mapping of keys, so '{part}' cannot be addressed "
                f"inside it. {instead}"
            )
        if node.flow_style:
            raise PatchError(
                f"{named} is written in flow style ({{...}}) on one line, which has no "
                f"per-key span to edit. {instead}"
            )
        spans = _child_spans(node, lines, limit)
        index = _index_of(node, part)
        last = depth == len(path) - 1
        if index is None:
            if not last:
                raise PatchError(
                    f"'{walked}' does not exist. Nothing is created along the way, so set the "
                    f"nearest existing parent with the whole subtree instead."
                )
            insert_at = spans[-1][1]
            return _Target(
                found=False,
                indent=node.value[0][0].start_mark.column,
                start_line=insert_at,
                end_line=insert_at,
                limit=limit,
            )
        key_node, value_node = node.value[index]
        start, end = spans[index]
        if last:
            return _Target(
                found=True, indent=key_node.start_mark.column,
                start_line=start, end_line=end, limit=limit,
            )
        node = value_node
        limit = end
    raise PatchError("key is empty.")  # unreachable: split_key refuses an empty path


def _removal_span(lines: list[str], target: _Target) -> tuple[int, int]:
    """Widen a key's span to the lines that go with it when it is DELETED.

    Upward over a comment block written directly above the key, which is the same
    "a comment belongs to the key below it" model _child_spans already uses when
    it trims one off the PREVIOUS key's span; it stops at a blank line, so a
    section header separated by one is left alone. Downward over the blank lines
    that followed, so a removal does not leave a hole. An edit deliberately does
    neither: the key is staying, and so is what a human wrote about it.
    """
    start, end = target.start_line, target.end_line
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    while end < target.limit and not lines[end].strip():
        end += 1
    return start, end


def _dedent(rows: list[str]) -> list[str]:
    """Strip the common leading indent, so content copied out of a file at any
    depth renders at the depth it is being written to."""
    widths = [len(row) - len(row.lstrip(" ")) for row in rows if row.strip()]
    cut = min(widths) if widths else 0
    return [row[cut:] if row.strip() else "" for row in rows]


def _render(key: str, content: str, indent: int, value: Any) -> str:
    """Render `key: value` at the given indent, block form unless it fits inline.

    A dict or list always goes to block form, so a one-line "- a" (a sequence) is
    never emitted as "key: - a", which is not valid YAML.
    """
    rows = _dedent(content.strip("\n").split("\n"))
    pad = " " * indent
    if len(rows) == 1 and not isinstance(value, (dict, list)):
        return f"{pad}{key}: {rows[0]}\n"
    inner = " " * (indent + 2)
    out = [f"{pad}{key}:"]
    out.extend(f"{inner}{row}" if row else "" for row in rows)
    return "\n".join(out) + "\n"


def _splice(text: str, start: int, end: int, replacement: str | None) -> str:
    """Replace lines [start, end) with replacement text (None removes them)."""
    lines = text.splitlines(keepends=True)
    before = lines[:start]
    after = lines[end:]
    if replacement is None:
        return "".join(before) + "".join(after)
    # Only possible at EOF, where the replacement would otherwise be appended to
    # the last line rather than starting on its own.
    if before and not before[-1].endswith("\n"):
        before[-1] += "\n"
    return "".join(before) + replacement + "".join(after)


def _source_value(text: str, target: _Target) -> str:
    """The addressed key's current VALUE as it is written in the file.

    The value rather than the whole pair, so the before and after sides of a diff
    describe the same thing; taken from the source rather than re-dumped, so the
    operator's own comments are on the side they are reading.
    """
    rows = [row.rstrip("\n") for row in text.splitlines()[target.start_line : target.end_line]]
    if not rows:
        return ""
    head = rows[0].split(":", 1)
    inline = head[1].strip() if len(head) > 1 else ""
    if inline and not inline.startswith("#"):
        return "\n".join([inline, *_dedent(rows[1:])]).strip("\n")
    return "\n".join(_dedent(rows[1:])).strip("\n")


def _verify(old_text: str, new_text: str, path: list[str], value: Any) -> None:
    """Refuse a splice whose result is not exactly the intended document.

    Everything outside the addressed path must be unchanged and the path itself
    must hold the intended value, so a span bug is a refusal rather than a
    corrupted configuration.yaml.
    """
    try:
        new_doc = load_tagged_lenient(new_text)
    except YamlParseError as err:
        raise PatchError(f"the patched file would not be valid YAML: {err}") from err
    try:
        old_doc = load_tagged_lenient(old_text)
    except YamlParseError:
        old_doc = None
    expected = copy.deepcopy(old_doc) if isinstance(old_doc, dict) else {}
    node = expected
    for part in path[:-1]:
        branch = node.get(part)
        if not isinstance(branch, dict):
            raise PatchError(f"'{'.'.join(path)}' could not be resolved in the parsed file.")
        node = branch
    if value is _MISSING:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    if (new_doc if new_doc is not None else {}) != expected:
        raise PatchError(
            "Refused: the patched file did not come out as intended, so nothing was written. "
            "This means the change could not be spliced in cleanly; read the file with "
            "get_yaml_config and write it whole with set_yaml_config instead."
        )


def parse_value(content: Any) -> Any:
    """Parse the YAML a caller supplied for a key's new value."""
    if not isinstance(content, str) or not content.strip():
        raise PatchError(
            "content must be a non-empty YAML string holding the key's new value. "
            "To delete the key instead, pass op 'remove'."
        )
    try:
        return load_tagged_lenient(content)
    except YamlParseError as err:
        raise PatchError(f"content is not valid YAML: {err}") from err


def apply_patch(text: str, key: Any, op: str, content: Any, value: Any) -> PatchResult:
    """Splice one key into text and return the verified result.

    `value` is the caller's content already through parse_value, so it is parsed
    once rather than here and again in the guard that inspects it. Ignored when
    op is 'remove'.
    """
    path = split_key(key)
    target = _locate(text, path)
    before = _source_value(text, target) if target.found else None
    if op == "remove":
        if not target.found:
            raise PatchError(f"'{key}' is not present in the file, so there is nothing to remove.")
        start, end = _removal_span(text.splitlines(keepends=True), target)
        new_text = _splice(text, start, end, None)
        _verify(text, new_text, path, _MISSING)
        return PatchResult(new_text, before)
    new_text = _splice(
        text, target.start_line, target.end_line, _render(path[-1], content, target.indent, value)
    )
    _verify(text, new_text, path, value)
    return PatchResult(new_text, before)
