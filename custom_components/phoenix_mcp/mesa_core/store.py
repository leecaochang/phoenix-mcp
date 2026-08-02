"""ProfileStore: the central profile storage interface (Module Proposal 4.2).

Key scheme: entity profiles are stored under their entity ID. Domain-,
integration-, area-, and device-level profiles and deployment defaults use
reserved ``__``-prefixed keys, which never collide with HA entity IDs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from custom_components.phoenix_mcp.mesa_core import validation
from custom_components.phoenix_mcp.mesa_core.backends import StorageBackend
from custom_components.phoenix_mcp.mesa_core.exceptions import InvalidCursorError, MesaValidationError
from custom_components.phoenix_mcp.mesa_core.profile import (
    ORIGIN_AUTHORITY,
    ControlMode,
    MetadataOrigin,
    SemanticProfile,
    TriggersAutomations,
    baseline_triggers_automations,
)

if TYPE_CHECKING:
    from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver, ProfileExplanation

_DEPLOYMENT_DEFAULTS_KEY = "__deployment_defaults__"
_DOMAIN_PREFIX = "__domain__:"
_INTEGRATION_PREFIX = "__integration__:"
_AREA_PREFIX = "__area__:"
_DEVICE_PREFIX = "__device__:"

MAX_PAGE_SIZE = 200


def _parse_enum[EnumT: StrEnum](enum: type[EnumT], value: Any, where: str) -> EnumT:
    """Parse an enum value into a MesaValidationError rather than a raw ValueError."""
    try:
        return enum(value)
    except ValueError as err:
        valid = [member.value for member in enum]
        raise MesaValidationError(f"{where}: invalid value {value!r} (valid: {valid})") from err


@dataclass
class DeploymentDefaults:
    """Operator-configured defaults for unprofiled entities (Spec 5.8)."""

    default_control_mode: ControlMode = ControlMode.CONFIRM
    triggers_automations_domains: list[str] = field(default_factory=list)
    domain_overrides: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeploymentDefaults:
        """Parse and validate operator defaults.

        Nested overrides are validated here rather than at use: they are read
        only for entities with no profile at any level, so a malformed override
        would sit inert until the one case it exists to cover and then decide
        that entity's policy from a crash or a silent wrong default.
        """
        if not isinstance(data, dict):
            raise MesaValidationError(
                f"deployment_defaults must be an object, got {type(data).__name__}"
            )
        inner = data.get("deployment_defaults", data)
        if not isinstance(inner, dict):
            raise MesaValidationError(
                f"deployment_defaults must be an object, got {type(inner).__name__}"
            )
        # Presence, not truthiness: `x or []` would read a falsy wrong type
        # (0, "", false, {}) as an empty list and accept it silently.
        domains = inner.get("triggers_automations_domains")
        domains = [] if domains is None else domains
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            raise MesaValidationError(
                "deployment_defaults.triggers_automations_domains must be an array of strings"
            )
        overrides = inner.get("domain_overrides")
        overrides = {} if overrides is None else overrides
        if not isinstance(overrides, dict):
            raise MesaValidationError("deployment_defaults.domain_overrides must be an object")
        for domain, override in overrides.items():
            where = f"deployment_defaults.domain_overrides.{domain}"
            if not isinstance(override, dict):
                raise MesaValidationError(
                    f"{where} must be an object, got {type(override).__name__}"
                )
            if "control_mode" in override:
                _parse_enum(ControlMode, override["control_mode"], f"{where}.control_mode")
            if "triggers_automations" in override:
                _parse_enum(
                    TriggersAutomations,
                    override["triggers_automations"],
                    f"{where}.triggers_automations",
                )
        return cls(
            default_control_mode=_parse_enum(
                ControlMode,
                inner.get("default_control_mode", "confirm"),
                "deployment_defaults.default_control_mode",
            ),
            triggers_automations_domains=list(domains),
            domain_overrides={k: dict(v) for k, v in overrides.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_defaults": {
                "default_control_mode": self.default_control_mode.value,
                "triggers_automations_domains": list(self.triggers_automations_domains),
                "domain_overrides": dict(self.domain_overrides),
            }
        }

    def control_mode_for(self, domain: str) -> ControlMode:
        override = self.domain_overrides.get(domain, {})
        if "control_mode" in override:
            return ControlMode(override["control_mode"])
        return self.default_control_mode

    def triggers_for(self, domain: str) -> TriggersAutomations:
        override = self.domain_overrides.get(domain, {})
        if "triggers_automations" in override:
            return TriggersAutomations(override["triggers_automations"])
        if domain in self.triggers_automations_domains:
            return TriggersAutomations.LIKELY
        return baseline_triggers_automations(domain)


@dataclass
class QueryRow:
    """One query match: the stored profile and its resolved effective profile.

    ``effective`` is populated for every returned row; it is None only for
    intermediate matches that were filtered out before the page was resolved.
    """

    entity_id: str
    stored: SemanticProfile
    effective: SemanticProfile | None = None


@dataclass
class ProfileQueryResult:
    rows: list[QueryRow]
    total_matched: int
    has_more: bool
    next_cursor: str | None
    limit: int = 50
    warnings: list[str] = field(default_factory=list)

    @property
    def profiles(self) -> list[SemanticProfile]:
        """The effective profile of each returned row (stored if unresolved)."""
        return [row.effective or row.stored for row in self.rows]


def _encode_cursor(offset: int, fingerprint: str) -> str:
    payload = json.dumps({"o": offset, "f": fingerprint})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str, fingerprint: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        offset = int(payload["o"])
        cursor_fp = str(payload["f"])
    except Exception as err:
        raise InvalidCursorError(f"malformed cursor: {cursor!r}") from err
    if cursor_fp != fingerprint:
        # Profile data changed since the cursor was issued (Spec 9.2): restart pagination.
        raise InvalidCursorError("cursor invalidated by profile changes")
    if offset < 0:
        raise InvalidCursorError("malformed cursor offset")
    return offset


class ProfileStore:
    """Read/write MESA profiles keyed by entity, domain, integration, area, or device IDs."""

    def __init__(
        self,
        backend: StorageBackend,
        *,
        get_entity_area: Callable[[str], str | None] | None = None,
        get_entity_integration: Callable[[str], str | None] | None = None,
        get_entity_device: Callable[[str], str | None] | None = None,
    ) -> None:
        self.backend = backend
        self.get_entity_area = get_entity_area
        self.get_entity_integration = get_entity_integration
        self.get_entity_device = get_entity_device
        self._resolver: InheritanceResolver | None = None

    # -- entity profiles ------------------------------------------------------

    @staticmethod
    def _stamped_doc(profile: SemanticProfile, entity_id: str) -> dict[str, Any]:
        """Serialise a profile, stamping metadata_origin when the document lacks it.

        Level 2 implementations MUST include metadata_origin in everything they
        write (Spec Section 2); without this, a location-defaulted origin (e.g.
        developer, from a sidecar import) would degrade to unknown on reload.

        The serialised document is validated before it is returned: the data
        model is mutable and the release supports read-modify-write, so a
        mutation that makes the profile malformed (e.g. changing the source to
        inferred_ai without adding confidence/generated_at) must fail the write
        rather than poison the store and raise on the next read.
        """
        try:
            doc = profile.to_dict()
        except (AttributeError, TypeError, ValueError) as err:
            # The typed fields are mutable, so a field can be set to a value that
            # does not serialise (e.g. control_mode assigned a raw string, whose
            # .value then fails). Report the write as malformed rather than let a
            # raw AttributeError escape the promised MesaValidationError contract.
            raise MesaValidationError(
                f"profile for {entity_id} could not be serialised: {err}"
            ) from err
        sp = doc.setdefault("semantic_profile", {})
        if "metadata_origin" not in sp:
            sp["metadata_origin"] = {"source": profile.metadata.source.value}
        validation.validate_or_raise(doc, entity_id)
        return doc

    def get(self, entity_id: str) -> SemanticProfile | None:
        data = self.backend.read(entity_id)
        if data is None:
            return None
        return SemanticProfile.from_dict(entity_id, data)

    def set(self, entity_id: str, profile: SemanticProfile) -> None:
        self.backend.write(entity_id, self._stamped_doc(profile, entity_id))

    def delete(self, entity_id: str) -> None:
        self.backend.delete(entity_id)

    def set_many(self, profiles: dict[str, SemanticProfile]) -> None:
        for entity_id, profile in profiles.items():
            self.set(entity_id, profile)

    def delete_many(self, entity_ids: list[str]) -> None:
        for entity_id in entity_ids:
            self.delete(entity_id)

    # -- domain / integration / area profiles ---------------------------------

    def get_domain_profile(self, domain: str) -> SemanticProfile | None:
        data = self.backend.read(f"{_DOMAIN_PREFIX}{domain}")
        if data is None:
            return None
        profile = SemanticProfile.from_dict(domain, data)
        profile.inheritance_scope = "domain"
        return profile

    def set_domain_profile(self, domain: str, profile: SemanticProfile) -> None:
        self.backend.write(f"{_DOMAIN_PREFIX}{domain}", self._stamped_doc(profile, domain))

    def delete_domain_profile(self, domain: str) -> None:
        self.backend.delete(f"{_DOMAIN_PREFIX}{domain}")

    def get_integration_profile(self, integration: str) -> SemanticProfile | None:
        data = self.backend.read(f"{_INTEGRATION_PREFIX}{integration}")
        if data is None:
            return None
        profile = SemanticProfile.from_dict(integration, data)
        profile.inheritance_scope = "integration"
        return profile

    def set_integration_profile(self, integration: str, profile: SemanticProfile) -> None:
        self.backend.write(
            f"{_INTEGRATION_PREFIX}{integration}", self._stamped_doc(profile, integration)
        )

    def delete_integration_profile(self, integration: str) -> None:
        self.backend.delete(f"{_INTEGRATION_PREFIX}{integration}")

    def get_area_profile(self, area_id: str) -> SemanticProfile | None:
        data = self.backend.read(f"{_AREA_PREFIX}{area_id}")
        if data is None:
            return None
        profile = SemanticProfile.from_dict(area_id, data)
        profile.inheritance_scope = "area"
        return profile

    def set_area_profile(self, area_id: str, profile: SemanticProfile) -> None:
        self.backend.write(f"{_AREA_PREFIX}{area_id}", self._stamped_doc(profile, area_id))

    def delete_area_profile(self, area_id: str) -> None:
        self.backend.delete(f"{_AREA_PREFIX}{area_id}")

    def get_device_profile(self, device_id: str) -> SemanticProfile | None:
        data = self.backend.read(f"{_DEVICE_PREFIX}{device_id}")
        if data is None:
            return None
        profile = SemanticProfile.from_dict(device_id, data)
        profile.inheritance_scope = "device"
        return profile

    def set_device_profile(self, device_id: str, profile: SemanticProfile) -> None:
        self.backend.write(f"{_DEVICE_PREFIX}{device_id}", self._stamped_doc(profile, device_id))

    def delete_device_profile(self, device_id: str) -> None:
        self.backend.delete(f"{_DEVICE_PREFIX}{device_id}")

    # -- deployment defaults ----------------------------------------------------

    def get_deployment_defaults(self) -> DeploymentDefaults | None:
        data = self.backend.read(_DEPLOYMENT_DEFAULTS_KEY)
        return DeploymentDefaults.from_dict(data) if data is not None else None

    def set_deployment_defaults(self, defaults: DeploymentDefaults | dict[str, Any]) -> None:
        # A passed-in dataclass is revalidated too, not only a dict: it is
        # mutable, so its fields may have been set to invalid shapes since
        # construction (e.g. domain_overrides mutated to a non-object value),
        # which would otherwise write clean and fail later at resolution.
        source = defaults if isinstance(defaults, dict) else self._defaults_to_dict(defaults)
        validated = DeploymentDefaults.from_dict(source)
        self.backend.write(_DEPLOYMENT_DEFAULTS_KEY, validated.to_dict())

    @staticmethod
    def _defaults_to_dict(defaults: DeploymentDefaults) -> dict[str, Any]:
        try:
            return defaults.to_dict()
        except (AttributeError, TypeError, ValueError) as err:
            raise MesaValidationError(
                f"deployment_defaults could not be serialised: {err}"
            ) from err

    # -- queries ----------------------------------------------------------------

    def entity_keys(self) -> list[str]:
        return [k for k in self.backend.list_keys() if not k.startswith("__")]

    def domain_keys(self) -> list[str]:
        """Domain names that have a domain-level profile stored."""
        return [k[len(_DOMAIN_PREFIX) :] for k in self.backend.list_keys(_DOMAIN_PREFIX)]

    def integration_keys(self) -> list[str]:
        """Integration names that have an integration-level profile stored."""
        return [k[len(_INTEGRATION_PREFIX) :] for k in self.backend.list_keys(_INTEGRATION_PREFIX)]

    def area_keys(self) -> list[str]:
        """Area IDs that have an area-level profile stored."""
        return [k[len(_AREA_PREFIX) :] for k in self.backend.list_keys(_AREA_PREFIX)]

    def device_keys(self) -> list[str]:
        """Device IDs that have a device-level profile stored."""
        return [k[len(_DEVICE_PREFIX) :] for k in self.backend.list_keys(_DEVICE_PREFIX)]

    def find_orphans(
        self,
        known_entity_ids: Iterable[str],
        *,
        known_domains: Iterable[str] | None = None,
        known_integrations: Iterable[str] | None = None,
        known_areas: Iterable[str] | None = None,
        known_devices: Iterable[str] | None = None,
    ) -> list[str]:
        """Stored profile keys absent from the deployment's registries.

        Hosts SHOULD run this at startup and on entity registry updates and
        surface results to the operator (Spec 5.5, entity renames). Each
        keyword registry, when supplied, extends the check to that scope's
        profiles; scoped orphans are returned as their full reserved key
        (``__device__:abc123``) so callers can tell the scopes apart. An
        omitted keyword leaves that scope unchecked.
        """
        known = set(known_entity_ids)
        orphans = [k for k in self.entity_keys() if k not in known]
        for registry, keys, prefix in (
            (known_domains, self.domain_keys, _DOMAIN_PREFIX),
            (known_integrations, self.integration_keys, _INTEGRATION_PREFIX),
            (known_areas, self.area_keys, _AREA_PREFIX),
            (known_devices, self.device_keys, _DEVICE_PREFIX),
        ):
            if registry is None:
                continue
            known_scope = set(registry)
            orphans.extend(f"{prefix}{k}" for k in keys() if k not in known_scope)
        return orphans

    def _fingerprint(self) -> str:
        """Identify the data a cursor was issued against (Spec 9.2).

        Covers profile content, not just the key set: an edit that changes what
        a filter matches, or the order of a page, reshuffles the results a
        stored offset points into, and keys alone cannot see that. Covers the
        inherited layers too, not only entity documents: query filters and the
        returned page depend on the effective profile, so a domain, integration,
        area, device, or deployment_defaults change can move a row across the page
        boundary or in and out of the match set without any entity document
        changing.
        """
        digest = hashlib.sha256()
        for key in sorted(self.backend.list_keys()):
            digest.update(key.encode())
            doc = self.backend.read(key)
            digest.update(json.dumps(doc, sort_keys=True, default=str).encode())
        return digest.hexdigest()[:16]

    def query(
        self,
        *,
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
        areas: list[str] | None = None,
        devices: list[str] | None = None,
        integrations: list[str] | None = None,
        intents: list[str] | None = None,
        include_inferred: bool = False,
        origin: str | None = None,
        min_origin_authority: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        resolver: InheritanceResolver | None = None,
    ) -> ProfileQueryResult:
        """Query entity profiles with filtering and pagination (Spec 9.2).

        Tag and intent filters match the effective (resolved) tag set per Spec
        9.2, so resolution is used; the resolver defaults to this store's. Cheap
        attribute filters (domain, origin, area, device, integration) run first
        and resolution is deferred to the survivors, or to just the returned
        page when neither a tag nor an intent filter is present.

        ``include_inferred=False`` excludes ``inferred_ai`` and ``unknown``
        origins (Spec 5.4 Rule 5) unless an explicit ``origin`` filter asks for
        them. ``origin`` is a Python-level filter deliberately not exposed over
        MCP: the wire schema offers ``min_origin_authority`` only (Spec 9.2),
        because an exact-origin filter can re-admit inferred profiles past the
        ``include_inferred`` opt-in gate, which hosts must mediate themselves.
        Raises ValueError for malformed filter arguments and InvalidCursorError
        for a stale or malformed cursor.
        """
        if tags_match not in ("any", "all"):
            raise ValueError(f"invalid tags_match: {tags_match!r}")
        origin_filter = MetadataOrigin(origin) if origin is not None else None
        min_authority: int | None = None
        if min_origin_authority is not None:
            try:
                min_authority = ORIGIN_AUTHORITY[MetadataOrigin(min_origin_authority)]
            except ValueError as err:
                raise ValueError(
                    f"invalid min_origin_authority: {min_origin_authority!r}"
                ) from err

        resolver = resolver or self._default_resolver()
        get_area = resolver.get_entity_area
        if areas and get_area is None:
            raise ValueError("areas filter requires the get_entity_area callback")
        get_device = resolver.get_entity_device
        if devices and get_device is None:
            raise ValueError("devices filter requires the get_entity_device callback")
        # No domain fallback here, unlike resolution: a fallback filter would
        # silently change which rows match, so an absent mapping fails loudly.
        get_integration = resolver.get_entity_integration
        if integrations and get_integration is None:
            raise ValueError("integrations filter requires the get_entity_integration callback")

        limit = max(1, min(limit, MAX_PAGE_SIZE))
        filter_needs_resolution = bool(tags or intents)
        warnings: list[str] = []
        matched: list[QueryRow] = []
        for key in self.entity_keys():
            try:
                stored = self.get(key)
            except MesaValidationError as err:
                warnings.append(f"skipped malformed profile {key}: {err}")
                continue
            if stored is None:
                continue
            if domains and stored.domain not in domains:
                continue
            source = stored.metadata.source
            if origin_filter is not None:
                if source != origin_filter:
                    continue
            elif not include_inferred and source in (
                MetadataOrigin.INFERRED_AI,
                MetadataOrigin.UNKNOWN,
            ):
                continue
            if min_authority is not None and ORIGIN_AUTHORITY[source] < min_authority:
                continue
            if areas and get_area is not None and get_area(key) not in areas:
                continue
            if devices and get_device is not None and get_device(key) not in devices:
                continue
            if (
                integrations
                and get_integration is not None
                and get_integration(key) not in integrations
            ):
                continue
            effective: SemanticProfile | None = None
            if filter_needs_resolution:
                effective = resolver.resolve(key, entity_profile=stored)
                effective_tags = set(effective.semantic_tags)
                if tags:
                    if tags_match == "all" and not set(tags) <= effective_tags:
                        continue
                    if tags_match == "any" and not set(tags) & effective_tags:
                        continue
                if intents:
                    # semantic_routing is an unmodelled field carried onto the
                    # effective profile, so intent_tags inherited from a domain,
                    # integration, or area profile are matched too, consistent
                    # with what get_effective() exposes (Spec 9.2).
                    routing = (
                        effective.raw.get("semantic_profile", {}).get("semantic_routing", {}) or {}
                    )
                    intent_tags = effective_tags | set(routing.get("intent_tags", []))
                    if not set(intents) & intent_tags:
                        continue
            matched.append(QueryRow(entity_id=key, stored=stored, effective=effective))

        matched.sort(key=lambda row: row.entity_id)
        fingerprint = self._fingerprint()
        offset = _decode_cursor(cursor, fingerprint) if cursor else 0
        page = matched[offset : offset + limit]
        has_more = offset + limit < len(matched)
        # OPT: resolve only the returned page when filtering did not already,
        # reusing the stored profile each row already holds.
        for row in page:
            if row.effective is None:
                row.effective = resolver.resolve(row.entity_id, entity_profile=row.stored)
        next_cursor = _encode_cursor(offset + limit, fingerprint) if has_more else None
        return ProfileQueryResult(
            rows=page,
            total_matched=len(matched),
            has_more=has_more,
            next_cursor=next_cursor,
            limit=limit,
            warnings=warnings,
        )

    # -- effective profiles -------------------------------------------------------

    def attach_resolver(self, resolver: InheritanceResolver) -> None:
        self._resolver = resolver

    def _default_resolver(self) -> InheritanceResolver:
        if self._resolver is None:
            from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver

            self._resolver = InheritanceResolver(store=self)
        return self._resolver

    def get_effective(self, entity_id: str) -> SemanticProfile:
        """Resolve the effective profile (inheritance + conflict rules, Spec 5.6/5.7).

        Without an attached resolver, a default resolver over this store is used:
        domain inheritance derives from the entity ID prefix and area inheritance
        requires the host's ``get_entity_area`` callback.
        """
        return self._default_resolver().resolve(entity_id)

    def explain(self, entity_id: str) -> ProfileExplanation:
        """Per-field provenance of the effective profile (Spec 9.5).

        Delegates to the attached (or default) resolver's ``explain``.
        """
        return self._default_resolver().explain(entity_id)

    # -- async variants -------------------------------------------------------------

    async def aget(self, entity_id: str) -> SemanticProfile | None:
        return await asyncio.to_thread(self.get, entity_id)

    async def aset(self, entity_id: str, profile: SemanticProfile) -> None:
        await asyncio.to_thread(self.set, entity_id, profile)

    async def adelete(self, entity_id: str) -> None:
        await asyncio.to_thread(self.delete, entity_id)

    async def aget_domain_profile(self, domain: str) -> SemanticProfile | None:
        return await asyncio.to_thread(self.get_domain_profile, domain)

    async def aset_domain_profile(self, domain: str, profile: SemanticProfile) -> None:
        await asyncio.to_thread(self.set_domain_profile, domain, profile)

    async def adelete_domain_profile(self, domain: str) -> None:
        await asyncio.to_thread(self.delete_domain_profile, domain)

    async def aget_integration_profile(self, integration: str) -> SemanticProfile | None:
        return await asyncio.to_thread(self.get_integration_profile, integration)

    async def aset_integration_profile(
        self, integration: str, profile: SemanticProfile
    ) -> None:
        await asyncio.to_thread(self.set_integration_profile, integration, profile)

    async def adelete_integration_profile(self, integration: str) -> None:
        await asyncio.to_thread(self.delete_integration_profile, integration)

    async def aget_area_profile(self, area_id: str) -> SemanticProfile | None:
        return await asyncio.to_thread(self.get_area_profile, area_id)

    async def aset_area_profile(self, area_id: str, profile: SemanticProfile) -> None:
        await asyncio.to_thread(self.set_area_profile, area_id, profile)

    async def adelete_area_profile(self, area_id: str) -> None:
        await asyncio.to_thread(self.delete_area_profile, area_id)

    async def aget_device_profile(self, device_id: str) -> SemanticProfile | None:
        return await asyncio.to_thread(self.get_device_profile, device_id)

    async def aset_device_profile(self, device_id: str, profile: SemanticProfile) -> None:
        await asyncio.to_thread(self.set_device_profile, device_id, profile)

    async def adelete_device_profile(self, device_id: str) -> None:
        await asyncio.to_thread(self.delete_device_profile, device_id)

    async def aget_deployment_defaults(self) -> DeploymentDefaults | None:
        return await asyncio.to_thread(self.get_deployment_defaults)

    async def aset_deployment_defaults(
        self, defaults: DeploymentDefaults | dict[str, Any]
    ) -> None:
        await asyncio.to_thread(self.set_deployment_defaults, defaults)

    async def aset_many(self, profiles: dict[str, SemanticProfile]) -> None:
        await asyncio.to_thread(self.set_many, profiles)

    async def adelete_many(self, entity_ids: list[str]) -> None:
        await asyncio.to_thread(self.delete_many, entity_ids)

    async def aentity_keys(self) -> list[str]:
        return await asyncio.to_thread(self.entity_keys)

    async def adomain_keys(self) -> list[str]:
        return await asyncio.to_thread(self.domain_keys)

    async def aintegration_keys(self) -> list[str]:
        return await asyncio.to_thread(self.integration_keys)

    async def aarea_keys(self) -> list[str]:
        return await asyncio.to_thread(self.area_keys)

    async def adevice_keys(self) -> list[str]:
        return await asyncio.to_thread(self.device_keys)

    async def aquery(self, **kwargs: Any) -> ProfileQueryResult:
        return await asyncio.to_thread(lambda: self.query(**kwargs))

    async def aget_effective(self, entity_id: str) -> SemanticProfile:
        return await asyncio.to_thread(self.get_effective, entity_id)

    async def aexplain(self, entity_id: str) -> ProfileExplanation:
        return await asyncio.to_thread(self.explain, entity_id)

    async def afind_orphans(
        self,
        known_entity_ids: Iterable[str],
        *,
        known_domains: Iterable[str] | None = None,
        known_integrations: Iterable[str] | None = None,
        known_areas: Iterable[str] | None = None,
        known_devices: Iterable[str] | None = None,
    ) -> list[str]:
        return await asyncio.to_thread(
            lambda: self.find_orphans(
                known_entity_ids,
                known_domains=known_domains,
                known_integrations=known_integrations,
                known_areas=known_areas,
                known_devices=known_devices,
            )
        )
