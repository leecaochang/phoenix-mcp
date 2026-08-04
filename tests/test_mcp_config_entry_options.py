"""Tests for get_config_entry_options / set_config_entry_options.

The tool that finishes a migration: when the entity a helper was built on is
removed, the helper keeps existing and quietly produces nothing, and nothing
else on the surface can repoint it.

The boundary being defended is HELPER vs INTEGRATION. A helper's settings are
entity references and numbers, every one of which is checked against the token's
tree; an integration's settings can carry a hostname and credentials, and
repointing one at a server the operator does not control is an exfiltration
route no scope check catches. So "is this a helper" is load-bearing, and it is
Home Assistant's OWN classification rather than a list maintained here.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord

OPTIONS_SCHEMA = vol.Schema({
    vol.Required("entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")),
    vol.Optional("hysteresis", default=0.0): selector.NumberSelector(
        selector.NumberSelectorConfig(mode="box")),
})


def _token(**caps) -> TokenRecord:
    base = {"cap_helper_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(), created_by="u",
        permissions=PermissionTree(domains={
            "sensor": PermissionNode(state="GREEN"),
            # Readable but not writable: the case that separates the WRITE bar
            # from a READ one.
            "binary_sensor": PermissionNode(state="YELLOW"),
        }), **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass, data=None):
    return await _call_tool(name, args, token, hass, data or MagicMock())


@pytest.fixture
def helper_entry(hass: HomeAssistant):
    """A helper config entry, with its integration classified as HA classifies one."""
    entry = MockConfigEntry(
        domain="threshold", entry_id="helper1", title="Kitchen threshold",
        options={"entity_id": "sensor.kitchen", "hysteresis": 0.0},
    )
    entry.add_to_hass(hass)
    # supports_options is computed from a REGISTERED config-flow handler, which a
    # MockConfigEntry has none of; the real threshold integration reports True.
    object.__setattr__(entry, "_supports_options", True)
    hass.states.async_set("sensor.kitchen", "20", {})
    hass.states.async_set("sensor.other", "5", {})
    return entry


@pytest.fixture
def as_helper(monkeypatch):
    """Classify threshold as a helper and hue as a hub, the way the manifests do."""
    async def _get_integration(hass, domain):
        return MagicMock(integration_type="helper" if domain == "threshold" else "hub")

    monkeypatch.setattr(
        "custom_components.phoenix_mcp.tools.helper.async_get_integration", _get_integration)


def _flow(hass, *, result=None):
    """Patch the options flow manager: init returns a form, configure creates."""
    created = {}

    async def _init(entry_id, context=None, data=None):
        return {"type": FlowResultType.FORM, "flow_id": "f1", "step_id": "init",
                "data_schema": OPTIONS_SCHEMA}

    async def _configure(flow_id, user_input=None):
        if result is not None:
            return result
        created["options"] = user_input
        entry = hass.config_entries.async_get_entry("helper1")
        hass.config_entries.async_update_entry(entry, options=user_input)
        return {"type": FlowResultType.CREATE_ENTRY, "data": user_input}

    return (patch.object(hass.config_entries.options, "async_init", _init),
            patch.object(hass.config_entries.options, "async_configure", _configure),
            created)


class TestRegistration:
    def test_executor_registered(self):
        assert "set_config_entry_options" in _EXECUTOR_REGISTRY


class TestRead:
    async def test_deny_without_cap(self, hass, helper_entry, as_helper):
        _, outcome, _ = await _call(
            "get_config_entry_options", {"entry_id": "helper1"},
            _token(cap_helper_write="deny"), hass)
        assert outcome == "denied"

    async def test_returns_options_and_the_schema_ha_itself_uses(self, hass, helper_entry, as_helper):
        init, configure, _ = _flow(hass)
        with init, configure:
            content, outcome, _ = await _call(
                "get_config_entry_options", {"entry_id": "helper1"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["options"] == {"entity_id": "sensor.kitchen", "hysteresis": 0.0}
        fields = {f["name"]: f for f in body["schema"]}
        assert fields["entity_id"]["required"] is True
        # The domains an entity field accepts come through, which is what lets an
        # agent pick a valid replacement without guessing.
        assert fields["entity_id"]["selector"]["entity"]["domain"] == ["sensor"]
        assert fields["hysteresis"]["default"] == 0.0

    async def test_editable_options_is_the_set_a_caller_sends_back(self, hass, helper_entry, as_helper):
        """LIVE-FOUND on a real third-party helper. attribute_as_sensor STORES
        entity_id and name while its options flow declares neither, so "send the
        options back with your change applied" fails validation on the extras,
        and omitting them is both correct and harmless because HA's schema flow
        merges over what is stored. Handing the caller the intersection removes
        the guess."""
        hass.config_entries.async_update_entry(
            helper_entry,
            options={"entity_id": "sensor.kitchen", "hysteresis": 0.0, "name": "Kitchen"},
        )
        init, configure, _ = _flow(hass)
        with init, configure:
            content, _, _ = await _call(
                "get_config_entry_options", {"entry_id": "helper1"}, _token(), hass)
        body = _json(content)
        assert "name" in body["options"]           # stored...
        assert "name" not in body["editable_options"]  # ...but the flow does not offer it
        assert set(body["editable_options"]) == {"entity_id", "hysteresis"}

    async def test_the_schema_flow_is_always_closed(self, hass, helper_entry, as_helper):
        """Reading the fields must not leave a half-finished dialog in the
        operator's UI: the flow lives in HA's manager, not in the request."""
        init, configure, _ = _flow(hass)
        with init, configure, patch.object(
            hass.config_entries.options, "async_abort") as abort:
            await _call("get_config_entry_options", {"entry_id": "helper1"}, _token(), hass)
        abort.assert_called_once_with("f1")

    async def test_an_integration_entry_is_refused(self, hass, as_helper):
        MockConfigEntry(domain="hue", entry_id="hub1", title="Hue").add_to_hass(hass)
        content, outcome, _ = await _call(
            "get_config_entry_options", {"entry_id": "hub1"}, _token(), hass)
        assert outcome == "not_found"
        assert "HELPER entries only" in content["content"][0]["text"]

    async def test_a_missing_entry_answers_identically_to_an_integration(self, hass, as_helper):
        MockConfigEntry(domain="hue", entry_id="hub1", title="Hue").add_to_hass(hass)
        hub, _o1, _ = await _call("get_config_entry_options", {"entry_id": "hub1"}, _token(), hass)
        gone, _o2, _ = await _call("get_config_entry_options", {"entry_id": "nope"}, _token(), hass)
        # Rule 12: whether an entry_id names a real integration is not disclosed.
        assert hub["content"][0]["text"] == gone["content"][0]["text"]


class TestWrite:
    async def test_repointing_a_helper_at_a_new_source(self, hass, helper_entry, as_helper):
        """The migration case: the original source is gone, point it elsewhere."""
        init, configure, created = _flow(hass)
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "helper1", "options": {"entity_id": "sensor.other", "hysteresis": 1.0}},
                _token(), hass)
        assert outcome == "allowed"
        assert created["options"]["entity_id"] == "sensor.other"
        assert _json(content)["options"]["entity_id"] == "sensor.other"

    async def test_deny_returns_forbidden_without_echoing_the_payload(self, hass, helper_entry, as_helper):
        """Rule 29(a): the cap check runs before anything reads the arguments."""
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "options": {"entity_id": "sensor.nonsense"}},
            _token(cap_helper_write="deny"), hass)
        assert outcome == "denied"
        assert "nonsense" not in content["content"][0]["text"]

    async def test_an_out_of_scope_entity_is_refused(self, hass, helper_entry, as_helper):
        """A helper exposes AND can actuate what it points at, so pointing one at
        an entity outside the tree would be a scope escape in both directions."""
        hass.states.async_set("lock.secret", "locked", {})
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "options": {"entity_id": "lock.secret"}}, _token(), hass)
        assert outcome == "denied"
        assert "lock.secret" in content["content"][0]["text"]

    async def test_read_access_alone_is_not_enough(self, hass, helper_entry, as_helper):
        """WRITE rather than READ, matching _unwritable_scene_members. A helper
        both exposes the entity it points at AND can actuate it (switch_as_x
        wraps a switch so the new entity turns the old one on), and nothing in
        the serialized schema says which a given field does."""
        hass.states.async_set("binary_sensor.readable", "off", {})
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "options": {"entity_id": "binary_sensor.readable"}},
            _token(), hass)
        assert outcome == "denied"
        assert "binary_sensor.readable" in content["content"][0]["text"]

    async def test_a_source_key_is_never_assumed_by_name(self, hass, helper_entry, as_helper):
        """Each helper names its source differently (entity_id, source,
        entity_ids), so the scope check matches the VALUE shape at any depth."""
        hass.states.async_set("lock.secret", "locked", {})
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1",
             "options": {"whatever_this_helper_calls_it": ["lock.secret"]}}, _token(), hass)
        assert outcome == "denied"
        assert "lock.secret" in content["content"][0]["text"]

    async def test_a_doomed_write_never_becomes_an_approval(self, hass, helper_entry, as_helper):
        """Rule 29."""
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _, outcome, _ = await _call(
            "set_config_entry_options", {"entry_id": "helper1", "options": {}},
            _token(cap_helper_write="confirm"), hass, data)
        assert outcome == "invalid_request"
        data.store.set_pending_approvals.assert_not_called()

    async def test_a_stale_hash_is_refused(self, hass, helper_entry, as_helper):
        _, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "options": {"entity_id": "sensor.other"},
             "expected_hash": "stale"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_a_multi_step_flow_is_refused_and_says_why(self, hass, helper_entry, as_helper):
        """One call cannot drive a flow that asks for another step, and a form
        coming back means nothing was applied."""
        init, configure, _ = _flow(hass, result={"type": FlowResultType.FORM, "step_id": "second"})
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "helper1", "options": {"entity_id": "sensor.other"}}, _token(), hass)
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "more than one" in text and "Nothing was changed" in text

    async def test_flow_errors_are_relayed_not_swallowed(self, hass, helper_entry, as_helper):
        init, configure, _ = _flow(
            hass, result={"type": FlowResultType.FORM, "errors": {"base": "need_lower_upper"}})
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "helper1", "options": {"entity_id": "sensor.other"}}, _token(), hass)
        assert outcome == "invalid_request"
        assert "need_lower_upper" in content["content"][0]["text"]

    async def test_an_unfinished_flow_is_always_aborted(self, hass, helper_entry, as_helper):
        init, configure, _ = _flow(hass, result={"type": FlowResultType.FORM, "step_id": "second"})
        with init, configure, patch.object(
            hass.config_entries.options, "async_abort") as abort:
            await _call("set_config_entry_options",
                        {"entry_id": "helper1", "options": {"entity_id": "sensor.other"}},
                        _token(), hass)
        abort.assert_called_once_with("f1")

    async def test_the_executor_revalidates_at_apply_time(self, hass, helper_entry, as_helper):
        """The approve path re-runs the executor directly, and the entry, the
        token's tree and the entities named can all move while it waits."""
        from custom_components.phoenix_mcp.tools.helper import _execute_set_config_entry_options

        args = {"entry_id": "helper1", "options": {"entity_id": "lock.secret"}}
        hass.states.async_set("lock.secret", "locked", {})
        init, configure, created = _flow(hass)
        with init, configure:
            _c, outcome, _ = await _execute_set_config_entry_options(
                args, _token(), hass, MagicMock())
        assert outcome == "denied"
        assert created == {}  # the flow was never driven

    async def test_the_change_is_recorded_as_a_restorable_version(
        self, hass, helper_entry, as_helper, monkeypatch,
    ):
        captured = {}

        async def _capture(data, token, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "custom_components.phoenix_mcp.tools.helper._record_version", _capture)
        init, configure, _ = _flow(hass)
        with init, configure:
            await _call("set_config_entry_options",
                        {"entry_id": "helper1", "options": {"entity_id": "sensor.other"}},
                        _token(), hass)
        assert captured["resource_type"] == "config_entry"
        assert captured["resource_id"] == "helper1"
        assert captured["before"]["entity_id"] == "sensor.kitchen"
        assert captured["after"]["entity_id"] == "sensor.other"
