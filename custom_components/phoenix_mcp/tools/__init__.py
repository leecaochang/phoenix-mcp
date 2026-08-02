"""Per-domain MCP tool modules.

Each module here owns one domain's tools end to end: the _tool_X gate, the
_execute_X side effect, its diff builders and its prechecks. They depend on
tool_common for the shared primitives and are imported by mcp_view, which owns
the transport and the dispatch registry. The dependency runs one way, so a
domain module never imports the transport that dispatches it.
"""
