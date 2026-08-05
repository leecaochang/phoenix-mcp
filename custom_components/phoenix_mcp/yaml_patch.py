"""Key- and index-addressed splicing for a YAML document.

Replaces, adds, or removes ONE addressed value inside a YAML file while leaving
every other byte exactly as it was. An address is either a dotted `key` of
mapping keys or a `path` list mixing mapping keys and 0-based list indexes; the
list form is what reaches an entry in a file whose top level is a sequence,
which most !include targets are. Pure functions, no hass, following esphome_yaml.py
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
build the tree (the resolve_json_path rule); and a flow-style collection ({...}
or [...] on one line) has no per-child line span to address, so anything inside
one is refused rather than guessed at. A negative index is refused for the same
reason resolve_json_path refuses one: Python would read it as counting from the
end, so a -1 computed off a stale read silently edits the wrong entry.

Every patch is VERIFIED before it is returned: the spliced text is re-parsed and
compared against the old document with only the addressed path changed. A span
bug therefore surfaces as a refusal rather than as a quietly mangled
configuration.yaml, which is the same self-check discipline yaml_includes applies
to its own splices. It is also what makes index addressing safe without a
deletion guard: that an entry the patch did not address survived is proved on
every write rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import copy

import yaml

from .yaml_includes import PhoenixLenientLoader, YamlParseError, load_tagged_lenient

PATCH_OPS = ("set", "remove")

# One step of an address: a mapping key or a list index.
Segment = str | int

_MISSING = object()


class PatchError(Exception):
    """A patch that cannot be applied. The message is shown to the caller."""


@dataclass(frozen=True)
class PatchResult:
    """A verified patch: the new file text and the key's previous source text."""

    text: str
    before: str | None


def split_key(key: Any) -> list[Segment]:
    """Split a dotted key path into mapping keys, refusing an empty segment.

    Keys containing a literal dot cannot be addressed, which is the same limit
    get_yaml_config's `key` argument already has; both stop at the mapping whose
    keys are entity ids (homeassistant.customize) and edit it whole.

    Mapping keys only. A list index is not expressible here on purpose: a
    numeric segment would be ambiguous against a mapping key that is literally
    "0", which HA configurations do contain. Indexes go through `path`.
    """
    if not isinstance(key, str) or not key.strip():
        raise PatchError("key must be a dotted path of mapping keys, e.g. 'recorder.include'.")
    parts = [part.strip() for part in key.split(".")]
    if any(not part for part in parts):
        raise PatchError(f"key '{key}' has an empty path segment.")
    return list(parts)


def normalize_path(key: Any, path: Any) -> list[Segment]:
    """One path from either argument form: dotted `key` or a `path` list.

    `path` is the list form patch_dashboard already uses, mixing mapping keys
    and list indexes, and it exists here because a file loaded at an !include
    target is routinely a top-level LIST (templates.yaml, sensors.yaml) whose
    entries a dotted key cannot reach at all.

    A negative index is refused rather than honoured. Python would accept it,
    and a -1 computed off a stale read silently edits the wrong end of a list,
    which is the resolve_json_path rule in a second place. `True` is rejected
    with the other non-integers: bool is an int subclass in Python, so an
    unchecked isinstance would read it as index 1.
    """
    if key is not None and path is not None:
        raise PatchError("Pass either key or path, not both.")
    if path is None:
        return split_key(key)
    if not isinstance(path, list) or not path:
        raise PatchError(
            "path must be a non-empty list of mapping keys and list indexes, "
            "e.g. [0, 'binary_sensor', 0, 'state']."
        )
    segments: list[Segment] = []
    for segment in path:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise PatchError(
                f"path segment {segment!r} is neither a mapping key (string) nor a "
                "list index (whole number)."
            )
        if isinstance(segment, str):
            if not segment.strip():
                raise PatchError("path has an empty mapping key.")
            segments.append(segment.strip())
        else:
            if segment < 0:
                raise PatchError(
                    f"path index {segment} is negative. Count list entries from 0; a "
                    "negative index computed from a stale read edits the wrong end."
                )
            segments.append(segment)
    return segments


