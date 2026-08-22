"""End-to-end tests driving the proxy with a raw client.

No browser is involved: the websocket protocol is small and fully specified, so
a plain aiohttp client exercises the same path a frontend would.
"""

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.decide import Decider
from custom_components.ha_rbac.denylog import DenyLog
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import Evaluator
from custom_components.ha_rbac.proxy import RbacProxy
from custom_components.ha_rbac.store import RbacStore
from custom_components.ha_rbac.const import ROLE_READ_ONLY

from tests.common import MockUser


def _free_port() -> int:
    """Return an unused TCP port."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="proxy_env")
async def proxy_env_fixture(
    hass: HomeAssistant,
    aiohttp_server: Any,
    socket_enabled: None,
    hass_access_token: str,
    hass_read_only_user: MockUser,
    hass_read_only_access_token: str,
) -> dict[str, Any]:
    """Start Home Assistant behind the proxy and return the pieces under test."""
    for domain in ("http", "websocket_api", "api", "config"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    upstream = await aiohttp_server(hass.http.app)

    store = RbacStore(hass)
    await store.async_load()
    catalog = Catalog(hass)
    catalog.rebuild()
    evaluator = Evaluator(hass, store)
    denylog = DenyLog(hass)
    decider = Decider(hass, catalog, REGISTRY)

    port = _free_port()
    proxy = RbacProxy(
        hass,
        evaluator,
        decider,
        denylog,
        upstream_host=upstream.host,
        upstream_port=upstream.port,
        bind_address="127.0.0.1",
        port=port,
    )
    await proxy.async_start()

    yield {
        "hass": hass,
        "store": store,
        "denylog": denylog,
        "base": f"http://127.0.0.1:{port}",
        "ws": f"http://127.0.0.1:{port}/api/websocket",
        "admin_token": hass_access_token,
        "read_only_user": hass_read_only_user,
        "read_only_token": hass_read_only_access_token,
    }

    await proxy.async_stop()


async def _bind(store: RbacStore, user: MockUser, role_id: str) -> None:
    """Bind a user to a role."""
    await store.async_set_binding(user.id, [role_id])


async def _ws_login(session: aiohttp.ClientSession, url: str, token: str) -> Any:
    """Complete the websocket auth handshake and return the open socket."""
    ws = await session.ws_connect(url)
    first = await asyncio.wait_for(ws.receive_json(), timeout=5)
    assert first["type"] == "auth_required"
    await ws.send_json({"type": "auth", "access_token": token})
    result = await asyncio.wait_for(ws.receive_json(), timeout=5)
    assert result["type"] == "auth_ok", result
    return ws


async def test_handshake_is_relayed(proxy_env: dict[str, Any]) -> None:
    """auth_required must arrive well inside Home Assistant's ten second window."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.close()


async def test_get_states_is_filtered(proxy_env: dict[str, Any]) -> None:
    """A read-only user sees every entity; a denied one disappears."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("lock.front", "locked")

    user, token = proxy_env["read_only_user"], proxy_env["read_only_token"]
    role = await store.async_create_role(
        {
            "name": "No locks",
            "allow": {"entities": {"all": {"read": True}}},
            "deny": {"entities": {"domains": {"lock": True}}},
        }
    )
    await store.async_set_binding(user.id, [role["id"]])

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json({"id": 1, "type": "get_states"})
        message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    entity_ids = {state["entity_id"] for state in message["result"]}
    assert "light.kitchen" in entity_ids
    assert "lock.front" not in entity_ids


async def test_render_template_is_refused_over_the_wire(
    proxy_env: dict[str, Any],
) -> None:
    """The headline case, end to end: the decoy entity_ids buys nothing."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("lock.front", "unlocked")
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json(
            {
                "id": 1,
                "type": "render_template",
                "template": "{{ states('lock.front') }}",
                "entity_ids": ["sun.sun"],
            }
        )
        message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    assert message["success"] is False
    assert message["error"]["code"] == "unauthorized"
    assert "unlocked" not in json.dumps(message)


async def test_denial_reaches_the_deny_log(proxy_env: dict[str, Any]) -> None:
    """An operator has to be able to see why the UI broke."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json({"id": 1, "type": "execute_script", "sequence": []})
        await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    recent = proxy_env["denylog"].async_recent()
    assert recent[0]["name"] == "execute_script"
    assert recent[0]["reason"] == "tier"


async def test_admin_passes_through_unfiltered(proxy_env: dict[str, Any]) -> None:
    """The fast path must not filter, so administrators pay nothing."""
    hass = proxy_env["hass"]
    hass.states.async_set("lock.front", "locked")

    token = proxy_env["admin_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json({"id": 1, "type": "get_states"})
        message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    entity_ids = {state["entity_id"] for state in message["result"]}
    assert "lock.front" in entity_ids


async def test_rest_states_are_filtered(proxy_env: dict[str, Any]) -> None:
    """The REST surface is covered too, not just the websocket."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("lock.front", "locked")

    user, token = proxy_env["read_only_user"], proxy_env["read_only_token"]
    role = await store.async_create_role(
        {
            "name": "No locks",
            "allow": {"entities": {"all": {"read": True}}},
            "deny": {"entities": {"domains": {"lock": True}}},
        }
    )
    await store.async_set_binding(user.id, [role["id"]])

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{proxy_env['base']}/api/states",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            payload = await response.json()

    entity_ids = {state["entity_id"] for state in payload}
    assert "light.kitchen" in entity_ids
    assert "lock.front" not in entity_ids


async def test_rest_control_is_denied_for_read_only(
    proxy_env: dict[str, Any],
) -> None:
    """POST is a mutation regardless of what the path is called."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("light.kitchen", "off")
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{proxy_env['base']}/api/services/light/turn_on",
            headers={"Authorization": f"Bearer {token}"},
            json={"entity_id": "light.kitchen"},
        ) as response:
            assert response.status == 401


async def test_coalesced_batches_are_handled(proxy_env: dict[str, Any]) -> None:
    """Once negotiated, both directions may carry JSON arrays."""
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("light.kitchen", "on")
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json(
            {"id": 1, "type": "supported_features",
             "features": {"coalesce_messages": 1}}
        )
        await asyncio.wait_for(ws.receive_json(), timeout=5)

        # A batch containing one permitted and one refused command.
        await ws.send_str(
            json.dumps(
                [
                    {"id": 2, "type": "get_states"},
                    {"id": 3, "type": "execute_script", "sequence": []},
                ]
            )
        )

        seen: dict[int, Any] = {}
        while len(seen) < 2:
            raw = await asyncio.wait_for(ws.receive_str(), timeout=5)
            parsed = json.loads(raw)
            for message in parsed if isinstance(parsed, list) else [parsed]:
                if isinstance(message, dict) and "id" in message:
                    seen.setdefault(message["id"], message)
        await ws.close()

    assert seen[2]["success"] is True
    assert seen[3]["success"] is False
