"""Invariants between hand-maintained security constants in const.py.

These constants are edited by humans and must stay mutually consistent; a drift
between them is a silent security gap (e.g. a physical service gated in
call_service but not in the native Hass* tool path). Assert the relationships
directly so an edit to one without the other fails loudly.
"""

from __future__ import annotations

from custom_components.phoenix_mcp.const import (
    PHYSICAL_GATE_DOMAINS,
    PHYSICAL_GATE_SERVICES,
    YAML_PROTECTED_SUBTREES,
)


def test_physical_gate_domains_cover_every_service_domain():
    # Both the call_service gate (policy_engine.call_needs_physical_gate) and the
    # native Hass* tools now gate on PHYSICAL_GATE_DOMAINS. PHYSICAL_GATE_SERVICES
    # remains the documented catalog of the actuation services on those domains;
    # every one of its domains MUST be in PHYSICAL_GATE_DOMAINS or that service
    # would slip both gates. (Gating the whole domain is what closes the
    # cover.toggle / *_cover_tilt / valve.toggle / alarm_arm_custom_bypass holes
    # an exact service-name list missed.)
    service_domains = {s.split("/")[0] for s in PHYSICAL_GATE_SERVICES}
    assert service_domains <= PHYSICAL_GATE_DOMAINS, (
        f"domains gated via services but not via PHYSICAL_GATE_DOMAINS: "
        f"{sorted(service_domains - PHYSICAL_GATE_DOMAINS)}"
    )


def test_physical_gate_domains_has_no_orphans():
    # The reverse: every gate domain should correspond to at least one gated
    # service, or the native-tool filter blocks a domain call_service allows.
    service_domains = {s.split("/")[0] for s in PHYSICAL_GATE_SERVICES}
    assert PHYSICAL_GATE_DOMAINS <= service_domains, (
        f"domains in PHYSICAL_GATE_DOMAINS with no gated service: "
        f"{sorted(PHYSICAL_GATE_DOMAINS - service_domains)}"
    )


def test_yaml_protected_subtrees_shape():
    # set_yaml_config refuses a change to any of these paths, so a malformed entry
    # (a bare string instead of a tuple, an empty child list) would silently
    # protect nothing. The check is cheap; the failure mode is not.
    assert YAML_PROTECTED_SUBTREES, "the deny floor must not be empty"
    for top, children in YAML_PROTECTED_SUBTREES.items():
        assert isinstance(top, str) and top, f"bad top-level key: {top!r}"
        assert isinstance(children, tuple) and children, (
            f"{top} must map to a non-empty tuple of child keys, got {children!r}"
        )
        for child in children:
            assert isinstance(child, str) and child, f"bad child key under {top}: {child!r}"
