"""Tests for the ESPHome Device Builder WebSocket client (esphome_builder.py).

The transport is exercised against a REAL local aiohttp server rather than a
stubbed ws_connect: the framing, the close behaviour, and the interaction with
asyncio.timeout are exactly what a hand-rolled fake would have to guess at, and
the harness already allows 127.0.0.1 sockets and ships an aiohttp client fixture.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from custom_components.phoenix_mcp import esphome_builder
from custom_components.phoenix_mcp.const import ESPHOME_BUILDER_VERIFIED_VERSION
from custom_components.phoenix_mcp.esphome_builder import (
    ESPHOME_BUILDER_ALLOWED_COMMANDS,
    BuilderAuthRequired,
    BuilderCommandError,
    BuilderUnavailable,
    async_builder_command,
    version_mismatch_note,
)

# Announces the VERIFIED version by default, so the ordinary tests model a
# deployment Phoenix has actually been checked against and no version note is
# appended to their errors. The mismatch case gets its own tests below.
SERVER_INFO = {
    "server_version": ESPHOME_BUILDER_VERIFIED_VERSION,
    "esphome_version": "2026.3.1",
    "port": 6052,
    "ha_addon": True,
    "ha_ingress": False,
    "requires_auth": False,
}


class FakeDeviceBuilder:
    """A scriptable stand-in for the add-on's /ws endpoint."""

    def __init__(self, *, requires_auth=False, replies=None, hang=False, keep_open=False):
        self.requires_auth = requires_auth
        # Each entry is either a frame dict or a list of frames to send in order.
        self.replies = replies or {}
        self.hang = hang
        # keep_open models an open-ended stream like devices/logs: frames are
        # sent and then the socket just stays up, with no result frame ever.
        self.keep_open = keep_open
        self.received: list[dict] = []
        # Overridable per test so the unverified-version path can be exercised.
        self.server_version = SERVER_INFO["server_version"]

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({
            **SERVER_INFO,
            "server_version": self.server_version,
            "requires_auth": self.requires_auth,
        }))
        if self.hang:
            await asyncio.sleep(30)
            return ws
        async for msg in ws:
            frame = json.loads(msg.data)
            self.received.append(frame)
            reply = self.replies.get(frame["command"], {"message_id": "1", "result": {}})
            for out in reply if isinstance(reply, list) else [reply]:
                await ws.send_str(json.dumps(out))
            if self.keep_open:
                # Stay readable so the client's own stream-stop frame is recorded
                # rather than lost when the socket is torn down.
                continue
            await ws.close()
            break
        return ws

    def app(self):
        app = web.Application()
        app.router.add_get("/ws", self.handler)
        return app


async def connect(fake, aiohttp_client, monkeypatch):
    """Point the client's session getter at a local server running `fake`."""
    client = await aiohttp_client(fake.app())
    monkeypatch.setattr(esphome_builder, "async_get_clientsession", lambda hass: client.session)
    return str(client.make_url("")).rstrip("/")


def stream(*outputs, success=True, code=0):
    frames = [{"message_id": "1", "event": "output", "data": o} for o in outputs]
    frames.append({
        "message_id": "1",
        "event": "result",
        "data": {"success": success, "code": code},
    })
    return frames


