"""Guard: the declared Home Assistant floor must match what the code imports.

`MIN_HA_VERSION` is a compatibility CLAIM, and nothing checked it. It said
2024.5.0 while `panel.py` imported `StaticPathConfig`,
`async_register_static_paths` and `remove_extra_js_url`, none of which exist
before 2024.7.0, so the two advertised releases raised `ImportError` before
setup finished. HACS reads the number out of `hacs.json` and lets the install
proceed, which is the whole failure: the claim was checked by nobody and
believed by the installer.

Neither half of that is catchable by the rest of the suite. The Python floor
guard parses SYNTAX and says nothing about which HA APIs exist; the suite itself
runs on one pinned modern HA, where every import resolves. So the checks here
are the ones that can be made offline, without a second HA installed:

  1. The number is stated identically everywhere it is stated. Four files
     restate it and they drifted independently before.
  2. Every module-level `from homeassistant...` import in the package is listed
     in `KNOWN_IMPORTS` below with the release that introduced it, and none of
     those releases is newer than the floor. A new module-level HA import fails
     here until someone writes down when it landed, which is the step that was
     skipped.

Rule 2 covers only UNGUARDED module-level imports, which is deliberate and is
the boundary that matters: those run during setup and an absent name is a failed
integration. An import inside a function or a `try` degrades to a clean error at
call time, so the four post-floor APIs the package does reach that way
(`lovelace.const.LOVELACE_DATA` before 2025.2, `condition.async_get_all_descriptions`,
`websocket_api.automation`, and the ai_task / conversation / llm feature probes)
are correctly out of scope here and are documented as optional-feature floors
instead.

Verifying an introduction version needs the upstream tag and is therefore a
human step, not something this test can do offline. `KNOWN_IMPORTS` is where
that verification is recorded; check a new entry against
`https://raw.githubusercontent.com/home-assistant/core/<tag>/<path>` before
adding it.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from custom_components.phoenix_mcp.const import MIN_HA_VERSION

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "phoenix_mcp"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


FLOOR = _version(MIN_HA_VERSION)

# (module, imported name) -> the HA release that introduced that NAME.
#
# Keyed by SYMBOL, not by module, and that is the whole point of the table. The
# first version keyed on the module alone, so a new API imported from a module
# already listed inherited that module's recorded version and the guard stayed
# green. Demonstrated with a real symbol: `from homeassistant.config_entries
# import ConfigSubentry` (absent from 2025.2.0) passed all 27 tests, which is
# exactly the "green guard on the drift it exists to catch" failure this file was
# written to prevent, reproduced inside the prevention.
#
# "0.0.0" means the name predates any release this project has ever claimed, so
# it can never constrain the floor. Anything with a real version was checked
# against that tag's source at
# https://raw.githubusercontent.com/home-assistant/core/<tag>/<path>.
KNOWN_IMPORTS: dict[str, dict[str, str]] = {
    "homeassistant": {
        # Integration loading predates Phoenix's minimum HA version and is the
        # same public module used by HA's 2025.2 device-removal endpoint.
        "loader": "0.0.0",
    },
    "homeassistant.components.automation.config": {
        "async_validate_config_item": "0.0.0",
    },
    "homeassistant.components.frontend": {
        "add_extra_js_url": "0.0.0",
        "async_register_built_in_panel": "0.0.0",
        "async_remove_panel": "0.0.0",
        # Verified absent in 2024.6.0, present in 2024.7.0.
        "remove_extra_js_url": "2024.7.0",
    },
    "homeassistant.components.http": {
        "HomeAssistantView": "0.0.0",
        # Verified absent in 2024.6.0, present in 2024.7.0. With
        # async_register_static_paths, this is why the old 2024.5 claim was false.
        "StaticPathConfig": "2024.7.0",
    },
    "homeassistant.components.http.const": {
        "KEY_AUTHENTICATED": "0.0.0",
        "KEY_HASS_USER": "0.0.0",
    },
    "homeassistant.components.script.config": {
        "async_validate_config_item": "0.0.0",
    },
    "homeassistant.components.sensor": {
        "SensorEntity": "0.0.0",
        "SensorStateClass": "0.0.0",
    },
    "homeassistant.components.websocket_api": {
        "const": "0.0.0",
    },
    "homeassistant.components.websocket_api.connection": {
        "ActiveConnection": "0.0.0",
    },
    "homeassistant.config_entries": {
        "ConfigEntry": "0.0.0",
        "ConfigEntryDisabler": "0.0.0",
        "ConfigEntryState": "0.0.0",
        "ConfigFlow": "0.0.0",
        "ConfigFlowResult": "0.0.0",
        "SOURCE_RECONFIGURE": "0.0.0",
        "SOURCE_USER": "0.0.0",
        # Public helper used by HA's own 2025.2 config-entry/device removal
        # surfaces; predates Phoenix MCP's current minimum version.
        "support_entry_unload": "0.0.0",
    },
    "homeassistant.const": {
        "EVENT_HOMEASSISTANT_STARTED": "0.0.0",
        "EVENT_HOMEASSISTANT_STOP": "0.0.0",
        "MATCH_ALL": "0.0.0",
        "UnitOfTime": "0.0.0",
        "__version__": "0.0.0",
    },
    "homeassistant.core": {
        "CoreState": "0.0.0",
        # Re-exported into core from homeassistant.util.event_type. Verified
        # present in 2024.7.0, so it predates the floor rather than setting it.
        "EventType": "2024.7.0",
        "Event": "0.0.0",
        "HomeAssistant": "0.0.0",
        "State": "0.0.0",
        "callback": "0.0.0",
        "valid_entity_id": "0.0.0",
    },
    "homeassistant.data_entry_flow": {
        "FlowResultType": "0.0.0",
    },
    "homeassistant.exceptions": {
        "HomeAssistantError": "0.0.0",
        "ServiceNotFound": "0.0.0",
        "ServiceValidationError": "0.0.0",
    },
    "homeassistant.helpers": {
        "area_registry": "0.0.0",
        # Verified absent in 2024.3.3, present in 2024.4.0.
        "category_registry": "2024.4.0",
        "config_validation": "0.0.0",
        "device_registry": "0.0.0",
        "entity_registry": "0.0.0",
        "floor_registry": "0.0.0",
        "intent": "0.0.0",
        # Both the issue registry and its helper-module import predate the
        # earliest Home Assistant release Phoenix has claimed.
        "issue_registry": "0.0.0",
        # Verified absent in 2024.2.5, present in 2024.3.0.
        "label_registry": "2024.3.0",
        "sun": "0.0.0",
    },
    "homeassistant.helpers.aiohttp_client": {
        "async_get_clientsession": "0.0.0",
    },
    "homeassistant.helpers.entity": {
        "DeviceInfo": "0.0.0",
    },
    "homeassistant.helpers.entity_platform": {
        "AddEntitiesCallback": "0.0.0",
    },
    "homeassistant.helpers.event": {
        "async_call_later": "0.0.0",
        "async_track_state_change_event": "0.0.0",
        "async_track_time_interval": "0.0.0",
    },
    "homeassistant.helpers.storage": {
        "Store": "0.0.0",
    },
    "homeassistant.helpers.service": {
        # Present in 2024.1.0, before Phoenix's earliest claimed HA floor.
        "async_get_all_descriptions": "0.0.0",
    },
    "homeassistant.loader": {
        "IntegrationNotFound": "0.0.0",
        "async_get_integration": "0.0.0",
    },
    "homeassistant.util": {
        "dt": "0.0.0",
        # Core helper/entity ID generation has used this public utility since
        # well before Phoenix's earliest supported Home Assistant release.
        "slugify": "0.0.0",
    },
    "homeassistant.util.dt": {
        "as_utc": "0.0.0",
        "parse_datetime": "0.0.0",
        "utcnow": "0.0.0",
    },
    "homeassistant.util.file": {
        "write_utf8_file_atomic": "0.0.0",
    },
    "homeassistant.util.ulid": {
        # Re-exported from ulid_transform. Verified absent in 2024.7.0 and
        # present in 2024.8.0, before Phoenix MCP's 2025.2.0 floor.
        "ulid_to_bytes_or_none": "2024.8.0",
    },
    "homeassistant.util.yaml": {
        "dump": "0.0.0",
        "load_yaml": "0.0.0",
    },
}


# Symbols that are NOT imported at module level but whose absence still breaks a
# documented part of the base product. Nothing discovers these, so they are listed
# by hand, and the list is the point of this table.
#
# This is the gap the symbol-level fix did not close. The automatic scan covers
# UNGUARDED module-level imports, because those crash setup; a guarded import
# degrades to a clean error instead, so it is deliberately out of scope. But
# `LOVELACE_DATA` is guarded AND load-bearing: without it every dashboard tool
# answers "lovelace is not loaded", which is a documented part of the tool
# surface, and it is the actual reason the floor is 2025.2 rather than 2024.7.
# With only the automatic scan, lowering every claim to 2024.7.0 passed all 62
# tests, i.e. the guard was green on precisely the regression it was written to
# stop, for the second time.
#
# Add an entry here when a feature named in the BASE compatibility claim depends
# on an API newer than the previous floor. Optional features tracked by their own
# const (Assist, MESA injection, AI Task) belong in OPTIONAL_FEATURE_FLOORS below.
MANDATORY_FEATURE_IMPORTS: dict[str, str] = {
    # tools/lovelace.py + ws_dispatch.py, via a guarded import. Verified absent in
    # 2025.1.4 and present in 2025.2.0.
    "homeassistant.components.lovelace.const.LOVELACE_DATA": "2025.2.0",
}

# Optional features, each gated in the panel by its own constant. These are not
# floor constraints; they are pinned so the numbers in const.py and the numbers
# the installation page promises cannot drift apart.
OPTIONAL_FEATURE_FLOORS: dict[str, str] = {
    "ASSIST_API_MIN_HA": "2025.2.0",
    "MESA_INJECT_MIN_HA": "2025.5.0",
    "AI_TASK_MIN_HA": "2025.7.0",
}


@pytest.mark.parametrize("symbol", sorted(MANDATORY_FEATURE_IMPORTS))
def test_mandatory_feature_imports_are_not_newer_than_the_floor(symbol: str) -> None:
    """A documented feature must not need HA newer than the declared minimum.

    Unlike the scanned imports, these do not crash setup. They fail at CALL time
    with a clean error, which is worse for this purpose rather than better: the
    integration starts, HACS is satisfied, and the operator discovers the hole one
    tool at a time.
    """
    introduced = _version(MANDATORY_FEATURE_IMPORTS[symbol])
    assert introduced <= FLOOR, (
        f"{symbol} needs HA {MANDATORY_FEATURE_IMPORTS[symbol]} but MIN_HA_VERSION "
        f"is {MIN_HA_VERSION}. It is not imported at setup, so nothing crashes; the "
        f"feature that depends on it simply fails on every call, which is exactly "
        f"the 2024.7-versus-2025.2 case. Raise MIN_HA_VERSION."
    )


def test_optional_feature_floors_match_their_constants() -> None:
    """The panel's per-feature gates and this table must agree."""
    from custom_components.phoenix_mcp import const  # noqa: PLC0415

    for name, expected in OPTIONAL_FEATURE_FLOORS.items():
        assert getattr(const, name) == expected, (
            f"const.{name} is {getattr(const, name)!r} but this guard expects "
            f"{expected!r}. Update both, and docs/install.html with them."
        )


