"""Credential masking for ESPHome device YAML.

ESPHome device files routinely carry credentials inline rather than behind
`!secret`: an API encryption key, an OTA password, a wifi password. Phoenix MCP
must be able to read and edit those files without ever handing the values to an
agent or writing them into an approval record, and without a blind write being
able to repoint a device at an attacker's network.

The approach is span-surgical. `yaml.compose` gives every scalar's exact source
span, so a placeholder is spliced into the RAW file text at those offsets and
nothing else moves. Comments, ordering, and formatting survive byte-for-byte,
which matters because these files are heavily commented; a parse-and-re-dump
round trip would silently destroy that.

Three detection layers feed one span set:

  1. Curated paths      known credential locations (api.encryption.key, ...).
  2. Key-name heuristic any scalar whose KEY looks like a credential, at any
                        depth, so a bindkey or a custom auth header is covered
                        without waiting for the curated list to learn about it.
  3. secrets.yaml       any scalar whose whole value matches a secrets.yaml
                        value, catching the same credential inlined in one file
                        while referenced by `!secret` in another.

Over-redaction is the accepted failure direction. Redacted spans are also
WRITE-FROZEN: a caller may replace one with a `!secret` reference (the supported
migration) but never with a different literal. Freezing is not only about
disclosure. `api.encryption.key` must match the noise PSK stored in HA's config
entry, and `ota[].password` governs the next upload's authentication, so a blind
change can leave a device unreachable, which is a physical-access problem for
anything mounted out of reach.

`secrets.yaml` itself is never readable or writable through any tool. Its values
are parsed here, internally, only to know what NOT to emit.
"""

from __future__ import annotations

import base64
import re
import secrets as secrets_module
from collections.abc import Iterator
from typing import Any

import yaml

from .yaml_includes import PhoenixLenientLoader, YamlParseError

# Known credential locations. "[]" matches any list index, so both the modern
# list form (ota: - platform: esphome / password:) and the legacy mapping form
# are covered; a curated list built only from the legacy shape would silently
# miss every current config.
ESPHOME_SECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("api", "encryption", "key"),
    ("api", "password"),
    ("wifi", "password"),
    ("wifi", "ap", "password"),
    ("wifi", "networks", "[]", "password"),
    ("ota", "password"),
    ("ota", "[]", "password"),
    ("web_server", "auth", "password"),
    ("mqtt", "password"),
)

# Layer 2: key names that mark their value as a credential wherever they appear.
_SECRET_KEY_RE = re.compile(r"(password|passwd|psk|bindkey|api_key|token|secret)", re.IGNORECASE)

# Layer 3 floor. A short secrets.yaml value (a pin number, a small integer)
# would match unrelated scalars all over a device file and mangle it, so only
# values of at least this length are cross-checked, and only as whole scalars.
MIN_CROSS_CHECK_LENGTH = 8

_PLACEHOLDER_PREFIX = "__PHOENIX_REDACTED__"
_PLACEHOLDER_SUFFIX = "__"
_PLACEHOLDER_RE = re.compile(
    re.escape(_PLACEHOLDER_PREFIX) + r"(?P<path>[^\s]*?)" + re.escape(_PLACEHOLDER_SUFFIX)
)

_ALLOWED_SUFFIXES = (".yaml", ".yml")
_ARCHIVE_DIR = "archive"

# Write-time marker letting an agent ask for a credential it must never choose.
# A language model does not produce cryptographic randomness, so the fix is
# Phoenix MCP GENERATING rather than Phoenix MCP accepting: the agent writes the
# tag, the real value is substituted here as the file is written, and every
# later read masks it. The agent therefore never learns the value, and because
# substitution happens at write time the approval record and its diff carry the
# INTENT (the tag) rather than a secret.
PHOENIX_GENERATE_TAG = "!phoenix_generate"

# Never generated, whatever the key name looks like: these are the HOUSE's
# credentials rather than the device's. Replacing one with randomness would take
# the device off the network with no route back except a cable, and in the wifi
# case would do it to every device that shares the secret.
_NEVER_GENERATE: frozenset[tuple[str, ...]] = frozenset({
    ("wifi", "password"),
    ("wifi", "ssid"),
    ("wifi", "networks", "[]", "password"),
})


class EsphomeSecretViolation(Exception):
    """Raised when a write would change, duplicate, or introduce a credential."""


def placeholder_for(path: str) -> str:
    """The placeholder that stands in for the value at a dotted path.

    Path-based, never value-derived: a hash of the real value would be an
    offline-guessing oracle for a weak password, and would change whenever the
    credential rotated.
    """
    return f"{_PLACEHOLDER_PREFIX}{path}{_PLACEHOLDER_SUFFIX}"


