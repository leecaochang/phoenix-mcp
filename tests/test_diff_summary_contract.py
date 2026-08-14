"""Pin the approval-diff summary templates against the panel catalog.

The summary an admin reads before approving is the confirm gate's safety
property. It is produced by Python and rendered by the panel, so the two must be
the same sentence. These tests are what make that mechanical rather than a
convention someone has to remember.

They are deliberately cheap and pure: no hass, no builders invoked. What they
check is the CONTRACT between const.DIFF_SUMMARY_TEMPLATES, the generated
en.json entries, and the keys the builders actually name.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import string

from custom_components.phoenix_mcp.const import (
    APPROVAL_SUMMARY_TEMPLATES,
    DIFF_SUMMARY_TEMPLATES,
    VERSION_SUMMARY_TEMPLATES,
)
from custom_components.phoenix_mcp.helpers import (
    diff_summary_fields,
    version_summary_fields,
)
from custom_components.phoenix_mcp.tools.lovelace import _CARD_OP_VERSION_KEYS

# Both summary surfaces share one contract; only the namespace differs.
NAMESPACES = [
    ("diff", DIFF_SUMMARY_TEMPLATES, diff_summary_fields),
    ("version", VERSION_SUMMARY_TEMPLATES, version_summary_fields),
]
ALL_TEMPLATES = {**DIFF_SUMMARY_TEMPLATES, **VERSION_SUMMARY_TEMPLATES}

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOG = REPO / "custom_components" / "phoenix_mcp" / "catalogs" / "en.json"
# Every module in the package, not a hand-listed pair: a summary call moving to
# another module (as _version_change_summary did when the shared tool primitives
# were extracted to tool_common.py) silently emptied the scan and reported every
# template it names as an orphan. The vendored mesa_core is excluded; it has its
# own catalog and Phoenix never edits it.
SOURCES = sorted(
    p for p in (REPO / "custom_components" / "phoenix_mcp").rglob("*.py")
    if "mesa_core" not in p.parts
)


def _catalog_diff_section() -> dict[str, str]:
    return json.loads(CATALOG.read_text())["panel"]["diff"]


def test_catalog_matches_templates_exactly() -> None:
    """Both generated sections; drift means someone hand-edited en.json.

    Regenerate with scripts/gen_diff_catalog.py rather than editing en.json.
    """
    panel = json.loads(CATALOG.read_text())["panel"]
    for section, templates, _ in NAMESPACES:
        assert panel[section] == dict(sorted(templates.items())), section


def test_every_diff_summary_has_friendly_title_and_body() -> None:
    """Summary is total, and never asks for data the persisted diff lacks."""
    for key, diff_template in DIFF_SUMMARY_TEMPLATES.items():
        available = {
            field for _, field, _, _ in string.Formatter().parse(diff_template)
            if field is not None
        }
        for part in ("title", "body"):
            friendly_key = f"{key}.{part}"
            assert friendly_key in APPROVAL_SUMMARY_TEMPLATES
            required = {
                field for _, field, _, _ in string.Formatter().parse(
                    APPROVAL_SUMMARY_TEMPLATES[friendly_key]
                ) if field is not None
            }
            assert required <= available, friendly_key

    panel = json.loads(CATALOG.read_text())["panel"]
    for key, template in APPROVAL_SUMMARY_TEMPLATES.items():
        assert panel["approvalSummary"][key] == template


def test_placeholders_are_plain_names() -> None:
    """Both str.format and the panel's interpolation only understand {name}.

    Positional or attribute-style fields ({0}, {a.b}) would format fine in
    Python and render as literal text in the panel. HA also validates a
    translated string's placeholder set against English, and only sees these.
    """
    for key, template in ALL_TEMPLATES.items():
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
        assert fields == [f for f in fields if f.isidentifier()], f"{key}: {fields}"


def test_no_disallowed_characters() -> None:
    """Same house rule the panel catalog test enforces."""
    for key, template in ALL_TEMPLATES.items():
        assert not re.search(r"[—–→]", template), key


def _keys_named_in_source() -> set[str]:
    """Every literal key passed as the first argument of a summary call.

    Parsed rather than grepped because the key is often a conditional
    (`_summary("call_service.mesa" if mesa_note else "call_service", ...)`), and
    a regex anchored on the opening paren only ever sees the first branch.
    """
    def literals(node: ast.AST):
        # Do not descend into an f-string: its fragments ("blueprint.",
        # ".consumers") are not keys, and the key it builds is covered by the
        # computed-prefix list in test_every_template_is_reachable.
        if isinstance(node, ast.JoinedStr):
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
            return
        for child in ast.iter_child_nodes(node):
            yield from literals(child)

    found: set[str] = set()
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in (
                "_summary", "diff_summary_fields",
                "_version_summary", "version_summary_fields",
            ):
                continue
            found.update(literals(node.args[0]))
    return found


def test_every_key_named_in_source_exists() -> None:
    """A builder naming a key that no template defines would raise at gate time.

    diff_summary_fields raises KeyError by design rather than degrading to a
    blank summary, so this catches it here instead of on an approval card.
    """
    assert sorted(_keys_named_in_source() - set(ALL_TEMPLATES)) == []


def test_every_template_is_reachable() -> None:
    """No orphan templates.

    Keys assembled at runtime (f"blueprint.{op}", f"dashboard.{verb.lower()}",
    f"hass_turn.{verb}", f"dashboard_card.{op}", and the two patch tools'
    f"patch_dashboard.{op}" / f"patch_yaml_config.{op}" / the shared
    f"patch.{op}") have no literal to match, so they are covered by prefix. That
    list doubles as the record of which keys are computed rather than written
    out.
    """
    computed_prefixes = (
        "blueprint.", "dashboard.", "dashboard_card.", "hass_turn.",
        "patch_dashboard.", "patch_yaml_config.", "patch.", "edit_energy_config.",
        "entity.", "device.",
    )
    named = _keys_named_in_source() | set(_CARD_OP_VERSION_KEYS.values())
    orphans = [
        key for key in ALL_TEMPLATES
        if key not in named and not key.startswith(computed_prefixes)
    ]
    assert orphans == []


def test_helper_reproduces_the_template() -> None:
    """diff_summary_fields formats the template and reports the same key."""
    fields = diff_summary_fields("edit_automation", automation_id="abc")
    assert fields == {
        "summary": "Edit automation 'abc'",
        "summary_key": "diff.edit_automation",
        "summary_params": {"automation_id": "abc"},
    }


def test_every_template_formats_with_its_own_placeholders() -> None:
    """Every template survives a real format() call.

    Guards a stray literal brace, which str.format raises on and which would
    otherwise only surface when that particular approval was created.
    """
    for namespace, templates, builder in NAMESPACES:
        for key, template in templates.items():
            fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
            rendered = builder(key, **{f: f"<{f}>" for f in fields})
            assert rendered["summary_key"] == f"{namespace}.{key}"
            for f in fields:
                assert f"<{f}>" in rendered["summary"], f"{key} dropped {f}"


def test_version_summary_builds_the_changes_line() -> None:
    """The Changes tab's one-liner, end to end."""
    assert version_summary_fields("cards.was", count=25, before=24) == {
        "summary": "25 cards (was 24)",
        "summary_key": "version.cards.was",
        "summary_params": {"count": 25, "before": 24},
    }


