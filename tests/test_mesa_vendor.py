"""Smoke tests for the vendored mesa-core package.

These guard the vendoring mechanics: the copy must import cleanly under its
rewritten path and must NOT pull in a top-level
``mesa_core`` module (which would indicate an unrewritten absolute import that
could collide with a future PyPI install).
"""

from __future__ import annotations

import importlib
import sys


def test_vendored_package_imports_with_pinned_version():
    from custom_components.phoenix_mcp import mesa_core

    assert mesa_core.__version__ == "1.3.0"


def test_no_top_level_mesa_core_leak():
    # Importing the vendored package must not register a bare ``mesa_core``
    # module: every internal import is rewritten to the vendored prefix.
    importlib.import_module("custom_components.phoenix_mcp.mesa_core")

    assert "mesa_core" not in sys.modules


def test_core_public_api_round_trips():
    from custom_components.phoenix_mcp.mesa_core import ControlMode, SemanticProfile

    profile = SemanticProfile.from_dict(
        "light.kitchen",
        {
            "semantic_profile": {
                "semantic_tags": ["lighting.ambient"],
                "operational_boundaries": {"control_mode": "autonomous"},
            },
            "privacy_classification": {"level": "normal"},
        },
    )
    assert profile.entity_id == "light.kitchen"
    assert profile.operational_boundaries.control_mode is ControlMode.AUTONOMOUS

    restored = SemanticProfile.from_dict("light.kitchen", profile.to_dict())
    assert restored.operational_boundaries.control_mode is ControlMode.AUTONOMOUS


def test_mcp_tool_registry_available():
    # The four retrieval tools register through the dict adapter that Phoenix MCP reuses.
    from custom_components.phoenix_mcp.mesa_core.backends import MemoryBackend
    from custom_components.phoenix_mcp.mesa_core.mcp.adapters import DictToolRegistry
    from custom_components.phoenix_mcp.mesa_core.mcp.tools import register_mesa_tools
    from custom_components.phoenix_mcp.mesa_core.store import ProfileStore

    registry = DictToolRegistry()
    register_mesa_tools(ProfileStore(backend=MemoryBackend()), adapter=registry)
    assert "mesa_query_profiles" in registry.tools
    assert "mesa_get_profile" in registry.tools


# --- the shipped copy matches what the sync produced --------------------------


