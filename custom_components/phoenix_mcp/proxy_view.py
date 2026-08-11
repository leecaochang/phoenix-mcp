"""REST proxy views for the Phoenix MCP integration."""


import asyncio
import dataclasses
import json
import logging
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.core import HomeAssistant

from .view_base import PhoenixView
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from .approvals import PendingApprovalCapacityError
from .audit import Outcome, generate_request_id

from .const import (
    BLOCKED_DOMAINS,
    CAP_DENY,
    DOMAIN,
    DUAL_GATE_SERVICES,
    ESPHOME_DOMAIN,
    HIGH_RISK_DOMAINS,
    LOG_LEVEL_ERROR_MESSAGE,
    LOG_LEVELS,
    MAX_LOG_ENTRIES,
    NO_TARGET_SERVICES,
    PROXY_TIMEOUT_SECONDS,
)
from .data import PhoenixData
from .mesa import async_apply_mesa_to_call, fire_mesa_blocked_event
from .helpers import (
    SystemLogUnavailableError,
    build_error_response as _error,
    build_safe_config,
    collect_log_entries as _collect_log_entries,
    effective_cap,
    async_evaluate_capability,
    async_get_authenticated_token as _async_get_authenticated_token,
    get_client_ip as _get_client_ip,
    log_request as _log,
    async_read_json_body as _read_json_body,
    render_template_for_token as _render_template_for_token,
    sanitize_service_data as _sanitize_service_data,
    service_not_found_hint as _service_not_found_hint,
    validation_error_message,
)
from .policy_engine import (
    EntityCreationNotPermitted,
    Permission,
    call_needs_physical_gate,
    canonical_entity_id,
    esphome_entry_writable,
    filter_entities_for_token,
    filter_service_response,
    resolve,
    resolve_esphome_user_service,
    resolve_service_targets,
    scrub_sensitive_attributes,
)
from .rate_limiter import RateLimitResult

_LOGGER = logging.getLogger(__name__)


def _json_response(
    body: Any,
    status: int,
    request_id: str,
    rl_result: RateLimitResult | None = None,
    extra_headers: dict[str, str] | None = None,
) -> web.Response:
    """Return a JSON success response. Adds X-RateLimit-* headers when rate limiting is active."""
    headers: dict[str, str] = {"X-Phoenix-Request-ID": request_id}
    if rl_result is not None and rl_result.rate_limiting_enabled:
        headers["X-RateLimit-Limit"] = str(rl_result.limit)
        headers["X-RateLimit-Remaining"] = str(rl_result.remaining)
        headers["X-RateLimit-Reset"] = str(rl_result.reset)
    if extra_headers:
        headers.update(extra_headers)
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(body, default=str),
        headers=headers,
    )


class PhoenixRootView(PhoenixView):
    """GET /api/phoenix-mcp/health - health check endpoint.

    Lives at /health rather than the base path because the base path is the MCP
    endpoint: it is the URL an operator pastes into a client config, so it is
    kept as short as possible.
    """

    url = "/api/phoenix-mcp/health"
    name = "api:phoenix-mcp:health"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()

        result = await _async_get_authenticated_token(hass, request, data, request_id, "/api/phoenix-mcp/health")
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        _log(data, token, request_id=request_id, method="GET", resource="/api/phoenix-mcp/health",
             outcome="allowed", client_ip=_get_client_ip(request))
        return _json_response({"message": "API running."}, 200, request_id, rl_result)


class PhoenixStatesView(PhoenixView):
    """GET /api/phoenix-mcp/states - list all entities accessible to the token."""

    url = "/api/phoenix-mcp/states"
    name = "api:phoenix-mcp:states"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()

        result = await _async_get_authenticated_token(hass, request, data, request_id, "/api/phoenix-mcp/states")
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        try:
            limit = max(1, min(int(request.query.get("limit", 500)), 500))
            offset = max(int(request.query.get("offset", 0)), 0)
        except ValueError:
            return _error("invalid_request", "Invalid pagination parameters.", 400, request_id)

        states = hass.states.async_all()
        filtered = filter_entities_for_token(states, token, hass)
        page = filtered[offset:offset + limit]

        _log(data, token, request_id=request_id, method="GET", resource="/api/phoenix-mcp/states",
             outcome="allowed", client_ip=_get_client_ip(request))
        return _json_response(page, 200, request_id, rl_result)


