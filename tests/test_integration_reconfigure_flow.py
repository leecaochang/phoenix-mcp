"""Official-flow driver tests for generic integration reconfiguration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, section

from custom_components.phoenix_mcp.tools import integration_reconfigure as subject


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.domain = "demo"
        self.modified_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
        self.state = ConfigEntryState.LOADED
        self._listeners = []

    def async_on_state_change(self, callback):
        self._listeners.append(callback)

        def remove():
            self._listeners.remove(callback)

        return remove

    def transition(self, state: ConfigEntryState) -> None:
        self.state = state
        for callback in list(self._listeners):
            callback()


class _FlowManager:
    def __init__(self, entry: _Entry, results, *, configure_hook=None) -> None:
        self.entry = entry
        self.results = list(results)
        self.configure_hook = configure_hook
        self.inputs = []
        self.aborted = []
        self.init_context = None

    async def async_init(self, domain, *, context):
        self.init_context = (domain, context)
        first = dict(self.results.pop(0))
        first.setdefault("flow_id", "flow-1")
        return first

    async def async_configure(self, flow_id, user_input):
        self.inputs.append((flow_id, user_input))
        if self.configure_hook is not None:
            self.configure_hook(len(self.inputs), user_input)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        result = dict(result)
        result.setdefault("flow_id", flow_id)
        return result

    def async_abort(self, flow_id):
        self.aborted.append(flow_id)


class _ConfigEntries:
    def __init__(self, entry: _Entry, manager: _FlowManager) -> None:
        self.entry = entry
        self.flow = manager

    def async_get_entry(self, entry_id):
        return self.entry if entry_id == self.entry.entry_id else None


def _hass(entry, results, *, configure_hook=None):
    manager = _FlowManager(entry, results, configure_hook=configure_hook)
    return SimpleNamespace(config_entries=_ConfigEntries(entry, manager)), manager


def _form(schema, *, errors=None, step_id="user"):
    return {
        "type": FlowResultType.FORM,
        "step_id": step_id,
        "data_schema": vol.Schema(schema),
        "errors": errors or {},
    }


def _success():
    return {
        "type": FlowResultType.ABORT,
        "reason": subject.RECONFIGURE_SUCCESS_REASON,
    }


@pytest.mark.asyncio
async def test_forms_reuse_repeated_fields_and_leave_static_defaults_to_ha():
    entry = _Entry()

    def commit(call, _payload):
        if call == 2:
            entry.modified_at += timedelta(seconds=1)
            entry.transition(ConfigEntryState.UNLOAD_IN_PROGRESS)
            entry.transition(ConfigEntryState.LOADED)

    hass, manager = _hass(
        entry,
        [
            _form({
                vol.Required("host"): str,
                vol.Required("port", default=8123): int,
                vol.Required(
                    "username", description={"suggested_value": "stored-user"}
                ): str,
            }),
            _form({vol.Required("host"): str, vol.Optional("name"): str}, step_id="again"),
            _success(),
        ],
        configure_hook=commit,
    )
    result = await subject.async_run_reconfigure_flow(
        hass, entry, {"host": "ha.local"}, []
    )
    assert result.status == subject.STATUS_VERIFIED
    assert manager.inputs == [
        ("flow-1", {"host": "ha.local", "username": "stored-user"}),
        ("flow-1", {"host": "ha.local"}),
    ]


@pytest.mark.asyncio
async def test_optional_null_is_omitted_only_without_a_schema_default():
    entry = _Entry()
    form = _form({
        vol.Optional("clearable"): str,
        vol.Optional("defaulted", default="keep"): str,
    })
    def commit(_call, _payload):
        entry.modified_at += timedelta(seconds=1)
        entry.transition(ConfigEntryState.LOADED)

    hass, manager = _hass(entry, [form, _success()], configure_hook=commit)
    result = await subject.async_run_reconfigure_flow(
        hass, entry, {"clearable": None, "defaulted": None}, []
    )

    assert result.status == subject.STATUS_VERIFIED
    assert manager.inputs == [("flow-1", {"defaulted": None})]
    assert manager.init_context[1] == {"source": "reconfigure", "entry_id": "entry-1"}
    assert not manager.aborted


@pytest.mark.asyncio
async def test_expandable_sections_use_flat_fields_and_hide_nested_fallbacks():
    entry = _Entry()

    def commit(_call, _payload):
        entry.modified_at += timedelta(seconds=1)
        entry.transition(ConfigEntryState.LOADED)

    form = _form({
        vol.Required(
            "host", description={"suggested_value": "stored-host"}
        ): str,
        vol.Required(
            "advanced_settings",
            description={
                "suggested_value": {"port": 9161, "community": "stored-community"}
            },
        ): section(
            vol.Schema({
                vol.Required("port", default=161): int,
                vol.Required("community", default="public"): str,
            }),
            {"collapsed": True},
        ),
    })
    hass, manager = _hass(entry, [form, _success()], configure_hook=commit)
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])

    assert result.status == subject.STATUS_VERIFIED
    assert manager.inputs == [("flow-1", {
        "host": "stored-host",
        "advanced_settings": {
            "port": 9161,
            "community": "stored-community",
        },
    })]
    public_schema = subject._public_schema(form)
    assert public_schema is not None
    rendered = repr(public_schema)
    assert "stored-host" not in rendered
    assert "stored-community" not in rendered
    assert "public" not in rendered
    assert "9161" not in rendered
    nested = public_schema[1]["schema"]
    assert nested[1]["name"] == "community"
    assert nested[1]["sensitive"] is True


@pytest.mark.asyncio
async def test_menu_choices_are_consumed_in_order():
    entry = _Entry()

    def commit(call, _payload):
        if call == 3:
            entry.modified_at += timedelta(seconds=1)
            entry.transition(ConfigEntryState.LOADED)

    menu = {"type": FlowResultType.MENU, "menu_options": ["local", "cloud"]}
    hass, manager = _hass(
        entry,
        [menu, menu, _form({}), _success()],
        configure_hook=commit,
    )
    result = await subject.async_run_reconfigure_flow(
        hass, entry, {}, ["local", "cloud"]
    )
    assert result.status == subject.STATUS_VERIFIED
    assert [payload for _, payload in manager.inputs] == [
        {"next_step_id": "local"},
        {"next_step_id": "cloud"},
        {},
    ]


@pytest.mark.asyncio
async def test_validation_errors_return_redacted_schema_and_abort():
    entry = _Entry()
    first = _form(
        {
            vol.Required(
                "password", description={"suggested_value": "stored-secret"}
            ): str,
            vol.Optional("host", default="stored-host"): str,
        }
    )
    rejected = _form(
        {vol.Required("password"): str}, errors={"password": "invalid_auth"}
    )
    hass, manager = _hass(entry, [first, rejected])
    result = await subject.async_run_reconfigure_flow(
        hass, entry, {"password": "new-secret"}, []
    )
    assert result.status == subject.STATUS_ABORTED
    assert result.details["validation_errors"] == {"password": "invalid_auth"}
    assert result.details["schema"][0]["sensitive"] is True
    rendered = repr(result.details)
    assert "stored-secret" not in rendered
    assert "stored-host" not in rendered
    assert manager.aborted == ["flow-1"]
    assert len(manager.inputs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported",
    [FlowResultType.EXTERNAL_STEP, FlowResultType.EXTERNAL_STEP_DONE,
     FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE],
)
async def test_unsupported_steps_are_exact_and_cleaned_up(unsupported):
    entry = _Entry()
    hass, manager = _hass(entry, [{"type": unsupported}])
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_ABORTED
    assert "Browser/OAuth" in result.reason
    assert result.details["step_type"] == unsupported.value
    assert manager.aborted == ["flow-1"]


@pytest.mark.asyncio
async def test_missing_and_invalid_menu_choices_abort_without_apply():
    entry = _Entry()
    menu = {"type": FlowResultType.MENU, "menu_options": ["local", "cloud"]}
    hass, manager = _hass(entry, [menu])
    missing = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert missing.status == subject.STATUS_ABORTED
    assert missing.details["menu_choice_index"] == 0
    assert manager.aborted == ["flow-1"]

    hass, manager = _hass(entry, [menu])
    invalid = await subject.async_run_reconfigure_flow(hass, entry, {}, ["other"])
    assert invalid.status == subject.STATUS_ABORTED
    assert invalid.details["menu_options"] == ["local", "cloud"]
    assert manager.aborted == ["flow-1"]


@pytest.mark.asyncio
async def test_success_with_unused_values_is_applied_but_incomplete(monkeypatch):
    monkeypatch.setattr(subject, "RELOAD_VERIFY_TIMEOUT", 0.001)
    entry = _Entry()

    def commit(_call, _payload):
        entry.modified_at += timedelta(seconds=1)

    hass, _manager = _hass(
        entry, [_form({vol.Required("host"): str}), _success()], configure_hook=commit
    )
    result = await subject.async_run_reconfigure_flow(
        hass, entry, {"host": "ha.local", "unused": 1}, ["unused-menu"]
    )
    assert result.status == subject.STATUS_INCOMPLETE
    assert result.applied is True
    assert result.details["unused_config_fields"] == ["unused"]
    assert result.details["unused_menu_choices"] == ["unused-menu"]


@pytest.mark.asyncio
async def test_success_without_attributable_reload_is_unverified(monkeypatch):
    monkeypatch.setattr(subject, "RELOAD_VERIFY_TIMEOUT", 0.001)
    entry = _Entry()
    hass, _manager = _hass(entry, [_form({}), _success()])
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_UNVERIFIED
    assert result.applied is True


@pytest.mark.asyncio
async def test_foreign_precommit_transition_is_not_attributed(monkeypatch):
    monkeypatch.setattr(subject, "RELOAD_VERIFY_TIMEOUT", 0.001)
    entry = _Entry()

    def foreign_then_commit(_call, _payload):
        entry.transition(ConfigEntryState.LOADED)  # still on the old boundary
        entry.modified_at += timedelta(seconds=1)

    hass, _manager = _hass(
        entry, [_form({}), _success()], configure_hook=foreign_then_commit
    )
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_UNVERIFIED
    assert result.details["reload_verified"] is False


@pytest.mark.asyncio
async def test_setup_failure_is_an_applied_but_unverified_result():
    entry = _Entry()

    def fail_setup(_call, _payload):
        entry.modified_at += timedelta(seconds=1)
        entry.transition(ConfigEntryState.SETUP_ERROR)

    hass, _manager = _hass(
        entry, [_form({}), _success()], configure_hook=fail_setup
    )
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_UNVERIFIED
    assert result.applied is True
    assert result.details["reload_verified"] is False


@pytest.mark.asyncio
async def test_abort_and_apply_failures_remain_non_applied():
    entry = _Entry()
    hass, manager = _hass(
        entry,
        [_form({}), {"type": FlowResultType.ABORT, "reason": "already_configured"}],
    )
    aborted = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert aborted.status == subject.STATUS_ABORTED
    assert aborted.applied is False
    assert manager.aborted == ["flow-1"]

    hass, manager = _hass(entry, [_form({}), RuntimeError("boom")])
    failed = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert failed.status == subject.STATUS_APPLY_FAILED
    assert failed.applied is False
    assert manager.aborted == ["flow-1"]


@pytest.mark.asyncio
async def test_exception_after_modified_at_is_applied_and_never_safe_to_retry():
    entry = _Entry()

    def mutate(_call, _payload):
        entry.modified_at += timedelta(seconds=1)

    hass, _manager = _hass(
        entry, [_form({}), RuntimeError("ambiguous")], configure_hook=mutate
    )
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_UNVERIFIED
    assert result.applied is True


@pytest.mark.asyncio
async def test_step_budget_aborts_the_correct_manager(monkeypatch):
    monkeypatch.setattr(subject, "FLOW_STEP_BUDGET", 3)
    entry = _Entry()
    forms = [_form({}, step_id=str(index)) for index in range(4)]
    hass, manager = _hass(entry, forms)
    result = await subject.async_run_reconfigure_flow(hass, entry, {}, [])
    assert result.status == subject.STATUS_ABORTED
    assert result.details["step_budget"] == 3
    assert len(manager.inputs) == 3
    assert manager.aborted == ["flow-1"]
