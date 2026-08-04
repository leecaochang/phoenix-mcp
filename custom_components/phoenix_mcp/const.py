"""Constants for the Phoenix MCP integration."""

import datetime
import pathlib
import re

# Phoenix's OWN operator-facing string catalogs (panel, notification, voice),
# deliberately NOT under translations/. hassfest validates translations/en.json
# against a closed set of Home Assistant categories and errors on any other
# top-level key, which fails the HACS submission; translations/ therefore holds
# only HA's own config and entity sections. Read by helpers._catalog_section and
# served to the panel by admin_view.PhoenixAdminCatalogView.
CATALOGS_DIR = pathlib.Path(__file__).parent / "catalogs"

PHOENIX_VERSION = "1.0.0"
MIN_HA_VERSION = "2024.5.0"
GITHUB_URL = "https://github.com/leecaochang/phoenix-mcp"
DOMAIN = "phoenix_mcp"
STORAGE_KEY = "phoenix_mcp"
STORAGE_VERSION = 2

PROXY_TIMEOUT_SECONDS = 30
MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_BATCH_ITEMS = 50

TOKEN_PREFIX = "phx_"
TOKEN_HEX_LENGTH = 64
TOKEN_LENGTH = len(TOKEN_PREFIX) + TOKEN_HEX_LENGTH

TOKEN_NAME_REGEX = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")

DEFAULT_RATE_LIMIT_REQUESTS = 60
DEFAULT_RATE_LIMIT_BURST = 10

# Token settings presets (named snapshots of a token's full configuration,
# switchable from the panel; gated by settings.token_presets_enabled).
MAX_PRESETS_PER_TOKEN = 8
PRESET_NAME_MAX_LENGTH = 40
DEFAULT_PRESET_NAME = "Default preset"

FLUSH_INTERVAL = datetime.timedelta(minutes=5)
EXPIRY_CHECK_INTERVAL = datetime.timedelta(minutes=1)
SENSOR_PUSH_INTERVAL = datetime.timedelta(hours=1)
# Coalescing window for per-request sensor state writes (update_token_counter):
# the first request after a flush schedules one write this many seconds out and
# further requests ride it, so a busy token produces at most one state_changed
# per window instead of one per request.
SENSOR_WRITE_DEBOUNCE_SECONDS = 5

AUDIT_LOG_MAXLEN = 10000
AUDIT_STORAGE_KEY = "phoenix_mcp_audit"
AUDIT_STORAGE_VERSION = 1
# audit_flush_interval is stored and exposed in minutes (not seconds).
# Valid values: 0 (disable periodic flush), 5, 10, 15, 30, 60.

# Configuration version history. Immutable before/after snapshots of
# agent-driven create/edit/delete of automations, scripts, scenes, and helpers,
# with admin-only rollback. Stored separately from tokens so the schema versions
# evolve independently.
VERSION_STORAGE_KEY = "phoenix_mcp_versions"
VERSION_STORAGE_VERSION = 1
# Per-resource FIFO retention: the newest N versions per (resource_type,
# resource_id) are kept; older ones are evicted on write.
MAX_VERSIONS_PER_RESOURCE = 20
# Resource types eligible for version history. VersionStore.record RAISES on
# anything absent here, and _record_version swallows that (history must never
# break a write), so a type missing from this set loses its whole rollback path
# SILENTLY, with only a log line. Adding a _record_version call site means adding
# the type here AND to tests/contract/version_resource_types.json;
# tests/test_frontend_contract.py pins this set against that fixture in both
# directions, because the two drifted exactly this way on 2026-08-03 (`energy`
# reached the fixture and the TS union but not here, and every energy version
# silently failed to record).
VERSIONED_RESOURCE_TYPES = frozenset(
    {"automation", "script", "scene", "helper", "dashboard", "yaml_config", "file", "entity",
     "blueprint", "esphome_yaml", "energy", "config_entry"}
)

# Dashboard card catalog. Which Lovelace cards this instance can actually render,
# harvested by the panel frontend and cached here so the MCP tool surface can
# answer "what cards do I have" before an agent authors one. It MUST be harvested
# in a browser: a card's type strings frequently do not exist as literals in its
# minified bundle (Mushroom builds all 19 of its types by string concatenation at
# runtime), so parsing the JS on disk finds roughly a quarter of a real catalog
# and silently misses the most-used card sets. window.customCards is the registry
# HA's own card picker reads, and only a browser that imported the resources has it.
CARD_CATALOG_STORAGE_KEY = "phoenix_mcp_card_catalog"
CARD_CATALOG_STORAGE_VERSION = 1
# Bounds on an ingest payload. The catalog is admin-supplied and third-party
# shaped, so every dimension is capped before it reaches .storage.
MAX_CARD_CATALOG_ENTRIES = 500
MAX_CARD_STUB_CONFIG_BYTES = 2000
MAX_CARD_CATALOG_FAILURES = 50

# agentCLI: the in-panel LLM chat that runs an agentic loop server-side on
# behalf of a chosen token. Provider API keys / base URLs persist in a dedicated
# Store, separate from tokens, so the recoverable secret never rides the general
# settings serialization (which the settings GET and audit snapshots echo).
AGENTCLI_SECRETS_STORAGE_KEY = "phoenix_mcp_agentcli_secrets"
AGENTCLI_SECRETS_STORAGE_VERSION = 1
AGENTCLI_PROVIDERS = frozenset({
    "claude", "deepseek", "chatgpt", "gemini", "grok", "kimi", "meta",
    "minimax", "openrouter", "nvidia", "ollama", "ollama_cloud"})
# Default endpoints/models; the panel may override the model per conversation.
AGENTCLI_CLAUDE_BASE_URL = "https://api.anthropic.com"
AGENTCLI_CLAUDE_DEFAULT_MODEL = "claude-opus-4-8"
AGENTCLI_ANTHROPIC_VERSION = "2023-06-01"
AGENTCLI_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# `deepseek-chat` and `deepseek-reasoner` were RETIRED on 2026-07-24 and now
# answer an HTTP error. They were never separate models, only routing labels for
# the non-thinking and thinking modes of the current generation, which is why the
# replacement is one id plus the `thinking` field rather than two ids. A shipped
# default is a guess that rots: the provider card refuses to store a model the
# provider does not list, so this constant is a starting point and not a promise.
AGENTCLI_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
# ChatGPT (OpenAI) is OpenAI-compatible; base already includes /v1.
AGENTCLI_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Gemini via Google's OpenAI-compatible endpoint (Authorization: Bearer <key>);
# base already includes the OpenAI path, so chat/models append cleanly.
AGENTCLI_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
AGENTCLI_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
# Grok (xAI) is OpenAI-compatible; base includes /v1 so chat/models append cleanly.
# No default model: the live /v1/models list populates the dropdown (like ChatGPT).
AGENTCLI_GROK_BASE_URL = "https://api.x.ai/v1"
AGENTCLI_GROK_DEFAULT_MODEL = ""
# MiniMax via its Anthropic-compatible API (Authorization: Bearer <key>); base has
# no /v1, so /v1/messages and /v1/models append cleanly. It speaks the Anthropic
# Messages wire format, so it runs through ClaudeProvider, not OpenAICompatProvider.
AGENTCLI_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
AGENTCLI_MINIMAX_DEFAULT_MODEL = "MiniMax-M2"
# MiniMax does not document a models-list endpoint; this curated set backs the
# model dropdown when the live probe finds none.
AGENTCLI_MINIMAX_MODELS = ("MiniMax-M2", "MiniMax-M1")
# Kimi (Moonshot) via its OpenAI-compatible API (Authorization: Bearer); base
# includes /v1 so chat/models append cleanly. It also fronts an
# Anthropic-compatible path at /anthropic, but the OpenAI path is the documented
# primary, is the one whose tool-calling is documented, and is the only one with a
# models-list endpoint, so Phoenix MCP runs Kimi through OpenAICompatProvider.
AGENTCLI_KIMI_BASE_URL = "https://api.moonshot.ai/v1"
# No default model, the ChatGPT/Grok convention: Kimi's /v1/models is scoped to
# what the ACCOUNT is entitled to, not the published catalog (K3 is plan-gated,
# and k2.5 + the moonshot-v1 series are withheld from newly registered users), so
# only the live list knows which models a given key can actually call. Naming a
# flagship default here would offer a model that answers 404 on the lower plans.
AGENTCLI_KIMI_DEFAULT_MODEL = ""
# Meta's Model API (dev.meta.ai) is OpenAI-compatible; base includes /v1 so
# chat/models append cleanly. Its predecessor api.llama.com is being retired.
AGENTCLI_META_BASE_URL = "https://api.meta.ai/v1"
AGENTCLI_META_DEFAULT_MODEL = "muse-spark-1.1"
# OpenRouter aggregates many providers behind one OpenAI-compatible key (a common
# region-friendly way to reach Meta's Llama and hundreds of other models). Base
# includes /api/v1; no default model (the live tool-capable list drives the dropdown).
AGENTCLI_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# NVIDIA (build.nvidia.com) hosts many vendors' models behind one OpenAI-compatible
# key (Authorization: Bearer nvapi-...); base includes /v1 so chat/models append
# cleanly. No default model: the live list drives the dropdown. Model ids are
# vendor-namespaced (e.g. "meta/llama-3.3-70b-instruct"), and tool-calling support
# varies by model, which the catalog does not advertise.
AGENTCLI_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
AGENTCLI_NVIDIA_DEFAULT_MODEL = ""
# Local Ollama has no default: the operator supplies a local base URL and picks a
# model from the installed list. Ollama Cloud is the same wire format but hosted
# at a fixed URL and authenticated with an API key (Authorization: Bearer).
AGENTCLI_OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
AGENTCLI_DEFAULT_MAX_TOKENS = 8192
# Explicit output cap sent to DeepSeek only. DeepSeek's undocumented default
# (historically 4K non-thinking / 32K thinking) truncated a large tool call
# (a whole-dashboard write) mid-JSON; 32K is at or above both historical
# defaults, so it only ever raises the ceiling, and is far under the V4
# documented 384K max output. Other OpenAI-compatible kinds are deliberately
# not sent max_tokens (OpenAI reasoning models reject the field outright).
AGENTCLI_DEEPSEEK_MAX_TOKENS = 32768
# How many Ollama /api/show lookups run at once during a capability refresh.
# Ollama is the only backend whose capabilities cost one request PER MODEL, and a
# large local library would otherwise arrive as one burst against a machine that
# is also running Home Assistant.
AGENTCLI_CAPABILITY_CONCURRENCY = 6

