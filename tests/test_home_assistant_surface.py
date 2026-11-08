"""What the integration relies on in Home Assistant is still there.

The rest of the suite tests ha-rbac's own behaviour. It can't see Home
Assistant removing something the panel uses, or adding a command the proxy has
to know about: Home Assistant 2026.11 did both (#83, and #84/#85), and every
other test stayed green. CI runs these against the newest beta every week. Both
breaks were in 2026.11's first beta, a week before the release.
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.ingress import SESSION_COMMAND, VALIDATE_COMMAND

PANEL = (
    Path(__file__).parents[1] / "custom_components/ha_rbac/frontend/ha-rbac-panel.js"
)

# Elements the panel puts in its templates, or creates from code.
_USED = re.compile(r"""<(ha-[a-z0-9-]+)|createElement\(\s*["'](ha-[a-z0-9-]+)["']""")
# Elements the panel defines itself.
_DEFINED_HERE = re.compile(r"""customElements\.define\(\s*["'](ha-[a-z0-9-]+)["']""")
# An element name as a string in Home Assistant's built frontend, which is how
# each one is registered. A removed element's name is gone entirely: in 2026.11,
# "ha-formfield" appears nowhere.
_NAMED = re.compile(rb"""["'`](ha-[a-z0-9-]+)["'`]""")

# Every websocket command the Supervisor integration registers, and how
# ha-rbac treats it. A command that isn't here fails the test, so a new one is
# looked at before Home Assistant releases it: decide whether the proxy has to
# intercept, gate or filter it, then add it.
REVIEWED_SUPERVISOR_COMMANDS = {
    # Home Assistant requires an administrator for these.
    "hassio/update/addon",
    "hassio/update/config/info",
    "hassio/update/config/update",
    "hassio/update/core",
    "supervisor/subscribe",
    # Requires the Supervisor's own user.
    "supervisor/event",
    # Checks for an administrator in its body; the catalogue reads that per
    # endpoint (test_catalog.py).
    "supervisor/api",
    # Added in 2026.11 for add-on panels. All three are checked against the
    # role's add-ons by slug. The proxy intercepts the two that mint and
    # validate an ingress session so it can record the session (#86); info
    # returns the add-on's details and mints nothing.
    "supervisor/ingress/info",
    SESSION_COMMAND,
    VALIDATE_COMMAND,
}


def _panel_elements() -> set[str]:
    """Return the Home Assistant elements the panel uses."""
    source = PANEL.read_text(encoding="utf-8")
    used = {tag or created for tag, created in _USED.findall(source)}
    return used - set(_DEFINED_HERE.findall(source))


def _frontend_element_names() -> set[str] | None:
    """Return every element name in the installed frontend, or None without it."""
    spec = importlib.util.find_spec("hass_frontend")
    if spec is None or not spec.submodule_search_locations:
        return None
    built = Path(next(iter(spec.submodule_search_locations))) / "frontend_latest"
    return {
        name.decode()
        for path in built.glob("*.js")
        for name in _NAMED.findall(path.read_bytes())
    }


def test_the_panel_uses_only_elements_this_home_assistant_defines() -> None:
    """#83: Home Assistant 2026.11 removed ha-formfield, and the panel lost its labels.

    An element Home Assistant no longer defines doesn't fail: the browser
    renders its children and drops the rest, here the checkbox labels.
    """
    names = _frontend_element_names()
    if names is None:
        message = (
            "home-assistant-frontend isn't installed; "
            "run testsupport/install_integration_requirements.py"
        )
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    used = _panel_elements()
    # If this finds nothing, the patterns no longer match how the panel is
    # written, and the check below would pass without checking anything.
    assert {"ha-button", "ha-checkbox"} <= used

    missing = sorted(used - names)
    assert not missing, (
        f"Home Assistant {HA_VERSION}'s frontend doesn't define {missing}, "
        "which the panel uses"
    )


async def test_every_supervisor_command_has_been_reviewed(
    hass: HomeAssistant,
) -> None:
    """#84/#85: 2026.11 opened add-on panels through three new commands.

    The proxy didn't know them, so it never recorded the ingress sessions they
    minted and refused everyone's add-on panels, administrators included.
    """
    from homeassistant.components.hassio.websocket_api import (  # noqa: PLC0415
        async_load_websocket_api,
    )

    await async_setup_component(hass, "websocket_api", {})
    before = set(hass.data["websocket_api"])
    async_load_websocket_api(hass)
    registered = set(hass.data["websocket_api"]) - before

    # A guard on the guard: if loading the commands stops working, nothing
    # would be new and the check below would pass without checking anything.
    assert "supervisor/api" in registered

    new = sorted(registered - REVIEWED_SUPERVISOR_COMMANDS)
    assert not new, (
        f"Home Assistant {HA_VERSION} registers Supervisor commands ha-rbac "
        f"hasn't reviewed: {new}. Work out how the proxy must treat each one, "
        "then add it to REVIEWED_SUPERVISOR_COMMANDS."
    )