def esphome_rel_path_ok(rel: str) -> bool:
    """Whether a config-relative path may be read or written as device YAML.

    Mirrors _yaml_read_path_ok's rules and adds two of its own: `archive/` is the
    Device Builder's trash, and secrets.yaml is never reachable in either
    direction.
    """
    if not isinstance(rel, str) or not rel.strip():
        return False
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):
        return False
    if parts[0].lower() == _ARCHIVE_DIR:
        return False
    name = parts[-1]
    if name.lower() == "secrets.yaml":
        return False
    return name.lower().endswith(_ALLOWED_SUFFIXES)


def _path_matches_curated(path: tuple[str, ...]) -> bool:
    """Whether a node path matches a curated secret path, honoring the [] wildcard."""
    for pattern in ESPHOME_SECRET_PATHS:
        if len(pattern) != len(path):
            continue
        if all(p == "[]" or p == seg for p, seg in zip(pattern, path)):
            return True
    return False


def _format_path(path: tuple[str, ...]) -> str:
    """Render a node path as the dotted form used inside a placeholder."""
    out = ""
    for seg in path:
        if seg.startswith("[") and seg.endswith("]"):
            out += seg
        else:
            out = f"{out}.{seg}" if out else seg
    return out


def _walk(node: Any, path: tuple[str, ...]) -> Iterator[tuple[str, tuple[str, ...], str, Any]]:
    """Yield (dotted_path, path_tuple, key_name, scalar_node) for every scalar."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = getattr(key_node, "value", None)
            if not isinstance(key, str):
                continue
            yield from _walk(value_node, path + (key,))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            yield from _walk(item, path + (f"[{index}]",))
    elif isinstance(node, yaml.ScalarNode):
        key = path[-1] if path else ""
        yield _format_path(path), path, key, node


def _compose(text: str) -> Any:
    """Parse to a node tree, keeping unknown tags intact.

    Composition stops at the parser level, so no constructor runs: !lambda,
    !secret, and any third-party tag survive without needing to be understood.
    """
    try:
        return yaml.compose(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError as err:
        raise YamlParseError(str(err)) from err


def _normalized_path(path: tuple[str, ...]) -> tuple[str, ...]:
    """Path with list indices collapsed to [] so curated patterns can match."""
    return tuple("[]" if seg.startswith("[") and seg.endswith("]") else seg for seg in path)


def _is_tagged(node: yaml.ScalarNode) -> bool:
    """Whether a scalar carries a custom tag (!secret, !lambda, ...).

    A tagged scalar is a reference or an expression, not a literal value, so
    there is nothing to hide and nothing to freeze.
    """
    return isinstance(node.tag, str) and node.tag.startswith("!")


def secret_values_from_text(text: str) -> set[str]:
    """Values defined in a secrets.yaml, for the layer-3 cross-check.

    Returns an empty set for unparseable or empty input: a missing secrets.yaml
    degrades redaction to layers 1 and 2 rather than failing every read.
    """
    try:
        data = yaml.load(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    return {
        value for value in data.values()
        if isinstance(value, str) and len(value) >= MIN_CROSS_CHECK_LENGTH
    }


def secret_keys_from_text(text: str) -> set[str]:
    """Key NAMES defined in a secrets.yaml, for !secret reference validation."""
    try:
        data = yaml.load(text, Loader=PhoenixLenientLoader)
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    return {k for k in data if isinstance(k, str)}


def _secret_spans(text: str, secret_values: set[str]) -> list[tuple[int, int, str]]:
    """Every span in `text` holding a credential, as (start, end, dotted_path).

    Unions all three detection layers. Ordered last-first so callers can splice
    without recomputing offsets.
    """
    root = _compose(text)
    if root is None:
        return []
    spans: list[tuple[int, int, str]] = []
    for dotted, path, key, node in _walk(root, ()):
        if _is_tagged(node):
            continue
        value = node.value
        if not isinstance(value, str) or not value:
            continue
        hit = (
            _path_matches_curated(_normalized_path(path))
            or bool(_SECRET_KEY_RE.search(key))
            or (len(value) >= MIN_CROSS_CHECK_LENGTH and value in secret_values)
        )
        if hit:
            spans.append((node.start_mark.index, node.end_mark.index, dotted))
    spans.sort(key=lambda s: s[0], reverse=True)
    return spans


def redact_esphome_text(
    text: str, secret_values: set[str] | None = None
) -> tuple[str, list[str]]:
    """Return (redacted_text, sorted redacted paths).

    Raises YamlParseError when the document does not parse. Reads FAIL CLOSED:
    a file whose credentials cannot be located must not be returned at all,
    because inline credentials are the normal case rather than the exception.
    """
    spans = _secret_spans(text, secret_values or set())
    out = text
    for start, end, dotted in spans:
        out = out[:start] + placeholder_for(dotted) + out[end:]
    return out, sorted(dotted for _, _, dotted in spans)


def inline_secret_values(text: str, secret_values: set[str] | None = None) -> set[str]:
    """The credential VALUES a device file carries inline, for scrubbing free text.

    Same three-layer detection as the masking path, but returning the values
    rather than their spans: the Device Builder's validation output quotes the
    offending lines back, so a file with an inline password can print it into an
    error message that has no YAML structure to splice.

    Short values are dropped (the MIN_CROSS_CHECK_LENGTH floor) because a scrub
    is a blind substring replace over arbitrary output, where a two-character
    "value" would corrupt unrelated text.
    """
    values: set[str] = set()
    for start, end, _dotted in _secret_spans(text, secret_values or set()):
        raw = text[start:end].strip().strip("\"'")
        if len(raw) >= MIN_CROSS_CHECK_LENGTH:
            values.add(raw)
    return values


def scrub_secret_values(text: str, values: set[str]) -> str:
    """Replace every occurrence of every known credential in free text.

    Longest first, so a value that contains another leaves no readable remainder
    behind. The placeholder is fixed rather than path-derived: this is
    unstructured output, not a file position a later write could splice back.
    """
    out = text
    for value in sorted(values, key=len, reverse=True):
        if value:
            out = out.replace(value, "<redacted>")
    return out


def _placeholder_at(node: yaml.ScalarNode) -> str | None:
    """The dotted path embedded in a scalar that is exactly one placeholder."""
    value = node.value
    if not isinstance(value, str):
        return None
    match = _PLACEHOLDER_RE.fullmatch(value.strip())
    return match.group("path") if match else None


def _generated_value(path: tuple[str, ...]) -> str:
    """Fresh randomness for one credential path.

    ESPHome's API encryption key must be exactly 32 base64-encoded bytes and
    nothing else is accepted. Passwords are free-form, so a URL-safe token keeps
    them clear of every YAML quoting corner case.
    """
    if _normalized_path(path) == ("api", "encryption", "key"):
        return base64.b64encode(secrets_module.token_bytes(32)).decode("ascii")
    return secrets_module.token_urlsafe(24)


def _generate_refusal(
    dotted: str, path: tuple[str, ...], key: str, disk_spans: dict[str, Any]
) -> str | None:
    """Why this generate request cannot be honored, or None if it can."""
    normalized = _normalized_path(path)
    if normalized in _NEVER_GENERATE:
        return (
            f"'{dotted}' is one of the house's own wifi credentials, which is never "
            f"generated: replacing it would take this device (and anything sharing the "
            f"secret) off the network. Use a !secret reference instead."
        )
    if not (_path_matches_curated(normalized) or _SECRET_KEY_RE.search(key)):
        return (
            f"'{dotted}' is not a credential field, so there is nothing to generate there. "
            f"{PHOENIX_GENERATE_TAG} is only for credential values such as an API "
            f"encryption key or an OTA password."
        )
    if dotted in disk_spans:
        return (
            f"'{dotted}' already holds a credential, and regenerating it would change the "
            "value the device is currently running: the device can be left unreachable "
            "until it is reflashed over a cable, and Home Assistant would need the new key "
            "before it could reconnect. Leave the existing placeholder in place to keep it."
        )
    return None


def _quote_scalar(value: str) -> str:
    """Render a generated value as a double-quoted YAML scalar.

    Quoted unconditionally so a value beginning with a character YAML treats as
    structural can never change the document's shape.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def splice_esphome_text(
    new_text: str,
    disk_text: str,
    secret_values: set[str] | None = None,
    secret_keys: set[str] | None = None,
    allow_literal_credentials: bool = False,
) -> str:
    """Return the raw text to write, restoring credentials behind placeholders.

    The frozen span set is recomputed from DISK content on every call, so what is
    frozen always derives from the file rather than from anything the caller
    claims about it.

    Raises EsphomeSecretViolation when a write would change a credential,
    duplicate one into another location, introduce a new inline one, or reference
    a `!secret` key that does not exist.

    allow_literal_credentials waives the three LITERAL refusals (changing a masked
    value, repeating a secrets.yaml value, a new inline credential) and is for the
    version-restore path ONLY. Those three exist to stop an AGENT authoring
    credentials; a restore re-applies raw text Phoenix itself recorded from a file
    Phoenix itself wrote, so they buy nothing there and cost correctness: a version
    snapshot carries the file's inline credentials as literals, which is byte-for-
    byte what restore must reproduce, so without the waiver every restore of a
    file with any inline credential is refused for "changing" a value it is
    rewriting unaltered. Inline credentials are the normal case in these files,
    so that covers nearly all of them.

    Scoping the waiver to restore rather than accepting a byte-identical literal
    generally is deliberate: accepted-versus-refused would otherwise tell a caller
    whether a guessed credential was correct, which is a guessing oracle. The
    placeholder rules and the !secret existence check still apply on both paths.
    """
    secret_values = secret_values or set()
    disk_spans = {dotted: (start, end) for start, end, dotted in _secret_spans(disk_text, secret_values)}
    disk_root = _compose(disk_text)
    disk_scalars = (
        {dotted: node for dotted, _, _, node in _walk(disk_root, ())} if disk_root is not None else {}
    )

    new_root = _compose(new_text)
    if new_root is None:
        return new_text

    replacements: list[tuple[int, int, str]] = []
    for dotted, path, key, node in _walk(new_root, ()):
        tagged = _is_tagged(node)

        if tagged:
            if node.tag == PHOENIX_GENERATE_TAG:
                refusal = _generate_refusal(dotted, path, key, disk_spans)
                if refusal is not None:
                    raise EsphomeSecretViolation(refusal)
                # Substituted HERE, as the file is written, so the value exists
                # only on disk. It is never returned to the caller, never in the
                # approval record (which holds the caller's own text), and is
                # masked by every subsequent read.
                replacements.append((
                    node.start_mark.index,
                    node.end_mark.index,
                    _quote_scalar(_generated_value(path)),
                ))
                continue

            # A !secret reference is the supported way to replace an inline
            # credential, so it is never a violation. Validate the target exists:
            # Tier 2 has no compile step, so a dangling reference would otherwise
            # only surface as a broken build much later.
            if node.tag == "!secret" and secret_keys is not None:
                name = str(node.value).strip()
                if name and name not in secret_keys:
                    raise EsphomeSecretViolation(
                        f"'{dotted}' references the secret '{name}', which is not defined in "
                        "the ESPHome secrets.yaml. Add it there first (with the value that is "
                        "currently inline), then retry this edit."
                    )
            continue

        embedded = _placeholder_at(node)
        if embedded is not None:
            if embedded != dotted:
                raise EsphomeSecretViolation(
                    f"The placeholder for '{embedded}' was moved to '{dotted}'. A masked value "
                    "cannot be copied to another location; leave each placeholder where it was."
                )
            span = disk_spans.get(dotted)
            if span is None:
                raise EsphomeSecretViolation(
                    f"'{dotted}' has no masked value on disk to restore. In new content, use a "
                    "!secret reference at this path instead of a placeholder."
                )
            disk_node = disk_scalars.get(dotted)
            original = disk_text[span[0]:span[1]]
            if disk_node is not None and not isinstance(disk_node.value, str):
                original = disk_text[span[0]:span[1]]
            replacements.append((node.start_mark.index, node.end_mark.index, original))
            continue

        value = node.value
        if not isinstance(value, str) or not value:
            continue

        if allow_literal_credentials:
            continue

        if dotted in disk_spans:
            raise EsphomeSecretViolation(
                f"'{dotted}' is a masked credential and cannot be changed to a new value. "
                "Replace it with a !secret reference and set the value in secrets.yaml "
                "instead. Changing an API encryption key or OTA password in place can also "
                "leave the device unreachable until it is reflashed over a cable."
            )

        if len(value) >= MIN_CROSS_CHECK_LENGTH and value in secret_values:
            raise EsphomeSecretViolation(
                f"'{dotted}' would write a value that is defined in secrets.yaml. Use a "
                "!secret reference rather than repeating the literal."
            )

        if _SECRET_KEY_RE.search(key) or _path_matches_curated(_normalized_path(path)):
            raise EsphomeSecretViolation(
                f"'{dotted}' is a credential field and cannot be given an inline value. "
                "Use a !secret reference and set the value in secrets.yaml."
            )

    out = new_text
    for start, end, original in sorted(replacements, key=lambda r: r[0], reverse=True):
        out = out[:start] + original + out[end:]
    return out