AGENTCLI_DEFAULT_EFFORT = "high"
AGENTCLI_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
# Per-turn cap on provider<->tool round trips before the loop pauses. In the
# interactive chat this is a "continue?" checkpoint (the operator can grant
# another N rounds), not a hard stop; the headless voice/AI-task loops stop
# here since there is no one to ask. Admin-configurable via the
# agentcli_max_iterations setting; this is the default and the clamp bounds.
AGENTCLI_MAX_ITERATIONS = 20
AGENTCLI_MAX_ITERATIONS_MIN = 3
AGENTCLI_MAX_ITERATIONS_MAX = 100
# Cap on the between-newline SSE buffer from a provider stream. A real frame is a
# few KB; this bounds a faulty/hostile provider that never sends a newline.
AGENTCLI_MAX_SSE_BUFFER_BYTES = 8 * 1024 * 1024
# Cap on the CUMULATIVE bytes of a single provider response (all frames summed),
# so a flood of small newline-delimited frames cannot exhaust memory even though
# each frame stays under the per-frame cap. A real response is far smaller.
AGENTCLI_MAX_STREAM_BYTES = 32 * 1024 * 1024
# Cap on the TOTAL provider-generated output retained across a whole turn (all
# iterations), measured on the assistant messages held in the conversation. The
# per-response cap above bounds one stream; this bounds the sum so 12 iterations
# cannot accumulate a large multiple. A real multi-step turn stays far below it.
AGENTCLI_MAX_TURN_OUTPUT_BYTES = 48 * 1024 * 1024
# Max idle gap between stream chunks before the provider request is aborted. The
# total time is deliberately unbounded (a long agentic turn streams for minutes),
# but a provider that stops sending data must not hang the turn forever.
AGENTCLI_STREAM_READ_TIMEOUT_SECONDS = 180
# Interactive inline-approval wait, tied to the live SSE connection (far longer
# than the per-token confirm_inline_wait cap, which is for unattended agents).
AGENTCLI_APPROVAL_WAIT_SECONDS = 900
# How often Agent Chat relays a held tool's progress line to the panel. Faster
# than the MCP keepalive because this is a local connection with no proxy in
# the way, and the panel is where a human is actually watching.
AGENTCLI_PROGRESS_INTERVAL_SECONDS = 2.0
# Sentinel client_ip so agentCLI-driven tool calls are attributable in the audit
# log without a core change to _dispatch_mcp.
AGENTCLI_CLIENT_IP = "agentcli"
# Transcript scrollback bound (also the conversation-memory bound): lines dropped
# from the window are dropped from what the LLM is sent.
AGENTCLI_SCROLLBACK_DEFAULT = 100
AGENTCLI_SCROLLBACK_MIN = 0
AGENTCLI_SCROLLBACK_MAX = 5000
# Display-only cap on the tool-result text streamed to the verbose panel view
# (the model always receives the full result via the message history). Large
# enough that ordinary results are shown whole; only pathological dumps clip.
AGENTCLI_TOOL_RESULT_MAX_CHARS = 20000

# Assist bridge (assist_api.py): a registered llm.API exposes the bound token's
# scoped tool surface to HA's native Assist/voice conversation agents.
# Sentinel client_ip so Assist-driven tool calls are attributable in the audit
# log without a core change to _dispatch_mcp (mirrors AGENTCLI_CLIENT_IP).
ASSIST_CLIENT_IP = "assist"
# Sentinel client_ip for the Phoenix MCP voice agent (voice_agent.py): Phoenix MCP registered as
# HA's conversation agent, running its own model loop. Distinct from ASSIST_CLIENT_IP
# (the llm.API bridge, which uses HA's model) for audit clarity; both suppress the
# stale-tools advisory since each rebuilds its tool list every turn.
VOICE_AGENT_CLIENT_IP = "voice"
# Sentinel client_ip for the Phoenix MCP AI Task entity (ai_task.py): Phoenix MCP registered as an
# HA AITaskEntity, running its own model loop for ai_task.generate_data. Distinct
# from the voice/assist sentinels for audit clarity; suppresses the stale-tools
# advisory like the others (it rebuilds its tool list every task).
AI_TASK_CLIENT_IP = "ai_task"
# Documented HA floor for the AI Task entity: the ai_task integration shipped in HA
# 2025.7. Real gate is runtime feature detection (the import may be absent); this
# only drives the panel's "requires HA X+" message, like ASSIST_API_MIN_HA.
AI_TASK_MIN_HA = "2025.7.0"
# Documented HA floor for the feature: convert_to_voluptuous (used to translate
# Phoenix MCP's JSON-Schema tool params into the llm.Tool voluptuous schema) shipped with
# the mcp client integration in HA 2025.2. The real gate is runtime feature
# detection (the import may be absent); this constant only drives the panel's
# "requires HA X+" message, decoupled from MIN_HA_VERSION like MESA_INJECT_MIN_HA.
ASSIST_API_MIN_HA = "2025.2.0"

# MESA (semantic safety layer) integration. Profiles persist in a separate
# Store from tokens so the two storage versions evolve independently.
# The inheritance levels a profile can be authored AT, most specific first.
# Deployment defaults are deliberately absent: they are a single fallback record
# rather than a keyed level, and every surface treats them separately.
#
# Hand-mirrored into the panel (the scope union, the six label maps, the editor's
# API dispatch, the list table, and the in-context injector), where a missing
# entry is mostly SILENT rather than a type error: the editor's dispatch used to
# fall through to the area endpoint for any scope it did not name. A contract
# fixture pins this list against the frontend in both directions.
MESA_SCOPES: tuple[str, ...] = ("entity", "device", "area", "integration", "domain")

