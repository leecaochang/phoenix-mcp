"""Radio network management: protocol/backend detection, projections, and ops.

Phoenix MCP's radio tool surface (network, device, and group reads plus Zigbee
writes) is protocol-generic at the tool layer and backend-specific here. v1
covers one protocol, Zigbee, with two backends:

- "z2m" (Zigbee2MQTT): driven over Z2M's MQTT request/response topics
  (<base>/bridge/request/... answered on <base>/bridge/response/...) and
  retained state topics (<base>/bridge/info, <base>/bridge/devices). The MQTT
  access goes through two module-level seams (_mqtt_subscribe/_mqtt_publish)
  so tests can fake the broker without paho-mqtt.
- "zha": driven through ws_dispatch for the clean read, binding, and group
  commands, plus the zha.permit / zha.remove admin services and the reconfigure
  task starter. Every dispatched write has a fixed command shape and resolves
  its radio identifiers from registry-scoped objects upstream.

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
import hashlib
import json
import logging
import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import device_registry as dr

from .const import (
    Z2M_BASE_TOPIC,
    Z2M_DEVICE_OPTIONS_RESPONSE_TIMEOUT_SECONDS,
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

# Zigbee2MQTT converter definitions are third-party data and can be extended by
# local converters. Keep both the published projection and accepted option
# payloads bounded independently of the general MCP response limit.
_Z2M_EXPOSE_MAX_ITEMS = 64
_Z2M_EXPOSE_MAX_DEPTH = 4
_Z2M_EXPOSE_MAX_VALUES = 64
_Z2M_EXPOSE_MAX_STRING = 512
_Z2M_OPTION_MAX_CHANGES = 32
_Z2M_OPTION_MAX_CONTAINER_ITEMS = 64
_Z2M_OPTION_MAX_DEPTH = 4
_Z2M_OPTION_MAX_STRING = 1024
_Z2M_PROPERTY_MAX_NAME = 128
_Z2M_FRIENDLY_NAME_MAX_LENGTH = 256
_Z2M_RADIO_MAX_ENDPOINTS = 32
_Z2M_RADIO_MAX_CLUSTERS = 64
_Z2M_RADIO_MAX_BINDINGS = 128
_Z2M_RADIO_MAX_REPORTINGS = 128
_Z2M_RADIO_MAX_NAME = 128
_Z2M_RADIO_MAX_REQUEST_CLUSTERS = 16
_ZIGBEE_GROUP_MAX_GROUPS = 256
_ZIGBEE_GROUP_MAX_MEMBERS = 256
_ZIGBEE_GROUP_MAX_SCENES = 256
_ZIGBEE_GROUP_NAME_MAX_LENGTH = 128
_ZIGBEE_GROUP_MAX_ENTITY_IDS = 64
_Z2M_DIRECT_PROPERTY_TYPES = frozenset(
    {"binary", "enum", "numeric", "text", "list", "composite"}
)


class RadioError(Exception):
    """A radio backend operation failed or the backend is unavailable."""


class Z2MOptionValidationError(ValueError):
    """A requested Zigbee2MQTT converter option does not match its definition."""


class Z2MPropertyValidationError(ValueError):
    """A direct Zigbee2MQTT exposed property is unsafe or invalid."""


class Z2MRadioConfigurationError(ValueError):
    """Zigbee2MQTT endpoint, binding, or reporting metadata is unsafe."""


class ZigbeeGroupConfigurationError(ValueError):
    """A Zigbee group payload or requested change is unsafe or malformed."""


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


def _bounded_expose_scalar(value: Any) -> Any:
    """Return one bounded JSON scalar from an expose definition, else None."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if not isinstance(value, float) or math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_Z2M_EXPOSE_MAX_STRING]
    return None


def _project_z2m_expose(
    raw: Any, *, depth: int, budget: list[int]
) -> dict[str, Any] | None:
    """Allowlist one Z2M expose object under a shared item budget."""
    if not isinstance(raw, dict) or depth > _Z2M_EXPOSE_MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1
    out: dict[str, Any] = {}
    for key in (
        "type",
        "name",
        "label",
        "property",
        "category",
        "access",
        "value_on",
        "value_off",
        "value_toggle",
        "value_min",
        "value_max",
        "value_step",
        "unit",
        "endpoint",
        "length_min",
        "length_max",
        "description",
    ):
        if key in raw:
            value = _bounded_expose_scalar(raw.get(key))
            if value is not None:
                out[key] = value

    values = raw.get("values")
    if isinstance(values, list):
        projected_values = []
        for value in values[:_Z2M_EXPOSE_MAX_VALUES]:
            scalar = _bounded_expose_scalar(value)
            if scalar is not None:
                projected_values.append(scalar)
        out["values"] = projected_values
        if len(values) > len(projected_values):
            out["values_truncated"] = True

    presets = raw.get("presets")
    if isinstance(presets, list):
        projected_presets = []
        for preset in presets[:_Z2M_EXPOSE_MAX_VALUES]:
            if not isinstance(preset, dict):
                continue
            item = {}
            for key in ("name", "value", "description"):
                if key in preset:
                    scalar = _bounded_expose_scalar(preset.get(key))
                    if scalar is not None:
                        item[key] = scalar
            if item:
                projected_presets.append(item)
        if projected_presets:
            out["presets"] = projected_presets
        if len(presets) > len(projected_presets):
            out["presets_truncated"] = True

    item_type = raw.get("item_type")
    if isinstance(item_type, dict):
        projected_item = _project_z2m_expose(
            item_type, depth=depth + 1, budget=budget
        )
        if projected_item is not None:
            out["item_type"] = projected_item
        else:
            out["item_type_truncated"] = True

    features = raw.get("features")
    if isinstance(features, list):
        projected_features = []
        for feature in features:
            projected = _project_z2m_expose(
                feature, depth=depth + 1, budget=budget
            )
            if projected is None:
                break
            projected_features.append(projected)
        out["features"] = projected_features
        out["features_total"] = len(features)
        if len(projected_features) < len(features):
            out["features_truncated"] = True
    return out or None


def project_z2m_expose_list(raw: Any) -> dict[str, Any]:
    """Return a bounded, recursively allowlisted Z2M expose list."""
    source = raw if isinstance(raw, list) else []
    budget = [_Z2M_EXPOSE_MAX_ITEMS]
    items = []
    for item in source:
        projected = _project_z2m_expose(item, depth=0, budget=budget)
        if projected is None:
            break
        items.append(projected)
    return {
        "items": items,
        "total": len(source),
        "truncated": len(items) < len(source),
    }


def _option_definition_map(entry: Any) -> dict[str, dict[str, Any]]:
    """Map converter option property names to their raw expose definitions."""
    entry = entry if isinstance(entry, dict) else {}
    definition = entry.get("definition")
    options = definition.get("options") if isinstance(definition, dict) else None
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for item in options if isinstance(options, list) else []:
        if not isinstance(item, dict):
            continue
        prop = item.get("property")
        if not isinstance(prop, str) or not prop or len(prop) > 128:
            continue
        if prop in result:
            duplicates.add(prop)
        else:
            result[prop] = item
    for prop in duplicates:
        result.pop(prop, None)
    return result


