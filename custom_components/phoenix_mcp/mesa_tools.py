"""Token-scoped wrappers around the vendored mesa-core retrieval tools.

mesa-core ships the four retrieval handlers (mesa_query_profiles,
mesa_get_profile, mesa_explain_profile, mesa_get_caller_context). Phoenix MCP does not
reimplement them; it builds a per-request ScopedProfileStore so the handlers
only ever see entities the token may read, then delegates to mesa-core. This
keeps total_matched counts, pagination cursors, and the fingerprint all
scope-relative so there is no entity-enumeration oracle.

Out-of-scope and ghost entity lookups return the byte-identical mesa-core
not_found envelope, so an inaccessible entity is indistinguishable from a
nonexistent one.
"""

from __future__ import annotations

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er_mod

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .const import CAP_DENY, MAX_PREVIEW_ENTITY_IDS
from .helpers import effective_cap
from .mesa import async_semantic_moments, build_caller_context
from .mesa_core import InheritanceResolver, ProfileQueryResult, ProfileStore
from .mesa_core.mcp.schemas import TOOL_DESCRIPTIONS, TOOL_SCHEMAS
from .mesa_core.mcp.tools import MesaToolHandlers
from .policy_engine import Permission, resolve

if TYPE_CHECKING:
    from .data import PhoenixData
    from .mesa import MesaRuntime
    from .mesa_core import SemanticProfile
    from .token_store import TokenRecord

MESA_TOOL_NAMES = frozenset({
    "mesa_query_profiles",
    "mesa_get_profile",
    "mesa_explain_profile",
    "mesa_get_caller_context",
    "mesa_request_lease",
    "mesa_release_lease",
})

# Tools whose entity_id argument must be scope-checked before the handler runs.
_ENTITY_TARGETED = frozenset({"mesa_get_profile", "mesa_explain_profile"})

# The advisory-lease tools (mesa-core 1.1, Enrichment Section 21). The MCP
# transport is stateless (session_id is normally the per-request id), which would
# make every lease call a different session and release or refresh impossible, so
# these two run with session_id = token.id: a lease belongs to the TOKEN, stable
# across stateless calls, and mesa-core's session matching isolates tokens from
# each other's leases.
#
# WHAT A LEASE THEREFORE EXCLUDES, stated plainly because the shorthand "a lease
# holds an entity" claims more than this: it excludes OTHER TOKENS, not other
# callers of the same token. mesa-core treats same-session overlap as a REFRESH
# and grants it, so two agents authenticating with one token do not exclude each
# other. That matches the model Phoenix is built around, one token per connected
# agent, where the token IS the unit of work that can be interrupted; it stops
# matching the moment a token is shared, and the fix then is to key the session
# to the conversation rather than the credential.
_LEASE_TOOLS = frozenset({"mesa_request_lease", "mesa_release_lease"})

# All six mesa_* tools (four retrieval plus the two lease tools) are gated by
# this capability: profiles are configuration metadata, the same sensitivity
# class as get_config.
MESA_TOOLS_CAP = "cap_config_read"


# MCP tool annotations for the six mesa_* tools, in the same shape mcp_view uses
# (see its _TOOL_ANNOTATIONS block for the conventions). Kept here rather than
# imported because mcp_view imports this module, not the reverse. The four
# retrieval tools are pure reads; the lease tools mutate in-memory lease state, so
# they are not read-only, but a lease is advisory metadata, never a device action,
# so neither is destructive.
_MESA_TOOL_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "mesa_query_profiles": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "mesa_get_profile": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "mesa_explain_profile": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "mesa_get_caller_context": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Each request mints a new lease with its own expiry, so repeating is not a no-op.
    "mesa_request_lease": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "mesa_release_lease": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}


def mesa_tool_defs() -> list[dict[str, Any]]:
    """tools/list entries for the mesa_* tools, tagged with the gating cap.

    Built from the vendored MCP schemas/descriptions so Phoenix MCP never re-specifies
    them. mcp_view strips the 'cap' key before exposing the tool.
    """
    return [
        {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "cap": MESA_TOOLS_CAP,
            "inputSchema": TOOL_SCHEMAS[name],
            "annotations": _MESA_TOOL_ANNOTATIONS[name],
        }
        for name in ("mesa_query_profiles", "mesa_get_profile",
                     "mesa_explain_profile", "mesa_get_caller_context",
                     "mesa_request_lease", "mesa_release_lease")
    ]


