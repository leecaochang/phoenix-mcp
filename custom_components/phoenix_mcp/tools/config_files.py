"""Config-file tools: the scoped filesystem jail and the raw configuration.yaml edit.

Two surfaces that both write files, kept together because they share the same
shape of risk and the same defence: resolve to a realpath, prove it is inside a
permitted directory, and refuse otherwise.

`cap_filesystem` covers www/, themes/ and custom_templates/ only
(const.FILESYSTEM_ALLOWED_DIRS). `_resolve_fs_path` is a real jail: it
realpaths and requires the result to sit under an allowed directory, so a
symlink or a traversal cannot escape it, and it cannot reach configuration.yaml
at all.

`cap_yaml_edit` covers configuration.yaml itself, and carries the
trust-boundary refusal: `_yaml_protected_check` hard-refuses any write that
adds, removes or modifies a path in const.YAML_PROTECTED_SUBTREES. The floor is
not "these keys are powerful" (command_line and shell_command stay writable,
because command execution is already accepted surface for this capability) but
"these keys redefine HA's OWN trust boundary": who may authenticate, which
proxy headers are trusted, whether brute-force defences run, which folder is
loaded as configuration, and what JavaScript loads into the authenticated
dashboard. It runs BOTH pre-gate and again at apply time against what is on
disk, so a change appearing during the approval window is still caught, and it
fails closed: content that does not parse as YAML is refused.

It also carries the accidental-deletion refusal, `_yaml_removal_check`: a write
that drops a top-level key present in the current file is refused unless
`remove_keys` names that key. Nothing else catches that case, because the file
is valid, no protected subtree moved, and expected_hash matches (the caller did
read first). Declaring the keys rather than setting a force flag is deliberate:
a bypass boolean gets set once and then always, while a named key lands in the
approval diff where a wrong claim is visible. Unlike the protected check this
one ABSTAINS in two cases: when the prior content is absent or unparseable
(refusing would make the tool unable to repair the broken file it exists to
repair), and on a version RESTORE, where reproducing the chosen snapshot
byte-for-byte is the whole operation and the admin has already seen it.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
import asyncio
import json
import logging
import os

from homeassistant.util.file import write_utf8_file_atomic as _write_utf8_file_atomic
from homeassistant.core import HomeAssistant

from ..const import CAP_DENY, DOMAIN, FILESYSTEM_ALLOWED_DIRS, MAX_FILE_BYTES, YAML_PROTECTED_SUBTREES
from ..data import PhoenixData
from ..helpers import content_hash, diff_summary_fields as _summary, effective_cap, redact_secrets_in_text as _redact_secrets_in_text, str_arg, str_list_arg
from ..tool_common import _CAP_FORBIDDEN_MESSAGE, _cas_conflict, _gate, _read_text_capped, _record_version, _restore_ctx, _text_file_cas_conflict, _tool_error, _tool_success, _truncate, _usable_path_arg, _version_content_payload, _write_text_atomic
from ..token_store import TokenRecord
from .. import yaml_includes

_LOGGER = logging.getLogger(__name__)


_CONFIG_YAML = "configuration.yaml"
_CONFIG_YAML_LOCK_KEY = f"{DOMAIN}_config_yaml_lock"


def _get_config_yaml_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Serialize whole-file configuration.yaml writes (set_yaml_config).

    That write is a full-file replace, so two concurrent callers would lose one
    edit outright, and the protected-subtree re-check reads the prior content a
    few statements before the write lands. Without this the window between them
    is a real one. expected_hash narrows it but is optional by design, so it
    cannot serve as the floor.

    Only set_yaml_config needs this: the automation/script/scene executors never
    touch configuration.yaml. yaml_includes fails closed with LocateError
    ("ambiguous") when a domain is inline there, so those paths only ever splice
    !include leaf files under their own domain locks.
    """
    if _CONFIG_YAML_LOCK_KEY not in hass.data:
        hass.data[_CONFIG_YAML_LOCK_KEY] = asyncio.Lock()
    return hass.data[_CONFIG_YAML_LOCK_KEY]


