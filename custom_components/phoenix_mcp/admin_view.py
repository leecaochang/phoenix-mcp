"""Admin API views for the Phoenix MCP integration."""


import asyncio
import functools
import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .view_base import PhoenixView
from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER
from homeassistant.helpers import device_registry as dr_mod, entity_registry as er_mod
from homeassistant.util.dt import as_utc, parse_datetime, utcnow

from .audit import Outcome
from .const import (
    AGENTCLI_MAX_ITERATIONS_MAX,
    AGENTCLI_MAX_ITERATIONS_MIN,
    AGENTCLI_SCROLLBACK_MAX,
    AGENTCLI_SCROLLBACK_MIN,
    PHOENIX_VERSION,
    BLOCKED_DOMAINS,
    CAP_CONFIRM,
    CAP_MODES,
    CAPABILITY_NAMES,
    CONFIRM_AVAILABLE_CAPS,
    DOMAIN,
    GITHUB_URL,
    MAX_CONFIRM_INLINE_WAIT_SECONDS,
    MAX_REQUEST_BODY_BYTES,
    MIN_CONFIRM_INLINE_WAIT_SECONDS,
    MAX_BATCH_APPROVALS,
    MESA_CONFIRM_CAP,
    MESA_MODES,
    MESA_STORAGE_KEY,
    MESA_STORAGE_VERSION,
    MIN_HA_VERSION,
    PERSONA_NAMES,
    PRESET_NAME_MAX_LENGTH,
    TOKEN_NAME_REGEX,
)
from .data import PhoenixData
from .helpers import cancel_expiry_timer, panel_catalog
from .policy_engine import Permission, get_effective_hint, resolve
from .token_store import PermissionTree, VALID_NODE_STATES, token_name_slug

_LOGGER = logging.getLogger(__name__)


def _err(
    code: str,
    message: str,
    status: int,
    request_id: str = "",
    key: str | None = None,
    params: dict | None = None,
) -> web.Response:
    """Return a JSON error response. Uses request_id if supplied, else generates one.

    `message` is the English an operator sees and stays authoritative: the panel
    shows it whenever there is no key, which covers every un-migrated call site
    and any older bundle. `key` names a panel catalog entry (`adminError.<key>`)
    so the message can be localized; `params` fills its {placeholders}.

    Not every message wants a key. Many are field-name echoes ("pass_through
    must be a boolean") whose whole content is a wire identifier, and those read
    better untranslated than half-translated.
    """
    rid = request_id or str(uuid.uuid4())
    body: dict[str, Any] = {"error": code, "message": message}
    if key:
        body["message_key"] = f"adminError.{key}"
        if params:
            body["message_params"] = params
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(body),
        # Every admin response reflects live, frequently-mutating state (tokens,
        # approvals, audit); without this a browser or intermediary can serve a
        # stale GET on a plain page refresh, e.g. an approval resolved elsewhere
        # still reading "pending" after reloading the Approvals tab.
        headers={"X-Phoenix-Request-ID": rid, "Cache-Control": "no-store"},
    )


def _ok(body: Any, status: int = 200, request_id: str = "") -> web.Response:
    """Return a JSON success response. Uses request_id if supplied, else generates one."""
    rid = request_id or str(uuid.uuid4())
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(body, default=str),
        headers={"X-Phoenix-Request-ID": rid, "Cache-Control": "no-store"},
    )


def require_admin(method: Callable) -> Callable:
    """Decorator for HomeAssistantView methods that require HA admin privileges.

    Generates a per-request ID, stashes it on the request object as 'phoenix_mcp_rid',
    and logs every admin API call so the ID can be correlated with response headers.
    """
    @functools.wraps(method)
    async def wrapper(self, request: web.Request, **kwargs: Any) -> web.Response:
        request_id = str(uuid.uuid4())
        request["phoenix_mcp_rid"] = request_id
        user = request.get(KEY_HASS_USER)
        if not request.get(KEY_AUTHENTICATED):
            _LOGGER.info("Admin %s %s unauthenticated rid=%s", request.method, request.path, request_id)
            return _err("unauthorized", "Authentication required.", 401, request_id, key="authRequired")
        if not user or not user.is_admin:
            _LOGGER.info("Admin %s %s forbidden rid=%s", request.method, request.path, request_id)
            return _err("forbidden", "Admin access required.", 403, request_id, key="adminRequired")
        # Logs user.id (UUID) rather than user.name. UUID is stable and non-spoofable;
        # user.name can be changed by the admin. Intentional.
        _LOGGER.info("Admin %s %s rid=%s user=%s", request.method, request.path, request_id, user.id)
        return await method(self, request, **kwargs)
    return wrapper


async def _read_body(request: web.Request, request_id: str = "") -> dict | web.Response:
    """Read and parse the request body as a JSON object.

    Returns an empty dict for requests with no body. Returns an error response
    on read failure, invalid JSON, or a non-object body.
    """
    if request.content_length is not None and request.content_length > MAX_REQUEST_BODY_BYTES:
        return _err("request_too_large", "Request body too large.", 413, request_id, key="bodyTooLarge")

    # aiohttp's StreamReader.read(n) is a SHORT read: it returns as soon as the
    # buffer has ANY data, up to n bytes, not once n bytes have actually
    # arrived. A single read(MAX_REQUEST_BODY_BYTES + 1) call could therefore
    # silently truncate a larger body that arrived across multiple TCP/chunked
    # segments (a real bug: intermittent "Invalid JSON body." on bigger
    # payloads, e.g. importing many MESA profiles). Loop until EOF instead,
    # accumulating chunks and bailing as soon as the cap is exceeded.
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await request.content.read(MAX_REQUEST_BODY_BYTES - total + 1)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_REQUEST_BODY_BYTES:
                return _err("request_too_large", "Request body too large.", 413, request_id, key="bodyTooLarge")
            at_eof = getattr(request.content, "at_eof", None)
            if callable(at_eof) and at_eof():
                break
    except Exception:
        return _err("invalid_request", "Failed to read request body.", 400, request_id, key="bodyUnreadable")

    body_bytes = b"".join(chunks)

    if not body_bytes:
        return {}

    try:
        parsed = json.loads(body_bytes)
    except json.JSONDecodeError:
        # Diagnostic only (never logs the body content itself). The short-read
        # bug above is now fixed, so this is a backstop: a byte count short of
        # Content-Length here would now point at something upstream of Phoenix MCP
        # (a proxy or tunnel), not this reader.
        _LOGGER.warning(
            "%s %s: invalid JSON body (%d bytes read, Content-Length %s) rid=%s",
            request.method, request.path, len(body_bytes),
            request.content_length, request_id,
        )
        return _err("invalid_request", "Invalid JSON body.", 400, request_id, key="badJson")

    if not isinstance(parsed, dict):
        return _err("invalid_request", "Request body must be a JSON object.", 400, request_id, key="bodyNotObject")

    return parsed


_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")
_MAX_NODE_ID_LEN = 255
_INJECTION_CHARS = frozenset("<>\"'&;")


def _validate_node_id(node_type: str, node_id: str, rid: str) -> web.Response | None:
    """Return an error response if node_id fails length, injection, or format checks."""
    if len(node_id) > _MAX_NODE_ID_LEN:
        return _err("invalid_request", "Node ID is too long.", 400, rid, key="nodeIdTooLong")
    if any(c in node_id for c in _INJECTION_CHARS):
        return _err("invalid_request", "Node ID contains invalid characters.", 400, rid, key="nodeIdInvalidChars")
    if node_type == "domains" and not _DOMAIN_RE.match(node_id):
        return _err("invalid_request", "Invalid domain name.", 400, rid, key="badDomain")
    if node_type == "entities" and not _ENTITY_RE.match(node_id):
        return _err("invalid_request", "Invalid entity ID format.", 400, rid, key="badEntityId")
    return None


def _parse_pagination(
    request: web.Request,
    rid: str,
    *,
    default_limit: int,
    max_limit: int,
) -> tuple[int, int] | web.Response:
    """Parse limit/offset, or return a 400 response.

One helper, so the paged endpoints cannot disagree. A negative limit is a
    client error, not a request for the whole buffer: it slices as "everything
    but the last N", and in the version store it is an explicit "return
    everything". Malformed input is reported rather than silently replaced with
    a default.
    """
    try:
        limit = int(request.query.get("limit", default_limit))
        offset = int(request.query.get("offset", 0))
    except (TypeError, ValueError):
        return _err("invalid_request", "Invalid pagination parameters.", 400, rid, key="badPagination")
    if limit < 0 or offset < 0:
        return _err("invalid_request", "limit and offset must be non-negative.", 400, rid)
    return min(limit, max_limit), offset


def _validate_permission_tree_body(body: dict, rid: str) -> web.Response | None:
    """Return an error response if any node ID, state, or hint in the permission tree body is invalid."""
    for section, node_type in (("domains", "domains"), ("devices", "devices"), ("entities", "entities")):
        raw_section = body.get(section, {})
        # A non-object section (list, string, number) is structurally invalid
        # but syntactically valid JSON. Without this it reached .items() and
        # raised AttributeError, which surfaced as a 500 rather than a 400.
        if not isinstance(raw_section, dict):
            return _err(
                "invalid_request",
                f"{section!r} must be an object keyed by node ID.",
                400,
                rid,
            )
        for key, value in raw_section.items():
            err = _validate_node_id(node_type, key, rid)
            if err:
                return err
            if not isinstance(value, dict):
                return _err(
                    "invalid_request",
                    f"Node {key!r} value must be an object with a 'state' key.",
                    400,
                    rid,
                )
            state = value.get("state", "GREY")
            if state not in VALID_NODE_STATES:
                return _err(
                    "invalid_request",
                    f"Invalid state {state!r} for {node_type[:-1]} {key!r}. "
                    f"Valid states: {sorted(VALID_NODE_STATES)}.",
                    400,
                    rid,
                )
            hint = value.get("hint")
            if hint is not None:
                if not isinstance(hint, str):
                    return _err("invalid_request", f"hint for {key!r} must be a string.", 400, rid)
                if len(hint) > 200:
                    return _err("invalid_request", f"hint for {key!r} exceeds 200 characters.", 400, rid)
    return None


def _build_entity_tree(hass: HomeAssistant) -> dict:
    """Build a domain-keyed tree of all non-disabled, non-Phoenix entities.

    Pulls from the entity, device, and area registries (all in-memory dicts).
    Synchronous; never performs I/O. The result is cached in
    PhoenixData.entity_tree_cache and invalidated on registry change events.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import label_registry as lr

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    label_reg = lr.async_get(hass)

    tree: dict[str, dict] = {}

    for entry in entity_reg.entities.values():
        if entry.disabled_by is not None:
            continue

        entity_id = entry.entity_id
        domain = entity_id.split(".")[0]

        if domain in BLOCKED_DOMAINS:
            continue

        # Exclude Phoenix MCP's own telemetry sensors (sensor.phoenix_mcp_* entities registered to the
        # phoenix_mcp platform). They live in the sensor domain so BLOCKED_DOMAINS won't catch them.
        # Showing them would let admins grant permissions that the runtime policy engine
        # always blocks, causing confusion. The intent is that Phoenix MCP internals never
        # appear in the permission UI, not just the phoenix_mcp domain itself.
        if entry.platform == DOMAIN:
            continue

        state = hass.states.get(entity_id)
        friendly_name = None
        if state:
            friendly_name = state.attributes.get("friendly_name") or state.name

        if domain not in tree:
            tree[domain] = {"devices": {}, "deviceless_entities": [], "entity_details": {}}

        area_id = entry.area_id
        if not area_id and entry.device_id:
            device = device_reg.async_get(entry.device_id)
            if device:
                area_id = device.area_id

        area_name = None
        if area_id:
            area = area_reg.async_get_area(area_id)
            area_name = area.name if area else None

        # Effective labels follow HA label-target semantics: the entity's own
        # labels plus those of its device. Used by the "Select by Label" picker.
        label_ids: set[str] = set(entry.labels)
        if entry.device_id:
            dev_for_labels = device_reg.async_get(entry.device_id)
            if dev_for_labels:
                label_ids |= dev_for_labels.labels
        labels = []
        for lid in label_ids:
            lbl = label_reg.async_get_label(lid)
            if lbl is not None:
                labels.append({"id": lid, "name": lbl.name})
        labels.sort(key=lambda x: x["name"].lower())

        entity_info: dict[str, Any] = {
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "device_id": entry.device_id,
            "area_id": area_id,
            "area_name": area_name,
            "labels": labels,
        }

        if entry.device_id:
            device_id = entry.device_id
            if device_id not in tree[domain]["devices"]:
                device = device_reg.async_get(device_id)
                if device:
                    d_area_id = device.area_id
                    d_area_name = None
                    if d_area_id:
                        da = area_reg.async_get_area(d_area_id)
                        d_area_name = da.name if da else None
                    tree[domain]["devices"][device_id] = {
                        "device_id": device_id,
                        "name": device.name_by_user or device.name or device_id,
                        "area_id": d_area_id,
                        "area_name": d_area_name,
                        "entities": [],
                    }
                else:
                    tree[domain]["devices"][device_id] = {
                        "device_id": device_id,
                        "name": device_id,
                        "area_id": None,
                        "area_name": None,
                        "entities": [],
                    }
            tree[domain]["devices"][device_id]["entities"].append(entity_id)
        else:
            tree[domain]["deviceless_entities"].append(entity_id)

        tree[domain]["entity_details"][entity_id] = entity_info

    return tree


def _build_resolution_path(entity_id: str, token: Any, hass: HomeAssistant) -> list[dict]:
    """Return the ancestor chain and each node's state for a given entity/token pair.

    Used by the resolve admin endpoint to explain why an entity has a particular
    effective permission.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    er_reg = er.async_get(hass)
    dr_reg = dr.async_get(hass)

    entry = er_reg.async_get(entity_id)
    if entry:
        entity_id = entry.entity_id
    domain = entity_id.split(".")[0]

    path: list[dict] = [{"level": "global", "state": "GREY"}]

    domain_node = token.permissions.domains.get(domain)
    path.append({"level": f"domain:{domain}", "state": domain_node.state if domain_node else "GREY"})

    if entry and entry.device_id:
        device = dr_reg.async_get(entry.device_id)
        if device:
            device_name = device.name_by_user or device.name or entry.device_id
        else:
            device_name = entry.device_id
        device_node = token.permissions.devices.get(entry.device_id)
        path.append({"level": f"device:{device_name}", "state": device_node.state if device_node else "GREY"})

    entity_node = token.permissions.entities.get(entity_id)
    path.append({"level": f"entity:{entity_id}", "state": entity_node.state if entity_node else "GREY"})

    return path


class PhoenixAdminInfoView(PhoenixView):
    """GET /api/phoenix-mcp/admin/info - integration metadata."""

    url = "/api/phoenix-mcp/admin/info"
    name = "api:phoenix-mcp:admin:info"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        from .mcp_view import tool_catalog_counts  # noqa: PLC0415 - avoid import cycle
        return _ok({
            "version": PHOENIX_VERSION,
            "min_ha_version": MIN_HA_VERSION,
            "github_url": GITHUB_URL,
            "tool_count": tool_catalog_counts(),
        }, request_id=request["phoenix_mcp_rid"])


