"""Tests for the scoped multimodal camera image tool."""

from __future__ import annotations

import asyncio
import importlib
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util.dt import utcnow

from custom_components.phoenix_mcp.const import MESA_MODE_ADVISORY, MESA_MODE_OFF
from custom_components.phoenix_mcp.data import PhoenixData
from custom_components.phoenix_mcp.mcp_view import _call_tool
from custom_components.phoenix_mcp.token_store import PermissionNode, PermissionTree, TokenRecord


def _data(*, mesa_mode: str = MESA_MODE_OFF, mesa: object | None = None) -> PhoenixData:
    store = MagicMock()
    store.get_settings.return_value = SimpleNamespace(mesa_mode=mesa_mode)
    return PhoenixData(
        store=store,
        rate_limiter=MagicMock(),
        audit=MagicMock(),
        mesa=mesa,
        mesa_setup_failed=False,
    )


def _token(*, camera: str = "allow", camera_scope: str = "GREEN") -> TokenRecord:
    return TokenRecord(
        id=str(uuid.uuid4()),
        name="camera-test",
        token_hash="x",
        created_at=utcnow(),
        created_by="test",
        cap_camera_read=camera,
        permissions=PermissionTree(
            domains={"camera": PermissionNode(state=camera_scope)},
        ),
    )


def _camera_module():
    """Load HA's camera module without requiring its optional JPEG library."""
    turbojpeg = SimpleNamespace(TurboJPEG=type("TurboJPEG", (), {}))
    sys.modules.setdefault("turbojpeg", turbojpeg)
    return importlib.import_module("homeassistant.components.camera")


async def test_returns_standard_mcp_image_content(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    image = SimpleNamespace(content=b"jpeg-bytes", content_type="image/jpeg")

    camera = _camera_module()
    with patch.object(camera, "async_get_image", AsyncMock(return_value=image)) as get_image:
        result, outcome, resource = await _call_tool(
            "get_camera_image", {"entity_id": "camera.front_door"},
            _token(), hass, _data(), "req-1", None,
        )

    assert outcome == "allowed"
    assert resource == "camera.front_door"
    assert result.get("isError") is None
    assert result["content"][0]["type"] == "text"
    image_block = result["content"][1]
    assert image_block["type"] == "image"
    assert image_block["mimeType"] == "image/jpeg"
    assert image_block["data"]
    get_image.assert_awaited_once_with(hass, "camera.front_door", width=None, height=None)


async def test_requires_dedicated_capability_before_entity_lookup(hass) -> None:
    result, outcome, resource = await _call_tool(
        "get_camera_image", {"entity_id": "camera.secret"},
        _token(camera="deny"), hass, _data(), "req-1", None,
    )

    assert outcome == "denied"
    assert resource == "get_camera_image"
    assert result["content"][0]["text"].startswith("Forbidden:")


async def test_requires_camera_permission(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    result, outcome, _ = await _call_tool(
        "get_camera_image", {"entity_id": "camera.front_door"},
        _token(camera_scope="RED"), hass, _data(), "req-1", None,
    )

    assert outcome == "denied"
    assert result["content"][0]["text"] == "Entity not found."


async def test_rejects_invalid_dimensions_without_fetching(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    camera = _camera_module()
    with patch.object(camera, "async_get_image", AsyncMock()) as get_image:
        result, outcome, _ = await _call_tool(
            "get_camera_image", {"entity_id": "camera.front_door", "width": 2049},
            _token(), hass, _data(), "req-1", None,
        )

    assert outcome == "invalid_request"
    assert "width" in result["content"][0]["text"]
    get_image.assert_not_awaited()


async def test_rejects_empty_oversized_and_unsupported_images(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    camera = _camera_module()
    for image in (
        SimpleNamespace(content=b"", content_type="image/jpeg"),
        SimpleNamespace(content=b"x" * (4 * 1024 * 1024 + 1), content_type="image/jpeg"),
        SimpleNamespace(content=b"bytes", content_type="image/webp"),
    ):
        with patch.object(camera, "async_get_image", AsyncMock(return_value=image)):
            result, outcome, _ = await _call_tool(
                "get_camera_image", {"entity_id": "camera.front_door"},
                _token(), hass, _data(), "req-1", None,
            )
        assert outcome == "invalid_request"
        assert result["content"][0]["text"]


async def test_handles_camera_timeout_without_leaking_exception(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    camera = _camera_module()
    with patch.object(camera, "async_get_image", AsyncMock(side_effect=asyncio.TimeoutError)):
        result, outcome, _ = await _call_tool(
            "get_camera_image", {"entity_id": "camera.front_door"},
            _token(), hass, _data(), "req-1", None,
        )

    assert outcome == "invalid_request"
    assert result["content"][0]["text"] == "Unable to retrieve the camera image."


async def test_mesa_deny_for_is_honored_without_control_mode_gate(hass) -> None:
    hass.states.async_set("camera.front_door", "idle")
    profile = MagicMock(
        domain="camera",
        privacy_classification=MagicMock(),
        person_traits=SimpleNamespace(is_minor=False),
    )
    runtime = MagicMock()
    runtime.resolver.resolve.return_value = profile
    runtime.enforcer.privacy.evaluate.return_value = SimpleNamespace(allowed=False)
    data = _data(mesa_mode=MESA_MODE_ADVISORY, mesa=runtime)

    camera = _camera_module()
    with patch.object(camera, "async_get_image", AsyncMock()) as get_image:
        result, outcome, _ = await _call_tool(
            "get_camera_image", {"entity_id": "camera.front_door"},
            _token(), hass, data, "req-1", None,
        )

    assert outcome == "denied"
    assert result["content"][0]["text"] == "Entity not found."
    get_image.assert_not_awaited()
    runtime.enforcer.privacy.evaluate.assert_called_once()
