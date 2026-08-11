"""Archetype-based adversarial testing across the whole tool surface.

The per-tool suites elsewhere in tests/ each assert what ONE tool does with
sensible input. This suite asserts what EVERY tool does with hostile input, and
derives both the tools and the attacks from the published inputSchema, so a
tool added tomorrow is covered without anyone writing a case for it. That is
the whole point: coverage that scales with the surface instead of with effort.

The attacks come from the shapes a real model actually emits. A list where a
string was declared is not hypothetical: a model targeting two entities by name
sends one, and HA's matcher raises AttributeError on it. So is a JSON-encoded
string where an object was declared, and a stray extra argument, which weaker
local models produce constantly.

CALLED DIRECTLY, NOT THROUGH THE DISPATCHER, on purpose. _dispatch_mcp wraps
tools/call in a catch-all that turns any unhandled exception into a clean
"Internal error". Running the torture through it would make this suite pass
against a tool that crashes on every input. The catch-all is defense in depth,
not a substitute for the tools being correct, so the assertion here is that
_call_tool itself never raises.

Four invariants, checked on every (tool, attack) pair:

1. It returns, rather than raising.
2. It returns the MCP content contract, with a recognised audit outcome.
3. It never leaks a traceback or an interpreter path into the agent's response.
4. A FULLY DENIED token learns nothing about its own payload. Every attack
   string carries a marker; if it comes back, the tool told a token with no
   capability something about what it sent, which would let it use the error
   text as a validator for its own input.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import mcp_view, mesa_tools
from custom_components.phoenix_mcp.const import CAPABILITY_NAMES
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import GlobalSettings, PermissionTree, TokenRecord
from custom_components.phoenix_mcp.version_store import VersionStore

# Threaded through every attack string. Its absence from a denied response is
# invariant 4; making it distinctive keeps that check from matching prose.
MARKER = "PHXTORTURE"

VALID_OUTCOMES = frozenset({
    "allowed", "denied", "not_found", "rate_limited",
    "not_implemented", "invalid_request", "pending_approval",
})

# Unambiguous evidence that an internal failure reached the caller. Deliberately
# narrow: legitimate responses do carry config-relative paths and validation
# messages, and a broad "no path anywhere" rule would fire on those.
LEAK_MARKERS = ("Traceback (most recent call last)", "site-packages/homeassistant")


# ---------------------------------------------------------------- the surface

def _all_defs() -> list[dict]:
    return [
        *mcp_view._ENTITY_TOOL_DEFS,
        *mcp_view._NATIVE_TOOL_DEFS,
        *mcp_view._SYSTEM_TOOL_DEFS,
        *mesa_tools.mesa_tool_defs(),
    ]


def _archetype(tool_def: dict) -> str:
    """Classify a tool by the SHAPE of its declared input.

    Derived from the schema rather than a hand-kept list, so the classification
    cannot drift away from the tool it describes.
    """
    props: dict = tool_def.get("inputSchema", {}).get("properties", {}) or {}
    names = set(props)
    if not names:
        return "parameterless"
    if names & {"area", "area_id", "device_id", "floor"}:
        return "bulk_target"
    if "entity_id" in names:
        return "entity_target"
    if {"config", "content", "card"} & names:
        return "config_blob_write"
    if any(n.endswith("_id") for n in names):
        return "optional_id"
    return "scalar_args"


# ------------------------------------------------------------------ the attacks

def _placeholder(name: str, schema: dict):
    """A structurally VALID value, so an attack on one field is not masked by a
    second field being absent."""
    kind = schema.get("type")
    if kind == "object":
        return {}
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return 1
    if name == "entity_id":
        return "light.phx_ghost"
    if name.endswith("_id"):
        return "phx_ghost_id"
    return f"{MARKER}_value"


def _hostile_values(kind: str | None) -> list:
    """Values of the wrong shape for a declared type, in the flavours real
    clients actually emit."""
    common = [None, 0, True, [], {}]
    if kind == "string":
        # The list case is the live-found intent-tool crash; the long string is
        # the unbounded-field case; the rest are traversal and template probes
        # that must be refused rather than interpreted.
        return common + [
            [f"{MARKER}_a", f"{MARKER}_b"],
            {"nested": MARKER},
            "",
            f"../../../etc/{MARKER}",
            "{{ states('person.someone') }}" + MARKER,
            f"{MARKER}\x00truncated",
            MARKER + "x" * 20000,
        ]
    if kind == "object":
        # A model that JSON-encodes an object it was told to send as an object.
        return common + [f'{{"json_as_string": "{MARKER}"}}', [{"k": MARKER}], f"{MARKER}"]
    if kind == "array":
        return common + [f"{MARKER}_not_a_list", {"0": MARKER}]
    if kind == "boolean":
        return [None, f"{MARKER}", 2, [], {}]
    if kind in ("integer", "number"):
        return [None, f"{MARKER}", [], {}, -1, 10**18]
    return common + [f"{MARKER}"]


def _attacks(tool_def: dict) -> list[tuple[str, dict]]:
    """Every (label, arguments) pair to throw at one tool."""
    schema = tool_def.get("inputSchema", {}) or {}
    props: dict = schema.get("properties", {}) or {}
    required: list[str] = schema.get("required", []) or []
    base = {name: _placeholder(name, spec) for name, spec in props.items()}

    cases: list[tuple[str, dict]] = [("empty", {})]
    # A stray argument must not hard-fail: unknown-key rejection was deliberately
    # not enforced, because weak models emit stray fields and refusing them tanks
    # task completion without protecting anything.
    cases.append(("unknown_key", {**base, f"{MARKER}_unknown": MARKER}))
    if required:
        # Every required field present but every one of them nonsense.
        cases.append(("all_required_wrong_type", {
            name: _hostile_values(props.get(name, {}).get("type"))[0] for name in required
        }))
    for name, spec in props.items():
        for value in _hostile_values(spec.get("type")):
            cases.append((f"{name}={type(value).__name__}", {**base, name: value}))
    return cases


# ------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    """Anything that does reach a file write must land in a throwaway dir, not
    in the shared testing config the rest of the suite reads."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\nscript: !include scripts.yaml\n",
        encoding="utf-8",
    )
    return tmp_path


