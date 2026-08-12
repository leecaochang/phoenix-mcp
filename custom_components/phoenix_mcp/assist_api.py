"""Assist bridge: expose Phoenix MCP's scoped tools to HA's native Assist/voice pipeline.

Registers a Home Assistant ``llm.API`` whose tools are the Phoenix MCP tool surface of a
single admin-chosen (bound) token. Every tool call flows through the same
``_dispatch_mcp`` path an external MCP client uses, so per-token scoping, MESA,
the approval gate, and the audit log all apply identically. Confirm-gated actions
degrade to "queued, approve in the Phoenix MCP panel": a voice turn cannot show inline
Approve/Reject buttons, so the tool reports the action as queued and returns
(the per-call token copy forces ``confirm_inline_wait_seconds`` to 0 so the turn
never blocks). The bound token stays a normal, live-editable Phoenix MCP token; it is
re-resolved fresh on every instance build and every tool call, so cap/scope edits
take effect on the next call with no re-registration.

HA-COUPLING POINT (re-verify on HA upgrades, same posture as ws_dispatch):
``homeassistant.helpers.llm`` (async_register_api / API / Tool / APIInstance) is
under active refactoring, and ``voluptuous_openapi.convert_to_voluptuous``
translates each tool's JSON-Schema parameters into the voluptuous schema
``llm.Tool`` advertises (HA's own ``mcp`` client integration uses the same call).
Both are feature-detected at import: when either is missing the bridge simply
never registers (``assist_api_supported()`` is False) and the panel shows a
"requires HA X+" hint, decoupled from MIN_HA_VERSION like the MESA injector.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import ASSIST_CLIENT_IP, DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers import llm as _llm
    from voluptuous_openapi import convert_to_voluptuous as _convert_to_voluptuous

    _ASSIST_API_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on HA version
    _llm = None  # type: ignore[assignment]
    _convert_to_voluptuous = None
    _ASSIST_API_SUPPORTED = False


def assist_api_supported() -> bool:
    """True when the running HA exposes the llm.API + schema-conversion seam."""
    return _ASSIST_API_SUPPORTED


_UNBOUND_PROMPT = (
    "No Home Assistant token is currently bound to Phoenix MCP. An administrator must bind "
    "a token in the Phoenix MCP panel (Tokens, then Tool Announcement & Assist) before this "
    "assistant can control anything. Until then you have no tools available."
)

_ASSIST_ADDENDUM = (
    "You are talking to a user through Home Assistant's voice/chat assistant. Keep "
    "answers brief and spoken-friendly. When a tool reports status "
    "\"queued_for_approval\", the action has NOT happened yet: it is waiting for an "
    "administrator to approve it in the Phoenix MCP panel. Tell the user it is queued for "
    "approval; do not retry it and do not claim it is done."
)


def _base_url(hass: HomeAssistant) -> str:
    """Best-effort external URL for the skill link in the primer; "" if none."""
    try:
        from homeassistant.helpers.network import get_url  # noqa: PLC0415

        return get_url(hass)
    except Exception:  # noqa: BLE001 - NoURLAvailableError and anything else
        return ""


def _unwrap_result(result: dict) -> dict:
    """Turn an MCP CallToolResult into the JSON object HA's Tool contract expects.

    Phoenix MCP tool results are ``{"content": [{"type": "text", "text": ...}], "isError"?}``.
    Most tools put a JSON object in that text; errors put a plain sentence. A dict
    passes through verbatim; anything else is wrapped so the return is always a dict.
    """
    from .agentcli import _flatten_text  # noqa: PLC0415

    text = _flatten_text(result.get("content", []))
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if result.get("isError"):
        return parsed if isinstance(parsed, dict) else {"error": text}
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        return {"result": parsed}
    return {"result": text}


if _ASSIST_API_SUPPORTED:

    class PhoenixAssistTool(_llm.Tool):
        """One announced Phoenix MCP tool, dispatched through the bound token's scope."""

        def __init__(self, name: str, description: str | None, parameters: Any, token_id: str) -> None:
            self.name = name
            self.description = description
            self.parameters = parameters
            self._token_id = token_id

        async def async_call(self, hass, tool_input, llm_context) -> dict:
            """Run one tool call end-to-end through Phoenix MCP's scoped dispatch path."""
            from .agentcli import _current_dispatch_token, _parse_pending  # noqa: PLC0415
            from .mcp_view import _dispatch_mcp  # noqa: PLC0415

            data = hass.data[DOMAIN]
            # Re-resolve the bound token per call: it may have been revoked,
            # expired, narrowed, or the kill switch flipped since the conversation
            # started. _current_dispatch_token also zeroes confirm_inline_wait so a
            # voice turn never blocks on an approval; instead the gate returns
            # pending and we report it as queued below.
            token = _current_dispatch_token(data, self._token_id)
            if token is None:
                return {"error": "This Phoenix MCP token is no longer available."}

            resp_msg, _m, _r, _o = await _dispatch_mcp(
                "tools/call",
                tool_input.id,
                {"name": self.name, "arguments": tool_input.tool_args or {}},
                token,
                hass,
                data,
                ASSIST_CLIENT_IP,
                _base_url(hass),
            )
            result = (resp_msg or {}).get("result", {}) or {}
            pending = _parse_pending(result)
            if pending:
                return {
                    "status": "queued_for_approval",
                    "approval_id": pending["approval_id"],
                    "message": (
                        "This action is queued for approval. An administrator must "
                        "approve it in the Phoenix MCP panel before it takes effect."
                    ),
                }
            return _unwrap_result(result)

    class PhoenixAssistAPI(_llm.API):
        """An llm.API whose tools are the bound Phoenix MCP token's scoped tool surface."""

        def __init__(self, hass: HomeAssistant, data: Any) -> None:
            super().__init__(hass=hass, id=DOMAIN, name="Phoenix MCP (scoped)")
            self._data = data

        async def async_get_api_instance(
            self, llm_context: "_llm.LLMContext"
        ) -> "_llm.APIInstance":
            """Build the per-turn tool set + primer for the currently bound token."""
            data = self._data
            settings = data.store.get_settings()
            token = None
            # Resolve the bound token FRESH every turn (never cache token state).
            # Unbound, missing, invalid, or Phoenix MCP disabled -> zero tools, never raise
            # (raising would fail the whole conversation turn).
            if data.ready and not data.shutting_down and not settings.kill_switch:
                token_id = settings.assist_bound_token_id
                if token_id:
                    candidate = data.store.get_token_by_id(token_id)
                    if candidate is not None and candidate.is_valid():
                        token = candidate

            if token is None:
                return _llm.APIInstance(
                    api=self,
                    api_prompt=_UNBOUND_PROMPT,
                    llm_context=llm_context,
                    tools=[],
                )

            from .agentcli import build_mcp_tool_list  # noqa: PLC0415
            from .mcp_view import _build_instructions  # noqa: PLC0415

            tools: list[Any] = []
            for tool_def in build_mcp_tool_list(token, data):
                name = tool_def.get("name")
                try:
                    parameters = _convert_to_voluptuous(tool_def.get("inputSchema") or {})
                except Exception as err:  # noqa: BLE001 - one bad schema must not
                    # take down the whole voice assistant; skip just that tool.
                    _LOGGER.warning(
                        "Assist bridge: skipping tool %s (schema conversion failed: %s)",
                        name,
                        err,
                    )
                    continue
                # name/description come from a tool def dict, so they are Any to
                # the checker; the def lists always carry both (test_mcp_tool_catalog
                # pins it), and a missing name would fail far earlier at tools/list.
                tools.append(
                    PhoenixAssistTool(
                        str(name), tool_def.get("description"), parameters, token.id,
                    )
                )

            api_prompt = _build_instructions(token, data, _base_url(self.hass)) + "\n\n" + _ASSIST_ADDENDUM
            return _llm.APIInstance(
                api=self,
                api_prompt=api_prompt,
                llm_context=llm_context,
                tools=tools,
            )

else:  # pragma: no cover - exercised only on HA versions lacking the seam
    PhoenixAssistTool = None  # type: ignore[assignment,misc]
    PhoenixAssistAPI = None  # type: ignore[assignment,misc]


def async_register_assist_api(hass: HomeAssistant, data: Any) -> None:
    """Register the single stable-id Phoenix MCP Assist API, if supported and not already up.

    Idempotent: a second call while registered is a no-op. Never raises; a failed
    registration logs and leaves the bridge simply absent.
    """
    if not _ASSIST_API_SUPPORTED or data.assist_api_unregister is not None:
        return
    try:
        data.assist_api_unregister = _llm.async_register_api(hass, PhoenixAssistAPI(hass, data))
    except Exception:  # noqa: BLE001 - HomeAssistantError on dup id, or anything
        _LOGGER.warning("Could not register the Phoenix MCP Assist API", exc_info=True)


def async_unregister_assist_api(data: Any) -> None:
    """Unregister the Assist API if registered. Idempotent; never raises."""
    unregister = data.assist_api_unregister
    if unregister is None:
        return
    data.assist_api_unregister = None
    try:
        unregister()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Assist API unregister failed", exc_info=True)
