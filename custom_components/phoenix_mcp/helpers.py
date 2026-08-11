"""Shared helpers used by multiple Phoenix MCP views."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import string
from collections.abc import Awaitable, Callable, Iterator
from functools import lru_cache
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util.dt import parse_datetime, utcnow

from .const import (
    CATALOGS_DIR,
    DIFF_SUMMARY_TEMPLATES,
    NOTIFICATION_TEMPLATES,
    VERSION_SUMMARY_TEMPLATES,
    VOICE_TEMPLATES,
    CAP_ALLOW,
    CAP_CONFIRM,
    CAP_DENY,
    CAPABILITY_NAMES,
    DOMAIN,
    DOMAIN_SERVICE_HINTS,
    PASS_THROUGH_EXEMPT_CAPS,
    SENSITIVE_ATTRIBUTES,
    SENSOR_WRITE_DEBOUNCE_SECONDS,
    TARGET_SELECTOR_KEYS,
    TOKEN_LENGTH,
    TOKEN_PREFIX,
)
from .policy_engine import (
    Permission,
    assist_expose_check,
    is_sensitive_key,
    parse_relative_time,
    resolve,
    resolve_registry_access,
    template_blocklist_vars,
)
from .token_store import token_name_slug

if TYPE_CHECKING:
    import voluptuous as vol
    from homeassistant.exceptions import ServiceValidationError

    # Type-only: helpers reaches the audit log through data.audit and never
    # imports the module at runtime. Annotations are lazy here, so this cannot
    # introduce a cycle.
    from .audit import Outcome
    from .data import PhoenixData
    from .rate_limiter import RateLimitResult
    from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)


def build_permitted_states(token: TokenRecord, hass: HomeAssistant) -> dict:
    """Return a {entity_id: ScrubbedState} dict for entities accessible to a token.

    resolve() is the single filter predicate (Phoenix MCP domain blocklist, Phoenix-platform
    sensor.phoenix_mcp_* block, pass-through WRITE, scoped READ/WRITE), plus the shared
    assist_expose_check for pass_through tokens with use_assist_exposure.

    This is the single source of truth for MCP template sandboxes. All template
    handlers must use this function so the filtering never diverges.
    """
    expose = assist_expose_check(token, hass)
    return {
        s.entity_id: ScrubbedState(s)
        for s in hass.states.async_all()
        if resolve(s.entity_id, token, hass) in (Permission.READ, Permission.WRITE)
        and (expose is None or expose(s.entity_id))
    }


def build_permitted_entity_ids(token: TokenRecord, hass: HomeAssistant) -> set:
    """Return the set of entity IDs accessible to a token, including registry-only entities.

    Unlike build_permitted_states (which needs current State objects), this function
    unions live states with the entity registry so that history and statistics endpoints
    can query recorder data for entities that are temporarily offline or disabled.
    Also applies use_assist_exposure filtering for pass_through tokens.
    """
    from homeassistant.helpers import entity_registry as er_mod

    registry = er_mod.async_get(hass)
    candidate_ids: set[str] = {s.entity_id for s in hass.states.async_all()}
    candidate_ids.update(entry.entity_id for entry in registry.entities.values())

    expose = assist_expose_check(token, hass)
    return {
        eid for eid in candidate_ids
        if resolve_registry_access(eid, token, hass) in (Permission.READ, Permission.WRITE)
        and (expose is None or expose(eid))
    }


def build_error_response(
    code: str,
    message: str,
    status: int,
    request_id: str,
    suggestions: list[str] | None = None,
) -> web.Response:
    """Return a JSON error response with an X-Phoenix-Request-ID header."""
    body: dict[str, Any] = {"error": code, "message": message}
    if suggestions:
        body["suggestions"] = suggestions
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(body),
        headers={"X-Phoenix-Request-ID": request_id},
    )


def service_not_found_hint(domain: str, service: str) -> tuple[str, list[str]] | None:
    """Actionable hint for a ServiceNotFound on a known core actuator domain.

    Returns (message, suggestions) naming the domain's valid core service verbs,
    or None if the domain is not in DOMAIN_SERVICE_HINTS (caller keeps the generic
    no-oracle "Forbidden."). Safe only in the ServiceNotFound catch, which is
    post-authorization: the token has already proven WRITE on an entity in this
    domain, so naming public HA-core verbs for it reveals nothing hidden. See
    DOMAIN_SERVICE_HINTS in const.py for the full leak-safety rationale.
    """
    services = DOMAIN_SERVICE_HINTS.get(domain)
    if not services:
        return None
    valid = list(services)
    message = (
        f"No service {domain}.{service}. Valid {domain} services include: "
        f"{', '.join(valid)}."
    )
    return message, valid


def get_client_ip(request: web.Request) -> str:
    """Return the remote IP address, or an empty string if unavailable."""
    return request.remote or ""


def log_request(
    data: PhoenixData,
    token: TokenRecord,
    *,
    request_id: str,
    method: str,
    resource: str,
    outcome: Outcome,
    client_ip: str,
    payload: dict | None = None,
    mesa_advisory: bool = False,
    stale_tools_advisory: bool = False,
) -> None:
    """Record an audit entry and update in-memory token counters."""
    # Attribute the request to the token's active settings preset, when one is
    # set. getattr-guarded so bare test doubles keep working.
    preset_name: str | None = None
    active_preset_id = getattr(token, "active_preset_id", None)
    if active_preset_id:
        preset_name = next(
            (p.name for p in getattr(token, "presets", []) if p.id == active_preset_id),
            None,
        )
    data.audit.record(
        request_id=request_id,
        token_id=token.id,
        token_name=token.name,
        method=method,
        resource=resource,
        outcome=outcome,
        client_ip=client_ip,
        settings=data.store.get_settings(),
        pass_through=token.pass_through,
        payload=payload,
        mesa_advisory=mesa_advisory,
        preset=preset_name,
        stale_tools_advisory=stale_tools_advisory,
    )
    update_token_counter(data, token.id, outcome)


def fire_rate_limit_events(hass: HomeAssistant, data: PhoenixData, token: TokenRecord) -> None:
    """Fire the phoenix_mcp_rate_limited bus event and optional persistent notification.

    The event fires on every 429, unthrottled.
    The persistent notification is throttled to at most once per token per minute
    to prevent notification flooding during sustained abuse.
    """
    # Event fires on every 429 - not throttled.
    hass.bus.async_fire("phoenix_mcp_rate_limited", {
        "token_id": token.id,
        "token_name": token.name,
        "timestamp": utcnow().isoformat(),
    })
    # Notification is throttled.
    settings = data.store.get_settings()
    if settings.notify_on_rate_limit:
        now_mono = time.monotonic()
        last = data.rate_limit_notified.get(token.id, 0.0)
        if now_mono - last >= 60.0:
            data.rate_limit_notified[token.id] = now_mono
            hass.async_create_task(
                hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "message": notification_text(
                            hass, "rate_limit.message", token=token.name),
                        "title": notification_text(hass, "rate_limit.title"),
                        "notification_id": f"phoenix_mcp_rate_limit_{token.id}",
                    },
                )
            )


def content_hash(content: str | bytes | dict | list) -> str:
    """Stable content hash for optimistic-concurrency (compare-and-swap) checks
    on Confirm-gated whole-blob config writes.

    A read tool returns this hash; the matching write tool accepts it as
    expected_hash and refuses if the target changed since the read (an admin or
    another agent edited it during the read/approve/apply window). For a JSON-able
    structure (a dashboard config) the hash is taken over a canonical
    (sorted-key, compact) serialization so key ordering never causes a false
    conflict; text/bytes are hashed as raw UTF-8. The value is opaque: agents
    echo it back verbatim, never recompute it.
    """
    if isinstance(content, (dict, list)):
        raw = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    elif isinstance(content, bytes):
        raw = content
    else:
        raw = str(content).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_time_param(value: str) -> datetime:
    """Parse a relative time string or ISO timestamp. Raises ValueError for unknown formats."""
    # HA's parse_datetime regex-matches its argument, so a non-string reaches it
    # as a TypeError rather than the ValueError every caller here handles.
    if not isinstance(value, str):
        raise ValueError(f"Unrecognized time format: {value!r}")
    try:
        return parse_relative_time(value)
    except ValueError:
        pass
    dt = parse_datetime(value)
    if dt is None:
        raise ValueError(f"Unrecognized time format: {value!r}")
    return dt


async def async_get_authenticated_token(
    hass: HomeAssistant,
    request: web.Request,
    data: PhoenixData,
    request_id: str,
    resource: str,
) -> tuple[TokenRecord, RateLimitResult] | web.Response:
    """Validate the Phoenix MCP bearer token and check rate limits.

    Returns (token, rl_result) on success, or an aiohttp Response on failure.
    Checks for kill switch, query-param token leakage, format pre-validation,
    hash lookup, revocation, expiry, and rate limits in that order.
    """
    if data.shutting_down:
        return build_error_response("service_unavailable", "Service unavailable.", 503, request_id)

    if data.store.get_settings().kill_switch:
        # Kill-switch mode should make Phoenix MCP invisible on the network. At startup
        # that is achieved by not registering any routes. At runtime, aiohttp
        # does not support unregistering routes, so 503 is the closest approximation.
        # This is a known architectural limitation; the routes exist but refuse service.
        return build_error_response("service_unavailable", "Service unavailable.", 503, request_id)

    _401 = build_error_response("unauthorized", "Unauthorized.", 401, request_id)
    _401.headers["WWW-Authenticate"] = 'Bearer realm="Phoenix MCP"'

    for key in ("token", "access_token"):
        if key in request.query:
            return _401

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _401

    presented = auth_header[7:]
    if not presented.startswith(TOKEN_PREFIX) or len(presented) != TOKEN_LENGTH:
        return _401

    token_hash = hashlib.sha256(presented.encode()).hexdigest()
    token = data.store.get_token_by_hash(token_hash)

    if token is None:
        return _401

    if not token.is_valid():
        if token.is_expired():
            await async_archive_expired_token(hass, data, token)
        return _401

    # Update last_used before the rate limit check so last_access reflects every
    # attempted request, not just allowed ones. This keeps last_access consistent
    # with request_count, which also increments on rate-limited requests.
    data.store.update_last_used(token.id, utcnow())

    rl_result = data.rate_limiter.check(
        token.id,
        token.rate_limit_requests,
        token.rate_limit_burst,
    )

    if not rl_result.allowed:
        fire_rate_limit_events(hass, data, token)
        log_request(
            data,
            token,
            request_id=request_id,
            method=request.method,
            resource=resource,
            outcome="rate_limited",
            client_ip=get_client_ip(request),
        )
        resp = build_error_response("rate_limited", "Rate limit exceeded.", 429, request_id)
        resp.headers["Retry-After"] = str(rl_result.retry_after)
        return resp

    return token, rl_result


def cancel_expiry_timer(data: PhoenixData, token_id: str) -> None:
    """Cancel and remove the pending expiry timer for a token, if one exists."""
    cancel = data.expiry_timers.pop(token_id, None)
    if cancel is not None:
        cancel()


def schedule_expiry_timer(hass: HomeAssistant, data: PhoenixData, token: TokenRecord) -> None:
    """Schedule a timer to archive a token at its expiry time.

    If the token has no expiry, or has already expired, no timer is scheduled.
    Any previously registered timer for this token is cancelled first.
    """
    if token.expires_at is None:
        return
    cancel_expiry_timer(data, token.id)
    delay = (token.expires_at - utcnow()).total_seconds()
    if delay <= 0:
        return

    @callback
    def _on_expiry(_now=None) -> None:
        data.expiry_timers.pop(token.id, None)
        hass.async_create_background_task(
            async_archive_expired_token(hass, data, token),
            f"phoenix_mcp_expire_{token.id}",
        )

    data.expiry_timers[token.id] = async_call_later(hass, delay, _on_expiry)


async def async_archive_expired_token(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
) -> None:
    """Move an expired token to the archive and perform full cleanup.

    Archives the record to storage, cancels its queued approvals, destroys rate
    limiter and counter state, fires the phoenix_mcp_token_expired bus event, and
    removes sensor entities.
    """
    from .approvals import (  # noqa: PLC0415 - avoid import cycle at module load
        REASON_TOKEN_EXPIRED,
        async_cancel_approvals_for_token,
        dismiss_approval_notification,
        fire_approval_resolved_event,
    )

    now = utcnow()
    slug = token_name_slug(token.name)
    cancel_expiry_timer(data, token.id)
    archived = await data.store.async_archive_token(token.id, revoked=False, revoked_at=now)
    if archived is None:
        return
    # Same queue hygiene as revoke: an expired token's approvals cannot be
    # legitimately approved anymore (re-validation would auto-reject them).
    async with data.store.async_lock:
        # Clear the Assist binding / voice-agent token if it pointed at this token
        # (rule 11), like revoke; the voice agent re-syncs to unregistered below.
        _s = data.store.get_settings()
        _patch: dict[str, Any] = {}
        if _s.assist_bound_token_id == token.id:
            _patch["assist_bound_token_id"] = None
        if _s.voice_agent_token_id == token.id:
            _patch["voice_agent_token_id"] = None
        if _s.ai_task_token_id == token.id:
            _patch["ai_task_token_id"] = None
        if _patch:
            await data.store.async_patch_settings(**_patch)
        cancelled = await async_cancel_approvals_for_token(
            data.store, token.id, REASON_TOKEN_EXPIRED,
            skip_ids=data.approvals_in_progress,
        )
    for approval in cancelled:
        dismiss_approval_notification(hass, approval.id)
        fire_approval_resolved_event(hass, approval)
    # Advisory leases the token still held; in-memory, so outside the store lock.
    from .mesa import release_token_leases  # noqa: PLC0415
    release_token_leases(data, token.id)
    if data.async_sync_voice_agent is not None:
        data.async_sync_voice_agent()
    if "voice_agent_token_id" in _patch:
        # The voice agent lost its token, so it is now unregistered; remove any
        # Phoenix-created Assist pipeline so a broken assistant is not left behind.
        from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
        await async_remove_assist_pipeline(hass, data)
    if "ai_task_token_id" in _patch and data.async_sync_ai_task is not None:
        # The AI Task lost its token; remove the AI Task entity from HA (and picker).
        data.async_sync_ai_task()
    data.rate_limiter.destroy(token.id)
    data.rate_limit_notified.pop(token.id, None)
    data.token_counters.pop(token.id, None)
    data.stale_tools_advised.pop(token.id, None)
    hass.bus.async_fire("phoenix_mcp_token_expired", {
        "token_id": token.id,
        "token_name": token.name,
        "timestamp": now.isoformat(),
    })
    if data.async_on_token_archived:
        try:
            await data.async_on_token_archived(slug)
        except Exception:
            _LOGGER.warning(
                "Sensor cleanup failed for expired token %s", token.id, exc_info=True,
            )


class _ContextProxy(dict):
    """Dict subclass that also supports attribute access.

    Used by ScrubbedState.context so templates can use both context.id and
    context | tojson without TypeError. Behaves as a plain dict for json.dumps().
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class ScrubbedState:
    """Lightweight State wrapper that strips sensitive attributes for use in template sandboxes."""

    def __init__(self, raw: Any) -> None:
        self.entity_id = raw.entity_id
        self.state = raw.state
        self.attributes = {k: v for k, v in raw.attributes.items() if k not in SENSITIVE_ATTRIBUTES}
        self.last_updated = getattr(raw, "last_updated", None)
        self.last_changed = getattr(raw, "last_changed", None)
        self.last_reported = getattr(raw, "last_reported", None)
        # Strip user_id from context to prevent HA user ID enumeration via templates.
        ctx = getattr(raw, "context", None)
        self.context: _ContextProxy | None
        if ctx is not None:
            self.context = _ContextProxy({
                "id": getattr(ctx, "id", None),
                "parent_id": getattr(ctx, "parent_id", None),
                "user_id": None,
            })
        else:
            self.context = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".")[0]

    @property
    def object_id(self) -> str:
        return self.entity_id.split(".", 1)[1] if "." in self.entity_id else self.entity_id

    @property
    def name(self) -> str:
        friendly = self.attributes.get("friendly_name")
        if friendly:
            return str(friendly)
        return self.object_id.replace("_", " ").title()

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": self.attributes,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "last_changed": self.last_changed.isoformat() if self.last_changed else None,
            "context": {
                "id": getattr(self.context, "id", None),
                "parent_id": getattr(self.context, "parent_id", None),
                "user_id": None,
            } if self.context is not None else None,
        }


