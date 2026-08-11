"""Guard: a Home Assistant target selector must never survive into a service call.

Phoenix MCP resolves and flattens every target itself and then calls the service
with an explicit entity list. Home Assistant, however, reads target selectors
back out of the call DATA (helpers.target.TargetSelection over ServiceCall.data)
and unions whatever it finds there with that list, so a selector left in
caller-supplied service data reaches entities the permission tree, the capability
gates and MESA never evaluated.

Two of the five selectors were resolved nowhere in Phoenix MCP and stripped
nowhere either: a request naming floor_id or label_id actuated everything under
that floor or label for a token scoped to a single entity. These tests pin the
strip at every surface that accepts caller-supplied service data, plus the
constant it reads, so a regression fails here rather than in the field.

The assertion is always about what reached hass.services.async_call, not about a
status code: the call is not refused for carrying a selector (the resolved
targets already say what it may reach), it is stripped, so a test asserting on
the response would pass with the selector still on the wire.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN, TARGET_SELECTOR_KEYS, TOKEN_PREFIX
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.helpers import sanitize_service_data
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.token_store import TokenRecord, TokenStore

# A body carrying every selector Home Assistant honours, alongside real service
# data. The real data must survive; every selector must not.
_ALL_SELECTORS = {
    "entity_id": "light.kitchen",
    "device_id": "dev-abc",
    "area_id": "kitchen",
    "floor_id": "ground_floor",
    "label_id": "everything",
}


def _make_token(**caps: str) -> tuple[TokenRecord, str]:
    import uuid

    from homeassistant.util.dt import utcnow

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return (
        TokenRecord(
            id=str(uuid.uuid4()),
            name="test-token",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=utcnow(),
            created_by="user1",
            **caps,
        ),
        raw,
    )


def _make_data(token: TokenRecord) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.get_token_by_hash.return_value = token
    store.get_settings.return_value = MagicMock(
        kill_switch=False,
        disable_all_logging=False,
        log_allowed=True,
        log_denied=True,
        log_rate_limited=True,
        log_entity_names=True,
        log_client_ip=True,
        notify_on_rate_limit=False,
        mesa_mode="off",
    )
    store.update_last_used = MagicMock()
    store.get_pending_approvals.return_value = []
    store.async_lock = asyncio.Lock()

    rate_limiter = MagicMock(spec=RateLimiter)
    rate_limiter.check.return_value = RateLimitResult(
        allowed=True, rate_limiting_enabled=False,
        limit=0, remaining=0, reset=0, retry_after=0,
    )
    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()
    return PhoenixData(store=store, rate_limiter=rate_limiter, audit=audit, rate_limit_notified={})


def _hass_with(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    hass.bus = MagicMock()
    hass.states = MagicMock()
    hass.states.async_all.return_value = []
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    return hass


def _call_data(hass: MagicMock) -> dict:
    """The service data Phoenix MCP actually handed Home Assistant."""
    assert hass.services.async_call.called, "the service was never called"
    return hass.services.async_call.call_args.args[2]


def _assert_no_selectors_but_entity_list(call_data: dict, expected: list[str]) -> None:
    """entity_id is the resolved list Phoenix MCP chose; nothing else selects."""
    assert call_data["entity_id"] == expected
    for key in TARGET_SELECTOR_KEYS - {"entity_id"}:
        assert key not in call_data, f"{key} reached Home Assistant and would widen the call"


# --- the constant ------------------------------------------------------------


def test_selector_keys_match_home_assistant_own_definition():
    """A Home Assistant release that adds a selector must fail here.

    TARGET_SELECTOR_KEYS is the strip list, so a sixth selector Phoenix MCP does
    not know about would silently widen every call. Pinning it against HA's own
    target-service field set turns that into a failing test on the upgrade that
    introduces it.
    """
    from homeassistant.helpers import config_validation as cv

    assert TARGET_SELECTOR_KEYS == {str(k) for k in cv.TARGET_SERVICE_FIELDS}


def test_target_selection_reads_exactly_those_keys():
    """The strip list is complete with respect to what HA actually honours.

    The field set above is a schema declaration; this reads the class that does
    the selecting at call time, so the two agreeing is what makes the strip
    sufficient rather than merely plausible.
    """
    from homeassistant.helpers.target import TargetSelection

    selected = {name.removesuffix("s") for name in TargetSelection.__slots__}
    assert selected == set(TARGET_SELECTOR_KEYS)


# --- the helper --------------------------------------------------------------


def test_sanitizer_strips_every_selector_and_keeps_real_data():
    out = sanitize_service_data({**_ALL_SELECTORS, "brightness_pct": 40})
    assert out == {"brightness_pct": 40}


def test_sanitizer_keeps_target_which_is_a_notify_data_field():
    """`target` is a target block only at HA's own API boundary.

    Reaching hass.services.async_call it selects nothing, and the notify domain
    uses it as a real field naming a notification recipient, so stripping it
    would break those calls while closing nothing.
    """
    out = sanitize_service_data({"message": "hi", "target": ["phone"]})
    assert out == {"message": "hi", "target": ["phone"]}


def test_sanitizer_returns_a_new_dict_and_never_mutates():
    """Every call site needs a snapshot: evaluation suspends at an await, and a
    caller still holding the original could swap the target under it."""
    original = {"entity_id": "light.a", "brightness_pct": 1}
    out = sanitize_service_data(original)
    assert original == {"entity_id": "light.a", "brightness_pct": 1}
    assert out is not original


def test_sanitizer_degrades_a_wrong_shaped_value_to_empty():
    for bad in (None, "entity_id=light.a", ["light.a"], 7, True):
        assert sanitize_service_data(bad) == {}


@pytest.mark.asyncio
async def test_mcp_call_service_strips_selectors(hass, token_store):
    """MCP applied no filtering at all: service_data was a free-form caller dict
    forwarded verbatim."""
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    token, _ = _make_token()
    data = _make_data(token)
    hass = _hass_with(data)

    args = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.kitchen",
        "service_data": {**_ALL_SELECTORS, "brightness_pct": 40},
    }
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_service_targets",
        return_value=(["light.kitchen"], 1),
    ):
        await _execute_call_service(args, token, hass, data, request_id="r1")

    call_data = _call_data(hass)
    _assert_no_selectors_but_entity_list(call_data, ["light.kitchen"])
    assert call_data["brightness_pct"] == 40


@pytest.mark.asyncio
async def test_approved_reexecution_strips_selectors(hass, token_store):
    """The executor is the single choke point, so an approval stored with a
    selector in its saved args cannot smuggle one past the gate on approval.

    A confirm-gated call persists the caller's arguments verbatim and re-runs
    them later, so sanitising only at the request boundary would leave the
    approve path as an unstripped second entrance.
    """
    from custom_components.phoenix_mcp.mcp_view import async_execute_approved_tool

    token, _ = _make_token()
    data = _make_data(token)
    hass = _hass_with(data)

    saved_args = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.kitchen",
        "service_data": dict(_ALL_SELECTORS),
    }
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_service_targets",
        return_value=(["light.kitchen"], 1),
    ):
        await async_execute_approved_tool("call_service", saved_args, token, hass, data)

    _assert_no_selectors_but_entity_list(_call_data(hass), ["light.kitchen"])


@pytest.mark.asyncio
async def test_dry_run_predicts_against_the_sanitised_call(hass, token_store):
    """The preview must evaluate the call that would run.

    dry_run_service exists to predict the real verdict, so it has to hand MESA
    the same service data the executor would, not the caller's original. Feeding
    it the unsanitised dict would predict a verdict for a call that cannot happen.
    """
    from custom_components.phoenix_mcp.tools import discovery

    token, _ = _make_token(cap_search="allow")
    data = _make_data(token)
    data.mesa = MagicMock()
    data.store.get_settings.return_value.mesa_mode = "enforced"
    hass = _hass_with(data)

    seen: dict = {}

    def _capture(*_args, **kwargs):
        seen.update(kwargs["service_data"])
        return MagicMock(allowed=["light.kitchen"], confirm=[], blocked=[], warnings=[])

    args = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.kitchen",
        "service_data": {**_ALL_SELECTORS, "brightness_pct": 40},
    }
    with (
        patch.object(discovery, "resolve_service_targets", return_value=(["light.kitchen"], 1)),
        patch.object(discovery, "evaluate_service_entities", side_effect=_capture),
    ):
        await discovery._tool_dry_run_service(args, token, hass, data)

    assert seen == {"brightness_pct": 40}


@pytest.mark.asyncio
async def test_mesa_evaluates_exactly_the_parameters_that_execute(hass, token_store):
    """MESA must judge the call that runs, not a filtered view of it.

    The safety property is an equality, not a subset: a decision made about a
    different payload than the one sent is precisely the divergence the target
    check exists to catch, and every audit record written afterwards would then
    describe a call that never happened. So the evaluated parameters must equal
    the executed ones, differing only in the entity: MESA decides per entity,
    while the call carries the whole resolved list.

    This holds today because target resolution CONSUMES the selectors, removing
    them from the outgoing call as well, so nothing has to be withheld to keep
    the two in step.

    The capture sits on the ENFORCER, not on evaluate_service_entities: patching
    the latter would replace the code that builds these parameters, so the
    assertion would hold no matter what that code did.
    """
    from custom_components.phoenix_mcp.mcp_view import _execute_call_service

    token, _ = _make_token()
    data = _make_data(token)
    data.store.get_settings.return_value.mesa_mode = "enforced"
    hass = _hass_with(data)

    evaluated: list[dict] = []

    def _capture(*, entity_id, service, service_params, **_kw):
        evaluated.append(dict(service_params))
        return MagicMock(allowed=True, warnings=[], rule_applied=None, reason=None)

    data.mesa = MagicMock()
    data.mesa.enforcer.evaluate = MagicMock(side_effect=_capture)

    args = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": ["light.kitchen", "light.hall"],
        "service_data": {**_ALL_SELECTORS, "brightness_pct": 40},
    }
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_service_targets",
        return_value=(["light.kitchen", "light.hall"], 2),
    ):
        await _execute_call_service(args, token, hass, data, request_id="r1")

    executed = _call_data(hass)
    assert [e["entity_id"] for e in evaluated] == ["light.kitchen", "light.hall"], (
        "the enforcer was not consulted per resolved entity"
    )
    for params in evaluated:
        assert {k: v for k, v in params.items() if k != "entity_id"} == {
            k: v for k, v in executed.items() if k != "entity_id"
        }


def test_contradictory_target_rule_name_matches_the_library():
    """The defect-signal branch keys on MESA's own rule name.

    Phoenix MCP logs this rule as a bug in itself rather than a policy outcome,
    so a renamed rule would silently turn that alarm off while the refusal it
    reports on kept happening.
    """
    import inspect

    from custom_components.phoenix_mcp.mesa import _RULE_CONTRADICTORY_TARGET
    from custom_components.phoenix_mcp.mesa_core import enforcer

    assert f'rule_applied="{_RULE_CONTRADICTORY_TARGET}"' in inspect.getsource(enforcer)


def test_native_clean_area_argument_is_not_a_target_selector():
    """HassVacuumCleanArea's room rides in service data and must stay spelled
    cleaning_area_id.

    Its `area` names the room to clean rather than scoping entity resolution, so
    it is genuine service data. Spelled area_id it would be a target selector,
    stripped by the sanitiser and silently dropped from every clean request.
    """
    from custom_components.phoenix_mcp.tools import native

    source = native._tool_hass_vacuum_clean_area.__code__.co_consts
    keys = {c for c in source if isinstance(c, str)}
    assert "cleaning_area_id" in keys
    assert not (keys & TARGET_SELECTOR_KEYS)
