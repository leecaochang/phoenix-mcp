"""Tests for the Energy dashboard tools: get_energy_config and edit_energy_config.

The Energy dashboard is the one place Home Assistant records that a statistic has
a ROLE (grid / solar / battery / gas / water / individual device), and it records
it in .storage/energy where no other Phoenix MCP read reaches. Removing an
integration therefore orphans an Energy source silently.

The READ tests pin what makes it safe rather than merely possible: it is scoped to
the token (energy/get_prefs runs as admin, so an unscoped passthrough would leak
every statistic on the instance), and a never-configured instance is an answer
rather than an error.

The WRITE tests pin the property the whole design rests on: energy/save_prefs
FULL-REPLACES each top-level key it receives, so `test_replace_statistic_repoints_
and_leaves_everything_else_alone` asserting `set(saved[0]) == {"device_consumption"}`
is not a detail, it is the guarantee. Sending a key the operation did not change
would put every entry under it at the mercy of whatever the tool reconstructed.
The other load-bearing pair is the addressing asymmetry: a statistic being WRITTEN
must resolve for the token, while one used to ADDRESS an existing entry must not
have to, because a dead entry is addressed by the very id that stopped resolving
and refusing it would make the broken entries the only ones unfixable.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import REDACTION_SENTINEL
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore
from custom_components.phoenix_mcp.ws_dispatch import ALLOWED_WS_COMMANDS, WsDispatchError

# Patched AT the call site in tools.energy, never at ws_dispatch where it is
# defined: the module imported the name at import time, so a mutation applied to
# the definition would not be seen here and the test would pass against the real
# dispatcher (or fail for the wrong reason).
_WS = "custom_components.phoenix_mcp.tools.energy.async_ws_command"


def _data() -> PhoenixData:
    return PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=VersionStore(),
    )


def _token(**caps) -> TokenRecord:
    caps.setdefault("cap_config_read", "allow")
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u",
        permissions=PermissionTree(domains={"sensor": PermissionNode(state="GREEN")}),
        **caps,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def _text(content: dict) -> str:
    return content["content"][0]["text"]


PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "flow_from": [{"stat_energy_from": "sensor.grid_import", "stat_cost": None}],
            "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
        },
        {"type": "solar", "stat_energy_from": "sensor.solar_production"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.washer_energy", "name": "Washer"},
    ],
}


async def _call(hass: HomeAssistant, token: TokenRecord) -> tuple[dict, str, str]:
    return await _call_tool("get_energy_config", {}, token, hass, _data(), "req-1", None)


async def test_returns_sources_devices_and_referenced_entities(hass: HomeAssistant) -> None:
    """The happy path returns the prefs verbatim plus a flat entity roll-up."""
    for entity_id in (
        "sensor.grid_import", "sensor.grid_export",
        "sensor.solar_production", "sensor.washer_energy",
    ):
        hass.states.async_set(entity_id, "1.0")

    with patch(_WS, AsyncMock(return_value=PREFS)):
        content, outcome, resource = await _call(hass, _token())

    assert outcome == "allowed"
    assert resource == "get_energy_config"
    body = _json(content)
    assert body["configured"] is True
    assert body["energy_sources"] == PREFS["energy_sources"]
    assert body["device_consumption"] == PREFS["device_consumption"]
    # Collected by walking the structure, so nested flow_from/flow_to entries are
    # found without this test naming the keys they live under.
    assert body["referenced_entities"] == [
        "sensor.grid_export", "sensor.grid_import",
        "sensor.solar_production", "sensor.washer_energy",
    ]


async def test_water_key_absent_upstream_is_still_emitted(hass: HomeAssistant) -> None:
    """device_consumption_water is NotRequired in HA; the response always has it."""
    with patch(_WS, AsyncMock(return_value=PREFS)):
        content, _, _ = await _call(hass, _token())

    assert _json(content)["device_consumption_water"] == []


@pytest.mark.parametrize(
    "message",
    [
        # A fresh instance: HA answers ERR_NOT_FOUND rather than empty lists.
        "not_found: No prefs",
        # The energy component is not set up, so ws_dispatch refuses before any
        # handler lookup. That HA has no Energy dashboard to depend on.
        "WebSocket command not available: energy/get_prefs",
    ],
)
async def test_no_energy_configuration_is_not_an_error(
    hass: HomeAssistant, message: str
) -> None:
    """Both "there is nothing configured" paths are an ANSWER, not a failed read.

    Surfacing either as a tool error would tell an agent the check did not run,
    when in fact it ran and found no dependency, which is exactly what a caller
    about to remove an integration needs to know.
    """
    with patch(_WS, AsyncMock(side_effect=WsDispatchError(message))):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    body = _json(content)
    assert body["configured"] is False
    assert body["energy_sources"] == []
    assert body["device_consumption"] == []
    assert body["device_consumption_water"] == []
    assert body["referenced_entities"] == []


async def test_out_of_scope_entities_are_redacted_and_not_rolled_up(hass: HomeAssistant) -> None:
    """The read runs as admin, so scoping is Phoenix's job, not HA's.

    The denied statistic must be redacted in the source entry AND absent from
    referenced_entities; naming it in the roll-up would hand back the very id the
    redaction removed one field earlier.
    """
    hass.states.async_set("sensor.solar_production", "1.0")
    hass.states.async_set("switch.secret_meter", "on")
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar_production"},
            # A switch: the token's tree grants sensor only, so this resolves to
            # NO_ACCESS. sensor.ghost_meter is absent from states and registry,
            # so it is NOT_FOUND. Both redact identically (rule 12).
            {"type": "battery", "stat_energy_from": "switch.secret_meter"},
        ],
        "device_consumption": [{"stat_consumption": "sensor.ghost_meter"}],
    }

    with patch(_WS, AsyncMock(return_value=prefs)):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    body = _json(content)
    assert body["energy_sources"][1]["stat_energy_from"] == REDACTION_SENTINEL
    assert body["device_consumption"][0]["stat_consumption"] == REDACTION_SENTINEL
    assert body["referenced_entities"] == ["sensor.solar_production"]


async def test_external_statistic_is_not_reported_as_an_entity(hass: HomeAssistant) -> None:
    """An external statistic id ("domain:name") is not an entity and never resolves.

    It must pass through untouched rather than being redacted as an unreachable
    entity, which would make every externally-fed Energy dashboard look denied.
    """
    prefs = {
        "energy_sources": [{"type": "gas", "stat_energy_from": "my_integration:gas_total"}],
        "device_consumption": [],
    }
    with patch(_WS, AsyncMock(return_value=prefs)):
        content, _, _ = await _call(hass, _token())

    body = _json(content)
    assert body["energy_sources"][0]["stat_energy_from"] == "my_integration:gas_total"
    assert body["referenced_entities"] == []


async def test_dispatch_failure_is_invalid_request_not_denied(hass: HomeAssistant) -> None:
    """A broken read is an internal failure, never a policy decision.

    Filing it as `denied` would corrupt the one audit signal an operator reads to
    see what the permission rules actually stopped (see the audit taxonomy rule).
    """
    with patch(_WS, AsyncMock(side_effect=WsDispatchError("timed out"))):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "invalid_request"
    assert content.get("isError") is True


async def test_unexpected_shape_degrades_instead_of_returning_a_bare_value(
    hass: HomeAssistant,
) -> None:
    """A non-mapping result must not reach the caller as the response body."""
    with patch(_WS, AsyncMock(return_value=["not", "a", "mapping"])):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    body = _json(content)
    assert body["configured"] is False
    assert body["energy_sources"] == []


async def test_denied_capability_is_forbidden_and_echoes_nothing(hass: HomeAssistant) -> None:
    """A denied token gets the uniform message and no detail about the instance."""
    called = AsyncMock(return_value=PREFS)
    with patch(_WS, called):
        content, outcome, _ = await _call(hass, _token(cap_config_read="deny"))

    assert outcome == "denied"
    assert _text(content) == "Forbidden."
    # The gate runs before the dispatch, so a denied read never touches HA.
    assert called.await_count == 0


def test_exactly_the_four_energy_commands_are_dispatchable() -> None:
    """energy/info and energy/fossil_energy_consumption stay off the allowlist.

    Neither earns its place: info lists the cost sensors HA auto-creates, which
    search_entities already finds, and fossil_energy_consumption is a statistics
    query get_statistics covers. Pinned because adding a neighbouring command is a
    one-line change nothing else would catch.
    """
    energy = {c for c in ALLOWED_WS_COMMANDS if c.startswith("energy/")}
    assert energy == {
        "energy/get_prefs", "energy/save_prefs", "energy/validate", "energy/solar_forecast",
    }


# ---------------------------------------------------------------------------
# edit_energy_config
# ---------------------------------------------------------------------------


def _writer(prefs: dict):
    """A patched async_ws_command that reads `prefs` and records what was saved.

    Returns (mock, saved) where saved collects each save_prefs payload. The reader
    hands back a DEEP COPY so a test can prove the tool built its result from its
    own mutation rather than from a shared object it happened to edit in place.
    """
    saved: list[dict] = []

    async def dispatch(hass, command, payload, **kw):
        if command == "energy/get_prefs":
            return json.loads(json.dumps(prefs))
        assert command == "energy/save_prefs", command
        saved.append(payload)
        return json.loads(json.dumps(prefs))

    return AsyncMock(side_effect=dispatch), saved


async def _edit(hass: HomeAssistant, token: TokenRecord, **args) -> tuple[dict, str, str]:
    return await _call_tool("edit_energy_config", args, token, hass, _data(), "req-1", None)


def _seed_states(hass: HomeAssistant, *entity_ids: str) -> None:
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "1.0")


LIVE = {
    "energy_sources": [
        {"type": "grid", "cost_adjustment_day": 0.0,
         "stat_energy_from": "sensor.grid_import", "stat_energy_to": None},
        {"type": "solar", "stat_energy_from": "sensor.solar_production"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.washer_energy", "name": "Washing Machine"},
        {"stat_consumption": "sensor.dead_meter", "name": "Treadmill"},
    ],
    "device_consumption_water": [],
}


async def test_replace_statistic_repoints_and_leaves_everything_else_alone(
    hass: HomeAssistant,
) -> None:
    """The migration case: one entry moves, the rest of the config is untouched."""
    _seed_states(hass, "sensor.washer_energy", "sensor.new_washer_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, resource = await _edit(
            hass, _token(cap_energy_write="allow"),
            op="replace_statistic",
            statistic="sensor.washer_energy",
            new_statistic="sensor.new_washer_energy",
        )

    assert outcome == "allowed"
    assert resource == "energy:preferences"
    # ONLY the key that changed is sent. energy_sources is absent, so HA's
    # full-replace semantics never touch it.
    assert len(saved) == 1
    assert set(saved[0]) == {"device_consumption"}
    assert saved[0]["device_consumption"] == [
        {"stat_consumption": "sensor.new_washer_energy", "name": "Washing Machine"},
        {"stat_consumption": "sensor.dead_meter", "name": "Treadmill"},
    ]


async def test_replace_statistic_addressed_by_device_name(hass: HomeAssistant) -> None:
    """A dead entry is addressable by NAME, which is the only handle it has left.

    Its statistic does not resolve, so the read showed it as the sentinel and the
    caller has no id to send. Without name addressing the broken entries this tool
    exists to fix would be the ones it could not touch.
    """
    _seed_states(hass, "sensor.treadmill_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"),
            op="replace_statistic", device_name="Treadmill",
            new_statistic="sensor.treadmill_energy",
        )

    assert outcome == "allowed"
    assert saved[0]["device_consumption"][1] == {
        "stat_consumption": "sensor.treadmill_energy", "name": "Treadmill",
    }


async def test_replace_statistic_hits_every_occurrence(hass: HomeAssistant) -> None:
    """A statistic used in a source AND a device entry moves in both places.

    Fixing only one would leave the other pointing at an integration that is about
    to be removed, which is the failure the whole migration is trying to avoid.
    """
    _seed_states(hass, "sensor.solar_production", "sensor.new_solar")
    prefs = {
        "energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar_production"}],
        "device_consumption": [{"stat_consumption": "sensor.solar_production", "name": "Array"}],
    }
    ws, saved = _writer(prefs)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic="sensor.solar_production", new_statistic="sensor.new_solar",
        )

    assert outcome == "allowed"
    assert set(saved[0]) == {"energy_sources", "device_consumption"}
    assert saved[0]["energy_sources"][0]["stat_energy_from"] == "sensor.new_solar"
    assert saved[0]["device_consumption"][0]["stat_consumption"] == "sensor.new_solar"


async def test_add_and_remove_device(hass: HomeAssistant) -> None:
    _seed_states(hass, "sensor.dryer_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"),
            op="add_device", statistic="sensor.dryer_energy", name="Dryer",
        )
    assert outcome == "allowed"
    assert saved[0]["device_consumption"][-1] == {"stat_consumption": "sensor.dryer_energy", "name": "Dryer"}

    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="remove_device", device_name="Treadmill",
        )
    assert outcome == "allowed"
    assert [e["name"] for e in saved[0]["device_consumption"]] == ["Washing Machine"]


async def test_add_device_refuses_a_duplicate(hass: HomeAssistant) -> None:
    _seed_states(hass, "sensor.washer_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="add_device", statistic="sensor.washer_energy",
        )
    assert outcome == "invalid_request"
    assert "already tracked" in _text(content)
    assert saved == []


async def test_set_source_updates_an_existing_source(hass: HomeAssistant) -> None:
    """Wiring grid export: the field is set, the import meter is not disturbed."""
    _seed_states(hass, "sensor.grid_export")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="grid", stat_energy_to="sensor.grid_export",
        )
    assert outcome == "allowed"
    grid = saved[0]["energy_sources"][0]
    assert grid["stat_energy_to"] == "sensor.grid_export"
    assert grid["stat_energy_from"] == "sensor.grid_import"
    assert set(saved[0]) == {"energy_sources"}


async def test_set_source_creates_a_missing_source(hass: HomeAssistant) -> None:
    _seed_states(hass, "sensor.batt_out", "sensor.batt_in")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="battery",
            stat_energy_from="sensor.batt_out", stat_energy_to="sensor.batt_in",
        )
    assert outcome == "allowed"
    assert saved[0]["energy_sources"][-1] == {
        "type": "battery", "stat_energy_from": "sensor.batt_out", "stat_energy_to": "sensor.batt_in",
    }


async def test_set_source_creation_refuses_without_the_required_fields(hass: HomeAssistant) -> None:
    """HA marks these vol.Required, so a partial create would fail at the schema.

    Refusing here names the missing field; letting it through would surface HA's
    message about a key the caller never saw.
    """
    _seed_states(hass, "sensor.batt_out")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="battery", stat_energy_from="sensor.batt_out",
        )
    assert outcome == "invalid_request"
    assert "stat_energy_to" in _text(content)
    assert saved == []


async def test_set_source_refuses_a_field_the_type_does_not_accept(hass: HomeAssistant) -> None:
    """Mirrored from HA's per-type schema: solar has no export meter."""
    _seed_states(hass, "sensor.x")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="solar", stat_energy_to="sensor.x",
        )
    assert outcome == "invalid_request"
    assert "does not take stat_energy_to" in _text(content)
    assert saved == []


