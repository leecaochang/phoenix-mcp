"""Phoenix MCP custom integration for Home Assistant."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant, Event, EventType, callback
from homeassistant.helpers import area_registry as ar_mod
from homeassistant.helpers import device_registry as dr_mod
from homeassistant.helpers import entity_registry as er_mod
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from .audit import AuditLog
from .version_store import VersionStore
from .card_catalog import CardCatalogStore
from .const import APPROVAL_SWEEP_INTERVAL, AUDIT_STORAGE_KEY, AUDIT_STORAGE_VERSION, CARD_CATALOG_STORAGE_KEY, CARD_CATALOG_STORAGE_VERSION, DOMAIN, EXPIRY_CHECK_INTERVAL, FLUSH_INTERVAL, RUNTIME_READY_KEY, SENSOR_PUSH_INTERVAL, VERSION_STORAGE_KEY, VERSION_STORAGE_VERSION
from .data import PhoenixData
from .helpers import async_archive_expired_token, cancel_expiry_timer, schedule_expiry_timer
from .policy_engine import template_blocklist_vars
from .rate_limiter import RateLimiter
from .token_store import TokenStore

# HA-added template names (globals, filters, and tests) that are safe: pure
# math/string/data helpers with no entity state or registry access. When the
# runtime audit runs, any name HA adds to the render environment that is not in
# this set and not in the blocklist triggers a warning so new HA template
# functions don't silently bypass Phoenix MCP filtering.
_SAFE_TEMPLATE_NAMES = frozenset({
    "bool", "float", "int", "version", "typeof", "is_number",
    "zip", "apply", "combine", "iif", "as_function", "pack", "unpack",
    "merge_response", "e", "pi", "tau", "sin", "cos", "tan",
    "asin", "acos", "atan", "atan2", "log", "sqrt", "average",
    "median", "statistical_mode", "min", "max", "bitwise_and",
    "bitwise_or", "bitwise_xor", "clamp", "wrap", "remap",
    "slugify", "urlencode", "md5", "sha1", "sha256", "sha512",
    "flatten", "shuffle", "intersect", "difference", "union",
    "symmetric_difference", "set", "tuple", "as_datetime",
    "as_local", "as_timedelta", "as_timestamp", "strptime",
    "timedelta", "now", "utcnow", "relative_time", "time_since",
    "time_until", "today_at",
    "range", "lipsum", "dict", "cycler", "joiner", "namespace", "undefined",
    # Filter-only names.
    "add", "base64_decode", "base64_encode", "contains", "from_hex",
    "from_json", "is_defined", "multiply", "ord", "ordinal",
    "regex_findall", "regex_findall_index", "regex_match", "regex_replace",
    "regex_search", "timestamp_custom", "timestamp_local", "timestamp_utc",
    "to_json", "random", "round",
    # Test-only names.
    "datetime", "list", "match", "search", "string_like",
})

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


def _entry_platforms() -> list[str]:
    """All platforms this entry may have forwarded (for a symmetric unload).

    sensor is always forwarded; ai_task is forwarded (best-effort) when HA supports
    it. ai_task_supported() is import-based and stable for the process lifetime, so
    unload computes the same list; unloading a platform that never set up is a no-op.
    """
    from .ai_task import ai_task_supported  # noqa: PLC0415

    platforms = list(PLATFORMS)
    if ai_task_supported():
        platforms.append("ai_task")
    return platforms


def _audit_template_sandbox() -> None:
    """Warn about unrecognized names in Phoenix MCP's template render environment.

    Runs once at setup. Audits the hass-less environment actually used by
    render_template_for_token() across globals, filters, AND tests (Jinja2
    render variables cannot shadow filters or tests, so every name HA registers
    there is reachable). Any unrecognized name triggers a log warning so future
    HA versions adding new template functions don't silently bypass Phoenix MCP
    entity filtering.
    """
    try:
        from .helpers import safe_template_env

        env = safe_template_env()
    except Exception:
        # Construction failing means every render_template call will fail the
        # same way: HA's TemplateEnvironment has changed shape. Surface it once,
        # loudly, at startup instead of only as per-call invalid_request errors.
        _LOGGER.warning(
            "Phoenix MCP: template sandbox environment failed to construct; the "
            "render_template tool will fail per-call on this HA version",
            exc_info=True,
        )
        return
    try:
        import jinja2.sandbox

        blocked = set(template_blocklist_vars().keys())
        overridden = {"states", "state_attr", "is_state", "is_state_attr", "has_value"}
        known = blocked | overridden | _SAFE_TEMPLATE_NAMES
        base = jinja2.sandbox.ImmutableSandboxedEnvironment()
        for kind, names, base_names in (
            ("global", env.globals, base.globals),
            ("filter", env.filters, base.filters),
            ("test", env.tests, base.tests),
        ):
            for name in set(names) - set(base_names):
                if name not in known and not name.startswith("_"):
                    _LOGGER.warning(
                        "Phoenix MCP template sandbox: unrecognized HA template %s '%s' - "
                        "this function is not blocked and may bypass entity filtering",
                        kind,
                        name,
                    )
    except Exception:
        _LOGGER.debug("Phoenix MCP: could not audit template environment", exc_info=True)


# Names of the view classes already registered on HA's aiohttp router, kept in
# hass.data under its own key so it survives unload: async_unload_entry pops
# hass.data[DOMAIN], but the ROUTES it registered cannot be unregistered and are
# still there. Same pattern as panel._PANEL_REGISTERED_KEY.
_VIEWS_REGISTERED_KEY = "phoenix_mcp_registered_views"


@callback
def _listen_once_until_unload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    event_type: EventType | str,
    listener: Callable,
) -> None:
    """Listen for one event, unregistering on unload ONLY if it never fired.

    `hass.bus.async_listen_once` already removes its own listener when the event
    arrives, so handing its remove-callback straight to `entry.async_on_unload`
    asks Home Assistant to remove the same listener twice: once by firing, once
    by unloading. The second removal raises `ValueError: list.remove(x): x not in
    list`, which HA catches and logs as "Unable to remove unknown job listener"
    with a traceback naming Phoenix.

    LIVE-FOUND on a config-entry reload, and only there: `homeassistant_started`
    fires once at boot, so the listener is already gone by the time any later
    reload unloads the entry. Nothing breaks, which is why it survived, but it
    puts an ERROR with a Phoenix traceback in the operator's log on every reload,
    and a log that cries wolf is where a real fault goes unnoticed. The
    `homeassistant_stop` registration has the same shape and the same defect at
    shutdown.

    The listener is wrapped rather than the removal being made
    exception-tolerant: swallowing the error would also hide a genuine
    double-remove somewhere else.
    """
    fired = False

    def _mark() -> None:
        nonlocal fired
        fired = True

    # inspect, not asyncio: asyncio.iscoroutinefunction is deprecated for 3.16.
    # This reads the function's own coroutine flag and evaluates no annotations,
    # so it is not the inspect.signature hazard documented for HA internals.
    #
    # The two wrappers have DISTINCT names rather than one conditional `_once`,
    # because a coroutine and a callback are different signatures and a shared
    # name is a redefinition. The async branch has to stay async: wrapping an
    # async listener in a @callback would leave its coroutine unawaited and
    # silently skip the shutdown flush _on_stop performs.
    if inspect.iscoroutinefunction(listener):
        async def _async_once(event: Event) -> None:
            _mark()
            await listener(event)

        remove = hass.bus.async_listen_once(event_type, _async_once)
    else:
        @callback
        def _sync_once(event: Event) -> None:
            _mark()
            listener(event)

        remove = hass.bus.async_listen_once(event_type, _sync_once)

    @callback
    def _remove_if_still_pending() -> None:
        if not fired:
            remove()

    entry.async_on_unload(_remove_if_still_pending)


@callback
def _register_views(hass: HomeAssistant, view_classes: list) -> None:
    """Register each view class once per Home Assistant process.

    Registration is permanent: aiohttp has no way to remove a route, so HA's
    register_view is one-way and unload leaves every Phoenix route in place. It
    does not RAISE on a repeat, which is the part worth knowing, because it makes
    the bug silent: aiohttp reuses a resource only when it is the last one added,
    so re-registering the whole set builds a second complete set of resources
    that shadow the first and can never be reached. A config-entry reload leaked
    about 93 dead route objects, every time, for the life of the process.

    Skipping the ones already present is correct rather than merely tidy: the
    views hold no per-setup state (they read hass.data[DOMAIN] fresh on every
    request), so the routes registered by the previous setup serve the new one
    with no change. The kill switch works the same way, refusing at request time
    rather than by removing routes.
    """
    registered: set[str] = hass.data.setdefault(_VIEWS_REGISTERED_KEY, set())
    for view_cls in view_classes:
        if view_cls.__name__ in registered:
            continue
        view = view_cls()
        view.hass = hass
        hass.http.register_view(view)
        registered.add(view_cls.__name__)


async def _rollback_failed_setup(
    hass: HomeAssistant, entry: ConfigEntry, data: PhoenixData | None
) -> None:
    """Remove every surface a partial setup could have published."""
    hass.data[RUNTIME_READY_KEY] = False
    if data is not None:
        data.ready = False
        data.shutting_down = True
        try:
            from .assist_api import async_unregister_assist_api  # noqa: PLC0415

            async_unregister_assist_api(data)
        except Exception:  # noqa: BLE001 - rollback continues through every surface
            _LOGGER.exception("Phoenix MCP: failed to unregister Assist during setup rollback")
        try:
            from .voice_agent import async_unregister_voice_agent  # noqa: PLC0415

            async_unregister_voice_agent(hass, entry)
        except Exception:  # noqa: BLE001 - rollback continues through every surface
            _LOGGER.exception("Phoenix MCP: failed to unregister voice during setup rollback")

    _remove_frontend(hass)
    try:
        await hass.config_entries.async_unload_platforms(entry, _entry_platforms())
    except Exception:  # noqa: BLE001 - preserve the original setup exception
        _LOGGER.exception("Phoenix MCP: failed to unload platforms during setup rollback")
    if data is not None and hass.data.get(DOMAIN) is data:
        hass.data.pop(DOMAIN, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Phoenix MCP atomically from a config entry."""
    hass.data[RUNTIME_READY_KEY] = False
    try:
        result = await _async_setup_entry_impl(hass, entry)
    except BaseException:
        await _rollback_failed_setup(hass, entry, hass.data.get(DOMAIN))
        raise
    if not result:
        await _rollback_failed_setup(hass, entry, hass.data.get(DOMAIN))
    return result