class _DomainFilteredStates:
    """Iterable wrapper for a single domain's entities inside FilteredStates.

    Supports both iteration ({% for state in states.light %}) yielding
    ScrubbedState objects, and attribute access (states.light.living_room)
    returning individual entities by object_id.
    """

    def __init__(self, entities: dict) -> None:
        self._entities = entities

    def __iter__(self) -> Iterator[Any]:
        return iter(self._entities.values())

    def __len__(self) -> int:
        return len(self._entities)

    def __getattr__(self, object_id: str) -> Any:
        if object_id.startswith("_"):
            raise AttributeError(object_id)
        return self._entities.get(object_id)


class FilteredStates:
    """Callable proxy over a permitted-entity dict mimicking the HA template 'states' global.

    HA templates use 'states' as both a callable (states('sensor.foo')) and a
    domain-keyed accessor (states.light). A plain dict breaks the callable form,
    so this proxy implements both protocols while restricting access to permitted entities.
    """

    def __init__(self, permitted: dict) -> None:
        self._permitted = permitted

    def __call__(self, entity_id: str, default: str = "unknown") -> str:
        s = self._permitted.get(entity_id)
        return s.state if s is not None else default

    def __getitem__(self, entity_id: str) -> Any:
        return self._permitted.get(entity_id)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._permitted.values())

    def __len__(self) -> int:
        return len(self._permitted)

    def __getattr__(self, domain: str) -> Any:
        if domain.startswith("_"):
            raise AttributeError(domain)
        entities = {
            eid.split(".", 1)[1]: s
            for eid, s in self._permitted.items()
            if eid.split(".")[0] == domain
        }
        return _DomainFilteredStates(entities)


