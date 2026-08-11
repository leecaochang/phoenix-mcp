"""Permission, narrowing, redaction, and truncation contracts for get_logbook."""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.logbook.websocket_api import ws_get_events

from custom_components.phoenix_mcp.mcp_view import _dispatch_mcp
from custom_components.phoenix_mcp.policy_engine import Permission
from tests.test_mcp_view import _make_data, _make_hass, _make_token

_CONTEXT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def _call(args: dict, *, token=None, hass=None, data=None):
    if token is None:
        token, _ = _make_token(cap_log_read="allow")
    if data is None:
        data = _make_data(token)
    if hass is None:
        hass = _make_hass(data)
    response, _method, _resource, outcome = await _dispatch_mcp(
        "tools/call",
        3,
        {"name": "get_logbook", "arguments": args},
        token,
        hass,
        data,
        "127.0.0.1",
        base_url="http://homeassistant.local",
    )
    text = response["result"]["content"][0]["text"]
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = text
    return body, response["result"], outcome


def test_installed_logbook_command_keeps_the_filters_phoenix_relies_on():
    """Pin the private HA command shape instead of hand-writing an assumed schema."""
    schema = ws_get_events._ws_schema.schema
    keys = {
        key.schema if hasattr(key, "schema") else key
        for key in schema
    }
    assert {"entity_ids", "device_ids", "context_id"} <= keys
    source = inspect.getsource(ws_get_events.__wrapped__)
    assert "EventProcessor(" in source
    assert "timestamp=True" in source
    assert 'msg.get("entity_ids")' in source
    assert 'msg.get("device_ids")' in source
    assert 'msg.get("context_id")' in source


@pytest.mark.asyncio
async def test_capability_denial_runs_before_hostile_argument_validation():
    token, _ = _make_token(cap_log_read="deny")
    body, result, outcome = await _call(
        {
            "entity_ids": ["marker.secret"],
            "device_ids": ["marker-device"],
            "context_id": "marker-context",
        },
        token=token,
    )
    assert outcome == "denied"
    assert result["isError"] is True
    assert body == "Forbidden."
    assert "marker" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"entity_ids": "light.one"}, "entity_ids must be an array"),
        ({"entity_ids": ["light.one", 7]}, "entity_ids must be an array"),
        ({"device_ids": [""]}, "device_ids must be an array"),
        ({"context_id": ["bad"]}, "Invalid context_id"),
        ({"context_id": "not-a-ulid"}, "Invalid context_id"),
        ({"search": ["not", "text"]}, "search must be a string"),
        ({"search": "x" * 513}, "search must be at most"),
        ({"limit": True}, "limit must be an integer"),
        ({"limit": 0}, "limit must be an integer"),
        ({"limit": 1001}, "limit must be an integer"),
    ],
)
async def test_malformed_filters_never_dispatch_to_home_assistant(args, message):
    with patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(),
    ) as command:
        body, result, outcome = await _call(args)
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert message in body
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_lists_are_deduplicated_and_capped_at_100_unique_values():
    entries = [{"entity_id": "light.one", "when": 1.0, "state": "on"}]
    command = AsyncMock(return_value=entries)
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        return_value=Permission.READ,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.filter_service_response",
        side_effect=lambda value, _token, _hass: value,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=command,
    ):
        body, _result, outcome = await _call(
            {"entity_ids": ["light.one", "light.one"]}
        )
    assert outcome == "allowed"
    assert body["filters"]["entity_ids"] == ["light.one"]
    assert command.await_args.args[2]["entity_ids"] == ["light.one"]

    too_many = [f"light.entity_{index}" for index in range(101)]
    body, result, outcome = await _call({"entity_ids": too_many})
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert "at most 100 unique" in body