def _data() -> PhoenixData:
    store = MagicMock()
    store.get_settings.return_value = GlobalSettings()
    return PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), versions=VersionStore())


def _token(mode: str) -> TokenRecord:
    """A token whose every capability is `mode`, with an EMPTY permission tree:
    no entity is ever in scope, so nothing real is actuated."""
    return TokenRecord(
        id=str(uuid.uuid4()), name="torture", token_hash="x", created_at=utcnow(),
        created_by="admin", permissions=PermissionTree(domains={}),
        **{cap: mode for cap in CAPABILITY_NAMES},
    )


def _assert_contract(result, outcome, resource, label: str) -> str:
    """Invariants 2 and 3. Returns the response text for further assertions."""
    assert isinstance(result, dict), f"{label}: returned {type(result).__name__}, not a dict"
    content = result.get("content")
    assert isinstance(content, list) and content, f"{label}: no content list"
    texts = []
    for item in content:
        assert item.get("type") == "text", f"{label}: content item is not text"
        assert isinstance(item.get("text"), str), f"{label}: content text is not a string"
        texts.append(item["text"])
    assert outcome in VALID_OUTCOMES, f"{label}: unrecognised outcome {outcome!r}"
    assert isinstance(resource, str), f"{label}: resource is not a string"
    joined = "\n".join(texts)
    for leak in LEAK_MARKERS:
        assert leak not in joined, f"{label}: leaked internals into the response ({leak})"
    return joined


# ---------------------------------------------------------------------- tests

ALL_DEFS = _all_defs()
BY_ARCHETYPE: dict[str, list[dict]] = {}
for _d in ALL_DEFS:
    BY_ARCHETYPE.setdefault(_archetype(_d), []).append(_d)

# Tools whose EXECUTION is not something a test run should trigger. Taken from
# the tool's own destructiveHint rather than a hand-kept list, so a new
# destructive tool excludes itself. They still get the denied-token pass below,
# which is where the uniform-refusal invariant lives anyway.
EXECUTABLE_DEFS = [d for d in ALL_DEFS if not d.get("annotations", {}).get("destructiveHint")]


def test_every_tool_falls_into_a_known_archetype():
    # A tool whose shape matches nothing would silently get no torture at all.
    assert set(BY_ARCHETYPE) <= {
        "parameterless", "entity_target", "bulk_target",
        "config_blob_write", "optional_id", "scalar_args",
    }
    assert sum(len(v) for v in BY_ARCHETYPE.values()) == len(ALL_DEFS)
    # Each archetype must actually have members, or the suite is quietly testing
    # fewer shapes than it claims to.
    for name in ("parameterless", "entity_target", "config_blob_write"):
        assert BY_ARCHETYPE.get(name), f"no tools classified as {name}"


