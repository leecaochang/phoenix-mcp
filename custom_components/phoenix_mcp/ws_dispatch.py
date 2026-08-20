"""In-process dispatch of Home Assistant WebSocket API commands.

Some HA capabilities (notably helper CRUD: input_boolean/create, etc.) are only
exposed through the WebSocket command API. There is no public host-side function
and the underlying storage collections are not reachable. This module invokes
HA's already-registered WS command handlers directly, in-process, with no socket
and no long-lived access token: it looks up the handler in
``hass.data["websocket_api"]``, validates the message against the registered
schema, and runs the handler against a synthetic ``ActiveConnection`` that
captures the result instead of writing to a socket.

This is the one place that leans on HA internals (`ActiveConnection`, the
`async_response` result flow). It is deliberately isolated here, with a version
shim and tests, so an HA change breaks this module loudly rather than the
callers. The create/update/delete commands are `@require_admin`, so dispatch
runs under a real admin user resolved from `hass.auth`; Phoenix MCP's own capability
gate (e.g. cap_helper_write + Confirm + audit) decides whether a call runs at
all, the same way create_automation performs a privileged file write under a cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.websocket_api import const as ws_const
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

# The only WS commands Phoenix MCP is permitted to dispatch in-process. async_ws_command
# refuses anything outside this set, so a future caller cannot turn user input
# into an arbitrary privileged command run as admin. Adding a command here is a
# deliberate act. Keep in sync with the callers in mcp_view.py (helper CRUD +
# list, backup read/create, lovelace dashboard CRUD) and radio.py (the two ZHA
# reads). restore_backup is deliberately absent (too destructive). The helper
# "list" read is used to capture the pre-change config for version history.
# Most ZHA write-side commands (zha/devices/permit, zha/devices/reconfigure,
# zha/topology/update) are deliberately absent: their handlers enable
# process-wide ZHA debug logging and/or park cleanup callbacks in
# connection.subscriptions, which a synthetic capturing connection never
# releases, and reconfigure/topology never call send_result (guaranteed
# timeout). Permit and remove go through the zha.permit / zha.remove admin
# services instead; reconfigure goes through async_zha_reconfigure_device below.
# zha/devices/bind, zha/devices/unbind, and the group CRUD commands are the
# narrow exceptions: they await ZHA's own operation and then call send_result
# without registering a subscription. Phoenix resolves every IEEE and numeric
# group id from scoped registry entities; callers never provide either kind of
# radio identifier.
_HELPER_DOMAINS = (
    "input_boolean", "input_number", "input_text",
    "input_select", "input_datetime", "counter", "timer",
)
ALLOWED_WS_COMMANDS: frozenset[str] = frozenset(
    [f"{domain}/{op}" for domain in _HELPER_DOMAINS for op in ("create", "update", "delete", "list")]
    + [
        "backup/agents/info", "backup/info", "backup/generate",
        "lovelace/dashboards/list", "lovelace/dashboards/create",
        "lovelace/dashboards/update", "lovelace/dashboards/delete",
        "logbook/get_events",
        # Integration-aware logger control. Unlike logger/set_level, this resolves
        # the integration's declared logger set and preserves HA's none/once/
        # permanent semantics. Phoenix adds capability, scope, approval, and
        # timed-restoration gates before reaching this command.
        "logger/integration_log_level",
        "zha/device", "zha/network/settings", "zha/groups",
        "zha/devices/groupable", "zha/group/add", "zha/group/remove",
        "zha/group/members/add", "zha/group/members/remove",
        "zha/devices/bind", "zha/devices/unbind",
        # Blueprint authoring. HA's own handlers do the whole job: blueprint/save
        # parses the YAML, builds a Blueprint against the domain schema, refuses an
        # existing path unless allow_override, and reloads every consumer on an
        # override; blueprint/delete raises BlueprintInUse when an automation still
        # references it. Both send_result cleanly. blueprint/import is deliberately
        # ABSENT: it fetches an operator-unseen URL from inside HA, which is an
        # SSRF primitive with LAN and supervisor reach. Callers pass literal YAML.
        "blueprint/save", "blueprint/delete",
        # Energy dashboard preferences, READ ONLY. This is the only route to the
        # mapping between a statistic and its Energy role (grid / solar / battery /
        # gas / water / individual device): it lives in .storage/energy, which no
        # filesystem tool can reach (FILESYSTEM_ALLOWED_DIRS is www/themes/
        # custom_templates), and no state or registry read exposes it. Without it a
        # caller removing an integration cannot see that it is about to orphan an
        # Energy source, because nothing else in HA records that dependency.
        # energy/save_prefs is @require_admin and each of its three top-level keys
        # is a FULL REPLACE: sending "device_consumption" with a partial list
        # silently deletes every other device the operator configured. That is why
        # NOTHING here composes a payload from what an agent sent. tools/energy.py
        # owns the only caller: it re-reads the current preferences UNREDACTED,
        # applies one addressed mutation, verifies the result differs from the
        # original in exactly the intended way, and sends ONLY the top-level keys
        # that actually changed (HA's EnergyManager.async_update copies through any
        # key the update omits, so an untouched key is never at risk).
        # energy/validate stays absent: it validates whatever is already persisted
        # rather than a submitted payload, so it cannot pre-flight a write.
        # energy/validate is a READ despite the name: it validates what is already
        # persisted, not a submitted payload, so it cannot pre-flight a write. What
        # it can do is answer "is the Energy dashboard actually working", naming
        # entries whose entity no longer exists or that record no statistics, which
        # is the failure the operator otherwise only meets as a blank graph.
        # energy/solar_forecast returns production forecasts for the config entries
        # a solar source names, and {} when none does.
        "energy/get_prefs", "energy/save_prefs", "energy/validate", "energy/solar_forecast",
    ]
)


class WsDispatchError(Exception):
    """Raised when an in-process WS command cannot be dispatched or fails."""


class WsDashboardNotFoundError(WsDispatchError):
    """Raised when a named Lovelace dashboard does not exist.

    Split out from the generic error so callers can tell "this dashboard is not
    there" (a legitimate absence, which the CAS paths treat as before=None) from
    "lovelace could not be read" (a failure that must surface, not be mistaken for
    absence and silently overwritten).
    """


class _CapturingConnection(ActiveConnection):
    """An ActiveConnection that captures the command result in a Future.

    Overrides the three result sinks the storage-collection handlers use
    (send_result on success, send_error on validation failure, and
    async_handle_exception on an unexpected error) so the dispatched command's
    outcome can be awaited instead of serialized onto a socket.
    """

    def __init__(self, hass: HomeAssistant, user: Any) -> None:
        # Build the constructor kwargs against the live ActiveConnection
        # signature and pass only what it accepts, so Phoenix MCP works across the HA
        # versions it supports: parameters have been added over time (e.g.
        # `remote` in HA 2026.6). Read names from the code object, never
        # inspect.signature, which on Python 3.14 evaluates ActiveConnection's
        # TYPE_CHECKING-only annotations and raises NameError.
        available: dict[str, Any] = {
            "logger": _LOGGER,
            "hass": hass,
            "send_message": self._capture_send,
            "user": user,
            "refresh_token": None,
            "remote": None,
        }
        code = ActiveConnection.__init__.__code__
        accepted = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
        super().__init__(**{k: v for k, v in available.items() if k in accepted})
        self.result_future: asyncio.Future = hass.loop.create_future()

    @callback
    def _capture_send(self, message: Any) -> None:
        """Capture a result delivered via send_message.

        Some handlers bypass send_result and send a pre-built result message
        directly (the logbook get_events handler sends already-serialized JSON
        bytes this way). Parse it, and on a success result message resolve the
        future; non-result messages (events, pings) are ignored.
        """
        if self.result_future.done():
            return
        data: Any = message
        if isinstance(data, (bytes, bytearray, str)):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return
        if not isinstance(data, dict) or data.get("type") != "result":
            return
        if data.get("success"):
            self.result_future.set_result(data.get("result"))
        else:
            err = data.get("error") or {}
            self.result_future.set_exception(
                WsDispatchError(f"{err.get('code')}: {err.get('message')}")
            )

    @callback
    def send_result(self, msg_id: int, result: Any | None = None) -> None:
        if not self.result_future.done():
            self.result_future.set_result(result)

    @callback
    def send_error(self, msg_id: int, code: str, message: str, *args: Any, **kwargs: Any) -> None:
        if not self.result_future.done():
            self.result_future.set_exception(WsDispatchError(f"{code}: {message}"))

    @callback
    def async_handle_exception(self, msg: dict, err: Exception) -> None:
        if not self.result_future.done():
            self.result_future.set_exception(err)


async def _resolve_admin_user(hass: HomeAssistant) -> Any:
    """Return a real active admin user (owner preferred) for require_admin commands."""
    users = await hass.auth.async_get_users()
    active_admins = [u for u in users if u.is_active and u.is_admin and not u.system_generated]
    if not active_admins:
        raise WsDispatchError("No active admin user is available to run this command.")
    return next((u for u in active_admins if u.is_owner), active_admins[0])


async def async_ws_command(
    hass: HomeAssistant,
    command: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Invoke a registered HA WebSocket command in-process and return its result.

    Raises WsDispatchError if the command is not on the ALLOWED_WS_COMMANDS
    allowlist, is not registered, the payload fails the command's schema, no
    admin user is available, or the handler reports an error / times out.
    """
    if command not in ALLOWED_WS_COMMANDS:
        raise WsDispatchError(f"WebSocket command not allowed: {command}")

    handlers = hass.data.get(ws_const.DOMAIN)
    if not handlers or command not in handlers:
        raise WsDispatchError(f"WebSocket command not available: {command}")
    handler, schema = handlers[command]

    msg: dict[str, Any] = {"id": 1, "type": command, **payload}
    if schema not in (None, False):
        try:
            msg = schema(msg)
        except vol.Invalid as err:
            raise WsDispatchError(f"Invalid arguments for {command}: {err}") from err

    user = await _resolve_admin_user(hass)

    # Everything from here leans on HA internals: ActiveConnection construction,
    # the require_admin/async_response decorators, and the handler delivering its
    # outcome through the connection. Wrap any unexpected failure (a changed
    # ActiveConnection signature, a handler-side error surfaced via the result
    # future, etc.) as WsDispatchError so callers always get a clean tool error
    # instead of a raw HA exception if HA changes underneath us.
    try:
        connection = _CapturingConnection(hass, user)
        # require_admin runs synchronously (raises if not admin); async_response
        # schedules a background task that resolves result_future.
        handler(hass, connection, msg)
        return await asyncio.wait_for(connection.result_future, timeout)
    except WsDispatchError:
        raise
    except TimeoutError as err:
        raise WsDispatchError(f"WebSocket command {command} timed out.") from err
    except Exception as err:  # noqa: BLE001 - degrade any HA-internal breakage to a clean error
        raise WsDispatchError(f"WebSocket command {command} failed: {err}") from err


