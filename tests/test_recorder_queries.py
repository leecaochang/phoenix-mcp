"""Tests for shared bounded Recorder query planning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.phoenix_mcp.recorder_queries import (
    add_statistic_periods,
    parse_recorder_window,
    statistic_page,
)

UTC = timezone.utc


def test_window_defaults_and_cursor() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    window = parse_recorder_window(
        {"cursor": "2026-08-11T06:00:00+00:00", "limit": 25},
        default_range=timedelta(hours=24),
        now=now,
    )
    assert window.start == now - timedelta(hours=24)
    assert window.end == now
    assert window.page_start == datetime(2026, 8, 11, 6, tzinfo=UTC)
    assert window.limit == 25


@pytest.mark.parametrize("limit", [0, 1001, True, "10"])
def test_window_rejects_unsafe_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        parse_recorder_window(
            {"limit": limit}, default_range=timedelta(hours=24),
            now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        )


def test_window_rejects_reversed_range_and_outside_cursor() -> None:
    with pytest.raises(ValueError, match="earlier"):
        parse_recorder_window(
            {"start_time": "2026-08-11T12:00:00+00:00", "end_time": "2026-08-11T11:00:00+00:00"},
            default_range=timedelta(hours=24),
        )
    with pytest.raises(ValueError, match="cursor"):
        parse_recorder_window(
            {
                "start_time": "2026-08-10T12:00:00+00:00",
                "end_time": "2026-08-11T12:00:00+00:00",
                "cursor": "2026-08-09T12:00:00+00:00",
            },
            default_range=timedelta(hours=24),
        )


def test_cursor_must_be_iso_not_relative() -> None:
    with pytest.raises(ValueError, match="cursor"):
        parse_recorder_window(
            {"cursor": "12h"},
            default_range=timedelta(hours=24),
            now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        )


def test_day_page_applies_period_cap_and_exposes_next_page() -> None:
    window = parse_recorder_window(
        {
            "start_time": "2024-01-15T12:00:00+00:00",
            "end_time": "2026-08-11T12:00:00+00:00",
            "limit": 1000,
        },
        default_range=timedelta(days=30),
    )
    page_start, page_end, effective_limit, has_more = statistic_page(window, "day")
    assert effective_limit == 366
    assert page_end == add_statistic_periods(page_start, "day", 366)
    assert has_more is True


def test_month_and_year_pages_follow_calendar_boundaries() -> None:
    window = parse_recorder_window(
        {
            "start_time": "2024-03-20T12:00:00+00:00",
            "end_time": "2026-08-11T12:00:00+00:00",
            "limit": 1000,
        },
        default_range=timedelta(days=30),
    )
    month_start, month_end, month_limit, month_more = statistic_page(window, "month")
    assert month_start.day == 1
    assert month_limit == 12
    assert month_end == add_statistic_periods(month_start, "month", 12)
    assert month_more is True

    year_start, year_end, year_limit, year_more = statistic_page(window, "year")
    assert year_start.month == 1 and year_start.day == 1
    assert year_limit == 1
    assert year_end == add_statistic_periods(year_start, "year", 1)
    assert year_more is True
