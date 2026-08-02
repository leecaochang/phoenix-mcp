"""Sensor platform for Phoenix MCP per-token telemetry."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import utcnow

from .const import DOMAIN
from .token_store import token_name_slug

if TYPE_CHECKING:
    from .data import PhoenixData
    from .token_store import TokenRecord

PARALLEL_UPDATES = 0

_SENSOR_TYPES = (
    "status",
    "request_count",
    "denied_count",
    "rate_limit_hits",
    "last_access",
    "expires_in",
)


def _make_sensors(
    token: TokenRecord,
    data: PhoenixData,
) -> list[PhoenixTokenSensor]:
    """Create the full set of sensor entities for one token."""
    slug = token_name_slug(token.name)
    return [PhoenixTokenSensor(token, slug, sensor_type, data) for sensor_type in _SENSOR_TYPES]


class PhoenixTokenSensor(SensorEntity):
    """HA sensor entity representing one telemetry dimension for a Phoenix MCP token.

    One sensor is created per entry in _SENSOR_TYPES per active token. Sensors
    are removed immediately when a token is revoked or archived.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    _NUMERIC_TYPES = frozenset({"request_count", "denied_count", "rate_limit_hits", "expires_in"})
    _COUNT_TYPES = frozenset({"request_count", "denied_count", "rate_limit_hits"})

    def __init__(
        self,
        token: TokenRecord,
        slug: str,
        sensor_type: str,
        data: PhoenixData,
    ) -> None:
        self._token = token
        self._slug = slug
        self._sensor_type = sensor_type
        self._data = data
        self._attr_unique_id = f"phoenix_mcp_{slug}_{sensor_type}"
        # HA resolves the display name from translations/<lang>.json's entity
        # section. The English there is byte-identical to the old .title() output,
        # so an existing dashboard or automation referring to the friendly name
        # reads the same; unique_id and entity_id are untouched either way.
        self._attr_translation_key = sensor_type
        self._device_info = DeviceInfo(
            identifiers={(DOMAIN, token.id)},
            name=f"Phoenix MCP Token: {token.name}",
            manufacturer="Phoenix MCP",
            model="Token Telemetry",
        )

    @property
    def state_class(self) -> SensorStateClass | None:
        if self._sensor_type in self._COUNT_TYPES:
            # Counters are monotonically increasing totals; TOTAL_INCREASING is the
            # correct state class for HA statistics semantics (not MEASUREMENT).
            return SensorStateClass.TOTAL_INCREASING
        if self._sensor_type == "expires_in":
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        # Unconditional: expires_in is always days. These two used to switch off
        # when the token had no expiry, because the value was then the string
        # "No expiry" and a unit on prose is meaningless. The value is now None
        # in that case, which HA reports as unknown with the unit intact.
        if self._sensor_type == "expires_in":
            return UnitOfTime.DAYS
        return None

    @property
    def token_id(self) -> str:
        return self._token.id

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    @property
    def native_value(self) -> str | int | None:
        token = self._token
        sensor_type = self._sensor_type

        if sensor_type == "status":
            if token.revoked:
                return "revoked"
            if token.is_expired():
                return "expired"
            return "active"

        if sensor_type == "request_count":
            return self._data.token_counters.get(token.id, {}).get("request_count", 0)

        if sensor_type == "denied_count":
            return self._data.token_counters.get(token.id, {}).get("denied_count", 0)

        if sensor_type == "rate_limit_hits":
            return self._data.token_counters.get(token.id, {}).get("rate_limit_hits", 0)

        if sensor_type == "last_access":
            if token.last_used_at is None:
                return None
            return token.last_used_at.isoformat()

        if sensor_type == "expires_in":
            # None, not a sentence. A token with no expiry used to report the
            # English string "No expiry", which made one sensor alternate between
            # a number with a unit and untranslated prose: it never localized, it
            # broke history and statistics for the sensor, and any template
            # comparing the value had to handle both types. None renders as
            # HA's own "unknown" in the viewer's language and compares cleanly.
            if token.expires_at is None:
                return None
            delta = token.expires_at - utcnow()
            return max(0, math.ceil(delta.total_seconds() / 86400))

        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize the sensor platform and create sensors for all existing tokens."""
    data: PhoenixData = hass.data[DOMAIN]
    data.async_add_entities_cb = async_add_entities

    sensors: list[PhoenixTokenSensor] = []
    for token in data.store.list_tokens():
        slug = token_name_slug(token.name)
        token_sensors = _make_sensors(token, data)
        data.platform_entities[slug] = token_sensors
        data.token_id_sensors[token.id] = token_sensors
        sensors.extend(token_sensors)

    if sensors:
        async_add_entities(sensors)


async def async_create_token_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    token: TokenRecord,
) -> None:
    """Create and register sensor entities for a newly created token."""
    data: PhoenixData = hass.data[DOMAIN]
    if data.async_add_entities_cb is None:
        return
    slug = token_name_slug(token.name)
    token_sensors = _make_sensors(token, data)
    data.platform_entities[slug] = token_sensors
    data.token_id_sensors[token.id] = token_sensors
    data.async_add_entities_cb(token_sensors)


async def async_remove_token_sensors(
    hass: HomeAssistant,
    token_slug: str,
) -> None:
    """Remove sensor entities for a revoked/archived token and clean up the entity registry.

    Removing from the entity registry prevents 'unavailable' ghost entries after
    the token is gone. The associated device is also removed from the device registry.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    data: PhoenixData = hass.data[DOMAIN]
    sensors = data.platform_entities.pop(token_slug, [])
    if sensors:
        data.token_id_sensors.pop(sensors[0].token_id, None)
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    # Capture device_id before entering the removal loop so we don't rely on
    # registry entries still being present after sensor.async_remove() runs.
    device_id = None
    for sensor in sensors:
        if sensor.unique_id and device_id is None:
            entity_id = entity_reg.async_get_entity_id("sensor", DOMAIN, sensor.unique_id)
            if entity_id:
                entry = entity_reg.async_get(entity_id)
                if entry:
                    device_id = entry.device_id

    for sensor in sensors:
        await sensor.async_remove()
        if sensor.unique_id:
            entity_id = entity_reg.async_get_entity_id("sensor", DOMAIN, sensor.unique_id)
            if entity_id:
                entity_reg.async_remove(entity_id)
    if device_id:
        device_reg.async_remove_device(device_id)