MESA_STORAGE_KEY = "phoenix_mcp_mesa"
MESA_STORAGE_VERSION = 1
# Global enforcement mode for the vendored MesaEnforcer. "off" disables MESA
# entirely; "advisory" warns but never blocks (except read_only, which is
# entity-nature, not policy); "enforced" blocks and routes confirm through the
# admin approval gate. Default advisory: the zero-profile domain baseline is
# aggressive (lock/alarm prohibited), so enforced is opt-in.
MESA_MODE_OFF = "off"
MESA_MODE_ADVISORY = "advisory"
MESA_MODE_ENFORCED = "enforced"
MESA_MODES = frozenset({MESA_MODE_OFF, MESA_MODE_ADVISORY, MESA_MODE_ENFORCED})
# Sentinel cap_name on a PendingApproval created by a MESA control_mode:confirm
# block. It is not a real capability; the approval re-validation path special-
# cases it so effective_cap() (which would auto-deny an unknown cap) is skipped.
MESA_CONFIRM_CAP = "mesa_control_mode"
# Executor key for re-running a MESA-gated service call after admin approval.
# Registered in mcp_view._EXECUTOR_REGISTRY but never dispatchable from the
# tool router, so a token cannot invoke the approved-bypass path directly.
MESA_APPROVED_EXECUTOR = "call_service_mesa_approved"

# Minimum HA frontend version for the optional MESA profile injector.
# Separate from MIN_HA_VERSION because the injector depends on frontend DOM shape.
MESA_INJECT_MIN_HA = "2025.5.0"

# MESA profile suggestions (mesa_suggestions.py). Two v1 signals; suggestions
# are computed admin-side and NEVER auto-applied.
MESA_SUGGEST_SIGNAL_BLAST_RADIUS = "blast_radius"
MESA_SUGGEST_SIGNAL_NAKED_RISKY = "naked_risky"
# Curated risky domains for the naked-risky signal: domain -> (suggested
# control_mode, concern text surfaced verbatim in the suggestion reason).
# lock/alarm suggest "prohibited" to MATCH mesa-core's zero-profile baseline,
# so applying a suggestion is never a relaxation under enforced mode; the
# rest suggest "confirm". Irreversibility rationale folded per signal 9e.
# The suggestion reason an operator reads, and which is written verbatim into a
# profile's control_reason when they Apply. English stays the stored form (like
# a diff summary, it is the record); the panel renders the key in its own
# language. Keys are mirrored into translations/en.json by
# scripts/gen_diff_catalog.py, so never hand-edit that section.
MESA_SUGGESTION_TEMPLATES: dict[str, str] = {
    "blast_radius.risky.one": (
        "This {noun} touches {count} risky entity ({shown}) and has no MESA profile of "
        "its own; an agent allowed to trigger it controls those entities indirectly, "
        "bypassing their own profiles."
    ),
    "blast_radius.risky.other": (
        "This {noun} touches {count} risky entities ({shown}) and has no MESA profile of "
        "its own; an agent allowed to trigger it controls those entities indirectly, "
        "bypassing their own profiles."
    ),
    "blast_radius.fanout": (
        "This {noun} references {count} entities and has no MESA profile of its own; "
        "one trigger has a wide blast radius."
    ),
    "naked_risky.domain": (
        "{count} {domain} entities have no MESA profile at any level. This domain "
        "{concern}. A domain-level profile covers them all at once.{baseline_note}"
    ),
    "naked_risky.entity": (
        "No MESA profile covers this entity at any level, and it {concern}.{baseline_note}"
    ),
}

# The domain nouns and the trailing baseline note the templates interpolate.
MESA_SUGGESTION_PHRASES: dict[str, str] = {
    "noun.automation": "automation",
    "noun.script": "script",
    "noun.scene": "scene",
    "shown.more": "{shown} + {extra} more",
    "baseline_note": (
        " Prohibited matches the built-in baseline for this domain, so the authored "
        "profile makes that intent explicit; choose confirm in Review to allow agent "
        "control behind approvals instead."
    ),
}

MESA_SUGGEST_RISKY_DOMAINS: dict[str, tuple[str, str]] = {
    "lock": ("prohibited", "controls a physical security boundary; a mistaken unlock cannot be undone remotely"),
    "alarm_control_panel": ("prohibited", "arms and disarms the home security system; a silent disarm defeats it"),
    "cover": ("confirm", "opens a physical entry point"),
    "valve": ("confirm", "controls water or gas flow; a wrong state can flood or vent unattended"),
    "siren": ("confirm", "sounds a loud alarm; false activation disturbs occupants and neighbours"),
    "water_heater": ("confirm", "reaches scald temperatures and burns energy; slow to recover"),
    "update": ("confirm", "firmware updates are effectively irreversible and can brick the device"),
}

# Each domain's concern clause is interpolated INTO a reason template, so it
# needs its own catalog entry: translating the sentence but not the clause spliced
# into it produces a half-translated line. Derived from the table above so the two
# cannot drift.
MESA_SUGGESTION_PHRASES.update({
    f"concern.{domain}": concern
    for domain, (_mode, concern) in MESA_SUGGEST_RISKY_DOMAINS.items()
})
# cover is risky only for these device classes (blinds and shades are noise).
MESA_SUGGEST_COVER_DEVICE_CLASSES = frozenset({"garage", "door", "gate"})
# The blast-radius signal fires on fan-out alone at this many referenced entities.
MESA_SUGGEST_FANOUT_THRESHOLD = 8
# The naked-risky signal consolidates to ONE domain-level suggestion at this many
# uncovered entities in one domain (cover exempt: a domain profile would catch
# blinds too).
MESA_SUGGEST_CONSOLIDATE_THRESHOLD = 5
# Cap on entity ids carried in a suggestion's evidence payload.
MESA_SUGGEST_MAX_EVIDENCE_IDS = 10

SENSITIVE_ATTRIBUTES = frozenset({
    "entity_picture",
    "stream_url",
    "access_token",
    "still_image_url",
})

# Substrings (matched case-insensitively against a key name) that mark a value as
# sensitive regardless of which integration produced it. Such values are dropped
# from state attributes and replaced with "<redacted>" in service-response and
# event data. Defense in depth: third-party integrations can surface secrets
# (tokens, passwords, API keys) under arbitrary attribute/response keys that the
# fixed SENSITIVE_ATTRIBUTES list does not name. Over-redaction is the safe
# failure mode here, so the substrings are matched liberally.
SENSITIVE_KEY_SUBSTRINGS = frozenset({
    "password", "secret", "api_key", "apikey", "access_token",
    "auth_token", "authorization", "credential", "private_key",
    "token", "session",
})

# The placeholder every redacting reader substitutes for a value the token may not
# see. Named here so a WRITE path can refuse to persist it: a config read is lossy
# (an out-of-scope or ghost entity comes back as this string), so a caller that
# echoes a read back would silently store the placeholder as if it were real
# configuration. `tool_common.redaction_sentinel_path` is that guard.
#
# The four PRODUCERS (policy_engine.filter_service_response, helpers.redact_structure,
# helpers.redact_diagnostics, esphome_yaml's value scrub) still spell the literal
# inline; this constant is deliberately NOT a refactor of them, because rewriting
# working redaction code carries more risk than the duplication does. What keeps the
# two honest is tests/test_redaction_sentinel.py, which asserts this value is what
# filter_service_response actually emits, so a producer that ever changes its
# placeholder fails there rather than silently disarming the write guard.
REDACTION_SENTINEL = "<redacted>"

# Domain-aware "lean" view for get_state / get_states. When a caller passes no
# explicit `fields` and does not set `detailed`, the state is narrowed to the base
# fields (entity_id, state) plus LEAN_ALWAYS_ATTRS plus the domain's important
# attributes below, to cut token cost on the common read path. Field selection
# always runs AFTER sensitive-attribute scrubbing, so it can never reveal a
# scrubbed value. Domains with no entry fall back to base + LEAN_ALWAYS_ATTRS only;
# describe_entity remains the full-detail single-entity tool.
#
# The attribute names are Home Assistant's own canonical primary-state attributes
# per domain (each domain's ATTR_* constants / entity.state_attributes), curated to
# what an agent needs to reason about or act on that domain's state, so a weak model
# rarely has to escalate to detailed=. This is an allowlist by design (never a
# denylist): an unlisted attribute or domain is simply omitted, and the fix for a
# missing attribute is to add it here, not to strip junk. Deliberately excluded:
# large/nested blobs (weather forecast, image/URL-class already scrubbed), long
# free text (release_summary, calendar description), the big capability *_list attrs
# (effect_list/source_list/fan_modes; the current *value* like effect/source stays),
# and location coordinates. HA-coupling point: re-verify these names on HA upgrades
# (e.g. counter uses minimum/maximum, not min/max like number).
LEAN_ALWAYS_ATTRS = ("friendly_name",)

