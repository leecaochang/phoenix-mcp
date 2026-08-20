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


class RadioError(Exception):
    """A radio backend operation failed or the backend is unavailable."""


class Z2MOptionValidationError(ValueError):
    """A requested Zigbee2MQTT converter option does not match its definition."""


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


def project_z2m_device(
    entry: Any, *, current_options: dict[str, Any] | None = None
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


async def _async_z2m_device_snapshot_with_info(
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
    entry, options, content_hash, _info = await _async_z2m_device_snapshot_with_info(
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
                await _async_z2m_device_snapshot_with_info(hass, ieee)
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
