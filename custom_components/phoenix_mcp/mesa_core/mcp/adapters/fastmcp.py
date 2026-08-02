"""Adapter for FastMCP-style servers (both fastmcp 2.x and mcp.server.fastmcp).

Registration prefers the ``server.tool(...)`` decorator API, which both FastMCP
lineages expose, falling back to ``add_tool`` for older versions.

Both lineages derive a tool's published ``inputSchema`` by introspecting the
registered function, so the function's signature is the published schema. A
generic ``params: dict`` shim would therefore advertise ``{"params": {...}}``
and reject every payload the specification documents, so the signature is built
from the tool's declared schema instead. That keeps what is advertised and what
is enforced the same object rather than two artifacts that can drift.
"""

from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any, Literal

from custom_components.phoenix_mcp.mesa_core.exceptions import MesaError
from custom_components.phoenix_mcp.mesa_core.mcp.adapters import ToolHandler

logger = logging.getLogger("mesa_core.mcp")

# Subscripted at runtime from the declared schema, which the static forms cannot express.
_LITERAL: Any = Literal
_LIST: Any = list

# JSON Schema keyword -> the pydantic Field constraint that republishes it.
_CONSTRAINTS = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minItems": "min_length",
    "maxItems": "max_length",
}


def _scalar_types() -> dict[str, Any]:
    """Strict pydantic scalar types, so the transport rejects what the published
    schema rejects rather than coercing it.

    Ordinary ``bool``/``int`` annotations let pydantic coerce a JSON string
    (``"false"``, ``"50"``) before the handler sees it, so the server would
    accept values its own ``boolean``/``integer`` schema forbids. Strict types
    accept only the JSON type the schema declares; ``number`` still accepts an
    integer or a float but not a string.
    """
    from pydantic import StrictBool, StrictInt, StrictStr

    return {
        "string": StrictStr,
        "boolean": StrictBool,
        "integer": StrictInt,
        "object": dict,
    }


def _base_annotation(spec: dict[str, Any]) -> Any:
    if "enum" in spec:
        return _LITERAL[tuple(spec["enum"])]
    if spec.get("type") == "array":
        return _LIST[_base_annotation(spec.get("items", {}))]
    return _scalar_types().get(spec.get("type", ""), Any)


def _annotation(spec: dict[str, Any]) -> Any:
    """The annotation that republishes one property of the declared schema."""
    from pydantic import Field, StrictFloat, StrictInt  # fastmcp's own dependency

    numeric = {arg: spec[key] for key, arg in _CONSTRAINTS.items() if key in spec}
    field_kwargs = dict(numeric)
    if "description" in spec:
        field_kwargs["description"] = spec["description"]

    if "enum" not in spec and spec.get("type") == "number":
        # number accepts an int or a float but not a string. The bounds are
        # applied to each union member so pydantic publishes JSON Schema
        # keywords (exclusiveMinimum/minimum/maximum) on each branch; a plain
        # Field on the union would emit gt/ge/le, which standard validators
        # ignore, showing clients a looser contract than the transport enforces.
        branch = Field(**numeric) if numeric else None
        int_t = Annotated[StrictInt, branch] if branch else StrictInt
        float_t = Annotated[StrictFloat, branch] if branch else StrictFloat
        union = int_t | float_t
        if "description" in spec:
            return Annotated[union, Field(description=spec["description"])]
        return union

    annotation = _base_annotation(spec)
    if not field_kwargs:
        return annotation
    return Annotated[annotation, Field(**field_kwargs)]


def tool_function(name: str, handler: ToolHandler, schema: dict[str, Any]) -> Any:
    """Wrap a handler in a callable whose signature is ``schema``."""
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for key, spec in properties.items():
        annotation = _annotation(spec)
        default = inspect.Parameter.empty if key in required else spec.get("default", None)
        annotations[key] = annotation
        parameters.append(
            inspect.Parameter(
                key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default
            )
        )

    async def tool_fn(**kwargs: Any) -> dict[str, Any]:
        # Both FastMCP lineages fill every unset optional with its None default,
        # so a None here means "not provided": strip it so the handler applies
        # its own default. An explicit caller-supplied null never arrives, the
        # non-nullable strict annotation rejects it at the transport first.
        return await handler({key: value for key, value in kwargs.items() if value is not None})

    annotations["return"] = dict[str, Any]
    tool_fn.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=dict[str, Any]
    )
    tool_fn.__annotations__ = annotations
    tool_fn.__name__ = name
    return tool_fn


class FastMCPRegistry:
    def __init__(self, server: Any) -> None:
        if server is None:
            raise MesaError("the fastmcp adapter requires server=<FastMCP instance>")
        self.server = server
        self.registered: list[str] = []

    def register_tool(
        self, name: str, handler: ToolHandler, schema: dict[str, Any], description: str
    ) -> None:
        tool_fn = tool_function(name, handler, schema)
        tool_fn.__doc__ = description

        tool_decorator = getattr(self.server, "tool", None)
        add_tool = getattr(self.server, "add_tool", None)
        if callable(tool_decorator):
            try:
                tool_decorator(name=name, description=description)(tool_fn)
            except TypeError:
                tool_decorator()(tool_fn)
        elif callable(add_tool):
            try:
                add_tool(tool_fn, name=name, description=description)
            except TypeError:
                add_tool(tool_fn)
        else:
            raise MesaError(
                "server does not look like a FastMCP instance (no .tool or .add_tool)"
            )
        self._forbid_extra_properties(name)
        self.registered.append(name)

    def _forbid_extra_properties(self, name: str) -> None:
        """Make the mcp.server.fastmcp lineage reject unknown properties.

        That lineage builds each tool's argument model with ``extra='ignore'``,
        so its published inputSchema omits ``additionalProperties: false`` and an
        unknown key is dropped before dispatch rather than rejected: a query with
        a mistyped filter name would silently run unfiltered. Flipping the model
        to ``extra='forbid'`` makes both the published schema and the transport
        agree with the declared schema. The standalone fastmcp lineage already
        forbids extras, so this is a no-op there; the handler-level validator
        remains the cross-transport backstop either way.
        """
        manager = getattr(self.server, "_tool_manager", None)
        get_tool = getattr(manager, "get_tool", None)
        if not callable(get_tool):
            return
        # This reaches into mcp.server.fastmcp internals (the tool's pydantic
        # argument model). If a future SDK version reshapes them, degrade to the
        # handler-level validator with a warning rather than crash every tool's
        # registration; the handler backstop still rejects known bad values,
        # only the transport-level extra-key rejection on this lineage is lost.
        try:
            tool: Any = get_tool(name)
            if inspect.isawaitable(tool):
                # standalone fastmcp 2.x exposes a _tool_manager whose get_tool
                # is a coroutine function. That lineage already forbids extras,
                # so there is nothing to fix here; close the coroutine rather
                # than leaving it unawaited, which would emit a RuntimeWarning
                # per registered tool.
                close = getattr(tool, "close", None)
                if close is not None:
                    close()
                return
            arg_model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
            if arg_model is None:
                return
            if arg_model.model_config.get("extra") != "forbid":
                arg_model.model_config["extra"] = "forbid"
                arg_model.model_rebuild(force=True)
            if hasattr(tool, "parameters"):
                tool.parameters = arg_model.model_json_schema(by_alias=True)
        except Exception:
            logger.warning(
                "could not set additionalProperties:false on mcp.server.fastmcp tool %r; "
                "unknown-field rejection on that transport is degraded to the handler "
                "validator",
                name,
                exc_info=True,
            )