DOMAIN_IMPORTANT_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "light": ("brightness", "color_mode", "color_temp_kelvin", "rgb_color", "effect",
              "supported_color_modes"),
    "climate": ("current_temperature", "temperature", "target_temp_high", "target_temp_low",
                "min_temp", "max_temp", "current_humidity", "hvac_action", "hvac_mode",
                "hvac_modes", "preset_mode", "fan_mode", "swing_mode"),
    "media_player": ("media_title", "media_artist", "source", "volume_level", "is_volume_muted",
                     "media_content_type", "media_position", "media_duration", "app_name"),
    "cover": ("current_position", "current_tilt_position", "device_class"),
    "valve": ("current_position", "device_class"),
    "fan": ("percentage", "preset_mode", "oscillating", "direction"),
    "sensor": ("device_class", "unit_of_measurement", "state_class"),
    "binary_sensor": ("device_class",),
    "lock": ("changed_by",),
    "alarm_control_panel": ("changed_by",),
    "vacuum": ("battery_level", "fan_speed", "status"),
    "humidifier": ("current_humidity", "humidity", "mode"),
    "water_heater": ("current_temperature", "temperature", "operation_mode"),
    "weather": ("temperature", "humidity", "wind_speed"),
    "device_tracker": ("source_type",),
    "number": ("min", "max", "step", "mode", "unit_of_measurement"),
    "select": ("options",),
    "switch": ("device_class",),
    "update": ("installed_version", "latest_version", "in_progress", "update_percentage", "title"),
    "timer": ("duration", "remaining", "finishes_at"),
    "sun": ("elevation", "next_dawn", "next_dusk", "next_rising", "next_setting"),
    "automation": ("last_triggered", "mode", "current", "max"),
    "script": ("last_triggered", "mode", "current", "max", "last_action"),
    "input_number": ("min", "max", "step", "mode"),
    "input_select": ("options",),
    "input_text": ("min", "max", "pattern", "mode"),
    "counter": ("minimum", "maximum", "step"),
    "calendar": ("message", "start_time", "end_time", "all_day", "location"),
    "remote": ("current_activity",),
}

BLOCKED_DOMAINS = frozenset({"phoenix_mcp"})

HIGH_RISK_DOMAINS = frozenset({
    "homeassistant",
    "recorder",
    "system_log",
    "hassio",
    "backup",
    "notify",
    "persistent_notification",
    "mqtt",
})

DUAL_GATE_SERVICES = frozenset({
    "homeassistant/restart",
    "homeassistant/stop",
})

# Services that require cap_physical_control even when pass_through is True.
# These represent irreversible or safety-relevant physical actions.
PHYSICAL_GATE_SERVICES = frozenset({
    "lock/lock",
    "lock/unlock",
    "lock/open",
    "alarm_control_panel/alarm_disarm",
    "alarm_control_panel/alarm_arm_away",
    "alarm_control_panel/alarm_arm_home",
    "alarm_control_panel/alarm_arm_night",
    "alarm_control_panel/alarm_arm_vacation",
    "alarm_control_panel/alarm_trigger",
    "cover/open_cover",
    "cover/close_cover",
    "cover/stop_cover",
    "cover/set_cover_position",
    "cover/set_cover_tilt_position",
    "valve/open_valve",
    "valve/close_valve",
    "valve/stop_valve",
    "valve/set_valve_position",
})

# Core HA services that take NO entity/device/area target: they re-read YAML
# configuration domain-wide. Phoenix MCP's normal call_service path flattens every call
# to an explicit entity_id list (rule 15), which these services' schemas reject
# ("extra keys not allowed @ data['entity_id']"), so they are handled by a
# dedicated no-target branch that calls them with bare service_data. They are the
# "apply" step for a raw set_yaml_config / write_file edit, so they gate on
# cap_yaml_edit (the same capability that authorized the edit authorizes the
# reload). HA-version-sensitive: this is a curated list of stable core reload
# services, deliberately conservative (integration-specific reloads like
# mqtt/rest/command_line are omitted); re-verify and extend on HA upgrades.
# homeassistant/reload_config_entry is excluded on purpose: it takes a target.
NO_TARGET_SERVICES = frozenset({
    "homeassistant/reload_all",
    "homeassistant/reload_core_config",
    "automation/reload",
    "script/reload",
    "scene/reload",
    "input_boolean/reload",
    "input_button/reload",
    "input_number/reload",
    "input_text/reload",
    "input_select/reload",
    "input_datetime/reload",
    "template/reload",
    "group/reload",
    "zone/reload",
    "person/reload",
    "schedule/reload",
    "counter/reload",
    "timer/reload",
})

# Capability modes. A token's per-capability state is one of these three values.
CAP_DENY = "deny"
CAP_ALLOW = "allow"
CAP_CONFIRM = "confirm"
CAP_MODES = frozenset({CAP_DENY, CAP_ALLOW, CAP_CONFIRM})

# Canonical list of all capability names. Every cap_* field on TokenRecord
# must appear here. Adding a new cap is a three-step change: add to this list,
# add to CAPABILITY_TIERS, and update the persona table in personas.py.
CAPABILITY_NAMES = (
    "cap_config_read",
    "cap_template_render",
    "cap_log_read",
    "cap_search",
    "cap_registry_read",
    "cap_traces",
    "cap_diagnostics",
    "cap_broadcast",
    "cap_service_response",
    "cap_automation_write",
    "cap_script_write",
    "cap_blueprint_write",
    "cap_scene_write",
    "cap_helper_write",
    "cap_physical_control",
    "cap_restart",
    "cap_integration_write",
    "cap_lovelace_write",
    "cap_registry_write",
    "cap_radio_write",
    "cap_energy_write",
    "cap_backup",
    "cap_filesystem",
    "cap_yaml_edit",
    "cap_esphome_yaml",
    "cap_esphome_flash",
)

# Tiers drive UI grouping and which capabilities offer Confirm.
# Read and Everyday tiers are deny/allow only; the others support Confirm.
CAPABILITY_TIERS: dict[str, str] = {
    "cap_config_read": "read",
    "cap_template_render": "read",
    "cap_log_read": "read",
    "cap_search": "read",
    "cap_registry_read": "read",
    "cap_traces": "read",
    "cap_diagnostics": "read",
    "cap_broadcast": "everyday",
    "cap_service_response": "everyday",
    "cap_automation_write": "config_write",
    "cap_script_write": "config_write",
    "cap_blueprint_write": "config_write",
    "cap_scene_write": "config_write",
    "cap_helper_write": "config_write",
    "cap_physical_control": "system",
    "cap_restart": "system",
    "cap_integration_write": "system",
    "cap_lovelace_write": "system",
    "cap_registry_write": "system",
    "cap_radio_write": "system",
    # Energy dashboard preferences: instance-wide dashboard configuration, so it
    # sits with cap_lovelace_write rather than in config_write, which holds the
    # things that define BEHAVIOUR (automations, scripts, scenes). Nothing here
    # actuates a device; a wrong write costs you the Energy setup, not the house.
    "cap_energy_write": "system",
    "cap_backup": "irreversible",
    "cap_filesystem": "irreversible",
    "cap_yaml_edit": "irreversible",
    "cap_esphome_yaml": "config_write",
    # Flashing is not config authoring: it actuates hardware, and a bad image can
    # leave a device unreachable until someone reaches it with a cable. It sits
    # with cap_radio_write, its nearest sibling in consequence.
    "cap_esphome_flash": "system",
}

# Capabilities for which Confirm is a meaningful third state.
# UI hides the Confirm option for caps not in this set.
CONFIRM_AVAILABLE_CAPS = frozenset({
    "cap_automation_write",
    "cap_script_write",
    "cap_blueprint_write",
    "cap_scene_write",
    "cap_helper_write",
    "cap_physical_control",
    "cap_restart",
    "cap_integration_write",
    "cap_lovelace_write",
    "cap_registry_write",
    "cap_radio_write",
    "cap_energy_write",
    "cap_backup",
    "cap_filesystem",
    "cap_yaml_edit",
    "cap_esphome_yaml",
    "cap_esphome_flash",
})