_SAFE_TEMPLATE_ENV = None


def safe_template_env() -> Any:
    """Return the cached hass-less TemplateEnvironment used for token template renders.

    A TemplateEnvironment constructed without hass never registers the hass-aware
    helpers (states, expand, area_entities, integration_entities, ...) as globals,
    filters, or tests, so entity access exists only through the permission-filtered
    variables Phoenix MCP injects. Rendering in the full hass environment and shadowing
    globals with render variables is NOT safe: Jinja2 variables never shadow
    filters or tests, so {{ 'sensor.x' | states }} would bypass the sandbox.
    """
    global _SAFE_TEMPLATE_ENV
    if _SAFE_TEMPLATE_ENV is None:
        from homeassistant.helpers.template import TemplateEnvironment  # noqa: PLC0415
        _SAFE_TEMPLATE_ENV = TemplateEnvironment(None)
    return _SAFE_TEMPLATE_ENV


def render_template_for_token(template_str: str, token: TokenRecord, hass: HomeAssistant) -> str:
    """Render a Jinja2 template against the token's permitted entity state.

    Renders in safe_template_env() with permission-restricted replacements for the
    HA state helpers and pure dt_util shims for the time helpers the hass-less
    environment lacks. Raises (TemplateError, ValueError, jinja2 errors) on any
    failure; callers map exceptions to an invalid_request response.
    """
    from homeassistant.helpers.template import MAX_TEMPLATE_OUTPUT  # noqa: PLC0415
    from homeassistant.exceptions import TemplateError  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    permitted = build_permitted_states(token, hass)
    filtered_states = FilteredStates(permitted)

    # Permission-restricted versions of the HA template state helpers.
    def _state_attr(entity_id: str, attr: str) -> Any:
        s = permitted.get(entity_id)
        return s.attributes.get(attr) if s is not None else None

    def _is_state(entity_id: str, value: str) -> bool:
        s = permitted.get(entity_id)
        return s is not None and s.state == value

    def _is_state_attr(entity_id: str, attr: str, value) -> bool:
        s = permitted.get(entity_id)
        return s is not None and s.attributes.get(attr) == value

    def _has_value(entity_id: str) -> bool:
        s = permitted.get(entity_id)
        return s is not None and s.state not in ("unknown", "unavailable")

    # Time helpers absent from the hass-less environment. These mirror HA's
    # DateTimeExtension implementations; they touch only dt_util, never entities.
    def _today_at(time_str: str = "") -> datetime:
        today = dt_util.start_of_local_day()
        if not time_str:
            return today
        parsed = dt_util.parse_time(time_str)
        if parsed is None:
            raise ValueError(f"could not convert str to datetime: '{time_str}'")
        return datetime.combine(today, parsed, today.tzinfo)

    def _localize(value: datetime) -> datetime:
        return value if value.tzinfo else dt_util.as_local(value)

    def _relative_time(value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        value = _localize(value)
        return value if dt_util.now() < value else dt_util.get_age(value)

    def _time_since(value: Any, precision: int = 1) -> Any:
        if not isinstance(value, datetime):
            return value
        value = _localize(value)
        return value if dt_util.now() < value else dt_util.get_age(value, precision)

    def _time_until(value: Any, precision: int = 1) -> Any:
        if not isinstance(value, datetime):
            return value
        value = _localize(value)
        return value if dt_util.now() > value else dt_util.get_time_remaining(value, precision)

    variables = {
        "states": filtered_states,
        "state_attr": _state_attr,
        "is_state": _is_state,
        "is_state_attr": _is_state_attr,
        "has_value": _has_value,
        "now": dt_util.now,
        "utcnow": dt_util.utcnow,
        "today_at": _today_at,
        "relative_time": _relative_time,
        "time_since": _time_since,
        "time_until": _time_until,
        # Defense in depth: these names do not exist in the hass-less environment,
        # but the enumeration helpers must return empty values rather than raise,
        # so keep the stubs as render variables.
        **template_blocklist_vars(),
    }

    compiled = safe_template_env().from_string(template_str)
    rendered = compiled.render(variables)
    if len(rendered) > MAX_TEMPLATE_OUTPUT:
        raise TemplateError(
            f"Template output exceeded maximum size of {MAX_TEMPLATE_OUTPUT} characters"
        )
    return rendered.strip()


# Ranks the level of a RECORD, which is why DEBUG and INFO stay here even though
# Home Assistant's system_log handler is attached at WARNING and stores neither:
# this map classifies whatever HA hands us, and dropping an entry would silently
# narrow what an operator can read if that ever changes. The narrower set a
# CALLER may ask for is const.LOG_LEVELS, and the two are deliberately different.
_LOG_LEVEL_RANK: dict[str, int] = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 3}
_PHOENIX_TOKEN_SCRUB_RE = re.compile(r"phx_[0-9a-f]{64}", re.IGNORECASE)
# Home Assistant long-lived access tokens (and other JWTs) are three base64url
# segments whose header always begins "eyJ". Redact so a leaked LLAT in a log
# line is not handed back to a token holding cap_log_read.
_JWT_SCRUB_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
# Credentials embedded in URLs/log text: query params (token=..., api_password=...)
# and userinfo (https://user:pass@host). Over-redaction in logs is acceptable.
_URL_CRED_QUERY_RE = re.compile(
    r"(?i)(access_token|refresh_token|api_password|password|api_key|apikey|client_secret|secret|token|auth)=[^\s&\"';]+"
)
_URL_CRED_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s:@]+@")
_PHOENIX_LOGGER_PREFIXES = ("homeassistant.components.phoenix_mcp", "custom_components.phoenix_mcp")


