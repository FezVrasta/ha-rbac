"""End-to-end tests driving the proxy with a raw client.

No browser is involved: the websocket protocol is small and fully specified, so
a plain aiohttp client exercises the same path a frontend would.
"""

import asyncio
import json
import socket
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.const import ROLE_READ_ONLY
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
    for domain in ("http", "websocket_api", "api", "config", "auth"):
        await async_setup_component(hass, domain, {"http": {}})
    await hass.async_block_till_done()

    # Registered before the server starts: aiohttp freezes the router on start.
    query_strings: list[str] = []

    async def _probe(request: web.Request) -> web.Response:
        query_strings.append(request.rel_url.query_string)
        return web.Response(text="ok")

    hass.http.app.router.add_route("GET", "/rbac_probe", _probe)

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
        "query_strings": query_strings,
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
    store = proxy_env["store"]
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


async def test_a_templates_value_never_reaches_a_denied_reader(
    proxy_env: dict[str, Any],
) -> None:
    """End to end: the decoy entity_ids buys nothing, and no value comes back.

    The subscription is permitted -- the request cannot be judged, but every
    result it streams reports what it read, and that can be. The rendered value
    of a denied lock must never arrive.
    """
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("lock.front", "unlocked")

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
        await ws.send_json(
            {
                "id": 1,
                "type": "render_template",
                "template": "{{ states('lock.front') }}",
                "entity_ids": ["sun.sun"],
            }
        )
        received: list[str] = []
        for _ in range(3):
            try:
                received.append(await asyncio.wait_for(ws.receive_str(), timeout=2))
            except TimeoutError:
                break
        await ws.close()

    assert "unlocked" not in " ".join(received)


async def test_a_template_reading_nothing_still_renders(
    proxy_env: dict[str, Any],
) -> None:
    """The dashboard heading is a template that reads no entity at all.

    Refusing every template showed restricted users raw Jinja on their home
    screen, which is what this exists to avoid.
    """
    store = proxy_env["store"]
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json(
            {"id": 1, "type": "render_template", "template": "Welcome home"}
        )
        rendered = None
        for _ in range(3):
            try:
                message = await asyncio.wait_for(ws.receive_json(), timeout=3)
            except TimeoutError:
                break
            if message.get("type") == "event":
                rendered = message["event"].get("result")
                break
        await ws.close()

    assert rendered == "Welcome home"


async def test_denial_reaches_the_deny_log(proxy_env: dict[str, Any]) -> None:
    """An operator has to be able to see why the UI broke."""
    store = proxy_env["store"]
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

    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"{proxy_env['base']}/api/states",
            headers={"Authorization": f"Bearer {token}"},
        ) as response,
    ):
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

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{proxy_env['base']}/api/services/light/turn_on",
            headers={"Authorization": f"Bearer {token}"},
            json={"entity_id": "light.kitchen"},
        ) as response,
    ):
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
            {
                "id": 1,
                "type": "supported_features",
                "features": {"coalesce_messages": 1},
            }
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


async def test_signed_paths_survive_the_proxy(proxy_env: dict[str, Any]) -> None:
    """Signed URLs are an HMAC over the exact path and ordered query parameters.

    Any prefix, reordering or added parameter breaks every camera snapshot and
    download link, and the signing secret cannot be re-created outside Home
    Assistant. This asserts the proxy forwards both untouched.
    """
    token = proxy_env["admin_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json(
            {
                "id": 1,
                "type": "auth/sign_path",
                "path": "/api/states",
                "expires": 30,
            }
        )
        message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    assert message["success"] is True, message
    signed = message["result"]["path"]
    assert "authSig=" in signed

    async with (
        aiohttp.ClientSession() as session,
        session.get(f"{proxy_env['base']}{signed}") as response,
    ):
        assert response.status == 200


async def test_query_string_is_forwarded_verbatim(proxy_env: dict[str, Any]) -> None:
    """Parameter order is part of the signature, so it must not be normalised."""
    seen = proxy_env["query_strings"]

    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"{proxy_env['base']}/rbac_probe?z=1&a=2&m=3",
            headers={"Authorization": f"Bearer {proxy_env['admin_token']}"},
        ) as response,
    ):
        assert response.status == 200

    assert seen == ["z=1&a=2&m=3"]


async def test_reusing_a_message_id_cannot_relabel_a_filter(
    proxy_env: dict[str, Any],
) -> None:
    """A repeated id in one coalesced frame must not swap which filter applies.

    Home Assistant requires ids to increase strictly and rejects a repeat, so a
    second use is always an attack. The proxy recorded it anyway, which meant
    `[{"id":5,"get_states"},{"id":5,"get_config"}]` correlated the get_states
    result to get_config's pass-through filter and forwarded every entity.
    """
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("lock.secret", "unlocked")

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
        await ws.send_str(
            json.dumps(
                [
                    {"id": 5, "type": "get_states"},
                    {"id": 5, "type": "get_config"},
                ]
            )
        )
        received: list[str] = []
        for _ in range(2):
            try:
                received.append(await asyncio.wait_for(ws.receive_str(), timeout=2))
            except TimeoutError:
                break
        await ws.close()

    assert "lock.secret" not in " ".join(received)


async def test_a_subscription_keeps_streaming_after_its_result(
    proxy_env: dict[str, Any],
) -> None:
    """Correlation must survive the result frame, or events are dropped.

    Only commands literally named `subscribe_*` used to keep their correlation,
    so history/stream, logbook/event_stream and weather/subscribe_forecast lost
    every event after the first reply.
    """
    hass, store = proxy_env["hass"], proxy_env["store"]
    hass.states.async_set("light.kitchen", "off")
    await _bind(store, proxy_env["read_only_user"], ROLE_READ_ONLY)
    token = proxy_env["read_only_token"]

    async with aiohttp.ClientSession() as session:
        ws = await _ws_login(session, proxy_env["ws"], token)
        await ws.send_json(
            {"id": 1, "type": "subscribe_entities", "entity_ids": ["light.kitchen"]}
        )
        result = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert result["success"] is True

        # Initial state, then a change: both must arrive.
        first = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert "a" in first["event"]

        hass.states.async_set("light.kitchen", "on")
        second = await asyncio.wait_for(ws.receive_json(), timeout=5)
        await ws.close()

    assert "c" in second["event"]
    assert "light.kitchen" in second["event"]["c"]
