"""Tests for raw YAML edit (get/set_yaml_config) and integration enable/disable."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.mcp_view import (
    _EXECUTOR_REGISTRY,
    _call_tool,
    _execute_patch_yaml_config,
    _execute_set_yaml_config,
)
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
)


@pytest.fixture(autouse=True)
def isolated_config_dir(hass, tmp_path):
    """set_yaml_config writes configuration.yaml; keep it out of the shared
    shared testing config dir so it cannot leak into other tests (the authoring
    executors now resolve configuration.yaml via yaml_includes)."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _token(**caps) -> TokenRecord:
    base = {"cap_yaml_edit": "allow", "cap_integration_write": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=PermissionTree(), **base,
    )


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(name, args, token, hass):
    return await _call_tool(name, args, token, hass, MagicMock())


class TestExecutorRegistration:
    def test_registered(self):
        assert "set_yaml_config" in _EXECUTOR_REGISTRY
        assert "set_integration_enabled" in _EXECUTOR_REGISTRY


class TestYamlConfig:
    async def test_get_deny(self, hass):
        _, outcome, _ = await _call("get_yaml_config", {}, _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"

    async def test_get_and_set(self, hass):
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default_config:\n")
        content, outcome, _ = await _call("get_yaml_config", {}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["content"] == "default_config:\n"

        content, outcome, _ = await _call("set_yaml_config", {"content": "default_config:\nfrontend:\n"}, _token(), hass)
        assert outcome == "allowed"
        with open(path, encoding="utf-8") as f:
            assert f.read() == "default_config:\nfrontend:\n"

    async def test_set_non_string(self, hass):
        _, outcome, _ = await _call("set_yaml_config", {"content": {"a": 1}}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_set_non_string_confirm_mode_rejected_before_pending(self, hass):
        """A doomed write must fail before a pending approval is created, even
        under confirm mode, otherwise it sails through as a false pending that
        can only fail once an admin approves it."""
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": {"a": 1}}, _token(cap_yaml_edit="confirm"), hass)
        assert outcome == "invalid_request"


class TestGetYamlConfigFileArg:
    """get_yaml_config reads other YAML files in the config dir, inside a jail."""

    async def test_zero_arg_response_shape_is_unchanged(self, hass):
        # The no-argument response is a contract: the CAS flow and existing
        # clients depend on exactly these keys, in this order, with no extras.
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default_config:\n")
        content, _, _ = await _call("get_yaml_config", {}, _token(), hass)
        body = _json(content)
        assert list(body) == ["path", "exists", "content", "content_hash"]
        assert body["path"] == "configuration.yaml"

    async def test_reads_an_include_target(self, hass):
        with open(hass.config.path("automations.yaml"), "w", encoding="utf-8") as f:
            f.write("- id: '1'\n  alias: A\n")
        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "automations.yaml"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["path"] == "automations.yaml"
        assert body["content"] == "- id: '1'\n  alias: A\n"

    async def test_reads_a_nested_package_file(self, hass):
        import os
        os.makedirs(hass.config.path("packages"), exist_ok=True)
        with open(hass.config.path("packages/kitchen.yaml"), "w", encoding="utf-8") as f:
            f.write("sensor: []\n")
        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "packages/kitchen.yaml"}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["content"] == "sensor: []\n"

    async def test_absent_file_reads_as_empty(self, hass):
        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "nope.yaml"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["exists"] is False
        assert body["content"] == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "../outside.yaml",
            "packages/../../outside.yaml",
            "/etc/passwd.yaml",
            "secrets.yaml",
            "SECRETS.YAML",
            "sub/secrets.yaml",
            "esphome/device.yaml",
            "ESPHOME/device.yaml",
            ".storage/core.entity_registry.yaml",
            "packages/.hidden/x.yaml",
            "notes.txt",
            "configuration",
        ],
    )
    async def test_refused_paths(self, hass, bad):
        content, outcome, _ = await _call("get_yaml_config", {"file": bad}, _token(), hass)
        assert outcome == "invalid_request"
        assert "configuration directory" in content["content"][0]["text"]

    @pytest.mark.parametrize("blank", ["", "   ", None])
    async def test_blank_file_arg_reads_configuration_yaml(self, hass, blank):
        # Models routinely send "" for an omitted optional string; treat a blank
        # value as unset rather than refusing it.
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write("default_config:\n")
        content, outcome, _ = await _call("get_yaml_config", {"file": blank}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["path"] == "configuration.yaml"

    async def test_non_string_file_arg_refused(self, hass):
        _, outcome, _ = await _call("get_yaml_config", {"file": ["a.yaml"]}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_symlink_out_of_config_dir_refused(self, hass, tmp_path):
        import os
        outside = tmp_path.parent / "outside_secret.yaml"
        outside.write_text("secret: 1\n", encoding="utf-8")
        os.symlink(outside, hass.config.path("link.yaml"))
        _, outcome, _ = await _call("get_yaml_config", {"file": "link.yaml"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_symlink_renaming_secrets_refused(self, hass):
        # The extension/name rules re-run on the RESOLVED path, so a symlink
        # cannot launder secrets.yaml behind an allowed-looking name.
        import os
        with open(hass.config.path("secrets.yaml"), "w", encoding="utf-8") as f:
            f.write("db_pass: hunter2\n")
        os.symlink(hass.config.path("secrets.yaml"), hass.config.path("innocent.yaml"))
        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "innocent.yaml"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "hunter2" not in content["content"][0]["text"]

    async def test_symlink_renaming_esphome_device_yaml_refused(self, hass):
        import os
        os.makedirs(hass.config.path("esphome"), exist_ok=True)
        with open(hass.config.path("esphome/device.yaml"), "w", encoding="utf-8") as f:
            f.write("wifi:\n  password: house-password\n")
        os.symlink(hass.config.path("esphome/device.yaml"), hass.config.path("innocent.yaml"))
        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "innocent.yaml"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "house-password" not in content["content"][0]["text"]

    async def test_deny_applies_to_file_reads(self, hass):
        _, outcome, _ = await _call(
            "get_yaml_config", {"file": "automations.yaml"}, _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"


class TestGetYamlConfigKeyArg:
    """key returns one fragment, tag-preserving, with the full-file hash."""

    def _write(self, hass, text):
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write(text)

    async def test_returns_the_fragment_only(self, hass):
        self._write(hass, "http:\n  server_port: 8123\nfrontend:\n  themes: {}\n")
        content, outcome, _ = await _call("get_yaml_config", {"key": "http"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["key_found"] is True
        assert body["content"] == "server_port: 8123\n"
        assert "frontend" not in body["content"]

    async def test_nested_dotted_key(self, hass):
        self._write(hass, "homeassistant:\n  packages: !include_dir_named integrations\n")
        content, _, _ = await _call(
            "get_yaml_config", {"key": "homeassistant.packages"}, _token(), hass)
        body = _json(content)
        assert body["key_found"] is True
        assert body["defined_via_include"] == "integrations"
        assert body["content"].strip() == "!include_dir_named integrations"

    async def test_missing_key(self, hass):
        self._write(hass, "http:\n  server_port: 8123\n")
        content, outcome, _ = await _call("get_yaml_config", {"key": "lovelace"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["key_found"] is False
        assert body["content"] == ""

    async def test_key_through_a_non_mapping_is_not_found(self, hass):
        self._write(hass, "http: 5\n")
        body = _json((await _call("get_yaml_config", {"key": "http.port"}, _token(), hass))[0])
        assert body["key_found"] is False

    async def test_labeled_top_level_key(self, hass):
        # HA allows "automation manual:" style labeled keys; they carry no dot,
        # so the walker finds them whole.
        self._write(hass, "automation manual:\n  - alias: A\n")
        body = _json((await _call(
            "get_yaml_config", {"key": "automation manual"}, _token(), hass))[0])
        assert body["key_found"] is True

    async def test_secret_tag_survives_as_source_text(self, hass):
        self._write(hass, "http:\n  api_password: !secret pw\n")
        body = _json((await _call("get_yaml_config", {"key": "http"}, _token(), hass))[0])
        assert "!secret pw" in body["content"]

    async def test_unknown_custom_tag_does_not_break_the_read(self, hass):
        # A third-party tag must not make the file unreadable.
        self._write(hass, "http:\n  server_port: 8123\ncustom: !my_vendor_tag thing\n")
        body = _json((await _call("get_yaml_config", {"key": "http"}, _token(), hass))[0])
        assert body["key_found"] is True

    async def test_invalid_yaml_with_key_errors(self, hass):
        self._write(hass, "http:\n  - [unclosed\n")
        content, outcome, _ = await _call("get_yaml_config", {"key": "http"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "not valid YAML" in content["content"][0]["text"]

    async def test_invalid_yaml_without_key_still_returns_raw_text(self, hass):
        # No key means no parse, so a broken file is still readable for repair.
        self._write(hass, "http:\n  - [unclosed\n")
        content, outcome, _ = await _call("get_yaml_config", {}, _token(), hass)
        assert outcome == "allowed"
        assert "unclosed" in _json(content)["content"]

    async def test_content_hash_is_the_whole_file_even_with_key(self, hass):
        text = "http:\n  server_port: 8123\nfrontend:\n"
        self._write(hass, text)
        body = _json((await _call("get_yaml_config", {"key": "http"}, _token(), hass))[0])
        assert body["content_hash"] == content_hash(text)

    async def test_key_from_another_file(self, hass):
        with open(hass.config.path("extra.yaml"), "w", encoding="utf-8") as f:
            f.write("outer:\n  inner: 7\n")
        body = _json((await _call(
            "get_yaml_config", {"file": "extra.yaml", "key": "outer.inner"}, _token(), hass))[0])
        assert body["key_found"] is True
        assert body["content"].strip() == "7"

    async def test_empty_key_rejected(self, hass):
        _, outcome, _ = await _call("get_yaml_config", {"key": "  "}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_absent_file_with_key_reports_not_found(self, hass):
        body = _json((await _call(
            "get_yaml_config", {"file": "gone.yaml", "key": "http"}, _token(), hass))[0])
        assert body["exists"] is False
        assert body["key_found"] is False


class TestYamlProtectedSubtrees:
    """set_yaml_config may never change the keys that define HA's trust boundary."""

    def _write(self, hass, text):
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, hass):
        with open(hass.config.path("configuration.yaml"), encoding="utf-8") as f:
            return f.read()

    @pytest.mark.parametrize(
        ("before", "after", "path"),
        [
            # add
            ("default_config:\n", "default_config:\nhttp:\n  trusted_proxies:\n    - 10.0.0.1\n",
             "http.trusted_proxies"),
            ("default_config:\n", "frontend:\n  extra_module_url:\n    - /local/x.js\n",
             "frontend.extra_module_url"),
            ("default_config:\n", "lovelace:\n  resources:\n    - url: /local/c.js\n",
             "lovelace.resources"),
            ("default_config:\n", "homeassistant:\n  auth_providers:\n    - type: trusted_networks\n",
             "homeassistant.auth_providers"),
            ("default_config:\n", "homeassistant:\n  packages: !include_dir_named pkg\n",
             "homeassistant.packages"),
            # modify
            ("http:\n  trusted_proxies:\n    - 10.0.0.1\n",
             "http:\n  trusted_proxies:\n    - 0.0.0.0/0\n", "http.trusted_proxies"),
            ("http:\n  ip_ban_enabled: true\n", "http:\n  ip_ban_enabled: false\n",
             "http.ip_ban_enabled"),
            # remove
            ("http:\n  ip_ban_enabled: true\n", "http:\n  server_port: 8123\n",
             "http.ip_ban_enabled"),
        ],
    )
    async def test_protected_change_refused(self, hass, before, after, path):
        self._write(hass, before)
        content, outcome, _ = await _call("set_yaml_config", {"content": after}, _token(), hass)
        assert outcome == "invalid_request"
        assert path in content["content"][0]["text"]
        assert self._read(hass) == before  # untouched

    async def test_unchanged_protected_value_passes(self, hass):
        before = "http:\n  trusted_proxies:\n    - 10.0.0.1\n  server_port: 8123\n"
        after = "http:\n  trusted_proxies:\n    - 10.0.0.1\n  server_port: 8124\n"
        self._write(hass, before)
        _, outcome, _ = await _call("set_yaml_config", {"content": after}, _token(), hass)
        assert outcome == "allowed"
        assert self._read(hass) == after

    async def test_unchanged_include_tag_value_passes(self, hass):
        # TaggedValue is a frozen dataclass, so structural equality holds and an
        # untouched !include_dir_named packages line is not seen as a change.
        before = "homeassistant:\n  packages: !include_dir_named pkg\n"
        after = "homeassistant:\n  packages: !include_dir_named pkg\nfrontend:\n"
        self._write(hass, before)
        _, outcome, _ = await _call("set_yaml_config", {"content": after}, _token(), hass)
        assert outcome == "allowed"

    async def test_changed_include_tag_value_refused(self, hass):
        self._write(hass, "homeassistant:\n  packages: !include_dir_named pkg\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "homeassistant:\n  packages: !include_dir_named attacker\n"},
            _token(), hass)
        assert outcome == "invalid_request"

    async def test_unrelated_powerful_keys_still_writable(self, hass):
        # The floor is not a "powerful keys" list: shell_command is already
        # accepted surface for a token holding cap_yaml_edit.
        self._write(hass, "default_config:\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\nshell_command:\n  ls: ls -la\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_unparseable_new_content_refused(self, hass):
        self._write(hass, "default_config:\n")
        content, outcome, _ = await _call(
            "set_yaml_config", {"content": "http:\n  - [unclosed\n"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "not valid YAML" in content["content"][0]["text"]
        assert self._read(hass) == "default_config:\n"

    async def test_unknown_custom_tag_is_writable(self, hass):
        # A third-party tag must not make configuration.yaml permanently unwritable.
        self._write(hass, "default_config:\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\nvendor: !my_vendor_tag thing\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_unparseable_old_content_fails_closed(self, hass):
        # The prior value cannot be established, so a protected key in the new
        # content counts as changed.
        self._write(hass, "http:\n  - [unclosed\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "http:\n  trusted_proxies:\n    - 10.0.0.1\n"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_unparseable_old_content_allows_a_clean_rewrite(self, hass):
        # Repairing a broken file is the whole point of the tool; only protected
        # keys are blocked.
        self._write(hass, "http:\n  - [unclosed\n")
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "http:\n  server_port: 8123\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_confirm_mode_refuses_before_creating_an_approval(self, hass):
        """A doomed write must not become a pending approval."""
        self._write(hass, "default_config:\n")
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _, outcome, _ = await _call_tool(
            "set_yaml_config",
            {"content": "http:\n  trusted_proxies:\n    - 0.0.0.0/0\n"},
            _token(cap_yaml_edit="confirm"), hass, data)
        assert outcome == "invalid_request"
        # create_approval persists through set_pending_approvals + async_save.
        data.store.set_pending_approvals.assert_not_called()

    async def test_denied_token_learns_nothing_about_its_payload(self, hass):
        """The cap-deny check runs first, so a denied token gets the uniform
        Forbidden message and never the protected-key detail."""
        self._write(hass, "default_config:\n")
        content, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "http:\n  trusted_proxies:\n    - 0.0.0.0/0\n"},
            _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"
        text = content["content"][0]["text"]
        assert "trusted_proxies" not in text and "protected" not in text.lower()

    async def test_executor_recheck_catches_approval_window_drift(self, hass):
        """The file gained a protected key while the approval was pending."""
        self._write(hass, "default_config:\n")
        # The admin approves a write that was clean when it was queued...
        self._write(hass, "http:\n  trusted_proxies:\n    - 10.0.0.1\n")
        _, outcome, _ = await _execute_set_yaml_config(
            {"content": "default_config:\n"}, _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "trusted_proxies" in self._read(hass)

    async def test_absent_file_with_protected_key_refused(self, hass):
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "http:\n  cors_allowed_origins:\n    - '*'\n"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_protected_key_under_a_non_mapping_parent_is_handled(self, hass):
        # "http: 5" is not a mapping; the walk must not raise.
        self._write(hass, "http: 5\n")
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "http: 6\n"}, _token(), hass)
        assert outcome == "allowed"


class TestYamlTopLevelKeyRemoval:
    """A whole-file replace may not silently drop a top-level key.

    Nothing else catches this case: the content is valid YAML, no protected
    subtree moved, and expected_hash matches because the caller did read the
    file first. Dropping `automation: !include automations.yaml` disables every
    automation while the file it names sits on disk intact.
    """

    def _write(self, hass, text):
        with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, hass):
        with open(hass.config.path("configuration.yaml"), encoding="utf-8") as f:
            return f.read()

    async def test_undeclared_removal_refused(self, hass):
        before = "default_config:\nautomation: !include automations.yaml\nrecorder:\n  purge_keep_days: 10\n"
        self._write(hass, before)
        content, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\nrecorder:\n  purge_keep_days: 10\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "automation" in content["content"][0]["text"]
        assert self._read(hass) == before  # untouched

    async def test_declared_removal_allowed(self, hass):
        after = "default_config:\nrecorder:\n  purge_keep_days: 10\n"
        self._write(
            hass,
            "default_config:\nautomation: !include automations.yaml\nrecorder:\n  purge_keep_days: 10\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": after, "remove_keys": ["automation"]}, _token(), hass)
        assert outcome == "allowed"
        assert self._read(hass) == after

    async def test_declaring_only_one_of_two_removals_refused(self, hass):
        """The declaration is per key, so a second removal cannot ride along."""
        self._write(hass, "default_config:\nautomation: !include a.yaml\nrecorder:\n")
        content, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\n", "remove_keys": ["automation"]},
            _token(), hass)
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "recorder" in text and "automation" not in text

    async def test_addition_and_modification_pass(self, hass):
        self._write(hass, "default_config:\nrecorder:\n  purge_keep_days: 10\n")
        after = "default_config:\nrecorder:\n  purge_keep_days: 30\nfrontend:\n"
        _, outcome, _ = await _call("set_yaml_config", {"content": after}, _token(), hass)
        assert outcome == "allowed"
        assert self._read(hass) == after

    async def test_byte_identical_content_passes(self, hass):
        same = "default_config:\nautomation: !include automations.yaml\n"
        self._write(hass, same)
        _, outcome, _ = await _call("set_yaml_config", {"content": same}, _token(), hass)
        assert outcome == "allowed"

    async def test_unparseable_prior_content_abstains(self, hass):
        """Repairing a broken configuration.yaml is exactly what this tool is
        for, so an unreadable prior file abstains rather than refusing."""
        self._write(hass, "default_config:\n  bad: [unclosed\nautomation: x\n")
        after = "default_config:\n"
        _, outcome, _ = await _call("set_yaml_config", {"content": after}, _token(), hass)
        assert outcome == "allowed"
        assert self._read(hass) == after

    async def test_absent_prior_file_abstains(self, hass):
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "default_config:\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_wrong_shaped_remove_keys_declares_nothing(self, hass):
        """str_list_arg degrades a wrong shape to absent, which refuses rather
        than waving the write through."""
        self._write(hass, "default_config:\nrecorder:\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\n", "remove_keys": {"recorder": True}},
            _token(), hass)
        assert outcome == "invalid_request"

    async def test_remove_keys_as_bare_string_is_accepted(self, hass):
        """A model that sends a scalar where a list is declared still lands."""
        self._write(hass, "default_config:\nrecorder:\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "default_config:\n", "remove_keys": "recorder"},
            _token(), hass)
        assert outcome == "allowed"

    async def test_executor_recheck_catches_approval_window_drift(self, hass):
        """A key appeared while the approval was pending, so the approved write
        now drops something the admin never saw."""
        self._write(hass, "default_config:\n")
        self._write(hass, "default_config:\nrecorder:\n")
        _, outcome, _ = await _execute_set_yaml_config(
            {"content": "default_config:\n"}, _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "recorder" in self._read(hass)

    async def test_denied_token_learns_nothing_about_its_payload(self, hass):
        """Rule 29(a): the cap-deny check runs first, so a denied token gets the
        uniform Forbidden message and never the removal detail."""
        self._write(hass, "default_config:\nrecorder:\n")
        content, outcome, _ = await _call(
            "set_yaml_config", {"content": "default_config:\n"},
            _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"
        text = content["content"][0]["text"]
        assert "recorder" not in text and "remove_keys" not in text

    async def test_diff_summary_names_the_actual_removals(self, hass):
        """The summary is computed from the content, not from remove_keys, so a
        write that declares one key and drops another cannot misreport itself in
        the History line an admin reads."""
        from custom_components.phoenix_mcp.tools.config_files import (
            _build_diff_set_yaml_config,
        )
        self._write(hass, "default_config:\nrecorder:\nautomation: !include a.yaml\n")
        diff = await _build_diff_set_yaml_config(
            {"content": "default_config:\n", "remove_keys": ["recorder"]},
            _token(), hass)
        assert diff["summary_key"] == "diff.set_yaml_config.removing"
        assert diff["summary_params"]["keys"] == "automation, recorder"

    async def test_restore_may_drop_keys(self, hass):
        """A version restore reproduces a snapshot the admin chose from a panel
        showing its content, so it is exempt. Without the exemption every
        rollback past an added key would be refused."""
        from custom_components.phoenix_mcp.tool_common import _restore_ctx
        self._write(hass, "default_config:\nrecorder:\n")
        ctx = _restore_ctx.set({"user_id": "admin-1"})
        try:
            _, outcome, _ = await _execute_set_yaml_config(
                {"content": "default_config:\n"}, _token(), hass, MagicMock())
        finally:
            _restore_ctx.reset(ctx)
        assert outcome == "allowed"
        assert self._read(hass) == "default_config:\n"

    async def test_diff_summary_plain_when_nothing_is_removed(self, hass):
        from custom_components.phoenix_mcp.tools.config_files import (
            _build_diff_set_yaml_config,
        )
        self._write(hass, "default_config:\n")
        diff = await _build_diff_set_yaml_config(
            {"content": "default_config:\nfrontend:\n"}, _token(), hass)
        assert diff["summary_key"] == "diff.set_yaml_config"


class TestYamlConfigContentHash:
    """Optimistic-concurrency (compare-and-swap) guard on set_yaml_config."""

    async def test_get_reports_content_hash(self, hass):
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default_config:\n")
        content, _, _ = await _call("get_yaml_config", {}, _token(), hass)
        assert _json(content)["content_hash"] == content_hash("default_config:\n")

    async def test_get_absent_reports_hash_of_empty(self, hass):
        content, _, _ = await _call("get_yaml_config", {}, _token(), hass)
        body = _json(content)
        assert body["exists"] is False
        assert body["content_hash"] == content_hash("")

    async def test_matching_hash_writes(self, hass):
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default_config:\n")
        h = _json((await _call("get_yaml_config", {}, _token(), hass))[0])["content_hash"]
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "default_config:\nfrontend:\n", "expected_hash": h}, _token(), hass)
        assert outcome == "allowed"
        with open(path, encoding="utf-8") as f:
            assert f.read() == "default_config:\nfrontend:\n"

    async def test_stale_hash_conflicts_before_gate_and_does_not_write(self, hass):
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("original:\n")
        stale = content_hash("what the agent read earlier")
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "clobber:\n", "expected_hash": stale}, _token(), hass)
        assert outcome == "invalid_request"
        with open(path, encoding="utf-8") as f:
            assert f.read() == "original:\n"  # untouched

    async def test_stale_hash_confirm_mode_fails_before_pending(self, hass):
        # Under confirm mode the stale write must be refused before a pending
        # approval is created, not queue a doomed approval (rule 29).
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("original:\n")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"content": "clobber:\n", "expected_hash": content_hash("stale")},
            _token(cap_yaml_edit="confirm"), hass)
        assert outcome == "invalid_request"

    async def test_executor_stale_hash_conflict_at_apply_time(self, hass):
        # The apply-time guard (approval-window drift), exercised directly on the
        # executor the admin approve path re-runs: the file changed since the read,
        # so the write is refused and nothing is overwritten.
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("current:\n")
        _, outcome, _ = await _execute_set_yaml_config(
            {"content": "new:\n", "expected_hash": content_hash("stale content")},
            _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        with open(path, encoding="utf-8") as f:
            assert f.read() == "current:\n"

    async def test_no_hash_still_writes(self, hass):
        # Back-compat: omitting expected_hash skips the CAS check entirely.
        # The new content keeps x so the removal guard has no opinion here.
        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x:\n")
        _, outcome, _ = await _call("set_yaml_config", {"content": "x:\ny:\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_absent_file_hashes_as_empty(self, hass):
        # An absent configuration.yaml reads as "" (get returns content ""), so
        # expected_hash of "" writes (creates), while any other hash conflicts.
        # The config dir is tmp_path-isolated here, so the file is genuinely absent.
        path = hass.config.path("configuration.yaml")
        assert not __import__("os").path.isfile(path)
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "created:\n", "expected_hash": content_hash("")}, _token(), hass)
        assert outcome == "allowed"

    async def test_absent_file_with_nonempty_hash_conflicts(self, hass):
        path = hass.config.path("configuration.yaml")
        assert not __import__("os").path.isfile(path)
        _, outcome, _ = await _call(
            "set_yaml_config", {"content": "x:\n", "expected_hash": content_hash("stale")}, _token(), hass)
        assert outcome == "invalid_request"
        assert not __import__("os").path.isfile(path)  # not created


class TestIntegrations:
    async def test_list_deny(self, hass):
        _, outcome, _ = await _call("list_integrations", {}, _token(cap_integration_write="deny"), hass)
        assert outcome == "denied"

    async def test_list_excludes_phoenix(self, hass):
        MockConfigEntry(domain="test_integration", title="Test", entry_id="e1").add_to_hass(hass)
        MockConfigEntry(domain="phoenix_mcp", title="Phoenix MCP", entry_id="phoenix_mcp1").add_to_hass(hass)
        content, outcome, _ = await _call(
            "list_integrations", {}, _token(pass_through=True), hass
        )
        assert outcome == "allowed"
        domains = {i["domain"] for i in _json(content)["integrations"]}
        assert "test_integration" in domains
        assert "phoenix_mcp" not in domains

    async def test_list_is_scoped_by_owned_entity_and_safe_projection(self, hass):
        from homeassistant.helpers import entity_registry as er

        visible = MockConfigEntry(
            domain="test_integration",
            title="Visible 192.168.1.2 https://example.invalid/setup",
            entry_id="visible-entry",
            data={"password": "secret", "host": "192.168.1.2"},
            options={"token": "secret"},
        )
        hidden = MockConfigEntry(
            domain="other_integration", title="Hidden", entry_id="hidden-entry"
        )
        visible.add_to_hass(hass)
        hidden.add_to_hass(hass)
        entity = er.async_get(hass).async_get_or_create(
            "light", "test_integration", "visible-light",
            config_entry=visible,
        )
        hass.states.async_set(entity.entity_id, "on")
        token = _token()
        token.permissions.entities[entity.entity_id] = PermissionNode(state="YELLOW")

        content, outcome, _ = await _call("list_integrations", {}, token, hass)

        assert outcome == "allowed"
        result = _json(content)
        assert result["count"] == 1
        item = result["integrations"][0]
        assert item["entry_id"] == visible.entry_id
        assert item["title"] == "Visible <redacted-ip> <redacted-url>"
        assert item["state"] == "not_loaded"
        assert item["enabled"] is True
        assert item["accessible_entity_count"] == 1
        assert item["accessible_device_count"] == 0
        assert set(item) == {
            "entry_id", "domain", "title", "source", "state", "enabled",
            "disabled_by", "setup_failure_reason", "supports_reload",
            "supports_unload", "supports_options", "supports_reconfigure",
            "pref_disable_new_entities", "pref_disable_polling",
            "accessible_entity_count", "accessible_device_count",
        }
        serialized = json.dumps(item)
        assert "secret" not in serialized
        assert "192.168.1.2" not in serialized
        assert hidden.entry_id not in serialized

    async def test_disabled_direct_entity_grant_does_not_reveal_entry(self, hass):
        from homeassistant.helpers import entity_registry as er

        entry = MockConfigEntry(
            domain="test_integration", title="Disabled", entry_id="disabled-entry"
        )
        entry.add_to_hass(hass)
        entity = er.async_get(hass).async_get_or_create(
            "switch", "test_integration", "disabled-switch",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        token = _token()
        token.permissions.entities[entity.entity_id] = PermissionNode(state="GREEN")
        content, outcome, _ = await _call("list_integrations", {}, token, hass)
        assert outcome == "allowed"
        assert _json(content)["integrations"] == []

    async def test_disable_calls_ha(self, hass):
        entry = MockConfigEntry(domain="test_integration", entry_id="e2")
        entry.add_to_hass(hass)
        with patch_set_disabled(hass) as mock:
            content, outcome, _ = await _call(
                "set_integration_enabled", {"entry_id": "e2", "enabled": False}, _token(), hass)
        assert outcome == "allowed"
        mock.assert_awaited_once()
        assert mock.await_args.args[1] == ConfigEntryDisabler.USER

    async def test_enable_passes_none(self, hass):
        entry = MockConfigEntry(domain="test_integration", entry_id="e3")
        entry.add_to_hass(hass)
        with patch_set_disabled(hass) as mock:
            _, outcome, _ = await _call(
                "set_integration_enabled", {"entry_id": "e3", "enabled": True}, _token(), hass)
        assert outcome == "allowed"
        assert mock.await_args.args[1] is None

    async def test_unknown_entry(self, hass):
        _, outcome, _ = await _call("set_integration_enabled", {"entry_id": "nope", "enabled": True}, _token(), hass)
        assert outcome == "not_found"

    async def test_phoenix_entry_refused(self, hass):
        MockConfigEntry(domain="phoenix_mcp", entry_id="phoenix_mcp2").add_to_hass(hass)
        _, outcome, _ = await _call("set_integration_enabled", {"entry_id": "phoenix_mcp2", "enabled": False}, _token(), hass)
        assert outcome == "not_found"

    async def test_non_bool(self, hass):
        MockConfigEntry(domain="test_integration", entry_id="e4").add_to_hass(hass)
        _, outcome, _ = await _call("set_integration_enabled", {"entry_id": "e4", "enabled": "yes"}, _token(), hass)
        assert outcome == "invalid_request"


def patch_set_disabled(hass):
    from unittest.mock import patch
    return patch.object(hass.config_entries, "async_set_disabled_by", new=AsyncMock(return_value=True))


class TestConfigYamlSerialization:
    """set_yaml_config is a whole-file replace, so it must hold a lock.

    Without one, two concurrent writers lose an edit outright and the
    protected-subtree re-check validates against content another writer can
    replace before the write lands.
    """

    async def test_executor_holds_the_config_yaml_lock(self, hass):
        import asyncio

        from custom_components.phoenix_mcp.tools.config_files import _get_yaml_file_lock

        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default_config:\n")

        lock = _get_yaml_file_lock(hass, "configuration.yaml")
        await lock.acquire()
        task = asyncio.create_task(
            _execute_set_yaml_config(
                {"content": "default_config:\nfrontend:\n"}, _token(), hass, MagicMock(),
            )
        )
        try:
            # A real bounded wait, not a handful of sleep(0) turns: the executor
            # job for the initial isfile probe does not settle within a few event
            # loop cycles either, so a turn-counting version passes with the lock
            # removed and proves nothing.
            done, _pending = await asyncio.wait([task], timeout=0.25)
            assert not done, "executor ran while the config yaml lock was held"
            with open(path, encoding="utf-8") as f:
                assert f.read() == "default_config:\n"
        finally:
            lock.release()
        _, outcome, _ = await task
        assert outcome == "allowed"
        with open(path, encoding="utf-8") as f:
            assert "frontend:" in f.read()

    async def test_write_happens_while_the_lock_is_held(self, hass, monkeypatch):
        """The write itself must land inside the critical section.

        Asserted at the write rather than around it: a lock acquired and
        released before the write would still leave the lost-update window open,
        and only observing lock state at the moment of the write rules that out.
        """
        import asyncio

        # Patch the module that CALLS it: the write and the lock both live in
        # tools/config_files.py, so mcp_view's binding is not what runs.
        from custom_components.phoenix_mcp.tools import config_files

        path = hass.config.path("configuration.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("a: 0\n")

        seen: list[bool] = []
        real_write = config_files._write_utf8_file_atomic

        def _checking_write(target, content):
            seen.append(config_files._get_yaml_file_lock(hass, "configuration.yaml").locked())
            return real_write(target, content)

        monkeypatch.setattr(config_files, "_write_utf8_file_atomic", _checking_write)

        # Both writers keep the same top-level key set and differ only in its
        # value: whichever runs second would otherwise be dropping the other's
        # key and be refused by the removal guard, which is a real refusal but
        # not what this test is about.
        await asyncio.gather(
            _execute_set_yaml_config({"content": "a: 1\n"}, _token(), hass, MagicMock()),
            _execute_set_yaml_config({"content": "a: 2\n"}, _token(), hass, MagicMock()),
        )

        assert seen == [True, True]
        # Whole-file replace: the survivor is one writer's content intact, never
        # a blend of the two.
        with open(path, encoding="utf-8") as f:
            assert f.read() in ("a: 1\n", "a: 2\n")


class TestSetYamlConfigTargetsIncludeFiles:
    """set_yaml_config writes any file configuration.yaml loads at a safe key.

    The write jail used to be configuration.yaml alone while the READ jail took
    a file argument, so an agent that read templates.yaml had no way to write it
    back, and a `file` argument sent to this tool was silently dropped and the
    content validated against configuration.yaml instead.
    """

    USER_CONFIG = (
        "automation: !include automations.yaml\n"
        "script: !include scripts.yaml\n"
        "scene: !include scenes.yaml\n"
        "sensor: !include sensors.yaml\n"
        "template: !include templates.yaml\n"
    )

    def _seed(self, tmp_path, config=None):
        (tmp_path / "configuration.yaml").write_text(
            self.USER_CONFIG if config is None else config, encoding="utf-8")

    async def test_writes_an_included_file(self, hass, tmp_path):
        self._seed(tmp_path)
        (tmp_path / "templates.yaml").write_text("- binary_sensor: []\n", encoding="utf-8")
        new = "- binary_sensor:\n    - name: Dark\n      state: 'on'\n"
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "templates.yaml", "content": new}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["path"] == "templates.yaml"
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == new
        # The file it was NOT aimed at is untouched.
        assert (tmp_path / "configuration.yaml").read_text(encoding="utf-8") == self.USER_CONFIG

    @pytest.mark.parametrize("rel", [
        "automations.yaml", "scripts.yaml", "scenes.yaml", "sensors.yaml", "templates.yaml",
    ])
    async def test_every_include_target_is_writable(self, hass, tmp_path, rel):
        self._seed(tmp_path)
        (tmp_path / rel).write_text("[]\n", encoding="utf-8")
        _, outcome, _ = await _call(
            "set_yaml_config", {"file": rel, "content": "[]\n"}, _token(), hass)
        assert outcome == "allowed"

    async def test_default_still_writes_configuration_yaml(self, hass, tmp_path):
        self._seed(tmp_path, "default_config:\n")
        content, outcome, _ = await _call(
            "set_yaml_config", {"content": "default_config:\nfrontend:\n"}, _token(), hass)
        assert outcome == "allowed"
        assert _json(content)["path"] == "configuration.yaml"

    async def test_refuses_a_file_nothing_loads(self, hass, tmp_path):
        self._seed(tmp_path)
        (tmp_path / "notes.yaml").write_text("a: 1\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "notes.yaml", "content": "a: 2\n"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "does not load notes.yaml" in content["content"][0]["text"]
        assert (tmp_path / "notes.yaml").read_text(encoding="utf-8") == "a: 1\n"

    async def test_refuses_a_file_loaded_under_a_protected_key(self, hass, tmp_path):
        """The trust-boundary floor follows the key, not the filename.

        http.yaml's own top level IS http:'s value, so trusted_proxies sits
        there as an ordinary-looking key and the content-shaped rule-30 check
        would never fire on it.
        """
        self._seed(tmp_path, "http: !include http.yaml\n")
        (tmp_path / "http.yaml").write_text("trusted_proxies:\n  - 10.0.0.1\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "http.yaml", "content": "trusted_proxies:\n  - 0.0.0.0/0\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "under http" in content["content"][0]["text"]
        assert "0.0.0.0/0" not in (tmp_path / "http.yaml").read_text(encoding="utf-8")

    async def test_refuses_a_packages_file(self, hass, tmp_path):
        self._seed(tmp_path, "homeassistant:\n  packages: !include_dir_named packages\n")
        (tmp_path / "packages").mkdir()
        (tmp_path / "packages/kitchen.yaml").write_text("light: []\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "packages/kitchen.yaml", "content": "frontend:\n  extra_module_url:\n    - /local/x.js\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "under homeassistant" in content["content"][0]["text"]

    async def test_refuses_a_new_file_in_a_packages_directory(self, hass, tmp_path):
        """Containment, not enumeration: the file does not exist yet."""
        self._seed(tmp_path, "homeassistant:\n  packages: !include_dir_named packages\n")
        (tmp_path / "packages").mkdir()
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "packages/new.yaml", "content": "light: []\n"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_refuses_when_a_protected_branch_cannot_be_read(self, hass, tmp_path):
        self._seed(tmp_path, "http: !include http.yaml\ntemplate: !include templates.yaml\n")
        (tmp_path / "http.yaml").write_text("{{{ not yaml", encoding="utf-8")
        (tmp_path / "templates.yaml").write_text("[]\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "templates.yaml", "content": "[]\n"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "could not be read" in content["content"][0]["text"]

    async def test_refuses_when_configuration_yaml_is_unparseable(self, hass, tmp_path):
        self._seed(tmp_path, "{{{ not yaml")
        (tmp_path / "templates.yaml").write_text("[]\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "templates.yaml", "content": "[]\n"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "cannot tell where templates.yaml is loaded" in content["content"][0]["text"]

    @pytest.mark.parametrize("bad", [
        "secrets.yaml", "../outside.yaml", "/etc/passwd", ".storage/core.entity_registry",
        "templates.txt",
    ])
    async def test_path_jail(self, hass, tmp_path, bad):
        self._seed(tmp_path)
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": bad, "content": "x\n"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "can be written" in content["content"][0]["text"]

    async def test_denied_token_learns_nothing_about_its_payload(self, hass, tmp_path):
        """Rule 29(a): the cap check runs before every path and mount refusal."""
        self._seed(tmp_path)
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "../escape.yaml", "content": "x\n"},
            _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"
        assert "escape.yaml" not in content["content"][0]["text"]

    async def test_executor_rechecks_the_mount_at_apply_time(self, hass, tmp_path):
        """configuration.yaml can be re-routed while an approval waits."""
        self._seed(tmp_path)
        (tmp_path / "templates.yaml").write_text("[]\n", encoding="utf-8")
        # Re-route the file under a protected key after the gate would have run.
        self._seed(tmp_path, "http: !include templates.yaml\n")
        content, outcome, _ = await _execute_set_yaml_config(
            {"file": "templates.yaml", "content": "trusted_proxies: []\n"},
            _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "under http" in content["content"][0]["text"]
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == "[]\n"

    async def test_version_record_names_the_file(self, hass, tmp_path):
        self._seed(tmp_path)
        (tmp_path / "templates.yaml").write_text("[]\n", encoding="utf-8")
        data = MagicMock()
        data.versions.record = AsyncMock()
        _, outcome, _ = await _execute_set_yaml_config(
            {"file": "templates.yaml", "content": "- a\n"}, _token(), hass, data)
        assert outcome == "allowed"
        record = data.versions.record.await_args.kwargs
        assert record["resource_type"] == "yaml_config"
        assert record["resource_id"] == "templates.yaml"

    async def test_expected_hash_applies_to_the_named_file(self, hass, tmp_path):
        self._seed(tmp_path)
        (tmp_path / "templates.yaml").write_text("- a\n", encoding="utf-8")
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "templates.yaml", "content": "- b\n", "expected_hash": content_hash("- stale\n")},
            _token(), hass)
        assert outcome == "invalid_request"
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == "- a\n"


class TestPatchYamlConfigTargetsIncludeFiles:
    """patch_yaml_config addresses a key or list entry in any writable file.

    Same jail and same mount gate as set_yaml_config: a file is patchable only
    where it is patchable as a whole.
    """

    async def test_patches_an_entry_in_an_include_target(self, hass, tmp_path):
        (tmp_path / "configuration.yaml").write_text(
            "template: !include templates.yaml\n", encoding="utf-8")
        (tmp_path / "templates.yaml").write_text(
            "- binary_sensor:\n    - name: 'Dark'\n      state: 'off'\n"
            "- sensor:\n    - name: 'Outside'\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "patch_yaml_config",
            {"file": "templates.yaml", "path": [0, "binary_sensor", 0, "state"],
             "content": "'on'"},
            _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["path"] == "templates.yaml"
        assert body["key"] == "[0].binary_sensor[0].state"
        written = (tmp_path / "templates.yaml").read_text(encoding="utf-8")
        assert "state: 'on'" in written
        # The entry the patch never addressed was never resent, so it is intact.
        assert "name: 'Outside'" in written

    async def test_refuses_a_file_loaded_under_a_protected_key(self, hass, tmp_path):
        (tmp_path / "configuration.yaml").write_text(
            "http: !include http.yaml\n", encoding="utf-8")
        (tmp_path / "http.yaml").write_text("server_port: 8123\n", encoding="utf-8")
        content, outcome, _ = await _call(
            "patch_yaml_config",
            {"file": "http.yaml", "key": "server_port", "content": "80"}, _token(), hass)
        assert outcome == "invalid_request"
        assert "under http" in content["content"][0]["text"]
        assert "8123" in (tmp_path / "http.yaml").read_text(encoding="utf-8")

    async def test_refuses_a_file_nothing_loads(self, hass, tmp_path):
        (tmp_path / "configuration.yaml").write_text("default_config:\n", encoding="utf-8")
        (tmp_path / "notes.yaml").write_text("a: 1\n", encoding="utf-8")
        _, outcome, _ = await _call(
            "patch_yaml_config",
            {"file": "notes.yaml", "key": "a", "content": "2"}, _token(), hass)
        assert outcome == "invalid_request"

    async def test_executor_rechecks_the_mount_at_apply_time(self, hass, tmp_path):
        (tmp_path / "templates.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / "configuration.yaml").write_text(
            "http: !include templates.yaml\n", encoding="utf-8")
        content, outcome, _ = await _execute_patch_yaml_config(
            {"file": "templates.yaml", "key": "a", "content": "2"},
            _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert "under http" in content["content"][0]["text"]
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == "a: 1\n"

    async def test_version_record_names_the_file(self, hass, tmp_path):
        (tmp_path / "configuration.yaml").write_text(
            "template: !include templates.yaml\n", encoding="utf-8")
        (tmp_path / "templates.yaml").write_text("- a: 1\n", encoding="utf-8")
        data = MagicMock()
        data.versions.record = AsyncMock()
        _, outcome, _ = await _execute_patch_yaml_config(
            {"file": "templates.yaml", "path": [0, "a"], "content": "2"},
            _token(), hass, data)
        assert outcome == "allowed"
        assert data.versions.record.await_args.kwargs["resource_id"] == "templates.yaml"

    async def test_denied_token_learns_nothing_about_its_payload(self, hass, tmp_path):
        content, outcome, _ = await _call(
            "patch_yaml_config", {"file": "../escape.yaml", "key": "x", "content": "1"},
            _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"
        assert "escape.yaml" not in content["content"][0]["text"]


class TestIncludeTargetRoundTrip:
    """The reported workflow, end to end: read an include target, edit, write back.

    A model reaches for expected_hash from the read, so the two tools have to
    agree on both the path and the hash for a real edit to land.
    """

    BEFORE = (
        "- binary_sensor:\n"
        "    - name: 'Dark'\n"
        "      unique_id: dark\n"
        "      device_class: light\n"
        "      state: >\n"
        "        {{ is_state('sun.sun', 'below_horizon') }}\n"
    )
    AFTER = (
        "- binary_sensor:\n"
        "    - name: 'Dark'\n"
        "      unique_id: dark\n"
        "      device_class: light\n"
        "      state: >\n"
        "        {% set lux = states('sensor.lux') | float(-1) %}\n"
        "        {{ is_state('sun.sun', 'below_horizon') or (lux >= 0 and lux < 2900) }}\n"
    )

    async def test_read_edit_write(self, hass, tmp_path):
        (tmp_path / "configuration.yaml").write_text(
            "template: !include templates.yaml\n", encoding="utf-8")
        (tmp_path / "templates.yaml").write_text(self.BEFORE, encoding="utf-8")
        token = _token()

        content, outcome, _ = await _call(
            "get_yaml_config", {"file": "templates.yaml"}, token, hass)
        assert outcome == "allowed"
        read = _json(content)
        assert read["content"] == self.BEFORE

        content, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "templates.yaml", "content": self.AFTER,
             "expected_hash": read["content_hash"]},
            token, hass)
        assert outcome == "allowed"
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == self.AFTER


class TestListEntryRemovalGuard:
    """The rule-31 backstop for a file whose top level is a LIST.

    The mapping-key guard abstains there (no keys to compare), so a
    read-modify-write returning 3 of 12 template blocks was accepted. Counting
    is the whole mechanism: list entries have no stable identity, so an edit and
    a delete-plus-add are indistinguishable and only the net matters.
    """

    THREE = "- a: 1\n- b: 2\n- c: 3\n"

    def _seed(self, tmp_path):
        (tmp_path / "configuration.yaml").write_text(
            "template: !include templates.yaml\n", encoding="utf-8")
        (tmp_path / "templates.yaml").write_text(self.THREE, encoding="utf-8")

    async def test_an_undeclared_drop_is_refused(self, hass, tmp_path):
        self._seed(tmp_path)
        content, outcome, _ = await _call(
            "set_yaml_config", {"file": "templates.yaml", "content": "- a: 1\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        text = content["content"][0]["text"]
        assert "removes 2 top-level entries" in text
        assert "remove_entries: 2" in text
        assert "patch_yaml_config" in text
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == self.THREE

    async def test_a_declared_drop_is_allowed(self, hass, tmp_path):
        self._seed(tmp_path)
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "templates.yaml", "content": "- a: 1\n", "remove_entries": 2},
            _token(), hass)
        assert outcome == "allowed"
        assert (tmp_path / "templates.yaml").read_text(encoding="utf-8") == "- a: 1\n"

    @pytest.mark.parametrize("declared", [1, 3, True, "2", None, -2])
    async def test_a_wrong_or_wrong_shaped_declaration_still_refuses(
        self, hass, tmp_path, declared
    ):
        """Degrade-to-absent: a value that is not the exact count refuses."""
        self._seed(tmp_path)
        args = {"file": "templates.yaml", "content": "- a: 1\n"}
        if declared is not None:
            args["remove_entries"] = declared
        _, outcome, _ = await _call("set_yaml_config", args, _token(), hass)
        assert outcome == "invalid_request"

    async def test_an_edit_that_keeps_the_count_needs_no_declaration(self, hass, tmp_path):
        self._seed(tmp_path)
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "templates.yaml", "content": "- a: 9\n- b: 2\n- c: 3\n"},
            _token(), hass)
        assert outcome == "allowed"

    async def test_growing_the_file_needs_no_declaration(self, hass, tmp_path):
        self._seed(tmp_path)
        _, outcome, _ = await _call(
            "set_yaml_config",
            {"file": "templates.yaml", "content": self.THREE + "- d: 4\n"},
            _token(), hass)
        assert outcome == "allowed"

    async def test_a_restore_is_exempt(self, hass, tmp_path):
        """Reproducing an admin-chosen snapshot legitimately drops later entries."""
        from custom_components.phoenix_mcp.tool_common import _restore_ctx

        self._seed(tmp_path)
        token = _token()
        marker = _restore_ctx.set({"user_id": "admin"})
        try:
            _, outcome, _ = await _execute_set_yaml_config(
                {"file": "templates.yaml", "content": "- a: 1\n"}, token, hass, MagicMock())
        finally:
            _restore_ctx.reset(marker)
        assert outcome == "allowed"

    @pytest.mark.parametrize("count,fragment", [(1, "removing_entry"), (2, "removing_entries")])
    async def test_the_diff_summary_reports_the_drop(self, hass, tmp_path, count, fragment):
        """The History line must say what the write drops, not just that it wrote."""
        from custom_components.phoenix_mcp.tools.config_files import (
            _build_diff_set_yaml_config,
        )

        self._seed(tmp_path)
        kept = "".join(f"- {name}: {n}\n" for n, name in list(enumerate("abc", 1))[: 3 - count])
        diff = await _build_diff_set_yaml_config(
            {"file": "templates.yaml", "content": kept}, _token(), hass)
        assert diff["summary_key"] == f"diff.set_yaml_config.{fragment}"
        assert diff["summary_params"]["file"] == "templates.yaml"
        assert diff["target"]["id"] == "templates.yaml"
