"""Tests for the MESA profile admin HTTP endpoints in admin_view.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.phoenix_mcp.admin_view import (
    PhoenixAdminMesaAreasView,
    PhoenixAdminMesaAreaView,
    PhoenixAdminMesaDefaultsView,
    PhoenixAdminMesaDevicesView,
    PhoenixAdminMesaDeviceOptionsView,
    PhoenixAdminMesaDeviceView,
    PhoenixAdminMesaDomainsView,
    PhoenixAdminMesaDomainView,
    PhoenixAdminMesaIntegrationsView,
    PhoenixAdminMesaIntegrationView,
    PhoenixAdminMesaIntegrationOptionsView,
    PhoenixAdminMesaIssuesView,
    PhoenixAdminMesaOrphansClearView,
    PhoenixAdminMesaProfilesView,
    PhoenixAdminMesaProfileView,
    PhoenixAdminMesaVocabularyView,
    _read_body,
)
from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mesa import async_setup_mesa
from custom_components.phoenix_mcp.rate_limiter import RateLimiter
from custom_components.phoenix_mcp.token_store import GlobalSettings, TokenStore


def _admin_request(body: bytes = b"", query: dict | None = None) -> MagicMock:
    from homeassistant.components.http.const import KEY_AUTHENTICATED, KEY_HASS_USER

    user = MagicMock()
    user.is_admin = True
    user.id = "admin-user"
    state: dict = {KEY_HASS_USER: user, KEY_AUTHENTICATED: True, "phoenix_mcp_rid": "rid"}

    def _get(k, default=None):
        if k == KEY_HASS_USER:
            return user
        if k == KEY_AUTHENTICATED:
            return True
        return default

    request = MagicMock()
    request.method = "GET"
    request.path = "/api/phoenix-mcp/admin/mesa"
    request.remote = "127.0.0.1"
    request.query = query or {}
    request.content_length = len(body)
    request.content = MagicMock()
    request.content.read = AsyncMock(return_value=body)
    request.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    request.__setitem__ = MagicMock(side_effect=lambda k, v: state.__setitem__(k, v))
    request.get = MagicMock(side_effect=_get)
    return request


async def _setup(hass: HomeAssistant, mesa_mode: str = "advisory") -> PhoenixData:
    runtime = await async_setup_mesa(hass, mesa_mode)
    store = MagicMock(spec=TokenStore)
    store.get_settings = MagicMock(return_value=GlobalSettings(mesa_mode=mesa_mode))
    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()
    data = PhoenixData(
        store=store, rate_limiter=MagicMock(spec=RateLimiter),
        audit=audit, mesa=runtime,
    )
    hass.data[DOMAIN] = data
    return data


def _body(resp) -> dict:
    return json.loads(resp.text)


class _ChunkedContent:
    """Small request-body fake that behaves like a streaming aiohttp reader."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _limit: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def at_eof(self) -> bool:
        return not self._chunks


@pytest.mark.asyncio
async def test_admin_json_body_reader_drains_partial_stream() -> None:
    payload = {
        "archive": {
            "mesa_export": {
                "format_version": "1.0",
                "entities": {
                    "light.kitchen": {
                        "semantic_profile": {
                            "operational_boundaries": {"control_mode": "confirm"}
                        }
                    }
                },
            }
        },
        "on_conflict": "skip",
    }
    raw = json.dumps(payload).encode()
    request = MagicMock()
    request.content_length = len(raw)
    request.content = _ChunkedContent([raw[:17], raw[17:73], raw[73:]])

    assert await _read_body(request, "rid") == payload


