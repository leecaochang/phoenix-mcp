"""Tests for the Phoenix MCP AI Task entity (ai_task.py + agentcli.async_run_ai_task).

The AITaskEntity base class cannot be imported here, because ai_task's own import
chain reaches a dependency this environment does not carry, so ai_task_supported()
is False and PhoenixAITaskEntity is None. That is the point of the feature-detect
guard: the generation core (async_generate_ai_task_data) is module-level and free
of that chain, so the real logic is covered without the entity base class. The
entity wrapper itself is thin (an availability property plus a delegate to the
core).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.phoenix_mcp import ai_task, agentcli
from custom_components.phoenix_mcp.agentcli import AiTaskError, async_run_ai_task
from custom_components.phoenix_mcp.const import AGENTCLI_MAX_TURN_OUTPUT_BYTES, AI_TASK_CLIENT_IP
from custom_components.phoenix_mcp.token_store import GlobalSettings
from homeassistant.exceptions import HomeAssistantError


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


def _staged_stream(*batches, capture=None):
    state = {"i": 0}

    async def gen(provider, session, **kwargs):
        if capture is not None:
            capture["system_prompt"] = kwargs.get("system_prompt")
        i = state["i"]
        state["i"] += 1
        batch = batches[i] if i < len(batches) else batches[-1]
        for ev in batch:
            yield ev

    return gen


def _tool_ev(name, tid, args=None):
    return {"type": agentcli.EV_TOOL, "name": name, "id": tid, "input": args or {}}


def _done(stop_reason):
    return {"type": agentcli.EV_DONE, "stop_reason": stop_reason,
            "assistant_msg": {"role": "assistant", "content": []}}


def _make_token():
    return SimpleNamespace(id="tok-1", announce_all_tools=True, confirm_inline_wait_seconds=0)


def _make_data(**settings_over):
    settings = GlobalSettings(**settings_over)
    store = MagicMock()
    store.get_settings.return_value = settings
    return SimpleNamespace(store=store, mesa=None, ready=True, shutting_down=False)


_FULL = dict(
    ai_task_enabled=True, ai_task_token_id="tok-1",
    ai_task_provider_id="i1", ai_task_model="m",
)


# --------------------------------------------------------------------------- #
# module-level helpers
# --------------------------------------------------------------------------- #

def test_supported_is_false_in_test_env():
    # Confirms the guard: entity base unimportable here, core still available.
    assert ai_task.ai_task_supported() is False
    assert ai_task.PhoenixAITaskEntity is None


# --------------------------------------------------------------------------- #
# async_sync_ai_task (dynamic entity lifecycle)
# --------------------------------------------------------------------------- #

def test_sync_noop_when_unsupported():
    # _AI_TASK_AVAILABLE is False in the test env; the sync must return before touching
    # the (None) entity class, even when the config would otherwise warrant an entity.
    data = _make_data(**_FULL)
    data.ai_task_entity = None
    add = MagicMock()
    ai_task.async_sync_ai_task(MagicMock(), SimpleNamespace(entry_id="e"), data, add)
    add.assert_not_called()
    assert data.ai_task_entity is None


def test_sync_adds_entity_when_fully_configured():
    data = _make_data(**_FULL)
    data.ai_task_entity = None
    add = MagicMock()
    dummy = SimpleNamespace(entity_id="ai_task.phoenix_mcp_ai_task")
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True), \
         patch.object(ai_task, "PhoenixAITaskEntity", MagicMock(return_value=dummy)):
        ai_task.async_sync_ai_task(MagicMock(), SimpleNamespace(entry_id="e"), data, add)
    assert data.ai_task_entity is dummy
    add.assert_called_once_with([dummy])


def test_sync_adds_configured_entity_before_runtime_ready_on_restart():
    """Platform setup precedes Phoenix's final ready publication on every restart."""
    data = _make_data(**_FULL)
    data.ready = False
    data.ai_task_entity = None
    add = MagicMock()
    dummy = SimpleNamespace(entity_id="ai_task.phoenix_mcp_ai_task")
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True), \
         patch.object(ai_task, "PhoenixAITaskEntity", MagicMock(return_value=dummy)):
        ai_task.async_sync_ai_task(MagicMock(), SimpleNamespace(entry_id="e"), data, add)
    assert data.ai_task_entity is dummy
    add.assert_called_once_with([dummy])


