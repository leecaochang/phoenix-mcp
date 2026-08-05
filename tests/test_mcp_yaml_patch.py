"""Tests for patch_yaml_config and the yaml_patch splice engine behind it.

The property the whole feature rests on is that a patch changes ONE key and
nothing else: not the comments, not the ordering, not the spacing, and not the
approval an operator has to read. So the assertions here are mostly about what
did NOT move, which is the opposite of how the whole-file tool is tested.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp import yaml_patch
from custom_components.phoenix_mcp.helpers import content_hash
from custom_components.phoenix_mcp.mcp_view import _EXECUTOR_REGISTRY, _call_tool
from custom_components.phoenix_mcp.tools.config_files import (
    _build_diff_patch_yaml_config,
    _execute_patch_yaml_config,
)
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord

# A configuration.yaml with the shapes that matter: a comment tied to the key
# below it, a comment separated from one by a blank line, nesting, a custom tag,
# and a footer.
SAMPLE = """\
# Loads default set of integrations. Do not remove.
default_config:

# Text to speech
tts:
  - platform: google_translate

recorder:
  purge_keep_days: 10
  exclude:
    entity_globs:
      - sensor.*_energy_*

# Trailing footer comment
automation: !include automations.yaml
"""


@pytest.fixture(autouse=True)
def isolated_config_dir(hass, tmp_path):
    """These tests write configuration.yaml; keep it out of the shared config dir."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _token(**caps) -> TokenRecord:
    base = {"cap_yaml_edit": "allow"}
    base.update(caps)
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x",
        created_at=utcnow(), created_by="u", permissions=PermissionTree(), **base,
    )


def _write(hass, text=SAMPLE):
    with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as f:
        f.write(text)


def _read(hass):
    with open(hass.config.path("configuration.yaml"), encoding="utf-8") as f:
        return f.read()


def _json(content: dict) -> dict:
    return json.loads(content["content"][0]["text"])


async def _call(args, token, hass, data=None):
    return await _call_tool("patch_yaml_config", args, token, hass, data or MagicMock())


def _patch(text, key, op="set", content=None, path=None):
    value = yaml_patch.parse_value(content) if op == "set" else None
    return yaml_patch.apply_patch(text, key, op, content, value, path)


