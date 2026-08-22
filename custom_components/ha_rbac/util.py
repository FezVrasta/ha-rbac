"""Deployment checks."""

from ipaddress import ip_address

from homeassistant.core import HomeAssistant, callback


@callback
def async_upstream_is_loopback_only(hass: HomeAssistant) -> bool:
    """Return True if Home Assistant only listens on loopback.

    Everything this integration enforces depends on it. An access token is not
    port-scoped, so if Home Assistant is reachable from the network, any user
    can present the token they already hold directly to it and receive
    unfiltered access. Nothing in the request path can detect that, which is why
    it is checked at setup and reported loudly.
    """
    server_host = getattr(hass.http, "server_host", None)
    if not server_host:
        return False

    hosts = server_host if isinstance(server_host, list) else [server_host]
    try:
        return all(ip_address(host).is_loopback for host in hosts)
    except ValueError:
        return False
