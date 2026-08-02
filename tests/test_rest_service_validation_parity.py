"""A bad service argument on REST must answer, and be AUDITED, like it does on MCP.

`hass.services.async_call` validates `service_data` against the TARGET service's
own voluptuous schema and re-raises `vol.Invalid` UNWRAPPED. It subclasses
neither `ServiceValidationError` nor `HomeAssistantError`, so the REST
`call_service` handler's except-ladder did not catch it at all and it escaped to
`HomeAssistantView`'s own wrapper, which answers a bare `400: Bad Request`.

Three things were wrong with that, and only the first is cosmetic:

1. `text/plain`, with no `X-Phoenix-Request-ID` (rule 18 requires one on every
   response).
2. **No audit row.** The request had already cleared authentication, the rate
   limiter, the capability gate and MESA, and then vanished from the one record
   an operator reads to see what a token did.
3. On MCP the same call answers `invalid_request` WITH the validator's message,
   so the two front doors disagreed about the same request.

Found live against a real instance, not by reading: a REST call with a
wrong-typed value returned `400: Bad Request` while the calls either side of it
appeared in the audit log and it did not.

The behavioural test below is the one that matters, because it asserts the audit
row exists. The structural test guards the same property one level up, by
comparing the two surfaces' except-ladders directly, so a future edit to either
one fails here rather than silently re-opening the gap.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import pathlib
import secrets
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.audit import AuditLog
from custom_components.phoenix_mcp.const import DOMAIN, TOKEN_PREFIX
from custom_components.phoenix_mcp.rate_limiter import RateLimiter, RateLimitResult
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.token_store import (
    PermissionNode,
    PermissionTree,
    TokenRecord,
    TokenStore,
)


_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "phoenix_mcp"


# --------------------------------------------------------------------------
# Structural: the two ladders must agree on which exceptions they name.
# --------------------------------------------------------------------------


def _handler_names(module: str, func: str) -> list[str]:
    """The exception type names caught around this function's service call.

    Read from the AST rather than by running the view: the point is to compare
    what the two modules DECIDE to catch, and driving every branch end to end on
    both surfaces would be far more machinery for a weaker guarantee.

    Returns them in source order, because order is load-bearing here:
    ServiceNotFound subclasses ServiceValidationError, so catching the general
    one first would silently swallow the specific one and start leaking service
    existence.
    """
    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func),
        None,
    )
    assert target is not None, f"{module}: no function named {func}; did it move or get renamed?"

    def _calls_service(node: ast.AST) -> bool:
        return any(
            isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "async_call"
            for c in ast.walk(node)
        )

    names: list[str] = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Try) or not any(_calls_service(b) for b in node.body):
            continue
        for handler in node.handlers:
            for exc in (handler.type.elts if isinstance(handler.type, ast.Tuple)
                        else [handler.type] if handler.type is not None else []):
                if isinstance(exc, ast.Name):
                    names.append(exc.id)
                elif isinstance(exc, ast.Attribute):
                    names.append(f"{getattr(exc.value, 'id', '?')}.{exc.attr}")
    assert names, (
        f"{module}.{func}: found no except-handlers around a services.async_call. "
        "The extractor is looking in the wrong place, so this test proves nothing."
    )
    return names


def test_both_surfaces_catch_the_same_service_call_exceptions():
    """REST and MCP must name the same exception types around the service call."""
    rest = _handler_names("proxy_view.py", "post")
    mcp = _handler_names("mcp_view.py", "_execute_call_service")
    for exc in ("ServiceNotFound", "ServiceValidationError", "vol.Invalid", "HomeAssistantError"):
        assert exc in rest, f"REST no longer catches {exc}; it will escape to aiohttp unaudited"
        assert exc in mcp, f"MCP no longer catches {exc}"


@pytest.mark.parametrize("module,func", [
    ("proxy_view.py", "post"),
    ("mcp_view.py", "_execute_call_service"),
])
def test_the_specific_exception_is_caught_before_its_base(module, func):
    """ServiceNotFound before ServiceValidationError, which it subclasses.

    Reversing them would answer a nonexistent service with the validator's
    message instead of the deliberately generic refusal, turning the handler
    into an oracle for which services exist.
    """
    names = _handler_names(module, func)
    assert names.index("ServiceNotFound") < names.index("ServiceValidationError")


def test_the_extractor_would_notice_a_missing_handler():
    """The guard above is only worth having if it can fail.

    Without this, an extractor that silently matched nothing would make every
    assertion above vacuous while still reporting green.
    """
    with pytest.raises(AssertionError, match="no function named"):
        _handler_names("proxy_view.py", "a_function_that_does_not_exist")


# --------------------------------------------------------------------------
# Behavioural: the response AND the audit row.
# --------------------------------------------------------------------------


def _token() -> tuple[TokenRecord, str]:
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    return (
        TokenRecord(
            id=str(uuid.uuid4()),
            name="t",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=utcnow(),
            created_by="u",
            permissions=PermissionTree(domains={"light": PermissionNode(state="GREEN")}),
            rate_limit_requests=60,
            rate_limit_burst=10,
        ),
        raw,
    )


def _data(token: TokenRecord) -> PhoenixData:
    store = MagicMock(spec=TokenStore)
    store.get_token_by_hash.return_value = token
    store.get_settings.return_value = MagicMock(
        kill_switch=False, disable_all_logging=False, log_allowed=True,
        log_denied=True, log_rate_limited=True, log_entity_names=True,
        log_client_ip=True, notify_on_rate_limit=False, mesa_mode="off",
    )
    store.update_last_used = MagicMock()
    store.get_pending_approvals.return_value = []
    store.async_lock = asyncio.Lock()

    limiter = MagicMock(spec=RateLimiter)
    limiter.check.return_value = RateLimitResult(
        allowed=True, rate_limiting_enabled=True, limit=60,
        remaining=59, reset=9999999999, retry_after=0,
    )
    audit = MagicMock(spec=AuditLog)
    audit.record = MagicMock()
    return PhoenixData(store=store, rate_limiter=limiter, audit=audit, rate_limit_notified={})


def _request(raw: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.method = "POST"
    req.remote = "127.0.0.1"
    req.headers = MagicMock()
    req.headers.get = MagicMock(
        side_effect=lambda k, d="": {"Authorization": f"Bearer {raw}"}.get(k, d))
    payload = json.dumps(body).encode()
    req.content_length = len(payload)
    # helpers.async_read_json_body loops read() until EOF, so the second call
    # must terminate the stream rather than repeat the body.
    req.content = MagicMock()
    req.content.read = AsyncMock(side_effect=[payload, b""])
    return req


async def test_a_wrong_typed_argument_is_a_400_that_is_actually_audited(hass):
    """The live-found gap: vol.Invalid escaped, so nothing recorded the call.

    Asserting the audit row is the point. A 400 with the right body but no
    record would still leave an operator unable to see that the token tried.
    """
    from custom_components.phoenix_mcp.proxy_view import PhoenixServiceView

    from homeassistant.helpers import entity_registry as er

    token, raw = _token()
    data = _data(token)
    hass.data[DOMAIN] = data
    # Rule 14: a target absent from the entity REGISTRY is refused before any
    # service call, so a state alone would never reach the ladder under test.
    entry = er.async_get(hass).async_get_or_create(
        "light", "demo", "kitchen-unique", suggested_object_id="kitchen")
    hass.states.async_set(entry.entity_id, "off")
    # Exactly what HA does for a service whose schema rejects the payload: it
    # raises voluptuous' own error, unwrapped and NOT a HomeAssistantError.
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(
        side_effect=vol.Invalid("expected float for dictionary value @ data['brightness']"))
    hass.services.async_services = MagicMock(return_value={"light": {"turn_on": {}}})

    view = PhoenixServiceView()
    view.hass = hass
    resp = await view.post(
        _request(raw, {"entity_id": "light.kitchen", "brightness": "nope"}),
        "light", "turn_on",
    )

    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_request"
    assert "brightness" in body["message"], "the validator's detail is what lets an agent self-correct"
    assert resp.headers.get("X-Phoenix-Request-ID"), "rule 18: every response carries one"

    outcomes = [c.kwargs.get("outcome") or c.args[0] for c in data.audit.record.call_args_list]
    assert outcomes, "the call left NO audit row, which is the defect this test exists for"
    assert "invalid_request" in outcomes
    assert "denied" not in outcomes, (
        "denied is reserved for a policy decision Phoenix made; a bad argument filed "
        "under it corrupts the signal an operator reads"
    )