@pytest.mark.asyncio
async def test_context_cannot_be_combined_with_entity_or_device_narrowing():
    for args in (
        {"context_id": _CONTEXT_ID, "entity_ids": ["light.one"]},
        {"context_id": _CONTEXT_ID, "device_ids": ["device-one"]},
    ):
        with patch(
            "custom_components.phoenix_mcp.mcp_view.async_ws_command",
            new=AsyncMock(),
        ) as command:
            body, _result, outcome = await _call(args)
        assert outcome == "invalid_request"
        assert "cannot be combined" in body
        command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {
            "start_time": "2026-01-02T00:00:00Z",
            "end_time": "2026-01-01T00:00:00Z",
        },
        {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-09T00:00:00Z",
        },
        {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-09T00:00:00Z",
            "search": "narrow text",
        },
        {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-02-02T00:00:00Z",
            "entity_ids": ["light.one"],
        },
    ],
)
async def test_invalid_or_oversized_ranges_are_rejected_before_ha(args):
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        return_value=Permission.READ,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(),
    ) as command:
        body, result, outcome = await _call(args)
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert isinstance(body, str)
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_narrowed_31_day_window_and_combined_resource_payload_are_allowed():
    command = AsyncMock(return_value=[])
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        return_value=Permission.READ,
    ) as entity_resolve, patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_device_registry_access",
        return_value=Permission.READ,
    ) as device_resolve, patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=command,
    ):
        body, _result, outcome = await _call(
            {
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-02-01T00:00:00Z",
                "entity_ids": ["light.one"],
                "device_ids": ["device-one"],
            }
        )
    assert outcome == "allowed"
    assert body["total"] == 0
    assert command.await_args.args[2] == {
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-02-01T00:00:00+00:00",
        "entity_ids": ["light.one"],
        "device_ids": ["device-one"],
    }
    entity_resolve.assert_called_once()
    device_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_inaccessible_and_missing_requested_resources_are_wire_identical():
    responses = []
    for permission in (Permission.NOT_FOUND, Permission.NO_ACCESS, Permission.DENY):
        with patch(
            "custom_components.phoenix_mcp.mcp_view.resolve",
            return_value=permission,
        ), patch(
            "custom_components.phoenix_mcp.mcp_view.async_ws_command",
            new=AsyncMock(),
        ) as command:
            body, result, outcome = await _call(
                {"entity_ids": ["light.hidden"]}
            )
        responses.append((body, result["content"], result["isError"], outcome))
        command.assert_not_awaited()
    assert responses[0] == responses[1] == responses[2]
    assert responses[0][0] == "Requested resource not found."
    assert responses[0][3] == "not_found"


@pytest.mark.asyncio
async def test_inaccessible_and_missing_requested_devices_are_identical():
    responses = []
    for permission in (Permission.NOT_FOUND, Permission.NO_ACCESS, Permission.DENY):
        with patch(
            "custom_components.phoenix_mcp.mcp_view.resolve_device_registry_access",
            return_value=permission,
        ), patch(
            "custom_components.phoenix_mcp.mcp_view.async_ws_command",
            new=AsyncMock(),
        ) as command:
            body, result, outcome = await _call(
                {"device_ids": ["device-hidden"]}
            )
        responses.append((body, result["content"], result["isError"], outcome))
        command.assert_not_awaited()
    assert responses[0] == responses[1] == responses[2]
    assert responses[0][0] == "Requested resource not found."
    assert responses[0][3] == "not_found"


@pytest.mark.asyncio
async def test_any_inaccessible_resource_rejects_the_whole_combined_request():
    def entity_permission(entity_id, _token, _hass):
        return Permission.READ if entity_id == "light.ok" else Permission.NO_ACCESS

    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        side_effect=entity_permission,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_device_registry_access",
        return_value=Permission.READ,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(),
    ) as command:
        body, _result, outcome = await _call(
            {
                "entity_ids": ["light.ok", "light.hidden"],
                "device_ids": ["device-ok"],
            }
        )
    assert outcome == "not_found"
    assert body == "Requested resource not found."
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_entity_permission_takes_precedence_and_device_only_rows_use_device_access():
    entries = [
        {
            "entity_id": "light.ok",
            "device_id": "device-hidden",
            "message": "entity wins",
            "when": 1.0,
        },
        {
            "entity_id": "light.hidden",
            "device_id": "device-ok",
            "message": "device cannot override entity",
            "when": 2.0,
        },
        {"device_id": "device-ok", "message": "device only", "when": 3.0},
        {"device_id": "device-hidden", "message": "hidden device", "when": 4.0},
        {"name": "unscoped", "message": "no resource", "when": 5.0},
    ]

    def entity_permission(entity_id, _token, _hass):
        return Permission.READ if entity_id == "light.ok" else Permission.NO_ACCESS

    def device_permission(device_id, _token, _hass):
        return Permission.READ if device_id == "device-ok" else Permission.NO_ACCESS

    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        side_effect=entity_permission,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.resolve_device_registry_access",
        side_effect=device_permission,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.filter_service_response",
        side_effect=lambda value, _token, _hass: value,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(return_value=entries),
    ):
        body, _result, outcome = await _call({})
    assert outcome == "allowed"
    assert [entry["message"] for entry in body["entries"]] == [
        "entity wins",
        "device only",
    ]