async def test_written_statistic_must_resolve_but_addressed_one_need_not(
    hass: HomeAssistant,
) -> None:
    """The asymmetry that makes broken entries fixable.

    sensor.dead_meter resolves to NOT_FOUND, and it is the ADDRESS of the entry
    being repaired, so it must be accepted. A ghost as the NEW value would create
    a fresh dangling reference and is refused.
    """
    _seed_states(hass, "sensor.treadmill_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic="sensor.dead_meter", new_statistic="sensor.treadmill_energy",
        )
    assert outcome == "allowed"

    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic="sensor.washer_energy", new_statistic="sensor.never_existed",
        )
    assert outcome == "invalid_request"
    assert "not an entity this token can use" in _text(content)
    assert saved == []


async def test_external_statistic_is_accepted_as_a_written_value(hass: HomeAssistant) -> None:
    """An external statistic is not an entity and never resolves; refusing it would
    make every externally-fed Energy dashboard unmanageable."""
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic="sensor.washer_energy", new_statistic="my_integration:washer",
        )
    assert outcome == "allowed"
    assert saved[0]["device_consumption"][0]["stat_consumption"] == "my_integration:washer"


async def test_sentinel_in_an_argument_is_refused(hass: HomeAssistant) -> None:
    """Rule 32: the placeholder names nothing, so writing it back is never right."""
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic=REDACTION_SENTINEL, new_statistic="sensor.washer_energy",
        )
    assert outcome == "invalid_request"
    assert "device_name" in _text(content)
    assert saved == []


