"""Real Home Assistant system-log fixtures shared by Phoenix tests."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from homeassistant.components.system_log import DedupStore, LogEntry


def make_log_entry(
    *,
    name: str = "homeassistant.components.light",
    message: str = "it broke",
    level: int = logging.ERROR,
    created: float = 1_700_000_000.0,
    pathname: str = "/config/custom_components/example/__init__.py",
    lineno: int = 17,
) -> LogEntry:
    """Build an entry through Home Assistant's installed LogEntry constructor."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=pathname,
        lineno=lineno,
        msg=message,
        args=(),
        exc_info=None,
    )
    record.created = created
    return LogEntry(record, re.compile(r"$^"), figure_out_source=False)


def make_log_store(entries: list[LogEntry], *, capacity: int = 50) -> DedupStore:
    """Add real entries through Home Assistant's installed deduplication store."""
    store = DedupStore(maxlen=capacity)
    for entry in entries:
        store.add_entry(entry)
    return store


def attach_log_store(hass, store: DedupStore) -> None:
    """Attach the real store using Home Assistant's current system_log envelope."""
    hass.data["system_log"] = SimpleNamespace(records=store)
    hass.config.config_dir = "/config"
