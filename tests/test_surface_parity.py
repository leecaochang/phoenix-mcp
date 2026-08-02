"""Parity between the two service-call surfaces: REST proxy and MCP call_service.

Phoenix exposes the same capability through two front doors. They are separate
implementations of one policy, so they can drift silently: nothing fails, the
same request just answers differently depending on which door the agent used.

The shape to watch, for NO_TARGET_SERVICES (the config-reload family): mcp_view
surfaces a ServiceValidationError as invalid_request WITH the message, and
proxy_view must do the same rather than returning a generic 403. The message is
safe because the call is post-cap_yaml_edit and describes the caller's own
reloadable config, and that reasoning applies equally to both surfaces, so a
stricter REST is accidental rather than deliberate. Nothing but a test catches
it: it is only visible by reading the two error-handling blocks side by side.

These tests pin the shared decisions so the next divergence fails loudly. They
deliberately assert on the DISPATCH FLAGS rather than only on end-to-end
behaviour: the flags are the single place the two surfaces encode the same
policy, so comparing them catches a drift even for a family that has no
end-to-end test yet.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "phoenix_mcp"

# The three service families that take NO entity target. Both surfaces route
# each one through their own _dispatch_no_target_* helper.
_FAMILIES = ("dual_gate", "no_target", "esphome")


def _dispatch_flags(module: str, helper: str) -> dict[str, dict[str, object]]:
    """Extract each call's keyword flags from a surface's dispatch helper calls.

    Read from the AST rather than by running the views: the point is to compare
    what the two modules DECIDE, and a family with no end-to-end test would
    otherwise be invisible here.
    """
    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    calls: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != helper:
            continue
        flags: dict[str, object] = {}
        for kw in node.keywords:
            if kw.arg in ("timeout_noun", "surface_validation_errors"):
                flags[kw.arg] = ast.literal_eval(kw.value)
        if flags:
            calls.append(flags)
    assert len(calls) == len(_FAMILIES), (
        f"{module}: expected {len(_FAMILIES)} {helper} call sites, found {len(calls)}. "
        f"If a family was added or removed, update _FAMILIES and the parity table."
    )
    return dict(zip(_FAMILIES, calls))


def _rest_flags() -> dict[str, dict[str, object]]:
    return _dispatch_flags("proxy_view.py", "_dispatch_no_target_call")


def _mcp_flags() -> dict[str, dict[str, object]]:
    return _dispatch_flags("mcp_view.py", "_dispatch_no_target_tool_call")


@pytest.mark.parametrize("family", _FAMILIES)
def test_both_surfaces_surface_validation_errors_identically(family):
    """The exact divergence that shipped: do not let it come back.

    A mismatch means the same service call with the same bad argument reports
    differently on REST than on MCP.
    """
    rest = _rest_flags()[family]["surface_validation_errors"]
    mcp = _mcp_flags()[family]["surface_validation_errors"]
    assert rest == mcp, (
        f"{family}: REST surface_validation_errors={rest} but MCP={mcp}. "
        f"The two surfaces must agree on whether a validation error's message "
        f"reaches the caller; differing means the same request answers "
        f"differently depending on which door was used."
    )


@pytest.mark.parametrize("family", _FAMILIES)
def test_both_surfaces_use_the_same_timeout_noun(family):
    """A slow call reports success with partial=True on both; same wording."""
    rest = _rest_flags()[family]["timeout_noun"]
    mcp = _mcp_flags()[family]["timeout_noun"]
    assert rest == mcp, f"{family}: REST timeout_noun={rest!r} but MCP={mcp!r}"


def test_the_expected_policy_is_what_is_actually_encoded():
    """Pin the values themselves, not just that the two agree.

    Without this, both surfaces flipping together would still pass the parity
    tests above while silently changing what a caller is told.

    dual_gate stays False: homeassistant/restart and /stop take no caller
    arguments worth reporting, and a message there would describe a service the
    token cannot otherwise probe. The other two are post-authorization and
    describe the caller's own input, so their messages are safe and useful.
    """
    expected = {
        "dual_gate": False,
        "no_target": True,
        "esphome": True,
    }
    for family, want in expected.items():
        assert _rest_flags()[family]["surface_validation_errors"] is want, family
        assert _mcp_flags()[family]["surface_validation_errors"] is want, family


def test_the_parity_check_would_notice_a_divergence():
    """Mutation check in-file: prove the comparison is not vacuous.

    If the extraction were reshaped so the flags stopped appearing as literal
    keywords, every test above would silently compare empty dicts.
    """
    rest, mcp = _rest_flags(), _mcp_flags()
    assert set(rest) == set(mcp) == set(_FAMILIES)
    for family in _FAMILIES:
        assert "surface_validation_errors" in rest[family], family
        assert "timeout_noun" in rest[family], family
        assert "surface_validation_errors" in mcp[family], family
        assert "timeout_noun" in mcp[family], family
