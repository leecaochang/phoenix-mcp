"""Tests for yaml_includes: include-graph resolution, locate, and splice editing.

Pure-module tests (no hass fixture): everything runs against tmp_path config
trees. The oracle tests cross-check Phoenix MCP's include semantics against HA's own
annotatedyaml loader on the same tree.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from custom_components.phoenix_mcp import yaml_includes as yi


def write(base: Path, rel: str, content: str = "") -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def read(base: Path, rel: str) -> str:
    return (base / rel).read_text(encoding="utf-8")


DEFAULT_CONFIGURATION = """\
    homeassistant:
      name: Test Home
    automation: !include automations.yaml
    script: !include scripts.yaml
    scene: !include scenes.yaml
"""


# ---------------------------------------------------------------------------
# Tag layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("snippet", [
    "value: !secret db_pass\n",
    "value: !env_var PORT 8080\n",
    "value: !input target_light\n",
    "value: !include other.yaml\n",
    "value: !include_dir_list dir\n",
    "value: !include_dir_merge_list dir\n",
    "value: !include_dir_named dir\n",
    "value: !include_dir_merge_named dir\n",
])
def test_tag_round_trip(snippet):
    data = yaml.load(snippet, Loader=yi.PhoenixSafeLoader)
    assert isinstance(data["value"], yi.TaggedValue)
    assert yi.dump_tagged(data) == snippet


def test_tagged_value_str():
    assert str(yi.TaggedValue("!secret", "db_pass")) == "!secret db_pass"
    assert str(yi.TaggedValue("!secret", "")) == "!secret"


def test_loader_does_not_touch_ha_shared_classes():
    """Phoenix MCP's constructors live only on PhoenixSafeLoader, never on yaml.SafeLoader."""
    assert "!secret" not in yaml.SafeLoader.yaml_constructors
    assert "!include" not in yaml.SafeLoader.yaml_constructors
    assert yi.TaggedValue not in yaml.SafeDumper.yaml_representers


def test_dump_parity_with_ha_dump():
    from homeassistant.util.yaml import dump as ha_dump

    data = {
        "alias": "Test",
        "empty": None,
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "tricky": ["on", "07:30:00", "yes", "unicode é"],
        "nested": {"list": [{"a": 1}, {"b": None}]},
    }
    assert yi.dump_tagged(data) == ha_dump(data)


def test_encode_tags():
    obj = {
        "password": yi.TaggedValue("!secret", "db_pass"),
        "nested": [yi.TaggedValue("!env_var", "PORT 8080"), "plain"],
    }
    assert yi.encode_tags(obj) == {
        "password": "!secret db_pass",
        "nested": ["!env_var PORT 8080", "plain"],
    }


def test_contains_include_tags():
    assert yi.contains_include_tags({"a": [yi.TaggedValue("!include", "x.yaml")]})
    assert not yi.contains_include_tags({"a": [yi.TaggedValue("!secret", "x")]})
    assert not yi.contains_include_tags({"a": ["!include x.yaml"]})  # plain string


@pytest.mark.parametrize("value,expected", [
    ("!secret db_pass", True),
    ("!secret", True),
    ("!include a.yaml", True),
    ("!include_dir_merge_list automations", True),
    ("!input name", True),
    ("!env_var PORT", True),
    ("!secretx", False),
    ("not !secret", False),
    ("!includes/other", False),
    ("plain", False),
])
def test_contains_tag_strings(value, expected):
    assert yi.contains_tag_strings({"nested": [{"k": value}]}) is expected


# ---------------------------------------------------------------------------
# Layout resolution
# ---------------------------------------------------------------------------


