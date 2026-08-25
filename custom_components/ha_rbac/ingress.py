"""Gate add-on ingress, which Home Assistant serves without authentication.

`HassIOIngress.requires_auth` is False, so an ingress request arrives with no
bearer token and the proxy can resolve no user from the request itself. The
only identity it carries is the `ingress_session` cookie -- and that session is
minted through a Supervisor command the proxy does see, on a connection it has
already authenticated. So the proxy records who each session was issued to and
judges the ingress request by that.

Without this, denying an add-on hides its panel and refuses its Supervisor
endpoints while leaving the add-on's own web UI reachable by anyone who knows
its ingress path -- a value that is stable for the life of the installation.
"""

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

INGRESS_PREFIX = "/api/hassio_ingress/"
SESSION_COOKIE = "ingress_session"
SESSION_ENDPOINT = "/ingress/session"
VALIDATE_ENDPOINT = "/ingress/validate_session"

# A session outlives a page load but not a working day. The frontend
# revalidates while an add-on panel is open, which refreshes the stamp.
SESSION_TTL = 8 * 3600
MAX_SESSIONS = 512

# The shortest gap between two rebuilds of the token map. A newly installed
# add-on has a token the map has never seen, and answering "not an add-on"
# forwards it unguarded -- so a miss rebuilds instead of trusting what it has.
# The limit is on rebuild *attempts*, not on the map's age: rebuilding costs a
# Supervisor call per installed add-on and the token in a miss is attacker
# controlled, but two rebuilds a moment apart would see the same thing anyway.
MISS_RELOAD_INTERVAL = 1


class IngressUnavailable(Exception):
    """Raised when the ingress token map could not be built.

    Treated as a denial rather than ignored: an unresolvable token would
    otherwise pass straight through and defeat the gate.
    """


def session_from(payload: Any) -> str | None:
    """Return the session id in a Supervisor ingress response, if there is one."""
    if not isinstance(payload, dict):
        return None
    # Supervisor answers `{"result": "ok", "data": {...}}`, but Home Assistant
    # unwraps `data` on some paths, so both shapes reach here.
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    session = body.get("session")
    return session if isinstance(session, str) else None


def token_from(path: str) -> str | None:
    """Return the ingress token a request path names, if it is an ingress path."""
    if not path.startswith(INGRESS_PREFIX):
        return None
    token = path[len(INGRESS_PREFIX) :].split("/", 1)[0]
    return token or None


class IngressGuard:
    """Tracks who owns each ingress session, and which add-on each token is."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty guard."""
        self._hass = hass
        self._sessions: dict[str, tuple[str, float]] = {}
        self._slugs: dict[str, str] = {}
        self._loaded_at: float | None = None

    @callback
    def remember_session(self, session: str, user_id: str) -> None:
        """Record which user a freshly minted ingress session belongs to."""
        self._expire()
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key][1])
            del self._sessions[oldest]
        self._sessions[session] = (user_id, time.monotonic())

    @callback
    def touch_session(self, session: str) -> None:
        """Extend a session the frontend is still using."""
        if (entry := self._sessions.get(session)) is not None:
            self._sessions[session] = (entry[0], time.monotonic())

    @callback
    def user_id_for(self, session: str | None) -> str | None:
        """Return the user a session belongs to, or None if it is not known."""
        if not session or (entry := self._sessions.get(session)) is None:
            return None
        user_id, stamp = entry
        if time.monotonic() - stamp > SESSION_TTL:
            del self._sessions[session]
            return None
        return user_id

    @callback
    def _expire(self) -> None:
        """Drop sessions past their lifetime."""
        now = time.monotonic()
        for session in [
            key
            for key, (_, stamp) in self._sessions.items()
            if now - stamp > SESSION_TTL
        ]:
            del self._sessions[session]

    @callback
    def invalidate(self) -> None:
        """Forget the token map, so the next request rebuilds it."""
        self._slugs.clear()
        self._loaded_at = None

    async def async_slug_for(self, token: str) -> str | None:
        """Return the add-on an ingress token belongs to, or None if it is none."""
        if token in self._slugs:
            return self._slugs[token]
        # A miss is either a token that belongs to nothing or an add-on
        # installed since the map was built, and only a rebuild tells them
        # apart. Guessing "not an add-on" would forward it unguarded.
        now = time.monotonic()
        if self._loaded_at is None or now - self._loaded_at > MISS_RELOAD_INTERVAL:
            self._loaded_at = now
            await self._async_load()
        return self._slugs.get(token)

    async def _async_load(self) -> None:
        """Build the token -> add-on map from Supervisor."""
        if "hassio" not in self._hass.config.components:
            # No Supervisor, so no ingress and nothing to guard.
            self._loaded_at = time.monotonic()
            return

        try:
            from homeassistant.components.hassio import (  # noqa: PLC0415
                get_supervisor_client,
            )

            client = get_supervisor_client(self._hass)
            slugs: dict[str, str] = {}
            for addon in await client.addons.list():
                info = await client.addons.addon_info(addon.slug)
                if entry := getattr(info, "ingress_entry", None):
                    if token := token_from(entry.rstrip("/") + "/"):
                        slugs[token] = addon.slug
        except (ImportError, AttributeError, OSError) as err:
            _LOGGER.error("Cannot read add-on ingress tokens: %s", err)
            raise IngressUnavailable(str(err)) from err
        except Exception as err:
            # aiohasupervisor raises its own error hierarchy, which cannot be
            # imported without making `hassio` a hard dependency of this
            # integration. Failing closed is the point, so the type does not
            # matter here.
            _LOGGER.error("Cannot read add-on ingress tokens: %s", err)
            raise IngressUnavailable(str(err)) from err

        self._slugs = slugs
        self._loaded_at = time.monotonic()
