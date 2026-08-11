"""Permission filtering on the REST service-catalog view.

Every filtering test asserts the drop, not just the keep. A filter that stopped
excluding anything would pass a test that only checks the permitted domain is
present, so each test also names a domain the token must not receive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from unittest.mock import MagicMock

from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN, TOKEN_PREFIX
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
)

def _token(tree: PermissionTree | None = None, **kw) -> tuple[TokenRecord, str]:
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    record = TokenRecord(
        id=str(uuid.uuid4()),
        name="t",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        created_at=utcnow(),
        created_by="u",
        permissions=tree or PermissionTree(),
        rate_limit_requests=60,
        rate_limit_burst=10,
        **kw,
    )
    return record, raw


def _data(token: TokenRecord) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.get_token_by_hash.return_value = token
    store.get_settings.return_value = MagicMock(
        kill_switch=False, disable_all_logging=False, log_allowed=True,
        log_denied=True, log_rate_limited=True, log_entity_names=True,
        log_client_ip=True, notify_on_rate_limit=False,
    )
    store.update_last_used = MagicMock()
    store.get_pending_approvals.return_value = []
    store.async_lock = asyncio.Lock()

    limiter = MagicMock(spec=RateLimiter)
    limiter.check.return_value = RateLimitResult(
        allowed=True, rate_limiting_enabled=True, limit=60,
        remaining=59, reset=9999999999, retry_after=0,
    )
    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()
    return PhoenixData(store=store, rate_limiter=limiter, audit=audit, rate_limit_notified={})


def _request(raw: str, query: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.method = "GET"
    req.remote = "127.0.0.1"
    req.headers = MagicMock()
    req.headers.get = MagicMock(
        side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    req.query = query or {}
    return req


def _view(cls, hass, data):
    hass.data[DOMAIN] = data
    view = cls()
    view.hass = hass
    return view


# --------------------------------------------------------------------------
# PhoenixServicesView
# --------------------------------------------------------------------------


class TestServicesView:
    """GET /api/phoenix-mcp/services."""

    def _view(self, hass, data):
        from custom_components.phoenix_mcp.proxy_view import PhoenixServicesView
        return _view(PhoenixServicesView, hass, data)

    def _services(self, hass):
        hass.services = MagicMock()
        hass.services.async_services.return_value = {
            "light": {"turn_on": {}},
            "lock": {"lock": {}},
            "phoenix_mcp": {"secret": {}},
        }

    async def test_only_domain_level_green_domains_are_listed(self, hass):
        """A WRITE grant on one entity does not expose the whole domain's services."""
        tree = PermissionTree(
            domains={"light": PermissionNode(state="GREEN")},
            entities={"lock.front": PermissionNode(state="GREEN")},
        )
        token, raw = _token(tree)
        self._services(hass)
        view = self._view(hass, _data(token))

        resp = await view.get(_request(raw))

        assert resp.status == 200
        domains = {row["domain"] for row in json.loads(resp.text)}
        assert domains == {"light"}
        assert "lock" not in domains

    async def test_pass_through_lists_everything_except_the_blocklist(self, hass):
        """Pass-through skips the tree, never the Phoenix domain blocklist."""
        token, raw = _token(pass_through=True)
        self._services(hass)
        view = self._view(hass, _data(token))

        resp = await view.get(_request(raw))

        domains = {row["domain"] for row in json.loads(resp.text)}
        assert {"light", "lock"} <= domains
        assert DOMAIN not in domains

    async def test_scoped_token_never_sees_the_phoenix_domain(self, hass):
        """Even an explicit GREEN grant on the Phoenix domain must not list it."""
        tree = PermissionTree(domains={DOMAIN: PermissionNode(state="GREEN")})
        token, raw = _token(tree)
        self._services(hass)
        view = self._view(hass, _data(token))

        resp = await view.get(_request(raw))

        domains = {row["domain"] for row in json.loads(resp.text)}
        assert DOMAIN not in domains

    async def test_no_writable_domains_returns_an_empty_list(self, hass):
        token, raw = _token()
        self._services(hass)
        view = self._view(hass, _data(token))

        resp = await view.get(_request(raw))

        assert resp.status == 200
        assert json.loads(resp.text) == []
