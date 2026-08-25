"""Config flow for RBAC access control."""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BIND_ADDRESS,
    CONF_PROXY_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PROXY_PORT,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
)
from .util import async_upstream_is_loopback_only


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Return the configuration schema."""
    port = NumberSelector(
        NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_PROXY_PORT,
                default=defaults.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT),
            ): port,
            vol.Required(
                CONF_BIND_ADDRESS,
                default=defaults.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS),
            ): TextSelector(),
            vol.Required(
                CONF_UPSTREAM_HOST,
                default=defaults.get(CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST),
            ): TextSelector(),
            vol.Required(
                CONF_UPSTREAM_PORT,
                default=defaults.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT),
            ): port,
        }
    )


class RbacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the proxy."""
        self._async_abort_entries_match()

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_PROXY_PORT: int(user_input[CONF_PROXY_PORT]),
                CONF_BIND_ADDRESS: user_input[CONF_BIND_ADDRESS],
                CONF_UPSTREAM_HOST: user_input[CONF_UPSTREAM_HOST],
                CONF_UPSTREAM_PORT: int(user_input[CONF_UPSTREAM_PORT]),
            }
            if data[CONF_PROXY_PORT] == data[CONF_UPSTREAM_PORT]:
                errors[CONF_PROXY_PORT] = "port_conflict"
            else:
                return self.async_create_entry(title="Access Control", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
            description_placeholders={
                "warning": (
                    ""
                    if async_upstream_is_loopback_only(self.hass)
                    else (
                        "Home Assistant is currently reachable from the "
                        "network, so nothing here is enforced yet. Once this is "
                        "answering, set Server host to 127.0.0.1 under Settings "
                        "> System > Network. Until you do, anyone can bypass "
                        "these rules by connecting to Home Assistant directly "
                        "with the token they already have."
                    )
                )
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return RbacOptionsFlow()


class RbacOptionsFlow(OptionsFlow):
    """Handle reconfiguration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the ports the proxy uses."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PROXY_PORT: int(user_input[CONF_PROXY_PORT]),
                    CONF_BIND_ADDRESS: user_input[CONF_BIND_ADDRESS],
                    CONF_UPSTREAM_HOST: user_input[CONF_UPSTREAM_HOST],
                    CONF_UPSTREAM_PORT: int(user_input[CONF_UPSTREAM_PORT]),
                }
            )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