def z2m_exposed_property_map(entry: Any) -> dict[str, dict[str, Any]]:
    """Map unique exposed property names to their raw leaf definitions.

    Converter exposes are recursive and local converters may repeat a property.
    Repeated properties are omitted because Phoenix cannot prove which endpoint
    a direct MQTT read or write would address.
    """
    entry = entry if isinstance(entry, dict) else {}
    definition = entry.get("definition")
    exposes = definition.get("exposes") if isinstance(definition, dict) else None
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    budget = [_Z2M_EXPOSE_MAX_ITEMS]
    overflow = [False]

    def _walk(items: Any, depth: int) -> None:
        if not isinstance(items, list):
            return
        if depth > _Z2M_EXPOSE_MAX_DEPTH or budget[0] <= 0:
            if items:
                overflow[0] = True
            return
        for item in items:
            if budget[0] <= 0:
                overflow[0] = True
                return
            if not isinstance(item, dict):
                continue
            budget[0] -= 1
            prop = item.get("property")
            if (
                isinstance(prop, str)
                and prop
                and len(prop) <= _Z2M_PROPERTY_MAX_NAME
                and item.get("type") in _Z2M_DIRECT_PROPERTY_TYPES
            ):
                if prop in result:
                    duplicates.add(prop)
                else:
                    result[prop] = item
            _walk(item.get("features"), depth + 1)

    _walk(exposes, 0)
    if overflow[0]:
        return {}
    for prop in duplicates:
        result.pop(prop, None)
    return result


