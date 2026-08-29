"""Shared primitives for the MCP tool surface.

The pieces every tool handler needs regardless of domain: the MCP content
envelopes (_tool_success/_tool_error/_tool_pending), the capability gate and its
inline-confirm wait, the progress bus the SSE writer drains, configuration
version capture, and the scoped file read/write plus compare-and-swap helpers.

These live here rather than in mcp_view so a per-domain tool module (see
tools/) can depend on them without importing the transport that dispatches it,
which would be a cycle. mcp_view re-exports every name here.
"""


import asyncio
import dataclasses
import json
from contextvars import ContextVar
import logging
import os
from typing import Any

from homeassistant.util.file import write_utf8_file_atomic as _write_utf8_file_atomic
from homeassistant.core import HomeAssistant, callback

from .const import (
    DOMAIN,
    MAX_DIFF_INLINE_BYTES,
    MAX_FILE_BYTES,
    MAX_CONFIRM_INLINE_WAIT_SECONDS,
    REDACTION_SENTINEL,
)
from .data import PhoenixData
from .mesa import mesa_confirm_preview
from .helpers import (
    content_hash,
    DiffSource,
    async_evaluate_capability,
    version_summary_fields as _version_summary,
)
from .token_store import TokenRecord

_LOGGER = logging.getLogger(__name__)