@pytest.mark.asyncio
async def test_sync_removes_registry_entry_when_not_configured(hass):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    reg.async_get_or_create("ai_task", "phoenix_mcp", "e1_ai_task")
    assert reg.async_get_entity_id("ai_task", "phoenix_mcp", "e1_ai_task") is not None

    data = _make_data(ai_task_enabled=False)  # not fully configured -> should not exist
    data.ai_task_entity = None
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True):
        ai_task.async_sync_ai_task(hass, SimpleNamespace(entry_id="e1"), data, MagicMock())
    # The registry entry (and thus the live entity + picker option) is gone.
    assert reg.async_get_entity_id("ai_task", "phoenix_mcp", "e1_ai_task") is None
    assert data.ai_task_entity is None


# --------------------------------------------------------------------------- #
# AI Task "default data-gen entity" preference (one-click)
# --------------------------------------------------------------------------- #

class _FakePrefs:
    """Stand-in for HA's AITaskPreferences."""

    def __init__(self, initial=None):
        self.gen_data_entity_id = initial

    def async_set_preferences(self, *, gen_data_entity_id):
        self.gen_data_entity_id = gen_data_entity_id


def test_preferred_status_unsupported_in_env(hass):
    # No prefs store in the test env (import needs turbojpeg): supported is False.
    status = ai_task.ai_task_preferred_status(hass, SimpleNamespace(entry_id="e"))
    assert status["supported"] is False
    assert status["is_preferred"] is False


def test_set_preferred_requires_entity(hass):
    # No Phoenix MCP AI Task entity registered -> setup error before touching prefs.
    with pytest.raises(ai_task.AiTaskSetupError):
        ai_task.set_ai_task_preferred(hass, SimpleNamespace(entry_id="nope"))


def test_set_preferred_updates_prefs_and_status(hass):
    from homeassistant.helpers import entity_registry as er

    ent = er.async_get(hass).async_get_or_create("ai_task", "phoenix_mcp", "e1_ai_task")
    fake = _FakePrefs()
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True), \
         patch.object(ai_task, "_ai_task_preferences", return_value=fake):
        status = ai_task.set_ai_task_preferred(hass, SimpleNamespace(entry_id="e1"))
    assert fake.gen_data_entity_id == ent.entity_id
    assert status["is_preferred"] is True
    assert status["entity_id"] == ent.entity_id


def test_clear_preferred_resets_when_ours(hass):
    from homeassistant.helpers import entity_registry as er

    ent = er.async_get(hass).async_get_or_create("ai_task", "phoenix_mcp", "e1_ai_task")
    fake = _FakePrefs(ent.entity_id)
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True), \
         patch.object(ai_task, "_ai_task_preferences", return_value=fake):
        status = ai_task.clear_ai_task_preferred(hass, SimpleNamespace(entry_id="e1"))
    assert fake.gen_data_entity_id is None
    assert status["is_preferred"] is False


def test_sync_clears_preference_when_removing_preferred_entity(hass):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    ent = reg.async_get_or_create("ai_task", "phoenix_mcp", "e1_ai_task")
    fake = _FakePrefs(ent.entity_id)  # Phoenix MCP is HA's default data-gen entity
    data = _make_data(ai_task_enabled=False)  # not configured -> entity removed
    data.ai_task_entity = None
    with patch.object(ai_task, "_AI_TASK_AVAILABLE", True), \
         patch.object(ai_task, "_ai_task_preferences", return_value=fake):
        ai_task.async_sync_ai_task(hass, SimpleNamespace(entry_id="e1"), data, MagicMock())
    assert reg.async_get_entity_id("ai_task", "phoenix_mcp", "e1_ai_task") is None
    assert fake.gen_data_entity_id is None  # dangling default cleared on teardown


def test_is_fully_configured():
    assert ai_task._is_fully_configured(GlobalSettings(**_FULL)) is True
    assert ai_task._is_fully_configured(GlobalSettings(ai_task_enabled=True)) is False
    assert ai_task._is_fully_configured(GlobalSettings(**{**_FULL, "ai_task_enabled": False})) is False