def _scrub_log_text(text: str) -> str:
    """Redact Phoenix MCP tokens, JWTs/LLATs, and URL-embedded credentials from a log line."""
    text = _PHOENIX_TOKEN_SCRUB_RE.sub("<phoenix-token>", text)
    text = _JWT_SCRUB_RE.sub("<token>", text)
    text = _URL_CRED_QUERY_RE.sub(r"\1=<redacted>", text)
    text = _URL_CRED_USERINFO_RE.sub("://<redacted>@", text)
    return text


# A "key: value" or "key = value" line in a YAML/config diff, capturing the
# leading key so its value can be redacted when the key name looks sensitive.
_CONFIG_SECRET_LINE_RE = re.compile(r"^(\s*)([\w.\-]+)(\s*[:=]\s*)(\S.*)$")


def redact_secrets_in_text(text: str | None) -> str | None:
    """Redact secret-valued config lines and embedded credentials from diff text.

    Applied to approval diffs (file writes, configuration.yaml edits) before they
    persist to .storage so secrets are not copied to disk verbatim. A line whose
    key name is sensitive (is_sensitive_key) has its value replaced; JWTs and
    URL-embedded credentials anywhere in the text are scrubbed too. The structure
    of the change stays visible to the reviewing admin.
    """
    if not text:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        m = _CONFIG_SECRET_LINE_RE.match(line)
        if m is not None and is_sensitive_key(m.group(2)):
            out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>")
        else:
            out.append(line)
    return _scrub_log_text("\n".join(out))


def redact_structure(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets from a JSON-able structure.

    A dict value whose key name is sensitive (is_sensitive_key) becomes
    "<redacted>"; string values are scrubbed for secret-valued config lines and
    embedded credentials (redact_secrets_in_text). Other scalars pass through
    unchanged. Used for audit payloads and the admin-facing copy of approval
    args so secrets are never serialised verbatim. Recursion is depth-bounded so
    a pathologically nested payload cannot raise RecursionError on the logging
    path; subtrees past the limit collapse to "<redacted>".
    """
    if _depth > 25:
        return "<redacted>"
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if is_sensitive_key(k) else redact_structure(v, _depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_structure(item, _depth + 1) for item in obj]
    if isinstance(obj, str):
        return redact_secrets_in_text(obj)
    return obj


# Network-topology scrubbing for integration-defined diagnostics (get_system_health).
# Phoenix MCP already withholds network topology / host layout from agents elsewhere
# (build_safe_config drops internal_url/external_url/config_dir/paths from get_config),
# but system_health values are integration-defined free-form strings that can carry the
# same infrastructure detail (LAN IPs, hostnames inside URLs, filesystem paths), which
# redact_structure (secret-keyed values + embedded credentials only) does not catch.
# These patterns are deliberately conservative. The IPv4 set is restricted to
# PRIVATE/loopback/link-local ranges, so a public-IP-shaped version string like
# "4.8.0.1" is NOT matched (it is outside these ranges); that avoids the main
# false-positive while still scrubbing the LAN topology that is the actual concern.
_PRIVATE_IPV4_RE = re.compile(
    r"\b(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"                  # 10.0.0.0/8
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"               # loopback 127.0.0.0/8
    r"|169\.254\.\d{1,3}\.\d{1,3}"                   # link-local 169.254.0.0/16
    r"|192\.168\.\d{1,3}\.\d{1,3}"                   # 192.168.0.0/16
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"  # 172.16.0.0/12
    r")\b"
)
# Link-local (fe80::/10) and unique-local (fc00::/7) IPv6.
_PRIVATE_IPV6_RE = re.compile(
    r"\b(?:fe80|f[cd][0-9a-f]{2})(?::[0-9a-f]{0,4}){2,7}\b", re.IGNORECASE
)
# Bare http(s) URL (host + optional port/path). URL-embedded credentials are already
# scrubbed upstream; this removes the host/topology itself.
_BARE_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
# Absolute filesystem paths: unix /a/b... (>=2 segments, so a lone "/" or a URL path
# already handled above is not matched) and Windows drive paths C:\a\b\... .
_UNIX_PATH_RE = re.compile(r"(?<![\w/])/(?:[\w.\-]+/)+[\w.\-]+")
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[\w.\-]+\\?){2,}")


def _scrub_network_topology(text: str) -> str:
    """Replace LAN IPs, bare URLs, and absolute filesystem paths with placeholders."""
    text = _BARE_URL_RE.sub("<redacted-url>", text)
    text = _PRIVATE_IPV4_RE.sub("<redacted-ip>", text)
    text = _PRIVATE_IPV6_RE.sub("<redacted-ip>", text)
    text = _WIN_PATH_RE.sub("<redacted-path>", text)
    text = _UNIX_PATH_RE.sub("<redacted-path>", text)
    return text


def _scrub_diagnostic_str(text: str) -> str:
    """Full diagnostic-string scrub: embedded credentials/JWTs then network topology."""
    return _scrub_network_topology(redact_secrets_in_text(text) or "")


def redact_diagnostics(obj: Any, _depth: int = 0) -> Any:
    """redact_structure plus a conservative network-topology scrub for diagnostics.

    get_system_health values are integration-defined and can disclose LAN IPs,
    hostnames-in-URLs, and filesystem paths that Phoenix MCP already withholds elsewhere
    (build_safe_config). Layered on redact_structure (secret-keyed values + embedded
    credentials), each string is also scrubbed for private/loopback/link-local IPs,
    bare URLs, and absolute paths. Because integration-defined payloads are free-form,
    dict KEYS get the same string scrub as values (an integration may key its health
    data by a URL, LAN IP, or path); a sensitive-named key still redacts its value.
    cap_diagnostics is an elevated read, so a little over-redaction is acceptable; the
    diagnostic shape is preserved.
    """
    if _depth > 25:
        return "<redacted>"
    if isinstance(obj, dict):
        return {
            (_scrub_diagnostic_str(k) if isinstance(k, str) else k):
                "<redacted>" if is_sensitive_key(k) else redact_diagnostics(v, _depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_diagnostics(item, _depth + 1) for item in obj]
    if isinstance(obj, str):
        return _scrub_diagnostic_str(obj)
    return obj


# Config keys safe to disclose to a cap_config_read token. Allowlist, not denylist,
# so a new HA config key defaults to excluded. Deliberately omits precise location
# (latitude/longitude/elevation/radius), internal_url/external_url, and filesystem
# paths (config_dir, allowlist_external_dirs/urls, media_dirs) which reveal home
# coordinates, network topology, and host layout beyond what an agent needs.
_SAFE_CONFIG_KEYS = frozenset({
    "location_name", "time_zone", "unit_system", "currency", "country",
    "language", "version", "config_source", "state", "safe_mode",
    "recovery_mode", "components",
})


def build_safe_config(hass: HomeAssistant) -> dict:
    """Return the cap_config_read-safe subset of hass.config.as_dict().

    Agent-useful context (HA version, time zone, units, location name, loaded
    components for capability detection) with the sensitive fields removed; Phoenix MCP's
    own components are stripped so a token cannot enumerate our routes.
    """
    raw = hass.config.as_dict()
    safe = {k: raw[k] for k in _SAFE_CONFIG_KEYS if k in raw}
    if "components" in safe:
        safe["components"] = sorted(
            c for c in safe["components"]
            if c != DOMAIN and not c.startswith(DOMAIN + ".")
        )
    return safe


class SystemLogUnavailableError(Exception):
    """Raised when the system-log source is absent."""

    status = "unavailable"


class SystemLogDegradedError(Exception):
    """Raised when the system-log source exists but cannot be read safely."""

    status = "degraded"


def check_system_log_compat(hass: HomeAssistant) -> str | None:
    """Best-effort probe that hass.data["system_log"] still has the shape get_logs reads.

    Runtime reads validate the same shape and fail loudly. This startup probe is
    retained as an early operator warning. Returns None when compatible OR when
    system_log is absent entirely, and a short reason string on shape drift.
    """
    try:
        syslog = hass.data.get("system_log")
        if syslog is None:
            return None
        records = getattr(syslog, "records", None)
        if records is None:
            return "system_log data has no 'records' attribute"
        if not callable(getattr(records, "values", None)):
            return "system_log records is no longer a dict-like mapping"
        return None
    except Exception as err:  # noqa: BLE001 - advisory probe must never abort setup
        return f"system_log probe could not run: {err}"


@dataclass(frozen=True)
class LogEntryPage:
    """One stateless page over Home Assistant's live deduplicated log ring."""

    entries: list[dict]
    matched_buckets: int
    has_more: bool
    next_cursor: str | None
    source: dict[str, Any]
    filters: dict[str, Any]
    warnings: list[str]


_LOG_CURSOR_VERSION = 1
_LOG_REDACTED_PATH = "<redacted-path>"
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ULID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", re.IGNORECASE)
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)
_ENTITY_ID_TEXT_RE = re.compile(r"(?<![\w.])([a-z0-9_]+\.[a-z0-9_]+)(?![\w.])")