def _tool_success(text: str) -> dict:
    """Return an MCP tool result content block with a plain-text payload."""
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message: str) -> dict:
    """Return an MCP tool result content block indicating an error."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _tool_pending(approval: Any, waited_seconds: int | None = None) -> dict:
    """Return an MCP tool result indicating a pending admin approval.

    isError is False because pending is a valid outcome, not a failure.

    THE WORDING IS LOAD-BEARING AND HAS TO MATCH THE STAGING MODEL. Approvals
    now queue and are cleared in one operator action, so the agent's job after
    a gate is to KEEP GOING and collect the outcomes together at the end. This
    message told it the opposite twice over: it pointed at wait_for_approval
    with "this approval_id", singular, from before that tool took a LIST, so an
    agent following it literally serialises on the first id and no later
    approval can even be created until that one stops waiting; and "you may
    tell the user it is pending and finish now" reads as an instruction to
    stop, which is the early-return-reads-as-success failure. Both were written
    when the inline wait was on by default and every gate resolved one at a
    time. Any edit here must keep the order: continue first, wait ONCE second,
    finish only if nothing depends on the outcome.

    `waited_seconds` is set only on the inline-wait timeout path and states a
    FACT rather than giving different advice: that hold already spent its
    budget without resolving, so an agent that immediately blocks on the same
    id is very likely to spend another. The advice itself is identical in all
    three paths that reach here, deliberately, because the agent cannot see
    which one it is in (the inline wait is a token setting) and the right next
    move does not depend on it.
    """
    waited = (
        f"This call already held for {waited_seconds} seconds without resolving, so the operator "
        "is not reviewing in real time right now. "
        if waited_seconds
        else ""
    )
    body = json.dumps({
        "status": "pending_approval",
        "approval_id": approval.id,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "review_url": f"/phoenix-mcp/approvals/{approval.id}",
        "message": (
            f"{waited}This action is queued for admin approval and will be applied once approved; "
            "the admin has been notified. Do not retry it. Continue with your remaining steps: "
            "further approval-gated calls queue alongside this one and the admin can clear the "
            "whole queue in a single action, so do NOT stop or wait after each one. When you "
            "genuinely need the outcomes, call wait_for_approval ONCE with approval_ids listing "
            "every approval you are waiting on (or get_approval_status for a one-shot check) to "
            "learn which were approved or rejected, and any reason. If nothing you have left to "
            "do depends on them, tell the user what is pending and finish."
        ),
    })
    return {"content": [{"type": "text", "text": body}]}


def _tool_inline_resolved(approval: Any) -> dict:
    """Result for an inline-waited confirm that resolved without applying.

    Used when a token with confirm_inline_wait_seconds set waited out the gate
    and the approval came back rejected or cancelled: tell the agent the outcome
    (with any reason) and that it must not retry, rather than handing back a
    stale pending stub. isError is True because the action did not happen.
    An admin APPROVAL whose executor then failed lands here too (the approve
    path stores the error result and resolves the record as failed with the
    "execution_failed" slug); that case must read as a failure to fix, never
    as a refusal, or the agent treats an approval as a refusal and loops
    retrying the same doomed call.
    """
    reason = getattr(approval, "rejected_reason", None)
    if reason == "execution_failed":
        result = getattr(approval, "result", None)
        tool_result = result.get("tool_result") if isinstance(result, dict) else None
        parts = [
            item.get("text", "")
            for item in (tool_result or {}).get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        failure = " ".join(p for p in parts if p).strip()
        if failure:
            body = json.dumps({
                "status": "execution_failed",
                "approval_id": approval.id,
                "message": (
                    f"The operator APPROVED this action, but executing it failed: {failure} "
                    "Do not retry the same call unchanged; fix the reported cause first "
                    "(for example, if it reports a stale expected_hash, re-read the "
                    "resource for the current content_hash)."
                ),
            })
            return {"content": [{"type": "text", "text": body}], "isError": True}
    if approval.status == "failed" and reason == "execution_interrupted":
        body = json.dumps({
            "status": "failed",
            "approval_id": approval.id,
            "message": (
                "The operator APPROVED this action and execution started, but its "
                "final outcome was interrupted. It may have partly applied. Check "
                "the real-world result before proposing any retry."
            ),
        })
        return {"content": [{"type": "text", "text": body}], "isError": True}
    # A rejection with a reason is the operator steering the next proposal, not
    # a stop signal (iterating through reject-with-reason rounds is the normal
    # card-building workflow; a stricter forbid-variations wording live-tested
    # badly, see agentcli._resolved_result). Reasonless rejection = ask first.
    if approval.status == "rejected" and reason:
        message = (
            f"This action was not applied (it was rejected). Reason: {reason}. "
            "That reason is the operator's direction for what to do next: "
            "address it in your next proposal instead of resubmitting the "
            "same change."
        )
    elif approval.status == "rejected":
        message = (
            "This action was not applied (it was rejected, no reason given). "
            "Do not resubmit the same change; ask the operator how they "
            "would like to proceed."
        )
    else:
        detail = f" Reason: {reason}." if reason else ""
        message = (
            f"This action was not applied (it was {approval.status}).{detail} "
            "Do not retry it; report the outcome to the user."
        )
    body = json.dumps({
        "status": approval.status,
        "approval_id": approval.id,
        "message": message,
    })
    return {"content": [{"type": "text", "text": body}], "isError": True}


def _approval_resource(approval: Any) -> str:
    """Resource string used in audit logs for a pending-approval entry."""
    return f"approval:{approval.tool_name}:{approval.id}"


@dataclasses.dataclass
class _ProgressBus:
    """What an in-flight SSE-framed request reports while it is still running.

    Single-writer by design: only the response writer touches the stream, and
    handler code deep in a tool only sets fields here. Two coroutines writing to
    one StreamResponse would interleave frames, so this trades a shared object for
    a class of bug that does not exist.

    `token` is the client's MCP progressToken. It is None unless the client asked
    for progress on THIS request, and no token means no progress notifications are
    ever emitted (the spec forbids unsolicited ones).
    """

    token: str | int | None = None
    total: float | None = None
    status: str | None = None
    status_key: str | None = None
    status_params: dict[str, Any] | None = None


# Set by the SSE writer before the dispatch task is created, so the task's copied
# context carries the same bus object the writer holds. Absent on the plain JSON
# path, where nothing can be sent before the response.
_progress_ctx: ContextVar[_ProgressBus | None] = ContextVar("phoenix_mcp_progress", default=None)


def _set_progress_status(
    status: str | None,
    total: float | None = None,
    *,
    key: str | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Describe what the current request is waiting on, for the next SSE tick.

    A no-op unless this request is SSE-framed and the client supplied a
    progressToken, so callers need no conditionals of their own.
    """
    bus = _progress_ctx.get()
    if bus is not None:
        bus.status = status
        bus.total = total
        bus.status_key = key
        bus.status_params = params


