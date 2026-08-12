"""Tests for the ESPHome firmware tools (compile, install, job polling, diagnostics).

The WebSocket layer has its own suite; these tests fake async_builder_command at
the seam so they can concentrate on what this layer owns: the capability split
between building and flashing, job-id scoping, the in-flight guard that keeps a
retry from destroying the build it is retrying, and never claiming a device was
flashed on the strength of a finished compile.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phoenix_mcp.esphome_builder import (
    BuilderCommandError,
    BuilderResult,
    BuilderUnavailable,
)
from custom_components.phoenix_mcp.tools import esphome
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree
from tests.test_mcp_esphome_tools import (
    _ApprStore,
    _call,
    _device_info,
    _esphome_entry,
    _json,
    _runtime,
    _token,
    _yaml_token,
)

_DASHBOARD_PATCH = "custom_components.phoenix_mcp.tools.esphome._esphome_dashboard"
_COMMAND_PATCH = "custom_components.phoenix_mcp.tools.esphome.async_builder_command"
_CAPTURE_PATCH = "custom_components.phoenix_mcp.tools.esphome.async_builder_capture"

FILE = "rf-blaster1.yaml"
JOB = "abc123def456"
UPLOAD = "999888777666"

REAL_API_KEY = "REALAPIKEY123456="
REAL_OTA_PASSWORD = "realotapassword"

# Every tool here, with the argument shape it needs and the cap it gates on.
TOOLS = {
    "compile_esphome_firmware": ({"file": FILE}, "cap_esphome_yaml"),
    "clean_esphome_build": ({"file": FILE}, "cap_esphome_yaml"),
    "install_esphome_firmware": ({"file": FILE}, "cap_esphome_flash"),
    "get_esphome_job": ({"job_id": JOB}, "cap_esphome_yaml"),
    "cancel_esphome_job": ({"job_id": JOB}, "cap_esphome_yaml"),
    "get_esphome_device_logs": ({"file": FILE}, "cap_esphome_yaml"),
    "decode_esphome_backtrace": ({"file": FILE, "lines": ["Backtrace: 0x400d1234:0x3ffb"]},
                                 "cap_esphome_yaml"),
}


def _flash_token(**caps):
    """A token that may both author and flash."""
    base = {"cap_esphome_flash": "allow"}
    base.update(caps)
    return _yaml_token(**base)


def _dash():
    return SimpleNamespace(url="http://127.0.0.1:6052", data={}, last_update_success=True)


def _job(**over):
    base = {"job_id": JOB, "configuration": FILE, "status": "running", "output": []}
    base.update(over)
    return base


class FakeBuilder:
    """Scripts async_builder_command per command name and records every call made."""

    def __init__(self, **by_command):
        self.by_command = by_command
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, hass, url, command, args, **kw):
        self.calls.append((command, args))
        reply = self.by_command.get(command, {})
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(args)
        return reply if isinstance(reply, BuilderResult) else BuilderResult(result=reply)

    @property
    def commands(self) -> list[str]:
        return [c for c, _ in self.calls]

    def args_for(self, command: str) -> dict:
        return next(a for c, a in self.calls if c == command)


async def _run(tool, args, token, hass, builder, data=None):
    args = dict(args)
    if tool == "get_esphome_job":
        if "job_id" in args:
            args = {"lookup": {"kind": "job", "id": args["job_id"]}}
        elif "file" in args:
            args = {"lookup": {"kind": "file", "id": args["file"]}}
    with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
        return await _call_tool(tool, args, token, hass, data or MagicMock())


class TestGating:
    @pytest.mark.parametrize("tool", list(TOOLS))
    async def test_cap_denied(self, hass, esphome_dir, tool):
        args, cap = TOOLS[tool]
        token = _flash_token(**{cap: "deny"})
        content, outcome, _ = await _run(tool, args, token, hass, FakeBuilder())
        assert outcome == "denied"
        assert "Forbidden" in content["content"][0]["text"]

    async def test_authoring_cap_alone_does_not_grant_flashing(self, hass, esphome_dir):
        """The whole point of the split: building is not permission to flash."""
        builder = FakeBuilder()
        content, outcome, _ = await _run(
            "install_esphome_firmware", {"file": FILE},
            _yaml_token(cap_esphome_flash="deny"), hass, builder)
        assert outcome == "denied"
        assert builder.calls == []

    async def test_flash_cap_alone_does_not_grant_compiling(self, hass, esphome_dir):
        builder = FakeBuilder()
        content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE},
            _flash_token(cap_esphome_yaml="deny"), hass, builder)
        assert outcome == "denied"
        assert builder.calls == []

    @pytest.mark.parametrize("tool", list(TOOLS))
    async def test_builder_absent_refuses_before_touching_anything(self, hass, esphome_dir, tool):
        args, _cap = TOOLS[tool]
        if tool == "get_esphome_job":
            args = {"lookup": {"kind": "job", "id": args["job_id"]}}
        builder = FakeBuilder()
        with patch(_DASHBOARD_PATCH, return_value=None), patch(_COMMAND_PATCH, builder):
            content, outcome, _ = await _call_tool(tool, args, _flash_token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "Device Builder add-on is not available" in content["content"][0]["text"]
        assert builder.calls == []


class TestCompile:
    async def test_enqueues_and_returns_a_job_id(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/compile": {"job_id": JOB, "status": "queued"}})
        content, outcome, resource = await _run(
            "compile_esphome_firmware", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert resource == f"esphome:{FILE}"
        body = _json(content)
        assert body["job_id"] == JOB
        assert "get_esphome_job" in body["note"]
        assert builder.args_for("firmware/compile") == {"configuration": FILE}

    @pytest.mark.parametrize(
        ("tool", "args", "command", "reply"),
        [
            ("compile_esphome_firmware", {"file": FILE}, "firmware/compile",
             {"job_id": JOB, "status": "queued"}),
            ("clean_esphome_build", {"file": FILE}, "firmware/clean",
             {"job_id": JOB, "status": "queued"}),
        ],
    )
    async def test_every_job_starter_names_the_tool_that_shows_progress(
        self, hass, esphome_dir, tool, args, command, reply
    ):
        # LIVE-REPORTED: these notes all said "Poll get_esphome_job", the agent did
        # exactly that, and polling returns instantly so the panel rendered nothing
        # for a multi-minute build. Only wait_for_esphome_job holds the request open,
        # which is what drives the progress line, and the operator ended up asking
        # the agent to monitor by hand. An agent does what the note NAMES, so every
        # tool that starts a job has to name this one, from the shared constant.
        builder = FakeBuilder(**{command: reply, "firmware/get_jobs": []})
        content, _o, _r = await _run(tool, args, _yaml_token(), hass, builder)
        note = _json(content)["note"]

        assert "wait_for_esphome_job" in note
        assert "Poll get_esphome_job" not in note

    async def test_never_approval_gated_even_when_the_cap_is_confirm(self, hass, esphome_dir):
        """Compiling writes nothing and touches no device, so it does not queue."""
        store = _ApprStore()
        data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)
        builder = FakeBuilder(**{"firmware/compile": {"job_id": JOB, "status": "queued"}})
        _content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE},
            _yaml_token(cap_esphome_yaml="confirm"), hass, builder, data)

        assert outcome == "allowed"
        assert store._p == []

    async def test_refuses_while_a_build_for_this_file_is_running(self, hass, esphome_dir):
        """A second submission would cancel the first and reap its log."""
        builder = FakeBuilder(**{
            "firmware/get_jobs": [_job(status="running")],
        })
        content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert JOB in text and "cancel_esphome_job" in text
        assert "firmware/compile" not in builder.commands

    async def test_a_terminal_job_for_the_same_file_does_not_block(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_jobs": [_job(status="completed")],
            "firmware/compile": {"job_id": "newjob", "status": "queued"},
        })
        _content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE}, _yaml_token(), hass, builder)
        assert outcome == "allowed"

    async def test_an_unrecognized_status_does_not_wedge_the_tool(self, hass, esphome_dir):
        """An unknown status must never permanently block every future build."""
        builder = FakeBuilder(**{
            "firmware/get_jobs": [_job(status="something_new")],
            "firmware/compile": {"job_id": "newjob", "status": "queued"},
        })
        _content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE}, _yaml_token(), hass, builder)
        assert outcome == "allowed"

    async def test_a_failed_inflight_check_does_not_block(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_jobs": BuilderUnavailable("down"),
            "firmware/compile": {"job_id": JOB, "status": "queued"},
        })
        _content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE}, _yaml_token(), hass, builder)
        assert outcome == "allowed"

    async def test_read_only_scope_cannot_start_a_build(self, hass, esphome_dir, esphome_entries):
        """Creating a build for a device is a write on that device."""
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_yaml="allow", permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        builder = FakeBuilder()
        content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": FILE}, token, hass, builder)

        assert outcome == "denied"
        assert builder.calls == []

    async def test_jail_refuses_secrets_yaml(self, hass, esphome_dir):
        builder = FakeBuilder()
        _content, outcome, _ = await _run(
            "compile_esphome_firmware", {"file": "secrets.yaml"}, _yaml_token(), hass, builder)
        assert outcome == "invalid_request"
        assert builder.calls == []


class TestInstall:
    async def test_confirm_queues_an_approval_and_flashes_nothing(self, hass, esphome_dir):
        store = _ApprStore()
        data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)
        builder = FakeBuilder()
        _content, outcome, _ = await _run(
            "install_esphome_firmware", {"file": FILE},
            _flash_token(cap_esphome_flash="confirm"), hass, builder, data)

        assert outcome == "pending_approval"
        assert len(store._p) == 1
        assert store._p[0]["tool_name"] == "install_esphome_firmware"
        assert store._p[0]["cap_name"] == "cap_esphome_flash"
        assert "firmware/install" not in builder.commands

    async def test_approval_diff_names_the_risk(self, hass, esphome_dir):
        store = _ApprStore()
        data = PhoenixData(store=store, rate_limiter=MagicMock(), audit=MagicMock(), mesa=None)
        await _run("install_esphome_firmware", {"file": FILE},
                   _flash_token(cap_esphome_flash="confirm"), hass, FakeBuilder(), data)

        diff = store._p[0]["diff"]
        assert diff["kind"] == "system_action"
        assert "Flash" in diff["summary"]
        assert "reflashed over a cable" in diff["preview"]["warning"]

    async def test_executor_sends_install_over_ota(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/install": {"job_id": JOB, "status": "queued"}})
        content, outcome, _ = await _run(
            "install_esphome_firmware", {"file": FILE}, _flash_token(), hass, builder)

        assert outcome == "allowed"
        assert builder.args_for("firmware/install") == {"configuration": FILE, "port": "OTA"}
        assert "flashed" in _json(content)["note"]

    async def test_a_caller_supplied_port_is_ignored(self, hass, esphome_dir):
        """Phoenix MCP only ever flashes over the air; a serial path is not reachable."""
        builder = FakeBuilder(**{"firmware/install": {"job_id": JOB, "status": "queued"}})
        await _run("install_esphome_firmware", {"file": FILE, "port": "/dev/ttyUSB0"},
                   _flash_token(), hass, builder)
        assert builder.args_for("firmware/install")["port"] == "OTA"

    async def test_executor_rechecks_for_a_build_started_during_the_window(self, hass, esphome_dir):
        """An approval can sit for minutes; the world moves while it does."""
        from custom_components.phoenix_mcp.mcp_view import _execute_install_esphome_firmware

        builder = FakeBuilder(**{"firmware/get_jobs": [_job(status="running")]})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
            content, outcome, _ = await _execute_install_esphome_firmware(
                {"file": FILE}, _flash_token(), hass, MagicMock())

        assert outcome == "invalid_request"
        assert "firmware/install" not in builder.commands

    async def test_executor_rechecks_scope(self, hass, esphome_dir, esphome_entries):
        from custom_components.phoenix_mcp.mcp_view import _execute_install_esphome_firmware

        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_flash="allow", permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        builder = FakeBuilder()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
            _content, outcome, _ = await _execute_install_esphome_firmware(
                {"file": FILE}, token, hass, MagicMock())

        assert outcome == "denied"
        assert builder.calls == []


class TestJobScoping:
    async def test_unknown_job_id_is_not_found(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_job": None})
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": "nosuchjob"}, _yaml_token(), hass, builder)
        assert outcome == "not_found"
        # get_esphome_job also names the file alternative, since that is the
        # recovery an agent without a job id actually needs; the wording is
        # asserted in TestJobLookupByFile, and rule 12 is covered by
        # test_out_of_scope_job_is_byte_identical_to_unknown below.
        assert content["content"][0]["text"].startswith("No such firmware job.")

    async def test_out_of_scope_job_is_byte_identical_to_unknown(
        self, hass, esphome_dir, esphome_entries
    ):
        """A job id must not be an oracle for another device's build."""
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_yaml="allow", permissions=PermissionTree(
            domains={"light": PermissionNode(state="GREEN")}))

        missing, missing_outcome, _ = await _run(
            "get_esphome_job", {"job_id": "nosuchjob"}, token, hass,
            FakeBuilder(**{"firmware/get_job": None}))
        denied, denied_outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, token, hass,
            FakeBuilder(**{"firmware/get_job": _job()}))

        assert missing["content"] == denied["content"]
        assert missing_outcome == "not_found"
        assert denied_outcome == "denied"

    async def test_missing_job_id_is_invalid_request(self, hass, esphome_dir):
        builder = FakeBuilder()
        _content, outcome, _ = await _run(
            "get_esphome_job", {}, _yaml_token(), hass, builder)
        assert outcome == "invalid_request"
        assert builder.calls == []

    async def test_a_job_naming_a_path_outside_the_jail_is_not_found(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_job": _job(configuration="../../etc/passwd")})
        _content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert outcome == "not_found"


class TestJobStatus:
    async def test_running_job_reads_the_live_buffer_and_never_follows(self, hass, esphome_dir):
        """Following a job that has not finished streams until it does."""
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="running", progress=42,
                                     output=["Compiling main.cpp", "Linking"]),
        })
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["finished"] is False
        assert body["progress"] == 42
        assert "Compiling main.cpp" in body["output"]
        assert "firmware/follow_job" not in builder.commands

    async def test_running_output_is_normalized_like_the_streaming_path(self, hass, esphome_dir):
        """The two log paths must not have two line handlers.

        A running job's buffer comes back as a list from get_job rather than as
        output frames, but it is the same producer: it carries the add-on's
        LITERAL \\033[ escapes and brings its own terminators. Hand-joining it
        yields visible colour codes with a blank line between every line, while
        the finished-job path goes through the collector and is clean.
        """
        builder = FakeBuilder(**{"firmware/get_job": _job(status="running", output=[
            "\033[32mINFO ESPHome 2026.7.3\033[0m\n",
            "\033[32mINFO Compiling app...\033[0m\n",
        ])})
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        out = _json(content)["output"]
        assert out == "INFO ESPHome 2026.7.3\nINFO Compiling app...\n"
        assert "\033[" not in out and "[32m" not in out
        assert "\n\n" not in out

    async def test_a_running_poll_returns_only_the_recent_tail(self, hass, esphome_dir):
        """A running poll must not return the whole log every time.

        An agent checking a several-minute build every 30 seconds would otherwise
        pull the same near-identical kilobytes into its context a dozen times
        over. Progress is a question about the last few lines, so a running poll
        is tail-only and much tighter; the full log is worth returning once, when
        the build is done.
        """
        builder = FakeBuilder(**{"firmware/get_job": _job(
            status="running",
            output=["BANNER line\n"] + [f"step {i}\n" for i in range(4000)],
        )})
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        body = _json(content)
        assert body["output_truncated"] is True
        assert len(body["output"]) < 5000
        # Tail-only: the newest lines survive, the repeated banner does not.
        assert "step 3999" in body["output"]
        assert "BANNER line" not in body["output"]

    async def test_a_finished_poll_still_returns_the_full_log(self, hass, esphome_dir):
        """The tighter running bound must not shrink the finished artifact."""
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed", output=[]),
            "firmware/follow_job": BuilderResult(output="x" * 12_000, success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert len(_json(content)["output"]) > 10_000

    async def test_an_unrecognized_status_is_not_treated_as_finished(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_job": _job(status="reticulating")})
        _content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert "firmware/follow_job" not in builder.commands

    async def test_finished_job_replays_the_log_sidecar(self, hass, esphome_dir):
        """A terminal job's own output list is emptied, so follow is the only route."""
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed", exit_code=0, output=[]),
            "firmware/follow_job": BuilderResult(output="Compiling\nSuccess\n", success=True,
                                                 exit_code=0),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        body = _json(content)
        assert body["finished"] is True
        assert "Success" in body["output"]
        assert builder.args_for("firmware/follow_job") == {"job_id": JOB}

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    async def test_every_terminal_state_replays_its_log(self, hass, esphome_dir, status):
        """A FAILED build is the one whose log the caller actually needs.

        Every terminal state empties the record's own output list, so treating
        only "completed" as terminal would hand back an empty log for exactly the
        builds worth reading.
        """
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status=status, output=[]),
            "firmware/follow_job": BuilderResult(output="error: no such pin GPIO99\n",
                                                 success=False, exit_code=1),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert "firmware/follow_job" in builder.commands
        body = _json(content)
        assert body["finished"] is True
        assert "GPIO99" in body["output"]

    async def test_a_log_replay_failure_still_reports_the_status(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="failed", exit_code=1),
            "firmware/follow_job": BuilderUnavailable("gone"),
        })
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["status"] == "failed"
        assert body["exit_code"] == 1
        assert body["output"] == ""

    async def test_a_plain_compile_makes_no_claim_about_flashing(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="ok", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert "flashed" not in _json(content)

    async def test_a_finished_compile_is_not_a_flashed_device(self, hass, esphome_dir):
        """install returns only its compile job; the upload is a separate one."""
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="Compiled", success=True, exit_code=0),
            "firmware/get_jobs": [
                _job(status="completed"),
                {"job_id": UPLOAD, "configuration": FILE, "status": "queued",
                 "depends_on": JOB},
            ],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        body = _json(content)
        assert body["flashed"] is False
        assert body["upload"]["job_id"] == UPLOAD
        assert body["upload"]["status"] == "queued"
        assert "being flashed" in body["note"]

    async def test_flashed_only_once_the_upload_completed(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="Compiled", success=True, exit_code=0),
            "firmware/get_jobs": [
                {"job_id": UPLOAD, "configuration": FILE, "status": "completed",
                 "depends_on": JOB},
            ],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        body = _json(content)
        assert body["flashed"] is True
        assert body["upload"]["finished"] is True

    async def test_a_failed_upload_is_not_flashed(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="Compiled", success=True, exit_code=0),
            "firmware/get_jobs": [
                {"job_id": UPLOAD, "configuration": FILE, "status": "failed", "depends_on": JOB},
            ],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert _json(content)["flashed"] is False

    async def test_a_deferred_install_says_so_instead_of_claiming_success(self, hass, esphome_dir):
        """Offline device: the flash is armed for its next wake, not applied."""
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed", is_deferred_install=True),
            "firmware/follow_job": BuilderResult(output="Compiled", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        body = _json(content)
        assert body["flashed"] is False
        assert body["armed_for_next_boot"] is True
        assert "next wake" in body["note"]

    async def test_a_failed_compile_never_looks_for_an_upload(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="failed", exit_code=1),
            "firmware/follow_job": BuilderResult(output="error", success=False, exit_code=1),
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert "flashed" not in _json(content)
        assert "firmware/get_jobs" not in builder.commands


def _sequence(*replies):
    """A scripted reply that advances one step per call, holding on the last."""
    box = list(replies)

    def _next(_args):
        return box.pop(0) if len(box) > 1 else box[0]

    return _next


class TestCleanBuild:
    """The escape hatch for a build environment that is wrong in a way a rebuild
    cannot fix, which otherwise sends the operator to the add-on UI mid-task.
    """

    async def test_enqueues_a_clean_for_this_configuration_only(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_jobs": [],
            "firmware/clean": {"job_id": "c1", "status": "queued"},
        })
        content, outcome, _ = await _run(
            "clean_esphome_build", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["job_id"] == "c1"
        assert builder.args_for("firmware/clean") == {"configuration": FILE}
        # LIVE-OBSERVED and contrary to the add-on's docs: clean also drops the
        # SHARED PlatformIO cache, so it slows every device's next build, not
        # just this one. An agent told only "one slower rebuild" would reach for
        # it as a free retry, so the note has to carry the real cost.
        assert "full one" in body["note"]
        assert "every other device" in body["note"]

    async def test_refuses_while_a_build_is_running_instead_of_killing_it(
        self, hass, esphome_dir
    ):
        # The add-on's clean CANCELS an in-flight build first. Phoenix MCP refuses
        # instead, so a build is never killed as a side effect of asking for
        # something else; cancel_esphome_job is how you stop one.
        builder = FakeBuilder(**{
            "firmware/get_jobs": [_job(job_id="running-1", status="running")],
        })
        content, outcome, _ = await _run(
            "clean_esphome_build", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "running-1" in text
        assert "cancel_esphome_job" in text
        assert "firmware/clean" not in builder.commands

    async def test_builder_absent_refuses_without_calling_out(self, hass, esphome_dir):
        builder = FakeBuilder()
        with patch(_DASHBOARD_PATCH, return_value=None), patch(_COMMAND_PATCH, builder):
            _c, outcome, _ = await _call_tool(
                "clean_esphome_build", {"file": FILE}, _yaml_token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert builder.calls == []

    async def test_an_unknown_file_never_reaches_the_addon(self, hass, esphome_dir):
        builder = FakeBuilder()
        _c, outcome, _ = await _run(
            "clean_esphome_build", {"file": "nope.yaml"}, _yaml_token(), hass, builder)
        assert outcome == "not_found"
        assert builder.calls == []


class TestJobLookupByFile:
    """A job id only exists in the conversation that started the build.

    On voice and Assist that conversation is exactly the one that does not
    survive, so an agent asked "is that build done?" a few minutes later has no
    id and nothing reliable to fall back on: a returned wait call is not proof
    of completion, since on a headless surface the wait returns early by design.
    """

    def _jobs(self, *rows):
        return FakeBuilder(**{
            "firmware/get_jobs": list(rows),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
        })

    async def test_reports_the_newest_job_for_a_file(self, hass, esphome_dir):
        old = _job(job_id="old-1", status="completed")
        old["created_at"] = "2026-07-29T09:00:00+00:00"
        new = _job(job_id="new-1", status="running")
        new["created_at"] = "2026-07-29T10:00:00+00:00"
        # Deliberately out of order: the reply's ordering is undocumented.
        builder = self._jobs(old, new)
        builder.by_command["firmware/get_job"] = new

        content, outcome, _ = await _run(
            "get_esphome_job", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert _json(content)["job_id"] == "new-1"

    async def test_a_file_with_no_jobs_says_so_plainly(self, hass, esphome_dir):
        builder = self._jobs()
        content, outcome, _ = await _run(
            "get_esphome_job", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "not_found"
        assert "No firmware job" in content["content"][0]["text"]

    async def test_an_unknown_file_never_reaches_the_addon(self, hass, esphome_dir):
        builder = self._jobs()
        _c, outcome, _ = await _run(
            "get_esphome_job", {"file": "nope.yaml"}, _yaml_token(), hass, builder)
        assert outcome == "not_found"
        assert builder.calls == []

    async def test_the_jail_still_applies_to_a_file_lookup(self, hass, esphome_dir):
        # The file argument must not become a second, laxer way into the tree.
        builder = self._jobs()
        for bad in ("secrets.yaml", "archive/old-device.yaml", "../configuration.yaml"):
            _c, outcome, _ = await _run(
                "get_esphome_job", {"file": bad}, _yaml_token(), hass, builder)
            assert outcome == "invalid_request", bad
        assert builder.calls == []

    async def test_cap_denied_before_any_lookup(self, hass, esphome_dir):
        builder = self._jobs()
        _c, outcome, _ = await _run(
            "get_esphome_job", {"file": FILE}, _yaml_token(cap_esphome_yaml="deny"), hass, builder)
        assert outcome == "denied"
        assert builder.calls == []

    async def test_neither_argument_is_refused(self, hass, esphome_dir):
        builder = self._jobs()
        content, outcome, _ = await _run(
            "get_esphome_job", {}, _yaml_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "lookup must contain kind job or file" in content["content"][0]["text"]

    async def test_an_explicit_job_id_still_wins(self, hass, esphome_dir):
        # Passing both must not silently prefer the newest job for the file.
        builder = FakeBuilder(**{
            "firmware/get_job": _job(job_id=JOB, status="completed"),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB, "file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert _json(content)["job_id"] == JOB

    async def test_an_unknown_job_id_names_the_file_alternative(self, hass, esphome_dir):
        # LIVE-OBSERVED: an agent that had lost its job id guessed one, got a
        # bare "No such firmware job", and spent two more calls discovering it
        # could ask by file. Naming the fix in the refusal is the same lesson as
        # the service-name hints.
        builder = FakeBuilder(**{"firmware/get_job": None})
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": "nope"}, _yaml_token(), hass, builder)

        assert outcome == "not_found"
        text = content["content"][0]["text"]
        assert "No such firmware job." in text
        assert "pass file instead" in text

    async def test_cancel_does_not_name_a_recovery_it_cannot_offer(self, hass, esphome_dir):
        # cancel takes an id only, so pointing it at `file` would send the caller
        # round another loop.
        builder = FakeBuilder(**{"firmware/get_job": None})
        content, outcome, _ = await _run(
            "cancel_esphome_job", {"job_id": "nope"}, _yaml_token(), hass, builder)

        assert outcome == "not_found"
        text = content["content"][0]["text"]
        assert "No such firmware job." in text
        assert "file" not in text

    async def test_cancel_never_accepts_a_file(self, hass, esphome_dir):
        # Stopping "whichever job is newest" is not something a caller can have
        # meant to name, so cancel keeps requiring an explicit id.
        builder = self._jobs(_job(job_id="new-1", status="running"))
        content, outcome, _ = await _run(
            "cancel_esphome_job", {"file": FILE}, _yaml_token(), hass, builder)

        assert outcome == "invalid_request"
        assert "job_id is required" in content["content"][0]["text"]
        assert builder.calls == []


class TestChainAcrossConfigurations:
    """The two jobs of a chain do not always share a configuration.

    On a rename the add-on renames the file up front, so the compile job already
    carries the NEW filename while the flash job queued behind it still carries
    the old one. A dependent lookup filtered by configuration finds nothing, and
    the rename appears to finish at the compile with no flash reported.
    """

    async def test_the_dependent_job_is_found_even_under_another_configuration(
        self, hass, esphome_dir
    ):
        tail = _job(job_id="tail-1", status="completed")
        tail["configuration"] = "old-name.yaml"
        tail["depends_on"] = JOB

        def jobs(args):
            # The filtered query is the one that used to be the only query.
            return [] if args.get("configuration") else [tail]

        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
            "firmware/get_jobs": jobs,
        })
        content, outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["upload"]["job_id"] == "tail-1"
        assert body["flashed"] is True

    async def test_the_filtered_query_is_still_tried_first(self, hass, esphome_dir):
        # The install case must keep its cheap, well-scoped lookup.
        tail = _job(job_id="tail-1", status="completed")
        tail["depends_on"] = JOB
        seen: list[dict] = []

        def jobs(args):
            seen.append(args)
            return [tail]

        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
            "firmware/get_jobs": jobs,
        })
        await _run("get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert seen and seen[0] == {"configuration": FILE}
        # Found on the first, scoped query: no unfiltered fleet-wide call at all.
        assert all(a.get("configuration") for a in seen)


class TestHeadlessWaitClamp:
    """A voice or Assist turn cannot hold a request open for a build.

    Those surfaces have no stream to write progress to and their own pipeline
    timeout is far under this tool's, so a full-length hold there is silence
    followed by a timeout while the build carries on invisibly regardless.
    """

    @pytest.mark.parametrize("client_ip", ["assist", "voice", "ai_task"])
    def test_headless_surfaces_get_the_short_wait(self, client_ip):
        assert esphome._clamp_job_wait(300, client_ip) == (
            esphome.ESPHOME_BUILDER_JOB_WAIT_HEADLESS_SECONDS)

    @pytest.mark.parametrize("client_ip", ["agentcli", None, "192.168.1.50"])
    def test_everything_else_keeps_the_full_wait(self, client_ip):
        # Agent Chat owns both ends of its own stream and renders the progress
        # line, and a real MCP client asked for a long call by calling this tool.
        assert esphome._clamp_job_wait(300, client_ip) == (
            esphome.ESPHOME_BUILDER_JOB_WAIT_SECONDS)

    async def test_a_voice_turn_returns_early_and_says_what_to_do_next(
        self, hass, esphome_dir, monkeypatch
    ):
        # The WIRING, not the clamp: the tool has to receive client_ip from the
        # dispatcher at all. Clamp-only tests stay green if that thread is cut.
        monkeypatch.setattr(esphome, "ESPHOME_BUILDER_JOB_POLL_SECONDS", 0.001)
        monkeypatch.setattr(esphome, "ESPHOME_BUILDER_JOB_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(esphome, "ESPHOME_BUILDER_JOB_WAIT_HEADLESS_SECONDS", 0.01)

        def _running(**_kw):
            return FakeBuilder(**{
                "firmware/get_job": _job(status="running"),
                "firmware/get_jobs": [],
            })

        voice = _running()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, voice):
            content, outcome, _ = await _call_tool(
                "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass,
                MagicMock(), "", "voice")

        assert outcome == "allowed"
        body = _json(content)
        assert body["finished"] is False
        # LIVE-FOUND wording failure: "ask them to check back, or call
        # get_esphome_job on the next turn" read as permission to poll NOW, and
        # the model spent all 20 of its rounds doing exactly that, ending the
        # turn with nothing to say. The note has to end the TURN, and say why.
        note = body["note"]
        assert "STOP CALLING TOOLS NOW" in note
        assert "not poll this job again in this turn" in note.replace("Do NOT", "not")
        assert "limited number of tool calls per turn" in note
        # LIVE-FOUND separately: a later turn read "the wait call returned" as
        # "the build finished". On this surface the wait ALWAYS returns early, so
        # the reply has to deny that inference outright.
        assert "HAS NOT FINISHED" in note
        # And it must point at the recovery that needs no remembered state.
        assert "with the file name" in note

        normal = _running()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, normal):
            content2, _o, _r = await _call_tool(
                "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass,
                MagicMock(), "", "192.168.1.50")

        # The ordinary caller waited materially longer, and gets no such note.
        assert normal.commands.count("firmware/get_job") > voice.commands.count("firmware/get_job")
        assert "note" not in _json(content2)

    async def test_get_esphome_job_carries_the_same_steer_on_a_headless_turn(
        self, hass, esphome_dir
    ):
        # Steering only wait_for_esphome_job moved the loop next door: told not
        # to loop on the wait tool, the model obeyed literally and polled THIS
        # one 18 times at two-second intervals instead.
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="running"),
            "firmware/get_jobs": [],
        })
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
            content, outcome, _ = await _call_tool(
                "get_esphome_job", {"lookup": {"kind": "job", "id": JOB}}, _yaml_token(), hass,
                MagicMock(), "", "voice")

        assert outcome == "allowed"
        assert "STOP CALLING TOOLS NOW" in _json(content)["note"]

    async def test_a_finished_job_gets_no_stop_note_even_headless(self, hass, esphome_dir):
        # The steer is about waiting. Once the job is done there is nothing to
        # wait for, and telling an agent to stop would strand the actual answer.
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
            content, _o, _r = await _call_tool(
                "get_esphome_job", {"lookup": {"kind": "job", "id": JOB}}, _yaml_token(), hass,
                MagicMock(), "", "voice")

        body = _json(content)
        assert body["finished"] is True
        assert "STOP CALLING TOOLS NOW" not in str(body.get("note", ""))

    async def test_an_ordinary_client_polling_gets_no_steer(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="running"),
            "firmware/get_jobs": [],
        })
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
            content, _o, _r = await _call_tool(
                "get_esphome_job", {"lookup": {"kind": "job", "id": JOB}}, _yaml_token(), hass,
                MagicMock(), "", "192.168.1.50")
        assert "note" not in _json(content)