# The ActiveConnection.__init__ params _CapturingConnection knows how to supply.
# Must stay in sync with the `available` dict in _CapturingConnection.__init__.
# HA adds params over time (e.g. `remote` in 2026.6); construction supplies the
# intersection with the live signature, so old and new HA both work.
_SUPPLIED_CONNECTION_PARAMS = frozenset(
    {"logger", "hass", "send_message", "user", "refresh_token", "remote"}
)
_REQUIRED_CONNECTION_METHODS = ("send_result", "send_error", "async_handle_exception")
# async_ws_command asserts two things about the registry beyond "it is a dict":
# each entry unpacks as (handler, schema), and the handler takes exactly
# (hass, connection, msg). Both are HA's shape, not ours, and neither is visible
# to the constructor/method checks below.
_REGISTRY_ENTRY_LEN = 2
_HANDLER_ARGCOUNT = 3


def _handler_contract_error(registry: dict) -> str | None:
    """Check a registered allowlisted command against the call in async_ws_command.

    Returns None when compatible OR when nothing to sample is registered yet
    (websocket_api can register after Phoenix MCP sets up, which is not drift).
    Samples rather than sweeps: every entry is registered by the same HA
    machinery, so one is representative and the probe stays cheap.
    """
    sample = next((c for c in sorted(ALLOWED_WS_COMMANDS) if c in registry), None)
    if sample is None:
        return None
    entry = registry[sample]
    if not isinstance(entry, tuple) or len(entry) != _REGISTRY_ENTRY_LEN:
        return (
            f"websocket_api registry entry for {sample} is no longer a "
            f"(handler, schema) pair"
        )
    handler = entry[0]
    if not callable(handler):
        return f"websocket_api handler for {sample} is not callable"
    # Read arity from the code object, never inspect.signature (see the comment
    # in check_ws_dispatch_compat). A handler taking *args is a decorator wrapper
    # that forwards whatever it is given, so it tells us nothing and is skipped.
    code = getattr(handler, "__code__", None)
    if code is None:
        return None
    if code.co_flags & 0x04:  # CO_VARARGS
        return None
    positional = code.co_argcount - len(getattr(handler, "__defaults__", None) or ())
    if positional != _HANDLER_ARGCOUNT:
        return (
            f"websocket_api handler for {sample} takes {positional} positional "
            f"args, expected {_HANDLER_ARGCOUNT} (hass, connection, msg)"
        )
    return None


