"""Profile store export and import (Module Proposal Section 4.12).

The archive is an envelope of canonical profile documents, the same JSON form
as ``mesa_profile.json``, the JSON Schema, and ``to_dict()``. That makes it
storage-backend-agnostic: any host that exposes its profiles through the
ProfileStore API can exchange archives with any other host, regardless of how
either stores profiles internally.

Export reads raw stored documents through the backend: full fidelity, no
validation, nothing silently dropped, so a backup is a backup. Import
validates every document and applies an explicit conflict policy, so a
corrupted or hostile archive cannot silently poison a store.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from custom_components.phoenix_mcp.mesa_core.exceptions import MesaError, MesaValidationError
from custom_components.phoenix_mcp.mesa_core.profile import SemanticProfile
from custom_components.phoenix_mcp.mesa_core.store import (
    _AREA_PREFIX,
    _DEPLOYMENT_DEFAULTS_KEY,
    _DEVICE_PREFIX,
    _DOMAIN_PREFIX,
    _INTEGRATION_PREFIX,
    ProfileStore,
)

# 1.1 added the devices section (MESA 1.1 device scope). Import accepts both
# versions; export always writes the current one, so an older mesa-core rejects
# a new archive outright instead of silently dropping the devices section.
ARCHIVE_FORMAT_VERSION = "1.1"
_ACCEPTED_FORMAT_VERSIONS = ("1.0", "1.1")

# (archive section, reserved-key prefix) in export order. Entities use the
# bare key. Unknown future reserved keys are not exported.
_SECTIONS = (
    ("domains", _DOMAIN_PREFIX),
    ("integrations", _INTEGRATION_PREFIX),
    ("areas", _AREA_PREFIX),
    ("devices", _DEVICE_PREFIX),
)


def _is_importable_key(key: Any) -> bool:
    """Whether an archive key may be written to the store.

    Each archive section addresses its own storage namespace and the setter
    applies that section's prefix, so a key carrying a reserved prefix of its
    own would land outside the section it was declared in.
    """
    return isinstance(key, str) and bool(key) and not key.startswith("__")


@dataclass
class ImportResult:
    """Outcome of an import: what landed, what was held back, and why."""

    imported: int = 0
    overwritten: int = 0
    skipped_existing: list[str] = field(default_factory=list)
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.invalid


def export_profiles(store: ProfileStore) -> dict[str, Any]:
    """Export every stored profile document into a portable archive dict.

    The caller serialises the result (``json.dumps``) wherever it wants: a
    download, a backup file, another deployment.
    """
    from custom_components.phoenix_mcp.mesa_core import __version__

    entities: dict[str, Any] = {}
    scoped: dict[str, dict[str, Any]] = {section: {} for section, _ in _SECTIONS}
    defaults: dict[str, Any] | None = None
    for key in store.backend.list_keys():
        doc = store.backend.read(key)
        if doc is None:
            continue
        if key == _DEPLOYMENT_DEFAULTS_KEY:
            defaults = doc
            continue
        for section, prefix in _SECTIONS:
            if key.startswith(prefix):
                scoped[section][key[len(prefix) :]] = doc
                break
        else:
            if not key.startswith("__"):
                entities[key] = doc

    archive: dict[str, Any] = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "mesa_core_version": __version__,
        "entities": entities,
        **scoped,
    }
    if defaults is not None:
        archive["deployment_defaults"] = defaults
    return {"mesa_export": archive}


def import_profiles(
    store: ProfileStore,
    archive: dict[str, Any],
    *,
    on_conflict: str = "skip",
) -> ImportResult:
    """Import an archive produced by :func:`export_profiles`.

    ``on_conflict`` decides what happens when a key already exists in the
    target store: ``"skip"`` (default) leaves the existing profile, or
    ``"overwrite"`` replaces it; ``"error"`` raises MesaError on first
    conflict, before anything else is written. Documents that fail validation
    are reported in ``ImportResult.invalid`` and never written.
    """
    if on_conflict not in ("skip", "overwrite", "error"):
        raise ValueError(f"invalid on_conflict: {on_conflict!r}")
    if not isinstance(archive, dict):
        raise MesaValidationError("not a mesa_export archive (archive must be an object)")
    inner = archive.get("mesa_export")
    if not isinstance(inner, dict):
        raise MesaValidationError("not a mesa_export archive (missing 'mesa_export' root)")
    fmt = inner.get("format_version")
    if fmt not in _ACCEPTED_FORMAT_VERSIONS:
        raise MesaValidationError(f"unsupported archive format_version: {fmt!r}")

    result = ImportResult()
    sections: list[tuple[str, str, Any]] = [("entities", "", store.set)]
    sections += [
        ("domains", _DOMAIN_PREFIX, store.set_domain_profile),
        ("integrations", _INTEGRATION_PREFIX, store.set_integration_profile),
        ("areas", _AREA_PREFIX, store.set_area_profile),
        ("devices", _DEVICE_PREFIX, store.set_device_profile),
    ]

    # Conflict scan first, so on_conflict="error" is all-or-nothing.
    if on_conflict == "error":
        for section, prefix, _setter in sections:
            docs = inner.get(section)
            if not isinstance(docs, dict):
                continue
            for key in docs:
                if not _is_importable_key(key):
                    continue
                if store.backend.read(f"{prefix}{key}") is not None:
                    raise MesaError(f"import conflict: {section} key {key!r} already exists")
        if "deployment_defaults" in inner and store.backend.read(
            _DEPLOYMENT_DEFAULTS_KEY
        ) is not None:
            raise MesaError("import conflict: deployment_defaults already exist")

    for section, prefix, setter in sections:
        # Absent is fine (an omitted section is empty); an explicit non-object,
        # including null, is a wrong type and is reported, not read as absent.
        if section not in inner:
            continue
        docs = inner[section]
        if not isinstance(docs, dict):
            result.invalid[section] = (
                f"section must be an object of key to profile, got {type(docs).__name__}"
            )
            continue
        for key, doc in docs.items():
            label = f"{section}:{key}"
            if not _is_importable_key(key):
                # Reserved keys address the domain/integration/area/device/
                # defaults namespaces. An entity entry named "__domain__:lock" would
                # otherwise be written straight through store.set as a
                # domain-wide policy the archive never declared as one.
                result.invalid[label] = (
                    f"illegal key {key!r}: '__'-prefixed keys are reserved for the "
                    "scoped sections of the archive"
                )
                continue
            try:
                profile = SemanticProfile.from_dict(key, doc)
            except MesaValidationError as err:
                result.invalid[label] = str(err)
                continue
            exists = store.backend.read(f"{prefix}{key}") is not None
            if exists and on_conflict == "skip":
                result.skipped_existing.append(label)
                continue
            setter(key, profile)
            if exists:
                result.overwritten += 1
            else:
                result.imported += 1

    if "deployment_defaults" in inner:
        defaults = inner["deployment_defaults"]
        if not isinstance(defaults, dict):
            # Quarantined, not skipped: an explicit non-object (including null)
            # is a wrong type, and silently dropping an operator's defaults
            # leaves the store running on a policy floor nobody chose.
            result.invalid["deployment_defaults"] = (
                f"must be an object, got {type(defaults).__name__}"
            )
            return result
        exists = store.backend.read(_DEPLOYMENT_DEFAULTS_KEY) is not None
        if exists and on_conflict == "skip":
            result.skipped_existing.append("deployment_defaults")
        else:
            try:
                store.set_deployment_defaults(defaults)
            except (MesaValidationError, ValueError, TypeError, KeyError, AttributeError) as err:
                # DeploymentDefaults.from_dict reports malformed nested values as
                # MesaValidationError, which is not a ValueError; a malformed
                # defaults object must be quarantined here, not abort the import.
                result.invalid["deployment_defaults"] = str(err)
            else:
                if exists:
                    result.overwritten += 1
                else:
                    result.imported += 1

    return result


async def aexport_profiles(store: ProfileStore) -> dict[str, Any]:
    return await asyncio.to_thread(export_profiles, store)


async def aimport_profiles(
    store: ProfileStore, archive: dict[str, Any], *, on_conflict: str = "skip"
) -> ImportResult:
    return await asyncio.to_thread(
        lambda: import_profiles(store, archive, on_conflict=on_conflict)
    )
