"""Radio tools: the Zigbee network and device surface (cap_radio_write).

The TOOL layer only. The backend adapter that talks to Zigbee2MQTT and ZHA is
the package-level radio.py, imported here as `radio_backend` so the two are
never confused inside a module that shares its name.

Every tool resolves through the token's permission tree before touching a
radio: `_resolve_radio_device` returns a device only when the token can see it,
so a device lookup cannot become an oracle for the rest of the mesh. The three
mutating tools (permit join, reconfigure, remove) are Confirm-eligible and each
builds a diff naming the specific device, because "remove device" on a mesh is
not reversible from here.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

from typing import Any
import asyncio
import dataclasses
import json
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant

from ..const import CAP_DENY, PROXY_TIMEOUT_SECONDS
from ..data import PhoenixData
from ..mesa import entity_control_mode
from ..radio import RadioError
from ..helpers import diff_summary_fields as _summary, effective_cap
from .discovery import _accessible_entity_ids
from ..tool_common import _CAP_FORBIDDEN_MESSAGE, _gate, _tool_error, _tool_success
from ..policy_engine import Permission, resolve
from ..token_store import TokenRecord
from .. import radio as radio_backend

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _ResolvedRadioDevice:
    """A radio device resolved under a token's scope: all three always present."""

    device: Any
    backend: str
    ieee: str


def _resolve_radio_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, tool_name: str, *, require_write: bool
) -> _ResolvedRadioDevice | tuple[dict, str, str]:
    """Resolve a device_id to a _ResolvedRadioDevice, or the tool error to return.

    Returns one or the other, not an (error, device, backend, ieee) 4-tuple. The
    tuple form guaranteed the three values were non-None only via the FIRST
    element, so every caller then passed `str | None` into code needing a str.
    This is the same single-return shape as _read_body, _validate_preset_name and
    yaml_includes._layout_or_result; a frozen dataclass rather than a NamedTuple
    so `isinstance(..., tuple)` discriminates the two cases cleanly. Visibility mirrors get_device: the device must have at least one
    accessible entity, and a ghost device and an out-of-scope device return
    identical not_found bodies (no existence oracle). Write ops additionally
    require WRITE on at least one of the device's entities; a visible but
    READ-only device gets a specific message, since the token can already see
    it. A visible non-radio device (including the Z2M bridge itself, which
    never detects as a radio device, protecting the coordinator) is
    invalid_request.
    """
    device_id = str(args.get("device_id") or "").strip()
    if not device_id:
        return _tool_error("device_id is required."), "invalid_request", tool_name
    device = dr.async_get(hass).async_get(device_id)
    ent_reg = er.async_get(hass)
    accessible = [
        eid for eid in _accessible_entity_ids(token, hass)
        if (entry := ent_reg.async_get(eid)) is not None and entry.device_id == device_id
    ]
    if device is None or not accessible:
        return _tool_error("Device not found."), "not_found", device_id
    detected = radio_backend.zigbee_backend_for_device(device)
    if detected is None:
        return _tool_error("Not a radio-managed device."), "invalid_request", device_id
    if require_write and not any(
        resolve(eid, token, hass) == Permission.WRITE for eid in accessible
    ):
        return (
            _tool_error("Read-only access to this device; radio writes need write access."),
            "denied",
            device_id,
        )
    backend, ieee = detected
    return _ResolvedRadioDevice(device=device, backend=backend, ieee=ieee)


@dataclasses.dataclass(frozen=True)
class _ResolvedPermitJoin:
    """Validated permit_zigbee_join args; backend and duration always present."""

    backend: str
    duration: int
    router_ieee: str | None = None
    router_label: str | None = None


