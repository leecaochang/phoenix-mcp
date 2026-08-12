"""Tests for the agentCLI in-panel LLM chat (provider parsers + agent loop)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp import agentcli
from custom_components.phoenix_mcp.agentcli import (
    ClaudeProvider,
    OpenAICompatProvider,
    ProviderConfig,
    _norm_stop,
    _parse_pending,
    async_run_agent_turn,
)
from custom_components.phoenix_mcp.approvals import STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED
from custom_components.phoenix_mcp.const import AGENTCLI_DEEPSEEK_MAX_TOKENS, AGENTCLI_PROVIDERS
from custom_components.phoenix_mcp.token_store import GlobalSettings


# --------------------------------------------------------------------------- #
# Fakes for the raw-HTTP layer
# --------------------------------------------------------------------------- #

class _FakeContent:
    def __init__(self, data: bytes, chunk: int = 8) -> None:
        self._data = data
        self._chunk = chunk

    async def iter_chunked(self, _n: int):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i:i + self._chunk]


class _FakeResp:
    def __init__(self, status: int = 200, body: bytes = b"", json_data=None, text_data: str = "") -> None:
        self.status = status
        self.content = _FakeContent(body)
        self._json = json_data
        self._text = text_data

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, headers=None, json=None, timeout=None, allow_redirects=None):  # noqa: A002
        self.calls.append(("POST", {"url": url, "json": json, "allow_redirects": allow_redirects}))
        return self._resp

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        self.calls.append(("GET", {"url": url, "allow_redirects": allow_redirects}))
        return self._resp


def _sse(*frames: str) -> bytes:
    return ("\n\n".join(frames) + "\n\n").encode()


async def _collect(agen):
    return [ev async for ev in agen]


# --------------------------------------------------------------------------- #
# Provider SSE parsing
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_claude_stream_parses_text_tool_and_done():
    body = _sse(
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
        'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu1","name":"get_state"}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"entity_id\\":\\"light.x\\"}"}}',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        'event: message_stop\ndata: {"type":"message_stop"}',
    )
    cfg = ProviderConfig(kind="claude", model="claude-opus-4-8", base_url="https://x", api_key="k")
    provider = ClaudeProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    types = [e["type"] for e in events]
    assert agentcli.EV_TEXT in types
    tool = next(e for e in events if e["type"] == agentcli.EV_TOOL)
    assert tool["name"] == "get_state"
    assert tool["input"] == {"entity_id": "light.x"}
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["stop_reason"] == "tool_use"
    # Assembled assistant message carries text + tool_use blocks in order.
    blocks = done["assistant_msg"]["content"]
    assert blocks[0]["type"] == "text" and blocks[0]["text"] == "Hi"
    assert blocks[1]["type"] == "tool_use" and blocks[1]["id"] == "tu1"


@pytest.mark.asyncio
async def test_claude_stream_non_200_yields_error():
    cfg = ProviderConfig(kind="claude", model="m", base_url="https://x", api_key="bad")
    provider = ClaudeProvider(cfg)
    session = _FakeSession(_FakeResp(401, b"", text_data="unauthorized"))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    assert events == [] or events[0]["type"] == agentcli.EV_ERROR
    err = events[0]
    assert err["type"] == agentcli.EV_ERROR and err["code"] == "auth"


def test_parse_provider_error_extracts_message_and_code():
    body = ('{"error": {"message": "You exceeded your current quota.", '
            '"type": "insufficient_quota", "param": null, "code": "insufficient_quota"}}')
    msg, code = agentcli._parse_provider_error(body)
    assert msg == "You exceeded your current quota."
    assert code == "insufficient_quota"
    # message-only nested error still yields the human sentence.
    msg2, code2 = agentcli._parse_provider_error('{"error": {"message": "bad request"}}')
    assert msg2 == "bad request" and code2 is None
    # non-JSON falls back to the raw (trimmed) text, no code.
    msg3, code3 = agentcli._parse_provider_error("  plain text error  ")
    assert msg3 == "plain text error" and code3 is None


@pytest.mark.asyncio
async def test_openai_quota_error_is_clean_and_not_retryable():
    body = ('{"error": {"message": "You exceeded your current quota, please check '
            'your plan and billing details.", "type": "insufficient_quota", '
            '"code": "insufficient_quota"}}')
    cfg = ProviderConfig(kind="chatgpt", model="gpt-5", base_url="https://api.openai.com/v1", api_key="k")
    provider = agentcli.OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(429, b"", text_data=body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = events[0]
    assert err["type"] == agentcli.EV_ERROR
    assert err["code"] == "quota"
    assert err["retryable"] is False
    # The human sentence is surfaced, never the raw JSON blob.
    assert err["message"].startswith("You exceeded your current quota")
    assert "{" not in err["message"]


@pytest.mark.asyncio
async def test_openai_plain_rate_limit_stays_retryable():
    body = '{"error": {"message": "Rate limit reached.", "type": "rate_limit_exceeded"}}'
    cfg = ProviderConfig(kind="chatgpt", model="gpt-5", base_url="https://api.openai.com/v1", api_key="k")
    provider = agentcli.OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(429, b"", text_data=body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = events[0]
    assert err["code"] == "rate_limit"
    assert err["retryable"] is True
    assert err["message"] == "Rate limit reached."


@pytest.mark.asyncio
async def test_openai_stream_accumulates_split_tool_args():
    body = _sse(
        'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_state","arguments":"{\\"entity_id\\":"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"light.x\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    tool = next(e for e in events if e["type"] == agentcli.EV_TOOL)
    assert tool["name"] == "get_state" and tool["input"] == {"entity_id": "light.x"}
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_claude_stream_reports_usage_from_message_start_and_delta():
    # message_start carries the call's input tokens (fresh + cache write + cache
    # read all count as sent context); the closing message_delta carries the
    # final cumulative output tokens. Both surface as EV_USAGE.
    body = _sse(
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,"cache_creation_input_tokens":20,"cache_read_input_tokens":300,"output_tokens":2}}}',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":55}}',
        'event: message_stop\ndata: {"type":"message_stop"}',
    )
    cfg = ProviderConfig(kind="claude", model="claude-opus-4-8", base_url="https://x", api_key="k")
    provider = ClaudeProvider(cfg)
    events = await _collect(provider.stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[], options={}))
    usage = [e for e in events if e["type"] == agentcli.EV_USAGE]
    assert usage[0] == {"type": agentcli.EV_USAGE, "input_tokens": 420, "output_tokens": 2}
    assert usage[-1] == {"type": agentcli.EV_USAGE, "input_tokens": 420, "output_tokens": 55}


@pytest.mark.asyncio
async def test_openai_usage_chunk_with_empty_choices_reports_usage():
    # OpenAI's final usage chunk (stream_options include_usage) carries
    # choices: [] and must not be skipped by the empty-choices guard.
    body = _sse(
        'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":812,"completion_tokens":90,"total_tokens":902}}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    events = await _collect(provider.stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[], options={}))
    usage = next(e for e in events if e["type"] == agentcli.EV_USAGE)
    assert usage["input_tokens"] == 812 and usage["output_tokens"] == 90
    # The reply itself still parses normally.
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["assistant_msg"]["content"] == "Hi"


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "expects_flag"), [
    ("chatgpt", True), ("deepseek", True), ("openrouter", True), ("kimi", True),
    ("gemini", False), ("grok", False), ("nvidia", False), ("meta", False),
    ("ollama", False), ("ollama_cloud", False),
])
async def test_stream_options_include_usage_only_for_curated_kinds(kind, expects_flag):
    # OpenAI omits streaming usage unless asked; DeepSeek/OpenRouter/Kimi document
    # accepting the flag. Everyone else must NOT get it (an unrecognized
    # stream_options could reject the whole request there).
    body = _sse("data: [DONE]")
    cfg = ProviderConfig(kind=kind, model="m", base_url="https://h", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    sent = session.calls[0][1]["json"]
    if expects_flag:
        assert sent["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in sent


@pytest.mark.asyncio
async def test_openai_tool_only_assistant_content_is_empty_string_not_null():
    # A tool-only reply (no text) must carry content "" not null: a JSON null is
    # rejected by Ollama's OpenAI-compatible endpoint on the next turn.
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_state","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="ollama", model="llama", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    events = await _collect(provider.stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[], options={}))
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["assistant_msg"]["content"] == ""
    assert done["assistant_msg"]["content"] is not None


@pytest.mark.asyncio
async def test_openai_reasoning_content_surfaces_as_thinking():
    body = _sse(
        'data: {"choices":[{"delta":{"reasoning_content":"Let me think. "},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"Turn it on."},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"Done."},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"show_thinking": True}))
    thinking = "".join(e["text"] for e in events if e["type"] == agentcli.EV_THINKING)
    assert thinking == "Let me think. Turn it on."
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    # Reasoning is display-only: it is not folded into the assistant message.
    assert done["assistant_msg"]["content"] == "Done."


@pytest.mark.asyncio
async def test_openai_reasoning_hidden_when_show_thinking_off():
    body = _sse(
        'data: {"choices":[{"delta":{"reasoning_content":"secret plan"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"Done."},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))  # show_thinking off
    assert not any(e["type"] == agentcli.EV_THINKING for e in events)


@pytest.mark.asyncio
async def test_ollama_reasoning_field_surfaces_as_thinking_only():
    body = _sse(
        'data: {"choices":[{"delta":{"reasoning":"Let me think. "},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"Done."},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="ollama", model="m", base_url="http://h:11434")
    events = await _collect(OpenAICompatProvider(cfg).stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[],
        options={"show_thinking": True},
    ))
    assert "".join(e["text"] for e in events if e["type"] == agentcli.EV_THINKING) == "Let me think. "
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["assistant_msg"]["content"] == "Done."


@pytest.mark.asyncio
async def test_openai_inline_think_tags_are_split_out():
    # <think> arrives split across chunks; the reply text must exclude it.
    body = _sse(
        'data: {"choices":[{"delta":{"content":"<thi"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"nk>secret</think>Hello"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" there."},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="ollama", model="r1", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    # show_thinking on -> thinking captured, reply excludes it
    events = await _collect(provider.stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[],
        options={"show_thinking": True}))
    text = "".join(e["text"] for e in events if e["type"] == agentcli.EV_TEXT)
    thinking = "".join(e["text"] for e in events if e["type"] == agentcli.EV_THINKING)
    assert text == "Hello there."
    assert thinking == "secret"
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["assistant_msg"]["content"] == "Hello there."
    # show_thinking off -> thinking dropped, reply still excludes the tags
    events2 = await _collect(provider.stream_turn(
        _FakeSession(_FakeResp(200, body)), system_prompt="s", messages=[], tools=[], options={}))
    assert "".join(e["text"] for e in events2 if e["type"] == agentcli.EV_TEXT) == "Hello there."
    assert not any(e["type"] == agentcli.EV_THINKING for e in events2)


@pytest.mark.asyncio
async def test_deepseek_thinking_toggle_maps_to_body():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    # Thinking on: enabled toggle + reasoning_effort; temperature is dropped
    # (thinking mode does not support it).
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": True, "effort": "max", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "max"
    assert "temperature" not in sent
    # Thinking off: disabled toggle, no reasoning_effort, temperature honored.
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": False, "effort": "max", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in sent
    assert sent["temperature"] == 0.5


@pytest.mark.asyncio
async def test_chatgpt_reasoning_effort_maps_to_body():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    cfg = ProviderConfig(kind="chatgpt", model="o3-mini", base_url="https://o", api_key="k")
    provider = OpenAICompatProvider(cfg)
    # A reasoning model: effort becomes reasoning_effort; temperature is dropped.
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"effort": "high", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "high"
    assert "temperature" not in sent
    # A plain gpt-* model sends no effort: temperature is honored.
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert "reasoning_effort" not in sent
    assert sent["temperature"] == 0.5


@pytest.mark.asyncio
async def test_ollama_reasoning_effort_maps_to_openai_body():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    provider = OpenAICompatProvider(ProviderConfig(
        kind="ollama", model="m", base_url="http://h:11434",
    ))
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": True, "effort": "high", "temperature": 0.2},
    ))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "high"
    assert sent["temperature"] == 0.2
    assert "think" not in sent

    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": False, "temperature": 0.2},
    ))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "none"
    assert "think" not in sent


@pytest.mark.asyncio
async def test_gemini_sends_reasoning_effort_and_never_temperature():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    cfg = ProviderConfig(kind="gemini", model="gemini-3.5-flash",
                         base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    # effort maps to Gemini's thinking_level; temperature is dropped even if passed
    # (Google recommends leaving it at the default for its reasoning models).
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"effort": "high", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "high"
    assert "temperature" not in sent
    # Uses the OpenAI-compat chat path under the Gemini base URL.
    assert session.calls[-1][1]["url"].endswith("/v1beta/openai/chat/completions")


@pytest.mark.asyncio
async def test_grok_reasoning_effort_and_bearer_and_url():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    cfg = ProviderConfig(kind="grok", model="grok-4", base_url="https://api.x.ai/v1", api_key="k")
    provider = OpenAICompatProvider(cfg)
    # An effort level maps to reasoning_effort and drops temperature (reasoning turn).
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": True, "effort": "high", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "high"
    assert "temperature" not in sent
    assert session.calls[-1][1]["url"] == "https://api.x.ai/v1/chat/completions"
    # No effort: no reasoning_effort, temperature honored, Bearer auth.
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert "reasoning_effort" not in sent
    assert sent["temperature"] == 0.5
    assert provider._headers()["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_kimi_k3_takes_effort_and_k2_takes_thinking_toggle():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    # K3: reasoning_effort only (it always reasons, so the panel sends no thinking
    # flag), and temperature is dropped because no kimi-k* model accepts it.
    cfg = ProviderConfig(kind="kimi", model="kimi-k3",
                         base_url="https://api.moonshot.ai/v1", api_key="mk")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"effort": "max", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "max"
    assert "thinking" not in sent
    assert "temperature" not in sent
    # Runs through the OpenAI-compatible path, Bearer auth.
    assert session.calls[-1][1]["url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert provider._headers()["Authorization"] == "Bearer mk"
    # K2.x: the thinking object instead, with no reasoning_effort (it has none).
    cfg2 = ProviderConfig(kind="kimi", model="kimi-k2.6",
                          base_url="https://api.moonshot.ai/v1", api_key="mk")
    provider2 = OpenAICompatProvider(cfg2)
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider2.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": False, "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in sent
    assert "temperature" not in sent
    # A legacy moonshot-v1 model has no reasoning control at all, so a headless
    # turn (no options) sends neither field and temperature is honored.
    cfg3 = ProviderConfig(kind="kimi", model="moonshot-v1-128k",
                          base_url="https://api.moonshot.ai/v1", api_key="mk")
    provider3 = OpenAICompatProvider(cfg3)
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider3.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert "thinking" not in sent and "reasoning_effort" not in sent
    assert sent["temperature"] == 0.5


@pytest.mark.asyncio
async def test_meta_sends_reasoning_effort_and_never_temperature():
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    cfg = ProviderConfig(kind="meta", model="muse-spark-1.1",
                         base_url="https://api.meta.ai/v1", api_key="LLM|1|k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    # effort maps to reasoning_effort; temperature is dropped even if passed (Meta
    # tunes the model for its default).
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"effort": "xhigh", "temperature": 0.5}))
    sent = session.calls[-1][1]["json"]
    assert sent["reasoning_effort"] == "xhigh"
    assert "temperature" not in sent
    assert session.calls[-1][1]["url"] == "https://api.meta.ai/v1/chat/completions"
    assert provider._headers()["Authorization"] == "Bearer LLM|1|k"
    # A headless turn passes no options: Muse Spark always reasons, so nothing is
    # sent and the model's own default effort applies. Never reasoning_effort
    # "none", which Muse Spark rejects with a 400.
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    sent = session.calls[-1][1]["json"]
    assert "reasoning_effort" not in sent


def test_provider_kinds_match_the_const_allowlist():
    # AGENTCLI_PROVIDERS gates instance creation while _KINDS supplies each kind's
    # endpoint defaults, so a kind added to one and not the other is either
    # uncreatable or created with an empty base URL.
    assert set(agentcli._KINDS) == set(AGENTCLI_PROVIDERS)


@pytest.mark.asyncio
async def test_minimax_runs_through_claude_provider_bearer_and_adaptive():
    cfg = ProviderConfig(kind="minimax", model="MiniMax-M2",
                         base_url="https://api.minimax.io/anthropic", api_key="mkey")
    # Routed to the Anthropic-format provider, not OpenAICompat.
    provider = agentcli.build_provider(cfg)
    assert isinstance(provider, ClaudeProvider)
    # Auth is Bearer (not Anthropic's x-api-key), with the anthropic-version header.
    h = provider._headers()
    assert h["Authorization"] == "Bearer mkey"
    assert "x-api-key" not in h
    assert h["anthropic-version"]
    # Thinking on -> plain adaptive; none of Anthropic's display/output_config extras.
    body = _sse('data: {"type":"message_stop"}')
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[],
        options={"thinking": True, "effort": "high", "show_thinking": True}))
    sent = session.calls[-1][1]["json"]
    assert sent["thinking"] == {"type": "adaptive"}
    assert "output_config" not in sent
    assert session.calls[-1][1]["url"].endswith("/anthropic/v1/messages")
    # Claude itself is unchanged: x-api-key auth and an output_config effort.
    ccfg = ProviderConfig(kind="claude", model="m", base_url="https://api.anthropic.com", api_key="ck")
    ch = ClaudeProvider(ccfg)._headers()
    assert ch["x-api-key"] == "ck" and "Authorization" not in ch


@pytest.mark.asyncio
async def test_claude_sets_prompt_cache_breakpoints():
    cfg = ProviderConfig(kind="claude", model="m", base_url="https://x", api_key="k")
    provider = ClaudeProvider(cfg)
    body = _sse('data: {"type":"message_stop"}')
    # String-content user message (the typed chat message shape).
    messages = [{"role": "user", "content": "hi"}]
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="sys", messages=messages, tools=[], options={}))
    sent = session.calls[-1][1]["json"]
    # System becomes a cached block; the last message's last block is marked.
    assert sent["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    last_block = sent["messages"][-1]["content"][-1]
    assert last_block == {
        "type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    # The caller's stored transcript is never mutated (it round-trips the browser).
    assert messages == [{"role": "user", "content": "hi"}]

    # Block-content last message (tool_result shape): marker on the LAST block only.
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": []},
            {"type": "tool_result", "tool_use_id": "t2", "content": []},
        ]},
    ]
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="sys", messages=messages, tools=[], options={}))
    sent = session.calls[-1][1]["json"]
    blocks = sent["messages"][-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent["messages"][0]["content"][0]
    assert "cache_control" not in messages[-1]["content"][-1]


@pytest.mark.asyncio
async def test_minimax_gets_no_cache_markers():
    # MiniMax's Anthropic-compatible API is not verified to accept cache_control.
    cfg = ProviderConfig(kind="minimax", model="MiniMax-M2",
                         base_url="https://api.minimax.io/anthropic", api_key="mk")
    provider = ClaudeProvider(cfg)
    body = _sse('data: {"type":"message_stop"}')
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
        tools=[], options={}))
    sent = session.calls[-1][1]["json"]
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_openrouter_bearer_url_and_tool_capable_model_filter():
    cfg = ProviderConfig(kind="openrouter", model="",
                         base_url="https://openrouter.ai/api/v1", api_key="or")
    provider = agentcli.build_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._headers()["Authorization"] == "Bearer or"
    # Chat goes to the OpenAI-compat path under the OpenRouter base.
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"temperature": 0.5}))
    assert session.calls[-1][1]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    # No thinking branch fires: temperature is honored, no reasoning_effort/think.
    sent = session.calls[-1][1]["json"]
    assert sent["temperature"] == 0.5
    assert "reasoning_effort" not in sent and "think" not in sent
    # Model list keeps only tool-capable models, sorted.
    models_json = {"data": [
        {"id": "meta-llama/llama-3.3-70b-instruct", "supported_parameters": ["tools", "temperature"]},
        {"id": "some/embedding-model", "supported_parameters": ["temperature"]},
        {"id": "anthropic/claude-x", "supported_parameters": ["tools"]},
    ]}
    models = await provider.list_models(_FakeSession(_FakeResp(200, b"", json_data=models_json)))
    assert models == ["anthropic/claude-x", "meta-llama/llama-3.3-70b-instruct"]


@pytest.mark.asyncio
async def test_nvidia_bearer_url_and_non_chat_model_filter():
    cfg = ProviderConfig(kind="nvidia", model="",
                         base_url="https://integrate.api.nvidia.com/v1", api_key="nvapi-x")
    provider = agentcli.build_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._headers()["Authorization"] == "Bearer nvapi-x"
    # The base already carries /v1, so the OpenAI-compat path appends cleanly.
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"temperature": 0.5}))
    assert session.calls[-1][1]["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    # No thinking branch fires: temperature is honored, no reasoning_effort/think.
    sent = session.calls[-1][1]["json"]
    assert sent["temperature"] == 0.5
    assert "reasoning_effort" not in sent and "think" not in sent
    # Model list drops embedding/reranking models, sorted; tool support is not
    # detectable from NVIDIA's catalog, so chat models are all kept.
    models_json = {"data": [
        {"id": "meta/llama-3.3-70b-instruct"},
        {"id": "nvidia/nv-embedqa-e5-v5"},
        {"id": "nvidia/nv-rerankqa-mistral-4b-v3"},
        {"id": "deepseek-ai/deepseek-r1"},
    ]}
    models = await provider.list_models(_FakeSession(_FakeResp(200, b"", json_data=models_json)))
    assert models == ["deepseek-ai/deepseek-r1", "meta/llama-3.3-70b-instruct"]


@pytest.mark.asyncio
async def test_ollama_cloud_uses_bearer_and_v1_path_and_tags_with_auth():
    cfg = ProviderConfig(kind="ollama_cloud", model="gpt-oss:120b",
                         base_url="https://ollama.com", api_key="ok")
    provider = agentcli.build_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    # Cloud is keyed: Bearer header (local Ollama is keyless, no header).
    assert provider._headers()["Authorization"] == "Bearer ok"
    local = OpenAICompatProvider(ProviderConfig(kind="ollama", model="llama3",
                                                base_url="http://h:11434", api_key=None))
    assert "Authorization" not in local._headers()
    # Chat goes to the OpenAI-compat /v1 path under the cloud host.
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={"effort": "high"}))
    assert session.calls[-1][1]["url"] == "https://ollama.com/v1/chat/completions"
    assert session.calls[-1][1]["json"]["reasoning_effort"] == "high"
    assert "think" not in session.calls[-1][1]["json"]
    # Model listing hits /api/tags (with the Bearer header) and 401 rejects.
    ok, _ = await provider.validate(_FakeSession(_FakeResp(200, b"", json_data={"models": []})))
    assert ok is True
    ok, msg = await provider.validate(_FakeSession(_FakeResp(401, b"", text_data="bad")))
    assert ok is False and "rejected" in msg.lower()


@pytest.mark.asyncio
async def test_minimax_validate_soft_accepts_and_list_models_fallback():
    cfg = ProviderConfig(kind="minimax", model="MiniMax-M2",
                         base_url="https://api.minimax.io/anthropic", api_key="k")
    provider = ClaudeProvider(cfg)
    # A missing models endpoint (404) is not an auth failure -> soft-accept.
    ok, _ = await provider.validate(_FakeSession(_FakeResp(404, b"", text_data="not found")))
    assert ok is True
    # A real auth failure still rejects.
    ok, msg = await provider.validate(_FakeSession(_FakeResp(401, b"", text_data="bad")))
    assert ok is False and "rejected" in msg.lower()
    # list_models falls back to the curated set when the endpoint is absent.
    models = await provider.list_models(_FakeSession(_FakeResp(404, b"", text_data="x")))
    assert models == ["MiniMax-M2", "MiniMax-M1"]


@pytest.mark.asyncio
async def test_validate_retries_a_transient_connection_failure(monkeypatch):
    from aiohttp import ClientError
    monkeypatch.setattr(agentcli, "_VALIDATE_RETRY_BACKOFF_SECONDS", 0)

    class _FlakySession:
        """Raises a ClientError (a DNS/connect blip) on its first N get() calls."""
        def __init__(self, fail_times: int, resp: _FakeResp) -> None:
            self.fail_times = fail_times
            self.resp = resp
            self.calls = 0

        def get(self, url, headers=None, timeout=None, allow_redirects=None):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise ClientError("temporary failure in name resolution")
            return self.resp

    cfg = ProviderConfig(kind="chatgpt", model="gpt-5",
                         base_url="https://api.openai.com/v1", api_key="k")
    provider = OpenAICompatProvider(cfg)

    # One transient blip then success: the probe retries and validates.
    sess = _FlakySession(1, _FakeResp(200, b""))
    ok, _ = await provider.validate(sess)
    assert ok is True and sess.calls == 2

    # A persistent failure gives up after the attempt budget with a friendly message.
    sess2 = _FlakySession(99, _FakeResp(200, b""))
    ok2, msg2 = await provider.validate(sess2)
    assert ok2 is False and sess2.calls == agentcli._VALIDATE_ATTEMPTS
    assert "could not reach" in msg2.lower()

    # An auth rejection (a real HTTP status, not a network error) is NOT retried.
    sess3 = _FlakySession(0, _FakeResp(401, b"", text_data="bad"))
    ok3, msg3 = await provider.validate(sess3)
    assert ok3 is False and sess3.calls == 1 and "rejected" in msg3.lower()


def test_filter_gemini_models_strips_prefix_and_drops_non_chat():
    raw = [
        "models/gemini-2.5-flash", "models/gemini-2.5-pro", "models/gemini-1.5-pro",
        "models/text-embedding-004", "models/gemini-embedding-001", "models/imagen-3.0",
        "models/aqa",
    ]
    kept = agentcli._filter_gemini_models(raw)
    assert kept == ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]


def test_filter_chatgpt_models_keeps_only_chat_models():
    raw = [
        "gpt-4o", "gpt-4o-mini", "gpt-4.1", "o1", "o3-mini", "chatgpt-4o-latest",
        "text-embedding-3-small", "gpt-4o-audio-preview", "gpt-4o-realtime-preview",
        "gpt-4o-transcribe", "gpt-4o-mini-tts", "gpt-image-1", "dall-e-3",
        "whisper-1", "omni-moderation-latest", "gpt-3.5-turbo-instruct",
        "gpt-4o-search-preview",
    ]
    kept = agentcli._filter_chatgpt_models(raw)
    assert kept == ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o1", "o3-mini", "chatgpt-4o-latest"]
    # If nothing matches (e.g. a proxy with odd names), fall back to the raw list
    # rather than returning an empty dropdown.
    assert agentcli._filter_chatgpt_models(["weird-model"]) == ["weird-model"]


def test_claude_show_thinking_controls_display():
    body: dict = {}
    agentcli._apply_claude_options(body, {"thinking": True, "show_thinking": True})
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    body = {}
    agentcli._apply_claude_options(body, {"thinking": True, "show_thinking": False})
    assert body["thinking"] == {"type": "adaptive", "display": "omitted"}
    body = {}
    agentcli._apply_claude_options(body, {"thinking": True})  # default: omitted
    assert body["thinking"]["display"] == "omitted"
    body = {}
    agentcli._apply_claude_options(body, {"thinking": False, "show_thinking": True})
    assert "thinking" not in body


@pytest.mark.asyncio
async def test_ollama_malformed_tool_args_is_bad_tool_call():
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_state","arguments":"{not json"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="ollama", model="llama", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = next(e for e in events if e["type"] == agentcli.EV_ERROR)
    assert err["code"] == "bad_tool_call"
    # A genuinely malformed call (the stream DID report why it ended, and not on
    # the token limit) keeps the plain invalid-tool-call message. Resending it
    # would only produce the same syntax again, so it is not retryable.
    assert err["message"] == "The model produced an invalid tool call."
    assert err["retryable"] is False
    # No tool_use is emitted for the unparseable call.
    assert not any(e["type"] == agentcli.EV_TOOL for e in events)
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["stop_reason"] == "error"


@pytest.mark.asyncio
async def test_truncated_tool_args_at_output_limit_reports_truncation():
    # Live-observed with DeepSeek: a whole-dashboard write's JSON arguments hit
    # the provider's output-token limit mid-stream (finish_reason "length"), so
    # the args never parse. That is truncation, not model syntax; the error must
    # say so instead of the generic "invalid tool call".
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"set_dashboard_config","arguments":"{\\"config\\": {\\"views\\": [{\\"ca"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = next(e for e in events if e["type"] == agentcli.EV_ERROR)
    assert err["code"] == "tool_call_truncated"
    assert "output length limit" in err["message"]
    # Not retryable: the same request would hit the same cap again.
    assert err["retryable"] is False
    assert not any(e["type"] == agentcli.EV_TOOL for e in events)


@pytest.mark.asyncio
async def test_tool_args_cut_off_mid_stream_reports_a_dropped_connection():
    # The response simply stops arriving: partial arguments, and no chunk ever
    # carries a finish_reason. Every provider reports one when it finishes, so
    # its absence is the signal that the connection dropped rather than the
    # model emitting bad syntax. Reported as its own case because the other
    # readings both send the operator somewhere useless: "invalid tool call"
    # sends them to inspect a request that was fine, and the truncation message
    # sends them to make it smaller when size was never the problem.
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"edit_automation","arguments":"{\\"config\\": {\\"trig"}}]},"finish_reason":null}]}',
    )
    cfg = ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k")
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = next(e for e in events if e["type"] == agentcli.EV_ERROR)
    assert err["code"] == "tool_call_cut"
    assert "connection to the model ended" in err["message"]
    # The one retryable member of the family: nothing about the request was
    # wrong, so sending it again is the fix rather than a doomed repeat.
    assert err["retryable"] is True
    assert not any(e["type"] == agentcli.EV_TOOL for e in events)
    done = next(e for e in events if e["type"] == agentcli.EV_DONE)
    assert done["stop_reason"] == "error"


@pytest.mark.asyncio
async def test_clean_stream_with_bad_json_is_not_reported_as_a_dropped_connection():
    # The boundary the cut case must not swallow: a stream that ended cleanly on
    # a finish_reason but whose arguments are malformed is the model's syntax,
    # even though no "[DONE]" sentinel arrived. Keying the cut case on that
    # sentinel instead of on finish_reason would relabel this as a dropped
    # connection for every backend that does not send one.
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_state","arguments":"{not json"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    )
    cfg = ProviderConfig(kind="ollama", model="llama", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    err = next(e for e in events if e["type"] == agentcli.EV_ERROR)
    assert err["code"] == "bad_tool_call"


@pytest.mark.asyncio
async def test_deepseek_gets_explicit_max_tokens_other_kinds_do_not():
    # DeepSeek's undocumented default output cap truncated a large tool call;
    # Phoenix MCP now pins an explicit ceiling for DeepSeek only. ChatGPT must NOT get
    # max_tokens (OpenAI reasoning models reject the field).
    body = _sse('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]")
    ds = OpenAICompatProvider(ProviderConfig(kind="deepseek", model="deepseek-v4-flash", base_url="https://d", api_key="k"))
    session = _FakeSession(_FakeResp(200, body))
    await _collect(ds.stream_turn(session, system_prompt="s", messages=[], tools=[], options={"thinking": True}))
    assert session.calls[-1][1]["json"]["max_tokens"] == AGENTCLI_DEEPSEEK_MAX_TOKENS
    session = _FakeSession(_FakeResp(200, body))
    await _collect(ds.stream_turn(session, system_prompt="s", messages=[], tools=[], options={"thinking": False}))
    assert session.calls[-1][1]["json"]["max_tokens"] == AGENTCLI_DEEPSEEK_MAX_TOKENS
    for kind, base in (("chatgpt", "https://o"), ("ollama", "http://h:11434")):
        other = OpenAICompatProvider(ProviderConfig(kind=kind, model="m", base_url=base, api_key="k"))
        session = _FakeSession(_FakeResp(200, body))
        await _collect(other.stream_turn(session, system_prompt="s", messages=[], tools=[], options={}))
        assert "max_tokens" not in session.calls[-1][1]["json"]


@pytest.mark.asyncio
async def test_ollama_list_models_parses_api_tags():
    tags = {"models": [
        {"name": "llama3:latest", "model": "llama3:latest"},
        {"name": "qwen2:7b", "model": "qwen2:7b"},
    ]}
    cfg = ProviderConfig(kind="ollama", model="", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, json_data=tags))
    models = await provider.list_models(session)
    assert models == ["llama3:latest", "qwen2:7b"]
    # the request hit /api/tags, not the OpenAI /models path
    assert session.calls[-1][1]["url"].endswith("/api/tags")


@pytest.mark.asyncio
async def test_ollama_500_before_stream_yields_backend_error():
    cfg = ProviderConfig(kind="ollama", model="llama", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(500, b"", text_data="unknown tool"))
    events = await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    assert events[0]["type"] == agentcli.EV_ERROR
    assert events[0]["code"] == "ollama_backend"


# --------------------------------------------------------------------------- #
# Tool-schema mapping and result append
# --------------------------------------------------------------------------- #

def test_tool_schema_mapping_per_provider():
    mcp = [{"name": "get_state", "description": "d",
            "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}}}}]
    claude = ClaudeProvider(ProviderConfig("claude", "m", "https://x", "k")).format_tools(mcp)
    assert claude[0]["input_schema"]["properties"]["entity_id"]["type"] == "string"
    assert "inputSchema" not in claude[0]
    oai = OpenAICompatProvider(ProviderConfig("deepseek", "m", "https://d", "k")).format_tools(mcp)
    assert oai[0]["type"] == "function"
    assert oai[0]["function"]["parameters"]["properties"]["entity_id"]["type"] == "string"


def test_tool_list_orders_orientation_tools_first():
    # announce_all + pass_through: the full surface, no gating in play, so this
    # exercises pure ordering. Priority tools lead in their declared order; the
    # rest keep source order (stable sort).
    token = SimpleNamespace(announce_all_tools=True, pass_through=True)
    data = SimpleNamespace(mesa=None)
    names = [t["name"] for t in agentcli.build_mcp_tool_list(token, data)]
    priority = list(agentcli._TOOL_PRIORITY)
    assert names[:len(priority)] == priority
    # No duplicates introduced by the reorder.
    assert len(names) == len(set(names))
    # A pair of non-priority tools keeps its relative source order.
    assert names.index("get_history") < names.index("get_statistics")


def test_append_tool_results_shapes():
    results = [{"tool_use_id": "t1", "tool_name": "get_state", "result_text": "ok", "is_error": False},
               {"tool_use_id": "t2", "tool_name": "call_service", "result_text": "bad", "is_error": True}]
    claude_msgs: list = []
    ClaudeProvider(ProviderConfig("claude", "m", "https://x", "k")).append_tool_results(claude_msgs, results)
    # Anthropic: one user message with all tool_result blocks.
    assert len(claude_msgs) == 1
    content = claude_msgs[0]["content"]
    assert content[0]["tool_use_id"] == "t1" and "is_error" not in content[0]
    assert content[1]["is_error"] is True
    oai_msgs: list = []
    OpenAICompatProvider(ProviderConfig("deepseek", "m", "https://d", "k")).append_tool_results(oai_msgs, results)
    # OpenAI: one tool message per result.
    assert len(oai_msgs) == 2
    assert oai_msgs[0]["role"] == "tool" and oai_msgs[0]["tool_call_id"] == "t1"


def test_append_tool_results_translates_images_only_for_vision_models():
    image = {
        "data": "aGVsbG8=",
        "mime_type": "image/jpeg",
    }
    results = [{
        "tool_use_id": "t1",
        "tool_name": "get_camera_image",
        "result_text": "camera metadata",
        "images": [image],
        "is_error": False,
    }]

    claude_msgs: list = []
    ClaudeProvider(ProviderConfig("claude", "m", "https://x", "k", True)).append_tool_results(
        claude_msgs, results,
    )
    assert claude_msgs[0]["content"][0]["content"][1]["source"]["media_type"] == "image/jpeg"

    openai_msgs: list = []
    OpenAICompatProvider(ProviderConfig("chatgpt", "m", "https://x", "k", True)).append_tool_results(
        openai_msgs, results,
    )
    assert openai_msgs[1]["role"] == "user"
    assert openai_msgs[1]["content"][1]["type"] == "image_url"
    assert openai_msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    text_only: list = []
    ClaudeProvider(ProviderConfig("claude", "m", "https://x", "k")).append_tool_results(
        text_only, results,
    )
    assert "not known to support visual input" in text_only[0]["content"][0]["content"][0]["text"]
    assert len(text_only[0]["content"][0]["content"]) == 1


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_parse_pending_strict():
    pend = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "a1", "review_url": "/x"})}]}
    assert _parse_pending(pend)["approval_id"] == "a1"
    # isError present -> not pending
    err = dict(pend, isError=True)
    assert _parse_pending(err) is None
    # look-alike without approval_id -> not pending
    assert _parse_pending({"content": [{"type": "text", "text": '{"status":"pending_approval"}'}]}) is None


def test_resolved_result_reports_execution_failure_with_the_executor_error():
    # An admin APPROVED the action but the executor failed (the approve path
    # stores the error and flips the record to rejected/"execution_failed").
    # Live-found loop: reporting that as a bare "rejected" made the agent treat
    # an approval as a refusal and retry the same doomed call forever. The
    # feedback must say approved-but-failed and carry the executor's error.
    from types import SimpleNamespace
    approval = SimpleNamespace(
        rejected_reason="execution_failed",
        result={"tool_result": {"isError": True, "content": [{"type": "text", "text": (
            "This configuration changed since you last read it (expected_hash no "
            "longer matches). Re-read it and reapply your change."
        )}]}, "outcome": "invalid_request"},
    )
    out = agentcli._resolved_result("rejected", None, approval)
    assert out["isError"] is True
    body = json.loads(out["content"][0]["text"])
    assert body["status"] == "execution_failed"
    assert "APPROVED" in body["message"]
    assert "expected_hash no longer matches" in body["message"]
    assert "re-read" in body["message"].lower()


def test_resolved_result_admin_rejection_keeps_reason_and_status():
    from types import SimpleNamespace
    approval = SimpleNamespace(rejected_reason="wrong sensor", result=None)
    out = agentcli._resolved_result("rejected", None, approval)
    body = json.loads(out["content"][0]["text"])
    assert body["status"] == "rejected"
    assert "wrong sensor" in body["message"]
    # An admin typing the literal slug as their reason is NOT an execution
    # failure (no stored result): stays a plain rejection.
    slug = SimpleNamespace(rejected_reason="execution_failed", result=None)
    body2 = json.loads(agentcli._resolved_result("rejected", None, slug)["content"][0]["text"])
    assert body2["status"] == "rejected"


def test_resolved_result_approved_carries_operator_accepted_note():
    # Live-found: an agent iterating toward a goal read an approval as merely
    # "this step landed" and kept proposing variations of a change the operator
    # had already reviewed and settled on. The approved result must tell it the
    # operator accepted this exact change, appended without mutating the stored
    # record's result.
    applied = {"content": [{"type": "text", "text": '{"saved": true}'}]}
    out = agentcli._resolved_result(STATUS_APPROVED, applied)
    assert out["content"][0] == {"type": "text", "text": '{"saved": true}'}
    assert "reviewed this exact change and approved it" in out["content"][1]["text"]
    assert "Do not revise, move, replace, or re-attempt" in out["content"][1]["text"]
    assert "do not file a corrective follow-up" in out["content"][1]["text"]
    assert not out.get("isError")
    assert applied["content"] == [{"type": "text", "text": '{"saved": true}'}]


def test_norm_stop_mapping():
    assert _norm_stop("tool_calls") == "tool_use"
    assert _norm_stop("stop") == "end_turn"
    assert _norm_stop("length") == "max_tokens"
    assert _norm_stop("tool_use") == "tool_use"


def test_scrollback_setting_clamps():
    assert GlobalSettings.from_dict({"agentcli_scrollback_lines": 999999}).agentcli_scrollback_lines == 5000
    assert GlobalSettings.from_dict({"agentcli_scrollback_lines": -5}).agentcli_scrollback_lines == 0
    assert GlobalSettings.from_dict({"agentcli_scrollback_lines": "nope"}).agentcli_scrollback_lines == 100
    assert GlobalSettings.from_dict({}).agentcli_scrollback_lines == 100
    # round-trips through to_dict
    assert GlobalSettings(agentcli_scrollback_lines=1200).to_dict()["agentcli_scrollback_lines"] == 1200


def test_clip_display_caps_long_tool_results():
    from custom_components.phoenix_mcp.const import AGENTCLI_TOOL_RESULT_MAX_CHARS

    short = "small result"
    assert agentcli._clip_display(short) == short
    big = "x" * (AGENTCLI_TOOL_RESULT_MAX_CHARS + 500)
    clipped = agentcli._clip_display(big)
    assert len(clipped) < len(big)
    assert "truncated" in clipped


# --------------------------------------------------------------------------- #
# Secrets store
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_secret_store_instances(hass):
    store = agentcli.AgentCliStore(AsyncMock())
    c1 = await store.add("claude", {"api_key": "sk-secretw5c4", "model": "claude-opus-4-8"})
    c2 = await store.add("claude", {"api_key": "sk-otherq9r8", "model": "claude-opus-4-8"})
    o1 = await store.add("ollama", {"base_url": "http://192.168.1.44:11434"})

    listed = store.list_instances()
    names = {i["name"] for i in listed}
    # Two Anthropic accounts get discriminators; the single Ollama stays plain.
    assert "Anthropic (w5c4)" in names and "Anthropic (q9r8)" in names
    assert "Ollama (local)" in names
    # The list never leaks a key.
    assert "api_key" not in json.dumps(listed)
    assert "sk-" not in json.dumps(listed)

    # resolve returns a usable config (with the key, server-side only)
    cfg = store.resolve(c1, model_override="claude-opus-4-7")
    assert cfg.api_key == "sk-secretw5c4" and cfg.model == "claude-opus-4-7" and cfg.kind == "claude"
    assert store.resolve("nope") is None  # unknown instance
    # Ollama added with only a base URL (no model yet) still resolves.
    ollama = store.resolve(o1)
    assert ollama is not None and ollama.base_url == "http://192.168.1.44:11434"
    assert ollama.api_key is None and ollama.model == ""

    await store.delete(c2)
    assert store.resolve(c2) is None
    # With one Anthropic account left, its name drops the discriminator.
    remaining = {i["name"] for i in store.list_instances()}
    assert "Anthropic" in remaining


@pytest.mark.asyncio
async def test_secret_store_rejects_duplicate_provider_identity():
    store = agentcli.AgentCliStore(AsyncMock())
    await store.add("ollama", {"base_url": "HTTP://LOCALHOST:11434/", "model": "one"})

    with pytest.raises(agentcli.DuplicateProviderError):
        await store.add("ollama", {"base_url": "http://localhost:11434", "model": "two"})

    # Different endpoints are legitimate separate Ollama accounts.
    await store.add("ollama", {"base_url": "http://other-host:11434", "model": "one"})

    await store.add("openrouter", {"api_key": "same-key", "model": "one"})
    with pytest.raises(agentcli.DuplicateProviderError):
        await store.add("openrouter", {"api_key": "same-key", "model": "two"})
    await store.add("openrouter", {"api_key": "different-key", "model": "one"})


@pytest.mark.asyncio
async def test_secret_store_wipe_clears_all_accounts(hass):
    class _FakeHAStore:
        def __init__(self, *a, **k):
            self.saved = None
        async def async_load(self):
            return {}
        async def async_save(self, d):
            self.saved = d

    with patch("custom_components.phoenix_mcp.agentcli.Store", _FakeHAStore):
        store = await agentcli.AgentCliStore.async_create(hass)
    await store.add("claude", {"api_key": "sk-secretwipe", "model": "claude-opus-4-8"})
    await store.add("ollama", {"base_url": "http://192.168.1.44:11434"})
    assert len(store.list_instances()) == 2

    await store.async_wipe()

    assert store.list_instances() == []
    # The emptied set is persisted, so the on-disk key is gone too.
    assert store._store.saved == {"instances": {}}


@pytest.mark.asyncio
async def test_wipe_agentcli_secrets_helper(hass):
    store = agentcli.AgentCliStore(AsyncMock())
    await store.add("claude", {"api_key": "sk-secretwipe2", "model": "claude-opus-4-8"})
    with patch(
        "custom_components.phoenix_mcp.agentcli._get_secret_store",
        new=AsyncMock(return_value=store),
    ):
        await agentcli.async_wipe_agentcli_secrets(hass)
    assert store.list_instances() == []


@pytest.mark.asyncio
async def test_secret_store_migrates_legacy_shape(hass):
    """The pre-instances {providers: {kind: cfg}} shape migrates to instances."""
    class _FakeHAStore:
        def __init__(self, *a, **k):
            self.saved = None
        async def async_load(self):
            return {"providers": {"claude": {"api_key": "sk-legacyabcd", "model": "claude-opus-4-8"}}}
        async def async_save(self, d):
            self.saved = d

    with patch("custom_components.phoenix_mcp.agentcli.Store", _FakeHAStore):
        store = await agentcli.AgentCliStore.async_create(hass)
    listed = store.list_instances()
    assert len(listed) == 1 and listed[0]["kind"] == "claude"
    # The migrated instance resolves and carries the key server-side.
    cfg = store.resolve(listed[0]["id"])
    assert cfg is not None and cfg.api_key == "sk-legacyabcd"


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #

class _ScriptedProvider:
    """A provider whose turns are pre-scripted normalized-event lists."""

    def __init__(self, turns: list[list[dict]]) -> None:
        self.turns = turns
        self.i = 0

    def format_tools(self, tools):
        return tools

    def append_assistant(self, messages, msg):
        messages.append(msg)

    def append_tool_results(self, messages, results):
        messages.append({"role": "user", "tool_results": results})

    async def stream_turn(self, session, *, system_prompt, messages, tools, options):
        turn = self.turns[self.i]
        self.i += 1
        for ev in turn:
            yield ev


def _done(stop_reason: str) -> dict:
    return {"type": agentcli.EV_DONE, "stop_reason": stop_reason,
            "assistant_msg": {"role": "assistant", "content": []}}


def _loop_data():
    """A MagicMock PhoenixData that passes _current_dispatch_token: not shutting down,
    kill switch off, and a live valid token, so dispatches proceed."""
    data = MagicMock()
    data.shutting_down = False
    data.store.get_settings.return_value = MagicMock(kill_switch=False)
    live = MagicMock()
    live.is_valid.return_value = True
    data.store.get_token_by_id.return_value = live
    return data


@pytest.mark.asyncio
async def test_run_agent_turn_read_tool(hass):
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "get_state", "input": {"entity_id": "light.x"}},
         _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "It is on."}, _done("end_turn")],
    ])
    data = _loop_data()
    tool_result = {"content": [{"type": "text", "text": "on"}]}
    fake_dispatch = AsyncMock(return_value=({"result": tool_result}, "m", "r", "allowed"))
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch):
        await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )

    names = [n for n, _ in events]
    assert "tool_call" in names and "tool_result" in names
    assert names[-1] == "done"
    tr = next(p for n, p in events if n == "tool_result")
    assert tr["is_error"] is False and tr["summary"] == "on"
    fake_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_turn_repeated_rejections_stay_iteration_friendly(hass):
    # Reject-with-reason is the operator STEERING the next proposal; iterating
    # through several rejections is the normal card-building workflow. A
    # stricter contract (forbid variations, hard "stop proposing" escalation on
    # the second rejection) was live-tested and reverted: models turned
    # pedantic, gave up, or side-stepped the tools (dumped raw data into chat,
    # built an HTML chart instead). Every rejection's feedback must present the
    # reason as direction, and no rejection count may escalate to a stop order.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "add_dashboard_card", "input": {}},
         _done("tool_use")],
        [{"type": agentcli.EV_TOOL, "id": "tu2", "name": "add_dashboard_card", "input": {}},
         _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "Here is the revised card."}, _done("end_turn")],
    ])
    data = _loop_data()
    pending_result = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "ap1", "review_url": "/x"})}]}
    fake_dispatch = AsyncMock(return_value=({"result": pending_result}, "m", "r", "pending_approval"))
    rejected_record = MagicMock(status=STATUS_REJECTED, diff=None, result=None,
                                rejected_reason="only two lines, average the sensors")

    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli.get_approval", return_value=rejected_record):
        task = hass.async_create_task(async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        ))
        for _ in range(2):
            await asyncio.sleep(0.05)
            hass.bus.async_fire("phoenix_mcp_approval_resolved", {"approval_id": "ap1"})
        messages = await task

    results = [m["tool_results"] for m in messages if isinstance(m, dict) and "tool_results" in m]
    assert len(results) == 2
    for res in results:
        text = res[0]["result_text"]
        assert "only two lines, average the sensors" in text
        assert "direction for what to do next" in text
        assert "Stop proposing" not in text
        assert "declined" not in text


def test_resolved_result_reasonless_rejection_asks_instead_of_retrying():
    from types import SimpleNamespace
    out = agentcli._resolved_result(
        "rejected", None, SimpleNamespace(rejected_reason=None, result=None))
    body = json.loads(out["content"][0]["text"])
    assert body["status"] == "rejected"
    assert "no reason given" in body["message"]
    assert "ask the operator" in body["message"]


@pytest.mark.asyncio
async def test_run_agent_turn_emits_turn_cumulative_usage(hass):
    # EV_USAGE is replace-semantics per model call; the loop folds finished
    # calls and emits SSE "usage" with turn-cumulative totals plus the newest
    # call's input tokens as the context size.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_USAGE, "input_tokens": 1000, "output_tokens": 5},
         {"type": agentcli.EV_USAGE, "input_tokens": 1000, "output_tokens": 50},
         {"type": agentcli.EV_TOOL, "id": "tu1", "name": "get_state", "input": {}},
         _done("tool_use")],
        [{"type": agentcli.EV_USAGE, "input_tokens": 1600, "output_tokens": 30},
         {"type": agentcli.EV_TEXT, "text": "Done."}, _done("end_turn")],
    ])
    data = _loop_data()
    ok_result = {"content": [{"type": "text", "text": "on"}]}
    fake_dispatch = AsyncMock(return_value=({"result": ok_result}, "m", "r", "allowed"))
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch):
        await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )

    usage = [p for n, p in events if n == "usage"]
    assert usage[0] == {"input_tokens": 1000, "output_tokens": 5, "context_tokens": 1000}
    assert usage[1] == {"input_tokens": 1000, "output_tokens": 50, "context_tokens": 1000}
    # Second call: prior call folded (1000 in / 50 out) + new call's report.
    assert usage[2] == {"input_tokens": 2600, "output_tokens": 80, "context_tokens": 1600}


@pytest.mark.asyncio
async def test_run_agent_turn_inline_approval_resume(hass):
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "call_service", "input": {}},
         _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "Done."}, _done("end_turn")],
    ])
    data = _loop_data()
    pending_result = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "ap1", "review_url": "/phoenix-mcp#approvals/ap1"})}]}
    fake_dispatch = AsyncMock(return_value=({"result": pending_result}, "m", "r", "pending_approval"))

    approved_record = MagicMock(status=STATUS_APPROVED, diff=None,
                                result={"tool_result": {"content": [{"type": "text", "text": "applied"}]}})

    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli.get_approval", return_value=approved_record):
        task = hass.async_create_task(async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        ))
        # Let the loop reach the approval wait, then fire the resolution event.
        await asyncio.sleep(0.05)
        hass.bus.async_fire("phoenix_mcp_approval_resolved", {"approval_id": "ap1"})
        await task

    names = [n for n, _ in events]
    assert "approval_required" in names
    assert "approval_resolved" in names
    # The executed result flows back to the transcript as the tool_result summary,
    # with the operator-accepted note appended so the model treats the approved
    # change as settled instead of iterating on it.
    tr = next(p for n, p in events if n == "tool_result")
    assert tr["summary"].startswith("applied")
    assert "reviewed this exact change and approved it" in tr["summary"]
    assert names[-1] == "done"


@pytest.mark.asyncio
async def test_cancel_mid_approval_wait_skips_remaining_batch_calls(hass):
    # One batch with a gated call then a second call. Cancel fires while the
    # loop is blocked on the approval wait: the second call must never
    # dispatch (no side effect after the operator cancelled), but its
    # tool_use id still gets a synthetic result so the batch stays complete.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "call_service", "input": {}},
         {"type": agentcli.EV_TOOL, "id": "tu2", "name": "call_service", "input": {}},
         _done("tool_use")],
    ])
    data = _loop_data()
    pending_result = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "ap1", "review_url": "/phoenix-mcp#approvals/ap1"})}]}
    fake_dispatch = AsyncMock(return_value=({"result": pending_result}, "m", "r", "pending_approval"))
    pending_record = MagicMock(status=STATUS_PENDING, diff=None, result=None)
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    cancel = asyncio.Event()
    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli.get_approval", return_value=pending_record):
        task = hass.async_create_task(async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=cancel,
        ))
        # Let the loop reach the approval wait, then cancel instead of resolving.
        await asyncio.sleep(0.05)
        cancel.set()
        msgs = await task

    # Only the first (gated) call was ever dispatched.
    assert fake_dispatch.await_count == 1
    # Every tool_use id is still answered; tu2 carries the synthetic result.
    batch = next(m for m in msgs if m.get("tool_results"))
    by_id = {r["tool_use_id"]: r for r in batch["tool_results"]}
    assert set(by_id) == {"tu1", "tu2"}
    assert by_id["tu2"]["is_error"] is True
    assert "not run" in by_id["tu2"]["result_text"]
    # And the turn reported the cancellation.
    assert any(n == "error" and p.get("code") == "cancelled" for n, p in events)


@pytest.mark.asyncio
async def test_validate_base_url_blocks_ssrf_shapes():
    from custom_components.phoenix_mcp.agentcli import _validate_base_url
    # Literal-IP and shape checks need no DNS, so patch getaddrinfo away to be safe.
    with patch("asyncio.get_running_loop") as grl:
        grl.return_value.getaddrinfo = AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])
        # Acceptable: loopback and private literals, and a hostname resolving public.
        assert await _validate_base_url("http://127.0.0.1:11434") is None
        assert await _validate_base_url("http://192.168.1.50:11434") is None
        assert await _validate_base_url("https://ollama.example.com") is None
        # Rejected: non-http scheme, embedded credentials, fragment, link-local IP.
        assert await _validate_base_url("file:///etc/passwd") is not None
        assert await _validate_base_url("ftp://host") is not None
        assert await _validate_base_url("http://user:pass@host:11434") is not None
        assert await _validate_base_url("http://host:11434/#frag") is not None
        assert await _validate_base_url("http://169.254.169.254/latest/meta-data") is not None


@pytest.mark.asyncio
async def test_validate_base_url_rejects_hostname_resolving_to_metadata():
    # A DNS name (e.g. metadata.google.internal, *.nip.io) that resolves into
    # link-local metadata space must be rejected even though it is not a literal IP.
    from custom_components.phoenix_mcp.agentcli import _validate_base_url
    with patch("asyncio.get_running_loop") as grl:
        grl.return_value.getaddrinfo = AsyncMock(
            return_value=[(2, 1, 6, "", ("169.254.169.254", 0))])
        assert await _validate_base_url("http://metadata.google.internal") is not None
    # An unresolvable host is allowed (the actual request just fails cleanly).
    with patch("asyncio.get_running_loop") as grl:
        grl.return_value.getaddrinfo = AsyncMock(side_effect=OSError("no such host"))
        assert await _validate_base_url("http://does-not-resolve.invalid") is None


@pytest.mark.asyncio
async def test_sse_lines_caps_a_newlineless_flood():
    from custom_components.phoenix_mcp.agentcli import _sse_lines

    class _FloodContent:
        async def iter_chunked(self, _n):
            # Never a newline: a real frame is tiny, this is the hostile case.
            for _ in range(4000):
                yield b"x" * 4096

    resp = MagicMock()
    resp.content = _FloodContent()
    with pytest.raises(agentcli.ClientError):
        async for _line in _sse_lines(resp):
            pass


@pytest.mark.asyncio
async def test_sse_lines_caps_cumulative_newline_delimited_output(monkeypatch):
    # Many small newline-terminated frames (each under the per-frame cap) must
    # still be bounded in aggregate, or a hostile provider exhausts memory.
    monkeypatch.setattr(agentcli, "AGENTCLI_MAX_STREAM_BYTES", 100)

    class _ManyFrames:
        async def iter_chunked(self, _n):
            for _ in range(50):
                yield b"data: x\n"  # 8 bytes each -> 400 > 100

    resp = MagicMock()
    resp.content = _ManyFrames()
    with pytest.raises(agentcli.ClientError):
        async for _line in agentcli._sse_lines(resp):
            pass


@pytest.mark.asyncio
async def test_openai_requests_disable_redirects():
    # The admin-supplied Ollama base URL is an SSRF surface; requests must not
    # follow a redirect (which could 302 to a metadata endpoint).
    body = _sse("data: [DONE]")
    cfg = ProviderConfig(kind="ollama", model="llama", base_url="http://h:11434", api_key=None)
    provider = OpenAICompatProvider(cfg)
    session = _FakeSession(_FakeResp(200, body))
    await _collect(provider.stream_turn(
        session, system_prompt="s", messages=[], tools=[], options={}))
    assert session.calls[-1][1]["allow_redirects"] is False


def _make_chat_request(body: bytes):
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER
    user = MagicMock()
    user.is_admin = True
    user.id = "admin"

    def _get(k, default=None):
        if k == KEY_HASS_USER:
            return user
        if k == KEY_AUTHENTICATED:
            return True
        return default

    request = MagicMock()
    request.method = "POST"
    request.path = "/api/phoenix-mcp/agentcli/chat"
    request.content_length = len(body)
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=body)
    request.content.at_eof = MagicMock(return_value=True)
    request.__getitem__ = MagicMock(side_effect=lambda k: user if k == KEY_HASS_USER else None)
    request.get = MagicMock(side_effect=_get)
    return request


@pytest.mark.asyncio
async def test_chat_view_refuses_when_kill_switch_or_shutting_down():
    from custom_components.phoenix_mcp.agentcli import PhoenixAgentCliChatView

    view = PhoenixAgentCliChatView()
    for down, kill, expect in [(True, False, 503), (False, True, 503)]:
        data = MagicMock()
        data.shutting_down = down
        data.store.get_settings.return_value = MagicMock(kill_switch=kill)
        view.hass = MagicMock()
        view.hass.data = {agentcli.DOMAIN: data}
        req = _make_chat_request(json.dumps({"token_id": "t"}).encode())
        resp = await view.post(req)
        assert resp.status == expect


@pytest.mark.asyncio
async def test_chat_view_refuses_revoked_or_missing_token():
    from custom_components.phoenix_mcp.agentcli import PhoenixAgentCliChatView

    view = PhoenixAgentCliChatView()

    def _data(token):
        d = MagicMock()
        d.shutting_down = False
        d.store.get_settings.return_value = MagicMock(kill_switch=False)
        d.store.get_token_by_id.return_value = token
        return d

    # Missing token -> 404.
    view.hass = MagicMock()
    view.hass.data = {agentcli.DOMAIN: _data(None)}
    resp = await view.post(_make_chat_request(json.dumps({"token_id": "t"}).encode()))
    assert resp.status == 404

    # Revoked/expired token -> 400 (never opens the stream).
    revoked = MagicMock()
    revoked.is_valid.return_value = False
    view.hass = MagicMock()
    view.hass.data = {agentcli.DOMAIN: _data(revoked)}
    resp = await view.post(_make_chat_request(json.dumps({"token_id": "t"}).encode()))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_chat_view_streams_ready_and_cleans_up(hass):
    # Happy path through the streaming view: it opens the SSE stream, writes the
    # `ready` frame, drives async_run_agent_turn, and closes the stream in finally.
    import custom_components.phoenix_mcp.agentcli as ac
    from custom_components.phoenix_mcp.agentcli import PhoenixAgentCliChatView

    writes: list = []

    class _FakeStream:
        def __init__(self, **kw):
            self.headers = dict(kw.get("headers", {}))
            self.status = kw.get("status")

        async def prepare(self, req):
            pass

        async def write(self, data):
            writes.append(data)

        async def write_eof(self):
            writes.append(b"__EOF__")

    data = MagicMock()
    data.shutting_down = False
    data.store.get_settings.return_value = MagicMock(kill_switch=False)
    tok = MagicMock()
    tok.is_valid.return_value = True
    tok.id = "t1"
    data.store.get_token_by_id.return_value = tok

    store = MagicMock()
    store.resolve.return_value = ProviderConfig(kind="claude", model="m", base_url="https://x", api_key="k")

    view = PhoenixAgentCliChatView()
    view.hass = hass
    hass.data[ac.DOMAIN] = data

    body = json.dumps({"token_id": "t1", "instance_id": "i1", "user": "hi", "messages": []}).encode()
    with patch.object(ac.web, "StreamResponse", _FakeStream), \
         patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=store)), \
         patch("custom_components.phoenix_mcp.agentcli.build_provider", return_value=MagicMock()), \
         patch("custom_components.phoenix_mcp.agentcli.async_get_clientsession", return_value=MagicMock()), \
         patch("custom_components.phoenix_mcp.agentcli.async_run_agent_turn", new=AsyncMock()) as rat:
        await view.post(_make_chat_request(body))

    joined = b"".join(w for w in writes if isinstance(w, bytes) and w != b"__EOF__")
    assert b"event: ready" in joined       # the stream opened and emitted
    assert b"__EOF__" in writes            # write_eof ran in the finally (cleanup)
    rat.assert_awaited_once()              # the agent turn was driven


@pytest.mark.asyncio
async def test_await_agent_approval_cancels_orphaned_approval(hass):
    # When the turn is cancelled while an approval is pending, the stored
    # approval must be transitioned to a terminal (cancelled) state so an admin
    # cannot later approve and execute it after the operator abandoned the chat.
    from homeassistant.util.dt import utcnow

    from custom_components.phoenix_mcp.agentcli import _await_agent_approval
    from custom_components.phoenix_mcp.approvals import (
        STATUS_CANCELLED,
        STATUS_PENDING,
        PendingApproval,
        get_approval,
    )

    now = utcnow()
    appr = PendingApproval(
        id="a1", token_id="t", token_name="n", tool_name="call_service",
        cap_name="cap_physical_control", args={}, diff={}, status=STATUS_PENDING,
        created_at=now, expires_at=now, request_id="r",
    )

    class _Store:
        def __init__(self) -> None:
            self._a = [appr.to_dict(redact_args=False)]
            self.async_lock = asyncio.Lock()
            self.async_save = AsyncMock()

        def get_pending_approvals(self):
            return self._a

        def set_pending_approvals(self, v):
            self._a = v

    store = _Store()
    data = MagicMock(store=store)
    cancel = asyncio.Event()
    cancel.set()  # operator cancelled/closed the chat

    with patch("custom_components.phoenix_mcp.agentcli.dismiss_approval_notification") as dismiss, \
         patch("custom_components.phoenix_mcp.agentcli.fire_approval_resolved_event") as fire:
        result = await _await_agent_approval(hass, data, "a1", cancel, timeout=0.01)

    # The persisted approval is now terminal, so the admin approve endpoint (which
    # refuses a non-pending record) can no longer execute it.
    assert get_approval(store, "a1").status == STATUS_CANCELLED
    dismiss.assert_called_once()
    fire.assert_called_once()
    # The agent is told the action was not applied.
    assert result.get("isError") is True


@pytest.mark.asyncio
async def test_await_agent_approval_does_not_cancel_an_in_progress_approval(hass):
    # An admin approve claims the id in approvals_in_progress, releases the lock,
    # and runs the side effect while the record is still pending. Cancellation
    # must NOT flip that record to cancelled (the executor owns finalization);
    # otherwise the side effect runs but the record wrongly reads cancelled.
    from homeassistant.util.dt import utcnow

    from custom_components.phoenix_mcp.agentcli import _await_agent_approval
    from custom_components.phoenix_mcp.approvals import (
        STATUS_PENDING,
        PendingApproval,
        get_approval,
    )

    now = utcnow()
    appr = PendingApproval(
        id="a1", token_id="t", token_name="n", tool_name="call_service",
        cap_name="cap_physical_control", args={}, diff={}, status=STATUS_PENDING,
        created_at=now, expires_at=now, request_id="r",
    )

    class _Store:
        def __init__(self) -> None:
            self._a = [appr.to_dict(redact_args=False)]
            self.async_lock = asyncio.Lock()
            self.async_save = AsyncMock()

        def get_pending_approvals(self):
            return self._a

        def set_pending_approvals(self, v):
            self._a = v

    store = _Store()
    data = MagicMock(store=store)
    data.approvals_in_progress = {"a1"}  # an admin is mid-execution
    cancel = asyncio.Event()
    cancel.set()

    with patch("custom_components.phoenix_mcp.agentcli.dismiss_approval_notification") as dismiss, \
         patch("custom_components.phoenix_mcp.agentcli.fire_approval_resolved_event") as fire:
        await _await_agent_approval(hass, data, "a1", cancel, timeout=0.01)

    # Left pending for the executor to finalize; never cancelled out from under it.
    assert get_approval(store, "a1").status == STATUS_PENDING
    dismiss.assert_not_called()
    fire.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_turn_stops_on_output_budget(hass, monkeypatch):
    # A provider that produces more retained output than the turn budget is cut
    # off with an output_limit error rather than accumulating unbounded memory.
    monkeypatch.setattr(agentcli, "AGENTCLI_MAX_TURN_OUTPUT_BYTES", 100)
    big_done = {
        "type": agentcli.EV_DONE, "stop_reason": "end_turn",
        "assistant_msg": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]},
    }
    provider = _ScriptedProvider([[{"type": agentcli.EV_TEXT, "text": "hi"}, big_done]])
    data = _loop_data()
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    token = MagicMock()
    token.id = "tid"
    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", AsyncMock()):
        msgs = await async_run_agent_turn(
            hass=hass, data=data, token=token, provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )
    assert any(n == "error" and p.get("code") == "output_limit" for n, p in events)
    # The offending message was rejected BEFORE append: it is not retained in the
    # returned transcript, which therefore stays within the budget.
    blob = json.dumps(msgs)
    assert "x" * 500 not in blob
    assert len(blob) <= 100 + 200  # budget + slack, never budget + a full message


def test_current_dispatch_token_gating():
    from custom_components.phoenix_mcp.agentcli import _current_dispatch_token

    def _data(*, kill=False, down=False, token=None):
        d = MagicMock()
        d.shutting_down = down
        d.store.get_settings.return_value = MagicMock(kill_switch=kill)
        d.store.get_token_by_id.return_value = token
        return d

    valid = MagicMock()
    valid.is_valid.return_value = True
    revoked = MagicMock()
    revoked.is_valid.return_value = False

    assert _current_dispatch_token(_data(kill=True, token=valid), "t") is None
    assert _current_dispatch_token(_data(down=True, token=valid), "t") is None
    assert _current_dispatch_token(_data(token=None), "t") is None
    assert _current_dispatch_token(_data(token=revoked), "t") is None
    # Valid, live, kill switch off: a usable token comes back.
    assert _current_dispatch_token(_data(token=valid), "t") is not None


@pytest.mark.asyncio
async def test_run_agent_turn_stops_when_token_revoked_midturn(hass):
    # The token is revoked while the turn is running: no remaining call may
    # dispatch a side effect, every tool_use id still gets a synthetic result
    # (batch stays complete), and the turn ends with an unauthorized error.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "call_service", "input": {}},
         {"type": agentcli.EV_TOOL, "id": "tu2", "name": "call_service", "input": {}},
         _done("tool_use")],
    ])
    data = _loop_data()
    fake_dispatch = AsyncMock(return_value=({"result": {}}, "m", "r", "allowed"))
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    token = MagicMock()
    token.id = "tid"
    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli._current_dispatch_token", return_value=None):
        msgs = await async_run_agent_turn(
            hass=hass, data=data, token=token, provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )

    fake_dispatch.assert_not_awaited()
    batch = next(m for m in msgs if m.get("tool_results"))
    by_id = {r["tool_use_id"]: r for r in batch["tool_results"]}
    assert set(by_id) == {"tu1", "tu2"}
    assert all(r["is_error"] and "no longer authorized" in r["result_text"] for r in by_id.values())
    assert any(n == "error" and p.get("code") == "unauthorized" for n, p in events)


@pytest.mark.asyncio
async def test_run_agent_turn_rejection_reason_reaches_agent(hass):
    # An admin rejects from the Approvals panel WITH a reason; the agent loop must
    # feed that reason back as the tool result so the model can report it.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "tu1", "name": "call_service", "input": {}},
         _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "Understood."}, _done("end_turn")],
    ])
    data = _loop_data()
    pending_result = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "ap1", "review_url": "/phoenix-mcp#approvals/ap1"})}]}
    fake_dispatch = AsyncMock(return_value=({"result": pending_result}, "m", "r", "pending_approval"))

    rejected_record = MagicMock(status=STATUS_REJECTED, diff=None, result=None,
                                rejected_reason="Wrong room, use the office light")

    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli.get_approval", return_value=rejected_record):
        task = hass.async_create_task(async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        ))
        await asyncio.sleep(0.05)
        hass.bus.async_fire("phoenix_mcp_approval_resolved", {"approval_id": "ap1"})
        await task

    tr = next(p for n, p in events if n == "tool_result")
    assert tr["is_error"] is True
    assert "Wrong room, use the office light" in tr["summary"]
    # The rejection status is reported on the resolved event too.
    ar = next(p for n, p in events if n == "approval_resolved")
    assert ar["status"] == STATUS_REJECTED


@pytest.mark.asyncio
async def test_run_agent_turn_retries_transient_connection_error(hass):
    # A retryable connection error before any content is retried silently, and the
    # retry's success flows through (the user never sees the transient blip).
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_ERROR, "code": "network", "message": "DNS timeout", "retryable": True}],
        [{"type": agentcli.EV_TEXT, "text": "Recovered."}, _done("end_turn")],
    ])
    data = _loop_data()
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.agentcli._CONNECT_RETRY_BACKOFF_SECONDS", 0.01):
        await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )

    names = [n for n, _ in events]
    assert "error" not in names  # the transient failure was not surfaced
    assert "".join(p.get("text", "") for n, p in events if n == "assistant_delta") == "Recovered."
    assert provider.i == 2  # stream_turn was called twice (one retry)


@pytest.mark.asyncio
async def test_run_agent_turn_does_not_retry_non_retryable_error(hass):
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_ERROR, "code": "auth", "message": "API key rejected.", "retryable": False}],
    ])
    data = _loop_data()
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"):
        await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )

    err = next(p for n, p in events if n == "error")
    assert err["code"] == "auth"
    assert provider.i == 1  # no retry for a non-retryable error


@pytest.mark.asyncio
async def test_human_rejection_resets_iteration_budget(hass):
    # A human rejecting an action each round means no runaway, so the safety cap
    # (here 2) must NOT trip even after more than 2 rejected attempts.
    rejected = {"content": [{"type": "text", "text": json.dumps(
        {"status": "rejected", "message": "no"})}], "isError": True}
    pending_result = {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approval_id": "ap1", "review_url": "/x"})}]}
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": "t1", "name": "create_automation", "input": {}}, _done("tool_use")],
        [{"type": agentcli.EV_TOOL, "id": "t2", "name": "create_automation", "input": {}}, _done("tool_use")],
        [{"type": agentcli.EV_TOOL, "id": "t3", "name": "create_automation", "input": {}}, _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "Okay, I'll stop."}, _done("end_turn")],
    ])
    data = _loop_data()
    fake_dispatch = AsyncMock(return_value=({"result": pending_result}, "m", "r", "pending_approval"))
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch), \
         patch("custom_components.phoenix_mcp.agentcli.get_approval", return_value=None), \
         patch("custom_components.phoenix_mcp.agentcli._await_agent_approval", AsyncMock(return_value=rejected)):
        await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(), max_iterations=2,
        )

    assert not any(p.get("code") == "max_iterations" for n, p in events if n == "error")
    assert "".join(p.get("text", "") for n, p in events if n == "assistant_delta") == "Okay, I'll stop."
    assert provider.i == 4  # all four turns ran despite the cap of 2


@pytest.mark.asyncio
async def test_turn_emits_notice_on_output_limit_and_empty_reply(hass):
    data = _loop_data()

    async def run(final_events):
        provider = _ScriptedProvider([final_events])
        events: list = []

        async def emit(name, payload):
            events.append((name, payload))

        with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
             patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"):
            await async_run_agent_turn(
                hass=hass, data=data, token=MagicMock(), provider=provider,
                session=MagicMock(), messages=[], options={}, client_ip="agentcli",
                base_url="http://h", emit=emit, cancel=asyncio.Event(),
            )
        return events

    # Truncated at the output limit -> a notice explains why it stopped.
    ev = await run([{"type": agentcli.EV_TEXT, "text": "half a sen"}, _done("max_tokens")])
    notice = next(p for n, p in ev if n == "notice")
    assert "output length limit" in notice["message"]

    # An empty end_turn reply -> a notice, not a silent stop.
    ev = await run([_done("end_turn")])
    assert any(n == "notice" for n, _ in ev)

    # A normal reply with text -> NO notice.
    ev = await run([{"type": agentcli.EV_TEXT, "text": "All done."}, _done("end_turn")])
    assert not any(n == "notice" for n, _ in ev)


@pytest.mark.asyncio
async def test_safety_cap_pauses_at_continue_checkpoint_without_a_human(hass):
    # No human in the loop (tool calls just resolve): at the cap the turn must
    # pause with a continue_required checkpoint (not a hard error), and leave the
    # conversation resumable so a continue request can carry on.
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_TOOL, "id": f"t{i}", "name": "get_state", "input": {}}, _done("tool_use")]
        for i in range(6)
    ])
    data = _loop_data()
    fake_dispatch = AsyncMock(return_value=(
        {"result": {"content": [{"type": "text", "text": "ok"}]}}, "m", "r", "allowed"))
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", fake_dispatch):
        messages = await async_run_agent_turn(
            hass=hass, data=data, token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(), max_iterations=2,
        )

    # A continue checkpoint, not a hard-stop error.
    assert any(n == "continue_required" for n, _p in events)
    assert not any(p.get("code") == "max_iterations" for n, p in events if n == "error")
    # The conversation was left resumable (the last dispatched round's tool
    # results are retained, so a continue request continues from here).
    assert messages


@pytest.mark.asyncio
async def test_run_agent_turn_provider_error_stops(hass):
    provider = _ScriptedProvider([
        [{"type": agentcli.EV_ERROR, "code": "auth", "message": "bad key", "retryable": False}],
    ])
    events: list = []

    async def emit(name, payload):
        events.append((name, payload))

    with patch("custom_components.phoenix_mcp.agentcli.build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="sys"):
        await async_run_agent_turn(
            hass=hass, data=MagicMock(), token=MagicMock(), provider=provider,
            session=MagicMock(), messages=[], options={}, client_ip="agentcli",
            base_url="http://h", emit=emit, cancel=asyncio.Event(),
        )
    names = [n for n, _ in events]
    assert ("error", ) == tuple(dict.fromkeys(n for n in names if n == "error"))
    assert names[-1] == "done"
    err = next(p for n, p in events if n == "error")
    assert err["code"] == "auth"


@pytest.mark.asyncio
async def test_delete_provider_clears_voice_agent_binding(hass):
    """Deleting the provider the voice agent points at clears its config and re-syncs."""
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER
    from custom_components.phoenix_mcp.const import DOMAIN
    from custom_components.phoenix_mcp.token_store import GlobalSettings

    holder = {"s": GlobalSettings(voice_agent_enabled=True, voice_agent_token_id="t",
                                  voice_agent_provider_id="pid", voice_agent_model="m")}
    store = MagicMock()
    store.get_settings.side_effect = lambda: holder["s"]
    store.async_lock = _asyncio.Lock()

    async def _patch(**kw):
        holder["s"] = GlobalSettings.from_dict({**holder["s"].to_dict(), **kw})
        return holder["s"]
    store.async_patch_settings = AsyncMock(side_effect=_patch)

    data = MagicMock()
    data.store = store
    data.async_sync_voice_agent = MagicMock()

    hass.data[DOMAIN] = data
    secret_store = MagicMock()
    secret_store.delete = AsyncMock()

    user = MagicMock(); user.is_admin = True; user.id = "admin"

    def _get(k, d=None):
        return {"phoenix_mcp_rid": "rid", KEY_AUTHENTICATED: True, KEY_HASS_USER: user}.get(k, d)

    req = MagicMock()
    req.get = MagicMock(side_effect=_get)
    req.__getitem__ = MagicMock(side_effect=lambda k: {KEY_AUTHENTICATED: True, KEY_HASS_USER: user}.get(k))

    view = agentcli.PhoenixAgentCliProviderView()
    view.hass = hass
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)):
        resp = await view.delete(req, instance_id="pid")

    assert resp.status == 200
    assert holder["s"].voice_agent_provider_id is None
    assert holder["s"].voice_agent_model is None
    data.async_sync_voice_agent.assert_called_once()


@pytest.mark.asyncio
async def test_delete_provider_clears_ai_task_binding(hass):
    """Deleting the provider the AI Task entity points at clears its config."""
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER
    from custom_components.phoenix_mcp.const import DOMAIN
    from custom_components.phoenix_mcp.token_store import GlobalSettings

    holder = {"s": GlobalSettings(ai_task_enabled=True, ai_task_token_id="t",
                                  ai_task_provider_id="pid", ai_task_model="m")}
    store = MagicMock()
    store.get_settings.side_effect = lambda: holder["s"]
    store.async_lock = _asyncio.Lock()

    async def _patch(**kw):
        holder["s"] = GlobalSettings.from_dict({**holder["s"].to_dict(), **kw})
        return holder["s"]
    store.async_patch_settings = AsyncMock(side_effect=_patch)

    data = MagicMock()
    data.store = store
    data.async_sync_voice_agent = MagicMock()

    hass.data[DOMAIN] = data
    secret_store = MagicMock()
    secret_store.delete = AsyncMock()

    user = MagicMock(); user.is_admin = True; user.id = "admin"

    def _get(k, d=None):
        return {"phoenix_mcp_rid": "rid", KEY_AUTHENTICATED: True, KEY_HASS_USER: user}.get(k, d)

    req = MagicMock()
    req.get = MagicMock(side_effect=_get)
    req.__getitem__ = MagicMock(side_effect=lambda k: {KEY_AUTHENTICATED: True, KEY_HASS_USER: user}.get(k))

    view = agentcli.PhoenixAgentCliProviderView()
    view.hass = hass
    with patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)):
        resp = await view.delete(req, instance_id="pid")

    assert resp.status == 200
    assert holder["s"].ai_task_provider_id is None
    assert holder["s"].ai_task_model is None


@pytest.mark.asyncio
async def test_instances_sort_by_display_name_not_kind(hass):
    """Panel dropdowns list accounts alphabetically by what the user reads.

    The sort key must be the display NAME, not the kind key: the two diverge
    (kind "claude" displays as "Anthropic", "chatgpt" as "OpenAI"), so keying
    on kind silently lists them out of order in both the Settings card and the
    Agent Chat provider dropdown.
    """
    store = agentcli.AgentCliStore(AsyncMock())
    await store.add("openrouter", {"api_key": "sk-or"})
    await store.add("chatgpt", {"api_key": "sk-oa"})      # displays as "OpenAI"
    await store.add("claude", {"api_key": "sk-an"})       # displays as "Anthropic"
    await store.add("deepseek", {"api_key": "sk-ds"})

    names = [i["name"] for i in store.list_instances()]

    assert names == sorted(names, key=str.lower)
    assert names == ["Anthropic", "DeepSeek", "OpenAI", "OpenRouter"]


# --------------------------------------------------------------------------- #
# Held-tool progress relay
# --------------------------------------------------------------------------- #

class TestDispatchProgress:
    """Agent Chat is the second consumer of mcp_view's progress bus.

    A firmware build holds the request open for minutes; without this the panel
    shows a tool call and then nothing at all until it finishes.
    """

    @pytest.fixture(autouse=True)
    def _fast_ticks(self, monkeypatch):
        monkeypatch.setattr(agentcli, "AGENTCLI_PROGRESS_INTERVAL_SECONDS", 0.01)

    async def _run(self, statuses):
        from custom_components.phoenix_mcp.mcp_view import _set_progress_status

        emitted: list[tuple[str, dict]] = []

        async def emit(event, payload):
            emitted.append((event, payload))

        async def dispatch():
            for status in statuses:
                _set_progress_status(status)
                await asyncio.sleep(0.03)
            return ({"result": {"ok": True}}, "m", "r", "allowed")

        result = await agentcli._dispatch_with_progress(emit, "call-1", dispatch())
        return result, [p["message"] for e, p in emitted if e == "tool_progress"]

    async def test_relays_each_new_status(self):
        result, messages = await self._run(
            ["Compiling rf-blaster2: 20%", "Compiling rf-blaster2: 80%", "Flashing rf-blaster2: 40%"])

        assert result[0] == {"result": {"ok": True}}
        # The phase switch is the part a human reads; order must be preserved.
        assert messages[0] == "Compiling rf-blaster2: 20%"
        assert "Flashing rf-blaster2: 40%" in messages
        assert messages.index("Compiling rf-blaster2: 80%") < messages.index("Flashing rf-blaster2: 40%")

    async def test_an_unchanged_status_is_not_repeated(self):
        """A five-minute build must not spam the transcript with one line."""
        _result, messages = await self._run(["Compiling x: 10%"] * 5)
        assert messages == ["Compiling x: 10%"]

    async def test_a_tool_that_reports_nothing_emits_nothing(self):
        emitted: list = []

        async def emit(event, payload):
            emitted.append(event)

        async def dispatch():
            await asyncio.sleep(0.03)
            return ({"result": {}}, "m", "r", "allowed")

        await agentcli._dispatch_with_progress(emit, "call-1", dispatch())
        assert emitted == []

    async def test_the_bus_does_not_leak_into_the_next_call(self):
        """Each dispatch gets its own bus; a stale status must not resurface."""
        from custom_components.phoenix_mcp.mcp_view import _progress_ctx, _set_progress_status

        async def dispatch():
            _set_progress_status("Compiling x: 50%")
            return ({"result": {}}, "m", "r", "allowed")

        await agentcli._dispatch_with_progress(AsyncMock(), "call-1", dispatch())
        assert _progress_ctx.get() is None



class TestHeadlessSurfacesShareOneLoop:
    """The two headless surfaces must keep delegating to one shared loop.

    async_run_voice_turn and async_run_ai_task were 63 and 61 lines that differed
    in six: the provider-error policy, the audit sentinel, and collecting review
    URLs. Everything else, including the token re-resolution before each dispatch
    and the shape of every tool result, was duplicated, so a fix could land on one
    surface and miss the other. That is not hypothetical: the total-output ceiling
    existed on the interactive loop and on neither of these.

    These read the AST rather than the behaviour because the failure mode is
    structural. A future change that re-inlines a loop into one surface passes
    every behavioural test it was written against and silently restores the fork.
    """

    HEADLESS = ("async_run_voice_turn", "async_run_ai_task")

    @staticmethod
    def _func(name):
        import ast
        import pathlib

        src = pathlib.Path(agentcli.__file__).read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found; was it renamed?")

    def _calls(self, name):
        import ast

        return {
            n.func.id for n in ast.walk(self._func(name))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    def test_both_delegate_to_the_shared_loop(self):
        for name in self.HEADLESS:
            assert "_run_headless_turn" in self._calls(name), (
                f"{name} no longer calls _run_headless_turn; the two headless "
                "surfaces have re-forked and a fix to one will miss the other."
            )

    def test_neither_dispatches_tools_itself(self):
        """The tool loop lives in one place; a surface only decides how to report."""
        for name in self.HEADLESS:
            assert "_dispatch_mcp" not in self._calls(name), (
                f"{name} dispatches tools directly again. Token re-resolution, the "
                "queued_for_approval degradation and the output ceiling all live in "
                "_run_headless_turn; a private loop silently opts out of every one."
            )

    def test_the_shared_loop_enforces_the_turn_output_ceiling(self):
        """Pins the constant by name: the ceiling is the whole point of sharing."""
        import ast

        names = {
            n.id for n in ast.walk(self._func("_run_headless_turn"))
            if isinstance(n, ast.Name)
        }
        assert "AGENTCLI_MAX_TURN_OUTPUT_BYTES" in names


# --- changing an account's default model (it used to be frozen at creation) ---


def _admin_request(body: dict | None = None):
    """An authenticated admin request whose body is `body`."""
    import json as _json
    from unittest.mock import AsyncMock, MagicMock
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    user = MagicMock(); user.is_admin = True; user.id = "admin"
    ctx = {"phoenix_mcp_rid": "rid", KEY_AUTHENTICATED: True, KEY_HASS_USER: user}
    req = MagicMock()
    req.get = MagicMock(side_effect=lambda k, d=None: ctx.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: ctx[k])
    raw = _json.dumps(body or {}).encode()
    req.content.read = AsyncMock(side_effect=[raw, b""])
    req.headers = {}
    # _read_body compares content_length against the size cap, so a MagicMock
    # here raises a TypeError before the handler is reached.
    req.content_length = len(raw)
    return req


@pytest.mark.asyncio
async def test_create_provider_discovers_declared_capabilities(hass):
    """A newly added account has model metadata before its first chat."""
    secret_store = MagicMock()
    secret_store.add = AsyncMock(return_value="new-id")
    secret_store.list_instances.return_value = [{
        "id": "new-id", "kind": "ollama", "model": "muse-glimmer:30b-mlx",
    }]
    secret_store.has_duplicate.return_value = False
    secret_store.set_capabilities = AsyncMock(return_value=True)

    provider = MagicMock()
    provider.validate = AsyncMock(return_value=(True, ""))
    provider.list_models = AsyncMock(return_value=["muse-glimmer:30b-mlx"])
    provider.list_model_capabilities = AsyncMock(return_value={
        "muse-glimmer:30b-mlx": {"tools": True, "thinking": True, "vision": True},
    })
    cfg = agentcli.ProviderConfig(
        kind="ollama", model="muse-glimmer:30b-mlx",
        base_url="http://127.0.0.1:11434",
    )
    view = agentcli.PhoenixAgentCliProvidersView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.agentcli._probe_config", AsyncMock(return_value=cfg)), \
         patch("custom_components.phoenix_mcp.agentcli.build_provider", return_value=provider), \
         patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)), \
         patch("custom_components.phoenix_mcp.agentcli.async_get_clientsession", return_value=MagicMock()):
        response = await view.post(_admin_request({
            "kind": "ollama", "base_url": "http://127.0.0.1:11434",
            "model": "muse-glimmer:30b-mlx",
        }))

    assert response.status == 201
    secret_store.set_capabilities.assert_awaited_once()
    assert secret_store.set_capabilities.await_args.args[1] == {
        "muse-glimmer:30b-mlx": {"tools": True, "thinking": True, "vision": True},
    }


@pytest.mark.asyncio
async def test_create_provider_returns_conflict_for_duplicate(hass):
    secret_store = MagicMock()
    secret_store.has_duplicate.return_value = True
    secret_store.add = AsyncMock()
    provider = MagicMock()
    provider.validate = AsyncMock(return_value=(True, ""))
    cfg = agentcli.ProviderConfig(
        kind="ollama", model="m", base_url="http://127.0.0.1:11434",
    )
    view = agentcli.PhoenixAgentCliProvidersView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.agentcli._probe_config", AsyncMock(return_value=cfg)), \
         patch("custom_components.phoenix_mcp.agentcli.build_provider", return_value=provider), \
         patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)), \
         patch("custom_components.phoenix_mcp.agentcli.async_get_clientsession", return_value=MagicMock()):
        response = await view.post(_admin_request({
            "kind": "ollama", "base_url": "http://127.0.0.1:11434", "model": "m",
        }))

    assert response.status == 409
    assert json.loads(response.text)["error"] == "already_exists"
    assert "already configured" in json.loads(response.text)["message"]
    assert json.loads(response.text)["message_key"] == "adminError.providerAlreadyConfigured"
    provider.validate.assert_not_awaited()
    secret_store.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_provider_handles_duplicate_detected_during_add(hass):
    secret_store = MagicMock()
    secret_store.has_duplicate.return_value = False
    secret_store.add = AsyncMock(side_effect=agentcli.DuplicateProviderError())
    provider = MagicMock()
    provider.validate = AsyncMock(return_value=(True, ""))
    cfg = agentcli.ProviderConfig(
        kind="ollama", model="m", base_url="http://127.0.0.1:11434",
    )
    view = agentcli.PhoenixAgentCliProvidersView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.agentcli._probe_config", AsyncMock(return_value=cfg)), \
         patch("custom_components.phoenix_mcp.agentcli.build_provider", return_value=provider), \
         patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)), \
         patch("custom_components.phoenix_mcp.agentcli.async_get_clientsession", return_value=MagicMock()):
        response = await view.post(_admin_request({
            "kind": "ollama", "base_url": "http://127.0.0.1:11434", "model": "m",
        }))

    assert response.status == 409
    assert json.loads(response.text)["message_key"] == "adminError.providerAlreadyConfigured"
    secret_store.add.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_provider_returns_conflict_before_listing_models(hass):
    secret_store = MagicMock()
    secret_store.has_duplicate.return_value = True
    provider = MagicMock()
    provider.validate = AsyncMock(return_value=(True, ""))
    provider.list_models = AsyncMock(return_value=["m"])
    cfg = agentcli.ProviderConfig(
        kind="ollama", model="", base_url="http://127.0.0.1:11434",
    )
    view = agentcli.PhoenixAgentCliProbeView()
    view.hass = hass

    with patch("custom_components.phoenix_mcp.agentcli._probe_config", AsyncMock(return_value=cfg)), \
         patch("custom_components.phoenix_mcp.agentcli.build_provider", return_value=provider), \
         patch("custom_components.phoenix_mcp.agentcli._get_secret_store", AsyncMock(return_value=secret_store)), \
         patch("custom_components.phoenix_mcp.agentcli.async_get_clientsession", return_value=MagicMock()):
        response = await view.post(_admin_request({
            "kind": "ollama", "base_url": "http://127.0.0.1:11434",
        }))

    assert response.status == 409
    assert json.loads(response.text)["error"] == "already_exists"
    assert json.loads(response.text)["message_key"] == "adminError.providerAlreadyConfigured"
    provider.validate.assert_not_awaited()
    provider.list_models.assert_not_awaited()


class TestSetDefaultModel:
    """The default model was frozen at creation, so correcting it meant deleting
    the account and re-entering the API key. That is the wrong cost for a value
    that goes stale on the PROVIDER's schedule: a shipped default id can be
    retired out from under a working install, which is what happened here."""

    @pytest.mark.asyncio
    async def test_store_updates_only_the_model(self, hass):
        from homeassistant.helpers.storage import Store
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_agentcli_model"))
        iid = await store.add("deepseek", {"api_key": "k", "model": "old", "base_url": "https://d"})

        assert await store.set_model(iid, "deepseek-v4-flash") is True

        cfg = store.get(iid)
        assert cfg["model"] == "deepseek-v4-flash"
        # The credential and endpoint are what the account IS; a model change
        # must not disturb them or a working account breaks on an unrelated edit.
        assert cfg["api_key"] == "k"
        assert cfg["base_url"] == "https://d"
        assert cfg["kind"] == "deepseek"

    @pytest.mark.asyncio
    async def test_store_reports_an_unknown_account(self, hass):
        from homeassistant.helpers.storage import Store
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_agentcli_missing"))
        assert await store.set_model("nope", "m") is False

    @pytest.mark.asyncio
    async def test_patch_sets_the_model(self, hass):
        from unittest.mock import AsyncMock, patch
        secret_store = MagicMock()
        secret_store.set_model = AsyncMock(return_value=True)
        view = agentcli.PhoenixAgentCliProviderView()
        view.hass = hass
        with patch("custom_components.phoenix_mcp.agentcli._get_secret_store",
                   AsyncMock(return_value=secret_store)):
            resp = await view.patch(_admin_request({"model": "  deepseek-v4-flash  "}), instance_id="pid")
        assert resp.status == 200
        # Trimmed, because a stray space in a model id is a 400 from the provider
        # much later, in a place that does not mention the space.
        secret_store.set_model.assert_awaited_once_with("pid", "deepseek-v4-flash")

    @pytest.mark.asyncio
    async def test_patch_refuses_an_empty_model(self, hass):
        from unittest.mock import AsyncMock, patch
        secret_store = MagicMock()
        secret_store.set_model = AsyncMock(return_value=True)
        view = agentcli.PhoenixAgentCliProviderView()
        view.hass = hass
        with patch("custom_components.phoenix_mcp.agentcli._get_secret_store",
                   AsyncMock(return_value=secret_store)):
            resp = await view.patch(_admin_request({"model": "   "}), instance_id="pid")
        assert resp.status == 400
        secret_store.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_patch_reports_an_unknown_account(self, hass):
        from unittest.mock import AsyncMock, patch
        secret_store = MagicMock()
        secret_store.set_model = AsyncMock(return_value=False)
        view = agentcli.PhoenixAgentCliProviderView()
        view.hass = hass
        with patch("custom_components.phoenix_mcp.agentcli._get_secret_store",
                   AsyncMock(return_value=secret_store)):
            resp = await view.patch(_admin_request({"model": "m"}), instance_id="gone")
        assert resp.status == 404


# --- declared model capabilities (only 2 of 12 providers publish any) ---


class TestDeclaredCapabilities:
    """Most provider APIs return an id and an owner and nothing else, so
    capability discovery covers OpenRouter and Ollama and stops. The consumer
    contract that matters is that a MISSING key means "not declared" and can
    never be read as a limit."""

    def test_openrouter_keeps_what_the_tools_filter_threw_away(self):
        caps = agentcli._openrouter_capabilities([
            {"id": "a/reasoner", "supported_parameters": ["tools", "reasoning", "temperature"],
             "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "b/plain", "supported_parameters": ["temperature"]},
        ])
        assert caps["a/reasoner"] == {"tools": True, "thinking": True, "temperature": True, "vision": True}
        assert caps["b/plain"] == {"tools": False, "thinking": False, "temperature": True}

    def test_openrouter_can_keep_modality_metadata_without_parameters(self):
        caps = agentcli._openrouter_capabilities([
            {"id": "a/vision", "architecture": {"input_modalities": ["text", "image"]}},
        ])
        assert caps["a/vision"] == {"vision": True}

    def test_openrouter_skips_a_model_that_declares_nothing(self):
        """No supported_parameters is "not declared", so the model must be absent
        rather than present with everything False, which would read as a model
        that supports nothing at all."""
        caps = agentcli._openrouter_capabilities([
            {"id": "a/quiet"},
            {"id": "b/loud", "supported_parameters": ["tools"]},
        ])
        assert "a/quiet" not in caps
        assert caps["b/loud"]["tools"] is True

    @pytest.mark.asyncio
    async def test_ollama_reads_capabilities_and_omits_temperature(self):
        """Ollama takes a temperature for every model through its options block
        and does not list it, so claiming False would invent a limit."""
        from unittest.mock import MagicMock

        class _Resp:
            status = 200
            def __init__(self, body): self._body = body
            async def json(self, content_type=None): return self._body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        bodies = {
            "toolful": {"capabilities": ["completion", "tools", "thinking"]},
            "toolless": {"capabilities": ["completion"]},
        }
        session = MagicMock()
        session.post = lambda url, **kw: _Resp(bodies[kw["json"]["model"]])

        caps = await agentcli._ollama_capabilities(session, "http://h", {}, ["toolful", "toolless"])
        assert caps["toolful"] == {"tools": True, "thinking": True, "vision": False}
        assert caps["toolless"] == {"tools": False, "thinking": False, "vision": False}
        assert "temperature" not in caps["toolful"]

    @pytest.mark.asyncio
    async def test_ollama_drops_a_model_whose_lookup_failed(self):
        """A failed lookup says nothing about the model; recording it as
        all-False would strip a working model's controls."""
        from unittest.mock import MagicMock

        class _Resp:
            def __init__(self, status, body=None): self.status, self._body = status, body
            async def json(self, content_type=None): return self._body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        session = MagicMock()
        session.post = lambda url, **kw: (
            _Resp(200, {"capabilities": ["tools"]}) if kw["json"]["model"] == "ok" else _Resp(500))

        caps = await agentcli._ollama_capabilities(session, "http://h", {}, ["ok", "broken"])
        assert set(caps) == {"ok"}

    @pytest.mark.asyncio
    async def test_a_provider_that_declares_nothing_returns_empty(self, hass):
        from unittest.mock import MagicMock
        cfg = agentcli.ProviderConfig(kind="deepseek", model="deepseek-v4-flash",
                                      base_url="https://d", api_key="k")
        caps = await agentcli.OpenAICompatProvider(cfg).list_model_capabilities(
            MagicMock(), ["deepseek-v4-flash"])
        assert caps == {}

    @pytest.mark.asyncio
    async def test_claude_declares_nothing(self, hass):
        from unittest.mock import MagicMock
        cfg = agentcli.ProviderConfig(kind="claude", model="claude-opus-4-8",
                                      base_url="https://a", api_key="k")
        assert await agentcli.ClaudeProvider(cfg).list_model_capabilities(MagicMock(), ["x"]) == {}

    @pytest.mark.asyncio
    async def test_store_records_capabilities_with_a_timestamp(self, hass):
        from homeassistant.helpers.storage import Store
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_agentcli_caps"))
        iid = await store.add("ollama", {"base_url": "http://h", "model": "m"})

        assert await store.set_capabilities(iid, {"m": {"tools": True}}, "2026-08-04T00:00:00+00:00") is True

        row = next(i for i in store.list_instances() if i["id"] == iid)
        assert row["capabilities"] == {"m": {"tools": True}}
        # The timestamp is half the value: capabilities have no invalidation
        # signal of their own, so "last checked" is what lets an operator judge
        # a silently ageing answer.
        assert row["capabilities_checked_at"] == "2026-08-04T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_an_account_that_never_refreshed_reports_no_timestamp(self, hass):
        from homeassistant.helpers.storage import Store
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_agentcli_nocaps"))
        await store.add("claude", {"api_key": "k", "model": "m"})
        row = store.list_instances()[0]
        assert row["capabilities"] == {}
        assert row["capabilities_checked_at"] is None