async def test_a_no_op_change_is_refused_rather_than_written(hass: HomeAssistant) -> None:
    _seed_states(hass, "sensor.washer_energy")
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="replace_statistic",
            statistic="sensor.washer_energy", new_statistic="sensor.washer_energy",
        )
    assert outcome == "invalid_request"
    assert saved == []


async def test_unknown_address_is_refused(hass: HomeAssistant) -> None:
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="remove_device", device_name="Nothing Like This",
        )
    assert outcome == "invalid_request"
    assert "get_energy_config" in _text(content)
    assert saved == []


async def test_denied_capability_forbids_and_never_dispatches(hass: HomeAssistant) -> None:
    """Rule 29(a): a denied token learns nothing about its own payload's validity."""
    ws, saved = _writer(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(), op="replace_statistic",
            statistic="sensor.washer_energy", new_statistic="sensor.anything",
        )
    assert outcome == "denied"
    assert ws.await_count == 0
    assert saved == []


def test_structural_verification_refuses_an_over_broad_change() -> None:
    """The backstop for HA's full-replace write.

    Exercised directly because no current op can produce this: it is what would
    catch a future _apply_op bug BEFORE the operator's other entries are gone,
    rather than after.
    """
    from custom_components.phoenix_mcp.tools.energy import _OpError, _verify_structure

    before = {"energy_sources": [{}, {}], "device_consumption": [{}, {}], "device_consumption_water": []}
    lost_a_device = {"energy_sources": [{}, {}], "device_consumption": [{}], "device_consumption_water": []}
    with pytest.raises(_OpError, match="beyond what"):
        _verify_structure(before, lost_a_device, "replace_statistic")
    # The op that is ALLOWED to lose one passes the same input.
    _verify_structure(before, lost_a_device, "remove_device")