def describe_path(path: list[Segment]) -> str:
    """A path rendered for a message, e.g. [0]. binary_sensor as '[0].binary_sensor'."""
    out = ""
    for segment in path:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            out = f"{out}.{segment}" if out else segment
    return out or "the file's top level"


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError as err:
        raise PatchError(f"configuration.yaml is not valid YAML, so no key can be located in it: {err}") from err


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _child_start(child: Any, lines: list[str]) -> int:
    """Start line of one child of a block collection.

    A mapping pair starts at its key. A sequence item starts at its own first
    token, which sits on the dash line in the ordinary `- key: value` form but
    on the NEXT line when the dash stands alone, so that case walks back up to
    the dash; without it the dash would be left behind by an edit and become a
    second, empty entry. Same rule as yaml_includes._item_start_line.
    """
    if isinstance(child, tuple):
        return child[0].start_mark.line
    start = child.start_mark.line
    if start > 0 and lines[start - 1].rstrip() == "-":
        start -= 1
    return start


def _child_spans(node: yaml.nodes.Node, lines: list[str], limit: int) -> list[tuple[int, int]]:
    """Line span of each child in a block mapping or sequence, blanks trimmed.

    A child runs from its own first line to the start of the next child's, so a
    comment above one belongs to it and survives an edit to the child before it.
    `limit` bounds the LAST child: the file length at the top level, and the
    enclosing child's own end further down.

    Mapping pairs and sequence items share this because they share the shape
    PyYAML gives them, an ordered list of nodes carrying source marks; only
    where a child's first mark sits differs, which is _child_start's job.
    """
    starts = [_child_start(child, lines) for child in node.value]
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
    """Where an addressed value lives, or where a new one would be written."""

    found: bool
    indent: int
    start_line: int
    end_line: int
    limit: int
    in_sequence: bool = False
    # What sits before the key on its own line when that is not just spaces,
    # i.e. "- " for the FIRST pair of a mapping written inline after a dash
    # ("- a: 1"). Replacing that line without re-emitting the dash deletes the
    # list entry itself, which _verify catches as a mismatch rather than a
    # corrupted file; this is what makes the edit land instead of being refused.
    prefix: str = ""


def _locate(text: str, path: list[Segment]) -> _Target:
    """Find the addressed value's line span, or the point a new one goes."""
    lines = text.splitlines(keepends=True)
    root = _compose(text)
    if root is None:
        if len(path) > 1 or not isinstance(path[0], str):
            raise PatchError(
                f"'{describe_path(path[:1])}' does not exist and the file is empty. Nothing "
                "is created along the way, so write the file with set_yaml_config instead."
            )
        return _Target(found=False, indent=0, start_line=len(lines), end_line=len(lines), limit=len(lines))
    if not isinstance(root, (yaml.nodes.MappingNode, yaml.nodes.SequenceNode)):
        raise PatchError(
            "the file's top level is neither a mapping of keys nor a list of entries, "
            "so nothing can be addressed in it."
        )

    node: yaml.nodes.Node = root
    limit = len(lines)
    for depth, part in enumerate(path):
        walked = describe_path(path[: depth + 1])
        parent = describe_path(path[:depth]) if depth else ""
        # At the top level there is no enclosing value to rewrite instead, so the
        # only way forward is the whole-file tool.
        instead = (
            f"Set '{parent}' with the whole subtree instead."
            if parent
            else "Use set_yaml_config to write the file instead."
        )
        named = f"'{parent}'" if parent else "the file's top level"
        wants_index = isinstance(part, int)
        expected = "a list of entries" if wants_index else "a mapping of keys"
        holder = yaml.nodes.SequenceNode if wants_index else yaml.nodes.MappingNode
        if not isinstance(node, holder) or not node.value:
            raise PatchError(
                f"{named} does not hold {expected}, so '{walked}' cannot be addressed "
                f"inside it. {instead}"
            )
        if node.flow_style:
            shape = "[...]" if wants_index else "{...}"
            raise PatchError(
                f"{named} is written in flow style ({shape}) on one line, which has no "
                f"per-entry span to edit. {instead}"
            )
        spans = _child_spans(node, lines, limit)
        last = depth == len(path) - 1
        if wants_index:
            assert isinstance(part, int)
            if part > len(node.value):
                raise PatchError(
                    f"{named} has {len(node.value)} entries, so index {part} is out of "
                    f"range. Entries are counted from 0."
                )
            # An index one past the end APPENDS, which is the same affordance a
            # missing mapping key already has: set creates it. Refusing would
            # leave a whole-file rewrite as the only way to add one entry.
            if part == len(node.value):
                if not last:
                    raise PatchError(
                        f"'{walked}' does not exist. Nothing is created along the way, so "
                        "set the nearest existing parent with the whole subtree instead."
                    )
                append_at = spans[-1][1]
                return _Target(
                    found=False, indent=_child_indent(node.value[0], lines),
                    start_line=append_at, end_line=append_at, limit=limit,
                    in_sequence=True,
                )
            item = node.value[part]
            start, end = spans[part]
            if last:
                return _Target(
                    found=True, indent=_child_indent(item, lines),
                    start_line=start, end_line=end, limit=limit, in_sequence=True,
                )
            node = item
            limit = end
            continue
        assert isinstance(node, yaml.nodes.MappingNode)  # holder check above
        assert isinstance(part, str)  # wants_index is False here
        index = _index_of(node, part)
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
                prefix=_line_prefix(lines, start, key_node.start_mark.column),
            )
        node = value_node
        limit = end
    raise PatchError("path is empty.")  # unreachable: normalize_path refuses an empty path


