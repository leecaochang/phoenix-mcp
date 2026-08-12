"""Token storage CRUD for Phoenix MCP. No business logic lives here."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime, utcnow

from .const import (
    AGENTCLI_MAX_ITERATIONS,
    AGENTCLI_MAX_ITERATIONS_MAX,
    AGENTCLI_MAX_ITERATIONS_MIN,
    AGENTCLI_SCROLLBACK_DEFAULT,
    AGENTCLI_SCROLLBACK_MAX,
    AGENTCLI_SCROLLBACK_MIN,
    CAP_ALLOW,
    CAP_DENY,
    CAP_MODES,
    CAPABILITY_NAMES,
    CONFIRM_AVAILABLE_CAPS,
    DEFAULT_PRESET_NAME,
    DEFAULT_CONFIRM_INLINE_WAIT_SECONDS,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_REQUESTS,
    MAX_CONFIRM_INLINE_WAIT_SECONDS,
    MIN_CONFIRM_INLINE_WAIT_SECONDS,
    MAX_PRESETS_PER_TOKEN,
    MESA_MODE_ADVISORY,
    MESA_MODES,
    PERSONA_CUSTOM,
    PERSONA_NAMES,
    STORAGE_KEY,
    STORAGE_VERSION,
    TOKEN_HEX_LENGTH,
    TOKEN_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

VALID_NODE_STATES = frozenset({"GREY", "YELLOW", "GREEN", "RED"})


def _require_dt(value: str | None, field: str) -> datetime:
    """Parse a REQUIRED stored datetime, raising if it does not parse.

    created_at (and an archived record's revoked_at) are non-optional, but
    _parse_dt returns None for anything unparseable, which would build a record
    whose non-optional datetime is None and crash much later at an arbitrary
    comparison or .isoformat(). Raising here routes a corrupt value into the
    existing "skip the record, warn, never abort setup" handler at the load site,
    which is where every other corruption is already handled.
    """
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError(f"unparseable {field}: {value!r}")
    return parsed


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        # Phoenix MCP always writes UTC-aware values; a naive one can only come from
        # hand-edited storage or a record persisted before input normalization.
        # Assume UTC so expiry comparisons never mix naive and aware datetimes
        # (that TypeError would abort integration setup).
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clamp_inline_wait(value: Any) -> int:
    """Coerce confirm_inline_wait_seconds to 0 (off) or an int in [MIN, MAX].

    A non-numeric or non-positive value means off (0, the unattended-agent mode);
    any positive value enables the inline wait and is clamped into
    [MIN_CONFIRM_INLINE_WAIT_SECONDS, MAX_CONFIRM_INLINE_WAIT_SECONDS], so a stored
    or patched value can never sit below the useful floor or above the ceiling.
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return max(MIN_CONFIRM_INLINE_WAIT_SECONDS, min(seconds, MAX_CONFIRM_INLINE_WAIT_SECONDS))


def _load_rate(value: Any, default: int) -> int:
    """Coerce a stored rate-limit field to an int in [0, 100000].

    Storage can be hand-edited; a negative or non-numeric limit must never
    reach the rate limiter (a negative window size fails every request with an
    IndexError). Out-of-range values fall back to the default rather than
    clamping, because clamping a negative to 0 would silently disable rate
    limiting. The bounds match the admin API's create/patch validation.
    """
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if 0 <= parsed <= 100_000:
        return parsed
    return default


def _load_bool(value: object) -> bool:
    """Load a stored access-widening flag fail-closed: only real True is True.

    A corrupt or hand-edited record could store a truthy non-bool (e.g. the
    string "false", which is truthy under bool()); for a flag that widens
    access (pass_through, use_assist_exposure) that must never silently enable
    it. Anything that is not literally True loads as False.
    """
    return value is True


@dataclass
class PermissionNode:
    """Permission state for one node in the hierarchy (domain, device, or entity).

    state is one of GREY (inherit), YELLOW (read), GREEN (write), RED (deny).
    hint is an optional human-readable label used in the context endpoint.
    """

    state: str = "GREY"
    hint: str | None = None

    def to_dict(self) -> dict:
        return {"state": self.state, "hint": self.hint}

    @classmethod
    def from_dict(cls, data: dict) -> PermissionNode:
        state = data.get("state", "GREY")
        if state not in VALID_NODE_STATES:
            raise ValueError(f"Invalid permission node state: {state!r}")
        return cls(state=state, hint=data.get("hint"))


@dataclass
class PermissionTree:
    """Three-level permission hierarchy: domains, devices, and entities.

    Each level is a dict keyed by the relevant ID. Nodes with state GREY are
    omitted from storage; their absence implies inheritance.
    """

    domains: dict[str, PermissionNode] = field(default_factory=dict)
    devices: dict[str, PermissionNode] = field(default_factory=dict)
    entities: dict[str, PermissionNode] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "domains": {k: v.to_dict() for k, v in self.domains.items()},
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
            "entities": {k: v.to_dict() for k, v in self.entities.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> PermissionTree:
        return cls(
            domains={k: PermissionNode.from_dict(v) for k, v in data.get("domains", {}).items()},
            devices={k: PermissionNode.from_dict(v) for k, v in data.get("devices", {}).items()},
            entities={k: PermissionNode.from_dict(v) for k, v in data.get("entities", {}).items()},
        )


@dataclass
class TokenPreset:
    """A named snapshot of a token's full settings (the workspace model).

    Presets hold caps, persona, permission tree, rate limits, pass_through,
    use_assist_exposure, announce_all_tools, and confirm_inline_wait_seconds.
    The persona is a display snapshot only; applying a preset re-derives it
    from the applied caps.
    """

    id: str
    name: str
    created_at: datetime
    caps: dict[str, str]
    persona: str
    permissions: PermissionTree
    pass_through: bool
    use_assist_exposure: bool
    announce_all_tools: bool
    confirm_inline_wait_seconds: int
    rate_limit_requests: int
    rate_limit_burst: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "caps": dict(self.caps),
            "persona": self.persona,
            "permissions": self.permissions.to_dict(),
            "pass_through": self.pass_through,
            "use_assist_exposure": self.use_assist_exposure,
            "announce_all_tools": self.announce_all_tools,
            "confirm_inline_wait_seconds": self.confirm_inline_wait_seconds,
            "rate_limit_requests": self.rate_limit_requests,
            "rate_limit_burst": self.rate_limit_burst,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenPreset:
        caps: dict[str, str] = {}
        raw_caps = data.get("caps") or {}
        for cap_name in CAPABILITY_NAMES:
            value = raw_caps.get(cap_name)
            caps[cap_name] = value if value in CAP_MODES else CAP_DENY
        persona = data.get("persona", PERSONA_CUSTOM)
        if persona not in PERSONA_NAMES:
            persona = PERSONA_CUSTOM
        return cls(
            id=data["id"],
            name=data["name"],
            created_at=_parse_dt(data.get("created_at")) or utcnow(),
            caps=caps,
            persona=persona,
            permissions=PermissionTree.from_dict(data.get("permissions", {})),
            pass_through=_load_bool(data.get("pass_through")),
            use_assist_exposure=_load_bool(data.get("use_assist_exposure")),
            announce_all_tools=_load_bool(data.get("announce_all_tools")),
            confirm_inline_wait_seconds=_clamp_inline_wait(data.get("confirm_inline_wait_seconds", DEFAULT_CONFIRM_INLINE_WAIT_SECONDS)),
            rate_limit_requests=_load_rate(data.get("rate_limit_requests", DEFAULT_RATE_LIMIT_REQUESTS), DEFAULT_RATE_LIMIT_REQUESTS),
            rate_limit_burst=_load_rate(data.get("rate_limit_burst", DEFAULT_RATE_LIMIT_BURST), DEFAULT_RATE_LIMIT_BURST),
        )


def snapshot_preset(token: TokenRecord, name: str, preset_id: str | None = None) -> TokenPreset:
    """Build a preset from a token's CURRENT live settings (deep-copied tree)."""
    return TokenPreset(
        id=preset_id or str(uuid.uuid4()),
        name=name,
        created_at=utcnow(),
        caps=token.caps_dict(),
        persona=token.persona,
        permissions=PermissionTree.from_dict(token.permissions.to_dict()),
        pass_through=token.pass_through,
        use_assist_exposure=token.use_assist_exposure,
        announce_all_tools=token.announce_all_tools,
        confirm_inline_wait_seconds=token.confirm_inline_wait_seconds,
        rate_limit_requests=token.rate_limit_requests,
        rate_limit_burst=token.rate_limit_burst,
    )


@dataclass
class TokenRecord:
    """Active Phoenix MCP token with its configuration and permission tree.

    Capability flags are tri-state strings ("deny" / "allow" / "confirm").
    "confirm" routes the request through the admin-approval gate; only caps
    in CONFIRM_AVAILABLE_CAPS may be set to "confirm".
    """

    id: str
    name: str
    token_hash: str
    created_at: datetime
    created_by: str
    expires_at: datetime | None = None
    revoked: bool = False
    last_used_at: datetime | None = None
    updated_at: datetime | None = None
    pass_through: bool = False
    use_assist_exposure: bool = False
    # When False (default), the MCP tools/list is gated to the tools this token
    # can actually use (its caps and write scope). True announces the full tool
    # surface (diagnostic / opt-out). Not a capability: a presentation toggle.
    announce_all_tools: bool = False
    # Inline wait (seconds) on a confirm gate: a confirm-gated call holds the
    # response open up to this many seconds for an admin decision before falling
    # back to the immediate pending_approval reply. 0 means off (return
    # immediately, the unattended-agent mode, API-settable only); a positive
    # value is clamped to [MIN, MAX]_CONFIRM_INLINE_WAIT_SECONDS. The product
    # default is ON: async_create_token seeds new tokens with
    # DEFAULT_CONFIRM_INLINE_WAIT_SECONDS and from_dict defaults a legacy record
    # missing the field to it. This raw dataclass default stays 0 so a directly
    # constructed record (internal/tests) does not silently inherit the wait.
    confirm_inline_wait_seconds: int = 0
    rate_limit_requests: int = DEFAULT_RATE_LIMIT_REQUESTS
    rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST
    cap_automation_write: str = CAP_DENY
    cap_script_write: str = CAP_DENY
    # Authoring blueprints is separable from authoring automations: one blueprint
    # edit reloads every automation using it. No v1 allow_* equivalent, so a
    # migrated token defaults to deny.
    cap_blueprint_write: str = CAP_DENY
    cap_config_read: str = CAP_DENY
    cap_template_render: str = CAP_DENY
    cap_restart: str = CAP_DENY
    cap_physical_control: str = CAP_DENY
    cap_service_response: str = CAP_DENY
    cap_broadcast: str = CAP_DENY
    cap_log_read: str = CAP_DENY
    # Process-wide logger changes are deliberately separate from diagnostics.
    # No legacy allow_* equivalent: migrated and newly created tokens fail closed.
    cap_log_control: str = CAP_DENY
    cap_search: str = CAP_DENY
    # Camera images are privacy-sensitive and require an explicit grant even
    # for pass-through tokens. Migrated records default to deny via CAPABILITY_NAMES.
    cap_camera_read: str = CAP_DENY
    cap_registry_read: str = CAP_DENY
    cap_traces: str = CAP_DENY
    cap_diagnostics: str = CAP_DENY
    cap_scene_write: str = CAP_DENY
    cap_helper_write: str = CAP_DENY
    cap_integration_write: str = CAP_DENY
    cap_lovelace_write: str = CAP_DENY
    cap_registry_write: str = CAP_DENY
    cap_radio_write: str = CAP_DENY
    cap_energy_write: str = CAP_DENY
    cap_backup: str = CAP_DENY
    cap_filesystem: str = CAP_DENY
    cap_yaml_edit: str = CAP_DENY
    # ESPHome device YAML under config/esphome, in its own jail. Deliberately
    # DISJOINT from cap_filesystem (whose allowed dirs are www/themes/
    # custom_templates): device YAML embeds C++ lambdas and, in practice, inline
    # credentials, so it must not become reachable by widening a filesystem grant.
    # No v1 allow_* equivalent, so a migrated token defaults to deny.
    cap_esphome_yaml: str = CAP_DENY
    cap_esphome_flash: str = CAP_DENY
    persona: str = PERSONA_CUSTOM
    permissions: PermissionTree = field(default_factory=PermissionTree)
    # Settings presets (the workspace model): active_preset_id is sticky.
    # Manual edits never clear it; they are the active preset's unsaved state,
    # absorbed back into it when the admin switches presets.
    presets: list[TokenPreset] = field(default_factory=list)
    active_preset_id: str | None = None
    # Staleness tracking for the in-band "tool list changed" advisory (storage
    # only, not in to_dict): settings_version bumps on any settings/caps/
    # permissions change; tools_list_version echoes it at the token's last
    # tools/list call. Stale iff settings_version > tools_list_version.
    settings_version: int = 1
    tools_list_version: int = 1
    # The tool-catalog fingerprint this token last saw at tools/list. The second
    # staleness axis: settings_version catches the OPERATOR changing the token,
    # this catches the CODE changing (a deploy) under a client that already
    # fetched its schemas. Empty means never recorded, which is deliberately NOT
    # treated as stale: token_store cannot import mcp_view to learn the current
    # fingerprint (the dependency runs the other way), so an upgraded token has
    # no baseline and guessing one would advise every token once for nothing.
    # The first tools/list after the upgrade records it and deploys are caught
    # from then on.
    tools_catalog_fingerprint: str = ""

    def caps_dict(self) -> dict[str, str]:
        """Return the cap_*->mode mapping for this token."""
        return {name: getattr(self, name) for name in CAPABILITY_NAMES}

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "pass_through": self.pass_through,
            **({"use_assist_exposure": self.use_assist_exposure} if self.pass_through else {}),
            "announce_all_tools": self.announce_all_tools,
            "confirm_inline_wait_seconds": self.confirm_inline_wait_seconds,
            "rate_limit_requests": self.rate_limit_requests,
            "rate_limit_burst": self.rate_limit_burst,
            "persona": self.persona,
            "permissions": self.permissions.to_dict(),
            "presets": [p.to_dict() for p in self.presets],
            "active_preset_id": self.active_preset_id,
        }
        d.update(self.caps_dict())
        return d

    def to_storage_dict(self) -> dict:
        d = self.to_dict()
        d["token_hash"] = self.token_hash
        # use_assist_exposure is only meaningful for pass_through tokens.
        # to_dict() already conditionally includes it; storage mirrors that behavior
        # so scoped tokens do not accumulate a stale field over their lifetime.
        if self.pass_through:
            d["use_assist_exposure"] = self.use_assist_exposure
        # Staleness counters are internal bookkeeping, kept out of the admin JSON
        # (and therefore out of the frontend contract fixture).
        d["settings_version"] = self.settings_version
        d["tools_list_version"] = self.tools_list_version
        d["tools_catalog_fingerprint"] = self.tools_catalog_fingerprint
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TokenRecord:
        cap_kwargs: dict[str, str] = {}
        for cap_name in CAPABILITY_NAMES:
            value = data.get(cap_name)
            if value not in CAP_MODES:
                value = CAP_DENY
            cap_kwargs[cap_name] = value
        persona = data.get("persona", PERSONA_CUSTOM)
        if persona not in PERSONA_NAMES:
            persona = PERSONA_CUSTOM
        # Presets parse leniently: a corrupt preset is dropped with a warning,
        # never at the cost of the whole token record.
        presets: list[TokenPreset] = []
        for p in data.get("presets") or []:
            try:
                presets.append(TokenPreset.from_dict(p))
            except Exception as exc:  # noqa: BLE001 - any unparseable preset is dropped, never the token
                _LOGGER.warning(
                    "Dropping corrupt preset %r on token %r: %s",
                    p.get("id", "?") if isinstance(p, dict) else "?",
                    data.get("id", "?"), exc,
                )
        active_preset_id = data.get("active_preset_id")
        if active_preset_id is not None and not any(p.id == active_preset_id for p in presets):
            active_preset_id = None
        settings_version = int(data.get("settings_version", 1))
        permissions_raw = data.get("permissions")
        if not isinstance(permissions_raw, dict):
            # Fail closed: a corrupt (non-object) permission tree loads as an
            # empty tree, which grants nothing.
            permissions_raw = {}
        return cls(
            id=data["id"],
            name=data["name"],
            token_hash=data["token_hash"],
            created_at=_require_dt(data["created_at"], "created_at"),
            created_by=data["created_by"],
            expires_at=_parse_dt(data.get("expires_at")),
            revoked=data.get("revoked", False),
            last_used_at=_parse_dt(data.get("last_used_at")),
            pass_through=_load_bool(data.get("pass_through")),
            use_assist_exposure=_load_bool(data.get("use_assist_exposure")),
            announce_all_tools=_load_bool(data.get("announce_all_tools")),
            confirm_inline_wait_seconds=_clamp_inline_wait(data.get("confirm_inline_wait_seconds", DEFAULT_CONFIRM_INLINE_WAIT_SECONDS)),
            rate_limit_requests=_load_rate(data.get("rate_limit_requests", DEFAULT_RATE_LIMIT_REQUESTS), DEFAULT_RATE_LIMIT_REQUESTS),
            rate_limit_burst=_load_rate(data.get("rate_limit_burst", DEFAULT_RATE_LIMIT_BURST), DEFAULT_RATE_LIMIT_BURST),
            persona=persona,
            updated_at=_parse_dt(data.get("updated_at")),
            permissions=PermissionTree.from_dict(permissions_raw),
            presets=presets,
            active_preset_id=active_preset_id,
            settings_version=settings_version,
            # Records written before this field existed default to "not stale",
            # so an upgrade never fires a spurious advisory.
            tools_list_version=int(data.get("tools_list_version", settings_version)),
            tools_catalog_fingerprint=str(data.get("tools_catalog_fingerprint", "") or ""),
            **cap_kwargs,
        )

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return utcnow() >= self.expires_at

    def is_valid(self) -> bool:
        return not self.revoked and not self.is_expired()


@dataclass
class ArchivedTokenRecord:
    """Archived snapshot of a revoked or expired token. Retained for audit purposes.

    Only audit-trail fields are retained. pass_through, capability flags,
    rate limit parameters, and permission tree are NOT stored in archived records.
    """

    id: str
    name: str
    token_hash: str
    created_at: datetime
    created_by: str
    revoked_at: datetime
    revoked: bool = False
    expires_at: datetime | None = None
    last_used_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked": self.revoked,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }

    def to_storage_dict(self) -> dict:
        d = self.to_dict()
        d["token_hash"] = self.token_hash
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ArchivedTokenRecord:
        return cls(
            id=data["id"],
            name=data["name"],
            token_hash=data["token_hash"],
            created_at=_require_dt(data["created_at"], "created_at"),
            created_by=data["created_by"],
            revoked_at=_require_dt(data["revoked_at"], "revoked_at"),
            revoked=data.get("revoked", False),
            expires_at=_parse_dt(data.get("expires_at")),
            last_used_at=_parse_dt(data.get("last_used_at")),
            # pass_through and other privilege fields are intentionally not loaded even
            # if present in older storage records; archives never carry them.
        )


def _clamp_int(value: Any, valid: set[int], default: int) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return default
    return converted if converted in valid else default


def _clamp_range(value: Any, lo: int, hi: int, default: int) -> int:
    """Coerce value to an int clamped to [lo, hi]; default on non-numeric input."""
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, converted))