def test_structure_to_json_roundtrips_and_none():
    assert ai_task._structure_to_json(None) is None
    schema = vol.Schema({vol.Required("summary"): str, vol.Optional("count"): int})
    out = ai_task._structure_to_json(schema)
    assert out["type"] == "object"
    assert "summary" in out["properties"] and out["required"] == ["summary"]


def test_structure_to_json_supports_home_assistant_selector_schema():
    from homeassistant.helpers import selector

    schema = vol.Schema(
        {
            vol.Required("scene_name", description="Name of the example scene"): (
                selector.TextSelector({})
            ),
            vol.Required("purpose", description="One-sentence purpose of the scene"): (
                selector.TextSelector({})
            ),
            vol.Optional("priority"): selector.NumberSelector({"min": 1, "max": 5}),
        },
        extra=vol.PREVENT_EXTRA,
    )

    assert ai_task._structure_to_json(schema) == {
        "type": "object",
        "properties": {
            "scene_name": {
                "type": "string",
                "description": "Name of the example scene",
            },
            "purpose": {
                "type": "string",
                "description": "One-sentence purpose of the scene",
            },
            "priority": {
                "type": "number",
                "minimum": 1.0,
                "maximum": 5.0,
                "multipleOf": 1,
            },
        },
        "additionalProperties": False,
        "required": ["scene_name", "purpose"],
    }


def test_parse_structured_plain_and_fenced_and_bad():
    assert ai_task._parse_structured('{"a": 1}') == {"a": 1}
    assert ai_task._parse_structured('```json\n{"a": 2}\n```') == {"a": 2}
    assert ai_task._parse_structured('```\n{"a": 3}\n```') == {"a": 3}
    with pytest.raises(HomeAssistantError):
        ai_task._parse_structured("not json at all")


# --------------------------------------------------------------------------- #
# async_generate_ai_task_data
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_ai_task_raises_when_the_provider_blows_the_output_budget():
    """The output ceiling is a failure here, not a degraded answer.

    Voice degrades to a spoken sentence because someone is listening; an
    automation asked for data, so the only honest outcome is an error the caller
    turns into a HomeAssistantError. Shares AGENTCLI_MAX_TURN_OUTPUT_BYTES with
    the interactive loop, which had enforced it while both headless surfaces
    retained an unbounded transcript across every round.
    """
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    huge = {"role": "assistant", "content": "x" * (AGENTCLI_MAX_TURN_OUTPUT_BYTES + 1)}
    stream = _staged_stream([{"type": agentcli.EV_DONE, "stop_reason": "tool_use",
                              "assistant_msg": huge}])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"), \
         pytest.raises(AiTaskError, match="too much output"):
        await async_run_ai_task(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "summarize")

    # Rejected before the append, so it never entered the re-sent transcript.
    assert provider.tool_result_batches == []


@pytest.mark.asyncio
async def test_ai_task_raises_when_it_runs_out_of_steps():
    """Exhaustion must not masquerade as a formatting problem.

    Running out of rounds left final_text as "" (the round that exhausts the
    budget is a tool round, which resets it). With a structure, _parse_structured
    then reported "could not parse structured output" - blaming the model for
    JSON it never got the chance to write, and pointing the operator at the
    prompt instead of at Steps before check-in. Without a structure it returned
    an empty string to the automation silently.
    """
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([_tool_ev("get_state", "t1"), _done("tool_use")])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=_dispatch_ok("on")), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"), \
         pytest.raises(AiTaskError, match="ran out of steps"):
        await async_run_ai_task(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "summarize",
            max_iterations=3)


@pytest.mark.asyncio
async def test_ai_task_raises_on_a_truncated_result():
    """Half a result is not a result: an automation would store it as one.

    With a structure this previously surfaced as a JSON parse error (true, but
    it named the wrong cause); without one it returned a half-written answer
    that looked complete.
    """
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    stream = _staged_stream([
        {"type": agentcli.EV_TEXT, "text": '{"summary": "the kitchen'},
        {"type": agentcli.EV_DONE, "stop_reason": "max_tokens",
         "assistant_msg": {"role": "assistant", "content": []}},
    ])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"), \
         pytest.raises(AiTaskError, match="output length limit"):
        await async_run_ai_task(
            MagicMock(), data, token, provider, MagicMock(), "http://h", "summarize")