@pytest.mark.asyncio
async def test_put_then_get_profile(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass

    put_body = json.dumps({
        "semantic_profile": {
            "semantic_tags": ["lighting.ambient"],
            "operational_boundaries": {"control_mode": "autonomous"},
        }
    }).encode()
    resp = await view.put(_admin_request(body=put_body), entity_id="light.kitchen")
    assert resp.status == 200
    assert _body(resp)["entity_id"] == "light.kitchen"

    get_resp = await view.get(_admin_request(), entity_id="light.kitchen")
    out = _body(get_resp)
    assert out["stored"] is not None
    assert out["effective"]["semantic_profile"]["operational_boundaries"]["control_mode"] == "autonomous"
    assert "explanation" in out


@pytest.mark.asyncio
async def test_put_invalid_profile_rejected(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    # control_mode 'yolo' is not a valid enum value.
    bad = json.dumps({"semantic_profile": {"operational_boundaries": {"control_mode": "yolo"}}}).encode()
    resp = await view.put(_admin_request(body=bad), entity_id="light.kitchen")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_put_rejects_noncanonical_tag(hass: HomeAssistant):
    # Canonical-only tag entry is enforced server-side (not just in the UI):
    # a malformed/non-namespaced tag is rejected by mesa-core validation.
    await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    bad = json.dumps({"semantic_profile": {"semantic_tags": ["notdotted"]}}).encode()
    resp = await view.put(_admin_request(body=bad), entity_id="light.kitchen")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_put_stamps_user_origin(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    put_body = json.dumps({"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}).encode()
    await view.put(_admin_request(body=put_body), entity_id="light.kitchen")
    stored = data.mesa.store.get("light.kitchen")
    assert stored.metadata.source.value == "user"


@pytest.mark.asyncio
async def test_delete_profile(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    put_body = json.dumps({"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}).encode()
    await view.put(_admin_request(body=put_body), entity_id="light.kitchen")
    resp = await view.delete(_admin_request(), entity_id="light.kitchen")
    assert resp.status == 200
    assert data.mesa.store.get("light.kitchen") is None


@pytest.mark.asyncio
async def test_list_profiles(hass: HomeAssistant):
    await _setup(hass)
    view_one = PhoenixAdminMesaProfileView()
    view_one.hass = hass
    for eid in ("light.a", "light.b"):
        body = json.dumps({"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}).encode()
        await view_one.put(_admin_request(body=body), entity_id=eid)

    view = PhoenixAdminMesaProfilesView()
    view.hass = hass
    resp = await view.get(_admin_request())
    out = _body(resp)
    assert out["total_matched"] == 2
    assert {p["entity_id"] for p in out["profiles"]} == {"light.a", "light.b"}


@pytest.mark.asyncio
async def test_list_domain_profiles(hass: HomeAssistant):
    await _setup(hass)
    one = PhoenixAdminMesaDomainView()
    one.hass = hass
    for domain in ("lock", "cover"):
        body = json.dumps({"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}}).encode()
        await one.put(_admin_request(body=body), domain=domain)

    view = PhoenixAdminMesaDomainsView()
    view.hass = hass
    out = _body(await view.get(_admin_request()))
    assert {d["domain"] for d in out["domains"]} == {"lock", "cover"}
    assert all("document" in d for d in out["domains"])


@pytest.mark.asyncio
async def test_list_area_profiles_empty(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaAreasView()
    view.hass = hass
    out = _body(await view.get(_admin_request()))
    assert out["areas"] == []


@pytest.mark.asyncio
async def test_list_area_profiles(hass: HomeAssistant):
    await _setup(hass)
    one = PhoenixAdminMesaAreaView()
    one.hass = hass
    body = json.dumps({"semantic_profile": {"operational_boundaries": {"control_mode": "read_only"}}}).encode()
    await one.put(_admin_request(body=body), area_id="bedroom")

    view = PhoenixAdminMesaAreasView()
    view.hass = hass
    out = _body(await view.get(_admin_request()))
    assert [a["area_id"] for a in out["areas"]] == ["bedroom"]


@pytest.mark.asyncio
async def test_vocabulary_returns_canonical_tags(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaVocabularyView()
    view.hass = hass
    out = _body(await view.get(_admin_request()))
    assert "lighting.ambient" in out["canonical_tags"]
    assert "security.camera" in out["canonical_tags"]
    assert "lighting" in out["canonical_roots"]
    # Sorted for stable autocomplete ordering.
    assert out["canonical_tags"] == sorted(out["canonical_tags"])


@pytest.mark.asyncio
async def test_defaults_round_trip(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaDefaultsView()
    view.hass = hass
    body = json.dumps({"deployment_defaults": {"default_control_mode": "confirm"}}).encode()
    put_resp = await view.put(_admin_request(body=body))
    assert put_resp.status == 200

    get_resp = await view.get(_admin_request())
    out = _body(get_resp)
    assert out["deployment_defaults"]["deployment_defaults"]["default_control_mode"] == "confirm"


@pytest.mark.asyncio
async def test_issues_endpoint_refresh(hass: HomeAssistant):
    await _setup(hass)
    # Profile declares triggers_automations: none, but an automation references it.
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    pbody = json.dumps({
        "semantic_profile": {"operational_boundaries": {"triggers_automations": "none"}}
    }).encode()
    await view.put(_admin_request(body=pbody), entity_id="input_boolean.guest_mode")

    with open(hass.config.path("automations.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            "- id: a1\n  trigger:\n    - platform: state\n"
            "      entity_id: input_boolean.guest_mode\n  action: []\n"
        )

    issues_view = PhoenixAdminMesaIssuesView()
    issues_view.hass = hass
    resp = await issues_view.get(_admin_request(query={"refresh": "1"}))
    out = _body(resp)
    assert any(i["entity_id"] == "input_boolean.guest_mode" for i in out["issues"])
    # Area and integration orphan lists are always present in the response shape.
    assert out["orphan_areas"] == []
    assert out["orphan_integrations"] == []


@pytest.mark.asyncio
async def test_endpoints_503_when_mesa_unavailable(hass: HomeAssistant):
    data = await _setup(hass)
    data.mesa = None
    view = PhoenixAdminMesaProfilesView()
    view.hass = hass
    resp = await view.get(_admin_request())
    assert resp.status == 503


@pytest.mark.asyncio
async def test_domain_profile_crud(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaDomainView()
    view.hass = hass

    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}
    }).encode()
    put_resp = await view.put(_admin_request(body=body), domain="lock")
    assert put_resp.status == 200
    assert data.mesa.store.get_domain_profile("lock") is not None

    get_resp = await view.get(_admin_request(), domain="lock")
    assert _body(get_resp)["stored"] is not None

    del_resp = await view.delete(_admin_request(), domain="lock")
    assert del_resp.status == 200
    assert data.mesa.store.get_domain_profile("lock") is None


@pytest.mark.asyncio
async def test_integration_profile_crud(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaIntegrationView()
    view.hass = hass

    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}
    }).encode()
    put_resp = await view.put(_admin_request(body=body), integration="hue")
    assert put_resp.status == 200
    assert data.mesa.store.get_integration_profile("hue") is not None

    get_resp = await view.get(_admin_request(), integration="hue")
    assert _body(get_resp)["stored"] is not None

    list_view = PhoenixAdminMesaIntegrationsView()
    list_view.hass = hass
    list_resp = await list_view.get(_admin_request())
    assert {i["integration"] for i in _body(list_resp)["integrations"]} == {"hue"}

    del_resp = await view.delete(_admin_request(), integration="hue")
    assert del_resp.status == 200
    assert data.mesa.store.get_integration_profile("hue") is None


@pytest.mark.asyncio
async def test_integration_options_lists_platforms_with_entities(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    reg.async_get_or_create("light", "hue", "u1", suggested_object_id="a")
    reg.async_get_or_create("sensor", "hue", "u2", suggested_object_id="b")  # same platform -> deduped
    reg.async_get_or_create("lock", "yale_access_bluetooth", "u3", suggested_object_id="c")
    await _setup(hass)

    view = PhoenixAdminMesaIntegrationOptionsView()
    view.hass = hass
    resp = await view.get(_admin_request())
    opts = _body(resp)["integrations"]
    assert {o["id"] for o in opts} == {"hue", "yale_access_bluetooth"}
    # Every option carries a label (friendly title, or the component id as fallback).
    assert all(o.get("name") for o in opts)


@pytest.mark.asyncio
async def test_integration_put_rejects_invalid_name(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaIntegrationView()
    view.hass = hass
    body = json.dumps({"semantic_profile": {}}).encode()
    resp = await view.put(_admin_request(body=body), integration="Not An Integration")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_domain_put_rejects_invalid_domain_name(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaDomainView()
    view.hass = hass
    body = json.dumps({"semantic_profile": {}}).encode()
    resp = await view.put(_admin_request(body=body), domain="Not A Domain")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_area_profile_crud(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaAreaView()
    view.hass = hass

    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}
    }).encode()
    put_resp = await view.put(_admin_request(body=body), area_id="bedroom")
    assert put_resp.status == 200
    assert data.mesa.store.get_area_profile("bedroom") is not None

    del_resp = await view.delete(_admin_request(), area_id="bedroom")
    assert del_resp.status == 200
    assert data.mesa.store.get_area_profile("bedroom") is None


@pytest.mark.asyncio
async def test_domain_delete_changes_entity_provenance(hass: HomeAssistant):
    # Deleting a domain profile falls entities back to the next inheritance level;
    # explain reflects the new provenance. Covers the wide-blast-radius case.
    data = await _setup(hass)
    domain_view = PhoenixAdminMesaDomainView()
    domain_view.hass = hass
    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}
    }).encode()
    await domain_view.put(_admin_request(body=body), domain="light")

    # An unprofiled light inherits confirm from the domain profile.
    before = data.mesa.store.get_effective("light.somewhere")
    assert before.operational_boundaries.control_mode.value == "confirm"

    await domain_view.delete(_admin_request(), domain="light")
    after = data.mesa.store.get_effective("light.somewhere")
    # Falls back to the built-in light baseline (autonomous).
    assert after.operational_boundaries.control_mode.value == "autonomous"


@pytest.mark.asyncio
async def test_orphans_clear_deletes_all_orphan_profiles(hass: HomeAssistant):
    data = await _setup(hass)
    runtime = data.mesa
    body = json.dumps(
        {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}}
    ).encode()

    # Seed one orphan of each kind: a stored profile whose target does not exist.
    ev = PhoenixAdminMesaProfileView()
    ev.hass = hass
    av = PhoenixAdminMesaAreaView()
    av.hass = hass
    iv = PhoenixAdminMesaIntegrationView()
    iv.hass = hass
    await ev.put(_admin_request(body=body), entity_id="input_boolean.ghost_clear")
    await av.put(_admin_request(body=body), area_id="ghost_area_clear")
    await iv.put(_admin_request(body=body), integration="ghost_integration_clear")

    assert runtime.store.get("input_boolean.ghost_clear") is not None
    assert runtime.store.get_area_profile("ghost_area_clear") is not None
    assert runtime.store.get_integration_profile("ghost_integration_clear") is not None

    clear = PhoenixAdminMesaOrphansClearView()
    clear.hass = hass
    resp = await clear.post(_admin_request())
    assert resp.status == 200
    out = _body(resp)
    assert out["count"] == 3
    assert "input_boolean.ghost_clear" in out["deleted"]["entities"]
    assert "ghost_area_clear" in out["deleted"]["areas"]
    assert "ghost_integration_clear" in out["deleted"]["integrations"]

    # Profiles are gone and the orphan lists are now empty.
    assert runtime.store.get("input_boolean.ghost_clear") is None
    assert runtime.store.get_area_profile("ghost_area_clear") is None
    assert runtime.store.get_integration_profile("ghost_integration_clear") is None
    assert list(runtime.orphans) == []
    assert list(runtime.orphan_areas) == []
    assert list(runtime.orphan_integrations) == []


@pytest.mark.asyncio
async def test_orphans_clear_no_orphans_returns_zero(hass: HomeAssistant):
    await _setup(hass)
    clear = PhoenixAdminMesaOrphansClearView()
    clear.hass = hass
    resp = await clear.post(_admin_request())
    assert resp.status == 200
    out = _body(resp)
    assert out["count"] == 0
    assert out["deleted"] == {"entities": [], "devices": [], "areas": [], "integrations": []}


@pytest.mark.asyncio
async def test_orphans_clear_503_when_mesa_unavailable(hass: HomeAssistant):
    data = await _setup(hass)
    data.mesa = None
    clear = PhoenixAdminMesaOrphansClearView()
    clear.hass = hass
    resp = await clear.post(_admin_request())
    assert resp.status == 503


# ---------------------------------------------------------------------------
# Profile suggestions (issues extension + dismiss/restore endpoints)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issues_response_carries_suggestion_keys(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaIssuesView()
    view.hass = hass

    resp = await view.get(_admin_request())
    out = _body(resp)
    assert out["suggestions"] == []
    assert out["dismissed_suggestions"] == []


@pytest.mark.asyncio
async def test_issues_refresh_computes_suggestions(hass: HomeAssistant):
    await _setup(hass)
    hass.states.async_set("lock.front", "locked")
    view = PhoenixAdminMesaIssuesView()
    view.hass = hass

    resp = await view.get(_admin_request(query={"refresh": "1"}))
    out = _body(resp)
    keys = [s["key"] for s in out["suggestions"]]
    assert "naked_risky:entity:lock.front" in keys
    row = next(s for s in out["suggestions"] if s["key"] == "naked_risky:entity:lock.front")
    assert row["suggested_mode"] == "prohibited"
    assert row["scope"] == "entity"


@pytest.mark.asyncio
async def test_dismiss_and_restore_suggestion(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaSuggestionDismissView,
        PhoenixAdminMesaSuggestionRestoreView,
    )

    data = await _setup(hass)
    hass.states.async_set("lock.front", "locked")
    dismiss = PhoenixAdminMesaSuggestionDismissView()
    dismiss.hass = hass
    restore = PhoenixAdminMesaSuggestionRestoreView()
    restore.hass = hass
    key = "naked_risky:entity:lock.front"

    body = json.dumps({"key": key}).encode()
    resp = await dismiss.post(_admin_request(body=body))
    assert resp.status == 200
    out = _body(resp)
    assert out["dismissed"] == key
    assert key not in [s["key"] for s in out["suggestions"]]
    assert out["dismissed_suggestions"] == [key]
    # Persisted: a fresh runtime from the same store still has the dismissal.
    reloaded = await async_setup_mesa(hass, "advisory")
    assert reloaded.dismissed_suggestions == {key}

    resp = await restore.post(_admin_request(body=json.dumps({"key": key}).encode()))
    assert resp.status == 200
    out = _body(resp)
    assert out["restored"] == key
    assert key in [s["key"] for s in out["suggestions"]]
    assert out["dismissed_suggestions"] == []
    assert data.mesa.dismissed_suggestions == set()


@pytest.mark.asyncio
async def test_restore_all_clears_every_dismissal(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaSuggestionDismissView,
        PhoenixAdminMesaSuggestionRestoreView,
    )

    await _setup(hass)
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("valve.main", "closed")
    dismiss = PhoenixAdminMesaSuggestionDismissView()
    dismiss.hass = hass
    for key in ("naked_risky:entity:lock.front", "naked_risky:entity:valve.main"):
        resp = await dismiss.post(_admin_request(body=json.dumps({"key": key}).encode()))
        assert resp.status == 200

    restore = PhoenixAdminMesaSuggestionRestoreView()
    restore.hass = hass
    resp = await restore.post(_admin_request(body=json.dumps({"all": True}).encode()))
    out = _body(resp)
    assert out["restored"] == 2
    assert out["dismissed_suggestions"] == []
    assert len(out["suggestions"]) == 2


@pytest.mark.asyncio
async def test_dismiss_validation_errors(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaSuggestionDismissView

    await _setup(hass)
    view = PhoenixAdminMesaSuggestionDismissView()
    view.hass = hass

    resp = await view.post(_admin_request(body=b"{}"))
    assert resp.status == 400

    resp = await view.post(_admin_request(body=json.dumps({"key": "ghost:entity:x"}).encode()))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_restore_validation_errors(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaSuggestionRestoreView

    await _setup(hass)
    view = PhoenixAdminMesaSuggestionRestoreView()
    view.hass = hass

    resp = await view.post(_admin_request(body=b"{}"))
    assert resp.status == 400

    resp = await view.post(_admin_request(body=json.dumps({"key": "never:dismissed:x"}).encode()))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_suggestion_endpoints_503_without_mesa(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaSuggestionDismissView,
        PhoenixAdminMesaSuggestionRestoreView,
    )

    data = await _setup(hass)
    data.mesa = None
    for cls in (PhoenixAdminMesaSuggestionDismissView, PhoenixAdminMesaSuggestionRestoreView):
        view = cls()
        view.hass = hass
        resp = await view.post(_admin_request(body=json.dumps({"key": "x"}).encode()))
        assert resp.status == 503


@pytest.mark.asyncio
async def test_suggestions_only_refresh_does_not_touch_orphans(hass: HomeAssistant):
    """The Suggestions card's Rescan (?refresh=suggestions) must not silently
    recompute (and potentially change) the separate, unrelated orphans/issues
    banner; only ?refresh=1 (the full refresh) does that."""
    data = await _setup(hass)
    hass.states.async_set("lock.front", "locked")
    # Simulate a stale cached orphan list (as if computed earlier at startup),
    # deliberately NOT what a live recompute would currently find.
    data.mesa.orphans = ["light.long_gone"]
    view = PhoenixAdminMesaIssuesView()
    view.hass = hass

    resp = await view.get(_admin_request(query={"refresh": "suggestions"}))
    out = _body(resp)
    assert out["orphans"] == ["light.long_gone"]  # untouched
    assert "naked_risky:entity:lock.front" in [s["key"] for s in out["suggestions"]]  # recomputed

    # A full refresh, by contrast, does recompute the orphan list for real.
    resp = await view.get(_admin_request(query={"refresh": "1"}))
    out = _body(resp)
    assert out["orphans"] != ["light.long_gone"]


# ---------------------------------------------------------------------------
# Profile export / import (mesa-core 1.2 portability)
# ---------------------------------------------------------------------------


def _profile_body(mode: str = "confirm") -> bytes:
    return json.dumps(
        {"semantic_profile": {"operational_boundaries": {"control_mode": mode}}}
    ).encode()


async def _seed_profiles(hass: HomeAssistant) -> None:
    ev = PhoenixAdminMesaProfileView()
    ev.hass = hass
    dv = PhoenixAdminMesaDomainView()
    dv.hass = hass
    await ev.put(_admin_request(body=_profile_body()), entity_id="light.kitchen")
    await dv.put(_admin_request(body=_profile_body("prohibited")), domain="lock")


@pytest.mark.asyncio
async def test_export_returns_archive_of_stored_profiles(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaExportView

    await _setup(hass)
    await _seed_profiles(hass)

    view = PhoenixAdminMesaExportView()
    view.hass = hass
    resp = await view.get(_admin_request())
    assert resp.status == 200
    archive = _body(resp)
    inner = archive["mesa_export"]
    # Pinned against the library's own current format rather than a literal.
    # Export always writes the newest format and an older reader refuses it
    # outright, so the version is the library's to choose; what this asserts is
    # that Phoenix MCP passes it through rather than stamping one of its own.
    # An unintended change of that format is caught by the vendored version pin.
    from custom_components.phoenix_mcp.mesa_core.portability import ARCHIVE_FORMAT_VERSION

    assert inner["format_version"] == ARCHIVE_FORMAT_VERSION
    assert "light.kitchen" in inner["entities"]
    assert "lock" in inner["domains"]


@pytest.mark.asyncio
async def test_import_lands_new_profiles(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaExportView,
        PhoenixAdminMesaImportView,
    )

    data = await _setup(hass)
    await _seed_profiles(hass)
    export_view = PhoenixAdminMesaExportView()
    export_view.hass = hass
    archive = _body(await export_view.get(_admin_request()))

    # Wipe the store, then import the archive back.
    runtime = data.mesa
    for key in list(runtime.backend.list_keys()):
        runtime.backend.delete(key)
    assert runtime.store.get("light.kitchen") is None

    view = PhoenixAdminMesaImportView()
    view.hass = hass
    resp = await view.post(_admin_request(body=json.dumps({"archive": archive}).encode()))
    assert resp.status == 200
    out = _body(resp)
    assert out["imported"] == 2
    assert out["overwritten"] == 0
    assert out["invalid"] == {}
    assert runtime.store.get("light.kitchen") is not None
    assert runtime.store.get_domain_profile("lock") is not None


@pytest.mark.asyncio
async def test_import_skip_leaves_existing_untouched(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaExportView,
        PhoenixAdminMesaImportView,
    )

    data = await _setup(hass)
    await _seed_profiles(hass)
    export_view = PhoenixAdminMesaExportView()
    export_view.hass = hass
    archive = _body(await export_view.get(_admin_request()))

    # Change the local profile after the export so a re-import would differ.
    ev = PhoenixAdminMesaProfileView()
    ev.hass = hass
    await ev.put(_admin_request(body=_profile_body("read_only")), entity_id="light.kitchen")

    view = PhoenixAdminMesaImportView()
    view.hass = hass
    # on_conflict omitted: defaults to skip.
    resp = await view.post(_admin_request(body=json.dumps({"archive": archive}).encode()))
    out = _body(resp)
    assert out["imported"] == 0
    assert sorted(out["skipped_existing"]) == ["domains:lock", "entities:light.kitchen"]

    runtime = data.mesa
    stored = runtime.store.get("light.kitchen")
    cm = stored.operational_boundaries.control_mode
    assert getattr(cm, "value", cm) == "read_only"  # local edit survived


@pytest.mark.asyncio
async def test_import_overwrite_replaces_existing(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaExportView,
        PhoenixAdminMesaImportView,
    )

    data = await _setup(hass)
    await _seed_profiles(hass)
    export_view = PhoenixAdminMesaExportView()
    export_view.hass = hass
    archive = _body(await export_view.get(_admin_request()))

    ev = PhoenixAdminMesaProfileView()
    ev.hass = hass
    await ev.put(_admin_request(body=_profile_body("read_only")), entity_id="light.kitchen")

    view = PhoenixAdminMesaImportView()
    view.hass = hass
    resp = await view.post(
        _admin_request(body=json.dumps({"archive": archive, "on_conflict": "overwrite"}).encode())
    )
    out = _body(resp)
    assert out["overwritten"] == 2
    assert out["imported"] == 0

    runtime = data.mesa
    stored = runtime.store.get("light.kitchen")
    cm = stored.operational_boundaries.control_mode
    assert getattr(cm, "value", cm) == "confirm"  # archive won


@pytest.mark.asyncio
async def test_import_validation_errors(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaImportView

    await _setup(hass)
    view = PhoenixAdminMesaImportView()
    view.hass = hass

    # Missing archive.
    resp = await view.post(_admin_request(body=json.dumps({}).encode()))
    assert resp.status == 400

    # Invalid on_conflict value ("error" mode deliberately not exposed).
    resp = await view.post(
        _admin_request(body=json.dumps({"archive": {}, "on_conflict": "error"}).encode())
    )
    assert resp.status == 400

    # Not a mesa_export archive.
    resp = await view.post(
        _admin_request(body=json.dumps({"archive": {"nope": 1}}).encode())
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_import_quarantines_invalid_documents(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaImportView

    data = await _setup(hass)
    archive = {
        "mesa_export": {
            "format_version": "1.0",
            "entities": {
                "light.good": {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
                "light.bad": {"semantic_profile": {"operational_boundaries": {"control_mode": "warp_speed"}}},
            },
        }
    }
    view = PhoenixAdminMesaImportView()
    view.hass = hass
    resp = await view.post(_admin_request(body=json.dumps({"archive": archive}).encode()))
    assert resp.status == 200
    out = _body(resp)
    assert out["imported"] == 1
    assert list(out["invalid"]) == ["entities:light.bad"]

    runtime = data.mesa
    assert runtime.store.get("light.good") is not None
    assert runtime.store.get("light.bad") is None


@pytest.mark.asyncio
async def test_export_import_503_when_mesa_unavailable(hass: HomeAssistant):
    from custom_components.phoenix_mcp.admin_view import (
        PhoenixAdminMesaExportView,
        PhoenixAdminMesaImportView,
    )

    data = await _setup(hass)
    data.mesa = None
    ev = PhoenixAdminMesaExportView()
    ev.hass = hass
    assert (await ev.get(_admin_request())).status == 503
    iv = PhoenixAdminMesaImportView()
    iv.hass = hass
    assert (await iv.post(_admin_request(body=b"{}"))).status == 503


@pytest.mark.asyncio
async def test_import_succeeds_even_if_post_import_rescan_fails(hass: HomeAssistant, monkeypatch):
    """A refresh_orphans/refresh_suggestions crash must not take down the import.

    The import itself (validate + write + save) already completed by the time
    the best-effort rescan runs; a scan bug (e.g. an HA-coupling regression)
    must degrade, not turn a successful import into a client-visible failure.
    """
    from custom_components.phoenix_mcp.admin_view import PhoenixAdminMesaImportView

    data = await _setup(hass)

    def _boom(hass, runtime):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr("custom_components.phoenix_mcp.mesa_suggestions.refresh_suggestions", _boom)

    archive = {
        "mesa_export": {
            "format_version": "1.0",
            "entities": {
                "light.good": {"semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}},
            },
        }
    }
    view = PhoenixAdminMesaImportView()
    view.hass = hass
    resp = await view.post(_admin_request(body=json.dumps({"archive": archive}).encode()))
    assert resp.status == 200
    out = _body(resp)
    assert out["imported"] == 1

    runtime = data.mesa
    assert runtime.store.get("light.good") is not None


@pytest.mark.asyncio
async def test_device_profile_crud(hass: HomeAssistant):
    data = await _setup(hass)
    view = PhoenixAdminMesaDeviceView()
    view.hass = hass

    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "read_only"}}
    }).encode()
    put_resp = await view.put(_admin_request(body=body), device_id="dev-abc123")
    assert put_resp.status == 200
    assert data.mesa.store.get_device_profile("dev-abc123") is not None

    get_resp = await view.get(_admin_request(), device_id="dev-abc123")
    assert _body(get_resp)["stored"] is not None

    del_resp = await view.delete(_admin_request(), device_id="dev-abc123")
    assert del_resp.status == 200
    assert data.mesa.store.get_device_profile("dev-abc123") is None


@pytest.mark.asyncio
async def test_device_profile_put_rejects_invalid_document(hass: HomeAssistant):
    await _setup(hass)
    view = PhoenixAdminMesaDeviceView()
    view.hass = hass
    bad = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "yolo"}}
    }).encode()
    resp = await view.put(_admin_request(body=bad), device_id="dev-abc123")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_devices_list_returns_stored_device_profiles(hass: HomeAssistant):
    await _setup(hass)
    one = PhoenixAdminMesaDeviceView()
    one.hass = hass
    body = json.dumps({
        "semantic_profile": {"operational_boundaries": {"control_mode": "confirm"}}
    }).encode()
    for device_id in ("dev-b", "dev-a"):
        await one.put(_admin_request(body=body), device_id=device_id)

    view = PhoenixAdminMesaDevicesView()
    view.hass = hass
    out = _body(await view.get(_admin_request()))
    assert [d["device_id"] for d in out["devices"]] == ["dev-a", "dev-b"]


@pytest.mark.asyncio
async def test_device_options_report_the_operator_facing_name(hass: HomeAssistant):
    """A device id is opaque, so the picker has to carry a readable name.

    Unlike an area, whose id an admin can recognise, a device registry id says
    nothing, which is why this endpoint exists at all. A rename by the operator
    outranks the integration's own name, matching what HA displays.
    """
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers import device_registry as dr

    await _setup(hass)
    entry = ConfigEntry(
        version=1, minor_version=1, domain="demo", title="demo", data={}, source="user",
        options={}, unique_id=None, discovery_keys={}, subentries_data=(),
    )
    hass.config_entries._entries[entry.entry_id] = entry
    registry = dr.async_get(hass)
    plain = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "plain")}, name="Zigbee Lock",
    )
    renamed = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "renamed")}, name="Vendor Name",
    )
    registry.async_update_device(renamed.id, name_by_user="Front Door")

    view = PhoenixAdminMesaDeviceOptionsView()
    view.hass = hass
    options = {d["id"]: d["name"] for d in _body(await view.get(_admin_request()))["devices"]}
    assert options[plain.id] == "Zigbee Lock"
    assert options[renamed.id] == "Front Door"


@pytest.mark.asyncio
async def test_put_stamps_the_current_schema_version(hass: HomeAssistant):
    """A profile authored NOW must not be stored labelled as 1.0-era content.

    mesa-core reads an absent `schema_version` as 1.0, because 1.0 is the only
    version that could have written an unversioned document. That inference is
    right for a file found on disk and wrong for a document this panel just
    produced, so the write boundary migrates it. Migration is the ONLY operation
    allowed to set the label: mesa-core never stamps on save, or a future
    version needing a real transformation would skip every document that had
    ever been written.
    """
    from custom_components.phoenix_mcp.mesa_core.migration import CURRENT_SCHEMA_VERSION

    data = await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    # No schema_version, which is exactly what the panel sends.
    put_body = json.dumps({"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}).encode()

    await view.put(_admin_request(body=put_body), entity_id="light.kitchen")

    stored = data.mesa.store.get("light.kitchen")
    assert stored.to_dict()["semantic_profile"]["schema_version"] == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_every_scope_stamps_the_version_not_just_entities(hass: HomeAssistant):
    """All five write boundaries, because they are five separate handlers.

    Wiring one and forgetting the others is the obvious way to half-fix this,
    and nothing else would notice: an unstamped document still resolves
    identically today.
    """
    from custom_components.phoenix_mcp.mesa_core.migration import CURRENT_SCHEMA_VERSION

    data = await _setup(hass)
    body = json.dumps({"semantic_profile": {"semantic_tags": ["lighting.ambient"]}}).encode()

    domain_view = PhoenixAdminMesaDomainView(); domain_view.hass = hass
    await domain_view.put(_admin_request(body=body), domain="light")
    area_view = PhoenixAdminMesaAreaView(); area_view.hass = hass
    await area_view.put(_admin_request(body=body), area_id="kitchen")
    integ_view = PhoenixAdminMesaIntegrationView(); integ_view.hass = hass
    await integ_view.put(_admin_request(body=body), integration="hue")
    device_view = PhoenixAdminMesaDeviceView(); device_view.hass = hass
    await device_view.put(_admin_request(body=body), device_id="abc123")

    store = data.mesa.store
    for stored in (
        store.get_domain_profile("light"),
        store.get_area_profile("kitchen"),
        store.get_integration_profile("hue"),
        store.get_device_profile("abc123"),
    ):
        assert stored is not None
        assert stored.to_dict()["semantic_profile"]["schema_version"] == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_a_bare_semantic_profile_body_is_stamped_too(hass: HomeAssistant):
    """from_dict accepts TWO body shapes, so both have to be stamped.

    A body with no `semantic_profile` key is treated as the semantic profile
    itself. Guarding on that key alone therefore skipped a legitimate shape and
    stored it still claiming to be 1.0.
    """
    from custom_components.phoenix_mcp.mesa_core.migration import CURRENT_SCHEMA_VERSION

    data = await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    bare = json.dumps({"semantic_tags": ["lighting.ambient"]}).encode()

    await view.put(_admin_request(body=bare), entity_id="light.kitchen")

    stored = data.mesa.store.get("light.kitchen")
    assert stored.to_dict()["semantic_profile"]["schema_version"] == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_an_unparseable_version_does_not_become_a_500(hass: HomeAssistant):
    """Migration fails SOFT so a caller's bad value reaches the validator.

    `migrate_profile` raises on a version it cannot parse. Calling it unguarded
    would turn that into an unhandled error rather than the validation response
    the caller deserves.
    """
    await _setup(hass)
    view = PhoenixAdminMesaProfileView()
    view.hass = hass
    body = json.dumps({"semantic_profile": {"schema_version": "not-a-version"}}).encode()

    resp = await view.put(_admin_request(body=body), entity_id="light.kitchen")

    assert resp.status in (200, 400), "must be answered, never raised"
