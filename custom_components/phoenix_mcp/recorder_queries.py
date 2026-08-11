"""Bounded time, cursor, retention, and response helpers for Recorder tools."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping
from typing import Any, Literal

from homeassistant.util import dt as dt_util

from .helpers import parse_time_param

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_SIGNIFICANT_STATES_RANGE = timedelta(days=7)

STATISTIC_PERIOD_LIMITS = {
    "5minute": 1000,
    "hour": 1000,
    "day": 366,
    "week": 52,
    "month": 12,
    "year": 1,
}


@dataclass(frozen=True, slots=True)
class RecorderWindow:
    """A validated requested range and the page position within it."""

    start: datetime
    end: datetime
    page_start: datetime
    limit: int


def _as_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return dt_util.as_utc(value)


def parse_recorder_window(
    args: dict[str, Any], *, default_range: timedelta, now: datetime | None = None
) -> RecorderWindow:
    """Parse and validate the shared Recorder tool range and cursor inputs."""
    effective_now = _as_utc(now or dt_util.utcnow())
    end = effective_now
    if args.get("end_time"):
        try:
            end = _as_utc(parse_time_param(args["end_time"]))
        except (TypeError, ValueError) as err:
            raise ValueError("Invalid end_time format.") from err

    start = end - default_range
    if args.get("start_time"):
        try:
            start = _as_utc(parse_time_param(args["start_time"]))
        except (TypeError, ValueError) as err:
            raise ValueError("Invalid start_time format.") from err
    if start >= end:
        raise ValueError("start_time must be earlier than end_time.")

    page_start = start
    if args.get("cursor"):
        try:
            raw_cursor = args["cursor"]
            parsed_cursor = dt_util.parse_datetime(raw_cursor) if isinstance(raw_cursor, str) else None
            if parsed_cursor is None:
                raise ValueError
            page_start = _as_utc(parsed_cursor)
        except (TypeError, ValueError) as err:
            raise ValueError("Invalid cursor format.") from err
        if not start <= page_start < end:
            raise ValueError("cursor must fall within the requested time range.")

    raw_limit = args.get("limit", DEFAULT_LIMIT)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise ValueError("limit must be an integer.")
    if not 1 <= raw_limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
    return RecorderWindow(start=start, end=end, page_start=page_start, limit=raw_limit)


def iso_utc(value: datetime) -> str:
    """Return an ISO 8601 UTC timestamp."""
    return dt_util.as_utc(value).isoformat()


def state_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Extract a normalized Recorder state timestamp."""
    value = row.get("last_changed") or row.get("last_updated")
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            return _as_utc(parse_time_param(value))
        except ValueError:
            return None
    return None


def statistic_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Extract a normalized statistics bucket timestamp."""
    value = row.get("start")
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=dt_util.UTC)
    if isinstance(value, str):
        try:
            return _as_utc(parse_time_param(value))
        except ValueError:
            return None
    return None


def retention_metadata(kind: Literal["short_term", "long_term"], keep_days: int) -> dict[str, Any]:
    """Describe which Recorder store backs a response."""
    if kind == "short_term":
        return {"kind": kind, "configured_days": keep_days}
    return {"kind": kind}


def retention_warnings(
    kind: Literal["short_term", "long_term"], start: datetime, keep_days: int, now: datetime
) -> list[str]:
    """Warn when a short-term request predates configured Recorder retention."""
    if kind == "short_term" and start < now - timedelta(days=keep_days):
        return [
            f"The requested start is older than the configured {keep_days}-day short-term Recorder retention; earlier data may be unavailable."
        ]
    return []


def recorder_envelope(
    *,
    entity_id: str,
    window: RecorderWindow,
    covered_start: datetime,
    covered_end: datetime,
    rows: list[dict[str, Any]],
    timestamp_getter: Any,
    effective_limit: int,
    has_more: bool,
    next_cursor: datetime | None,
    retention: dict[str, Any],
    warnings: list[str],
    result_key: Literal["history", "statistics"],
) -> dict[str, Any]:
    """Build the common cursor-based Recorder response contract."""
    timestamps = [stamp for row in rows if (stamp := timestamp_getter(row)) is not None]
    data_range = None
    if timestamps:
        data_range = {"start": iso_utc(timestamps[0]), "end": iso_utc(timestamps[-1])}
    return {
        "entity_id": entity_id,
        "requested_range": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
        "covered_range": {"start": iso_utc(covered_start), "end": iso_utc(covered_end)},
        "data_range": data_range,
        "count": len(rows),
        "limit": window.limit,
        "effective_limit": effective_limit,
        "has_more": has_more,
        "next_cursor": iso_utc(next_cursor) if next_cursor is not None else None,
        "retention": retention,
        "warnings": warnings,
        result_key: rows,
    }


def floor_statistic_period(value: datetime, period: str) -> datetime:
    """Floor a timestamp to a Home Assistant local calendar period."""
    local = dt_util.as_local(value)
    if period == "5minute":
        local = local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)
    elif period == "hour":
        local = local.replace(minute=0, second=0, microsecond=0)
    elif period == "day":
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        local = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    elif period == "month":
        local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        local = local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unsupported statistics period: {period}")
    return dt_util.as_utc(local)


def add_statistic_periods(value: datetime, period: str, count: int) -> datetime:
    """Advance by local calendar periods without losing DST alignment."""
    if period == "5minute":
        return dt_util.as_utc(value) + timedelta(minutes=5 * count)
    elif period == "hour":
        return dt_util.as_utc(value) + timedelta(hours=count)

    local = dt_util.as_local(value)
    if period == "day":
        result = local + timedelta(days=count)
    elif period == "week":
        result = local + timedelta(weeks=count)
    elif period in ("month", "year"):
        months = count if period == "month" else count * 12
        month_index = local.year * 12 + local.month - 1 + months
        year, zero_month = divmod(month_index, 12)
        month = zero_month + 1
        result = local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))
    else:
        raise ValueError(f"Unsupported statistics period: {period}")
    return dt_util.as_utc(result)


def ceil_statistic_period(value: datetime, period: str) -> datetime:
    """Ceil a timestamp to the next local calendar bucket boundary."""
    floor = floor_statistic_period(value, period)
    if floor == value:
        return floor
    return add_statistic_periods(floor, period, 1)


def statistic_page(window: RecorderWindow, period: str) -> tuple[datetime, datetime, int, bool]:
    """Return an aligned and safely bounded statistics page."""
    effective_limit = min(window.limit, STATISTIC_PERIOD_LIMITS[period])
    page_start = floor_statistic_period(window.page_start, period)
    requested_end = ceil_statistic_period(window.end, period)
    page_end = min(add_statistic_periods(page_start, period, effective_limit), requested_end)
    return page_start, page_end, effective_limit, page_end < requested_end
