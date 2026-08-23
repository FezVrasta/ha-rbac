"""RBAC access control for Home Assistant."""

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant
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
from homeassistant.loader import async_get_integration

from . import websocket_api
from .catalog import Catalog
from .const import (
    CONF_BIND_ADDRESS,
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
from .decide import Decider
from .denylog import DenyLog
from .filters import REGISTRY
from .models import RbacData
from .policy import Evaluator
from .proxy import RbacProxy
from .store import RbacStore
from .util import async_upstream_is_loopback_only

_LOGGER = logging.getLogger(__name__)

# A role naming an area or a label desugars to concrete entity ids when it is
# compiled, so anything that changes those relationships invalidates it.
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

    evaluator = Evaluator(hass, store)
    denylog = DenyLog(hass)
    decider = Decider(hass, catalog, REGISTRY)

    data = RbacData(
        store=store,
        catalog=catalog,
        evaluator=evaluator,
        decider=decider,
        denylog=denylog,
    )
    hass.data[DATA_RBAC] = data

    store.async_add_listener(evaluator.invalidate)
    for event_type in REGISTRY_EVENTS:
        data.unsubscribes.append(
            hass.bus.async_listen(event_type, evaluator.invalidate)
        )
    # Integrations register websocket commands lazily as they are set up.
    data.unsubscribes.append(hass.bus.async_listen("component_loaded", catalog.rebuild))
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
            "http.server_host to 127.0.0.1 in configuration.yaml and expose only "
            "the proxy port",
            config.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT),
        )

    if catalog.degraded:
        _LOGGER.error(
            "Permission derivation is not working on this Home Assistant version, "
            "so every request will be denied rather than silently allowed"
        )

    async def _start_proxy(_event: Event | None = None) -> None:
        """Start the listener once Home Assistant is serving."""
        proxy = RbacProxy(
            hass,
            evaluator,
            decider,
            denylog,
            upstream_host=config.get(CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST),
            upstream_port=config.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT),
            bind_address=config.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS),
            port=config.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT),
        )
        await proxy.async_start()
        data.proxy = proxy

    if hass.is_running:
        await _start_proxy()
    else:
        data.unsubscribes.append(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_proxy)
        )

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

    for unsubscribe in data.unsubscribes:
        unsubscribe()
    if data.proxy is not None:
        await data.proxy.async_stop()

    # The handlers close over hass.data[DATA_RBAC], which has just been removed.
    websocket_api.async_unregister(hass)
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    return True
