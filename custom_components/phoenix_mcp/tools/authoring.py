"""Authoring tools: automation, script and scene CRUD over the YAML config store.

The three domains share one shape and therefore one module: each stores its
configs in a top-level YAML file (automations.yaml / scripts.yaml / scenes.yaml)
that HA also lets a human edit by hand, each is edited under its own
asyncio.Lock keyed into hass.data, and each write goes read-modify-write with an
optimistic-lock content_hash checked pre-gate and again at apply time. Splitting
them into three modules would only move the shared plumbing into a fourth.

`yaml_includes` is the reason the reads and writes are not a plain file load: a
domain can be split across !include files, so a write has to splice into the
leaf file that actually holds the entry. It fails closed with LocateError when a
domain is inline in configuration.yaml, which is why nothing here ever writes
that file.

Scene writes carry one extra check the other two do not: a scene captures member
entities' states, so `_unwritable_scene_members` refuses a scene naming an
entity the token cannot WRITE. Without it, a token could snapshot and later
restore entities outside its own scope.

mcp_view owns the transport, the dispatch registry and the executor registry,
and imports the names it registers or calls from here (async_restore_version
takes the six create/edit executors, and the reference walkers take the three
readers and their path constants). The dependency runs one way: this module
never imports the transport that dispatches it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol

from homeassistant.components.automation.config import async_validate_config_item as _validate_automation_config
from homeassistant.components.script.config import async_validate_config_item as _validate_script_config
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.file import write_utf8_file_atomic as _write_utf8_file_atomic
from homeassistant.util.yaml import dump as _yaml_dump, load_yaml as _load_yaml
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant

from ..const import CAP_DENY, DOMAIN
from ..data import PhoenixData
from ..helpers import content_hash, dict_arg, diff_summary_fields as _summary, effective_cap, str_arg
from ..tool_common import _CAP_FORBIDDEN_MESSAGE, _cas_conflict, _gate, _record_version, _tool_error, _tool_success, _truncate
from ..policy_engine import Permission, filter_entities_for_token, resolve
from ..token_store import TokenRecord
from .. import yaml_includes

_LOGGER = logging.getLogger(__name__)


_AUTOMATION_YAML = "automations.yaml"
_AUTOMATION_LOCK_KEY = f"{DOMAIN}_automation_lock"
_SCRIPT_CONFIG_PATH = "scripts.yaml"
_SCRIPT_LOCK_KEY = f"{DOMAIN}_script_lock"


def _get_automation_lock(hass: HomeAssistant) -> asyncio.Lock:
    if _AUTOMATION_LOCK_KEY not in hass.data:
        hass.data[_AUTOMATION_LOCK_KEY] = asyncio.Lock()
    return hass.data[_AUTOMATION_LOCK_KEY]


def _read_automations_yaml(path: str) -> list:
    if not os.path.isfile(path):
        return []
    data = _load_yaml(path)
    return data if isinstance(data, list) else []


def _write_automations_yaml(path: str, data: list) -> None:
    contents = _yaml_dump(data)
    _write_utf8_file_atomic(path, contents)


def _get_script_lock(hass: HomeAssistant) -> asyncio.Lock:
    if _SCRIPT_LOCK_KEY not in hass.data:
        hass.data[_SCRIPT_LOCK_KEY] = asyncio.Lock()
    return hass.data[_SCRIPT_LOCK_KEY]


def _read_scripts_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    data = _load_yaml(path)
    return data if isinstance(data, dict) else {}


def _write_scripts_yaml(path: str, data: dict) -> None:
    contents = _yaml_dump(data)
    _write_utf8_file_atomic(path, contents)


def _yaml_file_has_includes(path: str) -> bool:
    """Return True if the file exists and contains YAML !include directives."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "!include" in f.read()
    except OSError:
        return False


_SCENE_CONFIG_PATH = "scenes.yaml"
_SCENE_LOCK_KEY = f"{DOMAIN}_scene_yaml_lock"


def _read_scenes_yaml(path: str) -> list:
    if not os.path.isfile(path):
        return []
    data = _load_yaml(path)
    return data if isinstance(data, list) else []


def _write_scenes_yaml(path: str, data: list) -> None:
    _write_utf8_file_atomic(path, _yaml_dump(data))


def _get_scene_lock(hass: HomeAssistant) -> asyncio.Lock:
    if _SCENE_LOCK_KEY not in hass.data:
        hass.data[_SCENE_LOCK_KEY] = asyncio.Lock()
    return hass.data[_SCENE_LOCK_KEY]


_SCRIPT_ID_RE = re.compile(r"^[a-z0-9_]+$")


async def _read_config_entry(hass: HomeAssistant, domain: str, entry_id: str) -> Any:
    """Current stored config for one automation/script/scene entry, or None.

    Tag-encoded (a !secret renders as the display string "!secret name"; the
    value is never resolved), so the result is JSON-safe and hashable for the
    compare-and-swap guard.
    """
    return await hass.async_add_executor_job(
        yaml_includes.read_entry, hass.config.config_dir, domain, entry_id,
    )


def _config_read_response(
    id_key: str, entry_id: str, config: Any, resource: str
) -> tuple[dict, str, str]:
    """Shared body for the config-read tools: config plus its content_hash.

    The hash is over the same tag-encoded structure returned here, so an agent
    can echo it straight back as expected_hash on the matching edit tool.
    """
    return _tool_success(json.dumps({
        id_key: entry_id, "config": config, "content_hash": content_hash(config),
    }, indent=2, default=str)), "allowed", resource