# Uniform message for a capability-denied tool call. A denied call must look the
# same regardless of any entity it names, so it can never be used as a scope or
# existence oracle. Shared by _gate and any pre-gate deny short-circuit.
_CAP_FORBIDDEN_MESSAGE = (
    "Forbidden: this capability is not enabled for this token. It may have changed "
    "since you connected; call get_capability_summary for the current state, refresh "
    "your tool list (reconnect) if it changed, or ask the operator to grant it."
)


# Appended to the executed result when an interactive wait returns an
# operator-APPROVED action. Live-found: without it, an agent iterating toward a
# goal reads an approval as merely "this step landed" and keeps proposing
# variations of a change the operator already reviewed and settled on. A second
# live loop shaped the wording: a model whose card landed on a different view
# than it intended (its own arguments were off) filed corrective re-adds
# through two explicit rejections, so the note forbids corrective follow-ups
# outright and makes a noticed discrepancy a say-it-and-stop, not a fix-it.
_OPERATOR_ACCEPTED_NOTE = (
    "Note: the operator reviewed this exact change and approved it. What was "
    "applied, exactly as it landed, is what the operator accepted: treat it as "
    "final. Do not revise, move, replace, or re-attempt it, and do not file a "
    "corrective follow-up, even if the result differs from what you intended or "
    "seems to fall short of the request. If you see a discrepancy, say so in one "
    "sentence and stop; the operator will tell you if they want it changed. Only "
    "untouched steps the operator explicitly asked for may continue."
)


def _operator_accepted_result(tool_result: dict) -> dict:
    """The executed result of an operator-approved action, with the accepted
    note appended as an extra text content item (same in-band pattern as
    _STALE_TOOLS_ADVISORY). Shallow-copies so the stored approval record's
    result is never mutated."""
    out = dict(tool_result)
    out["content"] = list(out.get("content") or []) + [
        {"type": "text", "text": _OPERATOR_ACCEPTED_NOTE}
    ]
    return out


async def _gate(
    cap_name: str,
    token: TokenRecord,
    hass: HomeAssistant,
    data: PhoenixData,
    *,
    tool_name: str,
    args: dict,
    request_id: str,
    client_ip: str | None,
    diff: DiffSource,
) -> tuple[dict, str, str] | None:
    """Run capability gating. Returns a response tuple for deny/pending, or None for allow.

    For deny, returns (error_dict, "denied", tool_name).
    For pending, returns (pending_dict, "pending_approval", approval_resource).
    For allow, returns None and the caller proceeds with the side effect.

    diff should be passed as a zero-arg builder (`diff=lambda: _build_diff_x(...)`)
    rather than an already-awaited dict. Only the Confirm path reads it, so a
    builder keeps an allowed or denied call from paying for a config read, a
    lovelace load, or an ESPHome add-on round trip whose result is discarded.
    """
    result = await async_evaluate_capability(
        cap_name, token, hass, data,
        tool_name=tool_name, args=args, request_id=request_id,
        client_ip=client_ip, diff=diff,
    )
    if result.is_deny:
        return _tool_error(_CAP_FORBIDDEN_MESSAGE), "denied", tool_name
    if result.is_pending:
        return await _pending_or_inline(hass, data, token, result.approval)
    return None


