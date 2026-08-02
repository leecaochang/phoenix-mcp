"""Phoenix MCP as a Home Assistant AI Task entity.

Registers Phoenix MCP as an HA AITaskEntity ("Phoenix MCP AI Task"), so Phoenix MCP appears in the AI Task
entity picker and `ai_task.generate_data` runs through Phoenix MCP's OWN model loop on a
chosen token's scope, with per-token permissions, MESA safety, approvals, and audit,
no separate LLM integration required. This is the AI-Task-surface counterpart to the
voice agent (voice_agent.py) and the llm.API bridge (assist_api.py).

The model side is reused from agentcli.py (the Agent Chat engine): the provider
accounts + keys (AgentCliStore), provider construction, and the gated tool loop
(agentcli.async_run_ai_task dispatches every tool through mcp_view._dispatch_mcp with the
sentinel client_ip "ai_task", so scope, MESA, approvals, and audit apply identically).
A confirm-gated action degrades to "queued, approve in the Phoenix MCP panel"; the task never
blocks. Structured output (a task `structure`) is honored by converting the schema to
JSON, instructing the model to emit matching JSON, and parsing the result.

This file is NAMED ai_task.py deliberately: HA forwards the AI_TASK platform by
importing `<integration>/ai_task.py`, so the name IS the platform registration (the
opposite of voice_agent.py, which avoids conversation-ENTITY auto-discovery). The
`homeassistant.components.ai_task` import is guarded (feature-detect, like assist_api /
voice_agent) so Phoenix MCP setup never hard-crashes and this module still imports in a
minimal environment, where that import's transitive dependency chain is unavailable.
The generation core (async_generate_ai_task_data) is module-level and does NOT touch
the ai_task base class, so it works without it. HA-COUPLING POINT alongside
ws_dispatch / assist_api / voice_agent: the AI Task entity API can shift; re-verify on
upgrades.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ai_task shipped in HA 2025.7 and its import chain reaches camera -> turbojpeg, which
# a minimal environment may not have. Guard it so Phoenix MCP setup never crashes; the
# entity class is defined only when it imports, and async_setup_entry no-ops otherwise.
try:
    from homeassistant.components import ai_task as _ai_task

    _AI_TASK_AVAILABLE = True
except ImportError:  # pragma: no cover - absent only on older/minimal HA
    _ai_task = None  # type: ignore[assignment]
    _AI_TASK_AVAILABLE = False

# Concurrent AI Tasks are independent; do not serialize them behind one another.
PARALLEL_UPDATES = 0


def ai_task_supported() -> bool:
    """Whether the running HA exposes the AI Task entity platform."""
    return _AI_TASK_AVAILABLE


class AiTaskSetupError(Exception):
    """A one-click AI Task preference operation could not complete."""


def _ai_task_entity_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """The registered entity_id of Phoenix MCP's AI Task entity, or None if absent."""
    return er.async_get(hass).async_get_entity_id("ai_task", DOMAIN, f"{entry_id}_ai_task")


def _ai_task_preferences(hass: HomeAssistant) -> Any:
    """HA's AITaskPreferences object (holds gen_data_entity_id), or None."""
    try:
        from homeassistant.components.ai_task.const import DATA_PREFERENCES  # noqa: PLC0415
    except ImportError:  # pragma: no cover - older/minimal HA
        return None
    return hass.data.get(DATA_PREFERENCES)


def ai_task_preferred_status(hass: HomeAssistant, entry) -> dict:
    """Current 'Data generation tasks' default vs Phoenix MCP's entity, for the panel.

    supported: HA exposes both the entity platform and the preferences store.
    entity_id: Phoenix MCP's AI Task entity (null until fully configured, since it only
    exists then). gen_data_entity_id/gen_data_name: whatever is currently the default
    (so the panel can warn before overwriting it). is_preferred: Phoenix MCP is that default.
    """
    entity_id = _ai_task_entity_id(hass, entry.entry_id) if _AI_TASK_AVAILABLE else None
    prefs = _ai_task_preferences(hass)
    current = getattr(prefs, "gen_data_entity_id", None) if prefs is not None else None
    name = None
    if current:
        state = hass.states.get(current)
        name = state.name if state else current
    return {
        "supported": _AI_TASK_AVAILABLE and prefs is not None,
        "entity_id": entity_id,
        "gen_data_entity_id": current,
        "gen_data_name": name,
        "is_preferred": entity_id is not None and current == entity_id,
    }