@pytest.mark.asyncio
async def test_ai_task_stops_after_one_round_when_authority_is_lost():
    """A dead token buys one closing round here too, not max_iterations of them."""
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    dispatch = _dispatch_ok("should not run")
    stream = _staged_stream([_tool_ev("call_service", "t1"), _done("tool_use")])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=None), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        with pytest.raises(AiTaskError):
            await async_run_ai_task(
                MagicMock(), data, token, provider, MagicMock(), "http://h", "x",
                max_iterations=20)

    assert dispatch.calls == []
    assert len(provider.tool_result_batches) == 1


@pytest.mark.asyncio
async def test_generate_free_text_happy_path():
    data = _make_data(**_FULL)
    store = MagicMock()
    store.resolve.return_value = SimpleNamespace(model="m")
    with patch.object(agentcli, "_current_dispatch_token", return_value=_make_token()), \
         patch.object(agentcli, "_get_secret_store", AsyncMock(return_value=store)), \
         patch.object(agentcli, "build_provider", return_value=_MockProvider()), \
         patch.object(agentcli, "async_run_ai_task", AsyncMock(return_value="A plain summary.")):
        result = await ai_task.async_generate_ai_task_data(MagicMock(), data, "summarize", None)
    assert result == "A plain summary."


@pytest.mark.asyncio
async def test_generate_structured_parses_json():
    data = _make_data(**_FULL)
    store = MagicMock()
    store.resolve.return_value = SimpleNamespace(model="m")
    schema = vol.Schema({vol.Required("summary"): str})
    with patch.object(agentcli, "_current_dispatch_token", return_value=_make_token()), \
         patch.object(agentcli, "_get_secret_store", AsyncMock(return_value=store)), \
         patch.object(agentcli, "build_provider", return_value=_MockProvider()), \
         patch.object(agentcli, "async_run_ai_task", AsyncMock(return_value='{"summary": "ok"}')) as rt:
        result = await ai_task.async_generate_ai_task_data(MagicMock(), data, "do it", schema)
    assert result == {"summary": "ok"}
    # structure_json was passed to the loop (so the model was told to emit JSON).
    assert rt.call_args.kwargs["structure_json"]["type"] == "object"


@pytest.mark.asyncio
async def test_generate_disabled_raises():
    data = _make_data(ai_task_enabled=False)
    with pytest.raises(HomeAssistantError, match="not fully configured"):
        await ai_task.async_generate_ai_task_data(MagicMock(), data, "x", None)


@pytest.mark.asyncio
async def test_generate_kill_switch_raises_unavailable():
    data = _make_data(kill_switch=True, **_FULL)
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await ai_task.async_generate_ai_task_data(MagicMock(), data, "x", None)


@pytest.mark.asyncio
async def test_generate_token_gone_raises():
    data = _make_data(**_FULL)
    with patch.object(agentcli, "_current_dispatch_token", return_value=None):
        with pytest.raises(HomeAssistantError, match="token"):
            await ai_task.async_generate_ai_task_data(MagicMock(), data, "x", None)


@pytest.mark.asyncio
async def test_generate_wraps_loop_error():
    data = _make_data(**_FULL)
    store = MagicMock()
    store.resolve.return_value = SimpleNamespace(model="m")
    with patch.object(agentcli, "_current_dispatch_token", return_value=_make_token()), \
         patch.object(agentcli, "_get_secret_store", AsyncMock(return_value=store)), \
         patch.object(agentcli, "build_provider", return_value=_MockProvider()), \
         patch.object(agentcli, "async_run_ai_task", AsyncMock(side_effect=AiTaskError("model down"))):
        with pytest.raises(HomeAssistantError, match="model down"):
            await ai_task.async_generate_ai_task_data(MagicMock(), data, "x", None)


# --------------------------------------------------------------------------- #
# async_run_ai_task (the gated loop)
# --------------------------------------------------------------------------- #

