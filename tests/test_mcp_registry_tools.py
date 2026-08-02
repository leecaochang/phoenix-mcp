"""Tests for the registry-read MCP tools (list_areas/floors/zones/devices, get_device).

These tools gate on cap_registry_read and are scoped to entities the token can
read: areas/devices with no accessible entities never appear, and get_device
returns an identical not_found for a missing device and an inaccessible one.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _token(
    cap_registry_read: str = "allow",
    cap_search: str = "allow",
    permissions: PermissionTree | None = None,
    **caps,
) -> TokenRecord:
    """Token that grants READ/WRITE on the light domain and the zone.home entity."""
    tree = permissions or PermissionTree(
        domains={"light": PermissionNode(state="GREEN")},
        entities={"zone.home": PermissionNode(state="YELLOW")},
    )
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="reg-token",
        token_hash="x",
        created_at=utcnow(),
        created_by="user1",
        cap_registry_read=cap_registry_read,
        cap_search=cap_search,
        permissions=tree,
        **caps,
    )


def _result_json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


@pytest.fixture
def reg_env(hass: HomeAssistant):
    """Floor + areas + devices + entities with mixed accessibility.

    Accessible (light domain + zone.home): light.kitchen (device Hub, area
    Kitchen), light.bedroom (area Bedroom), zone.home. Denied: sensor.garage
    (device Sensor Hub, area Garage) - so Garage and Sensor Hub are invisible.
    """
    entry = MockConfigEntry(domain="test_integration", entry_id="e1")
    entry.add_to_hass(hass)
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    floor_reg = fr.async_get(hass)

    ground = floor_reg.async_create("Ground", level=0)
    kitchen = area_reg.async_create("Kitchen")
    area_reg.async_update(kitchen.id, floor_id=ground.floor_id)
    bedroom = area_reg.async_create("Bedroom")
    area_reg.async_update(bedroom.id, floor_id=ground.floor_id)
    garage = area_reg.async_create("Garage")

    hub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test_integration", "hub")},
        name="Living Hub",
        manufacturer="Acme",
        model="H1",
    )
    dev_reg.async_update_device(hub.id, area_id=kitchen.id)
    sensor_hub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test_integration", "sensor_hub")},
        name="Sensor Hub",
    )
    dev_reg.async_update_device(sensor_hub.id, area_id=garage.id)

    light_kitchen = ent_reg.async_get_or_create(
        "light", "test_integration", "uid_lk",
        config_entry=entry, device_id=hub.id, suggested_object_id="kitchen",
    )
    hass.states.async_set(light_kitchen.entity_id, "on", {"friendly_name": "Kitchen Light"})

    light_bedroom = ent_reg.async_get_or_create(
        "light", "test_integration", "uid_lb",
        config_entry=entry, suggested_object_id="bedroom",
    )
    ent_reg.async_update_entity(light_bedroom.entity_id, area_id=bedroom.id)
    hass.states.async_set(light_bedroom.entity_id, "off", {})

    sensor_garage = ent_reg.async_get_or_create(
        "sensor", "test_integration", "uid_sg",
        config_entry=entry, device_id=sensor_hub.id, suggested_object_id="garage",
    )
    hass.states.async_set(sensor_garage.entity_id, "5", {})

    hass.states.async_set("zone.home", "zoning", {
        "friendly_name": "Home", "latitude": 1.0, "longitude": 2.0, "radius": 100,
    })

    # A lock in Kitchen, denied by the default token (no lock grant) so it does
    # not disturb the scoped-enumeration tests; used by find_available_actions.
    lock_front = ent_reg.async_get_or_create(
        "lock", "test_integration", "uid_lock",
        config_entry=entry, device_id=hub.id, suggested_object_id="front",
    )
    hass.states.async_set(lock_front.entity_id, "locked", {})

    hass.services.async_register("light", "turn_on", lambda call: None)
    hass.services.async_register("light", "turn_off", lambda call: None)
    hass.services.async_register("lock", "lock", lambda call: None)
    hass.services.async_register("lock", "unlock", lambda call: None)

    return {
        "ground": ground, "kitchen": kitchen, "bedroom": bedroom, "garage": garage,
        "hub": hub, "sensor_hub": sensor_hub,
        "light_kitchen": light_kitchen.entity_id,
        "lock_front": lock_front.entity_id,
    }


async def _call(name: str, args: dict, token: TokenRecord, hass: HomeAssistant) -> tuple[dict, str, str]:
    data = MagicMock()
    data.mesa = None  # MESA-off path; per-tool MESA behavior is tested separately
    return await _call_tool(name, args, token, hass, data)


class TestCapGate:
    @pytest.mark.parametrize("tool", ["list_areas", "list_floors", "list_zones", "list_devices", "get_device"])
    async def test_deny_without_cap(self, hass, reg_env, tool):
        token = _token(cap_registry_read="deny")
        content, outcome, _ = await _call(tool, {"device_id": reg_env["hub"].id}, token, hass)
        assert outcome == "denied"
        assert content.get("isError") is True


class TestListAreas:
    async def test_only_accessible_areas(self, hass, reg_env):
        content, outcome, _ = await _call("list_areas", {}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        names = {a["name"] for a in body["areas"]}
        assert names == {"Kitchen", "Bedroom"}  # Garage has no accessible entity
        kitchen = next(a for a in body["areas"] if a["name"] == "Kitchen")
        assert kitchen["accessible_entity_count"] == 1
        assert kitchen["floor_id"] == reg_env["ground"].floor_id


class TestListFloors:
    async def test_floor_rollup(self, hass, reg_env):
        content, outcome, _ = await _call("list_floors", {}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["count"] == 1
        floor = body["floors"][0]
        assert floor["name"] == "Ground"
        assert floor["accessible_area_count"] == 2
        assert floor["accessible_entity_count"] == 2


class TestListZones:
    async def test_accessible_zone(self, hass, reg_env):
        content, outcome, _ = await _call("list_zones", {}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert [z["entity_id"] for z in body["zones"]] == ["zone.home"]
        assert body["zones"][0]["radius"] == 100


class TestListDevices:
    async def test_only_accessible_devices(self, hass, reg_env):
        content, outcome, _ = await _call("list_devices", {}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        ids = {d["device_id"] for d in body["devices"]}
        assert ids == {reg_env["hub"].id}  # Sensor Hub has no accessible entity
        assert body["devices"][0]["manufacturer"] == "Acme"


class TestGetDevice:
    async def test_accessible_device(self, hass, reg_env):
        content, outcome, _ = await _call("get_device", {"device_id": reg_env["hub"].id}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["entities"] == [reg_env["light_kitchen"]]
        assert body["model"] == "H1"

    async def test_missing_device_not_found(self, hass, reg_env):
        content, outcome, _ = await _call("get_device", {"device_id": "does_not_exist"}, _token(), hass)
        assert outcome == "not_found"
        assert content.get("isError") is True

    async def test_inaccessible_device_identical_not_found(self, hass, reg_env):
        # Sensor Hub exists but has only a denied entity: must look identical to a
        # nonexistent device (no existence oracle).
        missing, missing_outcome, _ = await _call("get_device", {"device_id": "does_not_exist"}, _token(), hass)
        hidden, hidden_outcome, _ = await _call("get_device", {"device_id": reg_env["sensor_hub"].id}, _token(), hass)
        assert hidden_outcome == missing_outcome == "not_found"
        assert hidden == missing

    async def test_missing_arg(self, hass, reg_env):
        content, outcome, _ = await _call("get_device", {}, _token(), hass)
        assert outcome == "invalid_request"


class TestSearchEntities:
    async def test_deny_without_cap(self, hass, reg_env):
        content, outcome, _ = await _call("search_entities", {}, _token(cap_search="deny"), hass)
        assert outcome == "denied"
        assert content.get("isError") is True

    async def test_query_matches_name(self, hass, reg_env):
        content, outcome, _ = await _call("search_entities", {"query": "kitchen"}, _token(), hass)
        assert outcome == "allowed"
        ids = [e["entity_id"] for e in _result_json(content)["entities"]]
        assert ids == ["light.kitchen"]

    async def test_domain_filter(self, hass, reg_env):
        content, _, _ = await _call("search_entities", {"domain": "light"}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.kitchen", "light.bedroom"}

    async def test_comma_joined_domain_string_is_split(self, hass, reg_env):
        """A model that wants multiple domains sometimes joins them into one
        comma-separated string instead of sending an array. domain accepts
        oneOf[string, array], so a single comma-joined string must be split into
        multiple domains, not treated as one literal (nonexistent) domain name
        that matches nothing."""
        content, _, _ = await _call("search_entities", {"domain": "light,zone"}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.kitchen", "light.bedroom", "zone.home"}

    async def test_denied_domain_returns_empty(self, hass, reg_env):
        # sensor.garage is not granted, so a sensor search returns nothing.
        content, _, _ = await _call("search_entities", {"domain": "sensor"}, _token(), hass)
        assert _result_json(content)["entities"] == []

    async def test_state_and_area_filter(self, hass, reg_env):
        content, _, _ = await _call("search_entities", {"state": "on", "area": "Kitchen"}, _token(), hass)
        ids = [e["entity_id"] for e in _result_json(content)["entities"]]
        assert ids == ["light.kitchen"]

    # Each filter's SKIP branch (the `continue`) was uncovered: every existing
    # test asserted what a filter keeps, none asserted what it drops. A filter
    # that silently stopped excluding would have passed the whole suite.

    async def test_device_class_filter_excludes_non_matching(self, hass, reg_env):
        hass.states.async_set("light.kitchen", "on",
                              {"friendly_name": "Kitchen Light", "device_class": "outlet"})
        content, _, _ = await _call("search_entities", {"device_class": "outlet"}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        # bedroom has no device_class, so the filter must drop it.
        assert ids == {"light.kitchen"}

    async def test_unavailable_filter_excludes_healthy_entities(self, hass, reg_env):
        hass.states.async_set("light.bedroom", "unavailable", {})
        content, _, _ = await _call("search_entities", {"unavailable": True}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.bedroom"}, "an 'on' light must not survive the unavailable filter"

    async def test_area_filter_excludes_entities_with_no_area(self, hass, reg_env):
        """zone.home has no area at all, so an area filter must drop it."""
        content, _, _ = await _call("search_entities", {"area": "Kitchen"}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert "zone.home" not in ids
        assert ids == {"light.kitchen"}

    async def test_area_filter_excludes_a_different_area(self, hass, reg_env):
        """light.bedroom has an area, just the wrong one: the other skip branch."""
        content, _, _ = await _call("search_entities", {"area": "Bedroom"}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.bedroom"}

    async def test_empty_string_device_class_is_not_a_filter(self, hass, reg_env):
        """A schema-completing tool caller sends every declared optional field on
        every call, including unset ones as an explicit empty string rather than
        omitting them. device_class="" must behave identically to omitting it,
        not as a filter for device_class == "" (which no real entity has, so it
        silently zeroes every result)."""
        content, _, _ = await _call(
            "search_entities", {"domain": "light", "device_class": ""}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.kitchen", "light.bedroom"}

    async def test_empty_string_state_is_not_a_filter(self, hass, reg_env):
        content, _, _ = await _call(
            "search_entities", {"domain": "light", "state": ""}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.kitchen", "light.bedroom"}

    async def test_empty_string_domain_is_not_a_filter(self, hass, reg_env):
        content, _, _ = await _call("search_entities", {"domain": ""}, _token(), hass)
        ids = {e["entity_id"] for e in _result_json(content)["entities"]}
        assert ids == {"light.kitchen", "light.bedroom", "zone.home"}

    async def test_stale_hours_does_not_crash_on_real_state(self, hass, reg_env):
        """last_changed on a real HA state dict is an ISO string (State.as_dict()
        serializes it, never a datetime), so stale_hours filtering must parse it
        back before subtracting. Subtracting from the string raises TypeError and
        fails every call that passes this argument."""
        content, outcome, _ = await _call(
            "search_entities", {"query": "kitchen", "stale_hours": 999999}, _token(), hass)
        assert outcome == "allowed"
        # A state just set by the fixture is nowhere near 999999 hours stale.
        assert _result_json(content)["entities"] == []

    async def test_stale_hours_zero_includes_everything(self, hass, reg_env):
        content, outcome, _ = await _call(
            "search_entities", {"query": "kitchen", "stale_hours": 0}, _token(), hass)
        assert outcome == "allowed"
        ids = [e["entity_id"] for e in _result_json(content)["entities"]]
        assert ids == ["light.kitchen"]

    async def test_control_mode_annotated_when_non_default(self, hass, reg_env):
        data = MagicMock()
        data.mesa = MagicMock()
        data.store.get_settings.return_value = SimpleNamespace(mesa_mode="advisory")
        with patch("custom_components.phoenix_mcp.tools.discovery.entity_control_mode", return_value="read_only"):
            content, outcome, _ = await _call_tool(
                "search_entities", {"query": "kitchen"}, _token(), hass, data)
        assert outcome == "allowed"
        row = _result_json(content)["entities"][0]
        assert row["control_mode"] == "read_only"

    async def test_control_mode_omitted_when_autonomous(self, hass, reg_env):
        data = MagicMock()
        data.mesa = MagicMock()
        data.store.get_settings.return_value = SimpleNamespace(mesa_mode="advisory")
        with patch("custom_components.phoenix_mcp.tools.discovery.entity_control_mode", return_value="autonomous"):
            content, _, _ = await _call_tool(
                "search_entities", {"query": "kitchen"}, _token(), hass, data)
        assert "control_mode" not in _result_json(content)["entities"][0]

    async def test_entity_category_annotated(self, hass, reg_env):
        # A config/diagnostic entity in scope is tagged with its entity_category so
        # the agent can tell a setup/health entity from a primary control. Discovery
        # keeps it visible (unlike bulk service targeting) and just labels it. The
        # annotation is independent of MESA (data.mesa is None via _call here).
        ent_reg = er.async_get(hass)
        diag = ent_reg.async_get_or_create(
            "light", "test_integration", "uid_calib",
            suggested_object_id="calibration",
            entity_category=EntityCategory.CONFIG,
        )
        hass.states.async_set(diag.entity_id, "off", {"friendly_name": "Calibration"})
        content, outcome, _ = await _call("search_entities", {"query": "calibration"}, _token(), hass)
        assert outcome == "allowed"
        row = _result_json(content)["entities"][0]
        assert row["entity_id"] == diag.entity_id
        assert row["entity_category"] == "config"

    async def test_entity_category_omitted_for_primary(self, hass, reg_env):
        content, _, _ = await _call("search_entities", {"query": "kitchen"}, _token(), hass)
        assert "entity_category" not in _result_json(content)["entities"][0]

    async def test_typo_falls_back_to_fuzzy(self, hass, reg_env):
        # A misspelled query matches nothing exactly, so the fuzzy fallback runs
        # and surfaces the closest entity, flagged as approximate.
        content, outcome, _ = await _call("search_entities", {"query": "kitchne"}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["fuzzy_fallback"] is True
        assert [e["entity_id"] for e in body["entities"]] == ["light.kitchen"]

    async def test_exact_match_does_not_flag_fuzzy(self, hass, reg_env):
        # The common path (exact match found) is untouched: no fuzzy flag at all.
        content, _, _ = await _call("search_entities", {"query": "kitchen"}, _token(), hass)
        body = _result_json(content)
        assert "fuzzy_fallback" not in body
        assert [e["entity_id"] for e in body["entities"]] == ["light.kitchen"]

    async def test_far_off_query_still_empty(self, hass, reg_env):
        # A query nowhere near any entity stays empty even through the fallback.
        content, _, _ = await _call("search_entities", {"query": "xyzzyqwertz"}, _token(), hass)
        body = _result_json(content)
        assert body["entities"] == []
        assert "fuzzy_fallback" not in body


class TestGetOverview:
    async def test_deny_without_cap(self, hass, reg_env):
        _, outcome, _ = await _call("get_overview", {}, _token(cap_search="deny"), hass)
        assert outcome == "denied"

    async def test_counts(self, hass, reg_env):
        content, outcome, _ = await _call("get_overview", {}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["total_accessible_entities"] == 3  # 2 lights + zone.home
        assert body["by_domain"] == {"light": 2, "zone": 1}
        assert body["by_area"] == {"(no area)": 1, "Bedroom": 1, "Kitchen": 1}
        assert body["unavailable_count"] == 0
        assert "mesa_mode" not in body  # mesa disabled in this data mock

    async def test_mesa_mode_present_when_active(self, hass, reg_env):
        data = MagicMock()
        data.mesa = MagicMock()
        data.store.get_settings.return_value = SimpleNamespace(mesa_mode="enforced")
        content, _, _ = await _call_tool("get_overview", {}, _token(), hass, data)
        assert _result_json(content)["mesa_mode"] == "enforced"

    @staticmethod
    async def _data_with_authored(hass, mesa_mode):
        from custom_components.phoenix_mcp.mesa import async_setup_mesa
        from custom_components.phoenix_mcp.mesa_core import MetadataOrigin, SemanticProfile

        runtime = await async_setup_mesa(hass, "enforced")
        runtime.store.set("light.gov", SemanticProfile.from_dict(
            "light.gov",
            {"semantic_profile": {"operational_boundaries": {
                "control_mode": "read_only", "control_reason": "Observe only."}}},
            default_origin=MetadataOrigin.USER,
        ))
        hass.states.async_set("light.gov", "on", {})
        data = MagicMock()
        data.mesa = runtime
        data.store.get_settings.return_value = SimpleNamespace(mesa_mode=mesa_mode)
        return data

    async def test_mesa_restrictions_present_with_cap(self, hass, reg_env):
        data = await self._data_with_authored(hass, "enforced")
        content, _, _ = await _call_tool(
            "get_overview", {}, _token(cap_config_read="allow"), hass, data)
        body = _result_json(content)
        assert body["mesa_mode"] == "enforced"
        r = body["mesa_restrictions"]
        assert r["by_control_mode"] == {"read_only": 1}
        assert r["restricted_entities"][0]["entity_id"] == "light.gov"

    async def test_mesa_restrictions_omitted_without_cap(self, hass, reg_env):
        data = await self._data_with_authored(hass, "enforced")
        content, _, _ = await _call_tool("get_overview", {}, _token(), hass, data)
        body = _result_json(content)
        assert body["mesa_mode"] == "enforced"
        assert "mesa_restrictions" not in body  # cap_config_read defaults to deny

    async def test_mesa_restrictions_omitted_when_off(self, hass, reg_env):
        data = await self._data_with_authored(hass, "off")
        content, _, _ = await _call_tool(
            "get_overview", {}, _token(cap_config_read="allow"), hass, data)
        assert "mesa_restrictions" not in _result_json(content)


class TestDescribeArea:
    async def test_deny_without_cap(self, hass, reg_env):
        _, outcome, _ = await _call("describe_area", {"area": "Kitchen"}, _token(cap_search="deny"), hass)
        assert outcome == "denied"

    async def test_describe_by_name(self, hass, reg_env):
        content, outcome, _ = await _call("describe_area", {"area": "kitchen"}, _token(), hass)
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["name"] == "Kitchen"
        assert body["floor_name"] == "Ground"
        assert body["accessible_entity_count"] == 1
        assert body["entities_by_domain"]["light"][0]["entity_id"] == "light.kitchen"

    async def test_empty_area_identical_not_found(self, hass, reg_env):
        # Garage exists but all its entities are denied: identical to nonexistent.
        missing, missing_outcome, _ = await _call("describe_area", {"area": "Nowhere"}, _token(), hass)
        hidden, hidden_outcome, _ = await _call("describe_area", {"area": "Garage"}, _token(), hass)
        assert hidden_outcome == missing_outcome == "not_found"
        assert hidden == missing

    async def test_missing_arg(self, hass, reg_env):
        _, outcome, _ = await _call("describe_area", {}, _token(), hass)
        assert outcome == "invalid_request"


def _data_no_mesa():
    data = MagicMock()
    data.mesa = None
    return data


def _lock_token(cap_physical_control: str = "deny") -> TokenRecord:
    tree = PermissionTree(domains={"lock": PermissionNode(state="GREEN")})
    return _token(permissions=tree, cap_physical_control=cap_physical_control)


class TestFindAvailableActions:
    async def test_deny_without_cap(self, hass, reg_env):
        content, outcome, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["light_kitchen"]},
            _token(cap_search="deny"), hass, _data_no_mesa(),
        )
        assert outcome == "denied"

    async def test_inaccessible_identical_not_found(self, hass, reg_env):
        token = _token()
        missing, missing_o, _ = await _call_tool(
            "find_available_actions", {"entity_id": "light.ghost"}, token, hass, _data_no_mesa())
        # sensor.garage exists but is denied to this token.
        hidden, hidden_o, _ = await _call_tool(
            "find_available_actions", {"entity_id": "sensor.garage"}, token, hass, _data_no_mesa())
        assert hidden_o == missing_o == "not_found"
        assert hidden == missing

    async def test_writable_service_available(self, hass, reg_env):
        content, outcome, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["light_kitchen"]},
            _token(), hass, _data_no_mesa())
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["writable"] is True
        turn_on = next(a for a in body["actions"] if a["service"] == "light.turn_on")
        assert turn_on["available"] is True
        assert "mesa_control_mode" not in body  # mesa disabled in this data mock

    async def test_physical_gate_blocks_without_cap(self, hass, reg_env):
        content, outcome, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["lock_front"]},
            _lock_token(cap_physical_control="deny"), hass, _data_no_mesa())
        assert outcome == "allowed"
        lock_svc = next(a for a in _result_json(content)["actions"] if a["service"] == "lock.lock")
        assert lock_svc["available"] is False
        assert "physical control" in lock_svc["reason"]

    async def test_physical_gate_allows_with_cap(self, hass, reg_env):
        content, _, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["lock_front"]},
            _lock_token(cap_physical_control="allow"), hass, _data_no_mesa())
        lock_svc = next(a for a in _result_json(content)["actions"] if a["service"] == "lock.lock")
        assert lock_svc["available"] is True

    async def test_missing_arg(self, hass, reg_env):
        _, outcome, _ = await _call_tool(
            "find_available_actions", {}, _token(), hass, _data_no_mesa())
        assert outcome == "invalid_request"

    async def test_includes_semantic_moments_for_any_read_entity(self, hass, reg_env, monkeypatch):
        # Purpose-built triggers/conditions (with examples) surface here for any
        # READ-accessible entity, independent of MESA profiles - the authoring
        # discovery path. Moments are omitted (not errored) if the HA lookup fails.
        async def _fake_moments(_hass, entity_id):
            return [{"id": "light.turned_on", "kind": "trigger", "example": {"trigger": "light.turned_on"}}]

        monkeypatch.setattr(
            "custom_components.phoenix_mcp.tools.discovery.async_semantic_moments", _fake_moments
        )
        content, outcome, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["light_kitchen"]},
            _token(), hass, _data_no_mesa())
        assert outcome == "allowed"
        body = _result_json(content)
        assert body["semantic_moments"][0]["id"] == "light.turned_on"
        assert body["semantic_moments"][0]["example"] == {"trigger": "light.turned_on"}

    async def test_semantic_moments_omitted_when_lookup_returns_none(self, hass, reg_env, monkeypatch):
        async def _no_moments(_hass, entity_id):
            return None

        monkeypatch.setattr(
            "custom_components.phoenix_mcp.tools.discovery.async_semantic_moments", _no_moments
        )
        content, _, _ = await _call_tool(
            "find_available_actions", {"entity_id": reg_env["light_kitchen"]},
            _token(), hass, _data_no_mesa())
        assert "semantic_moments" not in _result_json(content)


def test_rank_search_matches_multiword_and_relevance():
    from custom_components.phoenix_mcp.tools.discovery import _rank_search_matches

    rows = [
        {"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"},
        {"entity_id": "light.kitchen_counter", "friendly_name": "Kitchen Counter"},
        {"entity_id": "sensor.outdoor_temp", "friendly_name": "Outdoor Temperature"},
    ]
    out = _rank_search_matches(rows, "kitchen light")
    ids = [r["entity_id"] for r in out]
    # token-AND: every word must be present, so the outdoor sensor is dropped.
    assert "sensor.outdoor_temp" not in ids
    assert set(ids) == {"light.kitchen", "light.kitchen_counter"}
    # the exact friendly-name match leads.
    assert ids[0] == "light.kitchen"


def test_rank_search_matches_empty_query_is_passthrough():
    from custom_components.phoenix_mcp.tools.discovery import _rank_search_matches

    rows = [{"entity_id": "light.a", "friendly_name": "A"}]
    assert _rank_search_matches(rows, "") == rows


def test_fuzzy_search_matches_ranks_by_similarity():
    from custom_components.phoenix_mcp.tools.discovery import _fuzzy_search_matches

    rows = [
        {"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"},
        {"entity_id": "sensor.outdoor_temp", "friendly_name": "Outdoor Temperature"},
    ]
    # "kitchne" is a transposition typo of kitchen; the outdoor sensor is far off.
    out = _fuzzy_search_matches(rows, "kitchne")
    assert [r["entity_id"] for r in out] == ["light.kitchen"]


def test_fuzzy_search_matches_empty_when_nothing_close():
    from custom_components.phoenix_mcp.tools.discovery import _fuzzy_search_matches

    rows = [{"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}]
    assert _fuzzy_search_matches(rows, "xyzzyqwertz") == []
