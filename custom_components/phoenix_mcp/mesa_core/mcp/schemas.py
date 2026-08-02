"""Input schemas for the MESA MCP tools (Spec 9.2, 9.5).

``schemas/mesa_tools.schema.json`` is generated from TOOL_SCHEMAS and shipped
as the machine-readable artifact; a test asserts the two stay in sync.
"""

from __future__ import annotations

from typing import Any

TOOL_DESCRIPTIONS: dict[str, str] = {
    "mesa_query_profiles": (
        "Query MESA semantic profiles by domain, tag, area, device, integration, "
        "intent, or origin, with pagination. Returns effective "
        "(inheritance-resolved) profiles."
    ),
    "mesa_get_profile": (
        "Retrieve the complete effective MESA profile for one entity, optionally "
        "including its diagnostic profile."
    ),
    "mesa_explain_profile": (
        "Return the full inheritance resolution path for an entity: which profile "
        "level contributed each effective field and why. The first tool to reach "
        "for when agent behaviour is unexpected."
    ),
    "mesa_get_caller_context": (
        "Retrieve caller identity and roles for the current session."
    ),
    "mesa_request_lease": (
        "Request a temporary advisory coordination lease on entities (max 30s). "
        "Not a lock: native automations remain unaware. Partial grants are valid; "
        "denial_reasons explains denied entities."
    ),
    "mesa_release_lease": (
        "Release a held coordination lease early to signal completion."
    ),
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "mesa_query_profiles": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "domains": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "tags_match": {"enum": ["any", "all"], "default": "any"},
            "areas": {"type": "array", "items": {"type": "string"}},
            "devices": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filter by HA device registry ID; requires the host's "
                    "entity-to-device mapping (Spec 5.6)."
                ),
            },
            "integrations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filter by the integration that created the entity; requires "
                    "the host's entity-to-integration mapping (Spec 5.6)."
                ),
            },
            "intents": {"type": "array", "items": {"type": "string"}},
            "min_origin_authority": {
                "enum": ["inferred_ai", "hybrid", "user", "developer"]
            },
            "include_inferred": {"type": "boolean", "default": False},
            "include_fields": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            "cursor": {"type": "string"},
        },
    },
    "mesa_get_profile": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entity_id"],
        "properties": {
            "entity_id": {"type": "string"},
            "include_diagnostic": {"type": "boolean", "default": True},
            "include_semantic_moments": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Include the HA purpose-specific triggers/conditions "
                    "(2026.7+) this entity participates in, when the host "
                    "exposes them. Context only; carries no MESA authority."
                ),
            },
        },
    },
    "mesa_explain_profile": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entity_id"],
        "properties": {
            "entity_id": {"type": "string"},
            "show_conflicts": {"type": "boolean", "default": True},
        },
    },
    "mesa_get_caller_context": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    "mesa_request_lease": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entities", "duration_seconds"],
        "properties": {
            "entities": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "duration_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Requested duration; values above 30 are clamped (Spec 21.2).",
            },
            "intent": {"type": "string"},
            "priority_level": {
                "enum": ["deferential", "cooperative", "assertive"],
                "default": "cooperative",
            },
            "caller_priority": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Accepted but unused until multi-agent resolution (Spec 21.6).",
            },
            "preemption_handling": {
                "enum": ["rollback_abort", "continue_ignore"],
                "default": "rollback_abort",
            },
        },
    },
    "mesa_release_lease": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lease_id"],
        "properties": {"lease_id": {"type": "string"}},
    },
}


def tools_schema_document() -> dict[str, Any]:
    """The document shipped as schemas/mesa_tools.schema.json."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mesa-spec.org/schemas/mesa_tools.schema.json",
        "title": "MESA MCP tool input schemas",
        "description": (
            "Input schemas for the MESA retrieval API tools (MESA Specification "
            "Section 9) and the lease coordination tools (MESA Enrichment "
            "Section 21)."
        ),
        "tools": {
            name: {"description": TOOL_DESCRIPTIONS[name], "inputSchema": schema}
            for name, schema in TOOL_SCHEMAS.items()
        },
    }
