"""Include-graph-aware YAML reading and surgical editing for Phoenix MCP authoring tools.

Home Assistant's own YAML loader resolves !include directives eagerly (inlining
the referenced file's content) and resolves !secret to plaintext, so any
load-then-dump edit of a split configuration flattens the user's include layout
and can leak secret values into the written file. This module gives the
automation/script/scene authoring executors a safe alternative:

- A Phoenix-owned PyYAML SafeLoader/SafeDumper pair that keeps the HA custom tags
  (!include family, !secret, !env_var, !input) as opaque TaggedValue scalars.
  Nothing is resolved, so nothing can be flattened or leaked. The constructors
  and representers are registered ONLY on these subclasses, never on HA's
  shared loader/dumper classes.
- A layout resolver that parses configuration.yaml to find where a domain key
  (automation:, script:, scene:, including labeled keys like "automation manual")
  actually points, following include directives with realpath containment
  checks against the config directory.
- A locator that walks the include graph to the physical leaf file containing
  a given entry id, using compose-level start/end marks for the exact line span.
- A reverse mount scan (scan_mount_points) answering which top-level
  configuration.yaml keys can reach a given file, which is how the raw-YAML
  write tools decide whether a file may be written at all: an include target
  inherits the trust of the key it is LOADED at, not of its own filename.
- Splice-based edit/delete that rewrites ONLY the located entry's lines,
  preserving every untouched byte (comments, formatting, other entries), plus
  flavor-aware create routing. Before any write, the spliced text is re-parsed
  and verified entry-by-entry; any mismatch aborts without writing, so a span
  bug can only ever produce a refusal, never a corrupted file.

Callers (mcp_view executors) run the perform_* / read_entry wrappers in an
executor job under the per-domain asyncio lock. When configuration.yaml is
missing or unparseable the wrappers report fallback=True and the caller runs
the legacy hardcoded-file path unchanged.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

from homeassistant.util.file import write_utf8_file_atomic

CONFIGURATION_YAML = "configuration.yaml"
SECRETS_YAML = "secrets.yaml"
MAX_INCLUDE_DEPTH = 8

INCLUDE_TAGS = frozenset({
    "!include",
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_named",
    "!include_dir_merge_named",
})
OPAQUE_TAGS = INCLUDE_TAGS | frozenset({"!secret", "!env_var", "!input"})

# Hardcoded default files, used by read_entry's fallback when configuration.yaml
# cannot be parsed (mirrors the legacy mcp_view constants).
DEFAULT_FILES = {
    "automation": "automations.yaml",
    "script": "scripts.yaml",
    "scene": "scenes.yaml",
}

# automations.yaml / scenes.yaml are lists of dicts keyed by an "id" field;
# scripts.yaml is a mapping keyed by script id.
_DOMAIN_SHAPES = {"automation": "list", "scene": "list", "script": "named"}

# The leaf each domain uses in HA's default layout, and the only file the read
# side used to open. read_all_entries falls back to it when configuration.yaml
# itself cannot be parsed, matching OpResult.fallback on the write side.
_LEGACY_LEAF = {
    "automation": "automations.yaml",
    "script": "scripts.yaml",
    "scene": "scenes.yaml",
}

_LIST_FLAVORS = frozenset({"!include", "!include_dir_list", "!include_dir_merge_list"})
_NAMED_FLAVORS = frozenset({"!include", "!include_dir_named", "!include_dir_merge_named"})

# Refusal / error message templates. Tests pin these; mcp_view may override
# per-domain (the scene executor substitutes its oracle-safe wording).
MSG_INCLUDE_REFUSAL = (
    "{name} uses !include directives. Phoenix MCP cannot safely edit it "
    "without destroying the include structure."
)
MSG_DUPLICATE = (
    "{domain} id '{entry_id}' appears in more than one included file ({files}). "
    "Resolve the duplicate before editing through Phoenix MCP."
)
MSG_AMBIGUOUS = (
    "{domain} '{entry_id}' was not found in the {domain} include files. It may be "
    "defined in a package or inline in configuration.yaml, which Phoenix MCP does not edit."
)
MSG_CREATE_NO_ROUTE = (
    "configuration.yaml does not route {domain} to an editable YAML file, "
    "so Phoenix MCP cannot determine where to create it."
)
MSG_CREATE_MULTI = (
    "configuration.yaml routes {domain} to multiple include targets, "
    "so Phoenix MCP cannot choose where to create it."
)
MSG_SHAPE_MISMATCH = (
    "configuration.yaml routes {domain} through {tag}, "
    "which Phoenix MCP cannot edit for this domain."
)
MSG_OUTSIDE_CONFIG = (
    "configuration.yaml routes {domain} outside the Home Assistant "
    "configuration directory."
)
MSG_NOT_FOUND = {
    "automation": "No automation found with id '{entry_id}'.",
    "script": "No script found with id '{entry_id}'.",
    "scene": "No scene found with id '{entry_id}'.",
}


@dataclass(frozen=True)
class TaggedValue:
    """An unresolved HA custom-tag scalar, e.g. TaggedValue("!secret", "db_pass")."""

    tag: str
    value: str

    def __str__(self) -> str:
        return f"{self.tag} {self.value}".strip()


class PhoenixSafeLoader(yaml.SafeLoader):
    """SafeLoader keeping HA custom tags as opaque TaggedValue scalars."""


class PhoenixSafeDumper(yaml.SafeDumper):
    """SafeDumper re-emitting TaggedValue scalars with their original tag."""

    def choose_scalar_style(self) -> Any:
        # PyYAML only allows plain style for implicitly-tagged scalars, so an
        # explicit tag would emit as "!secret 'db_pass'". Hand-written HA config
        # uses the unquoted form; allow plain for Phoenix MCP's tags when the scalar
        # analysis permits it.
        style = super().choose_scalar_style()
        if (
            style == "'"
            and self.event.tag in OPAQUE_TAGS
            and not (self.simple_key_context and (self.analysis.empty or self.analysis.multiline))
            and (
                (self.flow_level and self.analysis.allow_flow_plain)
                or (not self.flow_level and self.analysis.allow_block_plain)
            )
        ):
            return ""
        return style


def _make_tag_constructor(tag: str) -> Callable[[yaml.SafeLoader, yaml.nodes.Node], TaggedValue]:
    def _construct(loader: yaml.SafeLoader, node: yaml.nodes.Node) -> TaggedValue:
        return TaggedValue(tag, loader.construct_scalar(node))

    return _construct


for _tag in sorted(OPAQUE_TAGS):
    PhoenixSafeLoader.add_constructor(_tag, _make_tag_constructor(_tag))

PhoenixSafeDumper.add_representer(
    TaggedValue,
    lambda dumper, data: dumper.represent_scalar(data.tag, data.value),
)


class PhoenixLenientLoader(PhoenixSafeLoader):
    """PhoenixSafeLoader that also tolerates local tags it has never heard of.

    The strict loader knows only HA's own tag set, so a third-party integration's
    custom tag raises. That is right for the authoring paths (an unknown tag in an
    automation body is a reason to refuse the edit), but wrong for whole-file reads
    and for the protected-key comparison: one unrecognized tag anywhere in
    configuration.yaml would make the file permanently unreadable and unwritable.
    Unknown tags round-trip as TaggedValue like the known ones.
    """


def _construct_unknown_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.nodes.Node) -> Any:
    if isinstance(node, yaml.nodes.ScalarNode):
        return TaggedValue(f"!{tag_suffix}", loader.construct_scalar(node))
    # Non-scalar custom tags (a tagged mapping or sequence) keep their structure;
    # the tag itself is dropped, which is fine for reading and comparison.
    if isinstance(node, yaml.nodes.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


PhoenixLenientLoader.add_multi_constructor("!", _construct_unknown_tag)


def load_tagged(path: str) -> Any:
    """Parse a YAML file with the opaque-tag loader. Returns None for empty files."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.load(f.read(), Loader=PhoenixSafeLoader)