class TestWaiting:
    """wait_for_esphome_job: the only place a build reports anything mid-flight.

    compile and install return immediately by design, so without a held request
    there is no in-flight call for progress notifications to ride on.
    """

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        monkeypatch.setattr(esphome, "ESPHOME_BUILDER_JOB_POLL_SECONDS", 0.001)

    @pytest.fixture
    def progress(self, monkeypatch):
        seen: list[str | None] = []
        # Patch the name in the module that CALLS it. tools.esphome imports
        # _set_progress_status from tool_common into its own namespace, so
        # patching mcp_view's binding leaves the real one running and the
        # "no progress was emitted" assertions pass without testing anything.
        monkeypatch.setattr(
            esphome, "_set_progress_status",
            lambda status, total=None, **_metadata: seen.append(status),
        )
        return seen

    async def test_cap_denied(self, hass, esphome_dir):
        builder = FakeBuilder()
        _c, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB},
            _yaml_token(cap_esphome_yaml="deny"), hass, builder)
        assert outcome == "denied"
        assert builder.calls == []

    async def test_an_already_finished_job_returns_at_once(self, hass, esphome_dir, progress):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output="done", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert _json(content)["finished"] is True
        assert [p for p in progress if p] == []

    async def test_waits_until_the_build_finishes_and_reports_percentages(
        self, hass, esphome_dir, progress
    ):
        builder = FakeBuilder(**{
            "firmware/get_job": _sequence(
                _job(status="running", progress=20),
                _job(status="running", progress=60),
                _job(status="completed", exit_code=0),
            ),
            "firmware/follow_job": BuilderResult(output="ok", success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert _json(content)["finished"] is True
        assert f"Compiling {FILE}: 20%" in progress

    async def test_follows_the_chain_into_the_upload(self, hass, esphome_dir, progress):
        """One call covers compile then flash, so returning means actually flashed."""
        upload_running = {"job_id": UPLOAD, "configuration": FILE, "status": "running",
                          "depends_on": JOB, "progress": 40}
        upload_done = {**upload_running, "status": "completed", "progress": 100}
        compile_done = _job(status="completed", exit_code=0)

        calls = {"n": 0}

        def _get_job(args):
            if args["job_id"] == UPLOAD:
                calls["n"] += 1
                return upload_running if calls["n"] < 2 else upload_done
            return compile_done

        builder = FakeBuilder(**{
            "firmware/get_job": _get_job,
            "firmware/get_jobs": _sequence([upload_running], [upload_done]),
            "firmware/follow_job": BuilderResult(output="ok", success=True, exit_code=0),
        })
        content, _outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        # The phase label switches, which is what a user reads.
        assert any(p and p.startswith("Flashing") for p in progress), progress
        body = _json(content)
        # Answers about the job the caller named, not the upload it followed.
        assert body["job_id"] == JOB
        assert body["flashed"] is True

    async def test_timeout_returns_the_unfinished_job(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_job": _job(status="running", progress=10)})
        content, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB, "timeout": 1}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["finished"] is False
        assert body["status"] == "running"

    async def test_timeout_is_clamped_to_the_ceiling(self, hass, esphome_dir):
        assert esphome._clamp_job_wait(99999) == 300
        assert esphome._clamp_job_wait(0) == 300
        assert esphome._clamp_job_wait("nonsense") == 300
        assert esphome._clamp_job_wait(30) == 30

    async def test_the_percentage_never_rides_in_the_protocol_progress_field(self):
        """MCP requires the numeric progress to increase on every notification.

        The add-on's percentage does not qualify: it resets at the compile-to-upload
        seam and was seen reading 27 on an already-completed job. It belongs in the
        human-readable message, with elapsed seconds carrying the monotonic promise.
        """
        msg = esphome._esphome_progress_message({"progress": 42}, FILE, "Compiling")
        assert msg == f"Compiling {FILE}: 42%"
        # No usable percentage degrades to the phase alone rather than inventing one.
        assert esphome._esphome_progress_message({}, FILE, "Flashing") == f"Flashing {FILE}"
        assert esphome._esphome_progress_message(
            {"progress": None}, FILE, "Flashing") == f"Flashing {FILE}"

    async def test_a_job_vanishing_mid_wait_does_not_hang(self, hass, esphome_dir):
        """A re-submission evicts the previous record; do not spin on a ghost."""
        builder = FakeBuilder(**{
            "firmware/get_job": _sequence(_job(status="running"), None),
        })
        _content, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert outcome == "allowed"

    async def test_scoping_applies(self, hass, esphome_dir, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_yaml="allow", permissions=PermissionTree(
            domains={"light": PermissionNode(state="GREEN")}))
        builder = FakeBuilder(**{"firmware/get_job": _job()})
        content, outcome, _ = await _run(
            "wait_for_esphome_job", {"job_id": JOB}, token, hass, builder)
        assert outcome == "denied"
        assert content["content"][0]["text"] == "No such firmware job."


class TestCancel:
    async def test_cancels_a_running_job(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_job": _job(status="running")})
        content, outcome, _ = await _run(
            "cancel_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        assert _json(content)["cancelled"] is True
        assert builder.args_for("firmware/cancel") == {"job_id": JOB}

    async def test_an_already_finished_job_is_not_an_error(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="running"),
            "firmware/cancel": BuilderCommandError("not_found"),
        })
        content, outcome, _ = await _run(
            "cancel_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["cancelled"] is False
        assert "already finished" in body["message"]

    async def test_scoping_applies(self, hass, esphome_dir, esphome_entries):
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_yaml="allow", permissions=PermissionTree(
            domains={"light": PermissionNode(state="GREEN")}))
        builder = FakeBuilder(**{"firmware/get_job": _job()})
        _content, outcome, _ = await _run(
            "cancel_esphome_job", {"job_id": JOB}, token, hass, builder)

        assert outcome == "denied"
        assert "firmware/cancel" not in builder.commands


class TestDeviceLogs:
    async def _capture(self, hass, token, args, result):
        capture = AsyncMock(return_value=result)
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_CAPTURE_PATCH, capture):
            content, outcome, resource = await _call(
                "get_esphome_device_logs", args, token, hass)
        return content, outcome, resource, capture

    async def test_captures_and_reports_the_window(self, hass, esphome_dir):
        content, outcome, resource, capture = await self._capture(
            hass, _yaml_token(), {"file": FILE, "seconds": 5},
            BuilderResult(output="[I][app:100]: boot\n"))

        assert outcome == "allowed"
        assert resource == f"esphome:{FILE}"
        body = _json(content)
        assert body["captured_seconds"] == 5
        assert "boot" in body["output"]
        assert capture.await_args.kwargs["capture_seconds"] == 5
        assert capture.await_args.args[3] == {"configuration": FILE}

    async def test_window_is_clamped(self, hass, esphome_dir):
        _c, _o, _r, capture = await self._capture(
            hass, _yaml_token(), {"file": FILE, "seconds": 9999}, BuilderResult(output=""))
        assert capture.await_args.kwargs["capture_seconds"] == 60

    async def test_nonsense_window_falls_back_to_the_default(self, hass, esphome_dir):
        _c, _o, _r, capture = await self._capture(
            hass, _yaml_token(), {"file": FILE, "seconds": 0}, BuilderResult(output=""))
        assert capture.await_args.kwargs["capture_seconds"] == 15

    async def test_a_silent_device_says_so(self, hass, esphome_dir):
        content, outcome, _r, _c = await self._capture(
            hass, _yaml_token(), {"file": FILE}, BuilderResult(output=""))
        assert outcome == "allowed"
        assert "logged nothing" in _json(content)["message"]

    async def test_read_scope_is_enough(self, hass, esphome_dir, esphome_entries):
        """Reading a console observes rather than changes, so READ is the bar."""
        _esphome_entry(hass, esphome_entries, runtime=_runtime())
        token = _token(cap_esphome_yaml="allow", permissions=PermissionTree(
            domains={"sensor": PermissionNode(state="YELLOW")}))
        _content, outcome, _r, _c = await self._capture(
            hass, token, {"file": FILE}, BuilderResult(output="line"))
        assert outcome == "allowed"


class TestBacktrace:
    async def test_decodes(self, hass, esphome_dir):
        builder = FakeBuilder(**{"devices/decode_backtrace": {
            "decoded": [{"index": 0, "text": "loop() at main.cpp:42"}],
            "stale_build": False,
            "unavailable_reason": "",
        }})
        content, outcome, _ = await _run(
            "decode_esphome_backtrace", {"file": FILE, "lines": ["Backtrace: 0x400d"]},
            _yaml_token(), hass, builder)

        assert outcome == "allowed"
        body = _json(content)
        assert body["decoded"][0]["text"] == "loop() at main.cpp:42"
        assert body["stale_build"] is False
        # Empty on success, so it is not worth putting in front of an agent.
        assert "unavailable_reason" not in body

    async def test_reports_a_real_unavailable_reason(self, hass, esphome_dir):
        builder = FakeBuilder(**{"devices/decode_backtrace": {
            "decoded": [], "stale_build": False, "unavailable_reason": "no_build",
        }})
        content, _outcome, _ = await _run(
            "decode_esphome_backtrace", {"file": FILE, "lines": ["Backtrace: 0x400d"]},
            _yaml_token(), hass, builder)
        assert _json(content)["unavailable_reason"] == "no_build"

    async def test_empty_lines_refused(self, hass, esphome_dir):
        builder = FakeBuilder()
        _content, outcome, _ = await _run(
            "decode_esphome_backtrace", {"file": FILE, "lines": []}, _yaml_token(), hass, builder)
        assert outcome == "invalid_request"
        assert builder.calls == []

    async def test_too_many_lines_refused(self, hass, esphome_dir):
        builder = FakeBuilder()
        content, outcome, _ = await _run(
            "decode_esphome_backtrace", {"file": FILE, "lines": ["x"] * 201},
            _yaml_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "200" in content["content"][0]["text"]
        assert builder.calls == []


class TestScrubbing:
    """A build log quotes far more of the config than a validation error does."""

    async def test_job_output_is_scrubbed(self, hass, esphome_dir):
        leaky = f"Reading configuration\n  key: {REAL_API_KEY}\n  password: {REAL_OTA_PASSWORD}\n"
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="completed"),
            "firmware/follow_job": BuilderResult(output=leaky, success=True, exit_code=0),
            "firmware/get_jobs": [],
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)

        blob = json.dumps(content)
        assert REAL_API_KEY not in blob
        assert REAL_OTA_PASSWORD not in blob
        assert "<redacted>" in _json(content)["output"]

    async def test_running_job_output_is_scrubbed_too(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_job": _job(status="running", output=[f"key: {REAL_API_KEY}"]),
        })
        content, _outcome, _ = await _run(
            "get_esphome_job", {"job_id": JOB}, _yaml_token(), hass, builder)
        assert REAL_API_KEY not in json.dumps(content)

    async def test_device_logs_are_scrubbed(self, hass, esphome_dir):
        capture = AsyncMock(return_value=BuilderResult(output=f"[C][ota]: {REAL_OTA_PASSWORD}\n"))
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_CAPTURE_PATCH, capture):
            content, _outcome, _ = await _call(
                "get_esphome_device_logs", {"file": FILE}, _yaml_token(), hass)
        assert REAL_OTA_PASSWORD not in json.dumps(content)

    async def test_backtrace_output_is_scrubbed(self, hass, esphome_dir):
        builder = FakeBuilder(**{"devices/decode_backtrace": {
            "decoded": [{"index": 0, "text": f"at ota.cpp with {REAL_OTA_PASSWORD}"}],
            "stale_build": False, "unavailable_reason": "",
        }})
        content, _outcome, _ = await _run(
            "decode_esphome_backtrace", {"file": FILE, "lines": ["Backtrace: 0x400d"]},
            _yaml_token(), hass, builder)
        assert REAL_OTA_PASSWORD not in json.dumps(content)