class PhoenixAdminCatalogView(PhoenixView):
    """GET /api/phoenix-mcp/admin/catalog/{language} - the panel's string catalog.

    Phoenix serves its own panel strings because they cannot live in
    translations/ (hassfest rejects any top-level key outside HA's own closed set
    of categories, which fails the HACS submission), and HA's translation API
    reads nothing else. helpers.panel_catalog reproduces the English-backing and
    placeholder-mismatch rules the panel used to get from that API.

    Admin-only like every other view here, which is not a downgrade: the panel is
    admin-only and the injected bundles only ever run for an admin. Kill-switch
    immune for the same reason the rest of the admin API is, so a panel opened to
    turn the kill switch back off is not left rendering raw keys.
    """

    url = "/api/phoenix-mcp/admin/catalog/{language}"
    name = "api:phoenix-mcp:admin:catalog"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, language: str) -> web.Response:
        resources = await self.hass.async_add_executor_job(panel_catalog, language)
        if not resources:
            return _err(
                "not_found", "Catalog not found.", 404,
                request_id=request["phoenix_mcp_rid"],
            )
        return _ok(
            {"language": language, "resources": resources},
            request_id=request["phoenix_mcp_rid"],
        )


class PhoenixAdminArchivedTokensView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/archived - list all archived tokens."""

    url = "/api/phoenix-mcp/admin/tokens/archived"
    name = "api:phoenix-mcp:admin:archived_tokens"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        data: PhoenixData = self.hass.data[DOMAIN]
        archived = [t.to_dict() for t in data.store.list_archived()]
        return _ok(archived, request_id=request["phoenix_mcp_rid"])


class PhoenixAdminArchivedTokenView(PhoenixView):
    """DELETE /api/phoenix-mcp/admin/tokens/archived/{token_id} - permanently delete an archived record."""

    url = "/api/phoenix-mcp/admin/tokens/archived/{token_id}"
    name = "api:phoenix-mcp:admin:archived_token"
    requires_auth = True

    @require_admin
    async def delete(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        deleted = await data.store.async_delete_archived(token_id)
        if not deleted:
            return _err("not_found", "Archived token not found.", 404, rid, key="archivedTokenNotFound")
        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )
        return web.Response(status=204, headers={"X-Phoenix-Request-ID": rid})


class PhoenixAdminTokensView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens - list active tokens.
    POST /api/phoenix-mcp/admin/tokens - create a new token.
    """

    url = "/api/phoenix-mcp/admin/tokens"
    name = "api:phoenix-mcp:admin:tokens"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        data: PhoenixData = self.hass.data[DOMAIN]
        tokens = [t.to_dict() for t in data.store.list_tokens()]
        return _ok(tokens, request_id=request["phoenix_mcp_rid"])

    @require_admin
    async def post(self, request: web.Request) -> web.Response:

        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        user = request[KEY_HASS_USER]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        name = body.get("name")
        if not name or not isinstance(name, str):
            return _err("invalid_request", "name is required.", 400, rid, key="nameRequired")
        if not TOKEN_NAME_REGEX.match(name):
            return _err("invalid_request", "name does not match required pattern.", 400, rid)
        pass_through = body.get("pass_through", False)
        if not isinstance(pass_through, bool):
            return _err("invalid_request", "pass_through must be a boolean.", 400, rid)
        # Require a real JSON true, not just a truthy value: bool("false") is
        # True, so a string would otherwise satisfy this acknowledgment gate.
        if pass_through and body.get("confirm_pass_through") is not True:
            return _err("invalid_request", "confirm_pass_through: true is required when enabling pass_through.", 400, rid)
        use_assist_exposure = body.get("use_assist_exposure", False)
        if not isinstance(use_assist_exposure, bool):
            return _err("invalid_request", "use_assist_exposure must be a boolean.", 400, rid)
        if not pass_through:
            use_assist_exposure = False

        expires_at = None
        if "expires_at" in body:
            raw_expires = body["expires_at"]
            expires_at = parse_datetime(raw_expires) if isinstance(raw_expires, str) else None
            if expires_at is None:
                return _err("invalid_request", "Invalid expires_at datetime.", 400, rid, key="badExpiresAt")
            # A timezone-less value would persist naive and later crash every
            # comparison against aware UTC (expiry checks run during setup, so
            # one poisoned record aborts the whole integration). Normalize to
            # UTC; a naive input is treated as HA's configured local time.
            expires_at = as_utc(expires_at)

        # Strict: int() would silently accept True (-> 1), 1.75 (-> 1), and
        # "12" (-> 12), which contradicts the error message and the documented
        # contract. Same test as confirm_inline_wait_seconds; bool is excluded
        # explicitly because it is a subclass of int.
        rate_limit_requests = body.get("rate_limit_requests", 60)
        rate_limit_burst = body.get("rate_limit_burst", 10)
        if not all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in (rate_limit_requests, rate_limit_burst)
        ):
            return _err("invalid_request", "rate_limit_requests and rate_limit_burst must be integers.", 400, rid)

        if rate_limit_requests < 0 or rate_limit_burst < 0:
            return _err("invalid_request", "rate_limit_requests and rate_limit_burst must be non-negative.", 400, rid)
        if rate_limit_requests > 100_000 or rate_limit_burst > 100_000:
            return _err("invalid_request", "rate_limit_requests and rate_limit_burst must not exceed 100000.", 400, rid)

        async with data.store.async_lock:
            if data.store.name_slug_exists(name):
                return _err("conflict", "A token with that name (or equivalent slug) already exists.", 409, rid, key="tokenNameTaken")
            record, raw_token = await data.store.async_create_token(
                name=name,
                created_by=user.id,
                expires_at=expires_at,
                pass_through=pass_through,
                use_assist_exposure=use_assist_exposure,
                rate_limit_requests=rate_limit_requests,
                rate_limit_burst=rate_limit_burst,
            )

        if data.async_on_token_created:
            await data.async_on_token_created(record)

        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )

        # raw_token is included once in the creation response and never again.
        response_body = record.to_dict()
        response_body["token"] = raw_token
        return _ok(response_body, status=201, request_id=rid)


# The per-token settings a PATCH may write. Capabilities are folded in from
# const, so adding one needs no change here.
_PATCHABLE_TOKEN_FIELDS = {
    "name", "pass_through", "use_assist_exposure", "announce_all_tools",
    "confirm_inline_wait_seconds",
    "rate_limit_requests", "rate_limit_burst", "persona",
} | set(CAPABILITY_NAMES)

_RATE_LIMIT_MAX = 100_000


def _validated_token_patch(
    body: dict, token: Any, token_id: str, data: PhoenixData, rid: str
) -> tuple[dict, web.Response | None]:
    """Filter a token PATCH body to writable fields and validate them.

    Returns (patchable, None) or ({}, error_response) on the first rejection.

    MUST be called with data.store.async_lock held. The name-uniqueness
    check and the pass-through acknowledgment both read store state that the same
    critical section then writes, so hoisting this out of the lock would reopen a
    race between two concurrent renames.
    """
    if "name" in body:
        new_name = body["name"]
        if not new_name or not isinstance(new_name, str):
            return {}, _err("invalid_request", "name is required.", 400, rid, key="nameRequired")
        if not TOKEN_NAME_REGEX.match(new_name):
            return {}, _err("invalid_request", "name must be 3-32 characters: letters, numbers, hyphens, or underscores.", 400, rid)
        if data.store.name_slug_exists(new_name, exclude_token_id=token_id):
            return {}, _err("invalid_request", "A token with this name already exists.", 400, rid)

    if "pass_through" in body:
        if not isinstance(body["pass_through"], bool):
            return {}, _err("invalid_request", "pass_through must be a boolean.", 400, rid)
        enabling = body["pass_through"]
        # Real JSON true only: bool("false") is True, so a truthy string
        # must not satisfy this acknowledgment gate.
        if enabling and not token.pass_through and body.get("confirm_pass_through") is not True:
            return {}, _err("invalid_request", "confirm_pass_through: true is required when enabling pass_through.", 400, rid)

    patchable = {k: v for k, v in body.items() if k in _PATCHABLE_TOKEN_FIELDS}

    for bool_field in ("announce_all_tools", "use_assist_exposure"):
        if bool_field in patchable and not isinstance(patchable[bool_field], bool):
            return {}, _err("invalid_request", f"{bool_field} must be a boolean.", 400, rid)

    if "confirm_inline_wait_seconds" in patchable:
        # 0 disables the inline wait (the unattended-agent mode, API-only);
        # any other value must fall in the enabled range [MIN, MAX].
        wait = patchable["confirm_inline_wait_seconds"]
        valid_int = isinstance(wait, int) and not isinstance(wait, bool)
        valid = valid_int and (wait == 0 or MIN_CONFIRM_INLINE_WAIT_SECONDS <= wait <= MAX_CONFIRM_INLINE_WAIT_SECONDS)
        if not valid:
            return {}, _err(
                "invalid_request",
                f"confirm_inline_wait_seconds must be 0 (off) or an integer from "
                f"{MIN_CONFIRM_INLINE_WAIT_SECONDS} to {MAX_CONFIRM_INLINE_WAIT_SECONDS}.",
                400,
                rid,
            )

    for cap_name in set(CAPABILITY_NAMES) & patchable.keys():
        value = patchable[cap_name]
        if value not in CAP_MODES:
            return {}, _err("invalid_request", f"{cap_name} must be one of: deny, allow, confirm.", 400, rid)
        if value == CAP_CONFIRM and cap_name not in CONFIRM_AVAILABLE_CAPS:
            return {}, _err("invalid_request", f"{cap_name} does not support 'confirm' mode.", 400, rid)

    if "persona" in patchable and patchable["persona"] not in PERSONA_NAMES:
        return {}, _err("invalid_request", "Unknown persona.", 400, rid, key="unknownPersona")

    if "use_assist_exposure" in patchable:
        # Keyed on the RESULTING pass_through, so enabling both in one PATCH works.
        resulting_pass_through = bool(patchable.get("pass_through", token.pass_through))
        if not resulting_pass_through:
            return {}, _err("invalid_request", "use_assist_exposure is only valid for pass_through tokens.", 400, rid)

    for rl_field in ("rate_limit_requests", "rate_limit_burst"):
        if rl_field not in patchable:
            continue
        # Strict, matching the create path: int() would coerce booleans, floats,
        # and numeric strings into a stored value the caller never asked for.
        value = patchable[rl_field]
        if not (isinstance(value, int) and not isinstance(value, bool)):
            return {}, _err("invalid_request", f"{rl_field} must be an integer.", 400, rid)
        if value < 0:
            return {}, _err("invalid_request", f"{rl_field} must be non-negative.", 400, rid)
        if value > _RATE_LIMIT_MAX:
            return {}, _err("invalid_request", f"{rl_field} must not exceed {_RATE_LIMIT_MAX}.", 400, rid)

    return patchable, None


async def _rebuild_token_sensors_after_rename(
    data: PhoenixData, token_id: str, old_name: str, updated: Any
) -> None:
    """Re-key a renamed token's sensors and device onto the new name slug.

    The per-token sensors and device are keyed on the token-name slug, so a
    rename has to remove the old slug's entities and recreate under the new one.
    Best-effort on both halves: a registry problem must never fail a patch that
    already succeeded, so each is caught and logged.
    """
    old_slug = token_name_slug(old_name)
    if data.async_on_token_archived:
        try:
            await data.async_on_token_archived(old_slug)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Sensor cleanup failed renaming token %s; registry may have a ghost entry", token_id, exc_info=True)
    if data.async_on_token_created:
        try:
            await data.async_on_token_created(updated)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Sensor recreate failed renaming token %s", token_id, exc_info=True)


class PhoenixAdminTokenView(PhoenixView):
    """GET/PATCH/DELETE /api/phoenix-mcp/admin/tokens/{token_id} - manage a single token."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}"
    name = "api:phoenix-mcp:admin:token"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")
        return _ok(token.to_dict(), request_id=rid)

    @require_admin
    async def patch(self, request: web.Request, token_id: str) -> web.Response:

        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        if "expires_at" in body:
            return _err("invalid_request", "expires_at is immutable after token creation.", 400, rid, key="expiresImmutable")

        old_name: str | None = None
        async with data.store.async_lock:
            token = data.store.get_token_by_id(token_id)
            if token is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")
            old_name = token.name

            patchable, invalid = _validated_token_patch(body, token, token_id, data, rid)
            if invalid is not None:
                return invalid
            updated = await data.store.async_patch_token(token_id, **patchable)
            if updated is None:
                # The record vanished between the lookup and the write inside the
                # same lock. Report it rather than dereferencing None; the write
                # has already happened either way, so this only affects what the
                # caller is told, never whether the change applied.
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )

        if updated is not None and old_name is not None and updated.name != old_name:
            await _rebuild_token_sensors_after_rename(data, token_id, old_name, updated)

        return _ok(updated.to_dict(), request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, token_id: str) -> web.Response:
        """Revoke a token. Archives it, cancels its queued approvals, fires the bus event."""
        from .approvals import (  # noqa: PLC0415
            REASON_REVOKED,
            async_cancel_approvals_for_token,
            dismiss_approval_notification,
            fire_approval_resolved_event,
        )

        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        user = request[KEY_HASS_USER]

        async with data.store.async_lock:
            token = data.store.get_token_by_id(token_id)
            if token is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

            token_name = token.name
            now = utcnow()

            await data.store.async_archive_token(token_id, revoked=True, revoked_at=now)
            cancel_expiry_timer(data, token_id)
            # If this token was the Assist binding or the voice agent's token, clear
            # it (rule 11): the llm.API stays registered but empty; the voice agent
            # re-syncs to unregistered (done after the lock).
            _s = data.store.get_settings()
            _patch: dict[str, Any] = {}
            if _s.assist_bound_token_id == token_id:
                _patch["assist_bound_token_id"] = None
            if _s.voice_agent_token_id == token_id:
                _patch["voice_agent_token_id"] = None
            if _s.ai_task_token_id == token_id:
                _patch["ai_task_token_id"] = None
            if _patch:
                await data.store.async_patch_settings(**_patch)
            # A revoked token's queued approvals must not linger as approvable
            # entries; an approval mid-execution is protected, like the sweep.
            cancelled = await async_cancel_approvals_for_token(
                data.store, token_id, REASON_REVOKED,
                skip_ids=data.approvals_in_progress,
            )
        for approval in cancelled:
            dismiss_approval_notification(hass, approval.id)
            fire_approval_resolved_event(hass, approval)

        # Advisory leases the token still held. Outside the store lock: they are
        # in-memory and unrelated to stored state.
        from .mesa import release_token_leases  # noqa: PLC0415
        release_token_leases(data, token_id)

        if data.async_sync_voice_agent is not None:
            data.async_sync_voice_agent()

        if "voice_agent_token_id" in _patch:
            # The voice agent lost its token, so it is now unregistered; remove any
            # Phoenix-created Assist pipeline so a broken assistant is not left behind.
            from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
            await async_remove_assist_pipeline(hass, data)

        if "ai_task_token_id" in _patch and data.async_sync_ai_task is not None:
            # The AI Task lost its token, so it is no longer fully configured; remove
            # the AI Task entity from HA (and the picker).
            data.async_sync_ai_task()

        data.rate_limiter.destroy(token_id)
        data.rate_limit_notified.pop(token_id, None)
        data.token_counters.pop(token_id, None)
        data.stale_tools_advised.pop(token_id, None)

        hass.bus.async_fire("phoenix_mcp_token_revoked", {
            "token_id": token_id,
            "token_name": token_name,
            "revoked_by": user.id,
            "timestamp": now.isoformat(),
        })

        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )

        slug = token_name_slug(token_name)
        if data.async_on_token_archived:
            try:
                await data.async_on_token_archived(slug)
            except Exception:
                _LOGGER.warning("Sensor removal failed for token %s; entity registry may have ghost entries", token_id, exc_info=True)

        return web.Response(status=204, headers={"X-Phoenix-Request-ID": rid})


def _presets_disabled(data: PhoenixData, rid: str) -> web.Response | None:
    """403 for every preset endpoint while the feature toggle is off."""
    if not data.store.get_settings().token_presets_enabled:
        return _err("forbidden", "Token presets are disabled. Enable them in Settings.", 403, rid, key="presetsDisabled")
    return None


def _validate_preset_name(raw: object, rid: str) -> str | web.Response:
    """Validate a preset name from a request body: the name, or an error response.

    Single-return rather than a (name, error) pair, matching _read_body and
    yaml_includes._layout_or_result: a pair guarantees the name is a str only via
    the OTHER element, which neither a reader nor a checker can follow.
    """
    if not isinstance(raw, str) or not raw.strip():
        return _err("invalid_request", "name is required.", 400, rid, key="nameRequired")
    name = raw.strip()
    if len(name) > PRESET_NAME_MAX_LENGTH:
        return _err("invalid_request", f"name must be at most {PRESET_NAME_MAX_LENGTH} characters.", 400, rid)
    if any(c in name for c in _INJECTION_CHARS):
        return _err("invalid_request", "name contains invalid characters.", 400, rid)
    return name


class PhoenixAdminTokenPresetsView(PhoenixView):
    """POST /api/phoenix-mcp/admin/tokens/{token_id}/presets - save the current settings as a new preset."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/presets"
    name = "api:phoenix-mcp:admin:token:presets"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        if (gate := _presets_disabled(data, rid)) is not None:
            return gate
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        name = _validate_preset_name(body.get("name"), rid)
        if isinstance(name, web.Response):
            return name

        async with data.store.async_lock:
            try:
                updated = await data.store.async_add_preset(token_id, name)
            except ValueError as exc:
                return _err("invalid_request", str(exc), 400, rid)
            if updated is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        _audit_admin(self.hass, request, rid, request.path)
        return _ok(updated.to_dict(), status=201, request_id=rid)