def _line_prefix(lines: list[str], line: int, column: int) -> str:
    """The non-blank text before a key on its own line, or "" when it is indent.

    Only ever "- ", from a mapping written inline after a sequence dash.
    """
    row = lines[line] if line < len(lines) else ""
    head = row[:column]
    return head if head.strip() else ""


def _child_indent(item: yaml.nodes.Node, lines: list[str]) -> int:
    """Column of a sequence item's own dash, which is where a replacement goes.

    The item node's column points at its first token, one past the dash in the
    `- key: value` form and at column 0 of the next line when the dash stands
    alone, so neither is the dash itself; the dash line is found the same way
    _child_start finds it and its indent read off the text.
    """
    line = _child_start(item, lines)
    row = lines[line] if line < len(lines) else ""
    return len(row) - len(row.lstrip(" "))


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


def _render(key: Segment, content: str, indent: int, value: Any, prefix: str = "") -> str:
    """Render the addressed child at the given indent, block form unless inline fits.

    A dict or list always goes to block form, so a one-line "- a" (a sequence) is
    never emitted as "key: - a", which is not valid YAML.

    A LIST INDEX renders the entry itself under a dash rather than a `key:`
    pair, and the index is not written anywhere: position IS the address, so an
    entry carries no name to restate.
    """
    rows = _dedent(content.strip("\n").split("\n"))
    pad = prefix or " " * indent
    if isinstance(key, int):
        inner = " " * (indent + 2)
        out = [f"{pad}- {rows[0]}" if rows[0] else f"{pad}-"]
        out.extend(f"{inner}{row}" if row else "" for row in rows[1:])
        return "\n".join(out) + "\n"
    if len(rows) == 1 and not isinstance(value, (dict, list)):
        return f"{pad}{key}: {rows[0]}\n"
    inner = " " * (indent + 2)
    if _is_block_header(rows[0]) and isinstance(value, str):
        # `state: >` with the body under it, which is how a multi-line template
        # is written by hand and what a read of one hands back. Emitting the
        # indicator on its own line below the key parses to the same string but
        # reformats every template it touches, and this tool's whole promise is
        # that it does not reformat what it is not changing.
        out = [f"{pad}{key}: {rows[0]}"]
        out.extend(f"{inner}{row}" if row else "" for row in rows[1:])
        return "\n".join(out) + "\n"
    out = [f"{pad}{key}:"]
    out.extend(f"{inner}{row}" if row else "" for row in rows)
    return "\n".join(out) + "\n"


