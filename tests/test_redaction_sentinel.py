"""const.REDACTION_SENTINEL must stay equal to what the redacting readers emit.

The constant exists so a WRITE path can refuse to persist a placeholder (see
tool_common.redaction_sentinel_path). The four PRODUCERS still spell the literal
inline, which was deliberate: rewriting working redaction code carries more risk
than the duplication. This file is what makes the duplication safe. If a producer
ever changes its placeholder, the write guard would silently stop matching and
patch_dashboard would start accepting redacted values again, which is precisely
the failure the guard exists to prevent, and nothing else would notice.

Asserting against the real filter_service_response rather than re-stating the
string: comparing a constant to a copy of itself is the vacuous-assertion trap.
"""

from __future__ import annotations

import uuid

from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import REDACTION_SENTINEL
from custom_components.phoenix_mcp.helpers import redact_structure
from custom_components.phoenix_mcp.policy_engine import filter_service_response
from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord


def _token() -> TokenRecord:
    """A token with an empty tree, so every entity resolves to NO_ACCESS."""
    return TokenRecord(
        id=str(uuid.uuid4()), name="t", token_hash="x", created_at=utcnow(),
        created_by="u", permissions=PermissionTree(domains={}),
    )


async def test_filter_service_response_emits_the_constant(hass: HomeAssistant) -> None:
    """The entity-id redaction path is what a dashboard read goes through."""
    out = filter_service_response({"entity": "light.not_visible"}, _token(), hass)
    assert out["entity"] == REDACTION_SENTINEL


async def test_ghost_entity_redacts_identically(hass: HomeAssistant) -> None:
    """Rule 8: absent from states AND registry is NO_ACCESS before any resolution.

    This is why widening a token's permissions cannot make a dashboard carrying
    dead references writable again, and therefore why patch_dashboard had to
    exist rather than the read simply being made faithful for an admin token.
    """
    out = filter_service_response({"entity": "lock.deleted_long_ago"}, _token(), hass)
    assert out["entity"] == REDACTION_SENTINEL


def test_redact_structure_emits_the_constant() -> None:
    """The sensitive-key path, used for approval records and audit payloads."""
    assert redact_structure({"password": "hunter2"})["password"] == REDACTION_SENTINEL
