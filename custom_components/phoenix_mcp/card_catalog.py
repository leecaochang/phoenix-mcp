"""Dashboard card catalog: which Lovelace cards this instance can actually render.

Phoenix MCP's dashboard write tools accept any card `type` whose shape is a dict
with a non-empty string, so an agent authoring a dashboard has no way to learn
which custom cards are installed and has to guess. This module holds the answer:
a cached catalog of every card type the instance can render, with a one-line
description, a documentation URL, and a worked stub config per card.

WHY THIS IS HARVESTED IN A BROWSER RATHER THAN READ OFF DISK. A card announces
itself by pushing onto `window.customCards`, which is the registry Home
Assistant's own card picker reads. That push happens at runtime, and much of a
real-world plugin set yields NOTHING to a static parse: some build their type
strings by concatenation, so no literal ever appears in the bundle, and others
never register in the picker at all. A browser harvest therefore finds several
times as many cards as parsing the JS on disk does, and finds descriptions and
worked example configs that a static parse cannot recover at all. The gap is not
an optimisation, it is the difference between a usable catalog and a misleading
one.

The frontend harvester posts here through the admin API. Nothing in this module
trusts that payload: it arrives from a browser, is assembled from third-party
card code, and every field is coerced, bounded, and dropped on doubt.

A NEVER-HARVESTED CATALOG IS NOT AN EMPTY ONE. `harvested_at is None` means no
browser has reported yet, and callers must say so rather than report zero cards.
An agent told "you have no custom cards" avoids every custom card on the system,
which is a worse failure than admitting the catalog is not ready.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .const import (
    MAX_CARD_CATALOG_ENTRIES,
    MAX_CARD_CATALOG_FAILURES,
    MAX_CARD_STUB_CONFIG_BYTES,
)

_LOGGER = logging.getLogger(__name__)

# Where an entry came from. "picker" is a window.customCards registration (the
# card advertises itself, so name/description are the author's own). "element"
# is a custom element that resolved by name without a picker registration:
# stack-in-card and config-template-card deliberately skip the picker so they do
# not clutter it, but they are real, widely used cards, so a catalog that listed
# only picker cards would tell an agent they do not exist.
CARD_SOURCES = frozenset({"picker", "element"})


@dataclass
class CardEntry:
    """One renderable card type.

    `available` is the load-bearing field. A card can register a picker entry
    while its custom element never defines, so "advertised" and "usable" are
    genuinely different states, and an agent that conflates them authors a card
    that renders as an error.
    """

    type: str
    name: str | None = None
    description: str | None = None
    documentation_url: str | None = None
    preview: bool = False
    available: bool = True
    has_visual_editor: bool = False
    stub_config: dict | None = None
    source: str = "picker"


@dataclass
class CardCatalog:
    """The harvested catalog plus the provenance needed to judge its freshness."""

    entries: list[CardEntry] = field(default_factory=list)
    harvested_at: str | None = None
    resource_count: int = 0
    # Resources that failed to import during the harvest. Surfaced to the admin
    # as a cleanup hint: a registered resource whose file is gone is a real and
    # common condition, typically a stale duplicate of a card that has since
    # moved to a different path.
    failed_imports: list[dict] = field(default_factory=list)

    @property
    def harvested(self) -> bool:
        """True once a browser has reported, even if it found nothing."""
        return self.harvested_at is not None


def _clean_str(value: object, limit: int = 300) -> str | None:
    """Coerce a harvested string, or None for anything else.

    Degrades a wrong-shaped value to absent rather than stringifying it, the
    helpers.str_arg rule: str(True) invents content nobody authored.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] or None


def _clean_stub(value: object) -> dict | None:
    """Keep a stub config only if it is a dict that serializes within bounds.

    Stub configs come from third-party card code via getStubConfig, so the shape
    is whatever that card returned. Anything non-dict, unserializable, or over
    the cap is dropped: the catalog degrades to "no example" rather than
    refusing the whole entry, since the card itself is still worth knowing about.
    """
    if not isinstance(value, dict):
        return None
    try:
        # No `default=` fallback on purpose: this doubles as the check that the
        # value can round-trip to .storage. Coercing an unserializable value here
        # would measure a stringified form while storing the original, which then
        # fails at Store.async_save instead of at the boundary that vetted it.
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(encoded) > MAX_CARD_STUB_CONFIG_BYTES:
        return None
    return value


