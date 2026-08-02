"""Lovelace tools: dashboard CRUD, whole-layout writes and card-level edits.

The dashboard surface in one module. Everything here rides cap_lovelace_write,
including the reads: a layout names entities and their placement, which is the
same information the writes edit, so the read tools gate on the write
capability rather than a separate read one.

Two write shapes, deliberately both: set_dashboard_config replaces a whole
layout, and the three card ops carry one card each, because a whole-layout
replace forces the model to regurgitate the entire dashboard inside one tool
call's arguments, which blows weaker providers' output limits.
Both guard with an optimistic-lock content_hash, checked pre-gate and again at
apply time so a change made during the approval window is caught.

mcp_view owns the transport, the dispatch registry and the executor registry, and
imports the names it registers or calls from here. The dependency runs one way:
this module never imports the transport that dispatches it. Shared primitives
come from ..tool_common.
"""

from __future__ import annotations

import copy
import json
from typing import Any, TypeGuard

from homeassistant.core import HomeAssistant

from ..card_catalog import CardEntry
from ..const import CAP_DENY, MAX_DIFF_INLINE_BYTES
from ..data import PhoenixData
from ..helpers import (
    content_hash,
    dict_arg,
    diff_summary_fields as _summary,
    effective_cap,
    str_arg,
    version_summary_fields as _version_summary,
)
from ..policy_engine import filter_service_response
from ..token_store import TokenRecord
from ..tool_common import (
    _CAP_FORBIDDEN_MESSAGE,
    _cas_conflict,
    _gate,
    _record_version,
    _tool_error,
    _tool_success,
    _truncate,
)
from ..ws_dispatch import (
    WsDashboardNotFoundError,
    WsDispatchError,
    async_get_lovelace_config,
    async_save_lovelace_config,
    async_ws_command,
)


