"""Phoenix MCP as a Home Assistant conversation agent.

Registers Phoenix MCP directly as a conversation agent (conversation.async_set_agent), so
"Phoenix MCP" appears in Settings > Voice assistants and drives HA's native Assist/voice
pipeline using Phoenix MCP's OWN model loop, no separate LLM integration required. This is
the self-contained counterpart to the llm.API bridge (assist_api.py, which hands
Phoenix MCP's tools to a model supplied by another integration).

Everything model-side is reused from agentcli.py (the Agent Chat engine): the
provider accounts + keys (AgentCliStore), provider construction, and the gated
tool loop (async_run_voice_turn dispatches every tool through mcp_view._dispatch_mcp, so
per-token scope, MESA, approvals, and audit apply identically). A confirm-gated
action degrades to "queued, approve in the Phoenix MCP panel" (voice has no inline Approve
button); the turn never blocks. The bound token, provider, and model are configured
in Phoenix MCP's own panel (GlobalSettings.voice_agent_*), not an HA config-entry options flow.

This module is deliberately NOT named conversation.py so HA does not treat it as a
conversation ENTITY platform; Phoenix MCP uses the lighter non-entity async_set_agent path.
HA-COUPLING POINT alongside ws_dispatch / assist_api: the conversation agent API
(AbstractConversationAgent / ConversationResult / IntentResponse) can shift across
HA versions; re-verify on upgrades.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .helpers import voice_text

_LOGGER = logging.getLogger(__name__)

# conversation is a guaranteed core integration in any real HA, but guard the
# import so Phoenix MCP setup never hard-crashes if its deps are ever unavailable (and so
# the module imports in a minimal env). Mirrors assist_api's feature-detect.
try:
    from homeassistant.components import conversation as _conversation

    _CONVERSATION_AVAILABLE = True
except ImportError:  # pragma: no cover - conversation absent only in broken envs
    _conversation = None  # type: ignore[assignment]
    _CONVERSATION_AVAILABLE = False


def _base_url(hass: HomeAssistant) -> str:
    """Best-effort external URL for the skill link in the primer; "" if none."""
    try:
        from homeassistant.helpers.network import get_url  # noqa: PLC0415

        return get_url(hass)
    except Exception:  # noqa: BLE001 - NoURLAvailableError and anything else
        return ""


async def async_voice_answer(
    hass: HomeAssistant,
    data: Any,
    user_text: str,
    *,
    include_review_links: bool = False,
    language: object = None,
) -> str:
    """Resolve config + token + provider, then run the gated voice loop; return text.

    The real work, kept independent of the conversation base class so it is testable
    without the (heavy) conversation component. Never raises: every failure degrades
    to a spoken sentence. include_review_links is passed through to async_run_voice_turn
    (True only for a text surface; see async_process). Every decline sentence is
    spoken back to the caller, so it is rendered in the CONVERSATION's language
    (helpers.voice_text), not the server's; language=None falls back to English.
    """
    from .agentcli import (  # noqa: PLC0415
        _current_dispatch_token,
        _get_secret_store,
        build_provider,
        async_run_voice_turn,
    )

    settings = data.store.get_settings()
    if data.shutting_down or settings.kill_switch or not settings.voice_agent_enabled:
        return voice_text(language, "unavailable")
    if not (settings.voice_agent_token_id and settings.voice_agent_provider_id):
        return voice_text(language, "not_configured")

    # Re-resolve the token fresh every turn (never cache state); also zeroes the
    # inline-confirm wait so a confirm gate returns pending immediately and the loop
    # reports it queued rather than blocking a voice turn.
    token = _current_dispatch_token(data, settings.voice_agent_token_id)
    if token is None:
        return voice_text(language, "token_unavailable")

    store = await _get_secret_store(hass)
    cfg = store.resolve(settings.voice_agent_provider_id, settings.voice_agent_model or None)
    if cfg is None or not cfg.model:
        return voice_text(language, "not_configured")

    provider = build_provider(cfg)
    session = async_get_clientsession(hass)
    try:
        return await async_run_voice_turn(
            hass, data, token, provider, session, _base_url(hass), user_text,
            include_review_links=include_review_links,
            max_iterations=data.store.get_settings().agentcli_max_iterations,
            language=language,
        )
    except Exception:  # noqa: BLE001 - a provider/loop failure must not raise
        _LOGGER.exception("Phoenix MCP voice agent turn failed")
        return voice_text(language, "error")


if _CONVERSATION_AVAILABLE:

    class PhoenixConversationAgent(_conversation.AbstractConversationAgent):
        """A conversation agent that runs Phoenix MCP's scoped model loop for one bound token."""

        def __init__(self, hass: HomeAssistant, data: Any) -> None:
            self.hass = hass
            self._data = data

        @property
        def supported_languages(self) -> list[str] | Literal["*"]:
            # Phoenix MCP does not gate on language; the underlying model handles it.
            return MATCH_ALL

        async def async_process(
            self, user_input: "_conversation.ConversationInput"
        ) -> "_conversation.ConversationResult":
            """Answer one utterance, spoken as the final assistant text."""
            # A voice satellite sets satellite_id; only then would an appended link be
            # read aloud by TTS. Absent it (the typed Assist chat), include the link.
            text = await async_voice_answer(
                self.hass, self._data, user_input.text,
                include_review_links=getattr(user_input, "satellite_id", None) is None,
                language=user_input.language,
            )
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(text)
            return _conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id,
            )

