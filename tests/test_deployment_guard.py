"""Tests for the deployment check.

Everything this integration enforces rests on Home Assistant not being
reachable from the network. That is a configuration property, not a code one,
so it is the most likely way for the whole layer to become decorative.
"""

import asyncio
import socket

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_rbac import async_setup_entry, async_unload_entry
from custom_components.ha_rbac.const import (
    CONF_BIND_ADDRESS,
    CONF_PROXY_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DATA_RBAC,
    DOMAIN,
)
from custom_components.ha_rbac.util import (
    async_upstream_is_loopback_only,
    is_loopback_bind,
)


def _free_port() -> int:
    """Return an unused TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize(
    ("server_host", "expected"),
    [
        pytest.param(["127.0.0.1"], True, id="loopback"),
        pytest.param(["::1"], True, id="loopback-v6"),
        pytest.param(["127.0.0.1", "::1"], True, id="both-loopbacks"),
        pytest.param(["0.0.0.0"], False, id="all-interfaces"),
        pytest.param(["192.168.1.10"], False, id="lan-address"),
        pytest.param(["127.0.0.1", "192.168.1.10"], False, id="one-lan-address"),
        pytest.param([], False, id="unset"),
        pytest.param(None, False, id="none"),
    ],
)
async def test_loopback_detection(
    hass: HomeAssistant, server_host: list[str] | None, expected: bool
) -> None:
    """A single non-loopback address is enough to defeat the whole layer."""
    await async_setup_component(hass, "http", {"http": {}})
    await hass.async_block_till_done()
    hass.http.server_host = server_host

    assert async_upstream_is_loopback_only(hass) is expected


async def test_unparseable_host_is_treated_as_exposed(hass: HomeAssistant) -> None:
    """An unrecognised value must not be assumed safe."""
    await async_setup_component(hass, "http", {"http": {}})
    await hass.async_block_till_done()
    hass.http.server_host = ["not-an-ip"]

    assert async_upstream_is_loopback_only(hass) is False


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        pytest.param("127.0.0.1", True, id="loopback"),
        pytest.param("127.0.0.2", True, id="loopback-block"),
        pytest.param(" 127.0.0.1 ", True, id="whitespace"),
        pytest.param("::1", True, id="loopback-v6"),
        pytest.param("::ffff:127.0.0.1", True, id="mapped-v4"),
        pytest.param("localhost", True, id="localhost"),
        pytest.param("0.0.0.0", False, id="all-interfaces"),
        pytest.param("192.168.1.10", False, id="lan-address"),
        pytest.param("not-an-ip", False, id="unparseable"),
    ],
)
def test_bind_loopback_detection(address: str, expected: bool) -> None:
    """The whole 127.0.0.0/8 block counts, not just 127.0.0.1.

    A string match on the one address let 127.0.0.2 and IPv4-mapped forms
    through, so binding to those looked reachable when it was not.
    """
    assert is_loopback_bind(address) is expected


async def test_nothing_is_reachable_before_the_proxy_starts(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """The startup window is a closed port, not an unfiltered one (#21).

    Enforcement lives entirely in the proxy, and on boot the proxy is deferred
    to the started event -- so there is a stretch where Home Assistant is up
    and nothing is filtering. The worry is that a token holder gets through
    unjudged during it, since a Home Assistant token is not port-scoped.

    They cannot, on a correctly moved instance, and this pins why. Home
    Assistant binds `server_host` when its own HTTP server starts, early in
    bootstrap, and on a moved instance that is loopback. So for the whole
    window the public port has nothing listening on it at all: connections are
    refused rather than served. There is no unfiltered path to take, because
    the thing being bypassed is a closed socket.

    What that leaves is the ordinary requirement, already checked at setup and
    reported loudly: if Home Assistant is *not* loopback-only, the window is
    real and so is every other moment. That is the deployment mistake the rest
    of this file is about, not a property of startup.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    # As a moved instance comes up: Home Assistant off the network, the port
    # everyone actually uses not yet taken by anything.
    hass.http.server_host = ["127.0.0.1"]
    public_port = _free_port()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PROXY_PORT: public_port,
            CONF_BIND_ADDRESS: "0.0.0.0",
            CONF_UPSTREAM_HOST: "127.0.0.1",
            CONF_UPSTREAM_PORT: _free_port(),
        },
    )
    entry.add_to_hass(hass)

    hass.set_state(CoreState.not_running)
    assert await async_setup_entry(hass, entry)

    # The window itself: set up, enforcing nothing, proxy not yet started.
    assert hass.data[DATA_RBAC].proxy is None, "precondition: the start is deferred"
    assert async_upstream_is_loopback_only(hass) is True

    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", public_port)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert hass.data[DATA_RBAC].proxy is not None, "and then it takes the port"

    assert await async_unload_entry(hass, entry)
    await hass.async_block_till_done()
