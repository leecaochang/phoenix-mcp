"""Config flow for the Phoenix MCP integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN

class PhoenixConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-step config flow that enforces a single Phoenix MCP instance per HA installation."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the user-initiated setup step. Creates the entry immediately on submit."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Phoenix MCP", data={})
        return self.async_show_form(step_id="user")