# --- capability PROBE: asking the API instead of guessing ---


class _ProbeProvider:
    """Stands in for a provider, answering a scripted status per request body."""

    def __init__(self, answer):
        self.answer = answer
        self.sent: list[dict] = []

    async def probe_option(self, session, extra: dict):
        self.sent.append(extra)
        return self.answer(extra)


def _effort_of(extra: dict):
    """The effort level in a probe fragment, whichever backend shape it uses."""
    if "reasoning_effort" in extra:
        return extra["reasoning_effort"]
    return (extra.get("output_config") or {}).get("effort")


class TestCapabilityProbe:
    """The two-stage technique, and it is the ORDER that makes it sound.

    Stage one sends a deliberately invalid effort and asks whether the field is
    VALIDATED. Only then does stage two's 200 mean anything, because a field
    known to be validated accepts a value only if it is in the vocabulary.
    Without stage one, a server that ignores unknown parameters answers 200 to
    every level and reads as "supports all five".
    """

    async def _probe(self, hass, kind, answer, model="m"):
        from unittest.mock import MagicMock, patch
        cfg = agentcli.ProviderConfig(kind=kind, model=model, base_url="https://x", api_key="k")
        prov = _ProbeProvider(answer)
        with patch.object(agentcli, "build_provider", return_value=prov):
            caps, calls, answered = await agentcli.async_probe_capabilities(MagicMock(), cfg)
        return caps, calls, prov, answered

    @pytest.mark.asyncio
    async def test_finds_the_levels_a_validated_field_accepts(self, hass):
        from custom_components.phoenix_mcp.const import AGENTCLI_PROBE_SENTINEL
        accepted = {"low", "high", "max"}

        def answer(extra):
            level = _effort_of(extra)
            if level is None:
                return 200                      # the temperature probe
            return 200 if level in accepted else 400

        caps, _calls, prov, _a = await self._probe(hass, "deepseek", answer)
        assert caps["effort_levels"] == ["low", "high", "max"]
        # The sentinel went first: the validation question has to be answered
        # before any level result can be read.
        assert _effort_of(prov.sent[0]) == AGENTCLI_PROBE_SENTINEL
        # DeepSeek only reads the effort when thinking is on, so a probe that
        # omitted the toggle would test nothing and pass every level.
        assert prov.sent[0]["thinking"] == {"type": "enabled"}

    @pytest.mark.asyncio
    async def test_a_server_that_ignores_unknown_parameters_teaches_nothing(self, hass):
        """The failure this whole design exists to avoid: everything answers 200,
        which would read as "supports all five levels"."""
        caps, calls, prov, _a = await self._probe(hass, "deepseek", lambda extra: 200)
        assert "effort_levels" not in caps
        # One sentinel call, then it gave up on levels entirely.
        assert calls == 2  # sentinel + temperature
        assert len([s for s in prov.sent if _effort_of(s)]) == 1

    @pytest.mark.asyncio
    async def test_an_unreachable_provider_records_nothing(self, hass):
        caps, _calls, _p, _a = await self._probe(hass, "deepseek", lambda extra: None)
        assert caps == {}

    @pytest.mark.asyncio
    async def test_every_level_refused_records_nothing(self, hass):
        """Cannot be true of a working model, so something else answered the
        calls; recording it would strip the control entirely."""
        caps, _calls, _p, _a = await self._probe(hass, "deepseek", lambda extra: 400)
        assert "effort_levels" not in caps

    @pytest.mark.asyncio
    async def test_a_refused_temperature_is_recorded_a_accepted_one_is_not(self, hass):
        def refuses_temp(extra):
            if "temperature" in extra:
                return 400
            return 200 if _effort_of(extra) != "phoenix-probe-invalid" else 400
        caps, _c, _p, _a = await self._probe(hass, "chatgpt", refuses_temp)
        assert caps["temperature"] is False

        def accepts_temp(extra):
            return 200 if "temperature" in extra else 400
        caps, _c, _p, _a = await self._probe(hass, "chatgpt", accepts_temp)
        # An accepted temperature may still be ignored, so 200 records nothing.
        assert "temperature" not in caps

    @pytest.mark.asyncio
    async def test_ollama_is_probed_for_validated_effort_levels(self, hass):
        accepted = {"low", "medium", "high", "max"}

        def answer(extra):
            level = _effort_of(extra)
            if level is None:
                return 200
            return 200 if level in accepted else 400

        caps, calls, prov, _a = await self._probe(hass, "ollama", answer)
        assert caps["effort_levels"] == ["low", "medium", "high", "max"]
        assert calls == 7  # sentinel + five candidates + temperature
        assert _effort_of(prov.sent[0]) == agentcli.AGENTCLI_PROBE_SENTINEL

    @pytest.mark.asyncio
    async def test_claude_uses_its_own_nested_effort_field(self, hass):
        caps, _c, prov, _a = await self._probe(
            hass, "claude", lambda extra: 400 if _effort_of(extra) == "phoenix-probe-invalid" else 200)
        assert caps["effort_levels"] == list(
            __import__("custom_components.phoenix_mcp.const", fromlist=["x"]).AGENTCLI_EFFORT_LEVEL_ORDER)
        assert "output_config" in prov.sent[0]

    @pytest.mark.asyncio
    async def test_the_run_is_bounded(self, hass):
        from custom_components.phoenix_mcp.const import AGENTCLI_PROBE_MAX_CALLS
        _caps, calls, _p, _a = await self._probe(
            hass, "deepseek", lambda extra: 400 if _effort_of(extra) == "phoenix-probe-invalid" else 200)
        assert calls <= AGENTCLI_PROBE_MAX_CALLS


