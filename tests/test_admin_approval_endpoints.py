"""Tests for the admin approval HTTP endpoints in admin_view.py."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminApprovalApproveView,
    PhoenixAdminApprovalBatchApproveView,
    PhoenixAdminApprovalRejectView,
    PhoenixAdminApprovalView,
    PhoenixAdminApprovalsView,
)
from custom_components.phoenix_mcp.approvals import (
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN, TOKEN_PREFIX
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.token_store import GlobalSettings, TokenRecord, TokenStore


# ---- helpers -----------------------------------------------------------------


def _make_token(token_id: str = "tok-1", name: str = "alice", **caps) -> TokenRecord:
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return TokenRecord(
        id=token_id,
        name=name,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        created_at=utcnow(),
        created_by="admin",
        **caps,
    )


def _make_pending(approval_id: str, token_id: str = "tok-1", **kwargs) -> dict:
    now = utcnow()
    base = {
        "id": approval_id,
        "token_id": token_id,
        "token_name": "alice",
        "tool_name": "restart_ha",
        "cap_name": "cap_restart",
        "args": {},
        "diff": {"kind": "system_action", "summary": "Restart"},
        "status": STATUS_PENDING,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=3600)).isoformat(),
        "resolved_at": None,
        "approved_by_user_id": None,
        "rejected_reason": None,
        "result": None,
        "request_id": "rid-1",
        "client_ip": None,
    }
    base.update(kwargs)
    return base


def _make_store(pending: list[dict] | None = None, tokens: list[TokenRecord] | None = None,
                kill_switch: bool = False) -> MagicMock:
    store = MagicMock(spec=TokenStore)
    store._pending = list(pending or [])
    store.async_save = AsyncMock()
    store.async_lock = asyncio.Lock()
    store.get_pending_approvals = MagicMock(side_effect=lambda: store._pending)
    store.set_pending_approvals = MagicMock(side_effect=lambda lst: setattr(store, "_pending", lst))
    settings = GlobalSettings(kill_switch=kill_switch)
    store.get_settings = MagicMock(return_value=settings)
    by_id = {t.id: t for t in (tokens or [])}
    store.get_token_by_id = MagicMock(side_effect=lambda i: by_id.get(i))
    return store


def _make_data(store: MagicMock) -> PhoenixData:
    rate_limiter = MagicMock(spec=RateLimiter)
    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()
    return PhoenixData(
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
    )


def _make_admin_request(body: bytes = b"", query: dict | None = None) -> MagicMock:
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    user = MagicMock()
    user.is_admin = True
    user.id = "admin-user"

    def _get(k, default=None):
        if k == KEY_HASS_USER:
            return user
        if k == KEY_AUTHENTICATED:
            return True
        return default

    rid = "test-rid"
    state: dict = {KEY_HASS_USER: user, KEY_AUTHENTICATED: True, "phoenix_mcp_rid": rid}

    request = MagicMock()
    request.query = query or {}
    request.read = AsyncMock(return_value=body)
    request.content_length = len(body)
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=body)
    request.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    request.get = MagicMock(side_effect=_get)
    return request


def _make_hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


# ---- list view ---------------------------------------------------------------


class TestApprovalsList:
    @pytest.mark.asyncio
    async def test_returns_all_when_no_filter(self):
        store = _make_store(pending=[
            _make_pending("appr_a"),
            _make_pending("appr_b", status=STATUS_APPROVED),
        ])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalsView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(request)

        body = json.loads(resp.text)
        assert body["total"] == 2

    @pytest.mark.asyncio
    async def test_filters_by_status(self):
        store = _make_store(pending=[
            _make_pending("appr_a"),
            _make_pending("appr_b", status=STATUS_APPROVED),
        ])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalsView()
        view.hass = hass
        request = _make_admin_request(query={"status": STATUS_PENDING})

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(request)

        body = json.loads(resp.text)
        assert body["total"] == 1
        assert body["approvals"][0]["id"] == "appr_a"

    @pytest.mark.asyncio
    async def test_filters_by_token(self):
        store = _make_store(pending=[
            _make_pending("appr_a", token_id="tok-1"),
            _make_pending("appr_b", token_id="tok-2"),
        ])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalsView()
        view.hass = hass
        request = _make_admin_request(query={"token_id": "tok-1"})

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(request)

        body = json.loads(resp.text)
        assert body["total"] == 1
        assert body["approvals"][0]["id"] == "appr_a"


# ---- detail view -------------------------------------------------------------


class TestApprovalDetail:
    @pytest.mark.asyncio
    async def test_returns_record(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(request, approval_id="appr_a")

        body = json.loads(resp.text)
        assert body["id"] == "appr_a"
        assert body["tool_name"] == "restart_ha"

    @pytest.mark.asyncio
    async def test_returns_404_for_missing(self):
        store = _make_store(pending=[])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(request, approval_id="appr_missing")

        assert resp.status == 404


# ---- delete (cancel alias) ---------------------------------------------------


class TestApprovalDelete:
    @pytest.mark.asyncio
    async def test_delete_cancels_pending(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.delete(request, approval_id="appr_a")

        body = json.loads(resp.text)
        assert body["status"] == STATUS_CANCELLED
        assert body["rejected_reason"] == "admin_cancelled"

    @pytest.mark.asyncio
    async def test_delete_idempotent_on_terminal(self):
        store = _make_store(pending=[_make_pending("appr_a", status=STATUS_APPROVED)])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.delete(request, approval_id="appr_a")

        body = json.loads(resp.text)
        # Idempotent: already-terminal record returns its current state, not cancelled.
        assert body["status"] == STATUS_APPROVED

    @pytest.mark.asyncio
    async def test_delete_rejected_when_already_in_progress(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        data.approvals_in_progress.add("appr_a")
        hass = _make_hass(data)
        view = PhoenixAdminApprovalView()
        view.hass = hass
        request = _make_admin_request()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.delete(request, approval_id="appr_a")

        assert resp.status == 409
        assert next(r for r in store._pending if r["id"] == "appr_a")["status"] == STATUS_PENDING


# ---- reject view -------------------------------------------------------------


class TestApprovalReject:
    @pytest.mark.asyncio
    async def test_reject_with_reason(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalRejectView()
        view.hass = hass
        body = json.dumps({"reason": "not_safe"}).encode()
        request = _make_admin_request(body=body)

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_REJECTED
        assert out["rejected_reason"] == "not_safe"

    @pytest.mark.asyncio
    async def test_reject_without_body(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalRejectView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_REJECTED
        assert out["rejected_reason"] is None

    @pytest.mark.asyncio
    async def test_reject_rejected_when_already_in_progress(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data(store)
        data.approvals_in_progress.add("appr_a")
        hass = _make_hass(data)
        view = PhoenixAdminApprovalRejectView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 409
        assert next(r for r in store._pending if r["id"] == "appr_a")["status"] == STATUS_PENDING


# ---- approve view ------------------------------------------------------------


class TestApprovalApprove:
    @pytest.mark.asyncio
    async def test_approve_runs_executor_and_marks_approved(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _fake_executor(name, args, tok, hass, data):
            return ({"content": [{"type": "text", "text": '{"success": true}'}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_APPROVED
        assert out["result"]["outcome"] == "allowed"
        assert out["approved_by_user_id"] == "admin-user"

    @pytest.mark.asyncio
    async def test_approve_marks_execution_on_disk_before_running_the_executor(self):
        """The durable half of the double-run guard.

        approvals_in_progress makes a double-click safe within one process, but
        it dies with the process. The executor runs OUTSIDE the store lock and
        its outcome is written afterwards, so a restart in that window left an
        untouched pending record whose action may already have applied. The
        marker is only worth anything if it is on disk BEFORE the executor runs,
        which is what this observes from inside it.
        """
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")
        seen: dict = {}

        async def _observing_executor(name, args, tok, hass, data):
            entry = next(r for r in store._pending if r["id"] == "appr_a")
            seen["marker"] = entry.get("execution_started_at")
            seen["approved_at"] = entry.get("approved_at")
            seen["approved_by_user_id"] = entry.get("approved_by_user_id")
            seen["status"] = entry["status"]
            seen["audit_methods"] = [
                call.kwargs["method"] for call in data.audit.record.call_args_list
            ]
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_observing_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 200
        assert seen["marker"] is not None, (
            "execution_started_at was not persisted before the executor ran"
        )
        assert seen["approved_at"] == seen["marker"]
        assert seen["approved_by_user_id"] == "admin-user"
        assert seen["audit_methods"] == ["approval/approved"]
        assert seen["status"] == STATUS_PENDING
        # Cleared once the outcome is recorded, so the history carries no
        # half-finished marker and a later startup has nothing to reconcile.
        assert "execution_started_at" not in next(r for r in store._pending if r["id"] == "appr_a")

    @pytest.mark.asyncio
    async def test_approve_refuses_when_the_marker_cannot_be_persisted(self):
        """Fail CLOSED: no durable marker, no execution.

        The same store records the terminal status afterwards, so a disk that
        cannot take the marker cannot record the outcome either. Running anyway
        would leave a pending record whose action HAS run with nothing saying so,
        which is precisely the replay the marker exists to prevent. Asserting the
        executor is never reached, because "did not block the approval" was the
        original contract here and it was the wrong one.
        """
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        store.async_save = AsyncMock(side_effect=OSError("disk full"))
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        executor = AsyncMock()
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 503
        executor.assert_not_called()
        # Still pending, and the claim released so a retry is possible once the
        # store is writable again.
        assert next(r for r in store._pending if r["id"] == "appr_a")["status"] == STATUS_PENDING
        assert "appr_a" not in data.approvals_in_progress

    @pytest.mark.asyncio
    async def test_approve_rejected_when_already_in_progress(self):
        # Double-run race guard: an approve whose id is already claimed by a
        # concurrent in-flight approve returns 409 and never runs the executor.
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        data.approvals_in_progress.add("appr_a")
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        executor = AsyncMock()
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 409
        executor.assert_not_called()
        # Untouched: still pending for the in-flight request to finalize.
        assert next(r for r in store._pending if r["id"] == "appr_a")["status"] == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_approve_releases_in_progress_claim(self):
        # The claim is released after execution so the id is not stuck.
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _fake_executor(name, args, tok, hass, data):
            return ({"content": [{"type": "text", "text": '{"success": true}'}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert json.loads(resp.text)["status"] == STATUS_APPROVED
        assert "appr_a" not in data.approvals_in_progress

    @pytest.mark.asyncio
    async def test_approve_conflicts_if_record_resolved_during_execution(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _executor_that_resolves_elsewhere(name, args, tok, hass, data):
            store._pending[0]["status"] = STATUS_REJECTED
            store._pending[0]["rejected_reason"] = "admin_cancelled"
            return ({"content": [{"type": "text", "text": '{"success": true}'}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_executor_that_resolves_elsewhere), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 409
        assert store._pending[0]["status"] == STATUS_REJECTED
        assert [call.kwargs["method"] for call in data.audit.record.call_args_list] == [
            "approval/approved",
            "approval/executed",
        ]
        audit_kwargs = data.audit.record.call_args_list[-1].kwargs
        assert audit_kwargs["outcome"] == "allowed"
        assert audit_kwargs["payload"] == {
            "finalization": "conflict",
            "stored_status": STATUS_REJECTED,
            "executor_outcome": "allowed",
        }
        assert "appr_a" not in data.approvals_in_progress

    @pytest.mark.asyncio
    async def test_approve_rejected_when_executor_returns_error(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _failing_executor(name, args, tok, hass, data):
            return ({"content": [{"type": "text", "text": "Restart failed."}], "isError": True}, "denied", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_failing_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_FAILED
        assert out["rejected_reason"] == "execution_failed"

    @pytest.mark.asyncio
    async def test_approve_cancels_when_token_revoked(self):
        # No token in store -> get_token_by_id returns None.
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 409
        # Storage should now show the approval cancelled.
        cancelled = next(r for r in store._pending if r["id"] == "appr_a")
        assert cancelled["status"] == STATUS_CANCELLED
        assert cancelled["rejected_reason"] == "token_inactive"

    @pytest.mark.asyncio
    async def test_approve_rejected_when_cap_now_deny(self):
        token = _make_token("tok-1", cap_restart="deny")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 409
        rejected = next(r for r in store._pending if r["id"] == "appr_a")
        assert rejected["status"] == STATUS_FAILED
        assert rejected["rejected_reason"] == "capability_denied"

    @pytest.mark.asyncio
    async def test_approve_mesa_sentinel_cap_skips_effective_cap_recheck(self):
        from custom_components.phoenix_mcp.const import MESA_APPROVED_EXECUTOR, MESA_CONFIRM_CAP

        # The MESA sentinel cap is not a real token capability, so effective_cap
        # would auto-deny it. The approve path must skip that recheck and execute
        # (MESA re-validation lives inside the executor instead).
        token = _make_token("tok-1")  # no mesa cap; effective_cap would be deny
        pending = _make_pending(
            "appr_m",
            token_id="tok-1",
            cap_name=MESA_CONFIRM_CAP,
            tool_name=MESA_APPROVED_EXECUTOR,
            args={"domain": "light", "service": "turn_on", "entity_id": ["light.a"]},
        )
        store = _make_store(pending=[pending], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _fake_executor(name, args, tok, hass, data):
            assert name == MESA_APPROVED_EXECUTOR
            return ({"content": [{"type": "text", "text": '{"success": true}'}]}, "allowed", "svc")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_m")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_APPROVED

    @pytest.mark.asyncio
    async def test_approve_cancels_when_kill_switch_engaged(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(
            pending=[_make_pending("appr_a", token_id="tok-1")],
            tokens=[token],
            kill_switch=True,
        )
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 503
        cancelled = next(r for r in store._pending if r["id"] == "appr_a")
        assert cancelled["status"] == STATUS_CANCELLED
        assert cancelled["rejected_reason"] == "kill_switch"

    @pytest.mark.asyncio
    async def test_approve_idempotent_on_terminal(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(
            pending=[_make_pending("appr_a", token_id="tok-1", status=STATUS_APPROVED)],
            tokens=[token],
        )
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request, approval_id="appr_a")

        out = json.loads(resp.text)
        assert out["status"] == STATUS_APPROVED  # already terminal; returned as-is

    @pytest.mark.asyncio
    async def test_approve_404_for_missing(self):
        store = _make_store(pending=[])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request, approval_id="appr_missing")

        assert resp.status == 404


# ---- audit logging on resolution (regression) --------------------------------


def _make_data_real_audit(store: MagicMock) -> PhoenixData:
    """Like _make_data but with a real AuditLog so a malformed record() call
    (missing settings=, or an outcome outside _VALID_OUTCOMES) is exercised
    rather than swallowed by a MagicMock."""
    rate_limiter = MagicMock(spec=RateLimiter)
    audit = AuditLog(store=None, maxlen=100)
    return PhoenixData(
        store=store,
        rate_limiter=rate_limiter,
        audit=audit,
    )


class TestApprovalClaimSignal:
    """An approve that is executing must stop every surface offering the action.

    Approve runs its saved tool INLINE in the admin's request, so the resolved
    event cannot fire until that finishes, which is seconds for a real write. For
    all of it the Approvals tab, the Agent Chat bubble and the persistent
    notification kept offering Approve and Reject on an approval already being
    acted on. The store-lock claim answered the second click with a 409, so the
    outcome was never wrong; the operator was being shown a control that could
    not work. These pin the claim/release signal that closes that window.
    """

    @staticmethod
    def _claim_events(hass) -> list[tuple[str, bool]]:
        return [
            (c.args[1]["approval_id"], c.args[1]["claimed"])
            for c in hass.bus.async_fire.call_args_list
            if c.args[0] == f"{DOMAIN}_approval_claimed"
        ]

    @pytest.mark.asyncio
    async def test_claim_event_fires_before_the_executor_runs(self):
        """Ordering is the whole point: firing it after execution would signal
        exactly when the resolved event already does, changing nothing."""
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        seen_during_execution: list[tuple[str, bool]] = []

        async def _fake_executor(name, args, tok, hass_, data_):
            seen_during_execution.extend(self_ref._claim_events(hass))
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        self_ref = self
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert json.loads(resp.text)["status"] == STATUS_APPROVED
        assert seen_during_execution == [("appr_a", True)]

    @pytest.mark.asyncio
    async def test_notification_is_dismissed_before_the_executor_runs(self):
        """The notification is the surface with no UI state of its own, so
        dismissing it IS its version of going non-actionable."""
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        dismissed_during_execution: list[int] = []

        async def _fake_executor(name, args, tok, hass_, data_):
            dismissed_during_execution.append(dismiss.call_count)
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss") as dismiss:
            await view.post(request, approval_id="appr_a")

        assert dismissed_during_execution == [1]

    @pytest.mark.asyncio
    async def test_successful_approve_never_releases(self):
        """A resolved approval must not come back: the resolved event already
        removed it everywhere, and a release would re-offer a dead action."""
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _fake_executor(name, args, tok, hass_, data_):
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            await view.post(request, approval_id="appr_a")

        assert self._claim_events(hass) == [("appr_a", True)]

    @pytest.mark.asyncio
    async def test_a_raising_executor_closes_the_approval_instead_of_re_offering_it(self):
        """A raising executor may already have applied its side effect.

        This test asserted the OPPOSITE contract: that the record stayed pending
        and the notification came back, so a live approval was never invisible.
        That reasoning is right about visibility and wrong about safety. A service
        call that succeeded and then raised in result handling is indistinguishable
        from one that never ran, so re-offering invites the admin to apply it a
        second time, seconds later, while they are still looking at it. The
        durable marker did not cover this: it is only read at STARTUP.

        The record is therefore resolved exactly as startup reconciliation
        resolves an interrupted one, and the surfaces learn from the resolved
        event rather than from the approval reappearing.
        """
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool",
                   side_effect=RuntimeError("boom")), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create") as create:
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 500
        stored = next(r for r in store._pending if r["id"] == "appr_a")
        assert stored["status"] == STATUS_FAILED
        assert stored["rejected_reason"] == "execution_interrupted"
        # Not released back to pending, so no claimed=False and no notification
        # inviting a second attempt.
        assert self._claim_events(hass) == [("appr_a", True)]
        assert create.call_count == 0
        assert "appr_a" not in data.approvals_in_progress

    @pytest.mark.asyncio
    async def test_a_second_approve_after_a_raising_executor_cannot_run_it_again(self):
        # The two-POST shape: the whole point is that the admin looking at the
        # 500 cannot click Approve again and apply the side effect twice.
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass

        executor = AsyncMock(side_effect=[
            RuntimeError("raised after the side effect landed"),
            ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha"),
        ])
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            first = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")
            second = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")

        assert first.status == 500
        # The executor ran exactly ONCE: that is the whole guarantee. The second
        # POST answers 200 because approving an already-terminal record is
        # idempotent (pre-existing behaviour), so it reports the stored outcome
        # rather than running anything.
        assert executor.await_count == 1
        assert json.loads(second.text)["status"] == STATUS_FAILED
        assert json.loads(second.text)["rejected_reason"] == "execution_interrupted"

    @pytest.mark.asyncio
    async def test_an_unregistered_tool_stays_retryable(self):
        # The other side of the line: the registry lookup is the first thing
        # async_execute_approved_tool does, so a KeyError means nothing ran and
        # nothing can have applied. That one keeps its old, correct behaviour, and
        # the marker is cleared so a later startup does not resolve a healthy
        # pending approval as interrupted.
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass

        from custom_components.phoenix_mcp.mcp_view import ExecutorNotRegistered

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool",
                   side_effect=ExecutorNotRegistered("no such tool")), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create") as create:
            resp = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")

        assert resp.status == 400
        stored = next(r for r in store._pending if r["id"] == "appr_a")
        assert stored["status"] == STATUS_PENDING
        assert "execution_started_at" not in stored
        assert self._claim_events(hass) == [("appr_a", True), ("appr_a", False)]
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_a_real_executor_raising_KeyError_is_not_treated_as_a_missing_one(self):
        """A KeyError from INSIDE an executor must not read as "nothing ran".

        This drives the REAL registry and the real dispatcher, because mocking
        `async_execute_approved_tool` cannot tell a lookup failure from an
        executor failure, and that is precisely the confusion being tested. The
        old code caught bare KeyError for the retryable branch, so an executor
        doing an ordinary dict lookup on a service response could clear the
        durable marker and re-offer an action it had already applied.
        """
        from custom_components.phoenix_mcp import mcp_view

        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(
            pending=[_make_pending("appr_a", token_id="tok-1", tool_name="restart_ha")],
            tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        applied: list[str] = []

        async def _executor(args, tok, hass_, data_):
            applied.append("side effect landed")
            raise KeyError("a lookup inside the executor, long after the write")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch.dict(mcp_view._EXECUTOR_REGISTRY, {"restart_ha": _executor}), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            first = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")
            second = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")

        assert first.status == 500
        # Ran once, and the record is closed rather than offered back.
        assert applied == ["side effect landed"]
        stored = next(r for r in store._pending if r["id"] == "appr_a")
        assert stored["status"] == STATUS_FAILED
        assert stored["rejected_reason"] == "execution_interrupted"
        assert json.loads(second.text)["rejected_reason"] == "execution_interrupted"

    @pytest.mark.asyncio
    async def test_cancellation_mid_execution_does_not_re_offer_the_approval(self):
        """asyncio.CancelledError does NOT inherit from Exception.

        A cancelled task therefore skipped the failure handler entirely, ran the
        finally, released the claim and put the approval back on offer with its
        side effect possibly applied. The cancellation is re-raised rather than
        swallowed, so the assertion is about what the STORE looks like afterwards.
        """
        from custom_components.phoenix_mcp import mcp_view

        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(
            pending=[_make_pending("appr_a", token_id="tok-1", tool_name="restart_ha")],
            tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        applied: list[str] = []

        async def _cancelled_executor(args, tok, hass_, data_):
            applied.append("side effect landed")
            raise asyncio.CancelledError()

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch.dict(mcp_view._EXECUTOR_REGISTRY, {"restart_ha": _cancelled_executor}), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            with pytest.raises(asyncio.CancelledError):
                await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")

        assert applied == ["side effect landed"]
        stored = next(r for r in store._pending if r["id"] == "appr_a")
        assert stored["status"] == STATUS_FAILED
        assert stored["rejected_reason"] == "execution_interrupted"

    @pytest.mark.asyncio
    async def test_an_approval_carrying_a_stale_marker_refuses_to_run_again(self):
        """Belt and braces for every path that leaves a marker behind.

        A marker on a still-pending record means an earlier attempt reached
        dispatch and never reported back, so the action may already have applied.
        Overwriting the marker and running again is exactly the retry that must
        not happen; startup reconciliation is what resolves these.
        """
        from custom_components.phoenix_mcp import mcp_view

        token = _make_token("tok-1", cap_restart="confirm")
        pending = _make_pending("appr_a", token_id="tok-1", tool_name="restart_ha")
        pending["execution_started_at"] = utcnow().isoformat()
        store = _make_store(pending=[pending], tokens=[token])
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        applied: list[str] = []

        async def _executor(args, tok, hass_, data_):
            applied.append("should never run")
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch.dict(mcp_view._EXECUTOR_REGISTRY, {"restart_ha": _executor}), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            resp = await view.post(_make_admin_request(body=b"{}"), approval_id="appr_a")

        assert resp.status == 409
        assert applied == []
        assert "appr_a" not in data.approvals_in_progress

    @pytest.mark.asyncio
    async def test_list_reports_in_progress_so_a_reloaded_panel_agrees(self):
        """The event only reaches a panel that was already open. A page loaded
        mid-execution has to learn it from the row."""
        store = _make_store(pending=[_make_pending("appr_a"), _make_pending("appr_b")])
        data = _make_data(store)
        data.approvals_in_progress.add("appr_a")
        hass = _make_hass(data)
        view = PhoenixAdminApprovalsView()
        view.hass = hass

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.get(_make_admin_request())

        rows = {r["id"]: r for r in json.loads(resp.text)["approvals"]}
        assert rows["appr_a"]["in_progress"] is True
        assert rows["appr_b"]["in_progress"] is False


class TestApprovalAuditLogging:
    @pytest.mark.asyncio
    async def test_reject_writes_queryable_audit_entry(self):
        store = _make_store(pending=[_make_pending("appr_a")])
        data = _make_data_real_audit(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalRejectView()
        view.hass = hass
        request = _make_admin_request(body=json.dumps({"reason": "not_safe"}).encode())

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 200
        # The entry exists and is filterable by a canonical outcome (would be
        # None if the recorded outcome were outside _VALID_OUTCOMES).
        denied = data.audit.query(outcome="denied")
        assert denied is not None
        assert any("appr_a" in e.resource for e in denied)

    @pytest.mark.asyncio
    async def test_approve_writes_allowed_audit_entry(self):
        token = _make_token("tok-1", cap_restart="confirm")
        store = _make_store(pending=[_make_pending("appr_a", token_id="tok-1")], tokens=[token])
        data = _make_data_real_audit(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalApproveView()
        view.hass = hass
        request = _make_admin_request(body=b"{}")

        async def _fake_executor(name, args, tok, hass, data):
            return ({"content": [{"type": "text", "text": '{"success": true}'}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request, approval_id="appr_a")

        assert resp.status == 200
        allowed = data.audit.query(outcome="allowed")
        assert allowed is not None
        assert any("appr_a" in e.resource for e in allowed)


# ---- batch approve -----------------------------------------------------------


class TestBatchApprove:
    """POST /admin/approvals/batch/approve.

    Purely an operator convenience: every id still runs through the SAME
    _approve_approval, so these tests are about the BATCH's own contract (order,
    stop-at-failure, what is reported, what is left pending) rather than about
    re-proving the per-approval lifecycle, which the tests above already cover.
    """

    @staticmethod
    def _view_and_request(pending, tokens, ids, kill_switch=False):
        store = _make_store(pending=pending, tokens=tokens, kill_switch=kill_switch)
        data = _make_data(store)
        hass = _make_hass(data)
        view = PhoenixAdminApprovalBatchApproveView()
        view.hass = hass
        request = _make_admin_request(body=json.dumps({"approval_ids": ids}).encode())
        return view, request, store, data

    @pytest.mark.asyncio
    async def test_approves_every_id_in_order(self):
        token = _make_token("tok-1", cap_restart="confirm")
        view, request, store, _data = self._view_and_request(
            [_make_pending("appr_a"), _make_pending("appr_b"), _make_pending("appr_c")],
            [token], ["appr_a", "appr_b", "appr_c"],
        )
        seen: list[str] = []

        async def _fake_executor(name, args, tok, hass, data):
            seen.append(name)
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request)

        out = json.loads(resp.text)
        assert resp.status == 200
        assert [a["approval_id"] for a in out["applied"]] == ["appr_a", "appr_b", "appr_c"]
        assert out["failed"] is None and out["remaining"] == []
        assert len(seen) == 3
        assert all(r["status"] == STATUS_APPROVED for r in store._pending)

    @pytest.mark.asyncio
    async def test_stops_at_first_failure_and_leaves_the_rest_pending(self):
        """The whole point of stop-at-failure: the queue is not burned through.

        Item 2 fails, so item 3 must never reach its executor and must still be
        individually approvable afterwards.
        """
        token = _make_token("tok-1", cap_restart="confirm")
        view, request, store, _data = self._view_and_request(
            [_make_pending("appr_a"), _make_pending("appr_b"), _make_pending("appr_c")],
            [token], ["appr_a", "appr_b", "appr_c"],
        )
        seen: list[str] = []

        async def _fake_executor(name, args, tok, hass, data):
            seen.append(tok.id)
            if len(seen) == 2:
                return ({"content": [{"type": "text", "text": "boom"}], "isError": True}, "invalid_request", "x")
            return ({"content": [{"type": "text", "text": "{}"}]}, "allowed", "restart_ha")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            resp = await view.post(request)

        out = json.loads(resp.text)
        assert [a["approval_id"] for a in out["applied"]] == ["appr_a"]
        assert out["failed"]["approval_id"] == "appr_b"
        assert out["remaining"] == ["appr_c"]
        # The third executor never ran.
        assert len(seen) == 2
        by_id = {r["id"]: r for r in store._pending}
        assert by_id["appr_a"]["status"] == STATUS_APPROVED
        assert by_id["appr_c"]["status"] == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_execution_failure_is_not_reported_as_applied(self):
        """A 200 is not enough.

        An executor returning isError finalizes the record as rejected with
        execution_failed and _approve_approval still answers 200; counting that as
        applied would report a failed write as a success.
        """
        token = _make_token("tok-1", cap_restart="confirm")
        view, request, _store, _data = self._view_and_request(
            [_make_pending("appr_a")], [token], ["appr_a"],
        )

        async def _fake_executor(name, args, tok, hass, data):
            return ({"content": [{"type": "text", "text": "nope"}], "isError": True}, "invalid_request", "x")

        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", side_effect=_fake_executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"), \
             patch("homeassistant.components.persistent_notification.async_create"):
            resp = await view.post(request)

        out = json.loads(resp.text)
        assert out["applied"] == []
        assert out["failed"]["approval_id"] == "appr_a"

    @pytest.mark.asyncio
    async def test_kill_switch_stops_the_batch_at_the_first_item(self):
        token = _make_token("tok-1", cap_restart="confirm")
        view, request, store, _data = self._view_and_request(
            [_make_pending("appr_a"), _make_pending("appr_b")],
            [token], ["appr_a", "appr_b"], kill_switch=True,
        )
        executor = AsyncMock()
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f), \
             patch("custom_components.phoenix_mcp.mcp_view.async_execute_approved_tool", executor), \
             patch("homeassistant.components.persistent_notification.async_dismiss"):
            resp = await view.post(request)

        out = json.loads(resp.text)
        executor.assert_not_called()
        assert out["applied"] == [] and out["failed"]["approval_id"] == "appr_a"
        assert out["remaining"] == ["appr_b"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [
        {}, {"approval_ids": []}, {"approval_ids": "appr_a"},
        {"approval_ids": ["appr_a", ""]}, {"approval_ids": ["appr_a", 3]},
    ])
    async def test_rejects_a_malformed_id_list(self, body):
        view, request, _s, _d = self._view_and_request([], [], ["x"])
        request.content.read = AsyncMock(return_value=json.dumps(body).encode())
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_a_repeated_id(self):
        """A duplicate would hit the already-terminal branch and halt the batch
        on the caller's own typo, so it is refused up front instead."""
        view, request, _s, _d = self._view_and_request([], [], ["appr_a", "appr_a"])
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request)
        assert resp.status == 400
        assert "repeat" in json.loads(resp.text)["message"]

    @pytest.mark.asyncio
    async def test_rejects_more_than_the_request_bound(self):
        from custom_components.phoenix_mcp.const import MAX_BATCH_APPROVALS
        ids = [f"appr_{i}" for i in range(MAX_BATCH_APPROVALS + 1)]
        view, request, _s, _d = self._view_and_request([], [], ids)
        with patch("custom_components.phoenix_mcp.admin_view.require_admin", lambda f: f):
            resp = await view.post(request)
        assert resp.status == 400
