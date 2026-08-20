"""Tests for the radio management tools (get_radio_network, get_radio_device,
permit_zigbee_join, reconfigure_zigbee_device, set_zigbee_device_options,
remove_zigbee_device).

The Z2M backend is exercised against a fake MQTT broker monkeypatched over
radio.py's two seams (_mqtt_subscribe/_mqtt_publish), so no paho client is
needed. The ZHA backend is exercised with async_ws_command / the reconfigure
starter monkeypatched (zigpy is not installed here, so the real zha component
cannot load) and fake zha.permit/zha.remove services. Both network
projections are asserted against realistic fixtures that INCLUDE key material,
proving the allowlist never leaks it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.phoenix_mcp.radio as radio
import custom_components.phoenix_mcp.tools.radio as radio_tools
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool, _tool_gate_map, async_execute_approved_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord

Z2M_BULB_IEEE = "0x00158d0001112222"
Z2M_LOCK_IEEE = "0x00158d0009998888"
ZHA_BULB_IEEE = "00:11:22:33:44:55:66:77"

BRIDGE_INFO = {
    "version": "2.12.1",
    "commit": "abc123",
    "coordinator": {
        "ieee_address": "0x00124b002e1e03c7",
        "type": "zStack3x0",
        "meta": {"revision": 20240710},
    },
    "network": {"channel": 15, "pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd"},
    "permit_join": False,
    "log_level": "info",
    "restart_required": False,
    "config": {
        "advanced": {
            "network_key": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
            "pan_id": 6754,
        },
        "mqtt": {"server": "mqtt://core-mosquitto:1883", "password": "hunter2"},
        "device_options": {"temperature_precision": 1},
        "devices": {
            Z2M_BULB_IEEE: {
                "friendly_name": "Z2M Bulb",
                "temperature_precision": 2,
                "calibration": 0.5,
                "mode": "fast",
                "invert": False,
                "thresholds": [1, 2],
            },
        },
    },
    "config_schema": {"type": "object"},
}

ZHA_NETWORK_SETTINGS = {
    "radio_type": "ezsp",
    "device": {"path": "/dev/ttyUSB0", "baudrate": 115200},
    "settings": {
        "network_info": {
            "channel": 15,
            "channel_mask": [15],
            "pan_id": 6754,
            "extended_pan_id": "dd:dd:dd:dd:dd:dd:dd:dd",
            "nwk_update_id": 0,
            "security_level": 5,
            "network_key": {"key": "aa:aa:aa:aa", "tx_counter": 123, "rx_counter": 0},
            "tc_link_key": {"key": "cc:cc:cc:cc"},
            "key_table": [{"key": "ee:ee:ee:ee"}],
            "stack_specific": {"ezsp": {"hashed_tclk": "deadbeefdeadbeef"}},
        },
        "node_info": {
            "nwk": 0,
            "ieee": "00:12:4b:00:2e:1e:03:c7",
            "logical_type": "coordinator",
        },
    },
}


class FakeBroker:
    """In-memory stand-in for the MQTT broker behind radio.py's two seams."""

    def __init__(self) -> None:
        self.retained: dict[str, str] = {}
        self.published: list[tuple[str, dict]] = []
        self.subs: dict[str, list] = {}
        # request path -> fn(request_dict) -> response dict WITHOUT transaction
        # (echoed automatically), or None to simulate no answer.
        self.responders: dict[str, object] = {}

    async def subscribe(self, hass, topic, cb):
        self.subs.setdefault(topic, []).append(cb)
        if topic in self.retained:
            cb(SimpleNamespace(topic=topic, payload=self.retained[topic]))

        def _unsub() -> None:
            self.subs[topic].remove(cb)

        return _unsub

    async def publish(self, hass, topic, payload):
        request = json.loads(payload)
        self.published.append((topic, request))
        prefix = "zigbee2mqtt/bridge/request/"
        if not topic.startswith(prefix):
            return
        path = topic[len(prefix):]
        responder = self.responders.get(path)
        if responder is None:
            return
        response = responder(request)
        if response is None:
            return
        response = {**response, "transaction": request.get("transaction")}
        for cb in list(self.subs.get(f"zigbee2mqtt/bridge/response/{path}", [])):
            cb(SimpleNamespace(topic=topic, payload=json.dumps(response)))