# --- learning from a refusal during real work (the always-on backstop) ---


class TestLearnedRefusals:
    """Covers the providers the other two mechanisms cannot reach: the ones that
    publish no capabilities AND do not validate a probe. A real turn refusing a
    parameter is ground truth, costs nothing extra, and is the only signal left."""

    def test_names_the_option_the_provider_refused(self):
        assert agentcli._refused_option(
            400, "Unsupported parameter: 'temperature' is not supported with this model.",
            {"temperature": 0.7}) == "temperature"

    def test_ignores_an_option_phoenix_did_not_send(self):
        """The guard that stops an unrelated error mentioning a knob from
        teaching us the knob is unsupported."""
        assert agentcli._refused_option(
            400, "temperature must be provided by the caller", {"model": "m"}) is None

    @pytest.mark.parametrize("status", [401, 429, 500, 503])
    def test_only_a_400_teaches_anything(self, status):
        """An outage, a quota or an auth failure says nothing about the option."""
        assert agentcli._refused_option(
            status, "'temperature' is not supported", {"temperature": 1}) is None

    def test_a_substring_match_is_not_enough(self):
        """`max_temperature` is not `temperature`; a word boundary keeps a
        neighbouring field name from disabling the wrong control."""
        assert agentcli._refused_option(
            400, "max_temperature out of range", {"temperature": 1}) is None

    @pytest.mark.asyncio
    async def test_a_refusal_narrows_what_the_panel_offers(self, hass):
        from homeassistant.helpers.storage import Store
        from homeassistant.util.dt import utcnow
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_learned"))
        iid = await store.add("chatgpt", {"api_key": "k", "model": "o9"})

        assert await store.record_refusal(iid, "o9", "temperature", utcnow().isoformat()) is True

        row = next(i for i in store.list_instances() if i["id"] == iid)
        assert row["capabilities"]["o9"]["temperature"] is False

    @pytest.mark.asyncio
    async def test_a_refusal_expires(self, hass):
        from datetime import timedelta
        from homeassistant.helpers.storage import Store
        from homeassistant.util.dt import utcnow
        from custom_components.phoenix_mcp.const import AGENTCLI_LEARNED_REFUSAL_TTL_DAYS

        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_learned_old"))
        iid = await store.add("chatgpt", {"api_key": "k", "model": "o9"})
        stale = (utcnow() - timedelta(days=AGENTCLI_LEARNED_REFUSAL_TTL_DAYS + 1)).isoformat()
        await store.record_refusal(iid, "o9", "temperature", stale)

        # A model that rejected a parameter a month ago may accept it after an
        # upgrade. A permanent "no" learned from one moment is the same silently
        # stale answer this whole area exists to remove.
        row = next(i for i in store.list_instances() if i["id"] == iid)
        assert row["capabilities"].get("o9", {}) == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_timestamp_expires_rather_than_sticking(self, hass):
        from homeassistant.helpers.storage import Store
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_learned_bad"))
        iid = await store.add("chatgpt", {"api_key": "k", "model": "o9"})
        await store.record_refusal(iid, "o9", "temperature", "not-a-date")
        # Fails toward offering the control rather than hiding it.
        row = next(i for i in store.list_instances() if i["id"] == iid)
        assert row["capabilities"].get("o9", {}) == {}

    @pytest.mark.asyncio
    async def test_learning_never_overwrites_a_declared_fact_on_disk(self, hass):
        """Declared and probed answers are durable statements about the API;
        a learned refusal is one observation that has to fade, so it is stored
        apart and merged only on read."""
        from homeassistant.helpers.storage import Store
        from homeassistant.util.dt import utcnow
        store = agentcli.AgentCliStore(Store(hass, 1, "phx_test_learned_merge"))
        iid = await store.add("openrouter", {"api_key": "k", "model": "a/b"})
        await store.set_capabilities(iid, {"a/b": {"tools": True, "temperature": True}}, utcnow().isoformat())
        await store.record_refusal(iid, "a/b", "temperature", utcnow().isoformat())

        row = next(i for i in store.list_instances() if i["id"] == iid)
        assert row["capabilities"]["a/b"]["temperature"] is False             # overlaid on read
        assert row["capabilities"]["a/b"]["tools"] is True
        # AFTER the overlay ran, not before: get() copies only the top level, so
        # an overlay that wrote through would corrupt the stored fact and a check
        # taken beforehand would never see it.
        assert store.get(iid)["capabilities"]["a/b"]["temperature"] is True
        row2 = next(i for i in store.list_instances() if i["id"] == iid)
        assert row2["capabilities"]["a/b"]["temperature"] is False


