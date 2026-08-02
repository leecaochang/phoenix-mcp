"""Tests for the MCP protocol layer: discovery, version negotiation, unknown methods.

These cover the three things a client learns about the server before it learns
anything about Home Assistant: which protocol revisions this transport claims,
who the server says it is, and what happens to a method it does not implement.

The version list is the load-bearing one. It is a compliance CLAIM (a client is
entitled to assume every MUST of a listed revision is honored), so the pins here
assert what is deliberately ABSENT as well as what is present.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from unittest.mock import patch

import pytest

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.const import (
    MCP_DISCOVER_TTL_MS,
    MCP_PROTOCOL_VERSION_PREFERRED,
    MCP_PROTOCOL_VERSIONS,
)
from custom_components.phoenix_mcp.mcp_view import _MCP_METHODS, _dispatch_mcp

from tests.test_mcp_sse import _FakeStream, _make_request, _stream_patch, _view
from tests.test_mcp_view import _make_data, _make_hass, _make_token

_BASE_URL = "http://homeassistant.local"


def _env(**token_kwargs):
    token, raw = _make_token(**token_kwargs)
    data = _make_data(token)
    hass = _make_hass(data)
    return token, raw, data, hass


async def _dispatch(method: str, params: dict | None = None, msg_id=1, **token_kwargs):
    token, _raw, data, hass = _env(**token_kwargs)
    return await _dispatch_mcp(
        method, msg_id, params or {}, token, hass, data, "127.0.0.1",
        base_url=_BASE_URL,
    )


# --- server/discover ---------------------------------------------------------


class TestServerDiscover:
    async def test_returns_versions_capabilities_and_identity(self):
        resp, method, resource, outcome = await _dispatch("server/discover")

        result = resp["result"]
        assert result["supportedVersions"] == list(MCP_PROTOCOL_VERSIONS)
        assert result["capabilities"] == mcp_view._SERVER_CAPABILITIES
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == MCP_DISCOVER_TTL_MS
        server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert server_info["name"] == "Phoenix MCP"
        assert server_info["version"]
        assert (method, resource, outcome) == (
            "server/discover", "/api/phoenix-mcp", "allowed")

    async def test_cache_scope_is_private_never_public(self):
        # The instructions name THIS token's confirm-gated capabilities. A
        # "public" scope invites a shared intermediary to hand one token's
        # answer to the next token reaching it through the same proxy.
        resp, _m, _r, _o = await _dispatch("server/discover")
        assert resp["result"]["cacheScope"] == "private"

    async def test_instructions_are_token_aware(self):
        # The whole reason discover carries instructions: a client on a revision
        # with no initialize would otherwise never be told which capabilities
        # are approval-gated.
        resp, _m, _r, _o = await _dispatch(
            "server/discover", cap_automation_write="confirm")
        assert "cap_automation_write" in resp["result"]["instructions"]

    async def test_capabilities_are_the_same_claim_as_initialize(self):
        # One definition, so a client reading only one of the two never learns
        # something different from a client reading the other.
        discover, _m, _r, _o = await _dispatch("server/discover")
        initialize, _m2, _r2, _o2 = await _dispatch("initialize")
        assert discover["result"]["capabilities"] == initialize["result"]["capabilities"]

    async def test_advertises_no_push_capability(self):
        # Stateless transport: nothing is held open to push a list-changed down,
        # and saying otherwise would have clients wait for a signal that never
        # arrives instead of re-listing.
        caps = mcp_view._SERVER_CAPABILITIES
        assert caps["tools"]["listChanged"] is False
        assert caps["resources"]["subscribe"] is False

    async def test_is_audited(self):
        token, _raw, data, hass = _env()
        with patch.object(mcp_view, "_log") as logged:
            await _dispatch_mcp(
                "server/discover", 1, {}, token, hass, data, "127.0.0.1",
                base_url=_BASE_URL)
        assert logged.call_count == 1
        assert logged.call_args.kwargs["method"] == "server/discover"
        assert logged.call_args.kwargs["outcome"] == "allowed"

    async def test_requires_a_token(self):
        # The spec presents discover as a pre-flight call, which makes
        # "it can be unauthenticated" an easy assumption. It carries this
        # token's own capability primer, so it is behind the bearer token like
        # everything else on this endpoint.
        _token, raw, data, hass = _env()
        data.store.get_token_by_hash.return_value = None
        resp = await _view(hass).post(_make_request(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            "application/json", raw))
        assert resp.status == 401

    async def test_no_handshake_required(self):
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(
            {"jsonrpc": "2.0", "id": 9, "method": "server/discover"},
            "application/json", raw))
        assert resp.status == 200
        assert json.loads(resp.text)["result"]["supportedVersions"]


# --- protocol version negotiation --------------------------------------------


class TestProtocolVersions:
    async def test_initialize_echoes_a_supported_version(self):
        resp, _m, _r, _o = await _dispatch(
            "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION_PREFERRED})
        assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION_PREFERRED

    async def test_initialize_echoes_a_non_preferred_supported_version(self):
        # The assertion above is VACUOUS while the list holds one entry: echoing
        # the request and always answering with the preferred version produce
        # the same string. Pin the real behaviour against a two-version server
        # so the echo cannot quietly regress the day a second one is added.
        # Patched on mcp_view (the reader), not const; patching the definition
        # module would not reach the already-imported name.
        with patch.object(mcp_view, "MCP_PROTOCOL_VERSIONS", ("2026-01-01", "2025-03-26")), \
             patch.object(mcp_view, "MCP_PROTOCOL_VERSION_PREFERRED", "2026-01-01"):
            resp, _m, _r, _o = await _dispatch(
                "initialize", {"protocolVersion": "2025-03-26"})
        assert resp["result"]["protocolVersion"] == "2025-03-26"

    async def test_discover_reports_every_supported_version(self):
        with patch.object(mcp_view, "MCP_PROTOCOL_VERSIONS", ("2026-01-01", "2025-03-26")):
            resp, _m, _r, _o = await _dispatch("server/discover")
        assert resp["result"]["supportedVersions"] == ["2026-01-01", "2025-03-26"]

    async def test_initialize_names_its_preferred_version_for_an_unknown_one(self):
        resp, _m, _r, _o = await _dispatch(
            "initialize", {"protocolVersion": "1900-01-01"})
        assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION_PREFERRED

    async def test_initialize_without_a_requested_version(self):
        resp, _m, _r, _o = await _dispatch("initialize", {})
        assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION_PREFERRED

    @pytest.mark.parametrize(
        "bogus", [None, 42, ["2025-03-26"], {"v": "2025-03-26"}, True])
    async def test_initialize_survives_a_wrong_shaped_version(self, bogus):
        # A model (or a malformed client) does not always send what the schema
        # declares. Tuple membership compares rather than hashes, so an
        # unhashable value degrades to the preferred version instead of raising.
        resp, _m, _r, _o = await _dispatch(
            "initialize", {"protocolVersion": bogus})
        assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION_PREFERRED

    def test_versions_are_not_overclaimed(self):
        # These two are ABSENT on purpose and adding either without the work is
        # the failure this pins. 2025-06-18 requires the server to validate the
        # MCP-Protocol-Version header and reject an unsupported value with 400;
        # this transport ignores that header and still accepts the JSON-RPC
        # batches that revision removed. 2026-07-28 additionally requires
        # header/body validation, a resultType on every result, and cache fields
        # on every list result.
        assert "2025-06-18" not in MCP_PROTOCOL_VERSIONS
        assert "2026-07-28" not in MCP_PROTOCOL_VERSIONS

    def test_version_list_is_ordered_newest_first(self):
        assert MCP_PROTOCOL_VERSION_PREFERRED == MCP_PROTOCOL_VERSIONS[0]
        assert list(MCP_PROTOCOL_VERSIONS) == sorted(MCP_PROTOCOL_VERSIONS, reverse=True)


# --- unknown methods ---------------------------------------------------------


_UNKNOWN = {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe"}


class TestUnknownMethod:
    async def test_returns_http_404_with_method_not_found(self):
        # 404 + a -32601 body is what tells a client this IS an MCP endpoint
        # that lacks the method, versus a 404 from a server hosting none.
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(_UNKNOWN, "application/json", raw))
        assert resp.status == 404
        assert json.loads(resp.text)["error"]["code"] == -32601

    async def test_is_404_for_an_sse_client_too(self):
        # The whole reason the check sits ahead of the framing choice: preparing
        # a stream puts a 200 on the wire and the status can no longer be set.
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream):
            resp = await _view(hass).post(_make_request(
                _UNKNOWN, "application/json, text/event-stream", raw))
        assert not stream.prepared
        assert resp.status == 404
        assert json.loads(resp.text)["error"]["code"] == -32601

    async def test_unknown_notification_is_accepted(self):
        # A valid envelope whose method we do not implement owes no reply.
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(
            {"jsonrpc": "2.0", "method": "notifications/cancelled"},
            "application/json", raw))
        assert resp.status == 202

    async def test_batch_keeps_the_error_in_the_body(self):
        # A batch is one HTTP response for N items, so a per-item status cannot
        # be expressed; the JSON-RPC error carries it instead.
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request([_UNKNOWN], "application/json", raw))
        assert resp.status == 200
        assert json.loads(resp.text)[0]["error"]["code"] == -32601

    @pytest.mark.parametrize("method", sorted(_MCP_METHODS))
    async def test_known_methods_are_never_404(self, method):
        _token, raw, _data, hass = _env()
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
        if method.startswith("notifications/") or method == "initialized":
            body.pop("id")
        resp = await _view(hass).post(_make_request(body, "application/json", raw))
        assert resp.status != 404


class TestMethodRegistryMatchesDispatcher:
    def test_registry_and_dispatcher_agree(self):
        # _MCP_METHODS mirrors the dispatcher's own branches, and a mirror with
        # nothing holding it in place drifts. Either direction is a real defect:
        # a method missing here is answered 404 despite being implemented, and a
        # stale entry here streams a method that does not exist.
        assert _dispatcher_methods() == set(_MCP_METHODS)


def _dispatcher_methods() -> set[str]:
    """Every literal `_dispatch_mcp` compares its `method` argument against."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_dispatch_mcp)))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "method"):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                found.add(comparator.value)
            elif isinstance(op, ast.In) and isinstance(comparator, (ast.Tuple, ast.List)):
                found.update(
                    e.value for e in comparator.elts if isinstance(e, ast.Constant))
    return found


def test_dispatcher_method_extraction_actually_finds_branches():
    # The AST walk above is the kind of guard that passes vacuously when it
    # silently matches nothing, so prove it reads real branches out.
    assert "tools/call" in _dispatcher_methods()
    assert len(_dispatcher_methods()) > 5
