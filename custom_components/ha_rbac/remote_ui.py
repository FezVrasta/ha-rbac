"""Say so when Home Assistant Cloud's Remote UI is on.

The Nabu Casa relay does not connect to a socket. `CloudClient.aiohttp_runner`
hands the relay Home Assistant's own `AppRunner`, and the relayed streams go
straight to its request handler in-process. Binding Home Assistant to loopback,
which is what makes the proxy the only way in, has no effect on that path. A
restricted user who opens the instance through its `ui.nabu.casa` address, or
whose Companion app falls back to the cloud URL, is served by stock Home
Assistant with no role applied and nothing logged.

Closing it would mean handing the relay a different runner, which replaces a
Core-internal object, so this only detects it and raises a repair.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_ID = "remote_ui_bypasses_roles"


@callback
def remote_ui_enabled(hass: HomeAssistant) -> bool:
    """Return True if Cloud is logged in with Remote UI switched on."""
    cloud = hass.data.get("cloud")
    if cloud is None:
        return False
    try:
        return bool(cloud.is_logged_in and cloud.client.prefs.remote_enabled)
    except AttributeError:
        return False


@callback
def async_check(hass: HomeAssistant, _event: Event | Any = None) -> None:
    """Raise or clear the repair issue to match Remote UI's current state."""
    if remote_ui_enabled(hass):
        _LOGGER.warning(
            "Home Assistant Cloud Remote UI is on. Requests through the Nabu "
            "Casa relay reach Home Assistant directly and bypass every role"
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_ID,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ID,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)


@callback
def async_watch(hass: HomeAssistant) -> list[Callable[[], None]]:
    """Check now and again whenever Cloud loads or its preferences change.

    Returns the callables that undo it.
    """
    listening: list[Callable[[], None]] = []

    @callback
    def check_and_listen(_event: Event | None = None) -> None:
        async_check(hass)
        if listening or (cloud := hass.data.get("cloud")) is None:
            return

        async def updated(_prefs: Any) -> None:
            async_check(hass)

        try:
            listening.append(cloud.client.prefs.async_listen_updates(updated))
        except (AttributeError, TypeError):
            listening.append(lambda: None)

    @callback
    def loaded(event: Event) -> None:
        if event.data.get("component") == "cloud":
            check_and_listen()

    check_and_listen()
    return [
        hass.bus.async_listen("component_loaded", loaded),
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STARTED, check_and_listen),
        lambda: [undo() for undo in listening],
    ]
