"""Tests for get_helper_settings / set_helper_settings.

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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord

def options_schema(current: dict | None = None) -> vol.Schema:
    """The step schema, with suggested values seeded from the stored settings.

    That seeding is what HA's SchemaCommonFlowHandler does (suggested_values =
    self._options), and it is where the read gets each field's current value.
    """
    current = current or {}

    def _field(key, default=vol.UNDEFINED):
        described = {"suggested_value": current[key]} if key in current else None
        return (vol.Required(key, description=described) if default is vol.UNDEFINED
                else vol.Optional(key, default=default, description=described))

    return vol.Schema({
        _field("entity_id"): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")),
        _field("hysteresis", 0.0): selector.NumberSelector(
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
    name = {
        "get_config_entry_options": "get_helper_settings",
        "set_config_entry_options": "set_helper_settings",
    }.get(name, name)
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
    registry_entry = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "threshold",
        "helper1",
        config_entry=entry,
        suggested_object_id="kitchen_threshold",
    )
    hass.states.async_set(registry_entry.entity_id, "off", {})
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
        entry = hass.config_entries.async_get_entry(entry_id)
        return {"type": FlowResultType.FORM, "flow_id": "f1", "step_id": "init",
                "data_schema": options_schema(dict(entry.options))}

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
        assert "set_helper_settings" in _EXECUTOR_REGISTRY


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
        assert body["settings"] == {"entity_id": "sensor.kitchen", "hysteresis": 0.0}
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
        assert "name" in body["settings"]           # stored...
        assert "name" not in body["editable_settings"]  # ...but the flow does not offer it
        assert body["editable_settings"] == {"entity_id": "sensor.kitchen", "hysteresis": 0.0}

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
                {"entry_id": "helper1", "settings": {"entity_id": "sensor.other", "hysteresis": 1.0}},
                _token(), hass)
        assert outcome == "allowed"
        assert created["options"]["entity_id"] == "sensor.other"
        assert _json(content)["settings"]["entity_id"] == "sensor.other"

    async def test_deny_returns_forbidden_without_echoing_the_payload(self, hass, helper_entry, as_helper):
        """Rule 29(a): the cap check runs before anything reads the arguments."""
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "settings": {"entity_id": "sensor.nonsense"}},
            _token(cap_helper_write="deny"), hass)
        assert outcome == "denied"
        assert "nonsense" not in content["content"][0]["text"]

    async def test_an_out_of_scope_entity_is_refused(self, hass, helper_entry, as_helper):
        """A helper exposes AND can actuate what it points at, so pointing one at
        an entity outside the tree would be a scope escape in both directions."""
        hass.states.async_set("lock.secret", "locked", {})
        content, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "settings": {"entity_id": "lock.secret"}}, _token(), hass)
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
            {"entry_id": "helper1", "settings": {"entity_id": "binary_sensor.readable"}},
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
             "settings": {"whatever_this_helper_calls_it": ["lock.secret"]}}, _token(), hass)
        assert outcome == "denied"
        assert "lock.secret" in content["content"][0]["text"]

    async def test_a_doomed_write_never_becomes_an_approval(self, hass, helper_entry, as_helper):
        """Rule 29."""
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _, outcome, _ = await _call(
            "set_config_entry_options", {"entry_id": "helper1", "settings": {}},
            _token(cap_helper_write="confirm"), hass, data)
        assert outcome == "invalid_request"
        data.store.set_pending_approvals.assert_not_called()

    async def test_a_stale_hash_is_refused(self, hass, helper_entry, as_helper):
        _, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "helper1", "settings": {"entity_id": "sensor.other"},
             "expected_hash": "stale"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_a_multi_step_flow_is_refused_and_says_why(self, hass, helper_entry, as_helper):
        """One call cannot drive a flow that asks for another step, and a form
        coming back means nothing was applied."""
        init, configure, _ = _flow(hass, result={"type": FlowResultType.FORM, "step_id": "second"})
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "helper1", "settings": {"entity_id": "sensor.other"}}, _token(), hass)
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "more than one" in text and "Nothing was changed" in text

    async def test_flow_errors_are_relayed_not_swallowed(self, hass, helper_entry, as_helper):
        init, configure, _ = _flow(
            hass, result={"type": FlowResultType.FORM, "errors": {"base": "need_lower_upper"}})
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "helper1", "settings": {"entity_id": "sensor.other"}}, _token(), hass)
        assert outcome == "invalid_request"
        assert "need_lower_upper" in content["content"][0]["text"]

    async def test_an_unfinished_flow_is_always_aborted(self, hass, helper_entry, as_helper):
        init, configure, _ = _flow(hass, result={"type": FlowResultType.FORM, "step_id": "second"})
        with init, configure, patch.object(
            hass.config_entries.options, "async_abort") as abort:
            await _call("set_config_entry_options",
                        {"entry_id": "helper1", "settings": {"entity_id": "sensor.other"}},
                        _token(), hass)
        abort.assert_called_once_with("f1")

    async def test_the_executor_revalidates_at_apply_time(self, hass, helper_entry, as_helper):
        """The approve path re-runs the executor directly, and the entry, the
        token's tree and the entities named can all move while it waits."""
        from custom_components.phoenix_mcp.tools.helper import _execute_set_config_entry_options

        args = {"entry_id": "helper1", "settings": {"entity_id": "lock.secret"}}
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
                        {"entry_id": "helper1", "settings": {"entity_id": "sensor.other"}},
                        _token(), hass)
        assert captured["resource_type"] == "config_entry"
        assert captured["resource_id"] == "helper1"
        assert captured["before"]["entity_id"] == "sensor.kitchen"
        assert captured["after"]["entity_id"] == "sensor.other"