def _resolve_permit_join(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> _ResolvedPermitJoin | tuple[dict, str, str]:
    """Validate permit_zigbee_join args to (backend, duration, router_ieee, router_label).

    Shared by the pre-gate check, the diff builder, and the executor so all
    three see the same resolution. The optional router device needs only READ
    visibility (permitting via a router does not mutate the router, and the
    no-router variant needs no device at all). With both backends present and
    no router, the backend arg is required.
    """
    duration_raw = args.get("duration", 60)
    if isinstance(duration_raw, str) and duration_raw.strip().lstrip("-").isdigit():
        duration_raw = int(duration_raw)
    if isinstance(duration_raw, bool) or not isinstance(duration_raw, int) or not 0 <= duration_raw <= 254:
        return _tool_error("duration must be an integer between 0 and 254."), "invalid_request", "permit_zigbee_join"
    backend = args.get("backend")
    if backend is not None and backend not in radio_backend.ZIGBEE_BACKENDS:
        return _tool_error("backend must be one of: z2m, zha."), "invalid_request", "permit_zigbee_join"
    router_ieee = None
    router_label = None
    if args.get("device_id"):
        resolved = _resolve_radio_device(
            args, token, hass, "permit_zigbee_join", require_write=False
        )
        if isinstance(resolved, tuple):
            return resolved
        device, dev_backend, ieee = resolved.device, resolved.backend, resolved.ieee
        if backend is not None and backend != dev_backend:
            return _tool_error("The router device belongs to the other Zigbee backend."), "invalid_request", "permit_zigbee_join"
        backend = dev_backend
        router_ieee = ieee
        router_label = device.name_by_user or device.name or ieee
    present = radio_backend.present_zigbee_backends(hass)
    if backend is None:
        if not present:
            return _tool_error("No Zigbee network is available."), "invalid_request", "permit_zigbee_join"
        if len(present) > 1:
            return _tool_error("Both Zigbee backends are present; pass backend: z2m or zha."), "invalid_request", "permit_zigbee_join"
        backend = present[0]
    elif backend not in present:
        return _tool_error("That Zigbee backend is not available."), "invalid_request", "permit_zigbee_join"
    return _ResolvedPermitJoin(backend, duration_raw, router_ieee, router_label)


async def _build_diff_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> dict:
    permit = _resolve_permit_join(args, token, hass)
    if isinstance(permit, tuple):
        return {"kind": "system_action", **_summary("zigbee_permit"), "preview": {}}
    backend, duration, router_label = permit.backend, permit.duration, permit.router_label
    if duration == 0:
        fields = _summary("zigbee_permit.close")
        warning = "Closes the join window immediately."
    else:
        fields = _summary("zigbee_permit.open", duration=duration)
        warning = "Any nearby Zigbee device can join while the network is open."
    preview: dict[str, Any] = {"duration": duration, "backend": backend, "warning": warning}
    if router_label:
        preview["via_device"] = router_label
    return {
        "kind": "system_action",
        **fields,
        "target": {"type": "system", "id": f"zigbee:{backend}", "label": "Zigbee network"},
        "preview": preview,
    }


def _radio_device_diff_preview(device: Any, ieee: str) -> dict:
    return {
        "device": device.name_by_user or device.name or ieee,
        "ieee": ieee,
        "manufacturer": device.manufacturer,
        "model": device.model,
    }


async def _build_diff_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> dict:
    resolved = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return {"kind": "system_action", **_summary("zigbee_reconfigure"), "preview": {}}
    device, ieee = resolved.device, resolved.ieee
    preview = _radio_device_diff_preview(device, ieee)
    preview["note"] = "Re-interviews the device; it may be briefly unresponsive. Battery devices must be awake."
    label = preview["device"]
    return {
        "kind": "system_action",
        **_summary("zigbee_reconfigure.device", label=label),
        "target": {"type": "device", "id": device.id, "label": label},
        "preview": preview,
    }


async def _build_diff_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> dict:
    resolved = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return {"kind": "system_action", **_summary("zigbee_remove"), "preview": {}}
    device, ieee = resolved.device, resolved.ieee
    preview = _radio_device_diff_preview(device, ieee)
    # The approval diff is admin-only, so it lists all of the device's registry
    # entities (not just the token-visible ones) plus any MESA control modes:
    # the reviewer should see the full blast radius of dropping the device.
    entries = er.async_entries_for_device(er.async_get(hass), device.id, include_disabled_entities=True)
    preview["entities"] = sorted(entry.entity_id for entry in entries)
    mesa_modes = {}
    for entry in entries:
        mode = entity_control_mode(data.mesa, token, entry.entity_id)
        if mode is not None:
            mesa_modes[entry.entity_id] = mode
    if mesa_modes:
        preview["mesa_control_modes"] = mesa_modes
    preview["warning"] = "Device must be re-paired to rejoin; its entities become unavailable."
    label = preview["device"]
    return {
        "kind": "system_action",
        **_summary("zigbee_remove.device", label=label),
        "target": {"type": "device", "id": device.id, "label": label},
        "preview": preview,
    }


async def _tool_get_radio_network(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: report every radio network present (safe projection only)."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_radio_network"
    networks = await radio_backend.async_radio_networks(hass)
    return (
        _tool_success(json.dumps({"networks": networks}, default=str)),
        "allowed",
        "get_radio_network",
    )


async def _tool_get_radio_device(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: radio-level diagnostics for one visible device."""
    if effective_cap(token, "cap_diagnostics") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_radio_device"
    resolved = _resolve_radio_device(
        args, token, hass, "get_radio_device", require_write=False
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        if backend == radio_backend.BACKEND_Z2M:
            entry = await radio_backend.async_z2m_device_entry(hass, ieee)
            if entry is None:
                # Registered in HA but unknown to Z2M (a stale discovery
                # leftover). The token already sees the device, so no oracle.
                return _tool_error("Device is not on the Zigbee network."), "invalid_request", device.id
            projected = radio_backend.project_z2m_device(entry)
        else:
            raw = await radio_backend.async_zha_device_info(hass, ieee)
            accessible = _accessible_entity_ids(token, hass)
            projected = radio_backend.project_zha_device(
                raw,
                accessible_entity_ids=accessible,
                visible_ieee_map=radio_backend.visible_zigbee_device_map(hass, accessible),
                pass_through=token.pass_through,
            )
    except RadioError as exc:
        return _tool_error(f"Failed to read Zigbee device: {exc}"), "invalid_request", device.id
    projected["device_id"] = device.id
    projected["name"] = device.name_by_user or device.name or projected.get("friendly_name")
    if device.area_id:
        projected["area_id"] = device.area_id
    return _tool_success(json.dumps(projected, default=str)), "allowed", device.id


async def _tool_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: open/close the Zigbee join window (Confirm-eligible)."""
    # Capability gate first: a denied token gets a uniform Forbidden with no
    # device work, so the tool can never be a scope/existence oracle.
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "permit_zigbee_join"
    pre = _resolve_permit_join(args, token, hass)
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="permit_zigbee_join", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_permit_zigbee_join(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_permit_zigbee_join(args, token, hass, data)


async def _execute_permit_zigbee_join(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    permit = _resolve_permit_join(args, token, hass)
    if isinstance(permit, tuple):
        return permit
    backend, duration = permit.backend, permit.duration
    router_ieee, router_label = permit.router_ieee, permit.router_label
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await radio_backend.async_permit_join(hass, backend, duration, router_ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", "permit_zigbee_join"
    except TimeoutError:
        return _tool_error("Timed out talking to the Zigbee network."), "invalid_request", "permit_zigbee_join"
    except HomeAssistantError as exc:
        _LOGGER.error("permit_zigbee_join failed: %s", exc)
        return _tool_error("Failed to change the Zigbee join window."), "denied", "permit_zigbee_join"
    body: dict[str, Any] = {"success": True, "backend": backend, "duration": duration}
    if duration == 0:
        body["message"] = "Join window closed."
    else:
        body["message"] = f"Network open for joining for {duration} seconds."
    if router_label:
        body["via_device"] = router_label
    return _tool_success(json.dumps(body)), "allowed", "permit_zigbee_join"


async def _tool_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: re-interview a Zigbee device (Confirm-eligible)."""
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "reconfigure_zigbee_device"
    pre = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="reconfigure_zigbee_device", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_reconfigure_zigbee_device(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_reconfigure_zigbee_device(args, token, hass, data)


async def _execute_reconfigure_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    resolved = _resolve_radio_device(
        args, token, hass, "reconfigure_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            result = await radio_backend.async_reconfigure_device(hass, backend, ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", device.id
    except TimeoutError:
        # Z2M answers device/configure only when the interview finishes; no
        # answer inside the window means it is still working (a battery
        # device may be asleep, but a mains device can just be slow to
        # complete its full endpoint/binding re-interview).
        body = {
            "success": True,
            "partial": True,
            "message": "Reconfigure started; it did not finish within the timeout and is still running.",
        }
        return _tool_success(json.dumps(body)), "allowed", device.id
    except HomeAssistantError as exc:
        _LOGGER.error("reconfigure_zigbee_device failed for %s: %s", ieee, exc)
        return _tool_error("Failed to reconfigure the device."), "denied", device.id
    if result.get("completed"):
        message = "Reconfigure completed."
    else:
        message = "Reconfigure started; it completes in the background (check get_radio_device later)."
    return (
        _tool_success(json.dumps({"success": True, "ieee": ieee, "backend": backend, "message": message})),
        "allowed",
        device.id,
    )


async def _tool_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: remove a device from the Zigbee network (Confirm-eligible)."""
    if effective_cap(token, "cap_radio_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "remove_zigbee_device"
    pre = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(pre, tuple):
        return pre
    blocked = await _gate(
        "cap_radio_write", token, hass, data,
        tool_name="remove_zigbee_device", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_remove_zigbee_device(args, token, hass, data),
    )
    if blocked is not None:
        return blocked
    return await _execute_remove_zigbee_device(args, token, hass, data)


async def _execute_remove_zigbee_device(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    resolved = _resolve_radio_device(
        args, token, hass, "remove_zigbee_device", require_write=True
    )
    if isinstance(resolved, tuple):
        return resolved
    device, backend, ieee = resolved.device, resolved.backend, resolved.ieee
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await radio_backend.async_remove_device(hass, backend, ieee)
    except RadioError as exc:
        return _tool_error(str(exc)), "invalid_request", device.id
    except TimeoutError:
        # Removing an unreachable device can outlast the window; the leave
        # request stays queued, so report partial rather than failure.
        body = {
            "success": True,
            "partial": True,
            "message": "Removal started; the network drops the device when it responds or times out.",
        }
        return _tool_success(json.dumps(body)), "allowed", device.id
    except HomeAssistantError as exc:
        _LOGGER.error("remove_zigbee_device failed for %s: %s", ieee, exc)
        return _tool_error("Failed to remove the device."), "denied", device.id
    return (
        _tool_success(json.dumps({
            "success": True,
            "removed": True,
            "ieee": ieee,
            "backend": backend,
            "message": "Device removed. Re-pair it to rejoin the network.",
        })),
        "allowed",
        device.id,
    )
