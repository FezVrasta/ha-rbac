"""Advertise the proxy's port over zeroconf.

Moving Home Assistant to a hidden loopback port takes its own
`_home-assistant._tcp.local.` broadcast with it: Home Assistant advertises the
port it binds, which is now the one nothing off the machine can reach. The
companion apps discover that port and offer it, so a fresh install auto-detects
an address it cannot connect to.

This re-registers the same service with the port the proxy actually answers on.
None of the API is public, so every entry point degrades to "left it alone"
rather than raising: a broken advertisement is a worse setup experience, not a
broken instance.
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class Unavailable(Exception):
    """Home Assistant does not expose zeroconf the way this expects."""


def _api() -> Any:
    """Return the zeroconf module and service-info class, or raise Unavailable."""
    try:
        from homeassistant.components import zeroconf  # noqa: PLC0415
        from homeassistant.components.zeroconf.const import (  # noqa: PLC0415
            ZEROCONF_TYPE,
        )
        from zeroconf.asyncio import AsyncServiceInfo  # noqa: PLC0415
    except ImportError as err:
        raise Unavailable(str(err)) from err
    for name in ("async_get_async_instance", "_async_get_local_service_info"):
        if not hasattr(zeroconf, name):
            raise Unavailable(f"homeassistant.components.zeroconf has no {name}")
    return zeroconf, ZEROCONF_TYPE, AsyncServiceInfo


def _rewrite_port(value: str, hidden_port: int, proxy_port: int) -> str:
    """Return a URL property with the hidden port swapped for the proxy port.

    Only a URL that names the hidden port is touched. A user who configured an
    explicit internal or external URL meant it, and it is left as it stands.
    """
    if not value:
        return value
    return value.replace(f":{hidden_port}", f":{proxy_port}")


async def async_advertise_proxy_port(
    hass: HomeAssistant, hidden_port: int, proxy_port: int
) -> bool:
    """Re-register the instance's zeroconf service on the proxy port.

    Returns True if the advertisement was updated.
    """
    if "zeroconf" not in hass.config.components:
        return False
    try:
        zeroconf, zeroconf_type, service_info_cls = _api()
        aio_zc = await zeroconf.async_get_async_instance(hass)
        info = await zeroconf._async_get_local_service_info(hass)  # noqa: SLF001
        if info.port == proxy_port:
            return False

        properties = {
            key: (
                _rewrite_port(value, hidden_port, proxy_port)
                if isinstance(value, str)
                else value
            )
            for key, value in dict(info.decoded_properties).items()
        }

        corrected = service_info_cls(
            zeroconf_type,
            name=info.name,
            server=info.server,
            parsed_addresses=info.parsed_addresses(),
            port=proxy_port,
            properties=properties,
        )
        await aio_zc.async_register_service(corrected, allow_name_change=True)
    except (Unavailable, OSError, RuntimeError, AttributeError) as err:
        _LOGGER.debug("Could not advertise the proxy port over zeroconf: %s", err)
        return False
    _LOGGER.info("Advertising Home Assistant on port %s over zeroconf", proxy_port)
    return True
