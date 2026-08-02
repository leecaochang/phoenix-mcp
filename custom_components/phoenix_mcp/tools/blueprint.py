"""Blueprint tools: list, read, and the create/edit/delete authoring surface.

Reads gate on cap_config_read; writes carry a DUAL gate, and that is the
load-bearing detail in this module. cap_blueprint_write says "you may author
blueprints" and the domain cap (cap_automation_write / cap_script_write) says
"you may affect this domain's configs". Both must permit, because a blueprint
edit reloads every automation or script built from it while those configs stay
byte-identical: without the domain key a token could rewire scripts through a
script blueprint with cap_script_write set to deny. That is also why the
approval diff names the consumer entities rather than showing the YAML alone.

Writes go through HA's own blueprint/save and blueprint/delete WS commands, so
HA re-validates on the way in and refuses a delete while the blueprint is still
in use. Phoenix validates first anyway, so a doomed write is refused before an
approval is ever created, using the same generic BLUEPRINT_SCHEMA HA's save
handler uses.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import (
    CAP_DENY,
    MAX_DIFF_INLINE_BYTES,
    MAX_FILE_BYTES,
    MAX_PREVIEW_ENTITY_IDS,
)
from ..data import PhoenixData
from ..helpers import diff_summary_fields as _summary, effective_cap
from ..token_store import TokenRecord
from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _gate,
    _read_text_capped,
    _record_version,
    _tool_error,
    _tool_success,
    _truncate,
    _usable_path_arg,
    _version_content_payload,
)
from ..ws_dispatch import WsDispatchError, async_ws_command

_LOGGER = logging.getLogger(__name__)


async def _domain_blueprints(hass: HomeAssistant, domain: str) -> dict:
    """Return {path: Blueprint | Exception} for a blueprint domain.

    A blueprint that failed to parse is stored as its own exception by HA, so the
    mapping is not uniformly Blueprint; callers must check. Raises on a domain that
    cannot be read at all.
    """
    if domain == "automation":
        from homeassistant.components.automation import async_get_blueprints as _get_bp  # noqa: PLC0415
    else:
        from homeassistant.components.script import async_get_blueprints as _get_bp  # noqa: PLC0415
    return await _get_bp(hass).async_get_blueprints()


async def _tool_list_blueprints(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list automation/script blueprints and their inputs (cap_config_read)."""
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_blueprints"
    domain_arg = str(args.get("domain") or "").strip().lower()
    domains = [domain_arg] if domain_arg in ("automation", "script") else ["automation", "script"]
    out: list[dict] = []
    # A domain that could not be read and a blueprint that failed to parse both used
    # to vanish silently, so a short list looked like a complete one. The counts are
    # reported instead; the listing itself still succeeds.
    warnings: list[str] = []
    for dom in domains:
        try:
            blueprints = await _domain_blueprints(hass, dom)
        except Exception:  # noqa: BLE001 - a missing/again-failing domain yields no blueprints
            _LOGGER.debug("list_blueprints failed for domain %s", dom, exc_info=True)
            warnings.append(f"Failed to read {dom} blueprints; none are listed for that domain.")
            continue
        failed = 0
        for path, bp in sorted(blueprints.items()):
            if isinstance(bp, Exception):
                failed += 1  # a blueprint that failed to load is stored as its exception
                continue
            out.append(_blueprint_meta(dom, path, bp))
        if failed:
            warnings.append(f"{failed} {dom} blueprint(s) failed to load and were omitted.")
    body: dict[str, Any] = {"count": len(out), "blueprints": out}
    if warnings:
        body["warnings"] = warnings
    return _tool_success(json.dumps(body, default=str)), "allowed", "list_blueprints"


_BLUEPRINT_DOMAINS = ("automation", "script")

# The domain cap that must ALSO permit a blueprint write, per domain. Two keys by
# design: cap_blueprint_write says "you may author blueprints", the domain cap says
# "you may affect this domain's configs". Without the second, a token holding
# cap_blueprint_write but cap_script_write=deny could edit a script blueprint and
# rewire scripts it cannot write directly, which is escalation through the
# blueprint surface. Mirrors the pass-through dual-gate precedent.
_BLUEPRINT_DOMAIN_CAPS = {
    "automation": "cap_automation_write",
    "script": "cap_script_write",
}

_BLUEPRINT_PATH_REFUSED = (
    "path must be a relative .yaml or .yml path inside the blueprint domain "
    "folder, e.g. 'my_author/my_blueprint.yaml'."
)