# ---------------------------------------------------------------------------
# Health check, the two later ops, tariff fields, and the forecast read
# ---------------------------------------------------------------------------


def _bus(prefs: dict, *, validate: dict | None = None, forecast: dict | None = None):
    """A patched async_ws_command answering every energy command.

    Returns (mock, saved). Unlike _writer this also serves energy/validate and
    energy/solar_forecast, so a read that folds in the health check can be
    exercised end to end.
    """
    saved: list[dict] = []

    async def dispatch(hass, command, payload, **kw):
        if command == "energy/get_prefs":
            return json.loads(json.dumps(prefs))
        if command == "energy/validate":
            if validate is None:
                raise WsDispatchError("not available")
            return validate
        if command == "energy/solar_forecast":
            return {} if forecast is None else forecast
        assert command == "energy/save_prefs", command
        saved.append(payload)
        return json.loads(json.dumps(prefs))

    return AsyncMock(side_effect=dispatch), saved


async def test_health_check_labels_each_issue_with_the_entry_it_is_about(
    hass: HomeAssistant,
) -> None:
    """HA reports issues as arrays positionally parallel to the prefs lists.

    Unreadable on its own, so each is paired back with the thing the operator
    recognises: a device's own label, a source's type.
    """
    hass.states.async_set("sensor.grid_import", "1.0")
    report = {
        "energy_sources": [[{"type": "statistics_not_defined",
                             "affected_entities": [["sensor.grid_import", None]]}], []],
        "device_consumption": [
            [],
            [{"type": "entity_not_defined", "affected_entities": [["sensor.dead_meter", None]]}],
        ],
        "device_consumption_water": [],
    }
    ws, _ = _bus(LIVE, validate=report)
    with patch(_WS, ws):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    issues = _json(content)["issues"]
    assert [(i["where"], i["label"], i["type"]) for i in issues] == [
        ("energy_sources", "grid", "statistics_not_defined"),
        ("device_consumption", "Treadmill", "entity_not_defined"),
    ]


