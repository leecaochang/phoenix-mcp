"""Tests for token_store.py."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import (
    CAP_ALLOW,
    CAP_DENY,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_REQUESTS,
    TOKEN_LENGTH,
    TOKEN_PREFIX,
)
from custom_components.phoenix_mcp.token_store import (
    ArchivedTokenRecord,
    GlobalSettings,
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
    token_name_slug as _slugify,
    hmac_compare,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PermissionNode
# ---------------------------------------------------------------------------


class TestPermissionNode:
    def test_default_state_is_grey(self):
        node = PermissionNode()
        assert node.state == "GREY"
        assert node.hint is None

    def test_round_trip(self):
        node = PermissionNode(state="GREEN", hint="Main light")
        assert PermissionNode.from_dict(node.to_dict()) == node

    def test_from_dict_missing_fields(self):
        node = PermissionNode.from_dict({})
        assert node.state == "GREY"
        assert node.hint is None


# ---------------------------------------------------------------------------
# PermissionTree
# ---------------------------------------------------------------------------


class TestPermissionTree:
    def test_empty_defaults(self):
        tree = PermissionTree()
        assert tree.domains == {}
        assert tree.devices == {}
        assert tree.entities == {}

    def test_round_trip(self):
        tree = PermissionTree(
            domains={"light": PermissionNode(state="GREEN")},
            entities={"sensor.temp": PermissionNode(state="YELLOW", hint="bedroom temp")},
        )
        restored = PermissionTree.from_dict(tree.to_dict())
        assert restored.domains["light"].state == "GREEN"
        assert restored.entities["sensor.temp"].hint == "bedroom temp"

    def test_from_dict_empty(self):
        tree = PermissionTree.from_dict({})
        assert tree.domains == {}


# ---------------------------------------------------------------------------
# TokenRecord
# ---------------------------------------------------------------------------


class TestTokenRecord:
    def _make(self, **kwargs):
        defaults = {
            "id": "test-uuid",
            "name": "test",
            "token_hash": "abc",
            "created_at": utcnow(),
            "created_by": "user1",
        }
        defaults.update(kwargs)
        return TokenRecord(**defaults)

    def test_is_valid_active(self):
        token = self._make()
        assert token.is_valid() is True

    def test_is_valid_revoked(self):
        token = self._make(revoked=True)
        assert token.is_valid() is False

    def test_is_expired_no_expiry(self):
        token = self._make()
        assert token.is_expired() is False

    def test_is_expired_past(self):
        token = self._make(expires_at=utcnow() - timedelta(seconds=1))
        assert token.is_expired() is True
        assert token.is_valid() is False

    def test_is_expired_future(self):
        token = self._make(expires_at=utcnow() + timedelta(days=1))
        assert token.is_expired() is False

    def test_round_trip(self):
        token = self._make(
            expires_at=utcnow() + timedelta(days=7),
            pass_through=True,
            cap_restart="allow",
            permissions=PermissionTree(domains={"light": PermissionNode(state="GREEN")}),
        )
        restored = TokenRecord.from_dict(token.to_storage_dict())
        assert restored.id == token.id
        assert restored.name == token.name
        assert restored.pass_through is True
        assert restored.cap_restart == "allow"
        assert restored.permissions.domains["light"].state == "GREEN"

    def test_from_dict_defaults(self):
        token = TokenRecord.from_dict({
            "id": "x",
            "name": "x",
            "token_hash": "x",
            "created_at": utcnow().isoformat(),
            "created_by": "user1",
        })
        assert token.rate_limit_requests == DEFAULT_RATE_LIMIT_REQUESTS
        assert token.rate_limit_burst == DEFAULT_RATE_LIMIT_BURST
        assert token.revoked is False
        assert token.pass_through is False
        assert token.announce_all_tools is False

    def test_from_dict_fills_new_caps_to_deny(self):
        # A token persisted before the capability expansion carries only the
        # original caps. Loading it must default every newly-added cap to deny
        # (the no-storage-bump upgrade path), never raise or leave it unset.
        token = TokenRecord.from_dict({
            "id": "x",
            "name": "x",
            "token_hash": "x",
            "created_at": utcnow().isoformat(),
            "created_by": "user1",
            "cap_config_read": CAP_ALLOW,
            "cap_automation_write": CAP_ALLOW,
        })
        assert token.cap_config_read == CAP_ALLOW
        assert token.cap_automation_write == CAP_ALLOW
        for cap_name in (
            "cap_search", "cap_registry_read", "cap_traces", "cap_diagnostics",
            "cap_scene_write", "cap_helper_write", "cap_integration_write",
            "cap_lovelace_write", "cap_registry_write", "cap_radio_write",
            "cap_backup", "cap_filesystem", "cap_yaml_edit",
        ):
            assert getattr(token, cap_name) == CAP_DENY

    def test_announce_all_tools_round_trip(self):
        token = self._make()
        token.announce_all_tools = True
        d = token.to_dict()
        assert d["announce_all_tools"] is True
        restored = TokenRecord.from_dict(token.to_storage_dict())
        assert restored.announce_all_tools is True

    def test_from_dict_naive_expires_at_normalized_to_utc(self):
        # A timezone-less expires_at (hand-edited storage, or a record written
        # before API input normalization) must load aware: a naive value makes
        # every comparison against aware UTC raise TypeError, and the startup
        # expiry sweep runs during setup, so one bad record aborted the whole
        # integration.
        token = TokenRecord.from_dict({
            "id": "x", "name": "x", "token_hash": "x",
            "created_at": utcnow().isoformat(), "created_by": "user1",
            "expires_at": "2030-01-01T12:00:00",
        })
        assert token.expires_at is not None
        assert token.expires_at.tzinfo is not None
        assert token.is_expired() is False

        past = TokenRecord.from_dict({
            "id": "y", "name": "y", "token_hash": "y",
            "created_at": utcnow().isoformat(), "created_by": "user1",
            "expires_at": "2020-01-01T12:00:00",
        })
        assert past.is_expired() is True

    def test_from_dict_rate_limits_sanitized(self):
        def _load(requests, burst):
            return TokenRecord.from_dict({
                "id": "x", "name": "x", "token_hash": "x",
                "created_at": utcnow().isoformat(), "created_by": "user1",
                "rate_limit_requests": requests, "rate_limit_burst": burst,
            })

        # A negative limit would make the rate limiter fail every request with
        # an IndexError on an empty window; non-numeric values would raise at
        # comparison time. Both fall back to the defaults.
        bad = _load(-5, "abc")
        assert bad.rate_limit_requests == DEFAULT_RATE_LIMIT_REQUESTS
        assert bad.rate_limit_burst == DEFAULT_RATE_LIMIT_BURST

        oversized = _load(200_000, True)
        assert oversized.rate_limit_requests == DEFAULT_RATE_LIMIT_REQUESTS
        assert oversized.rate_limit_burst == DEFAULT_RATE_LIMIT_BURST

        # In-range values load verbatim, including a numeric string.
        ok = _load("30", 0)
        assert ok.rate_limit_requests == 30
        assert ok.rate_limit_burst == 0

    def test_from_dict_non_dict_permissions_load_empty(self):
        # Fail closed: a corrupt (non-object) permission tree loads as an empty
        # tree granting nothing, instead of raising AttributeError and killing
        # the record (or, pre-hardening, the whole store load).
        for garbage in (None, "corrupt", [1, 2], 7):
            token = TokenRecord.from_dict({
                "id": "x", "name": "x", "token_hash": "x",
                "created_at": utcnow().isoformat(), "created_by": "user1",
                "permissions": garbage,
            })
            assert token.permissions.domains == {}
            assert token.permissions.devices == {}
            assert token.permissions.entities == {}


# ---------------------------------------------------------------------------
# ArchivedTokenRecord
# ---------------------------------------------------------------------------


class TestArchivedTokenRecord:
    def test_round_trip(self):
        now = utcnow()
        record = ArchivedTokenRecord(
            id="a", name="b", token_hash="c",
            created_at=now, created_by="u", revoked_at=now,
            revoked=True,
        )
        restored = ArchivedTokenRecord.from_dict(record.to_storage_dict())
        assert restored.id == "a"
        assert restored.revoked is True


# ---------------------------------------------------------------------------
# GlobalSettings
# ---------------------------------------------------------------------------


class TestGlobalSettings:
    def test_defaults(self):
        s = GlobalSettings()
        assert s.kill_switch is False
        assert s.log_allowed is True
        assert s.notify_on_rate_limit is False
        assert (s.agentcli_conversation_style, s.agentcli_detail_level,
                s.agentcli_home_focused) == ("direct", "concise", False)
        assert (s.voice_agent_conversation_style, s.voice_agent_detail_level,
                s.voice_agent_home_focused) == ("direct", "concise", False)
        assert (s.ai_task_conversation_style, s.ai_task_detail_level) == (
            "direct", "balanced",
        )

    def test_round_trip(self):
        s = GlobalSettings(
            kill_switch=True, log_client_ip=False,
            agentcli_conversation_style="warm", agentcli_detail_level="detailed",
            agentcli_home_focused=True,
            voice_agent_conversation_style="technical", voice_agent_detail_level="balanced",
            voice_agent_home_focused=True,
            ai_task_conversation_style="calm_guide", ai_task_detail_level="concise",
        )
        restored = GlobalSettings.from_dict(s.to_dict())
        assert restored.kill_switch is True
        assert restored.log_client_ip is False
        assert restored.agentcli_conversation_style == "warm"
        assert restored.agentcli_detail_level == "detailed"
        assert restored.agentcli_home_focused is True
        assert restored.voice_agent_conversation_style == "technical"
        assert restored.voice_agent_detail_level == "balanced"
        assert restored.voice_agent_home_focused is True
        assert restored.ai_task_conversation_style == "calm_guide"
        assert restored.ai_task_detail_level == "concise"

    def test_conversation_behavior_invalid_storage_uses_safe_defaults(self):
        s = GlobalSettings.from_dict({
            "agentcli_conversation_style": "invented",
            "agentcli_detail_level": "huge",
            "voice_agent_conversation_style": 3,
            "voice_agent_detail_level": None,
            "ai_task_conversation_style": "",
            "ai_task_detail_level": "verbose",
        })
        assert (s.agentcli_conversation_style, s.agentcli_detail_level) == (
            "direct", "concise",
        )
        assert (s.voice_agent_conversation_style, s.voice_agent_detail_level) == (
            "direct", "concise",
        )
        assert (s.ai_task_conversation_style, s.ai_task_detail_level) == (
            "direct", "balanced",
        )

    def test_agentcli_max_iterations_default_and_clamp(self):
        from custom_components.phoenix_mcp.const import AGENTCLI_MAX_ITERATIONS
        assert GlobalSettings().agentcli_max_iterations == AGENTCLI_MAX_ITERATIONS
        assert GlobalSettings(agentcli_max_iterations=40).to_dict()["agentcli_max_iterations"] == 40
        # Load-side clamp: out-of-range and non-numeric fall to the bounds/default.
        assert GlobalSettings.from_dict({"agentcli_max_iterations": 9999}).agentcli_max_iterations == 100
        assert GlobalSettings.from_dict({"agentcli_max_iterations": 1}).agentcli_max_iterations == 3
        assert GlobalSettings.from_dict({"agentcli_max_iterations": "x"}).agentcli_max_iterations == AGENTCLI_MAX_ITERATIONS

    def test_mesa_inject_default_off(self):
        assert GlobalSettings().mesa_inject_enabled is False

    def test_mesa_inject_round_trip(self):
        s = GlobalSettings(mesa_inject_enabled=True)
        assert GlobalSettings.from_dict(s.to_dict()).mesa_inject_enabled is True

    def test_assist_binding_default_none(self):
        assert GlobalSettings().assist_bound_token_id is None

    def test_assist_binding_round_trip(self):
        s = GlobalSettings(assist_bound_token_id="tok-123")
        assert GlobalSettings.from_dict(s.to_dict()).assist_bound_token_id == "tok-123"

    def test_assist_binding_empty_string_coerces_to_none(self):
        assert GlobalSettings.from_dict({"assist_bound_token_id": ""}).assist_bound_token_id is None
        assert GlobalSettings.from_dict({"assist_bound_token_id": 5}).assist_bound_token_id is None

    def test_voice_agent_defaults(self):
        s = GlobalSettings()
        assert s.voice_agent_enabled is False
        assert s.voice_agent_token_id is None
        assert s.voice_agent_provider_id is None
        assert s.voice_agent_model is None
        assert s.voice_agent_pipeline_id is None

    def test_voice_agent_round_trip(self):
        s = GlobalSettings(
            voice_agent_enabled=True, voice_agent_token_id="tok",
            voice_agent_provider_id="inst", voice_agent_model="claude-opus-4-8",
            voice_agent_pipeline_id="pl1",
        )
        r = GlobalSettings.from_dict(s.to_dict())
        assert r.voice_agent_enabled is True
        assert r.voice_agent_token_id == "tok"
        assert r.voice_agent_provider_id == "inst"
        assert r.voice_agent_model == "claude-opus-4-8"
        assert r.voice_agent_pipeline_id == "pl1"

    def test_voice_agent_empty_strings_coerce_to_none(self):
        r = GlobalSettings.from_dict({"voice_agent_token_id": "", "voice_agent_model": ""})
        assert r.voice_agent_token_id is None
        assert r.voice_agent_model is None

    def test_ai_task_defaults(self):
        s = GlobalSettings()
        assert s.ai_task_enabled is False
        assert s.ai_task_token_id is None
        assert s.ai_task_provider_id is None
        assert s.ai_task_model is None

    def test_ai_task_round_trip(self):
        s = GlobalSettings(
            ai_task_enabled=True, ai_task_token_id="tok",
            ai_task_provider_id="inst", ai_task_model="claude-opus-4-8",
        )
        r = GlobalSettings.from_dict(s.to_dict())
        assert r.ai_task_enabled is True
        assert r.ai_task_token_id == "tok"
        assert r.ai_task_provider_id == "inst"
        assert r.ai_task_model == "claude-opus-4-8"

    def test_ai_task_empty_strings_coerce_to_none(self):
        r = GlobalSettings.from_dict({"ai_task_token_id": "", "ai_task_provider_id": ""})
        assert r.ai_task_token_id is None
        assert r.ai_task_provider_id is None


# ---------------------------------------------------------------------------
# TokenStore - creation and loading
# ---------------------------------------------------------------------------


class TestTokenStoreLoad:
    async def test_empty_load(self, token_store):
        assert token_store.list_tokens() == []
        assert token_store.list_archived() == []

    async def test_load_with_existing_data(self, hass):
        now = utcnow()
        existing = {
            "version": 1,
            "tokens": [{
                "id": "tok1",
                "name": "mytoken",
                "token_hash": "hash1",
                "created_at": now.isoformat(),
                "created_by": "admin",
            }],
            "archived_tokens": [],
            "settings": {"kill_switch": True},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()

        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)

        tokens = store.list_tokens()
        assert len(tokens) == 1
        assert tokens[0].name == "mytoken"
        assert store.get_settings().kill_switch is True

    async def test_load_skips_unparseable_records_and_keeps_siblings(self, hass):
        # The corrupt-record handler must skip ANY record from_dict cannot
        # parse, whatever the exception type. Nested garbage inside the
        # permission tree raises AttributeError, which the old
        # (KeyError, TypeError, ValueError) catch let escape, aborting setup.
        now = utcnow()
        good = {
            "id": "tok-good", "name": "good", "token_hash": "h1",
            "created_at": now.isoformat(), "created_by": "admin",
        }
        nested_corrupt = dict(
            good, id="tok-bad", name="bad", token_hash="h2",
            permissions={"domains": ["not-a-node-map"]},
        )
        existing = {
            "version": 2,
            "tokens": [nested_corrupt, "not-even-a-dict", good],
            "archived_tokens": ["garbage"],
            "settings": {},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()

        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)

        tokens = store.list_tokens()
        assert [t.id for t in tokens] == ["tok-good"]
        assert store.list_archived() == []

    @pytest.mark.parametrize("bad_created_at", ["not-a-date", "", "2026-13-45T99:99:99"])
    async def test_load_skips_a_record_whose_required_datetime_is_unparseable(
        self, hass, bad_created_at
    ):
        """created_at is non-optional, so an unparseable one must skip the record.

        _parse_dt returns None for anything it cannot parse, which would build a
        TokenRecord whose non-optional created_at is None. That does not raise at
        construction, so the record loads looking valid and crashes much later at
        an arbitrary expiry comparison or .isoformat(). Raising routes it into
        the same skip-and-warn handler as every other corruption.
        """
        now = utcnow()
        good = {
            "id": "tok-good", "name": "good", "token_hash": "h1",
            "created_at": now.isoformat(), "created_by": "admin",
        }
        bad = dict(good, id="tok-bad", name="bad", token_hash="h2",
                   created_at=bad_created_at)
        existing = {"version": 2, "tokens": [bad, good], "settings": {}}
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()

        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)

        assert [t.id for t in store.list_tokens()] == ["tok-good"]

    async def test_load_skips_an_archived_record_with_an_unparseable_revoked_at(self, hass):
        """Same rule for an archived record's revoked_at, also non-optional."""
        now = utcnow()
        archived_good = {
            "id": "arch-good", "name": "g", "token_hash": "h1",
            "created_at": now.isoformat(), "created_by": "admin",
            "revoked_at": now.isoformat(), "revoked": True,
        }
        archived_bad = dict(archived_good, id="arch-bad", name="b", token_hash="h2",
                            revoked_at="nonsense")
        existing = {
            "version": 2, "tokens": [],
            "archived_tokens": [archived_bad, archived_good], "settings": {},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()

        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)

        assert [t.id for t in store.list_archived()] == ["arch-good"]

    async def test_load_non_dict_root_ignored(self, hass):
        # A storage root that is not an object (corrupted file) must not crash
        # setup: migration and every collection read assume a dict root.
        for corrupt_root in ([1, 2, 3], "corrupt", 42):
            mock_store = AsyncMock()
            mock_store.async_load = AsyncMock(return_value=corrupt_root)
            mock_store.async_save = AsyncMock()
            with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
                store = await TokenStore.async_create(hass)
            assert store.list_tokens() == []
            assert store.list_archived() == []
            assert store.get_pending_approvals() == []
            mock_store.async_save.assert_awaited()  # cleaned form healed to disk

    async def test_load_wrong_typed_collections_reset(self, hass):
        # Each top-level collection of the wrong type is reset to an empty
        # container instead of crashing (tokens:int -> TypeError in migration;
        # settings/entity_hints of the wrong type -> AttributeError; a string
        # pending_approvals would silently become a list of characters).
        existing = {
            "version": 2,
            "tokens": 7,
            "archived_tokens": {"x": 1},
            "pending_approvals": "abc",
            "settings": [],
            "entity_hints": ["not", "a", "map"],
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()
        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)
        assert store.list_tokens() == []
        assert store.list_archived() == []
        assert store.get_pending_approvals() == []
        assert store.get_settings().kill_switch is False
        mock_store.async_save.assert_awaited()

    async def test_load_valid_collections_untouched_no_spurious_save(self, hass):
        # A well-formed v2 store must load verbatim with no heal-save fired by
        # normalization (the token carries persona, so migration is a no-op too).
        now = utcnow()
        existing = {
            "version": 2,
            "tokens": [{
                "id": "tok1", "name": "mytoken", "token_hash": "h",
                "created_at": now.isoformat(), "created_by": "admin",
                "persona": "custom",
            }],
            "archived_tokens": [],
            "pending_approvals": [],
            "settings": {},
            "entity_hints": {"light.kitchen": "hint"},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()
        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)
        assert [t.id for t in store.list_tokens()] == ["tok1"]
        mock_store.async_save.assert_not_awaited()

    async def test_load_falsy_root_healed_but_fresh_install_not(self, hass):
        # `[]`, `""`, and `0` are corrupt roots and should heal to disk; None
        # (no stored file yet) is a fresh install and must NOT trigger a save.
        for corrupt_root in ([], "", 0):
            mock_store = AsyncMock()
            mock_store.async_load = AsyncMock(return_value=corrupt_root)
            mock_store.async_save = AsyncMock()
            with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
                store = await TokenStore.async_create(hass)
            assert store.list_tokens() == []
            mock_store.async_save.assert_awaited()

        fresh = AsyncMock()
        fresh.async_load = AsyncMock(return_value=None)
        fresh.async_save = AsyncMock()
        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=fresh):
            store = await TokenStore.async_create(hass)
        assert store.list_tokens() == []
        fresh.async_save.assert_not_awaited()

    async def test_load_discards_non_dict_approval_members(self, hass):
        # A correctly-typed pending_approvals LIST can still hold a string/int
        # member; the startup expiry sweep calls entry.get(...) on each, which
        # would abort setup. Non-dict members are discarded (and healed).
        good_approval = {"id": "ap1", "token_id": "t", "status": "pending"}
        existing = {
            "version": 2,
            "tokens": [],
            "archived_tokens": [],
            "pending_approvals": [good_approval, "corrupt", 7, None],
            "settings": {},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()
        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)
        assert store.get_pending_approvals() == [good_approval]
        mock_store.async_save.assert_awaited()  # cleaned form healed

    async def test_load_non_bool_privilege_flags_fail_closed(self, hass):
        # A stored access-widening flag that is a truthy non-bool (e.g. the
        # string "false") must load False, never silently enable pass-through.
        now = utcnow()
        existing = {
            "version": 2,
            "tokens": [{
                "id": "tok1", "name": "t", "token_hash": "h",
                "created_at": now.isoformat(), "created_by": "admin",
                "persona": "custom",
                "pass_through": "false",
                "use_assist_exposure": "true",
                "announce_all_tools": 1,
            }],
            "archived_tokens": [],
            "settings": {},
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=existing)
        mock_store.async_save = AsyncMock()
        with patch("custom_components.phoenix_mcp.token_store._PhoenixStore", return_value=mock_store):
            store = await TokenStore.async_create(hass)
        tok = store.list_tokens()[0]
        assert tok.pass_through is False
        assert tok.use_assist_exposure is False
        assert tok.announce_all_tools is False