class PhoenixStateView(PhoenixView):
    """GET /api/phoenix-mcp/states/{entity_id} - get state for a single entity."""

    url = "/api/phoenix-mcp/states/{entity_id}"
    name = "api:phoenix-mcp:state"
    requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = f"/api/phoenix-mcp/states/{entity_id}"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        # Canonicalize first so the permission check and the state fetch below use
        # the same id (a registry id or alias would otherwise pass resolve() but
        # 404 on hass.states.get of the original value).
        entity_id = canonical_entity_id(entity_id, hass)
        perm = resolve(entity_id, token, hass)

        if perm == Permission.NOT_FOUND:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="not_found", client_ip=client_ip)
            return _error("not_found", "Entity not found.", 404, request_id)

        if perm in (Permission.NO_ACCESS, Permission.DENY):
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="denied", client_ip=client_ip)
            # Return identical 404 body to avoid revealing entity existence.
            return _error("not_found", "Entity not found.", 404, request_id)

        state = hass.states.get(entity_id)
        if state is None:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="not_found", client_ip=client_ip)
            return _error("not_found", "Entity not found.", 404, request_id)

        _log(data, token, request_id=request_id, method="GET", resource=resource,
             outcome="allowed", client_ip=client_ip)
        return _json_response(scrub_sensitive_attributes(state), 200, request_id, rl_result)


@dataclasses.dataclass(frozen=True)
class _MesaAllowed:
    """MESA permitted the call: the surviving entities plus any advisory text."""

    entities: list[str]
    warnings: list[str]


async def _mesa_gate(
    hass: HomeAssistant,
    data: PhoenixData,
    token: Any,
    *,
    domain: str,
    service: str,
    service_data: dict,
    entities: list[str],
    body: dict,
    request_id: str,
    resource: str,
    client_ip: str,
    rl_result: RateLimitResult,
) -> _MesaAllowed | web.Response:
    """Run MESA on the flattened, Phoenix-permitted entity list.

    Returns the surviving entities and any advisory warnings, or the response to
    send. MESA runs LAST, after Phoenix has resolved and flattened targets and
    after the capability gates, so it never sees an entity Phoenix already
    denied.
    """
    outcome = await async_apply_mesa_to_call(
        hass, data, token,
        domain=domain, service=service, service_data=service_data,
        entities=entities,
        request_id=request_id, client_ip=client_ip, session_id=request_id,
    )
    if outcome.blocked:
        fire_mesa_blocked_event(hass, token, outcome.blocked)
    if outcome.decision == "pending":
        approval = outcome.approval
        if approval is None:
            # Fail CLOSED, like the capability gate: falling through here would
            # run the very call MESA just held for approval.
            _LOGGER.error("MESA returned pending with no approval record rid=%s", request_id)
            return _error("forbidden", "Forbidden.", 403, request_id)
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="pending_approval", client_ip=client_ip, payload=body)
        return _json_response(
            {
                "status": "pending_approval",
                "approval_id": approval.id,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                "review_url": f"/phoenix-mcp#approvals/{approval.id}",
                "message": "This action requires admin approval. The admin has been notified.",
            },
            202, request_id, rl_result,
        )
    if outcome.decision == "deny":
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="denied", client_ip=client_ip, payload=body)
        return _error("forbidden", "Forbidden.", 403, request_id)
    return _MesaAllowed(entities=outcome.entities, warnings=list(outcome.warnings or []))