async def _tool_list_dashboards(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: list Lovelace dashboards."""
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "list_dashboards"
    try:
        result = await async_ws_command(hass, "lovelace/dashboards/list", {})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to list dashboards: {exc}"), "invalid_request", "list_dashboards"
    return _tool_success(json.dumps({"dashboards": result}, default=str)), "allowed", "list_dashboards"


def _card_row(entry: CardEntry, *, detailed: bool) -> dict:
    """Project one catalog entry. Lean by default, with the example only on request.

    The lean row carries what picking a card needs (type, what it is, whether it
    works); the stub config is what CONFIGURING one needs and is an order of
    magnitude larger. A well-stocked instance carries dozens of cards, so
    returning every example by default would spend most of a discovery call's
    budget on the ones the agent is not going to use. Same reasoning as the
    ESPHome automation catalog projection and get_state's lean/detailed split.
    """
    row: dict[str, Any] = {
        "type": f"custom:{entry.type}",
        "name": entry.name,
        "description": entry.description,
    }
    if entry.documentation_url:
        row["documentation_url"] = entry.documentation_url
    # Emitted only when true to the caller's disadvantage, the mesa_advisory
    # pattern: an available card is the norm and saying so on every row is noise,
    # but an unavailable one must never be silently indistinguishable.
    if not entry.available:
        row["available"] = False
        row["note"] = "Registered but its element did not load; do not use it."
    if detailed:
        row["has_visual_editor"] = entry.has_visual_editor
        if entry.stub_config is not None:
            row["example_config"] = entry.stub_config
    return row


async def _tool_list_dashboard_cards(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    """MCP tool: list the custom dashboard cards installed on this instance.

    Answers "what can I build with" before an agent authors a card, which it
    otherwise has to guess. The catalog is harvested by the panel frontend (see
    card_catalog.py for why it cannot be read off disk) and cached, so this tool
    is a pure read of that cache and never blocks on a browser.
    """
    tool = "list_dashboard_cards"
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", tool

    catalog = data.card_catalog.catalog
    wanted = str_arg(args.get("type")).strip()
    detailed = bool(args.get("detailed"))

    if not catalog.harvested:
        # NEVER report this as an empty catalog. An agent told the instance has
        # no custom cards will avoid every custom card on it, which is a worse
        # and less recoverable failure than admitting the catalog is not ready.
        return _tool_success(json.dumps({
            "harvested": False,
            "cards": [],
            "notice": (
                "The card catalog has not been harvested yet, so the installed custom "
                "cards are UNKNOWN (this is not the same as none being installed). Open "
                "the Phoenix MCP panel in a browser once to build it. Until then, prefer "
                "built-in Home Assistant card types."
            ),
        }, default=str)), "allowed", tool

    if wanted:
        entry = data.card_catalog.get(wanted)
        if entry is None:
            # Not an oracle: the catalog is instance-wide deployment metadata,
            # not entity state, and this token already holds cap_lovelace_write.
            return _tool_success(json.dumps({
                "harvested": True,
                "found": False,
                "type": wanted,
                "notice": "That card type is not installed on this instance.",
            }, default=str)), "allowed", tool
        return _tool_success(json.dumps({
            "harvested": True,
            "found": True,
            "card": _card_row(entry, detailed=True),
        }, default=str)), "allowed", tool

    rows = [_card_row(e, detailed=detailed) for e in catalog.entries]
    body: dict[str, Any] = {
        "harvested": True,
        "harvested_at": catalog.harvested_at,
        "count": len(rows),
        "cards": rows,
    }
    if not detailed:
        body["notice"] = (
            "Built-in Home Assistant card types are always available in addition to these. "
            "Call this tool with a type to get that card's example config before authoring it."
        )
    return _tool_success(json.dumps(body, default=str)), "allowed", tool


def _collect_card_types(node: Any, out: set[str]) -> None:
    """Walk a card or layout collecting every `custom:` type it names."""
    if isinstance(node, dict):
        value = node.get("type")
        if isinstance(value, str) and value.startswith("custom:"):
            out.add(value[len("custom:"):])
        for child in node.values():
            _collect_card_types(child, out)
    elif isinstance(node, list):
        for child in node:
            _collect_card_types(child, out)


def _card_warnings(payload: Any, data: PhoenixData) -> list[str]:
    """Advisory notes about custom cards this instance cannot render.

    ADVISORY, NEVER A REFUSAL. The catalog is a cache of what one browser could
    see, so failing the write closed would block a legitimate card whenever the
    cache is cold, stale by one plugin install, or harvested from a page that
    saw a partial registry. The cost of being wrong in that direction is a write
    the operator wanted being refused with no way to override; the cost in this
    direction is a warning next to a card that does render.

    Silent when the catalog has never been harvested: with nothing to compare
    against, every custom card would be reported as unknown, which trains the
    reader to ignore the field exactly when it starts being accurate.
    """
    catalog = data.card_catalog
    if not catalog.catalog.harvested:
        return []
    types: set[str] = set()
    _collect_card_types(payload, types)
    warnings: list[str] = []
    for card_type in sorted(types):
        entry = catalog.get(card_type)
        if entry is None:
            warnings.append(
                f"'custom:{card_type}' is not installed on this instance and will render as an "
                f"error. Call list_dashboard_cards to see what is available."
            )
        elif not entry.available:
            warnings.append(
                f"'custom:{card_type}' is registered but its element did not load, so it will "
                f"render as an error."
            )
    return warnings


async def _tool_create_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: create a dashboard (Confirm-gated)."""
    blocked = await _gate(
        "cap_lovelace_write", token, hass, data,
        tool_name="create_dashboard", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_dashboard("Create", args, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_create_dashboard(args, token, hass, data)


async def _execute_create_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    config = args.get("config")
    if not isinstance(config, dict) or not config:
        return _tool_error("config must be a non-empty object (at least url_path and title)."), "invalid_request", "create_dashboard"
    try:
        item = await async_ws_command(hass, "lovelace/dashboards/create", dict(config))
    except WsDispatchError as exc:
        return _tool_error(f"Failed to create dashboard: {exc}"), "invalid_request", "create_dashboard"
    return _tool_success(json.dumps({"dashboard": item}, default=str)), "allowed", "create_dashboard"


async def _tool_edit_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: edit a dashboard (Confirm-gated)."""
    blocked = await _gate(
        "cap_lovelace_write", token, hass, data,
        tool_name="edit_dashboard", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_dashboard("Edit", args, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_edit_dashboard(args, token, hass, data)


async def _execute_edit_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    dashboard_id = str(args.get("dashboard_id") or "").strip()
    config = args.get("config")
    if not dashboard_id:
        return _tool_error("dashboard_id is required."), "invalid_request", "edit_dashboard"
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "edit_dashboard"
    try:
        item = await async_ws_command(hass, "lovelace/dashboards/update", {"dashboard_id": dashboard_id, **config})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to edit dashboard: {exc}"), "invalid_request", "edit_dashboard"
    return _tool_success(json.dumps({"dashboard": item}, default=str)), "allowed", f"dashboard:{dashboard_id}"


async def _tool_delete_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: delete a dashboard (Confirm-gated)."""
    blocked = await _gate(
        "cap_lovelace_write", token, hass, data,
        tool_name="delete_dashboard", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_dashboard("Delete", args, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_delete_dashboard(args, token, hass, data)


async def _execute_delete_dashboard(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    dashboard_id = str(args.get("dashboard_id") or "").strip()
    if not dashboard_id:
        return _tool_error("dashboard_id is required."), "invalid_request", "delete_dashboard"
    try:
        await async_ws_command(hass, "lovelace/dashboards/delete", {"dashboard_id": dashboard_id})
    except WsDispatchError as exc:
        return _tool_error(f"Failed to delete dashboard: {exc}"), "invalid_request", "delete_dashboard"
    return _tool_success(f"Dashboard '{dashboard_id}' deleted successfully."), "allowed", f"dashboard:{dashboard_id}"


def _no_stored_config_message(url_path: str | None) -> str:
    """Explain an auto-generated (no stored config) dashboard, naming the target.

    Omitting url_path addresses the DEFAULT dashboard, which is commonly
    auto-generated. An agent that drops url_path mid-conversation would
    otherwise read an unnamed "this dashboard has no stored config" as
    contradicting the named dashboard it just read successfully. Naming which
    dashboard the message is about makes the wrong-target case self-evident.
    """
    if url_path is None:
        return (
            "The DEFAULT dashboard (no url_path was given) has no stored config "
            "(it is auto-generated). If you meant a specific dashboard, pass its "
            "url_path (see list_dashboards); otherwise use set_dashboard_config "
            "to store a layout for the default dashboard first."
        )
    return (
        f"Dashboard {url_path!r} has no stored config (it is auto-generated). "
        "Use set_dashboard_config to store a layout first."
    )


async def _tool_get_dashboard_config(
    args: dict, token: TokenRecord, hass: HomeAssistant
) -> tuple[dict, str, str]:
    """MCP tool: read a dashboard's view/card layout, entity ids redacted to scope."""
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        return _tool_error("Forbidden."), "denied", "get_dashboard_config"
    url_path = str(args.get("url_path") or "").strip() or None
    try:
        config = await async_get_lovelace_config(hass, url_path)
    except WsDispatchError as exc:
        return _tool_error(f"Could not read dashboard: {exc}"), "invalid_request", "get_dashboard_config"
    if config is None:
        return _tool_error(_no_stored_config_message(url_path)), "not_found", f"dashboard:{url_path or 'lovelace'}"
    redacted = filter_service_response(config, token, hass)
    # content_hash is over the RAW (unredacted) config the executor re-reads, so
    # redaction of the returned config does not affect the optimistic-lock guard.
    return _tool_success(json.dumps({
        "url_path": url_path, "config": redacted, "content_hash": content_hash(config),
    }, default=str)), "allowed", f"dashboard:{url_path or 'lovelace'}"


async def _build_diff_set_dashboard_config(args: dict, token: TokenRecord, hass: HomeAssistant) -> dict:
    config = dict_arg(args.get("config"))
    url_path = str(args.get("url_path") or "").strip() or None
    label = url_path or "(default dashboard)"
    views = config.get("views")
    before = None
    try:
        current = await async_get_lovelace_config(hass, url_path)
        if current is not None:
            # Redact out-of-scope entity ids the same way get_dashboard_config does.
            before = _truncate(json.dumps(filter_service_response(current, token, hass), indent=2, default=str))
    except Exception:  # noqa: BLE001 - diagnostic only; a new/auto dashboard has no before
        pass
    return {
        "kind": "yaml_diff",
        **_summary("set_dashboard_config", label=label),
        "target": {"type": "dashboard", "id": url_path, "label": label},
        "before": before,
        "after": _truncate(json.dumps(config, indent=2, default=str)),
        "preview": {"url_path": url_path, "views": len(views) if isinstance(views, list) else None},
    }


def _dashboard_write_precheck(args: dict, tool_name: str) -> tuple[dict, str, str] | None:
    """Pre-gate validation for dashboard writes; None means OK to proceed.

    Checks config is an object so a doomed request is rejected before a
    pending approval is created. The executor re-validates at apply time.
    """
    if not isinstance(args.get("config"), dict):
        return _tool_error("config must be an object."), "invalid_request", tool_name
    return None


async def _tool_set_dashboard_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None,
) -> tuple[dict, str, str]:
    """MCP tool: replace a dashboard's view/card layout (Confirm-gated)."""
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", "set_dashboard_config"
    pre = _dashboard_write_precheck(args, "set_dashboard_config")
    if pre is not None:
        return pre
    if args.get("expected_hash"):
        url_path = str(args.get("url_path") or "").strip() or None
        try:
            current = await async_get_lovelace_config(hass, url_path)
        except WsDashboardNotFoundError:
            current = None  # genuinely absent: any expected_hash conflicts
        except WsDispatchError as exc:
            # An unreadable lovelace must not be mistaken for an absent dashboard;
            # doing so would let the CAS guard pass against a phantom None.
            return _tool_error(
                f"Could not read dashboard: {exc}"
            ), "invalid_request", "set_dashboard_config"
        conflict = _cas_conflict(args.get("expected_hash"), current, "set_dashboard_config")
        if conflict is not None:
            return conflict
    blocked = await _gate(
        "cap_lovelace_write", token, hass, data,
        tool_name="set_dashboard_config", args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_set_dashboard_config(args, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_set_dashboard_config(args, token, hass, data)


async def _execute_set_dashboard_config(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    config = args.get("config")
    if not isinstance(config, dict):
        return _tool_error("config must be an object."), "invalid_request", "set_dashboard_config"
    url_path = str(args.get("url_path") or "").strip() or None
    resource_id = url_path or "lovelace"
    try:
        before = await async_get_lovelace_config(hass, url_path)
    except WsDashboardNotFoundError:
        before = None  # creating a dashboard config that does not exist yet
    except WsDispatchError as exc:
        # Not absence: writing now would record a phantom "create" and lose the
        # real prior layout from version history.
        return _tool_error(
            f"Could not read dashboard: {exc}"
        ), "invalid_request", "set_dashboard_config"
    # Optimistic-concurrency guard at apply time: catches a change made during the
    # read/approve window (an absent dashboard is None, so any expected_hash conflicts).
    conflict = _cas_conflict(args.get("expected_hash"), before, "set_dashboard_config")
    if conflict is not None:
        return conflict
    try:
        await async_save_lovelace_config(hass, url_path, config)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to save dashboard config: {exc}"), "denied", "set_dashboard_config"
    await _record_version(
        data, token, resource_type="dashboard", resource_id=resource_id,
        action="edit" if before is not None else "create",
        before=before, after=config, alias=url_path or "(default)",
    )
    body: dict[str, Any] = {"url_path": url_path, "saved": True}
    warnings = _card_warnings(config, data)
    if warnings:
        body["warnings"] = warnings
    return _tool_success(json.dumps(body, default=str)), "allowed", f"dashboard:{resource_id}"


def _card_index_ok(value: Any) -> TypeGuard[int]:
    """True for a real int index (bool subclasses int and must not pass).

    A TypeGuard rather than a plain bool so the range comparisons that follow
    every call are checkable: they only run once this has returned True, but
    without the guard the value still reads as Any | None there.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _card_shape_reason(card: Any) -> str | None:
    """Cheap structural check on an agent-supplied card config."""
    if not isinstance(card, dict) or not isinstance(card.get("type"), str) or not card["type"]:
        return "card must be an object with a non-empty type."
    return None


def _resolve_dashboard_cards(
    config: dict, view_index: Any, section_index: Any
) -> tuple[list | None, str | None]:
    """Locate the cards list addressed by view_index (+ section_index).

    Callers pass a deep copy, never the live lovelace config object: the cards
    list is created on the target view/section when absent (so an add can
    target a view with no cards yet), which is a mutation. Returns
    (cards, None) on success or (None, reason) for an unaddressable target.
    """
    if "strategy" in config:
        return None, "This dashboard is strategy-generated; it has no editable card list."
    views = config.get("views")
    if not isinstance(views, list) or not views:
        return None, "This dashboard has no views."
    if not _card_index_ok(view_index) or not 0 <= view_index < len(views):
        return None, f"view_index must be an integer in 0..{len(views) - 1}."
    view = views[view_index]
    if not isinstance(view, dict):
        return None, "The target view is not a mapping."
    if "strategy" in view:
        return None, "The target view is strategy-generated; it has no editable card list."
    sections = view.get("sections")
    if isinstance(sections, list):
        if section_index is None:
            return None, f"View {view_index} uses sections; pass section_index (0..{len(sections) - 1})."
        if not _card_index_ok(section_index) or not 0 <= section_index < len(sections):
            return None, f"section_index must be an integer in 0..{len(sections) - 1}."
        section = sections[section_index]
        if not isinstance(section, dict):
            return None, "The target section is not a mapping."
        cards = section.setdefault("cards", [])
    else:
        if section_index is not None:
            return None, f"View {view_index} has no sections; omit section_index."
        cards = view.setdefault("cards", [])
    if not isinstance(cards, list):
        return None, "The target view's cards entry is not a list."
    return cards, None


def _apply_dashboard_card_op(
    config: dict, op: str, args: dict
) -> tuple[dict | None, Any, str | None]:
    """Apply one card-level op ("add" | "edit" | "delete") to a copy of config.

    Returns (new_config, prior_card, None) on success or (None, None, reason).
    prior_card is the card an edit replaced or a delete removed (None for add).
    Always deep-copies first: config may be the object the lovelace integration
    holds live, which must never be mutated in place.
    """
    new_config = copy.deepcopy(config)
    cards, reason = _resolve_dashboard_cards(new_config, args.get("view_index"), args.get("section_index"))
    if cards is None:
        return None, None, reason
    if op == "add":
        card = args.get("card")
        reason = _card_shape_reason(card)
        if reason is not None:
            return None, None, reason
        position = args.get("position")
        if position is None:
            cards.append(card)
        else:
            if not _card_index_ok(position) or not 0 <= position <= len(cards):
                return None, None, f"position must be an integer in 0..{len(cards)}."
            cards.insert(position, card)
        return new_config, None, None
    card_index = args.get("card_index")
    if not _card_index_ok(card_index) or not 0 <= card_index < len(cards):
        if not cards:
            return None, None, "The target card list is empty."
        return None, None, f"card_index must be an integer in 0..{len(cards) - 1}."
    prior = cards[card_index]
    if op == "edit":
        card = args.get("card")
        reason = _card_shape_reason(card)
        if reason is not None:
            return None, None, reason
        cards[card_index] = card
    else:
        cards.pop(card_index)
    return new_config, prior, None


async def _dashboard_card_current(
    args: dict, hass: HomeAssistant, tool_name: str
) -> dict | tuple[dict, str, str]:
    """Read the target dashboard's current stored config for a card-level op.

    Returns the config, or the tool error to return. Single-return like
    _read_body and _resolve_radio_device; the pair form guaranteed a non-None
    config only via the other element. A card op needs a stored
    base layout, so an auto-generated dashboard is an invalid_request steering
    to set_dashboard_config; a CAS mismatch (expected_hash supplied and stale)
    is refused here too. Used both pre-gate and by the executor.
    """
    url_path = str(args.get("url_path") or "").strip() or None
    try:
        current = await async_get_lovelace_config(hass, url_path)
    except WsDispatchError as exc:
        return _tool_error(f"Could not read dashboard: {exc}"), "invalid_request", tool_name
    if not isinstance(current, dict):
        return (
            _tool_error(_no_stored_config_message(url_path)),
            "invalid_request", tool_name,
        )
    conflict = _cas_conflict(args.get("expected_hash"), current, tool_name)
    if conflict is not None:
        return conflict
    return current


# Version-summary key per card op, so the Changes tab's "added/edited/deleted"
# verb is translatable rather than an English word spliced in at record time.
_CARD_OP_VERSION_KEYS = {"add": "card.added", "edit": "card.edited", "delete": "card.deleted"}


async def _build_diff_dashboard_card(
    args: dict, op: str, token: TokenRecord, hass: HomeAssistant
) -> dict:
    url_path = str(args.get("url_path") or "").strip() or None
    label = url_path or "(default dashboard)"
    view_index = args.get("view_index")
    section_index = args.get("section_index")
    raw_card = args.get("card")
    # None, not {}: a delete op has no card, and the diff must show no `after`.
    card = raw_card if isinstance(raw_card, dict) else None
    before = None
    prior = None
    try:
        current = await async_get_lovelace_config(hass, url_path)
        if isinstance(current, dict):
            _, prior, _ = _apply_dashboard_card_op(current, op, args)
    except Exception:  # noqa: BLE001 - diagnostic only; the precheck already validated
        pass
    if prior is not None:
        # Redact out-of-scope entity ids the same way get_dashboard_config does.
        # Card diffs truncate at the version-snapshot bound, not the 4000-char
        # display default: the panel PARSES diff.before to render the Before
        # side of the live preview, and a real card (a multi-series chart) can
        # alone exceed 4000 chars (live-observed), which broke that parse.
        before = _truncate(
            json.dumps(filter_service_response(prior, token, hass), indent=2, default=str),
            max_chars=MAX_DIFF_INLINE_BYTES,
        )
    card_type = (card or {}).get("type") if op != "delete" else (prior or {}).get("type") if isinstance(prior, dict) else None
    _card_key = f"dashboard_card.{op}" + (".section" if section_index is not None else "")
    _card_params: dict[str, Any] = {"label": label, "view_index": view_index}
    if section_index is not None:
        _card_params["section_index"] = section_index
    if op == "add":
        _card_params["card_type"] = card_type or "card"
    else:
        _card_params["card_index"] = args.get("card_index")
    preview: dict[str, Any] = {"url_path": url_path, "view_index": view_index}
    if section_index is not None:
        preview["section_index"] = section_index
    if op == "add":
        preview["position"] = args.get("position") if args.get("position") is not None else "append"
    else:
        preview["card_index"] = args.get("card_index")
    if card_type:
        preview["card_type"] = card_type
    return {
        "kind": "yaml_diff",
        **_summary(_card_key, **_card_params),
        "target": {"type": "dashboard", "id": url_path, "label": label},
        "before": before,
        "after": _truncate(json.dumps(card, indent=2, default=str), max_chars=MAX_DIFF_INLINE_BYTES) if card is not None else None,
        "preview": preview,
    }


async def _tool_dashboard_card(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData,
    request_id: str = "", client_ip: str | None = None, *, op: str, tool_name: str,
) -> tuple[dict, str, str]:
    """MCP tool: card-level dashboard edit (Confirm-gated).

    Whole-layout replace forces the model to regurgitate the entire dashboard
    inside one tool call's JSON arguments, which blows weaker providers' output
    limits; these ops carry only the one card. The full op is validated
    pre-gate against the current layout, so a structurally doomed op never
    becomes a pending approval, and the executor re-reads and re-validates at
    apply time.
    """
    if effective_cap(token, "cap_lovelace_write") == CAP_DENY:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool_name
    current = await _dashboard_card_current(args, hass, tool_name)
    if isinstance(current, tuple):
        return current
    _, _, reason = _apply_dashboard_card_op(current, op, args)
    if reason is not None:
        return _tool_error(reason), "invalid_request", tool_name
    blocked = await _gate(
        "cap_lovelace_write", token, hass, data,
        tool_name=tool_name, args=args, request_id=request_id,
        client_ip=client_ip, diff=lambda: _build_diff_dashboard_card(args, op, token, hass),
    )
    if blocked is not None:
        return blocked
    return await _execute_dashboard_card(args, op, tool_name, token, hass, data)


async def _execute_dashboard_card(
    args: dict, op: str, tool_name: str, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    current = await _dashboard_card_current(args, hass, tool_name)
    if isinstance(current, tuple):
        return current
    new_config, prior, reason = _apply_dashboard_card_op(current, op, args)
    if reason is not None or new_config is None:
        return _tool_error(reason or "Invalid card operation."), "invalid_request", tool_name
    url_path = str(args.get("url_path") or "").strip() or None
    resource_id = url_path or "lovelace"
    try:
        await async_save_lovelace_config(hass, url_path, new_config)
    except WsDispatchError as exc:
        return _tool_error(f"Failed to save dashboard config: {exc}"), "denied", tool_name
    # The version record's full layout snapshot cannot say WHICH card moved, so
    # the summary carries the op context for the Changes list.
    raw_card = args.get("card")
    # None, not {}: a delete op has no card, and the diff must show no `after`.
    card = raw_card if isinstance(raw_card, dict) else None
    subject = (card or prior or {}).get("type") or "card"
    parts = [_version_summary("loc.view", index=args.get("view_index"))["summary"]]
    if args.get("section_index") is not None:
        parts.append(_version_summary("loc.section", index=args.get("section_index"))["summary"])
    if op != "add":
        parts.append(_version_summary("loc.card", index=args.get("card_index"))["summary"])
    await _record_version(
        data, token, resource_type="dashboard", resource_id=resource_id,
        action="edit", before=current, after=new_config, alias=url_path or "(default)",
        summary=_version_summary(
            _CARD_OP_VERSION_KEYS[op], subject=subject, where=", ".join(str(x) for x in parts),
        ),
    )
    body: dict[str, Any] = {
        "url_path": url_path, "saved": True, "op": op,
        "view_index": args.get("view_index"),
        # Unlike a whole-blob replace (which must be re-read and merged after a
        # conflict), a card op is structural, so the resulting layout's hash is
        # safe to hand back: the agent can chain further card ops on this
        # dashboard without another read.
        "content_hash": content_hash(new_config),
    }
    if args.get("section_index") is not None:
        body["section_index"] = args.get("section_index")
    # Emitted only when non-empty, the mesa_advisory convention. A delete names
    # no new card, so only add/edit can warn.
    warnings = _card_warnings(args.get("card"), data) if op in ("add", "edit") else []
    if warnings:
        body["warnings"] = warnings
    return _tool_success(json.dumps(body, default=str)), "allowed", f"dashboard:{resource_id}"


async def _execute_add_dashboard_card(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    return await _execute_dashboard_card(args, "add", "add_dashboard_card", token, hass, data)


async def _execute_edit_dashboard_card(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    return await _execute_dashboard_card(args, "edit", "edit_dashboard_card", token, hass, data)


async def _execute_delete_dashboard_card(
    args: dict, token: TokenRecord, hass: HomeAssistant, data: PhoenixData
) -> tuple[dict, str, str]:
    return await _execute_dashboard_card(args, "delete", "delete_dashboard_card", token, hass, data)


def _build_diff_dashboard(verb: str, args: dict, hass: HomeAssistant) -> dict:
    dashboard_id = str(args.get("dashboard_id") or "").strip()
    config = dict_arg(args.get("config"))
    label = config.get("title") or dashboard_id or config.get("url_path")
    kind = "config_diff" if verb == "Create" else ("yaml_diff" if verb == "Edit" else "system_action")
    diff: dict = {
        "kind": kind,
        **_summary(f"dashboard.{verb.lower()}", label=label),
        "target": {"type": "dashboard", "id": dashboard_id or None, "label": label},
        "preview": {"url_path": config.get("url_path"), "title": config.get("title")},
    }
    if verb != "Delete":
        diff["after"] = _truncate(json.dumps(config, indent=2, default=str))
    else:
        diff["preview"]["warning"] = "This dashboard will be removed permanently."
    return diff