# ---------------------------------------------------------------------------
# Scoped filesystem (cap_filesystem): www/ themes/ custom_templates/
# ---------------------------------------------------------------------------




def _resolve_fs_path(hass: HomeAssistant, path: Any) -> str | None:
    """Resolve a path to a realpath strictly inside an allowed config dir, or None.

    realpath collapses '..' before the containment check, so traversal out of the
    allowlist is refused (returns None).
    """
    if _usable_path_arg(path) is None:
        return None
    config_dir = os.path.realpath(hass.config.config_dir)
    candidate = os.path.realpath(os.path.join(config_dir, path))
    for allowed in FILESYSTEM_ALLOWED_DIRS:
        base = os.path.realpath(os.path.join(config_dir, allowed))
        if candidate == base or candidate.startswith(base + os.sep):
            return candidate
    return None


def _listdir(target: str) -> list[dict]:
    return [
        {"name": name, "is_dir": os.path.isdir(os.path.join(target, name))}
        for name in sorted(os.listdir(target))
    ]






async def _tool_list_files(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list files in an allowed config directory."""
    if effective_cap(token, "cap_filesystem") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_files"
    path = str_arg(args.get("path"))
    if not path:
        return _tool_success(json.dumps({"directories": list(FILESYSTEM_ALLOWED_DIRS)})), "allowed", "list_files"
    target = _resolve_fs_path(hass, path)
    if target is None or not await hass.async_add_executor_job(os.path.isdir, target):
        return _tool_error("Directory not found."), "not_found", path
    entries = await hass.async_add_executor_job(_listdir, target)
    return _tool_success(json.dumps({"path": path, "entries": entries}, default=str)), "allowed", path






async def _tool_read_file(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read a UTF-8 text file from an allowed config directory."""
    if effective_cap(token, "cap_filesystem") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "read_file"
    path = str_arg(args.get("path"))
    target = _resolve_fs_path(hass, path)
    if target is None or not await hass.async_add_executor_job(os.path.isfile, target):
        return _tool_error("File not found."), "not_found", path
    try:
        content = await hass.async_add_executor_job(_read_text_capped, target)
    except ValueError:
        return _tool_error("File exceeds the maximum readable size."), "invalid_request", path
    except OSError:
        _LOGGER.exception("read_file failed for %s", path)
        return _tool_error("Failed to read file."), "invalid_request", path
    return _tool_success(json.dumps({
        "path": path, "content": content, "content_hash": content_hash(content),
    }, default=str)), "allowed", path


def _file_write_precheck(args: dict, hass: HomeAssistant, tool_name: str) -> tuple[dict, str, str] | None:
    """Pre-gate validation for filesystem writes; None means OK to proceed.

    Checks the path is inside an allowed directory and content is a
    size-bounded string, so a doomed request is rejected before a pending
    approval is created. The executor re-validates at apply time.
    """
    if _resolve_fs_path(hass, args.get("path", "")) is None:
        return _tool_error("Path is outside the allowed directories (www/, themes/, custom_templates/)."), "denied", tool_name
    content = args.get("content")
    if not isinstance(content, str):
        return _tool_error("content must be a string."), "invalid_request", tool_name
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _tool_error("Content exceeds the maximum file size."), "invalid_request", tool_name
    return None


async def _tool_write_file(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: write a file under an allowed config directory (Confirm-gated)."""
    if effective_cap(token, "cap_filesystem") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "write_file"
    pre = _file_write_precheck(args, hass, "write_file")
    if pre is not None:
        return pre
    conflict = await _text_file_cas_conflict(
        args.get("expected_hash"), _resolve_fs_path(hass, args.get("path", "")), hass, "write_file",
    )
    if conflict is not None:
        return conflict
    blocked = await _gate(
        "cap_filesystem", token, hass, data,
        tool_name="write_file", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_write_file(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_write_file(args, token, hass, data)


async def _execute_write_file(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    path = args.get("path", "")
    content = args.get("content")
    target = _resolve_fs_path(hass, path)
    if target is None:
        return _tool_error("Path is outside the allowed directories (www/, themes/, custom_templates/)."), "denied", "write_file"
    if not isinstance(content, str):
        return _tool_error("content must be a string."), "invalid_request", "write_file"
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _tool_error("Content exceeds the maximum file size."), "invalid_request", "write_file"
    existed = await hass.async_add_executor_job(os.path.isfile, target)
    before_content: str | None = None
    if existed:
        try:
            before_content = await hass.async_add_executor_job(_read_text_capped, target)
        except (OSError, ValueError):
            before_content = None  # unreadable/too large: capture as no prior content
    # Optimistic-concurrency guard at apply time: catches a change made during the
    # read/approve window (an absent/unreadable file hashes as "").
    conflict = _cas_conflict(args.get("expected_hash"), before_content or "", "write_file")
    if conflict is not None:
        return conflict
    try:
        await hass.async_add_executor_job(_write_text_atomic, target, content)
    except OSError:
        _LOGGER.exception("write_file failed")
        return _tool_error("Failed to write file."), "invalid_request", "write_file"
    # realpath touches the filesystem, so it goes to the executor like the
    # isfile probe above; relpath is pure string work and stays here.
    config_root = await hass.async_add_executor_job(
        os.path.realpath, hass.config.config_dir,
    )
    rel = os.path.relpath(target, config_root)
    await _record_version(
        data, token, resource_type="file", resource_id=rel,
        action="edit" if existed else "create",
        before=_version_content_payload(before_content, path=rel),
        after=_version_content_payload(content, path=rel),
        alias=rel,
    )
    return _tool_success(json.dumps({"path": path, "bytes_written": len(content.encode("utf-8"))})), "allowed", f"file:{path}"


# ---------------------------------------------------------------------------
# Raw configuration.yaml edit (cap_yaml_edit)
# ---------------------------------------------------------------------------


_YAML_READ_REFUSED = (
    "Only .yaml and .yml files inside the Home Assistant configuration directory can "
    "be read; secrets.yaml and hidden directories are excluded."
)


def _yaml_read_path_ok(rel: str) -> bool:
    """Structural rules for a readable YAML path, applied to a config-relative path.

    Run against BOTH the caller's argument and the realpath-resolved result, so a
    symlink cannot rename its way past the extension or secrets.yaml rule.
    """
    parts = [p for p in rel.replace(os.sep, "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):
        return False  # .storage, .cloud, and any other hidden directory
    name = parts[-1].lower()
    return name != yaml_includes.SECRETS_YAML and name.endswith((".yaml", ".yml"))


def _resolve_yaml_read_path(hass: HomeAssistant, file_arg: Any) -> tuple[str, str] | None:
    """Resolve a get_yaml_config file argument to (realpath, config-relative path).

    Returns None when the path is refused. Containment is checked on the realpath so
    '..' and symlinks cannot escape the configuration directory.
    """
    # A blank value means "not set" (models routinely send "" for an omitted
    # optional string), so it reads configuration.yaml rather than erroring.
    if file_arg is None or (isinstance(file_arg, str) and not file_arg.strip()):
        rel_arg = _CONFIG_YAML
    elif _usable_path_arg(file_arg) is not None:
        rel_arg = file_arg.strip()
    else:
        return None
    if os.path.isabs(rel_arg) or not _yaml_read_path_ok(rel_arg):
        return None
    config_dir = os.path.realpath(hass.config.config_dir)
    candidate = os.path.realpath(os.path.join(config_dir, rel_arg))
    if candidate != config_dir and not candidate.startswith(config_dir + os.sep):
        return None
    rel = os.path.relpath(candidate, config_dir)
    if not _yaml_read_path_ok(rel):
        return None
    return candidate, rel


def _fragment_text(fragment: Any) -> str:
    """Dump one YAML fragment for display, without the document-end marker.

    PyYAML appends "...\\n" after a bare scalar document; it is valid YAML but
    noise in a one-key answer.
    """
    text = yaml_includes.dump_tagged(fragment)
    if text.endswith("\n...\n"):
        text = text[: -len("...\n")]
    return text


def _yaml_fragment(text: str, key: str) -> tuple[bool, Any]:
    """Walk a dotted key path through mappings. Returns (found, value).

    Raises yaml_includes.YamlParseError when the document does not parse.
    """
    node = yaml_includes.load_tagged_lenient(text)
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


async def _tool_get_yaml_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read a YAML config file, or one key inside it."""
    if effective_cap(token, "cap_yaml_edit") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_yaml_config"
    resolved = _resolve_yaml_read_path(hass, args.get("file"))
    if resolved is None:
        return _tool_error(_YAML_READ_REFUSED), "invalid_request", "get_yaml_config"
    path, rel = resolved
    key = args.get("key")
    if key is not None and (not isinstance(key, str) or not key.strip()):
        return _tool_error("key must be a non-empty string."), "invalid_request", "get_yaml_config"
    if not await hass.async_add_executor_job(os.path.isfile, path):
        # An absent file reads as empty content rather than an error, so a
        # get-then-set round trip can create configuration.yaml and the CAS hash
        # of "" still matches. Shape is identical to the present-file case.
        body: dict[str, Any] = {
            "path": rel, "exists": False, "content": "", "content_hash": content_hash(""),
        }
        if key is not None:
            body["key_found"] = False
        return _tool_success(json.dumps(body)), "allowed", "get_yaml_config"
    try:
        content = await hass.async_add_executor_job(_read_text_capped, path)
    except ValueError:
        return _tool_error(f"{rel} exceeds the maximum readable size."), "invalid_request", "get_yaml_config"
    except OSError:
        _LOGGER.exception("get_yaml_config failed to read %s", rel)
        return _tool_error(f"Failed to read {rel}."), "invalid_request", "get_yaml_config"
    # content_hash is over the WHOLE file even when a fragment is returned: it is
    # the optimistic-lock token for a full-file write, not a checksum of the reply.
    body = {"path": rel, "exists": True, "content": content, "content_hash": content_hash(content)}
    if key is not None:
        try:
            found, fragment = await hass.async_add_executor_job(
                _yaml_fragment, content, key.strip())
        except yaml_includes.YamlParseError as err:
            return _tool_error(f"{rel} is not valid YAML: {err}"), "invalid_request", "get_yaml_config"
        body["key_found"] = found
        body["content"] = _fragment_text(fragment) if found else ""
        if (
            isinstance(fragment, yaml_includes.TaggedValue)
            and fragment.tag in yaml_includes.INCLUDE_TAGS
        ):
            # The key's real body lives in another file; name it so the agent can
            # read that file rather than concluding the key is empty.
            body["defined_via_include"] = fragment.value
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_yaml_config"


def _yaml_write_precheck(args: dict, tool_name: str) -> tuple[dict, str, str] | None:
    """Pre-gate validation for configuration.yaml writes; None means OK to proceed.

    Checks content is a size-bounded string so a doomed request is rejected
    before a pending approval is created. The executor re-validates at apply
    time.
    """
    content = args.get("content")
    if not isinstance(content, str):
        return _tool_error("content must be a string."), "invalid_request", tool_name
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _tool_error("Content exceeds the maximum file size."), "invalid_request", tool_name
    return None


_MISSING = object()


def _protected_subtree_diff(old_text: str | None, new_text: str) -> list[str]:
    """Dotted paths under YAML_PROTECTED_SUBTREES whose value the write changes.

    Both sides parse with the lenient loader so an unknown third-party tag cannot
    make the file unwritable. A value that is byte-identical on both sides passes,
    however sensitive the key: this refuses CHANGES, not the presence of a key.

    Raises yaml_includes.YamlParseError when the NEW content does not parse. Old
    content that does not parse is treated as empty, which fails closed: every
    protected key in the new content then counts as changed.
    """
    new_doc = yaml_includes.load_tagged_lenient(new_text)
    try:
        old_doc = yaml_includes.load_tagged_lenient(old_text) if old_text else None
    except yaml_includes.YamlParseError:
        old_doc = None
    if not isinstance(new_doc, dict):
        new_doc = {}
    if not isinstance(old_doc, dict):
        old_doc = {}

    changed: list[str] = []
    for top, children in YAML_PROTECTED_SUBTREES.items():
        new_top = new_doc.get(top)
        old_top = old_doc.get(top)
        for child in children:
            new_val = new_top.get(child, _MISSING) if isinstance(new_top, dict) else _MISSING
            old_val = old_top.get(child, _MISSING) if isinstance(old_top, dict) else _MISSING
            if new_val is _MISSING and old_val is _MISSING:
                continue
            if new_val is _MISSING or old_val is _MISSING or new_val != old_val:
                changed.append(f"{top}.{child}")
    return changed


def _yaml_protected_check(
    old_text: str | None, new_text: str, tool_name: str
) -> tuple[dict, str, str] | None:
    """Refuse a write that changes a protected subtree; None means OK to proceed.

    Runs pre-gate AND at apply time, so a change that appears during the approval
    window is caught too. Unparseable new content is refused rather than written:
    the check cannot be evaluated against it, and a configuration.yaml that does
    not parse stops Home Assistant from starting anyway.
    """
    try:
        changed = _protected_subtree_diff(old_text, new_text)
    except yaml_includes.YamlParseError as err:
        return (
            _tool_error(f"content is not valid YAML: {err}"),
            "invalid_request", tool_name,
        )
    if changed:
        return (
            _tool_error(
                "Refused: this write changes protected configuration keys: "
                f"{', '.join(changed)}. These keys define Home Assistant's own "
                "authentication, proxy trust, and dashboard code-loading, so they "
                "cannot be added, removed, or modified through this tool. Leave them "
                "exactly as they are; an administrator edits them by hand."
            ),
            "invalid_request", tool_name,
        )
    return None


def _removed_top_level_keys(
    old_text: str | None, new_text: str, declared: Sequence[str]
) -> list[str]:
    """Top-level keys the write drops without having declared them in remove_keys.

    A whole-file replace can silently delete configuration the caller never
    mentioned, and nothing else here catches it: the content is valid YAML, no
    protected subtree moved, and expected_hash matches because the caller DID
    read the file first. The worst shape is not a mangled file but a tidy one,
    e.g. dropping `automation: !include automations.yaml` disables every
    automation while automations.yaml sits on disk intact and check_config
    passes.

    Declaring a key is the escape hatch rather than a bare force flag: a single
    bypass boolean is set once and then always set, whereas naming the keys puts
    the intent in the approval diff where a wrong claim is visible.

    Both sides parse with the lenient loader, matching _protected_subtree_diff.
    Old content that is absent or does not parse yields NO removals, i.e. this
    check abstains rather than fails closed. That is deliberate: repairing a
    configuration.yaml that no longer parses is a legitimate use of this tool
    and refusing would make the tool unable to fix the one state that most needs
    fixing. The protected-subtree refusal still runs on that path.

    Assumes the NEW content already parsed, which holds because
    _yaml_protected_check runs first at both call sites and refuses if it did not.
    """
    try:
        old_doc = yaml_includes.load_tagged_lenient(old_text) if old_text else None
    except yaml_includes.YamlParseError:
        return []
    if not isinstance(old_doc, dict):
        return []
    try:
        new_doc = yaml_includes.load_tagged_lenient(new_text)
    except yaml_includes.YamlParseError:
        return []
    if not isinstance(new_doc, dict):
        new_doc = {}

    allowed = set(declared)
    return [key for key in old_doc if key not in new_doc and key not in allowed]


def _yaml_removal_check(
    old_text: str | None, new_text: str, args: dict, tool_name: str
) -> tuple[dict, str, str] | None:
    """Refuse an undeclared top-level key removal; None means OK to proceed.

    Runs pre-gate AND at apply time for the same reason the protected-subtree
    check does: the file can change while an approval waits, so a key that was
    present at gate time may be one this write is now dropping.

    A wrong-shaped remove_keys degrades to no declarations via str_list_arg,
    which refuses rather than waves the write through.
    """
    declared = str_list_arg(args.get("remove_keys"))
    removed = _removed_top_level_keys(old_text, new_text, declared)
    if removed:
        return (
            _tool_error(
                "Refused: this write removes top-level configuration keys that were "
                f"not declared: {', '.join(sorted(removed))}. If you meant to remove "
                "them, pass remove_keys naming exactly those keys. If you did not, "
                "this write was about to drop configuration you never intended to "
                "touch: re-read the file with get_yaml_config and preserve every key "
                "you are not changing."
            ),
            "invalid_request", tool_name,
        )
    return None


async def _read_config_yaml_text(hass: HomeAssistant) -> str | None:
    """Current configuration.yaml text, or None when absent or unreadable."""
    path = hass.config.path(_CONFIG_YAML)
    if not await hass.async_add_executor_job(os.path.isfile, path):
        return None
    try:
        return await hass.async_add_executor_job(_read_text_capped, path)
    except (OSError, ValueError):
        return None


async def _tool_set_yaml_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: replace configuration.yaml (Confirm-gated)."""
    if effective_cap(token, "cap_yaml_edit") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_yaml_config"
    pre = _yaml_write_precheck(args, "set_yaml_config")
    if pre is not None:
        return pre
    current = await _read_config_yaml_text(hass)
    protected = await hass.async_add_executor_job(
        _yaml_protected_check, current, args["content"], "set_yaml_config",
    )
    if protected is not None:
        return protected
    removal = await hass.async_add_executor_job(
        _yaml_removal_check, current, args["content"], args, "set_yaml_config",
    )
    if removal is not None:
        return removal
    conflict = await _text_file_cas_conflict(
        args.get("expected_hash"), hass.config.path(_CONFIG_YAML), hass, "set_yaml_config",
    )
    if conflict is not None:
        return conflict
    blocked = await _gate(
        "cap_yaml_edit", token, hass, data,
        tool_name="set_yaml_config", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_yaml_config(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_yaml_config(args, token, hass, data)


async def _execute_set_yaml_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    content = args.get("content")
    if not isinstance(content, str):
        return _tool_error("content must be a string."), "invalid_request", "set_yaml_config"
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _tool_error("Content exceeds the maximum file size."), "invalid_request", "set_yaml_config"
    path = hass.config.path(_CONFIG_YAML)
    # Read, protected-subtree re-check, CAS and write are one critical section:
    # the write is a whole-file replace, so an interleaved second writer would
    # both lose an edit and invalidate the prior content the check above ran on.
    async with _get_config_yaml_lock(hass):
        existed = await hass.async_add_executor_job(os.path.isfile, path)
        before_content: str | None = None
        if existed:
            try:
                before_content = await hass.async_add_executor_job(_read_text_capped, path)
            except (OSError, ValueError):
                before_content = None  # unreadable/too large: capture as no prior content
        # Re-check the protected subtrees against what is on disk NOW: the file can
        # change between the gate and an admin approving the write.
        protected = await hass.async_add_executor_job(
            _yaml_protected_check, before_content, content, "set_yaml_config",
        )
        if protected is not None:
            return protected
        # Same approval-window reasoning as the protected re-check above: a key
        # present when the gate ran may be one this write is now dropping.
        #
        # A version RESTORE is exempt, mirroring esphome_yaml's literal-credential
        # waiver: the admin picked that snapshot from a panel showing its content,
        # reproducing it byte-for-byte is the entire operation, and it legitimately
        # drops keys added after it was taken. This guard exists to catch an AGENT
        # dropping a key it never mentioned, which is the opposite situation. The
        # ContextVar is read HERE, in the event loop, because it does not cross
        # into an executor job.
        if _restore_ctx.get() is None:
            removal = await hass.async_add_executor_job(
                _yaml_removal_check, before_content, content, args, "set_yaml_config",
            )
            if removal is not None:
                return removal
        # Optimistic-concurrency guard at apply time: catches a change made during the
        # read/approve window (an absent/unreadable file hashes as "").
        conflict = _cas_conflict(args.get("expected_hash"), before_content or "", "set_yaml_config")
        if conflict is not None:
            return conflict
        try:
            await hass.async_add_executor_job(_write_utf8_file_atomic, path, content)
        except OSError:
            _LOGGER.exception("set_yaml_config failed")
            return _tool_error("Failed to write configuration.yaml."), "invalid_request", "set_yaml_config"
        # Inside the lock so the history cannot record two writes out of order.
        await _record_version(
            data, token, resource_type="yaml_config", resource_id=_CONFIG_YAML,
            action="edit" if existed else "create",
            before=_version_content_payload(before_content),
            after=_version_content_payload(content),
            alias=_CONFIG_YAML,
        )
    return (
        _tool_success(json.dumps({
            "path": _CONFIG_YAML,
            "bytes_written": len(content.encode("utf-8")),
            "note": "Run check_config and restart Home Assistant to apply.",
        })),
        "allowed", "set_yaml_config",
    )


def _read_capped_if_file(path: str) -> str | None:
    """isfile-guarded _read_text_capped for diff builders. Returns None when the
    file is missing, too large, or unreadable. Safe to run in an executor job."""
    try:
        if os.path.isfile(path):
            return _read_text_capped(path)
    except (OSError, ValueError):
        return None
    return None


async def _build_diff_write_file(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    path = args.get("path", "")
    content = args.get("content") if isinstance(args.get("content"), str) else ""
    target = _resolve_fs_path(hass, path)
    before = None
    if target is not None:
        raw = await hass.async_add_executor_job(_read_capped_if_file, target)
        before = _truncate(raw) if raw is not None else None
    return {
        "kind": "file_write",
        **_summary("write_file", path=path),
        "target": {"type": "file", "id": path, "label": path},
        "before": _redact_secrets_in_text(before),
        "after": _redact_secrets_in_text(_truncate(str(content))),
        "preview": {"path": path, "outside_allowed_dirs": target is None,
                    "bytes": len(str(content).encode("utf-8"))},
    }


async def _build_diff_set_yaml_config(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    raw_content = args.get("content")
    content: str = raw_content if isinstance(raw_content, str) else ""
    path = hass.config.path(_CONFIG_YAML)
    raw = await hass.async_add_executor_job(_read_capped_if_file, path)
    before = _truncate(raw) if raw is not None else None
    # Computed from the content, never from the caller's own remove_keys: the
    # summary is the line an admin reads in History, so it must report what the
    # write actually drops rather than what it claims to drop.
    removing = await hass.async_add_executor_job(
        _removed_top_level_keys, raw, content, [],
    )
    return {
        "kind": "yaml_diff",
        **(
            _summary("set_yaml_config.removing", keys=", ".join(sorted(removing)))
            if removing else _summary("set_yaml_config")
        ),
        "target": {"type": "file", "id": _CONFIG_YAML, "label": _CONFIG_YAML},
        "before": _redact_secrets_in_text(before),
        "after": _redact_secrets_in_text(_truncate(str(content))),
        "preview": {"warning": "Replaces the entire configuration.yaml; a broken file blocks HA startup."},
    }