def _not_found_envelope(entity_id: str) -> dict[str, Any]:
    """Replicate mesa_core.mcp.tools._error('not_found', ...) byte-for-byte."""
    return {
        "error": "not_found",
        "message": f"entity {entity_id!r} has no MESA profile at any level",
        "details": {},
    }


def _is_visible(entity_id: str, token: TokenRecord, hass: HomeAssistant) -> bool:
    """True when the token may read the entity (READ or WRITE)."""
    return resolve(entity_id, token, hass) in (Permission.READ, Permission.WRITE)


class ScopedProfileStore:
    """A read-only ProfileStore view filtered to one token's permission scope.

    Entity-level reads are hidden when the token cannot read the entity, so the
    delegated mesa-core handlers and resolver never observe out-of-scope
    entities. The scoped layers (device, area, integration, domain) and the
    deployment defaults pass through UNFILTERED, and that direction is
    load-bearing rather than a convenience.

    Every inheritance layer only ever tightens, and the device layer cannot
    loosen at all, since the loosening overrides are valid at entity scope only.
    So hiding a layer from a caller could only produce a MORE permissive
    effective profile than the operator authored: a permission filter whose
    effect is to relax policy is inverted. The axis to filter on is which
    ENTITIES a caller may address, never which layers contribute to one; once an
    entity is in scope, all five layers contribute to it.

    Resolution therefore discloses nothing new either, since the caller already
    receives the merged effective profile for entities it can see. The real
    confidentiality surface is ENUMERATION: device_keys / area_keys /
    integration_keys name objects a caller may have no entity access to, which is
    why none of them is exposed here. If one is ever needed, filter it to keys
    with at least one visible entity mapping to them rather than passing it on.
    """

    def __init__(self, inner, token: TokenRecord, hass: HomeAssistant) -> None:
        self._inner = inner
        self._token = token
        self._hass = hass

    def entity_keys(self) -> list[str]:
        return [k for k in self._inner.entity_keys() if _is_visible(k, self._token, self._hass)]

    def get(self, entity_id: str) -> SemanticProfile | None:
        if not _is_visible(entity_id, self._token, self._hass):
            return None
        return self._inner.get(entity_id)

    def get_domain_profile(self, domain: str) -> SemanticProfile | None:
        return self._inner.get_domain_profile(domain)

    def get_integration_profile(self, integration: str) -> SemanticProfile | None:
        return self._inner.get_integration_profile(integration)

    def get_area_profile(self, area_id: str) -> SemanticProfile | None:
        return self._inner.get_area_profile(area_id)

    def get_device_profile(self, device_id: str) -> SemanticProfile | None:
        return self._inner.get_device_profile(device_id)

    def get_deployment_defaults(self) -> Any:
        return self._inner.get_deployment_defaults()

    @property
    def get_entity_area(self) -> Any:
        return self._inner.get_entity_area

    @property
    def get_entity_integration(self) -> Any:
        return self._inner.get_entity_integration

    @property
    def get_entity_device(self) -> Any:
        return self._inner.get_entity_device

    def _fingerprint(self) -> str:
        digest = hashlib.sha256("|".join(self.entity_keys()).encode())
        return digest.hexdigest()[:16]

    def query(self, **kwargs: Any) -> ProfileQueryResult:
        """Run mesa-core's profile query over this scoped view.

        mesa-core's MesaToolHandlers delegates mesa_query_profiles to
        store.query(); driving the real ProfileStore.query with this scoped
        store as self keeps total_matched, the cursor, and the fingerprint all
        scope-relative (no entity-enumeration oracle). The handler always passes
        a scoped resolver; _default_resolver mirrors it for the no-resolver path.
        """
        return ProfileStore.query(self, **kwargs)  # type: ignore[arg-type]  # ScopedProfileStore duck-types ProfileStore's read surface by design; mesa_core is read-only so it cannot declare a Protocol

    def _default_resolver(self) -> InheritanceResolver:
        return InheritanceResolver(
            store=self,  # type: ignore[arg-type]  # ScopedProfileStore duck-types ProfileStore's read surface by design; mesa_core is read-only so it cannot declare a Protocol
            get_entity_area=self.get_entity_area,
            get_entity_integration=self.get_entity_integration,
            get_entity_device=self.get_entity_device,
        )