def _logger_prefix_matches(logger_name: str, prefix: str) -> bool:
    """Match one complete logger namespace, never an adjacent lookalike."""
    return logger_name == prefix or logger_name.startswith(prefix + ".")


def _log_iso(timestamp: float) -> str:
    """Return a stable UTC ISO timestamp for one system-log epoch value."""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _log_timestamp(value: Any) -> float | None:
    """Coerce one system-log timestamp without accepting booleans or infinities."""
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        value = value.timestamp()
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _safe_log_path(path: Any, hass: HomeAssistant) -> str:
    """Return only a normalized HA package or custom-component relative path."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return _LOG_REDACTED_PATH
    normalized = path.replace("\\", "/")
    config_dir = str(getattr(hass.config, "config_dir", "") or "").replace("\\", "/").rstrip("/")
    if config_dir and normalized.startswith(config_dir + "/"):
        normalized = normalized[len(config_dir) + 1 :]
    elif "/custom_components/" in normalized:
        normalized = "custom_components/" + normalized.split("/custom_components/", 1)[1]
    elif "/homeassistant/" in normalized:
        normalized = "homeassistant/" + normalized.split("/homeassistant/", 1)[1]

    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return _LOG_REDACTED_PATH
    safe_roots = ("homeassistant", "components", "custom_components")
    if not pure.parts or pure.parts[0] not in safe_roots:
        return _LOG_REDACTED_PATH
    return pure.as_posix()


def _safe_log_location(value: Any, hass: HomeAssistant, *, root_cause: bool) -> dict | None:
    """Normalize a system-log source or root-cause tuple without host paths."""
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    line = value[1]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        line = None
    result: dict[str, Any] = {"file": _safe_log_path(value[0], hass), "line": line}
    if root_cause and len(value) >= 3:
        function = value[2]
        result["function"] = function if isinstance(function, str) and function.isidentifier() else None
    return result


def _strict_phoenix_log_text(text: str, token: TokenRecord, hass: HomeAssistant) -> str:
    """Apply the stronger Phoenix self-diagnostics text boundary."""
    scrubbed = _scrub_diagnostic_str(_scrub_log_text(text))
    scrubbed = _UUID_RE.sub("<redacted-id>", scrubbed)
    scrubbed = _ULID_RE.sub("<redacted-id>", scrubbed)
    scrubbed = _LONG_HEX_RE.sub("<redacted-id>", scrubbed)

    def _entity_replacement(match: re.Match[str]) -> str:
        entity_id = match.group(1)
        try:
            permission = resolve(entity_id, token, hass)
        except Exception:
            return "<redacted-entity>"
        if permission in (Permission.READ, Permission.WRITE):
            return entity_id
        return "<redacted-entity>"

    return _ENTITY_ID_TEXT_RE.sub(_entity_replacement, scrubbed)


def _log_filter_fingerprint(filters: dict[str, Any], phoenix_only: bool) -> str:
    """Hash the effective filters so a cursor cannot silently change queries."""
    payload = {**filters, "phoenix_only": phoenix_only}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _encode_log_cursor(
    *,
    snapshot_at: float,
    timestamp: float,
    bucket_id: str,
    filter_hash: str,
    since: float | None,
    until: float | None,
) -> str:
    payload = {
        "v": _LOG_CURSOR_VERSION,
        "snapshot": snapshot_at,
        "timestamp": timestamp,
        "bucket": bucket_id,
        "filters": filter_hash,
        "since": since,
        "until": until,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_log_cursor(
    cursor: str, filter_hash: str
) -> tuple[float, tuple[float, str], float | None, float | None]:
    """Decode and bind one opaque log cursor to the current effective filters."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(payload, dict) or payload.get("v") != _LOG_CURSOR_VERSION:
            raise ValueError
        snapshot = _log_timestamp(payload.get("snapshot"))
        timestamp = _log_timestamp(payload.get("timestamp"))
        since = _log_timestamp(payload.get("since")) if payload.get("since") is not None else None
        until = _log_timestamp(payload.get("until")) if payload.get("until") is not None else None
        bucket = payload.get("bucket")
        if (
            snapshot is None
            or timestamp is None
            or not isinstance(bucket, str)
            or len(bucket) != 64
            or any(character not in "0123456789abcdef" for character in bucket)
            or payload.get("filters") != filter_hash
        ):
            raise ValueError
    except Exception as err:
        raise ValueError("Invalid cursor for these log filters.") from err
    return snapshot, (timestamp, bucket), since, until


def _log_bucket_id(record: Any) -> str:
    """Return a non-reversible deterministic tie-breaker for one deduplicated bucket."""
    identity = getattr(record, "key", None)
    if identity is None:
        identity = (
            getattr(record, "name", None),
            getattr(record, "source", None),
            getattr(record, "root_cause", None),
        )
    return hashlib.sha256(repr(identity).encode(errors="replace")).hexdigest()


