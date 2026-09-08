"""Advertise the proxy's port to whatever would otherwise be handed the hidden one.

Moving Home Assistant to a hidden loopback port takes two different broadcasts
of that port with it. Zeroconf's `_home-assistant._tcp.local.` record is one:
the companion apps discover it and offer a port they cannot connect to. Home
Assistant's own auto-detected internal URL is the other, read by anything that
needs to hand a device a URL rather than have a browser type one in -- an
ESPHome voice satellite fetching the audio for its own spoken reply among them.

Both are corrected here. None of the zeroconf API is public, so that half
degrades to "left it alone" rather than raising: a broken advertisement is a
worse setup experience, not a broken instance.
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

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


@callback
def async_correct_internal_url(hass: HomeAssistant, proxy_port: int) -> bool:
    """Point Home Assistant's auto-detected internal URL at the proxy port.

    `homeassistant.helpers.network.get_url()` returns `hass.config.internal_url`
    outright when one is configured, and only falls back to
    `hass.config.api.local_ip` and `hass.config.api.port` when it is not. That
    fallback port is Home Assistant's own -- now the hidden loopback port this
    integration moved it to -- so anything handed a URL rather than one a
    person typed in gets an address nothing off the machine can reach. An
    ESPHome voice satellite fetching the audio for its own spoken reply is
    exactly this: the reply is generated, the proxy is never asked for it, and
    the satellite gets nothing.

    Left alone if an internal URL is already configured. That is what the
    fallback exists for, and a URL entered under Settings > System > Network on
    purpose already names a port this integration keeps free to answer on, so
    it is not ours to second-guess.

    Set directly on `hass.config` rather than through `async_update`, so
    nothing is written to storage. Reapplying this on every start is what keeps
    a changed proxy port in step, and it means removing this integration, or
    setting an internal URL by hand later, needs nothing undone here.
    """
    if hass.config.internal_url:
        return False
    api = hass.config.api
    if api is None or not api.local_ip:
        return False
    scheme = "https" if api.use_ssl else "http"
    hass.config.internal_url = f"{scheme}://{api.local_ip}:{proxy_port}"
    _LOGGER.info(
        "Setting Home Assistant's internal URL to %s, so devices such as "
        "ESPHome voice satellites can reach media the proxy serves",
        hass.config.internal_url,
    )
    return True
