"""Frontend/backend contract drift guard for the admin token response.

The frontend's TokenRecord type (frontend_src/types.ts) is hand-mirrored from the
Python TokenRecord serializer. There was no guard, so the two could drift silently.
This test pins the serialized key set against a shared committed fixture
(tests/contract/token_record_keys.json); the matching frontend test asserts the
TS type covers the same keys.

If TokenRecord.to_dict gains or loses a field, this test fails. To resolve:
  1. regenerate tests/contract/token_record_keys.json from the serializer, and
  2. update frontend_src/types.ts + contract.test.ts to match.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from custom_components.phoenix_mcp.token_store import TokenRecord

_CONTRACT = os.path.join(os.path.dirname(__file__), "contract", "token_record_keys.json")


def _sample_token() -> TokenRecord:
    # pass_through=True so the conditional use_assist_exposure field is emitted,
    # giving the full superset of serialized keys.
    return TokenRecord(
        id="x", name="n", token_hash="h",
        created_at=datetime.now(timezone.utc), created_by="u", pass_through=True,
    )


def test_token_record_serializer_matches_contract_fixture():
    live_keys = sorted(_sample_token().to_dict().keys())
    with open(_CONTRACT, encoding="utf-8") as f:
        expected = sorted(json.load(f)["token_record_keys"])
    assert live_keys == expected, (
        "TokenRecord.to_dict shape drifted from the frontend contract fixture. "
        "Regenerate tests/contract/token_record_keys.json and update "
        "frontend_src/types.ts + contract.test.ts."
    )


_VERSION_CONTRACT = os.path.join(
    os.path.dirname(__file__), "contract", "version_resource_types.json")
# Matches the keyword literal, never the `resource_type == "x"` comparisons in
# async_restore_version (a quote must follow the `=` immediately).
_VERSION_TYPE_RE = re.compile(r'resource_type=["\']([a-z_]+)["\']')


def _backend_version_resource_types() -> set[str]:
    """Every resource_type _record_version is actually called with.

    Read from the source rather than a hand-kept list, so this reflects reality
    and cannot itself drift; a call site passing a variable instead of a literal
    would be invisible here, which the count floor below is a backstop for.

    Scans the whole package, not just mcp_view: the ESPHome executors moved to
    tools/esphome.py and took "esphome_yaml" with them, which a single-file scan
    silently reported as a removed resource type.
    """
    import pathlib

    from custom_components.phoenix_mcp import mcp_view

    package = pathlib.Path(mcp_view.__file__).parent
    found: set[str] = set()
    for path in package.rglob("*.py"):
        if "mesa_core" in path.parts:
            continue
        found |= set(_VERSION_TYPE_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_version_resource_types_match_the_frontend_contract():
    """The TS VersionResourceType union must cover every recorded resource type.

    A type recorded by _record_version but absent from the union is mistyped at
    every use, and the same omission in ApprovalDiff.kind routes those approvals
    to the wrong renderer. Neither fails anywhere else.
    """
    with open(_VERSION_CONTRACT, encoding="utf-8") as f:
        expected = set(json.load(f)["version_resource_types"])
    live = _backend_version_resource_types()
    assert live == expected, (
        "The resource types recorded by mcp_view drifted from the frontend "
        "contract fixture. Update tests/contract/version_resource_types.json, "
        "the VersionResourceType union in frontend_src/types.ts, and the map in "
        "frontend_src/__tests__/contract.test.ts. If the new type's version "
        "payload is the raw {content, bytes} blob, also add it to "
        "RAW_CONTENT_TYPES in frontend_src/views/ChangesView.tsx."
    )


def test_versioned_resource_types_accepts_every_recorded_type():
    """VersionStore.record RAISES on a type absent from VERSIONED_RESOURCE_TYPES.

    _record_version swallows that, deliberately: history capture must never break
    the write it is recording. The consequence is that a type missing from the
    const set loses its ENTIRE rollback path silently, leaving only a log line
    nobody reads, while every write still reports success.

    The test above pins the call sites against the frontend fixture and passed
    happily while this was broken, because the two sets are maintained separately.
    Live-hit on 2026-08-03: `energy` reached the fixture, the TS union and the
    label map, but not this frozenset, so every Energy write recorded nothing and
    the Changes tab was simply empty of them.
    """
    from custom_components.phoenix_mcp.const import VERSIONED_RESOURCE_TYPES

    recorded = _backend_version_resource_types()
    missing = sorted(recorded - VERSIONED_RESOURCE_TYPES)
    assert missing == [], (
        f"_record_version records {missing} but const.VERSIONED_RESOURCE_TYPES "
        "rejects them, so their version history is silently dropped."
    )


def test_version_resource_type_scan_still_finds_the_call_sites():
    # Guards the guard: if _record_version is refactored to pass the type
    # positionally or via a variable, the regex silently returns fewer types and
    # the assertion above would pass while checking almost nothing.
    assert len(_backend_version_resource_types()) >= 10


_PROVIDER_CONTRACT = os.path.join(
    os.path.dirname(__file__), "contract", "agentcli_provider_kinds.json")


def test_agentcli_provider_kinds_match_the_frontend_contract():
    """The provider allowlist is hand-mirrored into the panel three times.

    A kind added to the backend but not to the frontend KINDS table is simply
    absent from the "Add new provider" dropdown, i.e. shipped and unreachable,
    with nothing failing. The matching frontend test pins the TS union AND that
    dropdown table against this same fixture.
    """
    from custom_components.phoenix_mcp.const import AGENTCLI_PROVIDERS

    with open(_PROVIDER_CONTRACT, encoding="utf-8") as f:
        expected = set(json.load(f)["agentcli_provider_kinds"])
    assert set(AGENTCLI_PROVIDERS) == expected, (
        "const.AGENTCLI_PROVIDERS drifted from the frontend contract fixture. "
        "Update tests/contract/agentcli_provider_kinds.json, the "
        "AgentCliProviderKind union and KINDS table in the frontend, and add the "
        "kind's endpoint defaults to agentcli._KINDS."
    )


def test_contract_fixture_covers_every_capability():
    # The cap_* fields are the most drift-prone (each new capability touches both
    # sides); assert the fixture carries the full canonical capability set.
    from custom_components.phoenix_mcp.const import CAPABILITY_NAMES

    with open(_CONTRACT, encoding="utf-8") as f:
        keys = set(json.load(f)["token_record_keys"])
    assert set(CAPABILITY_NAMES) <= keys


_CAP_MATRIX = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "frontend_src", "components", "CapabilityMatrix.tsx",
)

# One entry of the frontend CAPS table. The four fields are always in this
# order, which is what makes a regex sufficient here (the same pragmatic choice
# as the provider-kind and version-resource-type scans above).
_CAP_ENTRY_RE = re.compile(
    r'key:\s*"(?P<key>cap_\w+)",\s*'
    r'labelKey:\s*"[^"]*",\s*'
    r'descriptionKey:\s*"[^"]*",\s*'
    r'tier:\s*"(?P<tier>\w+)",\s*'
    r'confirmAvailable:\s*(?P<confirm>true|false),'
)


def _frontend_caps() -> dict[str, tuple[str, bool]]:
    """The panel's own tier / confirm-availability table, read from source."""
    with open(_CAP_MATRIX, encoding="utf-8") as f:
        return {
            m.group("key"): (m.group("tier"), m.group("confirm") == "true")
            for m in _CAP_ENTRY_RE.finditer(f.read())
        }


