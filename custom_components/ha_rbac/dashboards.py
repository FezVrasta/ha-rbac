"""Which entities each dashboard puts on screen.

A role can grant whatever a dashboard shows, and that has to mean *now* rather
than at the moment the role was saved: dashboards get edited, and a grant that
froze the entity list would drift out of step without anyone noticing. So the
list is read from the dashboards themselves and refreshed when Home Assistant
says one changed.

The lookup is consulted for every entity check, which is the hottest path in
the proxy, so it answers from a cache. The cache is filled asynchronously and
read synchronously.
"""

import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .extract import entity_ids_in

_LOGGER = logging.getLogger(__name__)

# The default dashboard is stored under None rather than under a url path.
DEFAULT_URL_PATH = "lovelace"


class DashboardEntities:
    """A per-dashboard entity list, kept current with the dashboards."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty lookup."""
        self._hass = hass
        self._entities: dict[str, set[str]] = {}
        self._unsubscribe: Any = None

    async def async_start(self) -> None:
        """Read every dashboard, and keep listening for changes."""
        try:
            from homeassistant.components.lovelace.const import (  # noqa: PLC0415
                EVENT_LOVELACE_UPDATED,
            )
        except ImportError:
            # No dashboards on this installation, so nothing to track.
            return

        @callback
        def _changed(event: Event) -> None:
            url_path = event.data.get("url_path") or DEFAULT_URL_PATH
            self._hass.async_create_task(self.async_refresh(url_path))

        self._unsubscribe = self._hass.bus.async_listen(
            EVENT_LOVELACE_UPDATED, _changed
        )
        await self.async_refresh()

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def entities_for(self, url_path: str) -> set[str]:
        """Return the entities a dashboard shows, as last read."""
        return self._entities.get(url_path, set())

    def known(self) -> set[str]:
        """Return the dashboards this has an answer for."""
        return set(self._entities)

    async def async_refresh(self, url_path: str | None = None) -> None:
        """Re-read one dashboard, or all of them."""
        dashboards = self._dashboards()
        if dashboards is None:
            return

        wanted = [url_path] if url_path is not None else [*self._url_paths(dashboards)]
        known = set(self._hass.states.async_entity_ids())
        for path in wanted:
            key = None if path == DEFAULT_URL_PATH else path
            holder = dashboards.get(key)
            if holder is None:
                self._entities.pop(path, None)
                continue
            try:
                config = await holder.async_load(False)
            except Exception as err:  # noqa: BLE001
                # A dashboard built by a strategy stores no config, which is not
                # a fault: there is simply nothing written down to read.
                _LOGGER.debug("No readable config for dashboard %s: %s", path, err)
                self._entities.pop(path, None)
                continue
            self._entities[path] = entity_ids_in(config, known.__contains__)

    def _dashboards(self) -> dict[Any, Any] | None:
        """Return Home Assistant's dashboard registry, if there is one."""
        try:
            from homeassistant.components.lovelace.const import (  # noqa: PLC0415
                LOVELACE_DATA,
            )
        except ImportError:
            return None
        data = self._hass.data.get(LOVELACE_DATA)
        return getattr(data, "dashboards", None) if data else None

    @staticmethod
    def _url_paths(dashboards: dict[Any, Any]) -> list[str]:
        """Return every dashboard's url path, naming the default one."""
        return [key or DEFAULT_URL_PATH for key in dashboards]