# Capabilities ALWAYS evaluated regardless of pass_through state.
# All other capabilities are bypassed (treated as "allow") when pass_through is True,
# except that "confirm" is honored even for non-exempt caps under pass_through
# (admin's intent to gate is preserved). See helpers.effective_cap.
PASS_THROUGH_EXEMPT_CAPS = frozenset({
    "cap_restart",
    "cap_physical_control",
    "cap_automation_write",
    "cap_script_write",
    "cap_blueprint_write",
    "cap_log_read",
    "cap_scene_write",
    "cap_helper_write",
    "cap_integration_write",
    "cap_lovelace_write",
    "cap_registry_write",
    "cap_radio_write",
    "cap_energy_write",
    "cap_backup",
    "cap_filesystem",
    "cap_yaml_edit",
    "cap_esphome_yaml",
    "cap_esphome_flash",
})

# Zigbee2MQTT bridge API. Phoenix MCP talks to Z2M over its MQTT request/response
# topics (<base>/bridge/request/... answered on <base>/bridge/response/...)
# and retained state topics (<base>/bridge/info, <base>/bridge/devices).
# The base topic is Z2M's default; a custom base_topic install would need
# this changed. Version-sensitive surface (shapes verified against Z2M 2.x).
Z2M_BASE_TOPIC = "zigbee2mqtt"
Z2M_REQUEST_TIMEOUT_SECONDS = 30.0
Z2M_RETAINED_READ_TIMEOUT_SECONDS = 5.0

# ESPHome Device Builder commands. Phoenix MCP speaks the add-on's multiplexed
# WebSocket API at <dashboard url>/ws for validation, for the
# board/component/automation reference data an authoring agent needs, and for
# the firmware job model (compile, install, poll, cancel).
# Version-sensitive surface: the frame shapes and command names are the add-on's,
# with no stability promise, so re-verify them on add-on upgrades.
# The add-on version every command here was live-verified against. Its OWN
# documentation has been wrong five separate times, so this records what was
# checked on the wire rather than what was read. Used ONLY to explain a failure
# (a refused command, a reply we cannot parse); a mismatch is never warned about
# on its own, because a warning whose consequence never arrives teaches operators
# to click past it, exactly as the install diff refuses to flag a patch bump.
# Bump it after re-verifying against a new add-on, not merely after upgrading.
ESPHOME_BUILDER_VERIFIED_VERSION = "2026.7.3"
ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS = 10.0
# Validation runs the config through ESPHome itself (no compile), which is
# seconds on a small file but can pull external_components on a large one.
ESPHOME_BUILDER_VALIDATE_TIMEOUT_SECONDS = 60.0
ESPHOME_BUILDER_OUTPUT_MAX_CHARS = 65_536
ESPHOME_BUILDER_PAGE_LIMIT = 20
ESPHOME_BUILDER_PAGE_LIMIT_MAX = 50
ESPHOME_BUILDER_MAX_COMPONENT_IDS = 10

# Firmware jobs. Compile and install ENQUEUE and return a job record, so those
# calls use the normal request timeout above; only the log replay of a finished
# job needs a longer one.
ESPHOME_BUILDER_JOB_LOG_TIMEOUT_SECONDS = 60.0
# A real build log runs 3-10k lines. The add-on already keeps only the last
# 2000, and the full 64KB budget above would still swamp an agent's context, so
# job output gets its own tighter bound.
ESPHOME_BUILDER_JOB_OUTPUT_MAX_CHARS = 16_384
# A poll of a RUNNING build gets a much tighter, tail-only slice. Live-found: the
# full log is near-identical between polls, so an agent checking a real build
# every 30 seconds pulled the same banner into its context a dozen times over.
# Progress is a question about the last few lines; the whole log is only worth
# returning once the build has actually finished.
ESPHOME_BUILDER_JOB_PROGRESS_MAX_CHARS = 4_096
# wait_for_esphome_job holds the request open so a build has something in flight
# to report progress on. This is the longest hold in the whole surface (the next
# is the 180s inline confirm), chosen to fit a real build in one call; the SSE
# keepalive is what keeps an intermediary from dropping it.
ESPHOME_BUILDER_JOB_WAIT_SECONDS = 300
# The same hold on a headless conversational surface (Assist's tool bridge, the
# voice agent, AI Task) is a hang: none of them can render a held request, their
# own pipeline timeouts are far shorter than the value above, and the build runs
# on regardless of whether anyone is waiting. Long enough for a cached build to
# land inside the turn, short enough that the reply is "still building, ask me
# again" rather than a timeout.
ESPHOME_BUILDER_JOB_WAIT_HEADLESS_SECONDS = 10
# Faster than the 15s keepalive so the reported percentage is fresh when a frame
# goes out, and so a build that finishes returns promptly rather than on the tick.
ESPHOME_BUILDER_JOB_POLL_SECONDS = 5.0
# The add-on caps a decode at 200 lines of 500 chars and times out server-side
# at 60s; the client waits a little longer so its own timeout is never the first
# to fire and mask the add-on's own answer.
ESPHOME_BUILDER_DECODE_TIMEOUT_SECONDS = 90.0
ESPHOME_BUILDER_MAX_BACKTRACE_LINES = 200
# devices/logs runs until it is killed, so a capture window bounds the request.
# The maximum is deliberately under the SSE keepalive story's comfort zone: this
# hold is real dead air on the wire for however long it runs.
ESPHOME_BUILDER_LOG_CAPTURE_SECONDS = 15
ESPHOME_BUILDER_LOG_CAPTURE_MAX_SECONDS = 60

# Pending-approval queue limits.
MAX_PENDING_APPROVALS_PER_TOKEN = 100
APPROVAL_DEFAULT_TTL_SECONDS = 3600
APPROVAL_SWEEP_INTERVAL = datetime.timedelta(minutes=5)

# How many approvals one batch-approve request may carry. A REQUEST-DURATION
# bound, not a review-policy one: admin_view._approve_approval runs each
# executor INLINE inside the admin's HTTP request (the same constraint that makes
# ESPHome builds enqueue a job instead), so a batch's wall time is the sum of its
# tools' and an unbounded one would hold a connection open for minutes and read
# as a hang. Set well above any realistic hand-selected batch; a migration
# touching every automation on a large instance ran to about 20.
MAX_BATCH_APPROVALS = 50

# Diff size limits for approval records.
MAX_DIFF_INLINE_BYTES = 100_000
MAX_PREVIEW_ENTITY_IDS = 500

# Persona identifiers. Definitions live in personas.py.
PERSONA_READ_ONLY = "read_only"
PERSONA_VOICE_ASSISTANT = "voice_assistant"
PERSONA_AUTOMATION_BUILDER = "automation_builder"
PERSONA_POWER_USER = "power_user"
PERSONA_HOME_ADMIN = "home_admin"
PERSONA_CUSTOM = "custom"
# Gentle starter persona: reads plus service calls, physical control gated to
# confirm. Seeded by the onboarding wizard and also offered in the normal picker.
PERSONA_NEW_USER = "new_user"
# Dashboard/UI work: reads + registry + dashboard write; filesystem confirm for
# theme and custom-card assets. No device control.
PERSONA_DASHBOARD_DESIGNER = "dashboard_designer"
# Routine upkeep: full reads + diagnostics + backups; restart gated to confirm.
# No config authoring or device control.
PERSONA_MAINTENANCE = "maintenance"
# ESPHome device work: diagnostic reads plus device-YAML editing gated to
# confirm. No entity control, no HA config authoring. Seeds caps only, so the
# admin still scopes the entity tree to the ESPHome devices.
PERSONA_ESPHOME = "esphome"
PERSONA_NAMES = frozenset({
    PERSONA_READ_ONLY,
    PERSONA_VOICE_ASSISTANT,
    PERSONA_AUTOMATION_BUILDER,
    PERSONA_POWER_USER,
    PERSONA_HOME_ADMIN,
    PERSONA_NEW_USER,
    PERSONA_DASHBOARD_DESIGNER,
    PERSONA_MAINTENANCE,
    PERSONA_ESPHOME,
    PERSONA_CUSTOM,
})

# Domains whose services require cap_physical_control.
# Derived from the domain portion of PHYSICAL_GATE_SERVICES. Used by the native
# Hass* action tools (HassTurnOn/Off, HassSetPosition) to physical-gate targets
# the same way call_service gates the underlying domain services, so a valve is
# gated whether it is actuated via valve/open_valve or via HassTurnOn.
PHYSICAL_GATE_DOMAINS = frozenset({"lock", "alarm_control_panel", "cover", "valve"})

