"""Tests for the core-domain service-name hint helper (helpers.service_not_found_hint).

The hint turns an opaque ServiceNotFound into an actionable list of valid core
service verbs for a curated set of actuator domains, so an agent that guessed the
wrong verb (e.g. valve.open instead of valve.open_valve) can self-correct. It is
only ever consulted post-authorization; see DOMAIN_SERVICE_HINTS in const.py for
the leak-safety rationale. These tests pin the contract mcp_view relies on and
cross-check the hardcoded verbs against the installed HA core so an upgrade
that renames or drops one fails here.
"""

from __future__ import annotations

from pathlib import Path

import homeassistant
import yaml

from custom_components.phoenix_mcp.const import DOMAIN_SERVICE_HINTS
from custom_components.phoenix_mcp.helpers import service_not_found_hint


def test_hint_for_mapped_domain_names_valid_verbs():
    result = service_not_found_hint("valve", "open")
    assert result is not None
    message, suggestions = result
    assert "open_valve" in suggestions
    assert "close_valve" in suggestions
    assert "valve.open" in message  # echoes the bad verb the agent tried
    assert "open_valve" in message


def test_hint_is_none_for_unmapped_domain():
    # light is deliberately excluded (obvious turn_on/turn_off verbs).
    assert service_not_found_hint("light", "make_bright") is None


def test_hint_is_none_for_unknown_domain():
    assert service_not_found_hint("totally_custom_domain", "frobnicate") is None


def test_hint_suggestions_match_the_static_map():
    for domain, services in DOMAIN_SERVICE_HINTS.items():
        result = service_not_found_hint(domain, "whatever")
        assert result is not None
        _message, suggestions = result
        assert suggestions == list(services)


def test_every_hint_verb_exists_in_the_installed_ha_core():
    # DOMAIN_SERVICE_HINTS is hardcoded rather than read from hass.services (that
    # is what keeps it from leaking custom services), so nothing but this test
    # notices when core renames or drops a verb and the hint starts pointing an
    # agent at a service that no longer exists. Each domain's services.yaml in the
    # installed HA package is the authority: it lists every service the
    # integration registers, and reading it needs no integration set up.
    components = Path(homeassistant.__file__).parent / "components"
    assert DOMAIN_SERVICE_HINTS, "nothing to check; the map cannot be empty"

    checked = 0
    missing: dict[str, list[str]] = {}
    for domain, verbs in DOMAIN_SERVICE_HINTS.items():
        manifest = components / domain / "services.yaml"
        assert manifest.is_file(), f"HA core no longer ships a {domain} services.yaml"
        real = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert isinstance(real, dict) and real, f"unreadable {domain}/services.yaml"
        absent = [v for v in verbs if v not in real]
        if absent:
            missing[domain] = absent
        checked += len(verbs)

    assert not missing, f"hint verbs absent from installed HA core: {missing}"
    assert checked == sum(len(v) for v in DOMAIN_SERVICE_HINTS.values())


def test_hint_never_reflects_the_attempted_service_into_suggestions():
    # The suggestions are the hardcoded valid verbs only, never the (invalid)
    # verb the caller passed - so the hint cannot be turned into an echo oracle.
    _message, suggestions = service_not_found_hint("cover", "definitely_not_real")
    assert "definitely_not_real" not in suggestions
