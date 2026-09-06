"""Tests for advertising the proxy port over zeroconf.

Moving Home Assistant to a hidden port takes its own broadcast with it, so the
companion apps discover a port they cannot reach. These pin that the record
Home Assistant registered ends up naming the port the proxy answers on.

They drive zeroconf's real `ServiceRegistry` rather than a mock, because the
way this breaks is a second record appearing beside the first -- which a mock
records as a call and reports as a pass.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from zeroconf import ServiceRegistry
from zeroconf.asyncio import AsyncServiceInfo

from custom_components.ha_rbac import discovery

HIDDEN_PORT = 8124
PROXY_PORT = 8123
ZEROCONF_TYPE = "_home-assistant._tcp.local."


def _service_info(port: int) -> AsyncServiceInfo:
    """Build a service info the way Home Assistant does, on a given port."""
    return AsyncServiceInfo(
        ZEROCONF_TYPE,
        name=f"Home.{ZEROCONF_TYPE}",
        server="uuid.local.",
        parsed_addresses=["192.168.1.10"],
        port=port,
        properties={
            "location_name": "Home",
            "uuid": "uuid",
            "version": "2026.9.0",
            "base_url": f"http://192.168.1.10:{port}",
            "internal_url": f"http://192.168.1.10:{port}",
            "external_url": "",
        },
    )


class _FakeAsyncZeroconf:
    """The half of `HaAsyncZeroconf` this touches, over a real registry."""

    def __init__(self, registered: AsyncServiceInfo | None = None) -> None:
        """Start from what Home Assistant has already broadcast, if anything."""
        self.registry = ServiceRegistry()
        if registered is not None:
            self.registry.async_add(registered)
        self.zeroconf = SimpleNamespace(registry=self.registry)

    async def async_update_service(self, info: AsyncServiceInfo) -> None:
        """Replace the record under its own name, as zeroconf does."""
        self.registry.async_update(info)

    async def async_register_service(self, info: AsyncServiceInfo, **kwargs) -> None:
        """Fail the way zeroconf does when the name is already taken."""
        raise AssertionError("the record is Home Assistant's to hold, not ours to add")


def _advertising(zeroconf: _FakeAsyncZeroconf) -> list[tuple[str, int]]:
    """Return every record being broadcast for the Home Assistant service."""
    return [
        (info.name, info.port)
        for info in zeroconf.registry.async_get_infos_type(ZEROCONF_TYPE)
    ]


def _patch(zeroconf: _FakeAsyncZeroconf | AsyncMock) -> object:
    """Hand the integration our zeroconf instance instead of the real one."""
    return patch(
        "homeassistant.components.zeroconf.async_get_async_instance",
        AsyncMock(return_value=zeroconf),
    )


async def test_the_advertised_record_moves_to_the_proxy_port(
    hass: HomeAssistant,
) -> None:
    """The port and the URL properties both move to the proxy's port."""
    hass.config.components.add("zeroconf")
    zeroconf = _FakeAsyncZeroconf(_service_info(HIDDEN_PORT))

    with _patch(zeroconf):
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is True
    (registered,) = zeroconf.registry.async_get_infos_type(ZEROCONF_TYPE)
    assert registered.port == PROXY_PORT
    props = registered.decoded_properties
    assert props["internal_url"] == f"http://192.168.1.10:{PROXY_PORT}"
    assert props["base_url"] == f"http://192.168.1.10:{PROXY_PORT}"
    assert props["uuid"] == "uuid"


async def test_the_correction_replaces_the_record_rather_than_joining_it(
    hass: HomeAssistant,
) -> None:
    """One instance is advertised, under the name it already had.

    Registering the corrected record instead of updating it renames it to
    "Home-2" and leaves the original -- pointing at the hidden port -- beside
    it, which is a worse answer than not correcting it at all.
    """
    hass.config.components.add("zeroconf")
    zeroconf = _FakeAsyncZeroconf(_service_info(HIDDEN_PORT))

    with _patch(zeroconf):
        await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert _advertising(zeroconf) == [(f"Home.{ZEROCONF_TYPE}", PROXY_PORT)]


async def test_nothing_happens_when_already_on_the_proxy_port(
    hass: HomeAssistant,
) -> None:
    """A record that already names the proxy port needs no rewrite."""
    hass.config.components.add("zeroconf")
    zeroconf = _FakeAsyncZeroconf(_service_info(PROXY_PORT))

    with _patch(zeroconf):
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is False
    assert _advertising(zeroconf) == [(f"Home.{ZEROCONF_TYPE}", PROXY_PORT)]


async def test_nothing_is_advertised_before_home_assistant_broadcasts(
    hass: HomeAssistant,
) -> None:
    """With no record registered yet, this has nothing to correct.

    Adding one here would be racing Home Assistant for its own name rather
    than fixing what it published.
    """
    hass.config.components.add("zeroconf")
    zeroconf = _FakeAsyncZeroconf()

    with _patch(zeroconf):
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is False
    assert _advertising(zeroconf) == []


async def test_a_missing_zeroconf_api_is_survived(hass: HomeAssistant) -> None:
    """Every entry point degrades to leaving the advertisement alone."""
    hass.config.components.add("zeroconf")
    with patch(
        "homeassistant.components.zeroconf.async_get_async_instance",
        AsyncMock(side_effect=RuntimeError("no zeroconf")),
    ):
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is False


async def test_a_drifted_zeroconf_api_is_survived(hass: HomeAssistant) -> None:
    """An instance that no longer keeps a registry is not reached into."""
    hass.config.components.add("zeroconf")
    with _patch(SimpleNamespace(zeroconf=SimpleNamespace())):
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is False


async def test_nothing_happens_when_zeroconf_is_not_loaded(
    hass: HomeAssistant,
) -> None:
    """With no zeroconf running, the advertisement is left alone entirely."""
    with patch(
        "homeassistant.components.zeroconf.async_get_async_instance"
    ) as get_instance:
        updated = await discovery.async_advertise_proxy_port(hass, PROXY_PORT)

    assert updated is False
    get_instance.assert_not_called()