# The keys Home Assistant reads out of a service call's DATA to decide which
# entities the call reaches. Every one of them must be stripped from
# caller-supplied service data before the call is made, because Phoenix MCP
# resolves and flattens targets itself and then passes an explicit entity list:
# a selector left in the data is unioned with that list by HA, reaching entities
# the permission tree, the capability gates and MESA never saw.
#
# Not a stylistic strip. HA's helpers.target.TargetSelection reads exactly these
# five off ServiceCall.data, and its entity-service schemas accept all five, so a
# leftover floor_id or label_id actuates a whole floor for a token scoped to one
# entity.
#
# Deliberately NOT in this set:
#  - "target", which is a target block only at HA's own WS/REST boundary (which
#    merges it into data before the call). Reaching hass.services.async_call it
#    selects nothing, and it is a REAL data field for the notify domain, whose
#    target names a notification recipient.
#  - "config_entry_id", which TargetSelection does not read and which is a
#    legitimate argument to the config-entry services.
# Both are refused by MESA's own enforcement, which is a stricter rule applied to
# what MESA is shown rather than to what Home Assistant is asked to do.
#
# HA-VERSION-SENSITIVE: a const-invariant test pins this against HA's own
# cv.TARGET_SERVICE_FIELDS, so a release that adds a sixth selector fails loudly
# here rather than silently widening what a call can reach.
TARGET_SELECTOR_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id"})

# The ESPHome integration's domain. Its services are ALL dynamically registered
# per device (services.yaml is deliberately empty upstream): a device's firmware
# declares actions, and HA registers each as
# esphome.<device_name_with_underscores>_<action_name>. They take no entity
# target, so they need their own no-target dispatch path and are authorized by
# the owning device's write scope rather than a capability.
ESPHOME_DOMAIN = "esphome"

# Actionable service-name hints for core actuator domains whose real service
# verbs differ from the naive guess (e.g. valve.open_valve, not valve.open;
# cover.open_cover, not cover.open). When a call_service call raises
# ServiceNotFound on one of these domains, Phoenix MCP returns an invalid_request naming
# these valid services instead of the opaque generic "Forbidden.", so an agent
# that guessed the wrong verb can self-correct.
#
# LEAK SAFETY: this map is consulted ONLY inside the ServiceNotFound catch, which
# is strictly post-authorization (the token has already cleared the cap gate,
# entity-scope resolution proving WRITE on an entity in this domain, and MESA).
# The values are a HARDCODED list of public HA-core service names, never a live
# hass.services lookup, so the hint reveals nothing about custom/third-party
# services or about domains/entities the token cannot reach; it only names public
# core verbs for a domain the token already actuates. Domains absent from this
# map keep the generic no-oracle "Forbidden." response.
#
# HA-VERSION-SENSITIVE: these are HA-core service names, verified against HA
# 2026.7. Core rarely renames a service, but re-verify on HA upgrades (like the
# other HA-coupling points). Extend the map when a new verb-mismatch domain
# surfaces; only physical/actuator domains where the naive verb differs are
# included (light/switch/siren use the obvious turn_on/turn_off and are omitted).
DOMAIN_SERVICE_HINTS: dict[str, tuple[str, ...]] = {
    "valve": ("open_valve", "close_valve", "stop_valve", "set_valve_position"),
    "cover": (
        "open_cover", "close_cover", "stop_cover", "set_cover_position",
        "open_cover_tilt", "close_cover_tilt", "set_cover_tilt_position",
    ),
    "lock": ("lock", "unlock", "open"),
    "climate": (
        "set_temperature", "set_hvac_mode", "set_fan_mode", "set_preset_mode",
        "set_humidity", "set_swing_mode", "turn_on", "turn_off",
    ),
    "fan": (
        "turn_on", "turn_off", "set_percentage", "set_preset_mode",
        "oscillate", "set_direction", "increase_speed", "decrease_speed",
    ),
    "vacuum": (
        "start", "stop", "pause", "return_to_base", "clean_spot",
        "locate", "set_fan_speed", "send_command",
    ),
    "humidifier": ("turn_on", "turn_off", "set_humidity", "set_mode"),
    "water_heater": (
        "set_temperature", "set_operation_mode", "set_away_mode",
        "turn_on", "turn_off",
    ),
    "media_player": (
        "turn_on", "turn_off", "media_play", "media_pause", "media_stop",
        "media_next_track", "media_previous_track", "volume_set", "volume_mute",
        "select_source", "play_media",
    ),
    "alarm_control_panel": (
        "alarm_arm_away", "alarm_arm_home", "alarm_arm_night",
        "alarm_arm_vacation", "alarm_disarm", "alarm_trigger",
    ),
    "lawn_mower": ("start_mowing", "pause", "dock"),
}

# assist_satellite feature bit for ANNOUNCE support.
ANNOUNCE_BIT = 2

# vacuum feature bits (VacuumEntityFeature), mirrored rather than imported so
# nothing here pulls a component module into the import graph; the ANNOUNCE_BIT
# precedent. HA's own vacuum intents set required_features, so an entity that
# cannot perform the operation is never targeted rather than being asked and
# erroring, and the native tools mirror that.
# HA-VERSION-SENSITIVE: a renumbered bit would silently target the wrong vacuums
# while every test still passed, so tests/test_native_intent_parity.py pins these
# against the installed VacuumEntityFeature enum.
VACUUM_START_BIT = 8192
VACUUM_RETURN_HOME_BIT = 16
VACUUM_CLEAN_AREA_BIT = 16384

# Maximum time range for history and statistics queries.
MAX_HISTORY_RANGE_DAYS = 7

# Maximum number of log entries returned by the logs endpoint/tool.
MAX_LOG_ENTRIES = 100

# Cap on a tools/call request's "name" field before it is used for dispatch or
# logged as the audit method/resource. Every real tool name is well under this;
# it exists only to bound a malformed client (a local model emitting garbled
# tool-call syntax, e.g. stray XML-like tag debris or special tokens leaking
# into what should be a bare tool name) so the audit log cannot be bloated or
# made unreadable by an unbounded client-supplied string.
MAX_TOOL_NAME_LENGTH = 200

# Hard cap for the search_entities free-text query. Ranking tokenizes on
# whitespace and the fuzzy fallback runs difflib against every accessible
# entity, so an unbounded query could stall the event loop; a real query is far
# shorter than this.
# get_relationships: how many source paths one holder of a dangling reference
# reports before the rest become a count. Live-measured: a single deleted script
# was called from 18 sub-buttons on one dashboard, and repeating that dashboard's
# identity 18 times spent most of the response on nothing an operator needs. The
# true total is always reported alongside, per the truncation contract, so a
# clipped list is never mistaken for a complete one.
MAX_DANGLING_PATHS = 3

# compare_entities: how many members of a list-valued attribute are named per
# side before the rest become a count. A light's effect_list and a media
# player's source_list run to hundreds of entries, and echoing both sides in
# full answers nothing the added/removed pair does not. The true totals are
# reported alongside, per the truncation contract.
MAX_COMPARE_LIST_VALUES = 25

# compare_entities: how many characters of one string attribute value are
# shown. This bounds ONE value's length rather than a page of rows, so nothing
# a caller could have asked for separately is withheld.
MAX_COMPARE_VALUE_CHARS = 200

# wait_for_approval, PLURAL form only: how many characters of one approval's
# tool result are echoed. A batch is a list of STATUSES, not a list of results.
# Twelve approved `edit_automation` records each echo their whole config, which
# measured 80KB live and blew the caller's output limit, so the answer to "did
# my twelve writes land" could not be delivered at all. An ERROR's text is the
# part that has to survive, and it is short: an agent told only "rejected",
# with the executor's reason buried, retries the same doomed call. A success
# only needs to say it succeeded. The single-id form stays unbounded and is
# where a full result is read.
MAX_APPROVAL_RESULT_CHARS = 500

MAX_SEARCH_QUERY_LEN = 512

# Hard cap (and default) for the bounded watch_entity tool.
# It blocks the tool call up to this many seconds waiting for a state change.
MAX_SUBSCRIPTION_SECONDS = 30

