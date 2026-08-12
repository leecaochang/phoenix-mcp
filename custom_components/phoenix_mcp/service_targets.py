"""Authorization boundary for service-data target selectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.service import async_get_all_descriptions

from .const import SECONDARY_TARGET_SELECTOR_KEYS, TARGET_SELECTOR_KEYS

_TARGET_SELECTOR_TYPES = frozenset({"area", "device", "entity", "target"})


def _contains_target_selector(value: object) -> bool:
    """Whether a selector document can name HA registry targets."""
    if isinstance(value, Mapping):
        if _TARGET_SELECTOR_TYPES.intersection(str(key) for key in value):
            return True
        return any(_contains_target_selector(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_target_selector(item) for item in value)
    return False


def secondary_target_fields(descriptions: object) -> frozenset[str]:
    """Collect non-primary target field names from HA service descriptions.

    This intentionally scans every registered service, not only the requested
    one. A field name that acts as a target anywhere is unsafe as opaque service
    data everywhere: otherwise a missing or late-loaded description would turn
    the same key from refused into accepted.
    """
    found = set(SECONDARY_TARGET_SELECTOR_KEYS)
    if not isinstance(descriptions, Mapping):
        return frozenset(found)
    for services in descriptions.values():
        if not isinstance(services, Mapping):
            continue
        for description in services.values():
            if not isinstance(description, Mapping):
                continue
            fields = description.get("fields")
            if not isinstance(fields, Mapping):
                continue
            for name, field in fields.items():
                if (
                    isinstance(field, Mapping)
                    and _contains_target_selector(field.get("selector"))
                    and str(name) not in TARGET_SELECTOR_KEYS
                ):
                    found.add(str(name))
    return frozenset(found)


async def unsupported_secondary_targets(
    hass: HomeAssistant, service_data: object
) -> tuple[str, ...]:
    """Return caller fields that could select targets Phoenix did not resolve.

    HA caches the description load and refreshes it when registered services
    change. If that compatibility seam fails, the static fallback still blocks
    every target field known at build time rather than disabling the guard.
    """
    if not isinstance(service_data, dict) or not service_data:
        return ()
    try:
        descriptions: Any = await async_get_all_descriptions(hass)
    except Exception:  # noqa: BLE001 - the static fallback is the safe degradation
        descriptions = {}
    unsafe = secondary_target_fields(descriptions)
    return tuple(sorted(str(key) for key in service_data if str(key) in unsafe))


def secondary_target_error(fields: tuple[str, ...]) -> str:
    """Explain a refusal using only field names the caller supplied."""
    joined = ", ".join(fields)
    return (
        f"Unsupported secondary target selector in service_data: {joined}. "
        "Phoenix MCP can authorize only the primary entity_id, device_id, or area_id "
        "target for a generic service call."
    )