def test_capability_tier_scan_still_finds_the_frontend_table():
    """Guards the two tests below from passing vacuously if CAPS is reformatted."""
    from custom_components.phoenix_mcp.const import CAPABILITY_NAMES

    found = _frontend_caps()
    assert len(found) == len(CAPABILITY_NAMES), (
        f"parsed {len(found)} entries out of CapabilityMatrix.tsx but there are "
        f"{len(CAPABILITY_NAMES)} capabilities; the CAPS table shape changed and "
        "_CAP_ENTRY_RE no longer matches it."
    )


def test_capability_tiers_match_the_frontend_table():
    """CAPABILITY_TIERS is hand-mirrored into the panel; nothing else compares them.

    The backend never reads the tier itself, so a capability filed under the
    wrong tier is invisible server-side and shows up only as a control sitting
    in the wrong group in the UI. Until this test existed CAPABILITY_TIERS had
    no consumer at all, which made the "the contract guards catch it" note in
    CLAUDE.md untrue for this one constant.
    """
    from custom_components.phoenix_mcp.const import CAPABILITY_NAMES, CAPABILITY_TIERS

    found = _frontend_caps()
    assert set(found) == set(CAPABILITY_NAMES), (
        "the panel's CAPS table and const.CAPABILITY_NAMES disagree on which "
        "capabilities exist. A capability missing from CAPS has no control in "
        "the UI; one only in CAPS renders a control the backend ignores."
    )
    drifted = {k: (v[0], CAPABILITY_TIERS[k]) for k, v in found.items() if v[0] != CAPABILITY_TIERS[k]}
    assert not drifted, (
        f"tier drift between CapabilityMatrix.tsx and const.CAPABILITY_TIERS "
        f"(panel, backend): {drifted}"
    )


