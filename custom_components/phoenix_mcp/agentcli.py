"""agentCLI: the in-panel LLM chat that runs an agentic loop server-side.

Phoenix MCP itself drives an LLM from a configured provider account on behalf
of a chosen Phoenix MCP token, calling that token's scoped Home Assistant tools through
the same in-process dispatch a real MCP client uses, so every capability gate,
MESA check, approval, and audit entry fires identically. The loop streams its
progress to the panel over Server-Sent Events; the browser owns the (ephemeral)
transcript and re-sends it each turn, so this module keeps no conversation state.

Constrained to no external Python dependency, so both the Anthropic
Messages API and the OpenAI-compatible provider APIs are spoken over
raw aiohttp via HA's shared client session, parsing streaming SSE by hand. The
provider API key is loaded from a dedicated secrets Store and is never logged,
persisted into a transcript, or echoed in an error payload.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
from datetime import datetime, timedelta, tzinfo
import ipaddress
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, cast
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .view_base import PhoenixView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import parse_datetime, utcnow

from .admin_view import _err, _ok, _read_body, require_admin
from .approvals import (
    REASON_AGENT_CHAT_ENDED,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    dismiss_approval_notification,
    fire_approval_resolved_event,
    get_approval,
    async_update_approval_status,
)
from .const import (
    AGENTCLI_ANTHROPIC_VERSION,
    AGENTCLI_APPROVAL_WAIT_SECONDS,
    AGENTCLI_PROGRESS_INTERVAL_SECONDS,
    AGENTCLI_CLAUDE_BASE_URL,
    AGENTCLI_CLAUDE_DEFAULT_MODEL,
    AGENTCLI_CLIENT_IP,
    AGENTCLI_CAPABILITY_CONCURRENCY,
    AGENTCLI_EFFORT_LEVEL_ORDER,
    AGENTCLI_LEARNABLE_OPTIONS,
    AGENTCLI_LEARNED_REFUSAL_TTL_DAYS,
    AGENTCLI_PROBE_MAX_CALLS,
    AGENTCLI_PROBE_SENTINEL,
    AGENTCLI_DEEPSEEK_BASE_URL,
    AGENTCLI_DEEPSEEK_DEFAULT_MODEL,
    AGENTCLI_GEMINI_BASE_URL,
    AGENTCLI_GEMINI_DEFAULT_MODEL,
    AGENTCLI_GROK_BASE_URL,
    AGENTCLI_GROK_DEFAULT_MODEL,
    AGENTCLI_GROQ_BASE_URL,
    AGENTCLI_KIMI_BASE_URL,
    AGENTCLI_KIMI_DEFAULT_MODEL,
    AGENTCLI_META_BASE_URL,
    AGENTCLI_META_DEFAULT_MODEL,
    AGENTCLI_MINIMAX_BASE_URL,
    AGENTCLI_MINIMAX_DEFAULT_MODEL,
    AGENTCLI_MINIMAX_MODELS,
    AGENTCLI_MISTRAL_BASE_URL,
    AGENTCLI_CEREBRAS_BASE_URL,
    AGENTCLI_FIREWORKS_BASE_URL,
    AGENTCLI_NVIDIA_BASE_URL,
    AGENTCLI_NVIDIA_DEFAULT_MODEL,
    AGENTCLI_OLLAMA_CLOUD_BASE_URL,
    AGENTCLI_OPENAI_BASE_URL,
    AGENTCLI_OPENROUTER_BASE_URL,
    AGENTCLI_TOGETHER_BASE_URL,
    AGENTCLI_ZAI_BASE_URL,
    AGENTCLI_ZAI_CODING_BASE_URL,
    AGENTCLI_DEEPSEEK_MAX_TOKENS,
    AGENTCLI_DEFAULT_EFFORT,
    AGENTCLI_DEFAULT_MAX_TOKENS,
    AGENTCLI_EFFORT_LEVELS,
    AGENTCLI_MAX_ITERATIONS,
    AGENTCLI_MAX_SSE_BUFFER_BYTES,
    AGENTCLI_MAX_STREAM_BYTES,
    AGENTCLI_MAX_TURN_OUTPUT_BYTES,
    AGENTCLI_STREAM_READ_TIMEOUT_SECONDS,
    AGENTCLI_SECRETS_STORAGE_KEY,
    AGENTCLI_SECRETS_STORAGE_VERSION,
    AGENTCLI_TOOL_RESULT_MAX_CHARS,
    AI_TASK_CLIENT_IP,
    CONVERSATION_STYLE_DIRECT,
    DETAIL_LEVEL_CONCISE,
    DOMAIN,
    MAX_TOOL_NAME_LENGTH,
    VOICE_AGENT_CLIENT_IP,
)
from .data import PhoenixData
from .helpers import canonical_language, get_client_ip, token_has_write_scope, voice_text
from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)

# Fast-failing timeout for the setup/validation probes so an unreachable host
# does not hang the panel for the OS default (~20s).
_PROBE_TIMEOUT = ClientTimeout(total=10, connect=5, sock_connect=5)
# Chat requests stream and can legitimately run for minutes, so bound only the
# connection, not the total time. The connect bound is kept modest so a transient
# DNS/connect failure fails fast enough to be retried within the retry window.
_CHAT_TIMEOUT = ClientTimeout(
    total=None, connect=8, sock_connect=8,
    sock_read=AGENTCLI_STREAM_READ_TIMEOUT_SECONDS,
)
# A transient connection failure (e.g. a DNS blip) before any content is streamed
# is retried with this backoff until the total window elapses, then surfaced.
_CONNECT_RETRY_WINDOW_SECONDS = 12.0
_CONNECT_RETRY_BACKOFF_SECONDS = 1.5
# The Settings "Validate"/probe step retries a transient connection failure (e.g. a
# DNS blip that fails within a second) a few times with a short backoff, so a
# one-off hiccup does not reject a good credential. An auth rejection or a real HTTP
# error is never retried.
_VALIDATE_ATTEMPTS = 3
_VALIDATE_RETRY_BACKOFF_SECONDS = 0.6
# Idle keepalive cadence for the SSE stream, so an intermediary does not drop the
# connection during a quiet stretch (model thinking, a pending approval).
AGENTCLI_HEARTBEAT_SECONDS = 15.0

# Normalized streaming-event types yielded by every provider, so the loop is
# provider-agnostic.
EV_TEXT = "text_delta"
EV_THINKING = "thinking_delta"
EV_TOOL = "tool_use_complete"
EV_DONE = "message_done"
EV_ERROR = "provider_error"
# Provider-reported token usage for the CURRENT model call, cumulative-replace
# semantics: each event carries the latest known totals for that one call, so a
# consumer keeps the newest event's numbers rather than summing events. Counts
# are exact from the provider; Phoenix MCP never estimates tokens itself.
EV_USAGE = "usage_update"


# --------------------------------------------------------------------------- #
# Secrets / provider configuration store
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProviderConfig:
    """Resolved config for one provider request (secret + non-secret combined)."""

    kind: str
    model: str
    base_url: str
    api_key: str | None = None
    # Tri-state: True is explicit provider/model evidence, False is explicit
    # refusal, and None means unknown. Unknown must remain text-only.
    vision: bool | None = None
    endpoint_id: str | None = None


@dataclass(frozen=True)
class ProbeConfigError:
    """A Phoenix-authored setup error with its panel localization contract."""

    message: str
    key: str
    params: dict[str, str | int] | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "message_key": f"adminError.{self.key}",
            "models": [],
        }
        if self.params:
            out["message_params"] = self.params
        return out


@dataclass(frozen=True)
class ProviderChoice:
    """One safe, non-secret choice rendered by a provider setup field."""

    value: str
    label: str
    label_key: str | None = None
    base_url: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "label": self.label}
        if self.label_key:
            out["label_key"] = self.label_key
        return out


@dataclass(frozen=True)
class ProviderField:
    """One declarative credential/setup field for the shared panel form."""

    id: str
    type: str
    label_key: str
    required: bool = True
    placeholder: str | None = None
    choices: tuple[ProviderChoice, ...] = ()

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "type": self.type, "label_key": self.label_key,
            "required": self.required,
        }
        if self.placeholder:
            out["placeholder"] = self.placeholder
        if self.choices:
            out["choices"] = [choice.public() for choice in self.choices]
        return out


@dataclass(frozen=True)
class ProviderDefinition:
    """Immutable provider registration, including protocol and setup policy."""

    kind: str
    label: str
    adapter: str
    base_url: str
    model: str = ""
    label_key: str | None = None
    fields: tuple[ProviderField, ...] = ()
    endpoints: tuple[ProviderChoice, ...] = ()
    include_usage: bool = False
    reasoning: str = "effort"
    model_filter: str = "chat"
    models_shape: str = "data"
    validation_error: str | None = None
    ollama: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind, "label": self.label,
            "fields": [field.public() for field in self.fields],
        }
        if self.label_key:
            out["label_key"] = self.label_key
        return out


_API_KEY_FIELD = ProviderField(
    "api_key", "secret", "settings.agentcliApiKey",
)
_OLLAMA_URL_FIELD = ProviderField(
    "base_url", "url", "settings.agentcliServerUrl", placeholder="http://host:11434",
)
_QWEN_URL_FIELD = ProviderField(
    "base_url", "url", "settings.agentcliBaseUrl",
    placeholder="https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
)
_ZAI_ENDPOINTS = (
    ProviderChoice(
        "standard", "Standard API", "settings.agentcliZaiStandard",
        AGENTCLI_ZAI_BASE_URL,
    ),
    ProviderChoice(
        "coding", "Coding Plan", "settings.agentcliZaiCoding",
        AGENTCLI_ZAI_CODING_BASE_URL,
    ),
)
_ZAI_ENDPOINT_FIELD = ProviderField(
    "endpoint_id", "choice", "settings.agentcliZaiPlan",
    choices=_ZAI_ENDPOINTS,
)

_PROVIDER_LABEL_KEYS = {
    "claude": "settings.providerAnthropic",
    "deepseek": "settings.providerDeepSeek",
    "chatgpt": "settings.providerOpenAI",
    "gemini": "settings.providerGemini",
    "grok": "settings.providerGrok",
    "groq": "settings.providerGroq",
    "kimi": "settings.providerKimi",
    "meta": "settings.providerMeta",
    "minimax": "settings.providerMiniMax",
    "mistral": "settings.providerMistral",
    "nvidia": "settings.providerNvidia",
    "ollama_cloud": "settings.providerOllamaCloud",
    "ollama": "settings.providerOllamaLocal",
    "openrouter": "settings.providerOpenRouter",
    "together": "settings.providerTogether",
    "cerebras": "settings.providerCerebras",
    "fireworks": "settings.providerFireworks",
    "qwen": "settings.providerQwen",
    "zai": "settings.providerZai",
}


def _definition(
    kind: str, label: str, base_url: str, *, adapter: str = "openai",
    model: str = "", label_key: str | None = None,
    fields: tuple[ProviderField, ...] = (_API_KEY_FIELD,),
    endpoints: tuple[ProviderChoice, ...] = (), include_usage: bool = False,
    reasoning: str = "effort", model_filter: str = "chat",
    models_shape: str = "data", validation_error: str | None = None,
    ollama: str | None = None,
) -> ProviderDefinition:
    return ProviderDefinition(
        kind, label, adapter, base_url, model,
        label_key or _PROVIDER_LABEL_KEYS[kind], fields, endpoints,
        include_usage, reasoning, model_filter, models_shape, validation_error,
        ollama,
    )


PROVIDER_DEFINITIONS: dict[str, ProviderDefinition] = {
    item.kind: item for item in (
        _definition("claude", "Anthropic", AGENTCLI_CLAUDE_BASE_URL, adapter="anthropic", model=AGENTCLI_CLAUDE_DEFAULT_MODEL),
        _definition("deepseek", "DeepSeek", AGENTCLI_DEEPSEEK_BASE_URL, model=AGENTCLI_DEEPSEEK_DEFAULT_MODEL, include_usage=True, reasoning="deepseek"),
        _definition("chatgpt", "OpenAI", AGENTCLI_OPENAI_BASE_URL, include_usage=True, model_filter="openai"),
        _definition("gemini", "Gemini", AGENTCLI_GEMINI_BASE_URL, model=AGENTCLI_GEMINI_DEFAULT_MODEL, reasoning="gemini", model_filter="gemini"),
        _definition("grok", "Grok", AGENTCLI_GROK_BASE_URL, model=AGENTCLI_GROK_DEFAULT_MODEL),
        _definition("groq", "Groq", AGENTCLI_GROQ_BASE_URL, include_usage=True),
        _definition("kimi", "Kimi", AGENTCLI_KIMI_BASE_URL, model=AGENTCLI_KIMI_DEFAULT_MODEL, include_usage=True, reasoning="kimi"),
        _definition("meta", "Meta", AGENTCLI_META_BASE_URL, model=AGENTCLI_META_DEFAULT_MODEL, reasoning="meta"),
        _definition("minimax", "MiniMax", AGENTCLI_MINIMAX_BASE_URL, adapter="anthropic", model=AGENTCLI_MINIMAX_DEFAULT_MODEL),
        _definition("mistral", "Mistral AI", AGENTCLI_MISTRAL_BASE_URL, reasoning="mistral", model_filter="mistral"),
        _definition("nvidia", "NVIDIA", AGENTCLI_NVIDIA_BASE_URL, model=AGENTCLI_NVIDIA_DEFAULT_MODEL, reasoning="probed_effort", model_filter="nvidia"),
        _definition("ollama_cloud", "Ollama (cloud)", AGENTCLI_OLLAMA_CLOUD_BASE_URL, reasoning="ollama", ollama="cloud"),
        _definition("ollama", "Ollama (local)", "", fields=(_OLLAMA_URL_FIELD,), reasoning="ollama", ollama="local"),
        _definition("openrouter", "OpenRouter", AGENTCLI_OPENROUTER_BASE_URL, include_usage=True, reasoning="probed_effort", model_filter="openrouter"),
        _definition(
            "together", "Together", AGENTCLI_TOGETHER_BASE_URL,
            model_filter="together", models_shape="list",
        ),
        _definition("cerebras", "Cerebras", AGENTCLI_CEREBRAS_BASE_URL),
        _definition(
            "fireworks", "Fireworks", AGENTCLI_FIREWORKS_BASE_URL,
            validation_error="fireworks",
        ),
        _definition(
            "qwen", "Alibaba Cloud Model Studio / Qwen", "",
            fields=(_API_KEY_FIELD, _QWEN_URL_FIELD), include_usage=True,
            reasoning="qwen", validation_error="qwen_openai",
        ),
        _definition("zai", "Z.ai", AGENTCLI_ZAI_BASE_URL, fields=(_ZAI_ENDPOINT_FIELD, _API_KEY_FIELD), endpoints=_ZAI_ENDPOINTS, reasoning="zai"),
    )
}

# Compatibility projection for tests and internal code being moved to the typed
# registry. It contains no independent provider facts.
_KINDS: dict[str, dict[str, Any]] = {
    kind: {
        "label": definition.label,
        "keyless": not any(field.id == "api_key" for field in definition.fields),
        "base_url": definition.base_url,
        "model": definition.model,
    }
    for kind, definition in PROVIDER_DEFINITIONS.items()
}


def _provider_catalog() -> list[dict[str, Any]]:
    return [
        definition.public()
        for definition in sorted(PROVIDER_DEFINITIONS.values(), key=lambda item: item.label.lower())
    ]


def _default_base_url(kind: str, endpoint_id: str | None = None) -> str:
    definition = PROVIDER_DEFINITIONS.get(kind)
    if definition is None:
        return ""
    if definition.endpoints:
        selected = endpoint_id or definition.endpoints[0].value
        return next(
            (choice.base_url or "" for choice in definition.endpoints if choice.value == selected),
            "",
        )
    return definition.base_url


def _default_model(kind: str) -> str:
    definition = PROVIDER_DEFINITIONS.get(kind)
    return definition.model if definition else ""


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _instance_name(inst: dict, has_dupes: bool) -> str:
    """Display name: the kind label alone, or with a discriminator when there is
    more than one account of the same kind (last 4 of the key, or the Ollama host)."""
    kind = inst.get("kind", "")
    label = _KINDS.get(kind, {}).get("label", kind)
    if not has_dupes:
        return label
    if kind == "ollama":
        host = _host_of(inst.get("base_url", ""))
        return f"{label} ({host})" if host else label
    if kind == "qwen":
        host = _host_of(inst.get("base_url", ""))
        return f"{label} ({host})" if host else label
    if kind == "zai":
        endpoint_id = str(inst.get("endpoint_id") or "standard")
        endpoint = next(
            (choice.label for choice in _ZAI_ENDPOINTS if choice.value == endpoint_id),
            endpoint_id,
        )
        return f"{label} ({endpoint})"
    key = inst.get("api_key", "") or ""
    tail = key[-4:] if len(key) >= 4 else key
    return f"{label} ({tail})" if tail else label


def _with_learned(inst: dict) -> dict:
    """Declared and probed capabilities, with unexpired learned refusals on top.

    Expiry is applied HERE, on read, rather than by sweeping storage: a refusal
    that has aged out simply stops being returned, so nothing has to run on a
    timer and a clock change cannot leave a stale record behind. An unparseable
    timestamp is treated as expired, which fails toward offering the control
    rather than toward hiding it.
    """
    caps = {model: dict(vals) for model, vals in (inst.get("capabilities") or {}).items()
            if isinstance(vals, dict)}
    learned = inst.get("learned") or {}
    if not isinstance(learned, dict):
        return caps
    cutoff = utcnow() - timedelta(days=AGENTCLI_LEARNED_REFUSAL_TTL_DAYS)
    for model, options in learned.items():
        if not isinstance(options, dict):
            continue
        for option, at in options.items():
            cap = AGENTCLI_LEARNABLE_OPTIONS.get(option)
            when = parse_datetime(at) if isinstance(at, str) else None
            if cap is None or when is None or when < cutoff:
                continue
            caps.setdefault(model, {})[cap] = False
    return caps


class DuplicateProviderError(ValueError):
    """Raised when an account with the same provider identity already exists."""

    def __str__(self) -> str:
        return "This provider account is already configured."


def _normalized_provider_url(value: object) -> str:
    """Normalize an Ollama endpoint for duplicate detection only."""
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return raw.rstrip("/")
    netloc = hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunparse((
        parsed.scheme.lower(), netloc, parsed.path.rstrip("/"),
        parsed.params, parsed.query, parsed.fragment,
    )).rstrip("/")


def _provider_identity(kind: str, cfg: dict) -> tuple[str, str]:
    """Return the non-secret identity used to reject exact duplicate accounts."""
    if kind == "ollama":
        return kind, _normalized_provider_url(cfg.get("base_url"))
    if kind == "qwen":
        return kind, "\0".join((
            _normalized_provider_url(cfg.get("base_url")),
            str(cfg.get("api_key") or "").strip(),
        ))
    if kind == "zai":
        return kind, "\0".join((
            str(cfg.get("endpoint_id") or "standard"),
            str(cfg.get("api_key") or "").strip(),
        ))
    return kind, str(cfg.get("api_key") or "").strip()


class AgentCliStore:
    """Provider accounts ("instances") in a dedicated Store, separate from tokens.

    Shape: {"instances": {"<id>": {"kind": str, "api_key"?: str, "base_url"?: str,
    "model": str}}}. Multiple instances of the same kind are allowed when their
    credentials or Ollama endpoints differ. Keys are write-only; list_instances()
    never returns a secret.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._data: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def async_create(cls, hass: HomeAssistant) -> AgentCliStore:
        store: Store[dict] = Store(hass, AGENTCLI_SECRETS_STORAGE_VERSION, AGENTCLI_SECRETS_STORAGE_KEY)
        inst = cls(store)
        raw = await store.async_load() or {}
        if isinstance(raw, dict) and isinstance(raw.get("instances"), dict):
            inst._data = raw["instances"]
        elif isinstance(raw, dict) and isinstance(raw.get("providers"), dict):
            # Migrate the pre-instances shape (one config per kind) to instances.
            inst._data = {
                uuid.uuid4().hex: {"kind": kind, **cfg}
                for kind, cfg in raw["providers"].items() if isinstance(cfg, dict)
            }
            await inst._save()
        else:
            inst._data = {}
        return inst

    async def _save(self) -> None:
        await self._store.async_save({"instances": self._data})

    def get(self, instance_id: str) -> dict | None:
        cfg = self._data.get(instance_id)
        return dict(cfg) if isinstance(cfg, dict) else None

    def has_duplicate(self, kind: str, cfg: dict) -> bool:
        """Whether the same provider endpoint or credential is already stored."""
        identity = _provider_identity(kind, cfg)
        return bool(identity[1]) and any(
            _provider_identity(inst.get("kind", ""), inst) == identity
            for inst in self._data.values()
            if isinstance(inst, dict)
        )

    async def add(self, kind: str, cfg: dict) -> str:
        async with self._lock:
            if self.has_duplicate(kind, cfg):
                raise DuplicateProviderError
            instance_id = uuid.uuid4().hex
            self._data[instance_id] = {"kind": kind, **cfg}
            await self._save()
            return instance_id

    async def set_model(self, instance_id: str, model: str) -> bool:
        """Change one account's default model. False if the account is unknown.

        The default model was frozen at creation until now, so correcting it
        meant deleting the account and re-entering the API key. That is the wrong
        cost for a value that goes stale on the PROVIDER's schedule rather than
        the operator's: a shipped default id can be retired out from under a
        working install, which is exactly what happened to this integration's.

        Only the model is patchable. The credential and base_url are what the
        account IS, and re-validating them is what the add flow already does.
        """
        async with self._lock:
            cfg = self._data.get(instance_id)
            if not isinstance(cfg, dict):
                return False
            cfg["model"] = model
            await self._save()
            return True

    async def set_capabilities(
        self, instance_id: str, capabilities: dict, checked_at: str,
    ) -> bool:
        """Record what this account's provider DECLARED, with when it was asked.

        Stored per account rather than per kind because the same kind can differ
        by endpoint: two Ollama servers hold different libraries, and one
        operator's OpenRouter key is entitled to models another's is not.

        The timestamp is half the value. Capabilities have no invalidation signal
        of their own, so "last checked" is what turns a silently ageing answer
        into one the operator can judge.
        """
        async with self._lock:
            cfg = self._data.get(instance_id)
            if not isinstance(cfg, dict):
                return False
            cfg["capabilities"] = capabilities
            cfg["capabilities_checked_at"] = checked_at
            await self._save()
            return True

    async def record_refusal(self, instance_id: str, model: str, option: str, at: str) -> bool:
        """Remember that a real turn had this option refused for this model.

        Kept SEPARATE from `capabilities` rather than written into it, and that
        separation is what makes expiry possible at all: declared and probed
        facts are durable statements about the API, while this one is an
        observation from a single moment that has to fade. Merged over the
        durable facts at read time, so nothing here can corrupt them.
        """
        async with self._lock:
            cfg = self._data.get(instance_id)
            if not isinstance(cfg, dict) or not model:
                return False
            learned = dict(cfg.get("learned") or {})
            per_model = dict(learned.get(model) or {})
            per_model[option] = at
            learned[model] = per_model
            cfg["learned"] = learned
            await self._save()
            return True

    async def delete(self, instance_id: str) -> None:
        async with self._lock:
            if instance_id in self._data:
                del self._data[instance_id]
                await self._save()

    async def async_wipe(self) -> None:
        """Delete every provider account, including the recoverable API keys."""
        async with self._lock:
            self._data = {}
            await self._save()

    def list_instances(self) -> list[dict]:
        """Configured accounts for the panel; never includes a key."""
        counts = Counter(inst.get("kind") for inst in self._data.values())
        out = []
        for iid, inst in self._data.items():
            kind = inst.get("kind", "")
            out.append({
                "id": iid,
                "kind": kind,
                "name": _instance_name(inst, counts[kind] > 1),
                "model": inst.get("model", ""),
                "base_url": inst.get("base_url", ""),
                "endpoint_id": inst.get("endpoint_id") or None,
                "capabilities": _with_learned(inst),
                "capabilities_checked_at": inst.get("capabilities_checked_at") or None,
            })
        # Sort by the DISPLAY name, not the kind key: the panel dropdowns show
        # names, and the two diverge (kind "claude" displays as "Anthropic",
        # "chatgpt" as "OpenAI"), so keying on kind lists them out of order.
        out.sort(key=lambda x: (x["name"].lower(), x["id"]))
        return out

    def resolve(self, instance_id: str, model_override: str | None = None) -> ProviderConfig | None:
        """Build a ProviderConfig for one instance, or None if it is unknown.

        The model may be empty (Ollama is added before a model is chosen); the
        chat endpoint checks for a model separately.
        """
        cfg = self.get(instance_id)
        if cfg is None:
            return None
        kind = cfg.get("kind", "")
        endpoint_id = str(cfg.get("endpoint_id") or "") or None
        base_url = (
            cfg.get("base_url") or _default_base_url(kind, endpoint_id) or ""
        ).rstrip("/")
        if not base_url:
            return None
        model = model_override or cfg.get("model") or _default_model(kind)
        model_caps = _with_learned(cfg).get(model) or {}
        vision = model_caps.get("vision") if isinstance(model_caps.get("vision"), bool) else None
        return ProviderConfig(
            kind=kind, model=model, base_url=base_url, api_key=cfg.get("api_key"),
            endpoint_id=endpoint_id, vision=vision,
        )


