"""Remote UI hands requests to Home Assistant's own server, past every role."""

from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_rbac import remote_ui
from custom_components.ha_rbac.const import DOMAIN


def _cloud(enabled: bool, logged_in: bool = True) -> SimpleNamespace:
    prefs = SimpleNamespace(remote_enabled=enabled)
    return SimpleNamespace(is_logged_in=logged_in, client=SimpleNamespace(prefs=prefs))


async def test_remote_ui_raises_a_repair_and_clears_it(hass: HomeAssistant) -> None:
    """GHSA-2j78-q2g6-8pg9: the relay never touches the proxy's socket."""
    registry = ir.async_get(hass)

    remote_ui.async_check(hass)
    assert registry.async_get_issue(DOMAIN, remote_ui.ISSUE_ID) is None

    hass.data["cloud"] = _cloud(True)
    remote_ui.async_check(hass)
    assert registry.async_get_issue(DOMAIN, remote_ui.ISSUE_ID) is not None

    hass.data["cloud"] = _cloud(False)
    remote_ui.async_check(hass)
    assert registry.async_get_issue(DOMAIN, remote_ui.ISSUE_ID) is None

    hass.data["cloud"] = _cloud(True, logged_in=False)
    remote_ui.async_check(hass)
    assert registry.async_get_issue(DOMAIN, remote_ui.ISSUE_ID) is None
