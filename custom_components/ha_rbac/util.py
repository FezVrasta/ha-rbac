"""Deployment checks."""

from ipaddress import ip_address

from homeassistant.core import HomeAssistant, callback


def is_loopback_bind(address: str) -> bool:
    """Return True if a bind address restricts the proxy to loopback only.

    A loopback bind means only the local machine can connect, which strands the
    instance when ``manage_http`` moves Home Assistant to loopback as well --
    unless something else on the host bridges the network.

    Resolved as an address rather than matched as a string, so the whole
    ``127.0.0.0/8`` block, ``::1`` and IPv4-mapped forms like
    ``::ffff:127.0.0.1`` all count, the same way ``.is_loopback`` is used for the
    upstream check beside it. ``localhost`` is not an address and is spelled out.
    """
    stripped = address.strip()
    if stripped == "localhost":
        return True
    try:
        return ip_address(stripped).is_loopback
    except ValueError:
        return False


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
