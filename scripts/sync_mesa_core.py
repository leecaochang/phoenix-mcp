#!/usr/bin/env python3
"""Vendor a copy of mesa-core into custom_components/phoenix_mcp/mesa_core/.

mesa-core is developed as a separate, read-only project. Phoenix MCP ships HACS users a
vendored copy rather than depending on a (not-yet-published) PyPI package. The
source uses absolute imports rooted at ``mesa_core``; vendoring under
``custom_components.phoenix_mcp`` requires rewriting those to the vendored path so the
copy is hermetic and never collides with a future ``pip install mesa-core`` or
another integration's copy.

This script reads the source tree only. It never modifies mesa-core. Re-run it
to re-sync after the upstream library changes; commit the regenerated
``mesa_core/`` directory.

Usage:
    python scripts/sync_mesa_core.py [SOURCE_DIR]   re-vendor from source
    python scripts/sync_mesa_core.py --check        verify, writing nothing

SOURCE_DIR defaults to the ``mesa-core`` symlink at the repository root.

``--check`` needs no source tree at all: it hashes the vendored files against
``tests/contract/mesa_core_vendor.json``, which IS tracked, so a clean checkout
(or CI, which has no mesa-core symlink) can tell a re-sync from a hand-edit. The
copy is ordinary Python that imports and passes tests whatever it says, so
nothing else would notice an edit to it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "mesa-core" / "mesa_core"
DEST = REPO_ROOT / "custom_components" / "phoenix_mcp" / "mesa_core"
# Tracked, unlike _VENDORED.txt (which is gitignored because it records a local
# absolute path and a sync date). This is the copy a clean checkout can check.
MANIFEST = REPO_ROOT / "tests" / "contract" / "mesa_core_vendor.json"

VENDOR_PREFIX = "custom_components.phoenix_mcp.mesa_core"

# Never copied into the vendored tree and never hashed. These are the operating
# system's and the editor's, not the library's: they exist only in a developer's
# working copy, they are gitignored, and a CI checkout on Linux does not have
# them. Copying them shipped junk; hashing them made the manifest describe a tree
# no clean checkout can reproduce, so the guard failed everywhere except the
# machine that generated it.
_EXCLUDED_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".gitkeep"})
# The provenance marker itself: it records a sync DATE and a local absolute path,
# so hashing it would change the digest on every re-run with no upstream change.
_MANIFEST_EXCLUDED_NAMES = _EXCLUDED_NAMES | {"_VENDORED.txt"}


def _is_excluded(path: Path, root: Path) -> bool:
    """True for OS/editor metadata and caches, which never belong in the copy."""
    rel = path.relative_to(root)
    if _EXCLUDED_DIRS.intersection(rel.parts):
        return True
    return path.name in _EXCLUDED_NAMES or path.name.endswith((".pyc", ".pyo", ".swp"))

# All internal imports in mesa-core take the form ``from mesa_core[.x] import``.
# A bare ``import mesa_core`` would bind the wrong name after rewriting, so we
# detect and refuse it rather than silently produce broken code.
_FROM_RE = re.compile(r"\bfrom mesa_core\b")
_BARE_IMPORT_RE = re.compile(r"\bimport mesa_core\b(?!\s*\.)")


def _rewrite_source(text: str, rel: Path) -> str:
    if _BARE_IMPORT_RE.search(text):
        raise SystemExit(
            f"ERROR: {rel} contains a bare 'import mesa_core' statement, which "
            "the vendoring rewrite cannot handle. Report this to the user; do "
            "not edit mesa-core."
        )
    return _FROM_RE.sub(f"from {VENDOR_PREFIX}", text)


def _read_source_version(source_root: Path) -> str:
    init = (source_root / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    return match.group(1) if match else "unknown"


def main() -> int:
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_SOURCE.resolve()
    if not source.is_dir():
        raise SystemExit(f"ERROR: source mesa_core package not found at {source}")

    version = _read_source_version(source)

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    copied = 0
    for src_path in sorted(source.rglob("*")):
        if _is_excluded(src_path, source):
            continue
        rel = src_path.relative_to(source)
        dest_path = DEST / rel
        if src_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            continue
        if src_path.suffix == ".py":
            text = src_path.read_text(encoding="utf-8")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(_rewrite_source(text, rel), encoding="utf-8")
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)
        copied += 1

    stamp = datetime.date.today().isoformat()
    (DEST / "_VENDORED.txt").write_text(
        "This directory is a vendored copy of mesa-core. Do not edit by hand.\n"
        "Regenerate with scripts/sync_mesa_core.py.\n\n"
        f"source_version: {version}\n"
        f"synced_on: {stamp}\n"
        f"source_path: {source}\n",
        encoding="utf-8",
    )
    _write_manifest(version)

    print(f"Vendored mesa-core {version} -> {DEST} ({copied} files, import prefix rewritten).")
    print(f"Wrote {MANIFEST.relative_to(REPO_ROOT)}; commit it with the vendored tree.")
    return 0


def _tree_digest() -> tuple[str, dict[str, str]]:
    """Hash every SHIPPED vendored file, and the whole tree.

    What it skips is the load-bearing part, and getting it wrong made the guard
    useless in the one place it had to work. It must hash exactly the files a
    clean checkout has:

    - OS and editor metadata (.DS_Store and friends) is gitignored, so it exists
      only in a developer's working copy. Hashing it made the manifest describe a
      tree CI cannot reproduce, and the vendoring test then failed on Linux while
      passing on the machine that generated it.
    - _VENDORED.txt carries a sync DATE and a local absolute path, so hashing it
      would change the digest on every re-run with no upstream change.
    """
    per_file: dict[str, str] = {}
    for path in sorted(DEST.rglob("*")):
        if not path.is_file() or _is_excluded(path, DEST):
            continue
        rel = path.relative_to(DEST).as_posix()
        if path.name in _MANIFEST_EXCLUDED_NAMES:
            continue
        per_file[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    combined = hashlib.sha256(
        "\n".join(f"{rel} {digest}" for rel, digest in per_file.items()).encode()
    ).hexdigest()
    return combined, per_file


def _write_manifest(version: str) -> None:
    combined, per_file = _tree_digest()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {
                "_comment": (
                    "Content hashes of the vendored mesa-core tree. Generated by "
                    "scripts/sync_mesa_core.py; tests/test_mesa_vendor.py fails if the "
                    "shipped files stop matching. This is TRACKED (unlike _VENDORED.txt, "
                    "which carries a local absolute path and a sync date) so a clean "
                    "checkout can tell a hand-edit from a re-sync."
                ),
                "source_version": version,
                "vendor_prefix": VENDOR_PREFIX,
                "tree_sha256": combined,
                "files": per_file,
            },
            indent=2,
            sort_keys=False,
        ) + "\n",
        encoding="utf-8",
    )


def check() -> int:
    """Report whether the vendored tree still matches its manifest. Writes nothing.

    The reason this exists is narrower than a supply-chain check and worth being
    precise about: mesa-core is a first-party project at a known read-only path,
    so this is not authenticating an unknown upstream. It catches a HAND-EDIT of
    the vendored copy, which the project forbids and which nothing else would
    notice, since the copy is ordinary Python that imports and passes tests
    whatever it says.
    """
    if not MANIFEST.exists():
        print(f"ERROR: {MANIFEST.relative_to(REPO_ROOT)} is missing; run the sync to create it.")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    combined, per_file = _tree_digest()
    if combined == manifest.get("tree_sha256"):
        print(f"Vendored mesa-core matches the manifest ({len(per_file)} files).")
        return 0

    recorded = manifest.get("files", {})
    changed = sorted(k for k in per_file.keys() & recorded.keys() if per_file[k] != recorded[k])
    added = sorted(per_file.keys() - recorded.keys())
    removed = sorted(recorded.keys() - per_file.keys())
    print("ERROR: the vendored mesa-core tree does not match its manifest.")
    for label, names in (("changed", changed), ("added", added), ("removed", removed)):
        for name in names:
            print(f"  {label}: {name}")
    print("\nIf this was a re-sync, re-run scripts/sync_mesa_core.py to regenerate the")
    print("manifest. If it was not, the vendored copy has been hand-edited; restore it")
    print("from the sync rather than keeping the edit.")
    return 1


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    raise SystemExit(main())
