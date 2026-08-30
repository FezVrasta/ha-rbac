"""Tests for the deployment check.

Everything this integration enforces rests on Home Assistant not being
reachable from the network. That is a configuration property, not a code one,
so it is the most likely way for the whole layer to become decorative.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.util import (
    async_upstream_is_loopback_only,
    is_loopback_bind,
)


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
