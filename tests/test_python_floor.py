"""Guard: Phoenix MCP's own source must parse on its declared Python floor.

The floor is Python 3.12, and it is DERIVED rather than chosen: the oldest
supported Home Assistant release, 2024.5.0, declares `REQUIRED_PYTHON_VER
(3, 12, 0)`, so no install this integration runs on has an older interpreter.
Nothing else enforces it. The suite runs on a much newer interpreter, where
later syntax simply works, and a type checker cannot enforce it either: its
target-version setting applies when parsing DEPENDENCIES too, and Home Assistant
itself uses syntax newer than this floor, which aborts the run before it checks
anything of ours.

`ast.parse(..., feature_version=(3, 12))` is the one mechanism that checks only
our files, and it rejects exactly the constructs that matter (type parameter
defaults, unparenthesized except groups, t-strings) while accepting everything
3.12 allows (PEP 695 `type` statements and generic parameter lists, PEP 604
unions, match statements, ExceptionGroup syntax).

Scope is Phoenix's own package. The vendored mesa_core is a copy of a separate
project and is excluded. Tests and scripts are excluded too: they never run on
a user's HA install.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# Tracks the interpreter the oldest supported HA release requires. Raising it is
# a deliberate product decision that starts with raising the HA floor (it drops
# installs on older interpreters), never a fix for a failing test.
PYTHON_FLOOR = (3, 12)

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "phoenix_mcp"


def _phoenix_sources() -> list[pathlib.Path]:
    return sorted(p for p in _PACKAGE.rglob("*.py") if "mesa_core" not in p.parts)


def test_there_are_sources_to_check():
    """A path typo would make every parametrized case vanish and the file pass."""
    assert len(_phoenix_sources()) > 25


@pytest.mark.parametrize("path", _phoenix_sources(), ids=lambda p: p.name)
def test_source_parses_on_the_python_floor(path: pathlib.Path):
    floor = ".".join(str(part) for part in PYTHON_FLOOR)
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=PYTHON_FLOOR)
    except SyntaxError as err:
        pytest.fail(
            f"{path.name}:{err.lineno} uses syntax newer than Python {floor}: {err.msg}. "
            f"CI runs a much newer interpreter, so this would only fail on a user's "
            f"Home Assistant install."
        )


def test_the_guard_actually_rejects_newer_syntax(tmp_path):
    """Mutation check in-file: prove feature_version is doing the work.

    Without this, a future Python whose ast ignores feature_version (or a typo in
    the tuple) would turn the whole module into a no-op that still reports green.
    """
    newer = tmp_path / "newer.py"
    newer.write_text("type Alias[T = int] = list[T]\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        ast.parse(newer.read_text(), feature_version=PYTHON_FLOOR)

    generic = tmp_path / "generic.py"
    generic.write_text("def f[T = int](x: T) -> T: return x\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        ast.parse(generic.read_text(), feature_version=PYTHON_FLOOR)


def test_the_guard_accepts_syntax_the_floor_allows():
    """The complement: it must not reject what 3.12 genuinely supports."""
    for src in (
        "def f(x: int | None) -> None: ...\n",              # PEP 604, 3.10
        "def f(x):\n    match x:\n        case 1: return 2\n",  # match, 3.10
        "async def f() -> None:\n    async with open('x') as fh: ...\n",
        "type Alias = int\n",                               # PEP 695, 3.12
        "def f[T](x: T) -> T: return x\n",                  # PEP 695, 3.12
    ):
        ast.parse(src, feature_version=PYTHON_FLOOR)