def check_ws_dispatch_compat(hass: HomeAssistant) -> str | None:
    """Best-effort check that the HA internals this module relies on are intact.

    Returns None when compatible, or a short human-readable reason when HA appears
    to have changed shape (in which case helper CRUD will still fail per-call with
    a clean WsDispatchError; this just surfaces it once at startup). Detects the
    realistic break scenarios: a new required ActiveConnection constructor param
    Phoenix MCP cannot supply, a missing result-sink method, a registry that is no
    longer a dict, and (via _handler_contract_error) a registry entry that no
    longer unpacks as (handler, schema) or a handler that no longer takes
    (hass, connection, msg). The last two are the call async_ws_command actually
    makes, and nothing about the connection object reveals them.
    """
    try:
        registry = hass.data.get(ws_const.DOMAIN)
        if registry is not None and not isinstance(registry, dict):
            return "websocket_api command registry is not a dict"
        # Read parameter names straight from the code object. Do NOT use
        # inspect.signature here: on Python 3.14 it eagerly evaluates the
        # target's annotations, and ActiveConnection.__init__ annotates a
        # TYPE_CHECKING-only name (WebSocketAdapter) that is undefined at
        # runtime, so signature() raises NameError and would abort setup.
        code = getattr(ActiveConnection.__init__, "__code__", None)
        if code is not None:
            posargs = code.co_varnames[1 : code.co_argcount]  # skip self
            defaults = getattr(ActiveConnection.__init__, "__defaults__", None) or ()
            required = list(posargs[: len(posargs) - len(defaults)] if defaults else posargs)
            # Keyword-only params are required too unless they have a default in
            # __kwdefaults__; construction would fail per-call on one Phoenix MCP cannot
            # supply, so the probe must flag it the same as a positional param.
            kwonly = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
            kwdefaults = getattr(ActiveConnection.__init__, "__kwdefaults__", None) or {}
            required += [p for p in kwonly if p not in kwdefaults]
            unsupported = [p for p in required if p not in _SUPPLIED_CONNECTION_PARAMS]
            if unsupported:
                return (
                    "ActiveConnection.__init__ has required params Phoenix MCP cannot "
                    f"supply: {', '.join(unsupported)}"
                )
        missing_methods = [m for m in _REQUIRED_CONNECTION_METHODS if not hasattr(ActiveConnection, m)]
        if missing_methods:
            return f"ActiveConnection is missing methods: {', '.join(missing_methods)}"
        if isinstance(registry, dict):
            handler_err = _handler_contract_error(registry)
            if handler_err is not None:
                return handler_err
        return None
    except Exception as err:  # noqa: BLE001 - advisory probe must never abort setup
        return f"compatibility probe could not run: {err}"