async def _entry_cas_conflict(
    args: dict, hass: HomeAssistant, domain: str, entry_id: str, tool_name: str
) -> tuple[dict, str, str] | None:
    """CAS check for an automation/script/scene edit, against its stored config.

    Runs before gating (so a stale edit never becomes a doomed pending approval)
    and again inside the executor (to catch a change made during the approval
    window). No expected_hash skips the check entirely.
    """
    expected = args.get("expected_hash")
    if not expected:
        return None
    current = await _read_config_entry(hass, domain, entry_id)
    return _cas_conflict(expected, current, tool_name)


async def _tool_list_automations(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list accessible automation.* entities with their editable id."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "list_automations"
    items: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        if not e["entity_id"].startswith("automation."):
            continue
        attrs = e.get("attributes", {})
        items.append({
            "entity_id": e["entity_id"],
            "alias": attrs.get("friendly_name"),
            "automation_id": attrs.get("id"),
            "state": e.get("state"),
        })
    items.sort(key=lambda a: a["entity_id"])
    return _tool_success(json.dumps({"count": len(items), "automations": items}, default=str)), "allowed", "list_automations"


async def _tool_get_automation(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read one automation's current stored config, with a content_hash."""
    if effective_cap(token, "cap_automation_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_automation"
    automation_id = str(args.get("automation_id") or "").strip()
    if not automation_id:
        return _tool_error("automation_id is required."), "invalid_request", "get_automation"
    config = await _read_config_entry(hass, "automation", automation_id)
    if config is None:
        return _tool_error(f"No automation found with id '{automation_id}'."), "not_found", f"automation:{automation_id}"
    return _config_read_response("automation_id", automation_id, config, f"automation:{automation_id}")


# The guidance an agent gets when its own config does not validate, one per
# authored domain. Agent-facing error text is deliberately English (CLAUDE.md).
_AUTOMATION_INVALID_MESSAGE = "Automation config failed validation. Check trigger, condition, and action fields."
_SCRIPT_INVALID_MESSAGE = "Script config failed validation. Check sequence, mode, and field definitions."


async def _config_validation_error(
    validator: Callable[[HomeAssistant, str, dict], Awaitable[Any]],
    hass: HomeAssistant,
    config_key: str,
    config: dict,
    *,
    tool_name: str,
    invalid_message: str,
) -> tuple[dict, str, str] | None:
    """Run an HA config validator; return an error triple, or None when the config validates.

    HA calls its automation and script validators with raise_on_errors=True, so a
    config the caller got wrong arrives as vol.Invalid or HomeAssistantError.
    That is the routine case during authoring, so it stays quiet (debug) and
    returns the domain's own guidance.

    Anything else is HA drift or a Phoenix bug wearing the same clothes. Reporting
    that as a validation failure tells an agent to rewrite a config that was never
    the problem, so it is logged with a traceback and reported as an internal
    error instead. The distinction matters because these validators are HA
    internals: the exception a version bump introduces arrives here first.
    """
    try:
        if await validator(hass, config_key, config) is None:
            return _tool_error(invalid_message), "invalid_request", tool_name
    except (vol.Invalid, HomeAssistantError) as exc:
        _LOGGER.debug("%s config validation rejected the config: %s", tool_name, exc)
        return _tool_error(invalid_message), "invalid_request", tool_name
    except Exception:
        _LOGGER.exception("%s config validation raised an unexpected error", tool_name)
        return _tool_error("Internal error validating this config."), "invalid_request", tool_name
    return None


async def _automation_write_precheck(
    args: dict, hass: HomeAssistant, tool_name: str, *, require_id: bool
) -> tuple[dict, str, str] | None:
    """Pre-gate validation for automation writes; None means OK to proceed.

    Checks automation_id presence and config schema so a doomed request is
    rejected before a pending approval is created. The executor re-validates at
    apply time.
    """
    automation_id = str(args.get("automation_id") or "").strip()
    if require_id and not automation_id:
        return _tool_error("automation_id is required."), "invalid_request", tool_name
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", tool_name
    return await _config_validation_error(
        _validate_automation_config, hass, automation_id or "phoenix_mcp_validate", config,
        tool_name=tool_name, invalid_message=_AUTOMATION_INVALID_MESSAGE,
    )


async def _tool_create_automation(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a new UI automation by appending to automations.yaml."""
    if effective_cap(token, "cap_automation_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "create_automation"
    pre = await _automation_write_precheck(args, hass, "create_automation", require_id=False)
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_automation_write", token, hass, data,
        tool_name="create_automation", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_create_automation(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_automation(args, token, hass, data)


async def _execute_create_automation(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "create_automation"

    # A restore of a deleted automation recreates it under its original id (passed
    # explicitly) so it returns in place and re-restoring is idempotent;
    # a fresh create mints a new id.
    automation_id = str(args.get("automation_id") or "").strip() or "phoenix_mcp_" + uuid.uuid4().hex[:16]
    config = {k: v for k, v in config.items() if k != "id"}
    config["id"] = automation_id

    # Validate config but write the original (not the validated result) to YAML.
    # HA's validator may return internal representations that don't round-trip
    # through YAML cleanly. HA's own automation UI follows the same pattern:
    # write the user's config and let automation.reload normalize it.
    invalid = await _config_validation_error(
        _validate_automation_config, hass, automation_id, config,
        tool_name="create_automation", invalid_message=_AUTOMATION_INVALID_MESSAGE,
    )
    if invalid is not None:
        return invalid

    path = os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
    lock = _get_automation_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_create, hass.config.config_dir, "automation",
                automation_id, config,
            )
            if op.fallback:
                # configuration.yaml missing or unparseable: legacy hardcoded-file path.
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("automations.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "create_automation"
                items = await hass.async_add_executor_job(_read_automations_yaml, path)
                items.append(config)
                await hass.async_add_executor_job(_write_automations_yaml, path, items)
            elif not op.ok:
                return _tool_error(op.error_text), "denied", "create_automation"
        await hass.services.async_call("automation", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("create_automation failed")
        return _tool_error("Failed to create automation. Check HA logs for details."), "invalid_request", "create_automation"

    await _record_version(
        data, token, resource_type="automation", resource_id=automation_id,
        action="create", before=None, after=config, alias=config.get("alias"),
    )
    return _tool_success(json.dumps(config, indent=2, default=str)), "allowed", "create_automation"


async def _tool_edit_automation(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: replace the config of an existing UI automation."""
    if effective_cap(token, "cap_automation_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "edit_automation"
    pre = await _automation_write_precheck(args, hass, "edit_automation", require_id=True)
    if pre is not None:
        return pre
    stale = await _entry_cas_conflict(
        args, hass, "automation", args.get("automation_id", "").strip(), "edit_automation",
    )
    if stale is not None:
        return stale
    blocked = await _gate(
        "cap_automation_write", token, hass, data,
        tool_name="edit_automation", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_edit_automation(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_automation(args, token, hass, data)


async def _execute_edit_automation(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    # HA async_validate_config_item validates automation IDs.
    automation_id = args.get("automation_id", "").strip()
    if not automation_id:
        return _tool_error("automation_id is required."), "invalid_request", "edit_automation"

    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "edit_automation"

    # Re-checked here (not just pre-gate) so a change made during the approval
    # window is caught before the write lands.
    stale = await _entry_cas_conflict(args, hass, "automation", automation_id, "edit_automation")
    if stale is not None:
        return stale

    config = {k: v for k, v in config.items() if k != "id"}
    config["id"] = automation_id

    invalid = await _config_validation_error(
        _validate_automation_config, hass, automation_id, config,
        tool_name="edit_automation", invalid_message=_AUTOMATION_INVALID_MESSAGE,
    )
    if invalid is not None:
        return invalid

    path = os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
    lock = _get_automation_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_edit, hass.config.config_dir, "automation",
                automation_id, config,
            )
            if op.fallback:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("automations.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "edit_automation"
                items = await hass.async_add_executor_job(_read_automations_yaml, path)
                idx = next((i for i, a in enumerate(items) if a.get("id") == automation_id), None)
                if idx is None:
                    return _tool_error(f"No automation found with id '{automation_id}'."), "denied", "edit_automation"
                before_cfg = items[idx]
                items[idx] = config
                await hass.async_add_executor_job(_write_automations_yaml, path, items)
            elif not op.ok:
                return _tool_error(op.error_text), "denied", "edit_automation"
            else:
                before_cfg = op.before
        await hass.services.async_call("automation", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("edit_automation failed")
        return _tool_error("Failed to edit automation. Check HA logs for details."), "invalid_request", "edit_automation"

    await _record_version(
        data, token, resource_type="automation", resource_id=automation_id,
        action="edit", before=before_cfg, after=config, alias=config.get("alias"),
    )
    return _tool_success(json.dumps(config, indent=2, default=str)), "allowed", "edit_automation"


async def _tool_delete_automation(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: permanently delete a UI automation."""
    blocked = await _gate(
        "cap_automation_write", token, hass, data,
        tool_name="delete_automation", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_automation(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_automation(args, token, hass, data)


async def _execute_delete_automation(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    automation_id = args.get("automation_id", "").strip()
    if not automation_id:
        return _tool_error("automation_id is required."), "invalid_request", "delete_automation"

    path = os.path.join(hass.config.config_dir, _AUTOMATION_YAML)
    lock = _get_automation_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_delete, hass.config.config_dir, "automation",
                automation_id,
            )
            if op.fallback:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("automations.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "delete_automation"
                items = await hass.async_add_executor_job(_read_automations_yaml, path)
                removed = next((a for a in items if a.get("id") == automation_id), None)
                filtered = [a for a in items if a.get("id") != automation_id]
                if len(filtered) == len(items):
                    return _tool_error(f"No automation found with id '{automation_id}'."), "denied", "delete_automation"
                await hass.async_add_executor_job(_write_automations_yaml, path, filtered)
            elif not op.ok:
                return _tool_error(op.error_text), "denied", "delete_automation"
            else:
                removed = op.before
        await hass.services.async_call("automation", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("delete_automation failed")
        return _tool_error("Failed to delete automation. Check HA logs for details."), "invalid_request", "delete_automation"

    await _record_version(
        data, token, resource_type="automation", resource_id=automation_id,
        action="delete", before=removed, after=None,
        alias=removed.get("alias") if isinstance(removed, dict) else None,
    )
    return _tool_success(f"Automation '{automation_id}' deleted successfully."), "allowed", "delete_automation"


async def _tool_list_scripts(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list accessible script.* entities with their editable id."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "list_scripts"
    items: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        entity_id = e["entity_id"]
        if not entity_id.startswith("script."):
            continue
        items.append({
            "entity_id": entity_id,
            "alias": e.get("attributes", {}).get("friendly_name"),
            # The script id is the object_id; there is no separate id attribute.
            "script_id": entity_id.split(".", 1)[1],
            "state": e.get("state"),
        })
    items.sort(key=lambda s: s["entity_id"])
    return _tool_success(json.dumps({"count": len(items), "scripts": items}, default=str)), "allowed", "list_scripts"


async def _tool_get_script(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read one script's current stored config, with a content_hash."""
    if effective_cap(token, "cap_script_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_script"
    script_id = str(args.get("script_id") or "").strip()
    if not script_id:
        return _tool_error("script_id is required."), "invalid_request", "get_script"
    config = await _read_config_entry(hass, "script", script_id)
    if config is None:
        return _tool_error(f"No script found with id '{script_id}'."), "not_found", f"script:{script_id}"
    return _config_read_response("script_id", script_id, config, f"script:{script_id}")


async def _script_write_precheck(args: dict, hass: HomeAssistant, tool_name: str) -> tuple[dict, str, str] | None:
    """Pre-gate validation for script writes; None means OK to proceed.

    Checks script_id format and config schema so a doomed request is rejected
    before a pending approval is created. The executor re-validates at apply
    time.
    """
    script_id = str(args.get("script_id") or "").strip()
    if not script_id:
        return _tool_error("script_id is required."), "invalid_request", tool_name
    if not _SCRIPT_ID_RE.match(script_id):
        return _tool_error("script_id must contain only lowercase letters, digits, and underscores."), "invalid_request", tool_name
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", tool_name
    return await _config_validation_error(
        _validate_script_config, hass, script_id, config,
        tool_name=tool_name, invalid_message=_SCRIPT_INVALID_MESSAGE,
    )


async def _tool_create_script(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a new script in scripts.yaml."""
    if effective_cap(token, "cap_script_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "create_script"
    pre = await _script_write_precheck(args, hass, "create_script")
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_script_write", token, hass, data,
        tool_name="create_script", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_create_script(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_script(args, token, hass, data)


async def _execute_create_script(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    script_id = args.get("script_id", "").strip()
    if not script_id:
        return _tool_error("script_id is required."), "invalid_request", "create_script"
    if not _SCRIPT_ID_RE.match(script_id):
        return _tool_error("script_id must contain only lowercase letters, digits, and underscores."), "invalid_request", "create_script"

    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "create_script"

    invalid = await _config_validation_error(
        _validate_script_config, hass, script_id, config,
        tool_name="create_script", invalid_message=_SCRIPT_INVALID_MESSAGE,
    )
    if invalid is not None:
        return invalid

    path = hass.config.path(_SCRIPT_CONFIG_PATH)
    lock = _get_script_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_create, hass.config.config_dir, "script",
                script_id, config,
            )
            if op.fallback:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("scripts.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "create_script"
                scripts = await hass.async_add_executor_job(_read_scripts_yaml, path)
                if script_id in scripts:
                    return _tool_error(f"A script with id '{script_id}' already exists. Use edit_script to update it."), "invalid_request", "create_script"
                scripts[script_id] = config
                await hass.async_add_executor_job(_write_scripts_yaml, path, scripts)
            elif not op.ok:
                outcome = "invalid_request" if op.error_kind == "already_exists" else "denied"
                return _tool_error(op.error_text), outcome, "create_script"
        await hass.services.async_call("script", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("create_script failed")
        return _tool_error("Failed to create script. Check HA logs for details."), "invalid_request", "create_script"

    await _record_version(
        data, token, resource_type="script", resource_id=script_id,
        action="create", before=None, after=config, alias=config.get("alias"),
    )
    return _tool_success(json.dumps({script_id: config}, indent=2, default=str)), "allowed", "create_script"


async def _tool_edit_script(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: replace the config of an existing script in scripts.yaml."""
    if effective_cap(token, "cap_script_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "edit_script"
    pre = await _script_write_precheck(args, hass, "edit_script")
    if pre is not None:
        return pre
    stale = await _entry_cas_conflict(
        args, hass, "script", str(args.get("script_id") or "").strip(), "edit_script",
    )
    if stale is not None:
        return stale
    blocked = await _gate(
        "cap_script_write", token, hass, data,
        tool_name="edit_script", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_edit_script(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_script(args, token, hass, data)


async def _execute_edit_script(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    script_id = args.get("script_id", "").strip()
    if not script_id:
        return _tool_error("script_id is required."), "invalid_request", "edit_script"
    if not _SCRIPT_ID_RE.match(script_id):
        return _tool_error("script_id must contain only lowercase letters, digits, and underscores."), "invalid_request", "edit_script"

    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "edit_script"

    # Re-checked here (not just pre-gate) so a change made during the approval
    # window is caught before the write lands.
    stale = await _entry_cas_conflict(args, hass, "script", script_id, "edit_script")
    if stale is not None:
        return stale

    invalid = await _config_validation_error(
        _validate_script_config, hass, script_id, config,
        tool_name="edit_script", invalid_message=_SCRIPT_INVALID_MESSAGE,
    )
    if invalid is not None:
        return invalid

    path = hass.config.path(_SCRIPT_CONFIG_PATH)
    lock = _get_script_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_edit, hass.config.config_dir, "script",
                script_id, config,
            )
            if op.fallback:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("scripts.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "edit_script"
                scripts = await hass.async_add_executor_job(_read_scripts_yaml, path)
                if script_id not in scripts:
                    return _tool_error(f"No script found with id '{script_id}'."), "denied", "edit_script"
                before_cfg = scripts[script_id]
                scripts[script_id] = config
                await hass.async_add_executor_job(_write_scripts_yaml, path, scripts)
            elif not op.ok:
                return _tool_error(op.error_text), "denied", "edit_script"
            else:
                before_cfg = op.before
        await hass.services.async_call("script", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("edit_script failed")
        return _tool_error("Failed to edit script. Check HA logs for details."), "invalid_request", "edit_script"

    await _record_version(
        data, token, resource_type="script", resource_id=script_id,
        action="edit", before=before_cfg, after=config, alias=config.get("alias"),
    )
    return _tool_success(json.dumps({script_id: config}, indent=2, default=str)), "allowed", "edit_script"


async def _tool_delete_script(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: permanently delete a script from scripts.yaml."""
    blocked = await _gate(
        "cap_script_write", token, hass, data,
        tool_name="delete_script", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_script(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_script(args, token, hass, data)


async def _execute_delete_script(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    script_id = args.get("script_id", "").strip()
    if not script_id:
        return _tool_error("script_id is required."), "invalid_request", "delete_script"
    if not _SCRIPT_ID_RE.match(script_id):
        return _tool_error("Invalid script ID format."), "invalid_request", "delete_script"

    path = hass.config.path(_SCRIPT_CONFIG_PATH)
    lock = _get_script_lock(hass)
    try:
        async with lock:
            op = await hass.async_add_executor_job(
                yaml_includes.perform_delete, hass.config.config_dir, "script",
                script_id,
            )
            if op.fallback:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("scripts.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "delete_script"
                scripts = await hass.async_add_executor_job(_read_scripts_yaml, path)
                if script_id not in scripts:
                    return _tool_error(f"No script found with id '{script_id}'."), "denied", "delete_script"
                before_cfg = scripts[script_id]
                del scripts[script_id]
                await hass.async_add_executor_job(_write_scripts_yaml, path, scripts)
            elif not op.ok:
                return _tool_error(op.error_text), "denied", "delete_script"
            else:
                before_cfg = op.before
        await hass.services.async_call("script", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("delete_script failed")
        return _tool_error("Failed to delete script. Check HA logs for details."), "invalid_request", "delete_script"

    await _record_version(
        data, token, resource_type="script", resource_id=script_id,
        action="delete", before=before_cfg, after=None,
        alias=before_cfg.get("alias") if isinstance(before_cfg, dict) else None,
    )
    return _tool_success(f"Script '{script_id}' deleted successfully."), "allowed", "delete_script"


# HA-COUPLING POINT. Up to HA 2026.6 hass.data[DATA_TRACE][key] was a plain
# LimitedSizeDict of run_id -> trace. HA 2026.7 replaced it with TraceBuckets,
# which holds two separately size-limited dicts: `runs` (the automation actually
# fired) and `not_triggered` (a trigger evaluated a relevant change and did NOT
# fire), so the latter can never evict the former. Both accessors below read
# either shape, because Phoenix supports HA 2024.5+. Re-verify on upgrades.
#
# Not-triggered traces are deliberately INCLUDED: "why did it not run" is the
# usual reason to read traces at all, and each trace self-reports via a
# not_triggered field in as_short_dict(), so the caller can tell them apart.


def _traces_for_automation(bucket: Any) -> list[Any]:
    """Every trace object stored for one automation, newest-shape or legacy."""
    all_traces = getattr(bucket, "all_traces", None)
    if callable(all_traces):
        return list(all_traces())
    return list(bucket.values())


def _trace_by_run_id(bucket: Any, run_id: str) -> Any | None:
    """One trace by run id, newest-shape or legacy. None when absent."""
    getter = getattr(bucket, "get", None)
    if callable(getter):  # legacy LimitedSizeDict, or the empty-dict default
        return getter(run_id)
    return bucket.runs.get(run_id) or bucket.not_triggered.get(run_id)


def _trace_summary(short: dict) -> dict:
    """Condense a trace short-dict to the fields that explain a run's outcome."""
    return {
        "run_id": short.get("run_id"),
        "state": short.get("state"),
        "script_execution": short.get("script_execution"),
        "last_step": short.get("last_step"),
        "error": short.get("error"),
        "timestamp": short.get("timestamp"),
    }


async def _tool_get_automation_traces(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: execution traces for an accessible automation."""
    if effective_cap(token, "cap_traces") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_automation_traces"

    raw = str(args.get("automation_id") or args.get("entity_id") or "").strip()
    if not raw:
        return _tool_error("Missing required argument: automation_id"), "invalid_request", "get_automation_traces"

    registry = er.async_get(hass)
    if raw.startswith("automation."):
        entity_id = raw
        entry = registry.async_get(entity_id)
        unique_id = entry.unique_id if entry is not None else None
    else:
        unique_id = raw
        entity_id = None
        for e in registry.entities.values():
            if e.domain == "automation" and e.unique_id == raw:
                entity_id = e.entity_id
                break

    # Scope: nonexistent and inaccessible look identical (no oracle).
    if entity_id is None or unique_id is None or resolve(entity_id, token, hass) not in (Permission.READ, Permission.WRITE):
        return _tool_error("Automation not found."), "not_found", raw

    from homeassistant.components.trace.const import DATA_TRACE  # noqa: PLC0415
    # An absent DATA_TRACE key means the trace component is not loaded at all, which
    # produces the same empty listing as an automation that has simply never run.
    trace_loaded = DATA_TRACE in hass.data
    runs: Any = hass.data.get(DATA_TRACE, {}).get(f"automation.{unique_id}", {})
    summary = bool(args.get("summary"))
    run_id = args.get("run_id")

    if run_id:
        trace = _trace_by_run_id(runs, run_id)
        if trace is None:
            return _tool_error("Trace run not found."), "not_found", raw
        run_body = _trace_summary(trace.as_short_dict()) if summary else trace.as_dict()
        return _tool_success(json.dumps(run_body, default=str)), "allowed", entity_id

    items = sorted(
        (t.as_short_dict() for t in _traces_for_automation(runs)),
        key=lambda d: d.get("timestamp", {}).get("start") or "",
        reverse=True,
    )
    if summary:
        items = [_trace_summary(d) for d in items]
    body: dict[str, Any] = {
        "automation_id": unique_id, "entity_id": entity_id, "count": len(items), "traces": items,
    }
    if not trace_loaded:
        body["warnings"] = ["The trace component is not loaded; trace history is unavailable."]
    return _tool_success(json.dumps(body, default=str)), "allowed", entity_id


# ---------------------------------------------------------------------------
# Scene CRUD (cap_scene_write) - mirrors the automation/script YAML pattern
# ---------------------------------------------------------------------------


def _scene_member_entities(config: Any) -> list[str]:
    ents = config.get("entities") if isinstance(config, dict) else None
    return list(ents.keys()) if isinstance(ents, dict) else []


def _unwritable_scene_members(config: Any, token: TokenRecord, hass: HomeAssistant) -> list[str]:
    """Scene member entities the token cannot WRITE (the scene will actuate them)."""
    return sorted(e for e in _scene_member_entities(config) if resolve(e, token, hass) != Permission.WRITE)


def _valid_scene_config(config: Any) -> bool:
    return (
        isinstance(config, dict)
        and isinstance(config.get("name"), str) and config["name"].strip() != ""
        and isinstance(config.get("entities"), dict) and len(config["entities"]) > 0
    )


async def _tool_list_scenes(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list accessible scene.* entities with their editable scene id."""
    if effective_cap(token, "cap_registry_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_scenes"
    scenes: list[dict] = []
    for e in filter_entities_for_token(hass.states.async_all(), token, hass):
        if not e["entity_id"].startswith("scene."):
            continue
        attrs = e.get("attributes", {})
        scenes.append({
            "entity_id": e["entity_id"],
            "name": attrs.get("friendly_name"),
            "scene_id": attrs.get("id"),
        })
    scenes.sort(key=lambda s: s["entity_id"])
    return _tool_success(json.dumps({"count": len(scenes), "scenes": scenes}, default=str)), "allowed", "list_scenes"


async def _tool_get_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read one scene's current stored config, with a content_hash.

    Applies edit_scene's member rule: a scene controlling entities this token
    cannot write is refused with the same message as a missing scene, so the
    scene id is not an existence oracle.
    """
    if effective_cap(token, "cap_scene_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_scene"
    scene_id = str(args.get("scene_id") or "").strip()
    if not scene_id:
        return _tool_error("scene_id is required."), "invalid_request", "get_scene"
    config = await _read_config_entry(hass, "scene", scene_id)
    if config is None or _unwritable_scene_members(config, token, hass):
        return _tool_error(
            f"No scene found with id '{scene_id}', or it controls entities outside your write scope."
        ), "denied", f"scene:{scene_id}"
    return _config_read_response("scene_id", scene_id, config, f"scene:{scene_id}")


async def _scene_write(
    config: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData, *, tool_name: str, scene_id: str, replace: bool
) -> tuple[dict, str, str]:
    """Shared create/edit body: validate, scope-check members, write scenes.yaml, reload."""
    if not _valid_scene_config(config):
        return _tool_error("config must include a non-empty 'name' and a non-empty 'entities' map."), "invalid_request", tool_name
    bad = _unwritable_scene_members(config, token, hass)
    if bad:
        return _tool_error("Scene references entities this token cannot write: " + ", ".join(bad)), "denied", tool_name

    config = {k: v for k, v in config.items() if k != "id"}
    config["id"] = scene_id
    path = hass.config.path(_SCENE_CONFIG_PATH)
    lock = _get_scene_lock(hass)
    try:
        async with lock:
            layout = await hass.async_add_executor_job(
                yaml_includes.resolve_domain_layout, hass.config.config_dir, "scene",
            )
            if layout is None:
                # configuration.yaml missing or unparseable: legacy hardcoded-file path.
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("scenes.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", tool_name
                items = await hass.async_add_executor_job(_read_scenes_yaml, path)
                if replace:
                    idx = next((i for i, s in enumerate(items) if isinstance(s, dict) and str(s.get("id")) == scene_id), None)
                    # The token must already own the scene it is replacing: it can only
                    # edit a scene whose CURRENT members are all WRITE-accessible. A
                    # missing scene and an out-of-scope one return the same error so the
                    # id is not an existence oracle.
                    if idx is None or _unwritable_scene_members(items[idx], token, hass):
                        return _tool_error(f"No scene found with id '{scene_id}', or it controls entities outside your write scope."), "denied", tool_name
                    before_cfg = items[idx]
                    items[idx] = config
                else:
                    before_cfg = None
                    items.append(config)
                await hass.async_add_executor_job(_write_scenes_yaml, path, items)
            elif layout.error:
                return _tool_error(layout.error), "denied", tool_name
            elif replace:
                # Routed edit: same oracle rule as the legacy path, checked on the
                # scene's CURRENT members before anything is written.
                current = await hass.async_add_executor_job(
                    yaml_includes.read_entry, hass.config.config_dir, "scene", scene_id,
                )
                if current is None or _unwritable_scene_members(current, token, hass):
                    return _tool_error(f"No scene found with id '{scene_id}', or it controls entities outside your write scope."), "denied", tool_name
                op = await hass.async_add_executor_job(
                    yaml_includes.perform_edit, hass.config.config_dir, "scene",
                    scene_id, config,
                )
                if not op.ok:
                    return _tool_error(op.error_text), "denied", tool_name
                before_cfg = op.before
            else:
                before_cfg = None
                op = await hass.async_add_executor_job(
                    yaml_includes.perform_create, hass.config.config_dir, "scene",
                    scene_id, config,
                )
                if not op.ok:
                    return _tool_error(op.error_text), "denied", tool_name
        await hass.services.async_call("scene", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("%s failed", tool_name)
        return _tool_error(f"Failed to {tool_name.replace('_', ' ')}. Check HA logs for details."), "invalid_request", tool_name
    await _record_version(
        data, token, resource_type="scene", resource_id=scene_id,
        action="edit" if replace else "create",
        before=before_cfg, after=config, alias=config.get("name"),
    )
    return _tool_success(json.dumps(config, indent=2, default=str)), "allowed", tool_name


def _scene_write_precheck(args: dict, tool_name: str, *, require_id: bool) -> tuple[dict, str, str] | None:
    """Pre-gate validation for scene writes; None means OK to proceed.

    Checks scene_id presence and config shape so a doomed request is rejected
    before a pending approval is created. The executor re-validates at apply
    time.
    """
    if require_id and not str(args.get("scene_id") or "").strip():
        return _tool_error("scene_id is required."), "invalid_request", tool_name
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", tool_name
    if not _valid_scene_config(config):
        return _tool_error("config must include a non-empty 'name' and a non-empty 'entities' map."), "invalid_request", tool_name
    return None


async def _tool_create_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a scene (Confirm-gated)."""
    if effective_cap(token, "cap_scene_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "create_scene"
    pre = _scene_write_precheck(args, "create_scene", require_id=False)
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_scene_write", token, hass, data,
        tool_name="create_scene", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_create_scene(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_scene(args, token, hass, data)


async def _execute_create_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "create_scene"
    # Restore of a deleted scene recreates it under its original id; a fresh
    # create mints a new one.
    scene_id = str(args.get("scene_id") or "").strip() or "phoenix_mcp_" + uuid.uuid4().hex[:16]
    return await _scene_write(
        config, token, hass, data, tool_name="create_scene",
        scene_id=scene_id, replace=False,
    )


async def _tool_edit_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: edit a scene (Confirm-gated)."""
    if effective_cap(token, "cap_scene_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "edit_scene"
    pre = _scene_write_precheck(args, "edit_scene", require_id=True)
    if pre is not None:
        return pre
    stale = await _entry_cas_conflict(
        args, hass, "scene", str(args.get("scene_id") or "").strip(), "edit_scene",
    )
    if stale is not None:
        return stale
    blocked = await _gate(
        "cap_scene_write", token, hass, data,
        tool_name="edit_scene", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_edit_scene(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_scene(args, token, hass, data)


async def _execute_edit_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    scene_id = str(args.get("scene_id") or "").strip()
    config = args.get("config")
    if not scene_id:
        return _tool_error("scene_id is required."), "invalid_request", "edit_scene"
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "edit_scene"
    # Re-checked here (not just pre-gate) so a change made during the approval
    # window is caught before the write lands.
    stale = await _entry_cas_conflict(args, hass, "scene", scene_id, "edit_scene")
    if stale is not None:
        return stale
    return await _scene_write(config, token, hass, data, tool_name="edit_scene", scene_id=scene_id, replace=True)


async def _tool_delete_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete a scene (Confirm-gated)."""
    blocked = await _gate(
        "cap_scene_write", token, hass, data,
        tool_name="delete_scene", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_scene(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_scene(args, token, hass, data)


async def _execute_delete_scene(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    scene_id = str(args.get("scene_id") or "").strip()
    if not scene_id:
        return _tool_error("scene_id is required."), "invalid_request", "delete_scene"
    path = hass.config.path(_SCENE_CONFIG_PATH)
    lock = _get_scene_lock(hass)
    try:
        async with lock:
            layout = await hass.async_add_executor_job(
                yaml_includes.resolve_domain_layout, hass.config.config_dir, "scene",
            )
            if layout is None:
                if await hass.async_add_executor_job(_yaml_file_has_includes, path):
                    return _tool_error("scenes.yaml uses !include directives. Phoenix MCP cannot safely edit it without destroying the include structure."), "denied", "delete_scene"
                items = await hass.async_add_executor_job(_read_scenes_yaml, path)
                existing = next((s for s in items if isinstance(s, dict) and str(s.get("id")) == scene_id), None)
                # The token may only delete a scene whose current members are all
                # WRITE-accessible. Missing and out-of-scope return the same error.
                if existing is None or _unwritable_scene_members(existing, token, hass):
                    return _tool_error(f"No scene found with id '{scene_id}', or it controls entities outside your write scope."), "denied", "delete_scene"
                filtered = [s for s in items if not (isinstance(s, dict) and str(s.get("id")) == scene_id)]
                await hass.async_add_executor_job(_write_scenes_yaml, path, filtered)
            elif layout.error:
                return _tool_error(layout.error), "denied", "delete_scene"
            else:
                existing = await hass.async_add_executor_job(
                    yaml_includes.read_entry, hass.config.config_dir, "scene", scene_id,
                )
                if existing is None or _unwritable_scene_members(existing, token, hass):
                    return _tool_error(f"No scene found with id '{scene_id}', or it controls entities outside your write scope."), "denied", "delete_scene"
                op = await hass.async_add_executor_job(
                    yaml_includes.perform_delete, hass.config.config_dir, "scene",
                    scene_id,
                )
                if not op.ok:
                    return _tool_error(op.error_text), "denied", "delete_scene"
                existing = op.before
        await hass.services.async_call("scene", "reload", blocking=True)
        # Reloading scenes.yaml does not remove the scene's entity-registry entry.
        registry = er.async_get(hass)
        for entry in list(registry.entities.values()):
            if entry.domain == "scene" and entry.unique_id == scene_id:
                registry.async_remove(entry.entity_id)
                break
    except Exception:
        _LOGGER.exception("delete_scene failed")
        return _tool_error("Failed to delete scene. Check HA logs for details."), "invalid_request", "delete_scene"
    await _record_version(
        data, token, resource_type="scene", resource_id=scene_id,
        action="delete", before=existing, after=None,
        alias=existing.get("name") if isinstance(existing, dict) else None,
    )
    return _tool_success(f"Scene '{scene_id}' deleted successfully."), "allowed", "delete_scene"


def _build_diff_create_automation(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    config = dict_arg(args.get("config"))
    return {
        "kind": "config_diff",
        **_summary("create_automation", alias=config.get("alias", "<no alias>")),
        "target": {"type": "automation", "id": None, "label": config.get("alias")},
        "before": None,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"alias": config.get("alias"), "mode": config.get("mode")},
    }


async def _build_diff_edit_automation(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    automation_id = (args.get("automation_id") or "").strip()
    config = dict_arg(args.get("config"))
    before = await _entry_before_json(hass, "automation", automation_id)
    return {
        "kind": "yaml_diff",
        **_summary("edit_automation", automation_id=automation_id),
        "target": {"type": "automation", "id": automation_id, "label": config.get("alias")},
        "before": before,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"alias": config.get("alias"), "mode": config.get("mode")},
    }


async def _build_diff_delete_automation(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    automation_id = str_arg(args.get("automation_id")).strip()
    before = await _entry_before_json(hass, "automation", automation_id)
    return {
        "kind": "system_action",
        **_summary("delete_automation", automation_id=automation_id),
        "target": {"type": "automation", "id": automation_id, "label": automation_id},
        "before": before,
        "preview": {"warning": "This automation will be removed permanently."},
    }


def _yaml_entry(hass: HomeAssistant, domain: str, entry_id: str) -> dict | None:
    """Read one automation/script/scene entry's stored config, or None.

    Fail-quiet on purpose: this only ever feeds a diff's `before` side, and an
    unreadable entry must not stop an approval from being raised for the write
    the caller actually asked for. The executor re-reads and re-validates for
    real at apply time.
    """
    try:
        return yaml_includes.read_entry(hass.config.config_dir, domain, entry_id)
    except Exception:  # noqa: BLE001 - diagnostic only
        return None


async def _entry_before_json(
    hass: HomeAssistant, domain: str, entry_id: str, *, wrap_key: str | None = None
) -> str | None:
    """Truncated JSON of an entry's current config for a diff's `before`, or None.

    wrap_key reproduces the shape each domain's `after` side uses, so both panes
    of the diff stay comparable: scripts render as {script_id: config}, while
    automations and scenes render the config bare.

    Fail-quiet end to end, including the executor dispatch and the serialization,
    not just the read: a diff is advisory, and nothing here may stop the write it
    describes from being gated.
    """
    try:
        current = await hass.async_add_executor_job(_yaml_entry, hass, domain, entry_id)
        if current is None:
            return None
        payload: Any = {wrap_key: current} if wrap_key is not None else current
        return _truncate(json.dumps(payload, indent=2, default=str))
    except Exception:  # noqa: BLE001 - diagnostic only
        return None


def _build_diff_create_scene(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    config = dict_arg(args.get("config"))
    members = _scene_member_entities(config)
    return {
        "kind": "config_diff",
        **_summary("create_scene", name=config.get("name", "<no name>")),
        "target": {"type": "scene", "id": None, "label": config.get("name")},
        "before": None,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"name": config.get("name"), "entities": members,
                    "unwritable_entities": _unwritable_scene_members(config, token, hass)},
    }


async def _build_diff_edit_scene(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    scene_id = str(args.get("scene_id") or "").strip()
    config = dict_arg(args.get("config"))
    before = await _entry_before_json(hass, "scene", scene_id)
    return {
        "kind": "yaml_diff",
        **_summary("edit_scene", scene_id=scene_id),
        "target": {"type": "scene", "id": scene_id, "label": config.get("name")},
        "before": before,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"name": config.get("name"), "entities": _scene_member_entities(config),
                    "unwritable_entities": _unwritable_scene_members(config, token, hass)},
    }


async def _build_diff_delete_scene(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    scene_id = str(args.get("scene_id") or "").strip()
    before = await _entry_before_json(hass, "scene", scene_id)
    return {
        "kind": "system_action",
        **_summary("delete_scene", scene_id=scene_id),
        "target": {"type": "scene", "id": scene_id, "label": scene_id},
        "before": before,
        "preview": {"warning": "This scene will be removed permanently."},
    }


def _build_diff_create_script(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    script_id = (args.get("script_id") or "").strip()
    config = dict_arg(args.get("config"))
    return {
        "kind": "config_diff",
        **_summary("create_script", script_id=script_id),
        "target": {"type": "script", "id": script_id, "label": config.get("alias")},
        "before": None,
        "after": _truncate(json.dumps({script_id: config}, indent=2, default=str)),
        "preview": {"alias": config.get("alias"), "mode": config.get("mode")},
    }


async def _build_diff_edit_script(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    script_id = (args.get("script_id") or "").strip()
    config = dict_arg(args.get("config"))
    before = await _entry_before_json(hass, "script", script_id, wrap_key=script_id)
    return {
        "kind": "yaml_diff",
        **_summary("edit_script", script_id=script_id),
        "target": {"type": "script", "id": script_id, "label": config.get("alias")},
        "before": before,
        "after": _truncate(json.dumps({script_id: config}, indent=2, default=str)),
        "preview": {"alias": config.get("alias"), "mode": config.get("mode")},
    }


async def _build_diff_delete_script(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    script_id = str_arg(args.get("script_id")).strip()
    before = await _entry_before_json(hass, "script", script_id, wrap_key=script_id)
    return {
        "kind": "system_action",
        **_summary("delete_script", script_id=script_id),
        "target": {"type": "script", "id": script_id, "label": script_id},
        "before": before,
        "preview": {"warning": "This script will be removed permanently."},
    }