class TestProbeAnswered:
    """A provider that declines every question has said nothing about the model.

    Live-hit with an OpenRouter key that had no credit: every probe came back
    refused for an ACCOUNT reason, and the panel reported it as a finding about
    the model. Only 200 and 400 are answers; everything else is the provider
    declining to be asked.
    """

    async def _probe(self, hass, answer):
        from unittest.mock import MagicMock, patch
        cfg = agentcli.ProviderConfig(kind="deepseek", model="m", base_url="https://x", api_key="k")
        with patch.object(agentcli, "build_provider", return_value=_ProbeProvider(answer)):
            return await agentcli.async_probe_capabilities(MagicMock(), cfg)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 402, 429, 503, None])
    async def test_a_declined_probe_is_not_an_answer(self, hass, status):
        _caps, _calls, answered = await self._probe(hass, lambda extra: status)
        assert answered is False

    @pytest.mark.asyncio
    async def test_a_400_counts_as_an_answer(self, hass):
        """A refusal IS information: it proves the request reached the model."""
        _caps, _calls, answered = await self._probe(hass, lambda extra: 400)
        assert answered is True

    @pytest.mark.asyncio
    async def test_a_200_counts_as_an_answer(self, hass):
        _caps, _calls, answered = await self._probe(hass, lambda extra: 200)
        assert answered is True