# ---------------------------------------------------------------------------
# Lovelace dashboard view/card config (read + write).
#
# The lovelace/config WS command returns an opaque orjson.Fragment that cannot be
# read back in-process, so these go directly through the lovelace integration's
# LovelaceConfig objects (the same internal state HA's own handler uses). Kept in
# this module, the designated home for HA-internals access. Only storage-mode
# dashboards are writable; YAML-mode dashboards are file-backed.
# ---------------------------------------------------------------------------


def _lovelace_dashboard(hass: HomeAssistant, url_path: str | None) -> Any:
    """Return the LovelaceConfig for a url_path (None = default dashboard), or None.

    Mirrors HA's lovelace/config handler: for the default, prefer the 'lovelace'
    dashboard (YAML mode) then the storage-mode default keyed None.

    None means ONLY that no dashboard is registered under that url_path. Lovelace
    being absent raises instead: returning None for both made an unreadable
    lovelace look like a missing dashboard, and the write paths treat a missing
    dashboard as "nothing to preserve".
    """
    try:
        from homeassistant.components.lovelace.const import (  # noqa: PLC0415
            DOMAIN as _LL_DOMAIN,
            LOVELACE_DATA,
        )
    except Exception as exc:  # noqa: BLE001 - lovelace not loaded
        raise WsDispatchError(f"lovelace is not loaded: {exc}") from exc
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        raise WsDispatchError("lovelace is not loaded")
    dashboards = data.dashboards
    if url_path is None:
        return dashboards.get(_LL_DOMAIN) or dashboards.get(None)
    return dashboards.get(url_path)