async def _dispatch_no_target_call(
    hass: HomeAssistant,
    data: PhoenixData,
    token: Any,
    *,
    domain: str,
    service: str,
    service_data: dict,
    body: dict,
    request_id: str,
    resource: str,
    client_ip: str,
    rl_result: RateLimitResult,
    timeout_noun: str,
    surface_validation_errors: bool,
) -> web.Response:
    """Call a service that takes NO entity target, and map the outcome.

    Three families reach this: the dual-gate services (homeassistant/restart and
    /stop, which have no entities in hass.states), the config-reload family in
    NO_TARGET_SERVICES (whose schemas reject an entity_id outright), and ESPHome
    user-defined actions (whose schema is built from only the arguments the
    device declared). Each ran its own near-identical copy of this, and their
    timeout/error branches were the least covered code in the module.

    Authorization is the CALLER's job and has already happened by the time this
    runs; nothing here decides whether the call is allowed.

    The two parameters are the differences that are real and must not be blurred:

    - timeout_noun: a slow call is reported as success with partial=True, not as
      a failure, and the sentence says "Service" for HA's own services but
      "Action" for a device-defined one.
    - surface_validation_errors: only the ESPHome family turns a
      ServiceValidationError / vol.Invalid into a 400 carrying the message,
      because a device-defined schema makes a wrong argument NAME the likeliest
      failure and the caller can fix it. For the other two the message would be
      about a service the token cannot otherwise probe, so they stay a uniform
      403 with no service-existence oracle.
    """
    if domain in HIGH_RISK_DOMAINS:
        _LOGGER.info(
            "High-risk service call %s/%s by token %s rid=%s",
            domain, service, token.name, request_id,
        )

    def _record(outcome: Outcome) -> None:
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome=outcome, client_ip=client_ip, payload=body)

    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            await hass.services.async_call(
                domain, service, service_data, blocking=True, return_response=False,
            )
    except asyncio.TimeoutError:
        _record("allowed")
        return _json_response(
            {
                "success": True,
                "partial": True,
                "message": (
                    f"{timeout_noun} dispatched but "
                    f"{'the device' if timeout_noun == 'Action' else 'HA'} did not "
                    f"respond within the timeout window."
                ),
            },
            200, request_id, rl_result,
        )
    except (ServiceValidationError, vol.Invalid) as err:
        if not surface_validation_errors:
            _record("denied")
            return _error("forbidden", "Forbidden.", 403, request_id)
        _record("invalid_request")
        return _error("invalid_request", validation_error_message(err), 400, request_id)
    except (ServiceNotFound, HomeAssistantError):
        _record("denied")
        return _error("forbidden", "Forbidden.", 403, request_id)
    _record("allowed")
    return _json_response({"success": True}, 200, request_id, rl_result)


async def _cap_gate(
    cap_name: str,
    token: Any,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    domain: str,
    service: str,
    entity_id: Any,
    device_id: Any,
    area_id: Any,
    service_data: dict,
    body: dict,
    request_id: str,
    resource: str,
    client_ip: str,
    rl_result: RateLimitResult,
) -> web.Response | None:
    """Dispatch a capability gate on the REST service-call path.

    Mirrors the MCP call_service gates: deny returns 403, confirm creates a
    pending approval and returns 202 (the same response shape as a MESA
    confirm), allow returns None and the caller proceeds. The approval stores
    MCP-shaped call_service args, so the shared _execute_call_service executor
    re-runs the identical call when the admin approves.
    """
    from .mcp_view import _build_diff_call_service  # noqa: PLC0415 - view modules import each other lazily

    args = {
        "domain": domain,
        "service": service,
        "entity_id": entity_id,
        "device_id": device_id,
        "area_id": area_id,
        "service_data": service_data,
    }
    try:
        result = await async_evaluate_capability(
            cap_name, token, hass, data,
            tool_name="call_service", args=args, request_id=request_id,
            client_ip=client_ip, diff=lambda: _build_diff_call_service(args, token, hass),
        )
    except PendingApprovalCapacityError:
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="rate_limited", client_ip=client_ip, payload=body)
        return _error("rate_limited", "Too many pending approvals for this token.", 429, request_id)
    if result.is_deny:
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="denied", client_ip=client_ip, payload=body)
        return _error("forbidden", "Forbidden.", 403, request_id)
    if result.is_pending:
        approval = result.approval
        if approval is None:
            # Invariant: a Confirm result always carries its approval. If that
            # ever breaks, refuse. Falling through would mean "no gate applied"
            # and the call would execute unreviewed, so this stays fail-CLOSED.
            _LOGGER.error("Confirm gate returned pending with no approval record rid=%s", request_id)
            return _error("forbidden", "Forbidden.", 403, request_id)
        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="pending_approval", client_ip=client_ip, payload=body)
        return _json_response(
            {
                "status": "pending_approval",
                "approval_id": approval.id,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                "review_url": f"/phoenix-mcp#approvals/{approval.id}",
                "message": "This action requires admin approval. The admin has been notified.",
            },
            202, request_id, rl_result,
        )
    return None