@pytest.fixture
def broker(monkeypatch) -> FakeBroker:
    fake = FakeBroker()
    fake.retained["zigbee2mqtt/bridge/info"] = json.dumps(BRIDGE_INFO)
    fake.retained["zigbee2mqtt/bridge/devices"] = json.dumps([
        {
            "ieee_address": Z2M_BULB_IEEE,
            "friendly_name": "Z2M Bulb",
            "type": "Router",
            "network_address": 4660,
            "supported": True,
            "disabled": False,
            "interview_state": "SUCCESSFUL",
            "power_source": "Mains (single phase)",
            "software_build_id": "1.0",
            "definition": {
                "model": "B1",
                "vendor": "Acme",
                "description": "Smart bulb",
                "exposes": [{
                    "type": "light",
                    "endpoint": "left",
                    "features": [{
                        "type": "numeric", "name": "brightness", "label": "Brightness",
                        "property": "brightness_left", "access": 7, "value_min": 0,
                        "value_max": 254, "value_step": 1, "unit": "%", "unknown": "drop",
                    }],
                    "unknown": "drop",
                }],
                "options": [
                    {"type": "numeric", "name": "Temperature precision", "property": "temperature_precision",
                     "access": 2, "value_min": 0, "value_max": 3, "value_step": 1},
                    {"type": "numeric", "name": "Calibration", "property": "calibration",
                     "access": 2, "value_min": -5, "value_max": 5, "value_step": 0.5, "unit": "°C"},
                    {"type": "enum", "name": "Mode", "property": "mode", "access": 2,
                     "values": ["slow", "fast"]},
                    {"type": "binary", "name": "Invert", "property": "invert", "access": 2,
                     "value_on": True, "value_off": False},
                    {"type": "list", "name": "Thresholds", "property": "thresholds", "access": 2,
                     "length_min": 1, "length_max": 4,
                     "item_type": {"type": "numeric", "value_min": 0, "value_max": 10}},
                ],
            },
        },
        {
            "ieee_address": Z2M_LOCK_IEEE,
            "friendly_name": "Z2M Lock",
            "type": "EndDevice",
            "definition": {"model": "L1", "vendor": "Acme", "description": "Lock"},
        },
    ])
    fake.responders["permit_join"] = lambda req: {"status": "ok", "data": {"time": req.get("time")}}
    fake.responders["device/configure"] = lambda req: {"status": "ok", "data": {"id": req.get("id")}}
    fake.responders["device/remove"] = lambda req: {"status": "ok", "data": {"id": req.get("id")}}

    def _device_options(req):
        info = json.loads(fake.retained["zigbee2mqtt/bridge/info"])
        own = info["config"]["devices"][Z2M_BULB_IEEE]
        before = {**info["config"]["device_options"], **own}
        before.pop("friendly_name", None)
        own.update(req["options"])
        after = {**info["config"]["device_options"], **own}
        after.pop("friendly_name", None)
        fake.retained["zigbee2mqtt/bridge/info"] = json.dumps(info)
        return {
            "status": "ok",
            "data": {
                "id": req.get("id"),
                "from": before,
                "to": after,
                "restart_required": req["options"].get("mode") == "slow",
            },
        }

    fake.responders["device/options"] = _device_options
    monkeypatch.setattr(radio, "_mqtt_subscribe", fake.subscribe)
    monkeypatch.setattr(radio, "_mqtt_publish", fake.publish)
    return fake


@pytest.fixture
def radio_env(hass: HomeAssistant):
    """Registry env: a Z2M bulb (light, grantable), a Z2M lock (out of scope),
    a ZHA bulb (light), the Z2M bridge device with a visible entity, and a
    plain non-radio device with a visible entity."""
    entry = MockConfigEntry(domain="mqtt", entry_id="e_radio")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    def _device(identifiers, name):
        return dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers=identifiers, name=name,
            manufacturer="Acme", model="M1",
        )

    z2m_bulb = _device({("mqtt", f"zigbee2mqtt_{Z2M_BULB_IEEE}")}, "Z2M Bulb")
    z2m_lock = _device({("mqtt", f"zigbee2mqtt_{Z2M_LOCK_IEEE}")}, "Z2M Lock")
    zha_bulb = _device({("zha", ZHA_BULB_IEEE)}, "ZHA Bulb")
    bridge = _device({("mqtt", "zigbee2mqtt_bridge_0x00124b002e1e03c7")}, "Z2M Bridge")
    plain = _device({("mqtt", "some_other_thing")}, "Plain Device")

    def _entity(domain, uid, device, object_id):
        e = ent_reg.async_get_or_create(
            domain, "mqtt", uid, config_entry=entry, device_id=device.id,
            suggested_object_id=object_id,
        )
        hass.states.async_set(e.entity_id, "on", {})
        return e.entity_id

    return {
        "z2m_bulb": z2m_bulb.id,
        "z2m_bulb_eid": _entity("light", "u1", z2m_bulb, "z2m_bulb"),
        "z2m_lock": z2m_lock.id,
        "z2m_lock_eid": _entity("lock", "u2", z2m_lock, "z2m_lock"),
        "zha_bulb": zha_bulb.id,
        "zha_bulb_eid": _entity("light", "u3", zha_bulb, "zha_bulb"),
        "bridge": bridge.id,
        "bridge_eid": _entity("light", "u4", bridge, "bridge_glow"),
        "plain": plain.id,
        "plain_eid": _entity("light", "u5", plain, "plain_bulb"),
    }


