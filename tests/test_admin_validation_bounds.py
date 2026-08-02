"""Structural-validation and pagination-bound regressions on the admin API.

Two defects, both reachable with syntactically valid JSON / query strings:

* A permission-tree section that is not an object reached ``.items()`` and
  raised ``AttributeError``, surfacing as a 500 instead of a 400.
* The paged endpoints disagreed on bounds. A NEGATIVE limit was accepted and
  slices as "everything but the last N" (and in the version store is an
  explicit "return everything"), and the approvals endpoint silently replaced
  malformed input with defaults rather than reporting it.

Admin-only surfaces, so these are correctness rather than privilege issues,
but both violate the documented contract.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminApprovalsView,
    PhoenixAdminAuditView,
    PhoenixAdminPermissionsView,
    PhoenixAdminTokensView,
    PhoenixAdminTokenView,
    _parse_pagination,
    _validate_permission_tree_body,
)

from tests.test_admin_view import (  # reuse the established harness
    _make_active_token,
    _make_admin_request,
    _make_data,
    _make_hass,
)


# --------------------------------------------------------------------------- #
# Permission tree: a non-object section must be a 400, never a 500
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "body",
    [
        {"domains": []},
        {"devices": "not-an-object"},
        {"entities": 42},
        {"domains": None},
    ],
)
def test_non_object_permission_section_is_rejected(body):
    """Previously raised AttributeError on .items(), escaping as a 500."""
    resp = _validate_permission_tree_body(body, "rid-1")
    assert resp is not None, f"{body!r} should be refused"
    assert resp.status == 400


def test_valid_permission_sections_still_pass():
    """The guard must not reject well-formed trees."""
    body = {
        "domains": {"light": {"state": "GREEN"}},
        "devices": {},
        "entities": {"light.kitchen": {"state": "YELLOW", "hint": "ok"}},
    }
    assert _validate_permission_tree_body(body, "rid-1") is None


@pytest.mark.asyncio
async def test_permissions_put_returns_400_not_500_for_bad_section():
    """End-to-end: the handler reports a client error, not an internal one."""
    data = _make_data()
    token = _make_active_token()
    data.store.get_token_by_id = MagicMock(return_value=token)
    hass = _make_hass(data)

    view = PhoenixAdminPermissionsView()
    view.hass = hass

    body = json.dumps({"domains": []}).encode()
    request = _make_admin_request(body=body)
    resp = await view.put(request, token_id=token.id)

    assert resp.status == 400


# --------------------------------------------------------------------------- #
# Pagination bounds
# --------------------------------------------------------------------------- #

def _req(**query):
    r = _make_admin_request()
    r.query = {k: str(v) for k, v in query.items()}
    return r


def test_negative_limit_is_rejected():
    """A negative limit slices as 'everything but the last N'; refuse it."""
    out = _parse_pagination(_req(limit=-1), "rid", default_limit=50, max_limit=500)
    assert getattr(out, "status", None) == 400


def test_negative_offset_is_rejected():
    out = _parse_pagination(_req(offset=-5), "rid", default_limit=50, max_limit=500)
    assert getattr(out, "status", None) == 400


def test_malformed_limit_is_reported_not_silently_defaulted():
    out = _parse_pagination(_req(limit="abc"), "rid", default_limit=50, max_limit=500)
    assert getattr(out, "status", None) == 400


def test_limit_is_clamped_to_the_maximum():
    limit, offset = _parse_pagination(_req(limit=10_000), "rid", default_limit=50, max_limit=500)
    assert (limit, offset) == (500, 0)


def test_defaults_apply_when_absent():
    assert _parse_pagination(_req(), "rid", default_limit=50, max_limit=500) == (50, 0)


def test_zero_limit_is_allowed():
    """Zero is a legitimate 'count only' request, distinct from negative."""
    assert _parse_pagination(_req(limit=0), "rid", default_limit=50, max_limit=500) == (0, 0)


@pytest.mark.asyncio
async def test_audit_endpoint_rejects_negative_limit():
    data = _make_data()
    hass = _make_hass(data)
    view = PhoenixAdminAuditView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"limit": "-1"}
    resp = await view.get(request)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_approvals_endpoint_rejects_negative_limit():
    data = _make_data()
    hass = _make_hass(data)
    view = PhoenixAdminApprovalsView()
    view.hass = hass

    request = _make_admin_request()
    request.query = {"limit": "-1"}
    resp = await view.get(request)

    assert resp.status == 400


# --------------------------------------------------------------------------- #
# Rate-limit fields must be real integers
#
# int() accepts bool (True -> 1), float (1.75 -> 1), and numeric strings
# ("12" -> 12), so a caller could store a value it never sent while the API
# advertised "must be integers". bool needs an explicit exclusion because it
# is a subclass of int.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "payload",
    [
        {"rate_limit_requests": True, "rate_limit_burst": 1},
        {"rate_limit_requests": 60, "rate_limit_burst": False},
        {"rate_limit_requests": 1.75, "rate_limit_burst": 1},
        {"rate_limit_requests": 60, "rate_limit_burst": 1.5},
        {"rate_limit_requests": "12", "rate_limit_burst": 1},
        {"rate_limit_requests": 60, "rate_limit_burst": "12"},
        {"rate_limit_requests": None, "rate_limit_burst": 1},
    ],
)
@pytest.mark.asyncio
async def test_create_rejects_non_integer_rate_limits(payload):
    data = _make_data()
    hass = _make_hass(data)
    token = _make_active_token()
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock(return_value=(token, "phx_rawtoken123"))

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "my-token", **payload}).encode()
    resp = await view.post(_make_admin_request(body=body))

    assert resp.status == 400, f"{payload!r} should be refused"
    data.store.async_create_token.assert_not_called()


@pytest.mark.asyncio
async def test_create_accepts_real_integer_rate_limits():
    data = _make_data()
    hass = _make_hass(data)
    token = _make_active_token()
    data.store.name_slug_exists.return_value = False
    data.store.async_create_token = AsyncMock(return_value=(token, "phx_rawtoken123"))

    view = PhoenixAdminTokensView()
    view.hass = hass

    body = json.dumps({"name": "my-token", "rate_limit_requests": 30, "rate_limit_burst": 5}).encode()
    resp = await view.post(_make_admin_request(body=body))

    assert resp.status == 201
    kwargs = data.store.async_create_token.call_args.kwargs
    assert kwargs["rate_limit_requests"] == 30 and kwargs["rate_limit_burst"] == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"rate_limit_requests": True},
        {"rate_limit_burst": False},
        {"rate_limit_requests": 1.75},
        {"rate_limit_burst": "12"},
    ],
)
@pytest.mark.asyncio
async def test_patch_rejects_non_integer_rate_limits(payload):
    data = _make_data()
    hass = _make_hass(data)
    token = _make_active_token()
    data.store.get_token_by_id = MagicMock(return_value=token)
    data.store.async_patch_token = AsyncMock(return_value=token)

    view = PhoenixAdminTokenView()
    view.hass = hass

    resp = await view.patch(_make_admin_request(body=json.dumps(payload).encode()), token_id=token.id)

    assert resp.status == 400, f"{payload!r} should be refused"
    data.store.async_patch_token.assert_not_called()
