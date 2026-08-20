"""Tests for the radio management tools (network/device/group reads, pairing,
reconfiguration, binding, reporting, options, direct properties, group
management, and device removal).

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
from homeassistant.components.mqtt.const import ATTR_DISCOVERY_PAYLOAD
from homeassistant.components.mqtt.models import DATA_MQTT
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
ZHA_TARGET_IEEE = "00:11:22:33:44:55:66:88"
Z2M_GROUP_NAME = "Test Zigbee Group"
Z2M_GROUP_ID = 42

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
        if topic == "zigbee2mqtt/Z2M Bulb/get":
            state = self.retained.get("zigbee2mqtt/Z2M Bulb")
            if state is not None:
                for cb in list(self.subs.get("zigbee2mqtt/Z2M Bulb", [])):
                    cb(SimpleNamespace(topic=topic, payload=state))
            return
        if topic == "zigbee2mqtt/Z2M Bulb/set":
            state = json.loads(self.retained["zigbee2mqtt/Z2M Bulb"])
            state.update(request)
            self.retained["zigbee2mqtt/Z2M Bulb"] = json.dumps(state)
            for cb in list(self.subs.get("zigbee2mqtt/Z2M Bulb", [])):
                cb(SimpleNamespace(topic=topic, payload=json.dumps(state)))
            return
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
            "endpoints": {
                "1": {
                    "name": "default",
                    "bindings": [],
                    "configured_reportings": [],
                    "clusters": {
                        "input": ["genOnOff", "genLevelCtrl", "genGroups"],
                        "output": ["genOnOff", "genLevelCtrl"],
                    },
                }
            },
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
                }, {
                    "type": "enum", "name": "LED mode", "label": "LED mode",
                    "property": "led_mode", "access": 3,
                    "values": ["off", "on", "auto"],
                }, {
                    "type": "numeric", "name": "Temperature", "label": "Temperature",
                    "property": "temperature", "access": 1,
                    "value_min": -40, "value_max": 125, "unit": "°C",
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
            "endpoints": {
                "1": {
                    "bindings": [],
                    "configured_reportings": [],
                    "clusters": {
                        "input": ["genOnOff", "genGroups"],
                        "output": [],
                    },
                }
            },
            "definition": {"model": "L1", "vendor": "Acme", "description": "Lock"},
        },
    ])
    fake.retained["zigbee2mqtt/Z2M Bulb"] = json.dumps({
        "brightness_left": 120,
        "led_mode": "auto",
        "temperature": 21.5,
    })
    fake.retained["zigbee2mqtt/bridge/groups"] = json.dumps([
        {
            "id": Z2M_GROUP_ID,
            "friendly_name": Z2M_GROUP_NAME,
            "scenes": [],
            "members": [{"ieee_address": Z2M_BULB_IEEE, "endpoint": 1}],
        }
    ])
    fake.responders["permit_join"] = lambda req: {"status": "ok", "data": {"time": req.get("time")}}
    fake.responders["device/configure"] = lambda req: {"status": "ok", "data": {"id": req.get("id")}}
    fake.responders["device/remove"] = lambda req: {"status": "ok", "data": {"id": req.get("id")}}

    def _binding(req, operation):
        devices = json.loads(fake.retained["zigbee2mqtt/bridge/devices"])
        source = next(item for item in devices if item["ieee_address"] == req["from"])
        endpoint = source["endpoints"][str(req["from_endpoint"])]
        for cluster in req["clusters"]:
            binding = {
                "cluster": cluster,
                "target": {
                    "type": "endpoint",
                    "ieee_address": req["to"],
                    "endpoint": req["to_endpoint"],
                },
            }
            if operation == "bind":
                endpoint["bindings"].append(binding)
            else:
                endpoint["bindings"].remove(binding)
        fake.retained["zigbee2mqtt/bridge/devices"] = json.dumps(devices)
        return {
            "status": "ok",
            "data": {
                "from": req["from"],
                "from_endpoint": req["from_endpoint"],
                "to": req["to"],
                "to_endpoint": req["to_endpoint"],
                "clusters": req["clusters"],
                "failed": [],
            },
        }

    fake.responders["device/bind"] = lambda req: _binding(req, "bind")
    fake.responders["device/unbind"] = lambda req: _binding(req, "unbind")

    def _reporting(req):
        devices = json.loads(fake.retained["zigbee2mqtt/bridge/devices"])
        device = next(item for item in devices if item["ieee_address"] == req["id"])
        endpoint = device["endpoints"][str(req["endpoint"])]
        desired = {
            key: req[key]
            for key in (
                "cluster",
                "attribute",
                "minimum_report_interval",
                "maximum_report_interval",
                "reportable_change",
            )
            if key in req
        }
        endpoint["configured_reportings"] = [
            item
            for item in endpoint["configured_reportings"]
            if not (
                item["cluster"] == req["cluster"]
                and item["attribute"] == req["attribute"]
            )
        ]
        endpoint["configured_reportings"].append(desired)
        fake.retained["zigbee2mqtt/bridge/devices"] = json.dumps(devices)
        return {"status": "ok", "data": {"id": req["id"], **desired, "endpoint": req["endpoint"]}}

    fake.responders["device/reporting/configure"] = _reporting

    def _group_add(req):
        groups = json.loads(fake.retained["zigbee2mqtt/bridge/groups"])
        group_id = max((group["id"] for group in groups), default=0) + 1
        groups.append({
            "id": group_id,
            "friendly_name": req["friendly_name"],
            "scenes": [],
            "members": [],
        })
        fake.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        return {
            "status": "ok",
            "data": {"id": group_id, "friendly_name": req["friendly_name"]},
        }

    def _group_member(req, operation):
        groups = json.loads(fake.retained["zigbee2mqtt/bridge/groups"])
        group = next(group for group in groups if group["id"] == req["group"])
        member = {"ieee_address": req["device"], "endpoint": req["endpoint"]}
        if operation == "add":
            group["members"].append(member)
        else:
            group["members"].remove(member)
        fake.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        return {
            "status": "ok",
            "data": {
                "group": req["group"],
                "device": req["device"],
                "endpoint": req["endpoint"],
            },
        }

    def _group_remove(req):
        groups = json.loads(fake.retained["zigbee2mqtt/bridge/groups"])
        groups = [group for group in groups if group["id"] != req["id"]]
        fake.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        return {"status": "ok", "data": {"id": req["id"], "force": req["force"]}}

    fake.responders["group/add"] = _group_add
    fake.responders["group/members/add"] = lambda req: _group_member(req, "add")
    fake.responders["group/members/remove"] = lambda req: _group_member(req, "remove")
    fake.responders["group/remove"] = _group_remove

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

    def _device(identifiers, name, *, via_device_id=None):
        return dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers=identifiers, name=name,
            manufacturer="Acme", model="M1", via_device_id=via_device_id,
        )

    bridge = _device({("mqtt", "zigbee2mqtt_bridge_0x00124b002e1e03c7")}, "Z2M Bridge")
    z2m_bulb = _device(
        {("mqtt", f"zigbee2mqtt_{Z2M_BULB_IEEE}")},
        "Z2M Bulb",
        via_device_id=bridge.id,
    )
    z2m_lock = _device(
        {("mqtt", f"zigbee2mqtt_{Z2M_LOCK_IEEE}")},
        "Z2M Lock",
        via_device_id=bridge.id,
    )
    zha_bulb = _device({("zha", ZHA_BULB_IEEE)}, "ZHA Bulb")
    zha_target = _device({("zha", ZHA_TARGET_IEEE)}, "ZHA Target")
    plain = _device({("mqtt", "some_other_thing")}, "Plain Device")
    z2m_group = _device(
        {("mqtt", "zigbee2mqtt_group_42")},
        Z2M_GROUP_NAME,
        via_device_id=bridge.id,
    )

    def _entity(domain, uid, device, object_id):
        e = ent_reg.async_get_or_create(
            domain, "mqtt", uid, config_entry=entry, device_id=device.id,
            suggested_object_id=object_id,
        )
        hass.states.async_set(e.entity_id, "on", {})
        return e.entity_id

    result = {
        "z2m_bulb": z2m_bulb.id,
        "z2m_bulb_eid": _entity("light", "u1", z2m_bulb, "z2m_bulb"),
        "z2m_lock": z2m_lock.id,
        "z2m_lock_eid": _entity("lock", "u2", z2m_lock, "z2m_lock"),
        "zha_bulb": zha_bulb.id,
        "zha_bulb_eid": _entity("light", "u3", zha_bulb, "zha_bulb"),
        "zha_target": zha_target.id,
        "zha_target_eid": _entity("light", "u6", zha_target, "zha_target"),
        "bridge": bridge.id,
        "bridge_eid": _entity("light", "u4", bridge, "bridge_glow"),
        "plain": plain.id,
        "plain_eid": _entity("light", "u5", plain, "plain_bulb"),
        "z2m_group": z2m_group.id,
        "z2m_group_eid": _entity("light", "u7", z2m_group, "test_zigbee_group"),
    }
    hass.data[DATA_MQTT] = SimpleNamespace(
        debug_info_entities={
            result["z2m_bulb_eid"]: {
                "discovery_data": {
                    ATTR_DISCOVERY_PAYLOAD: {
                        "schema": "json",
                        "state_topic": "zigbee2mqtt/Z2M Bulb",
                        "command_topic": "zigbee2mqtt/Z2M Bulb/set",
                        "brightness": True,
                    }
                }
            },
            result["z2m_group_eid"]: {
                "discovery_data": {
                    ATTR_DISCOVERY_PAYLOAD: {
                        "schema": "json",
                        "state_topic": f"zigbee2mqtt/{Z2M_GROUP_NAME}",
                        "command_topic": f"zigbee2mqtt/{Z2M_GROUP_NAME}/set",
                    }
                }
            },
        }
    )
    return result


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


def _z2m_pair_token(*, lock_state: str = "GREEN", **caps) -> TokenRecord:
    tree = PermissionTree(
        domains={
            "light": PermissionNode(state="GREEN"),
            "lock": PermissionNode(state=lock_state),
        }
    )
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

    def get_settings(self):
        return SimpleNamespace(mesa_mode="off")


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


class TestScanZigbeeTopology:
    @staticmethod
    def _raw_map() -> dict:
        return {
            "nodes": [
                {
                    "ieeeAddr": BRIDGE_INFO["coordinator"]["ieee_address"],
                    "friendlyName": "Coordinator secret name",
                    "type": "Coordinator",
                    "networkAddress": 0,
                },
                {
                    "ieeeAddr": Z2M_BULB_IEEE,
                    "friendlyName": "Z2M Bulb",
                    "type": "Router",
                    "networkAddress": 4660,
                    "definition": {"model": "secret model"},
                },
                {
                    "ieeeAddr": Z2M_LOCK_IEEE,
                    "friendlyName": "Hidden Lock",
                    "type": "EndDevice",
                    "networkAddress": 4661,
                    "failed": ["lqi"],
                },
            ],
            "links": [
                {
                    "source": {
                        "ieeeAddr": BRIDGE_INFO["coordinator"]["ieee_address"],
                        "networkAddress": 0,
                    },
                    "target": {"ieeeAddr": Z2M_BULB_IEEE, "networkAddress": 4660},
                    "lqi": 211,
                    "relationship": 1,
                    "routes": [{"destinationAddress": 4661, "nextHopAddress": 4660}],
                },
                {
                    "source": {"ieeeAddr": Z2M_LOCK_IEEE, "networkAddress": 4661},
                    "target": {"ieeeAddr": Z2M_BULB_IEEE, "networkAddress": 4660},
                    "lqi": 87,
                    "relationship": 2,
                },
            ],
        }

    async def test_denied_capability_is_not_an_oracle(self, hass, radio_env, broker):
        hass.config.components.add("mqtt")
        for token in (
            _light_token(cap_diagnostics="deny", cap_radio_write="allow"),
            _light_token(cap_diagnostics="allow", cap_radio_write="deny"),
        ):
            result = await _call_tool(
                "scan_zigbee_topology", {"backend": "z2m"}, token, hass, _data()
            )
            assert result[1] == "denied"
            assert broker.published == []

    async def test_confirm_previews_active_scan_without_publishing(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        data, store = _appr_data()
        token = _light_token(cap_diagnostics="allow", cap_radio_write="confirm")
        result = await _call_tool(
            "scan_zigbee_topology",
            {"backend": "z2m"},
            token,
            hass,
            data,
            "rid-topology",
            "1.2.3.4",
        )
        assert result[1] == "pending_approval"
        assert broker.published == []
        approval = store._p[0]
        assert approval["tool_name"] == "scan_zigbee_topology"
        assert approval["cap_name"] == "cap_radio_write"
        assert approval["diff"]["preview"]["routes"] is False
        assert "less responsive" in approval["diff"]["preview"]["warning"]

    async def test_z2m_scan_is_transaction_correlated_and_scope_projected(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        broker.responders["networkmap"] = lambda req: {
            "status": "ok",
            "data": {"type": "raw", "routes": False, "value": self._raw_map()},
        }
        token = _light_token(cap_diagnostics="allow", cap_radio_write="allow")
        result = await _call_tool(
            "scan_zigbee_topology", {"backend": "z2m"}, token, hass, _data()
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["backend"] == "z2m"
        assert body["nodes"] == [
            {"node_id": "coordinator", "kind": "coordinator", "type": "coordinator"},
            {
                "node_id": radio_env["z2m_bulb"],
                "kind": "device",
                "device_id": radio_env["z2m_bulb"],
                "type": "router",
            },
        ]
        assert body["links"] == [
            {
                "source": "coordinator",
                "target": radio_env["z2m_bulb"],
                "lqi": 211,
                "relationship": "child",
            }
        ]
        assert body["partial"] is True
        assert body["routes_included"] is False
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/networkmap"
        assert request["type"] == "raw" and request["routes"] is False
        assert isinstance(request["transaction"], str)
        text = _text(result)
        for secret in (
            Z2M_BULB_IEEE,
            Z2M_LOCK_IEEE,
            BRIDGE_INFO["coordinator"]["ieee_address"],
            "Hidden Lock",
            "secret model",
            "networkAddress",
            "destinationAddress",
            "failed",
        ):
            assert secret not in text

    async def test_both_backends_require_selection(self, hass, radio_env, broker):
        hass.config.components.update({"mqtt", "zha"})
        result = await _call_tool(
            "scan_zigbee_topology",
            {},
            _light_token(cap_diagnostics="allow", cap_radio_write="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "Both Zigbee backends" in _text(result)
        assert broker.published == []

    async def test_zha_backend_scans_only_visible_routers(
        self, hass, radio_env, broker, monkeypatch
    ):
        coordinator = SimpleNamespace(
            ieee="00:12:4b:00:2e:1e:03:c7",
            node_desc=SimpleNamespace(is_router=False),
        )
        visible_router = SimpleNamespace(
            ieee=ZHA_BULB_IEEE, node_desc=SimpleNamespace(is_router=True)
        )
        visible_end_device = SimpleNamespace(
            ieee=ZHA_TARGET_IEEE, node_desc=SimpleNamespace(is_router=False)
        )
        hidden_router = SimpleNamespace(
            ieee="00:99:99:99:99:99:99:99",
            node_desc=SimpleNamespace(is_router=True),
        )
        neighbor = SimpleNamespace(
            ieee=coordinator.ieee,
            lqi=190,
            relationship=SimpleNamespace(name="Parent"),
            device_type=SimpleNamespace(name="Coordinator"),
        )

        class _Topology:
            def __init__(self):
                self.scanned = None
                self.neighbors = {visible_router.ieee: [neighbor]}

            async def scan(self, *, devices):
                self.scanned = list(devices)

        topology = _Topology()
        app = SimpleNamespace(
            _device=coordinator,
            devices={
                coordinator.ieee: coordinator,
                visible_router.ieee: visible_router,
                visible_end_device.ieee: visible_end_device,
                hidden_router.ieee: hidden_router,
            },
            topology=topology,
        )
        monkeypatch.setattr(
            radio,
            "_get_zha_gateway",
            lambda _hass: SimpleNamespace(application_controller=app),
        )
        raw = await radio.async_scan_zha_topology(
            hass, {ZHA_BULB_IEEE.lower(), ZHA_TARGET_IEEE.lower()}
        )
        assert topology.scanned == [visible_router]
        assert all(item["ieeeAddr"] != hidden_router.ieee for item in raw["nodes"])
        projected = radio.project_zigbee_topology(
            "zha",
            raw,
            {
                ZHA_BULB_IEEE.lower(): radio_env["zha_bulb"],
                ZHA_TARGET_IEEE.lower(): radio_env["zha_target"],
            },
        )
        assert projected["links"] == [
            {
                "source": "coordinator",
                "target": radio_env["zha_bulb"],
                "lqi": 190,
                "relationship": "parent",
            }
        ]
        assert all("ieee" not in json.dumps(item).lower() for item in projected["nodes"])


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
        assert expose["total"] == 3 and expose["truncated"] is False
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
        fallback = body["direct_property_fallback"]
        assert fallback["status"] == "available"
        assert [item["property"] for item in fallback["properties"]] == [
            "led_mode", "temperature"
        ]
        assert fallback["properties"][0]["writable"] is True
        assert fallback["properties"][1]["writable"] is False
        text = _text(res)
        for secret in ("network_key", "core-mosquitto", "hunter2", "config_schema"):
            assert secret not in text

    async def test_z2m_binding_targets_are_scope_projected(
        self, hass, radio_env, broker
    ):
        devices = json.loads(broker.retained["zigbee2mqtt/bridge/devices"])
        source = next(item for item in devices if item["ieee_address"] == Z2M_BULB_IEEE)
        source["endpoints"]["1"]["bindings"] = [
            {
                "cluster": "genOnOff",
                "target": {
                    "type": "endpoint",
                    "ieee_address": Z2M_LOCK_IEEE,
                    "endpoint": 1,
                },
            },
            {
                "cluster": "genLevelCtrl",
                "target": {"type": "group", "id": 42},
            },
            {
                "cluster": "genOnOff",
                "target": {
                    "type": "endpoint",
                    "ieee_address": BRIDGE_INFO["coordinator"]["ieee_address"],
                    "endpoint": 1,
                },
            },
        ]
        broker.retained["zigbee2mqtt/bridge/devices"] = json.dumps(devices)
        hass.config.components.add("mqtt")

        hidden = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"]},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        hidden_config = _body(hidden)["radio_configuration"]
        targets = [
            binding["target"]
            for binding in hidden_config["endpoints"][0]["bindings"]
        ]
        assert {target["kind"] for target in targets} == {
            "coordinator",
            "group",
            "redacted_device",
        }
        assert all("ieee_address" not in target for target in targets)
        assert all("id" not in target for target in targets)

        visible = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"]},
            _z2m_pair_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        visible_targets = [
            binding["target"]
            for binding in _body(visible)["radio_configuration"]["endpoints"][0]["bindings"]
        ]
        assert any(
            target.get("device_id") == radio_env["z2m_lock"]
            for target in visible_targets
        )

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
        assert radio.z2m_exposed_property_map(entry) == {}
        assert len(body["exposes"]["items"][0]["description"]) == 512
        assert "secret_extension" not in json.dumps(body)

        malformed = radio.project_z2m_device({"definition": ["not", "a", "mapping"]})
        assert malformed["exposes"] == {"items": [], "total": 0, "truncated": False}
        assert malformed["options"]["definitions"] == {
            "items": [], "total": 0, "truncated": False,
        }

        duplicate = {
            "definition": {
                "exposes": [
                    {"type": "enum", "property": "mode", "values": ["a", "b"]},
                    {"type": "enum", "property": "mode", "values": ["a", "b"]},
                ]
            }
        }
        assert radio.z2m_exposed_property_map(duplicate) == {}

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

    async def test_direct_fallback_reads_one_exact_property(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": "led_mode"},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        read = _body(result)["property_read"]
        assert read["property"] == "led_mode"
        assert read["value"] == "auto"
        assert len(read["content_hash"]) == 64
        assert broker.published[-1] == (
            "zigbee2mqtt/Z2M Bulb/get", {"led_mode": ""}
        )

    async def test_direct_fallback_refuses_owned_property(
        self, hass, radio_env, broker
    ):
        mqtt_data = hass.data[DATA_MQTT]
        payload = mqtt_data.debug_info_entities[radio_env["z2m_bulb_eid"]][
            "discovery_data"
        ][ATTR_DISCOVERY_PAYLOAD]
        payload["value_template"] = '{{ value_json["led_mode"] }}'
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": "led_mode"},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert radio_env["z2m_bulb_eid"] in _text(result)
        assert not any(topic.endswith("/get") for topic, _ in broker.published)

    async def test_inaccessible_owner_blocks_without_revealing_entity_id(
        self, hass, radio_env, broker
    ):
        config_entry = hass.config_entries.async_get_entry("e_radio")
        assert config_entry is not None
        hidden = er.async_get(hass).async_get_or_create(
            "sensor",
            "mqtt",
            "hidden_led_mode_owner",
            config_entry=config_entry,
            device_id=radio_env["z2m_bulb"],
            suggested_object_id="secret_led_mode",
        )
        hass.data[DATA_MQTT].debug_info_entities[hidden.entity_id] = {
            "discovery_data": {
                ATTR_DISCOVERY_PAYLOAD: {
                    "state_topic": "zigbee2mqtt/Z2M Bulb",
                    "value_template": '{{ value_json["led_mode"] }}',
                }
            }
        }
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": "led_mode"},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "entity that owns this property" in _text(result)
        assert hidden.entity_id not in _text(result)
        assert not any(topic.endswith("/get") for topic, _ in broker.published)

    async def test_missing_discovery_metadata_fails_closed(
        self, hass, radio_env, broker
    ):
        hass.data[DATA_MQTT].debug_info_entities.clear()
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": "led_mode"},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "ownership is ambiguous" in _text(result)
        assert not any(topic.endswith("/get") for topic, _ in broker.published)

    async def test_disabled_entity_cannot_unlock_direct_fallback(
        self, hass, radio_env, broker
    ):
        er.async_get(hass).async_update_entity(
            radio_env["z2m_bulb_eid"], disabled_by=er.RegistryEntryDisabler.USER
        )
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": "led_mode"},
            _light_token(cap_diagnostics="allow"),
            hass,
            _data(),
        )
        # Disabling the only visible entity also removes device visibility. That
        # is even stricter than the ownership ambiguity block and must still
        # happen before any device-topic publish.
        assert result[1] in {"not_found", "invalid_request"}
        assert not any(topic.endswith("/get") for topic, _ in broker.published)

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


class TestSetZigbeeDeviceProperty:
    async def _current(self, hass, radio_env, token, property_name="led_mode") -> dict:
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"], "property": property_name},
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        return _body(result)["property_read"]

    @pytest.mark.parametrize(
        ("radio_cap", "physical_cap"),
        [("deny", "allow"), ("allow", "deny")],
    )
    async def test_either_capability_deny_is_not_an_oracle(
        self, hass, radio_env, broker, radio_cap, physical_cap
    ):
        result = await _call_tool(
            "set_zigbee_device_property",
            {
                "device_id": radio_env["z2m_bulb"],
                "property": "led_mode",
                "value": "on",
                "expected_hash": "0" * 64,
            },
            _light_token(
                cap_radio_write=radio_cap,
                cap_physical_control=physical_cap,
            ),
            hass,
            _data(),
        )
        assert result[1] == "denied"
        assert broker.published == []

    async def test_allow_changes_and_exactly_confirms_property(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        current = await self._current(hass, radio_env, token)
        data = _data()
        result = await _call_tool(
            "set_zigbee_device_property",
            {
                "device_id": radio_env["z2m_bulb"],
                "property": "led_mode",
                "value": "on",
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            data,
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["previous"] == "auto"
        assert body["value"] == "on"
        assert body["confirmation"] == "device_state"
        assert broker.published[-1] == (
            "zigbee2mqtt/Z2M Bulb/set", {"led_mode": "on"}
        )
        record = data.versions.list_for("device", radio_env["z2m_bulb"])[0]
        assert record.before["snapshot_type"] == "zigbee_exposed_property"
        assert record.before["restorable"] is False
        assert record.before["value"] == "auto"
        assert record.after["value"] == "on"

    async def test_read_only_expose_cannot_be_written(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        current = await self._current(hass, radio_env, token, "temperature")
        broker.published.clear()
        result = await _call_tool(
            "set_zigbee_device_property",
            {
                "device_id": radio_env["z2m_bulb"],
                "property": "temperature",
                "value": 22,
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "non-readable/writable" in _text(result)
        assert broker.published == []

    async def test_stale_hash_never_writes(self, hass, radio_env, broker):
        token = _light_token(
            cap_radio_write="allow", cap_physical_control="allow"
        )
        result = await _call_tool(
            "set_zigbee_device_property",
            {
                "device_id": radio_env["z2m_bulb"],
                "property": "led_mode",
                "value": "on",
                "expected_hash": "0" * 64,
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "changed after it was read" in _text(result)
        assert not any(topic.endswith("/set") for topic, _ in broker.published)

    async def test_capability_confirm_then_approve_executes(
        self, hass, radio_env, broker
    ):
        data, store = _appr_data()
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="confirm",
            cap_physical_control="allow",
        )
        current = await self._current(hass, radio_env, token)
        broker.published.clear()
        args = {
            "device_id": radio_env["z2m_bulb"],
            "property": "led_mode",
            "value": "on",
            "expected_hash": current["content_hash"],
        }
        result = await _call_tool(
            "set_zigbee_device_property",
            args,
            token,
            hass,
            data,
            "rid-property",
            "1.2.3.4",
        )
        assert result[1] == "pending_approval"
        assert not any(topic.endswith("/set") for topic, _ in broker.published)
        approval = store._p[0]
        assert approval["tool_name"] == "set_zigbee_device_property"
        assert approval["diff"]["preview"]["property"] == "led_mode"
        output = await async_execute_approved_tool(
            "set_zigbee_device_property", approval["args"], token, hass, data
        )
        assert output[1] == "allowed"
        assert broker.published[-1][0].endswith("/set")

    async def test_executor_rechecks_both_capabilities_after_approval(
        self, hass, radio_env, broker
    ):
        data, store = _appr_data()
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="confirm",
            cap_physical_control="allow",
        )
        current = await self._current(hass, radio_env, token)
        await _call_tool(
            "set_zigbee_device_property",
            {
                "device_id": radio_env["z2m_bulb"],
                "property": "led_mode",
                "value": "on",
                "expected_hash": current["content_hash"],
            },
            token,
            hass,
            data,
        )
        token.cap_physical_control = "deny"
        broker.published.clear()
        output = await async_execute_approved_tool(
            "set_zigbee_device_property", store._p[0]["args"], token, hass, data
        )
        assert output[1] == "denied"
        assert broker.published == []


class TestSetZigbeeBinding:
    async def _configuration(self, hass, device_id, token) -> dict:
        result = await _call_tool(
            "get_radio_device",
            {"device_id": device_id},
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        configuration = _body(result)["radio_configuration"]
        assert configuration["status"] == "available"
        return configuration

    async def _args(self, hass, radio_env, token, operation="bind") -> dict:
        source = await self._configuration(
            hass, radio_env["z2m_bulb"], token
        )
        target = await self._configuration(
            hass, radio_env["z2m_lock"], token
        )
        return {
            "operation": operation,
            "source_device_id": radio_env["z2m_bulb"],
            "target_device_id": radio_env["z2m_lock"],
            "source_endpoint": 1,
            "target_endpoint": 1,
            "clusters": ["genOnOff"],
            "expected_source_hash": source["content_hash"],
            "expected_target_hash": target["content_hash"],
        }

    @pytest.mark.parametrize(
        ("radio_cap", "physical_cap"),
        [("deny", "allow"), ("allow", "deny")],
    )
    async def test_either_capability_deny_is_not_an_oracle(
        self, hass, radio_env, broker, radio_cap, physical_cap
    ):
        token = _z2m_pair_token(
            cap_radio_write=radio_cap,
            cap_physical_control=physical_cap,
        )
        bodies = set()
        for source, target in (
            ("ghost", radio_env["z2m_lock"]),
            (radio_env["z2m_bulb"], "ghost"),
            (radio_env["z2m_bulb"], radio_env["z2m_lock"]),
        ):
            result = await _call_tool(
                "set_zigbee_binding",
                {
                    "operation": "bind",
                    "source_device_id": source,
                    "target_device_id": target,
                },
                token,
                hass,
                _data(),
            )
            assert result[1] == "denied"
            bodies.add(json.dumps(result[0]))
        assert len(bodies) == 1
        assert broker.published == []

    async def test_both_devices_require_write_scope(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            lock_state="YELLOW",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        result = await _call_tool(
            "set_zigbee_binding",
            {
                "operation": "bind",
                "source_device_id": radio_env["z2m_bulb"],
                "target_device_id": radio_env["z2m_lock"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "denied"
        assert "write access" in _text(result)
        assert broker.published == []

    async def test_z2m_devices_on_different_bridges_are_rejected(
        self, hass, radio_env, broker
    ):
        registry = dr.async_get(hass)
        other_bridge = registry.async_get_or_create(
            config_entry_id="e_radio",
            identifiers={("mqtt", "zigbee2mqtt_bridge_0x00124b002e1e03d8")},
            name="Other Z2M Bridge",
        )
        registry.async_update_device(
            radio_env["z2m_lock"], via_device_id=other_bridge.id
        )
        token = _z2m_pair_token(
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        result = await _call_tool(
            "set_zigbee_binding",
            {
                "operation": "bind",
                "source_device_id": radio_env["z2m_bulb"],
                "target_device_id": radio_env["z2m_lock"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "same Zigbee network" in _text(result)
        assert broker.published == []

    async def test_z2m_bind_and_unbind_exact_relationship(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        data = _data()
        bind_args = await self._args(hass, radio_env, token)
        result = await _call_tool(
            "set_zigbee_binding", bind_args, token, hass, data
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["confirmation"] == "retained_binding_state"
        assert body["source_content_hash"] != bind_args["expected_source_hash"]
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/device/bind"
        assert request["from"] == Z2M_BULB_IEEE
        assert request["to"] == Z2M_LOCK_IEEE
        assert request["from_endpoint"] == request["to_endpoint"] == 1
        assert request["clusters"] == ["genOnOff"]
        assert request["skip_disable_reporting"] is True

        unbind_args = await self._args(hass, radio_env, token, "unbind")
        unbound = await _call_tool(
            "set_zigbee_binding", unbind_args, token, hass, data
        )
        assert unbound[1] == "allowed"
        assert _body(unbound)["confirmation"] == "retained_binding_state"
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/device/unbind"
        assert request["skip_disable_reporting"] is True
        records = data.versions.list_for("device", radio_env["z2m_bulb"])
        assert len(records) == 2
        assert records[0].before["restorable"] is False
        assert records[0].before["snapshot_type"] == "zigbee_binding"
        assert Z2M_BULB_IEEE not in json.dumps(records[0].before)
        assert Z2M_LOCK_IEEE not in json.dumps(records[0].before)

    async def test_incompatible_cluster_and_stale_hash_never_publish(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        args = await self._args(hass, radio_env, token)
        broker.published.clear()
        incompatible = await _call_tool(
            "set_zigbee_binding",
            {**args, "clusters": ["genLevelCtrl"]},
            token,
            hass,
            _data(),
        )
        assert incompatible[1] == "invalid_request"
        assert "not source-output/target-input compatible" in _text(incompatible)
        stale = await _call_tool(
            "set_zigbee_binding",
            {**args, "expected_target_hash": "0" * 64},
            token,
            hass,
            _data(),
        )
        assert stale[1] == "invalid_request"
        assert "changed after it was read" in _text(stale)
        assert broker.published == []

    async def test_malformed_cluster_response_fails_closed(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        args = await self._args(hass, radio_env, token)
        broker.responders["device/bind"] = lambda req: {
            "status": "ok",
            "data": {"clusters": [{"name": "genOnOff"}], "failed": []},
        }
        result = await _call_tool(
            "set_zigbee_binding", args, token, hass, _data()
        )
        assert result[1] == "invalid_request"
        assert "did not confirm every requested cluster" in _text(result)

    async def test_confirm_queues_once_and_revalidates_both_caps(
        self, hass, radio_env, broker
    ):
        data, store = _appr_data()
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="confirm",
            cap_physical_control="confirm",
        )
        args = await self._args(hass, radio_env, token)
        broker.published.clear()
        result = await _call_tool(
            "set_zigbee_binding",
            args,
            token,
            hass,
            data,
            "rid-binding",
            "1.2.3.4",
        )
        assert result[1] == "pending_approval"
        assert len(store._p) == 1
        assert store._p[0]["tool_name"] == "set_zigbee_binding"
        assert broker.published == []
        token.cap_physical_control = "deny"
        output = await async_execute_approved_tool(
            "set_zigbee_binding", store._p[0]["args"], token, hass, data
        )
        assert output[1] == "denied"
        assert broker.published == []

    async def test_zha_uses_registry_derived_addresses_and_rejects_z2m_fields(
        self, hass, radio_env, broker, monkeypatch
    ):
        dispatch = AsyncMock(return_value=None)
        monkeypatch.setattr(radio, "async_ws_command", dispatch)
        token = _light_token(
            cap_radio_write="allow", cap_physical_control="allow"
        )
        args = {
            "operation": "bind",
            "source_device_id": radio_env["zha_bulb"],
            "target_device_id": radio_env["zha_target"],
        }
        result = await _call_tool(
            "set_zigbee_binding", args, token, hass, _data()
        )
        assert result[1] == "allowed"
        dispatch.assert_awaited_once_with(
            hass,
            "zha/devices/bind",
            {"source_ieee": ZHA_BULB_IEEE, "target_ieee": ZHA_TARGET_IEEE},
            timeout=radio.Z2M_REQUEST_TIMEOUT_SECONDS,
        )
        dispatch.reset_mock()
        rejected = await _call_tool(
            "set_zigbee_binding",
            {**args, "clusters": ["genOnOff"]},
            token,
            hass,
            _data(),
        )
        assert rejected[1] == "invalid_request"
        assert "omit endpoints, clusters, and hashes" in _text(rejected)
        dispatch.assert_not_awaited()

        self_binding = await _call_tool(
            "set_zigbee_binding",
            {**args, "target_device_id": radio_env["zha_bulb"]},
            token,
            hass,
            _data(),
        )
        assert self_binding[1] == "invalid_request"
        assert "must be different" in _text(self_binding)


class TestConfigureZigbeeReporting:
    async def _current(self, hass, radio_env, token) -> dict:
        result = await _call_tool(
            "get_radio_device",
            {"device_id": radio_env["z2m_bulb"]},
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        return _body(result)["radio_configuration"]

    async def _args(self, hass, radio_env, token) -> dict:
        current = await self._current(hass, radio_env, token)
        return {
            "device_id": radio_env["z2m_bulb"],
            "endpoint": 1,
            "cluster": "genLevelCtrl",
            "attribute": "currentLevel",
            "minimum_report_interval": 5,
            "maximum_report_interval": 60,
            "reportable_change": 10,
            "expected_hash": current["content_hash"],
        }

    async def test_cap_deny_not_an_oracle(self, hass, radio_env, broker):
        token = _light_token(cap_radio_write="deny")
        bodies = set()
        for device_id in ("ghost", radio_env["z2m_bulb"]):
            result = await _call_tool(
                "configure_zigbee_reporting",
                {
                    "device_id": device_id,
                    "endpoint": 1,
                    "cluster": "genLevelCtrl",
                    "attribute": "currentLevel",
                    "minimum_report_interval": 5,
                    "maximum_report_interval": 60,
                    "expected_hash": "0" * 64,
                },
                token,
                hass,
                _data(),
            )
            assert result[1] == "denied"
            bodies.add(json.dumps(result[0]))
        assert len(bodies) == 1
        assert broker.published == []

    async def test_configures_and_exactly_confirms_reporting(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow", cap_radio_write="allow"
        )
        args = await self._args(hass, radio_env, token)
        data = _data()
        result = await _call_tool(
            "configure_zigbee_reporting", args, token, hass, data
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["confirmation"] == "retained_reporting_state"
        assert body["reporting"]["attribute"] == "currentLevel"
        assert body["content_hash"] != args["expected_hash"]
        topic, request = broker.published[-1]
        assert topic == "zigbee2mqtt/bridge/request/device/reporting/configure"
        assert request["id"] == Z2M_BULB_IEEE
        assert request["endpoint"] == 1
        assert "options" not in request and "topic" not in request
        record = data.versions.list_for("device", radio_env["z2m_bulb"])[0]
        assert record.before["snapshot_type"] == "zigbee_reporting"
        assert record.before["restorable"] is False
        assert Z2M_BULB_IEEE not in json.dumps(record.before)
        assert Z2M_BULB_IEEE not in json.dumps(record.after)

    @pytest.mark.parametrize(
        "change",
        [
            {"cluster": "genScenes"},
            {"endpoint": 2},
            {"minimum_report_interval": -1},
            {"maximum_report_interval": 65536},
            {"reportable_change": -1},
        ],
    )
    async def test_invalid_endpoint_cluster_or_interval_never_publish(
        self, hass, radio_env, broker, change
    ):
        token = _light_token(
            cap_diagnostics="allow", cap_radio_write="allow"
        )
        args = await self._args(hass, radio_env, token)
        broker.published.clear()
        result = await _call_tool(
            "configure_zigbee_reporting",
            {**args, **change},
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert broker.published == []

    async def test_zha_and_stale_hash_fail_before_publish(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow", cap_radio_write="allow"
        )
        args = await self._args(hass, radio_env, token)
        broker.published.clear()
        stale = await _call_tool(
            "configure_zigbee_reporting",
            {**args, "expected_hash": "0" * 64},
            token,
            hass,
            _data(),
        )
        assert stale[1] == "invalid_request"
        zha = await _call_tool(
            "configure_zigbee_reporting",
            {**args, "device_id": radio_env["zha_bulb"]},
            token,
            hass,
            _data(),
        )
        assert zha[1] == "invalid_request"
        assert "only for Zigbee2MQTT" in _text(zha)
        assert broker.published == []

    async def test_mismatched_response_fails_closed(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow", cap_radio_write="allow"
        )
        args = await self._args(hass, radio_env, token)
        broker.published.clear()
        broker.responders["device/reporting/configure"] = lambda req: {
            "status": "ok",
            "data": {**req, "maximum_report_interval": 61},
        }
        result = await _call_tool(
            "configure_zigbee_reporting", args, token, hass, _data()
        )
        assert result[1] == "invalid_request"
        assert "mismatched reporting confirmation" in _text(result)


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


class TestZigbeeGroups:
    async def _z2m_group(self, hass, radio_env, token):
        hass.config.components.add("mqtt")
        result = await _call_tool("get_zigbee_groups", {}, token, hass, _data())
        assert result[1] == "allowed"
        groups = _body(result)["groups"]
        return next(group for group in groups if group["name"] == Z2M_GROUP_NAME)

    async def test_read_projects_registry_ids_without_radio_identifiers(
        self, hass, radio_env, broker
    ):
        group = await self._z2m_group(
            hass, radio_env, _light_token(cap_diagnostics="allow")
        )
        assert group["group_entity_ids"] == [radio_env["z2m_group_eid"]]
        assert group["members"] == [{"device_id": radio_env["z2m_bulb"], "endpoint": 1}]
        assert group["hidden_member_count"] == 0
        assert len(group["content_hash"]) == 64
        rendered = json.dumps(group)
        assert Z2M_BULB_IEEE not in rendered
        assert f'"id": {Z2M_GROUP_ID}' not in rendered
        assert "zigbee2mqtt/" not in rendered

    async def test_read_redacts_inaccessible_members(self, hass, radio_env, broker):
        groups = json.loads(broker.retained["zigbee2mqtt/bridge/groups"])
        groups[0]["members"].append(
            {"ieee_address": Z2M_LOCK_IEEE, "endpoint": 1}
        )
        broker.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        group = await self._z2m_group(
            hass, radio_env, _light_token(cap_diagnostics="allow")
        )
        assert group["hidden_member_count"] == 1
        assert group["fully_scoped"] is False
        assert Z2M_LOCK_IEEE not in json.dumps(group)
        assert radio_env["z2m_lock"] not in json.dumps(group)

    async def test_create_allocates_id_and_resolves_member_ieee_internally(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        token = _z2m_pair_token(
            cap_radio_write="allow", cap_physical_control="allow"
        )
        result = await _call_tool(
            "create_zigbee_group",
            {
                "name": "New Safe Group",
                "members": [{"device_id": radio_env["z2m_lock"], "endpoint": 1}],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        body = _body(result)
        assert body["name"] == "New Safe Group" and len(body["content_hash"]) == 64
        assert Z2M_LOCK_IEEE not in json.dumps(body)
        paths = [topic.rsplit("/request/", 1)[-1] for topic, _ in broker.published]
        assert paths[-2:] == ["group/add", "group/members/add"]
        assert "id" not in next(
            request for topic, request in broker.published if topic.endswith("group/add")
        )

    async def test_create_rejects_topic_name_without_publish(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        result = await _call_tool(
            "create_zigbee_group",
            {
                "name": "unsafe/topic",
                "members": [{"device_id": radio_env["z2m_bulb"], "endpoint": 1}],
            },
            _light_token(cap_radio_write="allow", cap_physical_control="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert broker.published == []

    async def test_create_rejects_reserved_bridge_name_without_publish(
        self, hass, radio_env, broker
    ):
        hass.config.components.add("mqtt")
        result = await _call_tool(
            "create_zigbee_group",
            {
                "name": "bridge",
                "members": [{"device_id": radio_env["z2m_bulb"], "endpoint": 1}],
            },
            _light_token(cap_radio_write="allow", cap_physical_control="allow"),
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert broker.published == []

    async def test_membership_add_and_remove_preserves_reporting(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        group = await self._z2m_group(hass, radio_env, token)
        add = await _call_tool(
            "set_zigbee_group_members",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "operation": "add",
                "members": [{"device_id": radio_env["z2m_lock"], "endpoint": 1}],
                "expected_hash": group["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert add[1] == "allowed"
        add_hash = _body(add)["content_hash"]
        remove = await _call_tool(
            "set_zigbee_group_members",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "operation": "remove",
                "members": [{"device_id": radio_env["z2m_lock"], "endpoint": 1}],
                "expected_hash": add_hash,
            },
            token,
            hass,
            _data(),
        )
        assert remove[1] == "allowed"
        topic, payload = broker.published[-1]
        assert topic.endswith("group/members/remove")
        assert payload["skip_disable_reporting"] is True
        assert Z2M_LOCK_IEEE not in json.dumps(_body(remove))

    async def test_membership_cannot_remove_last_anchor(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        group = await self._z2m_group(hass, radio_env, token)
        broker.published.clear()
        result = await _call_tool(
            "set_zigbee_group_members",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "operation": "remove",
                "members": [
                    {"device_id": radio_env["z2m_bulb"], "endpoint": 1}
                ],
                "expected_hash": group["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert "remove_zigbee_group" in _text(result)
        assert broker.published == []

    async def test_backend_error_does_not_echo_radio_identifiers(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        group = await self._z2m_group(hass, radio_env, token)
        broker.published.clear()
        broker.responders["group/remove"] = lambda _req: {
            "status": "error",
            "error": f"failed {Z2M_BULB_IEEE} at zigbee2mqtt/private",
        }
        result = await _call_tool(
            "remove_zigbee_group",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "expected_hash": group["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "invalid_request"
        assert Z2M_BULB_IEEE not in _text(result)
        assert "zigbee2mqtt/" not in _text(result)

    async def test_visible_device_mapping_is_backend_qualified(
        self, hass, radio_env, broker
    ):
        registry = dr.async_get(hass)
        collision = registry.async_get_or_create(
            config_entry_id="e_radio",
            identifiers={("zha", Z2M_BULB_IEEE)},
            name="Same address on ZHA",
        )
        entity = er.async_get(hass).async_get_or_create(
            "light",
            "mqtt",
            "same_address_on_zha",
            config_entry=MockConfigEntry(domain="mqtt", entry_id="e_radio"),
            device_id=collision.id,
        )
        hass.states.async_set(entity.entity_id, "on", {})
        mapping = radio_tools._scoped_zigbee_device_map(
            hass, {radio_env["z2m_bulb_eid"], entity.entity_id}
        )
        assert mapping[(radio.BACKEND_Z2M, Z2M_BULB_IEEE)] == radio_env["z2m_bulb"]
        assert mapping[(radio.BACKEND_ZHA, Z2M_BULB_IEEE)] == collision.id

    async def test_stale_hash_and_hidden_member_removal_fail_before_publish(
        self, hass, radio_env, broker
    ):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        await self._z2m_group(hass, radio_env, token)
        broker.published.clear()
        stale = await _call_tool(
            "remove_zigbee_group",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "expected_hash": "0" * 64,
            },
            token,
            hass,
            _data(),
        )
        assert stale[1] == "invalid_request" and broker.published == []

        groups = json.loads(broker.retained["zigbee2mqtt/bridge/groups"])
        groups[0]["members"].append(
            {"ieee_address": Z2M_LOCK_IEEE, "endpoint": 1}
        )
        broker.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        hidden = await self._z2m_group(hass, radio_env, token)
        denied = await _call_tool(
            "remove_zigbee_group",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "expected_hash": hidden["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert denied[1] == "denied" and broker.published == []

    async def test_remove_never_forces_z2m(self, hass, radio_env, broker):
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        group = await self._z2m_group(hass, radio_env, token)
        broker.published.clear()
        result = await _call_tool(
            "remove_zigbee_group",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "expected_hash": group["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert result[1] == "allowed"
        topic, payload = broker.published[-1]
        assert topic.endswith("group/remove") and payload["force"] is False

    async def test_confirmation_redacts_radio_ids_and_revalidates_hash(
        self, hass, radio_env, broker
    ):
        token = _z2m_pair_token(
            cap_diagnostics="allow",
            cap_radio_write="confirm",
            cap_physical_control="allow",
        )
        group = await self._z2m_group(hass, radio_env, token)
        broker.published.clear()
        data, store = _appr_data()
        result = await _call_tool(
            "set_zigbee_group_members",
            {
                "group_entity_id": radio_env["z2m_group_eid"],
                "operation": "add",
                "members": [{"device_id": radio_env["z2m_lock"], "endpoint": 1}],
                "expected_hash": group["content_hash"],
            },
            token,
            hass,
            data,
        )
        assert result[1] == "pending_approval"
        assert broker.published == []
        approval = store._p[0]
        assert approval["tool_name"] == "set_zigbee_group_members"
        rendered = json.dumps(approval["diff"])
        assert Z2M_BULB_IEEE not in rendered
        assert Z2M_LOCK_IEEE not in rendered
        assert "zigbee2mqtt/" not in rendered
        assert f'"id": {Z2M_GROUP_ID}' not in rendered

        groups = json.loads(broker.retained["zigbee2mqtt/bridge/groups"])
        groups[0]["scenes"].append({"id": 9, "name": "Concurrent change"})
        broker.retained["zigbee2mqtt/bridge/groups"] = json.dumps(groups)
        executed = await async_execute_approved_tool(
            "set_zigbee_group_members", approval["args"], token, hass, data
        )
        assert executed[1] == "invalid_request"
        assert broker.published == []

    async def test_zha_group_lifecycle_uses_registry_ids(
        self, hass, radio_env, broker, monkeypatch
    ):
        hass.config.components.add("zha")
        entry = MockConfigEntry(domain="zha", entry_id="e_zha_groups")
        entry.add_to_hass(hass)
        group_entity = er.async_get(hass).async_get_or_create(
            "light",
            "zha",
            "light_zha_group_0x002a",
            config_entry=entry,
            device_id=radio_env["zha_bulb"],
            suggested_object_id="zha_test_group",
        )
        hass.states.async_set(group_entity.entity_id, "off", {})
        members = [
            {
                "endpoint_id": 1,
                "device": {
                    "ieee": ZHA_BULB_IEEE,
                    "device_reg_id": radio_env["zha_bulb"],
                },
                "entities": [{"entity_id": group_entity.entity_id}],
            }
        ]

        calls = []

        async def _ws(_hass, command, payload, **kwargs):
            calls.append((command, payload))
            if command == "zha/groups":
                return [{"group_id": 42, "name": "ZHA Test", "members": members}]
            if command == "zha/devices/groupable":
                return [
                    {"endpoint_id": 1, "device": {"ieee": ZHA_BULB_IEEE}},
                    {"endpoint_id": 1, "device": {"ieee": ZHA_TARGET_IEEE}},
                ]
            if command == "zha/group/add":
                return {
                    "group_id": 43,
                    "name": payload["group_name"],
                    "members": [
                        {
                            "endpoint_id": payload["members"][0]["endpoint_id"],
                            "device": {
                                "ieee": str(payload["members"][0]["ieee"]),
                                "device_reg_id": radio_env["zha_target"],
                            },
                            "entities": [],
                        }
                    ],
                }
            if command == "zha/group/members/add":
                members.append(
                    {
                        "endpoint_id": payload["members"][0]["endpoint_id"],
                        "device": {
                            "ieee": str(payload["members"][0]["ieee"]),
                            "device_reg_id": radio_env["zha_target"],
                        },
                        "entities": [{"entity_id": group_entity.entity_id}],
                    }
                )
                return {"group_id": 42, "name": "ZHA Test", "members": members}
            if command == "zha/group/remove":
                return []
            raise AssertionError(command)

        monkeypatch.setattr(radio, "async_ws_command", _ws)
        token = _light_token(
            cap_diagnostics="allow",
            cap_radio_write="allow",
            cap_physical_control="allow",
        )
        read = await _call_tool("get_zigbee_groups", {}, token, hass, _data())
        zha_group = next(
            group for group in _body(read)["groups"] if group["backend"] == "zha"
        )
        assert zha_group["members"] == [
            {"device_id": radio_env["zha_bulb"], "endpoint": 1}
        ]
        created = await _call_tool(
            "create_zigbee_group",
            {
                "name": "ZHA New Group",
                "members": [
                    {"device_id": radio_env["zha_target"], "endpoint": 1}
                ],
            },
            token,
            hass,
            _data(),
        )
        assert created[1] == "allowed"
        create_payload = next(
            payload for command, payload in calls if command == "zha/group/add"
        )
        assert "group_id" not in create_payload
        assert ZHA_TARGET_IEEE not in json.dumps(_body(created))
        changed = await _call_tool(
            "set_zigbee_group_members",
            {
                "group_entity_id": group_entity.entity_id,
                "operation": "add",
                "members": [{"device_id": radio_env["zha_target"], "endpoint": 1}],
                "expected_hash": zha_group["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert changed[1] == "allowed"
        assert ZHA_TARGET_IEEE not in json.dumps(_body(changed))
        removed = await _call_tool(
            "remove_zigbee_group",
            {
                "group_entity_id": group_entity.entity_id,
                "expected_hash": _body(changed)["content_hash"],
            },
            token,
            hass,
            _data(),
        )
        assert removed[1] == "allowed"
        remove_payload = next(
            payload for command, payload in calls if command == "zha/group/remove"
        )
        assert remove_payload == {"group_ids": [42]}


class TestRadioZ2mPlumbing:
    async def test_backend_refuses_removing_every_group_member(
        self, hass, broker
    ):
        group = radio.normalize_zigbee_groups(
            radio.BACKEND_Z2M,
            json.loads(broker.retained["zigbee2mqtt/bridge/groups"]),
        )[0]
        with pytest.raises(
            radio.ZigbeeGroupConfigurationError, match="remove the group instead"
        ):
            await radio.async_set_zigbee_group_members(
                hass,
                backend=radio.BACKEND_Z2M,
                group=group,
                operation="remove",
                members=((Z2M_BULB_IEEE, 1),),
            )

    async def test_group_member_batch_rolls_back_completed_endpoints(
        self, hass, broker
    ):
        group = radio.normalize_zigbee_groups(
            radio.BACKEND_Z2M,
            json.loads(broker.retained["zigbee2mqtt/bridge/groups"]),
        )[0]
        original = broker.responders["group/members/add"]
        calls = 0

        def _fail_second(req):
            nonlocal calls
            calls += 1
            if calls == 2:
                return {"status": "error", "error": "sleepy endpoint"}
            return original(req)

        broker.responders["group/members/add"] = _fail_second
        with pytest.raises(radio.RadioError, match="rolled back"):
            await radio.async_set_zigbee_group_members(
                hass,
                backend=radio.BACKEND_Z2M,
                group=group,
                operation="add",
                members=((Z2M_LOCK_IEEE, 1), ("0x00158d0000001234", 1)),
            )
        retained = json.loads(broker.retained["zigbee2mqtt/bridge/groups"])[0]
        assert retained["members"] == [
            {"ieee_address": Z2M_BULB_IEEE, "endpoint": 1}
        ]
        rollback = broker.published[-1][1]
        assert rollback["skip_disable_reporting"] is True

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
        assert "set_zigbee_device_property" in gate_map["unavailable"]
        assert "get_radio_network" in gate_map["usable"]

        dual_confirm = _light_token(
            cap_radio_write="allow", cap_physical_control="confirm"
        )
        assert "set_zigbee_device_property" in _tool_gate_map(
            dual_confirm, data, hass
        )["needs_approval"]
        dual_allow = _light_token(
            cap_radio_write="allow", cap_physical_control="allow"
        )
        assert "set_zigbee_device_property" in _tool_gate_map(
            dual_allow, data, hass
        )["usable"]

        deny_token = _light_token(cap_radio_write="deny", cap_diagnostics="deny")
        gate_map = _tool_gate_map(deny_token, data, hass)
        for name in (
            "permit_zigbee_join", "reconfigure_zigbee_device",
            "set_zigbee_device_options", "remove_zigbee_device",
            "set_zigbee_device_property",
            "get_radio_network", "get_radio_device",
        ):
            assert name in gate_map["unavailable"]