class TestSpliceEngine:
    """The pure layer: what a patch changes, and what it leaves alone."""

    def test_only_the_addressed_lines_change(self):
        out = _patch(SAMPLE, "recorder.purge_keep_days", content="30").text
        assert out == SAMPLE.replace("purge_keep_days: 10", "purge_keep_days: 30")

    def test_every_comment_survives_an_edit(self):
        out = _patch(SAMPLE, "recorder", content="purge_keep_days: 3\n").text
        for comment in ("# Loads default", "# Text to speech", "# Trailing footer"):
            assert comment in out

    def test_a_new_nested_key_lands_under_its_parent(self):
        out = _patch(
            SAMPLE, "recorder.include", content="entities:\n  - sensor.grid_import\n"
        ).text
        assert "  include:\n    entities:\n      - sensor.grid_import\n" in out
        assert "  purge_keep_days: 10\n" in out
        assert "# Trailing footer comment" in out

    def test_a_new_top_level_key_is_appended(self):
        out = _patch(SAMPLE, "logger", content="default: warning\n").text
        assert out.startswith(SAMPLE.rstrip("\n").rsplit("\n", 1)[0])
        assert out.endswith("logger:\n  default: warning\n")

    def test_content_is_reindented_to_the_key_depth(self):
        """Content copied out of a file at some other depth still lands right."""
        flush = _patch(SAMPLE, "recorder.include", content="entities:\n  - sensor.a\n").text
        indented = _patch(
            SAMPLE, "recorder.include", content="    entities:\n      - sensor.a\n"
        ).text
        assert flush == indented

    def test_a_list_value_is_never_emitted_inline(self):
        """'key: - a' is not valid YAML, so a one-line sequence goes to block form."""
        out = _patch(SAMPLE, "tts", content="- platform: piper").text
        assert "tts:\n  - platform: piper\n" in out

    def test_a_tagged_scalar_round_trips(self):
        out = _patch(SAMPLE, "automation", content="!include_dir_list automations/").text
        assert "automation: !include_dir_list automations/" in out

    def test_removal_takes_the_comment_written_above_the_key(self):
        out = _patch(SAMPLE, "tts", op="remove").text
        assert "tts:" not in out
        assert "# Text to speech" not in out
        # ...and leaves the ones that belong to other keys.
        assert "# Loads default" in out and "# Trailing footer" in out

    def test_removal_leaves_a_comment_separated_by_a_blank_line_alone(self):
        text = "a: 1\n\n# section header\n\nb: 2\n"
        out = _patch(text, "b", op="remove").text
        assert "# section header" in out
        assert "b:" not in out

    def test_removing_the_only_key_empties_the_file(self):
        assert _patch("a: 1\n", "a", op="remove").text == ""

    def test_before_is_the_value_not_the_pair(self):
        """Both sides of the diff must describe the same thing."""
        assert _patch(SAMPLE, "recorder.purge_keep_days", content="30").before == "10"
        assert _patch(SAMPLE, "tts", content="- platform: piper").before == (
            "- platform: google_translate"
        )
        assert _patch(SAMPLE, "logger", content="default: info\n").before is None

    def test_an_empty_file_accepts_a_top_level_key(self):
        assert _patch("", "recorder", content="purge_keep_days: 5").text == (
            "recorder:\n  purge_keep_days: 5\n"
        )

    def test_a_missing_trailing_newline_is_restored(self):
        assert _patch("a: 1", "b", content="2").text == "a: 1\nb: 2\n"

    def test_a_duplicate_key_is_read_the_way_pyyaml_reads_it(self):
        """PyYAML keeps the last of a duplicated key, so that is the one edited."""
        assert _patch("a: 1\nb: 2\na: 3\n", "a", content="9").text == "a: 1\nb: 2\na: 9\n"

    @pytest.mark.parametrize("key,op,content,fragment", [
        ("recorder.include.entities", "set", "- a", "does not exist"),
        ("recorder.purge_keep_days.deeper", "set", "1", "does not hold a mapping"),
        ("absent", "remove", None, "nothing to remove"),
        ("", "set", "1", "dotted path"),
        ("recorder..include", "set", "1", "empty path segment"),
        ("recorder", "set", "", "non-empty YAML string"),
        ("recorder", "set", "a: [unclosed", "not valid YAML"),
    ])
    def test_refusals(self, key, op, content, fragment):
        with pytest.raises(yaml_patch.PatchError, match=fragment):
            _patch(SAMPLE, key, op=op, content=content)

    def test_a_flow_style_mapping_is_refused_rather_than_guessed_at(self):
        """{...} on one line has no per-key line span to splice into."""
        with pytest.raises(yaml_patch.PatchError, match="flow style"):
            _patch("http: {server_port: 8123}\n", "http.server_port", content="80")

    def test_a_mapping_key_against_a_list_document_is_refused(self):
        """A dotted key addresses mapping keys; a list top level needs an index."""
        with pytest.raises(yaml_patch.PatchError, match="does not hold a mapping of keys"):
            _patch("- one\n- two\n", "one", content="1")

    def test_an_index_against_a_mapping_document_is_refused(self):
        with pytest.raises(yaml_patch.PatchError, match="does not hold a list of entries"):
            _patch("a: 1\n", None, content="2", path=[0])

    def test_a_scalar_document_is_refused(self):
        with pytest.raises(yaml_patch.PatchError, match="neither a mapping of keys nor a list"):
            _patch("just a string\n", "a", content="1")

    def test_an_unparseable_file_is_refused_before_anything_is_located(self):
        with pytest.raises(yaml_patch.PatchError, match="not valid YAML"):
            _patch("a: [unclosed\n", "a", content="1")

    def test_an_unknown_custom_tag_does_not_make_the_file_unpatchable(self):
        text = "vendor: !custom_thing whatever\nrecorder:\n  purge_keep_days: 1\n"
        out = _patch(text, "recorder.purge_keep_days", content="2").text
        assert "!custom_thing whatever" in out and "purge_keep_days: 2" in out

    def test_the_self_check_refuses_a_result_that_is_not_what_was_asked_for(self, monkeypatch):
        """A span bug must surface as a refusal, not as a mangled file.

        Mutating the renderer is the only way in: every real path through
        _render is correct, and the point of the check is to catch the one that
        is not yet known to be. This mutation still PARSES, which is what makes
        it the interesting half: a wrong value is otherwise invisible.
        """
        monkeypatch.setattr(
            yaml_patch, "_render",
            lambda key, content, indent, value, prefix="": f"{' ' * indent}{key}: 999\n",
        )
        with pytest.raises(yaml_patch.PatchError, match="did not come out as intended"):
            _patch(SAMPLE, "recorder.purge_keep_days", content="30")

    def test_the_self_check_refuses_a_result_that_no_longer_parses(self, monkeypatch):
        monkeypatch.setattr(
            yaml_patch, "_render", lambda key, content, indent, value, prefix="": "wrong: 1\n"
        )
        with pytest.raises(yaml_patch.PatchError, match="would not be valid YAML"):
            _patch(SAMPLE, "recorder.purge_keep_days", content="30")