async def test_request_response_command_round_trips(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(replies={
        "boards/get_board": {"message_id": "1", "result": {"id": "esp32dev", "pins": []}},
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "boards/get_board", {"board_id": "esp32dev"}, timeout_seconds=5
    )

    assert res.result == {"id": "esp32dev", "pins": []}
    assert res.output == ""


async def test_streaming_command_collects_output_and_result(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream("INFO Reading configuration\n", "ERROR bad pin\n",
                                   success=False, code=1),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output == "INFO Reading configuration\nERROR bad pin\n"
    assert res.success is False
    assert res.exit_code == 1
    assert res.output_truncated is False


async def test_output_overflow_keeps_head_and_tail_and_flags_truncated(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    monkeypatch.setattr(esphome_builder, "ESPHOME_BUILDER_OUTPUT_MAX_CHARS", 100)
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream("HEAD" + ("x" * 500) + "TAIL", success=False, code=1),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output_truncated is True
    assert res.output.startswith("HEAD")
    # The failing line lands at the end of validation output, so the tail is the
    # part a caller cannot afford to lose.
    assert res.output.rstrip("\n").endswith("TAIL")
    assert len(res.output) < 500


async def test_ansi_escapes_stripped_from_output(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream("\x1b[31mERROR\x1b[0m bad pin\n"),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output == "ERROR bad pin\n"
    assert "\x1b" not in res.output


async def test_literal_backslash_ansi_stripped(hass, aiohttp_client, monkeypatch, socket_enabled):
    """The live add-on sends the escape as the literal characters \\033[, not a 0x1b byte.

    Found by smoke-testing against a real Device Builder: a regex matching only
    the real byte stripped nothing, and every line of validator output arrived
    wrapped in visible colour codes.
    """
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream(r"\033[32mINFO ESPHome 2026.7.3\033[0m"),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output == "INFO ESPHome 2026.7.3\n"
    assert "033" not in res.output


async def test_line_structure_survives_terminatorless_chunks(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    """The live add-on sends one event per line with the newline already stripped.

    Concatenating verbatim collapsed a whole config dump into one unreadable
    line. Chunks are re-terminated only when they did not bring their own.
    """
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream("esphome:", "  name: dev", "  board: esp32dev"),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output == "esphome:\n  name: dev\n  board: esp32dev\n"


async def test_existing_terminators_are_not_doubled(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(replies={
        "devices/validate": stream("line one\n", "line two\n"),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "a.yaml"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.output == "line one\nline two\n"


async def test_requires_auth_handshake_raises_and_sends_no_command(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    fake = FakeDeviceBuilder(requires_auth=True)
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderAuthRequired):
        await async_builder_command(hass, url, "boards/get_boards", {}, timeout_seconds=5)

    # The refusal must happen before anything is sent, not after a rejected command.
    assert fake.received == []


async def test_not_authenticated_error_frame_raises_auth_required(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    fake = FakeDeviceBuilder(replies={
        "boards/get_boards": {"message_id": "1", "error_code": "not_authenticated"},
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderAuthRequired):
        await async_builder_command(hass, url, "boards/get_boards", {}, timeout_seconds=5)


async def test_error_frame_raises_command_error_with_code_and_details(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    fake = FakeDeviceBuilder(replies={
        "devices/validate": {
            "message_id": "1", "error_code": "not_found", "details": "no such configuration",
        },
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderCommandError) as err:
        await async_builder_command(
            hass, url, "devices/validate", {"configuration": "ghost.yaml"}, timeout_seconds=5
        )

    assert err.value.error_code == "not_found"
    # No version note: this add-on announces the version Phoenix was verified
    # against, so the failure is a genuine one and blaming a version difference
    # would send the operator to upgrade something that was never the problem.
    assert err.value.details == "no such configuration"


async def test_a_failure_from_an_unverified_version_says_so(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    """The version arrives free on every handshake, so it is worth keeping.

    An add-on that no longer has a command refuses it in its own words, which
    reads as a Phoenix bug. Naming the version turns "not_found" into a
    diagnosis the operator can act on.
    """
    fake = FakeDeviceBuilder(replies={
        "devices/validate": {
            "message_id": "1", "error_code": "unknown_command", "details": "no such command",
        },
    })
    fake.server_version = "2099.1.0"
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderCommandError) as err:
        await async_builder_command(
            hass, url, "devices/validate", {"configuration": "x.yaml"}, timeout_seconds=5
        )

    assert err.value.error_code == "unknown_command"
    assert "no such command" in err.value.details
    assert "2099.1.0" in err.value.details
    assert ESPHOME_BUILDER_VERIFIED_VERSION in err.value.details


async def test_the_announced_version_rides_the_result(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    # Carried so a caller that cannot parse a reply can name the version that
    # produced it, which is the difference between "unexpected reply" and a
    # diagnosis. See the rename executor's unreadable-job branch.
    fake = FakeDeviceBuilder(replies={
        "devices/validate": {"message_id": "1", "result": {"ok": True}},
    })
    fake.server_version = "2099.1.0"
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "devices/validate", {"configuration": "x.yaml"}, timeout_seconds=5)

    assert res.server_version == "2099.1.0"


def test_a_matching_version_is_never_mentioned():
    # A version difference on its own is NOT worth saying out loud: a warning
    # whose consequence never arrives teaches operators to click past it, the
    # same reason the install diff refuses to flag a patch bump.
    assert version_mismatch_note("devices/validate", ESPHOME_BUILDER_VERIFIED_VERSION) is None
    assert version_mismatch_note("devices/validate", "") is None
    assert "2099.1.0" in version_mismatch_note("devices/validate", "2099.1.0")


async def test_connect_refused_raises_unavailable(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder()
    await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderUnavailable):
        # Port 1 on loopback is never a Device Builder.
        await async_builder_command(
            hass, "http://127.0.0.1:1", "boards/get_boards", {}, timeout_seconds=5
        )


async def test_timeout_raises_unavailable(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(hang=True)
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderUnavailable):
        await async_builder_command(hass, url, "boards/get_boards", {}, timeout_seconds=0.2)


async def test_message_id_is_a_string_and_args_pass_through(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder()
    url = await connect(fake, aiohttp_client, monkeypatch)

    await async_builder_command(
        hass, url, "components/get_components", {"query": "dht", "limit": 5}, timeout_seconds=5
    )

    assert fake.received == [{
        "command": "components/get_components",
        "message_id": "1",
        "args": {"query": "dht", "limit": 5},
    }]
    assert isinstance(fake.received[0]["message_id"], str)


async def test_follow_job_speaks_the_same_output_result_envelope(hass, aiohttp_client, monkeypatch, socket_enabled):
    """The singular follow uses the generic envelope, not the job_* frame names.

    This is the fact that lets the existing streaming client fetch a finished
    build's log with no new framing code, so it is worth pinning: the add-on's
    own API.md documents the job_output / job_completed names, which belong to
    the PLURAL follow_jobs and would need a different reader.
    """
    fake = FakeDeviceBuilder(replies={
        "firmware/follow_job": stream("Compiling .pioenvs/x/src/main.cpp", "Success", code=0),
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await async_builder_command(
        hass, url, "firmware/follow_job", {"job_id": "abc123"},
        timeout_seconds=5, collect_output=True,
    )

    assert res.success is True
    assert res.exit_code == 0
    assert "Compiling" in res.output


async def test_capture_returns_what_arrived_before_the_deadline(hass, aiohttp_client, monkeypatch, socket_enabled):
    """devices/logs never ends on its own, so the window is the whole request."""
    fake = FakeDeviceBuilder(
        keep_open=True,
        replies={"devices/logs": [
            {"message_id": "1", "event": "output", "data": "[I][app:100]: boot"},
            {"message_id": "1", "event": "output", "data": "[D][sensor:093]: 21.4"},
        ]},
    )
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await esphome_builder.async_builder_capture(
        hass, url, "devices/logs", {"configuration": "a.yaml"}, capture_seconds=0.4,
    )

    # Terminators are stripped by this command, so the collector re-adds them.
    assert res.output == "[I][app:100]: boot\n[D][sensor:093]: 21.4\n"
    assert res.output_truncated is False


async def test_capture_of_a_silent_stream_is_empty_not_an_error(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(keep_open=True, replies={"devices/logs": []})
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await esphome_builder.async_builder_capture(
        hass, url, "devices/logs", {"configuration": "a.yaml"}, capture_seconds=0.3,
    )

    assert res.output == ""


async def test_capture_still_raises_on_an_error_frame(hass, aiohttp_client, monkeypatch, socket_enabled):
    """A refused stream must not look like a device that had nothing to say."""
    fake = FakeDeviceBuilder(keep_open=True, replies={
        "devices/logs": {"message_id": "1", "error_code": "not_found", "details": "no such config"},
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    with pytest.raises(BuilderCommandError) as err:
        await esphome_builder.async_builder_capture(
            hass, url, "devices/logs", {"configuration": "gone.yaml"}, capture_seconds=5,
        )

    assert err.value.error_code == "not_found"


async def test_capture_bounds_its_output(hass, aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeDeviceBuilder(keep_open=True, replies={
        "devices/logs": [
            {"message_id": "1", "event": "output", "data": "x" * 400} for _ in range(5)
        ],
    })
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await esphome_builder.async_builder_capture(
        hass, url, "devices/logs", {"configuration": "a.yaml"},
        capture_seconds=0.4, max_chars=500,
    )

    assert res.output_truncated is True
    assert len(res.output) < 1000


@pytest.mark.parametrize("command", [
    "config/set_secret",
    "config/set_wifi_credentials",
    "devices/delete",
    "devices/delete_bulk",
    "devices/archive",
    "devices/get_config",
    "devices/get_api_key",
    # firmware/clean is deliberately NOT here any more: it takes a configuration
    # and discards only that build. reset_build_env stays, and the distinction is
    # the whole point, since it takes no argument, cancels every in-flight job on
    # both lanes, and wipes the shared environment for every device.
    "firmware/reset_build_env",
    "firmware/compile_bulk",
    "firmware/install_bulk",
    "remote_build/start",
    "auth/login",
])
async def test_disallowed_commands_raise_before_any_network_io(hass, monkeypatch, command):
    def explode(_hass):
        raise AssertionError("the allowlist must be checked before any session work")

    monkeypatch.setattr(esphome_builder, "async_get_clientsession", explode)

    with pytest.raises(ValueError):
        await async_builder_command(
            hass, "http://127.0.0.1:6052", command, {}, timeout_seconds=5
        )


@pytest.mark.parametrize("command", ["firmware/install_bulk", "devices/get_api_key"])
async def test_capture_checks_the_allowlist_before_any_network_io(hass, monkeypatch, command):
    """async_builder_capture is a second entry point and gets the same boundary."""
    def explode(_hass):
        raise AssertionError("the allowlist must be checked before any session work")

    monkeypatch.setattr(esphome_builder, "async_get_clientsession", explode)

    with pytest.raises(ValueError):
        await esphome_builder.async_builder_capture(
            hass, "http://127.0.0.1:6052", command, {}, capture_seconds=1
        )


def test_allowlist_is_exactly_the_reads_plus_the_firmware_job_model():
    """Pin what this client can reach.

    The Device Builder has no permission model of its own, so widening this set
    is the entire security decision. Any addition must be a deliberate diff here.

    The firmware band was added deliberately for Tier 3: compile and install
    enqueue a job and return, and the rest poll or stop one. The bulk variants
    are excluded on purpose, because Phoenix MCP authorizes one configuration at
    a time and a bulk call would flash every device in one unscoped request.

    devices/stop_stream was added deliberately too. It reaches nothing new: it
    ends a stream on the CURRENT connection, and one connection per request means
    the only stream it can ever hit is the one this client just opened. It earns
    its place by letting a capture end politely, since yanking the socket makes
    the DEVICE log a CONNECTION_CLOSED warning on every single capture.

    firmware/clean was added deliberately as well, and its sibling
    reset_build_env is the reason the line is drawn where it is: clean takes a
    configuration and discards only that build, while reset_build_env takes no
    argument at all, cancels every in-flight job on both lanes, and wipes the
    shared environment for every device. One is scoped to what the caller was
    already authorized for; the other is not scoped to anything.

    devices/rename is here last, and only because its single caller is gated on
    cap_esphome_flash. It compiles a renamed image and OTA-flashes it, so under
    the authoring capability it would be a way to flash a device without holding
    the capability that governs flashing. The allowlist cannot express that
    condition, so the pin below is not the whole control: test_mcp_esphome_rename
    pins the capability, and both are required.
    """
    assert ESPHOME_BUILDER_ALLOWED_COMMANDS == {
        # Reads.
        "devices/validate",
        "boards/get_board",
        "boards/get_boards",
        "components/get_component_bodies",
        "components/get_components",
        "components/get_categories",
        "automations/get_available",
        "devices/logs",
        "devices/stop_stream",
        "devices/decode_backtrace",
        # Firmware job model.
        "firmware/compile",
        "firmware/install",
        "firmware/get_job",
        "firmware/get_jobs",
        "firmware/follow_job",
        "firmware/cancel",
        "firmware/clean",
        "devices/rename",
    }


async def test_capture_ends_the_stream_politely_when_the_addon_names_one(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    """Closing the socket also ends the stream, but the DEVICE then logs a
    CONNECTION_CLOSED warning on every capture, which pollutes the operator's
    own logs with a scary line for a completely normal end of a window.
    """
    fake = FakeDeviceBuilder(keep_open=True, replies={"devices/logs": [
        {"message_id": "1", "stream_id": "s-42", "event": "output", "data": "boot"},
    ]})
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await esphome_builder.async_builder_capture(
        hass, url, "devices/logs", {"configuration": "a.yaml"}, capture_seconds=0.3,
    )

    assert res.output == "boot\n"
    # The stop is best-effort and the client closes straight after sending it, so
    # let the server's task run before inspecting what it received.
    for _ in range(50):
        if any(f["command"] == "devices/stop_stream" for f in fake.received):
            break
        await asyncio.sleep(0.01)
    stops = [f for f in fake.received if f["command"] == "devices/stop_stream"]
    assert stops, f"no stop_stream sent: {fake.received}"
    assert stops[0]["args"] == {"stream_id": "s-42"}


async def test_capture_without_a_stream_id_just_closes(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    """The id's carrier is undocumented, so absence must degrade to the old
    behaviour rather than inventing one or failing the capture.
    """
    fake = FakeDeviceBuilder(keep_open=True, replies={"devices/logs": [
        {"message_id": "1", "event": "output", "data": "boot"},
    ]})
    url = await connect(fake, aiohttp_client, monkeypatch)

    res = await esphome_builder.async_builder_capture(
        hass, url, "devices/logs", {"configuration": "a.yaml"}, capture_seconds=0.3,
    )

    assert res.output == "boot\n"
    await asyncio.sleep(0.1)
    assert [f for f in fake.received if f["command"] == "devices/stop_stream"] == []


async def test_a_failed_stream_stop_never_fails_the_capture(
    hass, aiohttp_client, monkeypatch, socket_enabled
):
    """The capture already succeeded by then; tidying up must not undo it."""
    fake = FakeDeviceBuilder(keep_open=True, replies={"devices/logs": [
        {"message_id": "1", "stream_id": "s-1", "event": "output", "data": "boot"},
    ]})
    url = await connect(fake, aiohttp_client, monkeypatch)

    async def _explode(_ws, _stream_id):
        raise RuntimeError("socket already gone")

    monkeypatch.setattr(esphome_builder, "_stop_stream", _explode)
    with pytest.raises(RuntimeError):
        await esphome_builder.async_builder_capture(
            hass, url, "devices/logs", {"configuration": "a.yaml"}, capture_seconds=0.3)