class PhoenixAdminTokenPresetView(PhoenixView):
    """PATCH/DELETE /api/phoenix-mcp/admin/tokens/{token_id}/presets/{preset_id} - rename or delete a preset."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/presets/{preset_id}"
    name = "api:phoenix-mcp:admin:token:preset"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, token_id: str, preset_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        if (gate := _presets_disabled(data, rid)) is not None:
            return gate
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        name = _validate_preset_name(body.get("name"), rid)
        if isinstance(name, web.Response):
            return name

        async with data.store.async_lock:
            try:
                updated = await data.store.async_rename_preset(token_id, preset_id, name)
            except LookupError:
                return _err("not_found", "Preset not found.", 404, rid, key="presetNotFound")
            except ValueError as exc:
                return _err("invalid_request", str(exc), 400, rid)
            if updated is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        _audit_admin(self.hass, request, rid, request.path)
        return _ok(updated.to_dict(), request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, token_id: str, preset_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        if (gate := _presets_disabled(data, rid)) is not None:
            return gate

        async with data.store.async_lock:
            try:
                updated = await data.store.async_delete_preset(token_id, preset_id)
            except LookupError:
                return _err("not_found", "Preset not found.", 404, rid, key="presetNotFound")
            except ValueError as exc:
                return _err("invalid_request", str(exc), 400, rid)
            if updated is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        _audit_admin(self.hass, request, rid, request.path)
        return _ok(updated.to_dict(), request_id=rid)


class PhoenixAdminTokenPresetApplyView(PhoenixView):
    """POST /api/phoenix-mcp/admin/tokens/{token_id}/presets/{preset_id}/apply - make the token match a preset.

    Workspace model: applying a DIFFERENT preset first auto-saves the
    live state back into the outgoing active preset, then applies the target and
    marks it active. Applying the ACTIVE preset reverts to its saved state
    (discards unsaved changes, no save-back). Enforcement never reads presets;
    this routes through the same store paths as a manual PATCH + permissions PUT.
    """

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/presets/{preset_id}/apply"
    name = "api:phoenix-mcp:admin:token:preset:apply"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, token_id: str, preset_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        if (gate := _presets_disabled(data, rid)) is not None:
            return gate
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        async with data.store.async_lock:
            token = data.store.get_token_by_id(token_id)
            if token is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")
            preset = next((p for p in token.presets if p.id == preset_id), None)
            if preset is None:
                return _err("not_found", "Preset not found.", 404, rid, key="presetNotFound")
            if preset.pass_through and not token.pass_through and not body.get("confirm_pass_through"):
                return _err("invalid_request", "confirm_pass_through: true is required when the preset enables pass_through.", 400, rid)

            # SWITCH: the outgoing preset absorbs the live state first, so
            # nothing the admin changed since the last switch is ever lost.
            # Applying the active preset itself skips this (that is the revert).
            if preset_id != token.active_preset_id and token.active_preset_id:
                await data.store.async_sync_preset(token_id, token.active_preset_id)

            await data.store.async_patch_token(
                token_id,
                pass_through=preset.pass_through,
                use_assist_exposure=preset.use_assist_exposure if preset.pass_through else False,
                announce_all_tools=preset.announce_all_tools,
                confirm_inline_wait_seconds=preset.confirm_inline_wait_seconds,
                rate_limit_requests=preset.rate_limit_requests,
                rate_limit_burst=preset.rate_limit_burst,
                **preset.caps,
            )
            # Deep-copy the stored tree so live edits never mutate the preset.
            await data.store.async_set_permissions(
                token_id, PermissionTree.from_dict(preset.permissions.to_dict()),
            )
            token.active_preset_id = preset.id
            await data.store.async_save()

        # New limits take effect cleanly rather than inheriting the old window.
        data.rate_limiter.destroy(token_id)
        _audit_admin(self.hass, request, rid, request.path)
        return _ok(token.to_dict(), request_id=rid)


class PhoenixAdminPermissionsView(PhoenixView):
    """GET/PUT /api/phoenix-mcp/admin/tokens/{token_id}/permissions - read or replace the full permission tree."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/permissions"
    name = "api:phoenix-mcp:admin:permissions"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")
        return _ok(token.permissions.to_dict(), request_id=rid)

    @require_admin
    async def put(self, request: web.Request, token_id: str) -> web.Response:

        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        err = _validate_permission_tree_body(body, rid)
        if err:
            return err

        try:
            new_tree = PermissionTree.from_dict(body)
        except Exception:
            return _err("invalid_request", "Invalid permission tree structure.", 400, rid)

        async with data.store.async_lock:
            token = data.store.get_token_by_id(token_id)
            if token is None:
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

            updated = await data.store.async_set_permissions(token_id, new_tree)
            if updated is None:
                # The record vanished between the lookup and the write inside the
                # same lock. Report it rather than dereferencing None; the write
                # has already happened either way, so this only affects what the
                # caller is told, never whether the change applied.
                return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )
        return _ok(updated.permissions.to_dict(), request_id=rid)


class PhoenixAdminPermissionDomainView(PhoenixView):
    """PATCH /api/phoenix-mcp/admin/tokens/{token_id}/permissions/domains/{node_id}."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/permissions/domains/{node_id}"
    name = "api:phoenix-mcp:admin:permission_domain"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, token_id: str, node_id: str) -> web.Response:
        return await _patch_permission_node(request, self.hass, token_id, "domains", node_id)


class PhoenixAdminPermissionDeviceView(PhoenixView):
    """PATCH /api/phoenix-mcp/admin/tokens/{token_id}/permissions/devices/{node_id}."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/permissions/devices/{node_id}"
    name = "api:phoenix-mcp:admin:permission_device"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, token_id: str, node_id: str) -> web.Response:
        return await _patch_permission_node(request, self.hass, token_id, "devices", node_id)


class PhoenixAdminPermissionEntityView(PhoenixView):
    """PATCH /api/phoenix-mcp/admin/tokens/{token_id}/permissions/entities/{node_id}."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/permissions/entities/{node_id}"
    name = "api:phoenix-mcp:admin:permission_entity"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, token_id: str, node_id: str) -> web.Response:
        return await _patch_permission_node(request, self.hass, token_id, "entities", node_id)


async def _patch_permission_node(
    request: web.Request,
    hass: HomeAssistant,
    token_id: str,
    node_type: str,
    node_id: str,
) -> web.Response:
    """Shared handler for PATCH on domain/device/entity permission nodes."""
    rid = request["phoenix_mcp_rid"]

    err = _validate_node_id(node_type, node_id, rid)
    if err:
        return err

    data: PhoenixData = hass.data[DOMAIN]

    body = await _read_body(request, rid)
    if isinstance(body, web.Response):
        return body

    state = body.get("state")
    if state not in VALID_NODE_STATES:
        return _err("invalid_request", f"state must be one of: {', '.join(sorted(VALID_NODE_STATES))}.", 400, rid)

    hint = body.get("hint")
    if hint is not None and not isinstance(hint, str):
        return _err("invalid_request", "hint must be a string.", 400, rid)
    if hint is not None and len(hint) > 200:
        return _err("invalid_request", "hint must be 200 characters or fewer.", 400, rid)

    async with data.store.async_lock:
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        updated = await data.store.async_patch_permission_node(
            token_id, node_type, node_id, state, hint
        )
        if updated is None:
            # Same reasoning as the token PATCH above: report rather than
            # dereference None if the record vanished inside the lock.
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

    user = request[KEY_HASS_USER]
    data.audit.record(
        request_id=rid,
        token_id="admin",
        token_name=f"admin:{user.id}",
        method=request.method,
        resource=request.path,
        outcome="allowed",
        client_ip=request.remote or "",
        settings=data.store.get_settings(),
    )
    return _ok(updated.permissions.to_dict(), request_id=rid)


class PhoenixAdminResolveView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/{token_id}/resolve/{entity_id} - explain effective permission."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/resolve/{entity_id}"
    name = "api:phoenix-mcp:admin:resolve"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str, entity_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        if not _ENTITY_RE.match(entity_id):
            return _err("invalid_request", "Invalid entity ID format.", 400, rid, key="badEntityId")
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        perm = resolve(entity_id, token, hass)
        resolution_path = _build_resolution_path(entity_id, token, hass)

        effective_map = {
            Permission.WRITE: "WRITE",
            Permission.READ: "READ",
            Permission.DENY: "DENY",
            Permission.NO_ACCESS: "NO_ACCESS",
            Permission.NOT_FOUND: "NOT_FOUND",
        }

        effective_hint = get_effective_hint(token, entity_id, hass, data.store.get_entity_hints())

        return _ok({
            "entity_id": entity_id,
            "resolution_path": resolution_path,
            "effective": effective_map.get(perm, "NO_ACCESS"),
            "effective_hint": effective_hint,
        }, request_id=rid)


class PhoenixAdminScopeView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/{token_id}/scope - enumerate all readable/writable entities."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/scope"
    name = "api:phoenix-mcp:admin:scope"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        all_states = hass.states.async_all()
        readable: list[str] = []
        writable: list[str] = []

        if token.pass_through:
            # Fast path: pass_through tokens have WRITE on everything except BLOCKED_DOMAINS
            # and Phoenix MCP platform entities. Avoids an O(n) resolve() call per entity.
            registry = er_mod.async_get(hass)
            for state in all_states:
                eid = state.entity_id
                if eid.split(".")[0] in BLOCKED_DOMAINS:
                    continue
                entry = registry.async_get(eid)
                if entry is not None and entry.platform == DOMAIN:
                    continue
                readable.append(eid)
                writable.append(eid)
        else:
            for state in all_states:
                eid = state.entity_id
                perm = resolve(eid, token, hass)
                if perm == Permission.WRITE:
                    readable.append(eid)
                    writable.append(eid)
                elif perm == Permission.READ:
                    readable.append(eid)

        # capability_flags reports raw stored values without the pass_through OR adjustments
        # applied by _build_server_info / _build_context_json. This is intentional: the
        # admin scope view is a diagnostic tool and the admin should see actual stored flags,
        # not the effective values a client would receive.
        return _ok({
            "token_id": token_id,
            "token_name": token.name,
            "readable": sorted(readable),
            "writable": sorted(writable),
            "persona": token.persona,
            "capability_flags": {name: getattr(token, name) for name in CAPABILITY_NAMES},
        }, request_id=rid)


class PhoenixAdminEntityTreeView(PhoenixView):
    """GET /api/phoenix-mcp/admin/entities - return (cached) entity tree for the permission UI."""

    url = "/api/phoenix-mcp/admin/entities"
    name = "api:phoenix-mcp:admin:entities"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]

        if request.query.get("force_reload"):
            data.entity_tree_cache_valid = False

        async with data.entity_tree_lock:
            if not data.entity_tree_cache_valid or data.entity_tree_cache is None:
                data.entity_tree_cache = _build_entity_tree(hass)
                data.entity_tree_cache_valid = True

        import functools
        json_body = await hass.async_add_executor_job(
            functools.partial(json.dumps, data.entity_tree_cache, default=str)
        )
        return web.Response(
            status=200,
            content_type="application/json",
            text=json_body,
            headers={"X-Phoenix-Request-ID": rid, "Cache-Control": "no-store"},
        )


class PhoenixAdminEntityHintsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/entity-hints - the global entity-hint map (entity_id -> hint)."""

    url = "/api/phoenix-mcp/admin/entity-hints"
    name = "api:phoenix-mcp:admin:entity_hints"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        return _ok({"entity_hints": dict(data.store.get_entity_hints())}, request_id=rid)


class PhoenixAdminEntityHintView(PhoenixView):
    """PUT /api/phoenix-mcp/admin/entity-hints/{entity_id} - set or clear one global entity hint.

    A global hint applies to every token that can see the entity. It is distinct
    from a per-token permission-node hint, which always takes precedence.
    """

    url = "/api/phoenix-mcp/admin/entity-hints/{entity_id}"
    name = "api:phoenix-mcp:admin:entity_hint"
    requires_auth = True

    @require_admin
    async def put(self, request: web.Request, entity_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        if not _ENTITY_RE.match(entity_id):
            return _err("invalid_request", "Invalid entity ID format.", 400, rid, key="badEntityId")
        data: PhoenixData = self.hass.data[DOMAIN]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        hint = body.get("hint")
        if hint is not None:
            if not isinstance(hint, str):
                return _err("invalid_request", "hint must be a string.", 400, rid)
            hint = hint.strip()
            if len(hint) > 200:
                return _err("invalid_request", "hint must be 200 characters or fewer.", 400, rid)

        async with data.store.async_lock:
            await data.store.async_set_entity_hint(entity_id, hint or None)

        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )
        return _ok({"entity_hints": dict(data.store.get_entity_hints())}, request_id=rid)


class PhoenixAdminTokenStatsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/{token_id}/stats - in-memory counters for one token."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/stats"
    name = "api:phoenix-mcp:admin:token_stats"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        counters = data.token_counters.get(token_id, {
            "request_count": 0,
            "denied_count": 0,
            "rate_limit_hits": 0,
        })

        last_used = token.last_used_at.isoformat() if token.last_used_at else None

        status = "expired" if token.is_expired() else "active"

        return _ok({
            "token_id": token_id,
            "token_name": token.name,
            "request_count": counters["request_count"],
            "denied_count": counters["denied_count"],
            "rate_limit_hits": counters["rate_limit_hits"],
            "last_used_at": last_used,
            "status": status,
        }, request_id=rid)


class PhoenixAdminTokenConnectionView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/{token_id}/connection - connection signals.

    Used by the onboarding wizard to detect when a token's MCP client has
    connected. The Streamable HTTP transport is stateless and registers no
    session, so ``request_count`` is the signal: any authenticated MCP request
    bumps it. The wizard treats the token as connected once it is above zero.
    """

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/connection"
    name = "api:phoenix-mcp:admin:token_connection"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        counters = data.token_counters.get(token_id, {})
        last_used = token.last_used_at.isoformat() if token.last_used_at else None

        return _ok({
            "last_used_at": last_used,
            "request_count": counters.get("request_count", 0),
        }, request_id=rid)


class PhoenixAdminTokenAuditView(PhoenixView):
    """GET /api/phoenix-mcp/admin/tokens/{token_id}/audit - paginated audit log for one token."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/audit"
    name = "api:phoenix-mcp:admin:token_audit"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        paged = _parse_pagination(request, rid, default_limit=100, max_limit=500)
        if isinstance(paged, web.Response):
            return paged
        limit, offset = paged

        outcome_filter = request.query.get("outcome")
        ip_filter = request.query.get("ip")

        entries = data.audit.query(
            token_id=token_id,
            outcome=outcome_filter,
            client_ip=ip_filter,
            limit=limit,
            offset=offset,
        )
        if entries is None:
            return _err("invalid_request", f"Unknown outcome filter: {outcome_filter!r}.", 400, rid)
        return _ok([e.to_dict() for e in entries], request_id=rid)


class PhoenixAdminAuditView(PhoenixView):
    """GET /api/phoenix-mcp/admin/audit - paginated global audit log with optional filters."""

    url = "/api/phoenix-mcp/admin/audit"
    name = "api:phoenix-mcp:admin:audit"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]

        paged = _parse_pagination(request, rid, default_limit=100, max_limit=500)
        if isinstance(paged, web.Response):
            return paged
        limit, offset = paged

        token_id_filter = request.query.get("token_id")
        outcome_filter = request.query.get("outcome")
        ip_filter = request.query.get("ip")
        method_filter = request.query.get("method")
        resource_filter = request.query.get("resource")
        since_raw = request.query.get("since")
        since_filter = parse_datetime(since_raw) if since_raw else None
        if since_raw and since_filter is None:
            return _err("invalid_request", "Invalid since timestamp.", 400, rid)
        if since_filter is not None:
            # Audit timestamps are aware UTC; a timezone-less since would raise
            # TypeError inside the filter. Naive input is treated as local time.
            since_filter = as_utc(since_filter)

        filter_kwargs: dict[str, Any] = {
            "token_id": token_id_filter,
            "outcome": outcome_filter,
            "client_ip": ip_filter,
            "method": method_filter,
            "resource": resource_filter,
            "since": since_filter,
        }
        total = data.audit.count(**filter_kwargs)
        if total is None:
            return _err("invalid_request", f"Unknown outcome filter: {outcome_filter!r}.", 400, rid)
        entries = data.audit.query(**filter_kwargs, limit=limit, offset=offset) or []
        return _ok({
            "entries": [e.to_dict() for e in entries],
            "total": total,
        }, request_id=rid)


def _settings_response(settings, hass=None) -> dict:
    """Settings dict plus the computed, non-persisted capability flags.

    assist_api_supported reflects whether the running HA exposes the llm.API +
    schema-conversion seam; the panel disables the Assist bind toggle when False.

    The esphome_* flags report what exists on this system rather than what is
    configured in Phoenix MCP, so the panel can mark the ESPHome capability and
    persona as inapplicable. esphome_builder_live is the coordinator's last
    refresh result, which the panel may show but tool announcement deliberately
    ignores (see mcp_view.esphome_availability).
    """
    from .ai_task import ai_task_supported  # noqa: PLC0415
    from .assist_api import assist_api_supported  # noqa: PLC0415
    from .tools.esphome import esphome_availability  # noqa: PLC0415
    from .voice_agent import assist_pipeline_supported  # noqa: PLC0415

    body = settings.to_dict()
    body["assist_api_supported"] = assist_api_supported()
    body["voice_agent_pipeline_supported"] = assist_pipeline_supported()
    body["ai_task_supported"] = ai_task_supported()
    esphome = esphome_availability(hass)
    body["esphome_integration"] = esphome.integration
    body["esphome_builder"] = esphome.builder
    body["esphome_builder_live"] = esphome.builder_live
    return body


# ---------------------------------------------------------------------------
# Settings PATCH: the writable surface, its validation, and its side effects.
#
# Split out of the handler, which had grown to 201 lines and ~64 branches doing
# four unrelated jobs at once. The shape is now: filter to what is writable,
# validate/coerce it, apply it under the store lock, then run the side effects
# keyed on which settings actually moved. The sets below were rebuilt on every
# request as handler locals; at module level they are also the readable
# statement of what this endpoint will accept.
# ---------------------------------------------------------------------------

_VALID_FLUSH_INTERVALS = frozenset({0, 5, 10, 15, 30, 60})
_VALID_LOG_MAXLENS = frozenset({100, 1000, 5000, 10000})

_BOOL_SETTINGS = frozenset({
    "kill_switch", "disable_all_logging", "log_allowed", "log_denied",
    "log_rate_limited", "log_entity_names", "log_client_ip", "notify_on_rate_limit",
    "notify_on_approval", "mesa_inject_enabled", "token_presets_enabled",
    "agentcli_global", "voice_agent_enabled", "ai_task_enabled",
})

# Nullable string settings: None/"" clears, anything else must be a string.
# The token and provider ones are additionally checked against live records
# below, so the panel cannot point a surface at a dead token or provider.
_NULLABLE_STRING_SETTINGS = (
    "voice_agent_token_id", "voice_agent_provider_id", "voice_agent_model",
    "ai_task_token_id", "ai_task_provider_id", "ai_task_model",
)

# (token setting, provider setting) pairs validated against live records.
_SURFACE_TOKEN_PROVIDER_PAIRS = (
    ("voice_agent_token_id", "voice_agent_provider_id"),
    ("ai_task_token_id", "ai_task_provider_id"),
)

_PATCHABLE_SETTINGS = _BOOL_SETTINGS | {
    "audit_flush_interval", "audit_log_maxlen", "mesa_mode",
    "agentcli_scrollback_lines", "agentcli_max_iterations", "assist_bound_token_id",
    *_NULLABLE_STRING_SETTINGS,
}


def _coerce_int_setting(
    patchable: dict,
    key: str,
    rid: str,
    *,
    allowed: frozenset[int] | None = None,
    bounds: tuple[int, int] | None = None,
) -> web.Response | None:
    """Coerce one integer setting in place. Returns an error response or None.

    int() coercion is deliberate and load-bearing: the panel posts form values as
    strings, so "1000" must be accepted. `allowed` rejects anything outside a
    fixed menu (the audit settings, whose values index real buffers and timers);
    `bounds` CLAMPS instead (the two agentcli limits, where an out-of-range value
    has an obvious nearest sane answer and refusing it would just annoy). Do not
    blur the two: a rejected menu value is a mistake worth surfacing, a clamped
    limit is not.
    """
    try:
        value = int(patchable[key])
    except (TypeError, ValueError):
        return _err("invalid_request", f"{key} must be an integer.", 400, rid)
    if allowed is not None and value not in allowed:
        return _err("invalid_request", f"{key} must be one of: {sorted(allowed)}.", 400, rid)
    if bounds is not None:
        value = max(bounds[0], min(bounds[1], value))
    patchable[key] = value
    return None


async def _validated_settings_patch(
    body: dict, data: PhoenixData, hass: HomeAssistant, rid: str
) -> tuple[dict, web.Response | None]:
    """Filter a PATCH body to the writable settings and validate/coerce them.

    Returns (patchable, None) on success or ({}, error_response) on the first
    rejection. Nothing here touches the store, so a rejected patch never reaches
    it; the caller applies the result under the lock.
    """
    patchable = {k: v for k, v in body.items() if k in _PATCHABLE_SETTINGS}

    for key in _BOOL_SETTINGS & patchable.keys():
        if not isinstance(patchable[key], bool):
            return {}, _err("invalid_request", f"{key!r} must be a boolean (true or false).", 400, rid)

    if "mesa_mode" in patchable and patchable["mesa_mode"] not in MESA_MODES:
        return {}, _err("invalid_request", f"mesa_mode must be one of: {sorted(MESA_MODES)}.", 400, rid)

    int_checks = (
        ("audit_flush_interval", {"allowed": _VALID_FLUSH_INTERVALS}),
        ("audit_log_maxlen", {"allowed": _VALID_LOG_MAXLENS}),
        ("agentcli_scrollback_lines",
         {"bounds": (AGENTCLI_SCROLLBACK_MIN, AGENTCLI_SCROLLBACK_MAX)}),
        ("agentcli_max_iterations",
         {"bounds": (AGENTCLI_MAX_ITERATIONS_MIN, AGENTCLI_MAX_ITERATIONS_MAX)}),
    )
    for key, kwargs in int_checks:
        if key in patchable:
            err = _coerce_int_setting(patchable, key, rid, **kwargs)
            if err is not None:
                return {}, err

    if "assist_bound_token_id" in patchable:
        # None/"" unbinds; a string must reference a currently active token, so
        # the panel cannot bind Assist to a revoked/expired/ghost id.
        val = patchable["assist_bound_token_id"]
        if val in (None, ""):
            patchable["assist_bound_token_id"] = None
        elif not isinstance(val, str):
            return {}, _err("invalid_request", "assist_bound_token_id must be a token id string or null.", 400, rid)
        else:
            bound = data.store.get_token_by_id(val)
            if bound is None or not bound.is_valid():
                return {}, _err("invalid_request", "assist_bound_token_id must reference an active token.", 400, rid)

    # Voice-agent and AI-Task string fields: each is None/"" (clear) or must
    # reference a live target, so the panel cannot point either surface at a dead
    # token or provider. Both runtime surfaces still degrade gracefully if the set
    # is incomplete.
    for vkey in _NULLABLE_STRING_SETTINGS:
        if vkey not in patchable:
            continue
        if patchable[vkey] in (None, ""):
            patchable[vkey] = None
        elif not isinstance(patchable[vkey], str):
            return {}, _err("invalid_request", f"{vkey} must be a string or null.", 400, rid)

    provider_store = None
    for tkey, pkey in _SURFACE_TOKEN_PROVIDER_PAIRS:
        if patchable.get(tkey):
            vtok = data.store.get_token_by_id(patchable[tkey])
            if vtok is None or not vtok.is_valid():
                return {}, _err("invalid_request", f"{tkey} must reference an active token.", 400, rid)
        if patchable.get(pkey):
            if provider_store is None:
                from .agentcli import _get_secret_store  # noqa: PLC0415
                provider_store = await _get_secret_store(hass)
            if provider_store.get(patchable[pkey]) is None:
                return {}, _err("invalid_request", f"{pkey} must reference a configured provider account.", 400, rid)

    return patchable, None


async def _apply_settings_side_effects(
    hass: HomeAssistant,
    data: PhoenixData,
    patchable: dict,
    updated: Any,
    old_kill_switch: bool,
) -> None:
    """Run the live re-syncs a settings change implies, in order.

    Each block is keyed on the settings that actually moved, so an unrelated
    patch is inert. Order matters where noted; everything else is independent.
    """
    if "audit_log_maxlen" in patchable:
        data.audit.resize(patchable["audit_log_maxlen"])

    if "audit_flush_interval" in patchable and data.reschedule_audit_flush is not None:
        data.reschedule_audit_flush()

    if "mesa_mode" in patchable and data.mesa is not None:
        data.mesa.set_mode(updated.mesa_mode)

    if "kill_switch" in patchable and old_kill_switch and not updated.kill_switch:
        # Kill switch just deactivated: re-register routes if not already registered.
        if not data.routes_registered and data.async_register_routes:
            await data.async_register_routes()
            data.routes_registered = True

    if "kill_switch" in patchable:
        # The Assist bridge is agent activity, so it follows the kill switch too.
        # Unlike aiohttp routes we can unregister an llm.API, so toggle it live.
        from .assist_api import async_register_assist_api, async_unregister_assist_api  # noqa: PLC0415
        if updated.kill_switch:
            async_unregister_assist_api(data)
        else:
            async_register_assist_api(hass, data)

    # Re-sync the Phoenix MCP voice conversation agent when its config or the kill switch
    # changed; the closure reads current settings and registers/unregisters to match.
    voice_touched = any(k.startswith("voice_agent_") for k in patchable)
    if data.async_sync_voice_agent is not None and ("kill_switch" in patchable or voice_touched):
        data.async_sync_voice_agent()

    # If a voice-agent config change left it not fully configured (disabled, or a
    # token/provider/model cleared), tear down any Phoenix-created Assist pipeline so a
    # broken assistant is not left pointing at the now-unregistered agent. The kill
    # switch is intentionally excluded (it keeps the agent registered-but-declining,
    # so its pipeline stays valid). No-op when nothing is tracked. Must run AFTER the
    # re-sync above, which is what unregisters the agent.
    if voice_touched:
        now = data.store.get_settings()
        fully = bool(
            now.voice_agent_enabled and now.voice_agent_token_id
            and now.voice_agent_provider_id and now.voice_agent_model
        )
        if not fully and now.voice_agent_pipeline_id is not None:
            from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
            await async_remove_assist_pipeline(hass, data)

    # Add/remove the Phoenix MCP AI Task entity to match its config (it exists only while
    # fully configured), so it appears in HA's AI Task picker only when usable.
    if data.async_sync_ai_task is not None and any(k.startswith("ai_task_") for k in patchable):
        data.async_sync_ai_task()

    if "mesa_inject_enabled" in patchable:
        # Add/remove the in-context profile injector module. Takes effect on the
        # next full HA page load (already-open tabs are unaffected).
        from .panel import async_sync_mesa_inject  # noqa: PLC0415
        await async_sync_mesa_inject(hass)

    if "agentcli_global" in patchable:
        # Add/remove the global Agent Chat window module. Takes effect on the
        # next full HA page load (already-open tabs are unaffected).
        from .panel import async_sync_agentchat_inject  # noqa: PLC0415
        await async_sync_agentchat_inject(hass)


