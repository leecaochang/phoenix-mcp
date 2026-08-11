"""Tests for the bounded statistics MCP contract."""

from __future__ import annotations

import functools
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _token() -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="t",
        token_hash="x",
        created_at=utcnow(),
        created_by="u",
        permissions=PermissionTree(domains={"sensor": PermissionNode(state="GREEN")}),
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(args: dict, hass):
    return await _call_tool("get_statistics", args, _token(), hass, MagicMock())


def _recorder(result: dict) -> tuple[MagicMock, AsyncMock]:
    instance = MagicMock()
    instance.keep_days = 10
    executor = AsyncMock(return_value=result)
    instance.async_add_executor_job = executor
    return instance, executor


async def test_statistics_common_envelope_and_safe_day_cap(hass) -> None:
    hass.states.async_set("sensor.power", "12", {"state_class": "measurement"})
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    rows = [
        {"start": now - timedelta(days=2), "mean": 10.0},
        {"start": now - timedelta(days=1), "mean": 12.0},
    ]
    instance, executor = _recorder({"sensor.power": rows})
    with patch("homeassistant.components.recorder.get_instance", return_value=instance):
        content, outcome, _ = await _call(
            {
                "entity_id": "sensor.power",
                "start_time": (now - timedelta(days=500)).isoformat(),
                "end_time": now.isoformat(),
                "period": "day",
                "limit": 1000,
                "statistic_types": ["mean", "last_reset"],
            },
            hass,
        )
    body = _json(content)
    assert outcome == "allowed"
    assert body["period"] == "day"
    assert body["limit"] == 1000
    assert body["effective_limit"] == 366
    assert body["has_more"] is True
    assert body["next_cursor"] == body["covered_range"]["end"]
    assert body["retention"] == {"kind": "long_term"}
    assert body["statistics"] == []
    assert body["count"] == 0
    assert "state_class" in body["warnings"][0]
    job = executor.await_args.args[0]
    assert isinstance(job, functools.partial)
    assert job.args[-1] == {"mean", "last_reset"}


async def test_empty_five_minute_page_can_have_more_and_warn_about_retention(hass) -> None:
    hass.states.async_set("sensor.power", "12", {"state_class": "measurement"})
    instance, _ = _recorder({})
    with patch("homeassistant.components.recorder.get_instance", return_value=instance):
        content, outcome, _ = await _call(
            {"entity_id": "sensor.power", "start_time": "30d", "period": "5minute", "limit": 10},
            hass,
        )
    body = _json(content)
    assert outcome == "allowed"
    assert body["statistics"] == []
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    assert body["retention"] == {"kind": "short_term", "configured_days": 10}
    assert len(body["warnings"]) == 2


async def test_statistics_returns_rows_and_complete_final_page(hass) -> None:
    hass.states.async_set("sensor.power", "12", {"state_class": "measurement"})
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=2)
    rows = [
        {"start": start, "mean": 10.0},
        {"start": start + timedelta(hours=1), "mean": 12.0},
    ]
    instance, _ = _recorder({"sensor.power": rows})
    with patch("homeassistant.components.recorder.get_instance", return_value=instance):
        content, outcome, _ = await _call(
            {
                "entity_id": "sensor.power",
                "start_time": start.isoformat(),
                "end_time": now.isoformat(),
                "period": "hour",
                "limit": 10,
            },
            hass,
        )
    body = _json(content)
    assert outcome == "allowed"
    assert body["statistics"] == [
        {"start": start.isoformat(), "mean": 10.0},
        {"start": (start + timedelta(hours=1)).isoformat(), "mean": 12.0},
    ]
    assert body["count"] == 2
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert body["warnings"] == []
    assert body["data_range"] == {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
    }


async def test_year_period_is_rejected_on_older_home_assistant(hass) -> None:
    hass.states.async_set("sensor.power", "12", {"state_class": "measurement"})
    with (
        patch("homeassistant.const.__version__", "2026.2.0"),
        patch("homeassistant.components.recorder.get_instance") as get_instance,
    ):
        content, outcome, _ = await _call(
            {"entity_id": "sensor.power", "period": "year"}, hass,
        )
    assert outcome == "invalid_request"
    assert "2026.3" in content["content"][0]["text"]
    get_instance.assert_not_called()


async def test_statistics_rejects_unknown_type_before_recorder_query(hass) -> None:
    hass.states.async_set("sensor.power", "12", {"state_class": "measurement"})
    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        content, outcome, _ = await _call(
            {"entity_id": "sensor.power", "statistic_types": ["median"]}, hass,
        )
    assert outcome == "invalid_request"
    assert "statistic_types" in content["content"][0]["text"]
    get_instance.assert_not_called()
