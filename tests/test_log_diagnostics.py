"""Adversarial contracts for bounded system-log and Phoenix diagnostics reads."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.system_log import DedupStore
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.const import TOKEN_PREFIX
from custom_components.phoenix_mcp.policy_engine import Permission
from tests.log_fixtures import attach_log_store, make_log_entry, make_log_store
from tests.test_mcp_view import _make_data, _make_hass, _make_token


async def _call(tool: str, args: dict, *, token=None, hass=None, data=None):
    if token is None:
        token, _ = _make_token(cap_log_read="allow")
    if data is None:
        data = _make_data(token)
    if hass is None:
        hass = _make_hass(data)
        attach_log_store(hass, DedupStore(maxlen=50))
    response, _method, _resource, outcome = await mcp_view._dispatch_mcp(
        "tools/call",
        3,
        {"name": tool, "arguments": args},
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


def _hass_with_entries(token, entries, *, capacity=50):
    data = _make_data(token)
    hass = _make_hass(data)
    attach_log_store(hass, make_log_store(entries, capacity=capacity))
    return hass, data


@pytest.mark.asyncio
async def test_empty_real_store_is_available_and_truthfully_empty():
    body, result, outcome = await _call("get_logs", {})
    assert outcome == "allowed"
    assert "isError" not in result
    assert body["source"] == {
        "status": "available",
        "kind": "home_assistant_system_log",
        "semantics": "deduplicated_buckets",
        "pagination": "best_effort_live_ring",
        "capacity": 50,
        "retained_buckets": 0,
        "skipped_buckets": 0,
        "earliest_first_occurred": None,
        "latest_occurred": None,
        "read_at": body["source"]["read_at"],
        "recorded_levels": ["WARNING", "ERROR", "CRITICAL"],
    }
    assert body["count"] == body["matched_buckets"] == 0
    assert body["entries"] == []


@pytest.mark.asyncio
async def test_absent_and_unreadable_sources_are_structured_tool_errors():
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    body, result, outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert body["source"]["status"] == "unavailable"
    assert body["entries"] == []
    assert body["filters"]["time_basis"] == "latest_occurrence"

    class UnreadableRecords:
        maxlen = 50

        def values(self):
            raise RuntimeError("changed upstream")

    hass.data["system_log"] = SimpleNamespace(records=UnreadableRecords())
    body, result, outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert body["source"]["status"] == "degraded"
    assert "changed upstream" not in body["source"]["reason"]


@pytest.mark.asyncio
async def test_partial_shape_drift_skips_only_bad_buckets_and_reports_coverage():
    token, _ = _make_token(cap_log_read="allow")
    good = make_log_entry(created=1_700_000_010)
    good.first_occurred = 1_700_000_000
    store = make_log_store([good], capacity=77)
    store["bad"] = SimpleNamespace(level="ERROR")
    hass, data = _hass_with_entries(token, [])
    attach_log_store(hass, store)
    body, _result, outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    assert outcome == "allowed"
    assert body["source"]["status"] == "degraded"
    assert body["source"]["capacity"] == 77
    assert body["source"]["retained_buckets"] == 2
    assert body["source"]["skipped_buckets"] == 1
    assert body["source"]["earliest_first_occurred"] == "2023-11-14T22:13:20+00:00"
    assert body["source"]["latest_occurred"] == "2023-11-14T22:13:30+00:00"
    assert body["count"] == 1
    assert "Skipped 1 malformed" in body["warnings"][0]


@pytest.mark.asyncio
async def test_bucket_with_unreadable_messages_is_skipped_not_raised():
    class BrokenMessages:
        def __iter__(self):
            raise RuntimeError("bad bucket")

    token, _ = _make_token(cap_log_read="allow")
    good = make_log_entry()
    broken = SimpleNamespace(
        level="ERROR",
        name="homeassistant.components.light",
        timestamp=1_700_000_001,
        first_occurred=1_700_000_001,
        message=BrokenMessages(),
        count=1,
    )
    store = make_log_store([good])
    store["broken"] = broken
    hass, data = _hass_with_entries(token, [])
    attach_log_store(hass, store)
    body, _result, outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    assert outcome == "allowed"
    assert body["source"]["status"] == "degraded"
    assert body["source"]["skipped_buckets"] == 1
    assert body["count"] == 1


@pytest.mark.asyncio
async def test_real_dedup_store_preserves_all_five_retained_message_variants():
    token, _ = _make_token(cap_log_read="allow")
    entries = [
        make_log_entry(message=f"variant {index}", created=1_700_000_000 + index)
        for index in range(7)
    ]
    hass, data = _hass_with_entries(token, entries)
    body, _result, outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    assert outcome == "allowed"
    assert body["count"] == 1
    assert body["entries"][0]["messages"] == [
        "variant 2",
        "variant 3",
        "variant 4",
        "variant 5",
        "variant 6",
    ]
    assert body["entries"][0]["occurrences"] == 7
    assert body["entries"][0]["first_occurred"] == "2023-11-14T22:13:20+00:00"
    assert body["entries"][0]["latest_occurred"] == "2023-11-14T22:13:26+00:00"


@pytest.mark.asyncio
async def test_source_and_root_cause_paths_are_normalized_without_host_disclosure():
    token, _ = _make_token(cap_log_read="allow")
    safe = make_log_entry(
        pathname="/usr/lib/homeassistant/components/light/__init__.py",
        lineno=44,
    )
    safe.root_cause = (
        "/config/custom_components/example/client.py",
        91,
        "async_update",
    )
    unsafe = make_log_entry(
        pathname="/srv/private/vendor/secret.py",
        lineno=12,
        created=1_700_000_001,
    )
    hass, data = _hass_with_entries(token, [safe, unsafe])
    body, _result, _outcome = await _call(
        "get_logs", {}, token=token, hass=hass, data=data
    )
    by_line = {entry["source"]["line"]: entry for entry in body["entries"]}
    assert by_line[44]["source"] == {
        "file": "homeassistant/components/light/__init__.py",
        "line": 44,
    }
    assert by_line[44]["root_cause"] == {
        "file": "custom_components/example/client.py",
        "line": 91,
        "function": "async_update",
    }
    assert by_line[12]["source"] == {"file": "<redacted-path>", "line": 12}


@pytest.mark.asyncio
async def test_filters_use_latest_occurrence_and_complete_logger_namespaces():
    token, _ = _make_token(cap_log_read="allow")
    entries = [
        make_log_entry(
            name="homeassistant.components.light",
            message="light exact",
            created=1_700_000_010,
            pathname="/config/custom_components/light/a.py",
        ),
        make_log_entry(
            name="homeassistant.components.light.child",
            message="light child",
            created=1_700_000_020,
            pathname="/config/custom_components/light/b.py",
        ),
        make_log_entry(
            name="homeassistant.components.lighthouse",
            message="lookalike",
            created=1_700_000_030,
            pathname="/config/custom_components/lighthouse/a.py",
        ),
    ]
    hass, data = _hass_with_entries(token, entries)
    args = {
        "integration": "light",
        "logger": "homeassistant.components.light",
        "search": "LIGHT",
        "since": "2023-11-14T22:13:35+00:00",
        "until": "2023-11-14T22:13:45+00:00",
    }
    body, _result, outcome = await _call(
        "get_logs", args, token=token, hass=hass, data=data
    )
    assert outcome == "allowed"
    assert [entry["messages"][0] for entry in body["entries"]] == ["light child"]
    assert body["filters"] == {
        "level": "WARNING",
        "integration": "light",
        "logger": "homeassistant.components.light",
        "search": "LIGHT",
        "since": "2023-11-14T22:13:35+00:00",
        "until": "2023-11-14T22:13:45+00:00",
        "time_basis": "latest_occurrence",
    }
    integration_only, _result, _outcome = await _call(
        "get_logs", {"integration": "light"}, token=token, hass=hass, data=data
    )
    assert {entry["messages"][0] for entry in integration_only["entries"]} == {
        "light exact",
        "light child",
    }


@pytest.mark.asyncio
async def test_search_runs_after_scrubbing_and_cannot_probe_raw_credentials():
    raw_token = TOKEN_PREFIX + "a" * 64
    token, _ = _make_token(cap_log_read="allow")
    entry = make_log_entry(message="request rejected")
    entry.exception = f"authentication failed for {raw_token}"
    hass, data = _hass_with_entries(token, [entry])
    raw_body, _result, _outcome = await _call(
        "get_logs", {"search": raw_token}, token=token, hass=hass, data=data
    )
    safe_body, _result, _outcome = await _call(
        "get_logs", {"search": "<phoenix-token>"}, token=token, hass=hass, data=data
    )
    assert raw_body["matched_buckets"] == 0
    assert safe_body["matched_buckets"] == 1
    assert raw_token not in json.dumps(safe_body)


@pytest.mark.asyncio
async def test_naive_and_timezone_aware_iso_bounds_share_utc_semantics():
    token, _ = _make_token(cap_log_read="allow")
    entry = make_log_entry(created=1_700_000_000)
    hass, data = _hass_with_entries(token, [entry])
    body, _result, outcome = await _call(
        "get_logs",
        {
            "since": "2023-11-14T22:00:00",
            "until": "2023-11-14T23:00:00Z",
        },
        token=token,
        hass=hass,
        data=data,
    )
    assert outcome == "allowed"
    assert body["count"] == 1
    assert body["filters"]["since"] == "2023-11-14T22:00:00+00:00"


@pytest.mark.asyncio
async def test_pagination_is_filter_bound_deterministic_and_reports_live_semantics():
    token, _ = _make_token(cap_log_read="allow")
    entries = [
        make_log_entry(
            message=f"row {index}",
            created=1_700_000_100,
            pathname=f"/config/custom_components/example/file_{index}.py",
        )
        for index in range(5)
    ]
    hass, data = _hass_with_entries(token, entries)
    first, _result, _outcome = await _call(
        "get_logs", {"limit": 2}, token=token, hass=hass, data=data
    )
    second, _result, _outcome = await _call(
        "get_logs",
        {"limit": 2, "cursor": first["next_cursor"]},
        token=token,
        hass=hass,
        data=data,
    )
    assert first["matched_buckets"] == second["matched_buckets"] == 5
    assert first["source"]["pagination"] == "best_effort_live_ring"
    first_messages = {entry["messages"][0] for entry in first["entries"]}
    second_messages = {entry["messages"][0] for entry in second["entries"]}
    assert first_messages.isdisjoint(second_messages)
    assert second["warnings"] == [
        "Pagination is best-effort over a live deduplicated ring; buckets updated after the first page may move."
    ]

    error, result, outcome = await _call(
        "get_logs",
        {"limit": 2, "level": "ERROR", "cursor": first["next_cursor"]},
        token=token,
        hass=hass,
        data=data,
    )
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert error == "Invalid cursor for these log filters."


@pytest.mark.asyncio
async def test_relative_time_cursor_reuses_the_original_resolved_window():
    token, _ = _make_token(cap_log_read="allow")
    now = utcnow().timestamp()
    entries = [
        make_log_entry(
            message=f"recent {index}",
            created=now - index,
            pathname=f"/config/custom_components/example/recent_{index}.py",
        )
        for index in range(3)
    ]
    hass, data = _hass_with_entries(token, entries)
    first, _result, _outcome = await _call(
        "get_logs", {"since": "1h", "limit": 1}, token=token, hass=hass, data=data
    )
    second, _result, outcome = await _call(
        "get_logs",
        {"since": "1h", "limit": 1, "cursor": first["next_cursor"]},
        token=token,
        hass=hass,
        data=data,
    )
    assert outcome == "allowed"
    assert second["filters"]["since"] == first["filters"]["since"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"since": "2024-01-02T00:00:00+00:00", "until": "2024-01-01T00:00:00+00:00"},
        {"integration": "Light!"},
        {"logger": "homeassistant..light"},
        {"search": "x" * 513},
        {"cursor": "x" * 4097},
    ],
)
async def test_invalid_log_filters_fail_before_reading_the_source(args):
    token, _ = _make_token(cap_log_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    body, result, outcome = await _call(
        "get_logs", args, token=token, hass=hass, data=data
    )
    assert outcome == "invalid_request"
    assert result["isError"] is True
    assert isinstance(body, str)


@pytest.mark.asyncio
async def test_phoenix_diagnostics_requires_both_capabilities_before_validation():
    for log_mode, diagnostics_mode in (("deny", "allow"), ("allow", "deny")):
        token, _ = _make_token(cap_log_read=log_mode)
        token.cap_diagnostics = diagnostics_mode
        body, _result, outcome = await _call(
            "get_phoenix_diagnostics", {"level": "INFO"}, token=token
        )
        assert outcome == "denied"
        assert body == "Forbidden."


@pytest.mark.asyncio
async def test_phoenix_diagnostics_is_separate_and_strongly_redacted():
    token, _ = _make_token(cap_log_read="allow")
    token.cap_diagnostics = "allow"
    phoenix = make_log_entry(
        name="custom_components.phoenix_mcp.mcp_view",
        message=(
            "entity sensor.allowed denied sensor.hidden at http://nas.local:8123/a "
            "/config/.storage/core 10.0.0.4 "
            "123e4567-e89b-12d3-a456-426614174000 "
            "01ARZ3NDEKTSV4RRFFQ69G5FAV "
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    )
    lookalike = make_log_entry(
        name="custom_components.phoenix_mcp_extra",
        message="not Phoenix",
        created=1_700_000_001,
        pathname="/config/custom_components/phoenix_mcp_extra/a.py",
    )
    ordinary = make_log_entry(
        name="homeassistant.components.light",
        message="ordinary",
        created=1_700_000_002,
    )
    hass, data = _hass_with_entries(token, [phoenix, lookalike, ordinary])

    def permission(entity_id, _token, _hass):
        return Permission.READ if entity_id == "sensor.allowed" else Permission.NO_ACCESS

    with patch("custom_components.phoenix_mcp.helpers.resolve", side_effect=permission):
        general, _result, _outcome = await _call(
            "get_logs", {}, token=token, hass=hass, data=data
        )
        diagnostics, _result, outcome = await _call(
            "get_phoenix_diagnostics",
            {"logger": "custom_components.phoenix_mcp"},
            token=token,
            hass=hass,
            data=data,
        )

    assert outcome == "allowed"
    assert {entry["messages"][0] for entry in general["entries"]} == {
        "not Phoenix",
        "ordinary",
    }
    assert diagnostics["count"] == 1
    text = diagnostics["entries"][0]["messages"][0]
    assert "sensor.allowed" in text
    assert "sensor.hidden" not in text
    assert "<redacted-entity>" in text
    assert "nas.local" not in text
    assert "/config" not in text
    assert "10.0.0.4" not in text
    assert "123e4567" not in text
    assert "01ARZ3" not in text
    assert "aaaaaaaaaaaaaaaa" not in text
    assert diagnostics["source"]["recorded_levels"] == [
        "WARNING",
        "ERROR",
        "CRITICAL",
    ]

    with patch(
        "custom_components.phoenix_mcp.helpers.resolve",
        side_effect=RuntimeError("registry drift"),
    ):
        fail_closed, _result, fail_closed_outcome = await _call(
            "get_phoenix_diagnostics", {}, token=token, hass=hass, data=data
        )
    assert fail_closed_outcome == "allowed"
    assert "sensor.allowed" not in fail_closed["entries"][0]["messages"][0]


@pytest.mark.asyncio
async def test_phoenix_search_cannot_probe_pre_redaction_topology():
    token, _ = _make_token(cap_log_read="allow")
    token.cap_diagnostics = "allow"
    entry = make_log_entry(
        name="custom_components.phoenix_mcp",
        message="connection from 192.168.1.55 failed",
    )
    hass, data = _hass_with_entries(token, [entry])
    with patch(
        "custom_components.phoenix_mcp.helpers.resolve",
        return_value=Permission.NO_ACCESS,
    ):
        raw, _result, _outcome = await _call(
            "get_phoenix_diagnostics",
            {"search": "192.168.1.55"},
            token=token,
            hass=hass,
            data=data,
        )
        safe, _result, _outcome = await _call(
            "get_phoenix_diagnostics",
            {"search": "<redacted-ip>"},
            token=token,
            hass=hass,
            data=data,
        )
    assert raw["matched_buckets"] == 0
    assert safe["matched_buckets"] == 1


@pytest.mark.asyncio
async def test_dual_cap_tool_announcement_and_gate_map_require_both_caps():
    for log_mode, diagnostics_mode, expected in (
        ("allow", "deny", False),
        ("deny", "allow", False),
        ("allow", "allow", True),
    ):
        token, _ = _make_token(cap_log_read=log_mode)
        token.cap_diagnostics = diagnostics_mode
        data = _make_data(token)
        hass = _make_hass(data)
        response, _method, _resource, _outcome = await mcp_view._dispatch_mcp(
            "tools/list",
            1,
            {},
            token,
            hass,
            data,
            "127.0.0.1",
            base_url="http://homeassistant.local",
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        gate_map = mcp_view._tool_gate_map(token, data, hass)
        assert ("get_phoenix_diagnostics" in names) is expected
        bucket = "usable" if expected else "unavailable"
        assert "get_phoenix_diagnostics" in gate_map[bucket]
        published = next(
            tool
            for tool in mcp_view._SYSTEM_TOOL_DEFS
            if tool["name"] == "get_phoenix_diagnostics"
        )
        assert published["caps"] == ["cap_diagnostics", "cap_log_read"]
