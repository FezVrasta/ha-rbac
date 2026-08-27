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
from ipaddress import ip_address, ip_network
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


def covers_loopback(entry: object) -> bool:
    """Return True if a `trusted_proxies` entry covers the address we call from.

    Entries may be single addresses or networks, so `127.0.0.0/8` counts as
    much as `127.0.0.1`. Anything unparseable is not a match rather than an
    error: the list is Home Assistant's to validate, not ours.
    """
    try:
        return ip_address(LOOPBACK) in ip_network(str(entry), strict=False)
    except ValueError:
        return False


def target_config(current: dict[str, Any], upstream_port: int) -> dict[str, Any]:
    """Return `current` moved to loopback on `upstream_port`.

    Built from the running config rather than from defaults, so SSL, CORS,
    trusted proxies and the ban settings survive the move. Only what this
    integration needs is changed.
    """
    moved = {key: value for key, value in current.items() if key not in _META_KEYS}
    moved["server_host"] = [LOOPBACK]
    moved["server_port"] = upstream_port

    # Every request now reaches Home Assistant from the proxy, over loopback, so
    # without this they all arrive as 127.0.0.1 and it can no longer tell one
    # user from another. That is not cosmetic: an IP ban would apply to the
    # proxy and therefore to everybody at once, and `trusted_networks` could not
    # work at all.
    #
    # Home Assistant only reads `X-Forwarded-For` from a peer it trusts, so the
    # address the proxy calls from goes on the list. Existing entries are kept:
    # an outer reverse proxy has to stay trusted for the chain to resolve past
    # it to the real client.
    proxies = [str(entry) for entry in (moved.get("trusted_proxies") or [])]
    if not any(covers_loopback(entry) for entry in proxies):
        proxies.append(LOOPBACK)
    moved["trusted_proxies"] = proxies
    moved["use_x_forwarded_for"] = True
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


async def async_running(hass: HomeAssistant) -> dict[str, Any]:
    """Return the config the HTTP server actually started with.

    Not the same question as `async_current`. A staged config is what Home
    Assistant will use *next* start; this is what the server in front of us is
    running now, which is the only thing worth asking before sending it headers
    it may reject. The store records which slot it booted from, and a default
    boot means no config at all rather than the stable one.
    """
    api_config, _ = _api()
    store = await _store(hass)
    active = getattr(store, "active_config_type", None)
    if active is getattr(api_config.ActiveConfigType, "PENDING", object()):
        return dict(store.pending or {})
    if active is getattr(api_config.ActiveConfigType, "STABLE", object()):
        return dict(store.stable or {})
    # Booted on defaults, so nothing is trusted and nothing is configured.
    return {}


async def async_running_or_empty(hass: HomeAssistant) -> dict[str, Any]:
    """`async_running`, but an unreadable config is an empty one.

    Callers that only want to know what is configured, on a build that may not
    expose any of this, should get "nothing" rather than an exception.
    """
    try:
        return await async_running(hass)
    except Exception as err:  # noqa: BLE001 -- see async_can_manage
        _LOGGER.debug("Cannot read the running HTTP config: %s", err)
        return {}


async def async_trusts_loopback(hass: HomeAssistant) -> bool:
    """Return True if the running Home Assistant reads our forwarded headers.

    Both halves are required. Home Assistant rejects a request carrying
    `X-Forwarded-For` from a peer it does not trust, with a 400 and no route to
    recovery, so a wrong yes here takes the whole instance down. Every way of
    failing to establish the answer therefore comes back as no.
    """
    running = await async_running_or_empty(hass)
    if not running.get("use_x_forwarded_for"):
        return False
    return any(
        covers_loopback(entry) for entry in (running.get("trusted_proxies") or [])
    )


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
