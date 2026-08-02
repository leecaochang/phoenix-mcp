"""Radio network management: protocol/backend detection, projections, and ops.

Phoenix MCP's radio tool surface (get_radio_network, get_radio_device, and the
Zigbee write tools) is protocol-generic at the tool layer and backend-specific
here. v1 covers one protocol, Zigbee, with two backends:

- "z2m" (Zigbee2MQTT): driven over Z2M's MQTT request/response topics
  (<base>/bridge/request/... answered on <base>/bridge/response/...) and
  retained state topics (<base>/bridge/info, <base>/bridge/devices). The MQTT
  access goes through two module-level seams (_mqtt_subscribe/_mqtt_publish)
  so tests can fake the broker without paho-mqtt.
- "zha": driven through ws_dispatch (the two clean read commands) plus the
  zha.permit / zha.remove admin services and the reconfigure task starter in
  ws_dispatch. The write-side ZHA WS commands are deliberately not dispatched
  (see the ALLOWED_WS_COMMANDS comment in ws_dispatch.py).

Every read is an ALLOWLIST projection, never a blocklist strip: both backends'
raw payloads can carry network key material (zigpy NetworkBackup keys; Z2M
bridge/info embeds the full Z2M config), and an allowlist means a future field
added upstream can never leak by default. Matter/Thread/Z-Wave land here later
as new protocol branches; the tool schemas in mcp_view.py do not change.

HA-coupling points (re-verify on HA and Z2M upgrades): the ZHA WS command
names and zha_device_info field names, the zha.permit/zha.remove service
schemas, and the Z2M bridge topic API (shapes verified against Z2M 2.x).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import device_registry as dr

from .const import (
    Z2M_BASE_TOPIC,
    Z2M_REQUEST_TIMEOUT_SECONDS,
    Z2M_RETAINED_READ_TIMEOUT_SECONDS,
)
from .ws_dispatch import (
    WsDispatchError,
    async_ws_command,
    async_zha_reconfigure_device,
)

_LOGGER = logging.getLogger(__name__)

BACKEND_Z2M = "z2m"
BACKEND_ZHA = "zha"
ZIGBEE_BACKENDS = (BACKEND_Z2M, BACKEND_ZHA)

_Z2M_DEVICE_ID_PREFIX = "zigbee2mqtt_0x"
_Z2M_BRIDGE_ID_PREFIX = "zigbee2mqtt_bridge_"


class RadioError(Exception):
    """A radio backend operation failed or the backend is unavailable."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def zigbee_backend_for_device(device: Any) -> tuple[str, str] | None:
    """Return (backend, ieee) for a Zigbee device registry entry, else None.

    ZHA devices carry a ("zha", "<ieee>") identifier; Zigbee2MQTT devices carry
    ("mqtt", "zigbee2mqtt_0x<ieee>") from MQTT discovery. The Z2M bridge device
    itself ("zigbee2mqtt_bridge_...") intentionally does not match, so the
    coordinator can never be targeted by the device-scoped radio tools.
    """
    for ident in getattr(device, "identifiers", ()) or ():
        if not isinstance(ident, (list, tuple)) or len(ident) < 2:
            continue
        domain, value = ident[0], str(ident[1])
        if domain == "zha":
            return (BACKEND_ZHA, value)
        if domain == "mqtt" and value.startswith(_Z2M_DEVICE_ID_PREFIX):
            return (BACKEND_Z2M, value[len("zigbee2mqtt_"):])
    return None


def z2m_present(hass: HomeAssistant) -> bool:
    """True when the mqtt integration is loaded and a Z2M bridge device exists."""
    if "mqtt" not in hass.config.components:
        return False
    registry = dr.async_get(hass)
    for device in registry.devices.values():
        for ident in device.identifiers:
            if (
                isinstance(ident, (list, tuple))
                and len(ident) >= 2
                and ident[0] == "mqtt"
                and str(ident[1]).startswith(_Z2M_BRIDGE_ID_PREFIX)
            ):
                return True
    return False


