"""Tests for the Phoenix MCP voice agent (voice_agent.py + agentcli.async_run_voice_turn).

Covers the headless voice loop (gated dispatch, pending -> queued degradation,
mid-turn authority loss), the config-resolution wrapper (async_voice_answer), the
conversation-agent response shape, and register/unregister lifecycle.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp import agentcli, voice_agent
from custom_components.phoenix_mcp.agentcli import EV_DONE, EV_ERROR, EV_TEXT, EV_TOOL, async_run_voice_turn
from custom_components.phoenix_mcp.const import (
    AGENTCLI_MAX_TURN_OUTPUT_BYTES,
    VOICE_AGENT_CLIENT_IP,
    VOICE_TEMPLATES,
)
from custom_components.phoenix_mcp.token_store import GlobalSettings


class _MockProvider:
    def __init__(self):
        self.tool_result_batches: list[list[dict]] = []

    def format_tools(self, tools):
        return tools

    def append_assistant(self, messages, msg):
        messages.append(msg)

    def append_tool_results(self, messages, results):
        self.tool_result_batches.append(results)
        messages.append({"role": "user", "content": results})


def _staged_stream(*batches):
    """An async-generator stand-in for _stream_turn_resilient yielding successive
    event batches, one per model turn."""
    state = {"i": 0}

    async def gen(provider, session, **kwargs):
        i = state["i"]
        state["i"] += 1
        batch = batches[i] if i < len(batches) else batches[-1]
        for ev in batch:
            yield ev

    return gen


def _tool_ev(name, tid, args=None):
    return {"type": EV_TOOL, "name": name, "id": tid, "input": args or {}}


def _done(stop_reason):
    return {"type": EV_DONE, "stop_reason": stop_reason, "assistant_msg": {"role": "assistant", "content": []}}


def _dispatch_ok(text):
    async def fake(method, msg_id, params, tok, hass, data, client_ip, base_url, *a, **k):
        fake.calls.append({"client_ip": client_ip, "name": params["name"]})
        return ({"result": {"content": [{"type": "text", "text": text}]}}, "n", "r", "allowed")
    fake.calls = []
    return fake


def _make_token():
    return SimpleNamespace(id="tok-1", announce_all_tools=True, confirm_inline_wait_seconds=0)


def _make_data(**settings_over):
    settings = GlobalSettings(**settings_over)
    store = MagicMock()
    store.get_settings.return_value = settings
    return SimpleNamespace(store=store, mesa=None, ready=True, shutting_down=False)


# --------------------------------------------------------------------------- #
# async_run_voice_turn
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_voice_turn_tool_then_answer_uses_voice_sentinel():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    dispatch = _dispatch_ok('{"state": "on"}')
    stream = _staged_stream(
        [_tool_ev("get_state", "t1", {"entity_id": "light.x"}), _done("tool_use")],
        [{"type": EV_TEXT, "text": "The light is on."}, _done("end_turn")],
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(MagicMock(), data, token, provider, MagicMock(), "http://h", "is the light on?")

    assert text == "The light is on."
    assert dispatch.calls == [{"client_ip": VOICE_AGENT_CLIENT_IP, "name": "get_state"}]


@pytest.mark.asyncio
async def test_voice_focus_refusal_strips_marker_and_gives_localized_recovery():
    token = _make_token()
    data = _make_data(
        voice_agent_conversation_style="calm_guide",
        voice_agent_detail_level="detailed",
        voice_agent_home_focused=True,
    )
    marker = agentcli._HOME_FOCUS_REFUSAL_MARKER
    capture: dict = {}
    events = [
        {"type": EV_TEXT, "text": marker[:10]},
        {"type": EV_TEXT, "text": marker[10:] + " This mode is for your home."},
        _done("end_turn"),
    ]

    async def stream(provider, session, **kwargs):
        capture.update(kwargs)
        for event in events:
            yield event
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h",
            "write a sort algorithm", language="zh-Hans",
        )
    assert marker not in text
    assert text.startswith("此对话处于居家专注模式")
    assert "This mode is for your home." not in text
    assert "请重复完整请求" in text
    assert "patient, reassuring" in capture["system_prompt"]
    assert "spoken-friendly" in capture["system_prompt"]
    assert capture["system_prompt"].rfind(marker) > capture["system_prompt"].rfind(
        "spoken-friendly"
    )


@pytest.mark.asyncio
async def test_voice_answer_anyway_is_a_deterministic_one_turn_bypass():
    token = _make_token()
    data = _make_data(voice_agent_home_focused=True)
    capture: dict = {}
    events = [{"type": EV_TEXT, "text": "Jane Austen."}, _done("end_turn")]

    async def stream(provider, session, **kwargs):
        capture.update(kwargs)
        for event in events:
            yield event

    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h",
            "Who wrote Emma? ANSWER   ANYWAY.",
        )

    assert text == "Jane Austen."
    assert agentcli._HOME_FOCUS_INSTRUCTION not in capture["system_prompt"]
    assert agentcli._HOME_FOCUS_REFUSAL_MARKER not in capture["system_prompt"]


@pytest.mark.parametrize(("language", "phrase"), [
    ("de", "Bitte trotzdem antworten"),
    ("es", "Responde de todos modos"),
    ("fr", "Réponds quand même"),
    ("ja", "そのまま回答してください"),
    ("ko", "그래도 답변해 주세요"),
    ("nl", "Wil je toch antwoorden"),
    ("pl", "Mimo to odpowiedz"),
    ("ru", "Ответь в любом случае"),
    ("zh-Hans", "请仍然回答"),
    ("zh-Hant", "請仍然回答"),
])
def test_voice_answer_anyway_recognizes_localized_phrase(language, phrase):
    assert agentcli._voice_home_focus_bypass(phrase, language) is True


@pytest.mark.asyncio
async def test_voice_turn_pending_becomes_queued_without_blocking():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()

    async def dispatch_pending(method, msg_id, params, *a, **k):
        return ({"result": {"content": [{"type": "text", "text": '{"status":"pending_approval","approval_id":"ap1"}'}]}}, "n", "r", "pending")

    stream = _staged_stream(
        [_tool_ev("call_service", "t1"), _done("tool_use")],
        [{"type": EV_TEXT, "text": "It is queued for approval."}, _done("end_turn")],
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch_pending), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(MagicMock(), data, token, provider, MagicMock(), "http://h", "unlock the door")

    assert text == "It is queued for approval."
    fed_back = provider.tool_result_batches[0][0]["result_text"]
    assert "queued_for_approval" in fed_back


@pytest.mark.asyncio
async def test_voice_turn_appends_review_link_only_when_requested():
    token = _make_token()
    data = _make_data()

    async def dispatch_pending(method, msg_id, params, *a, **k):
        return ({"result": {"content": [{"type": "text", "text": '{"status":"pending_approval","approval_id":"ap1","review_url":"/phoenix-mcp#approvals/ap1"}'}]}}, "n", "r", "pending")

    def stream_factory():
        return _staged_stream(
            [_tool_ev("call_service", "t1"), _done("tool_use")],
            [{"type": EV_TEXT, "text": "It is queued."}, _done("end_turn")],
        )

    for include, expect_link in ((True, True), (False, False)):
        with patch.object(agentcli, "_stream_turn_resilient", stream_factory()), \
             patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
             patch.object(agentcli, "_current_dispatch_token", return_value=token), \
             patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch_pending), \
             patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
            text = await async_run_voice_turn(
                MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h", "unlock",
                include_review_links=include,
            )
        assert ("http://h/phoenix-mcp#approvals/ap1" in text) is expect_link
        assert "It is queued." in text


@pytest.mark.asyncio
async def test_voice_turn_authority_loss_skips_side_effect():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    dispatch = _dispatch_ok("should not run")
    stream = _staged_stream(
        [_tool_ev("call_service", "t1"), _done("tool_use")],
        [{"type": EV_TEXT, "text": "I could not do that."}, _done("end_turn")],
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=None), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(MagicMock(), data, token, provider, MagicMock(), "http://h", "unlock")

    assert dispatch.calls == []  # no side effect ran
    assert text == "I could not do that."
    assert provider.tool_result_batches[0][0]["is_error"] is True


@pytest.mark.asyncio
async def test_voice_turn_stops_when_the_provider_blows_the_output_budget():
    """The headless surfaces share the interactive loop's total-output ceiling.

    Voice and AI Task retained assistant and tool messages across every round and
    re-sent the growing transcript on each one, with no ceiling at all, while
    async_run_agent_turn had enforced AGENTCLI_MAX_TURN_OUTPUT_BYTES since it was
    written. A faulty or hostile provider could therefore accumulate several
    stream-sized responses in memory on the two surfaces with no operator watching.
    """
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    huge = {"role": "assistant", "content": "x" * (AGENTCLI_MAX_TURN_OUTPUT_BYTES + 1)}
    stream = _staged_stream([
        {"type": EV_TEXT, "text": "partial"},
        {"type": EV_DONE, "stop_reason": "tool_use", "assistant_msg": huge},
    ])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "hi")

    # The partial text the model had already produced is preferred over an apology.
    assert text == "partial"
    # Rejected BEFORE the append, so the offending message never entered the
    # transcript that would be re-sent on the next round.
    assert provider.tool_result_batches == []


@pytest.mark.asyncio
async def test_voice_turn_output_budget_speaks_when_there_is_no_partial_text():
    """With nothing said yet, the ceiling still produces a sayable sentence."""
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    huge = {"role": "assistant", "content": "x" * (AGENTCLI_MAX_TURN_OUTPUT_BYTES + 1)}
    stream = _staged_stream([{"type": EV_DONE, "stop_reason": "tool_use", "assistant_msg": huge}])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "hi")

    assert text == VOICE_TEMPLATES["output_limit"]


@pytest.mark.asyncio
async def test_voice_turn_stops_after_one_round_when_authority_is_lost():
    """A dead token buys exactly one more model call, not max_iterations of them.

    The round that discovers the loss still feeds the refusals back, so the model
    gets a turn to tell the user what happened (the round asserted by
    test_voice_turn_authority_loss_skips_side_effect). If it asks for tools AGAIN
    instead of answering, every call would be refused identically, so the loop
    stops rather than spending its remaining rounds re-reporting one refusal.
    """
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    dispatch = _dispatch_ok("should not run")
    # Every round asks for a tool; a model that never gives up.
    stream = _staged_stream([_tool_ev("call_service", "t1"), _done("tool_use")])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=None), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "unlock",
            max_iterations=20)

    assert dispatch.calls == []
    # One refusal batch, from the round that discovered the loss. Without the
    # stop this reached 20, one per remaining round.
    assert len(provider.tool_result_batches) == 1


@pytest.mark.asyncio
async def test_voice_turn_running_out_of_steps_says_so():
    """Exhaustion is spoken as its own cause, not the generic apology."""
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([_tool_ev("get_state", "t1"), _done("tool_use")])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=_dispatch_ok("on")), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "hi",
            max_iterations=3)

    assert text == VOICE_TEMPLATES["out_of_steps"]


@pytest.mark.asyncio
async def test_voice_turn_marks_an_answer_the_model_truncated():
    """A cut-off answer is acted on as a whole one unless it says otherwise."""
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([
        {"type": EV_TEXT, "text": "The kitchen light is"},
        {"type": EV_DONE, "stop_reason": "max_tokens",
         "assistant_msg": {"role": "assistant", "content": []}},
    ])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "hi")

    assert text == f"The kitchen light is {VOICE_TEMPLATES['answer_truncated']}"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-Hans", "ko"])
async def test_voice_failure_sentences_follow_the_conversation_language(language: str):
    """Every spoken non-answer is localized, not just the pre-loop declines."""
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([_tool_ev("get_state", "t1"), _done("tool_use")])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=_dispatch_ok("on")), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "hi",
            max_iterations=3, language=language)

    assert text != VOICE_TEMPLATES["out_of_steps"], "still English under a zh-Hans conversation"
    assert not text.isascii(), f"expected a Chinese sentence, got {text!r}"


@pytest.mark.asyncio
async def test_voice_turn_provider_error_yields_spoken_fallback():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([{"type": EV_ERROR, "message": "the model is down"}])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_voice_turn(MagicMock(), data, token, provider, MagicMock(), "http://h", "hi")
    assert "the model is down" in text


# --------------------------------------------------------------------------- #
# async_voice_answer (config resolution)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_voice_answer_disabled_is_unavailable():
    data = _make_data(voice_agent_enabled=False)
    text = await voice_agent.async_voice_answer(MagicMock(), data, "hi")
    assert "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_voice_answer_incomplete_config():
    data = _make_data(voice_agent_enabled=True, voice_agent_token_id=None)
    text = await voice_agent.async_voice_answer(MagicMock(), data, "hi")
    assert "not fully configured" in text.lower()


@pytest.mark.asyncio
async def test_voice_answer_happy_path_runs_the_loop():
    data = _make_data(
        voice_agent_enabled=True, voice_agent_token_id="tok-1",
        voice_agent_provider_id="i1", voice_agent_model="m",
    )
    store = MagicMock()
    store.resolve.return_value = SimpleNamespace(model="m")
    with patch.object(agentcli, "_current_dispatch_token", return_value=_make_token()), \
         patch.object(agentcli, "_get_secret_store", AsyncMock(return_value=store)), \
         patch.object(agentcli, "build_provider", return_value=_MockProvider()), \
         patch.object(agentcli, "async_run_voice_turn", AsyncMock(return_value="The garage is closed.")):
        text = await voice_agent.async_voice_answer(MagicMock(), data, "is the garage open?")
    assert text == "The garage is closed."


@pytest.mark.asyncio
async def test_voice_answer_unknown_provider_is_misconfigured():
    data = _make_data(
        voice_agent_enabled=True, voice_agent_token_id="tok-1",
        voice_agent_provider_id="gone", voice_agent_model="m",
    )
    store = MagicMock()
    store.resolve.return_value = None
    with patch.object(agentcli, "_current_dispatch_token", return_value=_make_token()), \
         patch.object(agentcli, "_get_secret_store", AsyncMock(return_value=store)):
        text = await voice_agent.async_voice_answer(MagicMock(), data, "hi")
    assert "not fully configured" in text.lower()


# --------------------------------------------------------------------------- #
# Conversation agent + registration
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_agent_process_wraps_answer_as_speech():
    assert voice_agent.PhoenixConversationAgent is not None
    agent = voice_agent.PhoenixConversationAgent(MagicMock(), _make_data())
    user_input = SimpleNamespace(text="hi", language="en", conversation_id="c1")
    with patch.object(voice_agent, "async_voice_answer", AsyncMock(return_value="Hello there.")):
        result = await agent.async_process(user_input)
    assert result.response.speech["plain"]["speech"] == "Hello there."
    assert result.conversation_id == "c1"


_FULL_CFG = dict(
    voice_agent_enabled=True, voice_agent_token_id="t",
    voice_agent_provider_id="p", voice_agent_model="m",
)


def test_sync_registers_only_when_fully_configured():
    hass, entry = MagicMock(), MagicMock()
    with patch.object(voice_agent._conversation, "async_set_agent") as set_agent, \
         patch.object(voice_agent._conversation, "async_unset_agent") as unset_agent:
        voice_agent.async_sync_voice_agent(hass, entry, _make_data(**_FULL_CFG))
        assert set_agent.call_count == 1
        assert unset_agent.call_count == 0

        # Enabled but no provider selected: NOT registered (would answer "not configured").
        voice_agent.async_sync_voice_agent(hass, entry, _make_data(voice_agent_enabled=True, voice_agent_token_id="t"))
        assert set_agent.call_count == 1
        assert unset_agent.call_count == 1

        # Disabled: unregister.
        voice_agent.async_sync_voice_agent(hass, entry, _make_data(voice_agent_enabled=False))
        assert unset_agent.call_count == 2

        # Kill switch on + fully configured: STAYS registered (async_voice_answer declines
        # every turn), so a pipeline pointed at Phoenix MCP degrades gracefully, not hard-errors.
        voice_agent.async_sync_voice_agent(hass, entry, _make_data(kill_switch=True, **_FULL_CFG))
        assert set_agent.call_count == 2
        assert unset_agent.call_count == 2


def test_sync_registers_configured_agent_before_runtime_ready_on_restart():
    data = _make_data(**_FULL_CFG)
    data.ready = False
    with patch.object(voice_agent._conversation, "async_set_agent") as set_agent, \
         patch.object(voice_agent._conversation, "async_unset_agent") as unset_agent:
        voice_agent.async_sync_voice_agent(MagicMock(), MagicMock(), data)
    set_agent.assert_called_once()
    unset_agent.assert_not_called()


@pytest.mark.asyncio
async def test_voice_answer_kill_switch_is_unavailable():
    # Registered under kill switch, but every turn declines before any dispatch.
    data = _make_data(kill_switch=True, **_FULL_CFG)
    text = await voice_agent.async_voice_answer(MagicMock(), data, "unlock the door")
    assert "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_agent_process_includes_review_links_for_text_not_voice():
    agent = voice_agent.PhoenixConversationAgent(MagicMock(), _make_data())
    with patch.object(voice_agent, "async_voice_answer", AsyncMock(return_value="ok")) as va:
        await agent.async_process(SimpleNamespace(text="hi", language="en", conversation_id="c", satellite_id=None))
        assert va.call_args.kwargs["include_review_links"] is True
        await agent.async_process(SimpleNamespace(text="hi", language="en", conversation_id="c", satellite_id="sat-1"))
        assert va.call_args.kwargs["include_review_links"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-Hans", "ko"])
async def test_decline_sentences_follow_the_conversation_language(language: str):
    """A spoken decline is rendered in the CALLER's language, not the server's.

    These sentences were hardcoded English literals; a Chinese Assist pipeline
    got an English reply. The whole point of routing them through the catalog is
    that the language HA already hands async_process is the one used.
    """
    data = _make_data(kill_switch=True, **_FULL_CFG)
    english = await voice_agent.async_voice_answer(MagicMock(), data, "hi", language="en")
    localized = await voice_agent.async_voice_answer(MagicMock(), data, "hi", language=language)
    assert "unavailable" in english.lower()
    assert localized != english
    assert "Phoenix MCP" in localized  # product name stays Latin in every locale
    # An unknown locale and a regional English variant both fall back cleanly.
    assert await voice_agent.async_voice_answer(MagicMock(), data, "hi", language="id") == english
    assert await voice_agent.async_voice_answer(MagicMock(), data, "hi", language="en-GB") == english


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-Hans", "ko"])
async def test_agent_process_threads_the_conversation_language_through(language: str):
    """Without this thread-through the catalog lookup would always see None."""
    agent = voice_agent.PhoenixConversationAgent(MagicMock(), _make_data())
    with patch.object(voice_agent, "async_voice_answer", AsyncMock(return_value="ok")) as va:
        await agent.async_process(
            SimpleNamespace(text="hi", language=language, conversation_id="c", satellite_id=None)
        )
    assert va.call_args.kwargs["language"] == language


def test_every_voice_template_is_reachable_from_the_modules_that_speak():
    """No orphan keys, and no literal left behind: the two sets must agree.

    Scans BOTH modules that produce spoken text. voice_agent.py owns the four
    declines it answers with before the loop starts; agentcli.py owns how a turn
    that did not finish cleanly is said, which used to be English literals inside
    async_run_voice_turn. Checking only voice_agent would let a new key be added
    to const and used nowhere, or a spoken sentence be hardcoded in the loop again.
    """
    import pathlib
    import re

    from custom_components.phoenix_mcp import agentcli as _agentcli
    from custom_components.phoenix_mcp.const import VOICE_TEMPLATES

    used: set[str] = set()
    for mod in (voice_agent, _agentcli):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        # Both spellings: voice_text(language, "k") and the f-string form
        # voice_text(language, 'k') used inside an interpolated answer.
        used |= set(re.findall(r"""voice_text\(language,\s*["']([a-z_]+)["']""", src))
    assert used == set(VOICE_TEMPLATES), (
        f"used but undefined: {used - set(VOICE_TEMPLATES)}; "
        f"defined but unused: {set(VOICE_TEMPLATES) - used}"
    )


# --------------------------------------------------------------------------- #
# One-click Assist pipeline setup
# --------------------------------------------------------------------------- #

class _FakePipelineStore:
    """Minimal stand-in for HA's PipelineStorageCollection."""

    def __init__(self):
        self.data: dict = {}
        self._preferred: str | None = None
        self._n = 0

    async def async_create_item(self, settings):
        self._n += 1
        pid = f"pl{self._n}"
        item = SimpleNamespace(id=pid, name=settings.get("name"))
        self.data[pid] = item
        if self._preferred is None:
            self._preferred = pid
        return item

    async def async_delete_item(self, item_id):
        if self._preferred == item_id:  # HA raises PipelinePreferred here
            raise RuntimeError("cannot delete preferred pipeline")
        del self.data[item_id]

    def async_get_preferred_item(self):
        return self._preferred

    def async_set_preferred_item(self, item_id):
        if item_id not in self.data:
            raise KeyError(item_id)
        self._preferred = item_id


class _FakeStore:
    def __init__(self, settings):
        self._settings = settings
        self.async_lock = asyncio.Lock()

    def get_settings(self):
        return self._settings

    async def async_patch_settings(self, **kw):
        self._settings = replace(self._settings, **kw)
        return self._settings


def _make_pipeline_data(**settings_over):
    return SimpleNamespace(
        store=_FakeStore(GlobalSettings(**settings_over)), mesa=None, shutting_down=False
    )


@pytest.mark.asyncio
async def test_create_pipeline_sets_preferred_and_tracks_id():
    store = _FakePipelineStore()
    data = _make_pipeline_data(**_FULL_CFG)
    with patch.object(voice_agent, "_pipeline_store", return_value=store), \
         patch.object(voice_agent, "async_register_voice_agent"), \
         patch.object(voice_agent, "_resolve_pipeline_settings",
                      return_value={"name": "Phoenix MCP", "conversation_engine": "e"}):
        res = await voice_agent.async_create_assist_pipeline(
            MagicMock(), MagicMock(entry_id="e"), data, preferred=True,
        )
    assert res["name"] == "Phoenix MCP"
    assert res["preferred"] is True
    assert store.async_get_preferred_item() == res["pipeline_id"]
    assert data.store.get_settings().voice_agent_pipeline_id == res["pipeline_id"]


@pytest.mark.asyncio
async def test_create_pipeline_requires_full_config():
    data = _make_pipeline_data(voice_agent_enabled=True, voice_agent_token_id="t")  # no provider/model
    with patch.object(voice_agent, "_pipeline_store", return_value=_FakePipelineStore()):
        with pytest.raises(voice_agent.VoicePipelineError):
            await voice_agent.async_create_assist_pipeline(
                MagicMock(), MagicMock(entry_id="e"), data, preferred=True,
            )


@pytest.mark.asyncio
async def test_remove_pipeline_reassigns_preferred_then_deletes():
    store = _FakePipelineStore()
    phoenix = await store.async_create_item({"name": "Phoenix MCP"})  # first item -> preferred
    other = await store.async_create_item({"name": "Home Assistant"})
    store.async_set_preferred_item(phoenix.id)
    data = _make_pipeline_data(voice_agent_pipeline_id=phoenix.id, **_FULL_CFG)
    with patch.object(voice_agent, "_pipeline_store", return_value=store):
        await voice_agent.async_remove_assist_pipeline(MagicMock(), data)
    assert phoenix.id not in store.data
    assert store.async_get_preferred_item() == other.id
    assert data.store.get_settings().voice_agent_pipeline_id is None


@pytest.mark.asyncio
async def test_remove_pipeline_noop_when_untracked():
    store = _FakePipelineStore()
    data = _make_pipeline_data(**_FULL_CFG)  # voice_agent_pipeline_id is None
    with patch.object(voice_agent, "_pipeline_store", return_value=store):
        await voice_agent.async_remove_assist_pipeline(MagicMock(), data)  # must not raise
    assert data.store.get_settings().voice_agent_pipeline_id is None
