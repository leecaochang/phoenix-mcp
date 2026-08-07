"""Pin the native Hass* tool surface against the intents the installed HA registers.

The Hass* names, and the descriptions published with them, are a snapshot of what
Home Assistant generates at runtime: HA builds each tool from an intent handler's
own `description` and `slot_schema` (helpers/llm.py), while Phoenix MCP freezes
the resulting strings into tool_defs.py so the catalog is static and needs no
integration set up to serve. Nothing else notices when the two fall out of step,
and every way they can is silent: a reworded description keeps saying the old
thing, a renamed intent leaves a tool pointing at nothing, and an intent HA adds
is simply never considered.

These tests close all three directions. They also pin the triage list below, so
an intent HA starts registering fails here until somebody writes down why Phoenix
MCP does or does not publish it.

Bound worth knowing: the registry only holds intents whose component is set up,
so this checks the core actuator surface listed in _INTENT_COMPONENTS rather than
every intent a particular installation might have. That is the same bound the
service-hint drift test works under, and it covers every intent Phoenix mirrors.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import homeassistant
import pytest
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component

from custom_components.phoenix_mcp import const
from custom_components.phoenix_mcp.tool_defs import (
    _ENTITY_TOOL_DEFS,
    _NATIVE_TOOL_DEFS,
    _SYSTEM_TOOL_DEFS,
)

# The components registering the intents Phoenix MCP mirrors. Setting them up is
# what populates intent.async_get; there is no static table to read instead,
# because several handlers take their description as a constructor argument at
# registration time rather than carrying it as a class attribute.
_INTENT_COMPONENTS = (
    "homeassistant",
    "intent",
    "light",
    "fan",
    "climate",
    "media_player",
    "cover",
    "valve",
    "lock",
    "vacuum",
    "timer",
)

# Intents the installed HA registers and declares for an LLM platform that
# Phoenix MCP does not publish as a tool, each with the reason. Intents absent
# from HA's own LLM platform declarations are handled separately and need no
# entry here: mirroring that decision is not a Phoenix MCP choice.
NOT_PUBLISHED: dict[str, str] = {
    # HA exposes the timer family only when the voice device driving the request
    # supports timers. An MCP request carries no device context to make that
    # decision with, so the timer surface here is HassCancelAllTimers (which HA
    # publishes unconditionally) plus call_service on the timer domain.
    "HassStartTimer": "needs voice-device timer context",
    "HassCancelTimer": "needs voice-device timer context",
    "HassIncreaseTimer": "needs voice-device timer context",
    "HassDecreaseTimer": "needs voice-device timer context",
    "HassPauseTimer": "needs voice-device timer context",
    "HassUnpauseTimer": "needs voice-device timer context",
    "HassTimerStatus": "needs voice-device timer context",
}

# HassBroadcast's component cannot be set up here: assist_satellite pulls audio
# dependencies (tts, ffmpeg) that the test environment does not install. Its
# handler declares the description as a plain class attribute, so it is read off
# HA's source instead of the live registry, which checks the same string.
_SOURCE_VERIFIED: dict[str, tuple[str, str]] = {
    "HassBroadcast": ("assist_satellite/intent.py", "BroadcastIntentHandler"),
}


def _published_hass_tools() -> dict[str, str]:
    """Every Hass* tool Phoenix MCP publishes, mapped to its description.

    All three def lists are scanned: the Hass* names are not confined to
    _NATIVE_TOOL_DEFS (HassBroadcast is cap-gated and lives with the system
    tools), and a guard that read only one list would silently stop covering a
    tool that moved between them.
    """
    tools: dict[str, str] = {}
    for defs in (_NATIVE_TOOL_DEFS, _ENTITY_TOOL_DEFS, _SYSTEM_TOOL_DEFS):
        for tool_def in defs:
            if tool_def["name"].startswith("Hass"):
                tools[tool_def["name"]] = tool_def["description"]
    return tools


def _ha_llm_intents() -> set[str]:
    """Every intent the installed HA can expose through an LLM platform."""
    published = {intent.INTENT_BROADCAST}
    components_dir = Path(homeassistant.__file__).parent / "components"
    for component in _INTENT_COMPONENTS:
        if not (components_dir / component / "llm.py").is_file():
            continue
        module = importlib.import_module(f"homeassistant.components.{component}.llm")
        for attr in ("LLM_INTENTS", "TIMER_INTENTS"):
            published.update(getattr(module, attr, ()))
    assert published, "HA LLM intent declarations look empty"
    return published


def _description_from_ha_source(relative_path: str, class_name: str) -> str:
    """Read a handler's class-level `description` literal out of HA's source."""
    path = Path(homeassistant.__file__).parent / "components" / relative_path
    assert path.is_file(), f"HA core no longer ships {relative_path}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "description"
                    for t in stmt.targets
                )
                and isinstance(stmt.value, ast.Constant)
            ):
                return stmt.value.value
    raise AssertionError(f"no description literal on {class_name} in {relative_path}")


@pytest.fixture
async def ha_intents(hass) -> dict[str, str]:
    """Every registered intent_type mapped to its description."""
    for component in _INTENT_COMPONENTS:
        config = {} if component in ("homeassistant", "intent") else {component: {}}
        assert await async_setup_component(hass, component, config), (
            f"could not set up {component}; _INTENT_COMPONENTS may be stale"
        )
    await hass.async_block_till_done()
    live = {h.intent_type: (getattr(h, "description", None) or "") for h in intent.async_get(hass)}
    assert len(live) > 20, "intent registry looks empty; setup did not take effect"
    return live


async def test_every_published_hass_tool_matches_an_installed_ha_intent(ha_intents):
    # A tool whose intent HA renamed or dropped keeps being announced and keeps
    # failing at call time, with nothing upstream to say why.
    ours = _published_hass_tools()
    assert ours, "no Hass* tools found; the def lists cannot all be empty"
    unknown = [
        name
        for name in sorted(ours)
        if name not in ha_intents and name not in _SOURCE_VERIFIED
    ]
    assert not unknown, f"published Hass* tools with no matching installed HA intent: {unknown}"


async def test_every_published_description_matches_the_installed_ha_intent(ha_intents):
    # The whole point of freezing HA's wording is that it stays HA's wording.
    # Diverging on purpose is allowed, but it has to be a visible edit here.
    drifted = {
        name: {"ha": ha_intents[name], "phoenix": description}
        for name, description in sorted(_published_hass_tools().items())
        if name in ha_intents and ha_intents[name] != description
    }
    assert not drifted, f"description drift against installed HA: {drifted}"


async def test_source_verified_descriptions_match_ha_source():
    # Same check as above for the handler whose component cannot be set up here.
    ours = _published_hass_tools()
    for name, (relative_path, class_name) in _SOURCE_VERIFIED.items():
        assert name in ours, f"{name} is no longer published; drop its _SOURCE_VERIFIED entry"
        assert ours[name] == _description_from_ha_source(relative_path, class_name)


async def test_phoenix_publishes_nothing_ha_withholds_from_llm_apis(ha_intents):
    # HA 2026.8 moved intent publication from one AssistAPI ignore list to each
    # integration's llm.py platform. Publishing outside those declarations
    # should be a deliberate departure, not something that arrives by copying a
    # registered intent.
    outside_ha = sorted(set(_published_hass_tools()) - _ha_llm_intents())
    assert not outside_ha, f"publishing intents HA withholds from LLM APIs: {outside_ha}"


async def test_every_unpublished_ha_intent_has_a_written_reason(ha_intents):
    # The forward-looking half: an intent HA starts registering fails here until
    # somebody decides whether Phoenix MCP should publish it. Without this, a new
    # actuator intent is simply never noticed.
    ours = set(_published_hass_tools())
    ha_llm_intents = _ha_llm_intents()
    untriaged = sorted(
        name
        for name in ha_intents
        if name in ha_llm_intents and name not in ours and name not in NOT_PUBLISHED
    )
    assert not untriaged, (
        "installed HA registers intents Phoenix MCP neither publishes nor explains; "
        f"publish them or add a reason to NOT_PUBLISHED: {untriaged}"
    )


async def test_the_triage_list_has_no_stale_entries(ha_intents):
    # The reverse direction, so the list cannot rot into a record of intents that
    # no longer exist or that Phoenix MCP has since started publishing.
    ours = set(_published_hass_tools())
    assert all(reason.strip() for reason in NOT_PUBLISHED.values()), (
        "every NOT_PUBLISHED entry needs a reason"
    )
    gone = sorted(name for name in NOT_PUBLISHED if name not in ha_intents)
    assert not gone, f"NOT_PUBLISHED names intents installed HA no longer registers: {gone}"
    now_published = sorted(name for name in NOT_PUBLISHED if name in ours)
    assert not now_published, f"NOT_PUBLISHED names tools Phoenix MCP now publishes: {now_published}"


async def test_vacuum_feature_bits_match_the_installed_ha_enum():
    """const's vacuum bits are a mirror, so pin them to the enum they mirror.

    They are copied rather than imported so nothing in const pulls a component
    module into the import graph. A renumbered bit would silently target the
    wrong vacuums, or none at all, and no other test would notice: the tools
    would keep returning a well-formed action_done over an empty target list.
    """
    from homeassistant.components.vacuum import VacuumEntityFeature

    assert const.VACUUM_START_BIT == int(VacuumEntityFeature.START)
    assert const.VACUUM_RETURN_HOME_BIT == int(VacuumEntityFeature.RETURN_HOME)
    assert const.VACUUM_CLEAN_AREA_BIT == int(VacuumEntityFeature.CLEAN_AREA)