# A token's inline wait (seconds) on a confirm gate: a confirm-gated call holds
# the response open up to confirm_inline_wait_seconds for an admin decision
# before falling back to the immediate pending_approval reply. 0 means off,
# return immediately, and it is now BOTH the default and the panel's first
# option. The max is kept well within typical MCP client timeouts so the held
# response never outlives one.
#
# THE DEFAULT IS OFF, and that is a deliberate reversal (it was 60). Blocking is
# automatic behaviour decided by a TOKEN SETTING, but the party that knows
# whether the outcome is needed right now is the AGENT: firing twenty writes it
# needs them queued, not resolved one at a time; turning off one lock it does
# want the answer. Holding the request removes that choice, and because the hold
# blocks a whole request while tool calls arrive one at a time, approval N+1
# cannot even be CREATED until approval N stops waiting. Live-measured at the old
# 60s default, consecutive approvals landed 62.8 SECONDS apart, so a twenty-write
# change took twenty minutes just to reach the operator's queue.
#
# The architecture already answers this properly: return pending_approval at once
# and let the agent call wait_for_approval (now accepting several ids) if and when
# it wants the outcomes. Approvals then STAGE immediately and the operator clears
# them however suits, individually or in one batch. The inline wait stays
# available for an operator who prefers an external client to block on a single
# action, but it is opt-in rather than the thing everyone silently pays for.
# How often the MCP endpoint writes to an SSE-framed response while the request
# is still being handled. A confirm gate can hold a response for up to
# MAX_CONFIRM_INLINE_WAIT_SECONDS with nothing on the wire, which is exactly the
# shape a reverse proxy drops; a frame every 15s keeps the connection alive and,
# when the client supplied a progressToken, tells it what the hold is waiting for.
MCP_SSE_KEEPALIVE_SECONDS = 15.0

# The MCP protocol revisions this transport implements, newest first.
# server/discover returns the whole list; initialize echoes the client's
# requested version when it appears here and the first entry otherwise.
#
# ADDING AN ENTRY IS A COMPLIANCE CLAIM, NOT A LABEL: a client is entitled to
# assume every MUST that revision introduces is honored here, and the only thing
# stopping a wrong claim is this comment. Two revisions are deliberately ABSENT
# even though much of Phoenix MCP already matches them. 2025-06-18 requires the
# server to validate the MCP-Protocol-Version header and reject an unsupported
# value with HTTP 400; this transport ignores that header entirely, and it also
# still accepts the JSON-RPC batches that revision removed. 2026-07-28 requires
# server-side header/body validation (Mcp-Method, Mcp-Name, Mcp-Param-*), a
# resultType on every result, and cache fields on every list result, none of
# which exist yet. Claiming either today would make a conforming client trust
# checks that do not run.
MCP_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-03-26",)
MCP_PROTOCOL_VERSION_PREFERRED = MCP_PROTOCOL_VERSIONS[0]

# Freshness hint (milliseconds) on the server/discover result. Nothing in it is
# enforcement: identity and the version list are fixed for a build, and the
# instructions are advisory prose that every cap gate re-derives on the call
# itself, so a client holding a stale copy can be wrong about what to SAY and
# never about what it is allowed to DO.
MCP_DISCOVER_TTL_MS = 300_000

MIN_CONFIRM_INLINE_WAIT_SECONDS = 30
MAX_CONFIRM_INLINE_WAIT_SECONDS = 180
DEFAULT_CONFIRM_INLINE_WAIT_SECONDS = 0

# search_entities similarity cutoff (difflib ratio, 0.0-1.0) for the typo-tolerant
# fallback pass. Only consulted when exact token-AND matching returns nothing, so
# it never changes results for a query that already matches; a candidate must
# score at least this against the entity_id, object_id, or friendly_name to show.
SEARCH_FUZZY_MATCH_CUTOFF = 0.6

# Directories (relative to the HA config dir) the cap_filesystem tools may touch.
# Paths are realpath-resolved and must stay within one of these, blocking traversal.
FILESYSTEM_ALLOWED_DIRS = ("www", "themes", "custom_templates")
# Maximum file size (bytes) the filesystem read/write tools will handle inline.
MAX_FILE_BYTES = 1_048_576

# configuration.yaml subtrees set_yaml_config may never add, remove, or modify,
# as {top-level key: protected child keys}. Not operator-configurable, and not a
# general "powerful keys" list: command_line, shell_command and rest stay writable
# because command execution and outbound HTTP are already accepted surface for a
# token holding cap_yaml_edit. These four are different in kind, because each one
# redefines Home Assistant's OWN trust boundary rather than acting within it:
#   homeassistant: auth_providers / auth_mfa_modules change how the instance
#     authenticates anyone, and packages redirects which folder is loaded as
#     configuration, i.e. the very surface this tool is bounded by (a token that
#     can also write www/ could then point packages at content it controls).
#   http: trusted_proxies + use_x_forwarded_for turn a spoofable X-Forwarded-For
#     header into an authentication bypass; cors_allowed_origins, ip_ban_enabled
#     and login_attempts_threshold disarm the brute-force and origin defenses.
#   frontend: extra_module_url and lovelace: resources load arbitrary JavaScript
#     into the authenticated dashboard, which is a stored-XSS foothold with access
#     to the instance and its tokens.
# An admin edits these by hand. Enforced by mcp_view._yaml_protected_check on both
# the pre-gate and the apply-time path; unchanged values always pass.
YAML_PROTECTED_SUBTREES: dict[str, tuple[str, ...]] = {
    "homeassistant": ("auth_providers", "auth_mfa_modules", "packages"),
    "http": (
        "trusted_proxies", "use_x_forwarded_for", "cors_allowed_origins",
        "ip_ban_enabled", "login_attempts_threshold",
    ),
    "frontend": ("extra_module_url",),
    "lovelace": ("resources",),
}


