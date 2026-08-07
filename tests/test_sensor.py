"""Tests for Phoenix MCP sensor platform."""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import secrets
import uuid
from datetime import timedelta
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import UnitOfTime

from custom_components.phoenix_mcp.const import DOMAIN, TOKEN_PREFIX
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.sensor import (
    PhoenixTokenSensor,
    _SENSOR_TYPES,
    _make_sensors,
    async_create_token_sensors,
    async_remove_token_sensors,
    async_setup_entry,
)
from custom_components.phoenix_mcp.token_store import token_name_slug as _token_slug
from custom_components.phoenix_mcp.token_store import TokenRecord, TokenStore


def _make_token(
    name: str = "my-token",
    revoked: bool = False,
    expires_at=None,
    last_used_at=None,
) -> TokenRecord:
    from homeassistant.util.dt import utcnow

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return TokenRecord(
        id=str(uuid.uuid4()),
        name=name,
        token_hash=token_hash,
        created_at=utcnow(),
        created_by="user1",
        revoked=revoked,
        expires_at=expires_at,
        last_used_at=last_used_at,
    )


def _make_data(tokens: list[TokenRecord] | None = None) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.list_tokens = MagicMock(return_value=tokens or [])
    store.async_lock = asyncio.Lock()
    rate_limiter = MagicMock(spec=RateLimiter)
    audit = MagicMock(spec=AuditLog)
    return PhoenixData(
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
    )


def _make_hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return entry


# --- _token_slug ---

def test_token_slug_lowercases_and_replaces_hyphens():
    token = _make_token(name="My-Token")
    assert _token_slug(token.name) == "my_token"


def test_token_slug_no_hyphens_unchanged():
    token = _make_token(name="mytoken")
    assert _token_slug(token.name) == "mytoken"


# --- _make_sensors ---

def test_make_sensors_returns_six():
    token = _make_token(name="alpha")
    data = _make_data()
    sensors = _make_sensors(token, data)
    assert len(sensors) == 6


def test_make_sensors_unique_ids():
    """Identity is the token's id, never its name.

    The name is mutable, and while the slug WAS the identity every rename deleted
    six registry rows and created six new ones under new entity_ids, so anything
    naming one broke. The device was already keyed on the id; these now agree.
    """
    token = _make_token(name="alpha")
    data = _make_data()
    sensors = _make_sensors(token, data)
    unique_ids = [s._attr_unique_id for s in sensors]
    expected = [
        f"phoenix_mcp_{token.id}_status",
        f"phoenix_mcp_{token.id}_request_count",
        f"phoenix_mcp_{token.id}_denied_count",
        f"phoenix_mcp_{token.id}_rate_limit_hits",
        f"phoenix_mcp_{token.id}_last_access",
        f"phoenix_mcp_{token.id}_expires_in",
    ]
    assert unique_ids == expected
    assert not any("alpha" in uid for uid in unique_ids)


def test_make_sensors_names_come_from_the_translation_catalog():
    """Names moved from a .title() call to HA's own entity translations.

    Asserting the English too, not just the key: these are the friendly names an
    existing dashboard or automation refers to, so they had to stay byte-identical
    to what .title() produced.
    """
    import json
    import pathlib

    token = _make_token(name="my-token")
    data = _make_data()
    sensors = _make_sensors(token, data)
    keys = [s._attr_translation_key for s in sensors]
    assert keys[0] == "status"
    assert keys[1] == "request_count"
    assert keys[4] == "last_access"

    catalog = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent
         / "custom_components" / "phoenix_mcp" / "translations" / "en.json").read_text()
    )["entity"]["sensor"]
    assert catalog["status"]["name"] == "Status"
    assert catalog["request_count"]["name"] == "Request Count"
    assert catalog["last_access"]["name"] == "Last Access"
    # Every sensor type must have an entry, or HA falls back to the object id.
    assert set(catalog) == {s._sensor_type for s in sensors}


# --- status sensor ---

def test_status_active():
    token = _make_token()
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "status", data)
    assert sensor.native_value == "active"


def test_status_revoked():
    token = _make_token(revoked=True)
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "status", data)
    assert sensor.native_value == "revoked"