class TestAggregatorEffort:
    """An aggregator fronts many vendors behind one key, so no built-in answer
    fits every model. Excluding them from probing was backwards: a probe runs
    against ONE selected model, which is exactly what per-model variation needs.
    """

    @pytest.mark.parametrize("kind", ["openrouter", "nvidia"])
    def test_the_probe_asks_them_about_effort(self, kind):
        assert agentcli._effort_probe_body(kind, "high") == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("kind", ["minimax"])
    def test_a_backend_with_no_level_vocabulary_is_still_skipped(self, kind):
        """MiniMax has an adaptive toggle, not a level vocabulary."""
        assert agentcli._effort_probe_body(kind, "high") is None

    @pytest.mark.parametrize("kind", ["ollama", "ollama_cloud"])
    def test_ollama_probe_uses_its_openai_compatible_effort_field(self, kind):
        assert agentcli._effort_probe_body(kind, "high") == {"reasoning_effort": "high"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["openrouter", "nvidia"])
    async def test_an_effort_reaches_the_request(self, hass, kind):
        cfg = agentcli.ProviderConfig(kind=kind, model="vendor/m", base_url="https://x", api_key="k")
        # Capture the request the real builder produces.
        provider = agentcli.OpenAICompatProvider(cfg)
        sent = {}

        class _Resp:
            status = 200
            async def text(self): return ""
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            @property
            def content(self): raise AssertionError("not reached")

        def _post(url, **kw):
            sent.update(kw.get("json") or {})
            return _Resp()

        session = MagicMock()
        session.post = _post
        gen = provider.stream_turn(session, system_prompt="s", messages=[], tools=[],
                                   options={"effort": "high"})
        try:
            async for _ev in gen:
                break
        except Exception:
            pass
        assert sent.get("reasoning_effort") == "high"

    @pytest.mark.asyncio
    async def test_no_effort_means_no_field(self, hass):
        """A turn that chose nothing must not force a level on the model."""
        cfg = agentcli.ProviderConfig(kind="openrouter", model="vendor/m",
                                      base_url="https://x", api_key="k")
        provider = agentcli.OpenAICompatProvider(cfg)
        sent = {}

        class _Resp:
            status = 200
            async def text(self): return ""
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            @property
            def content(self): raise AssertionError("not reached")

        session = MagicMock()
        session.post = lambda url, **kw: (sent.update(kw.get("json") or {}), _Resp())[1]
        gen = provider.stream_turn(session, system_prompt="s", messages=[], tools=[], options={})
        try:
            async for _ev in gen:
                break
        except Exception:
            pass
        assert "reasoning_effort" not in sent