@pytest.mark.parametrize("tool_def", ALL_DEFS, ids=lambda d: d["name"])
async def test_denied_token_survives_hostile_input(tool_def: dict, hass: HomeAssistant):
    """Invariants 1-3 against a token holding nothing."""
    token, data, name = _token("deny"), _data(), tool_def["name"]
    for label, args in _attacks(tool_def):
        result, outcome, resource = await _call_tool(name, args, token, hass, data, "req", "1.2.3.4")
        _assert_contract(result, outcome, resource, f"{name}/{label}")


# Only a CAP-gated tool owes the uniform refusal. A capless tool (the native
# Hass* family) is scoped by entity and validates its arguments first, and its
# "'x' is not of type 'integer'" is a statement about the caller's own input,
# not about the instance; the same reasoning already lets ServiceValidationError
# text through so an agent can self-correct.
CAP_GATED_DEFS = [d for d in ALL_DEFS if mcp_view._tool_caps(d)]


@pytest.mark.parametrize("tool_def", CAP_GATED_DEFS, ids=lambda d: d["name"])
async def test_cap_denied_tool_never_echoes_the_payload(tool_def: dict, hass: HomeAssistant):
    """Invariant 4, the security one.

    A token whose capability is denied must not be able to use the tool's error
    messages as a validator for its own input. That is why the cap-deny check
    runs BEFORE any precheck: otherwise a denied token can ask "would this have
    worked?" and read the answer off the error text.
    """
    token, data, name = _token("deny"), _data(), tool_def["name"]
    for label, args in _attacks(tool_def):
        result, _outcome, _resource = await _call_tool(name, args, token, hass, data, "req", "1.2.3.4")
        text = "\n".join(i["text"] for i in result["content"])
        assert MARKER not in text, (
            f"{name}/{label}: a token denied {mcp_view._tool_caps(tool_def)} got its own payload "
            f"echoed back, so the error text is a validity oracle: {text[:300]}"
        )


@pytest.mark.parametrize("tool_def", EXECUTABLE_DEFS, ids=lambda d: d["name"])
async def test_permitted_token_survives_hostile_input(tool_def: dict, hass: HomeAssistant):
    """Invariants 1-3 with every capability granted, so the attack reaches the
    tool's own argument handling instead of stopping at the cap gate."""
    token, data, name = _token("allow"), _data(), tool_def["name"]
    for label, args in _attacks(tool_def):
        result, outcome, resource = await _call_tool(name, args, token, hass, data, "req", "1.2.3.4")
        _assert_contract(result, outcome, resource, f"{name}/{label}")


@pytest.mark.parametrize("tool_def", EXECUTABLE_DEFS, ids=lambda d: d["name"])
async def test_concurrent_identical_calls_do_not_break_each_other(
    tool_def: dict, hass: HomeAssistant,
):
    """The double-delete / racing-writer shape, cheaply.

    Two identical calls in flight at once is what an agent retrying a slow tool
    actually produces. Neither may raise, and gather is given no
    return_exceptions so a raise in either fails the test rather than hiding in
    a result list.
    """
    token, data, name = _token("allow"), _data(), tool_def["name"]
    schema = tool_def.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    args = {n: _placeholder(n, s) for n, s in props.items()}
    outcomes = await asyncio.gather(*(
        _call_tool(name, dict(args), token, hass, data, f"req{i}", "1.2.3.4") for i in range(2)
    ))
    for i, (result, outcome, resource) in enumerate(outcomes):
        _assert_contract(result, outcome, resource, f"{name}/concurrent-{i}")


async def test_unknown_arguments_are_tolerated_not_rejected(hass: HomeAssistant):
    """A stray argument must not turn a valid call into an error.

    Pinned separately from the sweep above because it is a DELIBERATE decision
    (no per-tool additionalProperties: false) that reads like an oversight, and
    the sweep only asserts the call survives, not that it stayed successful.
    """
    token, data = _token("allow"), _data()
    hass.states.async_set("light.kitchen", "on")
    clean, _, _ = await _call_tool("get_states", {}, token, hass, data)
    dirty, outcome, _ = await _call_tool(
        "get_states", {f"{MARKER}_unknown": MARKER, "nonsense": [1, 2]}, token, hass, data,
    )
    assert outcome == "allowed"
    assert dirty["content"][0]["text"] == clean["content"][0]["text"]
