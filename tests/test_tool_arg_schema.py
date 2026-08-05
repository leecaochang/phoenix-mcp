"""Every tool argument the code READS must be one the tool PUBLISHES.

An argument a handler acts on but the `inputSchema` never declares is invisible
to the caller, and a client that validates against the schema cannot send it at
all: the omission is enforced on its side, which no amount of server leniency
reaches. That is the same class as the stale-catalog case in mcp_view, arriving
through a different door.

It shipped once. `set_yaml_config` grew a `remove_entries` argument, the removal
guard refused a write and told the agent to pass it, and the schema did not list
it, so a well-behaved agent was handed an instruction it could not follow and
fell back to asking the operator to edit the file by hand. Nothing caught it:
test_mcp_tool_catalog.py pins the registry against the def lists, not the
arguments against the schema, and every behavioural test passed the argument
directly rather than through a schema.

The walk is per MODULE rather than per tool, deliberately. Arguments are read
far from the handler that received them (`remove_entries` is read in
`_declared_entry_removals`, three calls down from `_tool_set_yaml_config`), so
attributing a read to one tool would need real dataflow analysis. The union of
what a module reads against the union of what its tools declare is coarser but
answers the question that matters: is this argument published ANYWHERE. That is
exactly the check the shipped defect would have failed.

Undeclared ALIASES are the one legitimate case and are exempted by name with a
reason. An alias is a spelling a model might guess, accepted quietly so a near
miss still works; it is the opposite of the defect above, where the tool names
an argument it does not publish.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from custom_components.phoenix_mcp.tool_defs import (
    _ENTITY_TOOL_DEFS,
    _NATIVE_TOOL_DEFS,
    _SYSTEM_TOOL_DEFS,
)

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "phoenix_mcp"

# (module filename, argument) -> why it is read but not published.
ALIAS_EXEMPTIONS = {
    ("discovery.py", "name"): (
        "search_entities accepts `name` as an alias for its published `query`, "
        "so a model that guesses the other spelling still searches instead of "
        "silently searching for nothing. Publishing both would invite a caller "
        "to send two conflicting queries."
    ),
    ("authoring.py", "entity_id"): (
        "get_automation_traces accepts `entity_id` as an alias for its published "
        "`automation_id`: an agent holding the automation's entity id has the "
        "harder value to convert, and both resolve to the same automation."
    ),
}


def _tool_defs() -> dict[str, dict]:
    return {d["name"]: d for d in (*_ENTITY_TOOL_DEFS, *_NATIVE_TOOL_DEFS, *_SYSTEM_TOOL_DEFS)}


def _modules() -> list[pathlib.Path]:
    return [PACKAGE / "mcp_view.py", *sorted((PACKAGE / "tools").glob("*.py"))]


def _registered_handlers() -> dict[str, str]:
    """tool name -> the handler function name, read off mcp_view's own registry.

    The registry is the source of truth rather than a list kept here, so a tool
    added tomorrow is covered without this file being touched.
    """
    tree = ast.parse((PACKAGE / "mcp_view.py").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_register_tool"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[1], ast.Name)
        ):
            found[node.args[0].value] = node.args[1].id
    return found


def _module_of_function() -> dict[str, pathlib.Path]:
    """Top-level function name -> the module defining it."""
    out: dict[str, pathlib.Path] = {}
    for path in _modules():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, path)
    return out


def _tools_by_module() -> dict[pathlib.Path, set[str]]:
    homes = _module_of_function()
    out: dict[pathlib.Path, set[str]] = {}
    for tool, handler in _registered_handlers().items():
        home = homes.get(handler)
        if home is not None:
            out.setdefault(home, set()).add(tool)
    return out


def _args_read(path: pathlib.Path) -> set[str]:
    """Every literal key the module reads off a dict named `args`.

    Covers both `args.get("x")` and `args["x"]`. A non-literal key cannot be
    checked and is skipped; none of the tool surface uses one today.
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "args"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


MODULES_WITH_TOOLS = sorted(_tools_by_module(), key=lambda p: p.name)


@pytest.mark.parametrize("module", MODULES_WITH_TOOLS, ids=lambda p: p.name)
def test_every_argument_read_is_published_by_some_tool(module):
    defs = _tool_defs()
    declared: set[str] = set()
    for tool in _tools_by_module()[module]:
        declared |= set(defs.get(tool, {}).get("inputSchema", {}).get("properties", {}))
    exempt = {arg for (name, arg) in ALIAS_EXEMPTIONS if name == module.name}
    undeclared = sorted(_args_read(module) - declared - exempt)
    assert not undeclared, (
        f"{module.name} reads tool arguments that no tool in it publishes: "
        f"{undeclared}. A caller cannot send an argument its schema does not "
        f"declare, so either add it to the tool's inputSchema in tool_defs.py, "
        f"or add it to ALIAS_EXEMPTIONS with the reason it stays unpublished."
    )


def test_the_walk_actually_finds_arguments():
    """A guard that stops matching passes silently, so pin that it still reads.

    If the AST shapes ever stop matching (a refactor to a helper that takes the
    dict under another name), every module would come back with an empty set and
    this suite would go green while checking nothing.
    """
    found = {m.name: _args_read(m) for m in MODULES_WITH_TOOLS}
    assert sum(len(v) for v in found.values()) > 100, found
    assert "content" in found["config_files.py"]
    assert "remove_entries" in found["config_files.py"], (
        "the argument whose omission this test exists for is no longer read; "
        "if it was removed on purpose, pick another live argument here"
    )


def test_exemptions_are_all_still_real():
    """An exemption for an argument nobody reads any more is dead weight that
    would quietly excuse a future argument of the same name."""
    for (name, arg), reason in ALIAS_EXEMPTIONS.items():
        module = next((m for m in MODULES_WITH_TOOLS if m.name == name), None)
        assert module is not None, f"ALIAS_EXEMPTIONS names a module that has no tools: {name}"
        assert arg in _args_read(module), f"{name} no longer reads {arg!r}; drop the exemption"
        assert len(reason) > 40, f"{name}/{arg} needs a written reason, not a placeholder"
