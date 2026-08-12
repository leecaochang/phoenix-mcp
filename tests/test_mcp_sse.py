"""Tests for the MCP endpoint's SSE response framing and progress notifications.

The same single JSON-RPC response is delivered either as a plain JSON body or as
one SSE `message` event, chosen by the client's Accept header. The SSE path also
writes a frame every MCP_SSE_KEEPALIVE_SECONDS while the request is in flight, so
a confirm gate holding a response for minutes does not look like a dead
connection, and carries notifications/progress when the client asked for them.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.mcp_view import (
    PhoenixMcpView,
    _batch_expects_response,
    _progress_token,
)

from tests.test_mcp_view import _make_data, _make_hass, _make_token


class _FakeStream:
    """Captures what the view writes to a prepared StreamResponse."""

    def __init__(self, fail_writes: bool = False) -> None:
        self.frames: list[bytes] = []
        self.prepared = False
        self.eof = False
        self.fail_writes = fail_writes
        self.status = 200
        self.headers: dict[str, str] = {}

    async def prepare(self, _request):
        self.prepared = True

    async def write(self, data: bytes) -> None:
        if self.fail_writes:
            raise ConnectionResetError("client gone")
        self.frames.append(data)

    async def write_eof(self) -> None:
        self.eof = True

    # --- helpers the assertions use -------------------------------------
    @property
    def text(self) -> str:
        return b"".join(self.frames).decode()

    def messages(self) -> list[dict]:
        out = []
        for frame in self.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data: "):
                    out.append(json.loads(line[len("data: "):]))
        return out

    def keepalives(self) -> int:
        return self.text.count(": keepalive")


def _stream_patch(stream: _FakeStream):
    def _factory(*_a, status=200, headers=None, **_kw):
        stream.status = status
        stream.headers = dict(headers or {})
        return stream
    return patch.object(mcp_view.web, "StreamResponse", side_effect=_factory)


def _make_request(
    body: dict | list,
    accept: str,
    raw_token: str,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    payload = json.dumps(body).encode()
    req = MagicMock()
    req.method = "POST"
    req.remote = "127.0.0.1"
    headers = {"Authorization": f"Bearer {raw_token}", "Accept": accept}
    headers.update(extra_headers or {})
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda k, default="": headers.get(k, default))
    req.content_length = len(payload)
    chunks = [payload, b""]
    req.content = MagicMock()
    req.content.read = AsyncMock(side_effect=lambda *_a: chunks.pop(0) if chunks else b"")
    req.content.at_eof = lambda: not chunks
    req.url = MagicMock()
    req.url.origin.return_value = "http://homeassistant.local"
    req.query = {}
    return req


def _view(hass) -> PhoenixMcpView:
    view = PhoenixMcpView()
    view.hass = hass
    return view


def _env():
    token, raw = _make_token(cap_config_read="allow")
    data = _make_data(token)
    hass = _make_hass(data)
    hass.loop = asyncio.get_running_loop()
    return token, raw, data, hass


_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


class TestAcceptNegotiation:
    async def test_json_only_client_is_unchanged(self):
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(_PING, "application/json", raw))
        assert resp.content_type == "application/json"
        assert json.loads(resp.text)["id"] == 1

    async def test_sse_client_gets_one_message_event(self):
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(
                _make_request(_PING, "application/json, text/event-stream", raw))
        assert stream.prepared and stream.eof
        assert stream.headers["Content-Type"] == "text/event-stream"
        assert stream.text.startswith("event: message\n")
        assert stream.messages() == [json.loads(
            json.dumps((await _dispatch_plain(hass, raw))))]

    async def test_sse_payload_is_identical_to_the_json_payload(self):
        _token, raw, _data, hass = _env()
        plain = await _view(hass).post(_make_request(_PING, "application/json", raw))
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(_make_request(_PING, "text/event-stream", raw))
        assert stream.messages() == [json.loads(plain.text)]

    async def test_request_id_and_rate_limit_headers_ride_the_stream(self):
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(_make_request(_PING, "text/event-stream", raw))
        assert stream.headers["X-Phoenix-Request-ID"]
        assert stream.headers["X-RateLimit-Limit"] == "60"
        assert "X-RateLimit-Remaining" in stream.headers

    async def test_notification_stays_a_bare_202_even_with_sse_accept(self):
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            "text/event-stream", raw))
        assert resp.status == 202

    async def test_parse_error_stays_plain_json(self):
        _token, raw, _data, hass = _env()
        req = _make_request({}, "text/event-stream", raw)
        chunks = [b"{not json", b""]
        req.content.read = AsyncMock(side_effect=lambda *_a: chunks.pop(0) if chunks else b"")
        req.content.at_eof = lambda: not chunks
        resp = await _view(hass).post(req)
        assert resp.content_type == "application/json"
        assert json.loads(resp.text)["error"]["code"] == -32700

    async def test_invalid_envelope_stays_plain_json(self):
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(
            {"jsonrpc": "2.0", "id": 1, "method": 7}, "text/event-stream", raw))
        assert resp.content_type == "application/json"
        assert json.loads(resp.text)["error"]["code"] == -32600


async def _dispatch_plain(hass, raw):
    resp = await _view(hass).post(_make_request(_PING, "application/json", raw))
    return json.loads(resp.text)


class TestBatchOverSse:
    _BATCH = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]

    async def test_batch_returns_one_frame_with_every_entry(self):
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(_make_request(self._BATCH, "text/event-stream", raw))
        [payload] = stream.messages()
        assert [item["id"] for item in payload] == [1, 2]

    async def test_batch_matches_the_json_path(self):
        _token, raw, _data, hass = _env()
        plain = await _view(hass).post(_make_request(self._BATCH, "application/json", raw))
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(_make_request(self._BATCH, "text/event-stream", raw))
        assert stream.messages() == [json.loads(plain.text)]

    async def test_all_notification_batch_stays_202(self):
        _token, raw, _data, hass = _env()
        resp = await _view(hass).post(_make_request(
            [{"jsonrpc": "2.0", "method": "notifications/initialized"}],
            "text/event-stream", raw))
        assert resp.status == 202

    async def test_per_item_errors_survive_the_framing(self):
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream):
            await _view(hass).post(_make_request(
                [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, "garbage"],
                "text/event-stream", raw))
        [payload] = stream.messages()
        assert payload[1]["error"]["code"] == -32600


class TestBatchExpectsResponse:
    """The pre-scan must agree with what the dispatcher actually replies to."""

    @pytest.mark.parametrize(
        ("items", "expected"),
        [
            ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], True),
            ([{"jsonrpc": "2.0", "method": "notifications/initialized"}], False),
            ([{"jsonrpc": "2.0", "method": 7}], True),  # malformed no-id: -32600
            ([{"jsonrpc": "2.0", "method": "ping", "params": []}], False),  # positional notification
            ([{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}], True),
            (["garbage"], True),
            ([{"jsonrpc": "2.0", "id": 1, "result": {}}], False),  # a response object
            ([{"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"jsonrpc": "2.0", "id": 2, "method": "ping"}], True),
        ],
    )
    async def test_agrees_with_the_dispatcher(self, items, expected):
        token, _raw, data, hass = _env()
        assert _batch_expects_response(items) is expected
        actual = await mcp_view._dispatch_streamable_batch(
            items, token, hass, data, "127.0.0.1", base_url="http://h")
        assert bool(actual) is expected


class TestProgressToken:
    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"_meta": {"progressToken": "abc"}}, "abc"),
            ({"_meta": {"progressToken": 7}}, 7),
            ({"_meta": {"progressToken": True}}, None),  # bool is not an int here
            ({"_meta": {"progressToken": {"nested": 1}}}, None),
            ({"_meta": {"progressToken": ""}}, None),
            ({"_meta": "not-a-dict"}, None),
            ({}, None),
        ],
    )
    def test_extraction_is_tolerant(self, params, expected):
        assert _progress_token("tools/call", params) == expected

    def test_only_tool_calls_carry_progress(self):
        assert _progress_token("ping", {"_meta": {"progressToken": "abc"}}) is None


class TestKeepaliveAndProgress:
    """A slow dispatch must produce frames before the response frame."""

    def _slow_call(self, hass, seconds: float = 0.05):
        async def _slow(*_a, **_kw):
            mcp_view._set_progress_status("Waiting for operator approval: call_service", total=99.0)
            await asyncio.sleep(seconds)
            return {"content": [{"type": "text", "text": "done"}]}, "allowed", "x"
        return patch.object(mcp_view, "_call_tool", side_effect=_slow)

    def _tool_call(self, meta: dict | None = None) -> dict:
        params: dict = {"name": "get_config", "arguments": {}}
        if meta is not None:
            params["_meta"] = meta
        return {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params}

    async def test_keepalive_comments_when_no_progress_token(self, monkeypatch):
        monkeypatch.setattr(mcp_view, "MCP_SSE_KEEPALIVE_SECONDS", 0.01)
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream), self._slow_call(hass):
            await _view(hass).post(_make_request(self._tool_call(), "text/event-stream", raw))
        assert stream.keepalives() >= 1
        assert not [m for m in stream.messages() if m.get("method") == "notifications/progress"]
        assert stream.messages()[-1]["id"] == 9

    async def test_progress_notifications_when_token_supplied(self, monkeypatch):
        monkeypatch.setattr(mcp_view, "MCP_SSE_KEEPALIVE_SECONDS", 0.01)
        _token, raw, _data, hass = _env()
        stream = _FakeStream()
        with _stream_patch(stream), self._slow_call(hass):
            await _view(hass).post(_make_request(
                self._tool_call({"progressToken": "p1"}), "text/event-stream", raw))
        progress = [m for m in stream.messages() if m.get("method") == "notifications/progress"]
        assert progress, "expected at least one progress notification"
        assert stream.keepalives() == 0  # a real frame keeps the connection warm
        first = progress[0]["params"]
        assert first["progressToken"] == "p1"
        assert first["total"] == 99.0
        assert "Waiting for operator approval" in first["message"]
        # Progress must increase for a given token, per the MCP spec.
        values = [m["params"]["progress"] for m in progress]
        assert values == sorted(values)
        # Notifications carry no id and never enter the response.
        assert all("id" not in m for m in progress)
        assert stream.messages()[-1]["id"] == 9

    async def test_json_client_with_a_progress_token_gets_no_frames(self):
        _token, raw, _data, hass = _env()
        with self._slow_call(hass, seconds=0.0):
            resp = await _view(hass).post(_make_request(
                self._tool_call({"progressToken": "p1"}), "application/json", raw))
        assert resp.content_type == "application/json"
        assert json.loads(resp.text)["id"] == 9

    async def test_dispatch_completes_after_the_client_disconnects(self, monkeypatch):
        """A side effect must never be half-applied because nobody is listening."""
        monkeypatch.setattr(mcp_view, "MCP_SSE_KEEPALIVE_SECONDS", 0.01)
        _token, raw, _data, hass = _env()
        finished = asyncio.Event()

        async def _slow(*_a, **_kw):
            await asyncio.sleep(0.05)
            finished.set()
            return {"content": [{"type": "text", "text": "done"}]}, "allowed", "x"

        stream = _FakeStream(fail_writes=True)
        with _stream_patch(stream), patch.object(mcp_view, "_call_tool", side_effect=_slow):
            await _view(hass).post(_make_request(self._tool_call(), "text/event-stream", raw))
        assert finished.is_set()
        assert stream.frames == []  # nothing could be written