async def _await_inline_confirm(
    hass: HomeAssistant, data: PhoenixData, token: TokenRecord, approval: Any,
) -> tuple[dict, str, str]:
    """Optionally hold the response open for a confirm gate to resolve inline.

    When the token opts in (confirm_inline_wait_seconds > 0), block up to that
    many seconds (capped at MAX_CONFIRM_INLINE_WAIT_SECONDS) on the same
    phoenix_mcp_approval_resolved event wait_for_approval uses. On approval within the
    window, return the executed tool result the agent would otherwise have to
    poll for; on rejection, tell it the action did not apply; on timeout, fall
    back to the normal immediate pending_approval reply. The original request's
    audit outcome stays "pending_approval" in every case (the resolution is
    audited separately by the approve/reject path), so enabling the wait changes
    only what the agent receives, never how the request is logged.
    """
    from .approvals import (  # noqa: PLC0415
        STATUS_APPROVED,
        STATUS_PENDING,
        get_approval,
    )

    wait = min(token.confirm_inline_wait_seconds, MAX_CONFIRM_INLINE_WAIT_SECONDS)
    approval_id = approval.id
    future: asyncio.Future = hass.loop.create_future()

    @callback
    def _on_resolved(event: Any) -> None:
        if event.data.get("approval_id") == approval_id and not future.done():
            future.set_result(True)

    unsub = hass.bus.async_listen(f"{DOMAIN}_approval_resolved", _on_resolved)
    # This is the longest hold in the whole surface. On an SSE-framed request the
    # keepalive ticks now carry the reason, so the client shows "waiting for the
    # operator" instead of an unexplained silence.
    _set_progress_status(
        f"Waiting for operator approval: {approval.tool_name}", total=float(wait),
        key="agentchat.progress.waitingApproval",
        params={"name": approval.tool_name},
    )
    try:
        await asyncio.wait_for(future, wait)
    except TimeoutError:
        # The one place the agent is told how long it already held, so it does
        # not immediately block on the same id and spend the budget twice.
        return _tool_pending(approval, waited_seconds=wait), "pending_approval", _approval_resource(approval)
    finally:
        unsub()
        _set_progress_status(None)

    latest = get_approval(data.store, approval_id)
    resource = _approval_resource(latest or approval)
    if latest is None or latest.status == STATUS_PENDING:
        # Event fired but the record is not terminal (a rare race): behave as if
        # it timed out, so the agent still gets a usable pending reply.
        return _tool_pending(approval), "pending_approval", resource
    if latest.status == STATUS_APPROVED and latest.result:
        tool_result = latest.result.get("tool_result")
        if tool_result is not None:
            return _operator_accepted_result(tool_result), "pending_approval", resource
    # Rejected, cancelled, or approved with no stored result: report the outcome
    # without re-queuing (the original action must never be retried).
    return _tool_inline_resolved(latest), "pending_approval", resource


async def _pending_or_inline(
    hass: HomeAssistant,
    data: PhoenixData,
    token: TokenRecord,
    approval: Any,
) -> tuple[dict, str, str]:
    """A pending approval, held inline when the token is configured to wait.

    ONE definition, because there is more than one kind of confirm gate and they
    must not answer differently. `_gate` had this logic inline for a CAPABILITY
    confirm; the MESA confirm gates (call_service and the native tools) reached
    their own `_tool_pending` returns and never consulted
    `confirm_inline_wait_seconds` at all. So an operator who configured an inline
    wait got it for one kind of confirm and not the other, which is not a
    distinction anything states or intends.

    The second consequence was the worse one: `_OPERATOR_ACCEPTED_NOTE` is
    appended only when an approval resolves INLINE (here) or through Agent Chat,
    so a MESA confirm could never deliver it. That note exists to stop an agent
    revising a change the operator already accepted, which is exactly the loop a
    MESA-gated actuation invites.

    THE WAIT IS SKIPPED WHEN A BACKLOG ALREADY EXISTS, and that is what makes
    batch approval usable at all. The hold is a per-request block, and tool calls
    arrive one at a time, so approval N+1 cannot even be CREATED until approval N
    has finished waiting. Live-measured at a 60s wait: consecutive approvals
    appeared 62.8 seconds apart, i.e. a twenty-write migration would have taken
    twenty minutes just to fill the queue an operator is being asked to review in
    one go. The wait and batching were working against each other.

    So: if this token already has another approval pending, do not hold. The
    reasoning is not merely mechanical - an operator with something already
    unresolved is demonstrably not in instant-approve mode, so blocking buys the
    agent nothing and costs the queue everything. The FIRST call still waits,
    which is what preserves the interactive feel for a lone confirm, and the
    instant-approve workflow is untouched: there each approval resolves within
    seconds, so the queue is empty again by the next call and every call still
    waits. Twenty writes go from ~20 minutes to ~60 seconds.

    Inferred rather than declared by the caller deliberately. An explicit per-call
    flag would have to land on 40-odd write tools (moving the catalog fingerprint
    for every connected client), or ride in `params._meta`, which not every client
    can set; and an agent that forgot it would silently reintroduce the stall.
    """
    if token.confirm_inline_wait_seconds > 0 and not _token_has_other_pending(data, token, approval):
        return await _await_inline_confirm(hass, data, token, approval)
    return _tool_pending(approval), "pending_approval", _approval_resource(approval)