def test_admin_error_keys_exist_in_the_catalog() -> None:
    """Every key an _err call names has a panel entry.

    _err is deliberately lenient: no key means the panel shows the English, so a
    typo'd key would not raise anywhere. It would just silently stop localizing,
    which is exactly the kind of quiet regression a test should hold.
    """
    admin = REPO / "custom_components" / "phoenix_mcp" / "admin_view.py"
    named = set(re.findall(r'_err\([^\n]*?key="(\w+)"', admin.read_text()))
    catalog = json.loads(CATALOG.read_text())["panel"]["adminError"]
    assert named, "no _err call names a key; the migration was reverted?"
    assert sorted(named - set(catalog)) == []


def test_notification_templates_match_the_catalog() -> None:
    """The notification section is generated too, and HA renders it directly."""
    from custom_components.phoenix_mcp.const import NOTIFICATION_TEMPLATES

    stored = json.loads(CATALOG.read_text())["notification"]
    assert stored == dict(sorted(NOTIFICATION_TEMPLATES.items()))


def test_notification_text_falls_back_to_english() -> None:
    """An unknown language, or a missing key, still produces the English."""
    from custom_components.phoenix_mcp.helpers import notification_text

    class _Config:
        language = "xx-NOPE"

    class _Hass:
        config = _Config()

    assert notification_text(_Hass(), "rate_limit.title") == "Phoenix MCP Alert"
    assert "hit its rate limit" in notification_text(
        _Hass(), "rate_limit.message", token="daily"
    )


