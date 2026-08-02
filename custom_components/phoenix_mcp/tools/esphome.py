"""ESPHome tools: device YAML, Device Builder reads, firmware jobs and status.

The whole ESPHome surface in one module: get/set/delete device YAML behind the
config/esphome path jail and the credential masking in esphome_yaml.py, the four
Device Builder lookups, the firmware job tier (compile / install / poll / cancel
/ device logs / backtrace) plus the three lifecycle tools, and the fleet status
read. Their capability split is deliberate and documented per tool: compiling
rides cap_esphome_yaml, only putting an image on hardware needs
cap_esphome_flash.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""


import asyncio
import json
import logging
import os
import re
from typing import Any, NamedTuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntryState

from ..const import (
    AI_TASK_CLIENT_IP,
    ASSIST_CLIENT_IP,
    CAP_DENY,
    ESPHOME_BUILDER_DECODE_TIMEOUT_SECONDS,
    ESPHOME_BUILDER_JOB_LOG_TIMEOUT_SECONDS,
    ESPHOME_BUILDER_JOB_OUTPUT_MAX_CHARS,
    ESPHOME_BUILDER_JOB_POLL_SECONDS,
    ESPHOME_BUILDER_JOB_PROGRESS_MAX_CHARS,
    ESPHOME_BUILDER_JOB_WAIT_HEADLESS_SECONDS,
    ESPHOME_BUILDER_JOB_WAIT_SECONDS,
    ESPHOME_BUILDER_LOG_CAPTURE_MAX_SECONDS,
    ESPHOME_BUILDER_LOG_CAPTURE_SECONDS,
    ESPHOME_BUILDER_MAX_BACKTRACE_LINES,
    ESPHOME_BUILDER_MAX_COMPONENT_IDS,
    ESPHOME_BUILDER_PAGE_LIMIT,
    ESPHOME_BUILDER_PAGE_LIMIT_MAX,
    ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
    ESPHOME_BUILDER_VALIDATE_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    VOICE_AGENT_CLIENT_IP,
)
from ..data import PhoenixData
from ..mesa import entity_control_mode
from ..helpers import diff_summary_fields as _summary, content_hash, effective_cap
from ..policy_engine import Permission, esphome_entry_for_entity, esphome_entry_writable, resolve
from ..token_store import TokenRecord
from ..esphome_builder import (
    BuilderAuthRequired,
    BuilderCommandError,
    BuilderError,
    async_builder_capture,
    async_builder_command,
    collect_output_lines,
    version_mismatch_note,
)
from ..esphome_yaml import (
    EsphomeSecretViolation,
    esphome_rel_path_ok,
    inline_secret_values,
    redact_esphome_text,
    scrub_secret_values,
    secret_keys_from_text,
    secret_values_from_text,
    splice_esphome_text,
)
from .. import yaml_includes

from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _gate,
    _read_text_capped,
    _record_version,
    _restore_ctx,
    _set_progress_status,
    _text_file_cas_conflict,
    _tool_error,
    _tool_success,
    _truncate,
    _usable_path_arg,
    _version_content_payload,
    _write_text_atomic,
)

_LOGGER = logging.getLogger(__name__)


_ESPHOME_DOMAIN = "esphome"


# aioesphomeapi DeviceInfo fields worth surfacing. mac_address and
# bluetooth_mac_address are deliberately ABSENT: HA's own ESPHome diagnostics
# redact both, and Phoenix MCP withholds network identity from agents elsewhere.
_ESPHOME_DEVICE_INFO_FIELDS = (
    "esphome_version",
    "compilation_time",
    "manufacturer",
    "model",
    "project_name",
    "project_version",
    "suggested_area",
    "has_deep_sleep",
    "webserver_port",
    "uses_password",
    "api_encryption_supported",
)


def _esphome_dashboard(hass: HomeAssistant) -> Any:
    """Return the ESPHome Device Builder coordinator, or None when unavailable.

    Lazy-imported so Phoenix MCP never hard-depends on the esphome integration
    being installed. Returns None when the integration is absent, when the
    dashboard manager has not been set up yet, or when no Device Builder add-on
    is configured. Reading HA's own in-process coordinator is deliberate: the
    add-on's HTTP API is never called, so no credential-resolving endpoint
    (json-config, get_encryption_key) is reachable from here.
    """
    try:
        from homeassistant.components.esphome.dashboard import (  # noqa: PLC0415
            async_get_dashboard,
        )
    except ImportError:
        return None
    try:
        return async_get_dashboard(hass)
    except Exception:  # noqa: BLE001 - manager may not be set up yet
        return None


class EsphomeAvailability(NamedTuple):
    """What ESPHome surfaces exist on this system.

    integration   - the esphome integration is set up (devices may exist).
    builder       - the Device Builder add-on is configured and known to HA.
    builder_live  - the coordinator's last refresh of it succeeded.
    """

    integration: bool
    builder: bool
    builder_live: bool


def esphome_availability(hass: HomeAssistant) -> EsphomeAvailability:
    """Report which ESPHome surfaces are present, for tool gating and the panel.

    Announcement gating keys on CONFIGURED state, never on `builder_live`: an MCP
    client caches its tool list and this transport cannot push a list-changed
    notification, so a tool list that tracked a stopped add-on would flap
    mid-session. A configured-but-unreachable add-on keeps its tools announced
    and fails loudly at call time instead. `builder_live` exists for the admin
    panel, which re-reads it on every load.

    Fails OPEN. Hiding a tool is a usability affordance, not a security control:
    the capability gate and the call-time checks run regardless, so an unknown
    host announces everything rather than silently losing a tool surface.
    """
    if hass is None:
        return EsphomeAvailability(True, True, True)
    try:
        integration = _ESPHOME_DOMAIN in hass.config.components or bool(
            hass.config_entries.async_entries(_ESPHOME_DOMAIN)
        )
    except Exception:  # noqa: BLE001 - never let a probe hide the tool surface
        integration = True
    dashboard = _esphome_dashboard(hass)
    builder = dashboard is not None
    return EsphomeAvailability(
        integration=integration,
        builder=builder,
        builder_live=builder and bool(getattr(dashboard, "last_update_success", False)),
    )


def _esphome_actions_for_entity(
    hass: HomeAssistant, token: TokenRecord, entity_id: str, writable: bool
) -> list[dict]:
    """The owning ESPHome device's user-defined actions, as find_available_actions rows.

    Availability follows the device's write scope, matching how
    _execute_call_service authorizes them. `args` is included because these
    schemas are device-defined: without the signature an agent has no way to
    learn what a device's action expects.
    """
    entry = esphome_entry_for_entity(hass, entity_id)
    if entry is None:
        return []
    runtime = getattr(entry, "runtime_data", None)
    device_info = getattr(runtime, "device_info", None)
    name = getattr(device_info, "name", None)
    if not name:
        return []

    available = writable or esphome_entry_writable(hass, entry, token)
    prefix = name.replace("-", "_")
    rows: list[dict] = []
    for svc in (getattr(runtime, "services", None) or {}).values():
        action = getattr(svc, "name", None)
        if not action:
            continue
        row: dict = {
            "service": f"{_ESPHOME_DOMAIN}.{prefix}_{action}",
            "available": available,
            "args": [
                {"name": getattr(arg, "name", None), "type": _esphome_arg_type(arg)}
                for arg in (getattr(svc, "args", None) or [])
            ],
        }
        if not available:
            row["reason"] = "read-only access to this device"
        rows.append(row)
    rows.sort(key=lambda r: r["service"])
    return rows


def _esphome_action_signature(entry: Any, service: str) -> list[dict]:
    """Declared argument signature for one of an entry's user-defined actions."""
    runtime = getattr(entry, "runtime_data", None)
    device_info = getattr(runtime, "device_info", None)
    name = getattr(device_info, "name", None) or ""
    action = service[len(f"{name.replace('-', '_')}_"):]
    for svc in (getattr(runtime, "services", None) or {}).values():
        if getattr(svc, "name", None) == action:
            return [
                {"name": getattr(arg, "name", None), "type": _esphome_arg_type(arg)}
                for arg in (getattr(svc, "args", None) or [])
            ]
    return []


def _esphome_arg_type(arg: Any) -> str | None:
    """Render a user-service argument type as a plain lowercase string."""
    arg_type = getattr(arg, "type", None)
    if arg_type is None:
        return None
    return str(getattr(arg_type, "name", arg_type)).lower()


def _esphome_declared_actions(hass: HomeAssistant, device_info: Any, runtime: Any) -> list[dict]:
    """List a device's user-defined API actions and their HA service names.

    Enumerated from the entry's OWN runtime services, never by scanning
    hass.services: the esphome integration does not remove its dynamically
    registered services on disconnect or unload, so a registry scan would report
    actions belonging to devices that are gone. `registered` surfaces that drift
    instead of hiding it.
    """
    services = getattr(runtime, "services", None) or {}
    prefix = (getattr(device_info, "name", None) or "").replace("-", "_")
    actions: list[dict] = []
    for service in services.values():
        name = getattr(service, "name", None)
        if not name:
            continue
        ha_service = f"{prefix}_{name}"
        actions.append({
            "name": name,
            "ha_service": f"{_ESPHOME_DOMAIN}.{ha_service}",
            "registered": bool(hass.services.has_service(_ESPHOME_DOMAIN, ha_service)),
            "args": [
                {"name": getattr(arg, "name", None), "type": _esphome_arg_type(arg)}
                for arg in (getattr(service, "args", None) or [])
            ],
        })
    actions.sort(key=lambda a: a["name"])
    return actions


def _esphome_dashboard_detail(configured: dict, device_info: Any) -> dict:
    """Project a Device Builder ConfiguredDevice entry plus update status.

    update_available mirrors how HA's own ESPHome update entity decides: the
    version running on the device against the version the Device Builder would
    build now. The device's `address` is deliberately omitted; it is LAN topology,
    which Phoenix MCP withholds from agents on every other surface.
    """
    installed = getattr(device_info, "esphome_version", None)
    current = configured.get("current_version")
    detail: dict = {
        "configuration": configured.get("configuration"),
        "current_version": current,
        "deployed_version": configured.get("deployed_version"),
        "target_platform": configured.get("target_platform"),
    }
    if installed and current:
        detail["update_available"] = installed != current
    return detail


