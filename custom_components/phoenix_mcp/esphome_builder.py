"""Minimal client for the ESPHome Device Builder WebSocket API.

The Device Builder add-on exposes a multiplexed WebSocket API that the ESPHome
frontend itself speaks. Phoenix MCP uses a bounded slice of it: config
validation, the board / component / automation reference data that lets an
authoring agent look facts up instead of recalling them, and the firmware job
model (compile, install, poll, cancel) alongside device console logs and
backtrace decoding.

This is Phoenix MCP's only outbound network connection. It targets the same
host and port Home Assistant's own ESPHome dashboard coordinator already talks
to, over Home Assistant's shared aiohttp session, with no credentials involved.

SECURITY: the Device Builder has no permission model of its own. Its own threat
model states that an authenticated client can run arbitrary Python at config
time and arbitrary shell through ESPHome subprocess invocations, which makes an
authenticated session equivalent to shell access on the add-on's host. So the
allowlist below is the ONLY thing bounding what this client can reach. It is
checked before any session or socket work, so no caller can turn input into an
arbitrary command, and the commands deliberately left out of it are pinned off
by test rather than merely absent. Widening the set is a security decision
rather than a maintenance one, and the pinning tests exist to force that diff
through review.

Each call is one connection: connect, read the handshake, send one command,
close. Nothing is held between requests, matching the stateless transport
everywhere else in Phoenix MCP. Firmware jobs live server-side in the add-on and
are connection-independent, so a build outlives the request that started it and
Phoenix MCP needs no job store of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ESPHOME_BUILDER_OUTPUT_MAX_CHARS,
    ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS,
    ESPHOME_BUILDER_VERIFIED_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Every command Phoenix MCP may send, in two bands.
#
# Reads: validation runs ESPHome's config check without compiling, and the rest
# return catalog or diagnostic data.
#
# Firmware: the add-on's job model. compile and install ENQUEUE and return a job
# record immediately, so a multi-minute build never runs inside a request; the
# job is polled afterwards through get_job / get_jobs / follow_job, and stopped
# with cancel. The jobs themselves are the add-on's, and survive its restart.
#
# Deliberately absent, and pinned absent by test:
#   config/set_secret, config/set_wifi_credentials  write secrets.yaml
#   devices/delete, devices/archive                 destroy, bypassing versioning
#   firmware/reset_build_env                        cancels EVERY job, then wipes
#   remote_build/*                                  pairs with external builders
#   *_bulk variants                                 one configuration at a time
#   devices/get_config                              returns raw unredacted YAML
#   devices/get_api_key                             returns the native API key
# Phoenix MCP reads device YAML itself, through its own jail and credential
# masking, so a server-side raw read would only be a way around that. Deleting is
# Phoenix MCP's own tool rather than the add-on's, so the file is snapshotted into
# version history first and a mistaken delete stays recoverable.
ESPHOME_BUILDER_ALLOWED_COMMANDS = frozenset({
    # Reads.
    "devices/validate",
    "boards/get_board",
    "boards/get_boards",
    "components/get_component_bodies",
    "components/get_components",
    "components/get_categories",
    "automations/get_available",
    "devices/logs",
    # Ends a stream this connection itself opened, and nothing else: one
    # connection per request means there is never another stream to hit.
    "devices/stop_stream",
    "devices/decode_backtrace",
    # Firmware job model.
    "firmware/compile",
    "firmware/install",
    "firmware/get_job",
    "firmware/get_jobs",
    "firmware/follow_job",
    "firmware/cancel",
    # Discards ONE configuration's build artifacts. Its sibling
    # firmware/reset_build_env stays off and the difference is categorical, not a
    # matter of degree: clean takes a configuration and leaves other jobs
    # running, while reset_build_env takes no argument at all and CANCELS every
    # in-flight job on both lanes before wiping. Note clean is still not fully
    # contained (live-observed: it also drops the shared PlatformIO cache, so
    # every device's next build slows); the caller states that cost.
    "firmware/clean",
    # Compiles a renamed image and OTA-flashes it, so it is reachable ONLY from a
    # tool gated on cap_esphome_flash. Under the authoring capability it would be
    # a way to flash a device without holding the capability that governs
    # flashing, which is the reason it stayed off until there was a caller that
    # gates correctly.
    "devices/rename",
})

# The Device Builder colorizes its process output. Live-observed: it sends the
# escape as the LITERAL five characters \033[ rather than a real 0x1b byte, so
# matching only the real byte strips nothing. Both forms are handled, since the
# literal spelling is the add-on's own encoding choice and could change back.
_ANSI_RE = re.compile(r"(?:\x1b|\\033|\\x1b|\\e)\[[0-9;]*[A-Za-z]")

_TRUNCATION_MARKER = "\n... [output truncated] ...\n"

# Correlates the one command each connection sends. The API allows many in
# flight over one socket; Phoenix MCP never needs that.
_MESSAGE_ID = "1"


class BuilderError(Exception):
    """Base class for Device Builder client failures."""


class BuilderUnavailable(BuilderError):
    """The Device Builder could not be reached or did not answer in time."""


class BuilderAuthRequired(BuilderError):
    """The Device Builder wants credentials, which Phoenix MCP does not hold."""


class BuilderCommandError(BuilderError):
    """The Device Builder refused the command."""

    def __init__(self, error_code: str, details: str | None = None) -> None:
        self.error_code = error_code
        self.details = details
        super().__init__(f"{error_code}: {details}" if details else error_code)


@dataclass(frozen=True)
class BuilderResult:
    """One command's outcome.

    Request/response commands populate `result`. Streaming commands populate
    `output` plus the `success`/`exit_code` the final frame carried.

    `server_version` is whatever the add-on announced in this connection's
    handshake. Carried so a caller that cannot make sense of a reply can name
    the version that produced it, which is the difference between "unexpected
    reply" and a diagnosis.
    """

    result: Any = None
    output: str = ""
    output_truncated: bool = False
    success: bool | None = None
    exit_code: int | None = None
    server_version: str = ""


def version_mismatch_note(command: str, server_version: str) -> str | None:
    """Explain a failure in terms of an unverified add-on version, or None.

    Returns None when the versions agree, because blaming the version for a
    genuine command failure sends the operator to upgrade something that was
    never the problem. Only ever appended to an ERROR; a version difference on
    its own is not worth saying out loud.
    """
    if not server_version or server_version == ESPHOME_BUILDER_VERIFIED_VERSION:
        return None
    return (
        f"Note: this ESPHome Device Builder reports version {server_version}, while "
        f"Phoenix MCP's ESPHome tools were verified against "
        f"{ESPHOME_BUILDER_VERIFIED_VERSION}. If '{command}' worked before, the "
        "add-on's API may have changed in an upgrade."
    )


class _OutputCollector:
    """Bounded accumulator for a streaming command's output.

    Keeps the head and the tail rather than the first N characters: a validation
    failure prints the offending line at the END, which is the whole reason the
    caller asked.
    """

    def __init__(self, max_chars: int, head_ratio: float = 0.75) -> None:
        self._max_chars = max_chars
        # head_ratio 0 keeps only the tail, which is what a caller polling a
        # build in progress wants: the newest lines say where it has got to, and
        # the banner at the top is identical on every poll.
        self._head_ratio = head_ratio
        self._chunks: list[str] = []
        self._length = 0

    def add(self, chunk: str) -> None:
        cleaned = _ANSI_RE.sub("", chunk)
        # Live-observed: the add-on sends one event per line with the terminator
        # already stripped, so concatenating verbatim collapses the whole run
        # into a single unreadable line. Re-terminate only when the chunk did not
        # bring its own, which leaves a server that does send them untouched.
        if cleaned and not cleaned.endswith("\n"):
            cleaned += "\n"
        self._chunks.append(cleaned)
        self._length += len(cleaned)

    def finish(self) -> tuple[str, bool]:
        text = "".join(self._chunks)
        if len(text) <= self._max_chars:
            return text, False
        head_len = int(self._max_chars * self._head_ratio)
        tail_len = self._max_chars - head_len
        head = text[:head_len] if head_len else ""
        return head + _TRUNCATION_MARKER + text[-tail_len:], True


def _max_chars(override: int | None) -> int:
    """Resolve an output budget, reading the constant at call time.

    Not a default argument value: that would snapshot the constant at import and
    quietly ignore anyone who adjusts it later.
    """
    return ESPHOME_BUILDER_OUTPUT_MAX_CHARS if override is None else override


def _as_frame(raw: str) -> dict[str, Any] | None:
    """Parse one text frame, ignoring anything that is not a JSON object."""
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def _raise_for_error(frame: dict[str, Any]) -> None:
    code = frame.get("error_code")
    if not isinstance(code, str):
        return
    details = frame.get("details")
    if code == "not_authenticated":
        raise BuilderAuthRequired(
            "The ESPHome Device Builder rejected the connection as unauthenticated."
        )
    raise BuilderCommandError(code, details if isinstance(details, str) else None)


async def _await_handshake(ws: aiohttp.ClientWebSocketResponse) -> str:
    """Consume frames until the server's opening ServerInfoMessage.

    Returns the announced `server_version` (empty when it is missing or an
    unexpected type). It arrives free on every connection, and keeping it is
    what lets a failure say WHICH add-on produced it.

    The server speaks first, so nothing may be sent before this returns. A
    Device Builder configured with credentials is refused here, before the
    command is sent, because Phoenix MCP holds none and sending anyway would
    only produce a less clear error.
    """
    async for msg in ws:
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise BuilderUnavailable(
                "The ESPHome Device Builder closed the connection during the handshake."
            )
        frame = _as_frame(msg.data)
        if frame is None:
            continue
        if "server_version" not in frame:
            _raise_for_error(frame)
            continue
        if frame.get("requires_auth"):
            raise BuilderAuthRequired(
                "The ESPHome Device Builder requires authentication."
            )
        version = frame.get("server_version")
        return str(version) if isinstance(version, (str, int, float)) else ""
    raise BuilderUnavailable(
        "The ESPHome Device Builder closed the connection before the handshake."
    )


async def _read_reply(
    ws: aiohttp.ClientWebSocketResponse,
    collector: _OutputCollector | None,
) -> BuilderResult:
    """Read frames for the in-flight command until it resolves."""
    async for msg in ws:
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise BuilderUnavailable(
                "The ESPHome Device Builder closed the connection mid-command."
            )
        frame = _as_frame(msg.data)
        if frame is None or frame.get("message_id") != _MESSAGE_ID:
            continue

        _raise_for_error(frame)

        event = frame.get("event")
        if event == "output":
            if collector is not None:
                data = frame.get("data")
                if isinstance(data, str):
                    collector.add(data)
            continue
        if event == "result":
            data = frame.get("data")
            payload = data if isinstance(data, dict) else {}
            output, truncated = collector.finish() if collector else ("", False)
            success = payload.get("success")
            code = payload.get("code")
            return BuilderResult(
                output=output,
                output_truncated=truncated,
                success=success if isinstance(success, bool) else None,
                exit_code=code if isinstance(code, int) else None,
            )
        if "result" in frame:
            return BuilderResult(result=frame["result"])

    raise BuilderUnavailable(
        "The ESPHome Device Builder closed the connection before answering."
    )


async def async_builder_command(
    hass: HomeAssistant,
    url: str,
    command: str,
    args: dict[str, Any],
    *,
    timeout_seconds: float,
    collect_output: bool = False,
    max_chars: int | None = None,
) -> BuilderResult:
    """Run one allowlisted Device Builder command and return its outcome.

    `url` is the dashboard coordinator's base URL; this module never resolves
    the dashboard itself, which keeps it free of any import back into the view
    layer and leaves the coordinator lookup as a single test seam there.

    Raises BuilderUnavailable when the add-on cannot be reached, BuilderAuthRequired
    when it wants credentials, and BuilderCommandError when it refuses the command.
    """
    if command not in ESPHOME_BUILDER_ALLOWED_COMMANDS:
        # Before any network work: this is the read-only boundary, not a hint.
        raise ValueError(f"Command not allowed for the Device Builder client: {command}")

    session = async_get_clientsession(hass)
    collector = _OutputCollector(_max_chars(max_chars)) if collect_output else None
    endpoint = f"{url.rstrip('/')}/ws"

    try:
        async with asyncio.timeout(timeout_seconds):
            ws = await session.ws_connect(endpoint)
            try:
                version = await _await_handshake(ws)
                await ws.send_str(
                    json.dumps({
                        "command": command,
                        "message_id": _MESSAGE_ID,
                        "args": args,
                    })
                )
                try:
                    reply = await _read_reply(ws, collector)
                except BuilderCommandError as err:
                    # A refused command is where the version earns its keep: an
                    # add-on that no longer has this command says so in its own
                    # words, which read as a Phoenix bug without this context.
                    note = version_mismatch_note(command, version)
                    if note is None:
                        raise
                    raise BuilderCommandError(
                        err.error_code, f"{err.details} {note}".strip()
                    ) from err
                return replace(reply, server_version=version)
            finally:
                await ws.close()
    except TimeoutError as err:
        raise BuilderUnavailable(
            f"The ESPHome Device Builder did not answer within {timeout_seconds:.0f}s."
        ) from err
    except (aiohttp.ClientError, OSError) as err:
        _LOGGER.debug("ESPHome Device Builder request failed: %s", err)
        raise BuilderUnavailable("The ESPHome Device Builder could not be reached.") from err


def collect_output_lines(
    lines: Any, max_chars: int | None = None, *, head_ratio: float = 0.75
) -> tuple[str, bool]:
    """Normalize output that was delivered as a list rather than as frames.

    A running job's log arrives from firmware/get_job as an in-memory list, not
    as `output` events, but it is the same text from the same producer: it
    carries the add-on's literal ANSI escapes and brings its own line
    terminators. Joining it by hand is what produced visible colour codes and a
    blank line between every line, so it goes through the one collector that
    already knows those two things instead of a second line handler.
    """
    collector = _OutputCollector(_max_chars(max_chars), head_ratio)
    for line in lines if isinstance(lines, list) else []:
        collector.add(str(line))
    return collector.finish()


async def _collect_until(
    ws: aiohttp.ClientWebSocketResponse,
    collector: _OutputCollector,
    seconds: float,
) -> str | None:
    """Read output frames until the capture window closes.

    Reaching the deadline is the normal, successful end of a capture rather than
    a failure, so that TimeoutError is swallowed here. An error frame still
    raises: a stream the add-on refused outright is not the same as a quiet
    device that simply had nothing to say.

    Returns the stream id if the add-on named one, so the caller can end the
    stream politely instead of yanking the socket.
    """
    stream_id: str | None = None
    try:
        async with asyncio.timeout(seconds):
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    return stream_id
                frame = _as_frame(msg.data)
                if frame is None or frame.get("message_id") != _MESSAGE_ID:
                    continue
                _raise_for_error(frame)
                # The id can ride on any frame and its exact carrier is not
                # documented, so take it from whichever frame offers one first.
                found = frame.get("stream_id")
                if stream_id is None and isinstance(found, (str, int)):
                    stream_id = str(found)
                if frame.get("event") == "output":
                    data = frame.get("data")
                    if isinstance(data, str):
                        collector.add(data)
    except TimeoutError:
        return stream_id
    return stream_id


async def _stop_stream(ws: aiohttp.ClientWebSocketResponse, stream_id: str | None) -> None:
    """End the stream server-side before the socket goes away.

    Closing the socket alone does end it, but the add-on tears the subprocess
    down hard and the DEVICE logs a CONNECTION_CLOSED warning every single time,
    which pollutes the operator's own device log with a scary-looking line for
    what is a completely normal end of a capture. Best-effort throughout: the
    capture already succeeded by this point, so nothing here may turn it into a
    failure.
    """
    if stream_id is None:
        return
    try:
        await ws.send_str(
            json.dumps({
                "command": "devices/stop_stream",
                "message_id": _MESSAGE_ID,
                "args": {"stream_id": stream_id},
            })
        )
    except (aiohttp.ClientError, OSError, RuntimeError):
        _LOGGER.debug("ESPHome stream stop failed; closing anyway", exc_info=True)


async def async_builder_capture(
    hass: HomeAssistant,
    url: str,
    command: str,
    args: dict[str, Any],
    *,
    capture_seconds: float,
    max_chars: int | None = None,
) -> BuilderResult:
    """Collect a bounded window of an open-ended stream, then close.

    devices/logs runs until it is killed and never sends a `result` frame, so
    there is nothing to wait for and async_builder_command would hang: the capture
    window IS the request. The window is ended with devices/stop_stream when the
    add-on named a stream id, falling back to simply closing the socket, which
    also ends it but makes the DEVICE log a CONNECTION_CLOSED warning every time.

    Returns whatever arrived before the deadline. An empty capture is a normal
    outcome (a device with nothing to log), not an error.
    """
    if command not in ESPHOME_BUILDER_ALLOWED_COMMANDS:
        # Before any network work, exactly as in async_builder_command.
        raise ValueError(f"Command not allowed for the Device Builder client: {command}")

    session = async_get_clientsession(hass)
    collector = _OutputCollector(_max_chars(max_chars))

    try:
        # The connect and handshake get the normal request budget on top of the
        # capture window, so a slow add-on cannot eat the window the caller asked
        # for. The inner deadline in _collect_until always expires first.
        async with asyncio.timeout(ESPHOME_BUILDER_REQUEST_TIMEOUT_SECONDS + capture_seconds):
            ws = await session.ws_connect(f"{url.rstrip('/')}/ws")
            try:
                version = await _await_handshake(ws)
                await ws.send_str(
                    json.dumps({
                        "command": command,
                        "message_id": _MESSAGE_ID,
                        "args": args,
                    })
                )
                try:
                    stream_id = await _collect_until(ws, collector, capture_seconds)
                except BuilderCommandError as err:
                    note = version_mismatch_note(command, version)
                    if note is None:
                        raise
                    raise BuilderCommandError(
                        err.error_code, f"{err.details} {note}".strip()
                    ) from err
                await _stop_stream(ws, stream_id)
            finally:
                await ws.close()
    except TimeoutError as err:
        raise BuilderUnavailable(
            "The ESPHome Device Builder did not start the stream in time."
        ) from err
    except (aiohttp.ClientError, OSError) as err:
        _LOGGER.debug("ESPHome Device Builder stream failed: %s", err)
        raise BuilderUnavailable("The ESPHome Device Builder could not be reached.") from err

    output, truncated = collector.finish()
    return BuilderResult(output=output, output_truncated=truncated, server_version=version)