async def test_health_check_normalises_sets_and_tuples_before_redacting(
    hass: HomeAssistant,
) -> None:
    """HA's affected_entities is a SET of TUPLES, and both shapes fail silently.

    json.dumps cannot serialize a set at all, and filter_service_response walks
    dicts, lists and strings only, so a tuple falls straight through its recursion
    and would hand back an entity id the token cannot resolve. Normalising first is
    what puts those ids somewhere the redactor actually looks.
    """
    hass.states.async_set("switch.secret_meter", "on")
    report = {
        "energy_sources": [
            [{"type": "entity_not_defined",
              "affected_entities": {("switch.secret_meter", None)}}],
            [],
        ],
        "device_consumption": [],
        "device_consumption_water": [],
    }
    ws, _ = _bus(LIVE, validate=report)
    with patch(_WS, ws):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    affected = _json(content)["issues"][0]["affected_entities"]
    # A list of lists, not a stringified set, and the out-of-scope id is gone.
    assert affected == [[REDACTION_SENTINEL, None]]


async def test_health_check_reports_null_when_it_could_not_run(hass: HomeAssistant) -> None:
    """"Nothing is wrong" and "nobody checked" must not look the same.

    A caller deciding whether it is safe to delete something reads this field; an
    empty list would say the check passed when it never ran.
    """
    ws, _ = _bus(LIVE, validate=None)  # energy/validate raises
    with patch(_WS, ws):
        content, outcome, _ = await _call(hass, _token())

    assert outcome == "allowed"
    assert _json(content)["issues"] is None