class PhoenixServiceView(PhoenixView):
    """POST /api/phoenix-mcp/services/{domain}/{service} - call a HA service."""

    url = "/api/phoenix-mcp/services/{domain}/{service}"
    name = "api:phoenix-mcp:service"
    requires_auth = False

    async def post(self, request: web.Request, domain: str, service: str) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = f"service:{domain}/{service}"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        body = await _read_json_body(request, request_id)
        if isinstance(body, web.Response):
            return body

        service_key = f"{domain}/{service}"

        entity_id = body.get("entity_id")
        device_id = body.get("device_id")
        area_id = body.get("area_id")
        # Every HA target selector is stripped, not only the three read above:
        # the ones nothing reads here (floor_id, label_id) are exactly the ones
        # that would otherwise ride the body through to hass.services.async_call
        # unresolved and unchecked. See helpers.sanitize_service_data.
        service_data = _sanitize_service_data(body)

        # Capability gates mirror the MCP call_service tool: deny is 403,
        # confirm routes through the approval flow (202 pending), allow
        # proceeds. The three service families are disjoint, so elif matches
        # the MCP dispatch order exactly.
        gate_cap: str | None = None
        if service_key in DUAL_GATE_SERVICES:
            gate_cap = "cap_restart"
        elif call_needs_physical_gate(
            domain=domain, service=service, entity_id=entity_id,
            device_id=device_id, area_id=area_id, token=token, hass=hass,
        ):
            gate_cap = "cap_physical_control"
        elif service_key in NO_TARGET_SERVICES:
            gate_cap = "cap_yaml_edit"
        if gate_cap is not None:
            gated = await _cap_gate(
                gate_cap, token, hass, data,
                domain=domain, service=service,
                entity_id=entity_id, device_id=device_id, area_id=area_id,
                service_data=service_data, body=body,
                request_id=request_id, resource=resource,
                client_ip=client_ip, rl_result=rl_result,
            )
            if gated is not None:
                return gated

        # DUAL_GATE_SERVICES (homeassistant/restart, homeassistant/stop) have no
        # entities in hass.states. Routing them through resolve_service_targets
        # always produces an empty list and a spurious 403. The cap_restart
        # gate above is the only permission check required for these services.
        if service_key in DUAL_GATE_SERVICES:
            return await _dispatch_no_target_call(
                hass, data, token,
                domain=domain, service=service, service_data=service_data, body=body,
                request_id=request_id, resource=resource, client_ip=client_ip,
                rl_result=rl_result, timeout_noun="Service",
                surface_validation_errors=False,
            )

        # NO_TARGET_SERVICES are domain-wide config reloads that take no entity
        # target, so they bypass resolve_service_targets (which would attach an
        # entity_id list their schemas reject). The cap_yaml_edit gate above is
        # the only permission check required; any target in the body is ignored.
        #
        # surface_validation_errors=True matches mcp_view's call_service path.
        # This surface previously returned a generic 403 here while MCP returned
        # the message, so the same reload with the same bad argument reported
        # differently depending on which surface the agent used. MCP's reasoning
        # holds for both: the call is post-cap_yaml_edit, so the message
        # describes the caller's own reloadable config, never hidden state.
        # tests/test_surface_parity.py pins the two together.
        if service_key in NO_TARGET_SERVICES:
            return await _dispatch_no_target_call(
                hass, data, token,
                domain=domain, service=service, service_data=service_data, body=body,
                request_id=request_id, resource=resource, client_ip=client_ip,
                rl_result=rl_result, timeout_noun="Service",
                surface_validation_errors=True,
            )

        # ESPHome user-defined actions (esphome.<device>_<action>) take no entity
        # target: HA builds their schema from only the arguments the device
        # declared, so the flattened entity_id list below is rejected outright and
        # target resolution yields a spurious 403. Authorized by the owning
        # DEVICE's write scope rather than a capability (mirrors mcp_view's
        # _execute_call_service); MESA does not apply with no entity target. A
        # service no LOADED entry claims falls through and refuses uniformly.
        if domain == ESPHOME_DOMAIN:
            esphome_entry = resolve_esphome_user_service(hass, service)
            if esphome_entry is not None:
                if not esphome_entry_writable(hass, esphome_entry, token):
                    _log(data, token, request_id=request_id, method="POST", resource=resource,
                         outcome="denied", client_ip=client_ip, payload=body)
                    return _error("forbidden", "Forbidden.", 403, request_id)
                return await _dispatch_no_target_call(
                    hass, data, token,
                    domain=domain, service=service, service_data=service_data, body=body,
                    request_id=request_id, resource=resource, client_ip=client_ip,
                    rl_result=rl_result, timeout_noun="Action",
                    surface_validation_errors=True,
                )

        try:
            permitted_entities, requested_count = resolve_service_targets(
                entity_id=entity_id,
                device_id=device_id,
                area_id=area_id,
                service_domain=domain,
                token=token,
                hass=hass,
            )
        except EntityCreationNotPermitted:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="denied", client_ip=client_ip, payload=body)
            return _error("forbidden", "Forbidden.", 403, request_id)

        if not permitted_entities:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="denied", client_ip=client_ip, payload=body)
            return _error("forbidden", "Forbidden.", 403, request_id)

        mesa_gated = await _mesa_gate(
            hass, data, token,
            domain=domain, service=service, service_data=service_data,
            entities=permitted_entities, body=body, request_id=request_id,
            resource=resource, client_ip=client_ip, rl_result=rl_result,
        )
        if isinstance(mesa_gated, web.Response):
            return mesa_gated
        permitted_entities = mesa_gated.entities
        mesa_warnings = mesa_gated.warnings

        if domain in HIGH_RISK_DOMAINS:
            _LOGGER.info(
                "High-risk service call %s/%s by token %s rid=%s",
                domain, service, token.name, request_id,
            )

        affected_count = len(permitted_entities)
        call_data = dict(service_data)
        call_data["entity_id"] = permitted_entities

        extra = {
            "X-Phoenix-Entities-Requested": str(requested_count),
            "X-Phoenix-Entities-Affected": str(affected_count),
        }

        use_return_response = False
        if effective_cap(token, "cap_service_response") != CAP_DENY:
            try:
                from homeassistant.core import SupportsResponse as _SR
                handler = hass.services.async_services().get(domain, {}).get(service)
                use_return_response = (
                    handler is not None and
                    getattr(handler, "supports_response", None) not in (None, _SR.NONE)
                )
            except Exception:
                _LOGGER.debug(
                    "supports_response probe failed for %s/%s; calling without return_response",
                    domain, service, exc_info=True,
                )

        try:
            async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
                svc_response = await hass.services.async_call(
                    domain,
                    service,
                    call_data,
                    blocking=True,
                    return_response=use_return_response,
                )
        except asyncio.TimeoutError:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="allowed", client_ip=client_ip, payload=body,
                 mesa_advisory=bool(mesa_warnings))
            return _json_response(
                {"success": True, "partial": True, "message": "Service dispatched but HA did not respond within the timeout window."},
                200, request_id, rl_result, extra_headers=extra,
            )
        except ServiceNotFound:
            # For most domains, return 403, not 404. Phoenix MCP must never confirm or
            # deny the existence of a domain or service to the token holder:
            # a 404 here leaks that the service name is invalid; 403 is
            # indistinguishable from a permission denial. But for a curated set of
            # core actuator domains (DOMAIN_SERVICE_HINTS), name the valid core
            # verbs so an agent that guessed wrong can self-correct. Leak-safe: the
            # catch is post-authorization (WRITE already proven on an entity in this
            # domain) and the verbs come from a hardcoded public HA-core list, not a
            # live hass.services lookup. This surfaces as invalid_request (400), the
            # same self-correctable class as a ServiceValidationError.
            hint = _service_not_found_hint(domain, service)
            if hint is not None:
                _log(data, token, request_id=request_id, method="POST", resource=resource,
                     outcome="invalid_request", client_ip=client_ip, payload=body)
                return _error("invalid_request", hint[0], 400, request_id, suggestions=hint[1])
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="denied", client_ip=client_ip, payload=body)
            return _error("forbidden", "Forbidden.", 403, request_id)
        except (ServiceValidationError, vol.Invalid) as err:
            # Mirrors mcp_view._execute_call_service, and the no-target branch
            # above, so the same bad argument reads the same on both surfaces.
            #
            # vol.Invalid is the load-bearing half: hass.services.async_call
            # validates service_data against the TARGET service's own voluptuous
            # schema and re-raises it unwrapped, and it subclasses neither
            # ServiceValidationError nor HomeAssistantError. So it escaped this
            # handler entirely and reached HomeAssistantView's wrapper, which
            # answers a bare 400 with no X-Phoenix-Request-ID (rule 18) and, far
            # worse, NO AUDIT ROW: a call that cleared auth, the rate limiter,
            # the capability gate and MESA then vanished from the one record an
            # operator reads.
            #
            # Surfacing the message is safe for the same reason as on MCP: the
            # catch is post-authorization, so it describes the caller's own
            # argument, never hidden entities or services. ServiceNotFound
            # subclasses ServiceValidationError and is caught ABOVE, keeping the
            # no-oracle rule for service existence.
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="invalid_request", client_ip=client_ip, payload=body)
            return _error("invalid_request", validation_error_message(err), 400, request_id)
        except HomeAssistantError:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="denied", client_ip=client_ip, payload=body)
            return _error("forbidden", "Forbidden.", 403, request_id)

        filtered_response = filter_service_response(svc_response, token, hass) if svc_response is not None else None

        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="allowed", client_ip=client_ip, payload=body,
             mesa_advisory=bool(mesa_warnings))

        resp_body: dict[str, Any] = {"success": True}
        if filtered_response is not None:
            resp_body["service_response"] = filtered_response
        if mesa_warnings:
            resp_body["mesa_advisory"] = mesa_warnings

        return _json_response(resp_body, 200, request_id, rl_result, extra_headers=extra)