else:  # pragma: no cover - conversation absent only in broken envs
    PhoenixConversationAgent = None  # type: ignore[assignment,misc]


def async_register_voice_agent(hass: HomeAssistant, entry: Any, data: Any) -> None:
    """Register (or replace) Phoenix MCP's conversation agent for this config entry.

    Idempotent (async_set_agent replaces by entry_id); never raises. Absent when
    the conversation component is unavailable.
    """
    if not _CONVERSATION_AVAILABLE:
        return
    try:
        _conversation.async_set_agent(hass, entry, PhoenixConversationAgent(hass, data))
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not register the Phoenix MCP voice agent", exc_info=True)


def async_unregister_voice_agent(hass: HomeAssistant, entry: Any) -> None:
    """Unregister Phoenix MCP's conversation agent. Idempotent; never raises."""
    if not _CONVERSATION_AVAILABLE:
        return
    try:
        _conversation.async_unset_agent(hass, entry)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Voice agent unregister failed", exc_info=True)


def async_sync_voice_agent(hass: HomeAssistant, entry: Any, data: Any) -> None:
    """Register or unregister the agent to match current settings.

    Registered when fully configured (enabled + token + provider + model), so "Phoenix MCP"
    never appears as a conversation agent that answers "not configured"; a deleted
    provider or revoked token clears its field and unregisters the agent.

    The kill switch is deliberately NOT part of this gate. When it is on the agent
    stays registered but async_voice_answer declines every turn ("currently unavailable")
    before resolving the token or dispatching any tool, so nothing runs. Keeping it
    registered means an Assist pipeline pointed at Phoenix MCP (the one-click setup, which
    can be the operator's preferred assistant) degrades to a spoken "unavailable"
    during a kill switch instead of a hard "agent not found" error, and recovers on
    its own when the kill switch is turned back off. This matches how Phoenix MCP's other
    in-process, admin-only surfaces (the panel, the admin API) are unaffected by the
    kill switch, which gates the network route surface, not in-process helpers.
    """
    s = data.store.get_settings()
    fully_configured = bool(
        s.voice_agent_enabled and s.voice_agent_token_id
        and s.voice_agent_provider_id and s.voice_agent_model
    )
    if fully_configured and not data.shutting_down:
        async_register_voice_agent(hass, entry, data)
    else:
        async_unregister_voice_agent(hass, entry)


# --- One-click Assist pipeline setup -------------------------------------------------
#
# Phoenix MCP can create an Assist pipeline that points at its own conversation agent so the
# operator does not have to wire it up by hand in Settings > Voice assistants. This
# reaches into assist_pipeline internals (the Pipeline store, the private default
# settings resolver), a HA-COUPLING POINT re-verified on upgrades; every access is
# guarded and degrades to a clean error, and the private resolver has a hand-built
# text-only fallback so a resolver rename only loses STT/TTS auto-population.

try:  # assist_pipeline is a default HA integration but guard like the rest.
    from homeassistant.components.assist_pipeline.pipeline import (  # noqa: PLC0415
        KEY_ASSIST_PIPELINE as _KEY_ASSIST_PIPELINE,
    )

    _ASSIST_PIPELINE_AVAILABLE = True
except ImportError:  # pragma: no cover - assist_pipeline absent only in minimal envs
    _KEY_ASSIST_PIPELINE = None  # type: ignore[assignment]
    _ASSIST_PIPELINE_AVAILABLE = False


class VoicePipelineError(Exception):
    """A one-click Assist pipeline operation could not complete."""


def assist_pipeline_supported() -> bool:
    """Whether the running HA exposes the Assist pipeline store seam (one-click)."""
    return _ASSIST_PIPELINE_AVAILABLE


def _pipeline_store(hass: HomeAssistant) -> Any:
    """Return the Assist pipeline storage collection, or None if unavailable."""
    if not _ASSIST_PIPELINE_AVAILABLE:
        return None
    pipeline_data = hass.data.get(_KEY_ASSIST_PIPELINE)
    return getattr(pipeline_data, "pipeline_store", None)


