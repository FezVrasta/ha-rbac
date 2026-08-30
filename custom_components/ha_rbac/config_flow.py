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
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from . import http_config
from .const import (
    CONF_BIND_ADDRESS,
    CONF_MANAGE_HTTP,
    CONF_PROXY_PORT,
    CONF_RESTORE_ON_REMOVAL,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DATA_PREVIOUS_HTTP,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PROXY_PORT,
    DEFAULT_RESTORE_ON_REMOVAL,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
    LOOPBACK_BIND_WARNING,
)
from .util import async_upstream_is_loopback_only, is_loopback_bind


def _schema(
    defaults: dict[str, Any], *, manage: bool = False, moved: bool = False
) -> vol.Schema:
    """Return the configuration schema.

    `manage` adds the "keep Home Assistant out of the way" toggle, which the
    initial flow asks about in its own step instead, with room to explain what
    it will do.
    """
    port = NumberSelector(
        NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
    )
    fields: dict[Any, Any] = {
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
    if manage:
        fields[
            vol.Required(
                CONF_MANAGE_HTTP, default=defaults.get(CONF_MANAGE_HTTP, False)
            )
        ] = BooleanSelector()
    if moved:
        # Home Assistant offers no question at removal time -- an integration
        # gets one fire-and-forget callback and no way to prompt -- so the
        # question is asked here instead, while there is somewhere to ask it.
        fields[
            vol.Required(
                CONF_RESTORE_ON_REMOVAL,
                default=defaults.get(
                    CONF_RESTORE_ON_REMOVAL, DEFAULT_RESTORE_ON_REMOVAL
                ),
            )
        ] = BooleanSelector()
    return vol.Schema(fields)


class RbacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing chosen."""
        self._data: dict[str, Any] = {}

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
                self._data = data
                return await self.async_step_move()

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or self._suggested()),
            errors=errors,
            description_placeholders={"warning": await self._async_warning()},
        )

    @callback
    def _suggested(self) -> dict[str, Any]:
        """Return the ports to offer, read from the running server.

        The port Home Assistant answers on today is the one every browser,
        phone and bookmark already uses, so it is the one to take over --
        nothing then needs repointing afterwards. It is not always 8123: under
        Supervisor Home Assistant defaults to 80, and someone who chose their
        own port wants that one back.
        """
        current_port = getattr(self.hass.http, "server_port", None)
        if not current_port:
            return {}
        suggested: dict[str, Any] = {CONF_PROXY_PORT: current_port}
        if current_port == DEFAULT_UPSTREAM_PORT:
            # Home Assistant already sits where it would be moved to, so the
            # two defaults would collide and the form would refuse itself.
            suggested[CONF_UPSTREAM_PORT] = DEFAULT_UPSTREAM_PORT + 1
        return suggested

    async def _async_warning(self) -> str:
        """Return what to say about Home Assistant still being on the network.

        Which is nothing, in the ordinary case: it is on the network, that is
        exactly what the next step offers to change, and telling someone to go
        and do it by hand immediately before offering to do it for them reads
        as a contradiction. The warning is for the build where the offer cannot
        be made, since then the manual route really is the only one.
        """
        if async_upstream_is_loopback_only(self.hass):
            return ""
        if await http_config.async_can_manage(self.hass):
            return ""
        return (
            "Home Assistant is currently reachable from the network, so "
            "nothing here is enforced yet. Once this is answering, set Server "
            "host to 127.0.0.1 under Settings > System > Network. Until you "
            "do, anyone can bypass these rules by connecting to Home Assistant "
            "directly with the token they already have."
        )

    async def async_step_move(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to move Home Assistant out of the way.

        Doing it by hand is three steps that have to happen in one particular
        order, and getting it wrong is either a bind error or a lockout. This
        is the step that exists so nobody has to know that.
        """
        upstream_port = self._data[CONF_UPSTREAM_PORT]

        if user_input is not None:
            return self.async_create_entry(
                title="Access Control",
                data={**self._data, CONF_MANAGE_HTTP: user_input[CONF_MANAGE_HTTP]},
            )

        if not await http_config.async_can_manage(self.hass):
            # Nothing to offer, so do not offer it. The manual route is in the
            # README and the setup log says the same thing.
            return self.async_create_entry(
                title="Access Control",
                data={**self._data, CONF_MANAGE_HTTP: False},
            )

        warning = ""
        if is_loopback_bind(self._data.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS)):
            warning = f"\n\n{LOOPBACK_BIND_WARNING}"

        return self.async_show_form(
            step_id="move",
            data_schema=vol.Schema(
                {vol.Required(CONF_MANAGE_HTTP, default=True): BooleanSelector()}
            ),
            description_placeholders={
                "proxy_port": str(self._data[CONF_PROXY_PORT]),
                "upstream_port": str(upstream_port),
                "warning": warning,
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
        """Change the ports the proxy uses.

        A loopback bind with the move on is allowed, not refused: it is the
        right setup behind a reverse proxy on the same host. The warning about
        the case where nothing bridges the network is shown in the panel, which
        is the surface this integration is actually administered from.
        """
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PROXY_PORT: int(user_input[CONF_PROXY_PORT]),
                    CONF_BIND_ADDRESS: user_input[CONF_BIND_ADDRESS],
                    CONF_UPSTREAM_HOST: user_input[CONF_UPSTREAM_HOST],
                    CONF_UPSTREAM_PORT: int(user_input[CONF_UPSTREAM_PORT]),
                    CONF_MANAGE_HTTP: user_input.get(CONF_MANAGE_HTTP, False),
                    CONF_RESTORE_ON_REMOVAL: user_input.get(
                        CONF_RESTORE_ON_REMOVAL, DEFAULT_RESTORE_ON_REMOVAL
                    ),
                }
            )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                current,
                manage=await http_config.async_can_manage(self.hass),
                # Only worth asking where there is something to put back.
                moved=bool(self.config_entry.data.get(DATA_PREVIOUS_HTTP)),
            ),
        )
