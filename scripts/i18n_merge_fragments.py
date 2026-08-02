"""Merge parallel i18n extraction fragments into translations/en.json.

Parallel workers write their catalog additions to separate JSON files so they
never race on en.json. This folds those in, refusing on any key collision:
two workers claiming the same key means one of them strayed outside its
namespace, and silently letting the last writer win would drop a string.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOG = REPO / "custom_components" / "phoenix_mcp" / "catalogs" / "en.json"


def flatten(node: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            out[path] = value
        elif isinstance(value, dict):
            out.update(flatten(value, path))
    return out


def put(node: dict, dotted: str, value: str) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def main(paths: list[str]) -> None:
    doc = json.loads(CATALOG.read_text())
    panel = doc["panel"]
    existing = flatten(panel)

    incoming: dict[str, tuple[str, str]] = {}
    for path in paths:
        f = pathlib.Path(path)
        if not f.exists():
            print(f"skip (absent): {path}")
            continue
        for key, value in flatten(json.loads(f.read_text())).items():
            if key in existing and existing[key] != value:
                sys.exit(f"COLLISION with en.json: {key}\n  have {existing[key]!r}\n  new  {value!r}")
            if key in incoming and incoming[key][1] != value:
                sys.exit(f"COLLISION between fragments: {key} ({incoming[key][0]} vs {path})")
            incoming[key] = (path, value)

    for key, (_src, value) in sorted(incoming.items()):
        put(panel, key, value)

    CATALOG.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"merged {len(incoming)} keys from {len(paths)} fragments")


if __name__ == "__main__":
    main(sys.argv[1:])