def _token_has_other_pending(data: PhoenixData, token: TokenRecord, approval: Any) -> bool:
    """True when this token has a pending approval OTHER than this one.

    Excluding the current approval is load-bearing: `async_evaluate_capability`
    has already created and stored it by the time we get here, so counting it
    would make the queue never look empty and no call would ever wait.

    Fail-quiet: an unreadable store means we cannot prove a backlog, and the safe
    default is the existing behaviour (wait), not a silent change of it.
    """
    from .approvals import STATUS_PENDING, list_approvals  # noqa: PLC0415

    try:
        pending = list_approvals(data.store, status=STATUS_PENDING, token_id=token.id)
    except Exception:  # noqa: BLE001 - advisory only; never fail a gate over this
        _LOGGER.debug("Could not read pending approvals for %s", token.id, exc_info=True)
        return False
    return any(entry.id != approval.id for entry in pending)


# Set by an admin restore (admin_view) around a re-applied executor call, so the
# executor's own capture is stamped as a "rollback" attributed to the admin rather
# than a plain create/edit. asyncio-safe: the value is scoped to the restore task.
_restore_ctx: ContextVar[dict | None] = ContextVar("phx_restore_ctx", default=None)


def _dashboard_card_total(config: dict | None) -> int | None:
    """Total cards across a dashboard layout's views and sections, or None."""
    if not isinstance(config, dict) or not isinstance(config.get("views"), list):
        return None
    total = 0
    for view in config["views"]:
        if not isinstance(view, dict):
            continue
        if isinstance(view.get("cards"), list):
            total += len(view["cards"])
        if isinstance(view.get("sections"), list):
            for section in view["sections"]:
                if isinstance(section, dict) and isinstance(section.get("cards"), list):
                    total += len(section["cards"])
    return total


