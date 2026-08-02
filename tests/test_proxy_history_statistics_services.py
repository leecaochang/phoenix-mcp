"""Permission filtering on the three entity-scoped REST views.

PhoenixHistoryView, PhoenixStatisticsView and PhoenixServicesView are
authenticated endpoints that each do their own permission filtering, where a
regression would ship silently. They are grouped here because of what they
share: each one decides WHICH entities or domains the caller may see, and each
one decides it differently.

What every test here asserts is the DROP, not just the keep. A filter that
stopped excluding anything would pass a test that only checks the permitted
entity is present, so each filtering test names an entity or domain the token
must NOT receive and asserts its absence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

PROXY = "custom_components.phoenix_mcp.proxy_view"
# Both views import get_instance inside the method body, so proxy_view never
# binds it; patch it where it is defined.
RECORDER = "homeassistant.components.recorder.get_instance"


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
# PhoenixHistoryView
# --------------------------------------------------------------------------


class TestHistoryView:
    """GET /api/phoenix-mcp/history/period/{timestamp}."""

    def _view(self, hass, data):
        from custom_components.phoenix_mcp.proxy_view import PhoenixHistoryView
        return _view(PhoenixHistoryView, hass, data)

    async def test_only_permitted_entities_reach_the_recorder(self, hass):
        """The DB query itself must carry only permitted ids.

        Asserted on the recorder call rather than the response: filtering the
        response afterwards would still have read another token's history off
        disk, and a passing response-shape test cannot tell the two apart.
        """
        tree = PermissionTree(domains={"light": PermissionNode(state="GREEN")})
        token, raw = _token(tree)
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids",
                   return_value={"light.kitchen"}) as perms, \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
            resp = await view.get(_request(raw, {"filter_entity_id": "light.kitchen,lock.front"}),
                                  "2026-07-31T00:00:00+00:00")

        assert resp.status == 200
        perms.assert_called_once()
        queried = get_instance.return_value.async_add_executor_job.await_args.args[0]
        requested = queried.args[3]
        assert "light.kitchen" in requested
        assert "lock.front" not in requested

    async def test_no_permitted_entities_returns_empty_without_querying(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value=set()), \
             patch(RECORDER) as get_instance:
            resp = await view.get(_request(raw), "2026-07-31T00:00:00+00:00")

        assert resp.status == 200
        assert json.loads(resp.text) == {}
        get_instance.assert_not_called()

    async def test_invalid_start_time_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw), "not-a-timestamp")
        assert resp.status == 400
        assert json.loads(resp.text)["error"] == "invalid_request"

    async def test_invalid_end_time_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(
            _request(raw, {"end_time": "nonsense"}), "2026-07-31T00:00:00+00:00")
        assert resp.status == 400

    async def test_start_after_end_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"light.k"}):
            resp = await view.get(
                _request(raw, {"end_time": "2026-07-01T00:00:00+00:00"}),
                "2026-07-31T00:00:00+00:00")
        assert resp.status == 400

    @pytest.mark.parametrize("limit", ["0", "-5", "abc"])
    async def test_bad_limit_is_a_400(self, hass, limit):
        token, raw = _token()
        view = self._view(hass, _data(token))
        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"light.k"}):
            resp = await view.get(
                _request(raw, {"limit": limit}), "2026-07-31T00:00:00+00:00")
        assert resp.status == 400

    async def test_range_is_clamped_to_the_max_window(self, hass):
        """An unbounded start must not become an unbounded DB read."""
        from custom_components.phoenix_mcp.const import MAX_HISTORY_RANGE_DAYS

        token, raw = _token()
        view = self._view(hass, _data(token))
        ancient = (utcnow() - timedelta(days=MAX_HISTORY_RANGE_DAYS * 10)).isoformat()

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"light.k"}), \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
            resp = await view.get(_request(raw), ancient)

        assert resp.status == 200
        queried_start = get_instance.return_value.async_add_executor_job.await_args.args[0].args[1]
        floor = utcnow() - timedelta(days=MAX_HISTORY_RANGE_DAYS)
        assert queried_start >= floor - timedelta(minutes=1)
        # The clamp is reported, so a caller can tell the window was narrowed.
        assert resp.headers["X-Phoenix-History-Start"] == queried_start.isoformat()

    async def test_per_entity_truncation_is_flagged(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        states = [{"entity_id": "light.k", "state": str(i)} for i in range(5)]

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"light.k"}), \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(
                return_value={"light.k": states})
            resp = await view.get(_request(raw, {"limit": "2"}), "2026-07-31T00:00:00+00:00")

        body = json.loads(resp.text)
        assert body["light.k"]["truncated"] is True
        assert len(body["light.k"]["states"]) == 2

    async def test_recorder_failure_is_a_504_not_a_500(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"light.k"}), \
             patch(RECORDER, side_effect=RuntimeError("recorder down")):
            resp = await view.get(_request(raw), "2026-07-31T00:00:00+00:00")

        assert resp.status == 504
        assert json.loads(resp.text)["error"] == "gateway_timeout"

    async def test_sensitive_attributes_are_scrubbed(self, hass):
        """Sensitive-attribute scrubbing applies to history, not only to live reads."""
        token, raw = _token()
        view = self._view(hass, _data(token))
        row = {
            "entity_id": "camera.front",
            "state": "idle",
            "attributes": {"access_token": "leak", "entity_picture": "/x.jpg", "fps": 30},
        }

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"camera.front"}), \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(
                return_value={"camera.front": [row]})
            resp = await view.get(_request(raw), "2026-07-31T00:00:00+00:00")

        attrs = json.loads(resp.text)["camera.front"]["states"][0]["attributes"]
        assert "access_token" not in attrs
        assert "entity_picture" not in attrs
        assert attrs["fps"] == 30


# --------------------------------------------------------------------------
# PhoenixStatisticsView
# --------------------------------------------------------------------------


class TestStatisticsView:
    """GET /api/phoenix-mcp/statistics."""

    def _view(self, hass, data):
        from custom_components.phoenix_mcp.proxy_view import PhoenixStatisticsView
        return _view(PhoenixStatisticsView, hass, data)

    async def test_requested_ids_are_intersected_with_permitted(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids",
                   return_value={"sensor.allowed"}), \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
            resp = await view.get(_request(raw, {
                "start_time": "24h",
                "entity_ids": "sensor.allowed,sensor.forbidden",
            }))

        assert resp.status == 200
        ids = get_instance.return_value.async_add_executor_job.await_args.args[0].args[3]
        assert ids == {"sensor.allowed"}
        assert "sensor.forbidden" not in ids

    async def test_missing_start_time_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw))
        assert resp.status == 400

    async def test_invalid_start_time_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw, {"start_time": "nope"}))
        assert resp.status == 400

    async def test_invalid_end_time_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw, {"start_time": "24h", "end_time": "nope"}))
        assert resp.status == 400

    async def test_invalid_period_is_a_400(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw, {"start_time": "24h", "period": "fortnight"}))
        assert resp.status == 400

    async def test_all_statistic_types_invalid_is_a_400(self, hass):
        """An unrecognised type set must be refused, not silently widened to all."""
        token, raw = _token()
        view = self._view(hass, _data(token))
        resp = await view.get(_request(raw, {"start_time": "24h", "statistic_types": "bogus"}))
        assert resp.status == 400

    async def test_unknown_types_are_dropped_when_some_are_valid(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"sensor.a"}), \
             patch(RECORDER) as get_instance:
            get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
            await view.get(_request(raw, {"start_time": "24h", "statistic_types": "mean,bogus"}))

        types = get_instance.return_value.async_add_executor_job.await_args.args[0].args[6]
        assert types == {"mean"}

    async def test_no_permitted_entities_returns_empty_without_querying(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value=set()), \
             patch(RECORDER) as get_instance:
            resp = await view.get(_request(raw, {"start_time": "24h"}))

        assert resp.status == 200
        assert json.loads(resp.text) == {}
        get_instance.assert_not_called()

    async def test_recorder_failure_is_a_504(self, hass):
        token, raw = _token()
        view = self._view(hass, _data(token))

        with patch(f"{PROXY}._build_permitted_entity_ids", return_value={"sensor.a"}), \
             patch(RECORDER, side_effect=RuntimeError("boom")):
            resp = await view.get(_request(raw, {"start_time": "24h"}))

        assert resp.status == 504


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