def zha_present(hass: HomeAssistant) -> bool:
    """True when the zha integration is loaded."""
    return "zha" in hass.config.components


def present_zigbee_backends(hass: HomeAssistant) -> list[str]:
    """The Zigbee backends currently present, in a stable order."""
    present = []
    if z2m_present(hass):
        present.append(BACKEND_Z2M)
    if zha_present(hass):
        present.append(BACKEND_ZHA)
    return present


def visible_zigbee_device_map(hass: HomeAssistant, accessible_entity_ids: set[str]) -> dict[str, str]:
    """Map ieee -> device_id for every Zigbee device the token can see.

    A device is visible when at least one of its entities is in the token's
    accessible set (the get_device rule). Used to decide which neighbor
    identifiers a scoped token may see un-redacted.
    """
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    ent_reg = er.async_get(hass)
    visible_device_ids: set[str] = set()
    for entity_id in accessible_entity_ids:
        entry = ent_reg.async_get(entity_id)
        if entry is not None and entry.device_id:
            visible_device_ids.add(entry.device_id)
    dev_reg = dr.async_get(hass)
    ieee_map: dict[str, str] = {}
    for device_id in visible_device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        detected = zigbee_backend_for_device(device)
        if detected is not None:
            ieee_map[detected[1]] = device_id
    return ieee_map


# ---------------------------------------------------------------------------
# Z2M MQTT plumbing. The two seams below are the module's only broker contact;
# tests monkeypatch them (the mesa_suggestions._refs_for seam pattern) so the
# suite never needs a real or mocked paho client.
# ---------------------------------------------------------------------------


async def _mqtt_subscribe(hass: HomeAssistant, topic: str, msg_callback: Callable) -> Callable:
    """Subscribe to an MQTT topic via HA's public mqtt API; returns unsubscribe."""
    from homeassistant.components import mqtt  # noqa: PLC0415

    return await mqtt.async_subscribe(hass, topic, msg_callback)


async def _mqtt_publish(hass: HomeAssistant, topic: str, payload: str) -> None:
    """Publish an MQTT message via HA's public mqtt API."""
    from homeassistant.components import mqtt  # noqa: PLC0415

    await mqtt.async_publish(hass, topic, payload)


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


