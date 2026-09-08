"""Tests for correcting Home Assistant's auto-detected internal URL.

Moving Home Assistant to a hidden loopback port breaks `get_url()`'s fallback
for anyone who has not set an internal URL explicitly: it falls back to
`hass.config.api.port`, which is now the hidden port. That is what an ESPHome
voice satellite fetches its TTS reply from, so an uncorrected instance hands
it an address nothing off the machine can reach.
"""

from types import SimpleNamespace

from homeassistant.core import HomeAssistant

from custom_components.ha_rbac import discovery

PROXY_PORT = 8123


def _api(local_ip: str = "192.168.1.10", use_ssl: bool = False) -> SimpleNamespace:
    """Build a stand-in for `hass.config.api`."""
    return SimpleNamespace(local_ip=local_ip, use_ssl=use_ssl)


async def test_an_unconfigured_internal_url_is_pointed_at_the_proxy(
    hass: HomeAssistant,
) -> None:
    """With nothing configured, the proxy's port replaces the hidden one."""
    hass.config.internal_url = None
    hass.config.api = _api()

    corrected = discovery.async_correct_internal_url(hass, PROXY_PORT)

    assert corrected is True
    assert hass.config.internal_url == f"http://192.168.1.10:{PROXY_PORT}"


async def test_https_is_used_when_home_assistant_serves_it(
    hass: HomeAssistant,
) -> None:
    """The scheme follows Home Assistant's own, not a hardcoded default."""
    hass.config.internal_url = None
    hass.config.api = _api(use_ssl=True)

    discovery.async_correct_internal_url(hass, PROXY_PORT)

    assert hass.config.internal_url == f"https://192.168.1.10:{PROXY_PORT}"


async def test_an_explicit_internal_url_is_left_alone(hass: HomeAssistant) -> None:
    """A URL entered under Settings > System > Network is not second-guessed."""
    hass.config.internal_url = "http://192.168.1.10:8123"
    hass.config.api = _api()

    corrected = discovery.async_correct_internal_url(hass, PROXY_PORT)

    assert corrected is False
    assert hass.config.internal_url == "http://192.168.1.10:8123"


async def test_a_missing_api_config_is_survived(hass: HomeAssistant) -> None:
    """Nothing to build a URL from means nothing is set."""
    hass.config.internal_url = None
    hass.config.api = None

    corrected = discovery.async_correct_internal_url(hass, PROXY_PORT)

    assert corrected is False
    assert hass.config.internal_url is None