def _dispatch_ok(text):
    async def fake(method, msg_id, params, tok, hass, data, client_ip, base_url, *a, **k):
        fake.calls.append({"client_ip": client_ip, "name": params["name"]})
        return ({"result": {"content": [{"type": "text", "text": text}]}}, "n", "r", "allowed")
    fake.calls = []
    return fake


@pytest.mark.asyncio
async def test_run_ai_task_tool_then_answer_uses_ai_task_sentinel():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()
    dispatch = _dispatch_ok('{"state": "on"}')
    stream = _staged_stream(
        [_tool_ev("get_state", "t1", {"entity_id": "light.x"}), _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "final answer"}, _done("end_turn")],
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_ai_task(MagicMock(), data, token, provider, MagicMock(), "http://h", "instr")
    assert text == "final answer"
    assert dispatch.calls == [{"client_ip": AI_TASK_CLIENT_IP, "name": "get_state"}]


@pytest.mark.asyncio
async def test_run_ai_task_includes_structure_directive_in_system_prompt():
    token = _make_token()
    data = _make_data()
    capture: dict = {}
    stream = _staged_stream(
        [{"type": agentcli.EV_TEXT, "text": '{"a": 1}'}, _done("end_turn")],
        capture=capture,
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_ai_task(
            MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h", "instr",
            structure_json={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
    assert text == '{"a": 1}'
    assert "JSON schema" in capture["system_prompt"]
    assert '"type": "object"' in capture["system_prompt"]
    assert "Conversation presentation policy" not in capture["system_prompt"]
    assert "matter-of-fact" not in capture["system_prompt"]


@pytest.mark.asyncio
async def test_run_ai_task_free_text_uses_selected_style_and_detail():
    token = _make_token()
    data = _make_data(
        ai_task_conversation_style="technical", ai_task_detail_level="detailed",
    )
    capture: dict = {}
    stream = _staged_stream(
        [{"type": agentcli.EV_TEXT, "text": "result"}, _done("end_turn")],
        capture=capture,
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_ai_task(
            MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h", "instr",
        )
    assert text == "result"
    assert "precise terminology" in capture["system_prompt"]
    assert "thorough reasoning" in capture["system_prompt"]
    assert "AI Task free-text output contract" in capture["system_prompt"]
    assert "Do not mention tools, available entities" in capture["system_prompt"]
    assert agentcli._HOME_FOCUS_INSTRUCTION not in capture["system_prompt"]


@pytest.mark.asyncio
async def test_run_ai_task_pending_becomes_queued_without_blocking():
    token = _make_token()
    data = _make_data()
    provider = _MockProvider()

    async def dispatch_pending(method, msg_id, params, *a, **k):
        return ({"result": {"content": [{"type": "text", "text": '{"status":"pending_approval","approval_id":"ap1"}'}]}}, "n", "r", "pending")

    stream = _staged_stream(
        [_tool_ev("call_service", "t1"), _done("tool_use")],
        [{"type": agentcli.EV_TEXT, "text": "queued"}, _done("end_turn")],
    )
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch.object(agentcli, "_current_dispatch_token", return_value=token), \
         patch("custom_components.phoenix_mcp.mcp_view._dispatch_mcp", side_effect=dispatch_pending), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        text = await async_run_ai_task(MagicMock(), data, token, provider, MagicMock(), "http://h", "unlock")
    assert text == "queued"
    fed_back = provider.tool_result_batches[0][0]["result_text"]
    assert "queued_for_approval" in fed_back


@pytest.mark.asyncio
async def test_run_ai_task_error_raises_aitaskerror():
    token = _make_token()
    data = _make_data()
    stream = _staged_stream([{"type": agentcli.EV_ERROR, "message": "boom"}])
    with patch.object(agentcli, "_stream_turn_resilient", stream), \
         patch.object(agentcli, "build_mcp_tool_list", return_value=[]), \
         patch("custom_components.phoenix_mcp.mcp_view._build_instructions", return_value="SYS"):
        with pytest.raises(AiTaskError, match="boom"):
            await async_run_ai_task(MagicMock(), data, token, _MockProvider(), MagicMock(), "http://h", "hi")
