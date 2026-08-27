"""RBAC access control for Home Assistant."""

import logging
from pathlib import Path

import aiohttp
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import DOMAIN as HA_DOMAIN
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    label_registry as lr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from . import http_config, websocket_api
from .catalog import Catalog
from .const import (
    CONF_BIND_ADDRESS,
    CONF_MANAGE_HTTP,
    CONF_PROXY_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DATA_RBAC,
    DATA_STATIC_PATH_REGISTERED,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PROXY_PORT,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
    PANEL_URL_PATH,
    STATIC_URL_PATH,
)
from .dashboards import DashboardEntities
from .decide import Decider
from .denylog import DenyLog
from .filters import REGISTRY
from .models import RbacData
from .policy import Evaluator
from .proxy import RbacProxy
from .record import Recorder
from .store import RbacStore
from .util import async_upstream_is_loopback_only

_LOGGER = logging.getLogger(__name__)

# A role naming an area or a label desugars to concrete entity ids when it is
# compiled, so anything that changes those relationships invalidates it.
# Spelled out rather than imported: the constant lives in
# `homeassistant.const` on current builds and in
# `homeassistant.components.homeassistant.const` on 2026.8, while the
# service itself has been called this throughout.
SERVICE_RESTART = "restart"

REGISTRY_EVENTS = (
    er.EVENT_ENTITY_REGISTRY_UPDATED,
    dr.EVENT_DEVICE_REGISTRY_UPDATED,
    ar.EVENT_AREA_REGISTRY_UPDATED,
    lr.EVENT_LABEL_REGISTRY_UPDATED,
    fr.EVENT_FLOOR_REGISTRY_UPDATED,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the RBAC proxy and its admin panel."""
    # The options flow writes to `options`; reading only `data` meant a changed
    # port was accepted, redisplayed, and silently ignored.
    config = {**entry.data, **entry.options}

    store = RbacStore(hass)
    await store.async_load()

    catalog = Catalog(hass)
    catalog.rebuild()

    dashboard_entities = DashboardEntities(hass)
    evaluator = Evaluator(hass, store, dashboard_entities.entities_for)
    denylog = DenyLog(hass)
    recorder = Recorder()
    decider = Decider(hass, catalog, REGISTRY, recorder)

    data = RbacData(
        store=store,
        catalog=catalog,
        evaluator=evaluator,
        decider=decider,
        denylog=denylog,
        recorder=recorder,
    )
    hass.data[DATA_RBAC] = data

    store.async_add_listener(evaluator.invalidate)
    for event_type in REGISTRY_EVENTS:
        data.unsubscribes.append(
            hass.bus.async_listen(event_type, evaluator.invalidate)
        )
    # Integrations register websocket commands lazily as they are set up.
    data.unsubscribes.append(hass.bus.async_listen("component_loaded", catalog.rebuild))
    data.dashboard_entities = dashboard_entities
    websocket_api.async_register(hass)
    # The panel is how roles are administered, but it is not how they are
    # enforced. If the frontend is unavailable, keep enforcing and say so.
    try:
        await _async_register_panel(hass)
    except Exception:
        _LOGGER.exception(
            "Could not register the Access Control panel; roles are still "
            "enforced but must be managed over the websocket API"
        )

    if not async_upstream_is_loopback_only(hass):
        _LOGGER.warning(
            "Home Assistant is reachable from the network, so this integration "
            "enforces nothing: an access token is not port-scoped, and any user "
            "can present the token they already have directly to port %s. Set "
            "Server host to 127.0.0.1 under Settings > System > Network",
            config.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT),
        )

    if catalog.degraded:
        _LOGGER.error(
            "Permission derivation is not working on this Home Assistant version, "
            "so every request will be denied rather than silently allowed"
        )

    proxy_port = config.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT)
    upstream_port = config.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT)

    def _port_taken() -> str:
        """Explain the one setup mistake that produces a bare bind error."""
        return (
            f"Port {proxy_port} is already in use, which is what happens when "
            f"Home Assistant is still answering on it. Move Home Assistant to "
            f"port {upstream_port} under Settings > System > Network first, "
            f"then this can take over {proxy_port}."
        )

    manage_http = config.get(CONF_MANAGE_HTTP, False)

    async def _move_home_assistant() -> bool:
        """Stage Home Assistant's move to loopback and restart into it.

        Returns True if a restart is on its way, in which case there is no
        point starting a listener: the process is going down, and the port this
        wants is still held by Home Assistant until it does.
        """
        if not manage_http or http_config.is_aligned(hass, upstream_port):
            return False
        try:
            current = await http_config.async_current(hass)
            await http_config.async_stage(
                hass, http_config.target_config(current, upstream_port)
            )
        except (http_config.Unavailable, HomeAssistantError, OSError):
            _LOGGER.exception(
                "Could not move Home Assistant to port %s automatically. Do it "
                "under Settings > System > Network: set the port to %s and "
                "Server host to 127.0.0.1",
                upstream_port,
                upstream_port,
            )
            return False

        _LOGGER.warning(
            "Moving Home Assistant to 127.0.0.1:%s so this can answer on %s. "
            "Home Assistant is restarting. If it does not come back on %s "
            "within five minutes it undoes the change itself and returns to "
            "the port it is on now",
            upstream_port,
            proxy_port,
            proxy_port,
        )
        # Not held open as a task: Home Assistant warns that the call
        # outlived the shutdown it asked for, and there is nothing to wait
        # for -- the answer arrives as the process going away.
        await hass.services.async_call(HA_DOMAIN, SERVICE_RESTART, blocking=False)
        return True

    async def _confirm_move() -> None:
        """Make the move permanent, but only once the proxy really serves.

        This is the interlock the whole thing rests on. Until it runs, Home
        Assistant reverts to its previous configuration by itself, so a proxy
        that binds but cannot forward still leaves a way back in. Promoting on
        `async_start` alone would throw that away, since binding a port says
        nothing about whether anything answers on it.
        """
        if not manage_http:
            return
        bind = config.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS)
        reachable = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind
        try:
            async with async_get_clientsession(hass).get(
                f"http://{reachable}:{proxy_port}/",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                served = response.status < 500
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error(
                "The proxy is listening on %s but did not serve a request (%s), "
                "so Home Assistant's move has been left unconfirmed and will "
                "revert by itself",
                proxy_port,
                err,
            )
            return
        if served:
            await http_config.async_promote(hass)

    async def _start_proxy(_event: Event | None = None) -> None:
        """Start the listener once Home Assistant is serving."""
        if await _move_home_assistant():
            return
        # Every request now arrives from the proxy, so unless Home Assistant is
        # told to read a forwarded address it sees one client for the whole
        # house. It only reads one from a peer it trusts, and rejects the
        # request outright otherwise, so this is asked rather than assumed.
        running = await http_config.async_running_or_empty(hass)
        proxy = RbacProxy(
            hass,
            evaluator,
            decider,
            denylog,
            upstream_host=config.get(CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST),
            upstream_port=upstream_port,
            bind_address=config.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS),
            port=proxy_port,
            forward_client_ip=await http_config.async_trusts_loopback(hass),
            trusted_proxies=[str(e) for e in (running.get("trusted_proxies") or [])],
        )
        await proxy.async_start()
        data.proxy = proxy
        await _confirm_move()
        # Dashboards can only be read once Home Assistant has loaded them.
        await dashboard_entities.async_start()

    async def _start_proxy_later(event: Event) -> None:
        """Start on the started event, where nothing can catch a raise."""
        # Home Assistant removes a one-time listener when it fires, so calling
        # its unsubscribe afterwards asks for something that is already gone.
        # That is caught and logged upstream rather than raised, so it breaks
        # nothing -- it just puts an ERROR and a traceback in the log of every
        # shutdown, which is a bad way to spend a user's attention.
        if started in data.unsubscribes:
            data.unsubscribes.remove(started)
        try:
            await _start_proxy(event)
        except OSError:
            _LOGGER.error("%s", _port_taken())

    if hass.is_running:
        try:
            await _start_proxy()
        except OSError as err:
            raise ConfigEntryNotReady(_port_taken()) from err
    else:
        started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _start_proxy_later
        )
        data.unsubscribes.append(started)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve the admin panel."""
    # aiohttp cannot unregister a route, so re-registering on reload raises and
    # the panel would be lost until a full restart.
    if not hass.data.get(DATA_STATIC_PATH_REGISTERED):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_URL_PATH,
                    str(Path(__file__).parent / "frontend"),
                    cache_headers=False,
                )
            ]
        )
        hass.data[DATA_STATIC_PATH_REGISTERED] = True
    # The panel module is fetched by the browser and cached. Without a version
    # in the URL, upgrading the integration leaves people on the old panel until
    # they clear their cache, which is not something to ask of them.
    integration = await async_get_integration(hass, DOMAIN)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="ha-rbac-panel",
        module_url=f"{STATIC_URL_PATH}/ha-rbac-panel.js?v={integration.version}",
        sidebar_title="Access Control",
        sidebar_icon="mdi:shield-account",
        # Also reachable from Settings, through this integration's own card:
        # the frontend looks for a panel whose config_panel_domain matches an
        # integration and offers it as that integration's Configure page. There
        # is no way to add an entry to the Settings menu itself -- that list is
        # compiled into the frontend bundle, not served from the backend.
        config_panel_domain=DOMAIN,
        # Defence in depth only; the real gate is require_admin on the commands.
        require_admin=True,
    )


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restart the proxy when its configuration changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the proxy and remove the panel."""
    data: RbacData | None = hass.data.pop(DATA_RBAC, None)
    if data is None:
        return True

    # A recording is a temporary grant of full access, so it does not
    # outlive the thing that was granting it.
    data.recorder.stop_all()

    for unsubscribe in data.unsubscribes:
        unsubscribe()
    if data.proxy is not None:
        await data.proxy.async_stop()
    if data.dashboard_entities is not None:
        data.dashboard_entities.async_stop()

    # The handlers close over hass.data[DATA_RBAC], which has just been removed.
    websocket_api.async_unregister(hass)
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    return True