def sanitize_entries(raw: object) -> list[CardEntry]:
    """Build CardEntry rows from an untrusted harvest payload.

    Drops anything without a usable `type`, de-duplicates by type keeping the
    first occurrence, and caps the total. Never raises: a malformed payload
    yields fewer rows, never an error, because a partial catalog is still useful
    and a harvest failure must not break the admin request that carried it.
    """
    if not isinstance(raw, list):
        return []
    entries: list[CardEntry] = []
    seen: set[str] = set()
    for item in raw:
        if len(entries) >= MAX_CARD_CATALOG_ENTRIES:
            break
        if not isinstance(item, dict):
            continue
        card_type = _clean_str(item.get("type"), limit=120)
        if not card_type or card_type in seen:
            continue
        seen.add(card_type)
        raw_source = item.get("source")
        source = raw_source if isinstance(raw_source, str) and raw_source in CARD_SOURCES else "picker"
        entries.append(
            CardEntry(
                type=card_type,
                name=_clean_str(item.get("name")),
                description=_clean_str(item.get("description"), limit=500),
                documentation_url=_clean_str(item.get("documentation_url"), limit=500),
                preview=bool(item.get("preview")),
                available=bool(item.get("available", True)),
                has_visual_editor=bool(item.get("has_visual_editor")),
                stub_config=_clean_stub(item.get("stub_config")),
                source=source,
            )
        )
    return entries


def sanitize_failures(raw: object) -> list[dict]:
    """Build the failed-import list from an untrusted harvest payload."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:MAX_CARD_CATALOG_FAILURES]:
        if not isinstance(item, dict):
            continue
        url = _clean_str(item.get("url"), limit=500)
        if not url:
            continue
        out.append({"url": url, "error": _clean_str(item.get("error"), limit=200) or ""})
    return out


class CardCatalogStore:
    """Persisted card catalog, replaced wholesale by each harvest.

    Wholesale replacement rather than a merge is deliberate: a card removed from
    the instance must disappear from the catalog, and a merge would keep
    recommending it forever. The harvest is a full snapshot of what one browser
    could see, so it is the complete answer or it is not used.

    A None store keeps the catalog in memory only, matching VersionStore.
    """

    def __init__(self, store: Store | None = None) -> None:
        self._catalog = CardCatalog()
        self._store = store

    @property
    def catalog(self) -> CardCatalog:
        """The current catalog. Never None; check `.harvested` before reporting."""
        return self._catalog

    async def async_load(self) -> None:
        """Load the cached catalog. A corrupt or absent doc reads as never-harvested."""
        if self._store is None:
            return
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a bad cache must never block setup
            _LOGGER.exception("Could not load the card catalog; treating it as unharvested")
            return
        if not isinstance(data, dict):
            return
        self._catalog = CardCatalog(
            entries=sanitize_entries(data.get("entries")),
            harvested_at=_clean_str(data.get("harvested_at"), limit=64),
            resource_count=int(data.get("resource_count") or 0),
            failed_imports=sanitize_failures(data.get("failed_imports")),
        )

    async def async_save(self) -> None:
        """Persist the catalog."""
        if self._store is None:
            return
        await self._store.async_save(self.as_dict())

    async def async_replace(
        self, *, entries: object, resource_count: object, failed_imports: object
    ) -> CardCatalog:
        """Replace the catalog from a harvest payload and persist it."""
        count = 0
        if isinstance(resource_count, (int, float, str)):
            try:
                count = max(0, int(resource_count))
            except (TypeError, ValueError):
                count = 0
        self._catalog = CardCatalog(
            entries=sanitize_entries(entries),
            harvested_at=utcnow().isoformat(),
            resource_count=count,
            failed_imports=sanitize_failures(failed_imports),
        )
        await self.async_save()
        return self._catalog

    async def async_clear(self) -> None:
        """Reset to never-harvested. Used by the admin data wipe."""
        self._catalog = CardCatalog()
        await self.async_save()

    def get(self, card_type: str) -> CardEntry | None:
        """Look up one card by type, or None. Accepts a bare or `custom:`-prefixed type."""
        wanted = card_type[len("custom:"):] if card_type.startswith("custom:") else card_type
        for entry in self._catalog.entries:
            if entry.type == wanted:
                return entry
        return None

    def known_types(self) -> frozenset[str]:
        """Every card type the catalog knows, whether or not it is available."""
        return frozenset(e.type for e in self._catalog.entries)

    def as_dict(self) -> dict:
        """Serializable form, for the Store and the admin API."""
        return {
            "entries": [asdict(e) for e in self._catalog.entries],
            "harvested_at": self._catalog.harvested_at,
            "resource_count": self._catalog.resource_count,
            "failed_imports": list(self._catalog.failed_imports),
        }