# ---------------------------------------------------------------------------
# TokenStore - token creation
# ---------------------------------------------------------------------------


class TestTokenCreation:
    async def test_create_returns_raw_token_and_record(self, token_store):
        record, raw = await token_store.async_create_token("mytoken", "user1")
        assert raw.startswith(TOKEN_PREFIX)
        assert len(raw) == TOKEN_LENGTH
        assert record.name == "mytoken"
        assert record.created_by == "user1"

    async def test_raw_token_not_stored(self, token_store):
        record, raw = await token_store.async_create_token("t1", "u")
        expected_hash = _sha256(raw)
        assert record.token_hash == expected_hash
        assert raw not in record.token_hash

    async def test_create_saves_immediately(self, token_store, mock_store):
        await token_store.async_create_token("t1", "u")
        mock_store.async_save.assert_called()

    async def test_create_with_expiry(self, token_store):
        expiry = utcnow() + timedelta(days=30)
        record, _ = await token_store.async_create_token("t1", "u", expires_at=expiry)
        assert record.expires_at == expiry

    async def test_create_pass_through(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u", pass_through=True)
        assert record.pass_through is True

    async def test_burst_coerced_to_zero_when_requests_zero(self, token_store):
        record, _ = await token_store.async_create_token(
            "t1", "u", rate_limit_requests=0, rate_limit_burst=10
        )
        assert record.rate_limit_burst == 0

    async def test_burst_preserved_when_requests_nonzero(self, token_store):
        record, _ = await token_store.async_create_token(
            "t1", "u", rate_limit_requests=30, rate_limit_burst=5
        )
        assert record.rate_limit_burst == 5

    async def test_default_rate_limits(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        assert record.rate_limit_requests == DEFAULT_RATE_LIMIT_REQUESTS
        assert record.rate_limit_burst == DEFAULT_RATE_LIMIT_BURST

    async def test_default_capability_flags_all_deny(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        assert record.cap_automation_write == "deny"
        assert record.cap_config_read == "deny"
        assert record.cap_template_render == "deny"
        assert record.cap_restart == "deny"
        assert record.persona == "custom"

    async def test_multiple_tokens_have_unique_ids(self, token_store):
        r1, _ = await token_store.async_create_token("t1", "u")
        r2, _ = await token_store.async_create_token("t2", "u")
        assert r1.id != r2.id

    async def test_multiple_tokens_have_unique_hashes(self, token_store):
        r1, _ = await token_store.async_create_token("t1", "u")
        r2, _ = await token_store.async_create_token("t2", "u")
        assert r1.token_hash != r2.token_hash


# ---------------------------------------------------------------------------
# TokenStore - lookup
# ---------------------------------------------------------------------------


class TestTokenLookup:
    async def test_get_by_id(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        found = token_store.get_token_by_id(record.id)
        assert found is not None
        assert found.id == record.id

    async def test_get_by_id_missing(self, token_store):
        assert token_store.get_token_by_id("nonexistent") is None

    async def test_get_by_hash(self, token_store):
        record, raw = await token_store.async_create_token("t1", "u")
        presented_hash = _sha256(raw)
        found = token_store.get_token_by_hash(presented_hash)
        assert found is not None
        assert found.id == record.id

    async def test_get_by_hash_wrong_value(self, token_store):
        await token_store.async_create_token("t1", "u")
        assert token_store.get_token_by_hash(_sha256("wrong")) is None

    async def test_get_archived_by_hash(self, token_store):
        record, raw = await token_store.async_create_token("t1", "u")
        await token_store.async_archive_token(record.id, revoked=True)
        presented_hash = _sha256(raw)
        found = token_store.get_archived_by_hash(presented_hash)
        assert found is not None
        assert found.id == record.id


# ---------------------------------------------------------------------------
# TokenStore - listing
# ---------------------------------------------------------------------------


class TestTokenListing:
    async def test_list_tokens_empty(self, token_store):
        assert token_store.list_tokens() == []

    async def test_list_tokens_contains_created(self, token_store):
        r1, _ = await token_store.async_create_token("t1", "u")
        r2, _ = await token_store.async_create_token("t2", "u")
        ids = {t.id for t in token_store.list_tokens()}
        assert r1.id in ids
        assert r2.id in ids

    async def test_active_token_count(self, token_store):
        assert token_store.active_token_count() == 0
        await token_store.async_create_token("t1", "u")
        assert token_store.active_token_count() == 1
        await token_store.async_create_token("t2", "u")
        assert token_store.active_token_count() == 2

    async def test_list_archived_empty(self, token_store):
        assert token_store.list_archived() == []


# ---------------------------------------------------------------------------
# TokenStore - archival
# ---------------------------------------------------------------------------


class TestTokenArchival:
    async def test_revoke_moves_to_archived(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_archive_token(record.id, revoked=True)
        assert token_store.get_token_by_id(record.id) is None
        archived = token_store.list_archived()
        assert len(archived) == 1
        assert archived[0].id == record.id
        assert archived[0].revoked is True

    async def test_expire_archived_with_revoked_false(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_archive_token(record.id, revoked=False)
        archived = token_store.list_archived()
        assert archived[0].revoked is False

    async def test_archive_saves_immediately(self, token_store, mock_store):
        mock_store.async_save.reset_mock()
        record, _ = await token_store.async_create_token("t1", "u")
        mock_store.async_save.reset_mock()
        await token_store.async_archive_token(record.id, revoked=True)
        mock_store.async_save.assert_called()

    async def test_archive_nonexistent_returns_none(self, token_store):
        result = await token_store.async_archive_token("no-such-id", revoked=True)
        assert result is None

    async def test_archive_retains_audit_fields(self, token_store):
        expiry = utcnow() + timedelta(days=1)
        record, _ = await token_store.async_create_token("t1", "u", expires_at=expiry)
        token_store.update_last_used(record.id, utcnow())
        archived = await token_store.async_archive_token(record.id, revoked=True)
        assert archived.name == "t1"
        assert archived.created_by == "u"
        assert archived.expires_at == expiry
        assert archived.last_used_at is not None

    async def test_permission_tree_not_in_archived(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        archived = await token_store.async_archive_token(record.id, revoked=True)
        assert not hasattr(archived, "permissions")


# ---------------------------------------------------------------------------
# TokenStore - patching
# ---------------------------------------------------------------------------


class TestTokenPatch:
    async def test_patch_pass_through(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_patch_token(record.id, pass_through=True)
        assert updated.pass_through is True

    async def test_patch_announce_all_tools(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        assert record.announce_all_tools is False
        updated = await token_store.async_patch_token(record.id, announce_all_tools=True)
        assert updated.announce_all_tools is True

    async def test_patch_rate_limit(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_patch_token(
            record.id, rate_limit_requests=30, rate_limit_burst=5
        )
        assert updated.rate_limit_requests == 30
        assert updated.rate_limit_burst == 5

    async def test_patch_rate_requests_zero_coerces_burst(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u", rate_limit_burst=10)
        updated = await token_store.async_patch_token(record.id, rate_limit_requests=0)
        assert updated.rate_limit_burst == 0

    async def test_patch_capability_flags(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_patch_token(
            record.id,
            cap_restart="allow",
            cap_config_read="allow",
            cap_automation_write="allow",
            cap_template_render="allow",
        )
        assert updated.cap_restart == "allow"
        assert updated.cap_config_read == "allow"
        assert updated.cap_automation_write == "allow"
        assert updated.cap_template_render == "allow"

    async def test_patch_cap_change_after_persona_drops_to_custom(self, token_store):
        """Applying a persona and then changing any cap should auto-switch persona to custom
        (or to another matching preset if the new state happens to match one).
        """
        from custom_components.phoenix_mcp.personas import get_persona_caps

        record, _ = await token_store.async_create_token("t1", "u")
        # Seed the voice_assistant persona.
        await token_store.async_patch_token(
            record.id,
            persona="voice_assistant",
            **get_persona_caps("voice_assistant"),
        )
        assert token_store.get_token_by_id(record.id).persona == "voice_assistant"

        # Override cap_automation_write (was deny in voice_assistant). Persona should re-derive.
        updated = await token_store.async_patch_token(
            record.id,
            cap_automation_write="allow",
        )
        assert updated.cap_automation_write == "allow"
        assert updated.persona == "custom"

    async def test_patch_cap_change_can_promote_to_matching_persona(self, token_store):
        """If post-change caps happen to match a different preset exactly, detect that preset."""
        from custom_components.phoenix_mcp.personas import get_persona_caps

        record, _ = await token_store.async_create_token("t1", "u")
        # Start from automation_builder.
        await token_store.async_patch_token(
            record.id,
            persona="automation_builder",
            **get_persona_caps("automation_builder"),
        )
        assert token_store.get_token_by_id(record.id).persona == "automation_builder"

        # Patch exactly the caps that differ between automation_builder and
        # power_user; the resulting set matches power_user, so the label promotes.
        ab = get_persona_caps("automation_builder")
        pu = get_persona_caps("power_user")
        diff = {cap: mode for cap, mode in pu.items() if ab.get(cap) != mode}
        updated = await token_store.async_patch_token(record.id, **diff)
        assert updated.persona == "power_user"

    async def test_patch_non_cap_change_does_not_touch_persona(self, token_store):
        """Patching only rate-limit fields should NOT trigger persona re-derivation."""
        from custom_components.phoenix_mcp.personas import get_persona_caps

        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_token(
            record.id,
            persona="voice_assistant",
            **get_persona_caps("voice_assistant"),
        )
        updated = await token_store.async_patch_token(record.id, rate_limit_requests=30)
        assert updated.persona == "voice_assistant"
        assert updated.rate_limit_requests == 30

    async def test_patch_saves_immediately(self, token_store, mock_store):
        record, _ = await token_store.async_create_token("t1", "u")
        mock_store.async_save.reset_mock()
        await token_store.async_patch_token(record.id, cap_restart="allow")
        mock_store.async_save.assert_called()

    async def test_patch_nonexistent_returns_none(self, token_store):
        result = await token_store.async_patch_token("no-such-id", cap_restart="allow")
        assert result is None

    async def test_patch_applies_name_and_ignores_immutable(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_token(record.id, name="renamed-x", created_by="hacker")
        fetched = token_store.get_token_by_id(record.id)
        assert fetched.name == "renamed-x"   # name is now mutable (rename)
        assert fetched.created_by == "u"     # created_by stays immutable (ignored)

    async def test_name_slug_exists_excludes_self(self, token_store):
        a, _ = await token_store.async_create_token("alpha", "u")
        await token_store.async_create_token("beta", "u")
        assert token_store.name_slug_exists("beta") is True
        assert token_store.name_slug_exists("beta", exclude_token_id=a.id) is True   # beta is a different token
        assert token_store.name_slug_exists("alpha", exclude_token_id=a.id) is False  # only match is self


# ---------------------------------------------------------------------------
# TokenStore - permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    async def test_set_full_permission_tree(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        tree = PermissionTree(
            domains={"light": PermissionNode(state="GREEN")},
            entities={"sensor.temp": PermissionNode(state="YELLOW", hint="temp sensor")},
        )
        updated = await token_store.async_set_permissions(record.id, tree)
        assert updated.permissions.domains["light"].state == "GREEN"
        assert updated.permissions.entities["sensor.temp"].hint == "temp sensor"

    async def test_patch_permission_node_set(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_permission_node(
            record.id, "domains", "light", "GREEN", hint="ceiling lights"
        )
        token = token_store.get_token_by_id(record.id)
        assert token.permissions.domains["light"].state == "GREEN"
        assert token.permissions.domains["light"].hint == "ceiling lights"

    async def test_patch_permission_node_grey_removes_node(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_permission_node(record.id, "domains", "light", "GREEN")
        await token_store.async_patch_permission_node(record.id, "domains", "light", "GREY")
        token = token_store.get_token_by_id(record.id)
        assert "light" not in token.permissions.domains

    async def test_patch_permission_node_invalid_type_returns_none(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        result = await token_store.async_patch_permission_node(
            record.id, "invalid_type", "light", "GREEN"
        )
        assert result is None

    async def test_permissions_save_immediately(self, token_store, mock_store):
        record, _ = await token_store.async_create_token("t1", "u")
        mock_store.async_save.reset_mock()
        await token_store.async_patch_permission_node(record.id, "domains", "light", "GREEN")
        mock_store.async_save.assert_called()

    async def test_set_permissions_nonexistent_returns_none(self, token_store):
        result = await token_store.async_set_permissions("no-such-id", PermissionTree())
        assert result is None


# ---------------------------------------------------------------------------
# TokenStore - last_used_at
# ---------------------------------------------------------------------------


class TestLastUsed:
    async def test_update_last_used_in_memory(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        ts = utcnow()
        token_store.update_last_used(record.id, ts)
        token = token_store.get_token_by_id(record.id)
        assert token.last_used_at == ts

    async def test_update_last_used_nonexistent_no_error(self, token_store):
        token_store.update_last_used("no-such-id", utcnow())

    async def test_flush_last_used_calls_save(self, token_store, mock_store):
        mock_store.async_save.reset_mock()
        await token_store.async_flush_last_used()
        mock_store.async_save.assert_called_once()


# ---------------------------------------------------------------------------
# TokenStore - archived deletion
# ---------------------------------------------------------------------------


class TestArchivedDeletion:
    async def test_delete_archived_removes_record(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_archive_token(record.id, revoked=True)
        result = await token_store.async_delete_archived(record.id)
        assert result is True
        assert token_store.list_archived() == []

    async def test_delete_archived_saves_immediately(self, token_store, mock_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_archive_token(record.id, revoked=True)
        mock_store.async_save.reset_mock()
        await token_store.async_delete_archived(record.id)
        mock_store.async_save.assert_called()

    async def test_delete_archived_nonexistent_returns_false(self, token_store):
        result = await token_store.async_delete_archived("no-such-id")
        assert result is False


# ---------------------------------------------------------------------------
# TokenStore - settings
# ---------------------------------------------------------------------------


class TestSettings:
    async def test_get_settings_defaults(self, token_store):
        settings = token_store.get_settings()
        assert settings.kill_switch is False
        assert settings.log_allowed is True

    async def test_patch_settings(self, token_store):
        updated = await token_store.async_patch_settings(kill_switch=True, log_client_ip=False)
        assert updated.kill_switch is True
        assert updated.log_client_ip is False
        assert token_store.get_settings().kill_switch is True

    async def test_patch_settings_saves_immediately(self, token_store, mock_store):
        mock_store.async_save.reset_mock()
        await token_store.async_patch_settings(kill_switch=True)
        mock_store.async_save.assert_called()


# ---------------------------------------------------------------------------
# TokenStore - wipe
# ---------------------------------------------------------------------------


class TestWipe:
    async def test_wipe_clears_all(self, token_store):
        await token_store.async_create_token("t1", "u")
        await token_store.async_create_token("t2", "u")
        await token_store.async_patch_settings(
            kill_switch=True,
            agentcli_conversation_style="lively",
            agentcli_detail_level="detailed",
            agentcli_home_focused=True,
            voice_agent_conversation_style="warm",
            voice_agent_detail_level="balanced",
            voice_agent_home_focused=True,
            ai_task_conversation_style="technical",
            ai_task_detail_level="concise",
        )
        await token_store.async_wipe()
        assert token_store.list_tokens() == []
        assert token_store.list_archived() == []
        settings = token_store.get_settings()
        assert settings.kill_switch is False
        assert (settings.agentcli_conversation_style, settings.agentcli_detail_level,
                settings.agentcli_home_focused) == ("direct", "concise", False)
        assert (settings.voice_agent_conversation_style, settings.voice_agent_detail_level,
                settings.voice_agent_home_focused) == ("direct", "concise", False)
        assert (settings.ai_task_conversation_style, settings.ai_task_detail_level) == (
            "direct", "balanced",
        )

    async def test_wipe_saves_immediately(self, token_store, mock_store):
        mock_store.async_save.reset_mock()
        await token_store.async_wipe()
        mock_store.async_save.assert_called()

    async def test_wipe_clears_pending_approvals(self, token_store):
        # An orphaned pending approval must not survive a wipe (and thus must not
        # be persisted back to disk by the async_save inside async_wipe).
        token_store.set_pending_approvals([
            {"id": "ap1", "token_id": "gone", "status": "pending"},
        ])
        await token_store.async_wipe()
        assert token_store.get_pending_approvals() == []


# ---------------------------------------------------------------------------
# TokenStore - slug uniqueness
# ---------------------------------------------------------------------------


class TestSlugUniqueness:
    async def test_same_name_slug_detected(self, token_store):
        await token_store.async_create_token("my-token", "u")
        assert token_store.name_slug_exists("my-token") is True
        assert token_store.name_slug_exists("my_token") is True

    async def test_different_name_not_collision(self, token_store):
        await token_store.async_create_token("my-token", "u")
        assert token_store.name_slug_exists("other-token") is False

    async def test_slugify(self):
        assert _slugify("my-token") == "my_token"
        assert _slugify("MyToken") == "mytoken"
        assert _slugify("my_token") == "my_token"


# ---------------------------------------------------------------------------
# hmac_compare
# ---------------------------------------------------------------------------


class TestHmacCompare:
    def test_equal_hashes(self):
        h = _sha256("phx_abc123")
        assert hmac_compare(h, h) is True

    def test_unequal_hashes(self):
        h1 = _sha256("phx_abc")
        h2 = _sha256("phx_xyz")
        assert hmac_compare(h1, h2) is False

    def test_empty_strings(self):
        assert hmac_compare("", "") is True

    def test_one_empty(self):
        assert hmac_compare("abc", "") is False


# ---------------------------------------------------------------------------
# Concurrent PATCH (async_lock)
# ---------------------------------------------------------------------------


class TestConcurrentPatch:
    async def test_async_lock_prevents_interleaving(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        results = []

        async def patch_and_record(value):
            async with token_store.async_lock:
                await token_store.async_patch_token(
                    record.id, rate_limit_requests=value
                )
                token = token_store.get_token_by_id(record.id)
                results.append(token.rate_limit_requests)

        await asyncio.gather(patch_and_record(10), patch_and_record(20))
        assert len(results) == 2
        assert set(results) == {10, 20}

    async def test_async_lock_is_asyncio_lock(self, token_store):
        assert isinstance(token_store.async_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# Token settings presets (workspace model)
# ---------------------------------------------------------------------------


class TestPresets:
    async def test_add_preset_snapshots_current_and_marks_active(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW)
        await token_store.async_set_permissions(
            record.id,
            PermissionTree(entities={"light.a": PermissionNode(state="GREEN")}),
        )
        updated = await token_store.async_add_preset(record.id, "Baseline")
        assert len(updated.presets) == 1
        preset = updated.presets[0]
        assert updated.active_preset_id == preset.id
        assert preset.name == "Baseline"
        assert preset.caps["cap_restart"] == CAP_ALLOW
        assert preset.permissions.entities["light.a"].state == "GREEN"
        # Deep copy: mutating the live tree must not touch the preset.
        updated.permissions.entities["light.a"].state = "RED"
        assert preset.permissions.entities["light.a"].state == "GREEN"

    async def test_preset_round_trip_through_storage(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_add_preset(record.id, "Baseline")
        token = token_store.get_token_by_id(record.id)
        loaded = TokenRecord.from_dict(token.to_storage_dict())
        assert len(loaded.presets) == 1
        assert loaded.presets[0].to_dict() == token.presets[0].to_dict()
        assert loaded.active_preset_id == token.active_preset_id
        assert loaded.settings_version == token.settings_version
        assert loaded.tools_list_version == token.tools_list_version
        # Persisted deliberately: a deploy is only detectable across the HA
        # restart that applies it, so an in-memory-only fingerprint would lose
        # the very baseline the comparison needs.
        token.tools_catalog_fingerprint = "abc123abc123abc1"
        assert TokenRecord.from_dict(
            token.to_storage_dict()).tools_catalog_fingerprint == "abc123abc123abc1"

    async def test_corrupt_preset_dropped_token_survives(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_add_preset(record.id, "Baseline")
        raw = token_store.get_token_by_id(record.id).to_storage_dict()
        del raw["presets"][0]["id"]
        loaded = TokenRecord.from_dict(raw)
        assert loaded.presets == []
        # The dangling active reference is cleared with it.
        assert loaded.active_preset_id is None

    def test_new_field_defaults_on_legacy_records(self):
        base = {
            "id": "tid", "name": "t", "token_hash": "h",
            "created_at": utcnow().isoformat(), "created_by": "u",
        }
        loaded = TokenRecord.from_dict(base)
        assert loaded.presets == [] and loaded.active_preset_id is None
        assert loaded.settings_version == 1
        assert loaded.tools_list_version == 1
        # tools_list_version defaults to the LOADED settings_version so an
        # upgrade never makes an existing token look stale.
        loaded = TokenRecord.from_dict({**base, "settings_version": 5})
        assert loaded.tools_list_version == 5
        # The catalog fingerprint has NO such fallback: token_store cannot import
        # mcp_view to learn the current one, so a legacy record has no baseline
        # and empty must read as "never recorded", never as stale.
        assert loaded.tools_catalog_fingerprint == ""
        # confirm_inline_wait_seconds defaults to OFF. Blocking a confirm-gated
        # call is opt-in: holding the request stops the NEXT approval from even
        # being created (tool calls arrive one at a time), so a run of writes
        # reaches the operator's queue one per wait-period instead of at once.
        assert loaded.confirm_inline_wait_seconds == 0

    def test_inline_wait_round_trips_and_clamps(self):
        base = {
            "id": "tid", "name": "t", "token_hash": "h",
            "created_at": utcnow().isoformat(), "created_by": "u",
        }
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": 30}).confirm_inline_wait_seconds == 30
        # Over the cap clamps to 180; a below-floor positive clamps up to 30;
        # 0/negative/garbage means off (0).
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": 999}).confirm_inline_wait_seconds == 180
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": 10}).confirm_inline_wait_seconds == 30
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": 0}).confirm_inline_wait_seconds == 0
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": -5}).confirm_inline_wait_seconds == 0
        assert TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": "x"}).confirm_inline_wait_seconds == 0
        # Full serializer round trip preserves a valid value.
        loaded = TokenRecord.from_dict({**base, "confirm_inline_wait_seconds": 90})
        assert TokenRecord.from_dict(loaded.to_storage_dict()).confirm_inline_wait_seconds == 90

    async def test_inline_wait_patch_clamps(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_patch_token(record.id, confirm_inline_wait_seconds=999)
        assert updated.confirm_inline_wait_seconds == 180
        # 0 is a valid patch (disable, the unattended-agent mode).
        updated = await token_store.async_patch_token(record.id, confirm_inline_wait_seconds=0)
        assert updated.confirm_inline_wait_seconds == 0

    async def test_inline_wait_snapshotted_in_preset(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_patch_token(record.id, confirm_inline_wait_seconds=60)
        updated = await token_store.async_add_preset(record.id, "Baseline")
        assert updated.presets[0].confirm_inline_wait_seconds == 60

    async def test_duplicate_name_case_insensitive_raises(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        await token_store.async_add_preset(record.id, "Baseline")
        try:
            await token_store.async_add_preset(record.id, "baseline")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    async def test_preset_cap_enforced(self, token_store):
        from custom_components.phoenix_mcp.const import MAX_PRESETS_PER_TOKEN

        record, _ = await token_store.async_create_token("t1", "u")
        for i in range(MAX_PRESETS_PER_TOKEN):
            await token_store.async_add_preset(record.id, f"p{i}")
        try:
            await token_store.async_add_preset(record.id, "one-too-many")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    async def test_rename_preset(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_add_preset(record.id, "Old")
        pid = updated.presets[0].id
        updated = await token_store.async_rename_preset(record.id, pid, "New")
        assert updated.presets[0].name == "New"
        try:
            await token_store.async_rename_preset(record.id, "ghost", "X")
            raise AssertionError("expected LookupError")
        except LookupError:
            pass

    async def test_delete_active_preset_refused(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_add_preset(record.id, "A")
        pid_a = updated.presets[0].id
        updated = await token_store.async_add_preset(record.id, "B")
        pid_b = updated.presets[1].id
        assert updated.active_preset_id == pid_b
        try:
            await token_store.async_delete_preset(record.id, pid_b)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        updated = await token_store.async_delete_preset(record.id, pid_a)
        assert [p.id for p in updated.presets] == [pid_b]

    async def test_sync_preset_absorbs_live_state(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_add_preset(record.id, "A")
        preset = updated.presets[0]
        created_at = preset.created_at
        await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW)
        updated = await token_store.async_sync_preset(record.id, preset.id)
        synced = updated.presets[0]
        assert synced.caps["cap_restart"] == CAP_ALLOW
        assert synced.id == preset.id and synced.name == "A"
        assert synced.created_at == created_at

    async def test_seed_default_presets_idempotent(self, token_store):
        from custom_components.phoenix_mcp.const import DEFAULT_PRESET_NAME

        bare, _ = await token_store.async_create_token("bare", "u")
        preset_owner, _ = await token_store.async_create_token("owner", "u")
        await token_store.async_add_preset(preset_owner.id, "Mine")

        assert await token_store.async_seed_default_presets() == 1
        seeded = token_store.get_token_by_id(bare.id)
        assert seeded.presets[0].name == DEFAULT_PRESET_NAME
        assert seeded.active_preset_id == seeded.presets[0].id
        owner = token_store.get_token_by_id(preset_owner.id)
        assert [p.name for p in owner.presets] == ["Mine"]
        assert await token_store.async_seed_default_presets() == 0

    async def test_create_token_seeds_when_enabled(self, token_store):
        from custom_components.phoenix_mcp.const import DEFAULT_PRESET_NAME

        await token_store.async_patch_settings(token_presets_enabled=True)
        record, _ = await token_store.async_create_token("t1", "u")
        assert record.presets[0].name == DEFAULT_PRESET_NAME
        assert record.active_preset_id == record.presets[0].id


class TestSettingsVersion:
    async def test_cap_patch_bumps_and_keeps_active_preset(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        updated = await token_store.async_add_preset(record.id, "A")
        active = updated.active_preset_id
        before = updated.settings_version
        updated = await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW)
        assert updated.settings_version == before + 1
        # Workspace model: manual edits are the active preset's unsaved state,
        # they never clear the active label.
        assert updated.active_preset_id == active

    async def test_name_only_rename_does_not_bump(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        before = record.settings_version
        updated = await token_store.async_patch_token(record.id, name="renamed")
        assert updated.settings_version == before

    async def test_inline_wait_patch_does_not_bump(self, token_store):
        # confirm_inline_wait_seconds changes neither tools nor caps, so it must
        # not fire the stale-tools advisory (like a bare rename).
        record, _ = await token_store.async_create_token("t1", "u")
        before = record.settings_version
        updated = await token_store.async_patch_token(record.id, confirm_inline_wait_seconds=60)
        assert updated.confirm_inline_wait_seconds == 60
        assert updated.settings_version == before

    async def test_permission_writes_bump(self, token_store):
        record, _ = await token_store.async_create_token("t1", "u")
        before = record.settings_version
        await token_store.async_set_permissions(record.id, PermissionTree())
        assert token_store.get_token_by_id(record.id).settings_version == before + 1
        await token_store.async_patch_permission_node(
            record.id, "entities", "light.a", "GREEN"
        )
        assert token_store.get_token_by_id(record.id).settings_version == before + 2

    def test_settings_toggle_round_trip(self):
        assert GlobalSettings.from_dict({"token_presets_enabled": True}).token_presets_enabled is True
        assert GlobalSettings().token_presets_enabled is False
        assert GlobalSettings(token_presets_enabled=True).to_dict()["token_presets_enabled"] is True
