"""Guard: no test patches a name on mcp_view that the code under test no longer reads.

Moving a tool out of mcp_view.py into tools/ breaks three things, in increasing
order of how quietly they fail. An `import` fails at collection. A
`patch("...mcp_view.X")` for a name mcp_view no longer binds fails with
AttributeError. But a patch on a name mcp_view STILL binds, aimed at code that
now reads its own module's binding, fails silently: the patch applies to a name
nobody reads, the real function runs, and every "nothing happened" assertion
still passes.

That third case is not theoretical. A `patch("...mcp_view._gate")` site left
behind by a move keeps passing against a dead mock for every
`assert_not_called()` assertion it makes. This guard is what makes the next such
move announce itself.

The checker lives HERE rather than in a helper script, so it cannot go missing:
a guard that silently disappears is worse than one that never existed, because
the green suite still claims the property holds.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "custom_components" / "phoenix_mcp"


def _bound(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            out.add(node.targets[0].id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _defined(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            out.add(node.targets[0].id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _reads(tree: ast.AST) -> set[str]:
    out = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    out |= {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    return out


mcp_src = (PKG / "mcp_view.py").read_text()
mcp_binds = _bound(ast.parse(mcp_src))

# tool name -> owning module, read off mcp_view's _register_tool lines plus the
# import block that names where each handler came from.
handler_module: dict[str, str] = {}
imports: dict[str, str] = {}
for node in ast.parse(mcp_src).body:
    if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tools."):
        for a in node.names:
            imports[a.asname or a.name] = node.module.split(".", 1)[1]
for m in re.finditer(r'_register_tool\(\s*"([^"]+)"\s*,\s*(\w+)', mcp_src):
    if m.group(2) in imports:
        handler_module[m.group(1)] = imports[m.group(2)]

tools: dict[str, tuple[set[str], set[str]]] = {}
for p in sorted((PKG / "tools").glob("*.py")):
    if p.stem == "__init__":
        continue
    t = ast.parse(p.read_text())
    tools[p.stem] = (_reads(t), _defined(t))


def patch_targets(tree: ast.AST):
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        is_obj = isinstance(f, ast.Attribute) and f.attr in ("object", "setattr")
        is_str = (isinstance(f, ast.Name) and f.id == "patch") or (
            isinstance(f, ast.Attribute) and f.attr == "patch")
        if is_obj and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                and isinstance(n.args[1].value, str):
            yield ast.unparse(n.args[0]), n.args[1].value, n.lineno
        elif is_str and n.args and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str) and "." in n.args[0].value:
            path, _, attr = n.args[0].value.rpartition(".")
            yield path, attr, n.lineno


def find_stale_sites(test_root: pathlib.Path | None = None) -> tuple[list[str], int]:
    """Return (findings, sites_examined). A finding is a human-readable line."""
    findings: list[str] = []
    checked = 0
    for path in sorted((test_root or REPO / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text())
        funcs = sorted(
            (n.lineno, n.end_lineno, n) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for mod, attr, ln in patch_targets(tree):
            if not mod.endswith("mcp_view") or attr not in mcp_binds:
                continue
            checked += 1
            owner = next((n for s, e, n in funcs if s <= ln <= e), None)
            if owner is None:
                continue
            names = _reads(owner)
            strings = {c.value for c in ast.walk(owner)
                       if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            reachable = {handler_module[s] for s in strings if s in handler_module}
            for mname, (reads, defines) in tools.items():
                if attr not in reads:
                    continue
                if names & defines or mname in reachable:
                    why = "calls its symbols" if names & defines else "calls its tools by name"
                    findings.append(
                        f"{path}:{ln}: patch mcp_view.{attr} in {owner.name}() "
                        f"-- tools/{mname} reads it and this test {why}"
                    )
    return findings, checked


def test_no_stale_patch_targets() -> None:
    findings, checked = find_stale_sites(REPO / "tests")
    assert checked > 50, (
        f"only {checked} patch sites examined; the checker is not finding the "
        "tests it is supposed to scan"
    )
    assert not findings, "Patches that may no longer bite:\n" + "\n".join(findings)


def test_checker_reports_a_known_stale_patch(tmp_path: pathlib.Path) -> None:
    """The checker must actually catch the defect, not merely return an empty list.

    A checker that scans nothing reports zero findings exactly like a clean tree,
    so the guard is worthless until this passes. The fixture reproduces the real
    shape: a native-tool test that patches _gate on mcp_view, which both mcp_view
    and tools/native bind, while calling a symbol tools/native defines.
    """
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text(
        "from unittest.mock import patch\n"
        "from custom_components.phoenix_mcp.tools.native import _tool_hass_turn_on\n"
        "\n"
        "def test_x():\n"
        '    with patch("custom_components.phoenix_mcp.mcp_view._gate") as gate:\n'
        "        _tool_hass_turn_on({}, None, None, None)\n"
        "    gate.assert_not_called()\n"
    )
    ast.parse(fixture.read_text())  # the fixture itself must be valid Python

    findings, checked = find_stale_sites(tmp_path)
    assert checked == 1, f"the fixture's patch site was not even examined ({checked})"
    assert len(findings) == 1, f"checker missed the planted defect: {findings}"
    assert "_gate" in findings[0] and "tools/native" in findings[0]
