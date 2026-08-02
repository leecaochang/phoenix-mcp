"""Print the tool count from the registry, the single source of truth.

Usage: python scripts/count_tools.py

A count restated by hand in prose goes stale silently: nothing breaks and the
sentence still reads fine. So nothing states it by hand. The documentation
counts are pinned against mcp_view.tool_catalog_counts(), the admin info
endpoint serves it live, and this script answers "how many tools are there"
without anyone recounting.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from custom_components.phoenix_mcp.mcp_view import tool_catalog_counts  # noqa: E402


def main() -> None:
    counts = tool_catalog_counts()
    print(f"total:      {counts['total']}")
    print(f"native:     {counts['native']} (HA's own MCP tool names, implemented 1:1)")
    print(f"additional: {counts['additional']} (Phoenix MCP's own, mesa_* included)")


if __name__ == "__main__":
    main()