def set_ai_task_preferred(hass: HomeAssistant, entry) -> dict:
    """Make Phoenix MCP's AI Task entity HA's default 'Data generation tasks' entity.

    Requires the entity to exist (fully configured). Overwrites any prior default
    (there is only one), which the panel confirms first. Returns the new status.
    """
    entity_id = _ai_task_entity_id(hass, entry.entry_id)
    if entity_id is None:
        raise AiTaskSetupError(
            "Configure and enable the Phoenix MCP AI Task (token, provider, and model) first."
        )
    prefs = _ai_task_preferences(hass)
    if prefs is None:
        raise AiTaskSetupError("AI Task preferences are not available on this Home Assistant.")
    prefs.async_set_preferences(gen_data_entity_id=entity_id)
    return ai_task_preferred_status(hass, entry)


def clear_ai_task_preferred(hass: HomeAssistant, entry) -> dict:
    """Clear the default 'Data generation tasks' entity if it is Phoenix MCP's. Best-effort."""
    entity_id = _ai_task_entity_id(hass, entry.entry_id)
    prefs = _ai_task_preferences(hass)
    if prefs is not None and entity_id is not None and prefs.gen_data_entity_id == entity_id:
        prefs.async_set_preferences(gen_data_entity_id=None)
    return ai_task_preferred_status(hass, entry)


def _is_fully_configured(settings: Any) -> bool:
    return bool(
        settings.ai_task_enabled and settings.ai_task_token_id
        and settings.ai_task_provider_id and settings.ai_task_model
    )


def _structure_to_json(structure: Any) -> dict | None:
    """Convert a task's voluptuous structure schema to a JSON schema, or None."""
    if structure is None:
        return None
    try:
        from voluptuous_openapi import convert  # noqa: PLC0415

        return convert(structure)
    except Exception as err:  # noqa: BLE001 - unavailable/unconvertible schema
        raise HomeAssistantError(
            "This AI Task's structured output is not supported by the Phoenix MCP AI Task entity."
        ) from err


def _parse_structured(text: str) -> Any:
    """Parse the model's final text as JSON, tolerating markdown code fences."""
    body = text.strip()
    if body.startswith("```"):
        # Strip a leading ```json / ``` fence and its closing ```.
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        return json.loads(body.strip())
    except (json.JSONDecodeError, ValueError) as err:
        _LOGGER.error("Phoenix MCP AI Task: could not parse structured output: %s", err)
        raise HomeAssistantError(
            "The Phoenix MCP AI Task did not return valid JSON for the requested structure."
        ) from err


async def async_generate_ai_task_data(
    hass: HomeAssistant, data: Any, instructions: str, structure: Any
) -> Any:
    """Resolve config + token + provider, run the gated AI Task loop, return the data.

    Module-level (not a method) so it is testable without the ai_task base class.
    Returns a string (no structure) or a parsed JSON value (with a structure). Raises
    HomeAssistantError for any not-ready or generation failure, so the service call
    surfaces a clean error rather than a Phoenix MCP internal.
    """
    from .agentcli import (  # noqa: PLC0415
        AiTaskError,
        _current_dispatch_token,
        _get_secret_store,
        build_provider,
        async_run_ai_task,
    )

    settings = data.store.get_settings()
    if data.shutting_down or settings.kill_switch:
        raise HomeAssistantError("The Phoenix MCP AI Task entity is currently unavailable.")
    if not _is_fully_configured(settings):
        raise HomeAssistantError("The Phoenix MCP AI Task entity is not fully configured.")

    token = _current_dispatch_token(data, settings.ai_task_token_id)
    if token is None:
        raise HomeAssistantError("The Phoenix MCP AI Task entity's token is no longer available.")

    store = await _get_secret_store(hass)
    cfg = store.resolve(settings.ai_task_provider_id, settings.ai_task_model or None)
    if cfg is None or not cfg.model:
        raise HomeAssistantError("The Phoenix MCP AI Task entity is not fully configured.")

    structure_json = _structure_to_json(structure)
    provider = build_provider(cfg)
    session = async_get_clientsession(hass)
    try:
        text = await async_run_ai_task(
            hass, data, token, provider, session, _base_url(hass), instructions,
            structure_json=structure_json,
            max_iterations=data.store.get_settings().agentcli_max_iterations,
        )
    except AiTaskError as err:
        raise HomeAssistantError(f"The Phoenix MCP AI Task could not be completed: {err}") from err

    if structure_json is None:
        return text
    return _parse_structured(text)