def test_mesa_suggestion_templates_match_the_catalog() -> None:
    """Generated like the diff sections; drift means someone hand-edited en.json."""
    from custom_components.phoenix_mcp.const import (
        MESA_SUGGESTION_PHRASES,
        MESA_SUGGESTION_TEMPLATES,
    )

    stored = json.loads(CATALOG.read_text())["panel"]["mesaSuggestion"]
    expected = {**MESA_SUGGESTION_TEMPLATES, **MESA_SUGGESTION_PHRASES}
    assert stored == dict(sorted(expected.items()))


def test_every_suggestion_sub_phrase_has_a_catalog_entry() -> None:
    """A sentence splices these in, so a missing one is a half-translated line.

    The panel resolves noun_key / concern_key / baseline_key against the same
    section. If a template interpolates a clause with no entry of its own, the
    translated sentence keeps the English clause inside it, which reads as a
    partial translation rather than as a bug.
    """
    from custom_components.phoenix_mcp.const import (
        MESA_SUGGEST_RISKY_DOMAINS,
        MESA_SUGGESTION_PHRASES,
        MESA_SUGGESTION_TEMPLATES,
    )

    for domain in MESA_SUGGEST_RISKY_DOMAINS:
        assert f"concern.{domain}" in MESA_SUGGESTION_PHRASES, domain
    for noun in ("automation", "script", "scene"):
        assert f"noun.{noun}" in MESA_SUGGESTION_PHRASES, noun
    assert "baseline_note" in MESA_SUGGESTION_PHRASES

    # Every {placeholder} a template uses is either supplied at build time or is
    # one of these sub-phrases; nothing may be left unresolved.
    supplied = {"noun", "count", "shown", "domain", "concern", "baseline_note", "extra"}
    for key, template in MESA_SUGGESTION_TEMPLATES.items():
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        assert fields <= supplied, f"{key}: unresolvable {sorted(fields - supplied)}"


def test_suggestion_reason_reproduces_from_key_and_params() -> None:
    """The stored English and the key/params must be the same sentence.

    Same contract as a diff summary: the panel re-renders from the key, so if
    the two ever diverge an operator reads one thing and the record holds
    another.
    """
    from custom_components.phoenix_mcp.const import MESA_SUGGESTION_TEMPLATES
    from custom_components.phoenix_mcp.mesa_suggestions import _reason

    text, key, params = _reason(
        "naked_risky.domain", count=3, domain="lock", concern="is risky",
        baseline_note="", concern_key="concern.lock", baseline_key="",
    )
    plain = {k: v for k, v in params.items() if not k.endswith("_key")}
    assert text == MESA_SUGGESTION_TEMPLATES[key].format(**plain)
    assert "concern.lock" == params["concern_key"]