def _resolve_blueprint_path(hass: HomeAssistant, domain: str, path: Any) -> tuple[str, str] | None:
    """Resolve a blueprint path to (realpath, config-relative path), or None.

    Unlike get_blueprint (which can require an exact match against the paths HA
    already enumerated), a create names a file that does not exist yet, so this is
    a real jail: relative only, .yaml/.yml, no dot-segments, and the realpath must
    land under <config>/blueprints/<domain>/.
    """
    if _usable_path_arg(path) is None:
        return None
    rel = path.strip().replace(os.sep, "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or os.path.isabs(rel):
        return None
    if any(p.startswith(".") for p in parts):
        return None
    if not parts[-1].lower().endswith((".yaml", ".yml")):
        return None
    base = os.path.realpath(os.path.join(hass.config.config_dir, "blueprints", domain))
    candidate = os.path.realpath(os.path.join(base, *parts))
    if not candidate.startswith(base + os.sep):
        return None
    return candidate, "/".join(parts)


def _blueprint_consumers(hass: HomeAssistant, domain: str, path: str) -> list[str]:
    """Entity ids whose config comes from this blueprint, best-effort.

    Overwriting a blueprint makes HA reload every consumer, while those entities'
    own configs do not change by a byte, so the approval diff has to name them or
    the operator cannot see what a small YAML diff actually rewires.
    """
    try:
        if domain == "automation":
            from homeassistant.components.automation import (  # noqa: PLC0415
                automations_with_blueprint as _with_bp,
            )
        else:
            from homeassistant.components.script import (  # noqa: PLC0415
                scripts_with_blueprint as _with_bp,
            )
        return list(_with_bp(hass, path))
    except Exception:  # noqa: BLE001 - annotation only; never block the gate
        _LOGGER.debug("blueprint consumer lookup failed for %s/%s", domain, path, exc_info=True)
        return []


def _validate_blueprint_source(domain: str, content: str) -> str | None:
    """Validate blueprint YAML the way HA's own save path will. Error text or None.

    Side-effect-free, so it runs pre-gate. Deliberately mirrors HA's
    blueprint/save handler exactly, including its use of the GENERIC
    BLUEPRINT_SCHEMA for every domain: HA's Blueprint constructor checks the
    schema, that the declared domain matches the target, and that every !input the
    body uses has an input definition. The write itself re-validates inside HA.
    """
    from homeassistant.components.blueprint.models import Blueprint  # noqa: PLC0415
    from homeassistant.components.blueprint.schemas import (  # noqa: PLC0415
        BLUEPRINT_SCHEMA,
    )
    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415
    from homeassistant.util import yaml as yaml_util  # noqa: PLC0415

    try:
        data = yaml_util.parse_yaml(content)
    except HomeAssistantError as err:
        return f"content is not valid YAML: {err}"
    if not isinstance(data, dict):
        return "content must be a YAML mapping with a top-level 'blueprint' key."
    try:
        Blueprint(data, expected_domain=domain, schema=BLUEPRINT_SCHEMA)
    except HomeAssistantError as err:  # InvalidBlueprint and friends
        return f"not a valid {domain} blueprint: {err}"
    except Exception as err:  # noqa: BLE001 - never leak an unexpected type
        _LOGGER.debug("blueprint validation error", exc_info=True)
        return f"blueprint validation failed: {err}"
    return None


def _blueprint_meta(domain: str, path: str, bp: Any) -> dict:
    """The list_blueprints row shape, so both tools report a blueprint identically."""
    meta = bp.metadata or {}
    return {
        "domain": domain,
        "path": path,
        "name": meta.get("name") or path,
        "description": meta.get("description"),
        "input": meta.get("input"),
        "source_url": meta.get("source_url"),
    }


async def _tool_get_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read one blueprint's raw YAML source (cap_config_read)."""
    if effective_cap(token, "cap_config_read") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_blueprint"
    domain = str(args.get("domain") or "").strip().lower()
    if domain not in ("automation", "script"):
        return _tool_error("domain must be 'automation' or 'script'."), "invalid_request", "get_blueprint"
    path = str(args.get("path") or "").strip()
    if not path:
        return _tool_error("Missing required argument: path"), "invalid_request", "get_blueprint"
    try:
        blueprints = await _domain_blueprints(hass, domain)
    except Exception:  # noqa: BLE001 - the domain could not be read at all
        _LOGGER.debug("get_blueprint failed to list domain %s", domain, exc_info=True)
        return _tool_error("Failed to read blueprints."), "invalid_request", "get_blueprint"
    bp = blueprints.get(path)
    if bp is None:
        # The jail: only a path HA itself enumerated is ever joined onto the
        # blueprints directory, so traversal cannot be expressed here at all.
        return _tool_error("Blueprint not found."), "not_found", "get_blueprint"
    if isinstance(bp, Exception):
        return _tool_error("Blueprint failed to load."), "invalid_request", "get_blueprint"
    target = hass.config.path("blueprints", domain, path)
    try:
        content = await hass.async_add_executor_job(_read_text_capped, target)
    except ValueError:
        return _tool_error("Blueprint file exceeds the maximum readable size."), "invalid_request", "get_blueprint"
    except OSError:
        return _tool_error("Failed to read blueprint file."), "invalid_request", "get_blueprint"
    # Raw file text, not bp.data: the parsed form holds HA Input placeholder
    # objects that do not serialize, and the source is what an author needs to see.
    body = {**_blueprint_meta(domain, path, bp), "content": content}
    return _tool_success(json.dumps(body, default=str)), "allowed", "get_blueprint"


def _blueprint_write_precheck(
    args: dict, hass: HomeAssistant, tool_name: str, *, needs_content: bool
) -> tuple[tuple[dict, str, str] | None, str, str]:
    """Pre-gate validation for a blueprint write.

    Returns (refusal_response | None, realpath, config-relative path). The first
    element is a COMPLETE tool response tuple so callers can `return pre` directly.

    Runs after the explicit cap-deny checks, so a fully denied token learns
    nothing about its own payload, and is additive: HA re-validates on the way
    in regardless.
    """
    def _refuse(message: str) -> tuple[tuple[dict, str, str], str, str]:
        return (_tool_error(message), "invalid_request", tool_name), "", ""

    domain = str(args.get("domain") or "").strip().lower()
    if domain not in _BLUEPRINT_DOMAINS:
        return _refuse("domain must be 'automation' or 'script'.")
    resolved = _resolve_blueprint_path(hass, domain, args.get("path"))
    if resolved is None:
        return _refuse(_BLUEPRINT_PATH_REFUSED)
    target, rel = resolved
    if needs_content:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _refuse("content must be a non-empty string.")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return _refuse("Content exceeds the maximum file size.")
        problem = _validate_blueprint_source(domain, content)
        if problem is not None:
            return _refuse(problem)
    return None, target, rel


def _blueprint_denied(token: TokenRecord, args: dict, tool_name: str) -> tuple[dict, str, str] | None:
    """Uniform Forbidden when either key of the blueprint dual-gate denies.

    cap_blueprint_write authorizes blueprint authoring; the domain cap authorizes
    affecting that domain's configs. Both must permit, or a token could rewire
    scripts through a script blueprint while cap_script_write says deny.
    """
    if effective_cap(token, "cap_blueprint_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool_name
    domain = str(args.get("domain") or "").strip().lower()
    domain_cap = _BLUEPRINT_DOMAIN_CAPS.get(domain)
    if domain_cap is not None and effective_cap(token, domain_cap) == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool_name
    return None


async def _read_blueprint_source(hass: HomeAssistant, target: str) -> str | None:
    """Current blueprint file text, or None when absent or unreadable."""
    if not await hass.async_add_executor_job(os.path.isfile, target):
        return None
    try:
        return await hass.async_add_executor_job(_read_text_capped, target)
    except (OSError, ValueError):
        return None


async def _build_diff_blueprint(
    args: dict, hass: HomeAssistant, op: str
) -> dict:
    """Approval diff for a blueprint write.

    The consumer list is the point of this diff for an edit or delete: overwriting
    an in-use blueprint reloads every consumer while their own configs stay
    byte-identical, so a small YAML diff can silently rewire several automations.
    """
    domain = str(args.get("domain") or "").strip().lower()
    resolved = _resolve_blueprint_path(hass, domain, args.get("path"))
    rel = resolved[1] if resolved else str(args.get("path") or "")
    before = await _read_blueprint_source(hass, resolved[0]) if resolved else None
    content = args.get("content") if isinstance(args.get("content"), str) else None
    consumers = await hass.async_add_executor_job(
        _blueprint_consumers, hass, domain, rel) if rel else []
    fields = (
        _summary(f"blueprint.{op}.consumers", domain=domain, rel=rel, count=len(consumers))
        if consumers and op != "create"
        else _summary(f"blueprint.{op}", domain=domain, rel=rel)
    )
    return {
        "kind": "yaml_diff",
        **fields,
        "target": {"type": "blueprint", "id": f"{domain}/{rel}", "label": rel},
        "before": _truncate(before, max_chars=MAX_DIFF_INLINE_BYTES) if before else None,
        "after": _truncate(content, max_chars=MAX_DIFF_INLINE_BYTES) if content else None,
        "preview": {
            "domain": domain,
            "path": rel,
            "used_by": consumers[:MAX_PREVIEW_ENTITY_IDS],
            "used_by_count": len(consumers),
        },
    }


async def _tool_create_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a new blueprint (Confirm-gated)."""
    denied = _blueprint_denied(token, args, "create_blueprint")
    if denied is not None:
        return denied
    pre, _target, _rel = _blueprint_write_precheck(
        args, hass, "create_blueprint", needs_content=True)
    if pre is not None:
        return pre
    blocked = await _gate(
        "cap_blueprint_write", token, hass, data,
        tool_name="create_blueprint", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_blueprint(args, hass, "create"),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_blueprint(args, token, hass, data)


async def _tool_edit_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: replace an existing blueprint's source (Confirm-gated)."""
    denied = _blueprint_denied(token, args, "edit_blueprint")
    if denied is not None:
        return denied
    pre, target, _rel = _blueprint_write_precheck(
        args, hass, "edit_blueprint", needs_content=True)
    if pre is not None:
        return pre
    if await _read_blueprint_source(hass, target) is None:
        return _tool_error("Blueprint not found."), "not_found", "edit_blueprint"
    blocked = await _gate(
        "cap_blueprint_write", token, hass, data,
        tool_name="edit_blueprint", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_blueprint(args, hass, "edit"),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_blueprint(args, token, hass, data)


async def _tool_delete_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete a blueprint (Confirm-gated)."""
    denied = _blueprint_denied(token, args, "delete_blueprint")
    if denied is not None:
        return denied
    pre, target, _rel = _blueprint_write_precheck(
        args, hass, "delete_blueprint", needs_content=False)
    if pre is not None:
        return pre
    if await _read_blueprint_source(hass, target) is None:
        return _tool_error("Blueprint not found."), "not_found", "delete_blueprint"
    blocked = await _gate(
        "cap_blueprint_write", token, hass, data,
        tool_name="delete_blueprint", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_blueprint(args, hass, "delete"),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_blueprint(args, token, hass, data)


async def _save_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    *, tool_name: str, allow_override: bool,
) -> tuple[dict, str, str]:
    """Shared apply path for create/edit: HA's blueprint/save does the real work."""
    denied = _blueprint_denied(token, args, tool_name)
    if denied is not None:
        return denied
    pre, target, rel = _blueprint_write_precheck(
        args, hass, tool_name, needs_content=True)
    if pre is not None:
        return pre
    domain = str(args.get("domain") or "").strip().lower()
    content = args["content"]
    before = await _read_blueprint_source(hass, target)
    if allow_override and before is None:
        return _tool_error("Blueprint not found."), "not_found", tool_name
    if not allow_override and before is not None:
        return _tool_error(
            "A blueprint already exists at that path; use edit_blueprint to replace it."
        ), "invalid_request", tool_name
    consumers = await hass.async_add_executor_job(_blueprint_consumers, hass, domain, rel)
    try:
        result = await async_ws_command(hass, "blueprint/save", {
            "domain": domain, "path": rel, "yaml": content,
            "allow_override": allow_override,
        })
    except WsDispatchError as exc:
        return _tool_error(f"Failed to save blueprint: {exc}"), "invalid_request", tool_name
    await _record_version(
        data, token, resource_type="blueprint", resource_id=f"{domain}/{rel}",
        action="edit" if before is not None else "create",
        before=_version_content_payload(before) if before is not None else None,
        after=_version_content_payload(content),
        alias=rel,
    )
    body = {
        "domain": domain,
        "path": rel,
        "saved": True,
        "overrides_existing": bool((result or {}).get("overrides_existing")) if isinstance(result, dict) else before is not None,
    }
    if consumers:
        body["reloaded"] = consumers
        body["note"] = (
            f"{len(consumers)} existing {domain}(s) built from this blueprint were "
            "reloaded and now run the new version."
        )
    return _tool_success(json.dumps(body, default=str)), "allowed", f"blueprint:{domain}/{rel}"


async def _execute_create_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    return await _save_blueprint(
        args, token, hass, data, tool_name="create_blueprint", allow_override=False)


async def _execute_edit_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    return await _save_blueprint(
        args, token, hass, data, tool_name="edit_blueprint", allow_override=True)


async def _execute_delete_blueprint(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    denied = _blueprint_denied(token, args, "delete_blueprint")
    if denied is not None:
        return denied
    pre, target, rel = _blueprint_write_precheck(
        args, hass, "delete_blueprint", needs_content=False)
    if pre is not None:
        return pre
    domain = str(args.get("domain") or "").strip().lower()
    before = await _read_blueprint_source(hass, target)
    if before is None:
        return _tool_error("Blueprint not found."), "not_found", "delete_blueprint"
    try:
        await async_ws_command(hass, "blueprint/delete", {"domain": domain, "path": rel})
    except WsDispatchError as exc:
        # HA refuses while any automation or script still uses the blueprint
        # (BlueprintInUse), which is a legitimate outcome, not a Phoenix failure.
        return _tool_error(f"Failed to delete blueprint: {exc}"), "invalid_request", "delete_blueprint"
    await _record_version(
        data, token, resource_type="blueprint", resource_id=f"{domain}/{rel}",
        action="delete", before=_version_content_payload(before), after=None, alias=rel,
    )
    return _tool_success(json.dumps({
        "domain": domain, "path": rel, "deleted": True,
    }, default=str)), "allowed", f"blueprint:{domain}/{rel}"