async def async_get_lovelace_config(hass: HomeAssistant, url_path: str | None) -> dict[str, Any] | None:
    """Return a dashboard's stored view/card config, or None if it has none.

    None means the dashboard exists but is auto-generated (no stored config).
    Raises WsDashboardNotFoundError for an unknown dashboard, WsDispatchError when
    lovelace itself is unavailable or the read fails.
    """
    config = _lovelace_dashboard(hass, url_path)
    if config is None:
        raise WsDashboardNotFoundError(f"config_not_found: unknown dashboard {url_path!r}")
    try:
        from homeassistant.components.lovelace.const import ConfigNotFound  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"lovelace unavailable: {exc}") from exc
    try:
        return await config.async_load(False)
    except ConfigNotFound:
        return None
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"failed to read dashboard config: {exc}") from exc


async def async_save_lovelace_config(
    hass: HomeAssistant, url_path: str | None, config_data: dict[str, Any]
) -> None:
    """Save a dashboard's view/card config. Storage-mode dashboards only.

    Raises WsDashboardNotFoundError for an unknown dashboard and WsDispatchError for
    a YAML-mode dashboard (file-backed, cannot be written) or a failed save, rather
    than HA's opaque 'Not supported'.
    """
    config = _lovelace_dashboard(hass, url_path)
    if config is None:
        raise WsDashboardNotFoundError(f"config_not_found: unknown dashboard {url_path!r}")
    # mode property returns "storage" (MODE_STORAGE) or "yaml" (MODE_YAML).
    if getattr(config, "mode", None) != "storage":
        raise WsDispatchError(
            "only storage-mode dashboards can be written; this dashboard is YAML-mode"
        )
    try:
        await config.async_save(config_data)
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"failed to save dashboard config: {exc}") from exc


# ---------------------------------------------------------------------------
# ZHA device reconfigure (re-interview).
#
# The zha/devices/reconfigure WS command cannot be dispatched through
# async_ws_command: its handler parks a dispatcher-cleanup callback in
# connection.subscriptions (never released on a synthetic connection) and
# never calls send_result (progress arrives as subscription events), so the
# dispatch would leak a listener and then time out. This mirrors the handler
# body minus the subscription: validate the device exists, then start the
# reinterview task exactly like HA's own handler does.
# ---------------------------------------------------------------------------


async def async_zha_reconfigure_device(hass: HomeAssistant, ieee: str) -> None:
    """Start a ZHA device reinterview (fire-and-forget, like HA's own handler).

    The reinterview runs as a background task and completes out-of-band
    (5-60s+, longer for sleepy battery devices); there is no completion
    signal to await. Raises WsDispatchError when ZHA is unavailable or the
    device is not on the network. HA-coupling point: get_zha_gateway and the
    gateway API postdate the zha-lib extraction (~HA 2024.8); on older HA this
    degrades to a clean per-call error.
    """
    try:
        from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - zha missing or restructured
        raise WsDispatchError(f"ZHA is not available: {exc}") from exc
    try:
        gateway = get_zha_gateway(hass)
        device = gateway.get_device(_zha_eui64(ieee))
    except WsDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001 - gateway not running / API drift
        raise WsDispatchError(f"ZHA gateway is not available: {exc}") from exc
    if device is None:
        raise WsDispatchError("not_found: ZHA device not found")
    try:
        hass.async_create_task(gateway.async_reinterview_device(device.ieee))
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"failed to start reconfigure: {exc}") from exc


def _zha_eui64(ieee: str) -> Any:
    """Convert an ieee string to zigpy's EUI64 type (what the gateway keys on)."""
    try:
        from zigpy.types import EUI64  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"ZHA is not available: {exc}") from exc
    try:
        return EUI64.convert(ieee)
    except Exception as exc:  # noqa: BLE001
        raise WsDispatchError(f"invalid ieee address: {ieee}") from exc
