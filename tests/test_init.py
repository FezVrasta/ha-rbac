"""Tests for integration setup and the admin websocket API."""

import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.ha_rbac import async_setup_entry, async_unload_entry
from custom_components.ha_rbac.const import (
    CONF_BIND_ADDRESS,
    CONF_MANAGE_HTTP,
    CONF_PROXY_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DATA_RBAC,
    DOMAIN,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_READ_ONLY,
)


def _free_port() -> int:
    """Return an unused TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="entry")
async def entry_fixture(hass: HomeAssistant, socket_enabled: None) -> MockConfigEntry:
    """Set up the integration."""
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PROXY_PORT: _free_port(),
            CONF_BIND_ADDRESS: "127.0.0.1",
            CONF_UPSTREAM_HOST: "127.0.0.1",
            CONF_UPSTREAM_PORT: 8123,
        },
    )
    entry.add_to_hass(hass)
    # Called directly rather than through the config entry machinery: the
    # frontend package is not installed in a core checkout, so panel
    # registration cannot succeed here.
    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    yield entry

    if DATA_RBAC in hass.data:
        await async_unload_entry(hass, entry)


async def test_setup_exposes_runtime_state(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The integration loads and exposes its runtime state."""
    data = hass.data[DATA_RBAC]
    assert data.store.roles
    assert data.catalog.commands


async def test_setup_survives_an_unavailable_frontend(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Enforcement must not depend on the panel being registerable.

    The panel is how roles are administered, not how they are enforced.
    """
    assert DATA_RBAC in hass.data


async def test_unload_releases_everything(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Unloading must leave no listener behind."""
    assert await async_unload_entry(hass, entry)
    await hass.async_block_till_done()
    assert DATA_RBAC not in hass.data


async def test_roles_list_requires_admin(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """The admin API is the real gate; the panel flag is only cosmetic."""
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": f"{DOMAIN}/roles/list"})
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"


async def test_roles_crud_over_websocket(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Roles round-trip through the admin API."""
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/roles/list"})
    listed = await client.receive_json()
    assert {role["id"] for role in listed["result"]} == {
        ROLE_ADMIN,
        ROLE_EDITOR,
        "user",
        ROLE_READ_ONLY,
    }

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/roles/create", "role": {"name": "Guests"}}
    )
    created = await client.receive_json()
    role_id = created["result"]["id"]

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/roles/update",
            "role_id": role_id,
            "changes": {"name": "Visitors"},
        }
    )
    assert (await client.receive_json())["result"]["name"] == "Visitors"

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/roles/delete", "role_id": role_id}
    )
    assert (await client.receive_json())["success"] is True


async def test_predefined_role_edit_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Built-in roles are defined in code and must stay that way."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/roles/update",
            "role_id": ROLE_ADMIN,
            "changes": {"name": "Hijacked"},
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_allowed"


async def test_catalog_is_exposed_for_the_editor(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """The editor offers real options without shipping a list of commands."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/catalog"})
    result = (await client.receive_json())["result"]
    assert len(result["commands"]) > 20
    assert result["degraded"] is False
    assert any(entry["tier"] == "admin" for entry in result["commands"])


async def test_simulate_explains_a_denial(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_user: Any,
) -> None:
    """Without an explanation, a broken screen is all an operator gets."""
    data = hass.data[DATA_RBAC]
    await data.store.async_set_binding(hass_read_only_user.id, [ROLE_READ_ONLY])

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/simulate",
            "user_id": hass_read_only_user.id,
            "command": "execute_script",
            "payload": {"sequence": []},
        }
    )
    result = (await client.receive_json())["result"]
    assert result["allowed"] is False
    assert result["reason"] == "tier"
    assert "execute_script" in result["detail"]


async def test_bindings_round_trip(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_user: Any,
) -> None:
    """Assignments are listed and saved through the admin API."""
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/bindings/set",
            "user_id": hass_read_only_user.id,
            "role_ids": [ROLE_READ_ONLY],
        }
    )
    assert (await client.receive_json())["success"] is True

    await client.send_json_auto_id({"type": f"{DOMAIN}/bindings/list"})
    listed = (await client.receive_json())["result"]
    entry_for_user = next(
        item for item in listed if item["user_id"] == hass_read_only_user.id
    )
    assert entry_for_user["role_ids"] == [ROLE_READ_ONLY]