class TestPatchTool:
    def test_registered_as_an_executor(self):
        assert "patch_yaml_config" in _EXECUTOR_REGISTRY

    async def test_a_patch_writes_only_the_addressed_key(self, hass):
        _write(hass)
        content, outcome, _ = await _call(
            {"key": "recorder.purge_keep_days", "content": "30"}, _token(), hass)
        assert outcome == "allowed"
        body = _json(content)
        assert body["key"] == "recorder.purge_keep_days" and body["op"] == "set"
        assert _read(hass) == SAMPLE.replace("purge_keep_days: 10", "purge_keep_days: 30")

    async def test_the_returned_hash_chains_to_the_next_patch(self, hass):
        _write(hass)
        content, _, _ = await _call({"key": "logger", "content": "default: info\n"}, _token(), hass)
        chained = _json(content)["content_hash"]
        assert chained == content_hash(_read(hass))
        _, outcome, _ = await _call(
            {"key": "recorder.purge_keep_days", "content": "1", "expected_hash": chained},
            _token(), hass)
        assert outcome == "allowed"

    @pytest.mark.parametrize("key", ["recorder", "recorder.exclude", "tts", "automation"])
    async def test_a_read_of_a_key_round_trips_as_the_patch_content(self, hass, key):
        """The docs and the tool description both tell an agent that
        get_yaml_config's fragment for a key IS the shape `content` takes, so
        reading a key and writing it straight back must change nothing.

        Semantically, not byte-for-byte: the READ re-dumps the fragment in
        PyYAML's style, so a value whose file layout differs from that style
        (a block sequence indented under its key) comes back reformatted. The
        patch splices the caller's text verbatim, so this is a property of the
        read, and it is why the guidance is to address the narrowest key you are
        actually changing.
        """
        _write(hass)
        content, outcome, _ = await _call_tool(
            "get_yaml_config", {"key": key}, _token(), hass, MagicMock())
        assert outcome == "allowed"
        fragment = _json(content)["content"]
        _, outcome, _ = await _call({"key": key, "content": fragment}, _token(), hass)
        assert outcome == "allowed"
        after = _read(hass)
        assert yaml_patch.load_tagged_lenient(after) == yaml_patch.load_tagged_lenient(SAMPLE)
        # Whatever the read's style did to the addressed key, nothing else moved.
        for untouched in ("# Loads default set of integrations. Do not remove.",
                          "# Trailing footer comment", "automation: !include automations.yaml"):
            assert untouched in after

    async def test_addressing_the_leaf_you_change_is_byte_identical_elsewhere(self, hass):
        """The narrow-key path, which is the one the guidance points at."""
        _write(hass)
        _, outcome, _ = await _call(
            {"key": "recorder.exclude.entity_globs", "content": "- sensor.*_debug_*"},
            _token(), hass)
        assert outcome == "allowed"
        assert _read(hass) == SAMPLE.replace("sensor.*_energy_*", "sensor.*_debug_*")

    async def test_deny_returns_forbidden_without_echoing_the_payload(self, hass):
        """Rule 29(a): the cap-deny check runs before any validation of the input."""
        _write(hass)
        content, outcome, _ = await _call(
            {"key": "recorder.nonsense.deep", "content": "!!!"}, _token(cap_yaml_edit="deny"), hass)
        assert outcome == "denied"
        text = content["content"][0]["text"]
        assert "nonsense" not in text and "does not exist" not in text

    async def test_a_bad_key_is_refused_before_an_approval_exists(self, hass):
        """Rule 29: a doomed patch must not become a pending approval."""
        _write(hass)
        data = MagicMock()
        data.store.get_pending_approvals.return_value = []
        _, outcome, _ = await _call(
            {"key": "recorder.include.entities", "content": "- sensor.a"},
            _token(cap_yaml_edit="confirm"), hass, data)
        assert outcome == "invalid_request"
        data.store.set_pending_approvals.assert_not_called()

    async def test_a_protected_subtree_cannot_be_patched(self, hass):
        """Rule 30 applies to the resulting file however it was produced."""
        _write(hass, "http:\n  server_port: 8123\n")
        content, outcome, _ = await _call(
            {"key": "http", "content": "server_port: 8123\ntrusted_proxies:\n  - 0.0.0.0/0\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "trusted_proxies" in content["content"][0]["text"]
        assert "trusted_proxies" not in _read(hass)

    async def test_a_top_level_removal_declares_itself(self, hass):
        """Rule 31's declaration IS the key argument here, so no remove_keys."""
        _write(hass)
        _, outcome, _ = await _call({"key": "tts", "op": "remove"}, _token(), hass)
        assert outcome == "allowed"
        out = _read(hass)
        assert "tts:" not in out
        assert "recorder:" in out and "automation: !include automations.yaml" in out

    async def test_a_redacted_value_is_refused(self, hass):
        """Rule 32: a placeholder carried across from a lossy read."""
        _write(hass)
        content, outcome, _ = await _call(
            {"key": "recorder.include", "content": "entities:\n  - <redacted>\n"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert "<redacted>" in content["content"][0]["text"]

    async def test_a_stale_hash_is_refused_pre_gate(self, hass):
        _write(hass)
        _, outcome, _ = await _call(
            {"key": "recorder.purge_keep_days", "content": "30", "expected_hash": "stale"},
            _token(), hass)
        assert outcome == "invalid_request"
        assert _read(hass) == SAMPLE

    async def test_the_executor_recheck_catches_approval_window_drift(self, hass):
        """The addressed key moved out from under the patch while it waited."""
        _write(hass)
        args = {"key": "recorder.include", "content": "entities:\n  - sensor.a\n"}
        _write(hass, "default_config:\n")  # recorder is gone by approval time
        _, outcome, _ = await _execute_patch_yaml_config(args, _token(), hass, MagicMock())
        assert outcome == "invalid_request"
        assert _read(hass) == "default_config:\n"

    async def test_the_version_snapshot_is_the_whole_file(self, hass, monkeypatch):
        """A patch is a partial write but a restorable version is not: the
        snapshot has to reproduce configuration.yaml, not one key of it."""
        _write(hass)
        captured = {}

        async def _capture(data, token, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "custom_components.phoenix_mcp.tools.config_files._record_version", _capture)
        await _call({"key": "recorder.purge_keep_days", "content": "30"}, _token(), hass)
        assert captured["resource_type"] == "yaml_config"
        assert captured["before"]["content"] == SAMPLE
        assert captured["after"]["content"] == _read(hass)
        assert "purge_keep_days" in captured["summary"]["summary"]


class TestPatchDiff:
    """The approval an operator reads is the key, not the file."""

    async def test_the_diff_is_scoped_to_the_addressed_key(self, hass):
        _write(hass)
        diff = await _build_diff_patch_yaml_config(
            {"key": "recorder.purge_keep_days", "content": "30"}, _token(), hass)
        assert diff["before"] == "10"
        assert diff["after"] == "30"
        # The rest of the file is nowhere in what the operator is shown.
        assert "google_translate" not in json.dumps(diff)
        assert "recorder.purge_keep_days" in diff["summary"]

    async def test_a_new_key_has_no_before_side(self, hass):
        _write(hass)
        diff = await _build_diff_patch_yaml_config(
            {"key": "logger", "content": "default: warning\n"}, _token(), hass)
        assert diff["before"] is None
        assert diff["after"] == "default: warning\n"

    async def test_a_removal_has_no_after_side(self, hass):
        _write(hass)
        diff = await _build_diff_patch_yaml_config({"key": "tts", "op": "remove"}, _token(), hass)
        assert diff["after"] is None
        assert diff["before"] == "- platform: google_translate"
        assert "Remove" in diff["summary"]

    async def test_a_secret_in_the_new_value_is_masked_in_the_diff(self, hass):
        _write(hass, "recorder:\n  purge_keep_days: 1\n")
        diff = await _build_diff_patch_yaml_config(
            {"key": "recorder", "content": "db_url: mysql://u:hunter2@h/db\npassword: hunter2\n"},
            _token(), hass)
        assert "hunter2" not in json.dumps(diff)


# A templates.yaml: a top-level LIST, which a dotted key cannot address at all.
TEMPLATES = """\
# Darkness detection
- binary_sensor:
    - name: 'Dark'
      unique_id: dark
      device_class: light
      state: >
        {{ is_state('sun.sun', 'below_horizon') }}

- sensor:
    - name: 'Outside'
      unique_id: outside
      state: "{{ states('sensor.outdoor_temp') }}"
"""


class TestListAddressing:
    """Addressing a list entry, which is what an !include target usually is.

    The point of these is not that the edit lands: it is that nothing the patch
    did not address is resent, so nothing the caller never read can be lost.
    """

    def test_edits_one_field_deep_inside_an_entry(self):
        new_state = (
            ">\n"
            "  {% set lux = states('sensor.lux') | float(-1) %}\n"
            "  {{ is_state('sun.sun', 'below_horizon') or (lux >= 0 and lux < 2900) }}\n"
        )
        out = _patch(
            TEMPLATES, None, content=new_state,
            path=[0, "binary_sensor", 0, "state"],
        ).text
        assert "lux >= 0 and lux < 2900" in out
        # The folded indicator stays on the key's own line, as it was written.
        assert "state: >\n" in out
        # The other entry, its comment, and the untouched fields all survive.
        assert "# Darkness detection" in out
        assert "unique_id: outside" in out
        assert "device_class: light" in out
        assert "sensor.outdoor_temp" in out

    def test_replaces_a_whole_entry(self):
        out = _patch(
            TEMPLATES, None, content="switch:\n  - name: 'New'\n", path=[1],
        ).text
        assert "- switch:\n    - name: 'New'\n" in out
        assert "unique_id: outside" not in out
        assert "unique_id: dark" in out

    def test_removes_an_entry(self):
        out = _patch(TEMPLATES, None, op="remove", path=[0]).text
        assert "unique_id: dark" not in out
        assert "unique_id: outside" in out

    def test_appends_at_one_past_the_end(self):
        """The same affordance a missing mapping key has: set creates it."""
        out = _patch(TEMPLATES, None, content="switch:\n  - name: 'Third'\n", path=[2]).text
        assert out.rstrip("\n").endswith("- switch:\n    - name: 'Third'")
        assert "unique_id: dark" in out and "unique_id: outside" in out

    def test_before_is_the_entry_without_its_dash(self):
        before = _patch(TEMPLATES, None, content="switch: []\n", path=[1]).before
        assert before is not None
        assert before.startswith("sensor:")
        assert "unique_id: outside" in before

    def test_a_lone_dash_entry_is_not_left_behind(self):
        text = "-\n  a: 1\n-\n  a: 2\n"
        out = _patch(text, None, content="a: 9\n", path=[0]).text
        assert out == "- a: 9\n-\n  a: 2\n"

    def test_an_index_into_a_nested_list_works(self):
        out = _patch(TEMPLATES, None, content="name: 'Renamed'\nunique_id: dark\n",
                     path=[0, "binary_sensor", 0]).text
        assert "name: 'Renamed'" in out
        assert "unique_id: outside" in out

    @pytest.mark.parametrize("path,op,content,fragment", [
        ([9], "set", "a: 1", "out of range"),
        ([-1], "set", "a: 1", "negative"),
        ([True], "set", "a: 1", "neither a mapping key"),
        ([1.5], "set", "a: 1", "neither a mapping key"),
        ([], "set", "a: 1", "non-empty list"),
        (["binary_sensor"], "set", "a: 1", "does not hold a mapping of keys"),
        ([0, "binary_sensor", 9], "set", "a: 1", "out of range"),
        ([0, "absent", 0], "set", "a: 1", "does not exist"),
        ([5], "remove", None, "out of range"),
    ])
    def test_refusals(self, path, op, content, fragment):
        with pytest.raises(yaml_patch.PatchError, match=fragment):
            _patch(TEMPLATES, None, op=op, content=content, path=path)

    def test_both_forms_at_once_is_refused(self):
        with pytest.raises(yaml_patch.PatchError, match="not both"):
            _patch(TEMPLATES, "a", content="1", path=[0])

    def test_a_flow_style_list_is_refused_rather_than_guessed_at(self):
        with pytest.raises(yaml_patch.PatchError, match="flow style"):
            _patch("[a, b]\n", None, content="c", path=[0])

    def test_verify_refuses_a_splice_that_came_out_wrong(self, monkeypatch):
        """The guarantee that replaces a deletion guard, so it is pinned directly.

        A splice bug that dropped an unaddressed entry would otherwise be a
        silent data loss; _verify compares the whole re-parsed document.
        """
        real_splice = yaml_patch._splice

        def _lossy(text, start, end, replacement):
            # Valid YAML, wrong document: the entry the patch never addressed is
            # gone. This is the exact silent data loss a whole-file write can
            # commit and a patch must be incapable of.
            out = real_splice(text, start, end, replacement)
            head, sep, _ = out.partition("- sensor:")
            return head if sep else out

        monkeypatch.setattr(yaml_patch, "_splice", _lossy)
        with pytest.raises(yaml_patch.PatchError, match="did not come out as intended"):
            _patch(TEMPLATES, None, content="switch: []\n", path=[0])

    def test_verify_refuses_a_splice_that_did_not_apply(self, monkeypatch):
        """The other half: the addressed path must hold the intended value."""
        monkeypatch.setattr(yaml_patch, "_splice", lambda text, *_: text)
        with pytest.raises(yaml_patch.PatchError, match="did not come out as intended"):
            _patch(TEMPLATES, None, content="switch: []\n", path=[0])

    def test_a_key_sharing_its_line_with_the_dash_keeps_the_dash(self):
        """`- a: 1` puts the first pair on the dash's own line.

        Replacing that line without re-emitting the dash deletes the whole list
        entry. _verify caught it as a mismatch rather than writing it, which is
        the self-check working, but the edit has to LAND, not be refused.
        """
        assert _patch("- a: 1\n- b: 2\n", None, content="9", path=[0, "a"]).text == (
            "- a: 9\n- b: 2\n"
        )

    def test_a_later_key_in_an_inline_entry_is_indented_not_dashed(self):
        text = "- a: 1\n  b: 2\n"
        assert _patch(text, None, content="9", path=[0, "b"]).text == "- a: 1\n  b: 9\n"