def test_status_expired():
    from homeassistant.util.dt import utcnow

    token = _make_token(expires_at=utcnow() - timedelta(hours=1))
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "status", data)
    assert sensor.native_value == "expired"


# --- counter sensors ---

def test_request_count_zero_when_no_counters():
    token = _make_token()
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "request_count", data)
    assert sensor.native_value == 0


def test_request_count_from_token_counters():
    token = _make_token()
    data = _make_data()
    data.token_counters[token.id] = {"request_count": 42, "denied_count": 3, "rate_limit_hits": 1}
    sensor = PhoenixTokenSensor(token, "request_count", data)
    assert sensor.native_value == 42


def test_denied_count_from_token_counters():
    token = _make_token()
    data = _make_data()
    data.token_counters[token.id] = {"request_count": 10, "denied_count": 5, "rate_limit_hits": 0}
    sensor = PhoenixTokenSensor(token, "denied_count", data)
    assert sensor.native_value == 5


def test_rate_limit_hits_from_token_counters():
    token = _make_token()
    data = _make_data()
    data.token_counters[token.id] = {"request_count": 10, "denied_count": 0, "rate_limit_hits": 7}
    sensor = PhoenixTokenSensor(token, "rate_limit_hits", data)
    assert sensor.native_value == 7


def test_denied_count_zero_when_no_counters():
    token = _make_token()
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "denied_count", data)
    assert sensor.native_value == 0


# --- last_access sensor ---

def test_last_access_never_when_none():
    token = _make_token(last_used_at=None)
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "last_access", data)
    assert sensor.native_value is None


def test_last_access_returns_iso_string():
    from homeassistant.util.dt import utcnow

    ts = utcnow()
    token = _make_token(last_used_at=ts)
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "last_access", data)
    assert sensor.native_value == ts.isoformat()


# --- expires_in sensor ---

def test_expires_in_is_unknown_without_an_expiry():
    """No expiry reports None (HA "unknown"), never a sentence.

    It returned the English literal "No expiry" until 2026-07-31, which made one
    sensor alternate between a number carrying a unit and untranslated prose.
    The unit and state class now stay attached in both cases, so the sensor has
    one type and history and statistics work for it.
    """
    token = _make_token(expires_at=None)
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "expires_in", data)
    assert sensor.native_value is None
    assert sensor.state_class is SensorStateClass.MEASUREMENT
    assert sensor.native_unit_of_measurement == UnitOfTime.DAYS


def test_expires_in_carries_the_same_unit_with_and_without_an_expiry():
    """The unit must not depend on the value, or statistics reject the series."""
    from homeassistant.util.dt import utcnow

    data = _make_data()
    with_expiry = PhoenixTokenSensor(_make_token(expires_at=utcnow() + timedelta(days=5)), "expires_in", data)
    without = PhoenixTokenSensor(_make_token(expires_at=None), "expires_in", data)
    assert with_expiry.native_unit_of_measurement == without.native_unit_of_measurement
    assert with_expiry.state_class is without.state_class


def test_expires_in_returns_days():
    from homeassistant.util.dt import utcnow

    token = _make_token(expires_at=utcnow() + timedelta(days=5))
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "expires_in", data)
    assert sensor.native_value == 5


def test_expires_in_partial_day_rounds_up():
    from homeassistant.util.dt import utcnow

    token = _make_token(expires_at=utcnow() + timedelta(hours=2))
    data = _make_data()
    sensor = PhoenixTokenSensor(token, "expires_in", data)
    assert sensor.native_value == 1


# --- async_setup_entry ---

@pytest.mark.asyncio
async def test_setup_entry_registers_callback_and_adds_sensors():
    token = _make_token(name="tok-one")
    data = _make_data(tokens=[token])
    hass = _make_hass(data)
    entry = _make_entry()
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    assert data.async_add_entities_cb is add_entities
    assert "tok_one" in data.platform_entities
    assert len(data.platform_entities["tok_one"]) == 6
    add_entities.assert_called_once()
    added = add_entities.call_args.args[0]
    assert len(added) == 6


