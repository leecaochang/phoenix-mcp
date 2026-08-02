"""Tests for rename_esphome_device.

The add-on's devices/rename compiles a renamed image and OTA-flashes it. That is
why this tool exists at all rather than the command being allowlisted for the
authoring capability: under cap_esphome_yaml it would be a way to flash a device
without holding the capability that governs flashing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from custom_components.phoenix_mcp import mcp_view
from custom_components.phoenix_mcp.tools import esphome
from custom_components.phoenix_mcp.esphome_builder import ESPHOME_BUILDER_ALLOWED_COMMANDS
from custom_components.phoenix_mcp.mcp_view import _call_tool

from tests.test_mcp_esphome_firmware_tools import (
    FILE,
    FakeBuilder,
    _dash,
    _esphome_entry,
    _flash_token,
    _job,
    _runtime,
    _yaml_token,
)

_DASHBOARD_PATCH = "custom_components.phoenix_mcp.tools.esphome._esphome_dashboard"
_COMMAND_PATCH = "custom_components.phoenix_mcp.tools.esphome.async_builder_command"

RENAMED = {"configuration": FILE, "job": "j-compile", "tail_job": "j-upload"}


def _json_body(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _run(args, token, hass, builder, data=None):
    with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, builder):
        return await _call_tool(
            "rename_esphome_device", args, token, hass, data or MagicMock())


class TestCapability:
    async def test_the_authoring_capability_alone_cannot_rename(self, hass, esphome_dir):
        # THE test for this tool. devices/rename flashes, so a token that may
        # author but is denied flashing must not reach it.
        builder = FakeBuilder(**{"devices/rename": RENAMED})
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"},
            _yaml_token(cap_esphome_flash="deny"), hass, builder)

        assert outcome == "denied"
        assert builder.calls == []
        assert "new-name" not in content["content"][0]["text"]

    async def test_the_flash_capability_may_rename(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_jobs": [],
            "devices/rename": RENAMED,
        })
        _c, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)
        assert outcome == "allowed"

    def test_the_command_is_reachable_but_only_through_this_tool(self):
        # Pinned in both directions: the allowlist carries it (so the tool works)
        # and the tool carries the flash capability (so the allowlist entry is
        # not a way around the capability split).
        assert "devices/rename" in ESPHOME_BUILDER_ALLOWED_COMMANDS
        every = (mcp_view._ENTITY_TOOL_DEFS + mcp_view._SYSTEM_TOOL_DEFS
                 + mcp_view._NATIVE_TOOL_DEFS)
        defs = [d for d in every if d["name"] == "rename_esphome_device"]
        assert defs and defs[0]["cap"] == "cap_esphome_flash"


class TestValidation:
    @pytest.mark.parametrize("bad", [
        "Has-Capitals", "under_scores", "-leading", "trailing-", "", "a" * 40, 7, None,
        "spaces here", "dots.in.name",
    ])
    async def test_an_invalid_name_is_refused_before_anything_is_enqueued(
        self, hass, esphome_dir, bad
    ):
        builder = FakeBuilder(**{"firmware/get_jobs": []})
        _c, outcome, _ = await _run(
            {"file": FILE, "new_name": bad}, _flash_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "devices/rename" not in builder.commands

    async def test_the_current_name_comes_from_the_yaml_not_the_filename(
        self, hass, esphome_dir
    ):
        # A config's esphome name and its filename stem diverge routinely (an
        # imported config keeps its raw name while the file is slugified; an
        # adopted device gets renamed later). Live example on a real box:
        # esphome-web-31fca4.yaml holding doorbell-31fca4. Comparing the rename
        # target against the STEM refused that device's legitimate rename AND
        # blamed a name it does not have, which is the worse half.
        (esphome_dir / "imported.yaml").write_text(
            "esphome:\n  name: real-device-name\n")
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})

        content, outcome, _ = await _run(
            {"file": "imported.yaml", "new_name": "real-device-name"},
            _flash_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "already has that name" in content["content"][0]["text"]
        assert builder.calls == []

        # ...and renaming it to match its own FILENAME is a real rename, which
        # the stem compare used to refuse.
        content, outcome, _ = await _run(
            {"file": "imported.yaml", "new_name": "imported"}, _flash_token(), hass, builder)
        assert outcome == "allowed"

    async def test_a_substituted_name_is_resolved(self, hass, esphome_dir):
        # The generated bluetooth-proxy configs all carry esphome.name: ${name},
        # so an unresolved read would compare against the literal "${name}" and
        # never match, silently disabling the same-name refusal for exactly the
        # configs Home Assistant creates for you.
        (esphome_dir / "proxy.yaml").write_text(
            "substitutions:\n  name: bt-proxy-aabbcc\nesphome:\n  name: ${name}\n")
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})

        content, outcome, _ = await _run(
            {"file": "proxy.yaml", "new_name": "bt-proxy-aabbcc"},
            _flash_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "already has that name" in content["content"][0]["text"]
        assert builder.calls == []

    async def test_an_in_place_rename_is_allowed_not_refused_as_a_collision(
        self, hass, esphome_dir
    ):
        # new_name slugifies to this device's OWN file, so only esphome.name
        # changes. The add-on supports this and forces its config-only path
        # (the OTA chain needs a distinct filename to build against), so the
        # existing-file check must not fire on the file being renamed.
        (esphome_dir / "imported.yaml").write_text(
            "esphome:\n  name: real-device-name\n")
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})

        content, outcome, _ = await _run(
            {"file": "imported.yaml", "new_name": "imported"}, _flash_token(), hass, builder)

        assert outcome == "allowed"
        assert "already exists" not in content["content"][0]["text"]
        assert builder.args_for("devices/rename")["new_name"] == "imported"

    async def test_an_unreadable_file_falls_back_to_the_filename_stem(
        self, hass, esphome_dir
    ):
        # Fails soft, like the add-on: a config that will not parse must not
        # become un-renameable, and the stem is the same fallback upstream uses.
        (esphome_dir / "broken.yaml").write_text("esphome: [oops\n  name: ??\n")
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})

        content, outcome, _ = await _run(
            {"file": "broken.yaml", "new_name": "broken"}, _flash_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "already has that name" in content["content"][0]["text"]

    async def test_renaming_to_the_current_name_is_refused(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_jobs": []})
        _c, outcome, _ = await _run(
            {"file": FILE, "new_name": FILE.removesuffix(".yaml")}, _flash_token(), hass, builder)
        assert outcome == "invalid_request"
        assert "devices/rename" not in builder.commands

    async def test_an_existing_target_file_is_never_overwritten(self, hass, esphome_dir):
        # The add-on writes the renamed YAML up front, so landing on an existing
        # file would silently clobber another device's configuration.
        (esphome_dir / "taken.yaml").write_text("esphome:\n  name: taken\n")
        builder = FakeBuilder(**{"firmware/get_jobs": []})
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "taken"}, _flash_token(), hass, builder)

        assert outcome == "invalid_request"
        assert "already exists" in content["content"][0]["text"]
        assert "devices/rename" not in builder.commands
        assert (esphome_dir / "taken.yaml").read_text() == "esphome:\n  name: taken\n"

    async def test_a_running_build_is_never_destroyed_by_a_rename(self, hass, esphome_dir):
        builder = FakeBuilder(**{
            "firmware/get_jobs": [_job(job_id="running-1", status="running")],
        })
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)

        assert outcome == "invalid_request"
        assert "running-1" in content["content"][0]["text"]
        assert "devices/rename" not in builder.commands


class TestResult:
    async def test_reports_both_jobs_of_the_chain(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)

        assert outcome == "allowed"
        body = _json_body(content)
        assert body["job_id"] == "j-compile"
        assert body["tail_job_id"] == "j-upload"
        assert body["new_file"] == "new-name.yaml"
        assert builder.args_for("devices/rename") == {
            "configuration": FILE, "new_name": "new-name"}

    async def test_the_addon_returns_whole_job_records_not_bare_ids(self, hass, esphome_dir):
        # LIVE-FOUND. The API docs write the reply as {"job": <job_id>}, but the
        # add-on sends the whole job RECORD, so returning result["job"] verbatim
        # handed the agent a dict that get_esphome_job cannot accept.
        builder = FakeBuilder(**{
            "firmware/get_jobs": [],
            "devices/rename": {
                "configuration": FILE,
                "job": {"job_id": "j-compile", "status": "running"},
                "tail_job": {"job_id": "j-upload", "status": "queued"},
            },
        })
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)

        assert outcome == "allowed"
        body = _json_body(content)
        assert body["job_id"] == "j-compile"
        assert body["tail_job_id"] == "j-upload"

    async def test_a_bare_id_still_works_if_the_addon_ever_sends_one(self, hass, esphome_dir):
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        content, _o, _r = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)
        assert _json_body(content)["job_id"] == "j-compile"

    async def test_a_reply_carrying_no_job_is_a_config_only_rename(
        self, hass, esphome_dir
    ):
        # The add-on's config-only rename, verified against its SOURCE rather than
        # inferred from live runs that looked conclusive and were not: it returns
        # exactly this shape ({"configuration": ..., "job": None}) for an explicit
        # config_only argument (which Phoenix never sends) or for an IN-PLACE
        # rename, where the new name slugifies to the device's own file.
        #
        # The caller-facing distinction from a FAILED flash is the whole point:
        # there the config is reverted and installing anything is wrong, here the
        # file really does carry the new name and installing it is how you finish.
        builder = FakeBuilder(**{
            "firmware/get_jobs": [],
            "devices/rename": {"configuration": FILE, "job": None},
        })
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)

        assert outcome == "allowed"
        body = _json_body(content)
        assert body["config_only"] is True
        assert body["flashed"] is False
        assert "still answers to its OLD name" in body["note"]
        assert "NOT as a completed device rename" in body["note"]
        assert "install the firmware from this file" in body["note"]
        assert "job_id" not in body

    async def test_a_job_it_cannot_read_fails_loud_instead_of_reading_as_config_only(
        self, hass, esphome_dir
    ):
        # THE dangerous shape drift in this tier. The success branch keys on
        # "did we get a job id", so anything unreadable silently falls through to
        # the config-only branch and reports "nothing was compiled or flashed"
        # while a compile-and-flash chain runs on the device: the operator is
        # told the OPPOSITE of what is happening to their hardware.
        #
        # A missing or null "job" is the add-on's documented config-only reply
        # and must stay quiet; a job PRESENT but unreadable is a third shape and
        # must not be guessed at. The add-on's docs have already been wrong five
        # times, so this is a question of when, not whether.
        builder = FakeBuilder(**{
            "firmware/get_jobs": [],
            "devices/rename": {"configuration": FILE, "job": {"identifier": "j-new-shape"}},
        })
        content, outcome, _ = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)
        text = content["content"][0]["text"]

        assert outcome == "invalid_request"
        assert "cannot read" in text
        assert "Check the ESPHome dashboard" in text
        # Must NOT claim either outcome, since it does not know which happened.
        assert "config_only" not in text
        assert "nothing was compiled" not in text

    async def test_it_says_the_action_services_need_a_reconnect(self, hass, esphome_dir):
        # LIVE-CONFIRMED on a round-trip rename: after the flash the device is
        # back and its entities are fine, but esphome.<name>_<action> stays
        # UNREGISTERED until the config entry reconnects, because Home Assistant
        # registers those services on connect and never removes the old ones.
        # An operator reading only "renamed" finds a silently uncallable action.
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        content, _o, _r = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)

        note = _json_body(content)["note"]
        assert "reload" in note.lower()
        assert "config entry" in note

    async def test_config_only_is_never_taken_from_the_caller(self, hass, esphome_dir):
        # Same principle as install always sending port OTA: the caller does not
        # get to choose the quiet path.
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        await _run(
            {"file": FILE, "new_name": "new-name", "config_only": True},
            _flash_token(), hass, builder)
        assert "config_only" not in builder.args_for("devices/rename")

    async def test_it_says_a_failed_rename_reverts_itself(self, hass, esphome_dir):
        # LIVE-FOUND, and the whole reason this wording exists: the OTA flash
        # failed, the add-on reverted the configuration to the old name on its
        # own, and the agent read that revert as a half-finished rename. It then
        # called install_esphome_firmware on the OLD file to "complete" it, which
        # can never complete a rename and only reflashes what the device already
        # runs. A note that covers only the success path gets an improvised
        # recovery invented for the failure path, so the misreading is denied
        # here by name.
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        content, outcome, _r = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)
        note = _json_body(content)["note"]

        assert outcome == "allowed"
        assert "NOTHING is left half-applied" in note
        assert "Do NOT try to finish it by installing firmware" in note
        assert "rename_esphome_device again" in note

    async def test_it_warns_that_both_configurations_are_listed_meanwhile(
        self, hass, esphome_dir
    ):
        # LIVE-FOUND: the add-on COPIES the file rather than renaming it in place
        # and drops one only at the end, so mid-rename the operator sees two cards
        # and an agent listing files sees two configurations. Unwarned, that reads
        # as a half-applied rename or as debris to tidy up, and the tidying would
        # destroy the rename in flight.
        builder = FakeBuilder(**{"firmware/get_jobs": [], "devices/rename": RENAMED})
        content, _o, _r = await _run(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass, builder)
        note = _json_body(content)["note"]

        assert "both the old and the new" in note
        assert "neither copy is yours to clean up" in note
        assert "wait_for_esphome_job" in note


class TestApprovalDiff:
    async def test_it_names_the_action_services_that_will_break(
        self, hass, esphome_dir, esphome_entries
    ):
        # Entities survive a rename (HA keys them on the MAC); the device's
        # user-defined action SERVICES do not, and every automation calling one
        # stops resolving. That is the fact the card exists to surface.
        _esphome_entry(hass, esphome_entries)
        diff = esphome._build_diff_rename_esphome_device(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass)

        assert diff["kind"] == "system_action"
        assert "compiles and flashes" in diff["summary"]
        assert diff["preview"]["compiles_and_flashes"] is True
        assert "MAC" in diff["preview"]["retained"]

    async def test_it_survives_a_device_it_cannot_resolve(self, hass, esphome_dir):
        diff = esphome._build_diff_rename_esphome_device(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass)
        assert diff["preview"]["new_file"] == "new-name.yaml"
        assert diff["preview"]["device_online"] is False
        assert "cannot reach" in diff["preview"]["offline_note"]

    async def test_a_loaded_entry_does_not_mean_a_reachable_device(
        self, hass, esphome_dir, esphome_entries
    ):
        # entry.state describes the CONFIG ENTRY, not the device: a device that is
        # powered off or off the network keeps a perfectly LOADED entry, which is
        # the shape a live fleet actually presents. Reading the entry state as
        # liveness told the admin every offline device was online and dropped this
        # note in exactly the case it exists for, so the operator would approve
        # expecting a flash and get a file rename. Only runtime.available answers
        # it, which is what the install diff already reads.
        _esphome_entry(hass, esphome_entries, runtime=_runtime(available=False))
        diff = esphome._build_diff_rename_esphome_device(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass)

        assert diff["preview"]["device_online"] is False
        assert "cannot reach" in diff["preview"]["offline_note"]

    async def test_the_offline_note_never_promises_a_file_only_rename(
        self, hass, esphome_dir, esphome_entries
    ):
        # LIVE-FOUND: a device Home Assistant reported unavailable still answered
        # on the OTA port, so the add-on compiled and attempted a real flash while
        # this note promised the file would only be rewritten. device_online is
        # Home Assistant's API connection and the add-on never consults it, so the
        # note must describe that uncertainty instead of forecasting the add-on.
        _esphome_entry(hass, esphome_entries, runtime=_runtime(available=False))
        note = esphome._build_diff_rename_esphome_device(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass
        )["preview"]["offline_note"]

        assert "only rewrite" not in note
        assert "may still compile" in note
        assert "reverted in full" in note

    async def test_a_reachable_device_carries_no_offline_note(
        self, hass, esphome_dir, esphome_entries
    ):
        # The other direction, so "always offline" cannot pass for a fix: a note
        # claiming the rename is disk-only would be just as wrong on a device that
        # is about to be flashed.
        _esphome_entry(hass, esphome_entries, runtime=_runtime(available=True))
        diff = esphome._build_diff_rename_esphome_device(
            {"file": FILE, "new_name": "new-name"}, _flash_token(), hass)

        assert diff["preview"]["device_online"] is True
        assert "offline_note" not in diff["preview"]