async def test_binding_an_unknown_role_is_refused(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_user: Any,
) -> None:
    """A typo must not produce a binding that denies everything."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/bindings/set",
            "user_id": hass_read_only_user.id,
            "role_ids": ["nope"],
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_a_taken_port_explains_the_setup_order(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Home Assistant still holding 8123 is the one predictable install mistake.

    The proxy takes the port Home Assistant used to answer on, so installing
    before moving Home Assistant off it fails to bind. Left bare that surfaces
    as "Failed to set up" with an errno, which says nothing about the fix.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_PROXY_PORT: taken.getsockname()[1],
                CONF_BIND_ADDRESS: "127.0.0.1",
                CONF_UPSTREAM_HOST: "127.0.0.1",
                CONF_UPSTREAM_PORT: 8124,
            },
        )
        entry.add_to_hass(hass)

        with pytest.raises(ConfigEntryNotReady) as raised:
            await async_setup_entry(hass, entry)

    message = str(raised.value)
    assert "already in use" in message
    assert "8124" in message, "it must name the port to move Home Assistant to"
    assert "Settings > System > Network" in message, "and where to do it"


async def test_the_move_is_staged_and_the_proxy_waits_for_the_restart(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Home Assistant has to vacate the port before anything can take it.

    So the first run stages the move and restarts rather than binding: the port
    the proxy wants is still held by Home Assistant until the process goes down.
    Trying anyway is the bind error this whole feature exists to avoid.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PROXY_PORT: _free_port(),
            CONF_BIND_ADDRESS: "127.0.0.1",
            CONF_UPSTREAM_HOST: "127.0.0.1",
            CONF_UPSTREAM_PORT: 8124,
            CONF_MANAGE_HTTP: True,
        },
    )
    entry.add_to_hass(hass)

    staged: list[dict[str, Any]] = []
    restarts: list[ServiceCall] = []
    hass.services.async_register("homeassistant", "restart", restarts.append)

    async def _stage(_hass: HomeAssistant, config: dict[str, Any]) -> None:
        staged.append(config)

    with (
        patch(
            "custom_components.ha_rbac.http_config.async_current",
            return_value={"server_host": ["0.0.0.0"], "server_port": 8123},
        ),
        patch("custom_components.ha_rbac.http_config.async_stage", _stage),
    ):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert staged == [{"server_host": ["127.0.0.1"], "server_port": 8124}]
    assert len(restarts) == 1, "Home Assistant has to restart to apply the move"
    assert hass.data[DATA_RBAC].proxy is None, "nothing may bind before the restart"

    await async_unload_entry(hass, entry)


async def test_the_move_is_confirmed_only_once_the_proxy_serves(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """The interlock. Binding a port says nothing about answering on it.

    Home Assistant reverts an unconfirmed config within five minutes, which is
    the way back in if this layer is broken. Confirming on `async_start` alone
    would throw that away for a proxy that listens and forwards to nothing.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PROXY_PORT: _free_port(),
            CONF_BIND_ADDRESS: "127.0.0.1",
            CONF_UPSTREAM_HOST: "127.0.0.1",
            CONF_UPSTREAM_PORT: 8123,
            CONF_MANAGE_HTTP: True,
        },
    )
    entry.add_to_hass(hass)

    served = MagicMock()
    served.__aenter__ = AsyncMock(return_value=MagicMock(status=200))
    served.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("custom_components.ha_rbac.http_config.is_aligned", return_value=True),
        patch(
            "custom_components.ha_rbac.http_config.async_promote", AsyncMock()
        ) as promote,
        patch("custom_components.ha_rbac.async_get_clientsession") as session,
    ):
        session.return_value.get.return_value = served
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
        assert promote.called, "a proxy that answers must confirm the move"

    await async_unload_entry(hass, entry)


async def test_a_proxy_that_does_not_answer_leaves_the_move_to_revert(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Which is what puts Home Assistant back on its old port by itself.

    Nothing is mocked here beyond the promotion: the proxy binds for real and
    its upstream really is absent, so the request through it fails the way it
    would on a misconfigured install.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PROXY_PORT: _free_port(),
            CONF_BIND_ADDRESS: "127.0.0.1",
            CONF_UPSTREAM_HOST: "127.0.0.1",
            # A port nothing is listening on, named rather than assumed: other
            # tests in this suite bind Home Assistant's usual ones, and a
            # working upstream here would make this pass for the wrong reason.
            CONF_UPSTREAM_PORT: _free_port(),
            CONF_MANAGE_HTTP: True,
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ha_rbac.http_config.is_aligned", return_value=True),
        patch(
            "custom_components.ha_rbac.http_config.async_promote", AsyncMock()
        ) as promote,
    ):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert not promote.called, "an unanswerable proxy must not confirm anything"

    await async_unload_entry(hass, entry)
