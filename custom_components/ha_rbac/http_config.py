"""Move Home Assistant's own listener out of the way.

Everything this integration enforces rests on one setting: Home Assistant must
answer on loopback only, on a port this proxy is not using. Doing that by hand
is three steps in a particular order, and the order is not guessable -- taking
the port before Home Assistant has vacated it is a bind error rather than a
working setup, and closing the door before the proxy is up locks you out.

So it is done here instead. Home Assistant cannot change its HTTP config while
running: a new config is *staged*, applied on the next start, and then reverted
automatically unless something promotes it within five minutes. That trial is
what makes automating this safe rather than reckless -- if the proxy fails to
come up, nobody has to know how to undo anything, because Home Assistant undoes
it by itself and comes back on the port it was on before.

None of the API used here is public. Every entry point degrades to "cannot do
this, say so" rather than raising, since the fallback is the documented manual
route and not a broken installation.
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"

# Keys Home Assistant records about a staged config rather than accepting from
# one. Carrying them into a new config makes it look like it has already been
# tried and failed.
_META_KEYS = ("created_at", "error", "error_message")


class Unavailable(Exception):
    """Home Assistant does not expose its HTTP config the way this expects."""


def _api() -> Any:
    """Return Home Assistant's HTTP config module, or raise Unavailable."""
    try:
        from homeassistant.components.http import config, server  # noqa: PLC0415
    except ImportError as err:
        raise Unavailable(str(err)) from err
    for name in ("async_get_and_load_store", "ConfData"):
        if not hasattr(config, name):
            raise Unavailable(f"homeassistant.components.http.config has no {name}")
    if not hasattr(server, "async_verify_can_bind"):
        raise Unavailable("homeassistant.components.http.server cannot verify a bind")
    return config, server


async def _store(hass: HomeAssistant) -> Any:
    """Return Home Assistant's loaded HTTP config store."""
    config, _ = _api()
    return await config.async_get_and_load_store(hass)


def target_config(current: dict[str, Any], upstream_port: int) -> dict[str, Any]:
    """Return `current` moved to loopback on `upstream_port`.

    Built from the running config rather than from defaults, so SSL, CORS,
    trusted proxies and the ban settings survive the move. Only the two things
    this integration needs are changed.
    """
    moved = {key: value for key, value in current.items() if key not in _META_KEYS}
    moved["server_host"] = [LOOPBACK]
    moved["server_port"] = upstream_port
    return moved


def is_aligned(hass: HomeAssistant, upstream_port: int) -> bool:
    """Return True if Home Assistant already answers where the proxy expects.

    Read from the running server rather than the store: the store says what
    Home Assistant was asked to do, and this has to be true of what it is
    actually doing.
    """
    http = getattr(hass, "http", None)
    if http is None:
        return False
    return list(getattr(http, "server_host", None) or []) == [LOOPBACK] and (
        getattr(http, "server_port", None) == upstream_port
    )


async def async_can_manage(hass: HomeAssistant) -> bool:
    """Return True if this build of Home Assistant can be moved from here."""
    try:
        await _store(hass)
    except Exception as err:  # noqa: BLE001
        # Deliberately broad. The question being answered is "does this build
        # of Home Assistant look the way this module expects", and every way
        # the answer can be no -- a moved module, a renamed attribute, a
        # changed signature -- has to come back as no rather than as a
        # traceback. Answering yes wrongly restarts Home Assistant into a
        # config nobody staged.
        _LOGGER.debug("Cannot manage the HTTP config on this build: %s", err)
        return False
    return True


async def async_current(hass: HomeAssistant) -> dict[str, Any]:
    """Return the config Home Assistant is running, as far as it is stored."""
    store = await _store(hass)
    running = store.pending if store.pending is not None else store.stable
    return dict(running or {})


async def async_stage(hass: HomeAssistant, config: dict[str, Any]) -> None:
    """Stage a config for the next start, after checking it can be bound.

    The bind check is what turns "Home Assistant restarts into a config that
    cannot work" into an error reported while everything still answers.
    """
    api_config, server = _api()
    await server.async_verify_can_bind(hass, config)
    store = await api_config.async_get_and_load_store(hass)
    await store.async_set_pending(config)


async def async_promote(hass: HomeAssistant) -> bool:
    """Confirm the staged config, cancelling its automatic revert.

    Only ever called once the proxy has been proven to serve a request, which
    is the whole interlock: until then the five-minute revert is the way back
    in, and promoting early would throw it away.
    """
    try:
        store = await _store(hass)
        if store.pending is None:
            return False
        await store.async_promote_pending()
    except (Unavailable, HomeAssistantError, OSError):
        _LOGGER.exception(
            "Could not confirm Home Assistant's new network configuration. It "
            "will revert by itself shortly and Home Assistant will restart on "
            "its previous port"
        )
        return False
    _LOGGER.info("Confirmed Home Assistant's move to loopback")
    return True
