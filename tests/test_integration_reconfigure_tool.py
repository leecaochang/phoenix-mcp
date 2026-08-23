"""Security and approval tests for the generic reconfigure tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.const import APPROVAL_SUMMARY_TEMPLATES
from custom_components.phoenix_mcp.approvals import PendingApproval
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import (
    _CONFIG_ENTRY_CONTEXT_FINGERPRINT,
    _CONFIG_ENTRY_PRIVATE_IDENTITY_FINGERPRINT,
    _CONFIG_ENTRY_RECONFIGURE_SNAPSHOT,
    _CONFIG_ENTRY_STABLE_IDENTITY_FINGERPRINT,
    _ConfigEntryPrivateIdentity,
    _build_diff_reconfigure_integration,
    _call_tool,
    _config_entry_private_identity,
    async_execute_approved_tool,
)
from custom_components.phoenix_mcp.mesa import async_setup_mesa
from custom_components.phoenix_mcp.token_store import (
    GlobalSettings,
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
)
from custom_components.phoenix_mcp.tools.integration_reconfigure import (
    ReconfigureFlowResult,
    STATUS_VERIFIED,
)


def _body(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


def test_approval_summary_and_all_catalogs_state_the_review_limitations():
    english = APPROVAL_SUMMARY_TEMPLATES["integration.reconfigure.body"]
    for phrase in (
        "agent-provided values",
        "has not validated",
        "Browser or OAuth",
        "asynchronous progress",
        "cannot automatically roll",
    ):
        assert phrase in english
    catalog_dir = Path(__file__).parents[1] / "custom_components/phoenix_mcp/catalogs"
    for catalog_path in sorted(catalog_dir.glob("*.json")):
        catalog = json.loads(catalog_path.read_text())
        body = catalog["panel"]["approvalSummary"]["integration.reconfigure.body"]
        assert "{label}" in body
        assert body != ""


async def _data(hass: HomeAssistant) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.async_lock = asyncio.Lock()
    store.async_save = AsyncMock()
    store.get_settings.return_value = GlobalSettings(mesa_mode="off")
    store.list_tokens.return_value = []
    store.get_entity_hints.return_value = {}
    store.get_pending_approvals.return_value = []
    data = PhoenixData(
        store=store,
        rate_limiter=MagicMock(),
        audit=MagicMock(),
        hass=hass,
        mesa=await async_setup_mesa(hass, "off"),
    )
    hass.data[DOMAIN] = data
    return data


@pytest.fixture
def reconfigure_env(hass: HomeAssistant) -> dict:
    entry = MockConfigEntry(
        domain="test_integration",
        entry_id="reconfigure-owner",
        title="Test integration",
        unique_id="private-entry-identity",
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test_integration", "device-private-id")},
        connections={("mac", "00:11:22:33:44:55")},
        name="Test device",
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "test_integration",
        "reconfigure-sensor",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="reconfigure_sensor",
    )
    hass.states.async_set(entity.entity_id, "on")
    return {"entry": entry, "device": device, "entity": entity}


def _token(
    env: dict,
    *,
    cap="allow",
    write="deny",
    pass_through=False,
    device_state="GREEN",
    entity_state=None,
):
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="reconfigure-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        pass_through=pass_through,
        cap_integration_write=write,
        cap_integration_reconfigure=cap,
        permissions=PermissionTree(
            devices={env["device"].id: PermissionNode(state=device_state)},
            domains={"sensor": PermissionNode(state="GREEN")},
            entities=(
                {env["entity"].entity_id: PermissionNode(state=entity_state)}
                if entity_state is not None
                else {}
            ),
        ),
    )


async def test_capability_is_enforced_for_pass_through(hass, reconfigure_env):
    data = await _data(hass)
    _content, outcome, _resource = await _call_tool(
        "reconfigure_integration",
        {"entry_id": reconfigure_env["entry"].entry_id, "config": {}},
        _token(reconfigure_env, cap="deny", pass_through=True),
        hass,
        data,
    )
    assert outcome == "denied"


@pytest.mark.parametrize(
    "token_kwargs",
    ({"device_state": "YELLOW"}, {"entity_state": "YELLOW"}),
)
async def test_complete_config_entry_write_is_required(
    hass, reconfigure_env, token_kwargs
):
    data = await _data(hass)
    runner = AsyncMock()
    with patch(
        "custom_components.phoenix_mcp.mcp_view.async_run_reconfigure_flow", runner
    ):
        content, outcome, _resource = await _call_tool(
            "reconfigure_integration",
            {"entry_id": reconfigure_env["entry"].entry_id, "config": {}},
            _token(reconfigure_env, **token_kwargs),
            hass,
            data,
        )
    assert outcome == "not_found"
    assert "Integration not found" in content["content"][0]["text"]
    runner.assert_not_awaited()


async def test_list_integrations_accepts_either_capability(hass, reconfigure_env):
    data = await _data(hass)
    content, outcome, _resource = await _call_tool(
        "list_integrations",
        {},
        _token(reconfigure_env, cap="allow", write="deny"),
        hass,
        data,
    )
    assert outcome == "allowed"
    assert _body(content)["integrations"][0]["entry_id"] == "reconfigure-owner"


async def test_review_preview_redacts_credentials_but_keeps_endpoint(
    hass, reconfigure_env
):
    diff = await _build_diff_reconfigure_integration(
        {
            "entry_id": reconfigure_env["entry"].entry_id,
            "config": {
                "host": "ha.example.test",
                "url": "https://ha.example.test/api",
                "password": "do-not-show",
                "api_token": "also-secret",
            },
            "menu_choices": ["local"],
        },
        _token(reconfigure_env),
        hass,
    )
    preview = diff["preview"]
    assert "before" not in diff and "after" not in diff
    assert preview["submitted_config"]["host"] == "ha.example.test"
    assert preview["submitted_config"]["url"] == "https://ha.example.test/api"
    assert preview["submitted_config"]["password"] == "<redacted>"
    assert preview["submitted_config"]["api_token"] == "<redacted>"
    assert preview["operator_editable"] is False
    assert preview["automatic_rollback"] is False
    assert preview["affected_entities"] == [{
        "entity_id": reconfigure_env["entity"].entity_id,
        "device_id": reconfigure_env["device"].id,
    }]


async def test_approval_binds_private_identity_and_keeps_exact_durable_args(
    hass, reconfigure_env
):
    data = await _data(hass)
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    args = {
        "entry_id": reconfigure_env["entry"].entry_id,
        "config": {"host": "10.0.0.8", "password": "exact-secret"},
        "menu_choices": ["local"],
    }
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        _content, outcome, _resource = await _call_tool(
            "reconfigure_integration",
            args,
            _token(reconfigure_env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "pending_approval"
    saved = gate.await_args.kwargs["args"]
    assert saved["config"]["password"] == "exact-secret"
    assert saved["menu_choices"] == ["local"]
    assert isinstance(saved[_CONFIG_ENTRY_CONTEXT_FINGERPRINT], str)
    assert isinstance(saved[_CONFIG_ENTRY_PRIVATE_IDENTITY_FINGERPRINT], str)
    assert isinstance(saved[_CONFIG_ENTRY_STABLE_IDENTITY_FINGERPRINT], str)


async def test_real_durable_approval_replay_keeps_raw_config_but_public_view_does_not(
    hass, reconfigure_env
):
    data = await _data(hass)
    args = {
        "entry_id": reconfigure_env["entry"].entry_id,
        "config": {"host": "ha.example.test", "password": "durable-secret"},
        "menu_choices": ["local"],
    }
    content, outcome, _resource = await _call_tool(
        "reconfigure_integration",
        args,
        _token(reconfigure_env, cap="confirm"),
        hass,
        data,
    )
    assert outcome == "pending_approval"
    assert "durable-secret" not in json.dumps(content)
    stored = data.store.set_pending_approvals.call_args.args[0][0]
    assert stored["args"]["config"]["password"] == "durable-secret"
    public = PendingApproval.from_dict(stored).to_dict()
    assert public["args"]["config"]["password"] == "<redacted>"
    assert public["args"][_CONFIG_ENTRY_PRIVATE_IDENTITY_FINGERPRINT] == "<redacted>"


async def test_identity_race_refuses_before_starting_flow(hass, reconfigure_env):
    data = await _data(hass)
    gate = AsyncMock(return_value=({}, "pending_approval", "approval"))
    args = {"entry_id": reconfigure_env["entry"].entry_id, "config": {"host": "new"}}
    with patch("custom_components.phoenix_mcp.mcp_view._gate", gate):
        await _call_tool(
            "reconfigure_integration",
            args,
            _token(reconfigure_env, cap="confirm"),
            hass,
            data,
        )
    saved = gate.await_args.kwargs["args"]
    dr.async_get(hass).async_update_device(
        reconfigure_env["device"].id,
        new_connections={("mac", "AA:BB:CC:DD:EE:FF")},
    )
    runner = AsyncMock()
    with patch(
        "custom_components.phoenix_mcp.mcp_view.async_run_reconfigure_flow", runner
    ):
        content, outcome, _resource = await async_execute_approved_tool(
            "reconfigure_integration",
            saved,
            _token(reconfigure_env, cap="confirm"),
            hass,
            data,
        )
    assert outcome == "denied"
    assert "changed after approval" in content["content"][0]["text"]
    runner.assert_not_awaited()


async def test_same_domain_shared_owner_is_blocked(hass, reconfigure_env):
    data = await _data(hass)
    identity = _ConfigEntryPrivateIdentity(
        binding_fingerprint="binding",
        stable_fingerprint="stable",
        entities=[],
        devices=[],
        cross_domain_coowners=[],
        same_domain_coowners=[{
            "device_id": reconfigure_env["device"].id,
            "entry_id": "same-domain-coowner",
            "domain": "test_integration",
        }],
    )
    with patch(
        "custom_components.phoenix_mcp.mcp_view._config_entry_private_identity",
        return_value=identity,
    ):
        content, outcome, _resource = await _call_tool(
            "reconfigure_integration",
            {"entry_id": reconfigure_env["entry"].entry_id, "config": {}},
            _token(reconfigure_env),
            hass,
            data,
        )
    assert outcome == "denied"
    body = _body(content)
    assert body["status"] == "flow_aborted_before_apply"
    assert body["retry_safe"] is True


async def test_applied_result_is_versioned_redacted_and_never_retryable(
    hass, reconfigure_env
):
    data = await _data(hass)
    entry = reconfigure_env["entry"]
    flow_result = ReconfigureFlowResult(
        STATUS_VERIFIED,
        True,
        "Home Assistant accepted the reconfigure flow.",
        {"reload_verified": True},
        entry.modified_at,
        entry.modified_at + timedelta(seconds=1),
    )
    with patch(
        "custom_components.phoenix_mcp.mcp_view.async_run_reconfigure_flow",
        new=AsyncMock(return_value=flow_result),
    ):
        content, outcome, _resource = await _call_tool(
            "reconfigure_integration",
            {
                "entry_id": entry.entry_id,
                "config": {"host": "ha.example.test", "password": "do-not-store"},
            },
            _token(reconfigure_env),
            hass,
            data,
        )
    assert outcome == "allowed"
    body = _body(content)
    assert body["status"] == "applied_and_verified"
    assert body["retry_safe"] is False
    version = data.versions.list_for("config_entry", entry.entry_id)[0]
    assert version.before is None
    assert version.after["snapshot_type"] == _CONFIG_ENTRY_RECONFIGURE_SNAPSHOT
    assert version.after["restorable"] is False
    assert version.after["submitted_config"]["host"] == "ha.example.test"
    assert version.after["submitted_config"]["password"] == "<redacted>"
    assert "do-not-store" not in json.dumps(version.to_dict())


async def test_post_apply_private_identity_change_has_explicit_status(
    hass, reconfigure_env
):
    data = await _data(hass)
    entry = reconfigure_env["entry"]
    initial = _config_entry_private_identity(entry, hass)
    assert initial is not None
    changed = _ConfigEntryPrivateIdentity(
        binding_fingerprint="changed-binding",
        stable_fingerprint="changed-stable",
        entities=initial.entities,
        devices=initial.devices,
        cross_domain_coowners=[],
        same_domain_coowners=[],
    )
    flow_result = ReconfigureFlowResult(
        STATUS_VERIFIED,
        True,
        "Home Assistant accepted the reconfigure flow.",
        {"reload_verified": True},
        entry.modified_at,
        entry.modified_at + timedelta(seconds=1),
    )
    with (
        patch(
            "custom_components.phoenix_mcp.mcp_view._config_entry_private_identity",
            side_effect=[initial, initial, changed],
        ),
        patch(
            "custom_components.phoenix_mcp.mcp_view.async_run_reconfigure_flow",
            new=AsyncMock(return_value=flow_result),
        ),
    ):
        content, outcome, _resource = await _call_tool(
            "reconfigure_integration",
            {"entry_id": entry.entry_id, "config": {}},
            _token(reconfigure_env),
            hass,
            data,
        )
    assert outcome == "allowed"
    body = _body(content)
    assert body["status"] == "applied_identity_mismatch"
    assert body["retry_safe"] is False
