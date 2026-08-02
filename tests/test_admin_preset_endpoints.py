"""Tests for the token settings preset admin endpoints (workspace model).

Runs the views against a REAL TokenStore (conftest fixture) so the switch,
revert, and auto-save-back semantics are exercised end to end, not mocked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminSettingsView,
    PhoenixAdminTokenPresetApplyView,
    PhoenixAdminTokenPresetsView,
    PhoenixAdminTokenPresetView,
)
from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import CAP_ALLOW, CAP_DENY, DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree


def _make_data(token_store) -> PhoenixData:
    rate_limiter = MagicMock(spec=RateLimiter)
    audit = MagicMock(spec=AuditLog)
    return PhoenixData(store=token_store, rate_limiter=rate_limiter, audit=audit)


def _make_admin_request(body: dict | None = None) -> MagicMock:
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    raw = json.dumps(body).encode() if body is not None else b""
    user = MagicMock()
    user.is_admin = True
    user.id = "admin-user-id"

    def _get(k, default=None):
        if k == KEY_HASS_USER:
            return user
        if k == KEY_AUTHENTICATED:
            return True
        return default

    request = MagicMock()
    request.query = {}
    request.content_length = len(raw)
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=raw)
    request.__getitem__ = MagicMock(side_effect=lambda k: user if k == KEY_HASS_USER else None)
    request.get = MagicMock(side_effect=_get)
    request.method = "POST"
    request.path = "/api/phoenix-mcp/admin/tokens/x/presets"
    request.remote = "127.0.0.1"
    return request


def _make_hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    return hass


def _view(cls, hass):
    view = cls()
    view.hass = hass
    return view


async def _enable_presets(token_store):
    await token_store.async_patch_settings(token_presets_enabled=True)


@pytest.mark.asyncio
async def test_all_preset_endpoints_403_when_disabled(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    record, _ = await token_store.async_create_token("t1", "u")

    resp = await _view(PhoenixAdminTokenPresetsView, hass).post(
        _make_admin_request({"name": "A"}), token_id=record.id)
    assert resp.status == 403
    resp = await _view(PhoenixAdminTokenPresetView, hass).patch(
        _make_admin_request({"name": "B"}), token_id=record.id, preset_id="x")
    assert resp.status == 403
    resp = await _view(PhoenixAdminTokenPresetView, hass).delete(
        _make_admin_request(), token_id=record.id, preset_id="x")
    assert resp.status == 403
    resp = await _view(PhoenixAdminTokenPresetApplyView, hass).post(
        _make_admin_request(), token_id=record.id, preset_id="x")
    assert resp.status == 403
    for r in (resp,):
        assert json.loads(r.text)["error"] == "forbidden"


@pytest.mark.asyncio
async def test_create_preset_snapshots_current_settings(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    # The create-time seed made "Default preset"; change some settings so the
    # new preset provably snapshots the CURRENT state, not the seed.
    await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW, rate_limit_requests=120)
    await token_store.async_set_permissions(
        record.id, PermissionTree(entities={"light.a": PermissionNode(state="GREEN")}))

    resp = await _view(PhoenixAdminTokenPresetsView, hass).post(
        _make_admin_request({"name": "Power"}), token_id=record.id)
    assert resp.status == 201
    body = json.loads(resp.text)
    preset = next(p for p in body["presets"] if p["name"] == "Power")
    assert preset["caps"]["cap_restart"] == CAP_ALLOW
    assert preset["rate_limit_requests"] == 120
    assert preset["permissions"]["entities"]["light.a"]["state"] == "GREEN"
    assert body["active_preset_id"] == preset["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expected", [
    ("", 400), ("   ", 400), ("x" * 41, 400), ("bad<name>", 400),
])
async def test_create_preset_name_validation(token_store, name, expected):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    resp = await _view(PhoenixAdminTokenPresetsView, hass).post(
        _make_admin_request({"name": name}), token_id=record.id)
    assert resp.status == expected


@pytest.mark.asyncio
async def test_create_preset_duplicate_and_cap(token_store):
    from custom_components.phoenix_mcp.const import MAX_PRESETS_PER_TOKEN

    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    view = _view(PhoenixAdminTokenPresetsView, hass)

    resp = await view.post(_make_admin_request({"name": "default PRESET"}), token_id=record.id)
    assert resp.status == 400  # case-insensitive clash with the seeded default

    for i in range(MAX_PRESETS_PER_TOKEN - 1):
        resp = await view.post(_make_admin_request({"name": f"p{i}"}), token_id=record.id)
        assert resp.status == 201
    resp = await view.post(_make_admin_request({"name": "overflow"}), token_id=record.id)
    assert resp.status == 400
    assert "at most" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_switch_auto_saves_outgoing_then_applies_target(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    default_id = record.presets[0].id  # seeded, active, cap_restart deny

    # Fork a second preset "B" that allows restart, then dirty B's live state.
    await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW)
    updated = await token_store.async_add_preset(record.id, "B")
    b_id = updated.active_preset_id
    await token_store.async_patch_token(record.id, cap_search=CAP_ALLOW)  # unsaved change on B

    resp = await _view(PhoenixAdminTokenPresetApplyView, hass).post(
        _make_admin_request(), token_id=record.id, preset_id=default_id)
    assert resp.status == 200
    body = json.loads(resp.text)
    # Live state now matches the default preset (both changes rolled back)...
    assert body["cap_restart"] == CAP_DENY and body["cap_search"] == CAP_DENY
    assert body["active_preset_id"] == default_id
    # ...and the outgoing preset B absorbed its unsaved change first.
    b = next(p for p in body["presets"] if p["id"] == b_id)
    assert b["caps"]["cap_search"] == CAP_ALLOW
    assert b["caps"]["cap_restart"] == CAP_ALLOW
    # New rate limits take effect cleanly.
    data.rate_limiter.destroy.assert_called_once_with(record.id)


@pytest.mark.asyncio
async def test_apply_active_preset_reverts_unsaved_changes(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    default_id = record.presets[0].id
    await token_store.async_patch_token(record.id, cap_restart=CAP_ALLOW)  # dirty

    resp = await _view(PhoenixAdminTokenPresetApplyView, hass).post(
        _make_admin_request(), token_id=record.id, preset_id=default_id)
    assert resp.status == 200
    body = json.loads(resp.text)
    # Revert: the unsaved change is discarded, NOT saved back into the preset.
    assert body["cap_restart"] == CAP_DENY
    preset = next(p for p in body["presets"] if p["id"] == default_id)
    assert preset["caps"]["cap_restart"] == CAP_DENY
    assert body["active_preset_id"] == default_id


@pytest.mark.asyncio
async def test_apply_pass_through_preset_requires_confirm(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    # Capture a pass-through preset, then drop the token back to scoped.
    await token_store.async_patch_token(record.id, pass_through=True)
    updated = await token_store.async_add_preset(record.id, "PT")
    pt_id = updated.active_preset_id
    await token_store.async_patch_token(record.id, pass_through=False)
    view = _view(PhoenixAdminTokenPresetApplyView, hass)

    resp = await view.post(_make_admin_request(), token_id=record.id, preset_id=pt_id)
    assert resp.status == 400
    assert "confirm_pass_through" in json.loads(resp.text)["message"]

    resp = await view.post(
        _make_admin_request({"confirm_pass_through": True}), token_id=record.id, preset_id=pt_id)
    assert resp.status == 200
    assert json.loads(resp.text)["pass_through"] is True


@pytest.mark.asyncio
async def test_delete_active_preset_rejected_other_deleted(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    default_id = record.presets[0].id
    updated = await token_store.async_add_preset(record.id, "B")
    b_id = updated.active_preset_id
    view = _view(PhoenixAdminTokenPresetView, hass)

    resp = await view.delete(_make_admin_request(), token_id=record.id, preset_id=b_id)
    assert resp.status == 400  # B is active

    resp = await view.delete(_make_admin_request(), token_id=record.id, preset_id=default_id)
    assert resp.status == 200
    assert [p["id"] for p in json.loads(resp.text)["presets"]] == [b_id]

    resp = await view.delete(_make_admin_request(), token_id=record.id, preset_id="ghost")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_rename_preset_endpoint(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    pid = record.presets[0].id
    view = _view(PhoenixAdminTokenPresetView, hass)

    resp = await view.patch(_make_admin_request({"name": "Renamed"}), token_id=record.id, preset_id=pid)
    assert resp.status == 200
    assert json.loads(resp.text)["presets"][0]["name"] == "Renamed"

    resp = await view.patch(_make_admin_request({"name": "X"}), token_id=record.id, preset_id="ghost")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_apply_holds_async_lock(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    await _enable_presets(token_store)
    record, _ = await token_store.async_create_token("t1", "u")
    pid = record.presets[0].id

    lock_acquired = []
    original_acquire = token_store.async_lock.acquire

    async def tracking_acquire():
        lock_acquired.append(True)
        return await original_acquire()

    token_store.async_lock.acquire = tracking_acquire
    resp = await _view(PhoenixAdminTokenPresetApplyView, hass).post(
        _make_admin_request(), token_id=record.id, preset_id=pid)
    assert resp.status == 200
    assert len(lock_acquired) == 1


@pytest.mark.asyncio
async def test_settings_enable_seeds_existing_tokens(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    record, _ = await token_store.async_create_token("t1", "u")
    assert record.presets == []  # created while the feature was off

    request = _make_admin_request({"token_presets_enabled": True})
    request.method = "PATCH"
    request.path = "/api/phoenix-mcp/admin/settings"
    resp = await _view(PhoenixAdminSettingsView, hass).patch(request)
    assert resp.status == 200

    seeded = token_store.get_token_by_id(record.id)
    assert len(seeded.presets) == 1
    assert seeded.active_preset_id == seeded.presets[0].id


@pytest.mark.asyncio
async def test_settings_toggle_rejects_non_bool(token_store):
    data = _make_data(token_store)
    hass = _make_hass(data)
    request = _make_admin_request({"token_presets_enabled": "yes"})
    request.method = "PATCH"
    resp = await _view(PhoenixAdminSettingsView, hass).patch(request)
    assert resp.status == 400