@pytest.mark.asyncio
async def test_redaction_precedes_search_and_only_safe_fields_are_searchable():
    entries = [
        {
            "entity_id": "light.ok",
            "message": "raw secret",
            "context_message": "unsafe needle",
            "when": 1.0,
        }
    ]

    def redact(value, _token, _hass):
        value[0]["message"] = "<redacted>"
        return value

    async def run(search):
        with patch(
            "custom_components.phoenix_mcp.mcp_view.resolve",
            return_value=Permission.READ,
        ), patch(
            "custom_components.phoenix_mcp.mcp_view.filter_service_response",
            side_effect=redact,
        ), patch(
            "custom_components.phoenix_mcp.mcp_view.async_ws_command",
            new=AsyncMock(return_value=[dict(entries[0])]),
        ):
            return await _call({"search": search})

    raw, _result, _outcome = await run("raw secret")
    safe, _result, _outcome = await run("<redacted>")
    unsafe_field, _result, _outcome = await run("unsafe needle")
    assert raw["total"] == 0
    assert safe["total"] == 1
    assert unsafe_field["total"] == 0


@pytest.mark.asyncio
async def test_total_and_truncated_are_computed_before_most_recent_limit():
    entries = [
        {"entity_id": "light.ok", "message": "third", "when": 3.0},
        {"entity_id": "light.ok", "message": "first", "when": 1.0},
        {"entity_id": "light.ok", "message": "second", "when": 2.0},
    ]
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        return_value=Permission.READ,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.filter_service_response",
        side_effect=lambda value, _token, _hass: value,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(return_value=entries),
    ):
        body, _result, outcome = await _call({"limit": 2})
    assert outcome == "allowed"
    assert body["count"] == 2
    assert body["total"] == 3
    assert body["truncated"] is True
    assert [entry["message"] for entry in body["entries"]] == ["second", "third"]


@pytest.mark.asyncio
async def test_context_filter_is_forwarded_and_echoed_without_resource_filters():
    command = AsyncMock(return_value=[])
    with patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=command,
    ):
        body, _result, outcome = await _call({"context_id": _CONTEXT_ID})
    assert outcome == "allowed"
    assert command.await_args.args[2]["context_id"] == _CONTEXT_ID
    assert "entity_ids" not in command.await_args.args[2]
    assert "device_ids" not in command.await_args.args[2]
    assert body["filters"]["context_id"] == _CONTEXT_ID
    assert body["filters"]["limit"] == 100


def test_published_resource_lists_are_unique_and_bounded():
    from custom_components.phoenix_mcp.tool_defs import _SYSTEM_TOOL_DEFS

    definition = next(
        item for item in _SYSTEM_TOOL_DEFS if item["name"] == "get_logbook"
    )
    properties = definition["inputSchema"]["properties"]
    for key in ("entity_ids", "device_ids"):
        assert properties[key]["maxItems"] == 100
        assert properties[key]["uniqueItems"] is True


@pytest.mark.asyncio
async def test_visible_entry_shape_drift_is_an_error_not_false_chronology():
    with patch(
        "custom_components.phoenix_mcp.mcp_view.resolve",
        return_value=Permission.READ,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.filter_service_response",
        side_effect=lambda value, _token, _hass: value,
    ), patch(
        "custom_components.phoenix_mcp.mcp_view.async_ws_command",
        new=AsyncMock(
            return_value=[{"entity_id": "light.ok", "when": "changed-shape"}]
        ),
    ):
        body, result, outcome = await _call({})
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert body == "Unexpected logbook entry shape from Home Assistant."