@pytest.mark.asyncio
async def test_setup_entry_no_tokens_does_not_call_add_entities():
    data = _make_data(tokens=[])
    hass = _make_hass(data)
    entry = _make_entry()
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    assert data.async_add_entities_cb is add_entities
    add_entities.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_multiple_tokens():
    tokens = [_make_token(name="alpha"), _make_token(name="beta")]
    data = _make_data(tokens=tokens)
    hass = _make_hass(data)
    entry = _make_entry()
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    assert "alpha" in data.platform_entities
    assert "beta" in data.platform_entities
    added = add_entities.call_args.args[0]
    assert len(added) == 12


# --- async_create_token_sensors ---

@pytest.mark.asyncio
async def test_create_token_sensors_adds_to_platform_entities():
    token = _make_token(name="new-tok")
    data = _make_data()
    data.async_add_entities_cb = MagicMock()
    hass = _make_hass(data)
    entry = _make_entry()

    await async_create_token_sensors(hass, entry, token)

    assert "new_tok" in data.platform_entities
    assert len(data.platform_entities["new_tok"]) == 6
    data.async_add_entities_cb.assert_called_once()


@pytest.mark.asyncio
async def test_create_token_sensors_noop_when_no_callback():
    token = _make_token(name="new-tok")
    data = _make_data()
    data.async_add_entities_cb = None
    hass = _make_hass(data)
    entry = _make_entry()

    await async_create_token_sensors(hass, entry, token)

    assert "new_tok" not in data.platform_entities


# --- async_remove_token_sensors ---

@pytest.mark.asyncio
async def test_remove_token_sensors_calls_async_remove_on_each():
    data = _make_data()
    hass = _make_hass(data)

    sensor1 = MagicMock(spec=PhoenixTokenSensor)
    sensor1.async_remove = AsyncMock()
    sensor1.unique_id = None
    sensor2 = MagicMock(spec=PhoenixTokenSensor)
    sensor2.async_remove = AsyncMock()
    sensor2.unique_id = None
    data.platform_entities["gone_tok"] = [sensor1, sensor2]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=MagicMock()), \
         patch("homeassistant.helpers.device_registry.async_get", return_value=MagicMock()):
        await async_remove_token_sensors(hass, "gone_tok")

    sensor1.async_remove.assert_called_once()
    sensor2.async_remove.assert_called_once()
    assert "gone_tok" not in data.platform_entities


@pytest.mark.asyncio
async def test_remove_token_sensors_unknown_slug_is_noop():
    data = _make_data()
    hass = _make_hass(data)

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=MagicMock()), \
         patch("homeassistant.helpers.device_registry.async_get", return_value=MagicMock()):
        await async_remove_token_sensors(hass, "nonexistent")


@pytest.mark.asyncio
async def test_remove_token_sensors_pops_from_platform_entities():
    data = _make_data()
    hass = _make_hass(data)

    sensor = MagicMock(spec=PhoenixTokenSensor)
    sensor.async_remove = AsyncMock()
    sensor.unique_id = None
    data.platform_entities["gone_tok"] = [sensor]
    data.platform_entities["other_tok"] = [MagicMock()]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=MagicMock()), \
         patch("homeassistant.helpers.device_registry.async_get", return_value=MagicMock()):
        await async_remove_token_sensors(hass, "gone_tok")

    assert "gone_tok" not in data.platform_entities
    assert "other_tok" in data.platform_entities


# --- update_token_counter sensor-write debounce --------------------------------