async def test_rename_device(hass: HomeAssistant) -> None:
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"),
            op="rename_device", device_name="Washing Machine", name="Washer",
        )
    assert outcome == "allowed"
    assert saved[0]["device_consumption"][0] == {
        "stat_consumption": "sensor.washer_energy", "name": "Washer",
    }
    # The statistic is untouched: this renames a label, nothing else.
    assert set(saved[0]) == {"device_consumption"}


async def test_rename_device_to_the_same_name_is_refused(hass: HomeAssistant) -> None:
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"),
            op="rename_device", device_name="Treadmill", name="Treadmill",
        )
    assert outcome == "invalid_request"
    assert saved == []


async def test_remove_source(hass: HomeAssistant) -> None:
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="remove_source", source_type="solar",
        )
    assert outcome == "allowed"
    assert [s["type"] for s in saved[0]["energy_sources"]] == ["grid"]
    assert set(saved[0]) == {"energy_sources"}


async def test_remove_source_refuses_when_the_type_is_ambiguous(hass: HomeAssistant) -> None:
    """HA allows several sources of a type (multiple grid connections, batteries).

    Removing "the first one" would be a coin flip on which of the operator's meters
    stops being counted, so this refuses rather than guessing.
    """
    prefs = {
        "energy_sources": [
            {"type": "grid", "cost_adjustment_day": 0.0, "stat_energy_from": "sensor.grid_a"},
            {"type": "grid", "cost_adjustment_day": 0.0, "stat_energy_from": "sensor.grid_b"},
        ],
        "device_consumption": [],
    }
    ws, saved = _bus(prefs)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="remove_source", source_type="grid",
        )
    assert outcome == "invalid_request"
    assert "ambiguous" in _text(content)
    assert saved == []


async def test_remove_source_that_does_not_exist_is_refused(hass: HomeAssistant) -> None:
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="remove_source", source_type="water",
        )
    assert outcome == "invalid_request"
    assert "no water source" in _text(content)
    assert saved == []


async def test_tariff_from_sensor_fields(hass: HomeAssistant) -> None:
    """A time-of-use tariff names a sensor instead of a fixed number."""
    hass.states.async_set("sensor.tariff_now", "5.1")
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="grid",
            entity_energy_price="sensor.tariff_now",
        )
    assert outcome == "allowed"
    assert saved[0]["energy_sources"][0]["entity_energy_price"] == "sensor.tariff_now"


async def test_an_explicit_null_clears_a_field(hass: HomeAssistant) -> None:
    """Clearing a price is a real edit.

    Skipping a null would silently turn "unset this" into a no-op the caller has no
    way to notice, which is worse than refusing it.
    """
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="grid", number_energy_price=None,
        )
    assert outcome == "allowed"
    assert saved[0]["energy_sources"][0]["number_energy_price"] is None


async def test_cost_statistic_is_scope_checked_like_any_other_written_statistic(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.not_mine", "on")
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="grid", stat_cost="switch.not_mine",
        )
    assert outcome == "invalid_request"
    assert "stat_cost" in _text(content)
    assert saved == []


async def test_price_fields_are_refused_on_a_type_that_has_none(hass: HomeAssistant) -> None:
    """Solar and battery carry no cost fields in HA's schema."""
    hass.states.async_set("sensor.tariff_now", "5.1")
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="battery", entity_energy_price="sensor.tariff_now",
        )
    assert outcome == "invalid_request"
    assert "does not take entity_energy_price" in _text(content)
    assert saved == []