# Approval diff summaries, as format templates keyed by a stable slug.
#
# The summary an admin reads before approving is the confirm gate's whole safety
# property, so it has to be translatable. It is also PERSISTED on the approval
# record (it doubles as the audit trail), which means translating it server-side
# would freeze the language at creation time and make History mixed after a
# language switch. So the record carries all three: the English `summary` exactly
# as before, plus `summary_key` and `summary_params` for the panel to render in
# the operator's own language, falling back to the stored English for any record
# written before this existed.
#
# The template lives HERE and nowhere else. Python formats it to produce the
# stored English, and scripts/gen_diff_catalog.py generates the panel's
# translations/en.json `diff.*` entries FROM this dict, so the two cannot drift.
# tests/test_diff_summary_contract.py pins that generation and checks every
# builder's emitted key and params against it.
#
# Placeholders are `{name}` only: that is what both str.format and the panel's
# interpolation understand, and what HA's own translation loader validates a
# translated string against. A variant suffix (.mesa, .section, .device) is a
# SEPARATE key rather than a runtime concatenation, because a translator has to
# be able to move the clause.
DIFF_SUMMARY_TEMPLATES: dict[str, str] = {
    # Blueprints. Create never reports consumers (nothing uses it yet).
    "blueprint.create": "Create {domain} blueprint '{rel}'",
    "blueprint.edit": "Replace {domain} blueprint '{rel}'",
    "blueprint.delete": "Delete {domain} blueprint '{rel}'",
    "blueprint.edit.consumers": "Replace {domain} blueprint '{rel}' ({count} will be reloaded)",
    "blueprint.delete.consumers": "Delete {domain} blueprint '{rel}' ({count} still use it)",
    # Entity registry.
    "set_entity": "Update registry metadata ({fields}) for {entity_id}",
    "delete_entity": "Delete the registry entry for {entity_id}",
    # Zigbee. The bare forms are the error paths, where no device resolved.
    "zigbee_permit": "Open the Zigbee network for joining",
    "zigbee_permit.close": "Close the Zigbee join window",
    "zigbee_permit.open": "Open the Zigbee network for joining for {duration} seconds",
    "zigbee_reconfigure": "Reconfigure a Zigbee device",
    "zigbee_reconfigure.device": "Reconfigure (re-interview) Zigbee device {label}",
    "zigbee_remove": "Remove a Zigbee device",
    "zigbee_remove.device": "Remove Zigbee device {label} from the network",
    # System.
    "restart_ha": "Restart Home Assistant",
    "set_yaml_config": "Replace configuration.yaml",
    "set_yaml_config.removing": "Replace configuration.yaml, removing the top-level {keys}",
    "patch_yaml_config.set": "Set {path} in configuration.yaml",
    "patch_yaml_config.remove": "Remove {path} from configuration.yaml",
    "config_entry.options": "Change {keys} on helper '{label}'",
    "write_file": "Write file '{path}'",
    "integration.enable": "Enable integration {label}",
    "integration.disable": "Disable integration {label}",
    "create_backup": "Create backup",
    "create_backup.named": 'Create backup "{name}"',
    # ESPHome.
    "set_esphome_yaml": "Edit ESPHome device configuration {rel}. This does not flash the device.",
    "set_esphome_yaml.device": (
        "Edit ESPHome device configuration {rel} ({device}, {count} entities). "
        "This does not flash the device."
    ),
    "delete_esphome_yaml": "Delete ESPHome device configuration {rel}. The device itself is not touched.",
    "delete_esphome_yaml.device": (
        "Delete ESPHome device configuration {rel} ({device}). The device itself is not touched."
    ),
    "rename_esphome_device": "Rename ESPHome device {device} to {new_name}. This compiles and flashes the device.",
    "rename_esphome_device.services": (
        "Rename ESPHome device {device} to {new_name}, renaming {count} action service(s). "
        "This compiles and flashes the device."
    ),
    "install_esphome_firmware": "Flash ESPHome firmware to {label}",
    "install_esphome_firmware.jump": "Flash ESPHome firmware to {label} (ESPHome version change: {jump})",
    # Dashboards. The card ops spell out the location rather than passing a
    # pre-joined "view N, section M" fragment, so word order stays translatable.
    "set_dashboard_config": "Set dashboard layout '{label}'",
    "dashboard.create": "Create dashboard '{label}'",
    "dashboard.edit": "Edit dashboard '{label}'",
    "dashboard.delete": "Delete dashboard '{label}'",
    "dashboard_card.add": "Add '{card_type}' to dashboard '{label}' (view {view_index})",
    "dashboard_card.add.section": "Add '{card_type}' to dashboard '{label}' (view {view_index}, section {section_index})",
    "dashboard_card.edit": "Replace card {card_index} on dashboard '{label}' (view {view_index})",
    "dashboard_card.edit.section": "Replace card {card_index} on dashboard '{label}' (view {view_index}, section {section_index})",
    "dashboard_card.delete": "Delete card {card_index} from dashboard '{label}' (view {view_index})",
    "dashboard_card.delete.section": "Delete card {card_index} from dashboard '{label}' (view {view_index}, section {section_index})",
    # Energy dashboard. Each names the human-visible thing, never the JSON path:
    # an operator reviewing these knows their appliances, not HA's prefs schema.
    "edit_energy_config.replace_statistic": "Energy: point '{label}' at {new_statistic} (was {old_statistic})",
    "edit_energy_config.add_device": "Energy: track '{label}' ({new_statistic}) as an individual device",
    "edit_energy_config.remove_device": "Energy: stop tracking '{label}' ({old_statistic})",
    "edit_energy_config.rename_device": "Energy: rename '{old_label}' to '{label}'",
    "edit_energy_config.remove_source": "Energy: remove the {source_type} source ({old_statistic})",
    "edit_energy_config.set_source.update": "Energy: update the {source_type} source ({fields})",
    "edit_energy_config.set_source.create": "Energy: add a {source_type} source ({fields})",
    "patch_dashboard.set": "Set {path} on dashboard '{label}'",
    "patch_dashboard.append": "Append to {path} on dashboard '{label}'",
    "patch_dashboard.remove": "Remove {path} from dashboard '{label}'",
    # Service calls and the native physical-gate tools.
    "call_service": "Call {domain}/{service}",
    "call_service.mesa": "Call {domain}/{service} (includes MESA confirmation)",
    "hass_turn.on": "Turn on physical device(s): {names}",
    "hass_turn.off": "Turn off physical device(s): {names}",
    "hass_turn.on.mesa": "Turn on physical device(s): {names} (includes MESA confirmation)",
    "hass_turn.off.mesa": "Turn off physical device(s): {names} (includes MESA confirmation)",
    "hass_set_position": "Set cover/valve position",
    "hass_set_position.mesa": "Set cover/valve position (includes MESA confirmation)",
    "hass_stop_moving": "Stop moving cover",
    "hass_stop_moving.mesa": "Stop moving cover (includes MESA confirmation)",
    "mesa_service": "MESA confirmation for {domain}.{service}",
    # Authoring.
    "create_automation": "Create automation '{alias}'",
    "edit_automation": "Edit automation '{automation_id}'",
    "delete_automation": "Delete automation '{automation_id}'",
    "create_script": "Create script '{script_id}'",
    "edit_script": "Edit script '{script_id}'",
    "delete_script": "Delete script '{script_id}'",
    "create_scene": "Create scene '{name}'",
    "edit_scene": "Edit scene '{scene_id}'",
    "delete_scene": "Delete scene '{scene_id}'",
    "create_helper": "Create {helper_type} helper '{name}'",
    "edit_helper": "Edit {helper_type} helper '{helper_id}'",
    "delete_helper": "Delete {helper_type} helper '{helper_id}'",
}


# Version-record summaries, the Changes tab's one-line "what changed" text.
#
# Same contract as DIFF_SUMMARY_TEMPLATES above and generated into the same
# catalog by the same script, but a separate surface: an approval diff describes
# a change being PROPOSED, a version record describes one already APPLIED, and
# only some resource types get one at all (an automation's alias already tells
# the story, so those stay None).
#
# The card-op location is assembled from the three loc* parts joined with the
# panel's list separator, rather than one key per combination of view/section/
# card: it is a coordinate list, not prose, so the order is fixed and only the
# nouns need translating.
VERSION_SUMMARY_TEMPLATES: dict[str, str] = {
    "cards": "{count} cards",
    "cards.was": "{count} cards (was {before})",
    "size": "{size}",
    "size.was": "{size} (was {before})",
    "entity.removed": "registry entry removed",
    "entity.changed": "changed: {fields}",
    "card.added": "added {subject} ({where})",
    "card.edited": "edited {subject} ({where})",
    "card.deleted": "deleted {subject} ({where})",
    "loc.view": "view {index}",
    "loc.section": "section {index}",
    "loc.card": "card {index}",
    "energy.sources": "{count} sources, {devices} devices",
    "config_entry.options": "reconfigured {subject}",
    "patch.set": "set {subject}",
    "patch.append": "appended to {subject}",
    "patch.remove": "removed {subject}",
}


# Persistent-notification text, the only Phoenix strings HA itself renders
# outside the panel.
#
# Server-rendered and broadcast to every admin, so there is no per-viewer
# language to key off: these resolve against hass.config.language, the
# instance's own setting. Same {name} placeholder rule as the summary templates,
# and generated into the catalog by the same script.
NOTIFICATION_TEMPLATES: dict[str, str] = {
    "approval.title": "Phoenix MCP",
    # The link label is inside the markdown, so it has to be part of the string;
    # the URL is substituted and must not be translated.
    "approval.message": "Token '{token}' requested approval.\n\n[Review in Phoenix MCP]({url})",
    "rate_limit.title": "Phoenix MCP Alert",
    "rate_limit.message": "Phoenix MCP: token '{token}' has hit its rate limit.",
}

# The sentences the voice agent speaks when it declines a turn. Unlike a
# notification (one stored string shown to every admin, so it follows the
# SERVER language), these are spoken back to whoever is talking, so they
# resolve against the CONVERSATION's own language.
#
# `unavailable` deliberately does not say WHY. It covers the kill switch, an
# admin-disabled agent, and shutdown with one sentence, and the kill switch is
# a security control: naming it would announce the gateway's state to anyone
# within earshot of the satellite.
VOICE_TEMPLATES: dict[str, str] = {
    "unavailable": "The Phoenix MCP voice agent is currently unavailable.",
    "not_configured": "The Phoenix MCP voice agent is not fully configured.",
    "token_unavailable": "The Phoenix MCP voice agent's token is no longer available.",
    "error": "Sorry, the Phoenix MCP voice agent hit an error.",
    # How a turn that did not finish cleanly is SPOKEN. These were English
    # literals inside the loop until the turn-outcome split, so a Chinese
    # pipeline heard the four declines above translated and then an English
    # sentence the moment anything went wrong. The loop now reports an outcome
    # code and each surface renders it, which is what lets these live here.
    # {reason} is the provider's own error text and stays in whatever language
    # the provider produced: it is third-party output, not ours to translate.
    "could_not_complete": "Sorry, I could not complete that.",
    "provider_error": "Sorry, {reason}.",
    "output_limit": "Sorry, the model produced too much output, so I stopped.",
    "out_of_steps": "Sorry, I ran out of steps before finishing that.",
    # Appended after a real answer, so it is a follow-on sentence, not a reply.
    "answer_truncated": "I had to stop before finishing.",
}
