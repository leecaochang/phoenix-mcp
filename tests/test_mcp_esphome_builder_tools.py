"""Tests for the ESPHome Device Builder read tools (validate, boards, components, automations).

The WebSocket layer has its own suite; these tests fake async_builder_command at
the seam so they can concentrate on what this layer owns: capability gating, the
path jail, entity scoping, and credential scrubbing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.phoenix_mcp.esphome_builder import (
    BuilderAuthRequired,
    BuilderCommandError,
    BuilderResult,
    BuilderUnavailable,
)
from tests.test_mcp_esphome_tools import (
    _call,
    _device_info,
    _esphome_entry,
    _json,
    _runtime,
    _yaml_token,
)

_DASHBOARD_PATCH = "custom_components.phoenix_mcp.tools.esphome._esphome_dashboard"
_COMMAND_PATCH = "custom_components.phoenix_mcp.tools.esphome.async_builder_command"

# The credential values the fixture files carry, which must never reach a response.
REAL_API_KEY = "REALAPIKEY123456="
REAL_OTA_PASSWORD = "realotapassword"
REAL_WIFI_PASSWORD = "housepassword1"
REAL_WIFI_SSID = "MyHomeNetwork"

FILE_TOOLS = ("validate_esphome_yaml", "get_esphome_automations")
ALL_TOOLS = ("validate_esphome_yaml", "get_esphome_board",
             "get_esphome_component", "get_esphome_automations")


def _args(tool: str, **over) -> dict:
    base = {"file": "rf-blaster1.yaml"} if tool in FILE_TOOLS else {}
    base.update(over)
    return base


def _builder(result=None, **kw):
    """A stand-in Device Builder returning one scripted result."""
    return AsyncMock(return_value=BuilderResult(result=result, **kw))


def _dash():
    return SimpleNamespace(url="http://127.0.0.1:6052", data={}, last_update_success=True)


class TestGating:
    @pytest.mark.parametrize("tool", ALL_TOOLS)
    async def test_cap_denied(self, hass, esphome_dir, tool):
        with patch(_DASHBOARD_PATCH, return_value=_dash()):
            content, outcome, _ = await _call(
                tool, _args(tool), _yaml_token(cap_esphome_yaml="deny"), hass)
        assert outcome == "denied"
        assert "Forbidden" in content["content"][0]["text"]

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    async def test_builder_absent_refuses_before_touching_anything(self, hass, esphome_dir, tool):
        command = AsyncMock()
        with patch(_DASHBOARD_PATCH, return_value=None), patch(_COMMAND_PATCH, command):
            content, outcome, _ = await _call(tool, _args(tool), _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "Device Builder add-on is not available" in content["content"][0]["text"]
        command.assert_not_awaited()

    async def test_unreachable_and_auth_required_read_differently(self, hass, esphome_dir):
        """An operator can restart a stopped add-on; a credential wall is a different problem."""
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(side_effect=BuilderUnavailable("x"))):
            down, outcome, _ = await _call(
                "validate_esphome_yaml", _args("validate_esphome_yaml"), _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "did not respond" in down["content"][0]["text"]

        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(side_effect=BuilderAuthRequired("x"))):
            auth, _, _ = await _call(
                "validate_esphome_yaml", _args("validate_esphome_yaml"), _yaml_token(), hass)
        assert "requires authentication" in auth["content"][0]["text"]
        assert down["content"][0]["text"] != auth["content"][0]["text"]

    async def test_command_error_surfaces_its_code(self, hass, esphome_dir):
        err = BuilderCommandError("not_found", "no such configuration")
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(side_effect=err)):
            content, outcome, _ = await _call(
                "get_esphome_board", {"board_id": "ghost"}, _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "not_found" in content["content"][0]["text"]


class TestScopingAndJail:
    @pytest.mark.parametrize("tool", FILE_TOOLS)
    @pytest.mark.parametrize("bad", [
        "secrets.yaml", "SECRETS.YAML", "archive/old-device.yaml",
        "../configuration.yaml", "/etc/passwd", ".device-builder.json", "notes.txt",
    ])
    async def test_jail_refuses(self, hass, esphome_dir, tool, bad):
        command = AsyncMock()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            _content, outcome, _ = await _call(tool, {"file": bad}, _yaml_token(), hass)
        assert outcome == "invalid_request"
        command.assert_not_awaited()

    @pytest.mark.parametrize("tool", FILE_TOOLS)
    async def test_missing_and_out_of_scope_are_byte_identical(
        self, hass, esphome_dir, esphome_entries, tool
    ):
        """A file the token may not see must look exactly like one that is absent.

        The Device Builder would answer differently for the two, which is why
        existence is resolved here before it is ever asked.
        """
        command = AsyncMock(return_value=BuilderResult(result={}))
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            missing, missing_outcome, _ = await _call(
                tool, {"file": "nope.yaml"}, _yaml_token(), hass)
            _esphome_entry(hass, esphome_entries, entity_ids=("light.not_mine",),
                           runtime=_runtime(_device_info(name="rf-blaster1")))
            scoped, _outcome, _ = await _call(
                tool, {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert missing["content"][0]["text"] == scoped["content"][0]["text"]
        assert missing_outcome == "not_found"
        command.assert_not_awaited()


class TestValidate:
    async def test_reports_success_and_output(self, hass, esphome_dir):
        result = BuilderResult(output="INFO Configuration is valid!\n", success=True, exit_code=0)
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(return_value=result)):
            content, outcome, resource = await _call(
                "validate_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        body = _json(content)
        assert outcome == "allowed"
        assert resource == "esphome:rf-blaster1.yaml"
        assert body["valid"] is True
        assert body["exit_code"] == 0
        assert "Configuration is valid" in body["output"]

    async def test_sends_the_filename_and_never_the_content(self, hass, esphome_dir):
        """Validation must run on approved on-disk content, not a caller payload.

        ESPHome evaluates config at validation time and external_components can
        fetch and execute remote code, so accepting arbitrary content here would
        be an unreviewed code-execution path.
        """
        command = _builder()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            await _call("validate_esphome_yaml",
                        {"file": "rf-blaster1.yaml", "content": "esphome:\n  name: evil\n"},
                        _yaml_token(), hass)
        _hass_arg, _url, cmd, payload = command.await_args.args
        assert cmd == "devices/validate"
        assert payload == {"configuration": "rf-blaster1.yaml"}
        assert "content" not in payload and "yaml" not in payload

    async def test_output_scrubs_inline_and_secrets_yaml_credentials(self, hass, esphome_dir):
        """The validator quotes offending lines back, which can carry a credential."""
        noisy = (
            f'ERROR in api.encryption.key: "{REAL_API_KEY}" is not valid\n'
            f'ERROR ota password "{REAL_OTA_PASSWORD}" rejected\n'
            f'ERROR wifi password "{REAL_WIFI_PASSWORD}" rejected\n'
        )
        result = BuilderResult(output=noisy, success=False, exit_code=1)
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(return_value=result)):
            content, _, _ = await _call(
                "validate_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        text = content["content"][0]["text"]
        for secret in (REAL_API_KEY, REAL_OTA_PASSWORD, REAL_WIFI_PASSWORD):
            assert secret not in text, f"{secret} leaked through validation output"
        assert "<redacted>" in text
        # The useful part of the message survives.
        assert "is not valid" in _json(content)["output"]

    async def test_scrub_survives_an_unparseable_file(self, hass, esphome_dir):
        """A broken file is exactly when someone validates, so the scrub must still run."""
        (esphome_dir / "broken.yaml").write_text(
            f'esphome:\n  name: broken\n  bad: [unclosed\nwifi:\n  password: "{REAL_WIFI_PASSWORD}"\n'
        )
        result = BuilderResult(
            output=f'ERROR could not parse, near "{REAL_WIFI_PASSWORD}"\n',
            success=False, exit_code=1,
        )
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(return_value=result)):
            content, outcome, _ = await _call(
                "validate_esphome_yaml", {"file": "broken.yaml"}, _yaml_token(), hass)
        assert outcome == "allowed"
        assert REAL_WIFI_PASSWORD not in content["content"][0]["text"]

    async def test_truncation_is_reported(self, hass, esphome_dir):
        result = BuilderResult(output="x", success=False, exit_code=1, output_truncated=True)
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, AsyncMock(return_value=result)):
            content, _, _ = await _call(
                "validate_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert _json(content)["output_truncated"] is True


class TestBoards:
    async def test_board_id_fetches_one_board(self, hass, esphome_dir):
        command = _builder({"id": "esp32dev", "pins": [{"gpio": 2}]})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, outcome, _ = await _call(
                "get_esphome_board", {"board_id": "esp32dev"}, _yaml_token(), hass)
        assert outcome == "allowed"
        assert _json(content)["board"]["pins"] == [{"gpio": 2}]
        assert command.await_args.args[2] == "boards/get_board"

    async def test_no_board_id_searches(self, hass, esphome_dir):
        command = _builder({"boards": []})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, _, _ = await _call(
                "get_esphome_board", {"query": "esp32"}, _yaml_token(), hass)
        assert "boards" in _json(content)
        _h, _u, cmd, payload = command.await_args.args
        assert cmd == "boards/get_boards"
        assert payload["query"] == "esp32"

    @pytest.mark.parametrize(("given", "expected"), [
        (500, 50), (0, 20), (-3, 20), ("many", 20), (None, 20), (7, 7),
    ])
    async def test_limit_is_clamped(self, hass, esphome_dir, given, expected):
        command = _builder({"boards": []})
        args = {"query": "esp"} if given is None else {"query": "esp", "limit": given}
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            await _call("get_esphome_board", args, _yaml_token(), hass)
        assert command.await_args.args[3]["limit"] == expected


class TestComponents:
    async def test_ids_fetch_full_bodies(self, hass, esphome_dir):
        command = _builder({"dht": {"schema": {}}})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, _, _ = await _call(
                "get_esphome_component", {"component_ids": ["dht"]}, _yaml_token(), hass)
        _h, _u, cmd, payload = command.await_args.args
        assert cmd == "components/get_component_bodies"
        assert payload["component_ids"] == ["dht"]
        assert "dht" in _json(content)["components"]

    async def test_too_many_ids_refused(self, hass, esphome_dir):
        command = AsyncMock()
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, outcome, _ = await _call(
                "get_esphome_component", {"component_ids": [f"c{i}" for i in range(11)]},
                _yaml_token(), hass)
        assert outcome == "invalid_request"
        assert "at most 10" in content["content"][0]["text"]
        command.assert_not_awaited()

    async def test_query_searches(self, hass, esphome_dir):
        command = _builder({"components": []})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            await _call("get_esphome_component", {"query": "temperature"}, _yaml_token(), hass)
        assert command.await_args.args[2] == "components/get_components"

    async def test_bare_call_lists_categories(self, hass, esphome_dir):
        command = _builder([{"id": "sensor", "name": "Sensor", "count": 90}])
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, _, _ = await _call("get_esphome_component", {}, _yaml_token(), hass)
        assert command.await_args.args[2] == "components/get_categories"
        assert _json(content)["categories"][0]["id"] == "sensor"


class TestAutomations:
    async def test_returns_the_scoped_catalog(self, hass, esphome_dir):
        command = _builder({"triggers": [{"id": "binary_sensor.on_press"}], "actions": []})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, outcome, resource = await _call(
                "get_esphome_automations", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert outcome == "allowed"
        assert resource == "esphome:rf-blaster1.yaml"
        assert _json(content)["automations"]["triggers"][0]["id"] == "binary_sensor.on_press"

    async def test_args_are_exactly_the_configuration(self, hass, esphome_dir):
        """Never the draft-yaml variant: same config-time evaluation validate refuses."""
        command = _builder({})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            await _call("get_esphome_automations",
                        {"file": "rf-blaster1.yaml", "yaml": "esphome:\n  name: evil\n"},
                        _yaml_token(), hass)
        _h, _u, cmd, payload = command.await_args.args
        assert cmd == "automations/get_available"
        assert payload == {"configuration": "rf-blaster1.yaml"}

    async def test_catalog_is_projected_to_config_shaping_fields(self, hass, esphome_dir):
        """Prose is dropped; the flags that change how you write config are kept.

        The raw catalog for a single device runs to tens of kilobytes, enough to
        swamp an agent's context. The row SET is never trimmed, only the fields,
        because a catalog that silently omits entries is worse than a big one.
        """
        raw = {
            "triggers": [{
                "id": "on_boot", "name": "On Boot",
                "description": "A long prose description that costs tokens.",
                "docs_url": "https://esphome.io/components/esphome",
                "applies_to": [], "is_device_level": True, "supports_list": True,
            }],
            "actions": [{
                "id": "repeat", "name": "Repeat", "domain": "core",
                "description": "More prose.", "docs_url": "https://esphome.io/x",
                "is_control_flow": True, "has_else_branch": False,
                "accepts_action_list": ["then"], "form_editable": True,
            }],
            "conditions": [{
                "id": "lambda", "name": "Lambda", "domain": "core",
                "description": "Prose.", "docs_url": "https://esphome.io/y",
                "accepts_condition_list": [],
            }],
            "scripts": [{"id": "my_script", "parameters": {"x": "int"}}],
            "devices": [{"component_id": "logger", "id": "logger", "title": "Logger",
                         "name": "My Logger",
                         "is_entity_container": False, "parent_id": None}],
        }
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, _builder(raw)):
            content, _, _ = await _call(
                "get_esphome_automations", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        cat = _json(content)["automations"]

        # Every row survives, in every section.
        assert [len(cat[s]) for s in ("triggers", "actions", "conditions", "devices")] == [1, 1, 1, 1]
        # Prose and the UI-editor flag are gone.
        blob = json.dumps(cat)
        for dropped in ("description", "docs_url", "form_editable", "prose", "Prose", "On Boot"):
            assert dropped not in blob, f"{dropped} survived the projection"
        # The flags that shape config are kept.
        assert cat["triggers"][0] == {
            "id": "on_boot", "is_device_level": True, "supports_list": True}
        # `name` on a catalog row is just the id prettified for the add-on's own
        # dropdowns, so it is dropped; a device's name is the operator's and stays.
        assert "name" not in cat["actions"][0]
        assert cat["devices"][0].get("name") is None or "name" in cat["devices"][0]
        assert cat["actions"][0]["accepts_action_list"] == ["then"]
        assert cat["actions"][0]["is_control_flow"] is True
        # False and empty values are dropped rather than repeated on every row.
        assert "has_else_branch" not in cat["actions"][0]
        assert "applies_to" not in cat["triggers"][0]
        assert "is_entity_container" not in cat["devices"][0]
        assert cat["devices"][0]["name"] == "My Logger"
        # scripts are the device's OWN declarations, passed through whole.
        assert cat["scripts"] == [{"id": "my_script", "parameters": {"x": "int"}}]

    async def test_unexpected_catalog_shape_passes_through(self, hass, esphome_dir):
        """A future section Phoenix does not know about must not be dropped."""
        with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                patch(_COMMAND_PATCH, _builder({"future_section": [{"id": "x"}]})):
            content, _, _ = await _call(
                "get_esphome_automations", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert _json(content)["automations"]["future_section"] == [{"id": "x"}]

    async def test_response_is_scrubbed(self, hass, esphome_dir):
        command = _builder({"note": f"configured with {REAL_API_KEY}"})
        with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, command):
            content, _, _ = await _call(
                "get_esphome_automations", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)
        assert REAL_API_KEY not in content["content"][0]["text"]


class TestNoSecretsAnywhere:
    async def test_no_fixture_secret_appears_in_any_tool_response(self, hass, esphome_dir):
        """Sweep every tool at once: one uncovered path is one leaked credential."""
        echo = f"{REAL_API_KEY} {REAL_OTA_PASSWORD} {REAL_WIFI_PASSWORD} {REAL_WIFI_SSID}"
        results = {
            "validate_esphome_yaml": BuilderResult(output=echo, success=True, exit_code=0),
            "get_esphome_automations": BuilderResult(result={"echo": echo}),
            "get_esphome_board": BuilderResult(result={"echo": echo}),
            "get_esphome_component": BuilderResult(result={"echo": echo}),
        }
        for tool in FILE_TOOLS:
            with patch(_DASHBOARD_PATCH, return_value=_dash()), \
                    patch(_COMMAND_PATCH, AsyncMock(return_value=results[tool])):
                content, outcome, _ = await _call(tool, _args(tool), _yaml_token(), hass)
            assert outcome == "allowed"
            text = content["content"][0]["text"]
            for secret in (REAL_API_KEY, REAL_OTA_PASSWORD, REAL_WIFI_PASSWORD):
                assert secret not in text, f"{secret} leaked from {tool}"

    async def test_secrets_yaml_is_never_readable_through_these_tools(self, hass, esphome_dir):
        for tool in FILE_TOOLS:
            with patch(_DASHBOARD_PATCH, return_value=_dash()), patch(_COMMAND_PATCH, AsyncMock()):
                content, outcome, _ = await _call(
                    tool, {"file": "secrets.yaml"}, _yaml_token(), hass)
            assert outcome == "invalid_request"
            assert REAL_WIFI_PASSWORD not in json.dumps(content)


class TestFullStack:
    """One test with nothing between the tool and the wire.

    Every other test here stubs async_builder_command, so the seam between this layer
    and the WebSocket client is never exercised: a mismatch in how the result is
    unpacked would pass both suites while failing live.
    """

    async def test_validate_round_trips_through_a_real_websocket(
        self, hass, esphome_dir, aiohttp_client, monkeypatch, socket_enabled
    ):
        from custom_components.phoenix_mcp import esphome_builder
        from tests.test_esphome_builder import FakeDeviceBuilder, stream

        fake = FakeDeviceBuilder(replies={
            "devices/validate": stream(
                "INFO Reading configuration rf-blaster1.yaml...\n",
                f'ERROR key "{REAL_API_KEY}" is malformed\n',
                success=False, code=1,
            ),
        })
        client = await aiohttp_client(fake.app())
        monkeypatch.setattr(
            esphome_builder, "async_get_clientsession", lambda _hass: client.session)
        url = str(client.make_url("")).rstrip("/")

        with patch(_DASHBOARD_PATCH, return_value=SimpleNamespace(url=url)):
            content, outcome, resource = await _call(
                "validate_esphome_yaml", {"file": "rf-blaster1.yaml"}, _yaml_token(), hass)

        body = _json(content)
        assert outcome == "allowed"
        assert resource == "esphome:rf-blaster1.yaml"
        assert body["valid"] is False
        assert body["exit_code"] == 1
        assert "is malformed" in body["output"]
        # The credential the validator echoed back never reaches the agent.
        assert REAL_API_KEY not in content["content"][0]["text"]
        assert fake.received[0]["args"] == {"configuration": "rf-blaster1.yaml"}
