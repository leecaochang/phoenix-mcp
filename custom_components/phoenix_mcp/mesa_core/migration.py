"""Explicit schema version migration (Spec Section 23).

Profiles are never silently migrated or rewritten: this utility runs only when
the operator requests it, returns a migrated copy, and logs every
transformation applied. A document without a ``schema_version`` is 1.0-era and
is migrated through the 1.0 step; defaulting it to the current version would
silently exempt it from every migration.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

from custom_components.phoenix_mcp.mesa_core.exceptions import MesaError

logger = logging.getLogger("mesa_core.migration")

CURRENT_SCHEMA_VERSION = "1.1"


def _parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as err:
        raise MesaError(f"unparseable schema_version: {version!r}") from err


def _migrate_1_0_to_1_1(sp: dict[str, Any]) -> None:
    """The 1.1 format is purely additive (device inheritance scope,
    capability_semantics.control_mode typing), so the step restamps the
    version and transforms nothing."""


# source version, then (next version, transformation). Steps chain until the
# target is reached; a version with no registered step, and any downgrade,
# has no path and raises.
_MIGRATIONS: dict[tuple[int, ...], tuple[str, Callable[[dict[str, Any]], None]]] = {
    (1, 0): ("1.1", _migrate_1_0_to_1_1),
}


def migrate_profile(
    profile: dict[str, Any], target_version: str = CURRENT_SCHEMA_VERSION
) -> dict[str, Any]:
    """Migrate a profile document to ``target_version``, returning a copy.

    The original document is never modified. Raises MesaError when no
    migration path exists.
    """
    migrated = copy.deepcopy(profile)
    sp = migrated.get("semantic_profile")
    if not isinstance(sp, dict):
        raise MesaError("document has no semantic_profile object to migrate")

    source_version = sp.get("schema_version", "1.0")
    source = _parse_version(str(source_version))
    target = _parse_version(target_version)

    if source == target:
        if "schema_version" not in sp:
            sp["schema_version"] = target_version
            logger.info(
                "migration: stamped missing schema_version as %s", target_version
            )
        return migrated

    while source != target:
        step = _MIGRATIONS.get(source)
        if step is None or _parse_version(step[0]) > target:
            raise MesaError(
                f"no migration path from schema version {source_version} to {target_version}"
            )
        next_version, transform = step
        transform(sp)
        sp["schema_version"] = next_version
        logger.info(
            "migration: applied schema version step %s to %s",
            ".".join(str(part) for part in source),
            next_version,
        )
        source = _parse_version(next_version)
    return migrated
