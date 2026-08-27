"""Tests for reporting the real client address to Home Assistant.

Every request reaches Home Assistant from the proxy, over loopback, so without
a forwarded address it sees one client for the whole house: an IP ban would
apply to everybody at once and `trusted_networks` could not work at all.

Home Assistant only reads `X-Forwarded-For` from a peer it trusts and answers
400 to every request carrying one from a peer it does not, so the cost of
sending these wrongly is the whole instance rather than a degraded feature.
"""

import pytest
from aiohttp import hdrs

from custom_components.ha_rbac import http_config
from custom_components.ha_rbac.proxy import RbacProxy


class _Request:
    """Just the parts of an aiohttp request the header build reads."""

    def __init__(self, remote: str, headers: dict | None = None) -> None:
        self.remote = remote
        self.headers = {hdrs.HOST: "home.example", **(headers or {})}
        self.scheme = "https"


def _proxy(hass, **kwargs) -> RbacProxy:
    return RbacProxy(
        hass,
        None,
        None,
        None,
        upstream_host="127.0.0.1",
        upstream_port=8124,
        bind_address="0.0.0.0",
        port=8123,
        **kwargs,
    )


async def test_nothing_is_forwarded_when_it_would_be_rejected(hass) -> None:
    """The default.

    Sending these to an instance that does not trust us is a 400 on every
    request, so silence is the safe failure.
    """
    proxy = _proxy(hass)
    headers = proxy._request_headers(_Request("203.0.113.7"))

    assert hdrs.X_FORWARDED_FOR not in headers


async def test_the_client_address_is_forwarded(hass) -> None:
    """The point of the feature."""
    proxy = _proxy(hass, forward_client_ip=True, trusted_proxies=["127.0.0.1"])
    headers = proxy._request_headers(_Request("203.0.113.7"))

    assert headers[hdrs.X_FORWARDED_FOR] == "203.0.113.7"
    assert headers[hdrs.X_FORWARDED_PROTO] == "https"


async def test_a_chain_from_a_trusted_peer_is_appended_to(hass) -> None:
    """Behind another reverse proxy, the real client is in the chain already.

    Home Assistant reads right to left, skipping what it trusts, so appending
    keeps the original client resolvable. Overwriting would report the reverse
    proxy as the client and lose the person entirely.
    """
    proxy = _proxy(hass, forward_client_ip=True, trusted_proxies=["10.0.0.0/8"])
    headers = proxy._request_headers(
        _Request("10.0.0.5", {hdrs.X_FORWARDED_FOR: "203.0.113.7"})
    )

    assert headers[hdrs.X_FORWARDED_FOR] == "203.0.113.7, 10.0.0.5"


async def test_a_chain_from_an_untrusted_peer_is_discarded(hass) -> None:
    """Otherwise anyone could type their way past IP banning.

    A header from a peer nobody trusts is a claim the client made about itself.
    Honouring it would let someone spoof a source address into Home Assistant's
    ban list and the `trusted_networks` auth provider.
    """
    proxy = _proxy(hass, forward_client_ip=True, trusted_proxies=["10.0.0.0/8"])
    headers = proxy._request_headers(
        _Request("203.0.113.7", {hdrs.X_FORWARDED_FOR: "127.0.0.1"})
    )

    assert headers[hdrs.X_FORWARDED_FOR] == "203.0.113.7"


async def test_an_unparseable_trusted_proxy_is_not_a_crash(hass) -> None:
    """The list is Home Assistant's to validate, not ours to choke on."""
    proxy = _proxy(hass, forward_client_ip=True, trusted_proxies=["not-an-address"])
    headers = proxy._request_headers(
        _Request("203.0.113.7", {hdrs.X_FORWARDED_FOR: "198.51.100.1"})
    )

    assert headers[hdrs.X_FORWARDED_FOR] == "203.0.113.7"


@pytest.mark.parametrize(
    ("entry", "covered"),
    [
        ("127.0.0.1", True),
        ("127.0.0.0/8", True),
        ("::1", False),
        ("10.0.0.0/8", False),
        ("nonsense", False),
        ("", False),
    ],
)
def test_which_entries_count_as_trusting_the_proxy(entry: str, covered: bool) -> None:
    """A network entry counts as much as the bare address."""
    assert http_config.covers_loopback(entry) is covered


def test_the_move_adds_the_proxy_without_disturbing_the_list() -> None:
    """And does not add it twice when a network already covers it."""
    already = http_config.target_config({"trusted_proxies": ["127.0.0.0/8"]}, 8124)
    assert already["trusted_proxies"] == ["127.0.0.0/8"]

    fresh = http_config.target_config({}, 8124)
    assert fresh["trusted_proxies"] == ["127.0.0.1"]
    assert fresh["use_x_forwarded_for"] is True
