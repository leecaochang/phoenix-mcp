"""Typed base class for Phoenix MCP's aiohttp views.

`HomeAssistantView` does not declare `hass`. HA's own `register_view` never sets
it either (it only checks `url`/`name`, then calls `view.register(hass, ...)`),
which is exactly why Phoenix assigns it explicitly at registration:

    view = view_cls()
    view.hass = hass
    hass.http.register_view(view)

That assignment is what makes the attribute exist at runtime, but it tells a
type checker nothing, so without this class every `self.hass` across the view
modules reads as an undeclared attribute. That is the single largest source of
type-check noise in the package, and enough of it to bury anything real.

This class exists solely to declare that attribute. `hass: HomeAssistant` is an
ANNOTATION, not an assignment, so no class attribute is created, `hasattr` is
unchanged, and there is no runtime behaviour here whatsoever. Views keep
inheriting everything else from `HomeAssistantView`.

Kept in its own module rather than `helpers.py` so it can be imported by every
view module (admin_view, agentcli, mcp_view, proxy_view, skill_view) with no
possibility of an import cycle.
"""

from __future__ import annotations

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant


class PhoenixView(HomeAssistantView):
    """A HomeAssistantView that declares the `hass` attribute Phoenix assigns."""

    hass: HomeAssistant