def _log_messages(value: Any) -> list[str] | None:
    """Copy Home Assistant's retained message variants without accepting mappings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return None
    try:
        messages = list(value)
    except Exception:
        return None
    if not messages or not all(isinstance(item, str) for item in messages):
        return None
    return messages


def _log_exception(value: Any) -> str | None:
    """Normalize Home Assistant's current string and older iterable exception shapes."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return "".join(value) or None
    return None


def collect_log_entries(
    hass: HomeAssistant,
    level: str,
    integration: str | None,
    logger: str | None,
    search: str | None,
    since: datetime | None,
    until: datetime | None,
    since_query: str | None,
    until_query: str | None,
    limit: int,
    cursor: str | None,
    *,
    phoenix_only: bool = False,
    token: TokenRecord | None = None,
) -> LogEntryPage:
    """Read, validate, scrub, filter, and paginate the live system-log ring."""
    min_rank = _LOG_LEVEL_RANK.get(level.upper(), _LOG_LEVEL_RANK["WARNING"])
    syslog = hass.data.get("system_log")
    if syslog is None:
        raise SystemLogUnavailableError("the system_log integration is not loaded")
    records = getattr(syslog, "records", None)
    values = getattr(records, "values", None)
    if records is None or not callable(values):
        raise SystemLogDegradedError("system_log records is no longer a mapping")

    capacity = getattr(records, "maxlen", None)
    capacity_valid = isinstance(capacity, int) and not isinstance(capacity, bool) and capacity > 0
    warnings: list[str] = []
    if not capacity_valid:
        capacity = None
        warnings.append("The system-log ring capacity is unavailable on this Home Assistant version.")

    cursor_filters = {
        "level": level,
        "integration": integration,
        "logger": logger,
        "search": search,
        "since": since_query,
        "until": until_query,
        "time_basis": "latest_occurrence",
    }
    filter_hash = _log_filter_fingerprint(cursor_filters, phoenix_only)
    if cursor:
        snapshot_at, cursor_key, cursor_since, cursor_until = _decode_log_cursor(cursor, filter_hash)
        since_ts = cursor_since
        until_ts = cursor_until
    else:
        snapshot_at, cursor_key = utcnow().timestamp(), None
        since_ts = since.timestamp() if since else None
        until_ts = until.timestamp() if until else None
    effective_filters = {
        **cursor_filters,
        "since": _log_iso(since_ts) if since_ts is not None else None,
        "until": _log_iso(until_ts) if until_ts is not None else None,
    }
    entries_with_keys: list[tuple[tuple[float, str], dict]] = []
    retained_first: list[float] = []
    retained_latest: list[float] = []
    skipped = 0
    try:
        raw_records = list(values())
    except Exception as err:
        raise SystemLogDegradedError(
            "system_log records could not be read"
        ) from err
    for record in raw_records:
        record_level = getattr(record, "level", "")
        logger_name = getattr(record, "name", "")
        latest = _log_timestamp(getattr(record, "timestamp", None))
        first = _log_timestamp(getattr(record, "first_occurred", None))
        messages = _log_messages(getattr(record, "message", None))
        count = getattr(record, "count", 1)
        if (
            not isinstance(record_level, str)
            or record_level not in _LOG_LEVEL_RANK
            or not isinstance(logger_name, str)
            or not logger_name
            or latest is None
            or first is None
            or messages is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            skipped += 1
            continue
        retained_first.append(first)
        retained_latest.append(latest)
        is_phoenix = any(
            _logger_prefix_matches(logger_name, prefix)
            for prefix in _PHOENIX_LOGGER_PREFIXES
        )
        if phoenix_only != is_phoenix:
            continue
        if _LOG_LEVEL_RANK[record_level] < min_rank:
            continue
        if integration:
            if not (
                _logger_prefix_matches(
                    logger_name, f"homeassistant.components.{integration}"
                )
                or _logger_prefix_matches(
                    logger_name, f"custom_components.{integration}"
                )
            ):
                continue
        if logger and not _logger_prefix_matches(logger_name, logger):
            continue
        if since_ts is not None and latest < since_ts:
            continue
        if until_ts is not None and latest > until_ts:
            continue
        if latest > snapshot_at:
            continue

        scrub: Callable[[str], str] = _scrub_log_text
        if phoenix_only:
            if token is None:
                raise SystemLogDegradedError("Phoenix diagnostics token context is unavailable")
            scrub = lambda value: _strict_phoenix_log_text(value, token, hass)
        safe_messages = [scrub(message) for message in messages]
        exception = _log_exception(getattr(record, "exception", None))
        safe_exception = scrub(exception) if exception else None
        if search:
            needle = search.casefold()
            searchable = [*safe_messages, safe_exception or ""]
            if not any(needle in value.casefold() for value in searchable):
                continue

        bucket_id = _log_bucket_id(record)
        sort_key = (latest, bucket_id)
        entries_with_keys.append((sort_key, {
            "first_occurred": _log_iso(first),
            "latest_occurred": _log_iso(latest),
            "level": record_level,
            "logger": logger_name,
            "messages": safe_messages,
            "exception": safe_exception,
            "occurrences": count,
            "source": _safe_log_location(getattr(record, "source", None), hass, root_cause=False),
            "root_cause": _safe_log_location(getattr(record, "root_cause", None), hass, root_cause=True),
        }))

    entries_with_keys.sort(key=lambda item: item[0], reverse=True)
    matched_buckets = len(entries_with_keys)
    if cursor_key is not None:
        entries_with_keys = [item for item in entries_with_keys if item[0] < cursor_key]
    page_items = entries_with_keys[:limit]
    has_more = len(entries_with_keys) > limit
    next_cursor = None
    if has_more and page_items:
        last_key = page_items[-1][0]
        next_cursor = _encode_log_cursor(
            snapshot_at=snapshot_at,
            timestamp=last_key[0],
            bucket_id=last_key[1],
            filter_hash=filter_hash,
            since=since_ts,
            until=until_ts,
        )
    if skipped:
        warnings.append(f"Skipped {skipped} malformed system-log bucket(s).")
    if cursor:
        warnings.append(
            "Pagination is best-effort over a live deduplicated ring; buckets updated after the first page may move."
        )
    source = {
        "status": "degraded" if skipped or not capacity_valid else "available",
        "kind": "home_assistant_system_log",
        "semantics": "deduplicated_buckets",
        "pagination": "best_effort_live_ring",
        "capacity": capacity,
        "retained_buckets": len(raw_records),
        "skipped_buckets": skipped,
        "earliest_first_occurred": _log_iso(min(retained_first)) if retained_first else None,
        "latest_occurred": _log_iso(max(retained_latest)) if retained_latest else None,
        "read_at": _log_iso(snapshot_at),
        "recorded_levels": ["WARNING", "ERROR", "CRITICAL"],
    }
    return LogEntryPage(
        entries=[entry for _, entry in page_items],
        matched_buckets=matched_buckets,
        has_more=has_more,
        next_cursor=next_cursor,
        source=source,
        filters=effective_filters,
        warnings=warnings,
    )


@callback
def flush_sensor_writes(data: PhoenixData, _now=None) -> None:
    """Write HA state for every token marked dirty since the last flush.

    Runs at most once per debounce window (scheduled by update_token_counter).
    Tokens revoked/archived in the window drop out naturally: their
    token_id_sensors entry is already gone.

    Must be @callback: async_write_ha_state() requires the event loop thread.
    Without this decorator, HA's HassJob type-inference sees a plain sync
    function passed to async_call_later and dispatches it to the executor
    thread pool instead, which is exactly the thread-safety violation this
    decorator prevents (HA logs "calls async_write_ha_state from a thread
    other than the event loop").
    """
    data.sensor_flush_cancel = None
    dirty = data.sensor_write_dirty
    data.sensor_write_dirty = set()
    for token_id in dirty:
        for sensor in data.token_id_sensors.get(token_id, []):
            if sensor.hass is not None:
                sensor.async_write_ha_state()


def update_token_counter(data: PhoenixData, token_id: str, outcome: str) -> None:
    """Increment the in-memory request/denied/rate-limit counters for a token.

    Counters are initialised on first use and read by sensor.py and the admin
    stats view (the admin API reads data.token_counters directly, so it is
    always current). Sensor state writes are debounced: request_count and
    last_access change on EVERY request, and an immediate async_write_ha_state
    per request meant one state_changed event and recorder row per agent call
    (60+/min at the default rate limit). The first request after a flush
    schedules one write SENSOR_WRITE_DEBOUNCE_SECONDS out; requests inside the
    window coalesce into it.
    """
    if token_id not in data.token_counters:
        data.token_counters[token_id] = {
            "request_count": 0,
            "denied_count": 0,
            "rate_limit_hits": 0,
        }
    counters = data.token_counters[token_id]
    counters["request_count"] += 1
    if outcome in ("denied", "not_found"):
        counters["denied_count"] += 1
    elif outcome == "rate_limited":
        counters["rate_limit_hits"] += 1

    data.sensor_write_dirty.add(token_id)
    if data.hass is None:
        # Direct constructions (tests) have no loop to schedule on; keep the
        # old immediate-write behavior.
        flush_sensor_writes(data)
        return
    if data.sensor_flush_cancel is None:
        data.sensor_flush_cancel = async_call_later(
            data.hass,
            SENSOR_WRITE_DEBOUNCE_SECONDS,
            partial(flush_sensor_writes, data),
        )


# Capability evaluation
def dict_arg(value: object) -> dict:
    """A tool argument declared as an object, coerced to a dict.

    The dict sibling of str_arg, and the same degrade-to-absent rule: a model
    that sends a list or a bare string where a config object was declared gets
    the default, not a coerced interpretation of what it typed.

    Preferred over the `args.get(k) if isinstance(args.get(k), dict) else {}`
    idiom, which calls .get twice and, because the second call is a fresh lookup,
    leaves the result unnarrowed for a reader and a type checker alike.

    Always returns a dict, like str_arg always returns a str. A caller that must
    tell "absent" from "empty" (the card diff builders, where a missing card
    means the diff has no `after` side at all) cannot use this and says so
    inline instead.
    """
    return value if isinstance(value, dict) else {}


def sanitize_service_data(value: object) -> dict[str, Any]:
    """Caller-supplied service data with every HA target selector removed.

    Phoenix MCP resolves and flattens targets itself and then calls the service
    with an explicit entity list, so a selector arriving inside service data is
    never something to honour: Home Assistant unions it with that list, reaching
    entities the permission tree, the capability gates and MESA never evaluated.
    Stripping is therefore the only correct handling; a call is not refused for
    carrying one, because the resolved targets already say what it may reach.

    The keys are const.TARGET_SELECTOR_KEYS, which is pinned against HA's own
    definition, so a release that adds a selector is a failing test rather than a
    silently wider call.

    Returns a NEW dict and never mutates the caller's, which also gives every
    call site the snapshot it needs: evaluation suspends at an await, and a
    caller that still holds the original could otherwise swap the target between
    the check and the call.
    """
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k not in TARGET_SELECTOR_KEYS}


