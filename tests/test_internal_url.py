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


async def test_the_scheme_describes_the_proxy_not_what_is_behind_it(
    hass: HomeAssistant,
) -> None:
    """Always http, because that is what the proxy listens for.

    `api.use_ssl` is `ssl_certificate is not None`, and setup refuses an
    instance holding its own certificate -- the proxy speaks plain HTTP on both
    sides and cannot sit in front of one. So this is never true when the
    correction runs, and following it would hand out an `https://` URL for a
    listener with no SSL context if it ever were.
    """
    hass.config.internal_url = None
    hass.config.api = _api(use_ssl=True)

    discovery.async_correct_internal_url(hass, PROXY_PORT)

    assert hass.config.internal_url == f"http://192.168.1.10:{PROXY_PORT}"


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


async def test_a_changed_proxy_port_is_followed(hass: HomeAssistant) -> None:
    """Reconfiguring the port reloads the entry in place, in one process.

    The guard is "has somebody configured an internal URL", and after the first
    correction the answer is yes -- our own. Read literally it left the URL
    naming the port the proxy used to be on, for as long as Home Assistant kept
    running.
    """
    hass.config.internal_url = None
    hass.config.api = _api()

    discovery.async_correct_internal_url(hass, PROXY_PORT)
    assert hass.config.internal_url == f"http://192.168.1.10:{PROXY_PORT}"

    corrected = discovery.async_correct_internal_url(hass, 8080)

    assert corrected is True
    assert hass.config.internal_url == "http://192.168.1.10:8080"


async def test_a_url_set_by_hand_after_the_correction_wins(
    hass: HomeAssistant,
) -> None:
    """Ours is rewritable; a person's is not, whenever they set it."""
    hass.config.internal_url = None
    hass.config.api = _api()
    discovery.async_correct_internal_url(hass, PROXY_PORT)

    hass.config.internal_url = "https://home.example.com"
    corrected = discovery.async_correct_internal_url(hass, 8080)

    assert corrected is False
    assert hass.config.internal_url == "https://home.example.com"


async def test_correcting_twice_to_the_same_port_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """A reload that does not move the port is not a change to report."""
    hass.config.internal_url = None
    hass.config.api = _api()
    assert discovery.async_correct_internal_url(hass, PROXY_PORT) is True

    assert discovery.async_correct_internal_url(hass, PROXY_PORT) is False
    assert hass.config.internal_url == f"http://192.168.1.10:{PROXY_PORT}"


async def test_the_correction_is_cleared_when_the_entry_goes_away(
    hass: HomeAssistant,
) -> None:
    """A URL naming a port nothing answers on is worse than no URL at all.

    Once the proxy is gone the fallback this replaced names the port Home
    Assistant is being put back on, which is the right answer again.
    """
    hass.config.internal_url = None
    hass.config.api = _api()
    discovery.async_correct_internal_url(hass, PROXY_PORT)

    restored = discovery.async_restore_internal_url(hass)

    assert restored is True
    assert hass.config.internal_url is None


async def test_a_url_set_by_hand_survives_the_entry_going_away(
    hass: HomeAssistant,
) -> None:
    """Only what this integration set is taken back."""
    hass.config.internal_url = None
    hass.config.api = _api()
    discovery.async_correct_internal_url(hass, PROXY_PORT)
    hass.config.internal_url = "https://home.example.com"

    restored = discovery.async_restore_internal_url(hass)

    assert restored is False
    assert hass.config.internal_url == "https://home.example.com"


async def test_nothing_is_cleared_when_nothing_was_set(hass: HomeAssistant) -> None:
    """An explicit URL means the correction never ran, so there is nothing to undo."""
    hass.config.internal_url = "https://home.example.com"
    hass.config.api = _api()
    assert discovery.async_correct_internal_url(hass, PROXY_PORT) is False

    assert discovery.async_restore_internal_url(hass) is False
    assert hass.config.internal_url == "https://home.example.com"