class PhoenixConfigView(PhoenixView):
    """GET /api/phoenix-mcp/config - HA configuration (requires cap_config_read)."""

    url = "/api/phoenix-mcp/config"
    name = "api:phoenix-mcp:config"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = "/api/phoenix-mcp/config"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        if effective_cap(token, "cap_config_read") == CAP_DENY:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="denied", client_ip=client_ip)
            return _error("forbidden", "Forbidden.", 403, request_id)

        _log(data, token, request_id=request_id, method="GET", resource=resource,
             outcome="allowed", client_ip=client_ip)
        return _json_response(build_safe_config(hass), 200, request_id, rl_result)


class PhoenixTemplateView(PhoenixView):
    """POST /api/phoenix-mcp/template - render a Jinja2 template against permitted entity state."""

    url = "/api/phoenix-mcp/template"
    name = "api:phoenix-mcp:template"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = "/api/phoenix-mcp/template"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        if effective_cap(token, "cap_template_render") == CAP_DENY:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="denied", client_ip=client_ip)
            return _error("forbidden", "Forbidden.", 403, request_id)

        body = await _read_json_body(request, request_id)
        if isinstance(body, web.Response):
            return body

        template_str = body.get("template")
        if not template_str or not isinstance(template_str, str):
            return _error("invalid_request", "Missing or invalid 'template' field.", 400, request_id)

        try:
            rendered = _render_template_for_token(template_str, token, hass)
        except Exception:
            _log(data, token, request_id=request_id, method="POST", resource=resource,
                 outcome="invalid_request", client_ip=client_ip)
            return _error(
                "invalid_request",
                "Template rendering failed.",
                400,
                request_id,
                suggestions=["Check your template syntax."],
            )

        _log(data, token, request_id=request_id, method="POST", resource=resource,
             outcome="allowed", client_ip=client_ip)
        return _json_response({"rendered": str(rendered)}, 200, request_id, rl_result)