def _fmt_bytes(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"


def _payload_size(payload: dict | None) -> int | None:
    """Byte size of a raw-content version payload (inline or truncated marker)."""
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    size = payload.get("bytes")
    return size if isinstance(size, int) else None


def _version_change_summary(
    resource_type: str, before: dict | None, after: dict | None
) -> dict[str, Any] | None:
    """A one-line description of what a version record changed.

    Only for resource types whose alias/resource_id says nothing about the
    change itself: dashboards (card-count movement), raw yaml/file writes
    (size movement), and entity-registry edits (which fields changed).
    Automations/scripts/scenes/helpers return None; their alias is the story.

    Returns the summary/summary_key/summary_params triple to splat into the
    version record, or None when this resource type has nothing to say.
    """
    if resource_type == "dashboard":
        b, a = _dashboard_card_total(before), _dashboard_card_total(after)
        if a is not None and b is not None and a != b:
            return _version_summary("cards.was", count=a, before=b)
        if a is not None:
            return _version_summary("cards", count=a)
        return None
    if resource_type in ("yaml_config", "file"):
        b, a = _payload_size(before), _payload_size(after)
        if a is not None and b is not None:
            return (
                _version_summary("size.was", size=_fmt_bytes(a), before=_fmt_bytes(b))
                if a != b else _version_summary("size", size=_fmt_bytes(a))
            )
        if a is not None:
            return _version_summary("size", size=_fmt_bytes(a))
        return None
    if resource_type in ("entity", "device"):
        if after is None:
            return _version_summary(f"{resource_type}.removed")
        if before is None:
            return None
        changed = sorted(
            k for k in set(before) | set(after) if before.get(k) != after.get(k)
        )
        return _version_summary(
            f"{resource_type}.changed", fields=", ".join(changed)
        ) if changed else None
    if resource_type == "config_entry" and after is None and before is not None:
        if before.get("restorable") is False:
            return _version_summary("config_entry.removed")
    return None


async def _record_version(
    data: PhoenixData,
    token: TokenRecord,
    *,
    resource_type: str,
    resource_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    alias: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    """Best-effort capture of a config change into the version history.

    Called from each _execute_* at Phoenix MCP's single execution chokepoint, so it
    records both directly-allowed and Confirm-approved changes exactly once.
    Never raises: a version-store failure must not fail the user's actual write.
    request_id is intentionally not threaded (versions correlate to the audit log
    by token + resource + timestamp); approved_by_user_id is set only on the admin
    restore path. summary is a one-line what-changed description for the panel's
    Changes list; callers with op context (the card tools) pass their own, and
    everything else gets the computed default (which is None for resource types
    whose alias already tells the story).
    """
    ctx = _restore_ctx.get()
    approved_by_user_id = None
    if ctx is not None:
        # Admin restore: this reused create/edit is really a rollback by an admin.
        action = "rollback"
        approved_by_user_id = ctx.get("user_id")
    try:
        if summary is None:
            summary = _version_change_summary(resource_type, before, after)
        await data.versions.record(
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            before=before,
            after=after,
            alias=alias,
            token_id=token.id,
            token_name=token.name,
            approved_by_user_id=approved_by_user_id,
            **(summary or {}),
        )
    except Exception:  # noqa: BLE001 - history capture must never break a write
        _LOGGER.exception(
            "Failed to record %s version for %s %s", action, resource_type, resource_id
        )
        return
    # Let the admin panel's Changes tab refresh instantly instead of waiting for
    # its poll. Best-effort: a missing hass (tests) just falls back to polling.
    if data.hass is not None:
        data.hass.bus.async_fire(
            "phoenix_mcp_config_changed",
            {"resource_type": resource_type, "resource_id": resource_id, "action": action},
        )


def _version_content_payload(content: str | None, **extra: Any) -> dict | None:
    """Build a before/after payload for raw file / configuration.yaml version capture.

    Stores the content verbatim when it is within MAX_DIFF_INLINE_BYTES so an admin
    can restore it later; larger content is recorded as a non-restorable marker
    (metadata only) to bound .storage growth. Returns None when there was no prior
    content (a fresh create). Content is stored raw, like other version records, so
    the restore is byte-faithful; the version store is admin-only and lives in
    .storage alongside the rest of Phoenix MCP's at-rest data.
    """
    if content is None:
        return None
    payload: dict = {**extra}
    size = len(content.encode("utf-8"))
    if size > MAX_DIFF_INLINE_BYTES:
        payload.update({"content": None, "truncated": True, "bytes": size})
    else:
        payload["content"] = content
    return payload


def _usable_path_arg(value: Any) -> str | None:
    """A caller-supplied path argument, or None when it can never name a file.

    The NUL check is why this exists rather than a bare isinstance at each jail:
    os.path.realpath RAISES ValueError on an embedded NUL instead of returning
    something the containment check could refuse, so the classic truncation
    probe escapes every path jail below as an unhandled error rather than the
    clean refusal each one promises.
    """
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return value


def _read_text_capped(target: str) -> str:
    if os.path.getsize(target) > MAX_FILE_BYTES:
        raise ValueError("file too large")
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


def _write_text_atomic(target: str, content: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _write_utf8_file_atomic(target, content)


def _cas_conflict(
    expected: Any, current: str | dict | list | None, tool_name: str
) -> tuple[dict, str, str] | None:
    """Optimistic-concurrency check for a Confirm-gated whole-blob write.

    When the caller supplied expected_hash (from the matching read tool), refuse
    the write if the target's current content no longer hashes to it, meaning an
    admin or another agent changed it since the read. This runs both before
    gating (fail fast, so a stale write never becomes a doomed pending approval)
    and inside the executor (to catch drift during the approval window).
    A whole-blob replace must be re-read and merged, so the new hash is
    deliberately not echoed back. None (no expected_hash) skips the check.

    `current` is the target's current content normalized so an absent target
    hashes the same way its read tool reports it: "" for a missing raw file
    (get_yaml_config/read_file return content ""), None for an absent dashboard
    (get_dashboard_config returns not_found with no hash, so any expected_hash
    for it is a genuine conflict).
    """
    if not expected:
        return None
    current_hash = content_hash(current) if current is not None else None
    if current_hash == expected:
        return None
    return (
        _tool_error(
            "This configuration changed since you last read it (expected_hash no "
            "longer matches). Re-read it and reapply your change."
        ),
        "invalid_request",
        tool_name,
    )


async def _text_file_cas_conflict(
    expected: Any, path: str | None, hass: HomeAssistant, tool_name: str
) -> tuple[dict, str, str] | None:
    """Pre-gate CAS check for a raw-text-file writer (set_yaml_config, write_file).

    Reads the target's current text (an absent or unreadable file counts as "",
    matching what its read tool reports) and refuses if expected_hash no longer
    matches. Skips the read entirely when expected_hash is absent.
    """
    if not expected:
        return None
    current = ""
    # Split rather than combined into one condition: async_add_executor_job is
    # generic over the callable's return type, and folding both calls into a
    # single expression makes the checker bind that variable once for two
    # differently-typed callables.
    if path:
        exists = await hass.async_add_executor_job(os.path.isfile, path)
        if exists:
            try:
                current = await hass.async_add_executor_job(_read_text_capped, path)
            except (OSError, ValueError):
                current = ""
    return _cas_conflict(expected, current, tool_name)


def redaction_sentinel_path(value: Any, _prefix: tuple = ()) -> list | None:
    """Locate a REDACTION_SENTINEL anywhere in a value, or None if clean.

    THE WRITE-SIDE HALF OF REDACTION. Every config read is lossy: an entity the
    token cannot resolve (out of scope, or a GHOST that no longer exists) comes
    back as the sentinel, so a caller that reads a layout and writes it back
    persists the placeholder AS IF IT WERE CONFIGURATION. That is silent and
    permanent: the real entity id is gone from the stored config, so nobody can
    tell afterwards what the value used to be.

    Ghost entities are why this cannot be solved by widening permissions. Rule 8
    returns NO_ACCESS for an entity absent from both hass.states and the registry
    BEFORE any permission resolution runs, so a dashboard carrying a few dead
    references redacts at every permission level, for every token.

    Returns the PATH to the first offender rather than a bool, so the refusal can
    name where the caller has to look. Walks dict keys as well as values: the
    redacting readers drop an entity-id-shaped KEY's whole entry, but a caller
    can still hand one back.
    """
    if isinstance(value, str):
        return list(_prefix) if REDACTION_SENTINEL in value else None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and REDACTION_SENTINEL in key:
                return [*_prefix, key]
            found = redaction_sentinel_path(item, (*_prefix, key))
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = redaction_sentinel_path(item, (*_prefix, index))
            if found is not None:
                return found
    return None


def resolve_json_path(root: Any, path: Any) -> tuple[Any, Any, str | None]:
    """Walk `path` to the CONTAINER holding its last segment.

    Returns (container, key, None) so the caller can read, assign or delete the
    leaf itself, or (None, None, reason) when the path does not address anything.
    Surface-agnostic on purpose: the same addressing works for a Lovelace layout,
    a parsed YAML mapping, or an automation body, so a future patch tool on those
    surfaces reuses this rather than growing a second copy of the rules.

    Segments are dict keys (str) or list indices (int). NEGATIVE INDICES ARE
    REFUSED even though Python accepts them: a caller that computed -1 from a
    stale read silently edits the wrong end of a list, and there is no legible
    reason to address a config that way.

    NOTHING IS CREATED. A path whose parent is missing is a typo, not an
    instruction to build the intervening structure, and inventing containers is
    how a wrong path becomes a mangled config instead of an error message.
    """
    if not isinstance(path, list) or not path:
        return None, None, "path must be a non-empty array of keys and indices."
    node = root
    for depth, segment in enumerate(path[:-1]):
        node, reason = _descend(node, segment, depth)
        if reason is not None:
            return None, None, reason
    leaf = path[-1]
    if isinstance(leaf, bool) or not isinstance(leaf, (str, int)):
        return None, None, f"path segment {len(path) - 1} must be a string key or an integer index."
    if isinstance(node, dict):
        if not isinstance(leaf, str):
            return None, None, f"path segment {len(path) - 1} indexes a mapping, so it must be a string key."
        return node, leaf, None
    if isinstance(node, list):
        if not isinstance(leaf, int):
            return None, None, f"path segment {len(path) - 1} indexes a list, so it must be an integer."
        if not 0 <= leaf < len(node):
            return None, None, f"path segment {len(path) - 1} must be an integer in 0..{len(node) - 1}."
        return node, leaf, None
    return None, None, f"path segment {len(path) - 2} does not address a mapping or a list."


def _descend(node: Any, segment: Any, depth: int) -> tuple[Any, str | None]:
    """One step of resolve_json_path's walk. Returns (child, None) or (None, reason)."""
    if isinstance(segment, bool) or not isinstance(segment, (str, int)):
        return None, f"path segment {depth} must be a string key or an integer index."
    if isinstance(node, dict):
        if not isinstance(segment, str):
            return None, f"path segment {depth} indexes a mapping, so it must be a string key."
        if segment not in node:
            return None, f"path segment {depth} ({segment!r}) does not exist."
        return node[segment], None
    if isinstance(node, list):
        if not isinstance(segment, int):
            return None, f"path segment {depth} indexes a list, so it must be an integer."
        if not 0 <= segment < len(node):
            return None, f"path segment {depth} must be an integer in 0..{len(node) - 1}."
        return node[segment], None
    return None, f"path segment {depth} does not address a mapping or a list."


def _truncate(text: str, max_chars: int = MAX_DIFF_INLINE_BYTES) -> str:
    """Bound a diff string stored on an approval record.

    The cap exists because approval diffs are persisted in .storage alongside
    the pending queue, not because the panel cannot render them. It matches the
    version store's inline cap so an admin sees the SAME content before
    approving that the Changes tab shows afterwards. A smaller display cap
    clips a real automation mid-config, which is exactly the content an
    approver needs to read in full to decide.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... ({len(text) - max_chars} more characters)"


# Set True by a service-call path when MESA waved the action through under
# advisory mode (warnings emitted, not gated). Read at the tools/call logging
# point to flag the audit entry. A ContextVar is per-async-task and propagates
# within the same request, so it survives the await into _call_tool and back.
_mesa_advisory_ctx: ContextVar[bool] = ContextVar("phoenix_mcp_mesa_advisory", default=False)


# Set while async_execute_approved_tool runs an admin-approved action, so the MESA gate
# treats that approval as the human confirmation it asks for (confirm-approved
# semantics: confirm-mode entities proceed, prohibited/read_only entities are
# still rejected). Without this, an action gated by BOTH a confirm-mode
# capability and a MESA confirm-mode entity needed two sequential admin
# approvals: approving the capability gate re-ran the call, which then queued a
# second, separate MESA approval for the same action the admin just reviewed.
# asyncio-safe: scoped to the approving request's task.
_approved_exec_ctx: ContextVar[bool] = ContextVar("phoenix_mcp_approved_exec", default=False)


def _mesa_confirm_annotation(
    token: TokenRecord,
    hass: HomeAssistant,
    groups: list[tuple[str, str, list[str]]],
) -> dict | None:
    """MESA context block for a capability-gate approval diff, or None.

    Approving a gated call also satisfies MESA's confirm for its targets
    (async_execute_approved_tool runs confirm-approved), so the approval record itself
    must say MESA was part of what the admin's click covered: the reviewing
    admin sees it on the card, and History keeps a permanent marker. Shaped like
    build_mesa_service_diff's mesa block so the panel's existing renderer picks
    it up. Best-effort: None when MESA is off/absent, nothing is MESA-confirm,
    or anything fails (the gate diff must always build).
    """
    try:
        data = hass.data.get(DOMAIN)
        if data is None:
            return None
        confirm: list[str] = []
        rest: list[str] = []
        for domain, service, ents in groups:
            if not ents:
                continue
            gated = mesa_confirm_preview(
                data, token,
                domain=domain, service=service, service_data={}, entities=list(ents),
            )
            confirm.extend(gated)
            rest.extend(e for e in ents if e not in gated)
        if not confirm:
            return None
        return {
            "confirm_entities": confirm,
            "allowed_entities": rest,
            "warnings": [
                "Approving also satisfies the MESA confirmation for the entities "
                "listed under MESA confirm; no separate MESA approval will be queued."
            ],
        }
    except Exception:  # noqa: BLE001 - annotation only; never block the gate diff
        return None


def _resolve_area_id(entry: Any, device_registry: Any) -> str | None:
    """Return the area_id for an entity registry entry, falling back to the device's area."""
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device and device.area_id:
            return device.area_id
    return None