class TestCounterWriteDebounce:
    """update_token_counter coalesces sensor writes into one per debounce window.

    request_count/last_access change on every request; immediate writes meant one
    state_changed event and recorder row per agent call. Counters themselves stay
    immediately current in data.token_counters (the admin API reads those).
    """

    async def test_writes_coalesce_into_one_flush(self, hass):
        from pytest_homeassistant_custom_component.common import async_fire_time_changed
        from homeassistant.util.dt import utcnow

        from custom_components.phoenix_mcp.const import SENSOR_WRITE_DEBOUNCE_SECONDS
        from custom_components.phoenix_mcp.helpers import update_token_counter

        data = _make_data()
        data.hass = hass
        sensor = MagicMock()
        sensor.hass = hass
        data.token_id_sensors["t1"] = [sensor]

        update_token_counter(data, "t1", "allowed")
        update_token_counter(data, "t1", "denied")
        update_token_counter(data, "t1", "allowed")

        # Counters are current immediately; sensor writes are pending.
        assert data.token_counters["t1"]["request_count"] == 3
        assert data.token_counters["t1"]["denied_count"] == 1
        assert sensor.async_write_ha_state.call_count == 0
        assert data.sensor_flush_cancel is not None
        assert data.sensor_write_dirty == {"t1"}

        async_fire_time_changed(hass, utcnow() + timedelta(seconds=SENSOR_WRITE_DEBOUNCE_SECONDS + 1))
        await hass.async_block_till_done()

        # One write for three requests; state cleared for the next window.
        assert sensor.async_write_ha_state.call_count == 1
        assert data.sensor_write_dirty == set()
        assert data.sensor_flush_cancel is None

    async def test_flush_skips_archived_token(self, hass):
        from pytest_homeassistant_custom_component.common import async_fire_time_changed
        from homeassistant.util.dt import utcnow

        from custom_components.phoenix_mcp.const import SENSOR_WRITE_DEBOUNCE_SECONDS
        from custom_components.phoenix_mcp.helpers import update_token_counter

        data = _make_data()
        data.hass = hass
        sensor = MagicMock()
        sensor.hass = hass
        data.token_id_sensors["t1"] = [sensor]

        update_token_counter(data, "t1", "allowed")
        # Token revoked inside the window: its sensor registry entry is removed.
        del data.token_id_sensors["t1"]

        async_fire_time_changed(hass, utcnow() + timedelta(seconds=SENSOR_WRITE_DEBOUNCE_SECONDS + 1))
        await hass.async_block_till_done()

        assert sensor.async_write_ha_state.call_count == 0

    def test_writes_immediately_without_hass(self):
        from custom_components.phoenix_mcp.helpers import update_token_counter

        data = _make_data()  # data.hass is None (direct construction)
        sensor = MagicMock()
        sensor.hass = MagicMock()
        data.token_id_sensors["t1"] = [sensor]

        update_token_counter(data, "t1", "allowed")

        assert sensor.async_write_ha_state.call_count == 1
        assert data.sensor_write_dirty == set()

    def test_flush_sensor_writes_is_marked_callback(self):
        # flush_sensor_writes calls sensor.async_write_ha_state(), which requires
        # the event loop thread. Without @callback, HA's HassJob type-inference
        # sees a plain function passed to async_call_later and dispatches it to
        # the executor thread pool instead, which is exactly the thread-safety
        # violation ("calls async_write_ha_state from a thread other than the
        # event loop") this decorator prevents. A MagicMock sensor in the
        # coalesce test above can't catch a missing decorator (mocks don't care
        # what thread they're called from), so this asserts the marker directly.
        from homeassistant.core import is_callback
        from custom_components.phoenix_mcp.helpers import flush_sensor_writes

        assert is_callback(flush_sensor_writes)


# --- token identity survives a rename (real entity registry) ------------------
#
# These use the REAL entity and device registries rather than a MagicMock hass,
# because the whole defect lives in registry bookkeeping: a MagicMock records the
# calls and asserts nothing about identity, which is why the previous tests
# pinned the hazardous implementation instead of catching it. The sensors are
# registered by hand rather than through the platform so the test stays about
# identity and does not need a full config entry set up.

