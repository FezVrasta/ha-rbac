"""Tests for integration setup and the admin websocket API."""

import asyncio
import contextlib
import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_READ
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.ha_rbac import async_setup_entry, async_unload_entry
from custom_components.ha_rbac.catalog import Catalog
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
from custom_components.ha_rbac.decide import Decider
from custom_components.ha_rbac.denylog import DenyLog
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import Evaluator
from custom_components.ha_rbac.proxy import RbacProxy
from custom_components.ha_rbac.store import RbacStore


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


async def test_setup_refuses_an_instance_that_terminates_tls(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Refused before anything is staged, and above all before any restart.

    Reported as #29. The proxy is plaintext on both sides, so an instance
    holding its own certificate cannot be sat in front of. Coming up anyway
    would forward plaintext to a listener expecting TLS and serve nothing but
    errors, while reading as installed and enforcing -- and if the move ran, it
    would take the HTTPS port to answer plaintext on it, which is an outage
    until Home Assistant's own revert undoes it.

    The config flow refuses this too. This is the second half: a certificate
    can be added to an instance that was set up without one.
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

    with (
        patch.object(hass.http, "ssl_certificate", "/ssl/fullchain.pem", create=True),
        patch(
            "custom_components.ha_rbac.http_config.async_stage", new=AsyncMock()
        ) as stage,
        pytest.raises(ConfigEntryNotReady, match="serving HTTPS itself"),
    ):
        await async_setup_entry(hass, entry)

    stage.assert_not_called()
    assert DATA_RBAC not in hass.data


async def test_a_reload_rebinds_the_port(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A reload is unload-then-setup, and the proxy must bind its port again.

    If the re-bind cannot take the port straight back, setup raises
    ConfigEntryNotReady and the instance is left with nothing on the public
    port -- the reported reload lockout (#23). Holding a connection open across
    the unload mimics the reload request, which itself arrives through the
    proxy and so is in flight when it is told to stop.
    """
    port = entry.data[CONF_PROXY_PORT]

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()

    assert await async_unload_entry(hass, entry)
    await hass.async_block_till_done()

    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    assert hass.data[DATA_RBAC].proxy is not None


async def test_the_proxy_can_restart_on_the_same_port(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Stop then immediately start on the same port, which is what a reload does.

    The listener binds with reuse_address so the just-released socket does not
    block the next bind, and stopping releases the socket up front rather than
    only after draining. Together those are what let a reload take its port
    back instead of failing to bind.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    store = RbacStore(hass)
    await store.async_load()
    catalog = Catalog(hass)
    catalog.rebuild()
    decider = Decider(hass, catalog, REGISTRY)
    evaluator = Evaluator(hass, store)
    denylog = DenyLog(hass)
    port = _free_port()

    def _make() -> RbacProxy:
        return RbacProxy(
            hass,
            evaluator,
            decider,
            denylog,
            upstream_host="127.0.0.1",
            upstream_port=8123,
            bind_address="127.0.0.1",
            port=port,
        )

    first = _make()
    await first.async_start()
    await first.async_stop()

    # No settling in between: the port has to be free the moment `async_stop`
    # returns, because on a reload the setup half follows immediately.
    second = _make()
    try:
        await second.async_start()
    finally:
        await second.async_stop()
        await hass.async_block_till_done()


async def test_stopping_frees_the_port_first_and_drains_afterwards(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """The request driving an unload arrives through the proxy being unloaded.

    Waiting for it to drain is a cycle -- it cannot finish until the unload
    does. So the site is stopped and awaited, which frees the port and nothing
    else, and the drain is handed to a background task. Both halves are pinned:
    without the first, a reload cannot re-bind and the instance is stranded;
    without the second, every reload leaves a runner and an open client session
    behind for the life of the process.
    """
    for domain in ("http", "websocket_api"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    store = RbacStore(hass)
    await store.async_load()
    catalog = Catalog(hass)
    catalog.rebuild()
    proxy = RbacProxy(
        hass,
        Evaluator(hass, store),
        Decider(hass, catalog, REGISTRY),
        DenyLog(hass),
        upstream_host="127.0.0.1",
        upstream_port=8123,
        bind_address="127.0.0.1",
        port=_free_port(),
    )
    port = proxy._port
    await proxy.async_start()
    session = proxy._websession
    assert session is not None

    await proxy.async_stop()

    # The port is free straight away, before anything has been drained: this is
    # the state the setup half of a reload runs in.
    assert proxy._site is None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))

    # The drain is still outstanding, so nothing has been dropped on the floor.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert proxy._runner is None
    assert proxy._websession is None
    assert session.closed, "the client session outlived the proxy that opened it"


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


async def test_stopping_a_recording_reports_everything_it_saw(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """The panel writes its confirmation from this result, so it is pinned here.

    A recording notes entities, apps and capabilities, and the confirmation has
    to name all three -- reporting entities alone read as "recorded nothing" to
    anyone whose recording only opened a screen. `blocked` matters as much: an
    entity recorded under a `deny` rule is added and then overruled by it, and
    saying so is the only warning before a dashboard that is still empty.
    """
    data = hass.data[DATA_RBAC]
    role = await data.store.async_create_role(
        {
            "name": "Guests",
            "deny": {CAT_ENTITIES: {"domains": {"lock": True}}},
            "apps": {"allow": [], "deny": ["config/*"], "dashboards": {}},
        }
    )
    recording = data.recorder.start(role["id"])
    recording.note_entity("light.kitchen", POLICY_READ)
    recording.note_entity("lock.front", POLICY_READ)
    recording.apps.add("lovelace")
    recording.apps.add("config/automation")
    recording.capabilities.add("automations")

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/record/stop", "role_id": role["id"]}
    )
    result = (await client.receive_json())["result"]

    assert result["applied"] is True
    assert result["seen"]["entities"] == {
        "light.kitchen": POLICY_READ,
        "lock.front": POLICY_READ,
    }
    assert result["seen"]["apps"] == ["config/automation", "lovelace"]
    assert result["seen"]["capabilities"] == ["automations"]
    # Added to the allow side, and vetoed by the role's own denial. Both kinds
    # are reported: an entity the deny rule overrules, and a screen it does.
    assert result["blocked"] == ["lock.front"]
    assert result["blocked_apps"] == ["config/automation"]


async def test_discarding_a_recording_leaves_the_role_alone(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Discarding is how a recording is abandoned, and it must write nothing."""
    data = hass.data[DATA_RBAC]
    role = await data.store.async_create_role({"name": "Guests"})
    data.recorder.start(role["id"]).note_entity("light.kitchen", POLICY_READ)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/record/stop", "role_id": role["id"], "apply": False}
    )
    result = (await client.receive_json())["result"]

    assert result["applied"] is False
    assert result["seen"]["entities"] == {"light.kitchen": POLICY_READ}
    assert data.store.roles[role["id"]] == role


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

    assert staged == [
        {
            "server_host": ["127.0.0.1"],
            "server_port": 8124,
            # Without these every request reaches Home Assistant from the proxy
            # and it sees one client for the whole house.
            "trusted_proxies": ["127.0.0.1"],
            "use_x_forwarded_for": True,
        }
    ]
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


async def test_a_loopback_only_instance_is_not_moved_again_on_reload(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A reload of an already-moved instance must not move and restart it.

    Home Assistant can report its loopback bind as a bare string rather than a
    one-item list. That used to read as unaligned, so setting up again -- which
    is half of a reload -- staged another move and restarted. After the restart
    it still read as unaligned, so it restarted again: a loop that leaves the
    instance unreachable until the box is power-cycled. Alignment now recognises
    a loopback-only bind however it is spelled, so no move is staged.
    """
    for domain in ("http", "websocket_api", "api"):
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

    restarts: list[ServiceCall] = []
    hass.services.async_register("homeassistant", "restart", restarts.append)

    # Home Assistant is already loopback-only, but spelled as a bare string,
    # which is exactly the representation that used to defeat alignment.
    server = SimpleNamespace(server_host="127.0.0.1", server_port=8123)
    with patch.object(hass, "http", server, create=True):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert not restarts, "an already-moved instance must not be moved again"
    proxy = hass.data[DATA_RBAC].proxy
    assert proxy is not None, "the proxy should just start"

    await proxy.async_stop()
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
