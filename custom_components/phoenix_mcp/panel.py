"""Panel registration for the Phoenix MCP admin UI."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import (
    add_extra_js_url,
    async_register_built_in_panel,
    async_remove_panel,
    remove_extra_js_url,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import DOMAIN, MESA_INJECT_MIN_HA

_LOGGER = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_JS_FILE = _FRONTEND_DIR / "phoenix-mcp-panel.js"
_PANEL_URL = "/local/phoenix-mcp"
_JS_URL = f"{_PANEL_URL}/phoenix-mcp-panel.js"
_PANEL_KEY = "phoenix-mcp"
_PANEL_REGISTERED_KEY = "phoenix_mcp_panel_registered"

# Optional in-context profile injector (mesa_inject_enabled). Served from the same
# static dir as the panel, but loaded on EVERY HA page via the frontend extra-module
# mechanism (admin-gated and feature-detected in JS), not as a panel.
_INJECT_JS_FILE = _FRONTEND_DIR / "phoenix-mcp-inject.js"
_INJECT_JS_URL = f"{_PANEL_URL}/phoenix-mcp-inject.js"
_INJECT_REGISTERED_URL_KEY = "phoenix_mcp_inject_registered_url"

# Optional global Agent Chat window (agentcli_global). Same extra-module mechanism
# as the MESA injector: loaded on every HA page so the chat window can float over
# the whole HA UI. It is still opened only from the Phoenix MCP panel button, and it
# self-disables (falling back to the panel-only window) when injection is not
# supported. Independent of the kill switch here; the panel gates showing it.
_AGENTCHAT_JS_FILE = _FRONTEND_DIR / "phoenix-mcp-agentchat.js"
_AGENTCHAT_JS_URL = f"{_PANEL_URL}/phoenix-mcp-agentchat.js"
_AGENTCHAT_REGISTERED_URL_KEY = "phoenix_mcp_agentchat_registered_url"


def _js_url_with_cache_bust() -> str:
    """Append the bundle's mtime as a query param so each rebuild busts the cache.

    The panel JS is served without cache headers, so browsers otherwise keep a
    stale copy across frontend builds. Reading the mtime ties the URL to the
    actual file on disk; no version constant to keep in sync. Runs in the
    executor (see caller) since it touches the filesystem.
    """
    try:
        return f"{_JS_URL}?v={int(_JS_FILE.stat().st_mtime)}"
    except OSError:
        return _JS_URL


async def async_register_phoenix_panel(hass: HomeAssistant) -> None:
    """Register the static frontend bundle and the Lovelace panel.

    Safe to call on re-setup: removes any stale panel entry before registering.
    Static path registration is skipped silently if already registered.
    """
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                url_path=_PANEL_URL,
                path=str(_FRONTEND_DIR),
                cache_headers=False,
            )
        ])
    except RuntimeError as exc:
        _LOGGER.warning("Phoenix MCP: failed to register static path %s: %s", _PANEL_URL, exc)

    if hass.data.get(_PANEL_REGISTERED_KEY):
        async_remove_panel(hass, _PANEL_KEY)

    js_url = await hass.async_add_executor_job(_js_url_with_cache_bust)

    async_register_built_in_panel(
        hass=hass,
        component_name="custom",
        # The brand mark, Latin in every language: Home Assistant keeps its own
        # name untranslated in all 148 of its zh-Hans strings, and several places
        # name this integration in HA's own UI where it cannot be localized.
        sidebar_title="Phoenix MCP",
        sidebar_icon="mdi:fire",
        frontend_url_path=_PANEL_KEY,
        require_admin=True,
        config={
            "_panel_custom": {
                "name": "phoenix-mcp-panel",
                "js_url": js_url,
            }
        },
    )
    hass.data[_PANEL_REGISTERED_KEY] = True


def remove_phoenix_panel(hass: HomeAssistant) -> None:
    """Remove the panel if it was registered in this session.

    Silently skips if the panel was never registered (e.g. unload before setup
    completed, or HA restarted with the kill switch enabled).
    """
    if hass.data.pop(_PANEL_REGISTERED_KEY, False):
        async_remove_panel(hass, _PANEL_KEY)


def _inject_url_with_cache_bust() -> str:
    """The injector module URL with the bundle mtime appended (cache-bust).

    Runs in the executor (touches the filesystem). Falls back to the bare URL if
    the file is missing.
    """
    try:
        return f"{_INJECT_JS_URL}?v={int(_INJECT_JS_FILE.stat().st_mtime)}"
    except OSError:
        return _INJECT_JS_URL


def _inject_version_ok() -> bool:
    """Whether the running HA is at or above the injector feature baseline.

    The version comes from the package constant, which is the only always-present
    source. ``hass.config`` carries no ``version`` attribute, so reading one
    raises, and with the fail-open catch below that turned this whole gate into a
    constant True: the baseline was declared but never applied.

    Fail-open is still right when the version cannot be PARSED: the in-page
    feature-detection self-disables on a DOM it does not recognise, so the worst
    case is a script that adds nothing. This gate only avoids loading it on
    known-incompatible old HA; it is not a safety boundary.
    """
    try:
        from awesomeversion import AwesomeVersion

        return AwesomeVersion(HA_VERSION) >= AwesomeVersion(MESA_INJECT_MIN_HA)
    except Exception:  # noqa: BLE001 - version parsing must never disable the feature outright
        return True


async def async_sync_mesa_inject(hass: HomeAssistant) -> None:
    """Add or remove the in-context profile injector module to match settings.

    Idempotent; safe to call at setup and after any settings change. Gated on the
    mesa_inject_enabled setting and a soft HA-version baseline, never on the kill
    switch (this is an admin convenience, like the panel). The module is served
    from the panel's existing static path, so no extra path registration is needed.
    """
    data = hass.data.get(DOMAIN)
    if data is None:
        return
    enabled = data.store.get_settings().mesa_inject_enabled and _inject_version_ok()
    prior = hass.data.get(_INJECT_REGISTERED_URL_KEY)

    if not enabled:
        if prior:
            _remove_all_inject_urls(hass)
            _safe_remove_inject_url(hass, prior)
            hass.data.pop(_INJECT_REGISTERED_URL_KEY, None)
        return

    url = await hass.async_add_executor_job(_inject_url_with_cache_bust)
    if prior == url:
        return  # already current
    # Remove every prior inject URL (tracked or orphaned) before adding, so HA only
    # ever holds one phoenix-mcp-inject module and a full reload cannot start two instances.
    _remove_all_inject_urls(hass)
    if prior:
        _safe_remove_inject_url(hass, prior)
    add_extra_js_url(hass, url)  # es5=False -> loaded as an ES module
    hass.data[_INJECT_REGISTERED_URL_KEY] = url


def remove_mesa_inject(hass: HomeAssistant) -> None:
    """Remove the injector module URL on unload, if registered."""
    prior = hass.data.pop(_INJECT_REGISTERED_URL_KEY, None)
    if prior:
        _safe_remove_inject_url(hass, prior)


def _safe_remove_inject_url(hass: HomeAssistant, url: str) -> None:
    try:
        remove_extra_js_url(hass, url)
    except Exception:  # noqa: BLE001 - removal is best-effort; never block teardown
        _LOGGER.debug("Phoenix MCP: failed to remove inject module URL %s", url, exc_info=True)


def _remove_all_inject_urls(hass: HomeAssistant) -> None:
    """Remove every registered injector module URL, whatever its cache-bust value.

    Belt-and-suspenders so HA never holds two phoenix-mcp-inject modules at once: a full
    page load would otherwise run them as two fighting injector instances. Catches
    a stale URL orphaned in HA's extra-module set if our own tracking got out of
    sync (e.g. a prior removal that raised). Best-effort; never raises. Falls back
    to the caller's tracked-prior removal if the set cannot be read on this HA.
    """
    try:
        from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

        registered = hass.data.get(DATA_EXTRA_MODULE_URL)
        if not registered:
            return
        stale = [
            u for u in list(registered)  # type: ignore[call-overload]  # UrlManager is iterable at runtime, not declared so
            if u == _INJECT_JS_URL or u.startswith(f"{_INJECT_JS_URL}?")
        ]
        for u in stale:
            _safe_remove_inject_url(hass, u)
    except Exception:  # noqa: BLE001 - introspection is best-effort
        _LOGGER.debug("Phoenix MCP: inject module URL sweep failed", exc_info=True)


def _agentchat_url_with_cache_bust() -> str:
    """The Agent Chat module URL with the bundle mtime appended (cache-bust)."""
    try:
        return f"{_AGENTCHAT_JS_URL}?v={int(_AGENTCHAT_JS_FILE.stat().st_mtime)}"
    except OSError:
        return _AGENTCHAT_JS_URL


def _remove_all_agentchat_urls(hass: HomeAssistant) -> None:
    """Remove every registered Agent Chat module URL, whatever its cache-bust value."""
    try:
        from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

        registered = hass.data.get(DATA_EXTRA_MODULE_URL)
        if not registered:
            return
        stale = [
            u for u in list(registered)  # type: ignore[call-overload]  # UrlManager is iterable at runtime, not declared so
            if u == _AGENTCHAT_JS_URL or u.startswith(f"{_AGENTCHAT_JS_URL}?")
        ]
        for u in stale:
            _safe_remove_inject_url(hass, u)
    except Exception:  # noqa: BLE001 - introspection is best-effort
        _LOGGER.debug("Phoenix MCP: Agent Chat module URL sweep failed", exc_info=True)


async def async_sync_agentchat_inject(hass: HomeAssistant) -> None:
    """Add or remove the global Agent Chat window module to match settings.

    Idempotent; gated on the agentcli_global setting and the same soft HA-version
    baseline as the profile injector. Independent of the kill switch (the panel
    decides whether to actually show the window; when injection is off or
    unsupported the window falls back to the panel only).
    """
    data = hass.data.get(DOMAIN)
    if data is None:
        return
    enabled = data.store.get_settings().agentcli_global and _inject_version_ok()
    prior = hass.data.get(_AGENTCHAT_REGISTERED_URL_KEY)

    if not enabled:
        if prior:
            _remove_all_agentchat_urls(hass)
            _safe_remove_inject_url(hass, prior)
            hass.data.pop(_AGENTCHAT_REGISTERED_URL_KEY, None)
        return

    url = await hass.async_add_executor_job(_agentchat_url_with_cache_bust)
    if prior == url:
        return
    _remove_all_agentchat_urls(hass)
    if prior:
        _safe_remove_inject_url(hass, prior)
    add_extra_js_url(hass, url)  # es5=False -> loaded as an ES module
    hass.data[_AGENTCHAT_REGISTERED_URL_KEY] = url


def remove_agentchat_inject(hass: HomeAssistant) -> None:
    """Remove the Agent Chat module URL on unload, if registered."""
    prior = hass.data.pop(_AGENTCHAT_REGISTERED_URL_KEY, None)
    if prior:
        _safe_remove_inject_url(hass, prior)
