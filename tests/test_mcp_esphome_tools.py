"""Tests for the ESPHome MCP tools (fleet status, device YAML)."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.tools import esphome
from custom_components.phoenix_mcp.mcp_view import _call_tool, _execute_set_esphome_yaml, async_restore_version
from custom_components.phoenix_mcp.tools.esphome import esphome_availability
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.policy_engine import resolve_esphome_user_service
from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord

_DASHBOARD_PATCH = "custom_components.phoenix_mcp.tools.esphome._esphome_dashboard"


def _token(permissions: PermissionTree | None = None, **caps) -> TokenRecord:
    tree = permissions or PermissionTree(domains={"sensor": PermissionNode(state="GREEN")})
    base = {"cap_diagnostics": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=tree, **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass):
    if name in {"call_service", "dry_run_service"} and isinstance(args.get("service"), str):
        args = {"service": {"domain": args.pop("domain"), "name": args.pop("service"), "data": args.pop("service_data", {})}, **args}
    return await _call_tool(name, args, token, hass, MagicMock())


def _device_info(**overrides):
    """A stand-in for aioesphomeapi DeviceInfo.

    Duck-typed on purpose: aioesphomeapi is an esphome integration requirement
    rather than a Phoenix one and is not installed here, and the tool reads
    these by getattr.
    """
    base = {
        "name": "rf-blaster1",
        "friendly_name": "RF Blaster 1",
        "esphome_version": "2026.6.0",
        "compilation_time": "Jul 1 2026, 10:00:00",
        "manufacturer": "espressif",
        "model": "esp32dev",
        "project_name": None,
        "project_version": None,
        "suggested_area": None,
        "has_deep_sleep": False,
        "webserver_port": 0,
        "uses_password": False,
        "api_encryption_supported": True,
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "bluetooth_mac_address": "AA:BB:CC:DD:EE:00",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user_service(name="transmit_raw", args=(("timings", "INT_ARRAY"), ("repeats", "INT"))):
    return SimpleNamespace(
        name=name,
        args=[SimpleNamespace(name=n, type=SimpleNamespace(name=t)) for n, t in args],
    )


def _runtime(device_info=None, **overrides):
    base = {
        "available": True,
        "expected_disconnect": False,
        "device_info": device_info if device_info is not None else _device_info(),
        "bluetooth_device": None,
        "services": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)




def _esphome_entry(
    hass, entries, *, entity_ids=("sensor.rf_blaster1_rssi",), runtime=None,
    state=ConfigEntryState.LOADED, title="RF Blaster1",
):
    """Register real registry entities and append a fake entry the tool will see."""
    backing = MockConfigEntry(domain="esphome", title=title)
    backing.add_to_hass(hass)
    registry = er.async_get(hass)
    for eid in entity_ids:
        domain, object_id = eid.split(".", 1)
        registry.async_get_or_create(
            domain, "esphome", f"unique-{object_id}",
            config_entry=backing, suggested_object_id=object_id,
        )

    fake = SimpleNamespace(
        entry_id=backing.entry_id, domain="esphome", title=title, state=state, data={})
    if runtime is not None:
        fake.runtime_data = runtime
    entries.append(fake)
    return fake


class _ApprStore:
    """Minimal store the confirm gate can persist a real approval into."""

    def __init__(self) -> None:
        self._p: list = []
        self.async_lock = asyncio.Lock()
        self.async_save = AsyncMock()

    def get_pending_approvals(self) -> list:
        return self._p

    def set_pending_approvals(self, v: list) -> None:
        self._p = v


def _dashboard(data=None, last_update_success=True):
    return SimpleNamespace(data=data or {}, last_update_success=last_update_success)


class TestOverviewGating:
    async def test_cap_denied(self, hass):
        content, outcome, resource = await _call(
            "get_esphome_overview", {}, _token(cap_diagnostics="deny"), hass)
        assert outcome == "denied"
        assert resource == "get_esphome_overview"
        assert "Forbidden" in content["content"][0]["text"]

    async def test_no_esphome_entries(self, hass):
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, outcome, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["count"] == 0
        assert body["devices"] == []


class TestOverviewScoping:
    async def test_device_with_no_accessible_entity_is_absent(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, entity_ids=("light.other_thing",), runtime=_runtime())
        token = _token(permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="GREEN")}))
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, token, hass)
        assert _json(content)["count"] == 0

    async def test_accessible_device_is_present_with_count(self, hass, esphome_entries):
        _esphome_entry(
            hass, esphome_entries,
            entity_ids=("sensor.rf_blaster1_rssi", "sensor.rf_blaster1_uptime"),
            runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        body = _json(content)
        assert body["count"] == 1
        assert body["devices"][0]["accessible_entity_count"] == 2
        assert body["devices"][0]["name"] == "rf-blaster1"


class TestOverviewUnloadedEntries:
    async def test_unloaded_entry_does_not_raise_and_stays_minimal(self, hass, esphome_entries):
        # runtime_data is deleted on unload; reading it would raise AttributeError.
        _esphome_entry(hass, esphome_entries, state=ConfigEntryState.NOT_LOADED, runtime=None,
                             title="Bluetooth Proxy Steve Office")
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, outcome, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert outcome == "allowed"
        device = _json(content)["devices"][0]
        assert device["loaded"] is False
        assert device["title"] == "Bluetooth Proxy Steve Office"
        assert "esphome_version" not in device
        assert "available" not in device

    async def test_unloaded_entry_never_reports_live_fields(self, hass, esphome_entries):
        # A non-loaded entry can still be CARRYING runtime_data (setup that failed
        # after populating it, or a later state change). The LOADED guard is what
        # stops stale live data being reported as current, so this is the case
        # that makes the guard load-bearing rather than decorative.
        _esphome_entry(hass, esphome_entries, state=ConfigEntryState.SETUP_ERROR,
                       runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        device = _json(content)["devices"][0]
        assert device["loaded"] is False
        assert "available" not in device
        assert "esphome_version" not in device
        assert "name" not in device

    async def test_unloaded_entry_still_counted_against_the_tree(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, entity_ids=("light.not_mine",),
                             state=ConfigEntryState.NOT_LOADED, runtime=None)
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert _json(content)["count"] == 0


class TestOverviewSecurityPins:
    async def test_no_mac_address_anywhere(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        raw = content["content"][0]["text"]
        assert "AA:BB:CC:DD:EE:FF" not in raw
        assert "AA:BB:CC:DD:EE:00" not in raw
        assert "mac_address" not in raw

    async def test_entry_data_never_read(self, hass, esphome_entries):
        entry = _esphome_entry(hass, esphome_entries, runtime=_runtime())
        # The real entry.data carries the noise PSK and host; the tool must never
        # read it, so a leak here would show up verbatim in the response.
        entry.data = {"noise_psk": "SUPERSECRETPSK=", "host": "192.168.1.55"}
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        raw = content["content"][0]["text"]
        assert "SUPERSECRETPSK=" not in raw
        assert "192.168.1.55" not in raw

    async def test_dashboard_address_is_not_disclosed(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        dash = _dashboard({"rf-blaster1": {
            "configuration": "rf-blaster1.yaml", "current_version": "2026.6.0",
            "deployed_version": "2026.6.0", "target_platform": "ESP32",
            "address": "rf-blaster1.local", "web_port": 80,
        }})
        with patch(_DASHBOARD_PATCH, return_value=dash):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        raw = content["content"][0]["text"]
        assert "rf-blaster1.local" not in raw
        assert "rf-blaster1.yaml" in raw


class TestOverviewDashboard:
    async def test_absent_dashboard_warns(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        body = _json(content)
        assert any("Device Builder is not available" in w for w in body["warnings"])
        assert "device_builder" not in body["devices"][0]

    async def test_stale_dashboard_warns_but_still_reports(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        dash = _dashboard({"rf-blaster1": {"configuration": "rf-blaster1.yaml",
                                           "current_version": "2026.7.0"}},
                          last_update_success=False)
        with patch(_DASHBOARD_PATCH, return_value=dash):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        body = _json(content)
        assert any("unreachable at its last refresh" in w for w in body["warnings"])
        assert body["devices"][0]["device_builder"]["configuration"] == "rf-blaster1.yaml"

    async def test_update_available_true_when_versions_differ(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(_device_info(esphome_version="2026.6.0")))
        dash = _dashboard({"rf-blaster1": {"configuration": "rf-blaster1.yaml",
                                           "current_version": "2026.7.0"}})
        with patch(_DASHBOARD_PATCH, return_value=dash):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert _json(content)["devices"][0]["device_builder"]["update_available"] is True

    async def test_update_available_false_when_versions_match(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(_device_info(esphome_version="2026.7.0")))
        dash = _dashboard({"rf-blaster1": {"configuration": "rf-blaster1.yaml",
                                           "current_version": "2026.7.0"}})
        with patch(_DASHBOARD_PATCH, return_value=dash):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert _json(content)["devices"][0]["device_builder"]["update_available"] is False

    async def test_unmapped_configurations_reported_as_count_without_names(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        dash = _dashboard({
            "rf-blaster1": {"configuration": "rf-blaster1.yaml", "current_version": "2026.7.0"},
            "rf-blaster3": {"configuration": "rf-blaster3.yaml", "current_version": "2026.7.0"},
            "doorbell": {"configuration": "esphome-web-31fca4.yaml", "current_version": "2026.7.0"},
        })
        with patch(_DASHBOARD_PATCH, return_value=dash):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        raw = content["content"][0]["text"]
        assert _json(content)["unmapped_configurations"] == 2
        assert "rf-blaster3" not in raw
        assert "esphome-web-31fca4.yaml" not in raw


class TestOverviewBluetoothProxy:
    async def test_slot_counts_reported(self, hass, esphome_entries):
        bt = SimpleNamespace(available=True, ble_connections_free=2, ble_connections_limit=3)
        _esphome_entry(hass, esphome_entries, runtime=_runtime(bluetooth_device=bt))
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert _json(content)["devices"][0]["bluetooth_proxy"] == {
            "available": True, "connections_free": 2, "connections_limit": 3}

    async def test_absent_when_not_a_proxy(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert "bluetooth_proxy" not in _json(content)["devices"][0]


class TestUserServiceDispatch:
    """esphome.<device>_<action>: no entity target, device write scope authorizes."""

    async def _register(self, hass, name="rf_blaster1_transmit_raw"):
        calls: list = []

        async def _handler(call):
            calls.append(call)

        hass.services.async_register("esphome", name, _handler)
        return calls

    async def test_dispatched_with_bare_service_data(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        calls = await self._register(hass)
        content, outcome, _ = await _call(
            "call_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw",
             "service_data": {"timings": [1, 2, 3], "repeats": 2}},
            _token(), hass)
        assert outcome == "allowed"
        assert _json(content) == {"success": True}
        assert len(calls) == 1
        # Rule 15 flattening must NOT apply: the device-declared schema has no
        # entity_id field and voluptuous would reject the call outright.
        assert "entity_id" not in calls[0].data
        assert calls[0].data == {"timings": [1, 2, 3], "repeats": 2}

    async def test_read_only_token_refused(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        calls = await self._register(hass)
        token = _token(permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        content, outcome, _ = await _call(
            "call_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw", "service_data": {}},
            token, hass)
        assert outcome == "denied"
        assert "Forbidden" in content["content"][0]["text"]
        assert calls == []

    async def test_stale_service_with_no_loaded_owner_refused(self, hass, esphome_entries):
        # The esphome integration never removes its dynamically registered
        # services on unload, so a registered service can outlive its device.
        calls = await self._register(hass, "ghost_device_transmit_raw")
        content, outcome, _ = await _call(
            "call_service",
            {"domain": "esphome", "service": "ghost_device_transmit_raw", "service_data": {}},
            _token(), hass)
        assert outcome == "denied"
        assert "Forbidden" in content["content"][0]["text"]
        assert calls == []

    async def test_stale_service_of_a_still_loaded_device_refused(self, hass, esphome_entries):
        # The sharper case: the device IS loaded and its name prefixes the stale
        # service, but its current firmware no longer declares that action. Prefix
        # matching alone would happily dispatch it, so the owning entry's declared
        # action set is what has to decide.
        _esphome_entry(hass, esphome_entries,
                       runtime=_runtime(services={1: _user_service("transmit_raw")}))
        calls = await self._register(hass, "rf_blaster1_legacy_blast")
        content, outcome, _ = await _call(
            "call_service",
            {"domain": "esphome", "service": "rf_blaster1_legacy_blast", "service_data": {}},
            _token(), hass)
        assert outcome == "denied"
        assert "Forbidden" in content["content"][0]["text"]
        assert calls == []
        assert resolve_esphome_user_service(hass, "rf_blaster1_legacy_blast") is None

    async def test_longest_prefix_wins_over_shadowing_device(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, entity_ids=("sensor.short_rssi",),
                       runtime=_runtime(_device_info(name="rf-blaster1"),
                                        services={1: _user_service("ext_transmit_raw")}))
        _esphome_entry(hass, esphome_entries, entity_ids=("sensor.long_rssi",),
                       runtime=_runtime(_device_info(name="rf-blaster1-ext"),
                                        services={1: _user_service("transmit_raw")}))
        # Both devices can claim rf_blaster1_ext_transmit_raw; the longer device
        # name is the real owner.
        entry = resolve_esphome_user_service(hass, "rf_blaster1_ext_transmit_raw")
        assert entry.runtime_data.device_info.name == "rf-blaster1-ext"

    async def test_schema_rejection_is_surfaced_not_swallowed(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))

        async def _handler(call):
            raise vol.Invalid("extra keys not allowed @ data['timing']")

        hass.services.async_register("esphome", "rf_blaster1_transmit_raw", _handler)
        content, outcome, _ = await _call(
            "call_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw",
             "service_data": {"timing": [1]}},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "extra keys not allowed" in content["content"][0]["text"]

    async def test_audit_resource_matches_other_service_calls(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        await self._register(hass)
        _, _, resource = await _call(
            "call_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw", "service_data": {}},
            _token(), hass)
        assert resource == "service:esphome/rf_blaster1_transmit_raw"


class TestUserServiceDryRun:
    async def test_reports_signature_and_argument_mismatches(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        content, outcome, _ = await _call(
            "dry_run_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw",
             "service_data": {"timings": [1], "bogus": 1}},
            _token(cap_search="allow"), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["device_action"] is True
        assert body["would_execute"] is True
        assert body["declared_args"] == [
            {"name": "timings", "type": "int_array"},
            {"name": "repeats", "type": "int"},
        ]
        assert body["unknown_args"] == ["bogus"]
        assert body["missing_args"] == ["repeats"]

    async def test_read_only_token_predicts_denied(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        token = _token(cap_search="allow", permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        content, _, _ = await _call(
            "dry_run_service",
            {"domain": "esphome", "service": "rf_blaster1_transmit_raw", "service_data": {}},
            token, hass)
        body = _json(content)
        assert body["would_execute"] is False
        assert body["predicted_outcome"] == "denied"


class TestUserServiceDiscovery:
    async def test_find_available_actions_lists_device_actions(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        hass.states.async_set("sensor.rf_blaster1_rssi", "-60")
        content, outcome, _ = await _call(
            "find_available_actions", {"entity_id": "sensor.rf_blaster1_rssi"},
            _token(cap_search="allow"), hass)
        assert outcome == "allowed"
        rows = [a for a in _json(content)["actions"] if a["service"].startswith("esphome.")]
        assert len(rows) == 1
        assert rows[0]["service"] == "esphome.rf_blaster1_transmit_raw"
        assert rows[0]["available"] is True
        # The signature matters: these schemas are device-defined, so without it
        # an agent has no way to learn what the action expects.
        assert rows[0]["args"] == [
            {"name": "timings", "type": "int_array"},
            {"name": "repeats", "type": "int"},
        ]

    async def test_read_only_entity_marks_action_unavailable(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        hass.states.async_set("sensor.rf_blaster1_rssi", "-60")
        token = _token(cap_search="allow", permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        content, _, _ = await _call(
            "find_available_actions", {"entity_id": "sensor.rf_blaster1_rssi"}, token, hass)
        rows = [a for a in _json(content)["actions"] if a["service"].startswith("esphome.")]
        assert rows[0]["available"] is False
        assert rows[0]["reason"] == "read-only access to this device"

    async def test_non_esphome_entity_gains_no_rows(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime(services={1: _user_service()}))
        hass.states.async_set("sensor.unrelated", "1")
        content, _, _ = await _call(
            "find_available_actions", {"entity_id": "sensor.unrelated"},
            _token(cap_search="allow"), hass)
        assert [a for a in _json(content)["actions"] if a["service"].startswith("esphome.")] == []


DEVICE_YAML = '''esphome:
  name: rf-blaster1

# Enable Home Assistant API
api:
  encryption:
    key: "REALAPIKEY123456="

ota:
  - platform: esphome
    password: "realotapassword"

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

uart:
  baud_rate: 9600
'''

SECRETS_YAML = 'wifi_ssid: MyHomeNetwork\nwifi_password: housepassword1\n'




def _yaml_token(**caps) -> TokenRecord:
    base = {"cap_esphome_yaml": "allow"}
    base.update(caps)
    return _token(**base)


class TestDeviceYamlRead:
    async def test_cap_denied(self, hass, esphome_dir):
        content, outcome, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"},
            _yaml_token(cap_esphome_yaml="deny"), hass)
        assert outcome == "denied"
        # A fully denied token must learn nothing about the file or its payload.
        assert "rf-blaster1" not in content["content"][0]["text"]

    async def test_credentials_masked_and_hash_is_of_raw_bytes(self, hass, esphome_dir):
        content, outcome, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert "REALAPIKEY123456=" not in content["content"][0]["text"]
        assert "realotapassword" not in content["content"][0]["text"]
        assert body["redacted_paths"] == ["api.encryption.key", "ota[0].password"]
        assert body["content_hash"] == content_hash(DEVICE_YAML)
        # Formatting and tag references survive so an edit round-trips cleanly.
        assert "# Enable Home Assistant API" in body["content"]
        assert "ssid: !secret wifi_ssid" in body["content"]
        assert "baud_rate: 9600" in body["content"]

    async def test_defined_secrets_exposes_names_never_values(self, hass, esphome_dir):
        content, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        raw = content["content"][0]["text"]
        assert _json(content)["defined_secrets"] == ["wifi_password", "wifi_ssid"]
        assert "housepassword1" not in raw
        assert "MyHomeNetwork" not in raw

    async def test_listing_omits_excluded_entries(self, hass, esphome_dir):
        content, outcome, _ = await _call("get_esphome_yaml", {}, _yaml_token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        names = [row["file"] for row in body["files"]]
        assert names == ["rf-blaster1.yaml"]
        raw = content["content"][0]["text"]
        assert "secrets.yaml" not in raw
        assert "archive" not in raw
        assert "device-builder" not in raw

    @pytest.mark.parametrize("bad", [
        "secrets.yaml", "SECRETS.YAML", "archive/old-device.yaml",
        ".device-builder.json", "../configuration.yaml", "/etc/passwd",
        "rf-blaster1.yaml/../secrets.yaml", "notes.txt",
    ])
    async def test_jail_refuses(self, hass, esphome_dir, bad):
        content, outcome, _ = await _call(
            "get_esphome_yaml", {"file": bad}, _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "housepassword1" not in content["content"][0]["text"]

    async def test_symlink_to_secrets_refused(self, hass, esphome_dir):
        (esphome_dir / "sneaky.yaml").symlink_to(esphome_dir / "secrets.yaml")
        content, outcome, _ = await _call(
            "get_esphome_yaml", {"file": "sneaky.yaml"}, _yaml_token(), hass)
        # The rules re-run on the RESOLVED path, so renaming cannot launder it.
        assert outcome == "invalid_request"
        assert "housepassword1" not in content["content"][0]["text"]

    async def test_symlink_out_of_jail_refused(self, hass, esphome_dir, tmp_path):
        outside = tmp_path / "configuration.yaml"
        outside.write_text("homeassistant:\n")
        (esphome_dir / "escape.yaml").symlink_to(outside)
        _, outcome, _ = await _call(
            "get_esphome_yaml", {"file": "escape.yaml"}, _yaml_token(), hass)
        assert outcome == "invalid_request"

    async def test_unparseable_file_fails_closed(self, hass, esphome_dir):
        (esphome_dir / "broken.yaml").write_text('api:\n  key: "unclosed\n  x: [1,\n')
        content, outcome, _ = await _call(
            "get_esphome_yaml", {"file": "broken.yaml"}, _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "could not be parsed" in content["content"][0]["text"]

    async def test_missing_file_and_out_of_scope_are_identical(self, hass, esphome_dir, esphome_entries):
        missing, _, _ = await _call(
            "get_esphome_yaml", {"file": "nope.yaml"}, _yaml_token(), hass)
        # A device the token cannot see must be indistinguishable from absent.
        _esphome_entry(hass, esphome_entries, entity_ids=("light.not_mine",),
                       runtime=_runtime(_device_info(name="rf-blaster1")))
        scoped, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert missing["content"][0]["text"] == scoped["content"][0]["text"]


class TestDeviceYamlDelete:
    """Phoenix MCP owns the delete rather than allowlisting the add-on's
    devices/delete, for one reason: the add-on's delete bypasses version capture,
    so a mistaken delete of a heavily worked configuration is unrecoverable.
    Going through an executor snapshots first and makes it an ordinary Changes
    entry.
    """

    async def test_cap_denied_touches_nothing_and_reveals_nothing(self, hass, esphome_dir):
        content, outcome, _ = await _call(
            "delete_esphome_yaml", {"file": "rf-blaster1.yaml"},
            _yaml_token(cap_esphome_yaml="deny"), hass)
        assert outcome == "denied"
        assert "rf-blaster1" not in content["content"][0]["text"]
        assert (esphome_dir / "rf-blaster1.yaml").exists()

    async def test_missing_and_out_of_jail_are_byte_identical(self, hass, esphome_dir):
        missing, out_missing, _ = await _call(
            "delete_esphome_yaml", {"file": "no-such-device.yaml"}, _yaml_token(), hass)
        # An archived file is outside the jail; both must read the same to the
        # caller while the audit distinguishes them.
        secrets, out_secrets, _ = await _call(
            "delete_esphome_yaml", {"file": "secrets.yaml"}, _yaml_token(), hass)

        assert out_missing == "not_found"
        assert out_secrets == "invalid_request"
        assert (esphome_dir / "secrets.yaml").exists()

    async def test_secrets_yaml_can_never_be_deleted(self, hass, esphome_dir):
        for name in ("secrets.yaml", "./secrets.yaml", "archive/old-device.yaml"):
            _c, outcome, _ = await _call("delete_esphome_yaml", {"file": name}, _yaml_token(), hass)
            assert outcome == "invalid_request", name
        assert (esphome_dir / "secrets.yaml").exists()
        assert (esphome_dir / "archive" / "old-device.yaml").exists()

    async def test_deletes_the_file_and_snapshots_it_first(self, hass, esphome_dir):
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)

        content, outcome, _ = await _call_tool(
            "delete_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass, data)

        assert outcome == "allowed"
        assert _json(content)["deleted"] is True
        assert not (esphome_dir / "rf-blaster1.yaml").exists()

        history = versions.list_for("esphome_yaml", "rf-blaster1.yaml")
        assert [v.action for v in history] == ["delete"]
        # The snapshot is RAW, so the restore below can reproduce the file exactly.
        assert history[0].before["content"] == DEVICE_YAML
        assert history[0].after is None

    async def test_a_deleted_config_can_be_restored(self, hass, esphome_dir):
        # The whole justification for owning this tool.
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)
        await _call_tool(
            "delete_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass, data)
        assert not (esphome_dir / "rf-blaster1.yaml").exists()

        ver = versions.list_for("esphome_yaml", "rf-blaster1.yaml")[0]
        _res, outcome, _r = await async_restore_version(ver, "admin-1", hass, data, side="before")

        assert outcome == "allowed"
        assert (esphome_dir / "rf-blaster1.yaml").read_text() == DEVICE_YAML

    async def test_the_result_says_the_device_was_not_touched(self, hass, esphome_dir):
        # The consequence most likely to be misread by an agent reporting back.
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)
        content, _o, _r = await _call_tool(
            "delete_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass, data)
        note = _json(content)["note"]
        assert "keeps running" in note
        assert "entities" in note

    async def test_a_doomed_delete_never_becomes_a_pending_approval(self, hass, esphome_dir):
        # Rule 29, and the ONLY thing the pre-gate check buys: the executor
        # re-checks existence too, so without a confirm-mode capability in play
        # removing the precheck changes nothing observable. Found by mutation.
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _c, outcome, _ = await _call_tool(
            "delete_esphome_yaml", {"file": "no-such-device.yaml"},
            _yaml_token(cap_esphome_yaml="confirm"), hass, data)

        assert outcome == "not_found"
        data.store.set_pending_approvals.assert_not_called()

    async def test_an_out_of_jail_delete_never_becomes_a_pending_approval(self, hass, esphome_dir):
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _c, outcome, _ = await _call_tool(
            "delete_esphome_yaml", {"file": "secrets.yaml"},
            _yaml_token(cap_esphome_yaml="confirm"), hass, data)

        assert outcome == "invalid_request"
        data.store.set_pending_approvals.assert_not_called()
        assert (esphome_dir / "secrets.yaml").exists()

    async def test_the_approval_diff_masks_credentials_and_states_the_consequence(
        self, hass, esphome_dir
    ):
        diff = esphome._build_diff_delete_esphome_yaml(
            {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert diff["kind"] == "esphome_yaml"
        assert "REALAPIKEY123456=" not in diff["before"]
        assert "__PHOENIX_REDACTED__" in diff["before"]
        assert diff["after"] == ""
        assert "not touched" in diff["summary"]
        assert "keeps running" in diff["preview"]["warning"]


class TestDeviceYamlRestore:
    """Restoring a device file must work for a file carrying an inline
    credential, which is nearly all of them: the raw snapshot re-writes that
    literal at a frozen path, and the write-freeze would otherwise read
    rewriting it unchanged as changing it.

    This pins the WIRING, not just the waiver. Unit tests that exercise the
    splice directly stay green through the whole failure, because the executor
    is what has to notice it is restoring and say so; a ContextVar cannot cross
    into the executor thread on its own.
    """

    async def test_a_file_with_inline_credentials_can_be_rolled_back(self, hass, esphome_dir):
        versions = VersionStore()
        data = PhoenixData(
            store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=versions)
        token = _yaml_token()

        read, _, _ = await _call_tool("get_esphome_yaml", {"file": "rf-blaster1.yaml"}, token, hass, data)
        masked = _json(read)
        edited = masked["content"].replace("baud_rate: 9600", "baud_rate: 115200")
        _c, outcome, _r = await _call_tool(
            "set_esphome_yaml",
            {"file": "rf-blaster1.yaml", "content": edited, "expected_hash": masked["content_hash"]},
            token, hass, data)
        assert outcome == "allowed"
        assert "baud_rate: 115200" in (esphome_dir / "rf-blaster1.yaml").read_text()

        ver = versions.list_for("esphome_yaml", "rf-blaster1.yaml")[0]
        _res, outcome, _r = await async_restore_version(ver, "admin-1", hass, data, side="before")
        assert outcome == "allowed"
        # Byte-faithful, with the inline credentials still intact and unmasked.
        assert (esphome_dir / "rf-blaster1.yaml").read_text() == DEVICE_YAML

    async def test_restoring_does_not_waive_the_rules_for_a_normal_write(self, hass, esphome_dir):
        # The waiver must not leak: an agent rewriting the file it just read,
        # credentials and all, is still refused outside the restore path.
        data = MagicMock()
        content, outcome, _ = await _call_tool(
            "set_esphome_yaml", {"file": "rf-blaster1.yaml", "content": DEVICE_YAML},
            _yaml_token(), hass, data)
        assert outcome == "invalid_request"
        assert "masked credential" in content["content"][0]["text"]


class TestDeviceYamlWrite:
    async def test_non_secret_edit_preserves_credentials_on_disk(self, hass, esphome_dir):
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        masked = _json(read)
        edited = masked["content"].replace("baud_rate: 9600", "baud_rate: 115200")
        content, outcome, _ = await _call(
            "set_esphome_yaml",
            {"file": "rf-blaster1.yaml", "content": edited,
             "expected_hash": masked["content_hash"]},
            _yaml_token(), hass)
        assert outcome == "allowed"
        on_disk = (esphome_dir / "rf-blaster1.yaml").read_text()
        assert 'key: "REALAPIKEY123456="' in on_disk
        assert 'password: "realotapassword"' in on_disk
        assert "baud_rate: 115200" in on_disk
        assert "__PHOENIX_REDACTED__" not in on_disk
        assert _json(content)["file"] == "rf-blaster1.yaml"

    async def test_changing_a_credential_refused_before_any_approval(self, hass, esphome_dir):
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        bad = _json(read)["content"].replace(
            "__PHOENIX_REDACTED__api.encryption.key__", '"ATTACKERKEY123="')
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        content, outcome, _ = await _call_tool(
            "set_esphome_yaml", {"file": "rf-blaster1.yaml", "content": bad},
            _yaml_token(cap_esphome_yaml="confirm"), hass, data)
        assert outcome == "invalid_request"
        # Rule 29: a doomed write must not become a pending approval.
        data.store.set_pending_approvals.assert_not_called()
        msg = content["content"][0]["text"]
        assert "!secret" in msg and "secrets.yaml" in msg
        assert 'key: "REALAPIKEY123456="' in (esphome_dir / "rf-blaster1.yaml").read_text()

    async def test_migration_to_defined_secret_allowed(self, hass, esphome_dir):
        (esphome_dir / "secrets.yaml").write_text(
            SECRETS_YAML + "rf_api_key: REALAPIKEY123456=\n")
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        migrated = _json(read)["content"].replace(
            "__PHOENIX_REDACTED__api.encryption.key__", "!secret rf_api_key")
        _, outcome, _ = await _call(
            "set_esphome_yaml", {"file": "rf-blaster1.yaml", "content": migrated},
            _yaml_token(), hass)
        assert outcome == "allowed"
        on_disk = (esphome_dir / "rf-blaster1.yaml").read_text()
        assert "key: !secret rf_api_key" in on_disk
        assert "REALAPIKEY123456=" not in on_disk

    async def test_migration_to_undefined_secret_refused(self, hass, esphome_dir):
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        migrated = _json(read)["content"].replace(
            "__PHOENIX_REDACTED__api.encryption.key__", "!secret rf_api_key")
        content, outcome, _ = await _call(
            "set_esphome_yaml", {"file": "rf-blaster1.yaml", "content": migrated},
            _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "rf_api_key" in content["content"][0]["text"]

    async def test_stale_hash_refused(self, hass, esphome_dir):
        _, outcome, _ = await _call(
            "set_esphome_yaml",
            {"file": "rf-blaster1.yaml", "content": "esphome:\n  name: x\n",
             "expected_hash": "0" * 64},
            _yaml_token(), hass)
        assert outcome == "invalid_request"

    async def test_invalid_yaml_refused(self, hass, esphome_dir):
        content, outcome, _ = await _call(
            "set_esphome_yaml",
            {"file": "rf-blaster1.yaml", "content": "esphome:\n  name: [unclosed\n"},
            _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "not valid YAML" in content["content"][0]["text"]

    async def test_new_file_creation_allowed(self, hass, esphome_dir):
        _, outcome, _ = await _call(
            "set_esphome_yaml",
            {"file": "new-device.yaml", "content": "esphome:\n  name: new-device\n"},
            _yaml_token(), hass)
        assert outcome == "allowed"
        assert (esphome_dir / "new-device.yaml").exists()

    async def test_write_to_secrets_refused(self, hass, esphome_dir):
        _, outcome, _ = await _call(
            "set_esphome_yaml", {"file": "secrets.yaml", "content": "wifi_password: pwned\n"},
            _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert (esphome_dir / "secrets.yaml").read_text() == SECRETS_YAML


class TestDeviceYamlApprovalFlow:
    async def test_diff_and_stored_args_carry_no_credentials(self, hass, esphome_dir):
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        edited = _json(read)["content"].replace("baud_rate: 9600", "baud_rate: 115200")

        store = _ApprStore()
        data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)
        content, outcome, _ = await _call_tool(
            "set_esphome_yaml", {"file": "rf-blaster1.yaml", "content": edited},
            _yaml_token(cap_esphome_yaml="confirm"), hass, data)

        assert outcome == "pending_approval"
        assert len(store._p) == 1
        appr = store._p[0]
        assert appr["tool_name"] == "set_esphome_yaml"
        assert appr["cap_name"] == "cap_esphome_yaml"
        # Nothing anywhere in the record may carry a real credential.
        blob = json.dumps(appr)
        for secret in ("REALAPIKEY123456=", "realotapassword", "housepassword1", "MyHomeNetwork"):
            assert secret not in blob, secret
        assert "does not flash" in appr["diff"]["summary"]
        assert appr["diff"]["preview"]["flashes_device"] is False
        # The write must not have happened yet.
        assert "baud_rate: 9600" in (esphome_dir / "rf-blaster1.yaml").read_text()

    async def test_executor_recheck_catches_drift(self, hass, esphome_dir):
        read, _, _ = await _call(
            "get_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        masked = _json(read)
        edited = masked["content"].replace("baud_rate: 9600", "baud_rate: 115200")
        # An admin edits the file during the approval window.
        (esphome_dir / "rf-blaster1.yaml").write_text(DEVICE_YAML + "\nlogger:\n")
        content, outcome, _ = await _execute_set_esphome_yaml(
            {"file": "rf-blaster1.yaml", "content": edited,
             "expected_hash": masked["content_hash"]},
            _yaml_token(), hass, MagicMock())
        assert outcome == "invalid_request"


class TestWsAllowlistGuard:
    def test_encryption_key_command_never_allowlisted(self):
        # esphome/get_encryption_key returns the device's noise PSK in cleartext.
        assert "esphome/get_encryption_key" not in ALLOWED_WS_COMMANDS


class TestAvailability:
    def test_reports_all_three_surfaces(self, hass):
        hass.config.components.add("esphome")
        with patch(_DASHBOARD_PATCH, return_value=_dashboard(last_update_success=True)):
            avail = esphome_availability(hass)
        assert avail == (True, True, True)

    def test_unreachable_builder_is_still_configured(self, hass):
        """A stopped add-on must stay "configured": announcement keys on this."""
        hass.config.components.add("esphome")
        with patch(_DASHBOARD_PATCH, return_value=_dashboard(last_update_success=False)):
            avail = esphome_availability(hass)
        assert avail.builder is True
        assert avail.builder_live is False

    def test_no_esphome_at_all(self, hass):
        with patch(_DASHBOARD_PATCH, return_value=None), \
                patch.object(hass.config_entries, "async_entries", return_value=[]):
            avail = esphome_availability(hass)
        assert avail == (False, False, False)

    def test_config_entry_alone_counts_as_integration_present(self, hass):
        with patch(_DASHBOARD_PATCH, return_value=None), \
                patch.object(hass.config_entries, "async_entries", return_value=[object()]):
            avail = esphome_availability(hass)
        assert avail.integration is True
        assert avail.builder is False

    def test_unknown_host_fails_open(self):
        # Hiding tools is a usability affordance, not a security control, so an
        # unknown host must announce everything rather than lose a tool surface.
        assert esphome_availability(None) == (True, True, True)

    def test_probe_failure_fails_open(self, hass):
        broken = MagicMock()
        broken.config.components.__contains__.side_effect = RuntimeError("boom")
        broken.config_entries.async_entries.side_effect = RuntimeError("boom")
        with patch(_DASHBOARD_PATCH, return_value=None):
            avail = esphome_availability(broken)
        assert avail.integration is True


class TestOverviewDeclaredActions:
    async def test_actions_listed_with_munged_service_name_and_args(self, hass, esphome_entries):
        runtime = _runtime(services={1: _user_service()})
        _esphome_entry(hass, esphome_entries, runtime=runtime)
        hass.services.async_register("esphome", "rf_blaster1_transmit_raw", lambda call: None)
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        action = _json(content)["devices"][0]["actions"][0]
        assert action["name"] == "transmit_raw"
        # Hyphens in the device name become underscores in the HA service name.
        assert action["ha_service"] == "esphome.rf_blaster1_transmit_raw"
        assert action["registered"] is True
        assert action["args"] == [
            {"name": "timings", "type": "int_array"},
            {"name": "repeats", "type": "int"},
        ]

    async def test_unregistered_action_surfaces_drift(self, hass, esphome_entries):
        runtime = _runtime(services={1: _user_service()})
        _esphome_entry(hass, esphome_entries, runtime=runtime)
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert _json(content)["devices"][0]["actions"][0]["registered"] is False

    async def test_no_actions_key_when_device_declares_none(self, hass, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        with patch(_DASHBOARD_PATCH, return_value=None):
            content, _, _ = await _call("get_esphome_overview", {}, _token(), hass)
        assert "actions" not in _json(content)["devices"][0]
