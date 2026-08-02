"""Tests for the admin version-history HTTP endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminVersionRestoreView,
    PhoenixAdminVersionsView,
    PhoenixAdminVersionView,
)
from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.version_store import VersionStore


def _data() -> PhoenixData:
    return PhoenixData(
        store=MagicMock(), rate_limiter=MagicMock(), audit=MagicMock(), versions=VersionStore(),
    )


def _request(query: dict | None = None) -> MagicMock:
    user = MagicMock()
    user.is_admin = True
    user.id = "admin-user"
    state = {KEY_HASS_USER: user, KEY_AUTHENTICATED: True, "phoenix_mcp_rid": "test-rid"}
    req = MagicMock()
    req.query = query or {}
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


def _hass(data: PhoenixData) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: data}
    return hass


def _body(resp) -> dict:
    return json.loads(resp.body)


async def _seed(data: PhoenixData):
    """Create -> edit history on one automation; returns the edit record."""
    await data.versions.record(
        resource_type="automation", resource_id="aid1", action="create",
        before=None, after={"alias": "A"}, alias="A", token_name="agent")
    return await data.versions.record(
        resource_type="automation", resource_id="aid1", action="edit",
        before={"alias": "A"}, after={"alias": "B"}, alias="B", token_name="agent")


DEVICE_YAML = (
    'esphome:\n  name: dev\n'
    'api:\n  encryption:\n    key: "REALKEY123456="\n'
    'ota:\n  - platform: esphome\n    password: "Kx9fQ2mVb7"\n'
)


def _hass_exec(data: PhoenixData) -> MagicMock:
    """A hass whose executor jobs actually run, which the masking path needs."""
    hass = _hass(data)

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run
    return hass


async def _seed_esphome(data: PhoenixData, before: str | None, after: str | None):
    return await data.versions.record(
        resource_type="esphome_yaml", resource_id="dev.yaml", action="edit",
        before=None if before is None else {"content": before},
        after=None if after is None else {"content": after},
        alias="dev.yaml", token_name="agent")


class TestVersionDetailEsphomeMasking:
    """Without masking, the Changes tab is the one Phoenix MCP surface that
    renders credentials in clear text, including a value !phoenix_generate
    produced that the agent itself was never allowed to see. Storage stays raw
    so a restore is byte-faithful; only what is served to the panel is masked.
    """

    @pytest.mark.asyncio
    async def test_credentials_are_masked_on_both_sides(self):
        data = _data()
        rec = await _seed_esphome(data, DEVICE_YAML, DEVICE_YAML.replace("Kx9fQ2mVb7", "Zz1yTt8wQq"))
        view = PhoenixAdminVersionView()
        view.hass = _hass_exec(data)
        with patch("custom_components.phoenix_mcp.tools.esphome._read_esphome_secrets",
                   return_value=(set(), set())):
            body = _body(await view.get(_request(), version_id=rec.id))

        for side in ("before", "after"):
            content = body[side]["content"]
            assert "REALKEY123456=" not in content
            assert "__PHOENIX_REDACTED__api.encryption.key__" in content
        assert "Kx9fQ2mVb7" not in body["before"]["content"]
        assert "Zz1yTt8wQq" not in body["after"]["content"]
        # Everything that is not a credential still reads normally.
        assert "name: dev" in body["after"]["content"]

    @pytest.mark.asyncio
    async def test_the_stored_snapshot_is_left_raw_so_restore_stays_byte_faithful(self):
        data = _data()
        rec = await _seed_esphome(data, DEVICE_YAML, DEVICE_YAML)
        view = PhoenixAdminVersionView()
        view.hass = _hass_exec(data)
        with patch("custom_components.phoenix_mcp.tools.esphome._read_esphome_secrets",
                   return_value=(set(), set())):
            await view.get(_request(), version_id=rec.id)

        assert data.versions.get(rec.id).after["content"] == DEVICE_YAML

    @pytest.mark.asyncio
    async def test_unparseable_content_is_withheld_not_shown_raw(self):
        # Masking raises when it cannot locate the credentials, which is exactly
        # when the raw text must not be rendered.
        data = _data()
        rec = await _seed_esphome(data, None, "api:\n  key: [unclosed\n")
        view = PhoenixAdminVersionView()
        view.hass = _hass_exec(data)
        with patch("custom_components.phoenix_mcp.tools.esphome._read_esphome_secrets",
                   return_value=(set(), set())):
            body = _body(await view.get(_request(), version_id=rec.id))

        assert "unclosed" not in body["after"]["content"]
        assert "withheld" in body["after"]["content"]

    @pytest.mark.asyncio
    async def test_other_resource_types_are_untouched(self):
        data = _data()
        rec = await _seed(data)
        view = PhoenixAdminVersionView()
        view.hass = _hass_exec(data)
        body = _body(await view.get(_request(), version_id=rec.id))

        assert body["after"] == {"alias": "B"}


class TestVersionRestoreErrorDetail:
    @pytest.mark.asyncio
    async def test_the_executors_own_reason_is_relayed(self):
        # A fixed string turned a precise, actionable failure ("'wifi.ap.password'
        # is a masked credential...") into a support ticket. This is an admin-only
        # surface behind an HA session, so there is no caller to keep in the dark.
        data = _data()
        rec = await _seed(data)
        view = PhoenixAdminVersionRestoreView()
        view.hass = _hass(data)
        req = _request()
        req.content_length = 0
        req.content = MagicMock()
        req.content.read = AsyncMock(side_effect=[b"", b""])

        failure = ({"content": [{"type": "text", "text": "'wifi.ap.password' is a masked credential."}],
                    "isError": True}, "invalid_request", "esphome:dev.yaml")
        with patch("custom_components.phoenix_mcp.mcp_view.async_restore_version",
                   AsyncMock(return_value=failure)):
            resp = await view.post(req, version_id=rec.id)

        assert resp.status == 400
        assert "wifi.ap.password" in _body(resp)["message"]


class TestVersionsList:
    @pytest.mark.asyncio
    async def test_lists_newest_first_as_summaries(self):
        data = _data()
        await _seed(data)
        view = PhoenixAdminVersionsView()
        view.hass = _hass(data)
        resp = await view.get(_request({"resource_type": "automation", "resource_id": "aid1"}))
        assert resp.status == 200
        body = _body(resp)
        assert body["total"] == 2
        assert [v["action"] for v in body["versions"]] == ["edit", "create"]
        first = body["versions"][0]
        # summary omits the full configs but flags their presence
        assert "before" not in first and "after" not in first
        assert first["has_before"] is True and first["has_after"] is True
        assert first["alias"] == "B"

    @pytest.mark.asyncio
    async def test_missing_params_returns_400(self):
        view = PhoenixAdminVersionsView()
        view.hass = _hass(_data())
        resp = await view.get(_request({"resource_type": "automation"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_resource_returns_empty(self):
        view = PhoenixAdminVersionsView()
        view.hass = _hass(_data())
        resp = await view.get(_request({"resource_type": "automation", "resource_id": "nope"}))
        assert resp.status == 200
        assert _body(resp)["total"] == 0

    @pytest.mark.asyncio
    async def test_recent_feed_when_no_params(self):
        data = _data()
        await _seed(data)  # two automation versions
        await data.versions.record(
            resource_type="script", resource_id="s1", action="create",
            before=None, after={"alias": "S"}, alias="S", token_name="agent")
        view = PhoenixAdminVersionsView()
        view.hass = _hass(data)
        resp = await view.get(_request())  # neither param -> global recent feed
        assert resp.status == 200
        body = _body(resp)
        assert body["total"] == 3
        assert body["resource_type"] is None
        assert body["versions"][0]["resource_type"] == "script"  # newest first

    @pytest.mark.asyncio
    async def test_recent_feed_total_reflects_full_count_not_just_the_page(self):
        """total must be the true count across all resources, not len(page) -
        otherwise the panel's Load More button could never appear."""
        data = _data()
        await _seed(data)  # two automation versions
        await data.versions.record(
            resource_type="script", resource_id="s1", action="create",
            before=None, after={"alias": "S"}, alias="S", token_name="agent")
        view = PhoenixAdminVersionsView()
        view.hass = _hass(data)
        resp = await view.get(_request({"limit": "1"}))
        assert resp.status == 200
        body = _body(resp)
        assert len(body["versions"]) == 1
        assert body["total"] == 3

    @pytest.mark.asyncio
    async def test_recent_feed_offset_pages_through(self):
        data = _data()
        await _seed(data)  # two automation versions
        await data.versions.record(
            resource_type="script", resource_id="s1", action="create",
            before=None, after={"alias": "S"}, alias="S", token_name="agent")
        view = PhoenixAdminVersionsView()
        view.hass = _hass(data)
        page1 = _body(await view.get(_request({"limit": "1", "offset": "0"})))
        page2 = _body(await view.get(_request({"limit": "1", "offset": "1"})))
        assert page1["versions"][0]["resource_type"] == "script"
        assert page2["versions"][0]["action"] == "edit"


class TestVersionDetail:
    @pytest.mark.asyncio
    async def test_returns_full_before_after(self):
        data = _data()
        rec = await _seed(data)
        view = PhoenixAdminVersionView()
        view.hass = _hass(data)
        resp = await view.get(_request(), version_id=rec.id)
        assert resp.status == 200
        body = _body(resp)
        assert body["id"] == rec.id
        assert body["before"] == {"alias": "A"}
        assert body["after"] == {"alias": "B"}

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self):
        view = PhoenixAdminVersionView()
        view.hass = _hass(_data())
        resp = await view.get(_request(), version_id="does-not-exist")
        assert resp.status == 404