# Control modes surfaced as an explicit "do not operate / observe only" list in
# get_overview. Other authored modes (e.g. a confirm override) appear only in the
# counts, since confirm is the common domain default and listing every confirm
# entity would reintroduce the baseline noise a rollup must avoid.
_RESTRICTIVE_MODES = ("prohibited", "read_only")


def authored_restrictions(
    runtime: "MesaRuntime",
    token: "TokenRecord",
    hass: HomeAssistant,
    *,
    limit: int = MAX_PREVIEW_ENTITY_IDS,
) -> dict[str, Any]:
    """Scope-relative summary of ADMIN-AUTHORED entity profiles, for get_overview.

    Iterates only entities with a stored (operator-authored) profile via the
    scoped store, never baseline-derived modes, so the rollup reflects operator
    intent rather than domain defaults (the reason a naive control_mode count was
    originally omitted). ScopedProfileStore hides out-of-scope authored entities,
    so this is not an enumeration oracle.
    """
    scoped = ScopedProfileStore(runtime.store, token, hass)
    by_mode: dict[str, int] = {}
    restricted: list[dict[str, str]] = []
    truncated = 0
    for entity_id in sorted(scoped.entity_keys()):
        profile = scoped.get(entity_id)
        if profile is None:
            continue
        boundaries = profile.operational_boundaries
        cm = boundaries.control_mode
        mode = getattr(cm, "value", cm)
        if mode is None:
            continue
        by_mode[mode] = by_mode.get(mode, 0) + 1
        if mode in _RESTRICTIVE_MODES:
            if len(restricted) < limit:
                entry = {"entity_id": entity_id, "control_mode": mode}
                reason = getattr(boundaries, "control_reason", None)
                if reason:
                    entry["reason"] = reason
                restricted.append(entry)
            else:
                truncated += 1
    summary: dict[str, Any] = {
        "authored_profile_count": sum(by_mode.values()),
        "by_control_mode": dict(sorted(by_mode.items())),
        "restricted_entities": restricted,
    }
    if truncated:
        summary["restricted_truncated"] = truncated
    return summary


def _build_validity_context(hass: HomeAssistant) -> Callable[[str], dict[str, Any]]:
    """Return the per-entity context that decides whether a profile is still valid.

    A profile can declare what would invalidate it: an entity it depends on
    disappearing, or the integration or Home Assistant version moving past what
    it was authored against. Those triggers are only evaluable by the host, so
    without this callback an invalidated profile keeps reporting itself current.

    The callback is SYNCHRONOUS and runs once per entity, so the expensive part
    is hoisted: the registry union is computed once here and closed over, while
    the per-entity lookup is a single registry read.

    ``known_entity_ids`` must be COMPLETE, because anything missing from it reads
    as a removed entity and falsely invalidates. Neither source alone is
    complete: the registry omits entities with no unique ID, and the state
    machine omits disabled ones. It must also be REUSABLE rather than a
    generator, since the same object is read once per entity and a one-shot
    iterable would be drained by the first row, making every later row report
    real entities as removed.
    """
    registry = er_mod.async_get(hass)
    known = frozenset(registry.entities) | frozenset(hass.states.async_entity_ids())
    # The running Home Assistant version. Read from the package constant, which
    # is the only always-present source: Config carries no `version` attribute,
    # and reaching for one silently yields nothing.
    ha_version = str(HA_VERSION)

    def _validity_context(entity_id: str) -> dict[str, Any]:
        context: dict[str, Any] = {"known_entity_ids": known}
        if ha_version:
            context["ha_version"] = ha_version
        version = _integration_version(hass, entity_id)
        if version:
            context["integration_version"] = version
        return context

    return _validity_context


