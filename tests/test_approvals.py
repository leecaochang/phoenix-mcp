"""Tests for the approvals module (PendingApproval CRUD, lifecycle, expiry)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.approvals import (
    PendingApproval,
    PendingApprovalCapacityError,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    async_cancel_approvals_for_token,
    collect_pending_approvals_for_wipe,
    async_create_pending_approval,
    async_expire_overdue_approval_records,
    get_approval,
    list_approvals,
    REASON_EXECUTION_INTERRUPTED,
    async_mark_execution_started,
    async_reconcile_interrupted_approvals,
    async_update_approval_status,
)
from custom_components.phoenix_mcp.approvals import create_approval_notification
from custom_components.phoenix_mcp.const import DOMAIN, MAX_PENDING_APPROVALS_PER_TOKEN
from custom_components.phoenix_mcp.token_store import GlobalSettings


class _FakeStore:
    """Minimal store-like object for tests."""

    def __init__(self) -> None:
        self._pending: list[dict] = []
        self.async_save = AsyncMock()
        self.async_lock = asyncio.Lock()
        self._settings = GlobalSettings()

    def get_pending_approvals(self) -> list[dict]:
        return self._pending

    def set_pending_approvals(self, approvals: list[dict]) -> None:
        self._pending = approvals

    def get_settings(self) -> GlobalSettings:
        return self._settings

    async def async_patch_settings(self, **kwargs) -> GlobalSettings:
        self._settings = GlobalSettings.from_dict({**self._settings.to_dict(), **kwargs})
        return self._settings


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore()


# --- async_create_pending_approval --------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_returns_record_with_pending_status(self, store):
        record = await async_create_pending_approval(
            store,
            token_id="t1",
            token_name="alice",
            tool_name="restart_ha",
            cap_name="cap_restart",
            args={},
            diff={"kind": "system_action", "summary": "Restart"},
            request_id="rid-1",
        )
        assert record.status == STATUS_PENDING
        assert record.token_id == "t1"
        assert record.tool_name == "restart_ha"
        assert record.id.startswith("appr_")

    @pytest.mark.asyncio
    async def test_persists_to_storage(self, store):
        await async_create_pending_approval(
            store,
            token_id="t1", token_name="alice", tool_name="restart_ha",
            cap_name="cap_restart", args={}, diff={}, request_id="rid",
        )
        assert len(store.get_pending_approvals()) == 1
        store.async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expires_at_in_future(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        assert record.expires_at > utcnow()
        delta = record.expires_at - record.created_at
        assert 55 <= delta.total_seconds() <= 65

    @pytest.mark.asyncio
    async def test_per_token_capacity_enforced(self, store):
        for i in range(MAX_PENDING_APPROVALS_PER_TOKEN):
            await async_create_pending_approval(
                store, token_id="t1", token_name="a", tool_name="x",
                cap_name="cap_restart", args={"i": i}, diff={}, request_id=f"r{i}",
            )
        with pytest.raises(PendingApprovalCapacityError):
            await async_create_pending_approval(
                store, token_id="t1", token_name="a", tool_name="x",
                cap_name="cap_restart", args={}, diff={}, request_id="overflow",
            )

    @pytest.mark.asyncio
    async def test_capacity_is_per_token(self, store):
        # Fill t1 to capacity, then verify t2 can still create.
        for i in range(MAX_PENDING_APPROVALS_PER_TOKEN):
            await async_create_pending_approval(
                store, token_id="t1", token_name="a", tool_name="x",
                cap_name="cap_restart", args={}, diff={}, request_id=f"r{i}",
            )
        record = await async_create_pending_approval(
            store, token_id="t2", token_name="b", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r-other",
        )
        assert record.token_id == "t2"


# --- get / list ---------------------------------------------------------------


class TestGetAndList:
    @pytest.mark.asyncio
    async def test_get_returns_record(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
        )
        fetched = get_approval(store, record.id)
        assert fetched is not None
        assert fetched.id == record.id

    def test_get_returns_none_for_missing(self, store):
        assert get_approval(store, "appr_does_not_exist") is None

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, store):
        a = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        b = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r2",
        )
        async with store.async_lock:
            await async_update_approval_status(store, b.id, status=STATUS_APPROVED)
        pending = list_approvals(store, status=STATUS_PENDING)
        approved = list_approvals(store, status=STATUS_APPROVED)
        assert [r.id for r in pending] == [a.id]
        assert [r.id for r in approved] == [b.id]

    @pytest.mark.asyncio
    async def test_list_filters_by_token(self, store):
        a = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        await async_create_pending_approval(
            store, token_id="t2", token_name="b", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r2",
        )
        result = list_approvals(store, token_id="t1")
        assert [r.id for r in result] == [a.id]


# --- async_update_approval_status ---------------------------------------------------


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_approves_pending(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
        )
        async with store.async_lock:
            updated = await async_update_approval_status(
                store, record.id,
                status=STATUS_APPROVED,
                approved_by_user_id="user1",
                result={"ok": True},
            )
        assert updated.status == STATUS_APPROVED
        assert updated.approved_by_user_id == "user1"
        assert updated.result == {"ok": True}
        assert updated.resolved_at is not None

    @pytest.mark.asyncio
    async def test_idempotent_on_terminal(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
        )
        async with store.async_lock:
            await async_update_approval_status(store, record.id, status=STATUS_APPROVED)
            second = await async_update_approval_status(
                store, record.id, status=STATUS_REJECTED,
                rejected_reason="too_late",
            )
        # Second call observes the existing terminal state without overwriting.
        assert second.status == STATUS_APPROVED
        assert second.rejected_reason is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, store):
        async with store.async_lock:
            result = await async_update_approval_status(store, "appr_missing", status=STATUS_REJECTED)
        assert result is None


# --- async_cancel_approvals_for_token ----------------------------------------------


class TestCancelForToken:
    @pytest.mark.asyncio
    async def test_cancels_only_pending(self, store):
        a = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        b = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r2",
        )
        async with store.async_lock:
            await async_update_approval_status(store, a.id, status=STATUS_APPROVED)
        cancelled = await async_cancel_approvals_for_token(store, "t1", "token_revoked")
        assert [c.id for c in cancelled] == [b.id]
        assert cancelled[0].status == STATUS_CANCELLED
        assert get_approval(store, b.id).status == STATUS_CANCELLED
        assert get_approval(store, a.id).status == STATUS_APPROVED

    @pytest.mark.asyncio
    async def test_does_not_touch_other_tokens(self, store):
        await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        b = await async_create_pending_approval(
            store, token_id="t2", token_name="b", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r2",
        )
        await async_cancel_approvals_for_token(store, "t1", "token_revoked")
        assert get_approval(store, b.id).status == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_skips_in_progress_ids(self, store):
        # An approval whose saved action is mid-execution must not be flipped
        # out from under the executor, matching the expiry sweep's protection.
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        cancelled = await async_cancel_approvals_for_token(
            store, "t1", "token_revoked", skip_ids={record.id},
        )
        assert cancelled == []
        assert get_approval(store, record.id).status == STATUS_PENDING


# --- collect_pending_approvals_for_wipe ---------------------------------------


class TestCollectForWipe:
    @pytest.mark.asyncio
    async def test_returns_cancelled_records_for_every_pending(self, store):
        a = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r1",
        )
        b = await async_create_pending_approval(
            store, token_id="t2", token_name="b", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r2",
        )
        async with store.async_lock:
            await async_update_approval_status(store, b.id, status=STATUS_APPROVED)

        collected = collect_pending_approvals_for_wipe(store, "phoenix_mcp_data_wiped")

        # Only the still-pending record; spans all tokens (no per-token filter).
        assert [c.id for c in collected] == [a.id]
        assert collected[0].status == STATUS_CANCELLED
        assert collected[0].rejected_reason == "phoenix_mcp_data_wiped"

    def test_read_only_does_not_mutate_store(self, store):
        # It must not persist or flip statuses: async_wipe clears the queue.
        store.set_pending_approvals([
            {"id": "ap1", "token_id": "gone", "tool_name": "x",
             "status": STATUS_PENDING, "created_at": "2026-07-10T00:00:00+00:00",
             "expires_at": "2026-07-10T01:00:00+00:00"},
        ])
        collect_pending_approvals_for_wipe(store, "phoenix_mcp_data_wiped")
        assert store.get_pending_approvals()[0]["status"] == STATUS_PENDING
        store.async_save.assert_not_called()


# --- async_expire_overdue_approval_records ------------------------------------------


class TestExpire:
    @pytest.mark.asyncio
    async def test_expires_overdue(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        # Backdate the record so it appears overdue.
        raw = store.get_pending_approvals()
        raw[0]["expires_at"] = (utcnow() - timedelta(minutes=5)).isoformat()
        store.set_pending_approvals(raw)
        expired = await async_expire_overdue_approval_records(store)
        assert len(expired) == 1
        assert get_approval(store, record.id).status == STATUS_EXPIRED

    @pytest.mark.asyncio
    async def test_does_not_expire_in_window(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        expired = await async_expire_overdue_approval_records(store)
        assert expired == []
        assert get_approval(store, record.id).status == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_does_not_re_expire_terminal(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        async with store.async_lock:
            await async_update_approval_status(store, record.id, status=STATUS_APPROVED)
        # Now backdate — should not flip approved->expired.
        raw = store.get_pending_approvals()
        raw[0]["expires_at"] = (utcnow() - timedelta(minutes=5)).isoformat()
        store.set_pending_approvals(raw)
        expired = await async_expire_overdue_approval_records(store)
        assert expired == []
        assert get_approval(store, record.id).status == STATUS_APPROVED

    @pytest.mark.asyncio
    async def test_skips_in_progress_ids(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        raw = store.get_pending_approvals()
        raw[0]["expires_at"] = (utcnow() - timedelta(minutes=5)).isoformat()
        store.set_pending_approvals(raw)

        expired = await async_expire_overdue_approval_records(store, skip_ids={record.id})

        assert expired == []
        assert get_approval(store, record.id).status == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_returns_expired_records(self, store):
        record = await async_create_pending_approval(
            store, token_id="t1", token_name="a", tool_name="x",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
            ttl_seconds=60,
        )
        raw = store.get_pending_approvals()
        raw[0]["expires_at"] = (utcnow() - timedelta(minutes=5)).isoformat()
        store.set_pending_approvals(raw)

        expired = await async_expire_overdue_approval_records(store)

        assert [approval.id for approval in expired] == [record.id]
        assert expired[0].status == STATUS_EXPIRED


# --- expiry archival cancels queued approvals ---------------------------------


class TestExpiredTokenCancelsApprovals:
    @pytest.mark.asyncio
    async def test_archive_expired_token_rechecks_current_expiry_under_lock(self, store):
        """A concurrent expiry extension wins over an older timer snapshot."""
        from custom_components.phoenix_mcp.helpers import async_archive_expired_token

        stale = MagicMock(id="tok-exp", name="alice")
        stale.expires_at = utcnow() - timedelta(seconds=1)
        current = MagicMock(id="tok-exp", name="alice")
        current.expires_at = utcnow() + timedelta(hours=1)
        store.get_token_by_id = MagicMock(return_value=current)
        store.async_archive_token = AsyncMock()
        data = MagicMock(
            store=store,
            approvals_in_progress=set(),
            expiry_timers={},
        )
        data.settings_update_lock = asyncio.Lock()
        hass = MagicMock()

        await async_archive_expired_token(hass, data, stale)

        store.async_archive_token.assert_not_awaited()
        hass.bus.async_fire.assert_not_called()

    @pytest.mark.asyncio
    async def test_archive_expired_token_cancels_and_notifies(self, store):
        """Token expiry runs the same approval-queue hygiene as revoke: pending
        approvals flip to cancelled (reason token_expired), notifications are
        dismissed, and phoenix_mcp_approval_resolved fires for each."""
        from unittest.mock import MagicMock, patch

        from custom_components.phoenix_mcp.helpers import async_archive_expired_token

        record = await async_create_pending_approval(
            store, token_id="tok-exp", token_name="a", tool_name="restart_ha",
            cap_name="cap_restart", args={}, diff={}, request_id="r",
        )
        store.async_archive_token = AsyncMock(return_value=MagicMock())

        data = MagicMock()
        data.store = store
        data.approvals_in_progress = set()
        data.expiry_timers = {}
        data.settings_update_lock = asyncio.Lock()
        data.async_on_token_archived = None
        hass = MagicMock()
        token = MagicMock()
        token.id = "tok-exp"
        token.name = "alice"
        token.expires_at = utcnow() - timedelta(seconds=1)
        store.get_token_by_id = MagicMock(return_value=token)

        with patch(
            "custom_components.phoenix_mcp.approvals.dismiss_approval_notification"
        ) as dismiss:
            await async_archive_expired_token(hass, data, token)

        assert get_approval(store, record.id).status == STATUS_CANCELLED
        assert get_approval(store, record.id).rejected_reason == "token_expired"
        dismiss.assert_called_once_with(hass, record.id)
        resolved = [
            c.args[1] for c in hass.bus.async_fire.call_args_list
            if c.args[0] == "phoenix_mcp_approval_resolved"
        ]
        assert [p["approval_id"] for p in resolved] == [record.id]

    async def test_archive_expired_token_clears_assist_binding(self, store):
        """Expiry clears the Assist binding + voice-agent token if they pointed at
        the expiring token, and re-syncs the voice agent."""
        from unittest.mock import MagicMock, patch

        from custom_components.phoenix_mcp.helpers import async_archive_expired_token

        await store.async_patch_settings(assist_bound_token_id="tok-exp", voice_agent_token_id="tok-exp")
        store.async_archive_token = AsyncMock(return_value=MagicMock())

        data = MagicMock()
        data.store = store
        data.approvals_in_progress = set()
        data.expiry_timers = {}
        data.settings_update_lock = asyncio.Lock()
        data.async_on_token_archived = None
        data.async_sync_voice_agent = MagicMock()
        hass = MagicMock()
        token = MagicMock()
        token.id = "tok-exp"
        token.name = "alice"
        token.expires_at = utcnow() - timedelta(seconds=1)
        store.get_token_by_id = MagicMock(return_value=token)

        with patch("custom_components.phoenix_mcp.approvals.dismiss_approval_notification"):
            await async_archive_expired_token(hass, data, token)

        assert store.get_settings().assist_bound_token_id is None
        assert store.get_settings().voice_agent_token_id is None
        data.async_sync_voice_agent.assert_called_once()


# --- to_dict / from_dict round trip -----------------------------------------


class TestRecordSerialization:
    def test_round_trip(self):
        original = PendingApproval(
            id="appr_test",
            token_id="t1",
            token_name="alice",
            tool_name="restart_ha",
            cap_name="cap_restart",
            args={"k": "v"},
            diff={"kind": "system_action", "summary": "Restart"},
            status=STATUS_PENDING,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(seconds=60),
            request_id="rid",
        )
        roundtrip = PendingApproval.from_dict(original.to_dict())
        assert roundtrip.id == original.id
        assert roundtrip.tool_name == original.tool_name
        assert roundtrip.args == original.args
        assert roundtrip.diff == original.diff

    def test_is_terminal(self):
        for status in (STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED, STATUS_CANCELLED):
            r = PendingApproval(
                id="x", token_id="t", token_name="n", tool_name="x",
                cap_name="c", args={}, diff={}, status=status,
                created_at=utcnow(), expires_at=utcnow(), request_id="r",
            )
            assert r.is_terminal() is True
        r = PendingApproval(
            id="x", token_id="t", token_name="n", tool_name="x",
            cap_name="c", args={}, diff={}, status=STATUS_PENDING,
            created_at=utcnow(), expires_at=utcnow(), request_id="r",
        )
        assert r.is_terminal() is False


class TestNotificationGating:
    """create_approval_notification honours the notify_on_approval setting."""

    def _approval(self) -> PendingApproval:
        return PendingApproval(
            id="appr_x", token_id="t", token_name="codex", tool_name="call_service",
            cap_name="cap_physical_control", args={}, diff={}, status=STATUS_PENDING,
            created_at=utcnow(), expires_at=utcnow(), request_id="r",
        )

    def _data(self, *, notify: bool) -> MagicMock:
        data = MagicMock()
        data.store.get_settings = MagicMock(return_value=GlobalSettings(notify_on_approval=notify))
        return data

    def test_suppressed_when_disabled(self, hass):
        hass.data[DOMAIN] = self._data(notify=False)
        with patch("homeassistant.components.persistent_notification.async_create") as m:
            create_approval_notification(hass, self._approval())
            m.assert_not_called()

    def test_fired_when_enabled(self, hass):
        hass.data[DOMAIN] = self._data(notify=True)
        with patch("homeassistant.components.persistent_notification.async_create") as m:
            create_approval_notification(hass, self._approval())
            m.assert_called_once()


class TestArgsRedaction:
    """Approval args are redacted in the admin-facing serialisation but kept raw
    on the persistence path so the approved-action executor can re-run them."""

    def _approval(self) -> PendingApproval:
        now = utcnow()
        return PendingApproval(
            id="a1", token_id="t", token_name="n", tool_name="write_file",
            cap_name="cap_filesystem",
            args={"path": "/config/secrets.yaml", "content": "api_key: abc123secret"},
            diff={}, status=STATUS_PENDING,
            created_at=now, expires_at=now, request_id="r",
        )

    def test_default_to_dict_redacts_args(self):
        d = self._approval().to_dict()
        assert "abc123secret" not in d["args"]["content"]
        assert d["args"]["path"] == "/config/secrets.yaml"

    def test_persistence_to_dict_keeps_raw_args(self):
        d = self._approval().to_dict(redact_args=False)
        assert d["args"]["content"] == "api_key: abc123secret"

    def test_executor_only_approval_bindings_are_never_projected(self):
        approval = self._approval()
        approval.args["_config_entry_private_identity_fingerprint"] = "private-hash"
        assert approval.to_dict()["args"][
            "_config_entry_private_identity_fingerprint"
        ] == "<redacted>"
        assert approval.to_dict(redact_args=False)["args"][
            "_config_entry_private_identity_fingerprint"
        ] == "private-hash"

    @pytest.mark.asyncio
    async def test_created_approval_persists_raw_args(self, store):
        await async_create_pending_approval(
            store, token_id="t", token_name="n", tool_name="write_file",
            cap_name="cap_filesystem",
            args={"content": "api_key: abc123secret"}, diff={}, request_id="r",
        )
        stored = store.get_pending_approvals()[0]
        assert stored["args"]["content"] == "api_key: abc123secret"


# --- interrupted execution ----------------------------------------------------


class TestInterruptedExecution:
    """A crash between the side effect and its recorded outcome.

    The in-memory claim in data.approvals_in_progress makes a double-click safe,
    but it dies with the process. The executor runs OUTSIDE the store lock and
    the terminal status is written afterwards, so a stop in that window left an
    untouched pending record whose action may already have applied, and approving
    it again applied it twice. Nothing covered this: the existing tests are all
    same-process races.
    """

    async def _pending(self, store) -> str:
        approval = await async_create_pending_approval(
            store, token_id="t1", token_name="tok", tool_name="call_service",
            cap_name="cap_physical_control", args={}, diff={}, request_id="r",
        )
        return approval.id

    @pytest.mark.asyncio
    async def test_marker_is_written_before_the_executor_runs(self, store):
        approval_id = await self._pending(store)

        await async_mark_execution_started(store, approval_id)

        entry = store.get_pending_approvals()[0]
        # Still pending: the marker records that it MIGHT have applied, and the
        # record only becomes terminal once the executor actually reports.
        assert entry["status"] == STATUS_PENDING
        assert entry["execution_started_at"] is not None

    @pytest.mark.asyncio
    async def test_a_restart_mid_execution_resolves_rather_than_re_offering(self, store):
        approval_id = await self._pending(store)
        await async_mark_execution_started(store, approval_id)

        # A new process reads the store back and reconciles.
        changed = await async_reconcile_interrupted_approvals(store)

        assert [a.id for a in changed] == [approval_id]
        entry = store.get_pending_approvals()[0]
        assert entry["status"] == STATUS_REJECTED
        assert entry["rejected_reason"] == REASON_EXECUTION_INTERRUPTED
        # Approving it again is the one unrecoverable outcome, so the record must
        # not be pending any more.
        assert get_approval(store, approval_id).status != STATUS_PENDING

    @pytest.mark.asyncio
    async def test_an_untouched_pending_approval_is_left_alone(self, store):
        # The whole point is telling "nobody acted on this" apart from "someone
        # started acting and we lost the answer". Resolving both would throw away
        # every approval waiting in the queue at a restart.
        approval_id = await self._pending(store)

        changed = await async_reconcile_interrupted_approvals(store)

        assert changed == []
        assert get_approval(store, approval_id).status == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_a_completed_execution_clears_the_marker(self, store):
        approval_id = await self._pending(store)
        await async_mark_execution_started(store, approval_id)

        await async_update_approval_status(
            store, approval_id, status=STATUS_APPROVED, approved_by_user_id="admin",
        )

        entry = store.get_pending_approvals()[0]
        assert "execution_started_at" not in entry
        # And a later startup has nothing to reconcile.
        assert await async_reconcile_interrupted_approvals(store) == []

    @pytest.mark.asyncio
    async def test_an_unwritable_store_refuses_rather_than_running_unmarked(self, store):
        """Fail CLOSED, which reverses this test's original assertion.

        It used to require that an unwritable store not block an approval the
        admin had already authorized. That sounds right and is wrong: the same
        store has to persist the terminal status afterwards, so a disk that
        cannot take the marker cannot record the outcome either. Executing anyway
        buys nothing and leaves a pending record whose action HAS run with no
        trace saying so, which is the replay this marker exists to prevent, made
        permanent instead of crash-dependent.
        """
        approval_id = await self._pending(store)
        store.async_save = AsyncMock(side_effect=OSError("disk full"))

        assert await async_mark_execution_started(store, approval_id) is False
        # Still pending and still approvable once the disk is writable, with no
        # half-written marker left to be reconciled away at the next startup.
        assert get_approval(store, approval_id).status == STATUS_PENDING
        assert "execution_started_at" not in store.get_pending_approvals()[0]

    @pytest.mark.asyncio
    async def test_a_durable_marker_reports_success(self, store):
        approval_id = await self._pending(store)
        assert await async_mark_execution_started(store, approval_id) is True

    @pytest.mark.asyncio
    async def test_an_already_resolved_approval_reports_failure(self, store):
        # Nothing to mark, so nothing may run: the caller treats False as "do not
        # execute", and a terminal record must never be executed again.
        approval_id = await self._pending(store)
        await async_update_approval_status(
            store, approval_id, status=STATUS_APPROVED, approved_by_user_id="admin")

        assert await async_mark_execution_started(store, approval_id) is False

    @pytest.mark.asyncio
    async def test_the_marker_survives_a_store_round_trip(self, store):
        # It is only worth anything if it is still there in the NEXT process.
        approval_id = await self._pending(store)
        await async_mark_execution_started(store, approval_id)

        reloaded = PendingApproval.from_dict(store.get_pending_approvals()[0])

        assert reloaded.execution_started_at is not None
        assert PendingApproval.from_dict(
            reloaded.to_dict(redact_args=False)
        ).execution_started_at == reloaded.execution_started_at