async def _async_setup_entry_impl(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Phoenix MCP from a config entry.

    Initialises storage, registers admin views and the panel unconditionally.
    Proxy and MCP views are only registered when the kill switch is off.
    Schedules a periodic flush of last_used_at timestamps and wires registry
    change listeners to invalidate the entity tree cache.
    """
    store = await TokenStore.async_create(hass)
    rate_limiter = RateLimiter()
    audit_store: Store[dict] = Store(hass, AUDIT_STORAGE_VERSION, AUDIT_STORAGE_KEY)
    audit = AuditLog(store=audit_store, maxlen=store.get_settings().audit_log_maxlen)
    await audit.async_load()

    versions = VersionStore(store=Store(hass, VERSION_STORAGE_VERSION, VERSION_STORAGE_KEY))
    await versions.async_load()

    card_catalog = CardCatalogStore(
        store=Store(hass, CARD_CATALOG_STORAGE_VERSION, CARD_CATALOG_STORAGE_KEY)
    )
    await card_catalog.async_load()

    data = PhoenixData(
        hass=hass,
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
        versions=versions,
        card_catalog=card_catalog,
        ready=False,
        enforce_mcp_lifecycle=True,
    )
    # hass.data is keyed by DOMAIN (not config entry ID). This is intentional: the config
    # flow enforces a single Phoenix MCP instance via async_abort("already_configured"), so there
    # is always at most one entry. Keying by entry ID would add complexity for no benefit.
    hass.data[DOMAIN] = data

    from .logger_control import async_get_logger_override_manager
    data.logger_control = await async_get_logger_override_manager(hass)

    # Build the MESA runtime unconditionally (even under the kill switch): the
    # admin profile API must work regardless, and the enforcement gate is simply
    # never reached when no client routes are registered. A failure here must
    # not block Phoenix MCP setup. The integration remains available for reads
    # and recovery, while state-changing MESA gates fail closed unless the
    # operator explicitly selected off mode.
    from .mesa import async_setup_mesa
    try:
        data.mesa = await async_setup_mesa(hass, store.get_settings().mesa_mode)
        data.mesa_setup_failed = False
        ir.async_delete_issue(hass, DOMAIN, "mesa_runtime_unavailable")
    except Exception:  # noqa: BLE001 - MESA must never block Phoenix MCP startup
        _LOGGER.exception(
            "Phoenix MCP: MESA runtime setup failed; protected writes will be refused"
        )
        data.mesa = None
        data.mesa_setup_failed = True
        ir.async_create_issue(
            hass,
            DOMAIN,
            "mesa_runtime_unavailable",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="mesa_runtime_unavailable",
        )

    # Record mesa-core's own denials into the audit log. Attached even when MESA
    # is off (the mode is a runtime setting an admin can flip without a restart)
    # and independent of the kill switch, like the rest of the audit plumbing.
    # Detached on unload so a config-entry reload cannot stack handlers, which
    # would duplicate every future row.
    from .mesa_audit import attach_mesa_audit_bridge, detach_mesa_audit_bridge
    _mesa_bridge = attach_mesa_audit_bridge(data)
    entry.async_on_unload(lambda: detach_mesa_audit_bridge(_mesa_bridge))

    # Surface, once at startup, any HA-internal drift that would break the
    # in-process WS dispatch used by helper CRUD. Per-call failures still
    # degrade to a clean error; this just makes the cause visible early.
    from .ws_dispatch import check_ws_dispatch_compat
    _ws_incompat = check_ws_dispatch_compat(hass)
    if _ws_incompat is not None:
        _LOGGER.warning(
            "Phoenix MCP: in-process WebSocket dispatch may be incompatible with this HA "
            "version (%s); helper CRUD tools may be unavailable.",
            _ws_incompat,
        )
    from .helpers import check_system_log_compat
    _syslog_incompat = check_system_log_compat(hass)
    if _syslog_incompat is not None:
        _LOGGER.warning(
            "Phoenix MCP: system_log integration has changed shape on this HA version "
            "(%s); log diagnostic tools will report degraded source status.",
            _syslog_incompat,
        )

    from .admin_view import ALL_ADMIN_VIEWS
    from .agentcli import ALL_AGENTCLI_ADMIN_VIEWS
    _register_views(hass, ALL_ADMIN_VIEWS + ALL_AGENTCLI_ADMIN_VIEWS)

    from .panel import (
        async_register_phoenix_panel,
        async_sync_agentchat_inject,
        async_sync_mesa_inject,
    )
    await async_register_phoenix_panel(hass)
    # Optional, experimental, default-off: inject the in-context profile control
    # into HA's native config pages. Independent of the kill switch (admin-only).
    await async_sync_mesa_inject(hass)
    # Optional, default-off: the global Agent Chat window module (floats over the
    # whole HA UI). Independent of the kill switch here; the panel gates showing it.
    await async_sync_agentchat_inject(hass)

    settings = store.get_settings()

    async def _register_routes() -> None:
        """Register agent-facing views. Skipped when kill switch is active."""
        from .mcp_view import ALL_MCP_VIEWS
        from .skill_view import ALL_SKILL_VIEWS
        from .agentcli import ALL_AGENTCLI_CHAT_VIEWS
        _register_views(hass, ALL_MCP_VIEWS + ALL_SKILL_VIEWS + ALL_AGENTCLI_CHAT_VIEWS)

    data.async_register_routes = _register_routes
    if not settings.kill_switch:
        await _register_routes()
        data.routes_registered = True

    # Assist bridge: register the Phoenix MCP scoped llm.API so HA's native Assist/voice
    # conversation agents can drive the bound token's tools. Kill-switch-gated like
    # the client routes (Assist is agent activity); the admin settings PATCH toggles
    # it live when the kill switch flips. No-op on HA versions lacking the seam.
    from .assist_api import (
        async_probe_assist_api,
        async_register_assist_api,
        async_unregister_assist_api,
    )
    assist_supported = await async_probe_assist_api(hass)
    if not settings.kill_switch and assist_supported:
        async_register_assist_api(hass, data)
    entry.async_on_unload(lambda: async_unregister_assist_api(data))

    # Phoenix MCP voice agent: register Phoenix MCP as HA's own conversation agent when fully
    # configured, so native Assist/voice runs Phoenix MCP's own model loop (voice_agent.py).
    # NOT kill-switch-gated: it stays registered but declines every turn under the
    # kill switch, so an Assist pipeline pointed at Phoenix MCP degrades gracefully instead of
    # hard-erroring. The admin settings PATCH re-syncs it live via this closure.
    from .voice_agent import async_sync_voice_agent, async_unregister_voice_agent

    @callback
    def _sync_voice_agent() -> None:
        async_sync_voice_agent(hass, entry, data)

    data.async_sync_voice_agent = _sync_voice_agent
    _sync_voice_agent()
    entry.async_on_unload(lambda: async_unregister_voice_agent(hass, entry))

    from .sensor import async_create_token_sensors, async_remove_token_sensors

    async def _on_token_created(token) -> None:
        """Create sensor entities when a new token is minted."""
        await async_create_token_sensors(hass, entry, token)
        schedule_expiry_timer(hass, data, token)

    async def _on_token_archived(token_slug: str) -> None:
        """Remove sensor entities when a token is revoked or archived."""
        await async_remove_token_sensors(hass, token_slug)

    data.async_on_token_created = _on_token_created
    data.async_on_token_archived = _on_token_archived

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The AI Task entity (ai_task.py) is optional; forward it separately and guarded
    # so a failure setting up that platform never aborts Phoenix MCP's core setup.
    from .ai_task import ai_task_supported  # noqa: PLC0415
    if ai_task_supported():
        try:
            await hass.config_entries.async_forward_entry_setups(entry, ["ai_task"])
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not set up the Phoenix MCP AI Task entity", exc_info=True)

    async def _flush_last_used(_now=None) -> None:
        await store.async_flush_last_used()

    cancel_flush = async_track_time_interval(hass, _flush_last_used, FLUSH_INTERVAL)
    entry.async_on_unload(cancel_flush)

    async def _push_sensor_updates(_now=None) -> None:
        for sensors in data.token_id_sensors.values():
            for sensor in sensors:
                if sensor.hass is not None:
                    sensor.async_write_ha_state()

    cancel_sensor_push = async_track_time_interval(hass, _push_sensor_updates, SENSOR_PUSH_INTERVAL)
    entry.async_on_unload(cancel_sensor_push)

    async def _flush_audit(_now=None) -> None:
        try:
            await audit.async_save()
        except Exception:
            _LOGGER.warning("Audit flush failed; will retry next interval", exc_info=True)

    # Single-slot holder so the unload closure always cancels the LATEST timer,
    # not the one registered first.
    _audit_flush_cancel: list = []

    def _reschedule_audit_flush() -> None:
        """(Re)register the periodic audit flush from the current setting.

        Called at setup and by the admin settings PATCH when
        audit_flush_interval changes, so a new interval takes effect
        immediately. 0 ("Never") deregisters the timer entirely.
        """
        while _audit_flush_cancel:
            _audit_flush_cancel.pop()()
        interval = data.store.get_settings().audit_flush_interval
        if interval == 0:
            return
        _audit_flush_cancel.append(
            async_track_time_interval(hass, _flush_audit, timedelta(minutes=interval))
        )

    data.reschedule_audit_flush = _reschedule_audit_flush
    _reschedule_audit_flush()
    def _cancel_audit_flush() -> None:
        for cancel in _audit_flush_cancel:
            cancel()

    entry.async_on_unload(_cancel_audit_flush)

    def _cancel_pending_sensor_flush() -> None:
        if data.sensor_flush_cancel is not None:
            data.sensor_flush_cancel()
            data.sensor_flush_cancel = None

    entry.async_on_unload(_cancel_pending_sensor_flush)

    async def _check_expired_tokens(_now=None) -> None:
        for token in list(store.list_tokens()):
            if token.is_expired():
                await async_archive_expired_token(hass, data, token)

    await _check_expired_tokens()
    for _token in store.list_tokens():
        schedule_expiry_timer(hass, data, _token)
    cancel_expiry = async_track_time_interval(hass, _check_expired_tokens, EXPIRY_CHECK_INTERVAL)
    entry.async_on_unload(cancel_expiry)
    def _cancel_expiry_timers() -> None:
        for token_id in list(data.expiry_timers):
            cancel_expiry_timer(data, token_id)

    entry.async_on_unload(_cancel_expiry_timers)

    async def _sweep_expired_approvals(_now=None) -> None:
        from .approvals import (  # noqa: PLC0415
            dismiss_approval_notification,
            async_expire_overdue_approval_records,
            fire_approval_resolved_event,
        )

        async with store.async_lock:
            expired = await async_expire_overdue_approval_records(
                store,
                skip_ids=data.approvals_in_progress,
            )
        for approval in expired:
            dismiss_approval_notification(hass, approval.id)
            fire_approval_resolved_event(hass, approval)

    async def _reconcile_interrupted_approvals() -> None:
        """Resolve approvals whose executor started but never reported back.

        Runs once, at startup, BEFORE the expiry sweep: an interrupted approval
        is still pending and could otherwise be expired instead, which would
        report a clean timeout for an action that may well have applied.
        """
        from .approvals import (  # noqa: PLC0415
            dismiss_approval_notification,
            async_reconcile_interrupted_approvals,
            fire_approval_resolved_event,
        )

        async with store.async_lock:
            interrupted = await async_reconcile_interrupted_approvals(store)
        for approval in interrupted:
            dismiss_approval_notification(hass, approval.id)
            fire_approval_resolved_event(hass, approval)
            audit.record(
                request_id=approval.request_id,
                token_id="admin",
                token_name=f"admin:{approval.approved_by_user_id or 'unknown'}",
                method="approval/failed",
                resource=f"approval:{approval.tool_name}:{approval.id}",
                outcome="denied",
                client_ip="",
                settings=store.get_settings(),
                payload={
                    "reason": approval.rejected_reason,
                    "approved_at": (
                        approval.approved_at.isoformat()
                        if approval.approved_at else None
                    ),
                    "reconciled_at_startup": True,
                },
            )

    await _reconcile_interrupted_approvals()
    await _sweep_expired_approvals()
    cancel_approval_sweep = async_track_time_interval(
        hass,
        _sweep_expired_approvals,
        APPROVAL_SWEEP_INTERVAL,
    )
    entry.async_on_unload(cancel_approval_sweep)

    async def _on_stop(event: Event) -> None:
        # Both writes are best-effort, for the reason spelled out on
        # async_unload_entry: this runs inside Home Assistant's own stop
        # sequence, so an unwritable store must not raise into it. The first
        # failure must also not skip the second, hence two separate guards.
        try:
            await store.async_flush_last_used()
        except Exception:  # noqa: BLE001 - shutdown must not depend on a writable disk
            _LOGGER.exception("Phoenix MCP: could not flush token usage at shutdown")
        try:
            await audit.async_save()
        except Exception:  # noqa: BLE001 - shutdown must not depend on a writable disk
            _LOGGER.exception("Phoenix MCP: could not persist the audit log at shutdown")

    _listen_once_until_unload(hass, entry, EVENT_HOMEASSISTANT_STOP, _on_stop)

    _audit_template_sandbox()

    @callback
    def _invalidate_entity_tree(_event=None) -> None:
        data.entity_tree_cache_valid = False

    for _registry_event in (
        er_mod.EVENT_ENTITY_REGISTRY_UPDATED,
        dr_mod.EVENT_DEVICE_REGISTRY_UPDATED,
        ar_mod.EVENT_AREA_REGISTRY_UPDATED,
    ):
        entry.async_on_unload(
            hass.bus.async_listen(_registry_event, _invalidate_entity_tree)  # type: ignore[misc]  # loop var erases the event's typed payload
        )

    if data.mesa is not None:
        # Bound once here rather than read as data.mesa inside each closure: the
        # closures run long after this guard, so the attribute read cannot be
        # narrowed at their call sites and the runtime is fixed for this entry's
        # lifetime anyway.
        mesa_runtime = data.mesa
        from .mesa import (
            async_import_sidecar_profiles,
            async_refresh_trigger_issues,
            refresh_orphans,
        )
        from .mesa_suggestions import refresh_suggestions

        async def _mesa_startup() -> None:
            """Import sidecar profiles and prime trigger/orphan caches."""
            try:
                count = await async_import_sidecar_profiles(hass, mesa_runtime)
                if count:
                    _LOGGER.info("Phoenix MCP MESA: imported %d developer domain profile(s)", count)
                await async_refresh_trigger_issues(hass, mesa_runtime)
                refresh_orphans(hass, mesa_runtime)
            except Exception:  # noqa: BLE001 - background priming must not crash setup
                _LOGGER.warning("Phoenix MCP MESA: startup priming failed", exc_info=True)

        mesa_task = hass.async_create_background_task(_mesa_startup(), "phoenix_mcp_mesa_startup")
        def _cancel_mesa_startup() -> None:
            mesa_task.cancel()

        entry.async_on_unload(_cancel_mesa_startup)

        # Suggestions scan entity STATES, which are still loading while Phoenix MCP sets
        # up (a scan now would see a near-empty state machine and cache zero
        # findings). Prime once HA has fully started; on a reload HA is already
        # running with populated states, so prime immediately.
        @callback
        def _prime_suggestions(_event: Event | None = None) -> None:
            try:
                refresh_suggestions(hass, mesa_runtime)
            except Exception:  # noqa: BLE001 - priming must not crash startup
                _LOGGER.warning("Phoenix MCP MESA: suggestion priming failed", exc_info=True)

        if hass.state is CoreState.running:
            _prime_suggestions()
        else:
            _listen_once_until_unload(
                hass, entry, EVENT_HOMEASSISTANT_STARTED, _prime_suggestions,
            )

        async def _on_automation_reloaded(_event=None) -> None:
            # Script/scene edits fire no reload event, so their suggestion
            # staleness is bounded by the next explicit refresh (panel Rescan).
            await async_refresh_trigger_issues(hass, mesa_runtime)
            refresh_orphans(hass, mesa_runtime)
            refresh_suggestions(hass, mesa_runtime)

        # "automation_reloaded" is fired by HA's automation component on reload;
        # listen by string so we do not force-import that component.
        entry.async_on_unload(
            hass.bus.async_listen("automation_reloaded", _on_automation_reloaded)
        )

    # Publish readiness as the final synchronous setup action. Anything that
    # raised before here is caught by async_setup_entry and rolled back.
    data.ready = True
    hass.data[RUNTIME_READY_KEY] = True
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down Phoenix MCP: unload sensor platform, remove panel.

    EVERY step before the platform unload is best-effort, and that is the whole
    shape of this function. `shutting_down` is set first and `helpers` gates every
    token request on it, so anything that raises between here and the unload
    leaves the worst possible state: the entry still loaded, every route still
    registered, and every request answering 503 until the next restart, with a
    logged store error as the only clue. Persisting the last few audit rows and
    removing the panel are both worth attempting and neither is worth that, so a
    failure in either is logged and stepped over.
    """
    data: PhoenixData | None = hass.data.get(DOMAIN)
    if data is not None:
        data.ready = False
        data.shutting_down = True
        try:
            await data.audit.async_save()
        except Exception:  # noqa: BLE001 - teardown must not depend on a writable disk
            _LOGGER.exception("Phoenix MCP: could not persist the audit log during unload")
    hass.data[RUNTIME_READY_KEY] = False

    # The platform unload runs BEFORE the frontend comes down, and that order is
    # load-bearing: HA keeps a FAILED unload loaded, so tearing the panel down
    # first left a still-running integration with no administrative UI, and the
    # only ways back were a later successful reload or a restart. Nothing in the
    # teardown depends on the frontend already being gone, so waiting until the
    # outcome is known costs nothing.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _entry_platforms())

    if unload_ok:
        _remove_frontend(hass)
        hass.data.pop(DOMAIN, None)
    elif data is not None:
        # Still loaded, so it has to stay usable: this flag gates every token
        # request, and the panel and injectors are deliberately left registered.
        data.shutting_down = False
        data.ready = True
        hass.data[RUNTIME_READY_KEY] = True

    return unload_ok


@callback
def _remove_frontend(hass: HomeAssistant) -> None:
    """Take down the panel and both injected modules, one failure at a time.

    Guarded INDIVIDUALLY rather than as a block. They are three unrelated
    removals, and sharing one `try` meant an unexpected failure in the first
    silently skipped the other two, leaving modules injected into every Home
    Assistant page with nothing left to service them.
    """
    from .panel import (  # noqa: PLC0415
        remove_agentchat_inject,
        remove_mesa_inject,
        remove_phoenix_panel,
    )

    for name, remove in (
        ("MESA injector", remove_mesa_inject),
        ("Agent Chat injector", remove_agentchat_inject),
        ("panel", remove_phoenix_panel),
    ):
        try:
            remove(hass)
        except Exception:  # noqa: BLE001 - one stuck removal must not skip the rest
            _LOGGER.exception("Phoenix MCP: could not remove the %s during unload", name)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config entry migration handler. Currently a no-op (single storage version)."""
    return True