async def async_z2m_request(
    hass: HomeAssistant,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = Z2M_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one Z2M bridge request and await its matched response.

    Subscribes to <base>/bridge/response/<path> before publishing
    <base>/bridge/request/<path>, tags the request with a transaction id that
    Z2M echoes back (so a concurrent admin's identical request cannot be
    mistaken for ours), and awaits the matching response. Raises RadioError on
    a Z2M-reported error or an unavailable broker; a response timeout raises
    TimeoutError so callers can distinguish "still working, no answer yet"
    (e.g. a sleepy device mid-reinterview) from a hard failure.
    """
    transaction = uuid.uuid4().hex[:12]
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_response(msg: Any) -> None:
        data = _parse_json(getattr(msg, "payload", None))
        if not isinstance(data, dict) or data.get("transaction") != transaction:
            return
        if not future.done():
            future.set_result(data)

    try:
        unsubscribe = await _mqtt_subscribe(
            hass, f"{Z2M_BASE_TOPIC}/bridge/response/{path}", _on_response
        )
    except Exception as exc:  # noqa: BLE001 - broker/integration unavailable
        raise RadioError(f"MQTT is not available: {exc}") from exc
    try:
        request = json.dumps({**payload, "transaction": transaction})
        await _mqtt_publish(hass, f"{Z2M_BASE_TOPIC}/bridge/request/{path}", request)
        response = await asyncio.wait_for(future, timeout)
    finally:
        unsubscribe()
    if response.get("status") != "ok":
        message = response.get("error") or "request failed"
        raise RadioError(f"Zigbee2MQTT rejected the {path} request: {message}")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


async def async_z2m_retained(
    hass: HomeAssistant,
    topic_suffix: str,
    *,
    timeout: float = Z2M_RETAINED_READ_TIMEOUT_SECONDS,
) -> Any:
    """Read a retained Z2M bridge topic (<base>/<topic_suffix>).

    The broker delivers a retained message immediately on subscribe; the first
    payload wins. Raises RadioError when nothing arrives (Z2M down or the
    topic was never published).
    """
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_message(msg: Any) -> None:
        data = _parse_json(getattr(msg, "payload", None))
        if data is not None and not future.done():
            future.set_result(data)

    try:
        unsubscribe = await _mqtt_subscribe(hass, f"{Z2M_BASE_TOPIC}/{topic_suffix}", _on_message)
    except Exception as exc:  # noqa: BLE001
        raise RadioError(f"MQTT is not available: {exc}") from exc
    try:
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise RadioError(
                f"No data from Zigbee2MQTT on {topic_suffix}; is Zigbee2MQTT running?"
            ) from exc
    finally:
        unsubscribe()


# ---------------------------------------------------------------------------
# Projections. Allowlists only; _scalar() coerces backend-library types
# (zigpy EUI64 is a list-of-ints subclass that would JSON-render as a byte
# array) to plain strings.
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def project_z2m_network(info: Any) -> dict[str, Any]:
    """Project a Z2M bridge/info payload to the safe network summary."""
    info = info if isinstance(info, dict) else {}
    coordinator = info.get("coordinator") or {}
    network = info.get("network") or {}
    projected = {
        "protocol": "zigbee",
        "backend": BACKEND_Z2M,
        "version": _scalar(info.get("version")),
        "coordinator": {
            "type": _scalar(coordinator.get("type")),
            "ieee": _scalar(coordinator.get("ieee_address")),
        },
        "channel": _scalar(network.get("channel")),
        "pan_id": _scalar(network.get("pan_id")),
        "extended_pan_id": _scalar(network.get("extended_pan_id")),
        "permit_join": _scalar(info.get("permit_join")),
    }
    if info.get("permit_join_end") is not None:
        projected["permit_join_end"] = _scalar(info.get("permit_join_end"))
    if info.get("restart_required"):
        projected["restart_required"] = True
    return projected


def project_zha_network(raw: Any) -> dict[str, Any]:
    """Project a zha/network/settings payload to the safe network summary.

    The settings blob is a zigpy NetworkBackup; its network_info carries the
    network key, TC link key, key table, and stack-specific secrets, none of
    which may ever appear here.
    """
    raw = raw if isinstance(raw, dict) else {}
    settings = raw.get("settings") or {}
    network_info = settings.get("network_info") or {}
    node_info = settings.get("node_info") or {}
    return {
        "protocol": "zigbee",
        "backend": BACKEND_ZHA,
        "radio_type": _scalar(raw.get("radio_type")),
        "channel": _scalar(network_info.get("channel")),
        "channel_mask": _scalar(network_info.get("channel_mask")),
        "pan_id": _scalar(network_info.get("pan_id")),
        "extended_pan_id": _scalar(network_info.get("extended_pan_id")),
        "nwk_update_id": _scalar(network_info.get("nwk_update_id")),
        "security_level": _scalar(network_info.get("security_level")),
        "coordinator": {
            "ieee": _scalar(node_info.get("ieee")),
            "nwk": _scalar(node_info.get("nwk")),
            "logical_type": _scalar(node_info.get("logical_type")),
        },
    }


def project_z2m_device(entry: Any) -> dict[str, Any]:
    """Project one bridge/devices entry to the safe per-device view.

    Z2M has no per-device neighbor data without a whole-network map scan, so
    the Z2M view carries interview/definition metadata only.
    """
    entry = entry if isinstance(entry, dict) else {}
    definition = entry.get("definition") or {}
    projected = {
        "protocol": "zigbee",
        "backend": BACKEND_Z2M,
        "ieee": _scalar(entry.get("ieee_address")),
        "friendly_name": _scalar(entry.get("friendly_name")),
        "type": _scalar(entry.get("type")),
        "network_address": _scalar(entry.get("network_address")),
        "power_source": _scalar(entry.get("power_source")),
        "supported": _scalar(entry.get("supported")),
        "disabled": _scalar(entry.get("disabled")),
        "model": _scalar(definition.get("model")),
        "vendor": _scalar(definition.get("vendor")),
        "description": _scalar(definition.get("description")),
    }
    # Z2M spells interview state differently across versions.
    for key in ("interview_state", "interview_completed", "interviewing"):
        if entry.get(key) is not None:
            projected[key] = _scalar(entry.get(key))
    return projected


def project_zha_device(
    raw: Any,
    *,
    accessible_entity_ids: set[str],
    visible_ieee_map: dict[str, str],
    pass_through: bool,
) -> dict[str, Any]:
    """Project a zha/device payload to the scoped per-device view.

    The entities list is filtered to the token's accessible set. Neighbor
    metrics (lqi, depth, relationship) always pass through, but neighbor
    ieee/nwk identifiers are replaced with "<redacted>" unless the neighbor
    maps to a device the token can see (then its device_id is included so the
    agent can chain another get_radio_device call). Routes are intentionally
    omitted: they carry only nwk shorts, which cannot be scope-checked without
    whole-network enumeration. Pass-through tokens see everything.
    """
    raw = raw if isinstance(raw, dict) else {}
    projected = {
        "protocol": "zigbee",
        "backend": BACKEND_ZHA,
        "ieee": _scalar(raw.get("ieee")),
        "nwk": _scalar(raw.get("nwk")),
        "name": _scalar(raw.get("name")),
        "manufacturer": _scalar(raw.get("manufacturer")),
        "model": _scalar(raw.get("model")),
        "quirk_applied": _scalar(raw.get("quirk_applied")),
        "power_source": _scalar(raw.get("power_source")),
        "lqi": _scalar(raw.get("lqi")),
        "rssi": _scalar(raw.get("rssi")),
        "last_seen": _scalar(raw.get("last_seen")),
        "available": _scalar(raw.get("available")),
        "device_type": _scalar(raw.get("device_type")),
    }
    entities = []
    for item in raw.get("entities") or []:
        entity_id = (item or {}).get("entity_id") if isinstance(item, dict) else None
        if entity_id in accessible_entity_ids:
            entities.append({"entity_id": entity_id, "name": _scalar(item.get("name"))})
    projected["entities"] = entities
    neighbors = []
    for item in raw.get("neighbors") or []:
        if not isinstance(item, dict):
            continue
        neighbor: dict[str, Any] = {
            "lqi": _scalar(item.get("lqi")),
            "depth": _scalar(item.get("depth")),
            "relationship": _scalar(item.get("relationship")),
            "device_type": _scalar(item.get("device_type")),
        }
        ieee = _scalar(item.get("ieee"))
        if pass_through:
            neighbor["ieee"] = ieee
            neighbor["nwk"] = _scalar(item.get("nwk"))
        elif isinstance(ieee, str) and ieee in visible_ieee_map:
            neighbor["ieee"] = ieee
            neighbor["nwk"] = _scalar(item.get("nwk"))
            neighbor["device_id"] = visible_ieee_map[ieee]
        else:
            neighbor["ieee"] = "<redacted>"
            neighbor["nwk"] = "<redacted>"
        neighbors.append(neighbor)
    projected["neighbors"] = neighbors
    return projected


# ---------------------------------------------------------------------------
# Ops. Each raises RadioError on backend unavailability or a backend-reported
# failure; TimeoutError and HomeAssistantError from service calls propagate so
# the executors can apply their precedented handling (partial success, generic
# denial).
# ---------------------------------------------------------------------------


async def async_radio_networks(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Read and project every present radio network. Absent backends are omitted."""
    networks: list[dict[str, Any]] = []
    if z2m_present(hass):
        try:
            info = await async_z2m_retained(hass, "bridge/info")
            networks.append(project_z2m_network(info))
        except RadioError as exc:
            networks.append(
                {"protocol": "zigbee", "backend": BACKEND_Z2M, "error": str(exc)}
            )
    if zha_present(hass):
        try:
            raw = await async_ws_command(hass, "zha/network/settings", {})
            networks.append(project_zha_network(raw))
        except WsDispatchError as exc:
            networks.append(
                {"protocol": "zigbee", "backend": BACKEND_ZHA, "error": str(exc)}
            )
    return networks


async def async_z2m_device_entry(hass: HomeAssistant, ieee: str) -> dict[str, Any] | None:
    """Find one device's entry in the retained bridge/devices list, or None."""
    devices = await async_z2m_retained(hass, "bridge/devices")
    if not isinstance(devices, list):
        raise RadioError("Unexpected Zigbee2MQTT device list payload.")
    for entry in devices:
        if isinstance(entry, dict) and str(entry.get("ieee_address")) == ieee:
            return entry
    return None


async def async_zha_device_info(hass: HomeAssistant, ieee: str) -> dict[str, Any]:
    """Read one device's zha_device_info via the zha/device WS command."""
    try:
        raw = await async_ws_command(hass, "zha/device", {"ieee": ieee})
    except WsDispatchError as exc:
        raise RadioError(str(exc)) from exc
    return raw if isinstance(raw, dict) else {}


async def async_permit_join(
    hass: HomeAssistant,
    backend: str,
    duration: int,
    router_ieee: str | None = None,
) -> dict[str, Any]:
    """Open (or with duration 0, close) the Zigbee network for joining."""
    if backend == BACKEND_Z2M:
        payload: dict[str, Any] = {"time": duration}
        if router_ieee:
            payload["device"] = router_ieee
        data = await async_z2m_request(hass, "permit_join", payload)
        return {"backend": backend, "time": data.get("time", duration)}
    if backend == BACKEND_ZHA:
        payload = {"duration": duration}
        if router_ieee:
            payload["ieee"] = router_ieee
        try:
            await hass.services.async_call("zha", "permit", payload, blocking=True)
        except ServiceNotFound as exc:
            raise RadioError("ZHA is not available.") from exc
        return {"backend": backend, "time": duration}
    raise RadioError(f"Unknown Zigbee backend: {backend}")


async def async_reconfigure_device(hass: HomeAssistant, backend: str, ieee: str) -> dict[str, Any]:
    """Re-interview a Zigbee device (re-reads endpoints, bindings, reporting)."""
    if backend == BACKEND_Z2M:
        # Z2M answers device/configure only after the interview finishes, so a
        # timeout here usually means a sleepy device that will complete later.
        await async_z2m_request(hass, "device/configure", {"id": ieee})
        return {"backend": backend, "completed": True}
    if backend == BACKEND_ZHA:
        try:
            await async_zha_reconfigure_device(hass, ieee)
        except WsDispatchError as exc:
            raise RadioError(str(exc)) from exc
        return {"backend": backend, "completed": False}
    raise RadioError(f"Unknown Zigbee backend: {backend}")


async def async_remove_device(hass: HomeAssistant, backend: str, ieee: str) -> dict[str, Any]:
    """Remove (leave) a Zigbee device from the network."""
    if backend == BACKEND_Z2M:
        await async_z2m_request(hass, "device/remove", {"id": ieee, "force": False})
        return {"backend": backend, "removed": True}
    if backend == BACKEND_ZHA:
        try:
            await hass.services.async_call("zha", "remove", {"ieee": ieee}, blocking=True)
        except ServiceNotFound as exc:
            raise RadioError("ZHA is not available.") from exc
        return {"backend": backend, "removed": True}
    raise RadioError(f"Unknown Zigbee backend: {backend}")
