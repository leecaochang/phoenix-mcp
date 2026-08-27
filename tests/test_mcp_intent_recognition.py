"""Sentence recognition is diagnostic, read-only, and permission scoped."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hassil.models import MatchEntity, UnmatchedTextEntity
from homeassistant.setup import async_setup_component

from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree
from custom_components.phoenix_mcp.tools.discovery import _tool_recognize_intent
from tests.test_mcp_view import _make_token


def _result(
    *,
    entities: dict[str, MatchEntity] | None = None,
    unmatched: list[UnmatchedTextEntity] | None = None,
    metadata: dict | None = None,
):
    unmatched = unmatched or []
    return SimpleNamespace(
        intent=SimpleNamespace(name="HassTurnOn"),
        entities=entities or {},
        unmatched_entities={item.name: item for item in unmatched},
        unmatched_entities_list=unmatched,
        intent_metadata=metadata,
    )


def _body(tool_result: dict) -> dict:
    return json.loads(tool_result["content"][0]["text"])


@pytest.mark.asyncio
async def test_recognition_requires_search_or_config_read(hass):
    token, _ = _make_token()
    result, outcome, resource = await _tool_recognize_intent(
        {"sentence": "turn on the kitchen"}, token, hass
    )
    assert result["isError"] is True
    assert outcome == "denied"
    assert resource == "recognize_intent"


@pytest.mark.asyncio
async def test_config_read_alone_can_recognize_without_executing(hass):
    token, _ = _make_token(cap_config_read="allow")
    agent = SimpleNamespace(async_recognize_intent=AsyncMock(return_value=None))

    with patch(
        "homeassistant.components.conversation.agent_manager.async_get_agent",
        return_value=agent,
    ):
        result, outcome, _ = await _tool_recognize_intent(
            {"sentence": "what time is it", "language": "en"}, token, hass
        )

    assert outcome == "allowed"
    assert _body(result) == {"match": False, "near_misses": []}
    user_input = agent.async_recognize_intent.await_args.args[0]
    assert user_input.text == "what time is it"
    assert user_input.language == "en"
    assert user_input.device_id is None
    assert user_input.conversation_id is None


@pytest.mark.asyncio
async def test_installed_ha_default_agent_recognizes_without_processing(hass):
    """Exercise the real HA conversation API at the installed version."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    token, _ = _make_token(cap_config_read="allow")

    result, outcome, _ = await _tool_recognize_intent(
        {"sentence": "what time is it", "language": "en"}, token, hass
    )

    body = _body(result)
    assert outcome == "allowed"
    assert body["match"] is True
    assert body["intent"]["name"] == "HassGetCurrentTime"
    assert body["resolved_entities"] == []


@pytest.mark.asyncio
async def test_success_targets_are_intersected_with_read_scope(hass):
    hass.states.async_set("light.allowed", "on", {"friendly_name": "Allowed"})
    hass.states.async_set("light.secret", "off", {"friendly_name": "Secret"})
    token, _ = _make_token(permissions=PermissionTree(entities={
        "light.allowed": PermissionNode(state="YELLOW"),
    }))
    token.cap_search = "allow"
    recognition = _result(entities={
        "domain": MatchEntity("domain", "light", "lights"),
        "state": MatchEntity("state", "on", "on"),
    })
    agent = SimpleNamespace(async_recognize_intent=AsyncMock(return_value=recognition))
    captured: dict = {}

    def _match(_hass, constraints, preferences=None, states=None):
        captured["assistant"] = constraints.assistant
        captured["ids"] = [state.entity_id for state in states]
        return SimpleNamespace(is_match=True, states=states)

    with (
        patch(
            "homeassistant.components.conversation.agent_manager.async_get_agent",
            return_value=agent,
        ),
        patch("homeassistant.helpers.intent.async_match_targets", _match),
    ):
        result, outcome, _ = await _tool_recognize_intent(
            {"sentence": "which lights are on"}, token, hass
        )

    assert outcome == "allowed"
    assert captured == {"assistant": "conversation", "ids": ["light.allowed"]}
    assert _body(result) == {
        "match": True,
        "intent": {"name": "HassTurnOn"},
        "slots": {"domain": "lights", "state": "on"},
        "resolved_entities": [
            {"entity_id": "light.allowed", "state_match": True},
        ],
        "source": "builtin",
    }


@pytest.mark.asyncio
async def test_empty_read_scope_never_falls_back_to_all_states(hass):
    hass.states.async_set("light.secret", "off", {"friendly_name": "Secret"})
    token, _ = _make_token()
    token.cap_search = "allow"
    recognition = _result(entities={
        "domain": MatchEntity("domain", "light", "lights"),
    })
    agent = SimpleNamespace(async_recognize_intent=AsyncMock(return_value=recognition))

    with (
        patch(
            "homeassistant.components.conversation.agent_manager.async_get_agent",
            return_value=agent,
        ),
        patch("homeassistant.helpers.intent.async_match_targets") as matcher,
    ):
        result, _, _ = await _tool_recognize_intent(
            {"sentence": "turn on the lights"}, token, hass
        )

    matcher.assert_not_called()
    assert _body(result)["resolved_entities"] == []


@pytest.mark.asyncio
async def test_incomplete_recognition_returns_one_scrubbed_near_miss(hass):
    token, _ = _make_token()
    token.cap_search = "allow"
    recognition = _result(
        entities={"domain": MatchEntity("domain", "light", "lights")},
        unmatched=[UnmatchedTextEntity("color", "ultraviolet")],
        metadata={
            "hass_custom_sentence": True,
            "hass_custom_file": "/config/custom_sentences/en/private.yaml",
        },
    )
    agent = SimpleNamespace(async_recognize_intent=AsyncMock(return_value=recognition))

    with patch(
        "homeassistant.components.conversation.agent_manager.async_get_agent",
        return_value=agent,
    ):
        result, _, _ = await _tool_recognize_intent(
            {"sentence": "turn the lights ultraviolet"}, token, hass
        )

    body = _body(result)
    assert body == {
        "match": False,
        "near_misses": [{
            "intent": {"name": "HassTurnOn"},
            "slots": {"domain": "lights"},
            "unmatched_slots": {"color": "ultraviolet"},
            "source": "custom",
        }],
    }
    assert "private.yaml" not in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {},
    {"sentence": ""},
    {"sentence": 42},
    {"sentence": "x" * 513},
    {"sentence": "hello", "language": ""},
    {"sentence": "hello", "language": 42},
])
async def test_recognition_rejects_invalid_bounded_input(hass, args):
    token, _ = _make_token()
    token.cap_search = "allow"
    result, outcome, _ = await _tool_recognize_intent(args, token, hass)
    assert result["isError"] is True
    assert outcome == "invalid_request"