def _integration_version(hass: HomeAssistant, entity_id: str) -> str | None:
    """The version of the integration that created this entity, when it has one.

    Home Assistant requires ``version`` in a manifest only for CUSTOM
    integrations, so a core-integration entity has none and the key is omitted
    rather than filled with a stand-in: an invented value would either never
    match (invalidating every such profile) or always match (never invalidating
    one). A profile for a core entity pins ha_version instead.

    Fails quiet: this only decides whether one advisory field is present, and an
    unloaded or unknown integration is not worth failing a profile read over.
    """
    try:
        entry = er_mod.async_get(hass).async_get(entity_id)
        if entry is None or not entry.platform:
            return None
        from homeassistant.loader import async_get_loaded_integration  # noqa: PLC0415

        version = async_get_loaded_integration(hass, entry.platform).version
        return str(version) if version else None
    except Exception:  # noqa: BLE001 - an absent version is not an error
        return None


def _build_handlers(
    runtime: MesaRuntime,
    token: TokenRecord,
    hass: HomeAssistant,
    session_id: str,
    get_semantic_moments: Any = None,
) -> MesaToolHandlers:
    scoped = ScopedProfileStore(runtime.store, token, hass)
    resolver = InheritanceResolver(
        store=scoped,  # type: ignore[arg-type]  # ScopedProfileStore duck-types ProfileStore's read surface by design; mesa_core is read-only so it cannot declare a Protocol
        get_entity_area=runtime.store.get_entity_area,
        get_entity_integration=runtime.store.get_entity_integration,
        get_entity_device=runtime.store.get_entity_device,
    )
    ctx = build_caller_context(token, session_id)
    return MesaToolHandlers(
        store=scoped,  # type: ignore[arg-type]  # ScopedProfileStore duck-types ProfileStore's read surface by design; mesa_core is read-only so it cannot declare a Protocol
        resolver=resolver,
        caller_context_fn=lambda: ctx,
        lease_manager=runtime.lease_manager,
        get_semantic_moments=get_semantic_moments,
        get_validity_context=_build_validity_context(hass),
    )


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _tool_success(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def async_call_mesa_tool(
    tool_name: str,
    args: dict,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    session_id: str,
) -> tuple[dict, str, str]:
    """Dispatch a mesa_* retrieval tool with token scoping.

    Returns the standard Phoenix MCP tool tuple (result, outcome, resource).
    """
    if effective_cap(token, MESA_TOOLS_CAP) == CAP_DENY:
        return _tool_error("Forbidden."), "denied", tool_name

    runtime = data.mesa
    if runtime is None:
        return _tool_error("MESA is not available."), "denied", tool_name

    if tool_name in _LEASE_TOOLS:
        # Leases belong to the token across stateless calls (see _LEASE_TOOLS).
        session_id = token.id
        if tool_name == "mesa_request_lease":
            entities = args.get("entities")
            if isinstance(entities, list) and entities:
                # A lease is an intent to control, so it is scoped to entities
                # the token can WRITE. Out-of-scope, ghost, and blocked-domain
                # entities are silently dropped (indistinguishable from one
                # another, no enumeration oracle); nothing in scope is the
                # standard generic denial.
                in_scope = [
                    e for e in entities
                    if isinstance(e, str) and resolve(e, token, hass) is Permission.WRITE
                ]
                if not in_scope:
                    return _tool_error("Forbidden."), "denied", tool_name
                args = {**args, "entities": in_scope}

    get_semantic_moments = None
    if tool_name in _ENTITY_TARGETED:
        entity_id = args.get("entity_id")
        # An out-of-scope or ghost entity must look exactly like a nonexistent
        # one. resolve() also enforces the phx-domain blocklist.
        if entity_id and not _is_visible(entity_id, token, hass):
            return (
                _tool_success(json.dumps(_not_found_envelope(entity_id))),
                "not_found",
                entity_id,
            )
        # mesa-core's get_semantic_moments callback is synchronous, but HA's
        # purpose-specific trigger lookup is async, so pre-compute here (the
        # visibility check above already ran, so out-of-scope entities never
        # reach the HA lookup) and hand mesa-core a closure over the result.
        if (
            tool_name == "mesa_get_profile"
            and entity_id
            and bool(args.get("include_semantic_moments", False))
        ):
            moments = await async_semantic_moments(hass, entity_id)
            get_semantic_moments = lambda eid: moments if eid == entity_id else None  # noqa: E731

    handlers = _build_handlers(
        runtime, token, hass, session_id, get_semantic_moments=get_semantic_moments
    )
    handler = getattr(handlers, tool_name)
    result = await handler(args)
    return _tool_success(json.dumps(result, default=str)), "allowed", tool_name
