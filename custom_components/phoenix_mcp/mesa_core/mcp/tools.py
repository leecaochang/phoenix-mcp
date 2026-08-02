"""MESA MCP tool handlers and registration (Spec 9; Module Proposal 5).

``register_mesa_tools`` is the host server's single integration point: it
registers mesa_query_profiles, mesa_get_profile, mesa_explain_profile, and
mesa_get_caller_context into the host's tool registry via an adapter, plus
the lease coordination tools (mesa_request_lease, mesa_release_lease) when a
``lease_manager`` is provided.

Errors are returned as the Spec 9.6 envelope:
``{"error": code, "message": str, "details": {}}``.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Collection
from typing import Any

from custom_components.phoenix_mcp.mesa_core.exceptions import (
    InvalidCursorError,
    LeaseNotFoundError,
    MesaError,
    MesaValidationError,
)
from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver
from custom_components.phoenix_mcp.mesa_core.lease import LeaseManager
from custom_components.phoenix_mcp.mesa_core.mcp.adapters import ToolRegistry
from custom_components.phoenix_mcp.mesa_core.mcp.schemas import TOOL_DESCRIPTIONS, TOOL_SCHEMAS
from custom_components.phoenix_mcp.mesa_core.privacy import AccessDecision, CallerContext, PrivacyEnforcer
from custom_components.phoenix_mcp.mesa_core.profile import HELPER_DOMAINS, FreshnessReport, SemanticProfile
from custom_components.phoenix_mcp.mesa_core.store import ProfileStore

logger = logging.getLogger("mesa_core.mcp")

MESA_VERSION = "1.1"

# component_type values derivable from an entity ID's domain (Spec 9.3).
# Results are always entity-keyed, so scoped component types never occur.
_COMPONENT_DOMAINS = {"automation", "scene", "zone", "person"}


def _entity_id_set(value: Any) -> frozenset[str] | None:
    """Normalise a host-supplied entity registry, or None when it is unusable.

    The registry must be a reusable collection: a set, list, tuple, or
    anything else that can be iterated more than once. One-shot iterables are
    refused, because the callback runs once per entity and a host that hands
    back the same generator every time would have it drained by the first row
    of a query, leaving every later row to read an empty registry and report
    entities that exist as removed. Materialising here cannot fix that: by the
    time the second row arrives the values are already gone.

    A bare string is refused for the same class of reason: ``"light.x"`` is
    itself a collection of characters, so iterating it would report every real
    entity as removed.
    """
    if isinstance(value, str) or not isinstance(value, Collection):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


def _component_type(entity_id: str) -> str:
    domain = entity_id.split(".", 1)[0]
    if domain in _COMPONENT_DOMAINS:
        return domain
    if domain in HELPER_DOMAINS:
        return "helper"
    return "entity"


_ANONYMOUS = CallerContext(
    caller_id="anonymous", roles=[], is_authenticated=False, session_id=""
)


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": code, "message": message, "details": details or {}}


_TYPE_PREDICATES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}

# JSON Schema numeric-bound keyword -> predicate that holds when the value is in range.
_BOUNDS: dict[str, Callable[[Any, Any], bool]] = {
    "minimum": lambda v, bound: v >= bound,
    "maximum": lambda v, bound: v <= bound,
    "exclusiveMinimum": lambda v, bound: v > bound,
}


def _validate_value(name: str, spec: dict[str, Any], value: Any) -> None:
    if "enum" in spec:
        if value not in spec["enum"]:
            raise MesaValidationError(f"{name} must be one of {spec['enum']} (got {value!r})")
        return
    declared = spec.get("type")
    if declared is not None and not _TYPE_PREDICATES[declared](value):
        raise MesaValidationError(f"{name} must be of type {declared} (got {value!r})")
    for keyword, in_range in _BOUNDS.items():
        if keyword in spec and not in_range(value, spec[keyword]):
            raise MesaValidationError(
                f"{name} must satisfy {keyword} {spec[keyword]} (got {value!r})"
            )
    if declared == "array":
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise MesaValidationError(f"{name} must have at least {spec['minItems']} item(s)")
        items = spec.get("items")
        if items is not None:
            for item in value:
                _validate_value(f"{name} items", items, item)


def _validate_params(name: str, params: dict[str, Any]) -> None:
    """Validate a tool's params against its declared schema (Spec 9.2, 9.6).

    mesa-core carries no runtime dependencies, so this is a hand-rolled check of
    the constructs TOOL_SCHEMAS uses: additionalProperties is false everywhere, so
    unknown keys are rejected; required keys must be present; and each value is
    checked against its type/enum/bounds/items. It runs at the handler boundary,
    the one funnel every transport and custom registry shares, so a raw_sdk or
    direct-dispatch caller gets the same rejection a schema-aware transport would.
    Raises MesaValidationError, which each handler maps to the invalid_query
    envelope. Coercion is deliberately absent: bool("false") is True, so reading
    a string as a boolean would silently defeat an opt-in gate (Spec 9.2).
    """
    schema = TOOL_SCHEMAS[name]
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    unknown = set(params) - set(properties)
    if unknown:
        raise MesaValidationError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    for key in required:
        if key not in params:
            raise MesaValidationError(f"{key} is required")
    for key, value in params.items():
        # No None-skip: the published schema names a type for every field and
        # does not allow null, so an explicit null is a wrong type. The FastMCP
        # adapter strips its injected None defaults before dispatch, so a None
        # reaching here is a caller-supplied explicit null on a direct or custom
        # dispatch path, and is rejected like any other wrong type.
        _validate_value(key, properties[key], value)


class MesaToolHandlers:
    """The four core retrieval API tools (Spec 9.5), framework-agnostic."""

    def __init__(
        self,
        store: ProfileStore,
        resolver: InheritanceResolver | None = None,
        caller_context_fn: Callable[[], CallerContext] | None = None,
        lease_manager: LeaseManager | None = None,
        get_semantic_moments: Callable[[str], list[dict[str, Any]] | None] | None = None,
        privacy_enforcer: PrivacyEnforcer | None = None,
        get_validity_context: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.resolver = resolver or InheritanceResolver(store=store)
        self.caller_context_fn = caller_context_fn
        self.lease_manager = lease_manager
        self.get_semantic_moments = get_semantic_moments
        self.privacy = privacy_enforcer or PrivacyEnforcer()
        self.get_validity_context = get_validity_context

    def _semantic_moments(self, entity_id: str) -> list[dict[str, Any]] | None:
        """Live HA purpose-specific trigger vocabulary for one entity (Spec 9.5).

        Surfaced for agent context only: never stored, never consulted by
        enforcement. Entries without a string ``id`` are dropped; the rest
        pass through as the host supplied them. None means the host cannot
        answer, and the response field is omitted entirely.
        """
        if self.get_semantic_moments is None:
            return None
        try:
            moments = self.get_semantic_moments(entity_id)
        except Exception:
            logger.exception("get_semantic_moments callback failed")
            return None
        if moments is None:
            return None
        return [
            moment
            for moment in moments
            if isinstance(moment, dict) and isinstance(moment.get("id"), str)
        ]

    # -- helpers ---------------------------------------------------------------

    _VALIDITY_KEYS = ("known_entity_ids", "integration_version", "ha_version")

    def _freshness(self, entity_id: str, effective: SemanticProfile) -> FreshnessReport:
        """Evaluate one entity's freshness against its own deployment context.

        The context is fetched per entity, not per request: ``integration_version``
        is the version of the integration that created THIS entity, and a
        deployment runs many integrations at different versions, so a single
        request-wide version would be compared against profiles pinned to other
        integrations and report them stale on the strength of an unrelated
        version. ``review_after_days`` needs nothing from the host, but the
        registry and version pins can only be evaluated against data mesa-core
        never sees, so without the callback those triggers stay unevaluated and
        an invalidated profile keeps reporting ``current`` (Spec 5.4).

        Status and warnings come from one evaluation (Spec 5.4 and 5.5 read the
        same triggers), so they cannot disagree.

        The callback must be synchronous, and every value it supplies is
        type-checked before use. A wrong-typed value is dropped with a logged
        warning rather than evaluated: the failure modes are silent and
        wrong-way-round otherwise, because a bare string is itself a collection
        of characters (so an entity registry given as ``"light.x"`` would report
        every real entity missing) and a non-iterable would raise out of the
        handler as a ``server_error``. Unknown keys are ignored, and a raising
        callback degrades to no context.
        """
        supplied = self._supplied_context(entity_id)
        context: dict[str, Any] = {}
        for key in self._VALIDITY_KEYS:
            if key not in supplied:
                continue
            value = supplied[key]
            if key == "known_entity_ids":
                normalised = _entity_id_set(value)
                if normalised is None:
                    logger.warning(
                        "get_validity_context returned a %s for known_entity_ids on %s; "
                        "expected a reusable collection of entity IDs (a set or list, "
                        "not a generator or string), ignoring it",
                        type(value).__name__,
                        entity_id,
                    )
                    continue
                context[key] = normalised
            elif isinstance(value, str):
                context[key] = value
            else:
                logger.warning(
                    "get_validity_context returned a %s for %s on %s; expected a "
                    "version string, ignoring it",
                    type(value).__name__,
                    key,
                    entity_id,
                )
        return effective.freshness(**context)

    def _supplied_context(self, entity_id: str) -> dict[str, Any]:
        if self.get_validity_context is None:
            return {}
        try:
            supplied = self.get_validity_context(entity_id)
        except Exception:
            logger.exception("get_validity_context failed for %s", entity_id)
            return {}
        if inspect.isawaitable(supplied):
            # An async callback returns a coroutine, which would otherwise read
            # as "not a dict" and silently disable every invalidation check.
            close = getattr(supplied, "close", None)
            if close is not None:
                close()  # nothing awaits it; closing avoids a spurious warning
            logger.warning(
                "get_validity_context must be synchronous; an async callback returns a "
                "coroutine, so invalidation triggers cannot be evaluated for %s",
                entity_id,
            )
            return {}
        if not isinstance(supplied, dict):
            if supplied is not None:
                logger.warning(
                    "get_validity_context returned a %s, expected a dict; ignoring it",
                    type(supplied).__name__,
                )
            return {}
        return supplied

    def _caller_context(self) -> CallerContext | None:
        if self.caller_context_fn is None:
            return None
        return self.caller_context_fn()

    def _access(self, entity_id: str, effective: SemanticProfile) -> AccessDecision:
        """Apply access_roles before surfacing an entity's data (Spec 7.2).

        A conforming implementation with caller context MUST apply access_roles
        before acting on OR surfacing data; without caller context the base
        privacy level applies. Retrieval therefore runs the same evaluation the
        service-call path does, which is also what audit-logs the access of a
        restricted or person entity (Spec 7.1, 17).
        """
        return self.privacy.evaluate(
            effective.privacy_classification,
            self._caller_context(),
            entity_id=entity_id,
            is_person=effective.domain == "person",
            is_minor=effective.person_traits.is_minor is True,
        )

    @staticmethod
    def _denied_response(entity_id: str, decision: AccessDecision) -> dict[str, Any] | None:
        """The response shape for a denied entity, per deny_response_mode (Spec 7.2).

        ``omit`` returns None: the caller decides how absence is expressed
        (dropped from a list, not_found for a single entity). ``redact`` returns
        a placeholder that reveals nothing but the denial, and ``error`` names it
        outright.
        """
        if decision.deny_response_mode == "error":
            return _error(
                "forbidden",
                f"caller is not permitted to access {entity_id!r}",
                details={"entity_id": entity_id},
            )
        if decision.deny_response_mode == "redact":
            return {"entity_id": entity_id, "access": "denied"}
        return None

    def _result_object(
        self,
        entity_id: str,
        effective: SemanticProfile,
        include_fields: list[str] | None,
        freshness: FreshnessReport,
    ) -> dict[str, Any]:
        doc = effective.to_dict()
        sp = doc.get("semantic_profile", {})
        if include_fields:
            # metadata_origin and schema_version are always included (Spec 9.2);
            # requested fields absent from the profile are silently omitted.
            keep = set(include_fields) | {"metadata_origin", "schema_version"}
            sp = {k: v for k, v in sp.items() if k in keep}
        out: dict[str, Any] = {
            "entity_id": entity_id,
            "component_type": _component_type(entity_id),
            "semantic_profile": sp,
            "privacy_classification": doc.get("privacy_classification"),
        }
        # Gated on the EFFECTIVE origin, not the stored one: Spec 9.3 requires
        # staleness_status whenever the surfaced metadata_origin is inferred_ai,
        # which includes an entity whose only profile is an inferred domain,
        # integration, area, or device layer.
        if effective.is_inferred():
            out["staleness_status"] = freshness.status
        if (
            include_fields
            and "diagnostic_profile" in include_fields
            and effective.diagnostic_profile is not None
        ):
            out["diagnostic_profile"] = effective.diagnostic_profile
        return out

    # -- tools ----------------------------------------------------------------

    async def mesa_query_profiles(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query effective profiles with pagination and per-caller access shaping.

        ``pagination.returned`` is the number of objects actually in ``results``.
        ``total_matched`` counts entities matching the filters BEFORE per-caller
        access shaping, so an entity a caller may not see is reflected in
        ``total_matched`` but not in ``results`` or ``pagination.returned``.
        """
        try:
            _validate_params("mesa_query_profiles", params)
            result = self.store.query(
                domains=params.get("domains"),
                tags=params.get("tags"),
                tags_match=params.get("tags_match", "any"),
                areas=params.get("areas"),
                devices=params.get("devices"),
                integrations=params.get("integrations"),
                intents=params.get("intents"),
                include_inferred=params.get("include_inferred", False),
                min_origin_authority=params.get("min_origin_authority"),
                limit=params.get("limit", 50),
                cursor=params.get("cursor"),
                resolver=self.resolver,
            )
            include_fields = params.get("include_fields")
            validity_warnings: list[str] = []
            results: list[dict[str, Any]] = []
            for row in result.rows:
                effective = row.effective or row.stored
                decision = self._access(row.entity_id, effective)
                if not decision.allowed:
                    denied = self._denied_response(row.entity_id, decision)
                    if denied is not None:
                        results.append(denied)
                    continue
                # One evaluation per entity, against that entity's own context.
                # Spec 5.5: a host SHOULD surface a warning when an
                # invalidation trigger fires, for profiles of any origin.
                freshness = self._freshness(row.entity_id, effective)
                validity_warnings.extend(freshness.warnings)
                results.append(
                    self._result_object(row.entity_id, effective, include_fields, freshness)
                )
            response: dict[str, Any] = {
                "mesa_version": MESA_VERSION,
                "results": results,
                "total_matched": result.total_matched,
                "pagination": {
                    "limit": result.limit,
                    "returned": len(results),
                    "has_more": result.has_more,
                    "next_cursor": result.next_cursor,
                },
            }
            caller = self._caller_context()
            if caller is not None:
                response["caller_context"] = caller.to_dict()
            if result.warnings or validity_warnings:
                response["warnings"] = [*result.warnings, *validity_warnings]
            return response
        except InvalidCursorError as err:
            return _error("invalid_cursor", str(err))
        except (ValueError, MesaValidationError) as err:
            return _error("invalid_query", str(err))
        except Exception:
            logger.exception("mesa_query_profiles failed")
            return _error("server_error", "internal error in mesa_query_profiles")

    async def mesa_get_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            _validate_params("mesa_get_profile", params)
            entity_id = params.get("entity_id")
            if not entity_id:
                return _error("invalid_query", "entity_id is required")
            include_diagnostic = params.get("include_diagnostic", True)
            if not self.resolver.has_profile(entity_id):
                return _error(
                    "not_found", f"entity {entity_id!r} has no MESA profile at any level"
                )
            effective = self.resolver.resolve(entity_id)
            decision = self._access(entity_id, effective)
            if not decision.allowed:
                denied = self._denied_response(entity_id, decision)
                if denied is not None:
                    return denied
                # omit: a denied entity is indistinguishable from an absent one.
                return _error(
                    "not_found", f"entity {entity_id!r} has no MESA profile at any level"
                )
            doc = effective.to_dict()
            out: dict[str, Any] = {
                "mesa_version": MESA_VERSION,
                "entity_id": entity_id,
                "component_type": _component_type(entity_id),
                "semantic_profile": doc.get("semantic_profile", {}),
                "privacy_classification": doc.get("privacy_classification"),
            }
            if include_diagnostic and effective.diagnostic_profile is not None:
                out["diagnostic_profile"] = effective.diagnostic_profile
            freshness = self._freshness(entity_id, effective)
            # Spec 9.3: staleness_status accompanies an inferred_ai origin,
            # including one inherited from a scoped layer the entity does not
            # store itself. Spec 5.5's warnings apply to every origin.
            if effective.is_inferred():
                out["staleness_status"] = freshness.status
            if freshness.warnings:
                out["warnings"] = freshness.warnings
            if params.get("include_semantic_moments", False):
                moments = self._semantic_moments(entity_id)
                if moments is not None:
                    out["semantic_moments"] = moments
            return out
        except MesaValidationError as err:
            return _error("invalid_query", str(err))
        except Exception:
            logger.exception("mesa_get_profile failed")
            return _error("server_error", "internal error in mesa_get_profile")

    async def mesa_explain_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            _validate_params("mesa_explain_profile", params)
            entity_id = params.get("entity_id")
            if not entity_id:
                return _error("invalid_query", "entity_id is required")
            show_conflicts = params.get("show_conflicts", True)
            explanation = self.resolver.explain(entity_id)
            decision = self._access(entity_id, explanation.effective_profile)
            if not decision.allowed:
                denied = self._denied_response(entity_id, decision)
                if denied is not None:
                    return denied
                return _error(
                    "not_found", f"entity {entity_id!r} has no MESA profile at any level"
                )
            out = explanation.to_dict(show_conflicts=show_conflicts)
            out["mesa_version"] = MESA_VERSION
            return out
        except MesaValidationError as err:
            return _error("invalid_query", str(err))
        except Exception:
            logger.exception("mesa_explain_profile failed")
            return _error("server_error", "internal error in mesa_explain_profile")

    async def mesa_get_caller_context(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            _validate_params("mesa_get_caller_context", params)
            caller = self._caller_context() or _ANONYMOUS
            return {"mesa_version": MESA_VERSION, **caller.to_dict()}
        except MesaValidationError as err:
            return _error("invalid_query", str(err))
        except Exception:
            logger.exception("mesa_get_caller_context failed")
            return _error("server_error", "internal error in mesa_get_caller_context")

    # -- lease tools (Enrichment Section 21) --------------------------------------

    async def mesa_request_lease(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self.lease_manager is not None  # registered only when provided
        try:
            _validate_params("mesa_request_lease", params)
            entities = params.get("entities")
            if not entities or not isinstance(entities, list):
                return _error(
                    "invalid_query", "entities is required and must be a non-empty array"
                )
            duration = params.get("duration_seconds")
            if duration is None:
                return _error("invalid_query", "duration_seconds is required")
            caller = self._caller_context() or _ANONYMOUS
            response = await self.lease_manager.arequest(
                [str(e) for e in entities],
                # Passed through unconverted: the schema already guarantees a
                # number, and float() of an arbitrarily large JSON integer
                # raises OverflowError before the manager's min() clamp (which
                # compares int/float exactly) can apply the 30s cap.
                duration,
                session_id=caller.session_id,
                caller_id=caller.caller_id,
                intent=params.get("intent"),
                priority_level=params.get("priority_level", "cooperative"),
                preemption_handling=params.get("preemption_handling", "rollback_abort"),
                caller_priority=params.get("caller_priority"),
            )
            if not response.granted and response.automation_denials and set(
                response.automation_denials
            ) == set(response.entities_denied):
                # Total denial by protected/critical automations (Spec 9.6).
                return _error(
                    "lease_conflict",
                    "lease denied: all requested entities are under protected or "
                    "critical automation control",
                    details={"denial_reasons": response.denial_reasons},
                )
            return {"mesa_version": MESA_VERSION, **response.to_dict()}
        except (TypeError, ValueError, MesaValidationError) as err:
            return _error("invalid_query", str(err))
        except Exception:
            logger.exception("mesa_request_lease failed")
            return _error("server_error", "internal error in mesa_request_lease")

    async def mesa_release_lease(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self.lease_manager is not None  # registered only when provided
        try:
            _validate_params("mesa_release_lease", params)
            lease_id = params.get("lease_id")
            if not lease_id:
                return _error("invalid_query", "lease_id is required")
            caller = self._caller_context()
            lease = await self.lease_manager.arelease(
                str(lease_id),
                session_id=caller.session_id if caller is not None else None,
            )
            return {
                "mesa_version": MESA_VERSION,
                "released": True,
                "lease_id": lease.lease_id,
                "entities": list(lease.entities),
            }
        except MesaValidationError as err:
            return _error("invalid_query", str(err))
        except LeaseNotFoundError as err:
            return _error("lease_not_found", str(err))
        except Exception:
            logger.exception("mesa_release_lease failed")
            return _error("server_error", "internal error in mesa_release_lease")


def register_mesa_tools(
    store: ProfileStore,
    adapter: str | ToolRegistry = "fastmcp",
    server: Any = None,
    *,
    resolver: InheritanceResolver | None = None,
    enforcer: Any = None,
    lease_manager: LeaseManager | None = None,
    caller_context_fn: Callable[[], CallerContext] | None = None,
    get_semantic_moments: Callable[[str], list[dict[str, Any]] | None] | None = None,
    get_validity_context: Callable[[str], dict[str, Any]] | None = None,
) -> ToolRegistry:
    """Register all MESA MCP tools into the host server's tool registry.

    ``adapter`` is "fastmcp", "raw_sdk", or any object implementing the
    ToolRegistry protocol. Returns the registry used.

    ``get_validity_context`` supplies the deployment facts the Spec 5.5
    invalidation triggers are evaluated against, as a dict with any of
    ``known_entity_ids``, ``integration_version``, and ``ha_version``. It is
    called once per entity and receives that entity's ID, because
    ``integration_version`` means the version of the integration that created
    THAT entity: a deployment runs many integrations at different versions, so
    one request-wide version would be compared against profiles pinned to
    other integrations. ``known_entity_ids`` must be a reusable collection of
    entity IDs (a set or list, never a generator or a bare string) and must be
    the deployment's complete entity registry, since anything missing from it
    reads as a removed entity. The callback must be synchronous. Without
    the callback these triggers cannot be evaluated and an invalidated profile
    keeps reporting ``staleness_status: current`` (Spec 5.4);
    ``review_after_days`` is evaluated either way.

    When ``lease_manager`` is provided, the lease coordination tools
    (mesa_request_lease, mesa_release_lease) are registered as well; omitted,
    they are not, and the server does not participate in the Section 21
    protocol. ``enforcer`` is accepted for API stability; enforcement is
    wired into the host's service-call path directly (see the Module
    Proposal, Section 6.2), not exposed as a tool.
    """
    registry: ToolRegistry
    if isinstance(adapter, str):
        if adapter == "fastmcp":
            from custom_components.phoenix_mcp.mesa_core.mcp.adapters.fastmcp import FastMCPRegistry

            registry = FastMCPRegistry(server)
        elif adapter == "raw_sdk":
            from custom_components.phoenix_mcp.mesa_core.mcp.adapters.raw_sdk import RawSDKRegistry

            registry = RawSDKRegistry(server)
        else:
            raise MesaError(
                f"unknown adapter {adapter!r}; use 'fastmcp', 'raw_sdk', or a ToolRegistry"
            )
    else:
        registry = adapter

    handlers = MesaToolHandlers(
        store=store,
        resolver=resolver,
        caller_context_fn=caller_context_fn,
        lease_manager=lease_manager,
        get_semantic_moments=get_semantic_moments,
        get_validity_context=get_validity_context,
    )
    tools = [
        ("mesa_query_profiles", handlers.mesa_query_profiles),
        ("mesa_get_profile", handlers.mesa_get_profile),
        ("mesa_explain_profile", handlers.mesa_explain_profile),
        ("mesa_get_caller_context", handlers.mesa_get_caller_context),
    ]
    if lease_manager is not None:
        tools.append(("mesa_request_lease", handlers.mesa_request_lease))
        tools.append(("mesa_release_lease", handlers.mesa_release_lease))
    for name, handler in tools:
        registry.register_tool(name, handler, TOOL_SCHEMAS[name], TOOL_DESCRIPTIONS[name])
    return registry
