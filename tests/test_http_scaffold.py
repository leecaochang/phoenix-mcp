"""Real-HTTP coverage for Phoenix MCP route registration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.phoenix_mcp.const import DOMAIN


def _client_view_classes():
    """Return the complete kill-switch-gated client route set."""
    from custom_components.phoenix_mcp.agentcli import ALL_AGENTCLI_CHAT_VIEWS
    from custom_components.phoenix_mcp.mcp_view import ALL_MCP_VIEWS
    from custom_components.phoenix_mcp.skill_view import ALL_SKILL_VIEWS

    return list(ALL_MCP_VIEWS) + list(ALL_SKILL_VIEWS) + list(ALL_AGENTCLI_CHAT_VIEWS)


def _all_view_classes():
    """Return every route class registered by the integration."""
    from custom_components.phoenix_mcp.admin_view import ALL_ADMIN_VIEWS
    from custom_components.phoenix_mcp.agentcli import ALL_AGENTCLI_ADMIN_VIEWS

    return list(ALL_ADMIN_VIEWS) + list(ALL_AGENTCLI_ADMIN_VIEWS) + _client_view_classes()


@pytest.fixture
async def client_routes(hass: HomeAssistant, hass_client_no_auth):
    """Register the supported client routes on Home Assistant's real router."""
    assert await async_setup_component(hass, "http", {})
    data = MagicMock(shutting_down=False)
    data.store.get_settings.return_value.kill_switch = False
    hass.data[DOMAIN] = data

    for view_cls in _client_view_classes():
        view = view_cls()
        view.hass = hass
        hass.http.register_view(view)

    return await hass_client_no_auth()


def test_client_route_list_contains_no_rest_proxy() -> None:
    """Only MCP, context, skill, and Agent Chat remain token-facing."""
    assert {view.url for view in _client_view_classes()} == {
        "/api/phoenix-mcp",
        "/api/phoenix-mcp/context",
        "/api/phoenix-mcp/skill",
        "/api/phoenix-mcp/agentcli/chat",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/phoenix-mcp/health"),
        ("GET", "/api/phoenix-mcp/states"),
        ("GET", "/api/phoenix-mcp/states/sensor.example"),
        ("POST", "/api/phoenix-mcp/services/light/turn_on"),
        ("GET", "/api/phoenix-mcp/config"),
        ("POST", "/api/phoenix-mcp/template"),
        ("GET", "/api/phoenix-mcp/events"),
        ("GET", "/api/phoenix-mcp/services"),
        ("GET", "/api/phoenix-mcp/logs"),
        ("GET", "/api/phoenix-mcp/history/period/2026-08-11T00:00:00+00:00"),
        ("GET", "/api/phoenix-mcp/statistics"),
    ),
)
async def test_removed_rest_proxy_routes_return_404(client_routes, method, path):
    response = await client_routes.request(method, path)
    assert response.status == 404


async def test_skill_route_still_registers(client_routes):
    response = await client_routes.get("/api/phoenix-mcp/skill")
    assert response.status == 200
    assert "Phoenix MCP" in await response.text()


class TestRouteRegistrationIsIdempotent:
    """A config-entry reload must not add a second set of routes."""

    @pytest.mark.asyncio
    async def test_repeat_registration_adds_no_routes(self, hass: HomeAssistant):
        from custom_components.phoenix_mcp import _register_views

        assert await async_setup_component(hass, "http", {})
        await hass.async_block_till_done()
        views = _all_view_classes()

        _register_views(hass, views)
        after_first = len(list(hass.http.app.router.routes()))
        assert after_first > 0, "the first registration put nothing on the router"

        _register_views(hass, views)
        _register_views(hass, views)

        assert len(list(hass.http.app.router.routes())) == after_first

    @pytest.mark.asyncio
    async def test_a_route_still_resolves_after_repeat_registration(
        self, hass: HomeAssistant
    ):
        from aiohttp.test_utils import make_mocked_request

        from custom_components.phoenix_mcp import _register_views

        assert await async_setup_component(hass, "http", {})
        await hass.async_block_till_done()
        views = _all_view_classes()

        _register_views(hass, views)
        _register_views(hass, views)

        match = await hass.http.app.router.resolve(
            make_mocked_request("GET", "/api/phoenix-mcp/skill", app=hass.http.app)
        )
        assert match is not None
        assert callable(getattr(match, "handler", None))