async def test_solar_forecast_read(hass: HomeAssistant) -> None:
    ws, _ = _bus(LIVE, forecast={"entry_abc": {"wh_hours": {"2026-08-03T12:00:00+00:00": 400}}})
    with patch(_WS, ws):
        content, outcome, resource = await _call_tool(
            "get_solar_forecast", {}, _token(), hass, _data(), "req-1", None,
        )
    assert outcome == "allowed"
    assert resource == "get_solar_forecast"
    body = _json(content)
    assert body["configured"] is True
    assert body["forecasts"]["entry_abc"]["wh_hours"] == {"2026-08-03T12:00:00+00:00": 400}


async def test_solar_forecast_without_a_forecast_integration_is_empty_not_an_error(
    hass: HomeAssistant,
) -> None:
    """No solar source names a forecast entry, so HA answers {}. That is an answer."""
    ws, _ = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _call_tool(
            "get_solar_forecast", {}, _token(), hass, _data(), "req-1", None,
        )
    assert outcome == "allowed"
    body = _json(content)
    assert body["configured"] is False
    assert body["forecasts"] == {}


async def test_solar_forecast_denied_capability(hass: HomeAssistant) -> None:
    ws, _ = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _call_tool(
            "get_solar_forecast", {}, _token(cap_config_read="deny"), hass, _data(), "req-1", None,
        )
    assert outcome == "denied"
    assert _text(content) == "Forbidden."


# ---------------------------------------------------------------------------
# HA's cross-field source rules, mirrored so a doomed write never reaches an admin
# ---------------------------------------------------------------------------


async def test_both_price_shapes_at_once_is_refused_before_the_gate(
    hass: HomeAssistant,
) -> None:
    """LIVE-FOUND 2026-08-03: this became a PendingApproval and failed at save.

    Per-field validation cannot see it, so the call passed every check, waited for
    an admin, and only then hit HA's _grid_ensure_single_price_export. Rule 29 is
    about not spending a human's attention on a write that was never going to land.
    """
    hass.states.async_set("sensor.tariff_now", "5.1")
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="grid",
            number_energy_price_export=1.5, entity_energy_price_export="sensor.tariff_now",
        )
    assert outcome == "invalid_request"
    assert "either an entity or a fixed number for export price" in _text(content)
    assert saved == []


async def test_a_price_added_to_an_existing_number_price_is_refused(
    hass: HomeAssistant,
) -> None:
    """The rule reads the MERGED source, not the caller's fields alone.

    LIVE has number_energy_price 4.2 already, so supplying only the entity price
    still produces a source carrying both. Checking the caller's arguments in
    isolation would wave this through.
    """
    hass.states.async_set("sensor.tariff_now", "5.1")
    prefs = json.loads(json.dumps(LIVE))
    prefs["energy_sources"][0]["number_energy_price"] = 4.2
    ws, saved = _bus(prefs)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="grid",
            entity_energy_price="sensor.tariff_now",
        )
    assert outcome == "invalid_request"
    assert "import price" in _text(content)
    assert saved == []


async def test_clearing_the_other_price_first_makes_the_write_land(
    hass: HomeAssistant,
) -> None:
    """The refusal above names the fix, and the fix works in one call."""
    hass.states.async_set("sensor.tariff_now", "5.1")
    prefs = json.loads(json.dumps(LIVE))
    prefs["energy_sources"][0]["number_energy_price"] = 4.2
    ws, saved = _bus(prefs)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="grid",
            entity_energy_price="sensor.tariff_now", number_energy_price=None,
        )
    assert outcome == "allowed"
    grid = saved[0]["energy_sources"][0]
    assert grid["entity_energy_price"] == "sensor.tariff_now"
    assert grid["number_energy_price"] is None


async def test_price_on_an_external_statistic_is_refused(hass: HomeAssistant) -> None:
    """HA cannot derive cost for an external statistic; it wants a cost stat."""
    prefs = {
        "energy_sources": [{"type": "gas", "stat_energy_from": "my_integration:gas_total"}],
        "device_consumption": [],
    }
    ws, saved = _bus(prefs)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="gas", number_energy_price=2.0,
        )
    assert outcome == "invalid_request"
    assert "external statistic" in _text(content)
    assert saved == []


async def test_a_cost_stat_makes_the_external_statistic_rule_stand_down(
    hass: HomeAssistant,
) -> None:
    """HA skips that rule when a cost statistic is already set, and so does this.

    Mirroring only the refusal and not its exemption would forbid a configuration
    HA accepts, which is worse than not mirroring it at all.
    """
    prefs = {
        "energy_sources": [{
            "type": "gas",
            "stat_energy_from": "my_integration:gas_total",
            "stat_cost": "my_integration:gas_cost",
        }],
        "device_consumption": [],
    }
    ws, saved = _bus(prefs)
    with patch(_WS, ws):
        _, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source",
            source_type="gas", number_energy_price=2.0,
        )
    assert outcome == "allowed"
    assert saved[0]["energy_sources"][0]["number_energy_price"] == 2.0


