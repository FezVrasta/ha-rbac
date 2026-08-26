"""Tests for moving Home Assistant's own listener out of the way.

The setup this integration needs is three ordered steps, and both wrong orders
fail badly: taking the port before Home Assistant vacates it cannot bind, and
closing the door before the proxy answers locks you out. These cover doing it
automatically, and in particular the interlock that keeps the second one from
happening.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_rbac import http_config


def test_the_target_keeps_everything_it_is_not_moving() -> None:
    """A move must not quietly reset SSL, CORS or the ban settings.

    It is built from the running config for that reason. Losing
    `ssl_certificate` here would take the instance off HTTPS as a side effect
    of installing an access control layer.
    """
    current = {
        "server_host": ["0.0.0.0"],
        "server_port": 8123,
        "ssl_certificate": "/ssl/fullchain.pem",
        "cors_allowed_origins": ["https://cast.home-assistant.io"],
        "trusted_proxies": ["10.0.0.0/8"],
        "ip_ban_enabled": True,
    }
    moved = http_config.target_config(current, 8124)

    assert moved["server_host"] == ["127.0.0.1"]
    assert moved["server_port"] == 8124
    assert moved["ssl_certificate"] == "/ssl/fullchain.pem"
    assert moved["cors_allowed_origins"] == ["https://cast.home-assistant.io"]
    assert moved["trusted_proxies"] == ["10.0.0.0/8"]
    assert moved["ip_ban_enabled"] is True


def test_the_target_drops_what_home_assistant_records_about_a_config() -> None:
    """Carrying `error` over would present a fresh config as already failed."""
    moved = http_config.target_config(
        {
            "server_port": 8123,
            "created_at": "2026-01-01T00:00:00+00:00",
            "error": "not_promoted",
            "error_message": "nobody confirmed it",
        },
        8124,
    )
    assert "created_at" not in moved
    assert "error" not in moved
    assert "error_message" not in moved


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        (["127.0.0.1"], 8124, True),
        (["0.0.0.0"], 8124, False),
        (["127.0.0.1"], 8123, False),
        ([], 8124, False),
        (None, 8124, False),
        (["127.0.0.1", "192.168.1.5"], 8124, False),
    ],
    ids=[
        "where-we-want-it",
        "still-on-the-network",
        "loopback-but-wrong-port",
        "listening-everywhere",
        "unset",
        "loopback-and-something-else",
    ],
)
async def test_alignment_is_read_from_the_running_server(
    hass: HomeAssistant, host: Any, port: int, expected: bool
) -> None:
    """The store says what Home Assistant was asked to do, not what it is doing.

    A staged config that failed its trial and reverted still sits in the store,
    so believing it would have the proxy report a boundary that is not there.
    """
    server = SimpleNamespace(server_host=host, server_port=port)
    with patch.object(hass, "http", server, create=True):
        assert http_config.is_aligned(hass, 8124) is expected


async def test_an_unusable_config_is_refused_before_anything_restarts(
    hass: HomeAssistant,
) -> None:
    """The bind check is what keeps a bad port from becoming a failed boot.

    Home Assistant would recover on its own, but only after restarting twice
    and leaving the instance unreachable in between.
    """
    store = AsyncMock()
    with (
        patch(
            "homeassistant.components.http.server.async_verify_can_bind",
            side_effect=HomeAssistantError("Failed to create HTTP server at port 8124"),
        ),
        patch(
            "homeassistant.components.http.config.async_get_and_load_store",
            return_value=store,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await http_config.async_stage(hass, {"server_port": 8124})

    store.async_set_pending.assert_not_called()


async def test_nothing_is_promoted_when_nothing_is_staged(
    hass: HomeAssistant,
) -> None:
    """Promotion is only ever confirmation of a trial that is actually running."""
    store = AsyncMock()
    store.pending = None
    with patch(
        "homeassistant.components.http.config.async_get_and_load_store",
        return_value=store,
    ):
        assert await http_config.async_promote(hass) is False
    store.async_promote_pending.assert_not_called()


async def test_a_failed_promotion_is_reported_rather_than_raised(
    hass: HomeAssistant,
) -> None:
    """Failing to confirm is survivable: the revert puts everything back.

    Raising here would fail the config entry setup instead, which helps nobody
    -- the way back in is already scheduled.
    """
    store = AsyncMock()
    store.pending = {"server_port": 8124}
    store.async_promote_pending.side_effect = HomeAssistantError("nope")
    with patch(
        "homeassistant.components.http.config.async_get_and_load_store",
        return_value=store,
    ):
        assert await http_config.async_promote(hass) is False


async def test_an_unrecognisable_home_assistant_is_reported_not_guessed(
    hass: HomeAssistant,
) -> None:
    """None of this is public API, so the answer to "can I?" must be honest.

    Answering yes and failing later would restart Home Assistant into a config
    nobody staged. Answering no falls back to the documented manual route.
    """
    with patch(
        "homeassistant.components.http.config.async_get_and_load_store",
        side_effect=AttributeError("moved in a later release"),
    ):
        assert await http_config.async_can_manage(hass) is False