def test_confirm_availability_matches_the_frontend_table():
    """A cap the panel offers Confirm for that the backend refuses is a dead-end control.

    The backend rejects a confirm mode for anything outside
    CONFIRM_AVAILABLE_CAPS, so the two disagreeing means either an admin gets an
    error from a control the UI offered, or a gateable capability silently has
    no Confirm option.
    """
    from custom_components.phoenix_mcp.const import CONFIRM_AVAILABLE_CAPS

    found = _frontend_caps()
    drifted = {
        k: (v[1], k in CONFIRM_AVAILABLE_CAPS)
        for k, v in found.items()
        if v[1] != (k in CONFIRM_AVAILABLE_CAPS)
    }
    assert not drifted, (
        f"confirmAvailable drift between CapabilityMatrix.tsx and "
        f"const.CONFIRM_AVAILABLE_CAPS (panel, backend): {drifted}"
    )


_MESA_SCOPE_CONTRACT = os.path.join(
    os.path.dirname(__file__), "contract", "mesa_scopes.json")


def test_mesa_scopes_match_the_frontend_contract():
    """The inheritance levels are hand-mirrored into the panel many times.

    The editor's API dispatch, six label maps, the list table and the in-context
    injector each name the scopes, and most of those are silent when one is
    missing: the dispatch used to end in an untyped fallthrough that wrote any
    unnamed scope to the AREA endpoint. The matching frontend test pins the TS
    union, the label maps, the list table and the injector against this fixture.
    """
    from custom_components.phoenix_mcp.const import MESA_SCOPES

    with open(_MESA_SCOPE_CONTRACT, encoding="utf-8") as f:
        expected = json.load(f)["mesa_scopes"]
    assert list(MESA_SCOPES) == expected, (
        "const.MESA_SCOPES drifted from the frontend contract fixture. Update "
        "tests/contract/mesa_scopes.json, the MesaProfileScope union, every "
        "Record keyed by it, and the admin endpoints for the new level."
    )


def test_every_mesa_scope_has_admin_endpoints():
    """A level with no endpoints is authorable nowhere, which nothing else fails on.

    The panel reaches each level through its own routes, so a scope added to the
    canonical list without them ships as a dead entry in every picker.
    """
    from custom_components.phoenix_mcp.admin_view import ALL_ADMIN_VIEWS
    from custom_components.phoenix_mcp.const import MESA_SCOPES

    urls = {v.url for v in ALL_ADMIN_VIEWS}
    # entity profiles live under /mesa/profiles; the rest are named by their level.
    plural = {"entity": "profiles", "device": "devices", "area": "areas",
              "integration": "integrations", "domain": "domains"}
    for scope in MESA_SCOPES:
        section = plural[scope]
        assert f"/api/phoenix-mcp/admin/mesa/{section}" in urls, scope
        assert any(u.startswith(f"/api/phoenix-mcp/admin/mesa/{section}/{{") for u in urls), scope