def _resolve_pipeline_settings(hass: HomeAssistant, entry_id: str) -> dict:
    """Build pipeline settings for Phoenix MCP's agent (best-effort STT/TTS defaults)."""
    try:
        from homeassistant.components.assist_pipeline.pipeline import (  # noqa: PLC0415
            _async_resolve_default_pipeline_settings,
        )

        settings = _async_resolve_default_pipeline_settings(
            hass, conversation_engine_id=entry_id, pipeline_name="Phoenix MCP"
        )
    except Exception:  # noqa: BLE001 - private helper drift: fall back to text-only
        # WARNING, not debug: the fallback silently produces a pipeline with no
        # speech-to-text and no text-to-speech, so an operator who asked for a
        # voice assistant gets a text-only one and has no signal that anything
        # degraded. The broad catch stays (the resolver is private and may raise
        # for reasons beyond an import failure), but it stops being invisible.
        _LOGGER.warning(
            "Assist pipeline settings resolver unavailable; creating a text-only "
            "pipeline with no STT/TTS. Set these in Settings > Voice assistants.",
            exc_info=True,
        )
        lang = hass.config.language or "en"
        settings = {
            "conversation_engine": entry_id,
            "conversation_language": lang,
            "language": lang,
            "name": "Phoenix MCP",
            "stt_engine": None,
            "stt_language": None,
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    # Phoenix MCP must actually receive the utterance, so never prefer local intent handling.
    # The dict mixes str/None/bool values, so it is not a dict[str, str | None].
    settings["prefer_local_intents"] = False  # type: ignore[assignment]
    return settings


async def async_create_assist_pipeline(
    hass: HomeAssistant, entry: Any, data: Any, *, preferred: bool
) -> dict:
    """Create (or return the existing) Phoenix MCP Assist pipeline; optionally set preferred.

    Requires the voice agent to be fully configured so its conversation agent is
    registered and the pipeline resolves to a live agent. Persists the created
    pipeline id on settings so teardown can remove it. Raises VoicePipelineError on
    any problem the caller should surface.
    """
    store = _pipeline_store(hass)
    if store is None:
        raise VoicePipelineError("Assist pipeline support is not available.")

    s = data.store.get_settings()
    if not (
        s.voice_agent_enabled and s.voice_agent_token_id
        and s.voice_agent_provider_id and s.voice_agent_model
    ):
        raise VoicePipelineError(
            "Configure and enable the Phoenix MCP voice agent (token, provider, and model) first."
        )

    # Idempotent: if a tracked pipeline still exists, reuse it rather than duplicate.
    existing = s.voice_agent_pipeline_id
    if existing is not None and existing in getattr(store, "data", {}):
        if preferred:
            _set_preferred(store, existing)
        item = store.data[existing]
        return {"pipeline_id": existing, "name": getattr(item, "name", "Phoenix MCP"),
                "preferred": _is_preferred(store, existing)}

    # Ensure the agent is registered so conversation_engine resolves to a live agent.
    async_register_voice_agent(hass, entry, data)
    settings = _resolve_pipeline_settings(hass, entry.entry_id)
    try:
        pipeline = await store.async_create_item(settings)
    except Exception as err:  # noqa: BLE001
        raise VoicePipelineError("Could not create the Phoenix MCP Assist pipeline.") from err

    if preferred:
        _set_preferred(store, pipeline.id)

    async with data.store.async_lock:
        await data.store.async_patch_settings(voice_agent_pipeline_id=pipeline.id)
    return {"pipeline_id": pipeline.id, "name": getattr(pipeline, "name", "Phoenix MCP"),
            "preferred": _is_preferred(store, pipeline.id)}


async def async_remove_assist_pipeline(hass: HomeAssistant, data: Any) -> None:
    """Delete the Phoenix-created Assist pipeline if one is tracked. Best-effort.

    Clears the tracked id even if the pipeline is already gone. A preferred pipeline
    cannot be deleted directly, so preference is first moved to any other pipeline;
    if Phoenix MCP's is the only one it is left in place (HA keeps at least one pipeline).
    """
    s = data.store.get_settings()
    pipeline_id = s.voice_agent_pipeline_id
    if pipeline_id is None:
        return

    store = _pipeline_store(hass)
    if store is not None and pipeline_id in getattr(store, "data", {}):
        try:
            if _is_preferred(store, pipeline_id):
                other = next((pid for pid in store.data if pid != pipeline_id), None)
                if other is None:
                    # Phoenix MCP's is the only/preferred pipeline; HA keeps at least one,
                    # so leave it (clearing the tracked id below is enough).
                    _LOGGER.debug("Phoenix MCP pipeline is the only preferred one; not deleting")
                else:
                    _set_preferred(store, other)
            if not _is_preferred(store, pipeline_id):
                await store.async_delete_item(pipeline_id)
        except Exception:  # noqa: BLE001 - never let cleanup raise
            _LOGGER.warning("Could not remove the Phoenix MCP Assist pipeline", exc_info=True)

    async with data.store.async_lock:
        await data.store.async_patch_settings(voice_agent_pipeline_id=None)


def _is_preferred(store: Any, pipeline_id: str) -> bool:
    try:
        return store.async_get_preferred_item() == pipeline_id
    except Exception:  # noqa: BLE001
        return False


def _set_preferred(store: Any, pipeline_id: str) -> None:
    try:
        store.async_set_preferred_item(pipeline_id)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not set preferred Assist pipeline", exc_info=True)
