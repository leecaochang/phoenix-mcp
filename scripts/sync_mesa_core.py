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
    python scripts/sync_mesa_core.py [SOURCE_DIR]

SOURCE_DIR defaults to the ``mesa-core`` symlink at the repository root.
"""

from __future__ import annotations

import datetime
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "mesa-core" / "mesa_core"
DEST = REPO_ROOT / "custom_components" / "phoenix_mcp" / "mesa_core"

VENDOR_PREFIX = "custom_components.phoenix_mcp.mesa_core"

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
        if "__pycache__" in src_path.parts:
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

    print(f"Vendored mesa-core {version} -> {DEST} ({copied} files, import prefix rewritten).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