def str_arg(value: object, default: str = "") -> str:
    """A tool argument declared as a string, coerced to one.

    A model does not always follow the tool schema: a list where a string was
    declared reaches HA's matcher and raises. The established answer (matching
    resolve_intent_entities' name/area/floor coercion) is to degrade a
    wrong-shaped value to "absent" rather than to interpret it, so a call fails
    closed on its own terms instead of crashing into the dispatcher's catch-all
    and reporting a generic internal error.

    Deliberately NOT str(value): stringifying True to "True" or ["a"] to
    "['a']" invents an argument the caller never sent, which then has to be
    refused further down with a message about a value nobody typed.
    """
    return value if isinstance(value, str) else default


def str_list_arg(value: object) -> list[str]:
    """A tool argument declared as a string or a list of strings, coerced.

    Non-string members are dropped rather than stringified, for the reason in
    str_arg; a comma-joined string is NOT split here, since only some tools
    accept that form and each does its own splitting.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


# ---------------------
# async_evaluate_capability returns one of three results:
#   ("allow", None)          -> proceed to side-effect.
#   ("deny", None)           -> return forbidden to caller.
#   ("confirm", approval_id) -> create pending approval, return pending response.
#
# effective_cap collapses pass_through interaction into a single value used by
# self-summary endpoints. It is NOT a substitute for async_evaluate_capability when
# enforcing a check, because it does not go through the approval queue.


def effective_cap(token: TokenRecord, cap_name: str) -> str:
    """Return the cap mode after applying pass_through interaction rules.

    Exempt caps are unaffected by pass_through. For non-exempt caps under
    pass_through, "deny" becomes "allow" but "confirm" is preserved
    (the admin's intent to gate is honored).
    """
    raw = getattr(token, cap_name, CAP_DENY)
    if cap_name in PASS_THROUGH_EXEMPT_CAPS:
        return raw
    if token.pass_through:
        if raw == CAP_CONFIRM:
            return CAP_CONFIRM
        return CAP_ALLOW
    return raw


def token_has_write_scope(token: TokenRecord) -> bool:
    """True if the token can write to at least one entity.

    Used to decide whether to announce the control tools (call_service, the
    native Hass* action tools) in the MCP tools/list. Pass-through always has
    write scope; otherwise any GREEN grant in the permission tree counts. This
    is an advisory over-approximation (a GREEN under a RED ancestor still counts
    here), which is fine: the per-call permission check is the real gate.
    """
    if token.pass_through:
        return True
    tree = token.permissions
    for nodes in (tree.domains, tree.devices, tree.entities):
        for node in nodes.values():
            if node.state == "GREEN":
                return True
    return False


def effective_caps(token: TokenRecord) -> dict[str, str]:
    """Return the full cap_*->effective_mode mapping for a token."""
    return {name: effective_cap(token, name) for name in CAPABILITY_NAMES}


@dataclass
class CapabilityResult:
    """Outcome of an async_evaluate_capability call.

    mode is one of "allow" / "deny" / "confirm". When mode is "confirm",
    approval is the freshly created PendingApproval record and the caller
    must return a pending response without executing.
    """

    mode: str
    approval: Any | None = None

    @property
    def is_allow(self) -> bool:
        return self.mode == CAP_ALLOW

    @property
    def is_deny(self) -> bool:
        return self.mode == CAP_DENY

    @property
    def is_pending(self) -> bool:
        return self.mode == CAP_CONFIRM


# An approval diff, either already built or deferred behind a zero-arg builder.
# The builder form exists because most builders read config off disk (or call the
# ESPHome add-on) while the diff itself is read only when an approval is created,
# so an allow or a deny would otherwise pay for a result nobody looks at.
DiffSource = dict | Callable[[], "dict | Awaitable[dict]"] | None


async def _resolve_diff(diff: DiffSource) -> dict:
    """Resolve a diff given either as a built dict or as a deferred builder.

    A diff is only ever read when an approval is created, so the expensive
    builders (which read configuration off disk, load a lovelace config, or call
    the ESPHome add-on) are passed as a zero-arg callable and invoked here, on
    the confirm path alone. Passing a plain dict stays supported for the cheap
    builders that need no I/O.

    Awaitability is tested with collections.abc.Awaitable (an __await__ protocol
    check) rather than inspect, matching the project's standing rule against
    inspect on anything HA-adjacent, so sync and async builders can be mixed
    freely at the call sites.
    """
    if diff is None:
        return {}
    if not callable(diff):
        return diff or {}
    built = diff()
    if isinstance(built, Awaitable):
        built = await built
    return built or {}


async def async_evaluate_capability(
    cap_name: str,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    tool_name: str,
    args: dict,
    request_id: str,
    diff: DiffSource = None,
    client_ip: str | None = None,
) -> CapabilityResult:
    """Resolve a capability check into Allow / Deny / Pending(approval).

    Reads the effective cap mode (after pass-through interaction) and either
    permits, denies, or creates a pending approval and returns a Confirm result.
    Diff is supplied by the caller; it appears in the admin review UI. It may be
    a dict or a zero-arg builder, and a builder is invoked only on the Confirm
    path, so an allowed or denied call never pays for a diff nobody reads.
    """
    from .approvals import (  # noqa: PLC0415
        create_approval_notification,
        async_create_pending_approval,
        fire_approval_requested_event,
    )

    mode = effective_cap(token, cap_name)
    if mode == CAP_ALLOW:
        return CapabilityResult(CAP_ALLOW)
    if mode == CAP_DENY:
        return CapabilityResult(CAP_DENY)
    # Only now is the diff actually needed, so a deferred builder runs here and
    # nowhere else. Both early returns above leave it untouched.
    resolved_diff = await _resolve_diff(diff)
    async with data.store.async_lock:
        approval = await async_create_pending_approval(
            data.store,
            token_id=token.id,
            token_name=token.name,
            tool_name=tool_name,
            cap_name=cap_name,
            args=args,
            diff=resolved_diff,
            request_id=request_id,
            client_ip=client_ip,
        )
    create_approval_notification(hass, approval)
    fire_approval_requested_event(hass, approval)
    return CapabilityResult(CAP_CONFIRM, approval=approval)


def validation_error_message(err: ServiceValidationError | vol.Invalid) -> str:
    """Human-readable message for a service validation error surfaced to the agent.

    Shared by ServiceValidationError (HA's own per-domain validation) and
    vol.Invalid/MultipleInvalid (the service's raw schema check in
    hass.services.async_call, which HA re-raises unwrapped rather than
    converting to a HomeAssistantError - the same catch that already handles
    ServiceValidationError does not see it). A ServiceValidationError may render
    as an empty string when it carries only a translation key (translations are
    not loaded in-process), so fall back to a generic message rather than
    returning "Invalid request: ".
    """
    detail = str(err).strip()
    return f"Invalid request: {detail}" if detail else "Invalid request."


def diff_summary_fields(key: str, **params: object) -> dict[str, object]:
    """Build a diff's three summary fields from one template.

    Returns the English `summary` exactly as the old f-strings produced it, plus
    the `summary_key` and `summary_params` the panel needs to render the same
    sentence in the operator's language. Splat it into a diff dict.

    One template per key, held in const.DIFF_SUMMARY_TEMPLATES, is what keeps
    the stored English and the translated form from drifting: both are that one
    string. A KeyError here means a builder named a key that does not exist,
    which is a bug worth failing loudly on rather than degrading to a blank
    summary on the surface an admin approves from.
    """
    return _summary_fields("diff", DIFF_SUMMARY_TEMPLATES, key, params)


def version_summary_fields(key: str, **params: object) -> dict[str, object]:
    """The same three fields for a version record's one-line change summary.

    A separate namespace from the diff templates because it is a separate
    surface: a diff describes a change being proposed, a version record one
    already applied.
    """
    return _summary_fields("version", VERSION_SUMMARY_TEMPLATES, key, params)


def _summary_fields(
    namespace: str, templates: dict[str, str], key: str, params: dict[str, object]
) -> dict[str, object]:
    return {
        "summary": templates[key].format(**params),
        "summary_key": f"{namespace}.{key}",
        "summary_params": params,
    }


def notification_text(hass: object, key: str, **params: object) -> str:
    """One persistent-notification string in the instance's language.

    A notification is written once and shown to every admin, so unlike the panel
    there is no per-viewer language: this resolves against hass.config.language.
    Reads the same catalogs/<lang>.json the panel does, so a translator only
    ever edits one file, and falls back to the English template whenever the
    catalog cannot be read or lacks the key.
    """
    language = getattr(getattr(hass, "config", None), "language", None)
    return _localized(NOTIFICATION_TEMPLATES, "notification", key, language, params)


def voice_text(language: object, key: str, **params: object) -> str:
    """One spoken voice-agent sentence, in the CONVERSATION's language.

    The opposite resolution from notification_text: a spoken reply goes to
    whoever is talking, and HA hands the conversation's own language to
    async_process, so that is what it follows. Falls back to English for an
    unknown language exactly like the notification path.
    """
    return _localized(VOICE_TEMPLATES, "voice", key, language, params)


def _localized(
    templates: dict[str, str], section: str, key: str, language: object, params: dict
) -> str:
    """Resolve one backend-rendered string, falling back to its English template."""
    template = templates[key]
    if language and not str(language).lower().startswith("en"):
        translated = _catalog_section(str(language), section).get(key)
        if translated:
            template = translated
    return template.format(**params)


@lru_cache(maxsize=16)
def _catalog_section(language: str, section: str) -> dict[str, str]:
    """One top-level section of one language file, flattened to dotted keys.

    Reads catalogs/, NOT translations/. Phoenix's own operator-facing strings
    (panel, notification, voice) cannot live in translations/: hassfest validates
    that directory against a CLOSED set of Home Assistant categories and rejects
    the file outright on an unknown one, which blocks the HACS submission. Only
    HA's own sections (config, entity) remain there. See CATALOGS_DIR.

    Cached per (language, section): these are small reads on request paths, and
    the file only changes when the integration is updated. Any failure returns
    an empty mapping so the caller keeps the English.
    """
    path = CATALOGS_DIR / f"{language}.json"
    try:
        section_data = json.loads(path.read_text(encoding="utf-8")).get(section, {})
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}

    def walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(node, str):
            out[prefix] = node

    walk(section_data, "")
    return out


def _placeholders(value: str) -> frozenset[str]:
    """The {placeholder} names in one catalog string, ignoring literal text."""
    return frozenset(
        field for _, field, _, _ in string.Formatter().parse(value) if field
    )


@lru_cache(maxsize=8)
def panel_catalog(language: str) -> dict[str, str]:
    """The panel's strings for one language, English-backed, flattened.

    Reproduces the two behaviours the panel used to get from HA's own
    frontend/get_translations, which no longer serves these strings (they moved
    out of translations/ so hassfest would accept the integration; see
    CATALOGS_DIR):

      - English is the base and the requested language is overlaid on top, so a
        key a translator has not reached yet resolves to English HERE rather
        than rendering as a raw key in the panel;
      - a translated string whose {placeholder} set differs from English is
        DROPPED in favour of the English one. That is not tidiness: the panel
        interpolates by name, so a translation that renamed or dropped a
        placeholder renders a broken sentence or throws, and one that invented a
        placeholder would print a token the caller never passes. Failing back to
        a correct English sentence is the lesser harm, and it is exactly what HA
        did for us before.

    tests/test_i18n_locales.py already refuses to let a mismatched locale ship,
    so this is the runtime backstop for a hand-edited install, not the guard.
    """
    english = _catalog_section("en", "panel")
    if not language or language == "en":
        return english
    merged = dict(english)
    for key, value in _catalog_section(language, "panel").items():
        base = english.get(key)
        if base is not None and _placeholders(base) != _placeholders(value):
            continue
        merged[key] = value
    return merged
