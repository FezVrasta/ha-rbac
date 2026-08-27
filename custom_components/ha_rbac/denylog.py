"""A ring buffer of recent denials.

Denials are the only signal an operator has that a role is too tight, and a
denied request looks to the user like a broken UI. Without this the layer is
undebuggable.
"""

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import DENYLOG_SIZE, EVENT_RBAC_DENIED


@dataclass(slots=True)
class Denial:
    """One denied request."""

    user_id: str
    user_name: str
    kind: str
    name: str
    reason: str
    resources: list[str]
    # The diagnostic. It used to be what the refused person was shown, which
    # made it terse for them and unavailable here; now it is only ever read by
    # whoever is working out why a role is too tight.
    detail: str = ""


class DenyLog:
    """Keeps the most recent denials and fires an event for each."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the log."""
        self._hass = hass
        self._entries: deque[Denial] = deque(maxlen=DENYLOG_SIZE)

    @callback
    def async_record(self, denial: Denial) -> None:
        """Record a denial and fire `rbac_denied` so automations can react."""
        self._entries.append(denial)
        self._hass.bus.async_fire(EVENT_RBAC_DENIED, asdict(denial))

    @callback
    def async_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent denials, newest first."""
        return [asdict(entry) for entry in list(self._entries)[-limit:][::-1]]

    @callback
    def async_clear(self) -> None:
        """Discard the recorded denials."""
        self._entries.clear()