def _esphome_runtime_detail(
    hass: HomeAssistant, entry: Any, record: dict, dash_data: dict, mapped_names: set[str]
) -> None:
    """Fill in live runtime fields for a LOADED ESPHome config entry.

    Reads entry.runtime_data by duck typing rather than importing the esphome
    integration's RuntimeEntryData, so Phoenix MCP never pulls aioesphomeapi into
    its own import graph (it is an integration requirement, not a Phoenix one).
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    record["available"] = bool(getattr(runtime, "available", False))
    record["expected_disconnect"] = bool(getattr(runtime, "expected_disconnect", False))

    device_info = getattr(runtime, "device_info", None)
    if device_info is None:
        return
    name = getattr(device_info, "name", None)
    record["name"] = name
    record["friendly_name"] = getattr(device_info, "friendly_name", None)
    for field in _ESPHOME_DEVICE_INFO_FIELDS:
        value = getattr(device_info, field, None)
        if value is not None and value != "":
            record[field] = value

    bt = getattr(runtime, "bluetooth_device", None)
    if bt is not None:
        record["bluetooth_proxy"] = {
            "available": bool(getattr(bt, "available", False)),
            "connections_free": getattr(bt, "ble_connections_free", None),
            "connections_limit": getattr(bt, "ble_connections_limit", None),
        }

    actions = _esphome_declared_actions(hass, device_info, runtime)
    if actions:
        record["actions"] = actions

    if name and name in dash_data:
        mapped_names.add(name)
        record["device_builder"] = _esphome_dashboard_detail(dash_data[name], device_info)


async def _tool_get_esphome_overview(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: status of the ESPHome devices this token can see."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_esphome_overview"

    warnings: list[str] = []
    dashboard = _esphome_dashboard(hass)
    dash_data: dict = {}
    if dashboard is None:
        warnings.append(
            "The ESPHome Device Builder is not available; configuration filenames "
            "and update status are omitted."
        )
    else:
        dash_data = getattr(dashboard, "data", None) or {}
        if not getattr(dashboard, "last_update_success", True):
            warnings.append(
                "The ESPHome Device Builder was unreachable at its last refresh; "
                "version and configuration data may be stale."
            )

    registry = er.async_get(hass)
    devices: list[dict] = []
    mapped_names: set[str] = set()
    for entry in hass.config_entries.async_entries(_ESPHOME_DOMAIN):
        # Scope on the entity REGISTRY rather than live states so a device whose
        # entry failed to load is still counted against the token's tree; an
        # unloaded entry publishes no states and would otherwise vanish from the
        # fleet view precisely when it is most worth reporting.
        visible = sum(
            1
            for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
            if resolve(reg_entry.entity_id, token, hass) in (Permission.READ, Permission.WRITE)
        )
        if not visible:
            continue

        loaded = entry.state is ConfigEntryState.LOADED
        record: dict = {
            "title": entry.title,
            "state": str(entry.state),
            "loaded": loaded,
            "accessible_entity_count": visible,
        }
        # runtime_data is DELETED on unload, so reading it on a non-loaded entry
        # raises AttributeError. entry.data and entry.unique_id are never read at
        # all: they carry the API encryption key and the device MAC.
        if loaded:
            _esphome_runtime_detail(hass, entry, record, dash_data, mapped_names)
        devices.append(record)

    devices.sort(key=lambda d: ((d.get("name") or d.get("title") or "").lower()))
    body: dict = {"count": len(devices), "devices": devices}

    # Count only, never names: a Device Builder configuration whose device this
    # token cannot see would otherwise make the add-on an enumeration oracle.
    unmapped = sum(1 for name in dash_data if name not in mapped_names)
    if unmapped:
        body["unmapped_configurations"] = unmapped
    if warnings:
        body["warnings"] = warnings
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_esphome_overview"


_ESPHOME_DIR = "esphome"


_ESPHOME_PATH_REFUSED = (
    "Only .yaml and .yml files directly inside the ESPHome configuration directory "
    "can be read or written; secrets.yaml, the archive folder, and hidden files are "
    "excluded."
)


# Nonexistent and out-of-scope are byte-identical (rule 12): the difference would
# otherwise reveal which devices exist outside this token's tree.
_ESPHOME_NOT_FOUND = "ESPHome configuration file not found."


def _resolve_esphome_path(hass: HomeAssistant, file_arg: Any) -> tuple[str, str] | None:
    """Resolve a device-YAML argument to (realpath, esphome-relative path).

    Returns None when refused. The structural rules run on BOTH the caller's
    argument and the realpath-resolved result, so a symlink cannot rename its way
    past the secrets.yaml or archive rule. Rooted at config/esphome and
    deliberately NOT part of FILESYSTEM_ALLOWED_DIRS: widening cap_filesystem
    must never reach device YAML, which embeds C++ lambdas and credentials.
    """
    if _usable_path_arg(file_arg) is None:
        return None
    rel_arg = file_arg.strip()
    if os.path.isabs(rel_arg) or not esphome_rel_path_ok(rel_arg):
        return None
    root = os.path.realpath(os.path.join(hass.config.config_dir, _ESPHOME_DIR))
    candidate = os.path.realpath(os.path.join(root, rel_arg))
    if not candidate.startswith(root + os.sep):
        return None
    rel = os.path.relpath(candidate, root)
    if not esphome_rel_path_ok(rel):
        return None
    return candidate, rel


def _read_esphome_secrets(hass: HomeAssistant) -> tuple[set[str], set[str] | None]:
    """Read the ESPHome secrets.yaml INTERNALLY: (values, key names).

    Never returned through any tool in any form; values feed the layer-3
    redaction cross-check and key names validate !secret references. An
    unreadable file degrades to (empty values, None keys) so redaction falls back
    to layers 1 and 2 and !secret validation is skipped, rather than failing
    every read and write.
    """
    path = os.path.join(hass.config.config_dir, _ESPHOME_DIR, yaml_includes.SECRETS_YAML)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return set(), None
    return secret_values_from_text(text), secret_keys_from_text(text)


def _esphome_device_for_file(hass: HomeAssistant, rel: str) -> Any | None:
    """The LOADED ESPHome config entry whose device this file configures, or None.

    Prefers the Device Builder's own configuration filename, falling back to a
    device-name stem match when the add-on is not available. A file that maps to
    no live device is an orphan (a config authored but never adopted), which the
    capability alone governs since nothing running is affected.
    """
    dashboard = _esphome_dashboard(hass)
    dash_data = getattr(dashboard, "data", None) or {} if dashboard is not None else {}
    stem = os.path.splitext(os.path.basename(rel))[0]
    for entry in hass.config_entries.async_entries(_ESPHOME_DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        name = getattr(getattr(entry, "runtime_data", None), "device_info", None)
        name = getattr(name, "name", None)
        if not name:
            continue
        configured = dash_data.get(name) or {}
        if configured.get("configuration") == rel or name == stem:
            return entry
    return None


def _esphome_file_in_scope(hass: HomeAssistant, token: TokenRecord, rel: str, *, write: bool) -> bool:
    """Whether the token may read (or write) the device YAML at rel.

    A file mapped to a live device follows that device's entity scope; an orphan
    file follows the capability alone.
    """
    entry = _esphome_device_for_file(hass, rel)
    if entry is None:
        return True
    if write:
        return esphome_entry_writable(hass, entry, token)
    registry = er.async_get(hass)
    return any(
        resolve(reg_entry.entity_id, token, hass) in (Permission.READ, Permission.WRITE)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
    )


def _esphome_listing(hass: HomeAssistant, token: TokenRecord) -> list[dict]:
    """Device YAML files this token may read, with their mapped device."""
    root = os.path.join(hass.config.config_dir, _ESPHOME_DIR)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    rows: list[dict] = []
    for name in names:
        if not esphome_rel_path_ok(name):
            continue
        full = os.path.join(root, name)
        if not os.path.isfile(full):
            continue
        if not _esphome_file_in_scope(hass, token, name, write=False):
            continue
        entry = _esphome_device_for_file(hass, name)
        device = getattr(getattr(entry, "runtime_data", None), "device_info", None)
        rows.append({
            "file": name,
            "bytes": os.path.getsize(full),
            "device": getattr(device, "name", None),
        })
    rows.sort(key=lambda r: r["file"])
    return rows


async def _tool_get_esphome_yaml(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list ESPHome device YAML files, or read one with credentials masked."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_yaml"

    file_arg = args.get("file")
    secret_values, secret_keys = await hass.async_add_executor_job(_read_esphome_secrets, hass)

    if file_arg is None or (isinstance(file_arg, str) and not file_arg.strip()):
        rows = await hass.async_add_executor_job(_esphome_listing, hass, token)
        body: dict = {"count": len(rows), "files": rows}
        if secret_keys is not None:
            body["defined_secrets"] = sorted(secret_keys)
        if _esphome_dashboard(hass) is None:
            body["warnings"] = [
                "The ESPHome Device Builder is not available; files are matched to "
                "devices by name only."
            ]
        return _tool_success(json.dumps(body, default=str)), "allowed", "get_esphome_yaml"

    resolved = _resolve_esphome_path(hass, file_arg)
    if resolved is None:
        return _tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", "get_esphome_yaml"
    path, rel = resolved
    if not _esphome_file_in_scope(hass, token, rel, write=False):
        return _tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"

    try:
        raw = await hass.async_add_executor_job(_read_text_capped, path)
    except FileNotFoundError:
        return _tool_error(_ESPHOME_NOT_FOUND), "not_found", f"esphome:{rel}"
    except (OSError, ValueError):
        return _tool_error("Failed to read the ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    try:
        redacted, redacted_paths = await hass.async_add_executor_job(
            redact_esphome_text, raw, secret_values
        )
    except yaml_includes.YamlParseError as err:
        # Fail closed: a file whose credentials cannot be located must not be
        # returned, since inline credentials are the normal case here.
        return (
            _tool_error(f"The file could not be parsed, so its credentials cannot be masked: {err}"),
            "invalid_request", f"esphome:{rel}",
        )

    entry = _esphome_device_for_file(hass, rel)
    device_info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
    body = {
        "file": rel,
        "content": redacted,
        # Hash of the RAW bytes on disk, so compare-and-swap still works even
        # though the caller only ever sees the masked text.
        "content_hash": content_hash(raw),
        "redacted_paths": redacted_paths,
        "device": getattr(device_info, "name", None),
    }
    if secret_keys is not None:
        body["defined_secrets"] = sorted(secret_keys)
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


def _esphome_write_precheck(args: dict) -> tuple[dict, str, str] | None:
    """Structural checks that run before the gate; None means OK to proceed."""
    content = args.get("content")
    if not isinstance(content, str):
        return _tool_error("content must be a string."), "invalid_request", "set_esphome_yaml"
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _tool_error("content exceeds the maximum file size."), "invalid_request", "set_esphome_yaml"
    return None


def _esphome_prepare_write(
    hass: HomeAssistant, args: dict, rel: str, path: str, restoring: bool = False
) -> tuple[str, str, bool]:
    """Validate the proposed content and return (raw_to_write, before, existed).

    Runs the structural parse and the credential splice together, because both
    need the on-disk text. Raises YamlParseError or EsphomeSecretViolation.

    `restoring` must be passed in rather than read from _restore_ctx here: this
    runs via async_add_executor_job, and a ContextVar is not propagated into the
    worker thread, so reading it here would silently always see the default.
    """
    existed = os.path.exists(path)
    before = _read_text_capped(path) if existed else ""
    secret_values, secret_keys = _read_esphome_secrets(hass)
    # Structural only: real validation is a Device Builder compile, which this
    # tier deliberately does not reach.
    yaml_includes.load_tagged_lenient(args["content"])
    raw = splice_esphome_text(
        args["content"], before, secret_values, secret_keys,
        allow_literal_credentials=restoring,
    )
    return raw, before, existed


async def _build_diff_set_esphome_yaml(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Approval diff for a device-YAML write, with both sides masked."""
    resolved = _resolve_esphome_path(hass, args.get("file"))
    rel = resolved[1] if resolved else str(args.get("file"))
    before_masked = ""
    if resolved is not None:
        try:
            secret_values, _ = await hass.async_add_executor_job(_read_esphome_secrets, hass)
            raw = await hass.async_add_executor_job(_read_text_capped, resolved[0])
            before_masked, _ = await hass.async_add_executor_job(
                redact_esphome_text, raw, secret_values
            )
        except Exception:  # noqa: BLE001 - diff builders are best-effort
            before_masked = ""

    entry = _esphome_device_for_file(hass, rel) if resolved else None
    device_info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
    device = getattr(device_info, "name", None)
    count = 0
    if entry is not None:
        count = len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))

    fields = (
        _summary("set_esphome_yaml.device", rel=rel, device=device, count=count)
        if device else _summary("set_esphome_yaml", rel=rel)
    )
    return {
        "kind": "esphome_yaml",
        **fields,
        "before": _truncate(before_masked),
        "after": _truncate(str(args.get("content", ""))),
        "preview": {
            "file": rel,
            "device": device,
            # The write lands on disk only. Until a person installs from the
            # Device Builder, the device keeps running its current firmware.
            "flashes_device": False,
        },
    }