class PhoenixEventsView(PhoenixView):
    """GET /api/phoenix-mcp/events - HA event bus listener counts (requires cap_config_read)."""

    url = "/api/phoenix-mcp/events"
    name = "api:phoenix-mcp:events"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = "/api/phoenix-mcp/events"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        if effective_cap(token, "cap_config_read") == CAP_DENY:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="denied", client_ip=client_ip)
            return _error("forbidden", "Forbidden.", 403, request_id)

        # Match the native HA GET /api/events list shape.
        listeners = hass.bus.async_listeners()
        events = [{"event": k, "listener_count": v} for k, v in sorted(listeners.items())]

        _log(data, token, request_id=request_id, method="GET", resource=resource,
             outcome="allowed", client_ip=client_ip)
        return _json_response(events, 200, request_id, rl_result)


class PhoenixServicesView(PhoenixView):
    """GET /api/phoenix-mcp/services - list services in domains the token has WRITE access to."""

    url = "/api/phoenix-mcp/services"
    name = "api:phoenix-mcp:services"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = "/api/phoenix-mcp/services"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        # The Phoenix domain blocklist applies to BOTH branches, before any
        # permission resolution (rule 5). It used to be applied only on the
        # pass-through branch, which put the blocklist on the more-privileged
        # path and not the scoped one - backwards, and only unreachable because
        # Phoenix registers no services of its own today. A scoped token holding
        # a GREEN grant on the Phoenix domain would otherwise have had them
        # listed while a pass-through token would not.
        all_services = {
            domain: svcs
            for domain, svcs in hass.services.async_services().items()
            if domain not in BLOCKED_DOMAINS
        }

        if token.pass_through:
            filtered = all_services
        else:
            # Include only domains where the token has WRITE access at domain
            # level or higher. A WRITE grant on a single entity within a domain does not
            # qualify - the domain node itself must be GREEN.
            writable_domains: set[str] = {
                domain
                for domain, node in token.permissions.domains.items()
                if node.state == "GREEN"
            }
            filtered = {
                domain: svcs
                for domain, svcs in all_services.items()
                if domain in writable_domains
            }

        output = [
            {
                "domain": domain,
                "services": {
                    name: (desc.as_dict() if hasattr(desc, "as_dict") else desc)
                    for name, desc in svcs.items()
                },
            }
            for domain, svcs in sorted(filtered.items())
        ]

        _log(data, token, request_id=request_id, method="GET", resource=resource,
             outcome="allowed", client_ip=client_ip)
        return _json_response(output, 200, request_id, rl_result)