def _data() -> PhoenixData:
    return PhoenixData(store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)


def _token(tree: PermissionTree | None = None, **caps) -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=tree or PermissionTree(domains={}), **caps,
    )


def _light_token(**caps) -> TokenRecord:
    tree = PermissionTree(domains={"light": PermissionNode(state="GREEN")})
    return _token(tree=tree, **caps)


def _read_only_light_token(**caps) -> TokenRecord:
    tree = PermissionTree(domains={"light": PermissionNode(state="YELLOW")})
    return _token(tree=tree, **caps)


def _body(result: tuple) -> dict:
    return json.loads(result[0]["content"][0]["text"])


def _text(result: tuple) -> str:
    return result[0]["content"][0]["text"]


class _ApprStore:
    def __init__(self) -> None:
        self._p: list = []
        self.async_lock = asyncio.Lock()
        self.async_save = AsyncMock()

    def get_pending_approvals(self) -> list:
        return self._p

    def set_pending_approvals(self, v: list) -> None:
        self._p = v


def _appr_data() -> tuple[PhoenixData, _ApprStore]:
    store = _ApprStore()
    return PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None), store


class TestGetRadioNetwork:
    async def test_cap_deny(self, hass, radio_env, broker):
        res = await _call_tool("get_radio_network", {}, _light_token(cap_diagnostics="deny"), hass, _data())
        assert res[1] == "denied"

    async def test_no_backends_empty_list(self, hass, radio_env, broker):
        res = await _call_tool("get_radio_network", {}, _light_token(cap_diagnostics="allow"), hass, _data())
        assert res[1] == "allowed"
        assert _body(res) == {"networks": []}

    async def test_z2m_projection_no_key_material(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool("get_radio_network", {}, _light_token(cap_diagnostics="allow"), hass, _data())
        assert res[1] == "allowed"
        networks = _body(res)["networks"]
        assert len(networks) == 1
        net = networks[0]
        assert net["backend"] == "z2m" and net["protocol"] == "zigbee"
        assert net["channel"] == 15 and net["pan_id"] == 6754
        assert net["coordinator"]["ieee"] == "0x00124b002e1e03c7"
        assert net["permit_join"] is False
        text = _text(res)
        assert "network_key" not in text
        assert "core-mosquitto" not in text
        assert "hunter2" not in text
        assert "config_schema" not in text

    async def test_zha_projection_no_key_material(self, hass, radio_env, broker, monkeypatch):
        hass.config.components.add("zha")
        monkeypatch.setattr(radio, "async_ws_command", AsyncMock(return_value=ZHA_NETWORK_SETTINGS))
        res = await _call_tool("get_radio_network", {}, _light_token(cap_diagnostics="allow"), hass, _data())
        networks = _body(res)["networks"]
        assert len(networks) == 1
        net = networks[0]
        assert net["backend"] == "zha" and net["radio_type"] == "ezsp"
        assert net["channel"] == 15 and net["security_level"] == 5
        assert net["coordinator"]["logical_type"] == "coordinator"
        text = _text(res)
        for secret in ("network_key", "tc_link_key", "key_table", "stack_specific",
                       "deadbeef", "aa:aa:aa:aa", "cc:cc:cc:cc", "/dev/ttyUSB0"):
            assert secret not in text

    async def test_backend_read_failure_degrades_to_error_entry(self, hass, radio_env, broker, monkeypatch):
        hass.config.components.add("mqtt")
        monkeypatch.setattr(
            radio, "async_z2m_retained",
            AsyncMock(side_effect=radio.RadioError("No data from Zigbee2MQTT")),
        )
        res = await _call_tool("get_radio_network", {}, _light_token(cap_diagnostics="allow"), hass, _data())
        networks = _body(res)["networks"]
        assert networks[0]["backend"] == "z2m"
        assert "error" in networks[0]


class TestGetRadioDevice:
    async def test_cap_deny(self, hass, radio_env, broker):
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_diagnostics="deny"), hass, _data(),
        )
        assert res[1] == "denied"

    async def test_ghost_and_out_of_scope_identical(self, hass, radio_env, broker):
        token = _light_token(cap_diagnostics="allow")
        ghost = await _call_tool("get_radio_device", {"device_id": "does_not_exist"}, token, hass, _data())
        hidden = await _call_tool("get_radio_device", {"device_id": radio_env["z2m_lock"]}, token, hass, _data())
        assert ghost[1] == "not_found" and hidden[1] == "not_found"
        assert json.dumps(ghost[0]) == json.dumps(hidden[0])

    async def test_non_radio_device_invalid_request(self, hass, radio_env, broker):
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["plain"]},
            _light_token(cap_diagnostics="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "Not a radio-managed device" in _text(res)

    async def test_bridge_device_never_detects_as_radio(self, hass, radio_env, broker):
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["bridge"]},
            _light_token(cap_diagnostics="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "Not a radio-managed device" in _text(res)

    async def test_z2m_device_projection(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_diagnostics="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        body = _body(res)
        assert body["backend"] == "z2m"
        assert body["ieee"] == Z2M_BULB_IEEE
        assert body["model"] == "B1" and body["vendor"] == "Acme"
        assert body["interview_state"] == "SUCCESSFUL"
        assert body["device_id"] == radio_env["z2m_bulb"]
        expose = body["exposes"]
        assert expose["total"] == 1 and expose["truncated"] is False
        assert expose["items"][0]["type"] == "light"
        assert expose["items"][0]["features"][0]["property"] == "brightness_left"
        assert "unknown" not in _text(res)
        options = body["options"]
        assert options["definitions"]["total"] == 5
        assert options["values"] == {
            "temperature_precision": 2,
            "calibration": 0.5,
            "mode": "fast",
            "invert": False,
            "thresholds": [1, 2],
        }
        assert len(options["content_hash"]) == 64
        text = _text(res)
        for secret in ("network_key", "core-mosquitto", "hunter2", "config_schema"):
            assert secret not in text

    def test_z2m_exposes_are_bounded_and_allowlisted(self):
        entry = {
            "definition": {
                "exposes": [
                    {
                        "type": "numeric",
                        "property": f"value_{index}",
                        "description": "x" * 1000,
                        "secret_extension": "must not pass",
                    }
                    for index in range(100)
                ]
            }
        }
        body = radio.project_z2m_device(entry)
        assert body["exposes"]["total"] == 100
        assert len(body["exposes"]["items"]) == 64
        assert body["exposes"]["truncated"] is True
        assert len(body["exposes"]["items"][0]["description"]) == 512
        assert "secret_extension" not in json.dumps(body)

        malformed = radio.project_z2m_device({"definition": ["not", "a", "mapping"]})
        assert malformed["exposes"] == {"items": [], "total": 0, "truncated": False}
        assert malformed["options"]["definitions"] == {
            "items": [], "total": 0, "truncated": False,
        }

    async def test_z2m_device_missing_from_bridge(self, hass, radio_env, broker):
        broker.retained["zigbee2mqtt/bridge/devices"] = json.dumps([])
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_diagnostics="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "not on the Zigbee network" in _text(res)

    async def test_z2m_option_values_inherit_global_device_options(
        self, hass, radio_env, broker
    ):
        info = json.loads(broker.retained["zigbee2mqtt/bridge/info"])
        del info["config"]["devices"][Z2M_BULB_IEEE]["temperature_precision"]
        broker.retained["zigbee2mqtt/bridge/info"] = json.dumps(info)
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        assert _body(result)["options"]["values"]["temperature_precision"] == 1

    def _zha_device_payload(self, radio_env) -> dict:
        return {
            "ieee": ZHA_BULB_IEEE,
            "nwk": 4661,
            "name": "ZHA Bulb",
            "manufacturer": "Acme",
            "model": "ZB1",
            "quirk_applied": False,
            "power_source": "Mains",
            "lqi": 180,
            "rssi": -60,
            "last_seen": "2026-07-07T10:00:00",
            "available": True,
            "device_type": "Router",
            "entities": [
                {"entity_id": radio_env["zha_bulb_eid"], "name": "ZHA Bulb"},
                {"entity_id": "lock.zha_secret", "name": "Secret Lock"},
            ],
            "neighbors": [
                {"ieee": Z2M_BULB_IEEE, "nwk": "0x1234", "lqi": 200, "depth": 1,
                 "relationship": "Sibling", "device_type": "Router"},
                {"ieee": "00:99:99:99:99:99:99:99", "nwk": "0x9999", "lqi": 90,
                 "depth": 2, "relationship": "Sibling", "device_type": "EndDevice"},
            ],
            "routes": [{"dest_nwk": "0x9999", "next_hop": "0x8888"}],
        }

    async def test_zha_device_scoped_projection(self, hass, radio_env, broker, monkeypatch):
        monkeypatch.setattr(
            radio, "async_ws_command", AsyncMock(return_value=self._zha_device_payload(radio_env))
        )
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["zha_bulb"]},
            _light_token(cap_diagnostics="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        body = _body(res)
        assert body["backend"] == "zha" and body["lqi"] == 180
        entity_ids = [e["entity_id"] for e in body["entities"]]
        assert entity_ids == [radio_env["zha_bulb_eid"]]
        assert "lock.zha_secret" not in _text(res)
        visible, hidden = body["neighbors"]
        assert visible["ieee"] == Z2M_BULB_IEEE
        assert visible["device_id"] == radio_env["z2m_bulb"]
        assert hidden["ieee"] == "<redacted>" and hidden["nwk"] == "<redacted>"
        assert hidden["lqi"] == 90 and hidden["relationship"] == "Sibling"
        assert "routes" not in body
        assert "0x8888" not in _text(res)

    async def test_zha_device_pass_through_unredacted(self, hass, radio_env, broker, monkeypatch):
        monkeypatch.setattr(
            radio, "async_ws_command", AsyncMock(return_value=self._zha_device_payload(radio_env))
        )
        token = _token(cap_diagnostics="allow")
        token.pass_through = True
        res = await _call_tool(
            "get_radio_device", {"device_id": radio_env["zha_bulb"]}, token, hass, _data(),
        )
        body = _body(res)
        assert body["neighbors"][1]["ieee"] == "00:99:99:99:99:99:99:99"


class TestPermitZigbeeJoin:
    async def test_cap_deny_not_an_oracle(self, hass, radio_env, broker):
        token = _light_token(cap_radio_write="deny")
        bodies = set()
        for args in ({}, {"device_id": "ghost"}, {"device_id": radio_env["z2m_lock"]}):
            res = await _call_tool("permit_zigbee_join", args, token, hass, _data())
            assert res[1] == "denied"
            bodies.add(json.dumps(res[0]))
        assert len(bodies) == 1

    async def test_bad_duration_rejected_before_gating(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm")
        for duration in (300, -1, "abc", True):
            res = await _call_tool("permit_zigbee_join", {"duration": duration}, token, hass, data)
            assert res[1] == "invalid_request"
        assert store._p == []

    async def test_no_backend_available(self, hass, radio_env, broker):
        res = await _call_tool(
            "permit_zigbee_join", {}, _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "No Zigbee network" in _text(res)

    async def test_both_backends_require_backend_arg(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        hass.config.components.add("zha")
        res = await _call_tool(
            "permit_zigbee_join", {}, _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "backend" in _text(res)

    async def test_z2m_allow_publishes_permit(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 30},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert _body(res)["duration"] == 30
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/permit_join"
        assert request["time"] == 30 and "transaction" in request

    async def test_z2m_close_window(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 0},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert "closed" in _body(res)["message"]

    async def test_z2m_router_scoped(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 60, "device_id": radio_env["z2m_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        _topic, request = broker.published[-1]
        assert request["device"] == Z2M_BULB_IEEE
        assert _body(res)["via_device"] == "Z2M Bulb"

    async def test_z2m_error_response_surfaced(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        broker.responders["permit_join"] = lambda req: {"status": "error", "error": "no permit for you"}
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 30},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "invalid_request"
        assert "no permit for you" in _text(res)

    async def test_zha_allow_calls_service(self, hass, radio_env, broker):
        hass.config.components.add("zha")
        calls = []

        async def _permit(call):
            calls.append(dict(call.data))

        hass.services.async_register("zha", "permit", _permit)
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 45},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert calls == [{"duration": 45}]

    async def test_confirm_creates_real_approval_then_executes(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm")
        res = await _call_tool(
            "permit_zigbee_join", {"duration": 30}, token, hass, data, "rid-1", "1.2.3.4",
        )
        assert res[1] == "pending_approval"
        assert broker.published == []
        assert len(store._p) == 1
        appr = store._p[0]
        assert appr["tool_name"] == "permit_zigbee_join"
        assert appr["cap_name"] == "cap_radio_write"
        diff = appr["diff"]
        assert diff["kind"] == "system_action"
        assert "Open the Zigbee network" in diff["summary"]
        assert diff["preview"]["duration"] == 30

        out = await async_execute_approved_tool("permit_zigbee_join", appr["args"], token, hass, data)
        assert out[1] == "allowed"
        assert broker.published[-1][0] == "zigbee2mqtt/bridge/request/permit_join"


class TestReconfigureZigbeeDevice:
    async def test_read_only_scope_denied(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _read_only_light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "denied"
        assert "write access" in _text(res)

    async def test_ghost_and_out_of_scope_identical(self, hass, radio_env, broker):
        token = _light_token(cap_radio_write="allow")
        ghost = await _call_tool("reconfigure_zigbee_device", {"device_id": "ghost"}, token, hass, _data())
        hidden = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["z2m_lock"]}, token, hass, _data()
        )
        assert ghost[1] == "not_found" and hidden[1] == "not_found"
        assert json.dumps(ghost[0]) == json.dumps(hidden[0])

    async def test_z2m_allow_completes(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert "completed" in _body(res)["message"]
        _topic, request = broker.published[-1]
        assert request["id"] == Z2M_BULB_IEEE

    async def test_timeout_reports_partial(self, hass, radio_env, broker, monkeypatch):
        hass.config.components.add("mqtt")
        monkeypatch.setattr(radio, "async_reconfigure_device", AsyncMock(side_effect=TimeoutError()))
        res = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        body = _body(res)
        assert body["partial"] is True

    async def test_zha_dispatches_background_reinterview(self, hass, radio_env, broker, monkeypatch):
        started = AsyncMock()
        monkeypatch.setattr(radio, "async_zha_reconfigure_device", started)
        res = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["zha_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert "background" in _body(res)["message"]
        started.assert_awaited_once_with(hass, ZHA_BULB_IEEE)

    async def test_confirm_read_only_rejected_before_pending(self, hass, radio_env, broker):
        data, store = _appr_data()
        res = await _call_tool(
            "reconfigure_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _read_only_light_token(cap_radio_write="confirm"), hass, data,
        )
        assert res[1] == "denied"
        assert store._p == []


class TestSetZigbeeDeviceOptions:
    async def _current(self, hass, radio_env, token) -> dict:
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"]},
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        return _body(result)["options"]

    async def test_cap_deny_not_an_oracle(self, hass, radio_env, broker):
        token = _light_token(cap_radio_write="deny")
        bodies = set()
        for device_id in ("ghost", radio_env["z2m_lock"], radio_env["z2m_bulb"]):
            result = await _call_tool(
                "set_zigbee_device_options",
                {"device_id": device_id, "options": {"mode": "slow"}, "expected_hash": "0" * 64},
                token,
                hass,
                _data(),
            )
            assert result[1] == "denied"
            bodies.add(json.dumps(result[0]))
        assert len(bodies) == 1
        assert broker.published == []

    async def test_read_only_and_zha_rejected(self, hass, radio_env, broker):
        read_only = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": "0" * 64,
            },
            _read_only_light_token(cap_radio_write="allow"),
            hass,
            _data(),
        )
        assert read_only[1] == "denied"
        zha = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["zha_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": "0" * 64,
            },
            _light_token(cap_radio_write="allow"),
            hass,
            _data(),
        )
        assert zha[1] == "invalid_request"
        assert "only for Zigbee2MQTT" in _text(zha)
        assert broker.published == []

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {"unknown": 1},
            {"calibration": 0.3},
            {"calibration": 10},
            {"mode": "turbo"},
            {"invert": 1},
            {"thresholds": []},
            {"thresholds": [1, 2, 3, 4, 5]},
            {"thresholds": [11]},
        ],
    )
    async def test_invalid_converter_options_never_queue_or_publish(
        self, hass, radio_env, broker, options
    ):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": options,
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            data,
        )
        assert result[1] == "invalid_request"
        assert store._p == []
        assert broker.published == []

    async def test_allow_changes_options_and_records_non_restorable_history(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        data = _data()
        current = await self._current(hass, radio_env, token)
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"calibration": 1.5},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            data,
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["changed"] == {"calibration": 1.5}
        assert body["options"]["calibration"] == 1.5
        assert body["content_hash"] != current["content_hash"]
        assert body["restart_required"] is False
        assert body["confirmation"] == "response"
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/device/options"
        assert request["id"] == Z2M_BULB_IEEE
        assert request["options"] == {"calibration": 1.5}
        record = data.versions.list_for("device", radio_env["z2m_bulb"])[0]
        assert record.before["snapshot_type"] == "zigbee_device_options"
        assert record.before["restorable"] is False
        assert record.before["options"]["calibration"] == 0.5
        assert record.after["options"]["calibration"] == 1.5
        assert record.summary_key == "version.device.zigbee_options"

    async def test_response_timeout_confirms_exact_retained_values(
        self, hass, radio_env, broker, monkeypatch
    ):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)

        def _apply_without_response(req):
            info = json.loads(broker.retained["zigbee2mqtt/bridge/info"])
            info["config"]["devices"][Z2M_BULB_IEEE].update(req["options"])
            info["restart_required"] = True
            broker.retained["zigbee2mqtt/bridge/info"] = json.dumps(info)
            return None

        broker.responders["device/options"] = _apply_without_response
        monkeypatch.setattr(radio_tools, "PROXY_TIMEOUT_SECONDS", 0.01)
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["confirmation"] == "retained_state"
        assert body["options"]["mode"] == "slow"
        assert body["content_hash"] != current["content_hash"]
        assert body["restart_required"] is True

    async def test_response_timeout_without_retained_match_fails_closed(
        self, hass, radio_env, broker, monkeypatch
    ):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        broker.responders.pop("device/options")
        monkeypatch.setattr(radio_tools, "PROXY_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            radio,
            "async_confirm_z2m_device_options",
            AsyncMock(side_effect=TimeoutError),
        )
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "not visible in retained state" in _text(result)

    async def test_restart_requirement_is_reported_but_not_executed(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        assert _body(result)["restart_required"] is True
        assert [topic for topic, _request in broker.published] == [
            "zigbee2mqtt/bridge/request/device/options"
        ]

    async def test_confirm_diff_then_approve_executes(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        args = {
            "device_id": radio_env["z2m_bulb"],
            "options": {"calibration": 1.5, "mode": "slow"},
            "expected_hash": current["content_hash"],
        }
        result = await _call_tool(
            "set_zigbee_device_options", args, token, hass, data, "rid-options", "1.2.3.4"
        )
        assert result[1] == "pending_approval"
        assert broker.published == []
        approval = store._p[0]
        assert approval["tool_name"] == "set_zigbee_device_options"
        assert approval["diff"]["kind"] == "config_diff"
        assert approval["diff"]["preview"]["changed_keys"] == ["calibration", "mode"]
        assert current["content_hash"] == approval["diff"]["preview"]["content_hash"]
        output = await async_execute_approved_tool(
            "set_zigbee_device_options", approval["args"], token, hass, data
        )
        assert output[1] == "allowed"
        assert broker.published[-1][0] == "zigbee2mqtt/bridge/request/device/options"

    async def test_executor_rejects_stale_options_without_publishing(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            data,
        )
        info = json.loads(broker.retained["zigbee2mqtt/bridge/info"])
        info["config"]["devices"][Z2M_BULB_IEEE]["calibration"] = 1.0
        broker.retained["zigbee2mqtt/bridge/info"] = json.dumps(info)
        output = await async_execute_approved_tool(
            "set_zigbee_device_options", store._p[0]["args"], token, hass, data
        )
        assert output[1] == "invalid_request"
        assert "changed after they were read" in _text(output)
        assert broker.published == []

    async def test_unconfirmed_response_is_fail_closed(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        broker.responders["device/options"] = lambda req: {
            "status": "ok",
            "data": {"id": req["id"], "from": current["values"], "to": current["values"]},
        }
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"mode": "slow"},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "did not confirm" in _text(result)

    async def test_response_type_confusion_is_fail_closed(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        token = _light_token(cap_radio_write="allow", cap_diagnostics="allow")
        current = await self._current(hass, radio_env, token)
        unsafe = {**current["values"], "temperature_precision": True}
        broker.responders["device/options"] = lambda req: {
            "status": "ok",
            "data": {"id": req["id"], "from": current["values"], "to": unsafe},
        }
        result = await _call_tool(
            "set_zigbee_device_options",
            {
                "device_id": radio_env["z2m_bulb"],
                "options": {"temperature_precision": 1},
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "unsafe or incomplete" in _text(result)


class TestRemoveZigbeeDevice:
    async def test_cap_deny_not_an_oracle(self, hass, radio_env, broker):
        token = _light_token(cap_radio_write="deny")
        bodies = set()
        for device_id in ("ghost", radio_env["z2m_lock"], radio_env["z2m_bulb"]):
            res = await _call_tool("remove_zigbee_device", {"device_id": device_id}, token, hass, _data())
            assert res[1] == "denied"
            bodies.add(json.dumps(res[0]))
        assert len(bodies) == 1

    async def test_z2m_allow_removes(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        res = await _call_tool(
            "remove_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert _body(res)["removed"] is True
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/device/remove"
        assert request["id"] == Z2M_BULB_IEEE and request["force"] is False

    async def test_timeout_reports_partial(self, hass, radio_env, broker, monkeypatch):
        hass.config.components.add("mqtt")
        monkeypatch.setattr(radio, "async_remove_device", AsyncMock(side_effect=TimeoutError()))
        res = await _call_tool(
            "remove_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert _body(res)["partial"] is True

    async def test_zha_allow_calls_remove_service(self, hass, radio_env, broker):
        calls = []

        async def _remove(call):
            calls.append(dict(call.data))

        hass.services.async_register("zha", "remove", _remove)
        res = await _call_tool(
            "remove_zigbee_device", {"device_id": radio_env["zha_bulb"]},
            _light_token(cap_radio_write="allow"), hass, _data(),
        )
        assert res[1] == "allowed"
        assert calls == [{"ieee": ZHA_BULB_IEEE}]

    async def test_confirm_diff_then_approve_executes(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm")
        res = await _call_tool(
            "remove_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            token, hass, data, "rid-2", "1.2.3.4",
        )
        assert res[1] == "pending_approval"
        assert broker.published == []
        appr = store._p[0]
        diff = appr["diff"]
        assert diff["kind"] == "system_action"
        assert "Remove Zigbee device" in diff["summary"]
        assert radio_env["z2m_bulb_eid"] in diff["preview"]["entities"]
        assert "re-paired" in diff["preview"]["warning"]

        out = await async_execute_approved_tool("remove_zigbee_device", appr["args"], token, hass, data)
        assert out[1] == "allowed"
        assert broker.published[-1][0] == "zigbee2mqtt/bridge/request/device/remove"

    async def test_executor_revalidates_after_device_gone(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_radio_write="confirm")
        await _call_tool(
            "remove_zigbee_device", {"device_id": radio_env["z2m_bulb"]},
            token, hass, data, "rid-3", "1.2.3.4",
        )
        appr = store._p[0]
        dr.async_get(hass).async_remove_device(radio_env["z2m_bulb"])
        out = await async_execute_approved_tool("remove_zigbee_device", appr["args"], token, hass, data)
        assert out[1] == "not_found"
        assert broker.published == []


class TestRadioZ2mPlumbing:
    async def test_device_options_uses_short_response_window(self, hass, monkeypatch):
        request = AsyncMock(return_value={})
        monkeypatch.setattr(radio, "async_z2m_request", request)
        await radio.async_set_z2m_device_options(
            hass, Z2M_BULB_IEEE, {"mode": "slow"}
        )
        request.assert_awaited_once_with(
            hass,
            "device/options",
            {"id": Z2M_BULB_IEEE, "options": {"mode": "slow"}},
            timeout=radio.Z2M_DEVICE_OPTIONS_RESPONSE_TIMEOUT_SECONDS,
        )

    async def test_request_timeout_raises_timeout_error(self, hass, broker):
        broker.responders.pop("permit_join")
        with pytest.raises(TimeoutError):
            await radio.async_z2m_request(hass, "permit_join", {"time": 5}, timeout=0.05)

    async def test_transaction_mismatch_ignored(self, hass, broker):
        # A concurrent admin's response (different transaction) must not satisfy
        # our request.
        def _wrong_transaction(req):
            for cb in list(broker.subs.get("zigbee2mqtt/bridge/response/permit_join", [])):
                cb(SimpleNamespace(
                    topic="x",
                    payload=json.dumps({"status": "ok", "data": {}, "transaction": "someone-else"}),
                ))
            return None

        broker.responders["permit_join"] = _wrong_transaction
        with pytest.raises(TimeoutError):
            await radio.async_z2m_request(hass, "permit_join", {"time": 5}, timeout=0.05)

    async def test_retained_read_timeout_is_radio_error(self, hass, broker):
        with pytest.raises(radio.RadioError, match="Zigbee2MQTT"):
            await radio.async_z2m_retained(hass, "bridge/nonexistent", timeout=0.05)


class TestAnnouncementGating:
    def test_gate_map_buckets(self, hass):
        data = _data()
        confirm_token = _light_token(cap_radio_write="confirm", cap_diagnostics="allow")
        gate_map = _tool_gate_map(confirm_token, data, hass)
        for name in (
            "permit_zigbee_join", "reconfigure_zigbee_device",
            "set_zigbee_device_options", "remove_zigbee_device",
        ):
            assert name in gate_map["needs_approval"]
        assert "get_radio_network" in gate_map["usable"]

        deny_token = _light_token(cap_radio_write="deny", cap_diagnostics="deny")
        gate_map = _tool_gate_map(deny_token, data, hass)
        for name in (
            "permit_zigbee_join", "reconfigure_zigbee_device",
            "set_zigbee_device_options", "remove_zigbee_device",
            "get_radio_network", "get_radio_device",
        ):
            assert name in gate_map["unavailable"]