async def _tool_set_esphome_yaml(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: write one ESPHome device YAML file (Confirm-eligible)."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_esphome_yaml"

    pre = _esphome_write_precheck(args)
    if pre is not None:
        return pre

    resolved = _resolve_esphome_path(hass, args.get("file"))
    if resolved is None:
        return _tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", "set_esphome_yaml"
    path, rel = resolved
    if not _esphome_file_in_scope(hass, token, rel, write=True):
        return _tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"

    # Parse and splice BEFORE the gate so a doomed or credential-violating write
    # never becomes a pending approval an admin has to reject (rule 29).
    try:
        await hass.async_add_executor_job(_esphome_prepare_write, hass, args, rel, path)
    except yaml_includes.YamlParseError as err:
        return _tool_error(f"The content is not valid YAML: {err}"), "invalid_request", f"esphome:{rel}"
    except EsphomeSecretViolation as err:
        return _tool_error(str(err)), "invalid_request", f"esphome:{rel}"
    except (OSError, ValueError):
        return _tool_error("Failed to read the existing ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    conflict = await _text_file_cas_conflict(
        args.get("expected_hash"), path, hass, f"esphome:{rel}")
    if conflict is not None:
        return conflict

    blocked = await _gate(
        "cap_esphome_yaml", token, hass, data,
        tool_name="set_esphome_yaml", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_esphome_yaml(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_esphome_yaml(args, token, hass, data)


async def _execute_set_esphome_yaml(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Apply a device-YAML write, re-validating everything against current disk."""
    resolved = _resolve_esphome_path(hass, args.get("file"))
    if resolved is None:
        return _tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", "set_esphome_yaml"
    path, rel = resolved
    # Re-check scope, parse, splice, and CAS at apply time: an approval window is
    # minutes long and permissions or disk content can drift inside it.
    if not _esphome_file_in_scope(hass, token, rel, write=True):
        return _tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"
    # Read the ContextVar HERE, in the event loop, and hand the answer to the
    # worker thread: the executor job does not inherit this context.
    restoring = _restore_ctx.get() is not None
    try:
        raw, before, existed = await hass.async_add_executor_job(
            _esphome_prepare_write, hass, args, rel, path, restoring)
    except yaml_includes.YamlParseError as err:
        return _tool_error(f"The content is not valid YAML: {err}"), "invalid_request", f"esphome:{rel}"
    except EsphomeSecretViolation as err:
        return _tool_error(str(err)), "invalid_request", f"esphome:{rel}"
    except (OSError, ValueError):
        return _tool_error("Failed to read the existing ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    conflict = await _text_file_cas_conflict(
        args.get("expected_hash"), path, hass, f"esphome:{rel}")
    if conflict is not None:
        return conflict

    try:
        await hass.async_add_executor_job(_write_text_atomic, path, raw)
    except OSError:
        _LOGGER.exception("set_esphome_yaml failed to write %s", rel)
        return _tool_error("Failed to write the ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    # RAW before/after: restore must reproduce the file exactly, and this is an
    # admin-only surface holding values that are already plaintext on disk.
    await _record_version(
        data, token, resource_type="esphome_yaml", resource_id=rel,
        action="edit" if existed else "create",
        before=_version_content_payload(before, path=rel),
        after=_version_content_payload(raw, path=rel),
        alias=rel,
    )
    dashboard = _esphome_dashboard(hass)
    if dashboard is not None:
        try:
            await dashboard.async_request_refresh()
        except Exception:  # noqa: BLE001 - best effort, the write already landed
            _LOGGER.debug("ESPHome dashboard refresh failed after write", exc_info=True)

    return (
        _tool_success(json.dumps({
            "file": rel,
            "bytes_written": len(raw.encode("utf-8")),
            "content_hash": content_hash(raw),
            "note": (
                "Written to disk only. The device keeps running its current firmware "
                "until a build is compiled and installed."
                # Models do not reach for the checking tools unprompted, so the
                # write result points at the next step of the loop. Both named
                # tools ride the capability this write already used, so this can
                # never advertise something the caller cannot then call.
                + (
                    " Check it with validate_esphome_yaml, then prove it builds with"
                    " compile_esphome_firmware."
                    if _esphome_dashboard(hass) is not None else ""
                )
            ),
        })),
        "allowed", f"esphome:{rel}",
    )


def _esphome_delete_precheck(hass: HomeAssistant, token: TokenRecord, args: dict) -> tuple | None:
    """Refuse a delete that cannot work, BEFORE it becomes a pending approval.

    Runs after the cap-deny check, so a fully denied token still
    learns nothing about which files exist.
    """
    resolved = _resolve_esphome_path(hass, args.get("file"))
    if resolved is None:
        return _tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", "delete_esphome_yaml"
    path, rel = resolved
    if not _esphome_file_in_scope(hass, token, rel, write=True):
        return _tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"
    if not os.path.exists(path):
        # Byte-identical to the out-of-scope refusal above (rule 12), different
        # audit outcome.
        return _tool_error(_ESPHOME_NOT_FOUND), "not_found", f"esphome:{rel}"
    return None


def _build_diff_delete_esphome_yaml(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Approval diff for a device-YAML delete. The Before side is masked."""
    resolved = _resolve_esphome_path(hass, args.get("file"))
    rel = resolved[1] if resolved else str(args.get("file"))
    before_masked = ""
    if resolved is not None:
        try:
            secret_values, _ = _read_esphome_secrets(hass)
            raw = _read_text_capped(resolved[0])
            before_masked, _ = redact_esphome_text(raw, secret_values)
        except Exception:  # noqa: BLE001 - diff builders are best-effort
            before_masked = ""

    entry = _esphome_device_for_file(hass, rel) if resolved else None
    device_info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
    device = getattr(device_info, "name", None)

    fields = (
        _summary("delete_esphome_yaml.device", rel=rel, device=device)
        if device else _summary("delete_esphome_yaml", rel=rel)
    )
    return {
        "kind": "esphome_yaml",
        **fields,
        "before": _truncate(before_masked),
        "after": "",
        "preview": {
            "file": rel,
            "device": device,
            # The consequence most likely to be misread: deleting the config does
            # NOT stop, unadopt, or remove anything. Saying so on the card is the
            # difference between an informed approval and a surprised one.
            "device_still_running": device is not None,
            "warning": (
                "Deletes the configuration file only. The device keeps running its "
                "current firmware and keeps its Home Assistant entities; nothing is "
                "unadopted. The file is snapshotted first, so this is restorable "
                "from the Changes tab."
            ),
        },
    }


async def _tool_delete_esphome_yaml(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete one ESPHome device YAML file.

    Phoenix MCP owns this rather than allowlisting the add-on's devices/delete,
    for one reason: the add-on's delete would bypass version capture, so a
    mistaken delete of a heavily worked configuration would be unrecoverable.
    Going through an executor snapshots the file first, which makes the delete
    an ordinary restorable entry in the Changes tab.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "delete_esphome_yaml"

    refusal = _esphome_delete_precheck(hass, token, args)
    if refusal is not None:
        return refusal

    blocked = await _gate(
        "cap_esphome_yaml", token, hass, data,
        tool_name="delete_esphome_yaml", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_delete_esphome_yaml(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_esphome_yaml(args, token, hass, data)


async def _execute_delete_esphome_yaml(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Remove the file, after snapshotting it so the delete can be rolled back."""
    resolved = _resolve_esphome_path(hass, args.get("file"))
    if resolved is None:
        return _tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", "delete_esphome_yaml"
    path, rel = resolved
    # Re-check at apply time: an approval window is minutes long and both the
    # token's scope and the file itself can drift inside it.
    if not _esphome_file_in_scope(hass, token, rel, write=True):
        return _tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"
    if not await hass.async_add_executor_job(os.path.exists, path):
        return _tool_error(_ESPHOME_NOT_FOUND), "not_found", f"esphome:{rel}"

    try:
        before = await hass.async_add_executor_job(_read_text_capped, path)
    except (OSError, ValueError):
        return _tool_error("Failed to read the ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    # Snapshot BEFORE removing: a version recorded after a failed delete would
    # claim a rollback point that does not describe anything that happened, and
    # one recorded after a successful delete is a window where the only copy is
    # in memory.
    await _record_version(
        data, token, resource_type="esphome_yaml", resource_id=rel, action="delete",
        before=_version_content_payload(before, path=rel), after=None, alias=rel,
    )
    try:
        await hass.async_add_executor_job(os.remove, path)
    except OSError:
        _LOGGER.exception("delete_esphome_yaml failed to remove %s", rel)
        return _tool_error("Failed to delete the ESPHome configuration file."), "invalid_request", f"esphome:{rel}"

    dashboard = _esphome_dashboard(hass)
    if dashboard is not None:
        try:
            await dashboard.async_request_refresh()
        except Exception:  # noqa: BLE001 - best effort, the delete already landed
            _LOGGER.debug("ESPHome dashboard refresh failed after delete", exc_info=True)

    return (
        _tool_success(json.dumps({
            "file": rel,
            "deleted": True,
            "note": (
                "The configuration file is gone. The device itself was not touched: it "
                "keeps running its current firmware and keeps its Home Assistant "
                "entities. Restore the file from the Changes tab if this was wrong."
            ),
        })),
        "allowed", f"esphome:{rel}",
    )


_ESPHOME_BUILDER_ABSENT = "The ESPHome Device Builder add-on is not available."


_ESPHOME_BUILDER_UNREACHABLE = "The ESPHome Device Builder did not respond."


_ESPHOME_BUILDER_AUTH = (
    "The ESPHome Device Builder requires authentication, and Phoenix MCP holds no "
    "Device Builder credentials."
)


def _esphome_builder_url(hass: HomeAssistant) -> str | None:
    """The Device Builder's base URL, or None when it is not configured."""
    return getattr(_esphome_dashboard(hass), "url", None)


def _builder_refusal(err: BuilderError, resource: str) -> tuple[dict, str, str]:
    """Map a client failure to a tool error the agent can act on."""
    if isinstance(err, BuilderAuthRequired):
        message = _ESPHOME_BUILDER_AUTH
    elif isinstance(err, BuilderCommandError):
        message = f"The ESPHome Device Builder refused the request: {err.error_code}"
        if err.details:
            message += f" ({err.details})"
    else:
        message = _ESPHOME_BUILDER_UNREACHABLE
    return _tool_error(message), "invalid_request", resource


def _esphome_file_precheck(
    hass: HomeAssistant, token: TokenRecord, args: dict, tool_name: str, *, write: bool = False
) -> tuple[tuple[dict, str, str] | None, str, str]:
    """Resolve and authorize a device file for the file-bound builder tools.

    Existence is checked HERE, before any Device Builder call, so the add-on's
    own "not found" can never produce a different body than an out-of-scope
    refusal: both must be indistinguishable.

    `write` is set by the tools that start a firmware job. Creating a build for a
    device is at least as consequential as editing its YAML, so it must not be
    reachable from read-only scope; the lookups and diagnostics stay on read.
    """
    resolved = _resolve_esphome_path(hass, args.get("file"))
    if resolved is None:
        return (_tool_error(_ESPHOME_PATH_REFUSED), "invalid_request", tool_name), "", ""
    path, rel = resolved
    if not _esphome_file_in_scope(hass, token, rel, write=write):
        return (_tool_error(_ESPHOME_NOT_FOUND), "denied", f"esphome:{rel}"), path, rel
    if not os.path.isfile(path):
        return (_tool_error(_ESPHOME_NOT_FOUND), "not_found", f"esphome:{rel}"), path, rel
    return None, path, rel


def _esphome_scrub_values(hass: HomeAssistant, path: str) -> set[str]:
    """Every credential value that must not appear in Device Builder output.

    The secrets.yaml values plus whatever this file inlines. A file that does not
    parse still contributes the secrets.yaml values: it is often unparseable
    precisely because someone is validating a broken edit, and losing the scrub
    there would be the worst moment for it.
    """
    secret_values, _keys = _read_esphome_secrets(hass)
    values = set(secret_values or set())
    try:
        with open(path, encoding="utf-8") as handle:
            values |= inline_secret_values(handle.read(), values)
    except (OSError, ValueError, yaml_includes.YamlParseError):
        _LOGGER.debug("ESPHome scrub could not read inline credentials", exc_info=True)
    return values


def _esphome_page_limit(args: dict) -> int:
    """Clamp a caller-supplied page size into the configured bounds.

    A missing, non-integer, or non-positive limit falls back to the default
    rather than clamping up to 1: a model sending limit 0 means "unspecified",
    and answering with a single row would look like an empty catalog.
    """
    raw = args.get("limit")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return ESPHOME_BUILDER_PAGE_LIMIT
    return min(raw, ESPHOME_BUILDER_PAGE_LIMIT_MAX)


def _esphome_optional_args(args: dict, names: tuple[str, ...]) -> dict:
    """Pass through only the non-empty string filters the caller actually set."""
    out = {}
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    return out


async def _tool_validate_esphome_yaml(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: run the Device Builder's config validation over a device file.

    Validates what is ON DISK, never caller-supplied content, and that is a
    security boundary rather than a convenience. ESPHome evaluates configuration
    at validation time, and a config carrying external_components fetches and
    executes remote code to do it. Validating an arbitrary payload would
    therefore be an unreviewed code-execution primitive; a file on disk got
    there through set_esphome_yaml, which is capability-gated and can require an
    admin's approval. So the loop is write first, then validate.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "validate_esphome_yaml"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "validate_esphome_yaml"

    refusal, path, rel = _esphome_file_precheck(hass, token, args, "validate_esphome_yaml")
    if refusal is not None:
        return refusal

    values = await hass.async_add_executor_job(_esphome_scrub_values, hass, path)
    try:
        res = await async_builder_command(
            hass, url, "devices/validate", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_VALIDATE_TIMEOUT_SECONDS,
            collect_output=True,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")

    body = {
        "file": rel,
        "valid": res.success,
        "exit_code": res.exit_code,
        # Validation errors quote the offending lines back, which can include a
        # credential the file carries inline.
        "output": scrub_secret_values(res.output, values),
    }
    if res.output_truncated:
        body["output_truncated"] = True
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


async def _tool_get_esphome_board(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: look up an ESPHome board's pins and hardware, or search boards."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_board"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "get_esphome_board"

    board_id = args.get("board_id")
    if isinstance(board_id, str) and board_id.strip():
        command = "boards/get_board"
        payload: dict = {"board_id": board_id.strip()}
        key = "board"
    else:
        command = "boards/get_boards"
        payload = _esphome_optional_args(args, ("query", "platform"))
        offset = args.get("offset")
        payload["offset"] = offset if isinstance(offset, int) and offset > 0 else 0
        payload["limit"] = _esphome_page_limit(args)
        key = "boards"

    try:
        res = await async_builder_command(
            hass, url, command, payload,
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, "get_esphome_board")

    return (
        _tool_success(json.dumps({key: res.result}, default=str)),
        "allowed", "get_esphome_board",
    )


async def _tool_get_esphome_component(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: look up ESPHome component schemas, search them, or list categories."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_component"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "get_esphome_component"

    ids = args.get("component_ids")
    if isinstance(ids, list) and ids:
        wanted = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
        if len(wanted) > ESPHOME_BUILDER_MAX_COMPONENT_IDS:
            return (
                _tool_error(
                    f"Ask for at most {ESPHOME_BUILDER_MAX_COMPONENT_IDS} components at a time."
                ),
                "invalid_request", "get_esphome_component",
            )
        command = "components/get_component_bodies"
        payload: dict = {"component_ids": wanted}
        payload.update(_esphome_optional_args(args, ("platform", "board_id")))
        key = "components"
    elif any(
        isinstance(v, str) and v.strip()
        for v in (args.get("query"), args.get("category"))
    ):
        command = "components/get_components"
        payload = _esphome_optional_args(
            args, ("query", "category", "exclude_category", "platform", "board_id"))
        offset = args.get("offset")
        payload["offset"] = offset if isinstance(offset, int) and offset > 0 else 0
        payload["limit"] = _esphome_page_limit(args)
        key = "components"
    else:
        command = "components/get_categories"
        payload = _esphome_optional_args(args, ("board_id",))
        key = "categories"

    try:
        res = await async_builder_command(
            hass, url, command, payload,
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, "get_esphome_component")

    return (
        _tool_success(json.dumps({key: res.result}, default=str)),
        "allowed", "get_esphome_component",
    )


# Fields kept from the Device Builder's automation catalog, per section. An
# ALLOWLIST rather than a drop-list, matching radio.py: a future upstream field
# cannot bloat the response by default. Dropped are the prose (`description`,
# `docs_url`), `form_editable` (a UI-editor concern), and, on the three catalog
# sections, `name`: it is a prettified restatement of the id the caller already
# has ("Cc1101 -> Set Channel" for cc1101.set_channel), useful in the add-on's
# own dropdowns and redundant to an agent, which writes the id. `devices` KEEPS
# its name, which is the operator's own entity name rather than a derived one.
# Live-measured on a real device: the full payload was 64KB, enough to swamp an
# agent's context, against under 13KB projected. Every ROW is kept, since a
# catalog that silently omits entries is worse than a large one.
_ESPHOME_AUTOMATION_FIELDS: dict[str, tuple[str, ...]] = {
    "triggers": ("id", "applies_to", "is_device_level", "supports_list"),
    "actions": ("id", "domain", "is_control_flow", "has_else_branch",
                "accepts_action_list"),
    "conditions": ("id", "domain", "accepts_condition_list"),
    "devices": ("id", "component_id", "name", "title", "has_explicit_id",
                "parent_id", "is_entity_container"),
}


def _project_esphome_automations(result: Any) -> Any:
    """Trim the automation catalog to the fields that shape how config is written.

    Empty and false values are dropped too: a flag that is false on 57 of 60
    rows carries no information and costs a line each. `scripts` passes through
    whole, being the device's own declarations rather than catalog metadata.
    """
    if not isinstance(result, dict):
        return result
    out: dict[str, Any] = {}
    for section, rows in result.items():
        keep = _ESPHOME_AUTOMATION_FIELDS.get(section)
        if keep is None or not isinstance(rows, list):
            out[section] = rows
            continue
        out[section] = [
            {k: row[k] for k in keep if row.get(k) not in (None, [], {}, False)}
            if isinstance(row, dict) else row
            for row in rows
        ]
    return out


async def _tool_get_esphome_automations(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: the trigger/action/condition catalog a device's own config supports."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_automations"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "get_esphome_automations"

    refusal, path, rel = _esphome_file_precheck(hass, token, args, "get_esphome_automations")
    if refusal is not None:
        return refusal

    values = await hass.async_add_executor_job(_esphome_scrub_values, hass, path)
    try:
        # Exactly the filename. The API also accepts a draft `yaml` body, which
        # would be the same unreviewed config-time evaluation validate refuses.
        res = await async_builder_command(
            hass, url, "automations/get_available", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")

    # Belt-and-braces: the catalog is component metadata rather than config
    # values, but it is derived from this file, so scrub the rendered body too.
    rendered = json.dumps(
        {"file": rel, "automations": _project_esphome_automations(res.result)},
        default=str,
    )
    return _tool_success(scrub_secret_values(rendered, values)), "allowed", f"esphome:{rel}"


# Phoenix MCP only ever flashes over the air. A caller-supplied serial path would
# be a device-file access primitive, and a device that genuinely needs serial
# needs a person holding a cable anyway.
_ESPHOME_OTA_PORT = "OTA"


# Two sets, deliberately not complements of each other, because an unrecognized
# status must fail in a different direction in each place it is read.
#
# ACTIVE decides whether to refuse starting a new build. An unknown status counts
# as inactive, so a status Phoenix MCP has not seen before can never permanently
# wedge every build behind a job that will never look finished.
_ESPHOME_JOB_ACTIVE = frozenset({"queued", "running"})


# TERMINAL decides whether the log is fetched by replaying the finished job.
# An unknown status counts as NOT terminal, because following a job that is still
# running streams until it finishes, which would hold the request for the whole
# build; reading the live buffer instead merely risks an empty log.
_ESPHOME_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled"})


_ESPHOME_JOB_NOT_FOUND = "No such firmware job."


# Appended ONLY where `file` is actually accepted, i.e. get_esphome_job: naming a
# recovery the caller cannot use (cancel takes an id only) would just send it
# round another loop. Same principle as DOMAIN_SERVICE_HINTS: an agent told only
# "no" retries variations, while one told the fix takes it. Live-observed, an
# agent that had lost its job id guessed one, got the bare refusal, and spent two
# further calls working out that it could ask by file instead.
#
# Rule 12 is unaffected: whichever message applies, the unknown-id and
# out-of-scope branches return the SAME body and differ only in audit outcome,
# and the hint names no file the caller did not already supply.
_ESPHOME_JOB_NOT_FOUND_BY_FILE = (
    f"{_ESPHOME_JOB_NOT_FOUND} If you do not have a job_id, for example because "
    "the build was started in an earlier conversation, pass file instead (the "
    "device YAML filename) to get that device's most recent job."
)


# Every tool that starts a job shares this, because an agent does what the note
# NAMES and these all used to name polling. wait_for_esphome_job is the only
# thing that yields a progress line (it holds the request open, which is what
# drives the panel's activity renderer); get_esphome_job returns instantly and
# renders nothing, so the operator watched a multi-minute build against a blank
# window and had to ask the agent to monitor it. Live-reported.
_ESPHOME_FOLLOW_NOTE = (
    "To follow it, call wait_for_esphome_job with this job_id: it waits for the "
    "build and reports progress the operator can see, so relay what it says as it "
    "goes. Use get_esphome_job instead only to check back on it later."
)


def _esphome_job_list(result: Any) -> list[dict]:
    """Job records out of a get_jobs reply, whichever way it wraps them."""
    if isinstance(result, list):
        rows = result
    elif isinstance(result, dict):
        rows = result.get("jobs") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _esphome_job_id(value: Any) -> str | None:
    """A job id out of either a bare id or a whole job record.

    devices/rename returns the full job RECORD under "job" and "tail_job", not
    the bare id its documentation's `<job_id>` notation implies, and handing that
    dict back as a job_id gives an agent something no polling tool accepts.
    Accepting both shapes means a change in either direction keeps working.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        inner = value.get("job_id")
        return inner if isinstance(inner, str) and inner else None
    return None


async def _esphome_active_job(hass: HomeAssistant, url: str, rel: str) -> dict | None:
    """A queued or running job for this configuration, if there is one.

    Best-effort: a read failure returns None rather than refusing, since the
    command that follows will surface a genuinely broken add-on on its own.
    """
    try:
        res = await async_builder_command(
            hass, url, "firmware/get_jobs", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError:
        _LOGGER.debug("ESPHome in-flight job check failed", exc_info=True)
        return None
    for job in _esphome_job_list(res.result):
        if job.get("status") in _ESPHOME_JOB_ACTIVE and job.get("job_id"):
            return job
    return None


async def _esphome_inflight_refusal(
    hass: HomeAssistant, url: str, rel: str, resource: str
) -> tuple[dict, str, str] | None:
    """Refuse to start a build while one is already in flight for this file.

    Submitting a second job for the same configuration does not queue behind the
    first: the add-on cancels the first and reaps its log. So a retry issued
    while a build is running silently destroys the build it was retrying, and an
    agent that cannot see that just retries again.
    """
    active = await _esphome_active_job(hass, url, rel)
    if active is None:
        return None
    return (
        _tool_error(
            f"A firmware job for this file is already {active.get('status')} "
            f"(job_id {active.get('job_id')}). Poll it with get_esphome_job, or stop it with "
            "cancel_esphome_job. Starting another one would cancel it and discard its log."
        ),
        "invalid_request",
        resource,
    )


async def _esphome_job_record(hass: HomeAssistant, url: str, job_id: str) -> dict | None:
    """One job's metadata, or None when the add-on does not know the id."""
    try:
        res = await async_builder_command(
            hass, url, "firmware/get_job", {"job_id": job_id},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderCommandError:
        return None
    return res.result if isinstance(res.result, dict) else None


async def _esphome_latest_job_for_file(hass: HomeAssistant, url: str, rel: str) -> dict | None:
    """The newest job for one configuration, or None if it has never been built."""
    try:
        res = await async_builder_command(
            hass, url, "firmware/get_jobs", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError:
        _LOGGER.debug("ESPHome latest-job lookup failed", exc_info=True)
        return None
    jobs = _esphome_job_list(res.result)
    if not jobs:
        return None
    # Sorted rather than trusting the reply's order, which is undocumented.
    # created_at is an ISO string, so a plain string sort is chronological; a
    # record missing it drops to the bottom instead of winning by accident.
    return max(jobs, key=lambda j: str(j.get("created_at") or ""))


async def _esphome_job_precheck(
    hass: HomeAssistant, token: TokenRecord, url: str, args: dict, tool_name: str,
    allow_file: bool = False,
) -> tuple[tuple[dict, str, str] | None, dict]:
    """Resolve a job id and authorize the caller against the file it builds.

    A job id is a handle to another device's build log, so it is scoped exactly
    like the file: the job names its configuration, and that configuration has to
    be in the token's tree. An id the add-on does not know and an id belonging to
    a device this token cannot see return the same body, so polling ids is not a
    way to learn which ones exist.

    allow_file lets the caller name the CONFIGURATION instead, answering "is that
    build done?" with no remembered state. A job id only exists in the
    conversation that started the build, and a voice or Assist conversation is
    exactly the one that does not survive, so without this an agent asked later
    has no id and nothing reliable to guess from. Not offered for cancel:
    stopping "whichever job is newest" is not something a caller can have meant
    to name.
    """
    job_id = args.get("job_id")
    has_file = isinstance(args.get("file"), str) and args["file"].strip()
    if allow_file and has_file and (not isinstance(job_id, str) or not job_id.strip()):
        refusal, _path, rel = _esphome_file_precheck(hass, token, args, tool_name, write=False)
        if refusal is not None:
            return refusal, {}
        job = await _esphome_latest_job_for_file(hass, url, rel)
        if job is None:
            return (
                _tool_error(f"No firmware job has been started for {rel}."),
                "not_found", f"esphome:{rel}",
            ), {}
        return None, job
    if not isinstance(job_id, str) or not job_id.strip():
        message = "job_id or file is required." if allow_file else "job_id is required."
        return (_tool_error(message), "invalid_request", tool_name), {}
    # One message for every refusal below, so the three cases stay byte-identical.
    missing = _ESPHOME_JOB_NOT_FOUND_BY_FILE if allow_file else _ESPHOME_JOB_NOT_FOUND
    job = await _esphome_job_record(hass, url, job_id.strip())
    if job is None:
        return (_tool_error(missing), "not_found", tool_name), {}
    # Separate name: `rel` is already bound to a str on the by-file branch above,
    # and the add-on's payload is untyped, so reusing it would widen that binding.
    configuration = job.get("configuration")
    if not isinstance(configuration, str) or not esphome_rel_path_ok(configuration):
        return (_tool_error(missing), "not_found", tool_name), {}
    rel = configuration
    if not _esphome_file_in_scope(hass, token, rel, write=False):
        return (_tool_error(missing), "denied", f"esphome:{rel}"), {}
    return None, job


async def _esphome_job_output(
    hass: HomeAssistant, url: str, job: dict, values: set[str]
) -> tuple[str, bool]:
    """The job's log, fetched whichever way this job's state stores it.

    A running job holds its output in memory and get_job returns it. A finished
    job does NOT: the add-on writes the log to a sidecar file and empties the
    record's own output list, so the only way back to it is to follow the
    already-finished job, which replays the sidecar and closes immediately.
    """
    if job.get("status") not in _ESPHOME_JOB_TERMINAL:
        # Through the same collector the streaming path uses, never a second
        # line handler: this buffer carries the add-on's literal ANSI escapes and
        # its own terminators, and hand-joining it returned visible colour codes
        # with a blank line between every line (found by smoke-testing a live
        # build, which is the only place the two paths can be compared).
        text, truncated = collect_output_lines(
            job.get("output"), ESPHOME_BUILDER_JOB_PROGRESS_MAX_CHARS, head_ratio=0.0
        )
        return scrub_secret_values(text, values), truncated

    try:
        res = await async_builder_command(
            hass, url, "firmware/follow_job", {"job_id": job.get("job_id")},
            timeout_seconds=ESPHOME_BUILDER_JOB_LOG_TIMEOUT_SECONDS,
            collect_output=True,
            max_chars=ESPHOME_BUILDER_JOB_OUTPUT_MAX_CHARS,
        )
    except BuilderError:
        # The status is the answer the caller actually needs; a missing log is
        # worth degrading over rather than failing the whole poll.
        _LOGGER.debug("ESPHome job log replay failed", exc_info=True)
        return "", False
    return scrub_secret_values(res.output, values), res.output_truncated


async def _esphome_dependent_job(
    hass: HomeAssistant, url: str, rel: str, job_id: str
) -> dict | None:
    """The job queued behind this one, if any: an install's upload, a rename's flash.

    Looks under `rel` first and then unfiltered, because the two jobs of a chain
    do NOT always share a configuration. A rename's tail job keeps the OLD
    filename while its compile job already carries the new one, so the filtered
    lookup alone finds nothing and the rename appears to finish at the compile.
    The filtered call stays first so the common install case keeps its cheap,
    well-scoped query.
    """
    for args in ({"configuration": rel}, {}):
        try:
            res = await async_builder_command(
                hass, url, "firmware/get_jobs", args,
                timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
            )
        except BuilderError:
            _LOGGER.debug("ESPHome dependent job lookup failed", exc_info=True)
            return None
        for job in _esphome_job_list(res.result):
            if job.get("depends_on") == job_id:
                return job
    return None


async def _tool_compile_esphome_firmware(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: start a firmware build for one device file.

    Never approval-gated even when cap_esphome_yaml is set to confirm. Compiling
    writes no file and touches no device: it is validate_esphome_yaml with a
    longer runtime, and validate already runs the same config-time evaluation. So
    it adds build time rather than authority, and gating it would put an admin
    click in the middle of every iteration of an authoring loop.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "compile_esphome_firmware"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "compile_esphome_firmware"

    refusal, _path, rel = _esphome_file_precheck(
        hass, token, args, "compile_esphome_firmware", write=True
    )
    if refusal is not None:
        return refusal

    inflight = await _esphome_inflight_refusal(hass, url, rel, f"esphome:{rel}")
    if inflight is not None:
        return inflight

    try:
        res = await async_builder_command(
            hass, url, "firmware/compile", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")
    return _esphome_job_started(res.result, rel, install=False)


# ESPHome hostnames: lowercase letters, digits and hyphens, not starting or
# ending with one. Checked here so a bad name is refused before an approval
# exists rather than failing inside the add-on after a click.
_ESPHOME_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$")


def _esphome_action_services(hass: HomeAssistant, rel: str) -> list[str]:
    """The HA service names this device's user-defined actions currently occupy."""
    entry = _esphome_device_for_file(hass, rel)
    runtime = getattr(entry, "runtime_data", None) if entry is not None else None
    device_info = getattr(runtime, "device_info", None)
    if runtime is None or device_info is None:
        return []
    return [a["ha_service"] for a in _esphome_declared_actions(hass, device_info, runtime)]


_ESPHOME_SUBST_RE = re.compile(r"^\$\{?([a-zA-Z0-9_]+)\}?$")


def _esphome_configured_name(path: str, rel: str) -> str:
    """This configuration's real esphome name, NOT its filename stem.

    The two diverge routinely: an imported configuration keeps its raw name
    while the file is slugified, and a device adopted under one name can be
    renamed later, leaving a file whose stem is the ORIGINAL name. Comparing a
    rename target against the stem therefore both refuses a legitimate rename
    and blames a conflict that does not exist. This mirrors the add-on's own
    resolved_device_name, which is the value the rename is actually checked
    against; anything else disagrees with the thing doing the work. Resolves a
    ${sub} reference, because the generated configurations use one and an
    unresolved read would compare against the literal "${name}". Fails soft to
    the stem, the add-on's fallback too, so an unreadable or odd file degrades
    rather than blocking the rename.
    """
    stem = os.path.splitext(os.path.basename(rel))[0]
    try:
        with open(path, encoding="utf-8") as handle:
            doc = yaml_includes.load_tagged_lenient(handle.read())
    except (OSError, yaml_includes.YamlParseError):
        return stem
    if not isinstance(doc, dict):
        return stem
    section = doc.get("esphome")
    name = section.get("name") if isinstance(section, dict) else None
    if not isinstance(name, str) or not name.strip():
        return stem
    match = _ESPHOME_SUBST_RE.match(name.strip())
    if match is None:
        return name.strip()
    subs = doc.get("substitutions")
    value = subs.get(match.group(1)) if isinstance(subs, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else stem


async def _rename_esphome_precheck(
    hass: HomeAssistant, token: TokenRecord, url: str, args: dict
) -> tuple[tuple[dict, str, str] | None, str, str]:
    """Everything rename checks before anything is enqueued."""
    refusal, path, rel = _esphome_file_precheck(
        hass, token, args, "rename_esphome_device", write=True
    )
    if refusal is not None:
        return refusal, "", ""

    new_name = args.get("new_name")
    if not isinstance(new_name, str) or not _ESPHOME_NAME_RE.match(new_name):
        return (
            _tool_error(
                "new_name must be a valid ESPHome device name: lowercase letters, digits "
                "and hyphens, not starting or ending with a hyphen, 31 characters or fewer."
            ),
            "invalid_request", f"esphome:{rel}",
        ), "", ""

    target = f"{new_name}.yaml"
    # An IN-PLACE rename: the new name slugifies to this device's own file, so
    # only esphome.name changes. The add-on supports it and forces its
    # config-only path there, because the over-the-air chain needs a distinct
    # filename to compile against. It is not a no-op and must not be refused.
    in_place = target == rel
    configured = await hass.async_add_executor_job(_esphome_configured_name, path, rel)
    if new_name == configured:
        return (
            _tool_error("The device already has that name."),
            "invalid_request", f"esphome:{rel}",
        ), "", ""
    if not in_place:
        resolved_target = _resolve_esphome_path(hass, target)
        target_exists = resolved_target is not None and await hass.async_add_executor_job(
            os.path.exists, resolved_target[0],
        )
        if target_exists:
            # The add-on writes the renamed YAML up front; landing on an existing
            # file would be a silent overwrite of somebody else's device. Skipped
            # when in place, where the file it "collides" with is its own.
            return (
                _tool_error(f"A configuration named {target} already exists."),
                "invalid_request", f"esphome:{rel}",
            ), "", ""

    inflight = await _esphome_inflight_refusal(hass, url, rel, f"esphome:{rel}")
    if inflight is not None:
        return inflight, "", ""
    return None, rel, new_name


def _build_diff_rename_esphome_device(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    """Approval diff for a rename. The action-service break is the headline."""
    resolved = _resolve_esphome_path(hass, args.get("file"))
    rel = resolved[1] if resolved else str(args.get("file"))
    new_name = str(args.get("new_name", ""))

    entry = _esphome_device_for_file(hass, rel) if resolved else None
    runtime = getattr(entry, "runtime_data", None)
    device_info = getattr(runtime, "device_info", None)
    device = getattr(device_info, "name", None)
    # runtime.available, not entry.state: a LOADED entry says the integration set
    # the device up, not that the device is answering, and a powered-off device
    # keeps its entry loaded. The install diff already reads it this way, and
    # reading the entry state instead reported every offline device as online,
    # dropping the note below in exactly the case it exists for.
    online = bool(getattr(runtime, "available", False))

    preview: dict[str, Any] = {
        "file": rel,
        "device": device,
        "new_name": new_name,
        "new_file": f"{new_name}.yaml",
        "compiles_and_flashes": True,
        "device_online": online,
    }

    # The one thing a rename silently breaks. Entities, entity ids, areas,
    # history and MESA profiles all survive it, because HA keys ESPHome entities
    # on the device MAC rather than its name, but a user-defined action's HA
    # SERVICE name is built from the device name, so every automation or script
    # calling one stops resolving the moment this lands.
    services = _esphome_action_services(hass, rel)
    if services:
        renamed = [f"{s} becomes esphome.{new_name.replace('-', '_')}_{s.split('_', 1)[-1]}"
                   for s in services]
        preview["action_services_renamed"] = renamed
        preview["action_services_warning"] = (
            "Any automation, script or scene calling one of these actions will stop "
            "working until it is updated to the new service name. LIVE-CONFIRMED: the "
            "new service is not registered until this device's ESPHome config entry "
            "reconnects, so reload that entry (or restart Home Assistant) after the "
            "flash, or the renamed action stays uncallable even once the device is back."
        )

    preview["retained"] = (
        "Entities, entity ids, areas, history and MESA profiles are keyed on the "
        "device MAC, not its name, so they survive the rename."
    )
    if not online:
        # Deliberately does NOT promise a file-only rename. This flag is Home
        # Assistant's API connection; the add-on decides whether to flash by its
        # OWN reachability probe and never consults this one. Live-observed: a
        # device Home Assistant called unavailable still answered on the OTA port,
        # so the add-on compiled and attempted a real flash while this note
        # promised the file would only be rewritten.
        preview["offline_note"] = (
            "Home Assistant cannot reach this device, so the flash will probably "
            "fail. The add-on judges reachability separately and may still compile "
            "and attempt an over-the-air flash anyway. A rename whose flash fails "
            "is reverted in full, so nothing is left half-applied."
        )

    fields = (
        _summary("rename_esphome_device.services",
                 device=device or rel, new_name=new_name, count=len(services))
        if services
        else _summary("rename_esphome_device", device=device or rel, new_name=new_name)
    )
    return {
        "kind": "system_action",
        **fields,
        "preview": preview,
    }


async def _tool_rename_esphome_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: rename an ESPHome device, its file, and its running firmware.

    Gated on cap_esphome_flash rather than cap_esphome_yaml, and that is the
    whole reason this is safe to expose: the add-on's rename is a compile then an
    OTA-flash-then-swap chain, so putting it under the authoring capability would
    let a token explicitly denied cap_esphome_flash flash a device through a tool
    whose name says "rename".

    NOT versioned, for the restart_ha reason: the file change is inseparable from
    the flash, so a restorable snapshot of the old file would be an affordance
    that cannot actually undo anything. The add-on already deletes the new YAML
    and leaves the original untouched if the compile or flash fails.
    """
    if effective_cap(token, "cap_esphome_flash") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "rename_esphome_device"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "rename_esphome_device"

    refusal, rel, _new = await _rename_esphome_precheck(hass, token, url, args)
    if refusal is not None:
        return refusal

    blocked = await _gate(
        "cap_esphome_flash", token, hass, data,
        tool_name="rename_esphome_device", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_rename_esphome_device(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_rename_esphome_device(args, token, hass, data)


async def _execute_rename_esphome_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Apply the rename, re-validating everything against current state."""
    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "rename_esphome_device"

    # Re-run every check: an approval window is minutes long, and a build the
    # operator started meanwhile must not be destroyed by an approval click.
    refusal, rel, new_name = await _rename_esphome_precheck(hass, token, url, args)
    if refusal is not None:
        return refusal

    try:
        res = await async_builder_command(
            hass, url, "devices/rename",
            {"configuration": rel, "new_name": new_name},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")

    result = res.result if isinstance(res.result, dict) else {}
    # The add-on returns the whole job RECORD under "job" and "tail_job", not the
    # bare id its docs' `<job_id>` notation implies. Handing that dict back as
    # job_id gave an agent something get_esphome_job could not accept.
    # Live-found; both shapes are accepted so a future change either way works.
    raw_job = result.get("job")
    job_id = _esphome_job_id(raw_job)
    # THE dangerous shape drift in this tier, so it fails LOUD. A missing or null
    # "job" is the add-on's documented config-only reply, but a job we cannot READ
    # is a third shape, and the branch below treats anything without an id as
    # config-only. Left silent, a future add-on that rewraps the record would have
    # Phoenix report "nothing was compiled or flashed" while a compile-and-flash
    # chain ran on the device: the operator is told the opposite of what is
    # happening to their hardware, which is the worst failure available here.
    if raw_job is not None and job_id is None:
        note = version_mismatch_note("devices/rename", res.server_version)
        return _tool_error(
            "The ESPHome Device Builder replied with a rename job this version of "
            "Phoenix MCP cannot read, so it is not possible to say whether the "
            "device is being reflashed. Check the ESPHome dashboard directly before "
            "doing anything else." + (f" {note}" if note else "")
        ), "invalid_request", f"esphome:{rel}"
    body: dict[str, Any] = {
        "file": rel,
        "new_file": f"{new_name}.yaml",
        "new_name": new_name,
    }
    if job_id:
        body["job_id"] = job_id
        body["tail_job_id"] = _esphome_job_id(result.get("tail_job"))
        body["note"] = (
            "Renaming now: a build followed by an over-the-air flash is queued. "
            f"{_ESPHOME_FOLLOW_NOTE} UNTIL IT FINISHES both the old and the new "
            "configuration are listed, because the add-on copies the file and drops "
            "one only at the end: that is normal, and neither copy is yours to clean "
            "up. The device only answers to the new name once the flash has finished. "
            "IF THE FLASH FAILS, the add-on removes the new copy by itself: the "
            "rename did not happen, NOTHING is left half-applied, and a listing back "
            "to just the old name is that cleanup rather than a partial rename. "
            "Do NOT try to finish it by installing firmware from either file: that "
            "cannot complete a rename, it only reflashes what the device already runs. "
            "The ONLY recovery is to make the device reachable and call "
            "rename_esphome_device again. AFTERWARDS, on success, this "
            "device's user-defined action services stay unregistered until its ESPHome "
            "config entry reconnects, because Home Assistant registers them on connect "
            "and never removes the old ones: tell the operator to reload the ESPHome "
            "entry for this device (or restart Home Assistant) before expecting "
            "esphome.<name>_<action> to work again. Entities, areas and history are "
            "unaffected and need nothing."
        )
    else:
        # The add-on's config-only rename (mutations_simple.rename_device returning
        # job: None). Reached by an IN-PLACE rename, where the new name slugifies to
        # this device's own file and the over-the-air chain has no distinct filename
        # to compile against. Phoenix never sends the explicit config_only argument,
        # so in-place is the live route. The distinction that matters to the caller
        # is that the FILE changed and the DEVICE did not: unlike the failed-flash
        # case, the configuration on disk really does carry the new name, so
        # installing it is the correct way to finish the job rather than a mistake.
        body["flashed"] = False
        body["config_only"] = True
        body["note"] = (
            "The configuration was rewritten, but nothing was compiled or flashed: "
            "the device is still running its old firmware and still answers to its "
            "OLD name. This is what happens when the new name maps to the same "
            "configuration file, since building over the air needs a distinct "
            "filename. Report it as a configuration change and NOT as a completed "
            "device rename. To finish it, install the firmware from this file once "
            "the device is reachable."
        )
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


async def _tool_clean_esphome_build(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: discard one configuration's build artifacts.

    The escape hatch for a build environment that has gone wrong in a way a
    rebuild cannot fix (a stale cached object, a half-written build dir), which
    otherwise sends the operator to the add-on UI mid-task.

    Never approval-gated, and cap-only for the same reason as compile: it writes
    no configuration file and cannot reach a device.

    Its blast radius is wider than the add-on's own documentation suggests,
    which describes it as cleaning one device: it also deletes
    /data/pio_components and the shared PlatformIO cache, so the next build of
    every device slows down too. The tool description calls that cost out rather
    than presenting this as a free retry.

    Its sibling firmware/reset_build_env stays permanently unreachable, and the
    difference is categorical rather than one of degree: clean takes a
    configuration, cleans that build, and leaves other jobs running, while
    reset_build_env takes no argument at all and CANCELS EVERY in-flight job on
    both lanes before wiping.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "clean_esphome_build"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "clean_esphome_build"

    refusal, _path, rel = _esphome_file_precheck(
        hass, token, args, "clean_esphome_build", write=True
    )
    if refusal is not None:
        return refusal

    # The add-on's clean CANCELS an in-flight build for this configuration before
    # wiping. Refusing instead keeps the rule that a build is never killed as a
    # side effect of asking for something else: cancel_esphome_job is how you
    # stop one, and it says so.
    inflight = await _esphome_inflight_refusal(hass, url, rel, f"esphome:{rel}")
    if inflight is not None:
        return inflight

    try:
        res = await async_builder_command(
            hass, url, "firmware/clean", {"configuration": rel},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")
    return _esphome_job_started(res.result, rel, install=False, clean=True)


def _esphome_job_started(
    result: Any, rel: str, *, install: bool, clean: bool = False
) -> tuple[dict, str, str]:
    """Report a freshly enqueued job, with the polling instruction attached."""
    job = result if isinstance(result, dict) else {}
    job_id = job.get("job_id")
    if not job_id:
        return (
            _tool_error("The ESPHome Device Builder did not return a job."),
            "invalid_request",
            f"esphome:{rel}",
        )
    if install:
        note = (
            f"Compiling now. {_ESPHOME_FOLLOW_NOTE} The device is only flashed "
            "once the upload that follows the compile has finished."
        )
    elif clean:
        note = (
            f"Discarding this configuration's build artifacts. {_ESPHOME_FOLLOW_NOTE} "
            "Its next build will be a full one, and because the shared PlatformIO "
            "cache is dropped too, the next build of every other device is slower as well."
        )
    else:
        note = f"Building now. {_ESPHOME_FOLLOW_NOTE}"
    body = {
        "file": rel,
        "job_id": job_id,
        "status": job.get("status", "queued"),
        "note": note,
    }
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


async def _install_esphome_precheck(
    hass: HomeAssistant, token: TokenRecord, args: dict
) -> tuple[tuple[dict, str, str] | None, str, str]:
    """Everything install checks before it is allowed to enqueue anything.

    Shared by the gate path and the executor, so a change appearing during the
    approval window (the file going away, the token losing the device, another
    build starting meanwhile) is caught at apply time too.
    """
    url = _esphome_builder_url(hass)
    if url is None:
        return (
            (_tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "install_esphome_firmware"),
            "", "",
        )
    refusal, _path, rel = _esphome_file_precheck(
        hass, token, args, "install_esphome_firmware", write=True
    )
    if refusal is not None:
        return refusal, url, ""
    inflight = await _esphome_inflight_refusal(hass, url, rel, f"esphome:{rel}")
    if inflight is not None:
        return inflight, url, rel
    return None, url, rel


async def _tool_install_esphome_firmware(
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    request_id: str = "",
    client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: build and flash one device's firmware (Confirm-eligible)."""
    # Capability gate first, so a denied token gets a uniform Forbidden and never
    # learns anything about the file it named.
    if effective_cap(token, "cap_esphome_flash") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "install_esphome_firmware"

    pre, _url, _rel = await _install_esphome_precheck(hass, token, args)
    if pre is not None:
        return pre

    blocked = await _gate(
        "cap_esphome_flash", token, hass, data,
        tool_name="install_esphome_firmware", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_install_esphome_firmware(args, token, hass, data),
    )
    if blocked is not None:
        return blocked
    return await _execute_install_esphome_firmware(args, token, hass, data)


async def _execute_install_esphome_firmware(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """Side-effect path for install_esphome_firmware."""
    pre, url, rel = await _install_esphome_precheck(hass, token, args)
    if pre is not None:
        return pre

    try:
        res = await async_builder_command(
            hass, url, "firmware/install",
            {"configuration": rel, "port": _ESPHOME_OTA_PORT},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")
    return _esphome_job_started(res.result, rel, install=True)


def _esphome_version_jump(installed: Any, target: Any) -> str | None:
    """These two ESPHome versions differ by more than the patch digit, or None.

    A patch bump is routine. A version change is where the one real limit of an
    over-the-air update starts to matter: it rewrites only the app partition,
    never the bootloader or the partition table, so the device ends up running a
    new app on the bootloader it was originally cabled with. Whatever that
    bootloader cannot do stays undone, and OTA rollback is the one that costs the
    operator a recovery net.

    This is on the approval card because the card otherwise shows both version
    numbers without ever saying they are a version apart, which is the one fact
    that makes the risk legible.

    The wording deliberately does NOT predict an unbootable device: a version
    jump over the air completes cleanly in practice, and a warning that names a
    consequence which does not arrive teaches the operator to click past it,
    the same cry-wolf failure the patch-bump exclusion exists to prevent. It
    states the bootloader gap instead, which can be verified in the device's own
    boot log.
    """
    if not isinstance(installed, str) or not isinstance(target, str):
        return None
    left, right = installed.split(".")[:2], target.split(".")[:2]
    if len(left) < 2 or len(right) < 2 or left == right:
        return None
    return f"{installed} to {target}"


_ESPHOME_VERSION_JUMP_RISK = (
    "An over-the-air update rewrites only the app, never the bootloader or the "
    "partition table, so an ESPHome version change can leave a new app running "
    "on the bootloader the device was originally cabled with. Whatever that "
    "bootloader does not support stays unsupported: a device whose log reports "
    "'Bootloader rollback: not supported' cannot self-revert from a bad update. "
    "Flashing over a cable once updates the bootloader."
)


def _build_diff_install_esphome_firmware(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> dict:
    """Diff payload for install_esphome_firmware approvals.

    Best-effort throughout: this runs before the gate and must never raise, so a
    device that cannot be resolved simply produces a thinner card.
    """
    rel = args.get("file") if isinstance(args.get("file"), str) else ""
    preview: dict[str, Any] = {"file": rel}
    label = rel or "an ESPHome device"
    target_id = rel

    entry = _esphome_device_for_file(hass, rel) if rel else None
    if entry is not None:
        runtime = getattr(entry, "runtime_data", None)
        device_info = getattr(runtime, "device_info", None)
        name = getattr(device_info, "name", None)
        if name:
            label = name
            target_id = name
        installed = getattr(device_info, "esphome_version", None)
        if installed:
            preview["installed_version"] = installed
        dashboard = _esphome_dashboard(hass)
        configured = (getattr(dashboard, "data", None) or {}).get(name) or {}
        if configured.get("current_version"):
            preview["version_to_install"] = configured["current_version"]
        online = bool(getattr(runtime, "available", False))
        preview["device_online"] = online
        if not online:
            preview["deferred"] = (
                "The device is offline, so the flash will be armed for its next wake "
                "rather than applied now."
            )
        # Admin-only surface, so the full blast radius is listed rather than only
        # the entities this token can see.
        entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        preview["entities"] = sorted(e.entity_id for e in entries)
        mesa_modes = {}
        for reg_entry in entries:
            mode = entity_control_mode(data.mesa, token, reg_entry.entity_id)
            if mode is not None:
                mesa_modes[reg_entry.entity_id] = mode
        if mesa_modes:
            preview["mesa_control_modes"] = mesa_modes
    else:
        preview["device"] = "No live device maps to this file yet."

    preview["warning"] = (
        "Replaces the firmware the device is running. A bad image can leave it "
        "unreachable until it is reflashed over a cable."
    )

    # The version jump rides in the SUMMARY as well as the preview, because the
    # summary is the bold line and the History-visible marker (the same reasoning
    # as the MESA involvement note), while the preview renders as a YAML dump an
    # approver can skim past.
    jump = _esphome_version_jump(
        preview.get("installed_version"), preview.get("version_to_install")
    )
    if jump:
        preview["version_change"] = jump
        preview["version_change_risk"] = _ESPHOME_VERSION_JUMP_RISK

    fields = (
        _summary("install_esphome_firmware.jump", label=label, jump=jump)
        if jump else _summary("install_esphome_firmware", label=label)
    )
    return {
        "kind": "system_action",
        **fields,
        "target": {"type": "device", "id": target_id, "label": label},
        "preview": preview,
    }


async def _tool_get_esphome_job(
    args: dict, token: TokenRecord, hass: HomeAssistant, client_ip: str | None = None
) -> tuple[dict, str, str]:
    """MCP tool: report a firmware job's status and log."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_job"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "get_esphome_job"

    refusal, job = await _esphome_job_precheck(
        hass, token, url, args, "get_esphome_job", allow_file=True)
    if refusal is not None:
        return refusal
    # Carries the same stop-here steer as the wait tool on a headless surface.
    # LIVE-FOUND: told not to loop on wait_for_esphome_job, a model obeyed that
    # literally and then polled THIS tool 18 times at two-second intervals,
    # spending the whole per-turn round budget and ending the turn with no
    # answer. Steering only the tool it was told to stop calling just moves the
    # loop next door.
    note = _ESPHOME_HEADLESS_STOP if client_ip in _ESPHOME_HEADLESS_CLIENT_IPS else None
    return await _esphome_job_result(hass, url, job, note)


async def _esphome_job_result(
    hass: HomeAssistant, url: str, job: dict, unfinished_note: str | None = None
) -> tuple[dict, str, str]:
    """Build the reply for one job. Shared so waiting returns the same shape.

    unfinished_note rides only on a job that has NOT finished, so a caller that
    gave up waiting can say why and what to do next.
    """
    rel = job["configuration"]
    path = os.path.join(hass.config.config_dir, _ESPHOME_DIR, rel)
    values = await hass.async_add_executor_job(_esphome_scrub_values, hass, path)
    output, truncated = await _esphome_job_output(hass, url, job, values)

    status = job.get("status")
    finished = status in _ESPHOME_JOB_TERMINAL
    body: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "file": rel,
        "status": status,
        "finished": finished,
        "output": output,
    }
    if truncated:
        body["output_truncated"] = True
    for key in ("progress", "exit_code", "error"):
        if job.get(key) is not None:
            body[key] = job[key]

    # Only ever claim the device was flashed on the evidence of a COMPLETED
    # upload job. An install returns its compile job alone, so a finished compile
    # says nothing about the device, and an install deferred to the device's next
    # wake produces no upload job at all.
    if finished and status == "completed":
        upload = await _esphome_dependent_job(hass, url, rel, str(job.get("job_id") or ""))
        if upload is not None:
            upload_status = upload.get("status")
            body["upload"] = {
                "job_id": upload.get("job_id"),
                "status": upload_status,
                "finished": upload_status in _ESPHOME_JOB_TERMINAL,
            }
            body["flashed"] = upload_status == "completed"
            if upload_status in _ESPHOME_JOB_ACTIVE:
                body["note"] = "Compiled. The device is being flashed now; poll the upload job."
        elif job.get("is_deferred_install") or job.get("queued_update_armed"):
            body["flashed"] = False
            body["armed_for_next_boot"] = True
            body["note"] = (
                "Compiled, but the device was offline, so the flash is armed for its "
                "next wake and has NOT been applied yet."
            )
    if not finished and unfinished_note:
        body["note"] = unfinished_note
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


# Surfaces with no way to show a held request: Assist's tool bridge, the voice
# conversation agent, and AI Task. Agent Chat is deliberately NOT here (it owns
# both ends of its own stream and renders the progress line), and neither is a
# real MCP client, which asked for a long call by calling this tool.
_ESPHOME_HEADLESS_CLIENT_IPS = frozenset({
    ASSIST_CLIENT_IP, VOICE_AGENT_CLIENT_IP, AI_TASK_CLIENT_IP,
})


# Attached to any UNFINISHED job reply on those surfaces. Written imperatively
# and with the reason stated, because the first version ("ask them to check back,
# or call get_esphome_job on the next turn") was read as permission to poll
# immediately: the model spent all 20 of its rounds doing so and the turn ended
# with nothing to say. What was missing was that the turn itself must end.
_ESPHOME_HEADLESS_STOP = (
    "This surface cannot hold a request open while a build runs, and it allows "
    "only a limited number of tool calls per turn. THE BUILD HAS NOT FINISHED: "
    "this reply says nothing about whether it succeeded, only that it is still "
    "going. STOP CALLING TOOLS NOW and answer the user: say it is still building "
    "and ask them to check back in a few minutes. Do NOT poll this job again in "
    "this turn: polling spends the remaining calls and the turn then ends with no "
    "reply at all. When the user next asks, call get_esphome_job ONCE with the "
    "file name; you do not need to remember the job id."
)


def _clamp_job_wait(value: Any, client_ip: str | None = None) -> int:
    """Clamp a caller-supplied wait, defaulting to the maximum.

    Unlike a page size, the useful default here IS the ceiling: the caller asked
    to wait, and a build that finishes inside one call is the whole point.

    EXCEPT on a headless conversational surface, where the ceiling is a hang. A
    voice turn or an Assist reply has no stream to write progress to and its own
    pipeline timeout is far under this tool's, so a full-length hold there buys
    the user nothing but silence followed by an error, and the build carries on
    invisibly either way. Those callers get a token wait and are told to ask
    again, which is the interaction that surface can actually express.
    """
    if client_ip in _ESPHOME_HEADLESS_CLIENT_IPS:
        return ESPHOME_BUILDER_JOB_WAIT_HEADLESS_SECONDS
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return ESPHOME_BUILDER_JOB_WAIT_SECONDS
    return min(value, ESPHOME_BUILDER_JOB_WAIT_SECONDS)


def _esphome_progress_message(job: dict, rel: str, phase: str) -> str:
    """The human-readable line a client shows while a build runs.

    The percentage lives HERE and never in the protocol's numeric progress field,
    which MCP requires to increase on every notification for a token. The add-on's
    number does not qualify: it resets at the compile-to-upload seam and was
    observed reading 27 on an already-completed job. Elapsed seconds carries the
    monotonic guarantee; this string carries the meaning.
    """
    pct = job.get("progress")
    if isinstance(pct, int) and not isinstance(pct, bool) and 0 <= pct <= 100:
        return f"{phase} {rel}: {pct}%"
    return f"{phase} {rel}"


async def _tool_wait_for_esphome_job(
    args: dict, token: TokenRecord, hass: HomeAssistant, client_ip: str | None = None
) -> tuple[dict, str, str]:
    """MCP tool: hold until a firmware job finishes, reporting progress meanwhile.

    The polling sibling of get_esphome_job, and the only place a build reports
    anything while it runs: compile and install return immediately by design, so
    without a held request there is no in-flight call for progress to ride on.

    Follows the whole chain rather than one job. Waiting on an install's compile
    picks up the dependent upload when the compile finishes, so a single call
    reports "Compiling x: 70%" and then "Flashing x: 40%" and only returns once
    the device has actually been flashed.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "wait_for_esphome_job"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "wait_for_esphome_job"

    refusal, job = await _esphome_job_precheck(hass, token, url, args, "wait_for_esphome_job")
    if refusal is not None:
        return refusal

    started = job
    rel = job["configuration"]
    timeout = _clamp_job_wait(args.get("timeout"), client_ip)
    watching, phase = job, "Compiling"
    elapsed = 0.0

    try:
        while elapsed < timeout:
            status = watching.get("status")
            if status in _ESPHOME_JOB_TERMINAL:
                if status != "completed":
                    break
                # A completed compile is not the end of an install: the upload it
                # queued behind itself is where the device actually changes.
                nxt = await _esphome_dependent_job(hass, url, rel, str(watching.get("job_id") or ""))
                if nxt is None or nxt.get("status") in _ESPHOME_JOB_TERMINAL:
                    break
                watching, phase = nxt, "Flashing"
                continue

            _set_progress_status(
                _esphome_progress_message(watching, rel, phase), total=float(timeout)
            )
            await asyncio.sleep(ESPHOME_BUILDER_JOB_POLL_SECONDS)
            elapsed += ESPHOME_BUILDER_JOB_POLL_SECONDS

            fresh = await _esphome_job_record(hass, url, str(watching.get("job_id") or ""))
            if fresh is None:
                # The add-on forgot the job mid-wait (a re-submission evicts the
                # previous record). Report what the original job looks like now
                # rather than pretending the wait succeeded.
                break
            watching = fresh
    finally:
        _set_progress_status(None)

    # Always answer about the job the caller named, so the reply carries the
    # upload block and the flashed verdict rather than a bare upload status.
    current = await _esphome_job_record(hass, url, str(started.get("job_id") or "")) or started
    # One shared wording with get_esphome_job: two divergent versions of "stop
    # here" are two chances to leave a loophole, and the model found the gap
    # between them last time.
    note = _ESPHOME_HEADLESS_STOP if client_ip in _ESPHOME_HEADLESS_CLIENT_IPS else None
    content, outcome, resource = await _esphome_job_result(hass, url, current, note)
    return content, outcome, resource


async def _tool_cancel_esphome_job(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: stop a queued or running firmware job.

    Never approval-gated. Waiting for an admin to approve a stop request is
    backwards, and the build would finish before the click landed. It is also not
    more dangerous than starting one: ESPHome writes an update to the inactive
    partition and swaps on completion, so an interrupted upload leaves the device
    running exactly the firmware it already had.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "cancel_esphome_job"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "cancel_esphome_job"

    refusal, job = await _esphome_job_precheck(hass, token, url, args, "cancel_esphome_job")
    if refusal is not None:
        return refusal

    rel = job["configuration"]
    try:
        await async_builder_command(
            hass, url, "firmware/cancel", {"job_id": job.get("job_id")},
            timeout_seconds=ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
        )
    except BuilderCommandError:
        # The add-on answers not_found for an id it no longer has, which for a
        # job that already finished is the outcome the caller wanted anyway.
        return (
            _tool_success(json.dumps({"job_id": job.get("job_id"), "file": rel, "cancelled": False,
                                      "message": "The job was already finished."})),
            "allowed",
            f"esphome:{rel}",
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")
    return (
        _tool_success(json.dumps({"job_id": job.get("job_id"), "file": rel, "cancelled": True})),
        "allowed",
        f"esphome:{rel}",
    )


async def _tool_get_esphome_device_logs(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: capture a bounded window of a device's live console output.

    Read scope on the device, not write: this observes rather than changes. The
    console prints whatever the firmware logs, which can be more detailed than
    the entities Home Assistant exposes, so the device itself is the unit of
    authorization here.
    """
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "get_esphome_device_logs"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "get_esphome_device_logs"

    refusal, path, rel = _esphome_file_precheck(hass, token, args, "get_esphome_device_logs")
    if refusal is not None:
        return refusal

    seconds = _esphome_capture_seconds(args)
    values = await hass.async_add_executor_job(_esphome_scrub_values, hass, path)
    _set_progress_status(f"Capturing {rel} device logs", total=float(seconds))
    try:
        res = await async_builder_capture(
            hass, url, "devices/logs", {"configuration": rel},
            capture_seconds=seconds,
            max_chars=ESPHOME_BUILDER_JOB_OUTPUT_MAX_CHARS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")
    finally:
        _set_progress_status(None)

    body: dict[str, Any] = {
        "file": rel,
        "captured_seconds": seconds,
        "output": scrub_secret_values(res.output, values),
    }
    if res.output_truncated:
        body["output_truncated"] = True
    if not res.output:
        body["message"] = "The device logged nothing during the capture window."
    return _tool_success(json.dumps(body, default=str)), "allowed", f"esphome:{rel}"


def _esphome_capture_seconds(args: dict) -> int:
    """Clamp a caller-supplied capture window, defaulting nonsense to the default."""
    raw = args.get("seconds")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return ESPHOME_BUILDER_LOG_CAPTURE_SECONDS
    return min(raw, ESPHOME_BUILDER_LOG_CAPTURE_MAX_SECONDS)


async def _tool_decode_esphome_backtrace(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: symbolize an ESP32 crash backtrace against the device's build."""
    if effective_cap(token, "cap_esphome_yaml") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "decode_esphome_backtrace"

    url = _esphome_builder_url(hass)
    if url is None:
        return _tool_error(_ESPHOME_BUILDER_ABSENT), "invalid_request", "decode_esphome_backtrace"

    refusal, path, rel = _esphome_file_precheck(hass, token, args, "decode_esphome_backtrace")
    if refusal is not None:
        return refusal

    raw = args.get("lines")
    lines = [line for line in raw if isinstance(line, str)] if isinstance(raw, list) else []
    if not lines:
        return (
            _tool_error("Pass the backtrace lines from the device log in `lines`."),
            "invalid_request",
            f"esphome:{rel}",
        )
    if len(lines) > ESPHOME_BUILDER_MAX_BACKTRACE_LINES:
        return (
            _tool_error(
                f"Pass at most {ESPHOME_BUILDER_MAX_BACKTRACE_LINES} backtrace lines at a time."
            ),
            "invalid_request",
            f"esphome:{rel}",
        )

    values = await hass.async_add_executor_job(_esphome_scrub_values, hass, path)
    try:
        res = await async_builder_command(
            hass, url, "devices/decode_backtrace", {"configuration": rel, "lines": lines},
            timeout_seconds=ESPHOME_BUILDER_DECODE_TIMEOUT_SECONDS,
        )
    except BuilderError as err:
        return _builder_refusal(err, f"esphome:{rel}")

    payload = res.result if isinstance(res.result, dict) else {}
    body = {
        "file": rel,
        "decoded": payload.get("decoded") or [],
        "stale_build": bool(payload.get("stale_build")),
    }
    # The add-on reports "" here on success, so only a real reason is worth
    # putting in front of the agent.
    if payload.get("unavailable_reason"):
        body["unavailable_reason"] = payload["unavailable_reason"]
    rendered = json.dumps(body, default=str)
    return _tool_success(scrub_secret_values(rendered, values)), "allowed", f"esphome:{rel}"