_DEFAULT_LOG_LIMIT = 50


class PhoenixLogsView(PhoenixView):
    """GET /api/phoenix-mcp/logs - HA system log entries (requires cap_log_read)."""

    url = "/api/phoenix-mcp/logs"
    name = "api:phoenix-mcp:logs"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        request_id = generate_request_id()
        resource = "/api/phoenix-mcp/logs"
        client_ip = _get_client_ip(request)

        result = await _async_get_authenticated_token(hass, request, data, request_id, resource)
        if isinstance(result, web.Response):
            return result
        token, rl_result = result

        if effective_cap(token, "cap_log_read") == CAP_DENY:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="denied", client_ip=client_ip)
            return _error("forbidden", "Forbidden.", 403, request_id)

        raw_level = request.query.get("level", "WARNING").strip().upper()
        if raw_level not in LOG_LEVELS:
            return _error("invalid_request", LOG_LEVEL_ERROR_MESSAGE, 400, request_id)

        integration = request.query.get("integration", "").strip() or None

        limit = _DEFAULT_LOG_LIMIT
        raw_limit = request.query.get("limit", "")
        if raw_limit:
            try:
                limit = int(raw_limit)
                if not (1 <= limit <= MAX_LOG_ENTRIES):
                    return _error("invalid_request", f"limit must be between 1 and {MAX_LOG_ENTRIES}.", 400, request_id)
            except ValueError:
                return _error("invalid_request", "limit must be an integer.", 400, request_id)

        try:
            page = _collect_log_entries(hass, raw_level, integration, limit)
        except SystemLogUnavailableError:
            _log(data, token, request_id=request_id, method="GET", resource=resource,
                 outcome="invalid_request", client_ip=client_ip)
            return _error(
                "invalid_request",
                "The system_log integration is not loaded; logs are unavailable.",
                400, request_id,
            )
        _log(data, token, request_id=request_id, method="GET", resource=resource,
             outcome="allowed", client_ip=client_ip)
        # Same shape as the MCP get_logs tool: a caller must not have to know which
        # surface it used to learn whether it saw the whole log.
        return _json_response({
            "count": len(page.entries),
            "total": page.total,
            "truncated": page.total > len(page.entries),
            "entries": page.entries,
        }, 200, request_id, rl_result)


ALL_VIEWS: list[type[PhoenixView]] = [
    PhoenixRootView,
    PhoenixStatesView,
    PhoenixStateView,
    PhoenixServiceView,
    PhoenixConfigView,
    PhoenixTemplateView,
    PhoenixEventsView,
    PhoenixServicesView,
    PhoenixLogsView,
]