class TestVersionJumpNotice:
    """An over-the-air update rewrites only the app, so a version change leaves a
    new app on the bootloader the device was originally cabled with. The card
    showed both version numbers without ever saying they differed, which is the
    one fact that makes that legible.

    The notice deliberately does not predict an unbootable device: a version
    jump over the air completes cleanly in practice, and a warning whose
    consequence never arrives trains the operator to click past it, which is
    the cry-wolf failure the patch-bump exclusion below exists to prevent.
    """

    @pytest.mark.parametrize("installed,target,expected", [
        ("2026.6.2", "2026.7.3", "2026.6.2 to 2026.7.3"),
        ("2026.6.2", "2027.1.0", "2026.6.2 to 2027.1.0"),
        # A patch bump is routine and must NOT cry wolf, or the notice gets ignored.
        ("2026.7.1", "2026.7.3", None),
        ("2026.7.3", "2026.7.3", None),
        # Never raises on junk: this runs pre-gate and must not break the card.
        (None, "2026.7.3", None),
        ("2026.7.3", None, None),
        ("nonsense", "2026.7.3", None),
        ("2026", "2026.7.3", None),
    ])
    def test_only_a_real_version_change_is_flagged(self, installed, target, expected):
        assert esphome._esphome_version_jump(installed, target) == expected

    async def test_the_notice_reaches_the_summary_and_the_preview(
        self, hass, esphome_dir, esphome_entries
    ):
        from unittest.mock import patch as _patch

        _esphome_entry(hass, esphome_entries,
                       runtime=_runtime(_device_info(esphome_version="2026.6.2")))
        dash = SimpleNamespace(url="http://127.0.0.1:6052", last_update_success=True,
                               data={"rf-blaster1": {"current_version": "2026.7.3"}})
        with _patch(_DASHBOARD_PATCH, return_value=dash):
            diff = esphome._build_diff_install_esphome_firmware(
                {"file": FILE}, _flash_token(), hass, MagicMock())

        # The summary is the bold line and the History-visible marker.
        assert "ESPHome version change: 2026.6.2 to 2026.7.3" in diff["summary"]
        assert diff["preview"]["version_change"] == "2026.6.2 to 2026.7.3"
        # Pin the CLAIM, not just any prose: the risk is the bootloader gap the
        # operator can confirm in the device's own boot log, never a predicted brick.
        assert "bootloader" in diff["preview"]["version_change_risk"]
        assert "over-the-air" in diff["preview"]["version_change_risk"]
        assert "will not boot" not in diff["preview"]["version_change_risk"]

    async def test_no_notice_when_the_versions_match(self, hass, esphome_dir, esphome_entries):
        from unittest.mock import patch as _patch

        _esphome_entry(hass, esphome_entries,
                       runtime=_runtime(_device_info(esphome_version="2026.7.3")))
        dash = SimpleNamespace(url="http://127.0.0.1:6052", last_update_success=True,
                               data={"rf-blaster1": {"current_version": "2026.7.3"}})
        with _patch(_DASHBOARD_PATCH, return_value=dash):
            diff = esphome._build_diff_install_esphome_firmware(
                {"file": FILE}, _flash_token(), hass, MagicMock())

        assert "version change" not in diff["summary"]
        assert "version_change" not in diff["preview"]

    async def test_an_unresolvable_device_still_builds_a_card(self, hass, esphome_dir):
        diff = esphome._build_diff_install_esphome_firmware(
            {"file": FILE}, _flash_token(), hass, MagicMock())
        assert diff["kind"] == "system_action"
        assert "reflashed over a cable" in diff["preview"]["warning"]
