"""Tests for advertising the proxy port over zeroconf.

Moving Home Assistant to a hidden port takes its own broadcast with it, so the
companion apps discover a port they cannot reach. These pin that the service is
re-registered on the port the proxy actually answers on.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from zeroconf.asyncio import AsyncServiceInfo

from custom_components.ha_rbac import discovery

HIDDEN_PORT = 8124
PROXY_PORT = 8123


def _service_info(port: int) -> AsyncServiceInfo:
    """Build a service info the way Home Assistant does, on a given port."""
    return AsyncServiceInfo(
        "_home-assistant._tcp.local.",
        name="Home._home-assistant._tcp.local.",
        server="uuid.local.",
        parsed_addresses=["192.168.1.10"],
        port=port,
        properties={
            "location_name": "Home",
            "uuid": "uuid",
            "version": "2026.9.0",
            "base_url": f"http://192.168.1.10:{HIDDEN_PORT}",
            "internal_url": f"http://192.168.1.10:{HIDDEN_PORT}",
            "external_url": "",
        },
    )


async def test_the_service_is_re_registered_on_the_proxy_port(
    hass: HomeAssistant,
) -> None:
    """The port and the URL properties both move to the proxy's port."""
    hass.config.components.add("zeroconf")
    aio_zc = AsyncMock()

    with (
        patch(
            "homeassistant.components.zeroconf.async_get_async_instance",
            AsyncMock(return_value=aio_zc),
        ),
        patch(
            "homeassistant.components.zeroconf._async_get_local_service_info",
            AsyncMock(return_value=_service_info(HIDDEN_PORT)),
        ),
    ):
        updated = await discovery.async_advertise_proxy_port(
            hass, HIDDEN_PORT, PROXY_PORT
        )

    assert updated is True
    aio_zc.async_register_service.assert_awaited_once()
    registered = aio_zc.async_register_service.await_args.args[0]
    assert registered.port == PROXY_PORT
    props = registered.decoded_properties
    assert props["internal_url"] == f"http://192.168.1.10:{PROXY_PORT}"
    assert props["base_url"] == f"http://192.168.1.10:{PROXY_PORT}"


async def test_nothing_happens_when_already_on_the_proxy_port(
    hass: HomeAssistant,
) -> None:
    """A record that already names the proxy port needs no rewrite."""
    hass.config.components.add("zeroconf")
    aio_zc = AsyncMock()

    with (
        patch(
            "homeassistant.components.zeroconf.async_get_async_instance",
            AsyncMock(return_value=aio_zc),
        ),
        patch(
            "homeassistant.components.zeroconf._async_get_local_service_info",
            AsyncMock(return_value=_service_info(PROXY_PORT)),
        ),
    ):
        updated = await discovery.async_advertise_proxy_port(
            hass, HIDDEN_PORT, PROXY_PORT
        )

    assert updated is False
    aio_zc.async_register_service.assert_not_awaited()


async def test_a_missing_zeroconf_api_is_survived(hass: HomeAssistant) -> None:
    """Every entry point degrades to leaving the advertisement alone."""
    hass.config.components.add("zeroconf")
    with patch(
        "homeassistant.components.zeroconf.async_get_async_instance",
        AsyncMock(side_effect=RuntimeError("no zeroconf")),
    ):
        updated = await discovery.async_advertise_proxy_port(
            hass, HIDDEN_PORT, PROXY_PORT
        )

    assert updated is False


async def test_nothing_happens_when_zeroconf_is_not_loaded(
    hass: HomeAssistant,
) -> None:
    """With no zeroconf running, the advertisement is left alone entirely."""
    with patch(
        "homeassistant.components.zeroconf.async_get_async_instance"
    ) as get_instance:
        updated = await discovery.async_advertise_proxy_port(
            hass, HIDDEN_PORT, PROXY_PORT
        )

    assert updated is False
    get_instance.assert_not_called()