class TestTokenRenameKeepsSensorIdentity:
    """A rename must change what is DISPLAYED, never what is ADDRESSED."""

    @staticmethod
    def _register(hass, token):
        """Put one token's six sensors in the registry under the current scheme."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.sensor import (
            _SENSOR_TYPES, _device_name, _unique_id,
        )

        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        device = device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, token.id)},
            name=_device_name(token.name),
        )
        return {
            sensor_type: entity_reg.async_get_or_create(
                "sensor", DOMAIN, _unique_id(token.id, sensor_type),
                config_entry=entry, device_id=device.id,
            ).entity_id
            for sensor_type in _SENSOR_TYPES
        }, device.id

    @pytest.mark.asyncio
    async def test_rename_keeps_every_entity_id(self, hass):
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.sensor import (
            _device_name, async_rename_token_sensors,
        )

        token = _make_token(name="alpha")
        data = _make_data(tokens=[token])
        hass.data[DOMAIN] = data
        before, device_id = self._register(hass, token)

        renamed = replace(token, name="beta")
        await async_rename_token_sensors(hass, "alpha", renamed)

        entity_reg = er.async_get(hass)
        after = {
            sensor_type: entity_reg.async_get_entity_id("sensor", DOMAIN, uid)
            for sensor_type, uid in (
                (t, f"phoenix_mcp_{token.id}_{t}") for t in before
            )
        }
        # Same rows, same entity_ids: a dashboard card naming one still resolves.
        assert after == before
        assert all(entity_id is not None for entity_id in after.values())
        # Only the display name moved.
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, token.id)})
        assert device.name == _device_name("beta")
        assert device.id == device_id

    @pytest.mark.asyncio
    async def test_rename_repoints_the_sensors_at_the_new_record(self, hass):
        # The sensors read every value off the TokenRecord they hold. The rebuild
        # this replaced got that for free by constructing new ones; in-place has
        # to do it explicitly or the status sensor reports the stale record.
        from custom_components.phoenix_mcp.sensor import async_rename_token_sensors

        token = _make_token(name="alpha")
        data = _make_data(tokens=[token])
        hass.data[DOMAIN] = data
        sensors = _make_sensors(token, data)
        data.platform_entities["alpha"] = sensors
        data.token_id_sensors[token.id] = sensors

        revoked = replace(token, name="beta", revoked=True)
        await async_rename_token_sensors(hass, "alpha", revoked)

        status = next(s for s in sensors if s._sensor_type == "status")
        assert status.native_value == "revoked"
        # Re-keyed so the slug-keyed revoke path still finds them.
        assert "alpha" not in data.platform_entities
        assert data.platform_entities["beta"] is sensors

    @pytest.mark.asyncio
    async def test_platform_setup_runs_the_migration(self, hass):
        # The migration is only worth anything if setup actually performs it, and
        # calling the helper directly proves nothing about that wiring.
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.sensor import _legacy_unique_id, _unique_id

        token = _make_token(name="alpha")
        data = _make_data(tokens=[token])
        hass.data[DOMAIN] = data
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        entity_reg = er.async_get(hass)
        legacy_id = entity_reg.async_get_or_create(
            "sensor", DOMAIN, _legacy_unique_id("alpha", "status"), config_entry=entry,
        ).entity_id

        await async_setup_entry(hass, entry, MagicMock())

        assert entity_reg.async_get_entity_id(
            "sensor", DOMAIN, _unique_id(token.id, "status")
        ) == legacy_id

    @pytest.mark.asyncio
    async def test_upgrade_rekeys_legacy_rows_in_place(self, hass):
        # Without this an upgrade looks exactly like a rename: the old rows keep
        # their name-derived unique_ids, nothing claims them, and six new
        # entities appear beside them with _2 suffixes.
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.sensor import (
            _SENSOR_TYPES, _async_migrate_unique_ids, _legacy_unique_id, _unique_id,
        )

        token = _make_token(name="alpha")
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        entity_reg = er.async_get(hass)
        legacy = {
            sensor_type: entity_reg.async_get_or_create(
                "sensor", DOMAIN, _legacy_unique_id("alpha", sensor_type), config_entry=entry,
            ).entity_id
            for sensor_type in _SENSOR_TYPES
        }

        _async_migrate_unique_ids(hass, [token])

        for sensor_type, entity_id in legacy.items():
            # Same row, re-keyed: the entity_id an operator already references.
            assert entity_reg.async_get_entity_id(
                "sensor", DOMAIN, _unique_id(token.id, sensor_type)
            ) == entity_id
            assert entity_reg.async_get_entity_id(
                "sensor", DOMAIN, _legacy_unique_id("alpha", sensor_type)
            ) is None

    @pytest.mark.asyncio
    async def test_migration_leaves_an_already_claimed_id_alone(self, hass):
        # A legacy row left over from a pre-migration rename would collide with
        # the token's real row. Re-keying onto a taken unique_id raises, so the
        # leftover is stepped over rather than allowed to abort the rest.
        from homeassistant.helpers import entity_registry as er
        from custom_components.phoenix_mcp.sensor import (
            _async_migrate_unique_ids, _legacy_unique_id, _unique_id,
        )

        token = _make_token(name="alpha")
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        entity_reg = er.async_get(hass)
        stale = entity_reg.async_get_or_create(
            "sensor", DOMAIN, _legacy_unique_id("alpha", "status"), config_entry=entry,
        ).entity_id
        claimed = entity_reg.async_get_or_create(
            "sensor", DOMAIN, _unique_id(token.id, "status"), config_entry=entry,
        ).entity_id

        _async_migrate_unique_ids(hass, [token])

        assert entity_reg.async_get_entity_id(
            "sensor", DOMAIN, _unique_id(token.id, "status")
        ) == claimed
        assert entity_reg.async_get_entity_id(
            "sensor", DOMAIN, _legacy_unique_id("alpha", "status")
        ) == stale


# --- the entity IDs Home Assistant actually generates -------------------------


@pytest.mark.asyncio
async def test_generated_entity_ids_match_the_documentation(hass, enable_custom_integrations):
    """Pin the ID a FRESH install gets, through HA's real naming path.

    docs/operations.html lists these IDs for an operator to put in a dashboard or
    an automation, and they were wrong from the day they were written: the table
    said `sensor.phoenix_mcp_my_token_status` while HA produces
    `sensor.phoenix_mcp_token_my_token_status`. Nothing noticed because no test
    ever created a sensor through the entity platform, which is what composes
    `has_entity_name` device name + entity name into the object id; a hand-built
    registry row skips exactly that step and reproduces neither.
    """
    import logging
    from datetime import timedelta as _timedelta

    from homeassistant.helpers.entity_platform import EntityPlatform

    token = _make_token(name="my-token")
    data = _make_data(tokens=[token])
    hass.data[DOMAIN] = data
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name=DOMAIN, platform=None, scan_interval=_timedelta(seconds=60),
        entity_namespace=None,
    )
    platform.config_entry = entry
    # The sensors carry a translation_key rather than a literal name, and the
    # platform resolves it from the translations HA loads during a real setup.
    # A hand-built platform loads none, so every entity would come out unnamed and
    # collide into _2.._6 suffixes: the object id would be the DEVICE name alone,
    # which reproduces neither what HA generates nor what the docs say. Loading
    # the integration's own catalog keeps this exercising the real composition.
    _catalog = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "custom_components"
         / "phoenix_mcp" / "translations" / "en.json").read_text(encoding="utf-8")
    )["entity"]["sensor"]
    _translations = {
        f"component.{DOMAIN}.entity.sensor.{key}.name": value["name"]
        for key, value in _catalog.items()
    }

    async def _fake_get_translations(language, category, integration):
        return _translations

    # Patched AT the seam HA itself calls, not next to it: PlatformData now owns
    # translation loading and the read-only dictionaries the naming path reads.
    platform.platform_data._async_get_translations = _fake_get_translations
    await platform.platform_data.async_load_translations()

    sensors = _make_sensors(token, data)
    await platform.async_add_entities(sensors)
    await hass.async_block_till_done()

    # Each generated id is compared to the id documented for THAT sensor, and the
    # two sets are compared whole. A prefix assertion plus a separate "the docs
    # mention this string" check was the first attempt and bound nothing: with
    # both, HA could generate `..._wrong_status` and every assertion still passed,
    # which is the same ineffective-guard shape this file exists to prevent.
    docs = (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "operations.html"
    ).read_text(encoding="utf-8")

    generated = {s._sensor_type: s.entity_id for s in sensors}
    documented = {
        sensor_type: f"sensor.phoenix_mcp_token_my_token_{sensor_type}"
        for sensor_type in _SENSOR_TYPES
    }
    assert generated == documented, (
        "Home Assistant generates different entity IDs than docs/operations.html "
        "documents. Operators copy these into dashboards and automations.\n"
        f"  generated: {generated}\n  documented: {documented}"
    )
    # And the table really contains them, so correcting the expectation above
    # without correcting the page cannot pass.
    for sensor_type, entity_id in documented.items():
        assert entity_id in docs, (
            f"docs/operations.html does not list {entity_id} for the "
            f"{sensor_type} sensor."
        )