async def test_clearing_every_grid_meter_is_refused(hass: HomeAssistant) -> None:
    """A grid source with no meter at all is rejected by HA and here."""
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        content, outcome, _ = await _edit(
            hass, _token(cap_energy_write="allow"), op="set_source", source_type="grid",
            stat_energy_from=None, stat_energy_to=None,
        )
    assert outcome == "invalid_request"
    assert "at least one" in _text(content)
    assert saved == []


async def test_every_write_records_a_restorable_version(hass: HomeAssistant) -> None:
    """A version record is what makes an Energy write undoable.

    Never asserted until 2026-08-03, when the Changes tab appeared to be missing
    these rows. The cause turned out to be a missing label-map entry rather than a
    missing record, but the record itself had no test either way, and it is the
    only rollback path for the one destructive API in this module.
    """
    _seed_states(hass, "sensor.dryer_energy")
    data = _data()
    ws, saved = _bus(LIVE)
    with patch(_WS, ws):
        _, outcome, _ = await _call_tool(
            "edit_energy_config",
            {"op": "add_device", "statistic": "sensor.dryer_energy", "name": "Dryer"},
            _token(cap_energy_write="allow"), hass, data, "req-1", None,
        )
    assert outcome == "allowed"
    records = data.versions.list_for("energy", "preferences")
    assert len(records) == 1
    rec = records[0]
    assert rec.resource_type == "energy"
    assert rec.resource_id == "preferences"
    assert rec.alias == "Energy dashboard"
    # before/after must be the whole preferences, since the restore path writes
    # the snapshot back wholesale.
    assert rec.before["device_consumption"] == LIVE["device_consumption"]
    assert rec.after["device_consumption"][-1]["name"] == "Dryer"


async def test_the_approval_diff_carries_structured_preview_rows(hass: HomeAssistant) -> None:
    """The panel renders the configuration, not a reparse of diff.before/after.

    Those go through _truncate, and a truncated JSON string does not parse, which
    is the bug the card preview hit when a large card exceeded the bound. The rows
    are passed structured so the question never arises.
    """
    from custom_components.phoenix_mcp.tools.energy import _build_diff_edit_energy_config

    _seed_states(hass, "sensor.washer_energy", "sensor.grid_import", "sensor.solar_production")
    with patch(_WS, _bus(LIVE)[0]):
        diff = await _build_diff_edit_energy_config(
            {"op": "rename_device", "device_name": "Washing Machine", "name": "Washer"},
            _token(cap_energy_write="allow"), hass,
        )

    energy = diff["preview"]["energy"]
    assert [s["type"] for s in energy["before"]["sources"]] == ["grid", "solar"]
    # The grid meter list is ordered import then export, so the panel does not
    # need to know HA's field names to render them the right way round.
    assert energy["before"]["sources"][0]["meters"] == [["stat_energy_from", "sensor.grid_import"]]
    assert energy["before"]["devices"][0] == {
        "name": "Washing Machine", "statistic": "sensor.washer_energy", "water": False,
    }
    assert energy["after"]["devices"][0]["name"] == "Washer"
    # Same statistic on both sides: the panel pairs by statistic, so this renders
    # as one substitution rather than a removal plus an unrelated addition.
    assert energy["after"]["devices"][0]["statistic"] == "sensor.washer_energy"


async def test_preview_rows_are_scoped_like_every_other_read(hass: HomeAssistant) -> None:
    """An approval is opened by an admin, but the rows are built from a TOKEN read.

    Leaving them unscoped would disclose a statistic the token cannot resolve
    through a surface that exists only to describe that token's own request.
    """
    from custom_components.phoenix_mcp.tools.energy import _preview_rows

    hass.states.async_set("switch.secret_meter", "on")
    prefs = {
        "energy_sources": [{"type": "battery", "stat_energy_from": "switch.secret_meter"}],
        "device_consumption": [],
    }
    rows = _preview_rows(prefs, _token(), hass)
    assert rows["sources"][0]["meters"] == [["stat_energy_from", REDACTION_SENTINEL]]
