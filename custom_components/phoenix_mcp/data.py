"""Runtime data container for the Phoenix MCP integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .audit import AuditLog
from .card_catalog import CardCatalogStore
from .rate_limiter import RateLimiter
from .token_store import TokenStore
from .version_store import VersionStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .mesa import MesaRuntime
    from .logger_control import LoggerOverrideManager


@dataclass
class PhoenixData:
    """Runtime state stored in hass.data[DOMAIN]. Not persisted across HA restarts.

    All mutable shared state (counters, caches) lives here so it is accessible
    from views, sensors, and __init__ callbacks without globals.
    """

    store: TokenStore
    rate_limiter: RateLimiter
    audit: AuditLog
    # The HomeAssistant instance, set in __init__. Optional so direct constructions
    # (tests) need not supply it; used to fire bus events (e.g. phoenix_mcp_config_changed).
    hass: HomeAssistant | None = None
    # Configuration version history. Always present: __init__ supplies a
    # persistent store; direct constructions (tests) get an in-memory default.
    versions: VersionStore = field(default_factory=VersionStore)
    # Which Lovelace cards this instance can render, harvested by the panel
    # frontend. Always present: __init__ supplies a persistent store; direct
    # constructions (tests) get an in-memory default that reads as never-harvested.
    card_catalog: CardCatalogStore = field(default_factory=CardCatalogStore)
    # MESA semantic-safety runtime (store, resolver, enforcer, validator).
    # None only if MESA setup failed; views guard accordingly.
    mesa: MesaRuntime | None = None
    # Distinguishes an explicit MESA-off configuration from a runtime that
    # failed to initialize. State-changing MESA gates fail closed only for the
    # latter while advisory or enforced mode remains configured.
    mesa_setup_failed: bool = False
    # Process-global manager is also kept under its own hass.data key, so a
    # Phoenix config-entry reload cannot strand a timed logger restoration.
    logger_control: LoggerOverrideManager | None = None
    # Tracks the monotonic time of the last rate-limit notification per token
    # to enforce the one-per-minute throttle on phoenix_mcp_rate_limited bus events.
    rate_limit_notified: dict[str, float] = field(default_factory=dict)
    # settings_version already advised per token, so the stale-tool-list notice
    # fires once per staleness epoch (a fresh tools/list clears the entry). In
    # memory only; after a restart the worst case is one repeated advisory.
    stale_tools_advised: dict[str, str] = field(default_factory=dict)
    # In-memory request/denied/rate-limit counters keyed by token ID.
    token_counters: dict[str, dict[str, int]] = field(default_factory=dict)
    # Approval IDs whose saved action is currently mid-execution. Guards
    # _approve_approval against a concurrent double-approve re-running the same
    # side effect; in memory only (a restart drops it, leaving the approval
    # pending for retry).
    approvals_in_progress: set[str] = field(default_factory=set)
    entity_tree_cache: dict | None = None
    entity_tree_cache_valid: bool = False
    entity_tree_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes a global-settings write with the live registrations it changes.
    # The token-store lock protects persistence only; releasing it before route,
    # Assist, MESA, or injector reconciliation lets an older PATCH finish after a
    # newer one and put runtime state back on the obsolete generation.
    settings_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Keyed by token name slug; values are the list of PhoenixTokenSensor instances.
    platform_entities: dict[str, list] = field(default_factory=dict)
    # Keyed by token ID for fast sensor lookup during counter updates.
    token_id_sensors: dict[str, list] = field(default_factory=dict)
    async_add_entities_cb: Callable | None = None
    # Per-token expiry timers. Values are cancel callbacks from hass.async_call_later.
    expiry_timers: dict[str, Callable] = field(default_factory=dict)
    # Callbacks wired by __init__.py to decouple sensor lifecycle from views.
    async_on_token_created: Callable | None = None
    async_on_token_archived: Callable | None = None
    # Set to True once client routes have been registered; prevents duplicate registration.
    routes_registered: bool = False
    # Called by the admin settings PATCH when the kill switch is deactivated.
    async_register_routes: Callable | None = None
    # Unregister callback for the Assist bridge llm.API (assist_api.py), set when
    # the API is registered. None when unregistered (kill switch on, unsupported HA).
    assist_api_unregister: Callable | None = None
    # Re-syncs the Phoenix MCP voice conversation agent (voice_agent.py) to current settings
    # + kill switch. Set in __init__ (captures hass+entry); called by the admin
    # settings PATCH when voice_agent_* or the kill switch changes.
    async_sync_voice_agent: Callable | None = None
    # Adds/removes the Phoenix MCP AI Task entity (ai_task.py) to match current settings, so
    # it exists only while fully configured. Set by the ai_task platform's setup
    # (captures hass+entry+async_add_entities); called by the admin settings PATCH,
    # revoke/expiry, provider delete, and wipe when ai_task_* or a bound target changes.
    async_sync_ai_task: Callable | None = None
    # The live Phoenix MCP AI Task entity while it exists (None otherwise). Tracked so the
    # sync can remove it; the source of truth for "does the entity currently exist".
    ai_task_entity: object | None = None
    # Called by the admin settings PATCH when audit_flush_interval changes, so
    # the periodic flush timer picks up the new interval immediately.
    reschedule_audit_flush: Callable | None = None
    # Debounce state for per-request sensor writes (helpers.update_token_counter):
    # token IDs with unwritten counter changes, and the pending flush cancel.
    sensor_write_dirty: set[str] = field(default_factory=set)
    sensor_flush_cancel: Callable | None = None
    # Set to True by async_unload_entry. Views check this before accessing store/audit
    # to avoid KeyError 500s after unload (HA does not expose a view unregister API).
    shutting_down: bool = False
    # Direct constructions are ready by default for isolated tests. Real setup
    # passes False and publishes readiness only after every mandatory step has
    # completed. Permanent aiohttp routes refuse while this is false.
    ready: bool = True
    # Legacy Streamable HTTP sessions, keyed by the cryptographically random
    # Mcp-Session-Id returned from initialize. Direct unit constructions leave
    # lifecycle enforcement off; production setup enables it explicitly.
    mcp_sessions: dict[str, dict] = field(default_factory=dict)
    enforce_mcp_lifecycle: bool = False
