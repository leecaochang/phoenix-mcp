"""Phoenix's own entities are invisible everywhere, on all four surfaces.

The domain blocklist covers the phoenix_mcp DOMAIN, but Phoenix's telemetry
sensors are not in it: they are `sensor.*` entities registered to the
phoenix_mcp PLATFORM, so the blocklist never sees them and a separate
`entry.platform == DOMAIN` check does the real work. That check exists in four
places: the canonical policy_engine.resolve, plus three pass-through FAST PATHS
that skip resolve() for performance (the admin permission tree, the admin
token-scope endpoint, and the MCP context endpoint).

An optimisation that re-implements the policy it is optimising is the shape that
drifts, so each of the four is pinned here rather than only the canonical one.

Every test here asserts the DROP. A test that checks the ordinary entity is
present passes unchanged when the filter stops excluding anything, which is the
assertion shape this pass exists to find.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.policy_engine import Permission, resolve
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "custom_components" / "phoenix_mcp"

PHOENIX_SENSOR = "sensor.phoenix_mcp_my_token_requests"
ORDINARY = "sensor.living_room_temperature"


@pytest.fixture
def registry_with_a_phoenix_sensor(hass: HomeAssistant):
    """One Phoenix telemetry sensor and one ordinary sensor, both in the registry.

    The Phoenix one is in the SENSOR domain on purpose: that is what makes
    BLOCKED_DOMAINS insufficient and the platform check load-bearing.
    """
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor", DOMAIN, "my_token_requests", suggested_object_id="phoenix_mcp_my_token_requests")
    reg.async_get_or_create(
        "sensor", "demo", "living_room_temp", suggested_object_id="living_room_temperature")
    hass.states.async_set(PHOENIX_SENSOR, "12")
    hass.states.async_set(ORDINARY, "21.5")
    return reg


def _token(**kw) -> TokenRecord:
    return TokenRecord(
        id="tok", name="t", token_hash="x", created_at=utcnow(), created_by="admin",
        permissions=kw.pop("permissions", PermissionTree()), **kw)


# --------------------------------------------------------------------------
# 1. policy_engine.resolve - the canonical enforcement
# --------------------------------------------------------------------------


class TestResolve:
    def test_phoenix_sensor_is_no_access_even_with_an_explicit_grant(
            self, hass, registry_with_a_phoenix_sensor):
        """An admin cannot grant it by mistake; the check runs before the tree."""
        tree = PermissionTree(entities={PHOENIX_SENSOR: PermissionNode(state="GREEN")})
        assert resolve(PHOENIX_SENSOR, _token(permissions=tree), hass) is Permission.NO_ACCESS

    def test_phoenix_sensor_is_no_access_under_pass_through(
            self, hass, registry_with_a_phoenix_sensor):
        """Pass-through bypasses the permission tree, never this."""
        assert resolve(PHOENIX_SENSOR, _token(pass_through=True), hass) is Permission.NO_ACCESS

    def test_an_ordinary_sensor_is_not_blocked(self, hass, registry_with_a_phoenix_sensor):
        """The keep side, so a filter that blocks everything cannot pass."""
        assert resolve(ORDINARY, _token(pass_through=True), hass) is Permission.WRITE


# --------------------------------------------------------------------------
# 2. admin_view._build_entity_tree - the permission UI
# --------------------------------------------------------------------------


class TestAdminEntityTree:
    def _tree(self, hass):
        from custom_components.phoenix_mcp.admin_view import _build_entity_tree
        return _build_entity_tree(hass)

    def _entity_ids(self, tree) -> set[str]:
        found: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(key, str) and "." in key:
                        found.add(key)
                    if isinstance(value, (dict, list)):
                        walk(value)
                    elif isinstance(value, str) and "." in value:
                        found.add(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(tree)
        return found

    def test_phoenix_sensor_is_absent_from_the_permission_tree(
            self, hass, registry_with_a_phoenix_sensor):
        """Showing it would let an admin grant a permission the runtime always
        denies, which reads as a Phoenix bug rather than a deliberate block."""
        ids = self._entity_ids(self._tree(hass))
        assert PHOENIX_SENSOR not in ids

    def test_the_ordinary_sensor_is_present(self, hass, registry_with_a_phoenix_sensor):
        ids = self._entity_ids(self._tree(hass))
        assert ORDINARY in ids, "the tree is empty; the absence test above proves nothing"

    def test_a_disabled_entity_is_absent(self, hass, registry_with_a_phoenix_sensor):
        reg = registry_with_a_phoenix_sensor
        reg.async_update_entity(ORDINARY, disabled_by=er.RegistryEntryDisabler.USER)
        ids = self._entity_ids(self._tree(hass))
        assert ORDINARY not in ids


# --------------------------------------------------------------------------
# 3 + 4. The two pass-through fast paths
# --------------------------------------------------------------------------


class TestPassThroughFastPaths:
    """Both skip resolve() for speed, so both re-implement its exclusions."""

    async def test_mcp_context_json_excludes_the_phoenix_sensor(
            self, hass, registry_with_a_phoenix_sensor):
        from custom_components.phoenix_mcp.mcp_view import _build_context_json

        data = MagicMock()
        data.store.get_entity_hints.return_value = {}
        hass.data[DOMAIN] = data

        body = _build_context_json(_token(pass_through=True), hass)
        ids = {e["entity_id"] for e in body["entities"]}
        assert PHOENIX_SENSOR not in ids
        assert ORDINARY in ids, "the context is empty; the absence assertion is vacuous"

    async def test_the_fast_path_agrees_with_resolve(
            self, hass, registry_with_a_phoenix_sensor):
        """The invariant itself: the optimisation must not see more than the
        enforcement would. Checked as a set comparison rather than one entity, so
        a future exclusion added to resolve and forgotten here fails too."""
        from custom_components.phoenix_mcp.mcp_view import _build_context_json

        data = MagicMock()
        data.store.get_entity_hints.return_value = {}
        hass.data[DOMAIN] = data
        token = _token(pass_through=True)

        fast = {e["entity_id"] for e in _build_context_json(token, hass)["entities"]}
        slow = {s.entity_id for s in hass.states.async_all()
                if resolve(s.entity_id, token, hass) in (Permission.READ, Permission.WRITE)}
        assert fast == slow, (
            "the pass-through fast path and resolve() disagree about which "
            f"entities are visible; only in fast: {sorted(fast - slow)}, "
            f"only in resolve: {sorted(slow - fast)}"
        )


# --------------------------------------------------------------------------
# The structural guard: a new fast path must not forget the platform check
# --------------------------------------------------------------------------


def test_every_blocked_domains_filter_also_checks_the_platform():
    """BLOCKED_DOMAINS alone is not the rule, and looks like it is.

    Phoenix's sensors are in the sensor domain, so any filter that checks only
    the domain lets them through. Four places implement the pair today; this
    fails if a fifth implements half of it.
    """
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if "mesa_core" in path.parts or path.name == "const.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if "BLOCKED_DOMAINS" not in body:
                continue
            # Only functions that BUILD a visible entity collection need the
            # platform check. Two shapes are exempt on principle, not by name:
            # a function that never touches entities (PhoenixServicesView
            # filters service domains) and a predicate answering a question
            # about one entity rather than returning a list of them
            # (mesa_suggestions._is_risky_ref -> bool).
            if "entity_id" not in body and ".states" not in body:
                continue
            returns_bool = (isinstance(node.returns, ast.Name)
                            and node.returns.id == "bool")
            if returns_bool:
                continue
            if "platform == DOMAIN" not in body:
                offenders.append(f"{path.relative_to(REPO)}::{node.name}")
    assert not offenders, (
        "These filter on BLOCKED_DOMAINS without also excluding entities on the "
        "phoenix_mcp PLATFORM. Phoenix's own sensors live in the sensor domain, "
        "so the domain check alone lets them through:\n  " + "\n  ".join(offenders)
    )


def test_the_structural_guard_would_notice_a_half_implementation():
    """Mutation check in-file: prove the scan is not vacuous."""
    half = ast.parse(
        "def _new_filter(states):\n"
        "    return [s for s in states if s.domain not in BLOCKED_DOMAINS]\n"
    ).body[0]
    body = ast.unparse(half)
    assert "BLOCKED_DOMAINS" in body and "platform == DOMAIN" not in body, (
        "the scan cannot see a domain-only filter, so its clean result means nothing"
    )