class YamlParseError(Exception):
    """Raised when text handed to load_tagged_lenient is not valid YAML."""


def load_tagged_lenient(text: str) -> Any:
    """Parse YAML text keeping known AND unknown custom tags opaque.

    Raises YamlParseError on a real syntax error, so callers can surface the
    message without importing PyYAML themselves.
    """
    try:
        return yaml.load(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError as err:
        raise YamlParseError(str(err)) from err


def dump_tagged(data: Any) -> str:
    """Serialize with the tag-preserving dumper, style-matching HA's yaml.dump."""
    return yaml.dump(
        data,
        Dumper=PhoenixSafeDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).replace(": null\n", ":\n")


def encode_tags(obj: Any) -> Any:
    """Recursively replace TaggedValue with its display string ("!secret name").

    Makes located configs JSON-safe for the version store and approval diffs.
    """
    if isinstance(obj, TaggedValue):
        return str(obj)
    if isinstance(obj, dict):
        return {encode_tags(k): encode_tags(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [encode_tags(v) for v in obj]
    return obj


def contains_include_tags(obj: Any) -> bool:
    """Whether an include-family TaggedValue appears anywhere in the structure."""
    if isinstance(obj, TaggedValue):
        return obj.tag in INCLUDE_TAGS
    if isinstance(obj, dict):
        return any(contains_include_tags(k) or contains_include_tags(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(contains_include_tags(v) for v in obj)
    return False


_TAG_STRING_RE = re.compile(
    r"^!(include(_dir_(merge_)?(list|named))?|secret|env_var|input)(\s|$)"
)


def contains_tag_strings(obj: Any) -> bool:
    """Whether an encode_tags()-style tag string appears anywhere in the structure.

    Used by async_restore_version to refuse re-applying a stored config whose YAML
    tags were flattened to display strings (writing them back as quoted strings
    would silently break the config).
    """
    if isinstance(obj, str):
        return bool(_TAG_STRING_RE.match(obj))
    if isinstance(obj, dict):
        return any(contains_tag_strings(k) or contains_tag_strings(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(contains_tag_strings(v) for v in obj)
    return False


# ---------------------------------------------------------------------------
# Layout resolution (configuration.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainTarget:
    """One include branch for a domain key in configuration.yaml."""

    flavor: str      # the include tag, e.g. "!include_dir_merge_list"
    path: str        # realpath of the included file or directory
    config_key: str  # the configuration.yaml key that carried it


@dataclass(frozen=True)
class DomainLayout:
    """Where configuration.yaml routes a domain's YAML config."""

    domain: str
    config_dir: str
    targets: tuple[DomainTarget, ...]
    has_inline: bool
    has_packages: bool
    error: str | None = None


def _resolve_include_path(containing_file: str, value: str, config_dir: str) -> str | None:
    """Resolve an include target relative to its containing file, contained to config_dir."""
    candidate = os.path.realpath(os.path.join(os.path.dirname(containing_file), value))
    base = os.path.realpath(config_dir)
    if candidate == base or candidate.startswith(base + os.sep):
        return candidate
    return None


def resolve_domain_layout(config_dir: str, domain: str) -> DomainLayout | None:
    """Parse configuration.yaml and resolve the include branches for a domain.

    Returns None when configuration.yaml is missing or unparseable (caller
    falls back to the legacy hardcoded-file behavior). A structurally
    unresolvable layout returns a DomainLayout with error set.
    """
    config_path = os.path.join(config_dir, CONFIGURATION_YAML)
    if not os.path.isfile(config_path):
        return None
    try:
        root = load_tagged(config_path)
    except Exception:  # noqa: BLE001 - unparseable root file, use legacy path
        return None
    if not isinstance(root, dict):
        return None

    has_inline = False
    has_packages = False
    targets: list[DomainTarget] = []
    error: str | None = None

    ha_conf = root.get("homeassistant")
    if isinstance(ha_conf, dict):
        has_packages = "packages" in ha_conf
    elif isinstance(ha_conf, TaggedValue):
        # The core config is itself included; Phoenix MCP cannot see whether it defines
        # packages, so assume it might (fail-closed on not-found lookups).
        has_packages = True

    shape = _DOMAIN_SHAPES[domain]
    allowed_flavors = _LIST_FLAVORS if shape == "list" else _NAMED_FLAVORS
    for key, value in root.items():
        if not isinstance(key, str):
            continue
        if key != domain and not key.startswith(domain + " "):
            continue
        if isinstance(value, TaggedValue) and value.tag in INCLUDE_TAGS:
            if value.tag not in allowed_flavors:
                error = error or MSG_SHAPE_MISMATCH.format(domain=domain, tag=value.tag)
                continue
            resolved = _resolve_include_path(config_path, value.value, config_dir)
            if resolved is None:
                error = error or MSG_OUTSIDE_CONFIG.format(domain=domain)
                continue
            targets.append(DomainTarget(value.tag, resolved, key))
        elif isinstance(value, (list, dict)) and value:
            has_inline = True
        # None, empty containers, and plain scalars route nothing.

    return DomainLayout(
        domain=domain,
        config_dir=config_dir,
        targets=tuple(targets),
        has_inline=has_inline,
        has_packages=has_packages,
        error=error,
    )


def _iter_dir_files(directory: str) -> list[str]:
    """Enumerate a !include_dir_* directory exactly like annotatedyaml._find_files.

    Recursive os.walk (topdown), dot-prefixed dirs pruned, files sorted per
    directory (subdirectory order is filesystem order), *.yaml only, dot-files
    and secrets.yaml skipped.
    """
    found: list[str] = []
    for root_dir, dirs, files in os.walk(directory, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for basename in sorted(files):
            if basename.startswith("."):
                continue
            if not fnmatch.fnmatch(basename, "*.yaml"):
                continue
            if basename == SECRETS_YAML:
                continue
            found.append(os.path.join(root_dir, basename))
    return found


@dataclass(frozen=True)
class MountScan:
    """Which configuration.yaml top-level keys can reach a given file.

    The write side of the raw-YAML tools is bounded by WHERE a file's content
    lands in the configuration tree, not by the file's own shape: an ordinary
    looking include target mounted at `http:` puts trusted_proxies at its top
    level, and a packages file carries whole integration configs. So a caller
    deciding whether a file may be written asks this, and refuses on anything
    it cannot prove.

    `keys` are label-stripped (`automation manual` reports as `automation`),
    because the trust question is about the integration a key configures and
    HA merges the labeled variants into the same one.

    `opaque` is the half that makes fail-closed possible. A branch whose files
    cannot be read might route the target somewhere this scan never saw, so a
    caller must treat an opaque protected key exactly as it treats a reached
    one. Empty `keys` with `readable` True is a real negative (nothing routes
    this file); `readable` False means configuration.yaml itself is missing or
    unparseable and NOTHING here is known.
    """

    keys: frozenset[str]
    opaque: frozenset[str]
    readable: bool


def _load_lenient_file(path: str) -> Any:
    """Parse a file for the mount scan, tolerating unknown third-party tags.

    Deliberately the LENIENT loader, unlike the authoring paths: a vendor tag
    anywhere in an included file would otherwise make its branch opaque and
    refuse writes to unrelated files, which is the same reasoning that put the
    lenient loader behind get_yaml_config.
    """
    with open(path, "r", encoding="utf-8") as f:
        return load_tagged_lenient(f.read())


def _collect_directives(
    containing_file: str, node: Any, config_dir: str
) -> list[tuple[str, str]]:
    """Every (flavor, realpath) an include directive anywhere under `node` names.

    Walks the whole value rather than its top level, since a directive can sit
    at any depth (`http: {trusted_proxies: !include proxies.yaml}`). A target
    resolving outside the configuration directory is dropped: it cannot name a
    file the caller is allowed to write, which is jailed to config_dir already.
    """
    found: list[tuple[str, str]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, TaggedValue):
            if current.tag in INCLUDE_TAGS:
                resolved = _resolve_include_path(containing_file, current.value, config_dir)
                if resolved is not None:
                    found.append((current.tag, resolved))
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _is_within(directory: str, candidate: str) -> bool:
    """True when candidate sits inside directory (both already realpaths)."""
    return candidate.startswith(directory + os.sep)


def _reaches_target(
    directives: list[tuple[str, str]],
    target: str,
    config_dir: str,
    depth: int,
    seen: set[str],
) -> tuple[bool, bool]:
    """Walk an include branch. Returns (target is reachable, branch is opaque).

    A directory include matches by CONTAINMENT rather than by enumerating the
    files HA would load, which is the fail-closed direction twice over: it
    covers a file the caller is about to create inside that directory, and it
    covers a name HA's own `*.yaml` filter would skip. Traversal still walks
    only the files HA actually loads, since a file HA never reads cannot route
    anything.
    """
    if depth > MAX_INCLUDE_DEPTH:
        return False, True
    reachable = False
    opaque = False
    for flavor, path in directives:
        if flavor == "!include":
            if path == target:
                reachable = True
            leaves = [path]
        else:
            if _is_within(path, target):
                reachable = True
            leaves = _iter_dir_files(path)
        for leaf in leaves:
            real = os.path.realpath(leaf)
            if real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            try:
                document = _load_lenient_file(real)
            except (OSError, YamlParseError):
                opaque = True
                continue
            nested = _collect_directives(real, document, config_dir)
            if not nested:
                continue
            sub_reachable, sub_opaque = _reaches_target(
                nested, target, config_dir, depth + 1, seen)
            reachable = reachable or sub_reachable
            opaque = opaque or sub_opaque
    return reachable, opaque


def scan_mount_points(config_dir: str, target: str) -> MountScan:
    """Which top-level configuration.yaml keys route to `target`.

    Every key is walked independently, with its own `seen` set, so one file
    shared between two keys is reported under BOTH. That is the case the whole
    scan exists for: a file mounted at an ordinary domain key AND reachable
    from a protected one is not safe to write, and a first-match answer would
    report only whichever key happened to come first in the document.
    """
    config_path = os.path.join(config_dir, CONFIGURATION_YAML)
    if not os.path.isfile(config_path):
        return MountScan(frozenset(), frozenset(), readable=False)
    try:
        root = _load_lenient_file(config_path)
    except (OSError, YamlParseError):
        return MountScan(frozenset(), frozenset(), readable=False)
    if not isinstance(root, dict):
        return MountScan(frozenset(), frozenset(), readable=False)

    resolved_target = os.path.realpath(target)
    reached: set[str] = set()
    opaque: set[str] = set()
    for key, value in root.items():
        if not isinstance(key, str):
            continue
        directives = _collect_directives(config_path, value, config_dir)
        if not directives:
            continue
        is_reachable, is_opaque = _reaches_target(
            directives, resolved_target, config_dir, 1, set())
        top_key = key.split(" ", 1)[0]
        if is_reachable:
            reached.add(top_key)
        if is_opaque:
            opaque.add(top_key)
    return MountScan(frozenset(reached), frozenset(opaque), readable=True)


@dataclass(frozen=True)
class DomainEntries:
    """Every entry a domain defines, and every branch that could not be read.

    `unreadable` is the load-bearing half. The READ side of the tool surface used
    to open one hardcoded file per domain (automations.yaml and siblings), so an
    installation routing a domain any other way read as EMPTY and its callers
    reported that nothing referenced an entity. That is a confident wrong answer
    to the one question those callers exist to ask, and no caller could tell it
    from a genuinely unused entity. Anything this cannot read is now named
    instead of silently contributing nothing.
    """

    entries: Any                # list for automation/scene, dict for script
    unreadable: tuple[str, ...]


def _load_branch(path: str) -> Any:
    """Parse one leaf file, or None when it is missing or unparseable."""
    try:
        return load_tagged(path)
    except (OSError, yaml.YAMLError):
        return None


def read_all_entries(config_dir: str, domain: str) -> DomainEntries:
    """Read every entry a domain defines, following configuration.yaml's includes.

    The counterpart to locate_entry, which finds ONE entry to splice: this reads
    them all, for the reverse-reference walks that ask what references an entity.
    It has no need of spans, so it parses values rather than tracking source
    positions.

    Falls back to the legacy hardcoded leaf (automations.yaml / scripts.yaml /
    scenes.yaml) when configuration.yaml is missing or unparseable, matching what
    perform_create/edit/delete do with OpResult.fallback: a configuration.yaml
    that cannot be read must not make the default layout unreadable too.

    Packages are reported as unreadable rather than guessed at. HA merges them
    into the domain and Phoenix cannot see which ones without resolving the whole
    package tree, so a caller is told its answer is incomplete instead of being
    handed one that looks whole.
    """
    shape = _DOMAIN_SHAPES[domain]
    empty: Any = [] if shape == "list" else {}
    layout = resolve_domain_layout(config_dir, domain)
    if layout is None:
        legacy = os.path.join(config_dir, _LEGACY_LEAF[domain])
        loaded = _load_branch(legacy)
        return DomainEntries(loaded if isinstance(loaded, type(empty)) else empty, ())

    entries: Any = [] if shape == "list" else {}
    unreadable: list[str] = []

    if layout.error:
        unreadable.append(layout.error)
    if layout.has_packages:
        unreadable.append(
            f"{domain} may also be defined under homeassistant.packages, which is not read here"
        )
    if layout.has_inline:
        # Readable, unlike the write path, which refuses inline as ambiguous.
        root = _load_branch(os.path.join(config_dir, CONFIGURATION_YAML))
        for key, value in (root or {}).items():
            if not isinstance(key, str) or (key != domain and not key.startswith(domain + " ")):
                continue
            if shape == "list" and isinstance(value, list):
                entries.extend(value)
            elif shape == "named" and isinstance(value, dict):
                entries.update(value)

    for target in layout.targets:
        if target.flavor == "!include":
            loaded = _load_branch(target.path)
            if loaded is None:
                unreadable.append(f"{os.path.basename(target.path)} could not be read")
            elif shape == "list" and isinstance(loaded, list):
                entries.extend(loaded)
            elif shape == "named" and isinstance(loaded, dict):
                entries.update(loaded)
            continue
        if not os.path.isdir(target.path):
            unreadable.append(f"{target.config_key} points at a directory that does not exist")
            continue
        for leaf in _iter_dir_files(target.path):
            loaded = _load_branch(leaf)
            if loaded is None:
                unreadable.append(f"{os.path.basename(leaf)} could not be read")
                continue
            # Each flavor packages its files differently: *_dir_list gives one
            # entry per file, *_dir_merge_list a list per file, *_dir_named keys
            # by filename, *_dir_merge_named a mapping per file.
            if target.flavor == "!include_dir_list":
                entries.append(loaded)
            elif target.flavor == "!include_dir_merge_list" and isinstance(loaded, list):
                entries.extend(loaded)
            elif target.flavor == "!include_dir_named":
                entries[os.path.splitext(os.path.basename(leaf))[0]] = loaded
            elif target.flavor == "!include_dir_merge_named" and isinstance(loaded, dict):
                entries.update(loaded)

    return DomainEntries(entries, tuple(unreadable))


# ---------------------------------------------------------------------------
# Locate
# ---------------------------------------------------------------------------


class LocateError(Exception):
    """Raised when an entry cannot be uniquely located in the include graph."""

    def __init__(self, reason: str, message: str) -> None:
        # reason: not_found | duplicate | ambiguous | unresolvable | parse_error
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class LocatedEntry:
    """A uniquely located entry.

    ref_file/ref_kind/span describe where the entry is REFERENCED (the lines a
    delete removes). When the entry's body lives in a separate file reached via
    a plain !include value (e.g. "foo: !include foo.yaml"), content_file names
    it: edit rewrites content_file and leaves the reference intact; delete
    splices the reference and leaves the file orphaned on disk.
    """

    ref_file: str
    ref_kind: str            # "list_item" | "named_value" | "whole_file"
    ref_text: str            # snapshot of ref_file's text ("" for whole_file)
    start_line: int          # 0-based inclusive; -1 for whole_file
    end_line: int            # 0-based exclusive; -1 for whole_file
    key: str | None          # script id for named kinds
    config: Any              # parsed entry (TaggedValue where tags appear)
    content_file: str | None = None


def _entry_matches(domain: str, item: Any, entry_id: str) -> bool:
    if not isinstance(item, dict):
        return False
    if domain == "scene":
        # Mirror mcp_view._scene_write: scene ids compare str-coerced.
        return str(item.get("id")) == entry_id
    return item.get("id") == entry_id


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _parse_document(path: str) -> tuple[str, yaml.nodes.Node | None, PhoenixSafeLoader]:
    """Compose a file's single document node, keeping marks and the loader."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    loader = PhoenixSafeLoader(text)
    node = loader.get_single_node()
    return text, node, loader


def _item_start_line(item_node: yaml.nodes.Node, lines: list[str]) -> int:
    """Start line of a top-level list item, extended to a lone '-' line above."""
    start = item_node.start_mark.line
    if start > 0 and lines[start - 1].rstrip() == "-":
        start -= 1
    return start


class _FileScan:
    """Top-level entries of one physical YAML file, with line spans."""

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            self.text, self.node, self._loader = _parse_document(path)
        except yaml.YAMLError as exc:
            raise LocateError(
                "parse_error",
                f"{os.path.basename(path)} could not be parsed: {exc}",
            ) from exc
        self.lines = self.text.splitlines(keepends=True)

    def document_value(self) -> Any:
        if self.node is None:
            return None
        return self._loader.construct_object(self.node, deep=True)

    def sequence_entries(self) -> list[tuple[Any, int, int]]:
        """(value, start_line, end_line) for each top-level list item."""
        if not isinstance(self.node, yaml.nodes.SequenceNode):
            return []
        starts = [_item_start_line(child, self.lines) for child in self.node.value]
        spans = self._spans(starts)
        return [
            (self._loader.construct_object(child, deep=True), spans[i][0], spans[i][1])
            for i, child in enumerate(self.node.value)
        ]

    def mapping_entries(self) -> list[tuple[str, Any, int, int]]:
        """(key, value, start_line, end_line) for each top-level mapping pair."""
        if not isinstance(self.node, yaml.nodes.MappingNode):
            return []
        starts = [key_node.start_mark.line for key_node, _ in self.node.value]
        spans = self._spans(starts)
        return [
            (
                str(key_node.value),
                self._loader.construct_object(value_node, deep=True),
                spans[i][0],
                spans[i][1],
            )
            for i, (key_node, value_node) in enumerate(self.node.value)
        ]

    def _spans(self, starts: list[int]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        total = len(self.lines)
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else total
            while end > start + 1 and _is_blank_or_comment(self.lines[end - 1]):
                end -= 1
            spans.append((start, end))
        return spans


def _resolve_nested_include(
    ref: TaggedValue, containing_file: str, layout: DomainLayout
) -> str:
    """Resolve a plain !include TaggedValue found inside a leaf file."""
    resolved = _resolve_include_path(containing_file, ref.value, layout.config_dir)
    if resolved is None:
        raise LocateError(
            "unresolvable", MSG_OUTSIDE_CONFIG.format(domain=layout.domain)
        )
    return resolved


def _scan_list_file(
    layout: DomainLayout,
    entry_id: str,
    path: str,
    matches: list[LocatedEntry],
    walk_path: tuple[str, ...],
) -> None:
    """Scan one physical file expected to hold a top-level LIST of entries."""
    _guard_walk(path, walk_path, layout)
    scan = _FileScan(path)
    if scan.node is None:
        return
    if isinstance(scan.node, yaml.nodes.ScalarNode):
        doc = scan.document_value()
        if isinstance(doc, TaggedValue) and doc.tag == "!include":
            nested = _resolve_nested_include(doc, path, layout)
            _scan_list_file(layout, entry_id, nested, matches, walk_path + (os.path.realpath(path),))
        return
    for value, start, end in scan.sequence_entries():
        if isinstance(value, TaggedValue) and value.tag == "!include":
            nested = _resolve_nested_include(value, path, layout)
            nested_doc = _load_leaf_document(nested, layout, walk_path + (os.path.realpath(path),))
            if _entry_matches(layout.domain, nested_doc, entry_id):
                matches.append(LocatedEntry(
                    ref_file=path, ref_kind="list_item", ref_text=scan.text,
                    start_line=start, end_line=end, key=None,
                    config=nested_doc, content_file=nested,
                ))
            continue
        if _entry_matches(layout.domain, value, entry_id):
            matches.append(LocatedEntry(
                ref_file=path, ref_kind="list_item", ref_text=scan.text,
                start_line=start, end_line=end, key=None, config=value,
            ))


def _scan_named_file(
    layout: DomainLayout,
    entry_id: str,
    path: str,
    matches: list[LocatedEntry],
    walk_path: tuple[str, ...],
) -> None:
    """Scan one physical file expected to hold a top-level MAPPING of entries."""
    _guard_walk(path, walk_path, layout)
    scan = _FileScan(path)
    if scan.node is None:
        return
    if isinstance(scan.node, yaml.nodes.ScalarNode):
        doc = scan.document_value()
        if isinstance(doc, TaggedValue) and doc.tag == "!include":
            nested = _resolve_nested_include(doc, path, layout)
            _scan_named_file(layout, entry_id, nested, matches, walk_path + (os.path.realpath(path),))
        return
    for key, value, start, end in scan.mapping_entries():
        if key != entry_id:
            continue
        if isinstance(value, TaggedValue) and value.tag == "!include":
            nested = _resolve_nested_include(value, path, layout)
            nested_doc = _load_leaf_document(nested, layout, walk_path + (os.path.realpath(path),))
            matches.append(LocatedEntry(
                ref_file=path, ref_kind="named_value", ref_text=scan.text,
                start_line=start, end_line=end, key=key,
                config=nested_doc, content_file=nested,
            ))
            continue
        matches.append(LocatedEntry(
            ref_file=path, ref_kind="named_value", ref_text=scan.text,
            start_line=start, end_line=end, key=key, config=value,
        ))


def _load_leaf_document(
    path: str, layout: DomainLayout, walk_path: tuple[str, ...]
) -> Any:
    """Load a plain-included file's document, following whole-file include chains."""
    _guard_walk(path, walk_path, layout)
    scan = _FileScan(path)
    doc = scan.document_value()
    if isinstance(doc, TaggedValue) and doc.tag == "!include":
        nested = _resolve_nested_include(doc, path, layout)
        return _load_leaf_document(nested, layout, walk_path + (os.path.realpath(path),))
    return doc


def _guard_walk(path: str, walk_path: tuple[str, ...], layout: DomainLayout) -> None:
    domain = layout.domain
    real = os.path.realpath(path)
    if real in walk_path:
        raise LocateError(
            "unresolvable",
            f"configuration.yaml routes {domain} through an include cycle.",
        )
    if len(walk_path) >= MAX_INCLUDE_DEPTH:
        raise LocateError(
            "unresolvable",
            f"configuration.yaml routes {domain} through more than "
            f"{MAX_INCLUDE_DEPTH} levels of includes.",
        )
    # Containment must hold for the RESOLVED file, not just the path string:
    # a symlinked leaf inside an !include_dir_* tree can point outside the
    # config directory even though every enumerated path is inside it.
    base = os.path.realpath(layout.config_dir)
    if real != base and not real.startswith(base + os.sep):
        raise LocateError(
            "unresolvable", MSG_OUTSIDE_CONFIG.format(domain=domain)
        )
    if not os.path.isfile(path):
        raise LocateError(
            "parse_error",
            f"Included file {os.path.basename(path)} does not exist.",
        )


def locate_entry(layout: DomainLayout, entry_id: str) -> LocatedEntry:
    """Locate the single entry with entry_id across the domain's include graph."""
    domain = layout.domain
    shape = _DOMAIN_SHAPES[domain]
    matches: list[LocatedEntry] = []

    for target in layout.targets:
        if target.flavor == "!include":
            if not os.path.isfile(target.path):
                continue
            if shape == "list":
                _scan_list_file(layout, entry_id, target.path, matches, ())
            else:
                _scan_named_file(layout, entry_id, target.path, matches, ())
        elif target.flavor in ("!include_dir_list", "!include_dir_named"):
            for fname in _iter_dir_files(target.path):
                doc = _load_leaf_document(fname, layout, ())
                if target.flavor == "!include_dir_list":
                    if _entry_matches(domain, doc, entry_id):
                        matches.append(LocatedEntry(
                            ref_file=fname, ref_kind="whole_file", ref_text="",
                            start_line=-1, end_line=-1, key=None, config=doc,
                        ))
                else:
                    stem = os.path.splitext(os.path.basename(fname))[0]
                    if stem == entry_id:
                        matches.append(LocatedEntry(
                            ref_file=fname, ref_kind="whole_file", ref_text="",
                            start_line=-1, end_line=-1, key=stem,
                            config={} if doc is None else doc,
                        ))
        elif target.flavor == "!include_dir_merge_list":
            for fname in _iter_dir_files(target.path):
                _scan_list_file(layout, entry_id, fname, matches, ())
        elif target.flavor == "!include_dir_merge_named":
            for fname in _iter_dir_files(target.path):
                _scan_named_file(layout, entry_id, fname, matches, ())

    if len(matches) > 1:
        files = ", ".join(sorted({os.path.basename(m.ref_file) for m in matches}))
        raise LocateError(
            "duplicate",
            MSG_DUPLICATE.format(domain=domain, entry_id=entry_id, files=files),
        )
    if not matches:
        if layout.has_packages or layout.has_inline:
            raise LocateError(
                "ambiguous", MSG_AMBIGUOUS.format(domain=domain, entry_id=entry_id)
            )
        raise LocateError(
            "not_found", MSG_NOT_FOUND[domain].format(entry_id=entry_id)
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Splice + self-check
# ---------------------------------------------------------------------------


def _splice(text: str, start: int, end: int, replacement: str | None) -> str:
    """Replace lines [start, end) with replacement text (None removes them)."""
    lines = text.splitlines(keepends=True)
    before = lines[:start]
    after = lines[end:]
    # If the content preceding the splice point lost its trailing newline
    # (only possible at EOF), restore it so the replacement starts on its
    # own line.
    if replacement is not None:
        if before and not before[-1].endswith("\n"):
            before[-1] += "\n"
        return "".join(before) + replacement + "".join(after)
    return "".join(before) + "".join(after)


def _entries_of_text(domain: str, text: str) -> list[tuple[Any, Any]]:
    """Canonical (key, config) list of a file's top-level entries for self-checks."""
    doc = yaml.load(text, Loader=PhoenixSafeLoader)
    shape = _DOMAIN_SHAPES[domain]
    if doc is None:
        return []
    if shape == "list":
        if not isinstance(doc, list):
            return [(None, doc)]
        return [(None, item) for item in doc]
    if not isinstance(doc, dict):
        return [(None, doc)]
    return list(doc.items())


def _verify_splice(
    domain: str,
    old_text: str,
    new_text: str,
    entry_index: int,
    expect: Any,
) -> bool:
    """Verify a spliced file: all other entries unchanged, target as expected.

    expect is the new (key, config) tuple for an edit, or None for a delete.
    Returns False on any mismatch, including a file that no longer parses.
    """
    try:
        old_entries = _entries_of_text(domain, old_text)
        new_entries = _entries_of_text(domain, new_text)
    except yaml.YAMLError:
        return False
    expected = list(old_entries)
    if expect is None:
        del expected[entry_index]
    else:
        expected[entry_index] = expect
    return new_entries == expected


def _entry_index(text: str, start_line: int) -> int | None:
    """Index of the top-level entry whose span starts at start_line."""
    try:
        loader = PhoenixSafeLoader(text)
        node = loader.get_single_node()
    except yaml.YAMLError:
        return None
    lines = text.splitlines(keepends=True)
    if isinstance(node, yaml.nodes.SequenceNode):
        starts = [_item_start_line(child, lines) for child in node.value]
    elif isinstance(node, yaml.nodes.MappingNode):
        starts = [key_node.start_mark.line for key_node, _ in node.value]
    else:
        return None
    try:
        return starts.index(start_line)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Operation wrappers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpResult:
    """Outcome of a perform_* call. fallback=True means run the legacy path."""

    ok: bool
    fallback: bool = False
    error_kind: str | None = None  # refused | not_found | duplicate | ambiguous |
                                   # already_exists | io_error
    message: str | None = None
    before: Any = None             # tag-encoded prior entry (edit/delete)
    file: str | None = None        # leaf file written or removed

    @property
    def error_text(self) -> str:
        """The failure message, never None.

        message is None on success and on a fallback result, but the tool layer
        turns a failed op straight into _tool_error(...), and an MCP content item
        whose text is None is malformed: the agent receives a broken frame rather
        than a reason. Callers use this instead so that cannot happen.
        """
        return self.message or "The change could not be applied."


_LOCATE_KIND_MAP = {
    "not_found": "not_found",
    "duplicate": "duplicate",
    "ambiguous": "ambiguous",
    "unresolvable": "refused",
    "parse_error": "refused",
}


def _locate_error_result(exc: LocateError) -> OpResult:
    return OpResult(ok=False, error_kind=_LOCATE_KIND_MAP[exc.reason], message=exc.message)


def _layout_or_result(config_dir: str, domain: str) -> DomainLayout | OpResult:
    """Resolve the domain's include layout, or the OpResult that ends the call.

    Returns one or the other rather than a (layout, early) pair: the pair form
    guaranteed a non-None layout only via the SECOND element, which a reader (and
    a type checker) cannot see, so every caller then passed a `DomainLayout |
    None` into code that requires a layout. Single-return matches the
    `_read_body` idiom used by the view modules, and callers narrow with
    isinstance.
    """
    layout = resolve_domain_layout(config_dir, domain)
    if layout is None:
        return OpResult(ok=False, fallback=True)
    if layout.error:
        return OpResult(ok=False, error_kind="refused", message=layout.error)
    return layout


def _write_entry_files(located: LocatedEntry, domain: str, config: dict) -> OpResult:
    """Apply an edit to a located entry (splice or whole-file rewrite)."""
    encoded_before = encode_tags(located.config)
    if located.content_file is not None:
        # Body lives in its own plain-included file; rewrite it, reference intact.
        write_utf8_file_atomic(located.content_file, dump_tagged(config))
        return OpResult(ok=True, before=encoded_before, file=located.content_file)
    if located.ref_kind == "whole_file":
        write_utf8_file_atomic(located.ref_file, dump_tagged(config))
        return OpResult(ok=True, before=encoded_before, file=located.ref_file)

    # (key, config) the post-splice self-check expects to find at that index.
    # A list item has no key; a mapping entry carries its own.
    expect: tuple[str | None, dict]
    if located.ref_kind == "list_item":
        rendered = dump_tagged([config])
        expect = (None, config)
    else:
        rendered = dump_tagged({located.key: config})
        expect = (located.key, config)
    new_text = _splice(located.ref_text, located.start_line, located.end_line, rendered)
    index = _entry_index(located.ref_text, located.start_line)
    if index is None or not _verify_splice(domain, located.ref_text, new_text, index, expect):
        return OpResult(
            ok=False, error_kind="io_error",
            message="Phoenix MCP could not verify the edited file would remain intact; nothing was written.",
        )
    write_utf8_file_atomic(located.ref_file, new_text)
    return OpResult(ok=True, before=encoded_before, file=located.ref_file)


def perform_edit(config_dir: str, domain: str, entry_id: str, config: dict) -> OpResult:
    """Replace an entry's config in place, preserving the include layout."""
    layout = _layout_or_result(config_dir, domain)
    if isinstance(layout, OpResult):
        return layout
    try:
        located = locate_entry(layout, entry_id)
    except LocateError as exc:
        return _locate_error_result(exc)
    if contains_include_tags(located.config):
        return OpResult(
            ok=False, error_kind="refused",
            message=MSG_INCLUDE_REFUSAL.format(name=os.path.basename(located.ref_file)),
        )
    return _write_entry_files(located, domain, config)


def perform_delete(config_dir: str, domain: str, entry_id: str) -> OpResult:
    """Remove an entry in place, preserving the include layout."""
    layout = _layout_or_result(config_dir, domain)
    if isinstance(layout, OpResult):
        return layout
    try:
        located = locate_entry(layout, entry_id)
    except LocateError as exc:
        return _locate_error_result(exc)
    encoded_before = encode_tags(located.config)

    if located.ref_kind == "whole_file":
        os.unlink(located.ref_file)
        return OpResult(ok=True, before=encoded_before, file=located.ref_file)

    new_text = _splice(located.ref_text, located.start_line, located.end_line, None)
    index = _entry_index(located.ref_text, located.start_line)
    if index is None or not _verify_splice(domain, located.ref_text, new_text, index, None):
        return OpResult(
            ok=False, error_kind="io_error",
            message="Phoenix MCP could not verify the edited file would remain intact; nothing was written.",
        )
    write_utf8_file_atomic(located.ref_file, new_text)
    # A plain-included body file is left orphaned on disk deliberately;
    # deleting it could destroy content shared by another include.
    return OpResult(ok=True, before=encoded_before, file=located.ref_file)


_FILENAME_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


def _create_filename(entry_id: str) -> str:
    stem = _FILENAME_SAFE_RE.sub("_", entry_id.lower()).strip("_") or "entry"
    if not stem.startswith("phoenix_mcp_"):
        stem = f"phoenix_mcp_{stem}"
    return f"{stem}.yaml"


def perform_create(config_dir: str, domain: str, entry_id: str, config: dict) -> OpResult:
    """Create an entry, routing by the domain's include flavor."""
    layout = _layout_or_result(config_dir, domain)
    if isinstance(layout, OpResult):
        return layout

    # Graph-wide duplicate check for named entries (scripts): mirrors the
    # legacy "already exists" refusal. List domains mint unique ids upstream.
    # Refuse ONLY if the id already exists in the editable include files. Both
    # "not_found" and "ambiguous" (id absent from the include files, though the
    # config also has packages/inline that Phoenix MCP does not read) mean it is not a
    # duplicate Phoenix MCP can act on, so creation proceeds; a config the walker cannot
    # parse (unresolvable/parse_error) still blocks.
    if _DOMAIN_SHAPES[domain] == "named":
        exists_msg = f"A script with id '{entry_id}' already exists. Use edit_script to update it."
        try:
            locate_entry(layout, entry_id)
        except LocateError as exc:
            if exc.reason == "duplicate":
                return OpResult(ok=False, error_kind="already_exists", message=exists_msg)
            if exc.reason in ("unresolvable", "parse_error"):
                return _locate_error_result(exc)
            # not_found / ambiguous: not a duplicate in the editable files; proceed.
        else:
            return OpResult(ok=False, error_kind="already_exists", message=exists_msg)

    targets = layout.targets
    if not targets:
        return OpResult(
            ok=False, error_kind="refused",
            message=MSG_CREATE_NO_ROUTE.format(domain=domain),
        )
    if len(targets) > 1:
        plain = [t for t in targets if t.flavor == "!include"]
        if len(plain) == 1:
            target = plain[0]
        else:
            return OpResult(
                ok=False, error_kind="refused",
                message=MSG_CREATE_MULTI.format(domain=domain),
            )
    else:
        target = targets[0]

    shape = _DOMAIN_SHAPES[domain]
    if target.flavor == "!include":
        return _create_append(target.path, shape, entry_id, config)

    # Directory flavors write a new file.
    if not os.path.isdir(target.path):
        return OpResult(
            ok=False, error_kind="io_error",
            message=f"Include directory '{os.path.basename(target.path)}' does not exist.",
        )
    if target.flavor in ("!include_dir_named", "!include_dir_merge_named"):
        fname = f"{entry_id}.yaml"
    else:
        fname = _create_filename(entry_id)
    new_path = os.path.join(target.path, fname)
    if os.path.exists(new_path):
        return OpResult(
            ok=False, error_kind="io_error",
            message=f"A file named '{fname}' already exists in the include directory.",
        )
    if target.flavor == "!include_dir_list":
        content = dump_tagged(config)
    elif target.flavor == "!include_dir_merge_list":
        content = dump_tagged([config])
    elif target.flavor == "!include_dir_named":
        content = dump_tagged(config)
    else:  # !include_dir_merge_named
        content = dump_tagged({entry_id: config})
    write_utf8_file_atomic(new_path, content)
    return OpResult(ok=True, file=new_path)


def _create_append(path: str, shape: str, entry_id: str, config: dict) -> OpResult:
    """Append an entry to a plain !include file, creating the file if missing."""
    text = ""
    doc = None
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            doc = yaml.load(text, Loader=PhoenixSafeLoader)
        except (OSError, yaml.YAMLError) as exc:
            return OpResult(
                ok=False, error_kind="io_error",
                message=f"{os.path.basename(path)} could not be parsed: {exc}",
            )
        if isinstance(doc, TaggedValue):
            return OpResult(
                ok=False, error_kind="refused",
                message=MSG_INCLUDE_REFUSAL.format(name=os.path.basename(path)),
            )
        if doc is not None and not isinstance(doc, list if shape == "list" else dict):
            return OpResult(
                ok=False, error_kind="io_error",
                message=f"{os.path.basename(path)} does not contain the expected YAML structure.",
            )

    rendered = dump_tagged([config]) if shape == "list" else dump_tagged({entry_id: config})
    if isinstance(doc, (list, dict)) and len(doc) > 0:
        # Existing block entries: append after the last line, preserving every
        # prior byte (comments, formatting, other entries).
        line_count = len(text.splitlines(keepends=True))
        new_text = _splice(text, line_count, line_count, rendered)
    else:
        # No block entries to preserve: an empty/whitespace/comments-only file,
        # or a bare inline "[]" / "{}" (HA's default for an empty automations or
        # scenes file, and scripts). A text append after an inline "[]" would
        # produce invalid YAML ("[]" is a complete flow document, so a following
        # "- ..." is a second document). Keep any comment/blank lines, drop the
        # lone empty-collection token, and write the entry as a clean block doc.
        kept = [ln for ln in text.splitlines(keepends=True) if ln.strip() not in ("[]", "{}")]
        prefix = "".join(kept)
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        new_text = prefix + rendered

    # Post-write verification: the result must parse and contain the new entry,
    # so a malformed append is refused rather than written (the file is never
    # left in a state HA cannot load).
    try:
        reparsed = yaml.load(new_text, Loader=PhoenixSafeLoader)
    except yaml.YAMLError:
        reparsed = None
    if shape == "list":
        valid = isinstance(reparsed, list) and any(
            isinstance(x, dict) and x.get("id") == config.get("id") for x in reparsed)
    else:
        valid = isinstance(reparsed, dict) and entry_id in reparsed
    if not valid:
        return OpResult(
            ok=False, error_kind="io_error",
            message="Phoenix MCP could not produce a valid config file for the new entry; nothing was written.",
        )
    write_utf8_file_atomic(path, new_text)
    return OpResult(ok=True, file=path)


def read_entry(config_dir: str, domain: str, entry_id: str) -> Any:
    """Best-effort read of an entry's current config, tag-encoded. None if absent.

    Used by _resource_exists and the approval diff builders. Falls back to the
    hardcoded default file (parsed with the tag-tolerant loader) ONLY when
    configuration.yaml is missing or unparseable, matching _layout_or_result.
    A layout error (e.g. an include routed outside the config directory) is an
    authoritative refusal: the write paths refuse, so reading a stale default
    file here would desync diffs and existence checks from what is editable.
    """
    layout = resolve_domain_layout(config_dir, domain)
    if layout is not None and layout.error:
        return None
    if layout is None:
        path = os.path.join(config_dir, DEFAULT_FILES[domain])
        if not os.path.isfile(path):
            return None
        try:
            doc = load_tagged(path)
        except Exception:  # noqa: BLE001 - best-effort probe
            return None
        if _DOMAIN_SHAPES[domain] == "named":
            if isinstance(doc, dict) and entry_id in doc:
                return encode_tags(doc[entry_id])
            return None
        if isinstance(doc, list):
            for item in doc:
                if _entry_matches(domain, item, entry_id):
                    return encode_tags(item)
        return None
    try:
        located = locate_entry(layout, entry_id)
    except LocateError:
        return None
    return encode_tags(located.config)