def test_layout_default(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.error is None
    assert len(layout.targets) == 1
    assert layout.targets[0].flavor == "!include"
    assert layout.targets[0].path == os.path.realpath(str(tmp_path / "automations.yaml"))
    assert not layout.has_inline and not layout.has_packages


def test_layout_labeled_keys(tmp_path):
    write(tmp_path, "configuration.yaml", """\
        automation ui: !include automations.yaml
        automation manual: !include_dir_merge_list automations
    """)
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert [t.flavor for t in layout.targets] == ["!include", "!include_dir_merge_list"]
    assert [t.config_key for t in layout.targets] == ["automation ui", "automation manual"]


def test_layout_inline_and_packages(tmp_path):
    write(tmp_path, "configuration.yaml", """\
        homeassistant:
          packages: {}
        automation:
          - id: inline_one
            alias: Inline
    """)
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.has_inline and layout.has_packages
    assert layout.targets == ()


def test_layout_included_core_config_counts_as_packages(tmp_path):
    write(tmp_path, "configuration.yaml", "homeassistant: !include core.yaml\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.has_packages


def test_layout_missing_or_unparseable_returns_none(tmp_path):
    assert yi.resolve_domain_layout(str(tmp_path), "automation") is None
    write(tmp_path, "configuration.yaml", "{{{ not yaml")
    assert yi.resolve_domain_layout(str(tmp_path), "automation") is None
    write(tmp_path, "configuration.yaml", "- just\n- a list\n")
    assert yi.resolve_domain_layout(str(tmp_path), "automation") is None


def test_layout_containment_violation(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include ../outside.yaml\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.error == yi.MSG_OUTSIDE_CONFIG.format(domain="automation")


def test_layout_shape_mismatch(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_merge_list scripts\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "script")
    assert layout.error == yi.MSG_SHAPE_MISMATCH.format(
        domain="script", tag="!include_dir_merge_list")
    write(tmp_path, "configuration.yaml", "automation: !include_dir_named automations\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.error == yi.MSG_SHAPE_MISMATCH.format(
        domain="automation", tag="!include_dir_named")


def test_layout_non_include_tag_routes_nothing(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !secret weird\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout.targets == () and layout.error is None and not layout.has_inline


# ---------------------------------------------------------------------------
# Directory enumeration
# ---------------------------------------------------------------------------


def test_iter_dir_files_matches_ha_semantics(tmp_path):
    write(tmp_path, "inc/b.yaml", "- id: b\n")
    write(tmp_path, "inc/a.yaml", "- id: a\n")
    write(tmp_path, "inc/sub/c.yaml", "- id: c\n")
    write(tmp_path, "inc/.hidden.yaml", "- id: hidden\n")
    write(tmp_path, "inc/.hiddendir/d.yaml", "- id: d\n")
    write(tmp_path, "inc/secrets.yaml", "password: hunter2\n")
    write(tmp_path, "inc/notes.txt", "not yaml")
    files = yi._iter_dir_files(str(tmp_path / "inc"))
    names = [os.path.relpath(f, str(tmp_path / "inc")) for f in files]
    assert names[:2] == ["a.yaml", "b.yaml"]
    assert "sub/c.yaml" in names
    assert all(".hidden" not in n and "secrets.yaml" not in n and ".txt" not in n
               for n in names)


# ---------------------------------------------------------------------------
# Locate
# ---------------------------------------------------------------------------


def _automation_layout(tmp_path, config_body):
    write(tmp_path, "configuration.yaml", config_body)
    return yi.resolve_domain_layout(str(tmp_path), "automation")


def test_locate_plain_include_spans(tmp_path):
    write(tmp_path, "automations.yaml", """\
        # header
        - id: aaa
          alias: First
        # heading for second
        - id: bbb
          alias: Second
    """)
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    loc = yi.locate_entry(layout, "aaa")
    assert (loc.ref_kind, loc.start_line, loc.end_line) == ("list_item", 1, 3)
    loc = yi.locate_entry(layout, "bbb")
    assert (loc.start_line, loc.end_line) == (4, 6)
    assert loc.config == {"id": "bbb", "alias": "Second"}


def test_locate_lone_dash_extends_span(tmp_path):
    write(tmp_path, "automations.yaml", "-\n  id: aaa\n  alias: A\n- id: bbb\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    loc = yi.locate_entry(layout, "aaa")
    assert (loc.start_line, loc.end_line) == (0, 3)


def test_locate_dir_list_whole_file(tmp_path):
    write(tmp_path, "automations/one.yaml", "id: aaa\nalias: A\n")
    layout = _automation_layout(tmp_path, "automation: !include_dir_list automations\n")
    loc = yi.locate_entry(layout, "aaa")
    assert loc.ref_kind == "whole_file"
    assert loc.ref_file.endswith("one.yaml")
    assert loc.config == {"id": "aaa", "alias": "A"}


def test_locate_dir_merge_list_items(tmp_path):
    write(tmp_path, "automations/pack.yaml", "- id: aaa\n  alias: A\n- id: bbb\n  alias: B\n")
    layout = _automation_layout(tmp_path, "automation: !include_dir_merge_list automations\n")
    loc = yi.locate_entry(layout, "bbb")
    assert loc.ref_kind == "list_item" and loc.ref_file.endswith("pack.yaml")
    assert (loc.start_line, loc.end_line) == (2, 4)


def test_locate_dir_named_stem_and_empty_file(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_named scripts\n")
    write(tmp_path, "scripts/hello.yaml", "alias: Hello\nsequence: []\n")
    write(tmp_path, "scripts/empty.yaml", "")
    layout = yi.resolve_domain_layout(str(tmp_path), "script")
    loc = yi.locate_entry(layout, "hello")
    assert loc.ref_kind == "whole_file" and loc.key == "hello"
    # annotatedyaml treats an empty !include_dir_named file as an empty dict.
    loc = yi.locate_entry(layout, "empty")
    assert loc.config == {}


def test_locate_dir_merge_named_pairs(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_merge_named scripts\n")
    write(tmp_path, "scripts/pack.yaml", "one:\n  sequence: []\ntwo:\n  sequence: []\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "script")
    loc = yi.locate_entry(layout, "two")
    assert loc.ref_kind == "named_value" and loc.key == "two"
    assert (loc.start_line, loc.end_line) == (2, 4)


def test_locate_list_item_include_reference(tmp_path):
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n- !include extra.yaml\n")
    write(tmp_path, "extra.yaml", "id: bbb\nalias: B\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    loc = yi.locate_entry(layout, "bbb")
    assert loc.ref_kind == "list_item"
    assert loc.ref_file.endswith("automations.yaml")
    assert loc.content_file.endswith("extra.yaml")
    assert (loc.start_line, loc.end_line) == (2, 3)


def test_locate_named_value_include_reference(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include scripts.yaml\n")
    write(tmp_path, "scripts.yaml", "hello: !include hello.yaml\n")
    write(tmp_path, "hello.yaml", "alias: Hello\nsequence: []\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "script")
    loc = yi.locate_entry(layout, "hello")
    assert loc.ref_kind == "named_value"
    assert loc.content_file.endswith("hello.yaml")
    assert loc.config == {"alias": "Hello", "sequence": []}


def test_locate_whole_document_include_chain(tmp_path):
    write(tmp_path, "automations.yaml", "!include real.yaml\n")
    write(tmp_path, "real.yaml", "- id: aaa\n  alias: A\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    loc = yi.locate_entry(layout, "aaa")
    assert loc.ref_file.endswith("real.yaml") and loc.ref_kind == "list_item"


def test_locate_duplicate_across_files(tmp_path):
    write(tmp_path, "automations/a.yaml", "- id: dup\n  alias: A\n")
    write(tmp_path, "automations/b.yaml", "- id: dup\n  alias: B\n")
    layout = _automation_layout(tmp_path, "automation: !include_dir_merge_list automations\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "dup")
    assert exc.value.reason == "duplicate"
    assert "a.yaml" in exc.value.message and "b.yaml" in exc.value.message


def test_locate_not_found_vs_ambiguous(tmp_path):
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "ghost")
    assert exc.value.reason == "not_found"
    assert exc.value.message == "No automation found with id 'ghost'."

    layout = _automation_layout(tmp_path, """\
        homeassistant:
          packages: {}
        automation: !include automations.yaml
    """)
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "ghost")
    assert exc.value.reason == "ambiguous"


def test_locate_include_cycle(tmp_path):
    write(tmp_path, "automations.yaml", "!include loop.yaml\n")
    write(tmp_path, "loop.yaml", "!include automations.yaml\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "aaa")
    assert exc.value.reason == "unresolvable" and "cycle" in exc.value.message


def test_locate_depth_cap(tmp_path):
    write(tmp_path, "automations.yaml", "!include chain0.yaml\n")
    for i in range(10):
        write(tmp_path, f"chain{i}.yaml", f"!include chain{i + 1}.yaml\n")
    write(tmp_path, "chain10.yaml", "- id: aaa\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "aaa")
    assert exc.value.reason == "unresolvable" and "levels of includes" in exc.value.message


def test_locate_nested_containment_escape(tmp_path):
    write(tmp_path, "automations.yaml", "- !include ../../outside.yaml\n")
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "aaa")
    assert exc.value.reason == "unresolvable"
    assert exc.value.message == yi.MSG_OUTSIDE_CONFIG.format(domain="automation")


@pytest.mark.parametrize("flavor", ["!include_dir_merge_list", "!include_dir_list"])
def test_symlinked_dir_leaf_outside_config_refused(tmp_path, flavor):
    # A symlinked leaf inside an include dir resolves outside the config dir;
    # containment must hold for the resolved file, not the enumerated path.
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "leak.yaml", "- id: aaa\n  alias: Leaked\n" if flavor.endswith("merge_list") else "id: aaa\nalias: Leaked\n")
    config = tmp_path / "config"
    write(config, "automations/.keep.yaml", "")
    os.symlink(outside / "leak.yaml", config / "automations" / "leak.yaml")
    write(config, "configuration.yaml", f"automation: {flavor} automations\n")
    layout = yi.resolve_domain_layout(str(config), "automation")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "aaa")
    assert exc.value.reason == "unresolvable"
    assert exc.value.message == yi.MSG_OUTSIDE_CONFIG.format(domain="automation")
    assert yi.read_entry(str(config), "automation", "aaa") is None
    res = yi.perform_edit(str(config), "automation", "aaa", {"id": "aaa", "alias": "X"})
    assert not res.ok and not res.fallback and res.error_kind == "refused"
    assert "Leaked" in read(outside, "leak.yaml")


@pytest.mark.parametrize("flavor", ["!include_dir_named", "!include_dir_merge_named"])
def test_symlinked_named_dir_leaf_outside_config_refused(tmp_path, flavor):
    # Named directory flavors share the same containment guard as list flavors.
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_text = (
        "hello:\n  alias: Leaked\n  sequence: []\n"
        if flavor.endswith("merge_named")
        else "alias: Leaked\nsequence: []\n"
    )
    write(outside, "hello.yaml", outside_text)
    config = tmp_path / "config"
    write(config, "scripts/.keep.yaml", "")
    os.symlink(outside / "hello.yaml", config / "scripts" / "hello.yaml")
    write(config, "configuration.yaml", f"script: {flavor} scripts\n")

    layout = yi.resolve_domain_layout(str(config), "script")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "hello")
    assert exc.value.reason == "unresolvable"
    assert exc.value.message == yi.MSG_OUTSIDE_CONFIG.format(domain="script")
    assert yi.read_entry(str(config), "script", "hello") is None
    res = yi.perform_edit(str(config), "script", "hello", {"alias": "X", "sequence": []})
    assert not res.ok and not res.fallback and res.error_kind == "refused"
    assert read(outside, "hello.yaml") == outside_text


def test_symlinked_leaf_inside_config_still_locates(tmp_path):
    # A symlink whose target resolves INSIDE the config dir stays editable.
    write(tmp_path, "shared/common.yaml", "- id: aaa\n  alias: Shared\n")
    (tmp_path / "automations").mkdir()
    os.symlink(tmp_path / "shared" / "common.yaml", tmp_path / "automations" / "common.yaml")
    layout = _automation_layout(tmp_path, "automation: !include_dir_merge_list automations\n")
    loc = yi.locate_entry(layout, "aaa")
    assert loc.config == {"id": "aaa", "alias": "Shared"}


def test_locate_scene_id_str_coercion(tmp_path):
    write(tmp_path, "configuration.yaml", "scene: !include scenes.yaml\n")
    write(tmp_path, "scenes.yaml", "- id: 5\n  name: Numeric\n  entities: {}\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "scene")
    loc = yi.locate_entry(layout, "5")
    assert loc.config["name"] == "Numeric"


def test_locate_missing_plain_target_is_not_found(tmp_path):
    layout = _automation_layout(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(yi.LocateError) as exc:
        yi.locate_entry(layout, "aaa")
    assert exc.value.reason == "not_found"


# ---------------------------------------------------------------------------
# Edit / delete via splice
# ---------------------------------------------------------------------------


SPLICE_FILE = """\
    # top of file
    - id: aaa
      alias: First
    # bbb section
    - id: bbb
      alias: Second
      password: !secret db_pass

    # trailing notes
"""


def test_edit_preserves_untouched_bytes(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", SPLICE_FILE)
    res = yi.perform_edit(str(tmp_path), "automation", "aaa",
                          {"id": "aaa", "alias": "First v2"})
    assert res.ok
    assert read(tmp_path, "automations.yaml") == textwrap.dedent("""\
        # top of file
        - id: aaa
          alias: First v2
        # bbb section
        - id: bbb
          alias: Second
          password: !secret db_pass

        # trailing notes
    """)
    assert res.before == {"id": "aaa", "alias": "First"}


def test_edit_secret_bearing_entry_allowed_and_encoded(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", SPLICE_FILE)
    res = yi.perform_edit(str(tmp_path), "automation", "bbb",
                          {"id": "bbb", "alias": "Second v2"})
    assert res.ok
    assert res.before == {"id": "bbb", "alias": "Second", "password": "!secret db_pass"}
    text = read(tmp_path, "automations.yaml")
    assert "!secret" not in text  # the replaced entry dropped the reference
    assert "# top of file" in text and "# trailing notes" in text


def test_delete_preserves_heading_and_trailing_comments(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", SPLICE_FILE)
    res = yi.perform_delete(str(tmp_path), "automation", "aaa")
    assert res.ok
    assert read(tmp_path, "automations.yaml") == textwrap.dedent("""\
        # top of file
        # bbb section
        - id: bbb
          alias: Second
          password: !secret db_pass

        # trailing notes
    """)


def test_delete_last_entry_keeps_trailing_comments(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", SPLICE_FILE)
    res = yi.perform_delete(str(tmp_path), "automation", "bbb")
    assert res.ok
    text = read(tmp_path, "automations.yaml")
    # The "# bbb section" heading comment survives by design (comments are
    # never deleted); the entry itself is gone.
    assert "# trailing notes" in text and "- id: bbb" not in text
    assert yaml.load(text, Loader=yi.PhoenixSafeLoader) == [{"id": "aaa", "alias": "First"}]


def test_delete_block_scalar_with_comment_lines_never_corrupts(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", """\
        - id: aaa
          alias: A
          message: |
            hello
            # not a comment
        - id: bbb
          alias: B
    """)
    res = yi.perform_delete(str(tmp_path), "automation", "aaa")
    assert res.ok
    remaining = yaml.load(read(tmp_path, "automations.yaml"), Loader=yi.PhoenixSafeLoader)
    assert remaining == [{"id": "bbb", "alias": "B"}]


def test_edit_file_without_trailing_newline(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    (tmp_path / "automations.yaml").write_text(
        "- id: aaa\n  alias: A\n- id: bbb\n  alias: B", encoding="utf-8")
    res = yi.perform_edit(str(tmp_path), "automation", "bbb", {"id": "bbb", "alias": "B2"})
    assert res.ok
    entries = yaml.load(read(tmp_path, "automations.yaml"), Loader=yi.PhoenixSafeLoader)
    assert entries == [{"id": "aaa", "alias": "A"}, {"id": "bbb", "alias": "B2"}]


def test_edit_refuses_entry_with_internal_include(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n  action: !include actions.yaml\n")
    res = yi.perform_edit(str(tmp_path), "automation", "aaa", {"id": "aaa", "alias": "A2"})
    assert not res.ok and res.error_kind == "refused"
    assert res.message == yi.MSG_INCLUDE_REFUSAL.format(name="automations.yaml")
    # Delete of the same entry is allowed (line removal cannot corrupt).
    res = yi.perform_delete(str(tmp_path), "automation", "aaa")
    assert res.ok


def test_edit_content_file_keeps_reference(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "- !include extra.yaml\n")
    write(tmp_path, "extra.yaml", "id: aaa\nalias: A\n")
    res = yi.perform_edit(str(tmp_path), "automation", "aaa", {"id": "aaa", "alias": "A2"})
    assert res.ok and res.file.endswith("extra.yaml")
    assert read(tmp_path, "automations.yaml") == "- !include extra.yaml\n"
    assert yaml.safe_load(read(tmp_path, "extra.yaml")) == {"id": "aaa", "alias": "A2"}


def test_delete_content_reference_orphans_file(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n- !include extra.yaml\n")
    write(tmp_path, "extra.yaml", "id: bbb\nalias: B\n")
    res = yi.perform_delete(str(tmp_path), "automation", "bbb")
    assert res.ok
    assert read(tmp_path, "automations.yaml") == "- id: aaa\n  alias: A\n"
    assert (tmp_path / "extra.yaml").exists()  # orphaned, never deleted


def test_delete_dir_named_unlinks_file(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_named scripts\n")
    write(tmp_path, "scripts/hello.yaml", "alias: Hello\nsequence: []\n")
    res = yi.perform_delete(str(tmp_path), "script", "hello")
    assert res.ok
    assert not (tmp_path / "scripts/hello.yaml").exists()


def test_self_check_aborts_on_bad_span(tmp_path):
    """A corrupted span must refuse with io_error and write nothing."""
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n- id: bbb\n  alias: B\n")
    original = read(tmp_path, "automations.yaml")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    loc = yi.locate_entry(layout, "aaa")
    # Simulate span-math gone wrong: the span eats half of bbb too.
    broken = yi.LocatedEntry(
        ref_file=loc.ref_file, ref_kind=loc.ref_kind, ref_text=loc.ref_text,
        start_line=loc.start_line, end_line=loc.end_line + 1,
        key=None, config=loc.config,
    )
    res = yi._write_entry_files(broken, "automation", {"id": "aaa", "alias": "A2"})
    assert not res.ok and res.error_kind == "io_error"
    assert read(tmp_path, "automations.yaml") == original


# ---------------------------------------------------------------------------
# Create routing
# ---------------------------------------------------------------------------


def test_create_append_plain_include(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "- id: aaa\n  alias: A\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok
    assert read(tmp_path, "automations.yaml") == (
        "- id: aaa\n  alias: A\n- id: phoenix_mcp_x\n  alias: X\n")


def test_create_append_into_bare_empty_list(tmp_path):
    """HA's default empty automations.yaml is a bare inline '[]'; a text append
    after it would produce invalid YAML. The entry must replace the token."""
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "[]\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok, res.message
    text = read(tmp_path, "automations.yaml")
    assert "[]" not in text
    assert yaml.safe_load(text) == [{"id": "phoenix_mcp_x", "alias": "X"}]


def test_create_append_into_bare_empty_dict_scripts(tmp_path):
    """scripts.yaml default is a bare '{}'."""
    write(tmp_path, "configuration.yaml", "script: !include scripts.yaml\n")
    write(tmp_path, "scripts.yaml", "{}\n")
    res = yi.perform_create(str(tmp_path), "script", "hello", {"sequence": []})
    assert res.ok, res.message
    text = read(tmp_path, "scripts.yaml")
    assert "{}" not in text
    assert yaml.safe_load(text) == {"hello": {"sequence": []}}


def test_create_append_bare_list_preserves_comments(tmp_path):
    """A comments-only-plus-'[]' file keeps its comments; only the token is dropped."""
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    write(tmp_path, "automations.yaml", "# my automations\n[]\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok, res.message
    text = read(tmp_path, "automations.yaml")
    assert text.startswith("# my automations\n")
    assert yaml.safe_load(text) == [{"id": "phoenix_mcp_x", "alias": "X"}]


def test_create_plain_include_missing_file(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok
    assert read(tmp_path, "automations.yaml") == "- id: phoenix_mcp_x\n  alias: X\n"


def test_create_append_no_trailing_newline(tmp_path):
    write(tmp_path, "configuration.yaml", DEFAULT_CONFIGURATION)
    (tmp_path / "automations.yaml").write_text("- id: aaa\n  alias: A", encoding="utf-8")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok
    assert read(tmp_path, "automations.yaml") == (
        "- id: aaa\n  alias: A\n- id: phoenix_mcp_x\n  alias: X\n")


def test_create_dir_list_new_file(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_list automations\n")
    (tmp_path / "automations").mkdir()
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok and res.file.endswith("phoenix_mcp_x.yaml")
    assert yaml.safe_load(read(tmp_path, "automations/phoenix_mcp_x.yaml")) == {"id": "phoenix_mcp_x", "alias": "X"}


def test_create_dir_merge_list_new_file(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
    (tmp_path / "automations").mkdir()
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok
    assert yaml.safe_load(read(tmp_path, "automations/phoenix_mcp_x.yaml")) == [{"id": "phoenix_mcp_x", "alias": "X"}]


def test_create_dir_named_new_file(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_named scripts\n")
    (tmp_path / "scripts").mkdir()
    res = yi.perform_create(str(tmp_path), "script", "hello", {"sequence": []})
    assert res.ok and res.file.endswith("hello.yaml")
    assert yaml.safe_load(read(tmp_path, "scripts/hello.yaml")) == {"sequence": []}


def test_create_dir_merge_named_new_file(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_merge_named scripts\n")
    (tmp_path / "scripts").mkdir()
    res = yi.perform_create(str(tmp_path), "script", "hello", {"sequence": []})
    assert res.ok
    assert yaml.safe_load(read(tmp_path, "scripts/hello.yaml")) == {"hello": {"sequence": []}}


def test_create_multi_branch_prefers_single_plain_include(tmp_path):
    write(tmp_path, "configuration.yaml", """\
        automation: !include automations.yaml
        automation manual: !include_dir_merge_list automations
    """)
    write(tmp_path, "automations.yaml", "")
    (tmp_path / "automations").mkdir()
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x", "alias": "X"})
    assert res.ok and res.file.endswith("automations.yaml")


def test_create_multi_branch_ambiguous_refuses(tmp_path):
    write(tmp_path, "configuration.yaml", """\
        automation a: !include_dir_merge_list one
        automation b: !include_dir_merge_list two
    """)
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x"})
    assert not res.ok
    assert res.message == yi.MSG_CREATE_MULTI.format(domain="automation")


def test_create_no_route_refuses(tmp_path):
    write(tmp_path, "configuration.yaml", "sensor: !include sensors.yaml\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x"})
    assert not res.ok
    assert res.message == yi.MSG_CREATE_NO_ROUTE.format(domain="automation")


def test_create_filename_collision_refuses(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
    write(tmp_path, "automations/phoenix_mcp_x.yaml", "- id: other\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x"})
    assert not res.ok and res.error_kind == "io_error"
    assert "phoenix_mcp_x.yaml" in res.message


def test_create_missing_dir_refuses(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x"})
    assert not res.ok and res.error_kind == "io_error"


def test_create_filename_sanitized(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_list automations\n")
    (tmp_path / "automations").mkdir()
    res = yi.perform_create(str(tmp_path), "automation", "Weird ID!", {"id": "Weird ID!"})
    assert res.ok and res.file.endswith("phoenix_mcp_weird_id.yaml")


def test_create_script_proceeds_when_packages_present(tmp_path):
    """Regression: a config with homeassistant.packages must NOT block creating a
    brand-new script (locate returns 'ambiguous', which create treats as proceed)."""
    write(tmp_path, "configuration.yaml", """\
        script: !include scripts.yaml
        homeassistant:
          packages: !include_dir_named packages
    """)
    write(tmp_path, "scripts.yaml", "{}\n")
    (tmp_path / "packages").mkdir()
    res = yi.perform_create(str(tmp_path), "script", "phoenix_mcp_new", {"sequence": []})
    assert res.ok, res.message
    assert yaml.safe_load(read(tmp_path, "scripts.yaml")) == {"phoenix_mcp_new": {"sequence": []}}


def test_create_script_duplicate_across_leaves_refuses(tmp_path):
    """If the id exists in two leaves, create refuses as already_exists."""
    write(tmp_path, "configuration.yaml", "script: !include_dir_merge_named scripts\n")
    write(tmp_path, "scripts/a.yaml", "hello:\n  sequence: []\n")
    write(tmp_path, "scripts/b.yaml", "hello:\n  sequence: []\n")
    res = yi.perform_create(str(tmp_path), "script", "hello", {"sequence": []})
    assert not res.ok and res.error_kind == "already_exists"


def test_create_script_duplicate_graph_wide(tmp_path):
    write(tmp_path, "configuration.yaml", "script: !include_dir_merge_named scripts\n")
    write(tmp_path, "scripts/pack.yaml", "hello:\n  sequence: []\n")
    res = yi.perform_create(str(tmp_path), "script", "hello", {"sequence": []})
    assert not res.ok and res.error_kind == "already_exists"
    assert "already exists" in res.message


def test_create_append_refuses_wrong_shape(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include automations.yaml\n")
    write(tmp_path, "automations.yaml", "some: mapping\n")
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_x", {"id": "phoenix_mcp_x"})
    assert not res.ok and res.error_kind == "io_error"


# ---------------------------------------------------------------------------
# Fallback and layout-error propagation
# ---------------------------------------------------------------------------


def test_perform_fallback_without_configuration_yaml(tmp_path):
    for fn in (
        lambda: yi.perform_create(str(tmp_path), "automation", "x", {"id": "x"}),
        lambda: yi.perform_edit(str(tmp_path), "automation", "x", {"id": "x"}),
        lambda: yi.perform_delete(str(tmp_path), "automation", "x"),
    ):
        res = fn()
        assert not res.ok and res.fallback


def test_perform_layout_error_refuses(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include ../outside.yaml\n")
    res = yi.perform_edit(str(tmp_path), "automation", "x", {"id": "x"})
    assert not res.ok and not res.fallback and res.error_kind == "refused"
    assert res.message == yi.MSG_OUTSIDE_CONFIG.format(domain="automation")


# ---------------------------------------------------------------------------
# read_entry
# ---------------------------------------------------------------------------


def test_read_entry_routed_and_absent(tmp_path):
    write(tmp_path, "configuration.yaml", "automation: !include_dir_merge_list automations\n")
    write(tmp_path, "automations/pack.yaml", "- id: aaa\n  alias: A\n  password: !secret p\n")
    assert yi.read_entry(str(tmp_path), "automation", "aaa") == {
        "id": "aaa", "alias": "A", "password": "!secret p"}
    assert yi.read_entry(str(tmp_path), "automation", "ghost") is None


def test_read_entry_fallback_tolerates_secrets(tmp_path):
    # No configuration.yaml: fall back to the hardcoded default file. The legacy
    # loader raised on !secret; the tag-tolerant loader must not.
    write(tmp_path, "automations.yaml", "- id: aaa\n  password: !secret p\n")
    assert yi.read_entry(str(tmp_path), "automation", "aaa") == {
        "id": "aaa", "password": "!secret p"}
    write(tmp_path, "scripts.yaml", "hello:\n  sequence: []\n")
    assert yi.read_entry(str(tmp_path), "script", "hello") == {"sequence": []}
    assert yi.read_entry(str(tmp_path), "script", "ghost") is None


def test_read_entry_layout_error_is_authoritative_no_fallback(tmp_path):
    # configuration.yaml parsed but the layout was refused (routed outside the
    # config dir): read_entry must NOT fall back to a stale default file, or
    # diffs and existence checks would desync from what the write paths edit.
    write(tmp_path, "configuration.yaml", "automation: !include ../outside.yaml\n")
    write(tmp_path, "automations.yaml", "- id: stale\n  alias: Stale\n")
    layout = yi.resolve_domain_layout(str(tmp_path), "automation")
    assert layout is not None and layout.error
    assert yi.read_entry(str(tmp_path), "automation", "stale") is None


# ---------------------------------------------------------------------------
# Oracle: Phoenix MCP's include semantics against HA's own loader
# ---------------------------------------------------------------------------


def test_oracle_against_ha_loader(tmp_path):
    from homeassistant.util.yaml import Secrets, load_yaml

    write(tmp_path, "configuration.yaml", """\
        automation: !include_dir_merge_list automations
        script: !include_dir_merge_named scripts
    """)
    write(tmp_path, "secrets.yaml", "p: hunter2\n")
    write(tmp_path, "automations/a.yaml", "- id: one\n  alias: One\n")
    write(tmp_path, "automations/b.yaml", "- id: two\n  alias: Two\n  password: !secret p\n")
    write(tmp_path, "automations/.hidden.yaml", "- id: hidden\n")
    write(tmp_path, "scripts/pack.yaml", "hello:\n  sequence: []\n")

    resolved = load_yaml(str(tmp_path / "configuration.yaml"), Secrets(tmp_path))
    ha_automation_ids = [a["id"] for a in resolved["automation"]]
    ha_script_ids = list(resolved["script"])

    # Every entry HA resolves is locatable by Phoenix MCP, and nothing more.
    for entry_id in ha_automation_ids:
        assert yi.read_entry(str(tmp_path), "automation", entry_id) is not None
    assert yi.read_entry(str(tmp_path), "automation", "hidden") is None
    for entry_id in ha_script_ids:
        assert yi.read_entry(str(tmp_path), "script", entry_id) is not None

    # A routed create is visible to HA's loader afterwards.
    res = yi.perform_create(str(tmp_path), "automation", "phoenix_mcp_new", {"id": "phoenix_mcp_new", "alias": "New"})
    assert res.ok
    resolved = load_yaml(str(tmp_path / "configuration.yaml"), Secrets(tmp_path))
    assert "phoenix_mcp_new" in [a["id"] for a in resolved["automation"]]

    # A splice edit is invisible to HA's loader except for the intended change.
    res = yi.perform_edit(str(tmp_path), "automation", "one", {"id": "one", "alias": "One v2"})
    assert res.ok
    resolved = load_yaml(str(tmp_path / "configuration.yaml"), Secrets(tmp_path))
    by_id = {a["id"]: a for a in resolved["automation"]}
    assert by_id["one"]["alias"] == "One v2"
    assert by_id["two"] == {"id": "two", "alias": "Two", "password": "hunter2"}