class TestReconfigureMechanism:
    """Some helpers keep their config in entry.data and expose no options flow,
    so HA reconfigures them through the CONFIG flow with source=reconfigure.
    The two mechanisms are not interchangeable and the differences are the whole
    hazard: different manager, different store, and a different success signal.
    """

    @pytest.fixture
    def data_entry(self, hass):
        """A helper whose settings live in data, with no options flow."""
        entry = MockConfigEntry(
            domain="time_off", entry_id="data1", title="Pantry Light",
            data={"entities": ["sensor.kitchen"]}, options={},
        )
        entry.add_to_hass(hass)
        object.__setattr__(entry, "_supports_options", False)
        object.__setattr__(entry, "_supports_reconfigure", True)
        registry_entry = er.async_get(hass).async_get_or_create(
            "sensor",
            "time_off",
            "data1",
            config_entry=entry,
            suggested_object_id="pantry_light",
        )
        hass.states.async_set(registry_entry.entity_id, "off", {})
        hass.states.async_set("sensor.kitchen", "20", {})
        hass.states.async_set("sensor.other", "5", {})
        return entry

    @pytest.fixture
    def as_data_helper(self, monkeypatch):
        async def _get_integration(hass, domain):
            return MagicMock(integration_type="helper")

        monkeypatch.setattr(
            "custom_components.phoenix_mcp.tools.helper.async_get_integration", _get_integration)

    def _reconfigure_flow(self, hass, *, result=None):
        applied = {}

        async def _init(handler, *, context=None, data=None):
            assert context["source"] == "reconfigure"
            # A real time_off helper: it STORES {"entities": [id]} and its form
            # field is "entity", singular. The names and shapes differ.
            entry = hass.config_entries.async_get_entry("data1")
            current = (entry.data.get("entities") or [None])[0]
            return {"type": FlowResultType.FORM, "flow_id": "rf1", "step_id": "reconfigure",
                    "data_schema": vol.Schema({
                        vol.Required("entity", description={"suggested_value": current}):
                            selector.EntitySelector(
                                selector.EntitySelectorConfig(domain="sensor")),
                    })}

        async def _configure(flow_id, user_input=None):
            if result is not None:
                return result
            applied["settings"] = user_input
            entry = hass.config_entries.async_get_entry("data1")
            # The flow transforms the form field into the stored shape.
            hass.config_entries.async_update_entry(
                entry, data={"entities": [user_input["entity"]]})
            # HA's async_update_reload_and_abort updates the entry and then
            # ABORTS the flow; there is no CREATE_ENTRY on this path.
            return {"type": FlowResultType.ABORT, "reason": "reconfigure_successful"}

        return (patch.object(hass.config_entries.flow, "async_init", _init),
                patch.object(hass.config_entries.flow, "async_configure", _configure),
                applied)

    async def test_the_read_reports_the_mechanism_and_reads_data(
        self, hass, data_entry, as_data_helper,
    ):
        init, configure, _ = self._reconfigure_flow(hass)
        with init, configure:
            content, outcome, _ = await _call(
                "get_config_entry_options", {"entry_id": "data1"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["mechanism"] == "reconfigure"
        # entry.data, not entry.options: an options flow would read the wrong store.
        assert body["settings"] == {"entities": ["sensor.kitchen"]}
        # LIVE-FOUND. Intersecting stored keys with field names was the first
        # attempt and returns {} here, reporting that nothing can be changed on a
        # helper that reconfigures fine: this one stores "entities" (a list) and
        # its form field is "entity" (one id). The form's shape is the caller's
        # business; the stored shape is the integration's.
        assert body["editable_settings"] == {"entity": "sensor.kitchen"}

    async def test_a_successful_reconfigure_is_not_read_as_a_failure(
        self, hass, data_entry, as_data_helper,
    ):
        """THE TRAP. An options flow signals success with CREATE_ENTRY; a
        reconfigure signals it with ABORT + reason reconfigure_successful, so a
        single "type != CREATE_ENTRY means nothing happened" test reports every
        successful reconfigure as a failure."""
        init, configure, applied = self._reconfigure_flow(hass)
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "data1", "settings": {"entity": "sensor.other"}},
                _token(), hass)
        assert outcome == "allowed"
        assert applied["settings"] == {"entity": "sensor.other"}
        assert _json(content)["settings"] == {"entities": ["sensor.other"]}

    async def test_an_abort_for_any_other_reason_is_still_a_refusal(
        self, hass, data_entry, as_data_helper,
    ):
        """Relaxing the success test for one abort reason must not relax it for
        every abort: already_configured means nothing was written."""
        init, configure, applied = self._reconfigure_flow(
            hass, result={"type": FlowResultType.ABORT, "reason": "already_configured"})
        with init, configure:
            content, outcome, _ = await _call(
                "set_config_entry_options",
                {"entry_id": "data1", "settings": {"entity": "sensor.other"}},
                _token(), hass)
        assert outcome == "invalid_request"
        assert "already_configured" in content["content"][0]["text"]
        assert applied == {}

    async def test_an_unfinished_reconfigure_aborts_on_the_right_manager(
        self, hass, data_entry, as_data_helper,
    ):
        """The two mechanisms have SEPARATE flow managers, so aborting on the
        options manager would silently leave a reconfigure flow open in the
        operator's UI."""
        init, configure, _ = self._reconfigure_flow(
            hass, result={"type": FlowResultType.FORM, "step_id": "second"})
        with init, configure, \
                patch.object(hass.config_entries.flow, "async_abort") as flow_abort, \
                patch.object(hass.config_entries.options, "async_abort") as options_abort:
            await _call("set_config_entry_options",
                        {"entry_id": "data1", "settings": {"entity": "sensor.other"}},
                        _token(), hass)
        flow_abort.assert_called_once_with("rf1")
        options_abort.assert_not_called()

    async def test_scope_is_enforced_on_the_reconfigure_path_too(
        self, hass, data_entry, as_data_helper,
    ):
        hass.states.async_set("lock.secret", "locked", {})
        _c, outcome, _ = await _call(
            "set_config_entry_options",
            {"entry_id": "data1", "settings": {"entity": "lock.secret"}}, _token(), hass)
        assert outcome == "denied"

    async def test_an_undescribable_schema_does_not_claim_there_are_no_fields(
        self, hass, data_entry, as_data_helper,
    ):
        """An empty field list is a lie a caller acts on: it reads as "accepts
        nothing", and the caller then sends nothing and clears every optional
        setting. Say the form could not be described instead."""
        async def _init(handler, *, context=None, data=None):
            return {"type": FlowResultType.FORM, "flow_id": "rf1", "step_id": "reconfigure",
                    # A shape voluptuous_serialize cannot convert.
                    "data_schema": vol.Schema({vol.Required("entities"): list})}

        with patch.object(hass.config_entries.flow, "async_init", _init), \
                patch.object(hass.config_entries.flow, "async_abort"):
            content, outcome, _ = await _call(
                "get_config_entry_options", {"entry_id": "data1"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert "schema" not in body
        assert "editable_settings" not in body
        assert "could not be described" in body["note"]
        assert body["settings"] == {"entities": ["sensor.kitchen"]}

    async def test_a_confirm_only_step_has_no_fields_rather_than_unknown_ones(
        self, hass, data_entry, as_data_helper,
    ):
        """A step with no schema genuinely accepts nothing, which is a different
        answer from a schema that could not be described, and the caller acts on
        them differently."""
        async def _init(handler, *, context=None, data=None):
            return {"type": FlowResultType.FORM, "flow_id": "rf1", "step_id": "confirm"}

        with patch.object(hass.config_entries.flow, "async_init", _init), \
                patch.object(hass.config_entries.flow, "async_abort"):
            content, _, _ = await _call(
                "get_config_entry_options", {"entry_id": "data1"}, _token(), hass)
        body = _json(content)
        assert body["schema"] == []
        assert body["editable_settings"] == {}
        assert "could not be described" not in body["note"]

    async def test_a_helper_with_neither_flow_says_so(self, hass, data_entry, as_data_helper):
        object.__setattr__(data_entry, "_supports_reconfigure", False)
        content, outcome, _ = await _call(
            "get_config_entry_options", {"entry_id": "data1"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["mechanism"] is None
        assert body["schema"] == []
        assert "no way to change its settings" in body["note"]
