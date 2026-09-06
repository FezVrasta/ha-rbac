"""Advertise the proxy's port over zeroconf.

Moving Home Assistant to a hidden loopback port takes its own
`_home-assistant._tcp.local.` broadcast with it: Home Assistant advertises the
port it binds, which is now the one nothing off the machine can reach. The
companion apps discover that port and offer it, so a fresh install auto-detects
an address it cannot connect to.

This corrects the record Home Assistant already registered so it names the port
the proxy actually answers on. None of the API is public, so every entry point
degrades to "left it alone" rather than raising: a broken advertisement is a
worse setup experience, not a broken instance.
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class Unavailable(Exception):
    """Home Assistant does not expose zeroconf the way this expects."""


def _api() -> Any:
    """Return the zeroconf module, the service type and the info class."""
    try:
        from homeassistant.components import zeroconf  # noqa: PLC0415
        from homeassistant.components.zeroconf.const import (  # noqa: PLC0415
            ZEROCONF_TYPE,
        )
        from zeroconf.asyncio import AsyncServiceInfo  # noqa: PLC0415
    except ImportError as err:
        raise Unavailable(str(err)) from err
    if not hasattr(zeroconf, "async_get_async_instance"):
        raise Unavailable("homeassistant.components.zeroconf has no instance getter")
    return zeroconf, ZEROCONF_TYPE, AsyncServiceInfo


def _rewrite_port(value: str, hidden_port: int, proxy_port: int) -> str:
    """Return a URL property with the hidden port swapped for the proxy port.

    Only a URL that names the hidden port is touched. A user who configured an
    explicit internal or external URL meant it, and it is left as it stands.
    """
    if not value:
        return value
    return value.replace(f":{hidden_port}", f":{proxy_port}")


def _registered(aio_zc: Any, zeroconf_type: str) -> Any:
    """Return the instance's own registered service record.

    Raises Unavailable if there is not exactly one to correct. None means the
    broadcast has not started yet, and registering one here would be racing
    Home Assistant for the name rather than fixing what it published.
    """
    infos = aio_zc.zeroconf.registry.async_get_infos_type(zeroconf_type)
    if len(infos) != 1:
        raise Unavailable(f"{len(infos)} local records registered for {zeroconf_type}")
    return infos[0]


async def async_advertise_proxy_port(hass: HomeAssistant, proxy_port: int) -> bool:
    """Point the instance's zeroconf record at the proxy port.

    Returns True if the advertisement was updated.
    """
    if "zeroconf" not in hass.config.components:
        return False
    try:
        zeroconf, zeroconf_type, service_info_cls = _api()
        aio_zc = await zeroconf.async_get_async_instance(hass)
        info = _registered(aio_zc, zeroconf_type)
        if info.port == proxy_port:
            return False

        properties = {
            key: (
                _rewrite_port(value, info.port, proxy_port)
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
        # Updated, not registered afresh. Registering replays the name check,
        # and the name is already answering on the network -- its own. So
        # zeroconf takes it for a collision, renames this record to "<name>-2"
        # and leaves the original, still naming the port nothing can reach,
        # registered beside it. Two instances would show up in the companion
        # app, and the one that reads as the real one would be the broken one.
        await aio_zc.async_update_service(corrected)
    except Exception as err:  # noqa: BLE001
        # Deliberately broad, for the reason in the module docstring: none of
        # this is API anyone promised to keep, and every way it can be gone has
        # to come back as "the advertisement is as Home Assistant left it".
        _LOGGER.debug("Could not advertise the proxy port over zeroconf: %s", err)
        return False
    _LOGGER.info("Advertising Home Assistant on port %s over zeroconf", proxy_port)
    return True