def _coerce_opt_str(value: object) -> str | None:
    """Return a non-empty string or None (for optional string settings)."""
    return value if isinstance(value, str) and value else None


@dataclass
class GlobalSettings:
    """Integration-wide settings persisted to storage."""

    kill_switch: bool = False
    disable_all_logging: bool = False
    log_allowed: bool = True
    log_denied: bool = True
    log_rate_limited: bool = True
    log_entity_names: bool = True
    log_client_ip: bool = True
    notify_on_rate_limit: bool = False
    notify_on_approval: bool = True
    audit_flush_interval: int = 15
    audit_log_maxlen: int = 10000
    mesa_mode: str = MESA_MODE_ADVISORY
    # Experimental, admin-only convenience: inject a (+)/MESA-pill control into HA's
    # native config list pages so profiles can be created in context. Default off
    # because it DOM-patches the HA frontend (fragile across HA updates).
    mesa_inject_enabled: bool = False
    # Token settings presets: default off. Enabling seeds every token
    # with a "Default preset" from its current state.
    token_presets_enabled: bool = False
    # agentCLI transcript scrollback / conversation-memory bound (lines).
    # 0 = keep nothing once it scrolls off; capped at AGENTCLI_SCROLLBACK_MAX.
    agentcli_scrollback_lines: int = AGENTCLI_SCROLLBACK_DEFAULT
    # agentCLI per-turn tool-round cap. In the chat this is the "continue?"
    # checkpoint cadence (not a hard stop); clamped to [MIN, MAX].
    agentcli_max_iterations: int = AGENTCLI_MAX_ITERATIONS
    # Agent Chat window visibility: True (default) = float over the whole HA UI
    # via the global inject module (still opened only from the Phoenix MCP panel button,
    # falling back to panel-only if injection is unsupported); False = Phoenix MCP panel only.
    agentcli_global: bool = True
    # Assist bridge: the id of the token whose scoped tool surface HA's native
    # Assist/voice pipeline resolves against (assist_api.py). None = unbound (the
    # registered llm.API offers zero tools). Cleared on revoke/expiry of that token.
    assist_bound_token_id: str | None = None
    # Phoenix MCP voice agent (voice_agent.py): Phoenix MCP registered as HA's own conversation
    # agent, running its own model loop. Independent of the llm.API bridge above.
    # When enabled it needs a token (scope), an Agent Chat provider account, and a model.
    voice_agent_enabled: bool = False
    voice_agent_token_id: str | None = None
    voice_agent_provider_id: str | None = None
    voice_agent_model: str | None = None
    # Id of the Assist pipeline Phoenix MCP created for the voice agent via the one-click
    # setup (voice_agent.async_create_assist_pipeline). None when the operator set
    # up Assist manually or has not run the helper. Tracked so Phoenix MCP can remove that
    # pipeline again on teardown (disable / revoke / provider delete / wipe).
    voice_agent_pipeline_id: str | None = None
    # Phoenix MCP AI Task entity (ai_task.py): Phoenix MCP registered as an HA AITaskEntity running
    # its own model loop for ai_task.generate_data. Independent of the voice agent
    # and the bridge; when enabled it needs a token (scope), an Agent Chat provider
    # account, and a model.
    ai_task_enabled: bool = False
    ai_task_token_id: str | None = None
    ai_task_provider_id: str | None = None
    ai_task_model: str | None = None

    def to_dict(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "disable_all_logging": self.disable_all_logging,
            "log_allowed": self.log_allowed,
            "log_denied": self.log_denied,
            "log_rate_limited": self.log_rate_limited,
            "log_entity_names": self.log_entity_names,
            "log_client_ip": self.log_client_ip,
            "notify_on_rate_limit": self.notify_on_rate_limit,
            "notify_on_approval": self.notify_on_approval,
            "audit_flush_interval": self.audit_flush_interval,
            "audit_log_maxlen": self.audit_log_maxlen,
            "mesa_mode": self.mesa_mode,
            "mesa_inject_enabled": self.mesa_inject_enabled,
            "token_presets_enabled": self.token_presets_enabled,
            "agentcli_scrollback_lines": self.agentcli_scrollback_lines,
            "agentcli_max_iterations": self.agentcli_max_iterations,
            "agentcli_global": self.agentcli_global,
            "assist_bound_token_id": self.assist_bound_token_id,
            "voice_agent_enabled": self.voice_agent_enabled,
            "voice_agent_token_id": self.voice_agent_token_id,
            "voice_agent_provider_id": self.voice_agent_provider_id,
            "voice_agent_model": self.voice_agent_model,
            "voice_agent_pipeline_id": self.voice_agent_pipeline_id,
            "ai_task_enabled": self.ai_task_enabled,
            "ai_task_token_id": self.ai_task_token_id,
            "ai_task_provider_id": self.ai_task_provider_id,
            "ai_task_model": self.ai_task_model,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GlobalSettings:
        mesa_mode = data.get("mesa_mode", MESA_MODE_ADVISORY)
        return cls(
            kill_switch=bool(data.get("kill_switch", False)),
            disable_all_logging=bool(data.get("disable_all_logging", False)),
            log_allowed=bool(data.get("log_allowed", True)),
            log_denied=bool(data.get("log_denied", True)),
            log_rate_limited=bool(data.get("log_rate_limited", True)),
            log_entity_names=bool(data.get("log_entity_names", True)),
            log_client_ip=bool(data.get("log_client_ip", True)),
            notify_on_rate_limit=bool(data.get("notify_on_rate_limit", False)),
            notify_on_approval=bool(data.get("notify_on_approval", True)),
            audit_flush_interval=_clamp_int(data.get("audit_flush_interval"), {0, 5, 10, 15, 30, 60}, 15),
            audit_log_maxlen=_clamp_int(data.get("audit_log_maxlen"), {100, 1000, 5000, 10000}, 10000),
            mesa_mode=mesa_mode if mesa_mode in MESA_MODES else MESA_MODE_ADVISORY,
            mesa_inject_enabled=bool(data.get("mesa_inject_enabled", False)),
            token_presets_enabled=bool(data.get("token_presets_enabled", False)),
            agentcli_scrollback_lines=_clamp_range(
                data.get("agentcli_scrollback_lines", AGENTCLI_SCROLLBACK_DEFAULT),
                AGENTCLI_SCROLLBACK_MIN, AGENTCLI_SCROLLBACK_MAX, AGENTCLI_SCROLLBACK_DEFAULT,
            ),
            agentcli_max_iterations=_clamp_range(
                data.get("agentcli_max_iterations", AGENTCLI_MAX_ITERATIONS),
                AGENTCLI_MAX_ITERATIONS_MIN, AGENTCLI_MAX_ITERATIONS_MAX, AGENTCLI_MAX_ITERATIONS,
            ),
            agentcli_global=bool(data.get("agentcli_global", True)),
            assist_bound_token_id=_coerce_opt_str(data.get("assist_bound_token_id")),
            voice_agent_enabled=bool(data.get("voice_agent_enabled", False)),
            voice_agent_token_id=_coerce_opt_str(data.get("voice_agent_token_id")),
            voice_agent_provider_id=_coerce_opt_str(data.get("voice_agent_provider_id")),
            voice_agent_model=_coerce_opt_str(data.get("voice_agent_model")),
            voice_agent_pipeline_id=_coerce_opt_str(data.get("voice_agent_pipeline_id")),
            ai_task_enabled=bool(data.get("ai_task_enabled", False)),
            ai_task_token_id=_coerce_opt_str(data.get("ai_task_token_id")),
            ai_task_provider_id=_coerce_opt_str(data.get("ai_task_provider_id")),
            ai_task_model=_coerce_opt_str(data.get("ai_task_model")),
        )


class TokenStore:
    """Manages persistent storage and in-memory state for Phoenix MCP tokens."""

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self._hass = hass
        self._store = store
        self._tokens: dict[str, TokenRecord] = {}
        self._archived: dict[str, ArchivedTokenRecord] = {}
        self._pending_approvals: list[dict] = []
        self._settings: GlobalSettings = GlobalSettings()
        # Global per-entity hints (entity_id -> hint text), surfaced to every token
        # in the context endpoint. Distinct from per-token permission-node hints.
        self._entity_hints: dict[str, str] = {}
        self.async_lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def async_create(cls, hass: HomeAssistant) -> TokenStore:
        """Create a TokenStore and load persisted data from HA storage."""
        store = _PhoenixStore(hass, STORAGE_VERSION, STORAGE_KEY)
        instance = cls(hass, store)
        await instance.async_load()
        return instance

    async def async_load(self) -> None:
        """Load token and settings data from the HA storage file.

        Applies the v1 -> v2 migration in place when older storage is detected.
        """
        raw = await self._store.async_load()
        raw, normalized = _normalize_storage_root(raw)
        migrated = _migrate_storage_v1_to_v2(raw)

        tokens: dict[str, TokenRecord] = {}
        for r in raw.get("tokens", []):
            try:
                record = TokenRecord.from_dict(r)
                tokens[record.id] = record
            except Exception as exc:  # noqa: BLE001 - any unparseable record is skipped; one bad record must never abort setup
                _LOGGER.warning(
                    "Skipping corrupt token record %r: %s",
                    r.get("id", "?") if isinstance(r, dict) else "?", exc,
                )
        self._tokens = tokens

        archived: dict[str, ArchivedTokenRecord] = {}
        for r in raw.get("archived_tokens", []):
            try:
                archived_record = ArchivedTokenRecord.from_dict(r)
                archived[archived_record.id] = archived_record
            except Exception as exc:  # noqa: BLE001 - any unparseable record is skipped; one bad record must never abort setup
                _LOGGER.warning(
                    "Skipping corrupt archived token record %r: %s",
                    r.get("id", "?") if isinstance(r, dict) else "?", exc,
                )
        self._archived = archived

        self._settings = GlobalSettings.from_dict(raw.get("settings", {}))
        # The collection is normalized to a list above, but its MEMBERS are not:
        # a non-dict entry (string/int) would crash the startup expiry sweep,
        # which calls entry.get(...) on every member. Discard non-dict members.
        raw_approvals = raw.get("pending_approvals", [])
        self._pending_approvals = [a for a in raw_approvals if isinstance(a, dict)]
        approvals_cleaned = len(self._pending_approvals) != len(raw_approvals)
        self._entity_hints = {
            str(k): str(v) for k, v in (raw.get("entity_hints") or {}).items()
        }

        if migrated:
            _LOGGER.info("Phoenix MCP storage migrated from v1 to v2; saving canonical form")
        if migrated or normalized or approvals_cleaned:
            # Persist the cleaned form so a one-time corruption does not have to
            # be re-normalized on every load.
            await self.async_save()

    async def async_save(self) -> None:
        """Persist the current in-memory state to HA storage."""
        await self._store.async_save({
            "version": STORAGE_VERSION,
            "tokens": [t.to_storage_dict() for t in self._tokens.values()],
            "archived_tokens": [a.to_storage_dict() for a in self._archived.values()],
            "pending_approvals": self._pending_approvals,
            "settings": self._settings.to_dict(),
            "entity_hints": self._entity_hints,
        })

    def get_pending_approvals(self) -> list[dict]:
        """Return the pending-approvals array. Mutations require async_lock."""
        return self._pending_approvals

    def set_pending_approvals(self, approvals: list[dict]) -> None:
        """Replace the pending-approvals array in memory. Caller must save."""
        self._pending_approvals = approvals

    async def async_create_token(
        self,
        name: str,
        created_by: str,
        expires_at: datetime | None = None,
        pass_through: bool = False,
        use_assist_exposure: bool = False,
        rate_limit_requests: int = DEFAULT_RATE_LIMIT_REQUESTS,
        rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST,
    ) -> tuple[TokenRecord, str]:
        """Generate a new token, store it, and return (record, raw_token).

        The raw_token value is returned exactly once and never stored. Callers
        must pass it to the client immediately and discard it.
        """
        raw_token = TOKEN_PREFIX + secrets.token_hex(TOKEN_HEX_LENGTH // 2)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = utcnow()
        record = TokenRecord(
            id=str(uuid.uuid4()),
            name=name,
            token_hash=token_hash,
            created_at=now,
            created_by=created_by,
            expires_at=expires_at,
            pass_through=pass_through,
            use_assist_exposure=use_assist_exposure if pass_through else False,
            confirm_inline_wait_seconds=DEFAULT_CONFIRM_INLINE_WAIT_SECONDS,
            rate_limit_requests=rate_limit_requests,
            rate_limit_burst=rate_limit_burst if rate_limit_requests > 0 else 0,
            updated_at=now,
        )
        # While presets are enabled, every token carries an active preset from
        # birth (workspace model), same as the seed that ran at enable time.
        if self._settings.token_presets_enabled:
            preset = snapshot_preset(record, DEFAULT_PRESET_NAME)
            record.presets.append(preset)
            record.active_preset_id = preset.id
        self._tokens[record.id] = record
        await self.async_save()
        return record, raw_token

    def get_token_by_id(self, token_id: str) -> TokenRecord | None:
        return self._tokens.get(token_id)

    def get_token_by_hash(self, token_hash: str) -> TokenRecord | None:
        """Find a token by constant-time comparison of SHA-256 hashes."""
        for token in self._tokens.values():
            if hmac_compare(token.token_hash, token_hash):
                return token
        return None

    def get_archived_by_hash(self, token_hash: str) -> ArchivedTokenRecord | None:
        for record in self._archived.values():
            if hmac_compare(record.token_hash, token_hash):
                return record
        return None

    def list_tokens(self) -> list[TokenRecord]:
        return list(self._tokens.values())

    def list_archived(self) -> list[ArchivedTokenRecord]:
        return list(self._archived.values())

    def active_token_count(self) -> int:
        return len(self._tokens)

    def name_slug_exists(self, name: str, exclude_token_id: str | None = None) -> bool:
        """Return True if a token with an equivalent slug already exists.

        exclude_token_id skips that token, so a rename can keep (or re-case) its
        own name without colliding with itself.
        """
        slug = token_name_slug(name)
        return any(
            token_name_slug(t.name) == slug
            for tid, t in self._tokens.items()
            if tid != exclude_token_id
        )

    async def async_archive_token(
        self,
        token_id: str,
        revoked: bool,
        revoked_at: datetime | None = None,
    ) -> ArchivedTokenRecord | None:
        """Move a token from the active list to the archive and persist.

        Returns None if the token_id is not found.
        """
        token = self._tokens.pop(token_id, None)
        if token is None:
            return None
        archived = ArchivedTokenRecord(
            id=token.id,
            name=token.name,
            token_hash=token.token_hash,
            created_at=token.created_at,
            created_by=token.created_by,
            revoked_at=revoked_at or utcnow(),
            revoked=revoked,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
        )
        self._archived[archived.id] = archived
        await self.async_save()
        return archived

    async def async_patch_token(
        self,
        token_id: str,
        **kwargs,
    ) -> TokenRecord | None:
        """Update mutable capability and rate-limit fields on a token and persist.

        Callers must hold self.async_lock before calling to prevent concurrent writes.
        When rate_limit_requests is set to 0, rate_limit_burst is forced to 0.
        Returns None if the token is not found.
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        mutable_fields = {
            "name",
            "pass_through",
            "use_assist_exposure",
            "announce_all_tools",
            "confirm_inline_wait_seconds",
            "rate_limit_requests",
            "rate_limit_burst",
            "persona",
        }
        cap_fields = set(CAPABILITY_NAMES)
        cap_changed = False
        for key, value in kwargs.items():
            if key in cap_fields:
                if value not in CAP_MODES:
                    raise ValueError(
                        f"Invalid capability mode for {key!r}: {value!r}"
                    )
                if value == "confirm" and key not in CONFIRM_AVAILABLE_CAPS:
                    raise ValueError(
                        f"Capability {key!r} does not support 'confirm' mode"
                    )
                setattr(token, key, value)
                cap_changed = True
            elif key in mutable_fields:
                if key == "persona" and value not in PERSONA_NAMES:
                    raise ValueError(f"Unknown persona: {value!r}")
                if key == "confirm_inline_wait_seconds":
                    value = _clamp_inline_wait(value)
                setattr(token, key, value)
        if token.rate_limit_requests == 0:
            token.rate_limit_burst = 0
        # If any capability changed, re-derive persona from the resulting cap set.
        # This keeps the persona label honest: applying a preset and then tweaking
        # any cap drops the token to "custom" (or to a different matching preset).
        if cap_changed:
            from .personas import detect_persona  # noqa: PLC0415 - avoid circular import at module load
            token.persona = detect_persona(token.caps_dict())
        # Anything beyond a bare rename or an inline-wait tweak changes what the
        # connected agent may see or do, so advance the staleness counter (drives
        # the in-band advisory). confirm_inline_wait_seconds changes neither the
        # tool list nor the caps, so like name it must not fire a spurious advisory.
        non_invalidating = {"name", "confirm_inline_wait_seconds"}
        if cap_changed or any(k not in non_invalidating for k in kwargs):
            token.settings_version += 1
        token.updated_at = utcnow()
        await self.async_save()
        return token

    async def async_set_permissions(
        self,
        token_id: str,
        permissions: PermissionTree,
    ) -> TokenRecord | None:
        """Replace the entire permission tree for a token and persist."""
        token = self._tokens.get(token_id)
        if token is None:
            return None
        token.permissions = permissions
        token.settings_version += 1
        token.updated_at = utcnow()
        await self.async_save()
        return token

    async def async_patch_permission_node(
        self,
        token_id: str,
        node_type: str,
        node_id: str,
        state: str,
        hint: str | None = None,
    ) -> TokenRecord | None:
        """Set or clear a single permission node and persist.

        Setting state to GREY removes the node entirely (GREY is the default
        and is not stored explicitly).
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        if node_type not in ("domains", "devices", "entities"):
            return None
        collection = getattr(token.permissions, node_type, None)
        if collection is None:
            return None
        if state == "GREY":
            collection.pop(node_id, None)
        else:
            collection[node_id] = PermissionNode(state=state, hint=hint)
        token.settings_version += 1
        token.updated_at = utcnow()
        await self.async_save()
        return token

    def _preset_name_taken(self, token: TokenRecord, name: str, exclude_id: str | None = None) -> bool:
        lowered = name.lower()
        return any(p.name.lower() == lowered for p in token.presets if p.id != exclude_id)

    async def async_add_preset(self, token_id: str, name: str) -> TokenRecord | None:
        """Snapshot the token's CURRENT settings as a new preset and mark it active.

        Callers must hold self.async_lock. The new preset equals the live state
        by construction, so it becomes the active preset (workspace model: a
        dirty outgoing preset keeps its last saved payload; the unsaved changes
        move into the new preset). Raises ValueError on a duplicate name or
        when the per-token cap is reached. Returns None if the token is missing.
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        if len(token.presets) >= MAX_PRESETS_PER_TOKEN:
            raise ValueError(f"A token can have at most {MAX_PRESETS_PER_TOKEN} presets")
        if self._preset_name_taken(token, name):
            raise ValueError(f"A preset named {name!r} already exists on this token")
        preset = snapshot_preset(token, name)
        token.presets.append(preset)
        token.active_preset_id = preset.id
        token.updated_at = utcnow()
        await self.async_save()
        return token

    async def async_rename_preset(self, token_id: str, preset_id: str, name: str) -> TokenRecord | None:
        """Rename a preset. Callers must hold self.async_lock.

        Raises LookupError for an unknown preset and ValueError for a duplicate
        name. Returns None if the token is missing.
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        preset = next((p for p in token.presets if p.id == preset_id), None)
        if preset is None:
            raise LookupError(f"No preset {preset_id!r} on this token")
        if self._preset_name_taken(token, name, exclude_id=preset_id):
            raise ValueError(f"A preset named {name!r} already exists on this token")
        preset.name = name
        token.updated_at = utcnow()
        await self.async_save()
        return token

    async def async_delete_preset(self, token_id: str, preset_id: str) -> TokenRecord | None:
        """Delete a preset. Callers must hold self.async_lock.

        The ACTIVE preset cannot be deleted (it is where the token's unsaved
        changes live; switch away first). Raises LookupError for an unknown
        preset, ValueError for the active one. Returns None if the token is missing.
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        preset = next((p for p in token.presets if p.id == preset_id), None)
        if preset is None:
            raise LookupError(f"No preset {preset_id!r} on this token")
        if preset_id == token.active_preset_id:
            raise ValueError("The active preset cannot be deleted; switch to another preset first")
        token.presets.remove(preset)
        token.updated_at = utcnow()
        await self.async_save()
        return token

    async def async_sync_preset(self, token_id: str, preset_id: str) -> TokenRecord | None:
        """Overwrite a preset's payload from the token's live settings (auto-save-back).

        Callers must hold self.async_lock. Keeps the preset's id, name, and
        created_at. Raises LookupError for an unknown preset. Returns None if
        the token is missing. Does not bump settings_version (the live state
        the agent sees is unchanged).
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        for i, preset in enumerate(token.presets):
            if preset.id == preset_id:
                synced = snapshot_preset(token, preset.name, preset_id=preset.id)
                synced.created_at = preset.created_at
                token.presets[i] = synced
                await self.async_save()
                return token
        raise LookupError(f"No preset {preset_id!r} on this token")

    async def async_seed_default_presets(self) -> int:
        """Seed a "Default preset" on every token that has none, marked active.

        Callers must hold self.async_lock. Runs when the token_presets_enabled
        setting flips on; idempotent (tokens that already have presets are left
        alone). Returns the number of tokens seeded.
        """
        seeded = 0
        for token in self._tokens.values():
            if token.presets:
                continue
            preset = snapshot_preset(token, DEFAULT_PRESET_NAME)
            token.presets.append(preset)
            token.active_preset_id = preset.id
            seeded += 1
        if seeded:
            await self.async_save()
        return seeded

    def update_last_used(self, token_id: str, timestamp: datetime) -> None:
        """Record a last-used timestamp in memory only.

        Flushed periodically to storage by the interval registered in __init__.py
        and immediately on HA shutdown.
        """
        token = self._tokens.get(token_id)
        if token is not None:
            token.last_used_at = timestamp

    async def async_flush_last_used(self) -> None:
        """Flush in-memory last_used_at timestamps to storage."""
        await self.async_save()

    async def async_delete_archived(self, token_id: str) -> bool:
        """Permanently delete an archived token record. Returns False if not found."""
        async with self.async_lock:
            if token_id not in self._archived:
                return False
            del self._archived[token_id]
            await self.async_save()
        return True

    async def async_rotate_token(self, token_id: str) -> tuple[TokenRecord, str] | None:
        """Replace the token hash with a freshly generated value and persist.

        Returns (updated_record, raw_token) on success, or None if the token is not found.
        The raw token is returned exactly once and never stored. Callers must pass it to
        the client immediately and discard it.
        """
        token = self._tokens.get(token_id)
        if token is None:
            return None
        raw_token = TOKEN_PREFIX + secrets.token_hex(TOKEN_HEX_LENGTH // 2)
        token.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token.updated_at = utcnow()
        await self.async_save()
        return token, raw_token

    def get_settings(self) -> GlobalSettings:
        return self._settings

    def get_entity_hints(self) -> dict[str, str]:
        """Return the global entity-hint map (entity_id -> hint). Mutate via async_set_entity_hint."""
        return self._entity_hints

    async def async_set_entity_hint(self, entity_id: str, hint: str | None) -> None:
        """Set or clear the global hint for an entity, then persist. Falsy hint clears it."""
        if hint:
            self._entity_hints[entity_id] = hint
        else:
            self._entity_hints.pop(entity_id, None)
        await self.async_save()

    async def async_patch_settings(self, **kwargs) -> GlobalSettings:
        """Update any GlobalSettings fields by name and persist.

        Routes the merged result through GlobalSettings.from_dict so its
        coercion/clamping applies to every write path, not just the admin
        view's own validation; a bad internal write cannot persist an
        out-of-range or mistyped value.
        """
        self._settings = GlobalSettings.from_dict({**self._settings.to_dict(), **kwargs})
        await self.async_save()
        return self._settings

    async def async_wipe(self) -> None:
        """Clear all tokens, archived records, approvals, and settings, then persist.

        Pending approvals reference token IDs that this call deletes; leaving
        them would persist orphaned, un-approvable records (and their notifications
        would linger). The admin wipe handler dismisses those notifications and
        fires phoenix_mcp_approval_resolved for the captured records, mirroring revocation.
        """
        self._tokens.clear()
        self._archived.clear()
        self._pending_approvals = []
        self._settings = GlobalSettings()
        self._entity_hints = {}
        await self.async_save()


def token_name_slug(name: str) -> str:
    """Normalize a token name to a slug (lowercase, hyphens to underscores)."""
    return name.lower().replace("-", "_")


def hmac_compare(stored_hash: str, presented_hash: str) -> bool:
    """Constant-time comparison of two SHA-256 hex digests."""
    return hmac.compare_digest(stored_hash, presented_hash)


_LEGACY_ALLOW_TO_CAP = {
    "allow_automation_write": "cap_automation_write",
    "allow_script_write": "cap_script_write",
    "allow_config_read": "cap_config_read",
    "allow_template_render": "cap_template_render",
    "allow_restart": "cap_restart",
    "allow_physical_control": "cap_physical_control",
    "allow_service_response": "cap_service_response",
    "allow_broadcast": "cap_broadcast",
    "allow_log_read": "cap_log_read",
}


class _PhoenixStore(Store):
    """Subclass of HA's Store that wires our v1 -> v2 migration into the load path.

    HA's Store invokes `_async_migrate_func` when the stored major version differs
    from the constructor's version. The default implementation raises
    NotImplementedError; we override it to run `_migrate_storage_v1_to_v2` in place.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict
    ) -> dict:
        if old_major_version < STORAGE_VERSION:
            _migrate_storage_v1_to_v2(old_data)
        return old_data


# Every top-level storage collection and the container type it must be. A wrong
# type here (hand-edited or corrupted storage) would otherwise crash migration or
# parsing before the per-record loops can skip an individual bad entry.
_STORAGE_COLLECTIONS: dict[str, type] = {
    "tokens": list,
    "archived_tokens": list,
    "pending_approvals": list,
    "settings": dict,
    "entity_hints": dict,
}


def _normalize_storage_root(raw: object) -> tuple[dict, bool]:
    """Coerce the storage root and each top-level collection to its expected type.

    Returns the normalized dict and whether anything had to be replaced (so the
    caller can persist the cleaned form). A non-object root, or a collection of
    the wrong type, becomes an empty container; a correctly-typed collection is
    left alone, so individual bad entries inside it fall to the per-record loops.
    """
    if raw is None:
        # No stored file yet (fresh install); nothing to heal.
        return {}, False
    if not isinstance(raw, dict):
        # Any non-object root, including a falsy one ([], "", 0), is corrupt;
        # reset and heal it. (None is handled above so a fresh install never
        # triggers a spurious save.)
        _LOGGER.warning(
            "Phoenix MCP storage root is %s, not an object; ignoring it", type(raw).__name__,
        )
        return {}, True
    changed = False
    for key, expected in _STORAGE_COLLECTIONS.items():
        if key in raw and not isinstance(raw[key], expected):
            _LOGGER.warning(
                "Phoenix MCP storage key %r is %s, not %s; resetting it",
                key, type(raw[key]).__name__, expected.__name__,
            )
            raw[key] = expected()
            changed = True
    return raw, changed


def _migrate_storage_v1_to_v2(raw: dict) -> bool:
    """Migrate raw storage data from v1 to v2 in place.

    v1 used boolean allow_* flags. v2 uses cap_* tri-state strings
    ("deny" / "allow" / "confirm"), introduces persona, and adds the
    pending_approvals array. Returns True if any migration was applied.
    """
    if not isinstance(raw, dict) or not raw:
        return False
    needs_save = False
    for token in raw.get("tokens", []) or []:
        if not isinstance(token, dict):
            continue  # the load loop skips and logs it
        for old_key, new_key in _LEGACY_ALLOW_TO_CAP.items():
            if old_key in token:
                token[new_key] = CAP_ALLOW if token.pop(old_key) else CAP_DENY
                needs_save = True
        if "persona" not in token:
            token["persona"] = PERSONA_CUSTOM
            needs_save = True
    # Archived records do not retain capability flags,
    # but if a legacy archive carried them we still drop the keys.
    for archived in raw.get("archived_tokens", []) or []:
        if not isinstance(archived, dict):
            continue  # the load loop skips and logs it
        for old_key in _LEGACY_ALLOW_TO_CAP:
            if old_key in archived:
                archived.pop(old_key, None)
                needs_save = True
    if "pending_approvals" not in raw:
        raw["pending_approvals"] = []
        needs_save = True
    return needs_save