def project_z2m_property_definition(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded allowlist projection for one exposed property."""
    return _project_z2m_expose(schema, depth=0, budget=[1]) or {}


def z2m_property_hash(property_name: str, value: Any) -> str:
    """Stable, type-preserving hash for one direct exposed-property value."""
    canonical = json.dumps(
        {"property": property_name, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_z2m_exposed_value(
    property_name: str, value: Any, schema: dict[str, Any]
) -> Any:
    """Validate one direct exposed-property value against its converter schema."""
    try:
        return _validate_option_value(value, schema, f"property {property_name!r}")
    except Z2MOptionValidationError as exc:
        raise Z2MPropertyValidationError(
            str(exc).replace("option type", "property type")
        ) from exc


def _z2m_device_topic(entry: Any) -> str:
    """Build the exact Z2M device topic from bridge/devices data only."""
    entry = entry if isinstance(entry, dict) else {}
    friendly_name = entry.get("friendly_name")
    if (
        not isinstance(friendly_name, str)
        or not friendly_name
        or len(friendly_name) > _Z2M_FRIENDLY_NAME_MAX_LENGTH
        or "\x00" in friendly_name
        or "+" in friendly_name
        or "#" in friendly_name
    ):
        raise RadioError("Zigbee2MQTT returned an unsafe device friendly name.")
    return f"{Z2M_BASE_TOPIC}/{friendly_name}"


def _normalize_option_value(value: Any, *, depth: int = 0) -> Any:
    """Normalize one current/requested option value to bounded JSON data."""
    if depth > _Z2M_OPTION_MAX_DEPTH:
        raise Z2MOptionValidationError("Option values may not be nested that deeply.")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str) and len(value) > _Z2M_OPTION_MAX_STRING:
            raise Z2MOptionValidationError("Option text is too long.")
        if isinstance(value, float) and not math.isfinite(value):
            raise Z2MOptionValidationError("Option numbers must be finite.")
        return value
    if isinstance(value, list):
        if len(value) > _Z2M_OPTION_MAX_CONTAINER_ITEMS:
            raise Z2MOptionValidationError("Option lists are too large.")
        return [_normalize_option_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > _Z2M_OPTION_MAX_CONTAINER_ITEMS:
            raise Z2MOptionValidationError("Option objects are too large.")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise Z2MOptionValidationError("Option object keys must be short strings.")
            out[key] = _normalize_option_value(item, depth=depth + 1)
        return out
    raise Z2MOptionValidationError("Option values must be JSON-compatible.")


def z2m_options_hash(options: dict[str, Any]) -> str:
    """Stable content hash for the managed converter-option slice."""
    canonical = json.dumps(
        options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def z2m_effective_device_options(
    info: Any, entry: Any, ieee: str
) -> dict[str, Any]:
    """Reconstruct Z2M Device.options and select converter-defined options.

    Zigbee2MQTT's Device.options getter merges config.device_options with the
    device's own config.devices[ieee] block. bridge/info publishes that
    default-expanded config after removing its known secrets. We select only
    properties declared by definition.options, so the rest of the bridge config
    never crosses Phoenix's allowlist boundary.
    """
    info = info if isinstance(info, dict) else {}
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    defaults = config.get("device_options")
    devices = config.get("devices")
    own = None
    if isinstance(devices, dict):
        own = devices.get(ieee)
        if own is None:
            own = next(
                (value for key, value in devices.items() if str(key).lower() == ieee.lower()),
                None,
            )
    merged = {
        **(defaults if isinstance(defaults, dict) else {}),
        **(own if isinstance(own, dict) else {}),
    }
    values = {}
    for prop in _option_definition_map(entry):
        if prop in merged:
            values[prop] = _normalize_option_value(merged[prop])
    return values


def _same_scalar(left: Any, right: Any) -> bool:
    """Strict scalar equality, avoiding True == 1 and False == 0."""
    return type(left) is type(right) and left == right


def _validate_option_value(value: Any, schema: dict[str, Any], path: str) -> Any:
    value = _normalize_option_value(value)
    kind = schema.get("type")
    if kind == "binary":
        if "value_on" not in schema or "value_off" not in schema:
            raise Z2MOptionValidationError(
                f"{path} has no usable binary value definition."
            )
        binary_allowed = [schema.get("value_on"), schema.get("value_off")]
        if not any(_same_scalar(value, candidate) for candidate in binary_allowed):
            raise Z2MOptionValidationError(
                f"{path} must equal the option's value_on or value_off value."
            )
        return value
    if kind == "enum":
        enum_allowed = schema.get("values")
        if not isinstance(enum_allowed, list) or not any(
            _same_scalar(value, candidate) for candidate in enum_allowed
        ):
            raise Z2MOptionValidationError(f"{path} is not one of the allowed values.")
        return value
    if kind == "numeric":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Z2MOptionValidationError(f"{path} must be a number.")
        for field, relation in (("value_min", "minimum"), ("value_max", "maximum")):
            bound = schema.get(field)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                if field == "value_min" and value < bound:
                    raise Z2MOptionValidationError(f"{path} is below its {relation} of {bound}.")
                if field == "value_max" and value > bound:
                    raise Z2MOptionValidationError(f"{path} is above its {relation} of {bound}.")
        step = schema.get("value_step")
        if isinstance(step, (int, float)) and not isinstance(step, bool) and step > 0:
            origin = schema.get("value_min", 0)
            try:
                quotient = (Decimal(str(value)) - Decimal(str(origin))) / Decimal(str(step))
            except (InvalidOperation, ValueError):
                raise Z2MOptionValidationError(f"{path} does not match its numeric step.") from None
            if quotient != quotient.to_integral_value():
                raise Z2MOptionValidationError(f"{path} does not match its numeric step of {step}.")
        return value
    if kind == "text":
        if not isinstance(value, str):
            raise Z2MOptionValidationError(f"{path} must be text.")
        minimum, maximum = schema.get("length_min"), schema.get("length_max")
        if isinstance(minimum, int) and len(value) < minimum:
            raise Z2MOptionValidationError(
                f"{path} needs at least {minimum} characters."
            )
        if isinstance(maximum, int) and len(value) > maximum:
            raise Z2MOptionValidationError(
                f"{path} allows at most {maximum} characters."
            )
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise Z2MOptionValidationError(f"{path} must be a list.")
        minimum, maximum = schema.get("length_min"), schema.get("length_max")
        if isinstance(minimum, int) and len(value) < minimum:
            raise Z2MOptionValidationError(f"{path} needs at least {minimum} items.")
        if isinstance(maximum, int) and len(value) > maximum:
            raise Z2MOptionValidationError(f"{path} allows at most {maximum} items.")
        item_schema = schema.get("item_type")
        if not isinstance(item_schema, dict):
            raise Z2MOptionValidationError(f"{path} has no usable item definition.")
        return [
            _validate_option_value(item, item_schema, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if kind == "composite":
        if not isinstance(value, dict) or not value:
            raise Z2MOptionValidationError(f"{path} must be a non-empty object.")
        features = schema.get("features")
        feature_map = {}
        for feature in features if isinstance(features, list) else []:
            if not isinstance(feature, dict):
                continue
            key = feature.get("property") or feature.get("name")
            if isinstance(key, str) and key and key not in feature_map:
                feature_map[key] = feature
        unknown = sorted(set(value) - set(feature_map))
        if unknown:
            raise Z2MOptionValidationError(
                f"{path} contains unknown fields: {', '.join(unknown)}."
            )
        return {
            key: _validate_option_value(item, feature_map[key], f"{path}.{key}")
            for key, item in value.items()
        }
    raise Z2MOptionValidationError(
        f"{path} uses unsupported option type {kind!r}."
    )


def validate_z2m_option_changes(entry: Any, changes: Any) -> dict[str, Any]:
    """Validate a non-empty partial converter-option update."""
    if not isinstance(changes, dict) or not changes:
        raise Z2MOptionValidationError("options must be a non-empty object.")
    if len(changes) > _Z2M_OPTION_MAX_CHANGES:
        raise Z2MOptionValidationError(
            f"options may change at most {_Z2M_OPTION_MAX_CHANGES} properties at once."
        )
    definitions = _option_definition_map(entry)
    unknown = sorted(
        str(key) for key in changes if not isinstance(key, str) or key not in definitions
    )
    if unknown:
        raise Z2MOptionValidationError(
            f"Unknown or ambiguous converter options: {', '.join(unknown)}."
        )
    return {
        key: _validate_option_value(value, definitions[key], f"options.{key}")
        for key, value in changes.items()
    }


def project_z2m_option_values(entry: Any, values: Any) -> dict[str, Any]:
    """Select and normalize converter-defined values from a Z2M options object."""
    if not isinstance(values, dict):
        raise Z2MOptionValidationError("Zigbee2MQTT returned no resulting options.")
    definitions = _option_definition_map(entry)
    return {
        key: _normalize_option_value(value)
        for key, value in values.items()
        if key in definitions
    }


async def async_read_z2m_exposed_property(
    hass: HomeAssistant,
    entry: dict[str, Any],
    property_name: str,
    schema: dict[str, Any],
    *,
    timeout: float = Z2M_RETAINED_READ_TIMEOUT_SECONDS,
) -> Any:
    """Read one allowlisted property over a device's exact state/get topics."""
    topic = _z2m_device_topic(entry)
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_state(msg: Any) -> None:
        payload = _parse_json(getattr(msg, "payload", None))
        if not isinstance(payload, dict) or property_name not in payload or future.done():
            return
        try:
            value = validate_z2m_exposed_value(
                property_name, payload[property_name], schema
            )
        except Z2MPropertyValidationError:
            return
        future.set_result(value)

    try:
        unsubscribe = await _mqtt_subscribe(hass, topic, _on_state)
    except Exception as exc:  # noqa: BLE001 - broker/integration unavailable
        raise RadioError(f"MQTT is not available: {exc}") from exc
    try:
        try:
            await _mqtt_publish(
                hass,
                f"{topic}/get",
                json.dumps({property_name: ""}, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            raise RadioError(f"MQTT is not available: {exc}") from exc
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise RadioError(
                f"Zigbee2MQTT did not return property {property_name!r}."
            ) from exc
    finally:
        unsubscribe()


async def async_set_z2m_exposed_property(
    hass: HomeAssistant,
    entry: dict[str, Any],
    property_name: str,
    value: Any,
    schema: dict[str, Any],
    *,
    timeout: float = Z2M_REQUEST_TIMEOUT_SECONDS,
) -> Any:
    """Write and exactly confirm one allowlisted property on device state."""
    topic = _z2m_device_topic(entry)
    expected = validate_z2m_exposed_value(property_name, value, schema)
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_state(msg: Any) -> None:
        payload = _parse_json(getattr(msg, "payload", None))
        if not isinstance(payload, dict) or property_name not in payload or future.done():
            return
        try:
            observed = validate_z2m_exposed_value(
                property_name, payload[property_name], schema
            )
        except Z2MPropertyValidationError:
            return
        if z2m_property_hash(property_name, observed) == z2m_property_hash(
            property_name, expected
        ):
            future.set_result(observed)

    try:
        unsubscribe = await _mqtt_subscribe(hass, topic, _on_state)
    except Exception as exc:  # noqa: BLE001
        raise RadioError(f"MQTT is not available: {exc}") from exc
    try:
        try:
            await _mqtt_publish(
                hass,
                f"{topic}/set",
                json.dumps({property_name: expected}, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            raise RadioError(f"MQTT is not available: {exc}") from exc
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise RadioError(
                "Timed out waiting for Zigbee2MQTT to confirm the property change. "
                "Read the property again before retrying because the device may have applied it."
            ) from exc
    finally:
        unsubscribe()


def _z2m_endpoint_id(value: Any, path: str) -> int:
    """Return one real Zigbee endpoint id from retained metadata or arguments."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 254:
        raise Z2MRadioConfigurationError(f"{path} must be an integer from 1 to 254.")
    return value


def _z2m_short_name(value: Any, path: str) -> str:
    """Validate a bounded Zigbee cluster or attribute name."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _Z2M_RADIO_MAX_NAME
        or "\x00" in value
    ):
        raise Z2MRadioConfigurationError(f"{path} must be a short non-empty string.")
    return value


def _z2m_cluster_list(value: Any, path: str) -> list[str]:
    """Validate one bounded retained input/output cluster list."""
    if not isinstance(value, list) or len(value) > _Z2M_RADIO_MAX_CLUSTERS:
        raise Z2MRadioConfigurationError(f"{path} is missing or too large.")
    result: list[str] = []
    for index, item in enumerate(value):
        name = _z2m_short_name(item, f"{path}[{index}]")
        if name not in result:
            result.append(name)
    return result


def z2m_radio_configuration(entry: Any) -> dict[str, Any]:
    """Normalize the exact endpoint/binding/reporting slice of one Z2M device.

    This is both the content-hash source and the mutation validator. It fails
    closed rather than truncating: approving against a partial endpoint tree
    could hide a conflicting binding or reporting record.
    """
    entry = entry if isinstance(entry, dict) else {}
    raw_endpoints = entry.get("endpoints")
    if not isinstance(raw_endpoints, dict) or len(raw_endpoints) > _Z2M_RADIO_MAX_ENDPOINTS:
        raise Z2MRadioConfigurationError("Zigbee2MQTT endpoint metadata is missing or too large.")
    endpoints: list[dict[str, Any]] = []
    binding_count = 0
    reporting_count = 0
    seen_ids: set[int] = set()
    for raw_id, raw_endpoint in raw_endpoints.items():
        endpoint_id = _z2m_endpoint_id(raw_id, "endpoint")
        if endpoint_id in seen_ids or not isinstance(raw_endpoint, dict):
            raise Z2MRadioConfigurationError("Zigbee2MQTT endpoint metadata is ambiguous.")
        seen_ids.add(endpoint_id)
        raw_clusters = raw_endpoint.get("clusters")
        if not isinstance(raw_clusters, dict):
            raise Z2MRadioConfigurationError("Zigbee2MQTT cluster metadata is missing.")
        endpoint: dict[str, Any] = {
            "endpoint": endpoint_id,
            "input_clusters": _z2m_cluster_list(
                raw_clusters.get("input"), f"endpoint {endpoint_id} input clusters"
            ),
            "output_clusters": _z2m_cluster_list(
                raw_clusters.get("output"), f"endpoint {endpoint_id} output clusters"
            ),
            "bindings": [],
            "configured_reportings": [],
        }
        raw_name = raw_endpoint.get("name")
        if raw_name is not None:
            endpoint["name"] = _z2m_short_name(raw_name, f"endpoint {endpoint_id} name")

        raw_bindings = raw_endpoint.get("bindings")
        if not isinstance(raw_bindings, list):
            raise Z2MRadioConfigurationError("Zigbee2MQTT binding metadata is missing.")
        binding_count += len(raw_bindings)
        if binding_count > _Z2M_RADIO_MAX_BINDINGS:
            raise Z2MRadioConfigurationError("Zigbee2MQTT binding metadata is too large.")
        for index, raw_binding in enumerate(raw_bindings):
            if not isinstance(raw_binding, dict) or not isinstance(raw_binding.get("target"), dict):
                raise Z2MRadioConfigurationError("Zigbee2MQTT binding metadata is malformed.")
            cluster = _z2m_short_name(
                raw_binding.get("cluster"), f"endpoint {endpoint_id} binding {index} cluster"
            )
            raw_target = raw_binding["target"]
            target_type = raw_target.get("type")
            if target_type == "endpoint":
                ieee = raw_target.get("ieee_address")
                if not isinstance(ieee, str) or not ieee or len(ieee) > 32:
                    raise Z2MRadioConfigurationError("Zigbee2MQTT binding target is malformed.")
                target = {
                    "type": "endpoint",
                    "ieee_address": ieee.lower(),
                    "endpoint": _z2m_endpoint_id(
                        raw_target.get("endpoint"), "binding target endpoint"
                    ),
                }
            elif target_type == "group":
                group_id = raw_target.get("id")
                if isinstance(group_id, bool) or not isinstance(group_id, int) or not 1 <= group_id <= 65535:
                    raise Z2MRadioConfigurationError("Zigbee2MQTT binding group target is malformed.")
                target = {"type": "group", "id": group_id}
            else:
                raise Z2MRadioConfigurationError("Zigbee2MQTT binding target type is unknown.")
            endpoint["bindings"].append({"cluster": cluster, "target": target})

        raw_reportings = raw_endpoint.get("configured_reportings")
        if not isinstance(raw_reportings, list):
            raise Z2MRadioConfigurationError("Zigbee2MQTT reporting metadata is missing.")
        reporting_count += len(raw_reportings)
        if reporting_count > _Z2M_RADIO_MAX_REPORTINGS:
            raise Z2MRadioConfigurationError("Zigbee2MQTT reporting metadata is too large.")
        for index, raw_reporting in enumerate(raw_reportings):
            if not isinstance(raw_reporting, dict):
                raise Z2MRadioConfigurationError("Zigbee2MQTT reporting metadata is malformed.")
            attribute = raw_reporting.get("attribute")
            if isinstance(attribute, bool) or not isinstance(attribute, (str, int)):
                raise Z2MRadioConfigurationError("Zigbee2MQTT reporting attribute is malformed.")
            if isinstance(attribute, str):
                attribute = _z2m_short_name(
                    attribute, f"endpoint {endpoint_id} reporting {index} attribute"
                )
            elif not 0 <= attribute <= 65535:
                raise Z2MRadioConfigurationError("Zigbee2MQTT reporting attribute is out of range.")
            minimum = raw_reporting.get("minimum_report_interval")
            maximum = raw_reporting.get("maximum_report_interval")
            for field, value in (("minimum", minimum), ("maximum", maximum)):
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
                    raise Z2MRadioConfigurationError(
                        f"Zigbee2MQTT reporting {field} interval is malformed."
                    )
            reporting: dict[str, Any] = {
                "cluster": _z2m_short_name(
                    raw_reporting.get("cluster"), f"endpoint {endpoint_id} reporting {index} cluster"
                ),
                "attribute": attribute,
                "minimum_report_interval": minimum,
                "maximum_report_interval": maximum,
            }
            change = raw_reporting.get("reportable_change")
            if change is not None:
                if (
                    isinstance(change, bool)
                    or not isinstance(change, (int, float))
                    or not math.isfinite(change)
                    or change < 0
                ):
                    raise Z2MRadioConfigurationError("Zigbee2MQTT reportable change is malformed.")
                reporting["reportable_change"] = change
            endpoint["configured_reportings"].append(reporting)
        endpoint["bindings"].sort(
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
        endpoint["configured_reportings"].sort(
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
        endpoints.append(endpoint)
    endpoints.sort(key=lambda item: item["endpoint"])
    return {"endpoints": endpoints}


def z2m_radio_configuration_hash(configuration: dict[str, Any]) -> str:
    """Stable hash for a normalized endpoint/binding/reporting configuration."""
    canonical = json.dumps(
        configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_zigbee_group_name(value: Any) -> str:
    """Validate a user-facing group name without accepting an MQTT topic."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _ZIGBEE_GROUP_NAME_MAX_LENGTH
        or any(char in value for char in ("\x00", "/", "+", "#"))
    ):
        raise ZigbeeGroupConfigurationError(
            "name must be 1 to 128 characters with no surrounding whitespace or MQTT topic characters."
        )
    return value


def _zigbee_group_id(value: Any) -> int:
    """Validate a backend-provided Zigbee group id."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ZigbeeGroupConfigurationError("Zigbee group id is malformed.")
    return value


def _zigbee_group_ieee(value: Any) -> str:
    """Bound one backend-provided IEEE address for internal resolution only."""
    if not isinstance(value, str) or not value or len(value) > 32 or "\x00" in value:
        raise ZigbeeGroupConfigurationError("Zigbee group member address is malformed.")
    return value.lower()


def _zigbee_group_members(raw: Any, *, zha: bool) -> list[dict[str, Any]]:
    """Normalize one bounded backend member list."""
    if not isinstance(raw, list) or len(raw) > _ZIGBEE_GROUP_MAX_MEMBERS:
        raise ZigbeeGroupConfigurationError("Zigbee group members are missing or too large.")
    members: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ZigbeeGroupConfigurationError("Zigbee group member is malformed.")
        if zha:
            device = item.get("device")
            ieee = device.get("ieee") if isinstance(device, dict) else None
            endpoint = item.get("endpoint_id")
        else:
            ieee = item.get("ieee_address")
            endpoint = item.get("endpoint")
        key = (
            _zigbee_group_ieee(ieee),
            _z2m_endpoint_id(endpoint, "Zigbee group member endpoint"),
        )
        if key in seen:
            raise ZigbeeGroupConfigurationError("Zigbee group contains a duplicate member.")
        seen.add(key)
        members.append({"ieee": key[0], "endpoint": key[1]})
    members.sort(key=lambda item: (item["ieee"], item["endpoint"]))
    return members


def normalize_z2m_group(raw: Any) -> dict[str, Any]:
    """Normalize the complete Z2M group slice used for CAS and mutation."""
    if not isinstance(raw, dict):
        raise ZigbeeGroupConfigurationError("Zigbee2MQTT group is malformed.")
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or len(scenes) > _ZIGBEE_GROUP_MAX_SCENES:
        raise ZigbeeGroupConfigurationError("Zigbee2MQTT group scenes are missing or too large.")
    normalized_scenes: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ZigbeeGroupConfigurationError("Zigbee2MQTT group scene is malformed.")
        scene_id = scene.get("id")
        scene_name = scene.get("name")
        if (
            isinstance(scene_id, bool)
            or not isinstance(scene_id, int)
            or not 0 <= scene_id <= 255
            or not isinstance(scene_name, str)
            or len(scene_name) > _ZIGBEE_GROUP_NAME_MAX_LENGTH
            or "\x00" in scene_name
        ):
            raise ZigbeeGroupConfigurationError("Zigbee2MQTT group scene is malformed.")
        normalized_scenes.append({"id": scene_id, "name": scene_name})
    normalized_scenes.sort(key=lambda item: (item["id"], item["name"]))
    return {
        "id": _zigbee_group_id(raw.get("id")),
        "name": validate_zigbee_group_name(raw.get("friendly_name")),
        "members": _zigbee_group_members(raw.get("members"), zha=False),
        "scenes": normalized_scenes,
    }


def normalize_zha_group(raw: Any) -> dict[str, Any]:
    """Normalize the complete ZHA group slice used for CAS and mutation."""
    if not isinstance(raw, dict):
        raise ZigbeeGroupConfigurationError("ZHA group is malformed.")
    members = _zigbee_group_members(raw.get("members"), zha=True)
    entity_ids: set[str] = set()
    for item in raw.get("members", []):
        entities = item.get("entities") if isinstance(item, dict) else None
        if not isinstance(entities, list) or len(entities) > _ZIGBEE_GROUP_MAX_ENTITY_IDS:
            raise ZigbeeGroupConfigurationError("ZHA group entity metadata is malformed or too large.")
        for entity in entities:
            entity_id = entity.get("entity_id") if isinstance(entity, dict) else None
            if (
                not isinstance(entity_id, str)
                or not entity_id
                or len(entity_id) > 255
                or "\x00" in entity_id
            ):
                raise ZigbeeGroupConfigurationError("ZHA group entity metadata is malformed.")
            entity_ids.add(entity_id)
            if len(entity_ids) > _ZIGBEE_GROUP_MAX_ENTITY_IDS:
                raise ZigbeeGroupConfigurationError("ZHA group has too many entity anchors.")
    return {
        "id": _zigbee_group_id(raw.get("group_id")),
        "name": validate_zigbee_group_name(raw.get("name")),
        "members": members,
        "scenes": [],
        "entity_ids": sorted(entity_ids),
    }


def zigbee_group_hash(backend: str, configuration: dict[str, Any]) -> str:
    """Return a stable full-state hash without exposing radio identifiers."""
    canonical = json.dumps(
        {
            "backend": backend,
            "id": configuration.get("id"),
            "name": configuration.get("name"),
            "members": configuration.get("members"),
            "scenes": configuration.get("scenes"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def normalize_zigbee_groups(backend: str, raw: Any) -> list[dict[str, Any]]:
    """Normalize a bounded full group list from one backend."""
    if not isinstance(raw, list) or len(raw) > _ZIGBEE_GROUP_MAX_GROUPS:
        raise ZigbeeGroupConfigurationError("Zigbee group list is missing or too large.")
    if backend == BACKEND_Z2M:
        normalizer = normalize_z2m_group
    elif backend == BACKEND_ZHA:
        normalizer = normalize_zha_group
    else:
        raise ZigbeeGroupConfigurationError(f"Unknown Zigbee backend: {backend}")
    groups = [normalizer(item) for item in raw]
    ids = [item["id"] for item in groups]
    names = [item["name"] for item in groups]
    if len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise ZigbeeGroupConfigurationError("Zigbee group list contains duplicate identities.")
    groups.sort(key=lambda item: item["id"])
    return groups


def project_z2m_radio_configuration(
    entry: Any,
    *,
    visible_ieee_map: dict[str, str],
    coordinator_ieee: str | None = None,
) -> dict[str, Any]:
    """Scope-project endpoints without exposing inaccessible IEEE addresses."""
    try:
        configuration = z2m_radio_configuration(entry)
    except Z2MRadioConfigurationError:
        return {
            "status": "unavailable",
            "reason": "unsafe_or_unsupported_metadata",
        }
    projected = json.loads(json.dumps(configuration))
    visible = {key.lower(): value for key, value in visible_ieee_map.items()}
    coordinator = coordinator_ieee.lower() if isinstance(coordinator_ieee, str) else None
    for endpoint in projected["endpoints"]:
        for binding in endpoint["bindings"]:
            target = binding["target"]
            if target.get("type") == "group":
                binding["target"] = {"kind": "group"}
                continue
            ieee = str(target.get("ieee_address", "")).lower()
            target_endpoint = target.get("endpoint")
            if coordinator and ieee == coordinator:
                binding["target"] = {
                    "kind": "coordinator",
                    "endpoint": target_endpoint,
                }
            elif ieee in visible:
                binding["target"] = {
                    "kind": "device",
                    "device_id": visible[ieee],
                    "endpoint": target_endpoint,
                }
            else:
                binding["target"] = {"kind": "redacted_device"}
    return {
        "status": "available",
        **projected,
        "content_hash": z2m_radio_configuration_hash(configuration),
    }


def _z2m_configuration_endpoint(
    configuration: dict[str, Any], endpoint_id: int
) -> dict[str, Any]:
    endpoint = next(
        (item for item in configuration.get("endpoints", []) if item.get("endpoint") == endpoint_id),
        None,
    )
    if endpoint is None:
        raise Z2MRadioConfigurationError(f"Device does not have endpoint {endpoint_id}.")
    return endpoint


def validate_z2m_binding(
    source_entry: Any,
    target_entry: Any,
    *,
    target_ieee: str,
    source_endpoint: Any,
    target_endpoint: Any,
    clusters: Any,
    operation: str,
) -> tuple[int, int, tuple[str, ...], dict[str, Any], dict[str, Any]]:
    """Validate one exact device-to-device binding change against both devices."""
    if operation not in {"bind", "unbind"}:
        raise Z2MRadioConfigurationError("operation must be bind or unbind.")
    source_endpoint_id = _z2m_endpoint_id(source_endpoint, "source_endpoint")
    target_endpoint_id = _z2m_endpoint_id(target_endpoint, "target_endpoint")
    if (
        not isinstance(clusters, list)
        or not clusters
        or len(clusters) > _Z2M_RADIO_MAX_REQUEST_CLUSTERS
    ):
        raise Z2MRadioConfigurationError(
            f"clusters must contain 1 to {_Z2M_RADIO_MAX_REQUEST_CLUSTERS} names."
        )
    requested: list[str] = []
    for index, raw_cluster in enumerate(clusters):
        cluster = _z2m_short_name(raw_cluster, f"clusters[{index}]")
        if cluster in requested:
            raise Z2MRadioConfigurationError("clusters must not contain duplicates.")
        requested.append(cluster)
    source_configuration = z2m_radio_configuration(source_entry)
    target_configuration = z2m_radio_configuration(target_entry)
    source = _z2m_configuration_endpoint(source_configuration, source_endpoint_id)
    target = _z2m_configuration_endpoint(target_configuration, target_endpoint_id)
    allowed = set(source["output_clusters"]) & set(target["input_clusters"])
    unsupported = sorted(set(requested) - allowed)
    if unsupported:
        raise Z2MRadioConfigurationError(
            "Clusters are not source-output/target-input compatible: " + ", ".join(unsupported) + "."
        )
    target_ieee = target_ieee.lower()
    present = {
        item["cluster"]
        for item in source["bindings"]
        if item["target"].get("type") == "endpoint"
        and item["target"].get("ieee_address") == target_ieee
        and item["target"].get("endpoint") == target_endpoint_id
    }
    if operation == "bind" and present & set(requested):
        raise Z2MRadioConfigurationError("One or more requested bindings already exist.")
    if operation == "unbind" and set(requested) - present:
        raise Z2MRadioConfigurationError("One or more requested bindings do not exist.")
    return (
        source_endpoint_id,
        target_endpoint_id,
        tuple(requested),
        source_configuration,
        target_configuration,
    )


def validate_z2m_reporting(
    entry: Any,
    *,
    endpoint: Any,
    cluster: Any,
    attribute: Any,
    minimum_report_interval: Any,
    maximum_report_interval: Any,
    reportable_change: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one reporting configuration against retained endpoint metadata."""
    endpoint_id = _z2m_endpoint_id(endpoint, "endpoint")
    cluster_name = _z2m_short_name(cluster, "cluster")
    if isinstance(attribute, bool) or not isinstance(attribute, (str, int)):
        raise Z2MRadioConfigurationError("attribute must be a name or numeric Zigbee attribute id.")
    if isinstance(attribute, str):
        attribute = _z2m_short_name(attribute, "attribute")
    elif not 0 <= attribute <= 65535:
        raise Z2MRadioConfigurationError("attribute must be between 0 and 65535.")
    for name, value in (
        ("minimum_report_interval", minimum_report_interval),
        ("maximum_report_interval", maximum_report_interval),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
            raise Z2MRadioConfigurationError(f"{name} must be an integer from 0 to 65535.")
    if minimum_report_interval > maximum_report_interval and maximum_report_interval != 65535:
        raise Z2MRadioConfigurationError(
            "minimum_report_interval cannot exceed maximum_report_interval."
        )
    if reportable_change is not None and (
        isinstance(reportable_change, bool)
        or not isinstance(reportable_change, (int, float))
        or not math.isfinite(reportable_change)
        or reportable_change < 0
    ):
        raise Z2MRadioConfigurationError("reportable_change must be a finite non-negative number.")
    configuration = z2m_radio_configuration(entry)
    endpoint_config = _z2m_configuration_endpoint(configuration, endpoint_id)
    if cluster_name not in endpoint_config["input_clusters"]:
        raise Z2MRadioConfigurationError(
            "cluster must be listed as an input cluster on the selected endpoint."
        )
    desired: dict[str, Any] = {
        "cluster": cluster_name,
        "attribute": attribute,
        "minimum_report_interval": minimum_report_interval,
        "maximum_report_interval": maximum_report_interval,
    }
    if reportable_change is not None:
        desired["reportable_change"] = reportable_change
    existing = next(
        (
            item
            for item in endpoint_config["configured_reportings"]
            if item["cluster"] == cluster_name and item["attribute"] == attribute
        ),
        None,
    )
    if existing == desired:
        raise Z2MRadioConfigurationError("The requested reporting configuration is already current.")
    return configuration, {"endpoint": endpoint_id, **desired}


def project_z2m_device(
    entry: Any,
    *,
    current_options: dict[str, Any] | None = None,
    visible_ieee_map: dict[str, str] | None = None,
    coordinator_ieee: str | None = None,
) -> dict[str, Any]:
    """Project one bridge/devices entry to the safe per-device view.

    Z2M has no per-device neighbor data without a whole-network map scan, so
    the Z2M view carries interview/definition metadata only.
    """
    entry = entry if isinstance(entry, dict) else {}
    definition = (
        entry.get("definition")
        if isinstance(entry.get("definition"), dict)
        else {}
    )
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
    projected["exposes"] = project_z2m_expose_list(definition.get("exposes"))
    values = current_options if isinstance(current_options, dict) else {}
    projected["options"] = {
        "definitions": project_z2m_expose_list(definition.get("options")),
        "values": values,
        "content_hash": z2m_options_hash(values),
    }
    projected["radio_configuration"] = project_z2m_radio_configuration(
        entry,
        visible_ieee_map=visible_ieee_map or {},
        coordinator_ieee=coordinator_ieee,
    )
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
        if (
            isinstance(entry, dict)
            and str(entry.get("ieee_address")).lower() == ieee.lower()
        ):
            return entry
    return None


async def async_z2m_device_snapshot_with_info(
    hass: HomeAssistant, ieee: str
) -> tuple[dict[str, Any] | None, dict[str, Any], str, dict[str, Any]]:
    """Read one Z2M device, effective options, hash, and the source info."""
    devices, info = await asyncio.gather(
        async_z2m_retained(hass, "bridge/devices"),
        async_z2m_retained(hass, "bridge/info"),
    )
    if not isinstance(devices, list):
        raise RadioError("Unexpected Zigbee2MQTT device list payload.")
    entry = next(
        (
            item
            for item in devices
            if isinstance(item, dict)
            and str(item.get("ieee_address")).lower() == ieee.lower()
        ),
        None,
    )
    if entry is None:
        return None, {}, z2m_options_hash({}), info if isinstance(info, dict) else {}
    try:
        options = z2m_effective_device_options(info, entry, ieee)
    except Z2MOptionValidationError as exc:
        raise RadioError(f"Unsafe Zigbee2MQTT option payload: {exc}") from exc
    return entry, options, z2m_options_hash(options), info if isinstance(info, dict) else {}


async def async_z2m_device_snapshot(
    hass: HomeAssistant, ieee: str
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """Read one Z2M device plus its effective converter options and hash."""
    entry, options, content_hash, _info = await async_z2m_device_snapshot_with_info(
        hass, ieee
    )
    return entry, options, content_hash


async def async_confirm_z2m_device_options(
    hass: HomeAssistant,
    ieee: str,
    expected: dict[str, Any],
    *,
    timeout: float = Z2M_RETAINED_READ_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any], str, bool]:
    """Confirm requested options from retained state after a missing response.

    Zigbee2MQTT's correlated bridge response remains the primary confirmation.
    This bounded fallback exists because the option write can be persisted to
    bridge/info even when Phoenix never receives that response. Only an exact,
    type-preserving match of every requested converter option counts as success.
    """
    deadline = hass.loop.time() + timeout
    expected_hash = z2m_options_hash(expected)
    while True:
        remaining = deadline - hass.loop.time()
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(remaining):
            entry, current, current_hash, info = (
                await async_z2m_device_snapshot_with_info(hass, ieee)
            )
        if entry is None:
            raise RadioError("Device is no longer on the Zigbee network.")
        if all(key in current for key in expected):
            requested_slice = {key: current[key] for key in expected}
            if z2m_options_hash(requested_slice) == expected_hash:
                restart_required = bool(info.get("restart_required"))
                return entry, current, current_hash, restart_required
        remaining = deadline - hass.loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.25, remaining))


async def async_zha_device_info(hass: HomeAssistant, ieee: str) -> dict[str, Any]:
    """Read one device's zha_device_info via the zha/device WS command."""
    try:
        raw = await async_ws_command(hass, "zha/device", {"ieee": ieee})
    except WsDispatchError as exc:
        raise RadioError(str(exc)) from exc
    return raw if isinstance(raw, dict) else {}


async def async_zigbee_groups(
    hass: HomeAssistant, backend: str
) -> list[dict[str, Any]]:
    """Read and normalize the complete group state for one Zigbee backend."""
    try:
        if backend == BACKEND_Z2M:
            raw = await async_z2m_retained(hass, "bridge/groups")
        elif backend == BACKEND_ZHA:
            raw = await async_ws_command(hass, "zha/groups", {})
        else:
            raise RadioError(f"Unknown Zigbee backend: {backend}")
        return normalize_zigbee_groups(backend, raw)
    except (WsDispatchError, ZigbeeGroupConfigurationError) as exc:
        raise RadioError(f"Unsafe or unavailable {backend} group state: {exc}") from exc


async def async_zha_groupable_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Read ZHA's groupable device endpoints for scoped projection upstream."""
    try:
        raw = await async_ws_command(hass, "zha/devices/groupable", {})
    except WsDispatchError as exc:
        raise RadioError(str(exc)) from exc
    if not isinstance(raw, list) or len(raw) > _ZIGBEE_GROUP_MAX_MEMBERS:
        raise RadioError("ZHA groupable device payload is malformed or too large.")
    return [item for item in raw if isinstance(item, dict)]


async def _async_confirm_zigbee_group(
    hass: HomeAssistant,
    backend: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout: float = Z2M_RETAINED_READ_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Poll backend group state until an exact mutation predicate holds."""
    deadline = hass.loop.time() + timeout
    while True:
        groups = await async_zigbee_groups(hass, backend)
        if predicate(groups):
            return groups
        remaining = deadline - hass.loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.25, remaining))


def _group_by_id(groups: list[dict[str, Any]], group_id: int) -> dict[str, Any] | None:
    return next((group for group in groups if group["id"] == group_id), None)


async def async_create_zigbee_group(
    hass: HomeAssistant,
    *,
    backend: str,
    name: str,
    members: tuple[tuple[str, int], ...],
) -> dict[str, Any]:
    """Create a non-empty group and exactly confirm its resolved membership."""
    name = validate_zigbee_group_name(name)
    if not members or len(members) > _ZIGBEE_GROUP_MAX_MEMBERS:
        raise ZigbeeGroupConfigurationError("A group needs 1 to 256 members.")
    normalized_members = tuple(
        (_zigbee_group_ieee(ieee), _z2m_endpoint_id(endpoint, "member endpoint"))
        for ieee, endpoint in members
    )
    if len(normalized_members) != len(set(normalized_members)):
        raise ZigbeeGroupConfigurationError("Group members must not contain duplicates.")

    if backend == BACKEND_ZHA:
        payload_members = [
            {"ieee": ieee, "endpoint_id": endpoint}
            for ieee, endpoint in normalized_members
        ]
        try:
            raw = await async_ws_command(
                hass,
                "zha/group/add",
                {"group_name": name, "members": payload_members},
                timeout=Z2M_REQUEST_TIMEOUT_SECONDS,
            )
            created = normalize_zha_group(raw)
        except (WsDispatchError, ZigbeeGroupConfigurationError) as exc:
            raise RadioError("ZHA could not safely create and confirm the group.") from exc
        expected = set(normalized_members)
        if created["name"] != name or {
            (item["ieee"], item["endpoint"]) for item in created["members"]
        } != expected:
            raise RadioError("ZHA returned a mismatched group after creation.")
        return created

    if backend != BACKEND_Z2M:
        raise RadioError(f"Unknown Zigbee backend: {backend}")
    try:
        response = await async_z2m_request(
            hass, "group/add", {"friendly_name": name}
        )
    except (RadioError, TimeoutError) as exc:
        raise RadioError(
            "Zigbee2MQTT could not create the group. Read groups before retrying."
        ) from exc
    group_id = _zigbee_group_id(response.get("id"))
    if response.get("friendly_name") != name:
        raise RadioError("Zigbee2MQTT returned a mismatched group after creation.")
    added: list[tuple[str, int]] = []
    try:
        for ieee, endpoint in normalized_members:
            await async_z2m_request(
                hass,
                "group/members/add",
                {"group": group_id, "device": ieee, "endpoint": endpoint},
            )
            added.append((ieee, endpoint))
    except Exception as exc:  # noqa: BLE001 - compensate a partially-created group
        rollback_failed = False
        for ieee, endpoint in reversed(added):
            try:
                await async_z2m_request(
                    hass,
                    "group/members/remove",
                    {
                        "group": group_id,
                        "device": ieee,
                        "endpoint": endpoint,
                        "skip_disable_reporting": True,
                    },
                )
            except Exception:  # noqa: BLE001
                rollback_failed = True
        try:
            await async_z2m_request(
                hass, "group/remove", {"id": group_id, "force": False}
            )
        except Exception:  # noqa: BLE001
            rollback_failed = True
        suffix = " Cleanup was incomplete; inspect groups before retrying." if rollback_failed else " The empty group was rolled back."
        raise RadioError(f"Zigbee2MQTT could not add every requested member.{suffix}") from exc

    expected = set(normalized_members)
    try:
        groups = await _async_confirm_zigbee_group(
            hass,
            backend,
            lambda current: (
                (group := _group_by_id(current, group_id)) is not None
                and group["name"] == name
                and {(item["ieee"], item["endpoint"]) for item in group["members"]}
                == expected
            ),
        )
    except TimeoutError as exc:
        raise RadioError(
            "Timed out waiting for Zigbee2MQTT to confirm the new group. Read groups again before retrying."
        ) from exc
    confirmed_created = _group_by_id(groups, group_id)
    assert confirmed_created is not None
    return confirmed_created


async def async_set_zigbee_group_members(
    hass: HomeAssistant,
    *,
    backend: str,
    group: dict[str, Any],
    operation: str,
    members: tuple[tuple[str, int], ...],
) -> dict[str, Any]:
    """Add/remove exact members and confirm the complete resulting group state."""
    if operation not in {"add", "remove"}:
        raise ZigbeeGroupConfigurationError("operation must be add or remove.")
    group_id = _zigbee_group_id(group.get("id"))
    normalized = tuple(
        (_zigbee_group_ieee(ieee), _z2m_endpoint_id(endpoint, "member endpoint"))
        for ieee, endpoint in members
    )
    if not normalized or len(normalized) != len(set(normalized)):
        raise ZigbeeGroupConfigurationError("members must be a non-empty list without duplicates.")
    before = {(item["ieee"], item["endpoint"]) for item in group["members"]}
    requested = set(normalized)
    expected = before | requested if operation == "add" else before - requested
    if operation == "remove" and not expected:
        raise ZigbeeGroupConfigurationError(
            "Removing every member would leave an unanchored group; remove the group instead."
        )
    expected_configuration = {
        **group,
        "members": [
            {"ieee": ieee, "endpoint": endpoint}
            for ieee, endpoint in sorted(expected)
        ],
    }
    expected_hash = zigbee_group_hash(backend, expected_configuration)

    if backend == BACKEND_ZHA:
        try:
            raw = await async_ws_command(
                hass,
                f"zha/group/members/{operation}",
                {
                    "group_id": group_id,
                    "members": [
                        {"ieee": ieee, "endpoint_id": endpoint}
                        for ieee, endpoint in normalized
                    ],
                },
                timeout=Z2M_REQUEST_TIMEOUT_SECONDS,
            )
            after = normalize_zha_group(raw)
        except (WsDispatchError, ZigbeeGroupConfigurationError) as exc:
            raise RadioError(
                "ZHA could not confirm the complete group membership change. Read groups before retrying because some endpoints may have changed."
            ) from exc
        if zigbee_group_hash(backend, after) != expected_hash:
            raise RadioError("ZHA returned a mismatched group membership result.")
        return after

    if backend != BACKEND_Z2M:
        raise RadioError(f"Unknown Zigbee backend: {backend}")
    applied: list[tuple[str, int]] = []
    try:
        for ieee, endpoint in normalized:
            payload: dict[str, Any] = {
                "group": group_id,
                "device": ieee,
                "endpoint": endpoint,
            }
            if operation == "remove":
                payload["skip_disable_reporting"] = True
            await async_z2m_request(hass, f"group/members/{operation}", payload)
            applied.append((ieee, endpoint))
    except Exception as exc:  # noqa: BLE001 - compensate a partial batch
        inverse = "remove" if operation == "add" else "add"
        rollback_failed = False
        for ieee, endpoint in reversed(applied):
            payload = {
                "group": group_id,
                "device": ieee,
                "endpoint": endpoint,
            }
            if inverse == "remove":
                payload["skip_disable_reporting"] = True
            try:
                await async_z2m_request(
                    hass, f"group/members/{inverse}", payload
                )
            except Exception:  # noqa: BLE001
                rollback_failed = True
        suffix = (
            " Rollback was incomplete; read groups before retrying."
            if rollback_failed
            else " The completed endpoints were rolled back."
        )
        raise RadioError(
            f"Zigbee2MQTT could not change every requested group member.{suffix}"
        ) from exc
    try:
        groups = await _async_confirm_zigbee_group(
            hass,
            backend,
            lambda current: (
                (updated := _group_by_id(current, group_id)) is not None
                and zigbee_group_hash(backend, updated) == expected_hash
            ),
        )
    except TimeoutError as exc:
        raise RadioError(
            "Timed out waiting for Zigbee2MQTT to confirm group membership. Read groups again before retrying."
        ) from exc
    confirmed_after = _group_by_id(groups, group_id)
    assert confirmed_after is not None
    return confirmed_after


async def async_remove_zigbee_group(
    hass: HomeAssistant, *, backend: str, group: dict[str, Any]
) -> None:
    """Remove one resolved group without force and confirm that it is absent."""
    group_id = _zigbee_group_id(group.get("id"))
    if backend == BACKEND_ZHA:
        try:
            raw = await async_ws_command(
                hass,
                "zha/group/remove",
                {"group_ids": [group_id]},
                timeout=Z2M_REQUEST_TIMEOUT_SECONDS,
            )
            groups = normalize_zigbee_groups(backend, raw)
        except (WsDispatchError, ZigbeeGroupConfigurationError) as exc:
            raise RadioError("ZHA could not safely remove and confirm the group.") from exc
        if _group_by_id(groups, group_id) is not None:
            raise RadioError("ZHA did not confirm group removal.")
        return
    if backend != BACKEND_Z2M:
        raise RadioError(f"Unknown Zigbee backend: {backend}")
    try:
        response = await async_z2m_request(
            hass, "group/remove", {"id": group_id, "force": False}
        )
    except (RadioError, TimeoutError) as exc:
        raise RadioError(
            "Zigbee2MQTT could not remove the group. Read groups before retrying."
        ) from exc
    if response.get("id") != group_id or response.get("force") is not False:
        raise RadioError("Zigbee2MQTT returned a mismatched group removal result.")
    try:
        await _async_confirm_zigbee_group(
            hass,
            backend,
            lambda groups: _group_by_id(groups, group_id) is None,
        )
    except TimeoutError as exc:
        raise RadioError(
            "Timed out waiting for Zigbee2MQTT to confirm group removal. Read groups again before retrying."
        ) from exc


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


async def async_set_z2m_device_options(
    hass: HomeAssistant, ieee: str, options: dict[str, Any]
) -> dict[str, Any]:
    """Change converter-defined options for one Z2M device by IEEE address."""
    return await async_z2m_request(
        hass,
        "device/options",
        {"id": ieee, "options": options},
        timeout=Z2M_DEVICE_OPTIONS_RESPONSE_TIMEOUT_SECONDS,
    )


async def _async_confirm_z2m_radio_configuration(
    hass: HomeAssistant,
    ieee: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = Z2M_RETAINED_READ_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Poll retained bridge/devices until one exact radio mutation is visible."""
    deadline = hass.loop.time() + timeout
    while True:
        entry = await async_z2m_device_entry(hass, ieee)
        if entry is None:
            raise RadioError("Device is no longer on the Zigbee network.")
        try:
            configuration = z2m_radio_configuration(entry)
        except Z2MRadioConfigurationError as exc:
            raise RadioError(f"Unsafe Zigbee2MQTT radio metadata: {exc}") from exc
        if predicate(configuration):
            return entry, configuration, z2m_radio_configuration_hash(configuration)
        remaining = deadline - hass.loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.25, remaining))


async def async_set_z2m_binding(
    hass: HomeAssistant,
    *,
    operation: str,
    source_ieee: str,
    target_ieee: str,
    source_endpoint: int,
    target_endpoint: int,
    clusters: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Apply and exactly confirm one bounded device-to-device Z2M binding."""
    if operation not in {"bind", "unbind"}:
        raise Z2MRadioConfigurationError("operation must be bind or unbind.")
    payload = {
        "from": source_ieee,
        "from_endpoint": source_endpoint,
        "to": target_ieee,
        "to_endpoint": target_endpoint,
        "clusters": list(clusters),
        # Never let unbinding silently remove manual reporting configuration.
        "skip_disable_reporting": True,
    }
    response = await async_z2m_request(hass, f"device/{operation}", payload)
    returned = response.get("clusters")
    failed = response.get("failed")
    if (
        not isinstance(returned, list)
        or not isinstance(failed, list)
        or failed
        or not all(isinstance(cluster, str) for cluster in returned)
        or set(returned) != set(clusters)
        or len(returned) != len(clusters)
    ):
        raise RadioError(
            f"Zigbee2MQTT did not confirm every requested cluster for {operation}."
        )

    expected_present = operation == "bind"

    def _confirmed(configuration: dict[str, Any]) -> bool:
        try:
            endpoint = _z2m_configuration_endpoint(configuration, source_endpoint)
        except Z2MRadioConfigurationError:
            return False
        present = {
            item["cluster"]
            for item in endpoint["bindings"]
            if item["target"].get("type") == "endpoint"
            and item["target"].get("ieee_address") == target_ieee.lower()
            and item["target"].get("endpoint") == target_endpoint
        }
        return all((cluster in present) is expected_present for cluster in clusters)

    try:
        return await _async_confirm_z2m_radio_configuration(
            hass, source_ieee, _confirmed
        )
    except TimeoutError as exc:
        raise RadioError(
            "Timed out waiting for Zigbee2MQTT retained state to confirm the binding change. "
            "Read both devices again before retrying because the change may have applied."
        ) from exc


async def async_set_zha_binding(
    hass: HomeAssistant,
    *,
    operation: str,
    source_ieee: str,
    target_ieee: str,
) -> None:
    """Ask ZHA to bind/unbind every pair its own binding helper deems valid."""
    if operation not in {"bind", "unbind"}:
        raise RadioError("operation must be bind or unbind.")
    try:
        await async_ws_command(
            hass,
            f"zha/devices/{operation}",
            {"source_ieee": source_ieee, "target_ieee": target_ieee},
            timeout=Z2M_REQUEST_TIMEOUT_SECONDS,
        )
    except WsDispatchError as exc:
        raise RadioError(str(exc)) from exc


async def async_configure_z2m_reporting(
    hass: HomeAssistant,
    *,
    ieee: str,
    desired: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Configure and exactly confirm one Z2M reporting record."""
    payload = {"id": ieee, **desired}
    response = await async_z2m_request(
        hass, "device/reporting/configure", payload
    )
    for key, value in payload.items():
        if key == "reportable_change" and value is None:
            continue
        if key not in response or not _same_scalar(response[key], value):
            raise RadioError(
                "Zigbee2MQTT returned an incomplete or mismatched reporting confirmation."
            )

    endpoint_id = desired["endpoint"]
    reporting = {key: value for key, value in desired.items() if key != "endpoint"}

    def _confirmed(configuration: dict[str, Any]) -> bool:
        try:
            endpoint = _z2m_configuration_endpoint(configuration, endpoint_id)
        except Z2MRadioConfigurationError:
            return False
        return any(item == reporting for item in endpoint["configured_reportings"])

    try:
        return await _async_confirm_z2m_radio_configuration(hass, ieee, _confirmed)
    except TimeoutError as exc:
        raise RadioError(
            "Timed out waiting for Zigbee2MQTT retained state to confirm reporting. "
            "Read the device again before retrying because the change may have applied."
        ) from exc