def _base_url(hass: HomeAssistant) -> str:
    """Best-effort external URL for the skill link in the primer; "" if none."""
    try:
        from homeassistant.helpers.network import get_url  # noqa: PLC0415

        return get_url(hass)
    except Exception:  # noqa: BLE001 - NoURLAvailableError and anything else
        return ""


if _AI_TASK_AVAILABLE:

    class PhoenixAITaskEntity(_ai_task.AITaskEntity):
        """An AI Task entity that runs Phoenix MCP's scoped model loop for one bound token."""

        _attr_supported_features = _ai_task.AITaskEntityFeature.GENERATE_DATA
        _attr_name = "Phoenix MCP AI Task"

        def __init__(self, hass: HomeAssistant, entry: Any) -> None:
            self.hass = hass
            self._attr_unique_id = f"{entry.entry_id}_ai_task"

        @property
        def available(self) -> bool:
            """Available while it exists, unless kill-switched/shutting down.

            The entity only exists when fully configured (async_sync_ai_task adds it
            then and removes it otherwise), so availability tracks the kill switch: on
            during a kill switch it stays present but unavailable, recovering when the
            switch clears, rather than churning the entity in and out.
            """
            data = self.hass.data.get(DOMAIN)
            if data is None or data.shutting_down:
                return False
            settings = data.store.get_settings()
            return _is_fully_configured(settings) and not settings.kill_switch

        async def _async_generate_data(self, task: Any, chat_log: Any) -> Any:
            """Generate data for one ai_task.generate_data call."""
            data = self.hass.data.get(DOMAIN)
            if data is None:
                raise HomeAssistantError("Phoenix MCP is not set up.")
            result = await async_generate_ai_task_data(
                self.hass, data, task.instructions, task.structure
            )
            return _ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id,
                data=result,
            )

else:  # pragma: no cover - ai_task absent only on older/minimal HA
    PhoenixAITaskEntity = None  # type: ignore[assignment,misc]


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    """Set up the Phoenix MCP AI Task platform.

    The entity is not added unconditionally: it exists only while the voice-agent-style
    AI Task config is complete, so it never shows in HA's AI Task picker as an
    unconfigured or disabled option. A sync closure (stored on PhoenixData) adds/removes it
    live when the settings change, no restart needed.
    """
    if not _AI_TASK_AVAILABLE:
        return
    data = hass.data.get(DOMAIN)
    if data is None:
        return

    @callback
    def _sync() -> None:
        async_sync_ai_task(hass, config_entry, data, async_add_entities)

    data.async_sync_ai_task = _sync
    _sync()  # add the entity now if already fully configured


@callback
def async_sync_ai_task(hass: HomeAssistant, entry, data, async_add_entities) -> None:
    """Add or remove the Phoenix MCP AI Task entity to match current settings.

    Present only when fully configured (enabled + token + provider + model); the kill
    switch does NOT remove it (it stays present-but-unavailable, recovering when the
    switch clears). Removal drops the entity-registry entry, which cascades to removing
    the live entity, so it disappears from HA and the AI Task picker.
    """
    if not _AI_TASK_AVAILABLE:
        return
    should_exist = _is_fully_configured(data.store.get_settings())

    if should_exist and data.ai_task_entity is None:
        entity = PhoenixAITaskEntity(hass, entry)
        data.ai_task_entity = entity
        async_add_entities([entity])
    elif not should_exist:
        data.ai_task_entity = None
        # Remove any registry entry for our unique_id (covers both a tracked entity and
        # a leftover from a previous run where config changed while HA was down).
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("ai_task", DOMAIN, f"{entry.entry_id}_ai_task")
        if entity_id is not None:
            # If Phoenix MCP was HA's default "Data generation tasks" entity, clear that first
            # so a removed entity is not left behind as a dangling default (disable,
            # revoke, provider delete, and wipe all route through here).
            prefs = _ai_task_preferences(hass)
            if prefs is not None and prefs.gen_data_entity_id == entity_id:
                prefs.async_set_preferences(gen_data_entity_id=None)
            registry.async_remove(entity_id)
