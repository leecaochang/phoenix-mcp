"""The four surfaces that tell an agent what to do about a pending approval.

Approvals STAGE: they queue and the operator clears them in one action, so the
agent's job after a gate is to keep going and collect the outcomes together at
the end. That model is stated in four separate places written at different
times, and three of them still described the one-at-a-time world the inline wait
used to enforce by default, pointing at `wait_for_approval` with a singular
`approval_id` from before that tool took a list. An agent following that
literally serialises on the first id, and approval N+1 cannot even be created
until N stops waiting, which is exactly the stall staging exists to remove.

Wording is the only mechanism here: nothing enforces what an agent does next.
So the phrases are pinned, the way the operator-accepted note is.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.phoenix_mcp.skill_view import PHOENIX_SKILL_MARKDOWN
from custom_components.phoenix_mcp.tool_common import _tool_pending


def _message(**kwargs) -> str:
    approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)
    return json.loads(_tool_pending(approval, **kwargs)["content"][0]["text"])["message"]


class TestPendingToolResult:
    def test_it_uses_the_custom_panel_route_for_review(self):
        approval = SimpleNamespace(id="appr-1", tool_name="create_scene", expires_at=None)
        payload = json.loads(_tool_pending(approval)["content"][0]["text"])
        assert payload["review_url"] == "/phoenix-mcp/approvals/appr-1"

    def test_it_points_at_the_plural_wait(self):
        """The singular form is the defect: it predates approval_ids."""
        message = _message()
        assert "approval_ids" in message
        assert "wait_for_approval ONCE" in message

    def test_it_tells_the_agent_to_continue_rather_than_stop(self):
        """"You may finish now" read as an instruction to stop, which is the
        early-return-reads-as-success failure applied to a whole batch."""
        message = _message()
        assert "Continue with your remaining steps" in message
        assert "queue alongside" in message
        assert "do NOT stop or wait after each one" in message

    def test_it_still_forbids_a_retry(self):
        assert "Do not retry" in _message()

    def test_a_plain_pending_does_not_claim_a_wait_happened(self):
        message = _message()
        assert "already held" not in message

    def test_the_timeout_form_states_what_it_already_spent(self):
        message = _message(waited_seconds=60)
        assert "already held for 60 seconds" in message
        # ...and gives the SAME advice, since the agent cannot see the setting.
        assert "approval_ids" in message
        assert "Continue with your remaining steps" in message

    def test_zero_is_not_a_wait(self):
        """A falsy waited_seconds must not render "already held for 0 seconds"."""
        assert "already held" not in _message(waited_seconds=0)


class TestSurfacesAgree:
    """A primer, a skill guide and a tool result describing one event.

    They are read at different moments by the same agent, so a disagreement is
    not cosmetic: whichever it happens to weight decides whether a twenty-write
    run takes one approval round or twenty.
    """

    @pytest.fixture
    def primer(self, hass):
        from unittest.mock import MagicMock

        from custom_components.phoenix_mcp.mcp_view import _build_instructions
        from custom_components.phoenix_mcp.token_store import PermissionTree, TokenRecord
        from homeassistant.util.dt import utcnow

        token = TokenRecord(
            id="t", name="t", token_hash="x", created_at=utcnow(),
            created_by="u", permissions=PermissionTree(),
        )
        data = MagicMock()
        data.store.get_settings.return_value = SimpleNamespace(mesa_mode="off")
        return _build_instructions(token, data, hass)

    def test_the_primer_names_the_plural_wait(self, primer):
        assert "wait_for_approval ONCE with approval_ids" in primer

    def test_the_primer_says_keep_going(self, primer):
        assert "do not stop after one" in primer
        assert "queue alongside" in primer

    def test_the_skill_guide_names_the_plural_wait(self):
        assert "`approval_ids` (a list)" in PHOENIX_SKILL_MARKDOWN
        assert "call `wait_for_approval` ONCE" in PHOENIX_SKILL_MARKDOWN

    def test_the_skill_guide_says_keep_going(self):
        assert "Do not stop after one, and do not wait after each one" in PHOENIX_SKILL_MARKDOWN

    def test_every_surface_still_forbids_a_retry(self, primer):
        assert "Do not retry" in _message()
        assert "Do not retry" in primer
        assert "Do not retry" in PHOENIX_SKILL_MARKDOWN