async def _get_secret_store(hass: HomeAssistant) -> AgentCliStore:
    """Lazily create and cache the secrets store on hass.data (not on PhoenixData)."""
    key = "_phoenix_agentcli_store"
    inst = hass.data.get(key)
    if isinstance(inst, AgentCliStore):
        return inst
    lock = hass.data.setdefault("_phoenix_agentcli_store_lock", asyncio.Lock())
    async with lock:
        inst = hass.data.get(key)
        if not isinstance(inst, AgentCliStore):
            inst = await AgentCliStore.async_create(hass)
            hass.data[key] = inst
        return inst


async def async_wipe_agentcli_secrets(hass: HomeAssistant) -> None:
    """Erase all stored provider accounts and API keys from disk.

    Loads (or reuses the cached) store, so it wipes the on-disk file even when
    Agent Chat was never opened this session. The cached instance stays live but
    empty, so any open chat window loses its accounts immediately.
    """
    store = await _get_secret_store(hass)
    await store.async_wipe()


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

def _norm(ev_type: str, **fields: Any) -> dict:
    return {"type": ev_type, **fields}


def _usage_int(value: Any) -> int:
    """A provider-reported token count as a non-negative int; 0 for anything
    absent or malformed. bool is excluded explicitly (it subclasses int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


# Provider error bodies are JSON {"error": {"message", "type", "code"}} on both
# the Anthropic and OpenAI-compatible surfaces. Some backends put a bare string
# under "error" or a "message" at the top level. Pull out the human sentence and
# the machine code so the panel shows a clean line, never a raw JSON blob.
def _parse_provider_error(text: str) -> tuple[str, str | None]:
    raw = (text or "").strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw, None
    if not isinstance(obj, dict):
        return raw, None
    err = obj.get("error")
    if isinstance(err, str):
        return err.strip() or raw, None
    if isinstance(err, dict):
        msg = err.get("message") or ""
        code = err.get("code") or err.get("type")
        return (str(msg).strip() or raw), (str(code).lower() if code else None)
    if isinstance(obj.get("message"), str):
        return obj["message"].strip() or raw, None
    return raw, None


# Mistral documents a remaining-quota response header and currently publishes
# more granular numeric headers for token/request windows. Keep this an explicit
# allowlist: response headers are provider-controlled input, and no unrelated
# header should ever reach the operator or logs through an error event.
_MISTRAL_RATE_LIMIT_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("x-ratelimit-limit-tokens-minute", "tokens_minute_limit"),
    ("x-ratelimit-remaining-tokens-minute", "tokens_minute_remaining"),
    ("x-ratelimit-limit-tokens-5-minute", "tokens_five_minute_limit"),
    ("x-ratelimit-remaining-tokens-5-minute", "tokens_five_minute_remaining"),
    ("x-ratelimit-limit-tokens-month", "tokens_month_limit"),
    ("x-ratelimit-remaining-tokens-month", "tokens_month_remaining"),
    ("x-ratelimit-limit-req-minute", "requests_minute_limit"),
    ("x-ratelimit-limit-requests-minute", "requests_minute_limit"),
    ("x-ratelimit-remaining-req-minute", "requests_minute_remaining"),
    ("x-ratelimit-remaining-requests-minute", "requests_minute_remaining"),
    ("x-ratelimit-limit-req-second", "requests_second_limit"),
    ("x-ratelimit-limit-requests-second", "requests_second_limit"),
    ("x-ratelimit-remaining-req-second", "requests_second_remaining"),
    ("x-ratelimit-remaining-requests-second", "requests_second_remaining"),
    ("x-ratelimit-limit", "generic_limit"),
    ("x-ratelimit-remaining", "generic_remaining"),
    ("x-ratelimit-tokens-query-cost", "query_cost"),
    ("retry-after", "retry_after_seconds"),
)


def _mistral_rate_limit_headers(headers: Any) -> dict[str, int]:
    """Safe numeric Mistral quota diagnostics from one HTTP response."""
    try:
        raw = {str(name).lower(): str(value).strip() for name, value in headers.items()}
    except (AttributeError, TypeError, ValueError):
        return {}
    out: dict[str, int] = {}
    for header, field in _MISTRAL_RATE_LIMIT_HEADER_FIELDS:
        value = raw.get(header)
        if value is None or not value.isascii() or not value.isdecimal():
            continue
        parsed = int(value)
        # Preserve exact integers in the browser rather than silently rounding
        # an absurd/malformed provider value during JSON -> JavaScript parsing.
        if parsed <= 9_007_199_254_740_991:
            out.setdefault(field, parsed)
    return out


# Codes returned as HTTP 429/4xx that are NOT transient: a billing/quota/account
# problem will not clear by retrying, so surface it immediately instead of
# spending the connect-retry window (see _stream_turn_resilient) hammering it.
_NON_RETRYABLE_ERROR_CODES = frozenset({
    "insufficient_quota", "billing_hard_limit_reached", "billing_not_active",
    "account_deactivated", "access_terminated", "invalid_api_key",
})


async def _retry_transient(attempt: Callable[[], Awaitable[Any]]) -> Any:
    """Await attempt() with a bounded retry for a transient connection failure.

    A status-based result (whatever attempt() returns) is passed straight back, so
    an auth rejection or a real HTTP error is never retried; only a
    ClientError/TimeoutError is retried, briefly, and the last one is re-raised
    after the final try for the caller to turn into a friendly message. Used by the
    provider validate() probes so a one-off DNS/connect blip does not reject a good
    credential during the Settings Validate step.
    """
    last: Exception | None = None
    for i in range(_VALIDATE_ATTEMPTS):
        try:
            return await attempt()
        except (ClientError, asyncio.TimeoutError) as exc:
            last = exc
            if i + 1 < _VALIDATE_ATTEMPTS:
                await asyncio.sleep(_VALIDATE_RETRY_BACKOFF_SECONDS)
    assert last is not None
    raise last


async def _sse_lines(resp: Any) -> AsyncIterator[str]:
    """Yield decoded lines from a streaming HTTP response, buffering split frames.

    Two byte bounds guard against a faulty or hostile provider (memory
    exhaustion). The between-newline buffer is capped so a stream that never
    sends a newline cannot grow unbounded; the CUMULATIVE bytes for the whole
    response are also capped, so a stream of unlimited small newline-delimited
    frames (which downstream parsers append into text blocks and tool JSON) is
    bounded too. A real response is well under both. Exceeding either raises
    ClientError, which each provider's stream_turn turns into a clean chat error.
    The per-response cap bounds a turn at cap x AGENTCLI_MAX_ITERATIONS.
    """
    buffer = ""
    total = 0
    async for chunk in resp.content.iter_chunked(2048):
        total += len(chunk)
        if total > AGENTCLI_MAX_STREAM_BYTES:
            raise ClientError("Provider stream exceeded the maximum total size.")
        buffer += chunk.decode("utf-8", errors="replace")
        if len(buffer) > AGENTCLI_MAX_SSE_BUFFER_BYTES:
            raise ClientError("Provider stream frame exceeded the maximum size.")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    if buffer:
        yield buffer.rstrip("\r")


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class _ThinkSplitter:
    """Split a streamed content string into ("text"|"think", segment) pieces.

    Handles <think>...</think> blocks whose tags may be split across streamed
    chunks by holding back a short tail that could be the start of a tag.
    """

    def __init__(self) -> None:
        self._in_think = False
        self._carry = ""

    def _max_tail(self) -> int:
        return len(_THINK_CLOSE if self._in_think else _THINK_OPEN) - 1

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._carry += chunk
        while self._carry:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            kind = "think" if self._in_think else "text"
            idx = self._carry.find(tag)
            if idx == -1:
                # Emit everything except a possible partial tag at the tail.
                keep = self._max_tail()
                if len(self._carry) > keep:
                    seg = self._carry[:len(self._carry) - keep]
                    if seg:
                        out.append((kind, seg))
                    self._carry = self._carry[len(self._carry) - keep:]
                break
            if idx > 0:
                out.append((kind, self._carry[:idx]))
            self._carry = self._carry[idx + len(tag):]
            self._in_think = not self._in_think
        return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self._carry:
            out.append(("think" if self._in_think else "text", self._carry))
            self._carry = ""
        return out


def _apply_claude_options(body: dict, options: dict, kind: str = "claude") -> None:
    """Map the panel's generation options into an Anthropic-format request body.

    Claude gets the full surface (adaptive thinking with a display mode + an
    output_config effort). MiniMax's Anthropic-compatible API takes thinking on
    via `{type: adaptive}` but does not accept Anthropic's display/output_config
    extensions, so it gets only the plain adaptive toggle.
    """
    body["max_tokens"] = int(options.get("max_tokens") or AGENTCLI_DEFAULT_MAX_TOKENS)
    thinking = options.get("thinking", True)
    if thinking:
        if kind == "minimax":
            body["thinking"] = {"type": "adaptive"}
        else:
            # display=summarized streams a readable summary the panel can show;
            # omitted still lets the model think but streams no thinking text, so
            # when the operator has "show thinking" off we don't ship it at all.
            display = "summarized" if options.get("show_thinking") else "omitted"
            body["thinking"] = {"type": "adaptive", "display": display}
    if kind == "claude":
        effort = options.get("effort", AGENTCLI_DEFAULT_EFFORT)
        if effort in AGENTCLI_EFFORT_LEVELS:
            body["output_config"] = {"effort": effort}


def _cache_marked(system_prompt: str, messages: list[dict]) -> tuple[Any, list[dict]]:
    """Add Anthropic prompt-cache breakpoints (Claude only, never MiniMax).

    Two ephemeral markers: one on the system block, which caches the stable
    prefix (the tools array plus the system prompt, both large and unchanged
    across turns unless the token's caps change), and one on the last message
    block, so each agentic-loop iteration and each later turn reuses the prior
    message history instead of re-billing it. Markers go on copies only; the
    stored transcript round-trips through the browser and must stay clean.
    """
    marker = {"cache_control": {"type": "ephemeral"}}
    system = [{"type": "text", "text": system_prompt, **marker}]
    if not messages:
        return system, messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str) and content:
        marked = {**last, "content": [{"type": "text", "text": content, **marker}]}
    elif isinstance(content, list) and content:
        marked = {**last, "content": [*content[:-1], {**content[-1], **marker}]}
    else:
        return system, messages
    return system, [*messages[:-1], marked]


class ClaudeProvider:
    """Anthropic Messages API, streaming, over raw HTTP.

    Also backs MiniMax, whose API is Anthropic-compatible; kind-specific bits
    (auth header, thinking options, model listing) branch on self.kind.
    """

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.kind = cfg.kind

    def format_tools(self, mcp_tools: list[dict]) -> list[dict]:
        return [
            {"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("inputSchema", {"type": "object", "properties": {}})}
            for t in mcp_tools
        ]

    def append_assistant(self, messages: list[dict], assistant_msg: dict) -> None:
        messages.append(assistant_msg)

    def append_tool_results(self, messages: list[dict], results: list[dict]) -> None:
        # All tool_result blocks for a turn go in ONE user message.
        content = []
        for r in results:
            result_text = _model_result_text(r, self.cfg.vision is True)
            result_content: list[dict] = [{"type": "text", "text": result_text}]
            if self.cfg.vision is True:
                for image in r.get("images", []):
                    result_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image["mime_type"],
                            "data": image["data"],
                        },
                    })
            block: dict = {
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": result_content,
            }
            if r.get("is_error"):
                block["is_error"] = True
            content.append(block)
        messages.append({"role": "user", "content": content})

    async def validate(self, session: ClientSession) -> tuple[bool, str]:
        async def _probe() -> tuple[bool, str]:
            async with session.get(
                f"{self.cfg.base_url}/v1/models",
                headers=self._headers(), timeout=_PROBE_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return True, ""
                if resp.status in (401, 403):
                    return False, "API key rejected."
                if self.kind == "minimax":
                    # MiniMax's Anthropic-compatible base may not expose /v1/models;
                    # a non-auth error there does not mean the key is bad. Soft-accept
                    # (a real problem surfaces on the first message with a clean error).
                    return True, ""
                return False, f"Provider returned HTTP {resp.status}."
        try:
            return await _retry_transient(_probe)
        except (ClientError, asyncio.TimeoutError) as exc:
            return False, f"Could not reach the provider: {exc}"

    async def list_models(self, session: ClientSession) -> list[str]:
        fallback = list(AGENTCLI_MINIMAX_MODELS) if self.kind == "minimax" \
            else [AGENTCLI_CLAUDE_DEFAULT_MODEL]
        try:
            async with session.get(
                f"{self.cfg.base_url}/v1/models", headers=self._headers(), timeout=_PROBE_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return fallback
                body = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError):
            return fallback
        return [m["id"] for m in body.get("data", []) if m.get("id")] or fallback

    def _headers(self) -> dict:
        # MiniMax's Anthropic-compatible API authenticates with a Bearer token
        # rather than Anthropic's x-api-key; both send the anthropic-version.
        if self.kind == "minimax":
            return {
                "Authorization": f"Bearer {self.cfg.api_key or ''}",
                "anthropic-version": AGENTCLI_ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
        return {
            "x-api-key": self.cfg.api_key or "",
            "anthropic-version": AGENTCLI_ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def probe_option(self, session: ClientSession, extra: dict) -> int | None:
        """HTTP status for the smallest possible completion carrying `extra`.

        None when the provider could not be reached at all, which is not an
        answer about the option and must not be recorded as one.
        """
        body = {"model": self.cfg.model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}], **extra}
        try:
            async with session.post(f"{self.cfg.base_url}/v1/messages", headers=self._headers(),
                                    json=body, timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                return resp.status
        except (ClientError, asyncio.TimeoutError):
            return None

    async def list_model_capabilities(
        self, session: ClientSession, models: list[str],
    ) -> dict[str, dict]:
        """Anthropic's models endpoint reports no per-model parameters."""
        return {}

    async def stream_turn(
        self, session: ClientSession, *, system_prompt: str, messages: list[dict],
        tools: list[dict], options: dict,
    ) -> AsyncIterator[dict]:
        # Prompt caching is Anthropic-specific; MiniMax's compatible API is not
        # verified to accept cache_control, so it gets the plain uncached body.
        system, messages = (
            _cache_marked(system_prompt, messages) if self.kind == "claude"
            else (system_prompt, messages)
        )
        body = {
            "model": self.cfg.model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "stream": True,
        }
        _apply_claude_options(body, options, self.kind)
        try:
            async with session.post(
                f"{self.cfg.base_url}/v1/messages", headers=self._headers(),
                json=body, timeout=_CHAT_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    msg, err_code = _parse_provider_error((await resp.text())[:4000])
                    retryable = resp.status in (429, 500, 502, 503, 529)
                    if err_code in _NON_RETRYABLE_ERROR_CODES:
                        retryable = False
                    yield _norm(EV_ERROR, status=resp.status,
                                code=_claude_err_code(resp.status),
                                message=msg[:600], retryable=retryable,
                                refused=_refused_option(resp.status, msg, body))
                    return
                async for ev in self._parse(resp):
                    yield ev
        except (ClientError, asyncio.TimeoutError) as exc:
            yield _norm(EV_ERROR, status=0, code="network", message=str(exc), retryable=True)

    async def _parse(self, resp: Any) -> AsyncIterator[dict]:
        # Assemble content blocks by index into an assistant message.
        blocks: dict[int, dict] = {}
        tool_json: dict[int, str] = {}
        order: list[int] = []
        stop_reason = "end_turn"
        # message_start reports the call's input tokens; the closing
        # message_delta reports the final cumulative output tokens.
        input_tokens = 0
        async for line in _sse_lines(resp):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "content_block_start":
                idx = ev.get("index", 0)
                cb = ev.get("content_block", {})
                order.append(idx)
                if cb.get("type") == "tool_use":
                    blocks[idx] = {"type": "tool_use", "id": cb.get("id"), "name": cb.get("name"), "input": {}}
                    tool_json[idx] = ""
                elif cb.get("type") == "thinking":
                    blocks[idx] = {"type": "thinking", "thinking": ""}
                else:
                    blocks[idx] = {"type": "text", "text": ""}
            elif etype == "content_block_delta":
                idx = ev.get("index", 0)
                delta = ev.get("delta", {})
                dtype = delta.get("type")
                if dtype == "text_delta":
                    blocks.setdefault(idx, {"type": "text", "text": ""})
                    blocks[idx]["text"] = blocks[idx].get("text", "") + delta.get("text", "")
                    yield _norm(EV_TEXT, text=delta.get("text", ""))
                elif dtype == "thinking_delta":
                    yield _norm(EV_THINKING, text=delta.get("thinking", ""))
                elif dtype == "input_json_delta":
                    tool_json[idx] = tool_json.get(idx, "") + delta.get("partial_json", "")
            elif etype == "content_block_stop":
                idx = ev.get("index", 0)
                blk = blocks.get(idx)
                if blk and blk.get("type") == "tool_use":
                    try:
                        blk["input"] = json.loads(tool_json.get(idx) or "{}")
                    except json.JSONDecodeError:
                        blk["input"] = {}
                    yield _norm(EV_TOOL, id=blk.get("id"), name=blk.get("name"), input=blk["input"])
            elif etype == "message_start":
                usage = (ev.get("message") or {}).get("usage") or {}
                if usage:
                    # Context actually sent = fresh input + cache writes + cache
                    # reads (all three are prompt tokens on the wire).
                    input_tokens = (
                        _usage_int(usage.get("input_tokens"))
                        + _usage_int(usage.get("cache_creation_input_tokens"))
                        + _usage_int(usage.get("cache_read_input_tokens"))
                    )
                    yield _norm(EV_USAGE, input_tokens=input_tokens,
                                output_tokens=_usage_int(usage.get("output_tokens")))
            elif etype == "message_delta":
                sr = ev.get("delta", {}).get("stop_reason")
                if sr:
                    stop_reason = sr
                usage = ev.get("usage") or {}
                if "output_tokens" in usage:
                    yield _norm(EV_USAGE, input_tokens=input_tokens,
                                output_tokens=_usage_int(usage.get("output_tokens")))
            elif etype == "message_stop":
                break
            elif etype == "error":
                yield _norm(EV_ERROR, status=0, code="upstream",
                            message=str(ev.get("error", {}).get("message", "stream error")),
                            retryable=False)
                return
        # Assemble the assistant message in block order; drop thinking blocks from
        # the persisted history (they are display-only and Anthropic re-derives).
        content = []
        for idx in order:
            blk = blocks.get(idx)
            if not blk:
                continue
            if blk["type"] == "text" and blk.get("text"):
                content.append({"type": "text", "text": blk["text"]})
            elif blk["type"] == "tool_use":
                content.append({"type": "tool_use", "id": blk["id"], "name": blk["name"], "input": blk["input"]})
        assistant_msg = {"role": "assistant", "content": content}
        yield _norm(EV_DONE, stop_reason=_norm_stop(stop_reason), assistant_msg=assistant_msg)


def _claude_err_code(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    return "upstream"


# OpenAI's /models lists everything the key can access, which includes many
# non-chat models (embeddings, audio/realtime/transcribe/tts, image, moderation,
# completions-only "-instruct", and web-search variants that reject tool use).
# They are all "public" to the key, but only chat-capable models belong in the
# agentCLI model dropdown, so we include the chat families and drop the rest.
_CHATGPT_INCLUDE_RE = re.compile(r"^(gpt-|o\d|chatgpt-)", re.I)
_CHATGPT_EXCLUDE_RE = re.compile(
    r"audio|realtime|transcribe|tts|image|embedding|whisper|moderation|"
    r"dall-e|search|instruct|codex",
    re.I,
)


def _filter_chatgpt_models(models: list[str]) -> list[str]:
    chat = [m for m in models
            if _CHATGPT_INCLUDE_RE.match(m) and not _CHATGPT_EXCLUDE_RE.search(m)]
    return chat or models


_NON_CHAT_MODEL_RE = re.compile(
    r"embed|rerank|re-rank|image|imagen|audio|realtime|transcrib|tts|"
    r"whisper|moderation|dall-e|speech",
    re.I,
)


def _filter_generic_chat_models(models: list[str]) -> list[str]:
    """Drop catalog entries whose identifiers prove they are not chat models."""
    chat = sorted(model for model in models if not _NON_CHAT_MODEL_RE.search(model))
    return chat or sorted(models)


def _filter_together_models(data: list[dict]) -> list[str]:
    """Together publishes a bare array with an explicit model type."""
    chat = sorted(
        str(model["id"]) for model in data
        if model.get("id") and model.get("type") == "chat"
    )
    return chat or sorted(str(model["id"]) for model in data if model.get("id"))


def _strip_models_prefix(model_id: str) -> str:
    # Google's /models lists ids as "models/gemini-...", but the chat endpoint's
    # model field wants the bare name.
    return model_id[len("models/"):] if model_id.startswith("models/") else model_id


def _filter_gemini_models(models: list[str]) -> list[str]:
    """Keep Gemini chat models (drop embedding/imagen/aqa), bare-named."""
    out = []
    for m in models:
        mid = _strip_models_prefix(m)
        low = mid.lower()
        if low.startswith("gemini") and "embedding" not in low:
            out.append(mid)
    return out or [_strip_models_prefix(m) for m in models]


def _filter_openrouter_models(data: list[dict]) -> list[str]:
    """OpenRouter lists hundreds of models across providers. Agent Chat needs
    tool-calling, so keep only models that advertise "tools" in
    supported_parameters (a model without it cannot drive Phoenix MCP's tools), sorted for
    a stable dropdown. Fall back to every id if none advertise it (schema drift)."""
    tool_ids = sorted(m["id"] for m in data
                      if m.get("id") and "tools" in (m.get("supported_parameters") or []))
    return tool_ids or sorted(m["id"] for m in data if m.get("id"))


def _filter_mistral_models(data: list[dict]) -> list[str]:
    """Keep active Mistral models declared fit for tool-driven chat.

    Mistral publishes completion_chat and function_calling booleans per model.
    When that schema is present it is authoritative, so a model must declare
    both. If the schema disappears entirely, fall back to active ids rather than
    turning a provider response change into an empty account.
    """
    active = [m for m in data if m.get("id") and m.get("archived") is not True]
    if not any(isinstance(m.get("capabilities"), dict) for m in active):
        return [m["id"] for m in active]
    return [
        m["id"] for m in active
        if m.get("capabilities", {}).get("completion_chat") is True
        and m.get("capabilities", {}).get("function_calling") is True
    ]


def _mistral_capabilities(data: list[dict], models: list[str]) -> dict[str, dict]:
    """Translate Mistral's declared model capabilities into Phoenix fields."""
    wanted = set(models)
    out: dict[str, dict] = {}
    for row in data:
        model_id = row.get("id")
        declared = row.get("capabilities")
        if model_id not in wanted or not isinstance(declared, dict):
            continue
        caps: dict[str, bool] = {}
        chat = declared.get("completion_chat")
        tools = declared.get("function_calling")
        vision = declared.get("vision")
        if isinstance(chat, bool) and isinstance(tools, bool):
            caps["tools"] = chat and tools
        if isinstance(vision, bool):
            caps["vision"] = vision
        if caps:
            out[model_id] = caps
    return out


def _refused_option(status: int, message: str, sent: dict) -> str | None:
    """The request key a provider just refused, when it named one of ours.

    Bounded three ways, because a wrong answer here disables a control the
    operator was using. It must be a 400 (a refusal, not an outage or a quota);
    the key must be one whose refusal has a single unambiguous consequence; and
    Phoenix must have ACTUALLY SENT it in this request. That last guard is what
    stops an error mentioning "temperature" for some unrelated reason from
    teaching us that temperature is unsupported.
    """
    if status != 400 or not message:
        return None
    low = message.lower()
    for key in AGENTCLI_LEARNABLE_OPTIONS:
        if key in sent and re.search(rf"\b{re.escape(key)}\b", low):
            return key
    return None


def _effort_probe_body(kind: str, level: str) -> dict | None:
    """The request fragment that carries an effort level for this backend.

    None for a backend with no effort control at all, which is then not probed.
    MiniMax takes an adaptive object with no level vocabulary. Ollama's current
    OpenAI-compatible endpoint validates `reasoning_effort`, so it is probed like
    the other per-model APIs instead of being reduced to its older boolean shape.

    The aggregators (OpenRouter, NVIDIA) ARE probed, and excluding them was a
    wrong call worth not repeating. The reason given was that they pass through
    whatever the underlying model takes, which varies per model, but that is an
    argument FOR probing rather than against: a probe runs against the one
    selected model, so per-model variation is precisely what it handles.

    DeepSeek's fragment carries the thinking toggle too, because it only reads
    the effort when thinking is enabled: probing the level alone would test
    nothing and answer 200 for every value.
    """
    if kind == "claude":
        return {"output_config": {"effort": level}}
    if kind == "deepseek":
        return {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    if kind in (
        "chatgpt", "grok", "gemini", "kimi", "meta", "mistral", "openrouter",
        "nvidia", "ollama", "ollama_cloud", "groq", "together", "cerebras",
        "fireworks",
    ):
        return {"reasoning_effort": level}
    return None


async def async_probe_capabilities(
    session: ClientSession, cfg: ProviderConfig,
) -> tuple[dict, int, bool]:
    """What this model's API actually accepts, established by asking it.

    Returns (capabilities, calls_made, answered). Every value is omitted rather
    than guessed when the answer could not be established, so a partial or failed
    probe narrows nothing.

    `answered` is False when NO call came back with a usable status. A key with
    no credit, a revoked key or an unreachable host answers the same way to every
    question, and reporting that as "this provider does not validate its options"
    blames the provider for an account problem. Only 200 and 400 are answers
    here; everything else is the provider declining to be asked.

    TWO STAGES, and the order is the whole method. Stage one sends a
    deliberately INVALID effort and asks whether the field is VALIDATED: a 400
    proves the server parses it. Stage two then walks the real levels, and only
    now is a 200 meaningful, because a field known to be validated accepts a
    value only if that value is in its vocabulary.

    Without stage one, stage two is worthless. An OpenAI-compatible server that
    ignores an unknown parameter answers 200 to every level, which reads as
    "supports all five" and is the exact shape of a wrong answer this whole
    item exists to remove. A 400 is informative; a 200 on its own never is.

    That asymmetry was established the hard way: whether DeepSeek honoured the
    effort at all could not be told from its replies, because reasoning output
    appears or does not at any level, run to run.
    """
    provider = build_provider(cfg)
    caps: dict = {}
    calls = 0
    answered = False

    fragment = _effort_probe_body(cfg.kind, AGENTCLI_PROBE_SENTINEL)
    if fragment is not None:
        status = await provider.probe_option(session, fragment)
        calls += 1
        answered = answered or status in (200, 400)
        # Only a 400 is evidence. Unreachable (None) says nothing, and a 200
        # means the field is ignored or leniently defaulted, which are
        # indistinguishable and equally useless.
        if status == 400:
            levels = []
            for level in AGENTCLI_EFFORT_LEVEL_ORDER:
                if calls >= AGENTCLI_PROBE_MAX_CALLS:
                    break
                body = _effort_probe_body(cfg.kind, level)
                if body is None:
                    continue
                level_status = await provider.probe_option(session, body)
                answered = answered or level_status in (200, 400)
                if level_status == 200:
                    levels.append(level)
                calls += 1
            # An empty result means every level was refused, which cannot be
            # true of a working model and means something else answered the
            # calls. Recording it would strip the control entirely.
            if levels:
                caps["effort_levels"] = levels

    if calls < AGENTCLI_PROBE_MAX_CALLS:
        # Temperature is the mirror-image question: not "which values", but
        # "does this model refuse the field", which is what reasoning models do.
        # So here the 400 is the finding and a 200 records nothing, since an
        # accepted temperature may still be ignored.
        status = await provider.probe_option(session, {"temperature": 0.7})
        calls += 1
        answered = answered or status in (200, 400)
        if status == 400:
            caps["temperature"] = False

    return caps, calls, answered


def _openrouter_capabilities(data: list[dict]) -> dict[str, dict]:
    """Per-model declared capabilities from OpenRouter's `supported_parameters`.

    Phoenix already read this field to filter the dropdown by tool support and
    then threw the rest away, so a model's declared reasoning and temperature
    support were sitting in a response we had already paid for.

    A key is emitted ONLY for something the provider actually declares. A missing
    key means "not declared" and must never be read as False, which is the same
    distinction the Energy issues array makes between an empty list and None.
    """
    out: dict[str, dict] = {}
    for m in data:
        model_id = m.get("id")
        params = m.get("supported_parameters")
        architecture = m.get("architecture")
        modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
        if not isinstance(modalities, list):
            modalities = m.get("input_modalities")
        if not model_id or (not isinstance(params, list) and not isinstance(modalities, list)):
            continue
        row: dict[str, bool] = {}
        if isinstance(params, list):
            row.update({
                "tools": "tools" in params,
                "thinking": "reasoning" in params,
                "temperature": "temperature" in params,
            })
        if isinstance(modalities, list):
            row["vision"] = any(
                isinstance(item, str) and item.lower() in {"image", "image_url"}
                for item in modalities
            )
        out[model_id] = row
    return out


async def _ollama_capabilities(
    session: ClientSession, base_url: str, headers: dict, models: list[str],
) -> dict[str, dict]:
    """Per-model declared capabilities from Ollama's /api/show.

    Ollama is the one backend where a model that cannot call tools is an ordinary
    thing to have installed: a local library is whatever the operator pulled, and
    Agent Chat is useless without tool calling. `/api/tags` does not say, so this
    costs one lookup per model, which is why it runs on an explicit refresh and
    not on every card open.

    `temperature` is deliberately NOT emitted: Ollama takes it for every model
    through its options block and does not list it as a capability, so claiming
    False would be inventing a limit the server does not have.
    """
    sem = asyncio.Semaphore(AGENTCLI_CAPABILITY_CONCURRENCY)

    async def _one(name: str) -> tuple[str, dict] | None:
        async with sem:
            try:
                async with session.post(
                    f"{base_url}/api/show", headers=headers, json={"model": name},
                    timeout=_PROBE_TIMEOUT, allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        return None
                    body = await resp.json(content_type=None)
            except (ClientError, asyncio.TimeoutError, ValueError):
                return None
        caps = body.get("capabilities") if isinstance(body, dict) else None
        if not isinstance(caps, list):
            return None
        return name, {
            "tools": "tools" in caps,
            "thinking": "thinking" in caps,
            "vision": "vision" in caps,
        }

    pairs = await asyncio.gather(*(_one(m) for m in models))
    return dict(p for p in pairs if p is not None)


def _filter_nvidia_models(models: list[str]) -> list[str]:
    """Drop NVIDIA's non-chat models (embedding/reranking) from the dropdown.

    Unlike OpenRouter, NVIDIA's catalog does not advertise per-model parameters,
    so tool-calling support (which varies by model there) cannot be detected and
    is left to the operator. This only removes ids that provably cannot chat at
    all. Sorted for a stable dropdown; falls back to every id if the name
    convention ever changes enough to filter everything out.
    """
    chat = sorted(m for m in models
                  if not any(w in m.lower() for w in ("embed", "rerank")))
    return chat or sorted(models)


class OpenAICompatProvider:
    """OpenAI-compatible chat/completions, streaming. Backs DeepSeek, ChatGPT,
    Gemini, Grok, Kimi, Meta, Mistral, OpenRouter, NVIDIA, and both Ollama
    flavours.

    Ollama uses a slightly different shape (the OpenAI path lives under /v1 and
    models come from /api/tags). Its current OpenAI-compatible endpoint accepts
    reasoning_effort; older endpoints retain a boolean UI fallback until a probe
    proves level support. Local Ollama is keyless with a user-supplied base URL
    and is the fragile tool backend (see _parse); Ollama Cloud is the same shape
    hosted at a fixed URL with a Bearer API key.
    """

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.kind = cfg.kind
        self.definition = PROVIDER_DEFINITIONS[cfg.kind]

    @property
    def _is_ollama(self) -> bool:
        # Ollama-shaped (local or cloud): drives /v1 chat plus /api/tags metadata.
        return self.definition.ollama is not None

    @property
    def _is_ollama_local(self) -> bool:
        return self.definition.ollama == "local"

    @property
    def _is_ollama_cloud(self) -> bool:
        return self.definition.ollama == "cloud"

    def _chat_url(self) -> str:
        # Ollama exposes the OpenAI shape under /v1; DeepSeek at the root.
        return f"{self.cfg.base_url}/v1/chat/completions" if self._is_ollama \
            else f"{self.cfg.base_url}/chat/completions"

    def _headers(self) -> dict:
        # Every keyed backend uses a Bearer header, including Ollama Cloud; only
        # local Ollama is keyless (no auth header).
        h = {"content-type": "application/json"}
        if self.cfg.api_key and not self._is_ollama_local:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def format_tools(self, mcp_tools: list[dict]) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            }}
            for t in mcp_tools
        ]

    def append_assistant(self, messages: list[dict], assistant_msg: dict) -> None:
        messages.append(assistant_msg)

    def append_tool_results(self, messages: list[dict], results: list[dict]) -> None:
        for r in results:
            messages.append({
                "role": "tool",
                "tool_call_id": r["tool_use_id"],
                "name": r.get("tool_name", ""),
                "content": _model_result_text(r, self.cfg.vision is True),
            })
            if self.cfg.vision is True and r.get("images"):
                parts: list[dict] = [{
                    "type": "text",
                    "text": "The tool returned the following image for visual inspection.",
                }]
                for image in r["images"]:
                    data_uri = f"data:{image['mime_type']};base64,{image['data']}"
                    parts.append({
                        "type": "image_url",
                        # Mistral takes the URL itself here; OpenAI and the other
                        # compatible APIs take an object containing `url`.
                        "image_url": (
                            data_uri if self.definition.model_filter == "mistral"
                            else {"url": data_uri}
                        ),
                    })
                messages.append({"role": "user", "content": parts})

    async def validate(self, session: ClientSession) -> tuple[bool, str]:
        async def _probe() -> tuple[bool, str]:
            if self._is_ollama:
                async with session.get(f"{self.cfg.base_url}/api/tags",
                                       headers=self._headers(), timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                    if resp.status == 200:
                        return True, ""
                    if resp.status in (401, 403):
                        return False, "API key rejected."
                    label = "Ollama Cloud" if self._is_ollama_cloud else "Ollama"
                    return False, f"{label} returned HTTP {resp.status}."
            async with session.get(f"{self.cfg.base_url}/models", headers=self._headers(),
                                   timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                if resp.status == 200:
                    return True, ""
                if resp.status in (401, 403):
                    return False, "API key rejected."
                if self.definition.validation_error == "fireworks" and resp.status == 412:
                    return False, (
                        "Fireworks reports that this account is not active for API inference. "
                        "Check the account status and confirm that billing or free-trial credits "
                        "are activated."
                    )
                if self.definition.validation_error == "qwen_openai" and resp.status == 404:
                    return False, (
                        "Qwen returned HTTP 404. Phoenix uses the OpenAI-compatible Chat "
                        "Completions endpoint; enter that Base URL, not the "
                        "Anthropic-compatible /apps/anthropic URL."
                    )
                return False, f"Provider returned HTTP {resp.status}."
        try:
            return await _retry_transient(_probe)
        except (ClientError, asyncio.TimeoutError) as exc:
            if self._is_ollama_local:
                # Newlines are preserved in the panel (the banner uses
                # white-space: pre-line), so keep this as a short lead plus a
                # scannable checklist rather than one dense paragraph.
                return False, (
                    f"Could not reach Ollama at {self.cfg.base_url}.\n"
                    f"({exc})\n\n"
                    "Phoenix MCP connects from the Home Assistant server, not your browser, so "
                    "Ollama must be reachable from that host:\n"
                    "- Ollama is running and bound to a reachable interface (OLLAMA_HOST=0.0.0.0).\n"
                    "- The firewall allows the Ollama port (default 11434).\n"
                    "- 'localhost' here means the HA server, not your PC."
                )
            if self._is_ollama_cloud:
                return False, f"Could not reach Ollama Cloud at {self.cfg.base_url}. ({exc})"
            return False, f"Could not reach the provider: {exc}"

    async def list_models(self, session: ClientSession) -> list[str]:
        try:
            if self._is_ollama:
                async with session.get(f"{self.cfg.base_url}/api/tags",
                                       headers=self._headers(), timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                    if resp.status != 200:
                        return []
                    # content_type=None: some Ollama builds omit the JSON content
                    # type, which would otherwise make aiohttp .json() raise.
                    body = await resp.json(content_type=None)
                return [m.get("name") or m.get("model") for m in body.get("models", [])
                        if (m.get("name") or m.get("model"))]
            async with session.get(f"{self.cfg.base_url}/models", headers=self._headers(),
                                   timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                if resp.status != 200:
                    return [self.cfg.model] if self.cfg.model else []
                body = await resp.json(content_type=None)
            data = (
                body if self.definition.models_shape == "list" and isinstance(body, list)
                else body.get("data", []) if isinstance(body, dict)
                else []
            )
            if self.definition.model_filter == "openrouter":
                return _filter_openrouter_models(data) or ([self.cfg.model] if self.cfg.model else [])
            if self.definition.model_filter == "mistral":
                return _filter_mistral_models(data)
            models = [m["id"] for m in data if m.get("id")]
            if self.definition.model_filter == "openai":
                models = _filter_chatgpt_models(models)
            elif self.definition.model_filter == "gemini":
                models = _filter_gemini_models(models)
            elif self.definition.model_filter == "nvidia":
                models = _filter_nvidia_models(models)
            elif self.definition.model_filter == "together":
                return _filter_together_models(data)
            elif self.definition.model_filter == "chat":
                models = _filter_generic_chat_models(models)
            return models or ([self.cfg.model] if self.cfg.model else [])
        except (ClientError, asyncio.TimeoutError):
            return [self.cfg.model] if self.cfg.model else []

    async def probe_option(self, session: ClientSession, extra: dict) -> int | None:
        """HTTP status for the smallest possible completion carrying `extra`.

        None when the provider could not be reached at all, which is not an
        answer about the option and must not be recorded as one.
        """
        body = {"model": self.cfg.model, "max_tokens": 1, "stream": False,
                "messages": [{"role": "user", "content": "hi"}], **extra}
        try:
            async with session.post(self._chat_url(), headers=self._headers(), json=body,
                                    timeout=_PROBE_TIMEOUT, allow_redirects=False) as resp:
                return resp.status
        except (ClientError, asyncio.TimeoutError):
            return None

    async def list_model_capabilities(
        self, session: ClientSession, models: list[str],
    ) -> dict[str, dict]:
        """What each model DECLARES it accepts; empty when the provider says nothing.

        OpenRouter, Mistral, and Ollama report this. Every other backend's models
        endpoint returns an id and an owner, which is why the shipped capability
        table cannot simply be replaced by discovery and why probing the knobs is
        a separate step rather than part of this one.

        Empty means "the provider declared nothing", never "the model supports
        nothing": a caller reading it the second way would strip a working
        model's controls the moment a lookup failed.
        """
        try:
            if self._is_ollama:
                return await _ollama_capabilities(
                    session, self.cfg.base_url, self._headers(), models)
            if self.definition.model_filter == "openrouter":
                async with session.get(
                    f"{self.cfg.base_url}/models", headers=self._headers(),
                    timeout=_PROBE_TIMEOUT, allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        return {}
                    body = await resp.json(content_type=None)
                return _openrouter_capabilities(body.get("data", []))
            if self.definition.model_filter == "mistral":
                async with session.get(
                    f"{self.cfg.base_url}/models", headers=self._headers(),
                    timeout=_PROBE_TIMEOUT, allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        return {}
                    body = await resp.json(content_type=None)
                return _mistral_capabilities(body.get("data", []), models)
            if self.definition.reasoning in ("zai", "qwen"):
                return {model: {"thinking": True} for model in models}
        except (ClientError, asyncio.TimeoutError, ValueError):
            return {}
        return {}

    async def stream_turn(
        self, session: ClientSession, *, system_prompt: str, messages: list[dict],
        tools: list[dict], options: dict,
    ) -> AsyncIterator[dict]:
        # OpenAI shape carries the system prompt as the first message.
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        body: dict = {
            "model": self.cfg.model,
            "messages": full_messages,
            "tools": tools,
            "stream": True,
        }
        if self.definition.include_usage:
            body["stream_options"] = {"include_usage": True}
        thinking = options.get("thinking")
        effort = options.get("effort")
        # Map the panel's thinking selection to each backend's real API knob:
        #   DeepSeek: a thinking on/off toggle + reasoning_effort (low/high/max,
        #     default high; xhigh maps to max). Thinking mode ignores temperature,
        #     so we drop it while thinking is on. `low` became real when the v4
        #     models replaced the retired deepseek-chat / deepseek-reasoner
        #     aliases; before that low and medium both remapped to high.
        #   OpenAI reasoning models: reasoning_effort only (none/minimal/low/
        #     medium/high; none disables it), and they reject a custom temperature.
        #   Ollama: reasoning_effort on its OpenAI-compatible endpoint. Older
        #     servers that do not validate levels never establish effort_levels,
        #     so the panel keeps its boolean fallback. Off maps to `none`; on with
        #     no established level omits the field and uses the model's default.
        thinking_on = False
        reasoning_model = False
        if self.definition.reasoning == "deepseek":
            if thinking is not None:
                body["thinking"] = {"type": "enabled" if thinking else "disabled"}
                thinking_on = bool(thinking)
            if thinking_on and effort:
                # TOP-LEVEL, verified on the wire: a deliberately INVALID value
                # here answers 400, so the field is parsed and validated rather
                # than ignored. It is a standard OpenAI parameter, which is why it
                # sits beside `thinking` rather than inside it; DeepSeek's own
                # samples pass `thinking` through extra_body and leave this one a
                # normal argument.
                #
                # Worth knowing before "fixing" it: whether it worked could NOT be
                # established from the model's replies. Reasoning output appears or
                # does not at any effort level, run to run, so two sessions gave
                # opposite impressions. Only the invalid-value probe settled it.
                body["reasoning_effort"] = effort
            # DeepSeek's undocumented default output cap truncated a large tool
            # call mid-JSON (live-observed on a whole-dashboard write). 32K only
            # ever raises the ceiling; see const.py. Other kinds deliberately get
            # no max_tokens (OpenAI reasoning models reject the field).
            body["max_tokens"] = AGENTCLI_DEEPSEEK_MAX_TOKENS
        elif self.definition.reasoning == "effort":
            # OpenAI and xAI both take a top-level reasoning_effort on chat
            # completions; sending it marks a reasoning turn (skip temperature).
            # A model that does not accept the level returns a clean error.
            if effort:
                body["reasoning_effort"] = effort
                reasoning_model = True
        elif self.definition.reasoning == "gemini":
            # Gemini's OpenAI-compatible endpoint maps reasoning_effort to Gemini's
            # thinking_level (minimal/low/medium/high). Google strongly recommends
            # NOT setting temperature on Gemini 3 (reasoning is tuned for the
            # default), so Phoenix MCP never sends it for Gemini.
            if effort:
                body["reasoning_effort"] = effort
        elif self.definition.reasoning == "kimi":
            # Kimi splits the knob by model family, so the two fields are emitted
            # independently rather than as an if/else: K3 takes reasoning_effort
            # (low/high/max, default max) and cannot turn reasoning off, while K2.x
            # takes DeepSeek's thinking object and has no effort levels. The panel
            # offers only the one its selected model accepts, and a headless turn
            # (no options at all) sends neither and gets the model's own default.
            if thinking is not None:
                body["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if effort:
                body["reasoning_effort"] = effort
        elif self.definition.reasoning == "probed_effort":
            # Aggregators: one key fronting many vendors' models. Phoenix used to
            # offer no thinking control here at all, on the grounds that a single
            # control cannot fit every model behind the key. That reasoning is
            # obsolete now that capabilities are established PER MODEL: a
            # reasoning model reached through an aggregator was silently losing
            # its reasoning, while the same model reached directly kept it.
            #
            # Which field carries it is not assumed either. Both speak the
            # OpenAI shape, so `reasoning_effort` is the candidate, and the probe
            # is what confirms it: if the aggregator does not validate the field,
            # no levels are established, the panel offers none, and this branch
            # never fires. The wrong guess costs nothing.
            if effort:
                body["reasoning_effort"] = effort
        elif self.definition.reasoning == "meta":
            # Meta's Model API takes a top-level reasoning_effort (minimal/low/
            # medium/high/xhigh; "none" is rejected by Muse Spark, which always
            # reasons, so the panel never offers an off state). Meta tunes the model
            # for its default temperature, so Phoenix MCP does not send one.
            if effort:
                body["reasoning_effort"] = effort
        elif self.definition.reasoning == "mistral":
            # Mistral Small latest and Medium 3.5 accept `none` and `high`.
            # The panel only offers this control for those documented models.
            if effort:
                body["reasoning_effort"] = effort
        elif self.definition.reasoning == "zai":
            if thinking is not None:
                body["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if tools:
                body["tool_stream"] = True
                body["clear_thinking"] = False
        elif self.definition.reasoning == "qwen":
            if thinking is not None:
                body["enable_thinking"] = bool(thinking)
        elif self.definition.reasoning == "ollama":
            if effort:
                body["reasoning_effort"] = effort
            elif thinking is False:
                body["reasoning_effort"] = "none"
        temp = options.get("temperature")
        skip_temp = (
            (self.definition.reasoning == "deepseek" and thinking_on)
            or reasoning_model
            or self.definition.reasoning == "gemini"
            # Only Kimi's legacy moonshot-v1 models accept temperature, and those
            # have no reasoning control at all: a thinking or effort selection
            # therefore means a kimi-k* model, which rejects the field.
            or (self.definition.reasoning == "kimi" and (thinking is not None or bool(effort)))
            or self.definition.reasoning == "meta"
            or (self.definition.reasoning in ("zai", "qwen") and thinking is True)
        )
        if temp is not None and not skip_temp:
            try:
                body["temperature"] = float(temp)
            except (TypeError, ValueError):
                pass
        show_thinking = bool(options.get("show_thinking"))
        try:
            async with session.post(self._chat_url(), headers=self._headers(), json=body,
                                    timeout=_CHAT_TIMEOUT, allow_redirects=False) as resp:
                if resp.status != 200:
                    msg, err_code = _parse_provider_error((await resp.text())[:4000])
                    # Ollama commonly 500s on an unknown/hallucinated tool name.
                    code = "ollama_backend" if self._is_ollama else "upstream"
                    if resp.status in (401, 403):
                        code = "auth"
                    elif resp.status == 429:
                        code = "rate_limit"
                    retryable = resp.status in (429, 500, 502, 503)
                    if err_code in _NON_RETRYABLE_ERROR_CODES:
                        retryable = False
                        if err_code == "insufficient_quota":
                            code = "quota"
                    rate_limit = (
                        _mistral_rate_limit_headers(resp.headers)
                        if self.definition.model_filter == "mistral" and resp.status == 429 else {}
                    )
                    yield _norm(EV_ERROR, status=resp.status, code=code,
                                message=msg[:600], retryable=retryable,
                                refused=_refused_option(resp.status, msg, body),
                                **({"rate_limit": rate_limit} if rate_limit else {}))
                    return
                async for ev in self._parse(resp, show_thinking):
                    yield ev
        except (ClientError, asyncio.TimeoutError) as exc:
            yield _norm(EV_ERROR, status=0, code="network", message=str(exc), retryable=True)

    async def _parse(self, resp: Any, show_thinking: bool) -> AsyncIterator[dict]:
        text_parts: list[str] = []
        # Z.ai requires its reasoning_content to be returned verbatim with the
        # assistant tool call on the next round. Other compatible providers use
        # this field for display only, so retention is profile-controlled.
        replay_reasoning_parts: list[str] = []
        # Mistral emits reasoning as structured ThinkChunk content. Unlike the
        # display-only reasoning fields used by other compatible APIs, Mistral
        # requires these chunks to be replayed in the assistant message on the
        # next turn, even when verbose output is hidden in Phoenix.
        mistral_content_parts: list[dict] = []
        # tool calls accumulated by index: {index: {"id","name","args"}}
        calls: dict[int, dict] = {}
        finish = "stop"
        # Whether the stream ever reported WHY it ended. Every provider sends a
        # finish_reason on its final chunk, so the default above standing
        # untouched means the response stopped arriving rather than finishing:
        # the one signal that tells a truncated tool call apart from a malformed
        # one. Deliberately not keyed on the "[DONE]" sentinel, which is an
        # OpenAI convention some compatible backends never send, and which would
        # therefore relabel their genuinely malformed calls as dropped ones.
        saw_finish = False
        # Some local reasoning models (e.g. r1 via Ollama) emit their chain of
        # thought inline as <think>...</think> inside content rather than in a
        # separate reasoning field. Split it out so it is treated as thinking
        # (shown only when show_thinking) and never leaks into the reply or the
        # message history sent back next turn.
        think = _ThinkSplitter()
        async for line in _sse_lines(resp):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # Usage must be read BEFORE the empty-choices skip: OpenAI's final
            # usage chunk (stream_options include_usage) carries choices: [].
            # Some backends (Gemini, Grok, Ollama) volunteer usage on content
            # chunks instead; cumulative-replace semantics cover both.
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                pt = _usage_int(usage.get("prompt_tokens"))
                ct = _usage_int(usage.get("completion_tokens"))
                if pt or ct:
                    yield _norm(EV_USAGE, input_tokens=pt, output_tokens=ct)
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            # DeepSeek uses reasoning_content; Ollama's OpenAI-compatible path
            # uses reasoning. Both are display-only and never enter the assistant
            # message that Phoenix sends back on the next round.
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning and self.definition.reasoning == "zai":
                replay_reasoning_parts.append(str(reasoning))
            if reasoning and show_thinking:
                yield _norm(EV_THINKING, text=reasoning)
            content = delta.get("content")
            if self.definition.model_filter == "mistral" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    # Retain each structured block in its original order and
                    # shape. Mistral explicitly requires the full assistant
                    # content, including ThinkChunk, on subsequent turns.
                    mistral_content_parts.append(block)
                    if block.get("type") == "thinking":
                        thinking_text = "".join(
                            str(item.get("text", ""))
                            for item in block.get("thinking", [])
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                        if thinking_text:
                            if show_thinking:
                                yield _norm(EV_THINKING, text=thinking_text)
                    elif block.get("type") == "text" and block.get("text"):
                        seg = str(block["text"])
                        text_parts.append(seg)
                        yield _norm(EV_TEXT, text=seg)
            elif self.definition.model_filter == "mistral" and isinstance(content, str) \
                    and content and mistral_content_parts:
                # Mistral may switch from structured ThinkChunk content to
                # ordinary string deltas for the answer. Materialize those as
                # TextChunk content after the retained thinking blocks.
                text_parts.append(content)
                if mistral_content_parts[-1].get("type") == "text":
                    old_text = str(mistral_content_parts[-1].get("text", ""))
                    mistral_content_parts[-1]["text"] = old_text + content
                else:
                    mistral_content_parts.append({"type": "text", "text": content})
                yield _norm(EV_TEXT, text=content)
            elif isinstance(content, str) and content:
                for kind, seg in think.feed(content):
                    if kind == "text":
                        text_parts.append(seg)
                        yield _norm(EV_TEXT, text=seg)
                    elif show_thinking:
                        yield _norm(EV_THINKING, text=seg)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(idx, {"id": None, "name": None, "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
                saw_finish = True
        # Flush any held-back tail from the think splitter.
        for kind, seg in think.flush():
            if kind == "text":
                text_parts.append(seg)
                yield _norm(EV_TEXT, text=seg)
            elif show_thinking:
                yield _norm(EV_THINKING, text=seg)
        # Assemble.
        assistant_tool_calls = []
        parsed_tools = []
        bad = False
        for idx in sorted(calls):
            slot = calls[idx]
            args_str = slot["args"] or "{}"
            try:
                parsed = json.loads(args_str)
            except json.JSONDecodeError:
                bad = True
                parsed = None
                # Content is deliberately not logged (tool args can carry
                # sensitive values); name/size/finish is what diagnosis needs.
                _LOGGER.debug(
                    "agentCLI unparseable tool-call args: tool=%s args_len=%d finish_reason=%s",
                    slot["name"], len(args_str), finish if saw_finish else "<none>",
                )
            assistant_tool_calls.append({
                "id": slot["id"], "type": "function",
                "function": {"name": slot["name"], "arguments": args_str},
            })
            if parsed is not None:
                parsed_tools.append({"id": slot["id"], "name": slot["name"], "input": parsed})
        # Use "" (not None) for a tool-only reply: a null content serializes to
        # JSON null, which Ollama's OpenAI-compatible endpoint rejects on the next
        # turn ("invalid message content type: <nil>"). An empty string is valid
        # for OpenAI/DeepSeek/Gemini too, so this is safe across all of them.
        answer_text = "".join(text_parts)
        if self.kind == "mistral" and mistral_content_parts:
            assistant_content: str | list[dict] = mistral_content_parts
        else:
            assistant_content = answer_text
        assistant_msg: dict = {"role": "assistant", "content": assistant_content}
        if replay_reasoning_parts:
            assistant_msg["reasoning_content"] = "".join(replay_reasoning_parts)
        if assistant_tool_calls:
            assistant_msg["tool_calls"] = assistant_tool_calls
        if bad:
            # The model emitted a tool call whose arguments never parse as JSON.
            # Do not dispatch garbage; end the turn with a clear error rather
            # than hanging. THREE causes, and each sends the operator somewhere
            # different, so each gets its own code (the panel localizes on the
            # code, not the message, so one code cannot carry three sentences).
            #
            # A stream that stopped on the output-token limit was TRUNCATED
            # (live-observed: a whole-dashboard write blowing the provider's
            # default cap), so the fix is a smaller change. A stream that never
            # reported a finish_reason at all did not finish: the connection
            # dropped mid-call, the fix is to send it again, and NOTHING about
            # what the model wrote was wrong. That one is reported last-resort
            # as malformed output otherwise, which is the reading that costs the
            # most time: it sends the operator to inspect a config that was
            # fine. Anything else genuinely is malformed output (a known Ollama
            # failure mode).
            if finish == "length":
                code = "tool_call_truncated"
                retryable = False
                message = (
                    "The model hit its output length limit in the middle of a tool "
                    "call, so the action was not run. The requested change was too "
                    "large to emit in one call; try asking for a smaller change."
                )
            elif not saw_finish:
                code = "tool_call_cut"
                retryable = True
                message = (
                    "The connection to the model ended in the middle of a tool "
                    "call, so the action was not run. Nothing was applied and the "
                    "request itself was not at fault; send it again."
                )
            else:
                code = "bad_tool_call"
                retryable = False
                message = "The model produced an invalid tool call."
            yield _norm(EV_ERROR, status=0, code=code,
                        message=message, retryable=retryable)
            yield _norm(EV_DONE, stop_reason="error", assistant_msg=assistant_msg)
            return
        for tool_call in parsed_tools:
            yield _norm(EV_TOOL, id=tool_call["id"], name=tool_call["name"],
                        input=tool_call["input"])
        yield _norm(EV_DONE, stop_reason=_norm_stop(finish), assistant_msg=assistant_msg)


def _norm_stop(raw: str) -> str:
    """Normalize provider stop reasons to end_turn / tool_use / max_tokens / error."""
    return {
        "end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
        "stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
    }.get(raw, "end_turn")


def build_provider(cfg: ProviderConfig) -> ClaudeProvider | OpenAICompatProvider:
    definition = PROVIDER_DEFINITIONS.get(cfg.kind)
    if definition is None:
        raise ValueError(f"Unknown provider kind: {cfg.kind}")
    if definition.adapter == "anthropic":
        return ClaudeProvider(cfg)
    return OpenAICompatProvider(cfg)


# --------------------------------------------------------------------------- #
# Tool list (mirrors the MCP tools/list gating) and result helpers
# --------------------------------------------------------------------------- #

# Orientation and everyday tools listed first: models weight early tools more
# (weak local ones especially), and the system prompt tells the agent to orient
# via get_capability_summary/get_overview/search_entities before acting, so
# those should not sit buried mid-list. Everything else keeps its source order
# (the sort is stable). Deterministic across turns, so it never busts the
# provider prompt cache.
_TOOL_PRIORITY = (
    "get_capability_summary", "get_overview", "search_entities",
    "describe_entity", "describe_area", "get_state", "get_states",
    "call_service", "dry_run_service", "find_available_actions",
)


def build_mcp_tool_list(token: TokenRecord, data: PhoenixData) -> list[dict]:
    """The tool defs this token would announce, cap key stripped (mirrors tools/list)."""
    from .mcp_view import (  # noqa: PLC0415
        _ENTITY_TOOL_DEFS, _NATIVE_TOOL_DEFS, _SYSTEM_TOOL_DEFS, _tool_is_announced,
    )
    from .mesa_tools import mesa_tool_defs  # noqa: PLC0415

    announce_all = getattr(token, "announce_all_tools", False)
    has_write = token_has_write_scope(token)
    hass = getattr(data, "hass", None)
    mesa_defs = mesa_tool_defs() if getattr(data, "mesa", None) is not None else []
    out = []
    for d in list(_ENTITY_TOOL_DEFS) + list(_NATIVE_TOOL_DEFS) + list(_SYSTEM_TOOL_DEFS) + mesa_defs:
        if announce_all or _tool_is_announced(d, token, has_write, cast(Any, hass)):
            out.append({k: v for k, v in d.items() if k not in ("cap", "caps", "caps_any", "requires")})
    rank = {name: i for i, name in enumerate(_TOOL_PRIORITY)}
    out.sort(key=lambda t: rank.get(t["name"], len(rank)))
    return out


def catalog_payload_metrics(token: TokenRecord, data: PhoenixData) -> dict[str, int]:
    """Return deterministic byte metrics for the token's announced catalog."""
    tools = build_mcp_tool_list(token, data)
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
    claude = ClaudeProvider(ProviderConfig("claude", "metrics", "")).format_tools(tools)
    openai = OpenAICompatProvider(ProviderConfig("chatgpt", "metrics", "")).format_tools(tools)
    return {
        "tool_count": len(tools),
        "canonical_bytes": len(canonical),
        "claude_bytes": len(json.dumps(claude, sort_keys=True, separators=(",", ":")).encode()),
        "openai_bytes": len(json.dumps(openai, sort_keys=True, separators=(",", ":")).encode()),
    }


def _clip_display(text: str) -> str:
    """Cap tool-result text for the verbose panel view; the model still gets the
    full result via the message history."""
    if len(text) <= AGENTCLI_TOOL_RESULT_MAX_CHARS:
        return text
    return text[:AGENTCLI_TOOL_RESULT_MAX_CHARS] + f"\n… (truncated, {len(text)} chars total)"


_MODEL_IMAGE_FALLBACK = (
    "The image is displayed to the operator, but this provider/model is not known "
    "to support visual input. Do not infer or describe image contents."
)


def _model_result_text(result: dict, vision: bool) -> str:
    text = str(result.get("result_text") or "")
    if result.get("images") and not vision:
        return f"{text}\n{_MODEL_IMAGE_FALLBACK}".strip()
    return text


def _extract_image_blocks(content: Any) -> list[dict[str, str]]:
    """Validate and normalize MCP image blocks for internal consumers."""
    images: list[dict[str, str]] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        data = item.get("data")
        mime_type = str(item.get("mimeType") or "").lower().split(";", 1)[0].strip()
        if not isinstance(data, str) or mime_type not in {"image/jpeg", "image/png", "image/gif"}:
            continue
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            continue
        if not decoded or len(decoded) > 4 * 1024 * 1024:
            continue
        images.append({"data": data, "mime_type": mime_type})
    return images


def _flatten_text(content: list[dict]) -> str:
    return "\n".join(
        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
    )


_AGENTCLI_LOCAL_TIME_FIELDS: dict[str, frozenset[str]] = {
    "get_state": frozenset({"last_changed", "last_updated", "last_reported"}),
    "get_states": frozenset({"last_changed", "last_updated", "last_reported"}),
    "get_history": frozenset({
        "start", "end", "when", "last_changed", "last_updated", "last_reported",
    }),
    "get_statistics": frozenset({"start", "end", "last_reset"}),
    "get_logbook": frozenset({"start_time", "end_time", "when"}),
    "recent_activity": frozenset({"when"}),
    "get_automation_traces": frozenset({"start", "finish"}),
}


def _agentcli_local_time(value: Any, local_tz: tzinfo) -> str | None:
    """Return a local ISO companion for one recognized wire timestamp."""
    parsed: datetime | None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=dt_util.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
    else:
        return None
    return parsed.astimezone(local_tz).isoformat()


def _agentcli_add_local_time_fields(
    tool_name: str, text: str, local_tz: tzinfo,
) -> str:
    """Add local companions without changing canonical tool-result timestamps.

    This is an Agent Chat-only presentation layer. Cursor values deliberately get
    no companion because they are opaque protocol inputs for a subsequent call.
    """
    fields = _AGENTCLI_LOCAL_TIME_FIELDS.get(tool_name)
    if fields is None:
        return text
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text

    def add_companions(value: Any) -> Any:
        if isinstance(value, list):
            return [add_companions(item) for item in value]
        if not isinstance(value, dict):
            return value
        augmented: dict[str, Any] = {}
        for key, item in value.items():
            augmented[key] = add_companions(item)
            companion = _agentcli_local_time(item, local_tz) if key in fields else None
            companion_key = f"{key}_local"
            if companion is not None and companion_key not in value:
                augmented[companion_key] = companion
        return augmented

    return json.dumps(add_companions(payload), separators=(",", ":"), ensure_ascii=False)


_AGENTCLI_RESOURCE_INVENTORY_TOOLS = frozenset({
    "list_automations", "list_scripts", "list_scenes", "list_helpers", "list_devices",
})


def _agentcli_add_resource_match_guidance(
    tool_name: str, args: dict[str, Any], text: str,
) -> str:
    """Repeat Agent Chat's premise guard beside discovery evidence for weaker models.

    This provider-only suffix is not emitted in the visible tool summary and never changes the
    external MCP result. Inventory lists always carry the conditional reminder. A filtered entity
    search carries it only on a structurally proven zero match.
    """
    guidance: str | None = None
    if tool_name in _AGENTCLI_RESOURCE_INVENTORY_TOOLS:
        guidance = (
            "If you called this inventory to identify a resource named by the operator and no "
            "plausible match appears, stop now. State the mismatch and ask one focused clarifying "
            "question. Do not substitute a related entry or inspect logs, history, or traces for "
            "a guessed target."
        )
    elif tool_name == "search_entities" and str(args.get("query") or "").strip():
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("count") == 0 \
                and payload.get("entities") == []:
            guidance = (
                "This filtered discovery found no matching entity. If the current request depends "
                "on that named resource, stop now, state the mismatch, and ask one focused "
                "clarifying question. Do not substitute a related entity or search logs, history, "
                "or traces for a guessed target."
            )
    if guidance is None:
        return text
    return f"{text}\n\nPhoenix Agent Chat factual guard: {guidance}"


_AGENTCLI_LOCAL_QUERY_FIELDS: dict[str, frozenset[str]] = {
    "get_history": frozenset({"start_time", "end_time"}),
    "get_statistics": frozenset({"start_time", "end_time"}),
    "get_logbook": frozenset({"start_time", "end_time"}),
}


def _agentcli_prepare_time_args(
    tool_name: str, args: dict[str, Any], local_tz: tzinfo,
) -> tuple[dict[str, Any], str | None]:
    """Apply HA's zone to naive Agent Chat ranges and reject manual shifting."""
    fields = _AGENTCLI_LOCAL_QUERY_FIELDS.get(tool_name)
    if fields is None:
        return args, None
    prepared = dict(args)
    for field in fields:
        value = prepared.get(field)
        if not isinstance(value, str):
            continue
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return args, (
                f"Agent Chat {field} must be a Home Assistant local wall time without "
                "a timezone suffix, or a relative time. Pass the operator's intended "
                "clock time directly; Phoenix applies the configured time zone."
            )
        prepared[field] = parsed.replace(tzinfo=local_tz).isoformat()
    return prepared, None


def _parse_pending(result: dict) -> dict | None:
    """Detect a pending_approval CallToolResult, returning its fields or None.

    Strict: a text content item whose JSON has status=="pending_approval" AND the
    result carries no isError key (matches mcp_view._tool_pending).
    """
    if result.get("isError"):
        return None
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            parsed = json.loads(item.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "pending_approval" and parsed.get("approval_id"):
            return {
                "approval_id": parsed["approval_id"],
                "review_url": parsed.get("review_url"),
                "expires_at": parsed.get("expires_at"),
            }
    return None


def execution_failure_text(approval: Any) -> str | None:
    """The executor's own error text when an admin APPROVED an action but its
    execution failed (the approve path stores the error result and flips the
    record to rejected with the "execution_failed" slug). None for a real
    admin rejection or any other state. Reporting that case as a bare "rejected"
    makes an agent treat an approval as a refusal and loop retrying the same
    doomed call instead of fixing the reported cause."""
    if getattr(approval, "rejected_reason", None) != "execution_failed":
        return None
    result = getattr(approval, "result", None)
    if not isinstance(result, dict):
        return None
    tool_result = result.get("tool_result")
    if not isinstance(tool_result, dict):
        return None
    return _flatten_text(tool_result.get("content", [])).strip() or None


def _resolved_result(status: str, applied_result: dict | None, approval: Any = None) -> dict:
    """A CallToolResult to feed back after an interactive approval resolves.

    On a reject/cancel it surfaces the admin's rejection reason (set from the
    Approvals panel) to the agent, mirroring mcp_view._tool_inline_resolved, so a
    rejection with a reason is actually reported instead of a generic notice.
    An approved-but-execution-failed record is reported as exactly that, with
    the executor's error, never as a plain rejection. An approved result carries
    the operator-accepted note (mcp_view._operator_accepted_result) so the agent
    treats the approved change as settled instead of iterating on it.
    """
    if status == STATUS_APPROVED and applied_result is not None:
        from .tool_common import _operator_accepted_result  # noqa: PLC0415

        return _operator_accepted_result(applied_result)
    if status in (STATUS_REJECTED, STATUS_CANCELLED):
        failure = execution_failure_text(approval)
        if failure is not None:
            return {"content": [{"type": "text", "text": json.dumps({
                "status": "execution_failed",
                "message": (
                    f"The operator APPROVED this action, but executing it failed: {failure} "
                    "Do not retry the same call unchanged; fix the reported cause first "
                    "(for example, if it reports a stale expected_hash, re-read the "
                    "resource for the current content_hash)."
                ),
            })}], "isError": True}
        # A rejection with a reason is the operator STEERING, not stopping:
        # iterating on a proposal through several reject-with-reason rounds is
        # the normal way a card gets built. Live-tested the strict alternative
        # (forbid variations, escalate to a hard stop on the second rejection):
        # models turned pedantic, gave up, or side-stepped the tools entirely
        # (dumped raw data into chat, built an HTML chart instead). Only a
        # reasonless rejection means "do not just try again".
        reason = getattr(approval, "rejected_reason", None)
        if status == STATUS_REJECTED and reason:
            message = (
                f"This action was not applied (it was rejected). Reason: {reason}. "
                "That reason is the operator's direction for what to do next: "
                "address it in your next proposal instead of resubmitting the "
                "same change."
            )
        elif status == STATUS_REJECTED:
            message = (
                "This action was not applied (it was rejected, no reason given). "
                "Do not resubmit the same change; ask the operator how they "
                "would like to proceed."
            )
        else:
            detail = f" Reason: {reason}." if reason else ""
            message = (
                f"This action was not applied (it was cancelled).{detail} "
                "Do not retry it; report the outcome to the user."
            )
        return {"content": [{"type": "text", "text": json.dumps({
            "status": status,
            "message": message,
        })}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps({
        "status": "pending_approval",
        "message": "Still awaiting approval; it was not applied in this session. Do not retry.",
    })}]}


async def _dispatch_with_progress(emit: Any, tool_id: Any, coro: Any) -> Any:
    """Run one tool call, relaying whatever it reports while it is still running.

    Tools that hold the request open (a firmware build, a confirm gate) describe
    what they are waiting on through mcp_view's progress bus, which exists so an
    SSE-framed MCP request can emit notifications/progress. Agent Chat is the
    other consumer of the same signal: it owns both ends of its own stream, so it
    needs no progressToken negotiation and can always show the line.

    The bus is set BEFORE the task is created so the task's copied context holds
    the same object, matching how mcp_view's SSE writer does it. Only changed
    statuses are emitted, so a slow build does not spam the transcript.
    """
    from .tool_common import _ProgressBus, _progress_ctx  # noqa: PLC0415

    bus = _ProgressBus()
    _progress_ctx.set(bus)
    task = asyncio.ensure_future(coro)
    last: str | None = None
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=AGENTCLI_PROGRESS_INTERVAL_SECONDS)
            if done:
                return await task
            if bus.status and bus.status != last:
                last = bus.status
                await emit("tool_progress", {
                    "id": tool_id,
                    "message": bus.status,
                    **({"message_key": bus.status_key} if bus.status_key else {}),
                    **({"message_params": bus.status_params} if bus.status_params else {}),
                })
    finally:
        _progress_ctx.set(None)


async def _await_agent_approval(
    hass: HomeAssistant, data: PhoenixData, approval_id: str, cancel: asyncio.Event, timeout: float,
) -> dict:
    """Block until an approval resolves (or cancel/timeout), then return a result.

    Modeled on mcp_view._await_inline_confirm but tied to the live SSE connection
    (long timeout) and cancel-aware. The bus event carries only IDs, so the
    executed result is re-read from the persisted approval record.
    """
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_resolved(event: Any) -> None:
        # @callback so HA runs this in the event loop (not the executor thread);
        # otherwise future.set_result would never wake the awaiting loop.
        if event.data.get("approval_id") == approval_id and not future.done():
            future.set_result(True)

    unsub = hass.bus.async_listen(f"{DOMAIN}_approval_resolved", _on_resolved)
    cancel_task = hass.loop.create_task(cancel.wait())
    try:
        await asyncio.wait({future, cancel_task}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        unsub()
        if not cancel_task.done():
            cancel_task.cancel()

    latest = get_approval(data.store, approval_id)
    if latest is None:
        return _resolved_result(STATUS_PENDING, None)
    if latest.status == STATUS_PENDING:
        # The interactive turn ended (operator cancelled/closed the chat, or the
        # wait timed out) with no decision. Cancel the orphaned approval so an
        # admin cannot later approve and execute an action the operator already
        # abandoned. BUT never cancel an approval an admin is mid-executing: the
        # admin approve path claims the id in data.approvals_in_progress, releases
        # the lock, and runs the side effect while the record is still pending, so
        # cancelling it here would flip a record whose side effect already ran (the
        # executor would then fail to finalize). Skip those exactly like the expiry
        # sweep does; the executor owns finalization and we re-read the true
        # outcome. async_update_approval_status is itself a no-op on a non-pending record,
        # so a race where an admin resolved it fully is also handled.
        async with data.store.async_lock:
            if approval_id in data.approvals_in_progress:
                resolved = None
            else:
                resolved = await async_update_approval_status(
                    data.store, approval_id,
                    status=STATUS_CANCELLED,
                    rejected_reason=REASON_AGENT_CHAT_ENDED,
                )
        if resolved is not None and resolved.status == STATUS_CANCELLED:
            dismiss_approval_notification(hass, approval_id)
            fire_approval_resolved_event(hass, resolved)
            return _resolved_result(STATUS_CANCELLED, None, resolved)
        # In-progress, or an admin resolved it in the race: re-read the true outcome.
        latest = get_approval(data.store, approval_id) or latest
    applied = None
    if latest.status == STATUS_APPROVED and latest.result:
        applied = latest.result.get("tool_result")
    return _resolved_result(latest.status, applied, latest)


_STYLE_INSTRUCTIONS = {
    "direct": (
        "Use plain, efficient, matter-of-fact language and lead with the outcome. Do not add "
        "praise, encouragement, conversational preambles, or an announcement of what you are "
        "about to do."
    ),
    "warm": (
        "Be friendly and encouraging without praise, flattery, padding, or unnecessary "
        "social language."
    ),
    "calm_guide": (
        "Be patient, reassuring, and accessible, while never sounding patronizing."
    ),
    "lively": (
        "Use energetic language and occasional light humor only when the stakes are low."
    ),
    "technical": "Use precise terminology and compact implementation detail.",
}

_DETAIL_INSTRUCTIONS = {
    "concise": (
        "Use at most three short sentences or three short steps or bullets unless additional "
        "prose is required for safety, accuracy, an approval, or truthful completion reporting. "
        "Include only essential caveats. Do not add background, a full general workflow, a "
        "restatement of the request, or an offer to do more work."
    ),
    "balanced": (
        "Give enough context to understand and act, normally as a short paragraph or "
        "compact list."
    ),
    "detailed": (
        "Give thorough reasoning, steps, tradeoffs, and caveats without repetition."
    ),
}

_HOME_FOCUS_INSTRUCTION = (
    "Prioritize requests about operating, configuring, maintaining, or understanding the "
    "user's home and Home Assistant environment. If a request is clearly unrelated, briefly "
    "explain that this conversation is in Home-focused mode. When relevance is plausible or "
    "ambiguous, help normally. Never use Home Assistant tools merely to determine whether a "
    "request is relevant."
)

# Private protocol token. It is recognized only while Home-focused mode is active,
# stripped before display and retention, and never interpreted as user-facing text.
_HOME_FOCUS_REFUSAL_MARKER = "PHXFOCUS:"


def _conversation_behavior_prompt(
    style: str,
    detail: str,
    *,
    home_focused: bool = False,
) -> str:
    """Build the shared presentation contract for Phoenix-owned model surfaces."""
    style_instruction = _STYLE_INSTRUCTIONS.get(style, _STYLE_INSTRUCTIONS["direct"])
    detail_instruction = _DETAIL_INSTRUCTIONS.get(detail, _DETAIL_INSTRUCTIONS["concise"])
    prompt = (
        "\n\nConversation presentation policy: "
        f"{style_instruction} {detail_instruction} "
        "Style and detail affect user-facing prose only. They never change tools, tool "
        "arguments, verification, factual standards, permissions, approvals, safety decisions, "
        "or completion claims. Explicit operator tone or detail instructions override these "
        "defaults for that response. Suspend humor for failures, urgent situations, "
        "security-sensitive matters, approvals, and destructive actions. Apply the current "
        "style and detail to this response even when earlier assistant messages used different "
        "presentation defaults."
    )
    if home_focused:
        prompt += (
            f"\n\n{_HOME_FOCUS_INSTRUCTION} "
            "A decline must not answer, solve, summarize, or otherwise provide any part of the "
            "unrelated request before or after the brief explanation. Do not make an exception "
            "because the answer is short, easy, harmless, or already known. "
            "If the complete current request explicitly says to answer anyway, answer normally "
            "despite Home-focused mode."
        )
    return prompt


def _home_focus_output_contract(enabled: bool) -> str:
    """Return the private refusal-marker protocol at the final prompt boundary."""
    if not enabled:
        return ""
    return (
        "\n\nMandatory final-output protocol for Home-focused mode: If and only if the "
        "final reply declines the request solely because Home-focused mode is active, output "
        "exactly one brief refusal paragraph. Its first characters must be the literal sentinel "
        f"{_HOME_FOCUS_REFUSAL_MARKER} with the refusal immediately after it. Write nothing "
        "before the sentinel, and do not repeat the refusal or add a second paragraph. Do not "
        "omit, translate, paraphrase, escape, quote, or format the sentinel as markdown. Never "
        "output the sentinel for a normal answer, including a request that explicitly says to "
        "answer anyway."
    )


_AGENTCLI_ADDENDUM = (
    "\n\nYou are the assistant in an interactive chat inside the Home Assistant admin panel. "
    "The operator is present and reading your replies live. Take the actions the operator asks "
    "for directly using the tools above. The operator may name a home resource imprecisely or "
    "state a premise that the observed home does not support. For a named resource, initial "
    "discovery means a narrowly filtered name-and-domain search or the relevant inventory, not "
    "logs, history, traces, or a sweep of related devices. If initial read-only discovery "
    "cannot identify a resource the request depends on, or directly contradicts such a premise, "
    "stop the broad investigation, state the mismatch, and ask one focused clarifying question. "
    "Do not substitute a merely related entity, integration error, or temporal coincidence. "
    "Treat correlation as a lead, never as a cause. If an action needs approval "
    "it will surface an inline Approve/Reject control to the operator; you do not need to poll. "
    "An approved action's result stands as accepted exactly as applied: do not second-guess or "
    "adjust it unprompted. A rejection's reason is the operator steering your next proposal: "
    "address it and try again. Iterating through several proposals and rejections is the normal "
    "way to converge on what the operator wants; stay patient and constructive."
)


def _agentcli_time_context(hass: HomeAssistant) -> str:
    """Tell Agent Chat how to present Home Assistant timestamps locally."""
    now = dt_util.now()
    offset = now.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "UTC"
    return (
        "\n\nHome Assistant's local time zone is "
        f"{hass.config.time_zone} ({offset_label}). The current local date and time at the "
        f"start of this turn is {now.strftime('%Y-%m-%d %H:%M:%S')}. Common temporal tool "
        "results preserve their canonical timestamps and add authoritative companion fields "
        "whose names end in _local. Use the _local value when reasoning or reporting; never "
        "calculate a local value from its canonical counterpart. For get_history, "
        "get_statistics, and get_logbook absolute time arguments, send the intended Home "
        "Assistant wall time without a timezone suffix, for example 2026-08-14T05:00:00. "
        "Phoenix applies the configured zone; do not shift the window yourself. State dates "
        "and times matter-of-factly. Do not mention local time, UTC, offsets, or conversion "
        "unless the operator explicitly asks."
    )


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #

async def _stream_turn_resilient(
    provider: ClaudeProvider | OpenAICompatProvider, session: ClientSession, *,
    system_prompt: str, messages: list[dict], tools: list[dict], options: dict,
    cancel: asyncio.Event,
) -> AsyncIterator[dict]:
    """provider.stream_turn wrapped with a bounded retry for a transient connection
    failure (e.g. a DNS/connect timeout) that occurs BEFORE any content is
    produced. Retries with a short backoff until _CONNECT_RETRY_WINDOW_SECONDS
    elapses, then surfaces the error (annotated that it retried). Never retries
    once any content/tool/done event has been seen, since that could duplicate
    already-streamed output; a mid-stream or non-retryable error passes straight
    through. Every other event is yielded unchanged.
    """
    loop_time = asyncio.get_running_loop().time
    deadline = loop_time() + _CONNECT_RETRY_WINDOW_SECONDS
    attempt = 0
    while True:
        attempt += 1
        produced = False
        retry = False
        async for ev in provider.stream_turn(
            session, system_prompt=system_prompt, messages=messages, tools=tools, options=options,
        ):
            etype = ev.get("type")
            if (etype == EV_ERROR and not produced and ev.get("retryable")
                    and not cancel.is_set()
                    and loop_time() + _CONNECT_RETRY_BACKOFF_SECONDS < deadline):
                retry = True
                break
            if etype == EV_ERROR and attempt > 1:
                detail = str(ev.get("message") or "").strip()
                seconds = int(_CONNECT_RETRY_WINDOW_SECONDS)
                explicit_rate_limit = (
                    ev.get("status") == 429 and ev.get("code") == "rate_limit"
                )
                if explicit_rate_limit:
                    code = "provider_rate_limit_exhausted"
                    message = (
                        f"The provider's rate limit remained exceeded after retrying for about "
                        f"{seconds}s. Provider detail: {detail}"
                    )
                else:
                    code = "provider_retry_exhausted"
                    message = (
                        f"Could not reach the provider after retrying for about "
                        f"{seconds}s. {detail}"
                    )
                ev = {
                    **ev,
                    "code": code,
                    "message": message.strip(),
                    "message_params": {"seconds": seconds, "detail": detail},
                }
            if etype in (EV_TEXT, EV_THINKING, EV_TOOL, EV_DONE):
                produced = True
            yield ev
        if not retry or cancel.is_set():
            return
        await asyncio.sleep(_CONNECT_RETRY_BACKOFF_SECONDS)


@dataclasses.dataclass
class _ModelCallResult:
    """Everything one model call produced, drained from its event stream."""

    tool_calls: list[dict]
    assistant_msg: dict | None
    stop_reason: str
    errored: bool
    saw_text: bool
    usage_input: int
    usage_output: int
    context_tokens: int
    focus_declined: bool


def _strip_focus_marker_text(text: str) -> tuple[str, bool]:
    """Strip a leading private focus marker while preserving ordinary model text."""
    stripped = text.lstrip()
    if not stripped.startswith(_HOME_FOCUS_REFUSAL_MARKER):
        return text, False
    return stripped[len(_HOME_FOCUS_REFUSAL_MARKER):].lstrip(), True


def _strip_focus_marker_message(message: dict) -> tuple[dict, bool]:
    """Strip the marker from Anthropic, OpenAI, or Mistral assistant content."""
    content = message.get("content")
    if isinstance(content, str):
        cleaned, found = _strip_focus_marker_text(content)
        return ({**message, "content": cleaned} if found else message), found
    if not isinstance(content, list):
        return message, False
    for index, block in enumerate(content):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        if not block["text"].strip():
            continue
        cleaned, found = _strip_focus_marker_text(block["text"])
        if not found:
            return message, False
        blocks = list(content)
        blocks[index] = {**block, "text": cleaned}
        return {**message, "content": blocks}, True
    return message, False


@dataclasses.dataclass
class _FocusPrefixBuffer:
    """Hold only enough leading streamed text to recognize the private marker."""

    enabled: bool
    pending: str = ""
    decided: bool = False
    declined: bool = False

    def feed(self, text: str) -> str:
        if not self.enabled or self.decided:
            return text
        self.pending += text
        candidate = self.pending.lstrip()
        if len(self.pending) > len(_HOME_FOCUS_REFUSAL_MARKER) + 64:
            self.decided = True
            visible = self.pending
            self.pending = ""
            return visible
        if _HOME_FOCUS_REFUSAL_MARKER.startswith(candidate):
            return ""
        if candidate.startswith(_HOME_FOCUS_REFUSAL_MARKER):
            self.decided = True
            self.declined = True
            visible, _ = _strip_focus_marker_text(self.pending)
            self.pending = ""
            return visible
        self.decided = True
        visible = self.pending
        self.pending = ""
        return visible

    def finish(self) -> str:
        if not self.pending:
            return ""
        if self.pending.lstrip() == _HOME_FOCUS_REFUSAL_MARKER:
            self.pending = ""
            self.decided = True
            self.declined = True
            return ""
        visible = self.pending
        self.pending = ""
        self.decided = True
        return visible


async def _consume_model_stream(
    provider: ClaudeProvider | OpenAICompatProvider,
    session: ClientSession,
    *,
    system: str,
    messages: list[dict],
    tools: Any,
    options: dict,
    cancel: asyncio.Event,
    emit: Callable[[str, dict], Awaitable[None]],
    usage_done_input: int,
    usage_done_output: int,
    context_tokens: int,
    detect_focus_refusal: bool = False,
) -> _ModelCallResult:
    """Drain one model call's normalized event stream into a result.

    Pure translation of provider events to SSE frames plus accumulation; it
    makes no policy decisions and runs no tools. The usage totals are passed in
    because EV_USAGE is cumulative-REPLACE per call, not additive, so the frame
    the browser receives must add this call's latest report to the finished
    calls' running sum.
    """
    tool_calls: list[dict] = []
    assistant_msg: dict | None = None
    stop_reason = "end_turn"
    errored = False
    saw_text = False
    usage_cur_input = usage_cur_output = 0
    focus = _FocusPrefixBuffer(enabled=detect_focus_refusal)
    focus_event_emitted = False

    async for ev in _stream_turn_resilient(
        provider, session, system_prompt=system, messages=messages,
        tools=tools, options=options, cancel=cancel,
    ):
        etype = ev["type"]
        if etype == EV_TEXT:
            visible = focus.feed(ev["text"])
            if focus.declined and not focus_event_emitted:
                await emit("focus_declined", {})
                focus_event_emitted = True
            if visible:
                saw_text = True
                await emit("assistant_delta", {"text": visible})
        elif etype == EV_THINKING:
            await emit("thinking_delta", {"text": ev["text"]})
        elif etype == EV_TOOL:
            tool_calls.append(ev)
        elif etype == EV_USAGE:
            usage_cur_input = _usage_int(ev.get("input_tokens"))
            usage_cur_output = _usage_int(ev.get("output_tokens"))
            if usage_cur_input:
                context_tokens = usage_cur_input
            await emit("usage", {
                "input_tokens": usage_done_input + usage_cur_input,
                "output_tokens": usage_done_output + usage_cur_output,
                "context_tokens": context_tokens,
            })
        elif etype == EV_DONE:
            stop_reason = ev["stop_reason"]
            assistant_msg = ev["assistant_msg"]
            if detect_focus_refusal and assistant_msg is not None:
                assistant_msg, message_declined = _strip_focus_marker_message(assistant_msg)
                focus.declined = focus.declined or message_declined
                if message_declined and not focus.decided:
                    focus.pending = ""
                    focus.decided = True
                if focus.declined and not focus_event_emitted:
                    await emit("focus_declined", {})
                    focus_event_emitted = True
        elif etype == EV_ERROR:
            await emit("error", {"code": ev.get("code"), "message": ev.get("message"),
                                 "retryable": ev.get("retryable", False),
                                 **({"message_params": ev["message_params"]}
                                    if ev.get("message_params") else {}),
                                 # Mistral's adapter has already reduced response
                                 # headers to a strict allowlist of safe numeric
                                 # quota fields. Preserve that structured snapshot
                                 # through the provider -> SSE translation.
                                 **({"rate_limit": ev["rate_limit"]}
                                    if ev.get("rate_limit") else {}),
                                 # Which of Phoenix's own request keys the provider
                                 # refused, when it named one. The layer holding the
                                 # store records it; nothing down here can.
                                 **({"refused": ev["refused"]} if ev.get("refused") else {})})
            errored = True

    trailing = focus.finish()
    if focus.declined and not focus_event_emitted:
        await emit("focus_declined", {})
        focus_event_emitted = True
    if trailing:
        saw_text = True
        await emit("assistant_delta", {"text": trailing})

    return _ModelCallResult(
        tool_calls=tool_calls, assistant_msg=assistant_msg, stop_reason=stop_reason,
        errored=errored, saw_text=saw_text,
        usage_input=usage_cur_input, usage_output=usage_cur_output,
        context_tokens=context_tokens,
        focus_declined=focus.declined,
    )


@dataclasses.dataclass
class _ToolBatchResult:
    """Outcome of running one round's tool calls."""

    results: list[dict]
    human_resolved: bool
    stop_turn: bool
    authority_lost: bool


async def _run_tool_batch(
    tool_calls: list[dict],
    *,
    hass: HomeAssistant,
    data: PhoenixData,
    token_id: str,
    client_ip: str,
    base_url: str,
    emit: Callable[[str, dict], Awaitable[None]],
    cancel: asyncio.Event,
) -> _ToolBatchResult:
    """Dispatch one round's tool calls, honoring cancel and mid-turn authority loss.

    EVERY tool_use id gets a result even when nothing was dispatched: Anthropic
    requires each one answered in the next message, so a blocked call still
    appends a synthetic error rather than being skipped. That is why this drains
    the whole batch instead of returning early.

    The token is re-resolved per call (_current_dispatch_token), so a mid-turn
    revoke, expiry, kill switch or cap narrowing is honored BEFORE any side
    effect, not just at the start of the turn.
    """
    from .mcp_view import _dispatch_mcp  # noqa: PLC0415

    results: list[dict] = []
    human_resolved = False
    stop_turn = False
    authority_lost = False

    for tc in tool_calls:
        name = str(tc.get("name") or "")[:MAX_TOOL_NAME_LENGTH]
        dispatch_token = None if (cancel.is_set() or stop_turn) \
            else _current_dispatch_token(data, token_id)
        if dispatch_token is None:
            stop_turn = True
            if cancel.is_set():
                reason: str | None = "Cancelled by the operator; not run."
            else:
                authority_lost = True
                reason = "This token is no longer authorized; not run."
            results.append({"tool_use_id": tc.get("id"), "tool_name": name,
                            "result_text": reason, "is_error": True})
            continue
        args = tc.get("input") or {}
        await emit("tool_call", {"id": tc.get("id"), "name": name, "arguments": args})
        # emit() can set cancel if the client vanished mid-batch; re-check
        # AFTER the emit and before the side effect closes that gap.
        if cancel.is_set():
            stop_turn = True
            results.append({"tool_use_id": tc.get("id"), "tool_name": name,
                            "result_text": "Cancelled by the operator; not run.",
                            "is_error": True})
            continue
        if isinstance(args, dict):
            args, time_error = _agentcli_prepare_time_args(
                name, args, dt_util.now().tzinfo or dt_util.UTC,
            )
        else:
            time_error = None
        if time_error is not None:
            await emit("tool_result", {
                "id": tc.get("id"), "name": name,
                "is_error": True, "summary": time_error,
            })
            results.append({
                "tool_use_id": tc.get("id"), "tool_name": name,
                "result_text": time_error, "is_error": True,
            })
            continue
        resp_msg, _m, _r, _o = await _dispatch_with_progress(
            emit, tc.get("id"),
            _dispatch_mcp(
                "tools/call", tc.get("id"),
                {"name": name, "arguments": args},
                dispatch_token, hass, data, client_ip, base_url,
            ),
        )
        result = (resp_msg or {}).get("result", {}) or {}
        pending = _parse_pending(result)
        if pending:
            record = get_approval(data.store, pending["approval_id"])
            await emit("approval_required", {
                "approval_id": pending["approval_id"],
                "tool_name": name,
                "review_url": pending.get("review_url"),
                "expires_at": pending.get("expires_at"),
                "diff": getattr(record, "diff", None) if record else None,
            })
            result = await _await_agent_approval(
                hass, data, pending["approval_id"], cancel, AGENTCLI_APPROVAL_WAIT_SECONDS,
            )
            status = _status_of(result)
            # The transcript shows WHY, not just the status: the admin's
            # typed rejection reason, or the executor's error when an
            # approval executed but failed (status "execution_failed").
            resolved_record = get_approval(data.store, pending["approval_id"])
            reason = None
            if resolved_record is not None:
                reason = execution_failure_text(resolved_record) \
                    or getattr(resolved_record, "rejected_reason", None)
            await emit("approval_resolved", {
                "approval_id": pending["approval_id"],
                "status": status,
                "reason": _clip_display(reason) if reason else None,
            })
            if status in (STATUS_APPROVED, STATUS_REJECTED, "execution_failed"):
                human_resolved = True
        is_error = bool(result.get("isError"))
        content = result.get("content", [])
        text = _agentcli_add_local_time_fields(
            name,
            _flatten_text(content),
            dt_util.now().tzinfo or dt_util.UTC,
        )
        images = _extract_image_blocks(content)
        for index, image in enumerate(images):
            await emit("tool_image", {
                "id": tc.get("id"),
                "name": name,
                "index": index,
                "mime_type": image["mime_type"],
                "data": image["data"],
            })
        await emit("tool_result", {"id": tc.get("id"), "name": name,
                                   "is_error": is_error, "summary": _clip_display(text)})
        model_text = _agentcli_add_resource_match_guidance(name, args, text)
        results.append({"tool_use_id": tc.get("id"), "tool_name": name,
                        "result_text": model_text, "images": images, "is_error": is_error})

    return _ToolBatchResult(
        results=results, human_resolved=human_resolved,
        stop_turn=stop_turn, authority_lost=authority_lost,
    )


async def async_run_agent_turn(
    *, hass: HomeAssistant, data: PhoenixData, token: TokenRecord,
    provider: ClaudeProvider | OpenAICompatProvider,
    session: ClientSession, messages: list[dict], options: dict,
    client_ip: str, base_url: str,
    emit: Callable[[str, dict], Awaitable[None]],
    cancel: asyncio.Event,
    max_iterations: int = AGENTCLI_MAX_ITERATIONS,
    conversation_style: str = CONVERSATION_STYLE_DIRECT,
    detail_level: str = DETAIL_LEVEL_CONCISE,
    home_focused: bool = False,
) -> list[dict]:
    """Run one user turn to completion, streaming SSE events. Returns updated messages.

    The loop body is three phases: drain one model call (_consume_model_stream),
    decide whether to keep the assistant message against the turn output budget,
    and run whatever tool calls it asked for (_run_tool_batch). What stays here
    is the turn-level policy the phases must not own: the runaway-iteration cap
    and its human-in-the-loop reset, the output budget, and how the turn ends.
    """
    from .mcp_view import _build_instructions  # noqa: PLC0415

    system = (
        _build_instructions(token, data, base_url)
        + _agentcli_time_context(hass)
        + _conversation_behavior_prompt(
            conversation_style, detail_level, home_focused=home_focused,
        )
        + _AGENTCLI_ADDENDUM
        + _home_focus_output_contract(home_focused)
    )
    # Current local time is supplied above on every user turn. Keeping GetDateTime
    # out of this one catalog prevents a model from redundantly calling it at the
    # start of a chat. External MCP catalogs continue to announce the tool.
    agent_tools = [
        tool for tool in build_mcp_tool_list(token, data)
        if tool.get("name") != "GetDateTime"
    ]
    tools = provider.format_tools(agent_tools)
    # The token is re-resolved per dispatch (see _current_dispatch_token), so a
    # mid-turn revoke/expire/cap-change/kill-switch is honored before any side
    # effect. Each dispatch gets the inline-confirm wait zeroed so a confirm gate
    # returns pending immediately (snappy approval card); our own longer,
    # cancel-aware wait runs afterward.
    token_id = token.id

    stop_reason = "end_turn"
    saw_text = False
    turn_errored = False
    # Bound the TOTAL provider-generated output retained across the whole turn.
    # The per-response _sse_lines cap bounds one stream, but 12 iterations of it
    # would still let messages[] accumulate a large multiple; this shared budget
    # (measured on the retained assistant messages, which is what is held and
    # re-sent) caps the turn as a whole against a faulty/hostile provider. The
    # check happens BEFORE append (below), so the offending message is never
    # retained and the returned transcript itself stays within the budget.
    turn_output_bytes = 0
    # The safety cap bounds a RUNAWAY model (tools calling tools with no human in
    # the loop). A human approving or rejecting an action proves they are in the
    # loop, so that round resets the budget to zero rather than counting toward it.
    iterations = 0
    # Set when the loop reaches the per-turn cap: the turn ends cleanly (the
    # conversation is left resumable, ending in tool results) and a
    # continue_required checkpoint is emitted so the operator can grant another
    # N rounds. Distinct from a hard error or a completed answer.
    paused_for_continue = False
    # Provider-reported token usage across the turn's model calls. cur_* holds
    # the in-flight call's latest cumulative report (EV_USAGE is replace, not
    # add); done_* sums finished calls. The browser owns cross-turn session
    # accumulation, so the SSE event carries turn-cumulative totals plus the
    # last-known context size (the newest call's input tokens).
    usage_done_input = usage_done_output = 0
    usage_cur_input = usage_cur_output = 0
    context_tokens = 0
    while True:
        if iterations >= max_iterations:
            # Pause at the checkpoint rather than stopping dead: the loop has
            # done max_iterations rounds with no human gating an action, so ask
            # the operator whether to continue. The conversation is resumable
            # (the last round appended its tool results), so a continue request
            # picks up exactly here for another N rounds.
            paused_for_continue = True
            break
        iterations += 1
        if cancel.is_set():
            await emit("error", {"code": "cancelled", "message": "Cancelled."})
            break
        call = await _consume_model_stream(
            provider, session, system=system, messages=messages, tools=tools,
            options=options, cancel=cancel, emit=emit,
            usage_done_input=usage_done_input, usage_done_output=usage_done_output,
            context_tokens=context_tokens,
            detect_focus_refusal=home_focused,
        )
        tool_calls = call.tool_calls
        assistant_msg = call.assistant_msg
        stop_reason = call.stop_reason
        errored = call.errored
        saw_text = saw_text or call.saw_text
        usage_cur_input, usage_cur_output = call.usage_input, call.usage_output
        context_tokens = call.context_tokens
        # Fold the finished call's usage into the turn totals (even on error:
        # a partial call still consumed the tokens it reported).
        usage_done_input += usage_cur_input
        usage_done_output += usage_cur_output
        usage_cur_input = usage_cur_output = 0
        if errored:
            break
        if assistant_msg is not None:
            # Check the prospective total BEFORE appending, and reject the message
            # if it would blow the turn budget, so the offending message is never
            # retained in messages (which is returned/re-sent). The transcript thus
            # stays within the budget rather than budget + one whole response.
            prospective = turn_output_bytes + len(json.dumps(assistant_msg, default=str))
            if prospective > AGENTCLI_MAX_TURN_OUTPUT_BYTES:
                await emit("error", {"code": "output_limit", "message": (
                    "The provider produced too much output this turn; stopped.")})
                turn_errored = True
                break
            provider.append_assistant(messages, assistant_msg)
            turn_output_bytes = prospective
        if stop_reason != "tool_use":
            break

        batch = await _run_tool_batch(
            tool_calls, hass=hass, data=data, token_id=token_id,
            client_ip=client_ip, base_url=base_url, emit=emit, cancel=cancel,
        )
        results = batch.results
        human_resolved = batch.human_resolved
        stop_turn = batch.stop_turn
        authority_lost = batch.authority_lost
        if results:
            provider.append_tool_results(messages, results)
        # A human approved or rejected an action this round: reset the runaway
        # guard, since a human is actively gating the loop.
        if human_resolved:
            iterations = 0
        # The token lost authority mid-batch (revoked/expired/kill-switch): the
        # batch was completed with synthetic results (protocol-safe), now end the
        # turn rather than looping into more denied dispatches.
        if authority_lost:
            await emit("error", {"code": "unauthorized",
                                 "message": "This token is no longer authorized."})
            break
        if stop_turn:
            # Defence in depth, and deliberately NOT test-pinned: this branch has
            # no observable effect today, because every path that sets stop_turn
            # is already handled. Authority loss breaks above; a cancel is caught
            # by the loop-top check BEFORE the next model call, which emits the
            # same "cancelled" error. Mutating this to False leaves the whole
            # suite green, and a test asserting otherwise would be vacuous (one
            # was written and removed). It stays because it makes the turn's exit
            # explicit at the point the batch decided to stop, rather than
            # depending on an ordering argument two screens away.
            if cancel.is_set():
                await emit("error", {"code": "cancelled", "message": "Cancelled."})
            break

    # Make an ambiguous ending explain itself, so a reply the model truncated at
    # its output limit or an empty response never looks like the agent silently
    # stalled (a common, non-reproducible-looking "it just stopped" report).
    if not turn_errored and not cancel.is_set():
        if stop_reason == "max_tokens":
            await emit("notice", {"code": "max_tokens_stop", "message": (
                "The model stopped because it reached its output length limit. "
                "Ask it to continue, or raise the model's output token limit "
                "(for Ollama, its num_predict/context settings)."
            )})
        elif stop_reason == "end_turn" and not saw_text:
            await emit("notice", {"code": "empty_turn", "message": (
                "The model ended the turn without a text reply."
            )})
    # The loop hit the per-turn round cap with no human in the loop: offer to
    # continue instead of stopping silently. Emitted before the messages frame
    # so the client has the resumable conversation to send back on "Continue".
    if paused_for_continue and not cancel.is_set():
        await emit("continue_required", {"iterations": iterations})
    _LOGGER.debug(
        "agentCLI turn ended: stop_reason=%s saw_text=%s errored=%s paused=%s",
        stop_reason, saw_text, turn_errored, paused_for_continue,
    )
    await emit("messages", {"messages": messages})
    await emit("done", {"stop_reason": stop_reason})
    return messages


_HEADLESS_PROVIDER_ERROR = "the language model could not be reached"

# How a headless turn ended. A CODE, not a sentence, because the two surfaces
# render it very differently: voice speaks it, in the CONVERSATION's language via
# const.VOICE_TEMPLATES, while AI Task raises it as an English operator-facing
# error. The loop returning prose forced an English literal into a spoken reply.
TURN_OK = "ok"
TURN_PROVIDER_ERROR = "provider_error"
TURN_OUTPUT_LIMIT = "output_limit"
TURN_EXHAUSTED = "exhausted"
TURN_AUTHORITY_LOST = "authority_lost"
TURN_TRUNCATED = "truncated"


@dataclasses.dataclass
class _HeadlessTurnResult:
    """What one headless turn produced, and how it ended.

    `text` is the model's LAST text (reset at each tool round), so a partial
    answer survives a failure and the caller can still say something useful.

    `outcome` is one of the TURN_* codes and is the whole point of this type:
    every non-OK ending used to be indistinguishable from a normal one. Running
    out of rounds returned an empty string, which AI Task then reported as
    "could not parse structured output" - blaming the model for JSON it never
    got to write. Hitting the model's own output limit returned a half-answer
    with nothing marking it as half.

    `detail` carries the provider's own error text for TURN_PROVIDER_ERROR only.
    """

    text: str
    review_urls: list[str]
    outcome: str = TURN_OK
    detail: str = ""
    focus_declined: bool = False


async def _run_headless_turn(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    provider: ClaudeProvider | OpenAICompatProvider,
    session: ClientSession,
    base_url: str,
    *,
    system: str,
    seed: str,
    client_ip: str,
    max_iterations: int,
    detect_focus_refusal: bool = False,
) -> _HeadlessTurnResult:
    """Run one headless model/tool turn to completion.

    The shared engine behind async_run_voice_turn and async_run_ai_task, which
    were 63 and 61 lines differing in six of them (the error policy, the audit
    sentinel, and collecting review URLs). Everything a surface actually decides
    now lives in its wrapper; everything both surfaces must do identically lives
    here, so a fix to the tool-dispatch path cannot land on one and miss the other.

    HEADLESS means two things, both deliberate. There is no event emission, so
    none of the interactive loop's SSE machinery applies. And a confirm gate is
    never waited on: neither an Assist pipeline nor an ai_task.generate_data call
    can hold a request open for an admin, so a pending approval is fed back as a
    "queued_for_approval" tool result and the turn continues. That is the one
    policy that rules out sharing async_run_agent_turn's _run_tool_batch, which
    blocks up to AGENTCLI_APPROVAL_WAIT_SECONDS on a real human.

    The token is re-resolved before every dispatch (_current_dispatch_token), so a
    mid-turn revoke, expiry, kill switch or cap narrowing is honored before any
    side effect, exactly as on the interactive path.
    """
    from .mcp_view import _dispatch_mcp  # noqa: PLC0415

    tools = provider.format_tools(build_mcp_tool_list(token, data))
    messages: list[dict] = [{"role": "user", "content": seed}]
    token_id = token.id
    cancel = asyncio.Event()  # never fired; the stream primitive expects one
    final_text = ""
    review_urls: list[str] = []
    # Bound the TOTAL provider output retained across the turn, matching
    # async_run_agent_turn. Without it these surfaces retained assistant and tool
    # messages across every round and re-sent the growing transcript on each one,
    # with no ceiling at all against a faulty or hostile provider. Checked BEFORE
    # the append, so the offending message is never retained.
    turn_output_bytes = 0
    # Set when a dispatch was refused because the token lost authority mid-turn.
    # The round that discovers it still finishes and still feeds the refusals
    # back, so the model gets ONE turn to tell the user; asking for tools again
    # after that is stopped rather than repeated (see below).
    authority_lost = False
    # Only a break clears this. Falling out of the for is the runaway case: the
    # round that exhausts the budget is by definition a tool round, which has
    # already reset final_text, so the caller would otherwise receive "".
    exhausted = True
    focus_declined = False

    for _ in range(max_iterations):
        tool_calls: list[dict] = []
        assistant_msg: dict | None = None
        stop_reason = "end_turn"
        call_text = ""
        async for ev in _stream_turn_resilient(
            provider, session, system_prompt=system, messages=messages,
            tools=tools, options={}, cancel=cancel,
        ):
            etype = ev["type"]
            if etype == EV_TEXT:
                call_text += ev["text"]
            elif etype == EV_TOOL:
                tool_calls.append(ev)
            elif etype == EV_DONE:
                stop_reason = ev["stop_reason"]
                assistant_msg = ev["assistant_msg"]
            elif etype == EV_ERROR:
                return _HeadlessTurnResult(
                    final_text, review_urls, outcome=TURN_PROVIDER_ERROR,
                    detail=str(ev.get("message") or _HEADLESS_PROVIDER_ERROR),
                    focus_declined=focus_declined,
                )
        if detect_focus_refusal:
            call_text, text_declined = _strip_focus_marker_text(call_text)
            focus_declined = focus_declined or text_declined
            if assistant_msg is not None:
                assistant_msg, message_declined = _strip_focus_marker_message(assistant_msg)
                focus_declined = focus_declined or message_declined
        final_text += call_text
        if assistant_msg is not None:
            prospective = turn_output_bytes + len(json.dumps(assistant_msg, default=str))
            if prospective > AGENTCLI_MAX_TURN_OUTPUT_BYTES:
                return _HeadlessTurnResult(
                    final_text, review_urls, outcome=TURN_OUTPUT_LIMIT,
                    focus_declined=focus_declined,
                )
            provider.append_assistant(messages, assistant_msg)
            turn_output_bytes = prospective
        if stop_reason != "tool_use":
            exhausted = False
            break
        if authority_lost:
            # The previous round fed back "no longer authorized" for every call
            # and this round asked for tools again rather than answering. Every
            # one would be refused identically, so stop instead of spending the
            # remaining rounds re-reporting the same refusal to the same dead
            # token. The interactive loop ends the turn outright here; headless
            # allows the one intervening round so the model can say what happened.
            exhausted = False
            break

        # The answer is the model's LAST text (after tools resolve), not the
        # intermediate "let me check..." text before a tool round; reset it here.
        final_text = ""
        results: list[dict] = []
        for tc in tool_calls:
            name = str(tc.get("name") or "")[:MAX_TOOL_NAME_LENGTH]
            dispatch_token = _current_dispatch_token(data, token_id)
            if dispatch_token is None:
                authority_lost = True
                results.append({
                    "tool_use_id": tc.get("id"), "tool_name": name,
                    "result_text": "This token is no longer authorized; not run.",
                    "is_error": True,
                })
                continue
            resp_msg, _m, _r, _o = await _dispatch_mcp(
                "tools/call", tc.get("id"),
                {"name": name, "arguments": tc.get("input") or {}},
                dispatch_token, hass, data, client_ip, base_url,
            )
            result = (resp_msg or {}).get("result", {}) or {}
            pending = _parse_pending(result)
            if pending:
                if pending.get("review_url"):
                    review_urls.append(pending["review_url"])
                results.append({
                    "tool_use_id": tc.get("id"), "tool_name": name,
                    "result_text": json.dumps({
                        "status": "queued_for_approval",
                        "message": "Queued for admin approval in the Phoenix MCP panel; not applied yet.",
                    }),
                    "is_error": False,
                })
                continue
            content = result.get("content", [])
            results.append({
                "tool_use_id": tc.get("id"), "tool_name": name,
                "result_text": _flatten_text(content),
                "images": _extract_image_blocks(content),
                "is_error": bool(result.get("isError")),
            })
        provider.append_tool_results(messages, results)

    if authority_lost:
        # Reported ahead of exhaustion: a turn can hit both (authority lost in the
        # final allowed round), and the token dying is the actionable cause. It
        # also cannot be inferred from the outside - the closing round often ends
        # normally, so without this the turn looked like an ordinary success that
        # happened to produce nothing.
        return _HeadlessTurnResult(
            final_text, review_urls, outcome=TURN_AUTHORITY_LOST,
            focus_declined=focus_declined,
        )
    if exhausted:
        return _HeadlessTurnResult(
            final_text, review_urls, outcome=TURN_EXHAUSTED,
            focus_declined=focus_declined,
        )
    if stop_reason == "max_tokens":
        # The model stopped at its OWN output limit, so `text` is a sentence that
        # was cut off mid-thought. Reported rather than returned bare: a spoken
        # half-answer gets acted on, and a half-written JSON document fails to
        # parse and is then blamed on the model's formatting.
        return _HeadlessTurnResult(
            final_text, review_urls, outcome=TURN_TRUNCATED,
            focus_declined=focus_declined,
        )
    return _HeadlessTurnResult(
        final_text, review_urls, focus_declined=focus_declined,
    )


_VOICE_ADDENDUM = (
    "\n\nYou are answering the user through Home Assistant's voice/chat assistant. "
    "Keep replies spoken-friendly, even when the selected detail level is Detailed. "
    "Use the tools above to answer or act. "
    "If a tool result says \"queued_for_approval\", the action has NOT happened yet: it is "
    "waiting for an administrator to approve it in the Phoenix MCP panel. Tell the user it is queued "
    "for approval; do not retry it and do not claim it is done."
)


_VOICE_HOME_FOCUS_BYPASS_PHRASES: dict[str, tuple[str, ...]] = {
    "en": ("answer anyway",),
    "de": ("trotzdem antworten", "trotzdem geantwortet"),
    "es": ("responde de todos modos", "responder de todos modos"),
    "fr": ("réponds quand même", "répondre quand même"),
    "ja": ("そのまま回答",),
    "ko": ("그래도 답변",),
    "nl": ("toch antwoorden", "toch moet worden geantwoord"),
    "pl": ("mimo to odpowiedz", "mimo to odpowiedzieć"),
    "ru": ("ответь в любом случае", "ответить в любом случае"),
    "zh-Hans": ("仍然回答",),
    "zh-Hant": ("仍然回答",),
}


def _voice_home_focus_bypass(user_text: str, language: object = None) -> bool:
    """Recognize the localized explicit Voice override phrase deterministically."""
    normalized = " ".join(user_text.casefold().split())
    phrases = _VOICE_HOME_FOCUS_BYPASS_PHRASES.get(canonical_language(language), ())
    # English remains accepted in every pipeline as the documented protocol
    # literal, while the conversation language accepts its spoken equivalent.
    return any(phrase.casefold() in normalized for phrase in (*phrases, "answer anyway"))


async def async_run_voice_turn(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    provider: ClaudeProvider | OpenAICompatProvider,
    session: ClientSession,
    base_url: str,
    user_text: str,
    *,
    max_iterations: int = AGENTCLI_MAX_ITERATIONS,
    include_review_links: bool = False,
    language: object = None,
) -> str:
    """Run one conversation-agent turn to completion; return the spoken answer.

    A headless sibling of async_run_agent_turn for HA's native Assist/voice pipeline: no
    SSE, no interactive approval wait. A confirm gate returns pending, which is fed
    back to the model as a "queued_for_approval" tool result (the operator approves
    it in the Phoenix MCP panel), so a voice turn never blocks. The token is re-resolved
    before every dispatch, so a mid-turn revoke/expire/kill-switch/cap-narrow is
    honored before any side effect, exactly like async_run_agent_turn. Reuses the same
    provider stream, tool list, and gated dispatch primitives.

    include_review_links appends a clickable approval link for anything queued this
    turn. The caller passes True only for a TEXT surface (typed Assist chat): a
    conversation reply is both displayed and spoken from one string, so a link would
    otherwise be read aloud by TTS on a voice satellite.

    language is the CONVERSATION's language, threaded through for the same reason
    voice_agent's declines take one: a spoken reply goes to whoever is talking, not
    to the server. Every sentence this function can produce that is not the model's
    own words comes from const.VOICE_TEMPLATES; they were English literals here
    until the turn-outcome split, so a Chinese pipeline heard a translated decline
    and then an English apology the moment a turn failed. None falls back to English.
    """
    from .mcp_view import _build_instructions  # noqa: PLC0415

    settings = data.store.get_settings()
    home_focused = settings.voice_agent_home_focused and not _voice_home_focus_bypass(
        user_text, language
    )
    behavior = _conversation_behavior_prompt(
        settings.voice_agent_conversation_style,
        settings.voice_agent_detail_level,
        home_focused=home_focused,
    )

    turn = await _run_headless_turn(
        hass, data, token, provider, session, base_url,
        system=(
            _build_instructions(token, data, base_url)
            + behavior
            + _VOICE_ADDENDUM
            + _home_focus_output_contract(home_focused)
        ),
        seed=user_text, client_ip=VOICE_AGENT_CLIENT_IP, max_iterations=max_iterations,
        detect_focus_refusal=home_focused,
    )
    # Never raise at a microphone: every ending becomes something sayable, in the
    # CONVERSATION's language (voice_text), not the server's. A partial answer the
    # model already produced is preferred over an apology that discards it.
    if turn.outcome == TURN_PROVIDER_ERROR:
        return turn.text.strip() or voice_text(language, "provider_error", reason=turn.detail)
    if turn.outcome == TURN_OUTPUT_LIMIT:
        return turn.text.strip() or voice_text(language, "output_limit")
    if turn.outcome == TURN_AUTHORITY_LOST:
        # The closing round usually produces a sentence ("I could not do that");
        # say it. The fallback covers a model that asked for tools again instead.
        return turn.text.strip() or voice_text(language, "token_unavailable")
    if turn.outcome == TURN_EXHAUSTED:
        # By construction there is no partial answer here: the round that runs out
        # of steps is a tool round, which has already reset the text.
        return voice_text(language, "out_of_steps")

    if turn.focus_declined:
        # A weak model can emit a marked refusal and then answer the unrelated
        # request anyway. Voice has no visible transcript to preserve, so render
        # the refusal deterministically and never speak the model's mixed content.
        answer = (
            f"{voice_text(language, 'focus_declined')} "
            f"{voice_text(language, 'focus_answer_anyway')}"
        )
    else:
        answer = turn.text.strip() or voice_text(language, "could_not_complete")
    if turn.outcome == TURN_TRUNCATED and turn.text.strip():
        # Said out loud, because a cut-off answer is acted on as a whole one.
        answer = f"{answer} {voice_text(language, 'answer_truncated')}"
    if include_review_links and turn.review_urls:
        # Deterministically append a clickable approval link (the Assist chat renders
        # the reply as markdown). Absolute when a base URL is known so it clicks from
        # anywhere; the review_url is already a same-origin path like /phoenix-mcp#approvals/id.
        links = [f"{base_url}{u}" if base_url else u for u in turn.review_urls]
        joined = " ".join(f"[Approve in the Phoenix MCP panel]({href})" for href in links)
        answer = f"{answer}\n\n{joined}"
    return answer


_AI_TASK_ADDENDUM = (
    "\n\nYou are performing a Home Assistant AI Task: generate the requested data from "
    "the instructions. Use the tools above only when you need live data to answer. "
    "If a tool result says \"queued_for_approval\", that action is waiting for an "
    "administrator and has NOT happened; do not retry it. Return only the requested "
    "result as your final message, with no extra commentary."
)

_AI_TASK_STRUCTURE_DIRECTIVE = (
    "\n\nYour final message MUST be a single valid JSON value conforming to this JSON "
    "schema, with no prose, explanation, or markdown code fences:\n{schema}"
)


def _ai_task_free_text_output_contract(style: str, detail: str) -> str:
    """Repeat free-text presentation defaults at AI Task's final-output boundary."""
    style_instruction = _STYLE_INSTRUCTIONS.get(style, _STYLE_INSTRUCTIONS["direct"])
    detail_instruction = _DETAIL_INSTRUCTIONS.get(detail, _DETAIL_INSTRUCTIONS["balanced"])
    return (
        "\n\nAI Task free-text output contract: "
        f"{style_instruction} {detail_instruction} "
        "Return only the requested data. Do not mention tools, available entities, the task "
        "process, or additional work Phoenix could do unless the task instructions request it."
    )


class AiTaskError(Exception):
    """The AI Task model loop failed (provider/stream error)."""


async def async_run_ai_task(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    provider: ClaudeProvider | OpenAICompatProvider,
    session: ClientSession,
    base_url: str,
    instructions: str,
    *,
    structure_json: dict | None = None,
    max_iterations: int = AGENTCLI_MAX_ITERATIONS,
) -> str:
    """Run one AI Task (ai_task.generate_data) to completion; return the final text.

    A headless sibling of async_run_voice_turn for HA's AI Task platform: same gated tool
    loop (per-token scope, MESA, approvals, audit via _dispatch_mcp with sentinel
    client_ip "ai_task"), no SSE, no interactive wait. A confirm gate returns pending,
    fed back as a "queued_for_approval" tool result so a task never blocks. The token
    is re-resolved before every dispatch, so a mid-task revoke/kill-switch is honored.

    When structure_json is given (the caller's JSON schema derived from the task's
    structure), the model is instructed to emit only matching JSON; the caller parses
    the returned text.
    """
    from .mcp_view import _build_instructions  # noqa: PLC0415

    settings = data.store.get_settings()
    system = _build_instructions(token, data, base_url)
    if structure_json is None:
        system += _conversation_behavior_prompt(
            settings.ai_task_conversation_style,
            settings.ai_task_detail_level,
        )
    system += _AI_TASK_ADDENDUM
    if structure_json is None:
        system += _ai_task_free_text_output_contract(
            settings.ai_task_conversation_style,
            settings.ai_task_detail_level,
        )
    else:
        system += _AI_TASK_STRUCTURE_DIRECTIVE.format(schema=json.dumps(structure_json))

    turn = await _run_headless_turn(
        hass, data, token, provider, session, base_url,
        system=system, seed=instructions, client_ip=AI_TASK_CLIENT_IP,
        max_iterations=max_iterations,
    )
    # Unlike the voice surface there is no one listening to an apology: an
    # automation asked for data, so every non-OK ending is an error the caller
    # wraps as a HomeAssistantError. These messages are operator-facing and
    # deliberately English, like the rest of the agent-facing error text.
    if turn.outcome == TURN_PROVIDER_ERROR:
        raise AiTaskError(turn.detail)
    if turn.outcome == TURN_OUTPUT_LIMIT:
        raise AiTaskError("the model produced too much output this turn")
    if turn.outcome == TURN_AUTHORITY_LOST:
        raise AiTaskError(
            "the token lost authority mid-task (revoked, expired, or the kill "
            "switch); at least one tool call was refused, so the result is unsound"
        )
    if turn.outcome == TURN_EXHAUSTED:
        raise AiTaskError(
            "the model ran out of steps before returning a result; raise "
            "Steps before check-in in Phoenix MCP settings if the task needs more"
        )
    if turn.outcome == TURN_TRUNCATED:
        raise AiTaskError(
            "the model stopped at its own output length limit, so the result is "
            "incomplete; raise the model's output token limit"
        )
    return turn.text.strip()


def _status_of(result: dict) -> str:
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text", ""))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("status"):
                return parsed["status"]
    return STATUS_APPROVED if not result.get("isError") else STATUS_REJECTED


def _zero_inline_wait(token: TokenRecord) -> TokenRecord:
    """Return a copy of the token with confirm_inline_wait_seconds zeroed.

    Falls back to the original token if it is not a dataclass.
    """
    try:
        return dataclasses.replace(token, confirm_inline_wait_seconds=0)
    except (TypeError, ValueError):
        return token


def _current_dispatch_token(data: PhoenixData, token_id: str) -> TokenRecord | None:
    """Re-resolve the token for one tool dispatch, or None if it may no longer act.

    A single Agent Chat turn can stay open a long time (a confirm gate blocks on
    an approval wait), so authority must be re-checked before EVERY side effect,
    not just at stream open, mirroring async_get_authenticated_token on the MCP path.
    Returns None when Phoenix MCP is shutting down, the runtime kill switch is on, or the
    token has been revoked/expired or narrowed away since the turn started; the
    fresh record is returned otherwise so cap changes take effect immediately.
    """
    if (
        getattr(data, "ready", True) is False
        or getattr(data, "shutting_down", False) is True
        or data.store.get_settings().kill_switch
    ):
        return None
    token = data.store.get_token_by_id(token_id)
    if token is None or not token.is_valid():
        return None
    return _zero_inline_wait(token)


# --------------------------------------------------------------------------- #
# HTTP views
# --------------------------------------------------------------------------- #

class PhoenixAgentCliChatView(PhoenixView):
    """POST /api/phoenix-mcp/agentcli/chat - the streaming agent turn (admin, SSE)."""

    url = "/api/phoenix-mcp/agentcli/chat"
    name = "api:phoenix-mcp:agentcli:chat"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        data = self.hass.data.get(DOMAIN)
        if data is None:
            return _err(
                "not_found", "Phoenix MCP is not set up.", 404, rid,
                key="setupMissing",
            )

        # Agent Chat bypasses async_get_authenticated_token (it is admin-authed, not
        # bearer-authed), so it must enforce the same invariants that gate every
        # MCP request: shutdown, the runtime kill switch, and token validity.
        settings = data.store.get_settings()
        if not data.ready or data.shutting_down or settings.kill_switch:
            return _err(
                "service_unavailable", "Phoenix MCP is disabled (kill switch).", 503, rid,
                key="serviceDisabled",
            )

        token_id = str(body.get("token_id") or "")
        token = data.store.get_token_by_id(token_id)
        if token is None:
            return _err(
                "not_found", "Token not found.", 404, rid,
                key="tokenNotFound",
            )
        if not token.is_valid():
            return _err(
                "invalid_request", "This token is revoked or expired.", 400, rid,
                key="tokenInactive",
            )

        instance_id = str(body.get("instance_id") or "")
        store = await _get_secret_store(self.hass)
        cfg = store.resolve(instance_id, body.get("model") or None)
        if cfg is None:
            return _err(
                "invalid_request", "Provider account is not configured.", 400, rid,
                key="providerNotConfigured",
            )
        if not cfg.model:
            return _err(
                "invalid_request", "No model selected for this provider.", 400, rid,
                key="providerModelMissing",
            )

        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []
        bypass_supplied = "home_focus_bypass" in body
        home_focus_bypass = body.get("home_focus_bypass", False)
        if bypass_supplied and not isinstance(home_focus_bypass, bool):
            return _err(
                "invalid_request", "home_focus_bypass must be a boolean (true or false).",
                400, rid, key="booleanRequired", params={"field": "home_focus_bypass"},
            )
        if body.get("continue") is True:
            if bypass_supplied:
                return _err(
                    "invalid_request",
                    "home_focus_bypass is valid only with a new user message.",
                    400, rid, key="homeFocusBypassNewMessage",
                )
            # Resume a turn that paused at the round-cap checkpoint: no new user
            # message is appended; the model continues from the tool results the
            # prior turn left behind. Requires an existing conversation.
            if not messages:
                return _err(
                    "invalid_request", "Nothing to continue.", 400, rid,
                    key="nothingToContinue",
                )
        else:
            user_text = str(body.get("user") or "")
            if not user_text.strip():
                return _err(
                    "invalid_request", "Empty message.", 400, rid,
                    key="emptyMessage",
                )
            messages = [*messages, {"role": "user", "content": user_text}]
        options = body.get("options") if isinstance(body.get("options"), dict) else {}

        provider = build_provider(cfg)
        session = async_get_clientsession(self.hass)
        base_url = str(request.url.origin())
        client_ip = AGENTCLI_CLIENT_IP or get_client_ip(request)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Phoenix-Request-ID": rid,
            },
        )
        await resp.prepare(request)
        cancel = asyncio.Event()

        async def emit(name: str, payload: dict) -> None:
            # Learn from a refusal the model gave during REAL work. This is the
            # backstop under the declared and probed answers: it costs nothing
            # extra, it is ground truth rather than an inference, and it covers
            # the providers that publish nothing and validate nothing, which
            # neither of the other two mechanisms can reach.
            refused = payload.get("refused") if name == "error" else None
            if refused and cfg.model:
                await store.record_refusal(instance_id, cfg.model, refused, utcnow().isoformat())
            frame = f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"
            try:
                await resp.write(frame.encode("utf-8"))
            except (ConnectionResetError, ClientError, RuntimeError):
                cancel.set()

        async def _heartbeat() -> None:
            # Send an SSE comment periodically so an idle intermediary (reverse
            # proxy, Nabu Casa, a backgrounded tab) does not drop the streaming
            # connection during a quiet stretch, e.g. while the model is thinking
            # or an approval is pending. Comment frames carry no data: line, so the
            # panel's parser ignores them. A failed write means the client is gone.
            try:
                while not cancel.is_set():
                    await asyncio.sleep(AGENTCLI_HEARTBEAT_SECONDS)
                    try:
                        await resp.write(b": keepalive\n\n")
                    except (ConnectionResetError, ClientError, RuntimeError):
                        cancel.set()
                        return
            except asyncio.CancelledError:
                pass

        heartbeat = self.hass.loop.create_task(_heartbeat())
        try:
            await emit("ready", {"provider": cfg.kind, "model": cfg.model, "token_id": token_id})
            await async_run_agent_turn(
                hass=self.hass, data=data, token=token, provider=provider,
                session=session, messages=messages, options=options or {},
                client_ip=client_ip, base_url=base_url, emit=emit, cancel=cancel,
                max_iterations=settings.agentcli_max_iterations,
                conversation_style=settings.agentcli_conversation_style,
                detail_level=settings.agentcli_detail_level,
                home_focused=settings.agentcli_home_focused and not home_focus_bypass,
            )
        except Exception:  # noqa: BLE001 - never leak internals to the client
            _LOGGER.exception("agentCLI turn failed")
            await emit("error", {"code": "internal", "message": "Internal error."})
        finally:
            heartbeat.cancel()
            try:
                await resp.write_eof()
            except (ConnectionResetError, ClientError, RuntimeError):
                pass
        return cast(web.Response, resp)


def _ip_is_blocked(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool,
) -> bool:
    """Return whether an operator-supplied endpoint may target this address."""
    return (
        ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or (not allow_private and (ip.is_private or ip.is_loopback))
    )


async def _validate_base_url(
    url: str, *, allow_private: bool = True,
) -> ProbeConfigError | None:
    """Validate an admin-supplied provider base URL.

    Phoenix MCP makes server-side requests to this URL, so an admin with a stolen session
    could otherwise use it as an SSRF pivot from the HA host. Constrain it:
    http/https only, no embedded credentials or fragment, and no host that
    IS or RESOLVES TO a link-local / cloud-metadata address (169.254.169.254 and
    the fe80::/10 range, incl. DNS names like metadata.google.internal or a
    *.nip.io that points there). Loopback and private addresses are deliberately
    allowed, a local Ollama lives on one. The provider requests also run with
    redirects disabled, so an accepted host cannot 302 to metadata. This is a
    validation-time check, not connection-time, so it does not defeat DNS
    rebinding, which is out of scope for an admin-only config surface. Returns an
    localized error contract, or None when acceptable.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return ProbeConfigError("Enter a valid server URL.", "providerUrlInvalid")
    schemes = ("http", "https") if allow_private else ("https",)
    if parts.scheme not in schemes:
        if allow_private:
            return ProbeConfigError(
                "The server URL must start with http:// or https://.",
                "providerUrlScheme",
            )
        return ProbeConfigError(
            "The provider URL must start with https://.",
            "providerRemoteUrlScheme",
        )
    if not parts.hostname:
        return ProbeConfigError(
            "Enter a valid server URL including a host.", "providerUrlHostRequired",
        )
    if parts.username or parts.password:
        return ProbeConfigError(
            "The server URL must not contain embedded credentials.",
            "providerUrlCredentialsForbidden",
        )
    if parts.fragment:
        return ProbeConfigError(
            "The server URL must not contain a '#' fragment.",
            "providerUrlFragmentForbidden",
        )
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal, allow_private=allow_private):
            if allow_private:
                return ProbeConfigError(
                    "The server URL must not point at a link-local or metadata address.",
                    "providerUrlAddressForbidden",
                )
            return ProbeConfigError(
                "The provider URL must not point at a private, loopback, "
                "link-local, multicast, or reserved address.",
                "providerRemoteUrlAddressForbidden",
            )
        return None
    # A hostname: resolve it and reject if ANY address is link-local/metadata.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None)
    except (OSError, ValueError):
        return None  # unresolvable now; the actual request just fails cleanly
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if _ip_is_blocked(ip, allow_private=allow_private):
            if allow_private:
                return ProbeConfigError(
                    "The server URL resolves to a link-local or metadata address.",
                    "providerUrlResolutionForbidden",
                )
            return ProbeConfigError(
                "The provider URL resolves to a private, loopback, link-local, "
                "multicast, or reserved address.",
                "providerRemoteUrlResolutionForbidden",
            )
    return None


async def _probe_config(kind: str, body: dict) -> ProviderConfig | ProbeConfigError:
    """Build a throwaway config or a localized setup error contract."""
    definition = PROVIDER_DEFINITIONS.get(kind)
    if definition is None:
        return ProbeConfigError("Unknown provider.", "providerUnknown")
    api_key: str | None = None
    if any(field.id == "api_key" for field in definition.fields):
        api_key = str(body.get("api_key") or "").strip()
        if not api_key:
            return ProbeConfigError("Enter your API key.", "providerApiKeyRequired")

    endpoint_id: str | None = None
    if definition.endpoints:
        endpoint_id = str(body.get("endpoint_id") or definition.endpoints[0].value)
        if endpoint_id not in {choice.value for choice in definition.endpoints}:
            return ProbeConfigError(
                "Choose a valid provider plan.", "providerEndpointInvalid",
            )

    if any(field.id == "base_url" for field in definition.fields):
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            if kind == "ollama":
                return ProbeConfigError(
                    "Enter the Ollama server URL.", "ollamaUrlRequired",
                )
            return ProbeConfigError(
                "Enter the provider base URL.", "providerBaseUrlRequired",
            )
        err = await _validate_base_url(base_url, allow_private=kind == "ollama")
        if err:
            return err
    else:
        base_url = _default_base_url(kind, endpoint_id)
    model = str(body.get("model") or "").strip()
    return ProviderConfig(kind=kind, model=model or _default_model(kind),
                          base_url=base_url.rstrip("/"), api_key=api_key,
                          endpoint_id=endpoint_id)


def _stored_setup_config(cfg: ProviderConfig, model: str = "") -> dict[str, Any]:
    """Return only the setup values this provider persists, never fixed URLs."""
    definition = PROVIDER_DEFINITIONS[cfg.kind]
    out: dict[str, Any] = {}
    if any(field.id == "api_key" for field in definition.fields):
        out["api_key"] = cfg.api_key
    if any(field.id == "base_url" for field in definition.fields):
        out["base_url"] = cfg.base_url
    if cfg.endpoint_id:
        out["endpoint_id"] = cfg.endpoint_id
    if model:
        out["model"] = model
    return out


class PhoenixAgentCliProvidersView(PhoenixView):
    """GET (list accounts) / POST (create an account) for agentCLI providers."""

    url = "/api/phoenix-mcp/admin/agentcli/providers"
    name = "api:phoenix-mcp:admin:agentcli:providers"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        store = await _get_secret_store(self.hass)
        return _ok({
            "instances": store.list_instances(),
            "provider_types": _provider_catalog(),
        }, request_id=rid)

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        kind = str(body.get("kind") or "")
        if kind not in PROVIDER_DEFINITIONS:
            return _err(
                "invalid_request", "Unknown provider.", 400, rid,
                key="providerUnknown",
            )

        probe = await _probe_config(kind, body)
        if isinstance(probe, ProbeConfigError):
            return _err(
                "invalid_request", probe.message, 400, rid,
                key=probe.key, params=probe.params,
            )

        model = str(body.get("model") or "").strip()
        cfg = _stored_setup_config(probe, model)

        store = await _get_secret_store(self.hass)
        if store.has_duplicate(kind, cfg):
            return _err(
                "already_exists", str(DuplicateProviderError()), 409, rid,
                key="providerAlreadyConfigured",
            )

        session = async_get_clientsession(self.hass)
        provider = build_provider(probe)
        ok, reason = await provider.validate(session)
        if not ok:
            return _err(
                "invalid_request", f"Connection failed: {reason}", 400, rid,
                key="providerConnectionFailed", params={"detail": reason},
            )

        try:
            instance_id = await store.add(kind, cfg)
        except DuplicateProviderError as err:
            return _err(
                "already_exists", str(err), 409, rid,
                key="providerAlreadyConfigured",
            )
        # Initial setup must establish the same declared model capabilities as
        # the explicit Refresh models action. This is metadata-only (no model
        # completion calls), but it may be one /api/show request per model for
        # Ollama. The account is already valid and stored, so a transient
        # catalogue failure must not turn a successful setup into a failure.
        try:
            models = await provider.list_models(session)
            capabilities = await provider.list_model_capabilities(session, models)
            await store.set_capabilities(instance_id, capabilities, utcnow().isoformat())
        except Exception:  # noqa: BLE001 - capability discovery is best effort
            _LOGGER.debug("Initial Agent Chat capability discovery failed", exc_info=True)
        created = next((i for i in store.list_instances() if i["id"] == instance_id), None)
        return _ok({"instance": created}, status=201, request_id=rid)


class PhoenixAgentCliProviderView(PhoenixView):
    """DELETE /api/phoenix-mcp/admin/agentcli/providers/{instance_id} - remove one account."""

    url = "/api/phoenix-mcp/admin/agentcli/providers/{instance_id}"
    name = "api:phoenix-mcp:admin:agentcli:provider"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, instance_id: str) -> web.Response:
        """Change this account's default model.

        The model is NOT validated against the provider's live list here, and
        that is deliberate: the panel offers the discovered list, but a
        self-hosted server can serve a model its own index does not advertise,
        and a network round trip inside a PATCH would let an unreachable
        provider block an edit the operator can already see is correct. The UI
        guides; the endpoint records.
        """
        rid = request.get("phoenix_mcp_rid", "")
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        model = str(body.get("model") or "").strip()
        if not model:
            return _err(
                "invalid_request", "A model is required.", 400, rid,
                key="providerModelRequired",
            )
        store = await _get_secret_store(self.hass)
        if not await store.set_model(instance_id, model):
            return _err(
                "not_found", "Provider account not found.", 404, rid,
                key="providerAccountNotFound",
            )
        return _ok({"instance": {"id": instance_id, "model": model}}, request_id=rid)

    @require_admin
    async def delete(self, request: web.Request, instance_id: str) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        store = await _get_secret_store(self.hass)
        data = self.hass.data.get(DOMAIN)
        if data is None:
            await store.delete(instance_id)
            return _ok({"deleted": instance_id}, request_id=rid)

        # Settings PATCH validates provider references under this same lock. Delete
        # the provider and clear every dependent reference in one transaction so a
        # validated PATCH cannot persist the ID after it disappears.
        async with data.settings_update_lock:
            await store.delete(instance_id)
            async with data.store.async_lock:
                settings = data.store.get_settings()
                voice_removed = settings.voice_agent_provider_id == instance_id
                ai_task_removed = settings.ai_task_provider_id == instance_id
                patchable: dict[str, Any] = {}
                if voice_removed:
                    patchable.update(
                        voice_agent_provider_id=None,
                        voice_agent_model=None,
                    )
                if ai_task_removed:
                    patchable.update(
                        ai_task_provider_id=None,
                        ai_task_model=None,
                    )
                if patchable:
                    await data.store.async_patch_settings(**patchable)

            if voice_removed:
                if data.async_sync_voice_agent is not None:
                    data.async_sync_voice_agent()
                # The voice agent lost its provider, so it is now unregistered;
                # remove any Phoenix-created Assist pipeline as well.
                from .voice_agent import async_remove_assist_pipeline  # noqa: PLC0415
                await async_remove_assist_pipeline(self.hass, data)
            if ai_task_removed and data.async_sync_ai_task is not None:
                # The AI Task is no longer fully configured; remove its entity
                # from Home Assistant and the AI Task picker.
                data.async_sync_ai_task()
        return _ok({"deleted": instance_id}, request_id=rid)


class PhoenixAgentCliModelsView(PhoenixView):
    """GET /api/phoenix-mcp/admin/agentcli/providers/{instance_id}/models - available models."""

    url = "/api/phoenix-mcp/admin/agentcli/providers/{instance_id}/models"
    name = "api:phoenix-mcp:admin:agentcli:models"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, instance_id: str) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        store = await _get_secret_store(self.hass)
        cfg = store.resolve(instance_id)
        if cfg is None:
            return _err(
                "invalid_request", "Provider account is not configured.", 400, rid,
                key="providerNotConfigured",
            )
        session = async_get_clientsession(self.hass)
        models = await build_provider(cfg).list_models(session)
        return _ok({"models": models}, request_id=rid)


class PhoenixAgentCliRefreshView(PhoenixView):
    """POST /api/phoenix-mcp/admin/agentcli/providers/{instance_id}/refresh.

    Re-read one account's model list AND whatever capabilities its provider
    declares, then store both with a timestamp. Separate from the GET models
    endpoint because the two cost very different things: listing models is one
    cheap request and runs whenever the settings card is opened, while Ollama's
    capabilities cost one request PER MODEL, which is an explicit-button amount
    of work rather than a page-load amount.

    Nothing here spends completion tokens; probing the knobs that no provider
    declares is a later, separately-consented step.
    """

    url = "/api/phoenix-mcp/admin/agentcli/providers/{instance_id}/refresh"
    name = "api:phoenix-mcp:admin:agentcli:refresh"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, instance_id: str) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        store = await _get_secret_store(self.hass)
        cfg = store.resolve(instance_id)
        if cfg is None:
            return _err(
                "invalid_request", "Provider account is not configured.", 400, rid,
                key="providerNotConfigured",
            )
        session = async_get_clientsession(self.hass)
        provider = build_provider(cfg)
        models = await provider.list_models(session)
        capabilities = await provider.list_model_capabilities(session, models)
        checked_at = utcnow().isoformat()
        if not await store.set_capabilities(instance_id, capabilities, checked_at):
            return _err(
                "not_found", "Provider account not found.", 404, rid,
                key="providerAccountNotFound",
            )
        # `declared` tells the panel whether this provider reports capabilities at
        # all, so it can say "this provider does not publish them" instead of
        # showing an empty result that reads like a failed refresh.
        return _ok({
            "models": models,
            "capabilities": capabilities,
            "declared": bool(capabilities),
            "checked_at": checked_at,
        }, request_id=rid)


class PhoenixAgentCliProbeCapsView(PhoenixView):
    """POST /api/phoenix-mcp/admin/agentcli/providers/{instance_id}/probe.

    Ask the provider what its SELECTED model actually accepts, by sending a
    handful of one-token completions and reading the status codes.

    A SEPARATE endpoint from /refresh, and the separation is the consent
    boundary rather than tidiness: /refresh reads catalogues and costs nothing,
    while this spends the operator's own credit on real completion requests.
    Anything that bills someone gets its own button and its own confirmation.

    Only the selected model is probed. Probing a catalogue would multiply the
    spend by models nobody has chosen, and the only model whose knobs are about
    to be used is this one.
    """

    url = "/api/phoenix-mcp/admin/agentcli/providers/{instance_id}/probe"
    name = "api:phoenix-mcp:admin:agentcli:probecaps"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request, instance_id: str) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        store = await _get_secret_store(self.hass)
        cfg = store.resolve(instance_id)
        if cfg is None:
            return _err(
                "invalid_request", "Provider account is not configured.", 400, rid,
                key="providerNotConfigured",
            )
        if not cfg.model:
            return _err(
                "invalid_request", "Choose a model for this account first.", 400, rid,
                key="providerChooseModel",
            )
        session = async_get_clientsession(self.hass)
        probed, calls, answered = await async_probe_capabilities(session, cfg)

        # Merged over what the provider DECLARED rather than replacing it: the two
        # answer different questions (declared says what exists, probed says what
        # this endpoint accepts) and a probe that established nothing must leave
        # the declared answer standing.
        stored = store.get(instance_id) or {}
        existing = dict(stored.get("capabilities") or {})
        merged = {**(existing.get(cfg.model) or {}), **probed}
        existing[cfg.model] = merged
        checked_at = utcnow().isoformat()
        if not await store.set_capabilities(instance_id, existing, checked_at):
            return _err(
                "not_found", "Provider account not found.", 404, rid,
                key="providerAccountNotFound",
            )
        return _ok({
            "model": cfg.model,
            "probed": probed,
            "calls": calls,
            "checked_at": checked_at,
            # Whether this backend has an effort control to ask about AT ALL.
            # Without it the panel cannot tell "asked, and the provider ignores
            # unknown options" from "there was nothing here to ask", and it
            # reported the second as the first on a backend whose thinking
            # control is a boolean flag.
            "effort_checkable": _effort_probe_body(cfg.kind, AGENTCLI_PROBE_SENTINEL) is not None,
            # False when the provider declined every question (no credit, a bad
            # key, unreachable). Reporting that as a finding about the model
            # would blame the provider for an account problem.
            "answered": answered,
        }, request_id=rid)


class PhoenixAgentCliProbeView(PhoenixView):
    """POST /api/phoenix-mcp/admin/agentcli/probe.

    Validate a submitted credential/base URL and list the provider's models
    WITHOUT storing anything, so the Settings form can populate a model dropdown
    before the operator commits (and can cancel with no side effect).
    """

    url = "/api/phoenix-mcp/admin/agentcli/probe"
    name = "api:phoenix-mcp:admin:agentcli:probe"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        rid = request.get("phoenix_mcp_rid", "")
        body = await _read_body(request, rid)
        if isinstance(body, web.Response):
            return body
        kind = str(body.get("kind") or "")
        if kind not in PROVIDER_DEFINITIONS:
            return _err(
                "invalid_request", "Unknown provider.", 400, rid,
                key="providerUnknown",
            )

        probe = await _probe_config(kind, body)
        if isinstance(probe, ProbeConfigError):
            return _ok(probe.payload(), request_id=rid)
        cfg = _stored_setup_config(probe)
        store = await _get_secret_store(self.hass)
        if store.has_duplicate(kind, cfg):
            return _err(
                "already_exists", str(DuplicateProviderError()), 409, rid,
                key="providerAlreadyConfigured",
            )
        session = async_get_clientsession(self.hass)
        provider = build_provider(probe)
        ok, reason = await provider.validate(session)
        if not ok:
            if reason:
                return _ok(
                    {
                        "ok": False,
                        "error": reason,
                        "message_passthrough": True,
                        "models": [],
                    },
                    request_id=rid,
                )
            return _ok(
                ProbeConfigError(
                    "Connection failed.", "providerConnectionFailedNoDetail",
                ).payload(),
                request_id=rid,
            )
        models = await provider.list_models(session)
        return _ok({"ok": True, "models": models}, request_id=rid)


# Admin config/models views are kill-switch-immune (like the rest of the admin
# API); the streaming chat view is agent activity and is registered through the
# kill-switch-gated route set in __init__.py.
ALL_AGENTCLI_ADMIN_VIEWS: list[type[PhoenixView]] = [
    PhoenixAgentCliProvidersView,
    PhoenixAgentCliProviderView,
    PhoenixAgentCliModelsView,
    PhoenixAgentCliRefreshView,
    PhoenixAgentCliProbeCapsView,
    PhoenixAgentCliProbeView,
]
ALL_AGENTCLI_CHAT_VIEWS: list[type[PhoenixView]] = [
    PhoenixAgentCliChatView,
]
