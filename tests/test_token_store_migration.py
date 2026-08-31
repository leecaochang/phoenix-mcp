"""Tests for Phoenix token-store migrations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.phoenix_mcp.approvals import (
    REASON_EXECUTION_INTERRUPTED,
    async_reconcile_interrupted_approvals,
)
from custom_components.phoenix_mcp.const import (
    APPROVAL_REASON_FORMAT_INCOMPATIBLE,
    CAP_ALLOW,
    CAP_DENY,
    PERSONA_CUSTOM,
    STORAGE_VERSION,
)
from custom_components.phoenix_mcp.token_store import (
    _LEGACY_ALLOW_TO_CAP,
    _migrate_storage,
    _migrate_storage_v1_to_v2,
)


def _legacy_token() -> dict:
    return {
        "id": "tok-1",
        "name": "legacy",
        "token_hash": "x",
        "created_at": "2025-01-01T00:00:00+00:00",
        "created_by": "admin",
        "allow_restart": True,
        "allow_config_read": True,
        "allow_template_render": False,
        "allow_automation_write": False,
        "allow_script_write": False,
        "allow_physical_control": True,
        "allow_service_response": False,
        "allow_broadcast": False,
        "allow_log_read": True,
    }


class TestMigrationOnEmpty:
    def test_empty_dict_returns_false(self):
        assert _migrate_storage_v1_to_v2({}) is False

    def test_no_pending_approvals_then_added(self):
        raw = {"tokens": [], "archived_tokens": [], "settings": {}}
        changed = _migrate_storage_v1_to_v2(raw)
        assert changed is True
        assert raw["pending_approvals"] == []


class TestV2ApprovalMigration:
    def test_incompatible_volume_approvals_are_cancelled_once(self):
        approvals = [
            {
                "id": "legacy-mesa",
                "status": "pending",
                "tool_name": "call_service_mesa_approved",
                "args": {
                    "domain": "media_player",
                    "service": "volume_set",
                    "service_data": {"volume_level": 0.45},
                    "entity_id": ["media_player.one"],
                },
            },
            {
                "id": "unversioned-plan",
                "status": "pending",
                "tool_name": "HassSetVolumeRelative",
                "args": {"target_levels": []},
            },
            {
                "id": "current-plan",
                "status": "pending",
                "tool_name": "HassSetVolumeRelative",
                "args": {"_relative_volume_plan_version": 1},
            },
            {
                "id": "resolved-legacy",
                "status": "approved",
                "tool_name": "call_service_mesa_approved",
                "args": {"domain": "media_player", "service": "volume_set"},
            },
            {
                "id": "other-service",
                "status": "pending",
                "tool_name": "call_service_mesa_approved",
                "args": {"domain": "media_player", "service": "media_pause"},
            },
        ]
        raw = {"version": 2, "tokens": [], "pending_approvals": approvals}

        assert _migrate_storage(raw) is True

        by_id = {approval["id"]: approval for approval in approvals}
        assert raw["version"] == STORAGE_VERSION
        assert by_id["legacy-mesa"]["status"] == "cancelled"
        assert (
            by_id["legacy-mesa"]["rejected_reason"]
            == APPROVAL_REASON_FORMAT_INCOMPATIBLE
        )
        assert by_id["unversioned-plan"]["status"] == "cancelled"
        assert by_id["current-plan"]["status"] == "pending"
        assert by_id["resolved-legacy"]["status"] == "approved"
        assert by_id["other-service"]["status"] == "pending"
        assert raw["_invalidated_approval_notification_ids"] == [
            "legacy-mesa",
            "unversioned-plan",
        ]
        assert _migrate_storage(raw) is False

    async def test_execution_marker_survives_for_startup_reconciliation(self):
        started_at = "2026-08-31T00:00:00+00:00"
        approval = {
            "id": "interrupted-volume",
            "token_id": "token",
            "token_name": "test",
            "status": "pending",
            "tool_name": "call_service_mesa_approved",
            "cap_name": "mesa_control_mode",
            "args": {"domain": "media_player", "service": "volume_set"},
            "diff": {},
            "created_at": started_at,
            "expires_at": "2026-08-31T01:00:00+00:00",
            "request_id": "request",
            "execution_started_at": started_at,
        }
        raw = {"version": 2, "tokens": [], "pending_approvals": [approval]}

        assert _migrate_storage(raw) is True
        assert approval["status"] == "pending"
        assert approval["execution_started_at"] == started_at
        assert "_invalidated_approval_notification_ids" not in raw

        store = MagicMock()
        store.get_pending_approvals.return_value = raw["pending_approvals"]
        store.async_save = AsyncMock()
        reconciled = await async_reconcile_interrupted_approvals(store)

        assert len(reconciled) == 1
        assert approval["status"] == "failed"
        assert approval["rejected_reason"] == REASON_EXECUTION_INTERRUPTED
        assert "execution_started_at" not in approval
        store.async_save.assert_awaited_once()

    def test_v3_cancelled_record_gains_the_localized_reason(self):
        approval = {
            "id": "cancelled-by-v3",
            "status": "cancelled",
            "tool_name": "call_service_mesa_approved",
            "args": {"domain": "media_player", "service": "volume_set"},
        }
        raw = {"version": 3, "tokens": [], "pending_approvals": [approval]}

        assert _migrate_storage(raw) is True

        assert raw["version"] == STORAGE_VERSION
        assert (
            approval["rejected_reason"]
            == APPROVAL_REASON_FORMAT_INCOMPATIBLE
        )
        assert "_invalidated_approval_notification_ids" not in raw


class TestTokenMigration:
    def test_renames_allow_to_cap(self):
        raw = {"tokens": [_legacy_token()]}
        _migrate_storage_v1_to_v2(raw)
        token = raw["tokens"][0]
        # The v1 -> v2 migration owns only the legacy allow_* caps; caps added
        # after v2 are default-filled by TokenRecord.from_dict at load, not here.
        for cap in _LEGACY_ALLOW_TO_CAP.values():
            assert cap in token

    def test_true_becomes_allow(self):
        raw = {"tokens": [_legacy_token()]}
        _migrate_storage_v1_to_v2(raw)
        token = raw["tokens"][0]
        assert token["cap_restart"] == CAP_ALLOW
        assert token["cap_config_read"] == CAP_ALLOW
        assert token["cap_physical_control"] == CAP_ALLOW
        assert token["cap_log_read"] == CAP_ALLOW

    def test_false_becomes_deny(self):
        raw = {"tokens": [_legacy_token()]}
        _migrate_storage_v1_to_v2(raw)
        token = raw["tokens"][0]
        assert token["cap_template_render"] == CAP_DENY
        assert token["cap_automation_write"] == CAP_DENY
        assert token["cap_script_write"] == CAP_DENY
        assert token["cap_service_response"] == CAP_DENY
        assert token["cap_broadcast"] == CAP_DENY

    def test_persona_defaults_to_custom(self):
        raw = {"tokens": [_legacy_token()]}
        _migrate_storage_v1_to_v2(raw)
        assert raw["tokens"][0]["persona"] == PERSONA_CUSTOM

    def test_old_keys_dropped(self):
        raw = {"tokens": [_legacy_token()]}
        _migrate_storage_v1_to_v2(raw)
        token = raw["tokens"][0]
        for old_key in (
            "allow_restart", "allow_config_read", "allow_template_render",
            "allow_automation_write", "allow_script_write",
            "allow_physical_control", "allow_service_response",
            "allow_broadcast", "allow_log_read",
        ):
            assert old_key not in token

    def test_returns_true_when_migration_applied(self):
        raw = {"tokens": [_legacy_token()]}
        assert _migrate_storage_v1_to_v2(raw) is True


class TestMixedState:
    def test_already_migrated_token_is_left_alone(self):
        raw = {
            "tokens": [{
                "id": "tok-1",
                "name": "modern",
                "token_hash": "x",
                "created_at": "2025-01-01T00:00:00+00:00",
                "created_by": "admin",
                "cap_restart": "allow",
                "persona": "voice_assistant",
            }],
            "pending_approvals": [],
        }
        changed = _migrate_storage_v1_to_v2(raw)
        token = raw["tokens"][0]
        assert token["cap_restart"] == "allow"
        assert token["persona"] == "voice_assistant"
        # No new fields needed -> no change.
        assert changed is False

    def test_only_some_legacy_keys_present(self):
        # Partial legacy state: only one allow_ field present.
        raw = {
            "tokens": [{
                "id": "tok-1",
                "name": "partial",
                "token_hash": "x",
                "created_at": "2025-01-01T00:00:00+00:00",
                "created_by": "admin",
                "allow_restart": True,
            }],
        }
        changed = _migrate_storage_v1_to_v2(raw)
        assert changed is True
        token = raw["tokens"][0]
        assert token["cap_restart"] == "allow"
        assert "allow_restart" not in token
        assert token["persona"] == PERSONA_CUSTOM


class TestArchivedTokens:
    def test_legacy_keys_dropped_from_archives(self):
        # Spec says archived records do not retain capability flags;
        # if a legacy archive carried them, the migration drops them.
        raw = {
            "tokens": [],
            "archived_tokens": [{
                "id": "arc-1",
                "name": "old",
                "token_hash": "x",
                "created_at": "2025-01-01T00:00:00+00:00",
                "created_by": "admin",
                "revoked_at": "2025-02-01T00:00:00+00:00",
                "allow_restart": True,
                "allow_config_read": True,
            }],
        }
        _migrate_storage_v1_to_v2(raw)
        archived = raw["archived_tokens"][0]
        for old_key in ("allow_restart", "allow_config_read"):
            assert old_key not in archived
        # Capability migration is NOT applied to archives — they don't carry caps.
        assert "cap_restart" not in archived
