"""Invariants between hand-maintained security constants in const.py.

These constants are edited by humans and must stay mutually consistent; a drift
between them is a silent security gap (e.g. a physical service gated in
call_service but not in the native Hass* tool path). Assert the relationships
directly so an edit to one without the other fails loudly.
"""

from __future__ import annotations

import ast
import pathlib

import homeassistant.components.system_log

from custom_components.phoenix_mcp.const import (
    LOG_LEVELS,
    PHYSICAL_GATE_DOMAINS,
    PHYSICAL_GATE_SERVICES,
    YAML_PROTECTED_SUBTREES,
)
from custom_components.phoenix_mcp.helpers import _LOG_LEVEL_RANK


def test_physical_gate_domains_cover_every_service_domain():
    # Both the call_service gate (policy_engine.call_needs_physical_gate) and the
    # native Hass* tools now gate on PHYSICAL_GATE_DOMAINS. PHYSICAL_GATE_SERVICES
    # remains the documented catalog of the actuation services on those domains;
    # every one of its domains MUST be in PHYSICAL_GATE_DOMAINS or that service
    # would slip both gates. (Gating the whole domain is what closes the
    # cover.toggle / *_cover_tilt / valve.toggle / alarm_arm_custom_bypass holes
    # an exact service-name list missed.)
    service_domains = {s.split("/")[0] for s in PHYSICAL_GATE_SERVICES}
    assert service_domains <= PHYSICAL_GATE_DOMAINS, (
        f"domains gated via services but not via PHYSICAL_GATE_DOMAINS: "
        f"{sorted(service_domains - PHYSICAL_GATE_DOMAINS)}"
    )


def test_physical_gate_domains_has_no_orphans():
    # The reverse: every gate domain should correspond to at least one gated
    # service, or the native-tool filter blocks a domain call_service allows.
    service_domains = {s.split("/")[0] for s in PHYSICAL_GATE_SERVICES}
    assert PHYSICAL_GATE_DOMAINS <= service_domains, (
        f"domains in PHYSICAL_GATE_DOMAINS with no gated service: "
        f"{sorted(PHYSICAL_GATE_DOMAINS - service_domains)}"
    )


def _system_log_handler_level() -> str:
    """The level Home Assistant attaches its system_log handler at, read from HA.

    Read off the SOURCE rather than asserted from memory: this is the fact that
    decides which levels Phoenix may offer, and if HA ever lowers it the answer
    changes. Running async_setup to inspect the live handler would drag the whole
    component in for one constant, so the call is located by AST instead, the way
    the native-intent parity guard reads HassBroadcast's description.
    """
    source = pathlib.Path(homeassistant.components.system_log.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setLevel"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Attribute)
        ):
            return node.args[0].attr
    raise AssertionError(
        "no handler.setLevel(logging.X) call found in HA's system_log; the "
        "assumption behind const.LOG_LEVELS can no longer be verified"
    )


def test_log_levels_match_what_home_assistant_actually_collects():
    # get_logs reads hass.data["system_log"].records, and HA attaches that handler
    # at WARNING, so an INFO record never enters the store. Offering a level the
    # store never holds is a promise it cannot keep, and the old silent coercion
    # to WARNING made the gap invisible: a caller asking for INFO got warnings back
    # and read the quiet result as "nothing was logged". If HA ever lowers this,
    # the fix is to WIDEN LOG_LEVELS, not to change the assertion.
    assert _system_log_handler_level() == "WARNING"
    assert LOG_LEVELS == ("WARNING", "ERROR")


def test_log_levels_are_a_subset_of_the_record_ranks():
    # The two sets are deliberately different sizes (_LOG_LEVEL_RANK classifies a
    # RECORD's level, LOG_LEVELS is what a CALLER may ask for), but every offered
    # level must rank or collect_log_entries would fall back to its WARNING
    # default and silently answer a different question than the caller asked.
    assert set(LOG_LEVELS) <= set(_LOG_LEVEL_RANK)


def test_yaml_protected_subtrees_shape():
    # set_yaml_config refuses a change to any of these paths, so a malformed entry
    # (a bare string instead of a tuple, an empty child list) would silently
    # protect nothing. The check is cheap; the failure mode is not.
    assert YAML_PROTECTED_SUBTREES, "the deny floor must not be empty"
    for top, children in YAML_PROTECTED_SUBTREES.items():
        assert isinstance(top, str) and top, f"bad top-level key: {top!r}"
        assert isinstance(children, tuple) and children, (
            f"{top} must map to a non-empty tuple of child keys, got {children!r}"
        )
        for child in children:
            assert isinstance(child, str) and child, f"bad child key under {top}: {child!r}"