def _is_block_header(row: str) -> bool:
    """True for a block scalar indicator line: >, |, and their chomp/keep forms."""
    stripped = row.strip()
    return bool(stripped) and stripped[0] in "|>" and stripped[1:] in ("", "-", "+")


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
    if target.in_sequence:
        # The entry, not the pair: strip its dash and keep the rest aligned, so
        # the before side is the same shape the caller sends as content.
        stripped = rows[0].lstrip()
        rows = [
            rows[0].replace("-", " ", 1) if stripped == "-" else rows[0].replace("- ", "  ", 1),
            *rows[1:],
        ]
        return "\n".join(_dedent(rows)).strip("\n")
    head = rows[0].split(":", 1)
    inline = head[1].strip() if len(head) > 1 else ""
    if inline and not inline.startswith("#"):
        return "\n".join([inline, *_dedent(rows[1:])]).strip("\n")
    return "\n".join(_dedent(rows[1:])).strip("\n")


def _verify(old_text: str, new_text: str, path: list[Segment], value: Any) -> None:
    """Refuse a splice whose result is not exactly the intended document.

    Everything outside the addressed path must be unchanged and the path itself
    must hold the intended value, so a span bug is a refusal rather than a
    corrupted configuration.yaml. This is the check that makes index addressing
    safe without a deletion guard: an entry the patch did not address surviving
    byte-for-byte is not assumed, it is proved on every write.
    """
    try:
        new_doc = load_tagged_lenient(new_text)
    except YamlParseError as err:
        raise PatchError(f"the patched file would not be valid YAML: {err}") from err
    try:
        old_doc = load_tagged_lenient(old_text)
    except YamlParseError:
        old_doc = None
    empty: Any = [] if isinstance(path[0], int) else {}
    expected: Any = copy.deepcopy(old_doc) if isinstance(old_doc, (dict, list)) else empty
    unresolved = PatchError(
        f"'{describe_path(path)}' could not be resolved in the parsed file.")
    node: Any = expected
    for part in path[:-1]:
        if isinstance(part, int):
            if not isinstance(node, list) or not 0 <= part < len(node):
                raise unresolved
            node = node[part]
        else:
            if not isinstance(node, dict) or part not in node:
                raise unresolved
            node = node[part]
        if not isinstance(node, (dict, list)):
            raise unresolved
    leaf = path[-1]
    if isinstance(leaf, int):
        # len(node) is the append slot the locator allows; anything past it was
        # already refused there.
        if not isinstance(node, list) or not 0 <= leaf <= len(node):
            raise unresolved
        if value is _MISSING:
            del node[leaf]
        elif leaf == len(node):
            node.append(value)
        else:
            node[leaf] = value
    else:
        if not isinstance(node, dict):
            raise unresolved
        if value is _MISSING:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    if (new_doc if new_doc is not None else empty) != expected:
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


def apply_patch(
    text: str, key: Any, op: str, content: Any, value: Any, path_arg: Any = None
) -> PatchResult:
    """Splice one addressed value into text and return the verified result.

    `value` is the caller's content already through parse_value, so it is parsed
    once rather than here and again in the guard that inspects it. Ignored when
    op is 'remove'.
    """
    path = normalize_path(key, path_arg)
    target = _locate(text, path)
    before = _source_value(text, target) if target.found else None
    if op == "remove":
        if not target.found:
            raise PatchError(
                f"'{describe_path(path)}' is not present in the file, so there is "
                "nothing to remove."
            )
        start, end = _removal_span(text.splitlines(keepends=True), target)
        new_text = _splice(text, start, end, None)
        _verify(text, new_text, path, _MISSING)
        return PatchResult(new_text, before)
    new_text = _splice(
        text, target.start_line, target.end_line,
        _render(path[-1], content, target.indent, value, target.prefix),
    )
    _verify(text, new_text, path, value)
    return PatchResult(new_text, before)
