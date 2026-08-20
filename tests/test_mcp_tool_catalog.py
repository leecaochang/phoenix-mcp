"""Invariants over the MCP tool catalog itself.

The tool defs are hand-maintained lists, and their MCP annotations are a separate
hand-maintained map. A tool added without an annotation, or one whose annotation
contradicts how the tool is actually gated (a "read-only" tool sitting in the
executor registry), misinforms every client that honors the hints. Assert the
relationships directly so the drift fails loudly.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import agentcli, const, mcp_view, mesa_tools
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
)

_HINT_KEYS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
# restart_ha is not named delete_*/remove_* but is the most destructive tool here.
_DESTRUCTIVE_BY_NAME_EXTRA = {"restart_ha"}
_OPEN_WORLD_EXPECTED = {"list_blueprints", "get_blueprint"}


def _static_defs() -> list[dict]:
    return [
        *mcp_view._ENTITY_TOOL_DEFS,
        *mcp_view._NATIVE_TOOL_DEFS,
        *mcp_view._SYSTEM_TOOL_DEFS,
    ]


def _all_defs() -> list[dict]:
    return [*_static_defs(), *mesa_tools.mesa_tool_defs()]


def _by_name() -> dict[str, dict]:
    return {d["name"]: d for d in _all_defs()}


def test_every_tool_carries_exactly_the_four_hints():
    missing = [d["name"] for d in _all_defs() if "annotations" not in d]
    assert not missing, f"tools with no MCP annotations: {sorted(missing)}"
    for d in _all_defs():
        assert set(d["annotations"]) == _HINT_KEYS, (
            f"{d['name']} annotations keys are {sorted(d['annotations'])}, "
            f"expected exactly {sorted(_HINT_KEYS)}"
        )
        for key, value in d["annotations"].items():
            assert isinstance(value, bool), f"{d['name']}.{key} is {value!r}, not a bool"


def test_annotation_map_matches_the_def_lists_both_ways():
    mapped = set(mcp_view._TOOL_ANNOTATIONS)
    defined = {d["name"] for d in _static_defs()}
    assert defined - mapped == set(), (
        f"tools with no entry in _TOOL_ANNOTATIONS: {sorted(defined - mapped)}"
    )
    assert mapped - defined == set(), (
        f"stale _TOOL_ANNOTATIONS entries for tools that no longer exist: "
        f"{sorted(mapped - defined)}"
    )


def test_executor_registry_tools_are_not_read_only():
    # Anything reachable from the approval executor registry has a side effect by
    # construction: it is the function an admin's Approve re-runs.
    defs = _by_name()
    offenders = [
        name for name in mcp_view._EXECUTOR_REGISTRY
        if name in defs and defs[name]["annotations"]["readOnlyHint"]
    ]
    assert not offenders, f"executor-backed tools marked readOnlyHint true: {sorted(offenders)}"


def test_write_gated_tools_are_not_read_only():
    defs = _by_name()
    offenders = [
        name for name in mcp_view._WRITE_GATED_TOOLS
        if defs[name]["annotations"]["readOnlyHint"]
    ]
    assert not offenders, f"write-gated tools marked readOnlyHint true: {sorted(offenders)}"


def test_read_only_tools_are_in_neither_write_set():
    # The reverse direction: a tool claiming readOnlyHint must not be gated as a
    # write anywhere, or the annotation is lying to the client.
    read_only = {d["name"] for d in _all_defs() if d["annotations"]["readOnlyHint"]}
    assert not (read_only & set(mcp_view._EXECUTOR_REGISTRY))
    assert not (read_only & mcp_view._WRITE_GATED_TOOLS)


def test_destroying_tools_are_marked_destructive():
    offenders = [
        d["name"] for d in _all_defs()
        if (d["name"].startswith(("delete_", "remove_")) or d["name"] in _DESTRUCTIVE_BY_NAME_EXTRA)
        and not d["annotations"]["destructiveHint"]
    ]
    assert not offenders, f"destroying tools not marked destructiveHint: {sorted(offenders)}"


def test_read_only_tools_are_never_destructive():
    offenders = [
        d["name"] for d in _all_defs()
        if d["annotations"]["readOnlyHint"] and d["annotations"]["destructiveHint"]
    ]
    assert not offenders, f"tools claiming both readOnly and destructive: {sorted(offenders)}"


def test_open_world_is_exactly_the_blueprint_tools():
    open_world = {d["name"] for d in _all_defs() if d["annotations"]["openWorldHint"]}
    assert open_world == _OPEN_WORLD_EXPECTED, (
        "openWorldHint means the payload carries externally-authored content. "
        f"Expected {sorted(_OPEN_WORLD_EXPECTED)}, got {sorted(open_world)}"
    )


@pytest.mark.asyncio
async def test_tools_list_exposes_annotations_and_still_strips_cap():
    from tests.test_mcp_view import _make_data, _make_hass, _make_token

    token, _ = _make_token()
    token.announce_all_tools = True
    data = _make_data(token)
    res, _m, _r, _o = await mcp_view._dispatch_mcp(
        "tools/list", 1, {}, token, _make_hass(data), data, "127.0.0.1",
        base_url="http://homeassistant.local",
    )
    tools = res["result"]["tools"]
    assert tools, "expected a non-empty catalog"
    for tool in tools:
        assert "cap" not in tool, f"{tool['name']} leaked its gating cap to the client"
        assert "caps" not in tool, f"{tool['name']} leaked its gating caps to the client"
        assert "caps_any" not in tool, f"{tool['name']} leaked its any-of gating caps to the client"
        assert "requires" not in tool, f"{tool['name']} leaked its availability key to the client"
        assert set(tool["annotations"]) == _HINT_KEYS


def test_catalog_payload_metrics_cover_representative_profiles_and_provider_wires():
    """Pin deterministic payload bytes for read, write, and announce-all tokens."""
    from tests.test_mcp_view import _make_data, _make_token

    read_only, _ = _make_token(cap_config_read="allow", cap_log_read="allow")
    read_only.cap_search = "allow"
    read_only.cap_registry_read = "allow"
    read_only.cap_diagnostics = "allow"

    write_capable, _ = _make_token(
        cap_config_read="allow", cap_log_read="allow", cap_automation_write="allow"
    )
    write_capable.cap_search = "allow"
    write_capable.cap_registry_read = "allow"
    write_capable.cap_diagnostics = "allow"
    write_capable.cap_helper_write = "allow"
    write_capable.cap_lovelace_write = "confirm"
    write_capable.cap_registry_write = "allow"
    write_capable.permissions = PermissionTree(
        domains={"light": PermissionNode(state="GREEN")}
    )

    announce_all, _ = _make_token()
    announce_all.announce_all_tools = True
    profiles = {
        "read_only": read_only,
        "write_capable": write_capable,
        "announce_all": announce_all,
    }
    metrics = {
        name: agentcli.catalog_payload_metrics(token, _make_data(token))
        for name, token in profiles.items()
    }
    assert metrics == {
        "read_only": {
            "tool_count": 47,
            "canonical_bytes": 37879,
            "claude_bytes": 33040,
            "openai_bytes": 34403,
        },
        "write_capable": {
            "tool_count": 91,
            "canonical_bytes": 83524,
            "claude_bytes": 74140,
            "openai_bytes": 76779,
        },
            "announce_all": {
                "tool_count": 144,
                "canonical_bytes": 134258,
                "claude_bytes": 119396,
                "openai_bytes": 123572,
            },
    }
    assert metrics["announce_all"]["tool_count"] == len(_static_defs()) == 144
    assert mcp_view.tool_catalog_counts()["total"] == 150


def test_every_public_parameter_has_a_description():
    """Every property at every schema depth must explain its public meaning."""
    missing: list[str] = []

    def walk(tool: str, schema: dict, path: str = "") -> None:
        for name, child in schema.get("properties", {}).items():
            current = f"{path}.{name}" if path else name
            if not isinstance(child, dict) or not str(child.get("description", "")).strip():
                missing.append(f"{tool}.{current}")
            if not isinstance(child, dict):
                continue
            walk(tool, child, current)
            for keyword in ("oneOf", "anyOf", "allOf"):
                for index, branch in enumerate(child.get(keyword, [])):
                    if isinstance(branch, dict):
                        walk(tool, branch, f"{current}.{keyword}[{index}]")

    for definition in _all_defs():
        walk(definition["name"], definition["inputSchema"])
    assert not missing, f"public parameters without descriptions: {missing}"


def test_every_tool_capability_name_is_known():
    """A typo in either the single-cap or dual-cap form must fail closed in CI."""
    used = {
        cap
        for definition in _all_defs()
        for cap in mcp_view._tool_caps(definition)
    }
    assert used <= set(const.CAPABILITY_NAMES)


def test_requires_key_values_are_known():
    """A typo in a "requires" value would silently announce a tool unconditionally."""
    values = {d["requires"] for d in _all_defs() if "requires" in d}
    assert values <= {"esphome", "esphome_builder"}, f"unknown requires values: {sorted(values)}"


@pytest.mark.parametrize(
    ("integration", "builder"),
    [(False, False), (True, False), (True, True)],
)
@pytest.mark.asyncio
async def test_announcement_mirrors_agree_across_esphome_availability(integration, builder):
    """tools/list, the Agent Chat tool list, and the gate map must never disagree.

    The three are separate implementations of the same rule, so a change to one
    can silently diverge: an agent would be handed a tool that get_capability_summary
    calls unavailable, or vice versa.
    """
    from unittest.mock import patch

    from custom_components.phoenix_mcp import agentcli
    from tests.test_mcp_view import _make_data, _make_hass, _make_token

    token, _ = _make_token()
    # Grant every cap the ESPHome tools gate on, so this test varies only the
    # availability dimension it is about.
    token.cap_esphome_yaml = "allow"
    token.cap_esphome_flash = "allow"
    token.cap_diagnostics = "allow"
    data = _make_data(token)
    hass = _make_hass(data)
    hass.config.components = {"esphome"} if integration else set()
    data.hass = hass
    dashboard = object() if builder else None

    with patch("custom_components.phoenix_mcp.tools.esphome._esphome_dashboard", return_value=dashboard), \
            patch.object(hass.config_entries, "async_entries", return_value=[]):
        res, _m, _r, _o = await mcp_view._dispatch_mcp(
            "tools/list", 1, {}, token, hass, data, "127.0.0.1",
            base_url="http://homeassistant.local",
        )
        announced = {t["name"] for t in res["result"]["tools"]}
        agent_names = {t["name"] for t in agentcli.build_mcp_tool_list(token, data)}
        gate_map = mcp_view._tool_gate_map(token, data, hass)

    assert announced == agent_names, "tools/list and the Agent Chat list diverged"

    builder_tools = {d["name"] for d in _all_defs() if d.get("requires") == "esphome_builder"}
    esphome_tools = {d["name"] for d in _all_defs() if d.get("requires") == "esphome"}

    if builder:
        assert builder_tools <= announced
    else:
        assert not (builder_tools & announced)
        assert builder_tools <= set(gate_map["unavailable"])
        for name in builder_tools:
            assert "Device Builder" in gate_map["unavailable_reasons"][name]

    if integration or builder:
        assert esphome_tools <= announced
    else:
        assert not (esphome_tools & announced)
        assert esphome_tools <= set(gate_map["unavailable"])

    bucketed = set(gate_map["usable"]) | set(gate_map["needs_approval"])
    assert not (bucketed & (builder_tools if not builder else set())), (
        "a hidden tool must never be bucketed as usable"
    )


# ---------------------------------------------------------------------------
# Dispatch registry invariants.
#
# _call_tool used to be a 127-branch if/elif chain, so "every published tool is
# actually reachable" and "nothing unreachable is published" were guaranteed
# only by whoever last edited it. The registry is a dict, so both directions can
# simply be asserted.
# ---------------------------------------------------------------------------


def test_dispatch_registry_matches_the_def_lists_both_ways():
    published = set(_by_name())
    registered = set(mcp_view._TOOL_HANDLERS)
    assert published - registered == set(), "published but not dispatchable"
    assert registered - published == set(), "dispatchable but not published"


def test_dispatch_registry_covers_the_reported_tool_count():
    # tool_catalog_counts is the single source of truth for the tool count
    # (docs, the admin info endpoint, scripts/count_tools.py all read it).
    counts = mcp_view.tool_catalog_counts()
    assert len(mcp_view._TOOL_HANDLERS) == counts["total"]


def test_every_registered_handler_is_a_coroutine_function():
    # CO_COROUTINE off the code object, matching how _register_tool reads arity:
    # asyncio.iscoroutinefunction is deprecated, and inspect is off-limits here.
    co_coroutine = 0x80
    bad = [
        name for name, (handler, _w, _b) in mcp_view._TOOL_HANDLERS.items()
        if not handler.__code__.co_flags & co_coroutine
    ]
    assert bad == []


def test_registered_context_params_are_real_handler_parameters():
    """_register_tool infers context from the code object; keep that honest.

    A handler that names a context param it does not actually accept, or a
    binding whose kwarg the handler cannot take, fails at call time on a tool
    nobody may exercise until production.
    """
    for name, (handler, wanted, bound) in mcp_view._TOOL_HANDLERS.items():
        code = handler.__code__
        accepted = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
        assert set(wanted) <= accepted, name
        assert set(bound) <= accepted, name
        # args/token/hass are passed positionally by _call_tool.
        assert code.co_argcount >= 3, name


def test_duplicate_registration_is_refused():
    with pytest.raises(ValueError, match="duplicate tool registration"):
        mcp_view._register_tool("get_state", mcp_view._tool_get_state)


# The five targeting parameters the native Hass* tools share. They are the only
# way those tools select what to act on, and getting one wrong is not reported as
# a bad argument: an unusable value is coerced away, and a call left with no
# usable selector resolves to nothing and returns the same refusal as a denial.
_NATIVE_TARGET_PARAMS = ("name", "area", "floor", "domain", "device_class")

# (tool, param) pairs that share a targeting parameter's NAME while meaning
# something else, so they carry their own wording on purpose. HassVacuumCleanArea
# is the only one: its `area` is the room the vacuum is sent to clean, passed to
# the service as cleaning_area_id rather than scoping which entities are acted
# on, and its `name` is optional (omitting it uses every capable vacuum), so the
# at-least-one-selector rule in the shared text would be wrong there.
_NOT_A_TARGETING_SELECTOR = {
    ("HassVacuumCleanArea", "area"),
    ("HassVacuumCleanArea", "name"),
}


def test_native_targeting_params_are_documented():
    """A native tool must not ship a bare targeting selector.

    HA's intent slot schemas carry a type per field and no prose, so a def copied
    from one starts with nothing telling a model what shape the field wants or
    that at least one selector is needed. That is exactly the gap a wrong-shaped
    argument falls through, so the descriptions are required rather than nice to
    have, and this fails on a new tool that forgets them.
    """
    undocumented = [
        (tool_def["name"], param)
        for tool_def in mcp_view._NATIVE_TOOL_DEFS
        for param, schema in tool_def["inputSchema"].get("properties", {}).items()
        if param in _NATIVE_TARGET_PARAMS and not schema.get("description", "").strip()
    ]
    assert not undocumented, f"native targeting params with no description: {undocumented}"


def test_native_targeting_descriptions_have_one_definition_each():
    """One wording per parameter, so 16 copies cannot drift apart.

    The descriptions are shared module constants; this asserts the result rather
    than the mechanism, so it still holds if they are ever inlined. domain is
    exempt: its enum-constrained variant deliberately drops the worked example,
    which would otherwise name a domain the enum does not accept.
    """
    for param in ("name", "area", "floor", "device_class"):
        wordings = {
            tool_def["inputSchema"]["properties"][param]["description"]
            for tool_def in mcp_view._NATIVE_TOOL_DEFS
            if param in tool_def["inputSchema"].get("properties", {})
            and (tool_def["name"], param) not in _NOT_A_TARGETING_SELECTOR
        }
        assert len(wordings) <= 1, f"{param} has {len(wordings)} different descriptions"


def test_the_not_a_targeting_selector_exemptions_are_all_live():
    """The exemption list must not outlive the tools or parameters it names.

    A stale entry silently re-opens the drift it was written to allow: the shared
    wording could diverge on a tool nobody is exempting any more.
    """
    by_name = {t["name"]: t for t in mcp_view._NATIVE_TOOL_DEFS}
    for tool_name, param in _NOT_A_TARGETING_SELECTOR:
        assert tool_name in by_name, f"exemption names a tool that no longer exists: {tool_name}"
        props = by_name[tool_name]["inputSchema"].get("properties", {})
        assert param in props, f"exemption names a parameter {tool_name} no longer has: {param}"


def test_every_published_parameter_declares_a_type():
    """No tool parameter may be left without a declared type.

    NOT a style rule. A property with no "type" is not read as "any" by every
    client: live-found on patch_dashboard's `value`, a real MCP client serialized
    an object argument into a JSON STRING because the schema declared nothing for
    it. For a tool that writes structured config that is silent corruption, since
    a string lands where a mapping belongs and the write reports success.

    An enum or a oneOf/anyOf/allOf/$ref counts as a declaration; those constrain
    the value just as well. A parameter that genuinely accepts any JSON must SAY
    so with a type union rather than by omission, which is what patch_dashboard's
    `value` now does.
    """
    untyped = [
        f"{d['name']}.{param}"
        for d in _all_defs()
        for param, spec in (d.get("inputSchema", {}).get("properties") or {}).items()
        if isinstance(spec, dict)
        and not any(k in spec for k in ("type", "enum", "oneOf", "anyOf", "allOf", "$ref"))
    ]
    assert untyped == [], f"parameters with no declared type: {untyped}"


def _handler_calls_gate(fn) -> bool:
    """True when a handler calls a capability gate or its centralized wrapper."""
    for node in ast.walk(ast.parse(inspect.getsource(fn))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in ("_gate", "_integration_gate", "_pending_or_inline"):
                return True
    return False


def test_executor_registry_is_an_exact_proxy_for_gating():
    """For a cap-tied tool, "registers an executor" must mean "actually gates".

    _tool_gate_map buckets a Confirm-cap tool as needs_approval only when it is in
    _EXECUTOR_REGISTRY, because a cap-tied READ never calls _gate and would
    otherwise be reported as queueing for approval when it runs directly. That
    substitution is only sound while the two sets agree, so this pins them BOTH
    ways: a gating tool with no executor could never be applied after approval,
    and a non-gating tool with one would put the misreport straight back.

    Scoped to cap-tied tools on purpose. HassTurnOn/HassTurnOff gate through a
    helper rather than in their own body, and call_service_mesa_approved is the
    MESA sentinel executor that _call_tool can never dispatch; none of the three
    is cap-tied, so none reaches the branch this protects.
    """
    capped = {d["name"] for d in _all_defs() if mcp_view._tool_caps(d)}
    executors = set(mcp_view._EXECUTOR_REGISTRY)
    gating = {
        name for name, (fn, _pos, _kw) in mcp_view._TOOL_HANDLERS.items()
        if _handler_calls_gate(fn)
    }
    assert sorted(capped & executors) == sorted(capped & gating)


def test_cap_tied_reads_are_not_reported_as_needing_approval():
    """The concrete regression: a Confirm cap must not gate a read.

    Asserts the behaviour rather than the predicate, so a future refactor that
    keeps the sets aligned but changes the bucketing still fails here.
    """
    token = TokenRecord(
        id="t1", name="t", token_hash="x", created_at=utcnow(), created_by="u",
        permissions=PermissionTree(domains={"light": PermissionNode(state="GREEN")}),
        **{cap: "confirm" for cap in const.CAPABILITY_NAMES},
    )
    gate_map = mcp_view._tool_gate_map(token, SimpleNamespace(mesa=None), None)
    for read_tool in ("get_automation", "get_script", "get_dashboard_config", "get_yaml_config"):
        assert read_tool not in gate_map["needs_approval"], read_tool
        assert read_tool in gate_map["usable"], read_tool
    # The matching WRITES, on the same caps, must still be reported as gating.
    for write_tool in ("edit_automation", "edit_script", "set_dashboard_config", "patch_dashboard"):
        assert write_tool in gate_map["needs_approval"], write_tool


def test_list_integrations_is_visible_with_either_integration_capability():
    """The shared discovery read uses OR semantics, never accidental dual-cap AND."""
    base = dict(
        id="t1", name="t", token_hash="x", created_at=utcnow(), created_by="u",
        permissions=PermissionTree(),
    )
    definition = next(d for d in _all_defs() if d["name"] == "list_integrations")
    for granted in ("cap_integration_write", "cap_integration_reconfigure"):
        token = TokenRecord(**base, **{granted: "allow"})
        assert mcp_view._tool_is_announced(definition, token, False, None)
    denied = TokenRecord(**base)
    assert not mcp_view._tool_is_announced(definition, denied, False, None)