def test_optional_feature_floors_are_stated_on_the_install_page() -> None:
    # The install page promises these numbers to the reader; a silent change there
    # is the same class of defect as a silent change to the floor itself.
    html = (ROOT / "docs" / "install.html").read_text(encoding="utf-8")
    for name, version in OPTIONAL_FEATURE_FLOORS.items():
        major_minor = ".".join(version.split(".")[:2])
        assert major_minor in html, (
            f"docs/install.html never mentions {major_minor}, the floor for {name}."
        )


def _unguarded_module_level_ha_imports() -> dict[tuple[str, str], set[str]]:
    """Map each (module, imported name) to the files importing it at module level.

    The NAME is what matters and dropping it was the original defect: a guard
    keyed on the module alone lets a brand-new API ride in on an already-recorded
    module and says nothing.

    Guarded means nested in a function, a `try`, or a `TYPE_CHECKING` block: none
    of those runs at import time, so an absent name there cannot fail setup.
    """
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        if "mesa_core" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded: set[int] = set()

        class _Walker(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.AST) -> None:
                guarded.update(id(child) for child in ast.walk(node))

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Try(self, node: ast.AST) -> None:
                guarded.update(id(child) for child in ast.walk(node))

            def visit_If(self, node: ast.If) -> None:
                if "TYPE_CHECKING" in ast.unparse(node.test):
                    guarded.update(id(child) for child in ast.walk(node))
                else:
                    self.generic_visit(node)

        _Walker().visit(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith("homeassistant"):
                    continue
                for alias in node.names:
                    found.setdefault((node.module, alias.name), set()).add(path.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("homeassistant"):
                        found.setdefault((alias.name, "*module*"), set()).add(path.name)
    return found


def _recorded_pairs() -> set[tuple[str, str]]:
    return {(module, name) for module, names in KNOWN_IMPORTS.items() for name in names}


def test_every_module_level_ha_import_is_recorded() -> None:
    """A new setup-time HA import must state which release introduced it.

    Per SYMBOL. Keyed by module alone, this passed while
    `from homeassistant.config_entries import ConfigSubentry` (absent from
    2025.2.0) sat in the package, because that module was already recorded.
    """
    imports = _unguarded_module_level_ha_imports()
    recorded = _recorded_pairs()
    unrecorded = {pair: sorted(files) for pair, files in imports.items() if pair not in recorded}
    assert not unrecorded, (
        "These names are imported at module level (so they run during setup) but "
        "are not in KNOWN_IMPORTS. Check which HA release introduced each one, "
        "then add it under its module:\n"
        + "\n".join(
            f"  {module}.{name}  <- {', '.join(files)}"
            for (module, name), files in sorted(unrecorded.items())
        )
    )


def test_no_star_imports_from_home_assistant() -> None:
    """A star import cannot be checked, so it must not exist.

    `from homeassistant.x import *` binds names nobody enumerated, which would
    let an arbitrary post-floor API in through a module the table already lists.
    """
    offenders = sorted(
        f"{module} <- {', '.join(sorted(files))}"
        for (module, name), files in _unguarded_module_level_ha_imports().items()
        if name == "*"
    )
    assert not offenders, f"Star imports from Home Assistant cannot be version-checked: {offenders}"


@pytest.mark.parametrize("pair", sorted(_recorded_pairs()))
def test_no_module_level_import_needs_newer_ha_than_the_floor(pair: tuple[str, str]) -> None:
    """The floor must not be older than an API setup unconditionally imports."""
    module, name = pair
    stated = KNOWN_IMPORTS[module][name]
    assert _version(stated) <= FLOOR, (
        f"{module}.{name} needs HA {stated} but MIN_HA_VERSION is {MIN_HA_VERSION}. "
        f"Setup imports it unconditionally, so every install between the two raises "
        f"ImportError. Raise MIN_HA_VERSION (and the four places that restate it)."
    )


def test_known_imports_has_no_stale_entries() -> None:
    """An entry for a name nothing imports any more is misleading."""
    imports = _unguarded_module_level_ha_imports()
    stale = sorted(f"{module}.{name}" for module, name in _recorded_pairs() - set(imports))
    assert not stale, (
        f"KNOWN_IMPORTS lists names no longer imported at module level: {stale}. "
        "Remove them so the table keeps describing the real setup-time surface."
    )


def test_the_floor_is_stated_identically_everywhere() -> None:
    """Four files restate the number and they drifted independently before."""
    major_minor = ".".join(MIN_HA_VERSION.split(".")[:2])

    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["homeassistant"] == MIN_HA_VERSION, (
        f"hacs.json says {hacs['homeassistant']}, const.py says {MIN_HA_VERSION}. "
        "HACS reads hacs.json to decide whether an install may proceed."
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Home Assistant {MIN_HA_VERSION} or later" in readme
    assert f"Home%20Assistant-{major_minor}%2B" in readme, (
        f"The README badge does not read {major_minor}+."
    )

    for page, needle in (
        ("index.html", f"HA {major_minor}+"),
        ("install.html", f"{MIN_HA_VERSION} or later"),
    ):
        html = (ROOT / "docs" / page).read_text(encoding="utf-8")
        assert needle in html, f"docs/{page} does not state the floor as {needle!r}."