class PhoenixAdminSettingsView(PhoenixView):
    """GET/PATCH /api/phoenix-mcp/admin/settings - read or update global integration settings."""

    url = "/api/phoenix-mcp/admin/settings"
    name = "api:phoenix-mcp:admin:settings"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        data: PhoenixData = self.hass.data[DOMAIN]
        return _ok(_settings_response(data.store.get_settings(), self.hass), request_id=request["phoenix_mcp_rid"])

    @require_admin
    async def patch(self, request: web.Request) -> web.Response:

        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        patchable, invalid = await _validated_settings_patch(body, data, self.hass, rid)
        if invalid is not None:
            return invalid

        async with data.store.async_lock:
            old_kill_switch = data.store.get_settings().kill_switch
            old_presets_enabled = data.store.get_settings().token_presets_enabled
            updated = await data.store.async_patch_settings(**patchable)
            if updated.token_presets_enabled and not old_presets_enabled:
                # Feature just enabled: every token gets a "Default preset" from
                # its current state (workspace model). Idempotent on re-enables.
                seeded = await data.store.async_seed_default_presets()
                if seeded:
                    _LOGGER.info("Token presets enabled; seeded a default preset on %d token(s)", seeded)

        await _apply_settings_side_effects(
            self.hass, data, patchable, updated, old_kill_switch,
        )

        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=updated,
        )
        return _ok(_settings_response(updated, self.hass), request_id=rid)