def _sync_module():
    """Load scripts/sync_mesa_core.py without importing it as a package."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_mesa_core.py"
    spec = importlib.util.spec_from_file_location("_sync_mesa_core_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendored_tree_matches_its_manifest():
    """A hand-edit of the vendored copy must fail, not ship.

    The project forbids editing this directory, and nothing else would notice:
    the copy is ordinary Python that imports fine and passes every other test
    whatever it says, and the version string it reports is just a literal in a
    file an editor could change too. The existing tests here check the vendoring
    MECHANICS (the import path, no top-level leak), which an edit leaves intact.

    Deliberately NOT a comparison against the upstream source: mesa-core is a
    separate read-only project reached through a symlink that CI does not have,
    so a checkout has to be able to answer this on its own. The manifest is the
    tracked record of what the sync produced; _VENDORED.txt cannot be, since it
    carries a local absolute path and a sync date.
    """
    import json
    import pathlib

    sync = _sync_module()
    manifest_path = pathlib.Path(sync.MANIFEST)
    assert manifest_path.exists(), (
        "tests/contract/mesa_core_vendor.json is missing. Run "
        "scripts/sync_mesa_core.py to regenerate it."
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    combined, per_file = sync._tree_digest()

    recorded = manifest.get("files", {})
    changed = sorted(k for k in per_file.keys() & recorded.keys() if per_file[k] != recorded[k])
    added = sorted(per_file.keys() - recorded.keys())
    removed = sorted(recorded.keys() - per_file.keys())
    assert combined == manifest.get("tree_sha256"), (
        "The vendored mesa-core tree does not match its manifest.\n"
        f"  changed: {changed}\n  added: {added}\n  removed: {removed}\n"
        "If this was a re-sync, re-run scripts/sync_mesa_core.py. If it was not, "
        "the vendored copy has been hand-edited; restore it from the sync."
    )


def test_manifest_describes_exactly_the_tracked_files():
    """The manifest must describe what a CLEAN CHECKOUT has, not the dev tree.

    This is the invariant the first version got wrong: it hashed every file under
    the vendored directory, which on a macOS working copy included five gitignored
    .DS_Store files. The manifest then described a tree no other machine can
    reproduce, so the guard passed only where it was generated and failed in CI,
    which is the one place it exists to run. Comparing against git's own idea of
    what is tracked is the check that would have caught it; a hand-maintained
    exclusion list is the thing that drifts.
    """
    import json
    import pathlib
    import subprocess

    sync = _sync_module()
    repo_root = pathlib.Path(sync.REPO_ROOT)
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", str(pathlib.Path(sync.DEST).relative_to(repo_root))],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git available
        import pytest
        pytest.skip("git is not available to enumerate tracked files")

    dest_rel = pathlib.Path(sync.DEST).relative_to(repo_root).as_posix()
    tracked = {
        line[len(dest_rel) + 1:] for line in out.split("\0")
        if line and line.startswith(dest_rel + "/")
    }
    # _VENDORED.txt is deliberately untracked (local path + sync date), so it is
    # absent from both sides rather than an exception either of them makes.
    tracked.discard("_VENDORED.txt")
    manifest = json.loads(pathlib.Path(sync.MANIFEST).read_text(encoding="utf-8"))
    described = set(manifest["files"])

    assert described == tracked, (
        "The vendor manifest does not describe the tracked files.\n"
        f"  in the manifest but not tracked (CI will not have these): {sorted(described - tracked)}\n"
        f"  tracked but not in the manifest: {sorted(tracked - described)}\n"
        "Re-run scripts/sync_mesa_core.py; if untracked files are being hashed, they "
        "should be excluded by _is_excluded rather than committed."
    )


def test_the_vendored_tree_ships_no_os_metadata():
    # The sync copies non-Python files verbatim, so without an exclusion it also
    # copies whatever the operating system left in the source directory.
    import pathlib

    sync = _sync_module()
    # __pycache__ is excluded rather than asserted on: Python creates it at
    # runtime from the vendored source, so it is present on any machine that has
    # imported the package and says nothing about what the sync copied.
    junk = [
        p.relative_to(sync.DEST).as_posix()
        for p in pathlib.Path(sync.DEST).rglob("*")
        if p.is_file()
        and "__pycache__" not in p.relative_to(sync.DEST).parts
        and p.name in sync._EXCLUDED_NAMES
    ]
    assert junk == [], f"OS/editor metadata was vendored: {junk}"


def test_manifest_version_matches_the_package_it_describes():
    # A manifest describing a different release than the code it hashes would
    # make the version in the tree unverifiable, which is what it exists to fix.
    import json
    import pathlib

    from custom_components.phoenix_mcp import mesa_core

    sync = _sync_module()
    manifest = json.loads(pathlib.Path(sync.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["source_version"] == mesa_core.__version__


def test_check_mode_writes_nothing():
    # It has to be safe to run on a clean tree and in CI; a "verify" that mutates
    # the thing it verifies is worse than no verify.
    import pathlib

    sync = _sync_module()
    before = {
        p: p.stat().st_mtime_ns
        for p in pathlib.Path(sync.DEST).rglob("*") if p.is_file()
    }
    manifest_before = pathlib.Path(sync.MANIFEST).read_bytes()

    assert sync.check() == 0

    after = {
        p: p.stat().st_mtime_ns
        for p in pathlib.Path(sync.DEST).rglob("*") if p.is_file()
    }
    assert before == after
    assert pathlib.Path(sync.MANIFEST).read_bytes() == manifest_before