class PhoenixAdminVoiceAgentPipelineView(PhoenixView):
    """POST/DELETE /api/phoenix-mcp/admin/voice_agent/pipeline - one-click Assist setup.

    POST creates an Assist pipeline pointed at Phoenix MCP's conversation agent (optionally
    the preferred assistant) so the operator does not have to wire it up by hand;
    DELETE removes the Phoenix-created pipeline again. Admin session auth only.
    """

    url = "/api/phoenix-mcp/admin/voice_agent/pipeline"
    name = "api:phoenix-mcp:admin:voice_agent:pipeline"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        preferred = body.get("preferred", True)
        if not isinstance(preferred, bool):
            return _err("invalid_request", "preferred must be a boolean.", 400, rid)

        entries = self.hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return _err("invalid_request", "Phoenix MCP is not set up.", 400, rid)

        from .voice_agent import async_create_assist_pipeline, VoicePipelineError  # noqa: PLC0415
        try:
            result = await async_create_assist_pipeline(
                self.hass, entries[0], data, preferred=preferred
            )
        except VoicePipelineError as err:
            return _err("invalid_request", str(err), 400, rid)
        return _ok(result, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
        await async_remove_assist_pipeline(self.hass, data)
        return _ok({"ok": True}, request_id=rid)


class PhoenixAdminAiTaskPreferredView(PhoenixView):
    """GET/POST/DELETE /api/phoenix-mcp/admin/ai_task/preferred - the 'Data generation tasks'
    default entity. POST makes Phoenix MCP's AI Task entity HA's default (overwriting any
    prior one); DELETE clears it if it is Phoenix MCP's; GET reports the current state so the
    panel can render the button and warn before overwriting. Admin session auth only.
    """

    url = "/api/phoenix-mcp/admin/ai_task/preferred"
    name = "api:phoenix-mcp:admin:ai_task:preferred"
    requires_auth = True

    def _entry(self) -> Any:
        entries = self.hass.config_entries.async_entries(DOMAIN)
        return entries[0] if entries else None

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        from .ai_task import ai_task_preferred_status  # noqa: PLC0415
        entry = self._entry()
        if entry is None:
            return _err("invalid_request", "Phoenix MCP is not set up.", 400, rid)
        return _ok(ai_task_preferred_status(self.hass, entry), request_id=rid)

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        from .ai_task import set_ai_task_preferred, AiTaskSetupError  # noqa: PLC0415
        entry = self._entry()
        if entry is None:
            return _err("invalid_request", "Phoenix MCP is not set up.", 400, rid)
        try:
            return _ok(set_ai_task_preferred(self.hass, entry), request_id=rid)
        except AiTaskSetupError as err:
            return _err("invalid_request", str(err), 400, rid)

    @require_admin
    async def delete(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        from .ai_task import clear_ai_task_preferred  # noqa: PLC0415
        entry = self._entry()
        if entry is None:
            return _err("invalid_request", "Phoenix MCP is not set up.", 400, rid)
        return _ok(clear_ai_task_preferred(self.hass, entry), request_id=rid)


def _wipe_flag(value: Any, default: bool) -> bool:
    """Coerce a wipe-scope flag: honor a real bool, else fall back to default."""
    return value if isinstance(value, bool) else default


class PhoenixAdminWipeView(PhoenixView):
    """DELETE /api/phoenix-mcp/admin/wipe - selectively wipe Phoenix MCP data.

    Body flags (all optional): wipe_core (tokens/audit/versions/settings,
    default True), wipe_providers (Agent Chat provider keys, default True),
    wipe_mesa (MESA profiles, default False).
    """

    url = "/api/phoenix-mcp/admin/wipe"
    name = "api:phoenix-mcp:admin:wipe"
    requires_auth = True

    @require_admin
    async def delete(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        if body.get("confirm") != "WIPE":
            return _err("invalid_request", 'confirm must be "WIPE".', 400, rid)

        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        user = request[KEY_HASS_USER]

        # Selective scope. A non-boolean value (garbage) falls back to the
        # default, which for a destructive op means the safe direction: the two
        # credential-bearing scopes default on, MESA (authored safety policy)
        # defaults off, matching the panel's checkbox defaults.
        wipe_core = _wipe_flag(body.get("wipe_core"), True)
        wipe_providers = _wipe_flag(body.get("wipe_providers"), True)
        wipe_mesa = _wipe_flag(body.get("wipe_mesa"), False)

        if wipe_core:
            from .approvals import (  # noqa: PLC0415
                REASON_WIPED,
                collect_pending_approvals_for_wipe,
                dismiss_approval_notification,
                fire_approval_resolved_event,
            )

            # Remove any Phoenix-created Assist pipeline first (outside the store lock,
            # which it takes to clear its tracked id): once async_wipe resets settings
            # the id is gone and the pipeline would be orphaned in HA.
            from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
            await async_remove_assist_pipeline(hass, data)

            async with data.store.async_lock:
                data.rate_limiter.destroy_all()
                data.rate_limit_notified.clear()
                data.token_counters.clear()
                data.stale_tools_advised.clear()
                await data.audit.async_wipe()
                await data.versions.async_wipe()
                # The card catalog is a regenerable cache of what this instance
                # can render, not user data, but it is Phoenix-owned storage and
                # a wipe must not leave a file behind. The panel re-harvests on
                # its next load, so clearing it costs nothing.
                await data.card_catalog.async_clear()

                for _tid in list(data.expiry_timers):
                    cancel_expiry_timer(data, _tid)

                # Capture pending approvals BEFORE the wipe clears them, so their
                # notifications can be dismissed and phoenix_mcp_approval_resolved fired
                # (a wipe deletes every token, so its queued approvals must be torn
                # down exactly as revocation tears down a single token's).
                wiped_approvals = collect_pending_approvals_for_wipe(data.store, REASON_WIPED)
                data.approvals_in_progress.clear()

                active_slugs = [token_name_slug(t.name) for t in data.store.list_tokens()]
                await data.store.async_wipe()

            for approval in wiped_approvals:
                dismiss_approval_notification(hass, approval.id)
                fire_approval_resolved_event(hass, approval)

            if not data.routes_registered and data.async_register_routes:
                await data.async_register_routes()
                data.routes_registered = True

            # Sensor removal runs after the lock is released. A concurrent token creation
            # with the same slug as a just-wiped token could have its sensors removed here.
            # This race is accepted: wipe is a destructive admin operation and should not
            # be run concurrently with token creation.
            if data.async_on_token_archived:
                await asyncio.gather(
                    *[data.async_on_token_archived(slug) for slug in active_slugs],
                    return_exceptions=True,
                )
            # Clear unconditionally: if any sensor removal above failed and left stale
            # entries in token_id_sensors, they would cause async_write_ha_state() to
            # be called for token IDs that no longer exist. Since the wipe removes all
            # tokens, there are no valid sensors left regardless.
            data.token_id_sensors.clear()

            # Settings were reset, so the voice agent and AI Task are no longer
            # configured; re-sync both (they may have been active before the wipe).
            if data.async_sync_voice_agent is not None:
                data.async_sync_voice_agent()
            if data.async_sync_ai_task is not None:
                data.async_sync_ai_task()

        if wipe_providers:
            from .agentcli import async_wipe_agentcli_secrets
            await async_wipe_agentcli_secrets(hass)

        if wipe_mesa:
            if data.mesa is not None:
                async with data.mesa.lock:
                    await data.mesa.async_wipe()
            else:
                # MESA runtime unavailable this session (setup failed / off):
                # clear the on-disk store directly so "delete MESA data" holds.
                from homeassistant.helpers.storage import Store
                mesa_store: Store[dict] = Store(hass, MESA_STORAGE_VERSION, MESA_STORAGE_KEY)
                await mesa_store.async_save(
                    {"profiles": {}, "dismissed_suggestions": []}
                )

        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )

        return web.Response(status=204, headers={"X-Phoenix-Request-ID": rid})


class PhoenixAdminTokenRotateView(PhoenixView):
    """POST /api/phoenix-mcp/admin/tokens/{token_id}/rotate - replace the raw token value atomically."""

    url = "/api/phoenix-mcp/admin/tokens/{token_id}/rotate"
    name = "api:phoenix-mcp:admin:token_rotate"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, token_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        user = request[KEY_HASS_USER]

        async with data.store.async_lock:
            result = await data.store.async_rotate_token(token_id)

        if result is None:
            return _err("not_found", "Token not found.", 404, rid, key="tokenNotFound")

        token, raw_token = result

        hass.bus.async_fire("phoenix_mcp_token_rotated", {
            "token_id": token.id,
            "token_name": token.name,
            "rotated_by": user.id,
            "timestamp": utcnow().isoformat(),
        })

        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method=request.method,
            resource=request.path,
            outcome="allowed",
            client_ip=request.remote or "",
            settings=data.store.get_settings(),
        )

        response_body = token.to_dict()
        response_body["token"] = raw_token
        return _ok(response_body, request_id=rid)


class PhoenixAdminApprovalsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/approvals - list approvals, optionally filtered by status/token."""

    url = "/api/phoenix-mcp/admin/approvals"
    name = "api:phoenix-mcp:admin:approvals"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        from .approvals import list_approvals  # noqa: PLC0415

        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        status = request.query.get("status")
        token_id = request.query.get("token_id")
        paged = _parse_pagination(request, rid, default_limit=50, max_limit=500)
        if isinstance(paged, web.Response):
            return paged
        limit, offset = paged
        records = list_approvals(data.store, status=status, token_id=token_id)
        total = len(records)
        records = records[offset:offset + limit]
        # in_progress rides the row rather than only the claim EVENT, so a panel
        # that was loaded (or reloaded) while an approve was already executing
        # still renders it as non-actionable. The event gives the instant update;
        # this is what makes a fresh page correct.
        return _ok({
            "approvals": [
                {**r.to_dict(), "in_progress": r.id in data.approvals_in_progress}
                for r in records
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }, request_id=rid)


class PhoenixAdminApprovalView(PhoenixView):
    """GET / DELETE /api/phoenix-mcp/admin/approvals/{approval_id}.

    DELETE is an alias for reject with reason 'admin_cancelled'.
    """

    url = "/api/phoenix-mcp/admin/approvals/{approval_id}"
    name = "api:phoenix-mcp:admin:approval"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, approval_id: str) -> web.Response:
        from .approvals import get_approval  # noqa: PLC0415

        rid = request["phoenix_mcp_rid"]
        hass = self.hass
        data: PhoenixData = hass.data[DOMAIN]
        record = get_approval(data.store, approval_id)
        if record is None:
            return _err("not_found", "Approval not found.", 404, rid, key="approvalNotFound")
        return _ok(record.to_dict(), request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, approval_id: str) -> web.Response:
        from .approvals import REASON_ADMIN_CANCELLED  # noqa: PLC0415

        return await _resolve_approval(
            self.hass, request, approval_id,
            terminal_status="cancelled",
            auto_reason=REASON_ADMIN_CANCELLED,
        )


class PhoenixAdminApprovalApproveView(PhoenixView):
    """POST /api/phoenix-mcp/admin/approvals/{approval_id}/approve."""

    url = "/api/phoenix-mcp/admin/approvals/{approval_id}/approve"
    name = "api:phoenix-mcp:admin:approval_approve"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, approval_id: str) -> web.Response:
        return await _approve_approval(self.hass, request, approval_id)


class PhoenixAdminApprovalBatchApproveView(PhoenixView):
    """POST /api/phoenix-mcp/admin/approvals/batch/approve.

    Approves several pending approvals in ONE admin action. Purely an operator
    convenience: it changes nothing about the tool surface, the stateless
    transport, or the approval record. Each id still goes through the SAME
    `_approve_approval` (its own store-lock claim, its own token/capability/
    kill-switch re-validation, its own executor run, its own audit row), so the
    security model is byte-for-byte what it was, and a batch is indistinguishable
    from the admin clicking Approve N times, only faster.

    It exists because a routine migration produced ~20 near-identical writes and
    approving them one at a time was the single biggest cost of the exercise.

    STOPS AT THE FIRST FAILURE, deliberately. Home Assistant writes are not
    transactional, so no batch can be atomic; the honest choice is between
    stopping and ploughing on. Stopping wins because the likely failures here are
    SYSTEMATIC rather than independent (a capability revoked mid-batch, the kill
    switch thrown, a stale expected_hash after an earlier item rewrote the same
    file), and continuing would burn the rest of the queue producing the same
    error N more times. Everything after the failure is left PENDING and still
    individually approvable, so stopping costs the operator nothing but a second
    click once they have dealt with the cause.
    """

    url = "/api/phoenix-mcp/admin/approvals/batch/approve"
    name = "api:phoenix-mcp:admin:approval_batch_approve"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        ids = body.get("approval_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            return _err(
                "invalid_request", "approval_ids must be a non-empty array of strings.", 400, rid,
            )
        if len(ids) != len(set(ids)):
            # Not pedantry: the second attempt at a duplicate would hit the
            # in-progress claim or the already-terminal branch and be reported as
            # a failure, halting the batch on the caller's own typo.
            return _err("invalid_request", "approval_ids must not repeat an id.", 400, rid)
        if len(ids) > MAX_BATCH_APPROVALS:
            return _err(
                "invalid_request",
                f"A batch may hold at most {MAX_BATCH_APPROVALS} approvals.",
                400, rid,
            )

        applied: list[dict] = []
        failure: dict | None = None
        for index, approval_id in enumerate(ids):
            response = await _approve_approval(self.hass, request, approval_id)
            try:
                parsed = json.loads(response.text or "{}")
            except ValueError:  # pragma: no cover - _ok/_err always emit JSON
                parsed = {}
            # A 200 is not sufficient: an executor that returned isError finalizes
            # the record as rejected/execution_failed and still answers 200, and
            # treating that as applied would report a failed write as a success.
            if response.status == 200 and parsed.get("status") == "approved":
                applied.append({"approval_id": approval_id, "tool_name": parsed.get("tool_name")})
                continue
            failure = {
                "approval_id": approval_id,
                "tool_name": parsed.get("tool_name"),
                "status": response.status,
                "error": parsed.get("error") or parsed.get("rejected_reason") or "failed",
                "message": parsed.get("message"),
            }
            if parsed.get("message_key"):
                failure["message_key"] = parsed["message_key"]
                if parsed.get("message_params"):
                    failure["message_params"] = parsed["message_params"]
            # Everything from here on is untouched and still pending.
            remaining = ids[index + 1:]
            break
        else:
            remaining = []

        data: PhoenixData = self.hass.data[DOMAIN]
        user = request[KEY_HASS_USER]
        data.audit.record(
            request_id=rid,
            token_id="admin",
            token_name=f"admin:{user.id}",
            method="approval/batch_approve",
            resource=f"approvals:{len(ids)}",
            outcome="allowed" if failure is None else "denied",
            client_ip="",
            settings=data.store.get_settings(),
            payload={
                "requested": len(ids),
                "applied": len(applied),
                "stopped_at": failure["approval_id"] if failure else None,
            },
        )
        return _ok(
            {"applied": applied, "failed": failure, "remaining": remaining},
            request_id=rid,
        )


class PhoenixAdminApprovalRejectView(PhoenixView):
    """POST /api/phoenix-mcp/admin/approvals/{approval_id}/reject."""

    url = "/api/phoenix-mcp/admin/approvals/{approval_id}/reject"
    name = "api:phoenix-mcp:admin:approval_reject"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, approval_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        reason = body.get("reason") if isinstance(body, dict) else None
        if reason is not None and not isinstance(reason, str):
            return _err("invalid_request", "reason must be a string.", 400, rid)
        return await _resolve_approval(
            self.hass, request, approval_id,
            terminal_status="rejected",
            auto_reason=reason,
        )


async def _resolve_approval(
    hass,
    request: web.Request,
    approval_id: str,
    *,
    terminal_status: str,
    auto_reason: str | None,
) -> web.Response:
    """Reject or cancel a pending approval. Idempotent on already-resolved records."""
    from .approvals import (  # noqa: PLC0415
        dismiss_approval_notification,
        fire_approval_resolved_event,
        get_approval,
        async_update_approval_status,
    )

    rid = request["phoenix_mcp_rid"]
    data: PhoenixData = hass.data[DOMAIN]
    user = request[KEY_HASS_USER]
    async with data.store.async_lock:
        record = get_approval(data.store, approval_id)
        if record is None:
            return _err("not_found", "Approval not found.", 404, rid, key="approvalNotFound")
        if record.is_terminal():
            return _ok(record.to_dict(), request_id=rid)
        if approval_id in data.approvals_in_progress:
            return _err("conflict", "Approval is already being processed.", 409, rid)
        updated = await async_update_approval_status(
            data.store,
            approval_id,
            status=terminal_status,
            approved_by_user_id=user.id,
            rejected_reason=auto_reason,
        )
    if updated is None:
        return _err("not_found", "Approval not found.", 404, rid, key="approvalNotFound")
    dismiss_approval_notification(hass, approval_id)
    fire_approval_resolved_event(hass, updated)
    data.audit.record(
        request_id=rid,
        token_id="admin",
        token_name=f"admin:{user.id}",
        method=f"approval/{terminal_status}",
        resource=f"approval:{updated.tool_name}:{approval_id}",
        outcome="denied",
        client_ip="",
        settings=data.store.get_settings(),
    )
    return _ok(updated.to_dict(), request_id=rid)


async def _approve_approval(hass: HomeAssistant, request: web.Request, approval_id: str) -> web.Response:
    """Validate, execute, and finalize a previously-pending approval."""
    from .approvals import (  # noqa: PLC0415
        REASON_CAPABILITY_DENIED,
        REASON_KILL_SWITCH,
        REASON_TOKEN_INACTIVE,
        STATUS_APPROVED,
        STATUS_CANCELLED,
        STATUS_REJECTED,
        create_approval_notification,
        dismiss_approval_notification,
        fire_approval_claim_event,
        fire_approval_resolved_event,
        get_approval,
        async_update_approval_status,
    )
    from .helpers import effective_cap  # noqa: PLC0415
    from .mcp_view import async_execute_approved_tool  # noqa: PLC0415

    rid = request["phoenix_mcp_rid"]
    data: PhoenixData = hass.data[DOMAIN]
    user = request[KEY_HASS_USER]

    async with data.store.async_lock:
        record = get_approval(data.store, approval_id)
        if record is None:
            return _err("not_found", "Approval not found.", 404, rid, key="approvalNotFound")
        if record.is_terminal():
            return _ok(record.to_dict(), request_id=rid)

        token = data.store.get_token_by_id(record.token_id)
        if token is None or not token.is_valid():
            await async_update_approval_status(
                data.store, approval_id,
                status=STATUS_CANCELLED,
                approved_by_user_id=user.id,
                rejected_reason=REASON_TOKEN_INACTIVE,
            )
            updated = get_approval(data.store, approval_id)
            if updated:
                dismiss_approval_notification(hass, approval_id)
                fire_approval_resolved_event(hass, updated)
            return _err("not_found", "Token no longer active.", 409, rid)

        # The MESA sentinel cap is not a real token capability, so effective_cap
        # would auto-deny it. The MESA re-evaluation happens inside the executor
        # instead (it rejects entities that became prohibited/read_only).
        if record.cap_name != MESA_CONFIRM_CAP and effective_cap(token, record.cap_name) == "deny":
            await async_update_approval_status(
                data.store, approval_id,
                status=STATUS_REJECTED,
                approved_by_user_id=user.id,
                rejected_reason=REASON_CAPABILITY_DENIED,
            )
            updated = get_approval(data.store, approval_id)
            if updated:
                dismiss_approval_notification(hass, approval_id)
                fire_approval_resolved_event(hass, updated)
            return _err("forbidden", "Capability is now denied for this token.", 409, rid)

        settings = data.store.get_settings()
        if settings.kill_switch:
            await async_update_approval_status(
                data.store, approval_id,
                status=STATUS_CANCELLED,
                approved_by_user_id=user.id,
                rejected_reason=REASON_KILL_SWITCH,
            )
            updated = get_approval(data.store, approval_id)
            if updated:
                dismiss_approval_notification(hass, approval_id)
                fire_approval_resolved_event(hass, updated)
            return _err("service_unavailable", "Kill switch engaged.", 503, rid)

        # Atomic claim before releasing the lock: a second concurrent approve
        # that finds this id already claimed is rejected, so the saved side
        # effect runs exactly once even under a double-click / double POST.
        if approval_id in data.approvals_in_progress:
            return _err("conflict", "Approval is already being processed.", 409, rid)
        data.approvals_in_progress.add(approval_id)

    # Tell every surface the claim landed, BEFORE the execution rather than after
    # it: the resolved event cannot fire until the tool finishes, and until then
    # the panel, Agent Chat and the notification would all still offer Approve and
    # Reject on an approval that is already being acted on. Dismissing the
    # notification here is the same signal for the surface that has no UI state.
    resolved = False
    fire_approval_claim_event(hass, approval_id, claimed=True)
    dismiss_approval_notification(hass, approval_id)

    # Execute outside the lock so the tool can use it freely. The claim is
    # released in the finally below so a failed execution stays retryable while
    # a successful one cannot be re-run.
    try:
        try:
            tool_result, outcome, _resource = await async_execute_approved_tool(
                record.tool_name, record.args, token, hass, data,
            )
        except KeyError:
            return _err("invalid_request", "No executor registered for this tool.", 400, rid)
        except Exception:
            _LOGGER.exception("Approval execution failed for %s", approval_id)
            return _err("internal_error", "Execution failed.", 500, rid)

        is_error = bool(tool_result.get("isError"))
        saved_result = {"tool_result": tool_result, "outcome": outcome}
        final_status = STATUS_REJECTED if is_error else STATUS_APPROVED
        auto_reason = "execution_failed" if is_error else None
        audit_outcome: Outcome = "allowed" if final_status == STATUS_APPROVED else "denied"

        def _record_execution_audit(payload: dict | None = None) -> None:
            data.audit.record(
                request_id=rid,
                token_id=record.token_id,
                token_name=record.token_name,
                method=f"approval/{final_status}",
                resource=f"approval:{record.tool_name}:{approval_id}",
                outcome=audit_outcome,
                client_ip="",
                settings=settings,
                payload=payload,
            )

        async with data.store.async_lock:
            updated = await async_update_approval_status(
                data.store, approval_id,
                status=final_status,
                approved_by_user_id=user.id,
                rejected_reason=auto_reason,
                result=saved_result,
            )
        if updated is None:
            _record_execution_audit({
                "finalization": "missing_after_execution",
                "executor_outcome": outcome,
            })
            return _err("not_found", "Approval not found.", 404, rid, key="approvalNotFound")
        if updated.status != final_status:
            _record_execution_audit({
                "finalization": "conflict",
                "stored_status": updated.status,
                "executor_outcome": outcome,
            })
            return _err("conflict", "Approval was already resolved.", 409, rid)
        dismiss_approval_notification(hass, approval_id)
        resolved = True
        fire_approval_resolved_event(hass, updated)
        _record_execution_audit()
        return _ok(updated.to_dict(), request_id=rid)
    finally:
        data.approvals_in_progress.discard(approval_id)
        if not resolved:
            # Execution failed or finalization conflicted, so the record is still
            # pending and genuinely retryable. The surfaces were told to stop
            # offering it, so they have to be told it is back; leaving them quiet
            # would hide a live approval until the next reload. The notification
            # is recreated for the same reason.
            fire_approval_claim_event(hass, approval_id, claimed=False)
            still_pending = get_approval(data.store, approval_id)
            if still_pending is not None and not still_pending.is_terminal():
                create_approval_notification(hass, still_pending)


# ---------------------------------------------------------------------------
# MESA profile administration
# ---------------------------------------------------------------------------


def _mesa_runtime(hass: HomeAssistant, rid: str) -> tuple[Any, web.Response | None]:
    """Return the MESA runtime, or an error response when MESA is unavailable."""
    data: PhoenixData = hass.data[DOMAIN]
    if data.mesa is None:
        return None, _err("service_unavailable", "MESA is not available.", 503, rid)
    return data.mesa, None


def _mesa_migrated_doc(body: dict) -> dict:
    """A profile document stamped with the current MESA schema version.

    The store boundary is the ONLY sanctioned place that label is written.
    mesa-core deliberately never stamps on save, because `schema_version`
    describes the document's CONTENT rather than when it was last written:
    stamping every write would relabel legacy documents as current, and the
    first version needing a real transformation would then skip them with no
    way to tell afterwards. Migration is the one operation allowed to set it.

    That leaves an inverse problem here, which this fixes. A document from the
    panel carries no `schema_version`, and the parser reads "absent" as 1.0-era
    content, because 1.0 was the only version that could have written an
    unversioned document. Ours is freshly authored by a current-version editor,
    so the label said the opposite of the truth. The mislabel is the SAFE
    direction (a future migration still runs against it and no-ops), which is
    why this is hygiene rather than a bug.

    Nothing observable changes today: the 1.0 to 1.1 step is a pure restamp,
    since 1.1 was additive. It is wired up now so that the first version which
    genuinely transforms a document is applied at the boundary, rather than
    discovered later against a store full of documents claiming to be 1.0.

    Fails SOFT on purpose. A malformed or unparseable-version body is returned
    untouched so `SemanticProfile.from_dict` reports the real problem as a 400;
    raising here would turn a caller's bad payload into a 500.
    """
    from .mesa_core.exceptions import MesaError  # noqa: PLC0415
    from .mesa_core.migration import migrate_profile  # noqa: PLC0415

    try:
        if isinstance(body.get("semantic_profile"), dict):
            return migrate_profile(body)
        # `from_dict` also accepts a BARE semantic profile as the entire body
        # (verified: it wraps whatever it is handed), and that shape has to be
        # stamped too, or half the accepted inputs would still be stored
        # claiming to be 1.0. migrate_profile only understands the wrapped form,
        # so wrap it, migrate, and hand the inner object back.
        return migrate_profile({"semantic_profile": body})["semantic_profile"]
    except MesaError:
        return body


def _audit_admin(hass, request, rid, resource) -> None:
    data: PhoenixData = hass.data[DOMAIN]
    user = request[KEY_HASS_USER]
    data.audit.record(
        request_id=rid,
        token_id="admin",
        token_name=f"admin:{user.id}",
        method=request.method,
        resource=resource,
        outcome="allowed",
        client_ip=request.remote or "",
        settings=data.store.get_settings(),
    )


class PhoenixAdminMesaProfilesView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/profiles - list stored entity profiles (paginated)."""

    url = "/api/phoenix-mcp/admin/mesa/profiles"
    name = "api:phoenix-mcp:admin:mesa:profiles"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err

        from .mesa_core.exceptions import InvalidCursorError  # noqa: PLC0415

        q = request.query
        tag = q.get("tag")
        area = q.get("area")
        try:
            limit = int(q.get("limit", 50))
        except (TypeError, ValueError):
            return _err("invalid_request", "limit must be an integer.", 400, rid)
        try:
            domain = q.get("domain")
            result = runtime.store.query(
                domains=[domain] if domain else None,
                tags=[tag] if tag else None,
                areas=[area] if area else None,
                origin=q.get("origin"),
                include_inferred=True,  # admin sees every origin, including inferred
                limit=limit,
                cursor=q.get("cursor"),
            )
        except InvalidCursorError as exc:
            return _err("invalid_request", str(exc), 400, rid)
        except ValueError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        return _ok(
            {
                "profiles": [
                    {"entity_id": row.entity_id, "document": row.stored.to_dict()}
                    for row in result.rows
                ],
                "total_matched": result.total_matched,
                "has_more": result.has_more,
                "next_cursor": result.next_cursor,
            },
            request_id=rid,
        )


class PhoenixAdminMesaProfileView(PhoenixView):
    """GET/PUT/DELETE /api/phoenix-mcp/admin/mesa/profiles/{entity_id} - one entity profile."""

    url = "/api/phoenix-mcp/admin/mesa/profiles/{entity_id}"
    name = "api:phoenix-mcp:admin:mesa:profile"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        stored = runtime.store.get(entity_id)
        effective = runtime.store.get_effective(entity_id)
        explanation = runtime.resolver.explain(entity_id)
        return _ok(
            {
                "entity_id": entity_id,
                "stored": stored.to_dict() if stored is not None else None,
                "effective": effective.to_dict(),
                "explanation": explanation.to_dict(),
            },
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request, entity_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err

        if not _ENTITY_RE.match(entity_id):
            return _err("invalid_request", "Invalid entity ID format.", 400, rid, key="badEntityId")

        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        from .mesa import read_automation_configs  # noqa: PLC0415
        from .mesa_core import MetadataOrigin, SemanticProfile  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415

        try:
            profile = SemanticProfile.from_dict(
                entity_id, _mesa_migrated_doc(body), default_origin=MetadataOrigin.USER
            )
        except MesaValidationError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        async with runtime.lock:
            runtime.store.set(entity_id, profile)
            await runtime.async_save()

        # Cross-check the new profile against the automation registry.
        configs = await self.hass.async_add_executor_job(read_automation_configs, self.hass)
        issues = runtime.validator.validate_entity(entity_id, lambda: configs)

        _audit_admin(self.hass, request, rid, request.path)
        return _ok(
            {
                "entity_id": entity_id,
                "stored": profile.to_dict(),
                "warnings": [_issue_to_dict(i) for i in issues],
            },
            request_id=rid,
        )

    @require_admin
    async def delete(self, request: web.Request, entity_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        async with runtime.lock:
            runtime.store.delete(entity_id)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"entity_id": entity_id, "deleted": True}, request_id=rid)


class PhoenixAdminMesaDomainsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/domains - list all stored domain-level profiles."""

    url = "/api/phoenix-mcp/admin/mesa/domains"
    name = "api:phoenix-mcp:admin:mesa:domains"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        domains = []
        for domain in sorted(runtime.store.domain_keys()):
            stored = runtime.store.get_domain_profile(domain)
            if stored is not None:
                domains.append({"domain": domain, "document": stored.to_dict()})
        return _ok({"domains": domains}, request_id=rid)


class PhoenixAdminMesaAreasView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/areas - list all stored area-level profiles."""

    url = "/api/phoenix-mcp/admin/mesa/areas"
    name = "api:phoenix-mcp:admin:mesa:areas"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        areas = []
        for area_id in sorted(runtime.store.area_keys()):
            stored = runtime.store.get_area_profile(area_id)
            if stored is not None:
                areas.append({"area_id": area_id, "document": stored.to_dict()})
        return _ok({"areas": areas}, request_id=rid)


class PhoenixAdminMesaDomainView(PhoenixView):
    """GET/PUT/DELETE /api/phoenix-mcp/admin/mesa/domains/{domain} - one domain-level profile."""

    url = "/api/phoenix-mcp/admin/mesa/domains/{domain}"
    name = "api:phoenix-mcp:admin:mesa:domain"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, domain: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        stored = runtime.store.get_domain_profile(domain)
        return _ok(
            {"domain": domain, "stored": stored.to_dict() if stored is not None else None},
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request, domain: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        if not _DOMAIN_RE.match(domain):
            return _err("invalid_request", "Invalid domain name.", 400, rid, key="badDomain")
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        from .mesa_core import MetadataOrigin, SemanticProfile  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415

        try:
            profile = SemanticProfile.from_dict(domain, _mesa_migrated_doc(body), default_origin=MetadataOrigin.USER)
        except MesaValidationError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        async with runtime.lock:
            runtime.store.set_domain_profile(domain, profile)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"domain": domain, "stored": profile.to_dict()}, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, domain: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        async with runtime.lock:
            runtime.store.delete_domain_profile(domain)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"domain": domain, "deleted": True}, request_id=rid)


class PhoenixAdminMesaIntegrationsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/integrations - list all stored integration-level profiles.

    Integration profiles (MESA inheritance level between area and domain) are keyed
    by the integration's component name and govern the entities that integration
    created, regardless of entity domain. Vendor sidecars import here.
    """

    url = "/api/phoenix-mcp/admin/mesa/integrations"
    name = "api:phoenix-mcp:admin:mesa:integrations"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        integrations = []
        for integration in sorted(runtime.store.integration_keys()):
            stored = runtime.store.get_integration_profile(integration)
            if stored is not None:
                integrations.append({"integration": integration, "document": stored.to_dict()})
        return _ok({"integrations": integrations}, request_id=rid)


class PhoenixAdminMesaIntegrationView(PhoenixView):
    """GET/PUT/DELETE /api/phoenix-mcp/admin/mesa/integrations/{integration} - one profile."""

    url = "/api/phoenix-mcp/admin/mesa/integrations/{integration}"
    name = "api:phoenix-mcp:admin:mesa:integration"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, integration: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        stored = runtime.store.get_integration_profile(integration)
        return _ok(
            {"integration": integration, "stored": stored.to_dict() if stored is not None else None},
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request, integration: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        # Integration/component names share HA's domain character set.
        if not _DOMAIN_RE.match(integration):
            return _err("invalid_request", "Invalid integration name.", 400, rid)
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        from .mesa_core import MetadataOrigin, SemanticProfile  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415

        try:
            profile = SemanticProfile.from_dict(integration, _mesa_migrated_doc(body), default_origin=MetadataOrigin.USER)
        except MesaValidationError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        async with runtime.lock:
            runtime.store.set_integration_profile(integration, profile)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"integration": integration, "stored": profile.to_dict()}, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, integration: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        async with runtime.lock:
            runtime.store.delete_integration_profile(integration)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"integration": integration, "deleted": True}, request_id=rid)


class PhoenixAdminMesaIntegrationOptionsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/integration-options - integrations that have entities.

    Powers the "Add integration profile" picker. `id` is the component name (the key
    an integration profile is stored under); `name` is the integration's friendly
    title. Limited to integrations that created entities, since those are the only
    ones an integration profile can govern. Phoenix MCP's own domain is excluded.
    """

    url = "/api/phoenix-mcp/admin/mesa/integration-options"
    name = "api:phoenix-mcp:admin:mesa:integration-options"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        registry = er_mod.async_get(self.hass)
        domains = sorted(
            {e.platform for e in registry.entities.values() if e.platform and e.platform != DOMAIN}
        )
        names: dict[str, str] = {}
        try:
            from homeassistant.loader import async_get_integrations  # noqa: PLC0415

            for dom, integ in (await async_get_integrations(self.hass, domains)).items():
                if not isinstance(integ, BaseException):
                    names[dom] = getattr(integ, "name", None) or dom
        except Exception:  # noqa: BLE001 - friendly names are best-effort
            pass
        options = [{"id": dom, "name": names.get(dom, dom)} for dom in domains]
        return _ok({"integrations": options}, request_id=rid)


class PhoenixAdminMesaAreaView(PhoenixView):
    """GET/PUT/DELETE /api/phoenix-mcp/admin/mesa/areas/{area_id} - one area-level profile."""

    url = "/api/phoenix-mcp/admin/mesa/areas/{area_id}"
    name = "api:phoenix-mcp:admin:mesa:area"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, area_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        stored = runtime.store.get_area_profile(area_id)
        return _ok(
            {"area_id": area_id, "stored": stored.to_dict() if stored is not None else None},
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request, area_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        node_err = _validate_node_id("area", area_id, rid)
        if node_err is not None:
            return node_err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        from .mesa_core import MetadataOrigin, SemanticProfile  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415

        try:
            profile = SemanticProfile.from_dict(area_id, _mesa_migrated_doc(body), default_origin=MetadataOrigin.USER)
        except MesaValidationError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        async with runtime.lock:
            runtime.store.set_area_profile(area_id, profile)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"area_id": area_id, "stored": profile.to_dict()}, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, area_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        async with runtime.lock:
            runtime.store.delete_area_profile(area_id)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"area_id": area_id, "deleted": True}, request_id=rid)


class PhoenixAdminMesaDevicesView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/devices - list all stored device-level profiles."""

    url = "/api/phoenix-mcp/admin/mesa/devices"
    name = "api:phoenix-mcp:admin:mesa:devices"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        devices = []
        for device_id in sorted(runtime.store.device_keys()):
            stored = runtime.store.get_device_profile(device_id)
            if stored is not None:
                devices.append({"device_id": device_id, "document": stored.to_dict()})
        return _ok({"devices": devices}, request_id=rid)


class PhoenixAdminMesaDeviceOptionsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/device-options - devices available to profile.

    A device profile is keyed by an opaque registry id, so unlike areas there is
    no readable value for an admin to type or recognise. This is the picker
    source: registry id plus the display name HA itself shows, preferring the
    operator's own rename.
    """

    url = "/api/phoenix-mcp/admin/mesa/device-options"
    name = "api:phoenix-mcp:admin:mesa:device-options"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        registry = dr_mod.async_get(self.hass)
        options = []
        for device in registry.devices.values():
            name = device.name_by_user or device.name or device.id
            options.append({"id": device.id, "name": name})
        options.sort(key=lambda o: (o["name"].lower(), o["id"]))
        return _ok({"devices": options}, request_id=rid)


class PhoenixAdminMesaDeviceView(PhoenixView):
    """GET/PUT/DELETE /api/phoenix-mcp/admin/mesa/devices/{device_id} - one device profile."""

    url = "/api/phoenix-mcp/admin/mesa/devices/{device_id}"
    name = "api:phoenix-mcp:admin:mesa:device"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, device_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        stored = runtime.store.get_device_profile(device_id)
        return _ok(
            {"device_id": device_id, "stored": stored.to_dict() if stored is not None else None},
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request, device_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        node_err = _validate_node_id("device", device_id, rid)
        if node_err is not None:
            return node_err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body

        from .mesa_core import MetadataOrigin, SemanticProfile  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415

        try:
            profile = SemanticProfile.from_dict(device_id, _mesa_migrated_doc(body), default_origin=MetadataOrigin.USER)
        except MesaValidationError as exc:
            return _err("invalid_request", str(exc), 400, rid)

        async with runtime.lock:
            runtime.store.set_device_profile(device_id, profile)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"device_id": device_id, "stored": profile.to_dict()}, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        async with runtime.lock:
            runtime.store.delete_device_profile(device_id)
            await runtime.async_save()
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"device_id": device_id, "deleted": True}, request_id=rid)


class PhoenixAdminMesaVocabularyView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/vocabulary - the canonical MESA tag registry.

    Lets the admin UI source tag autocomplete from mesa-core directly, so the
    frontend never duplicates (and never drifts from) the canonical vocabulary.
    """

    url = "/api/phoenix-mcp/admin/mesa/vocabulary"
    name = "api:phoenix-mcp:admin:mesa:vocabulary"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        from .mesa_core import vocabulary  # noqa: PLC0415
        return _ok(
            {
                "canonical_tags": sorted(vocabulary.CANONICAL_TAGS),
                "canonical_roots": sorted(vocabulary.CANONICAL_ROOTS),
            },
            request_id=rid,
        )


class PhoenixAdminMesaDefaultsView(PhoenixView):
    """GET/PUT /api/phoenix-mcp/admin/mesa/defaults - deployment defaults for unprofiled entities."""

    url = "/api/phoenix-mcp/admin/mesa/defaults"
    name = "api:phoenix-mcp:admin:mesa:defaults"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        defaults = runtime.store.get_deployment_defaults()
        return _ok(
            {"deployment_defaults": defaults.to_dict() if defaults is not None else None},
            request_id=rid,
        )

    @require_admin
    async def put(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        try:
            async with runtime.lock:
                runtime.store.set_deployment_defaults(body)
                await runtime.async_save()
        except (ValueError, KeyError) as exc:
            return _err("invalid_request", f"Invalid deployment defaults: {exc}", 400, rid)
        _audit_admin(self.hass, request, rid, request.path)
        stored = runtime.store.get_deployment_defaults()
        return _ok(
            {"deployment_defaults": stored.to_dict() if stored is not None else None},
            request_id=rid,
        )


class PhoenixAdminMesaIssuesView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/issues - TriggerValidator issues and orphaned profiles."""

    url = "/api/phoenix-mcp/admin/mesa/issues"
    name = "api:phoenix-mcp:admin:mesa:issues"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        refresh = request.query.get("refresh")
        if refresh == "suggestions":
            # Scoped: the Suggestions card's own Rescan button. Trigger issues
            # and orphans are independent, separately-surfaced signals; a
            # suggestions-only rescan must not silently recompute (and
            # potentially change) that unrelated banner.
            from .mesa_suggestions import refresh_suggestions  # noqa: PLC0415

            refresh_suggestions(self.hass, runtime)
        elif refresh:
            from .mesa import async_refresh_trigger_issues, refresh_orphans  # noqa: PLC0415
            from .mesa_suggestions import refresh_suggestions  # noqa: PLC0415

            await async_refresh_trigger_issues(self.hass, runtime)
            refresh_orphans(self.hass, runtime)
            refresh_suggestions(self.hass, runtime)
        return _ok(
            {
                "issues": [_issue_to_dict(i) for i in runtime.trigger_issues],
                "orphans": list(runtime.orphans),
                "orphan_devices": list(runtime.orphan_devices),
                "orphan_areas": list(runtime.orphan_areas),
                "orphan_integrations": list(runtime.orphan_integrations),
                "suggestions": [_issue_to_dict(s) for s in runtime.suggestions],
                "dismissed_suggestions": sorted(runtime.dismissed_suggestions),
            },
            request_id=rid,
        )


def _issue_to_dict(issue) -> dict:
    """Serialise a mesa-core ValidationIssue dataclass."""
    import dataclasses  # noqa: PLC0415

    return dataclasses.asdict(issue)


def _version_summary(r) -> dict:
    """Compact list projection that omits the (potentially large) before/after configs."""
    return {
        "id": r.id,
        "resource_type": r.resource_type,
        "resource_id": r.resource_id,
        "alias": r.alias,
        "action": r.action,
        "token_id": r.token_id,
        "token_name": r.token_name,
        "approved_by_user_id": r.approved_by_user_id,
        "timestamp": r.timestamp,
        "summary": r.summary,
        "summary_key": r.summary_key,
        "summary_params": r.summary_params,
        "has_before": r.before is not None,
        "has_after": r.after is not None,
    }


class PhoenixAdminVersionsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/versions - configuration version history for one resource.

    Requires resource_type and resource_id query params. Returns compact summaries
    (no before/after); fetch a single version for the full diff payload.
    """

    url = "/api/phoenix-mcp/admin/versions"
    name = "api:phoenix-mcp:admin:versions"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        resource_type = request.query.get("resource_type")
        resource_id = request.query.get("resource_id")
        if resource_type and resource_id:
            records = data.versions.list_for(resource_type, resource_id)
            total = len(records)
        elif not resource_type and not resource_id:
            paged = _parse_pagination(request, rid, default_limit=50, max_limit=500)
            if isinstance(paged, web.Response):
                return paged
            limit, offset = paged
            records = data.versions.list_recent(limit, offset)
            total = data.versions.count_recent()
        else:
            return _err("invalid_request", "Provide both resource_type and resource_id, or neither for the recent feed.", 400, rid)
        return _ok({
            "resource_type": resource_type,
            "resource_id": resource_id,
            "versions": [_version_summary(r) for r in records],
            "total": total,
        }, request_id=rid)


_ESPHOME_VERSION_WITHHELD = (
    "# Phoenix MCP withheld this snapshot: its credentials could not be located, "
    "because the content does not parse as YAML. The stored version is intact and "
    "can still be restored."
)


async def _mask_esphome_version(hass: HomeAssistant, body: dict) -> dict:
    """Mask credentials in an esphome_yaml version's before/after, for DISPLAY only.

    The STORED snapshot stays raw, because a restore has to reproduce the file
    byte for byte; this masks only what is served to the panel. Without it the
    Changes tab was the one Phoenix MCP surface that rendered credentials in
    clear text, including a value generated by !phoenix_generate that the agent
    itself was never allowed to see. The file is already plaintext on disk and
    this view is admin-only, so nothing here was a disclosure to someone who
    could not already read it; a rendered pane is simply a far more exposed place
    for a secret to sit than a file is, being screenshot, screen-shared, and
    pasted into issues. The operator reads the real value from the ESPHome
    dashboard, which is the documented path.

    FAILS CLOSED: masking that raises means the credentials could not be located,
    which is exactly when the raw text must not be shown.
    """
    from .esphome_yaml import redact_esphome_text  # noqa: PLC0415
    from .tools.esphome import _read_esphome_secrets  # noqa: PLC0415

    try:
        secret_values, _ = await hass.async_add_executor_job(_read_esphome_secrets, hass)
    except Exception:  # noqa: BLE001
        secret_values = set()

    def _mask(text: str) -> str:
        return redact_esphome_text(text, secret_values)[0]

    out = dict(body)
    for side in ("before", "after"):
        payload = out.get(side)
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            continue
        try:
            masked = await hass.async_add_executor_job(_mask, content)
        except Exception:  # noqa: BLE001
            masked = _ESPHOME_VERSION_WITHHELD
        out[side] = {**payload, "content": masked}
    return out


class PhoenixAdminVersionView(PhoenixView):
    """GET /api/phoenix-mcp/admin/versions/{version_id} - one version with full before/after."""

    url = "/api/phoenix-mcp/admin/versions/{version_id}"
    name = "api:phoenix-mcp:admin:version"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, version_id: str) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        record = data.versions.get(version_id)
        if record is None:
            return _err("not_found", "Version not found.", 404, rid)
        body = record.to_dict()
        if record.resource_type == "esphome_yaml":
            body = await _mask_esphome_version(self.hass, body)
        return _ok(body, request_id=rid)


class PhoenixAdminVersionRestoreView(PhoenixView):
    """POST /api/phoenix-mcp/admin/versions/{version_id}/restore - re-apply a stored version.

    Existing resources are edited; deleted ones are recreated. Runs with admin
    authority and records the change as a 'rollback' attributed to the admin.
    """

    url = "/api/phoenix-mcp/admin/versions/{version_id}/restore"
    name = "api:phoenix-mcp:admin:version:restore"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, version_id: str) -> web.Response:
        from .mcp_view import async_restore_version  # noqa: PLC0415

        rid = request["phoenix_mcp_rid"]
        user = request[KEY_HASS_USER]
        data: PhoenixData = self.hass.data[DOMAIN]
        record = data.versions.get(version_id)
        if record is None:
            return _err("not_found", "Version not found.", 404, rid)
        # The panel sends no body for a default restore; _read_body returns {}
        # for an empty body, 400 for malformed/non-object JSON (which must not
        # silently restore the default side), and 413 over the size cap.
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        side = body.get("side")
        if side not in (None, "before", "after"):
            return _err("invalid_request", "side must be 'before' or 'after'.", 400, rid)
        try:
            tool_result, _outcome, _resource = await async_restore_version(record, user.id, self.hass, data, side)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Version restore failed for %s", version_id)
            return _err("internal_error", "Restore failed.", 500, rid)
        is_error = bool(tool_result.get("isError"))
        data.audit.record(
            request_id=rid,
            token_id=record.token_id or "admin",
            token_name=record.token_name or f"admin:{user.id}",
            method="version/restore",
            resource=f"version_restore:{record.resource_type}:{record.resource_id}",
            outcome="denied" if is_error else "allowed",
            client_ip="",
            settings=data.store.get_settings(),
        )
        if is_error:
            # Relay the executor's OWN reason. It named the exact path and cause
            # ("'wifi.ap.password' is a masked credential and cannot be changed to
            # a new value"); a fixed string turned that into a support ticket.
            # This is an admin-only surface authenticated by an HA session, so
            # there is no caller to keep in the dark, unlike the token-facing
            # tools where uniform messages are what prevent an oracle.
            detail = " ".join(
                item.get("text", "")
                for item in tool_result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
            return _err(
                "invalid_request",
                detail or "Restore could not be applied to the current configuration.",
                400, rid,
            )
        return _ok({
            "restored": True,
            "version_id": version_id,
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
        }, request_id=rid)


class PhoenixAdminMesaOrphansClearView(PhoenixView):
    """POST /api/phoenix-mcp/admin/mesa/orphans/clear - delete all orphaned MESA profiles.

    Recomputes the orphan lists, then deletes every orphaned entity, area, and
    integration profile in one pass (the one-by-one alternative is the per-profile
    DELETE endpoints). Profiles are never auto-deleted; this is an explicit admin
    action surfaced from the MESA tab orphan banner.
    """

    url = "/api/phoenix-mcp/admin/mesa/orphans/clear"
    name = "api:phoenix-mcp:admin:mesa:orphans:clear"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        from .mesa import refresh_orphans  # noqa: PLC0415

        async with runtime.lock:
            # Recompute against the live registries so we delete exactly what is
            # orphaned now, never a stale client-supplied list.
            refresh_orphans(self.hass, runtime)
            entities = list(runtime.orphans)
            devices = list(runtime.orphan_devices)
            areas = list(runtime.orphan_areas)
            integrations = list(runtime.orphan_integrations)
            for eid in entities:
                runtime.store.delete(eid)
            for device_id in devices:
                runtime.store.delete_device_profile(device_id)
            for area_id in areas:
                runtime.store.delete_area_profile(area_id)
            for integration in integrations:
                runtime.store.delete_integration_profile(integration)
            if entities or devices or areas or integrations:
                await runtime.async_save()
            refresh_orphans(self.hass, runtime)
        _audit_admin(self.hass, request, rid, request.path)
        return _ok(
            {
                "deleted": {
                    "entities": entities,
                    "devices": devices,
                    "areas": areas,
                    "integrations": integrations,
                },
                "count": len(entities) + len(devices) + len(areas) + len(integrations),
            },
            request_id=rid,
        )


class PhoenixAdminMesaExportView(PhoenixView):
    """GET /api/phoenix-mcp/admin/mesa/export - download every MESA profile as an archive.

    Delegates to mesa-core's export_profiles: a faithful, storage-agnostic dump
    of every stored profile document (entities, domains, integrations, areas,
    deployment defaults) that any MESA host can import. Export is sync over the
    in-memory dict backend with no await point, so no lock is needed.
    """

    url = "/api/phoenix-mcp/admin/mesa/export"
    name = "api:phoenix-mcp:admin:mesa:export"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        from .mesa_core.portability import export_profiles  # noqa: PLC0415

        archive = export_profiles(runtime.store)
        _audit_admin(self.hass, request, rid, request.path)
        return _ok(archive, request_id=rid)


class PhoenixAdminMesaImportView(PhoenixView):
    """POST /api/phoenix-mcp/admin/mesa/import - import a MESA profile archive.

    Body: {"archive": {...}, "on_conflict": "skip" | "overwrite"} (default
    skip). mesa-core validates every document and quarantines failures in the
    result's "invalid" map; nothing invalid is ever written. The all-or-nothing
    "error" mode is deliberately not exposed (the panel reports per-key results
    instead of aborting the whole archive on the first conflict).
    """

    url = "/api/phoenix-mcp/admin/mesa/import"
    name = "api:phoenix-mcp:admin:mesa:import"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        archive = body.get("archive")
        if not isinstance(archive, dict):
            return _err("invalid_request", "archive is required.", 400, rid)
        on_conflict = body.get("on_conflict", "skip")
        if on_conflict not in ("skip", "overwrite"):
            return _err(
                "invalid_request", "on_conflict must be 'skip' or 'overwrite'.", 400, rid
            )

        from .mesa import refresh_orphans  # noqa: PLC0415
        from .mesa_core.exceptions import MesaValidationError  # noqa: PLC0415
        from .mesa_core.portability import import_profiles  # noqa: PLC0415
        from .mesa_suggestions import refresh_suggestions  # noqa: PLC0415

        async with runtime.lock:
            try:
                result = import_profiles(runtime.store, archive, on_conflict=on_conflict)
            except MesaValidationError as exc:
                return _err("invalid_request", str(exc), 400, rid)
            if result.imported or result.overwritten:
                await runtime.async_save()
            # An archive from another deployment can carry profiles for targets
            # that do not exist here; recompute so the orphan banner and the
            # suggestions card reflect the new store immediately. Best-effort:
            # a scan failure must never take down an otherwise-successful
            # import response (the import itself already landed and saved
            # above); the panel's own Rescan recovers the cached lists.
            try:
                refresh_orphans(self.hass, runtime)
                refresh_suggestions(self.hass, runtime)
            except Exception:  # noqa: BLE001 - degrade, never fail the import
                _LOGGER.warning(
                    "MESA import: post-import orphan/suggestion rescan failed rid=%s",
                    rid, exc_info=True,
                )
        _audit_admin(self.hass, request, rid, request.path)
        return _ok(
            {
                "imported": result.imported,
                "overwritten": result.overwritten,
                "skipped_existing": list(result.skipped_existing),
                "invalid": dict(result.invalid),
            },
            request_id=rid,
        )


def _suggestions_payload(runtime) -> dict:
    return {
        "suggestions": [_issue_to_dict(s) for s in runtime.suggestions],
        "dismissed_suggestions": sorted(runtime.dismissed_suggestions),
    }


class PhoenixAdminCardCatalogView(PhoenixView):
    """GET/POST /api/phoenix-mcp/admin/card_catalog - the dashboard card catalog.

    POST is the panel harvester reporting which Lovelace cards this instance can
    render. It has to come from a browser: a card registers itself on
    window.customCards at runtime and half of a real plugin set builds its type
    strings by concatenation, so the backend cannot read this off disk (see
    card_catalog.py). The payload is admin-authenticated but assembled from
    third-party card code, so card_catalog sanitizes every field; this view only
    bounds the request and hands it over.

    GET backs the panel's catalog status. It reports harvested_at as None when no
    browser has reported yet, which callers must not render as "no cards".
    """

    url = "/api/phoenix-mcp/admin/card_catalog"
    name = "api:phoenix-mcp:admin:card_catalog"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        catalog = data.card_catalog.catalog
        return _ok({
            "harvested": catalog.harvested,
            **data.card_catalog.as_dict(),
        }, request_id=rid)

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        data: PhoenixData = self.hass.data[DOMAIN]
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        if not isinstance(body.get("entries"), list):
            return _err("invalid_request", "entries must be a list.", 400, rid)
        catalog = await data.card_catalog.async_replace(
            entries=body.get("entries"),
            resource_count=body.get("resource_count"),
            failed_imports=body.get("failed_imports"),
        )
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({
            "harvested": catalog.harvested,
            "harvested_at": catalog.harvested_at,
            "cards": len(catalog.entries),
            "available": sum(1 for e in catalog.entries if e.available),
            "failed_imports": len(catalog.failed_imports),
        }, request_id=rid)


class PhoenixAdminMesaSuggestionDismissView(PhoenixView):
    """POST /api/phoenix-mcp/admin/mesa/suggestions/dismiss - dismiss one profile suggestion.

    The key must match a CURRENTLY computed suggestion (recomputed under the
    lock, never trusted from the client), which bounds the dismissed set by
    construction. Dismissals persist in the phoenix_mcp_mesa store.
    """

    url = "/api/phoenix-mcp/admin/mesa/suggestions/dismiss"
    name = "api:phoenix-mcp:admin:mesa:suggestions:dismiss"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        key = body.get("key")
        if not isinstance(key, str) or not key:
            return _err("invalid_request", "key is required.", 400, rid)

        from .mesa_suggestions import refresh_suggestions  # noqa: PLC0415

        async with runtime.lock:
            refresh_suggestions(self.hass, runtime)
            if key not in {s.key for s in runtime.suggestions}:
                return _err("not_found", "No current suggestion with that key.", 404, rid)
            runtime.dismissed_suggestions.add(key)
            await runtime.async_save()
            refresh_suggestions(self.hass, runtime)
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"dismissed": key, **_suggestions_payload(runtime)}, request_id=rid)


class PhoenixAdminMesaSuggestionRestoreView(PhoenixView):
    """POST /api/phoenix-mcp/admin/mesa/suggestions/restore - un-dismiss suggestion(s).

    Body: {"key": "..."} restores one, {"all": true} clears every dismissal.
    """

    url = "/api/phoenix-mcp/admin/mesa/suggestions/restore"
    name = "api:phoenix-mcp:admin:mesa:suggestions:restore"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request["phoenix_mcp_rid"]
        runtime, err = _mesa_runtime(self.hass, rid)
        if err is not None:
            return err
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        key = body.get("key")
        restore_all = body.get("all") is True
        if not restore_all and (not isinstance(key, str) or not key):
            return _err("invalid_request", "Provide key or all: true.", 400, rid)

        from .mesa_suggestions import refresh_suggestions  # noqa: PLC0415

        async with runtime.lock:
            if restore_all:
                restored: str | int = len(runtime.dismissed_suggestions)
                runtime.dismissed_suggestions.clear()
            else:
                if key not in runtime.dismissed_suggestions:
                    return _err("not_found", "That key is not dismissed.", 404, rid)
                runtime.dismissed_suggestions.discard(key)
                restored = str(key)
            await runtime.async_save()
            refresh_suggestions(self.hass, runtime)
        _audit_admin(self.hass, request, rid, request.path)
        return _ok({"restored": restored, **_suggestions_payload(runtime)}, request_id=rid)


ALL_ADMIN_VIEWS: list[type[PhoenixView]] = [
    PhoenixAdminInfoView,
    PhoenixAdminCatalogView,
    PhoenixAdminArchivedTokensView,
    PhoenixAdminArchivedTokenView,
    PhoenixAdminTokensView,
    PhoenixAdminTokenView,
    PhoenixAdminTokenPresetsView,
    PhoenixAdminTokenPresetView,
    PhoenixAdminTokenPresetApplyView,
    PhoenixAdminPermissionsView,
    PhoenixAdminPermissionDomainView,
    PhoenixAdminPermissionDeviceView,
    PhoenixAdminPermissionEntityView,
    PhoenixAdminResolveView,
    PhoenixAdminTokenRotateView,
    PhoenixAdminScopeView,
    PhoenixAdminEntityTreeView,
    PhoenixAdminEntityHintsView,
    PhoenixAdminEntityHintView,
    PhoenixAdminTokenStatsView,
    PhoenixAdminTokenConnectionView,
    PhoenixAdminTokenAuditView,
    PhoenixAdminAuditView,
    PhoenixAdminSettingsView,
    PhoenixAdminVoiceAgentPipelineView,
    PhoenixAdminAiTaskPreferredView,
    PhoenixAdminWipeView,
    PhoenixAdminApprovalsView,
    PhoenixAdminApprovalView,
    PhoenixAdminApprovalApproveView,
    PhoenixAdminApprovalBatchApproveView,
    PhoenixAdminApprovalRejectView,
    PhoenixAdminVersionsView,
    PhoenixAdminVersionView,
    PhoenixAdminVersionRestoreView,
    PhoenixAdminMesaProfilesView,
    PhoenixAdminMesaProfileView,
    PhoenixAdminMesaDomainsView,
    PhoenixAdminMesaDomainView,
    PhoenixAdminMesaIntegrationsView,
    PhoenixAdminMesaIntegrationView,
    PhoenixAdminMesaIntegrationOptionsView,
    PhoenixAdminMesaAreasView,
    PhoenixAdminMesaAreaView,
    PhoenixAdminMesaDevicesView,
    PhoenixAdminMesaDeviceOptionsView,
    PhoenixAdminMesaDeviceView,
    PhoenixAdminMesaVocabularyView,
    PhoenixAdminMesaDefaultsView,
    PhoenixAdminMesaIssuesView,
    PhoenixAdminMesaOrphansClearView,
    PhoenixAdminMesaExportView,
    PhoenixAdminMesaImportView,
    PhoenixAdminMesaSuggestionDismissView,
    PhoenixAdminMesaSuggestionRestoreView,
    PhoenixAdminCardCatalogView,
]
