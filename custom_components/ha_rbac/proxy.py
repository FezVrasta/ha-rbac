"""The filtering reverse proxy.

Modelled on `homeassistant/components/hassio/ingress.py`, which is the working
reverse proxy already in the tree.

Two constraints shape everything here. The proxy must be mounted at `/` and must
not touch paths or query strings, because signed URLs (`authSig`) are an HMAC
over the exact path and the ordered list of non-safe query parameters, and the
signing secret lives in `hass.data` where it cannot be re-created. And the
proxy must never send a command upstream on a client's websocket, because
message ids have to increase strictly per connection -- being in-process, it
reads the registries directly instead.
"""

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import ClientTimeout, ClientWebSocketResponse, hdrs, web
from aiohttp.helpers import must_be_empty_body
from homeassistant.auth.models import User
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.async_ import create_eager_task
from homeassistant.util.json import json_loads
from multidict import CIMultiDict
from yarl import URL

from .decide import KIND_HTTP, KIND_WS, Decider, Decision
from .denylog import Denial, DenyLog
from .filters import REGISTRY, FilterContext
from .policy import Evaluator

_LOGGER = logging.getLogger(__name__)

INIT_HEADERS_FILTER = {
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_ENCODING,
    hdrs.TRANSFER_ENCODING,
    # Ask upstream for plain text so responses can be inspected.
    hdrs.ACCEPT_ENCODING,
    hdrs.SEC_WEBSOCKET_EXTENSIONS,
    hdrs.SEC_WEBSOCKET_PROTOCOL,
    hdrs.SEC_WEBSOCKET_VERSION,
    hdrs.SEC_WEBSOCKET_KEY,
}
RESPONSE_HEADERS_FILTER = {
    hdrs.TRANSFER_ENCODING,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_ENCODING,
}

MAX_WEBSOCKET_MESSAGE_SIZE = 16 * 1024 * 1024
MAX_SIMPLE_RESPONSE_SIZE = 4194000
DISABLED_TIMEOUT = ClientTimeout(total=None)

WS_PATH = "/api/websocket"

# Websocket message types from the auth handshake.
TYPE_AUTH = "auth"
TYPE_AUTH_REQUIRED = "auth_required"
TYPE_AUTH_OK = "auth_ok"
TYPE_RESULT = "result"
TYPE_EVENT = "event"

ERR_UNAUTHORIZED = "unauthorized"


def _is_websocket(request: web.Request) -> bool:
    """Return True if a request is a websocket upgrade."""
    headers = request.headers
    return bool(
        "upgrade" in headers.get(hdrs.CONNECTION, "").lower()
        and headers.get(hdrs.UPGRADE, "").lower() == "websocket"
    )


class RbacProxy:
    """Serves Home Assistant with per-user filtering applied."""

    def __init__(
        self,
        hass: HomeAssistant,
        evaluator: Evaluator,
        decider: Decider,
        denylog: DenyLog,
        *,
        upstream_host: str,
        upstream_port: int,
        bind_address: str,
        port: int,
        forward_client_ip: bool = False,
    ) -> None:
        """Initialise the proxy."""
        self._hass = hass
        self._evaluator = evaluator
        self._decider = decider
        self._denylog = denylog
        self._base = URL.build(scheme="http", host=upstream_host, port=upstream_port)
        self._bind_address = bind_address
        self._port = port
        # Home Assistant rejects forwarded headers outright from a peer that is
        # not in `trusted_proxies`, and attributing every login to the proxy
        # would let one bad password ban every user. The caller checks the
        # configuration and leaves this off unless it is safe.
        self._forward_client_ip = forward_client_ip
        self._runner: web.AppRunner | None = None
        self._websession = async_get_clientsession(hass)

    async def async_start(self) -> None:
        """Bind the listener."""
        app = web.Application(client_max_size=1024**3)
        # Mounted at "/" with no prefix: signed paths are an HMAC over the exact
        # path, so any rewriting breaks every camera snapshot and download link.
        app.router.add_route("*", "/{path:.*}", self._handle)

        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self._bind_address, self._port, shutdown_timeout=10
        )
        await site.start()
        _LOGGER.info(
            "RBAC proxy listening on %s:%s, forwarding to %s",
            self._bind_address,
            self._port,
            self._base,
        )

    async def async_stop(self) -> None:
        """Release the listener."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @callback
    def _upstream_url(self, request: web.Request) -> URL:
        """Return the upstream URL, preserving path and query byte for byte."""
        return self._base.join(URL(request.rel_url.raw_path_qs, encoded=True))

    @callback
    def _request_headers(self, request: web.Request) -> CIMultiDict[str]:
        """Build the upstream request headers."""
        headers = CIMultiDict(
            (name, value)
            for name, value in request.headers.items()
            if name not in INIT_HEADERS_FILTER
        )
        # Only meaningful when Home Assistant trusts this proxy; the caller
        # checks `trusted_proxies` and disables this otherwise, because
        # untrusted forwarded headers make HA reject the request outright.
        if self._forward_client_ip:
            peer = request.remote or ""
            existing = request.headers.get(hdrs.X_FORWARDED_FOR)
            headers[hdrs.X_FORWARDED_FOR] = f"{existing}, {peer}" if existing else peer
            headers[hdrs.X_FORWARDED_HOST] = request.headers.get(hdrs.HOST, "")
            headers[hdrs.X_FORWARDED_PROTO] = request.scheme
        return headers

    async def _resolve_user(self, request: web.Request) -> User | None:
        """Identify the user behind a request.

        In-process, so this is a single in-memory call. An external proxy could
        not do it at all: the access token carries only `{iss, iat, exp}` and is
        signed with a per-refresh-token secret.
        """
        if (auth := request.headers.get(hdrs.AUTHORIZATION)) and auth.startswith(
            "Bearer "
        ):
            token = auth.removeprefix("Bearer ")
            if (refresh_token := self._hass.auth.async_validate_access_token(token)):
                return refresh_token.user
        return None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """Route a request to the websocket relay or the HTTP passthrough."""
        if _is_websocket(request):
            return await self._handle_websocket(request)
        return await self._handle_http(request)

    async def _handle_http(self, request: web.Request) -> web.StreamResponse:
        """Proxy one HTTP request, filtering the response where needed."""
        user = await self._resolve_user(request)
        permissions = self._evaluator.async_permissions(user)

        decision = Decision(allowed=True)
        if user is not None and not permissions.full_access:
            body = await self._peek_json(request)
            name = f"{request.method} {request.path}"
            decision = self._decider.decide(permissions, KIND_HTTP, name, body)
            if not decision.allowed:
                self._record(user, KIND_HTTP, name, decision)
                return web.json_response(
                    {"message": "Unauthorized"}, status=401
                )

        try:
            return await self._forward_http(request, permissions, decision)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Upstream error for %s: %s", request.path, err)
            raise web.HTTPBadGateway from None

    async def _peek_json(self, request: web.Request) -> dict[str, Any]:
        """Read a JSON body without consuming it for the upstream request."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return {}
        try:
            raw = await request.read()
        except (aiohttp.ClientError, asyncio.CancelledError):
            return {}
        if not raw:
            return {}
        try:
            parsed = json_loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {"_body": parsed}

    async def _forward_http(
        self,
        request: web.Request,
        permissions: Any,
        decision: Decision,
    ) -> web.StreamResponse:
        """Send the request upstream and relay the response."""
        method = request.method
        data = None if method in ("GET", "HEAD") else await request.read()

        async with self._websession.request(
            method,
            self._upstream_url(request),
            headers=self._request_headers(request),
            allow_redirects=False,
            data=data,
            timeout=DISABLED_TIMEOUT,
            skip_auto_headers={hdrs.CONTENT_TYPE},
        ) as result:
            headers = CIMultiDict(
                (name, value)
                for name, value in result.headers.items()
                if name not in RESPONSE_HEADERS_FILTER
            )
            content_type = (
                result.headers.get(hdrs.CONTENT_TYPE, "application/octet-stream")
                .partition(";")[0]
                .strip()
            )

            if must_be_empty_body(method, result.status):
                return web.Response(headers=headers, status=result.status)

            if decision.filter_response and content_type == "application/json":
                filtered = await self._filter_http_body(result, permissions, request)
                if filtered is not None:
                    return web.json_response(
                        filtered, status=result.status, headers=headers
                    )

            # Large or non-JSON bodies stream through untouched. Camera and
            # media responses are the common case and there is nothing in them
            # to filter that filtering the entity's state has not already
            # handled.
            response = web.StreamResponse(status=result.status, headers=headers)
            response.content_type = content_type
            await response.prepare(request)
            try:
                async for chunk, _ in result.content.iter_chunks():
                    await response.write(chunk)
            except (aiohttp.ClientError, ConnectionResetError, ConnectionError) as err:
                _LOGGER.debug("Stream error for %s: %s", request.path, err)
            return response

    async def _filter_http_body(
        self, result: aiohttp.ClientResponse, permissions: Any, request: web.Request
    ) -> Any:
        """Filter a JSON response body, or return None to stream it instead."""
        length = result.headers.get(hdrs.CONTENT_LENGTH)
        if length is not None and int(length) > MAX_SIMPLE_RESPONSE_SIZE:
            return None
        raw = await result.read()
        if not raw:
            return None
        try:
            payload = json_loads(raw)
        except ValueError:
            return None
        ctx = FilterContext(self._hass, permissions.check_entity)
        return REGISTRY.filter_result(f"{request.method} {request.path}", ctx, payload)

    @callback
    def _record(self, user: User | None, kind: str, name: str, decision: Decision) -> None:
        """Note a denial so an operator can see why a UI broke."""
        self._denylog.async_record(
            Denial(
                user_id=user.id if user else "",
                user_name=user.name or "" if user else "",
                kind=kind,
                name=name,
                reason=decision.reason,
                resources=decision.resources,
            )
        )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Relay a websocket connection, inspecting every message."""
        protocols = [
            proto.strip()
            for proto in request.headers.get(hdrs.SEC_WEBSOCKET_PROTOCOL, "").split(",")
            if proto.strip()
        ]
        client_ws = web.WebSocketResponse(
            protocols=protocols,
            autoclose=False,
            # Pings are relayed explicitly; letting aiohttp answer upstream
            # would keep Home Assistant believing a dead client is alive.
            autoping=False,
            max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
        )
        await client_ws.prepare(request)

        try:
            # Connect upstream eagerly. Home Assistant sends `auth_required`
            # immediately and gives the client ten seconds to answer, so a lazy
            # connect would start that clock late.
            async with self._websession.ws_connect(
                self._upstream_url(request),
                headers=self._request_headers(request),
                protocols=protocols,
                autoclose=False,
                autoping=False,
                max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
            ) as server_ws:
                session = _WsSession(self, client_ws, server_ws)
                await asyncio.wait(
                    [
                        create_eager_task(session.pump_inbound()),
                        create_eager_task(session.pump_outbound()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Websocket proxy error: %s", err)

        return client_ws


class _WsSession:
    """One proxied websocket connection.

    Holds the per-connection state the filtering depends on: which user
    authenticated, which request id asked for what, and whether the client
    negotiated coalesced framing.
    """

    def __init__(
        self,
        proxy: RbacProxy,
        client_ws: web.WebSocketResponse,
        server_ws: ClientWebSocketResponse,
    ) -> None:
        """Initialise the session."""
        self._proxy = proxy
        self._client = client_ws
        self._server = server_ws
        self._user: User | None = None
        self._permissions = proxy._evaluator.async_permissions(None)
        # Correlates a result or event back to the command that asked for it.
        # Without it an outbound frame cannot be filtered, so an unknown id is
        # dropped rather than forwarded.
        self._pending: dict[int, str] = {}
        self._coalesced = False

    @property
    def _full_access(self) -> bool:
        """Return True if this connection needs no inspection at all."""
        return self._permissions.full_access

    async def pump_inbound(self) -> None:
        """Relay client -> Home Assistant, deciding on each command."""
        try:
            async for msg in self._client:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    await self._on_client_text(msg.data)
                elif msg.type is aiohttp.WSMsgType.BINARY:
                    # The first byte is a per-connection handler id negotiated
                    # in an earlier result; the payload is opaque. Relayed as-is.
                    await self._server.send_bytes(msg.data)
                elif msg.type is aiohttp.WSMsgType.PING:
                    await self._server.ping(msg.data)
                elif msg.type is aiohttp.WSMsgType.PONG:
                    await self._server.pong(msg.data)
                else:
                    break
        except (RuntimeError, ConnectionResetError, asyncio.CancelledError):
            pass

    async def pump_outbound(self) -> None:
        """Relay Home Assistant -> client, filtering results and events."""
        try:
            async for msg in self._server:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    await self._on_server_text(msg.data)
                elif msg.type is aiohttp.WSMsgType.BINARY:
                    await self._client.send_bytes(msg.data)
                elif msg.type is aiohttp.WSMsgType.PING:
                    await self._client.ping(msg.data)
                elif msg.type is aiohttp.WSMsgType.PONG:
                    await self._client.pong(msg.data)
                else:
                    break
        except (RuntimeError, ConnectionResetError, asyncio.CancelledError):
            pass

    async def _on_client_text(self, raw: str) -> None:
        """Handle one inbound text frame, which may be a coalesced batch."""
        try:
            parsed = json_loads(raw)
        except ValueError:
            await self._server.send_str(raw)
            return

        messages = parsed if isinstance(parsed, list) else [parsed]
        forward: list[dict[str, Any]] = []

        for message in messages:
            if not isinstance(message, dict):
                continue
            if await self._intercept(message):
                forward.append(message)

        if not forward:
            return
        # Preserve the framing the client chose.
        if isinstance(parsed, list):
            await self._server.send_str(json.dumps(forward))
        else:
            await self._server.send_str(json.dumps(forward[0]))

    async def _intercept(self, message: dict[str, Any]) -> bool:
        """Return True if a command should reach Home Assistant."""
        msg_type = message.get("type")
        msg_id = message.get("id")

        # Correlate first, unconditionally. An outbound frame whose id is not in
        # this map is dropped, so a command that is forwarded but never recorded
        # would have its reply silently swallowed and hang the client.
        if isinstance(msg_id, int) and isinstance(msg_type, str):
            self._pending[msg_id] = msg_type

        if msg_type == TYPE_AUTH:
            await self._on_auth(message)
            return True

        if msg_type == "supported_features":
            features = message.get("features") or {}
            if isinstance(features, dict) and features.get("coalesce_messages"):
                self._coalesced = True
            return True

        if self._full_access or not isinstance(msg_type, str):
            return True

        decision = self._proxy._decider.decide(
            self._permissions, KIND_WS, msg_type, message
        )
        if decision.allowed:
            return True

        self._proxy._record(self._user, KIND_WS, msg_type, decision)
        if isinstance(msg_id, int):
            self._pending.pop(msg_id, None)
        await self._deny(msg_id, decision)
        return False

    async def _on_auth(self, message: dict[str, Any]) -> None:
        """Learn who the connection belongs to from its auth frame."""
        token = message.get("access_token")
        if not isinstance(token, str):
            return
        refresh_token = self._proxy._hass.auth.async_validate_access_token(token)
        if refresh_token is None:
            return
        self._user = refresh_token.user
        self._permissions = self._proxy._evaluator.async_permissions(self._user)

    async def _deny(self, msg_id: Any, decision: Decision) -> None:
        """Answer a refused command in Home Assistant's own error shape.

        Home Assistant never sees the id, which is harmless: it rejects reuse of
        an id, not a gap in the sequence.
        """
        await self._client.send_str(
            json.dumps(
                {
                    "id": msg_id,
                    "type": TYPE_RESULT,
                    "success": False,
                    "error": {
                        "code": ERR_UNAUTHORIZED,
                        "message": decision.detail or "Unauthorized",
                    },
                }
            )
        )

    async def _on_server_text(self, raw: str) -> None:
        """Handle one outbound text frame, which may be a coalesced batch."""
        if self._full_access:
            await self._client.send_str(raw)
            return

        try:
            parsed = json_loads(raw)
        except ValueError:
            await self._client.send_str(raw)
            return

        messages = parsed if isinstance(parsed, list) else [parsed]
        kept: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if (filtered := self._filter_outbound(message)) is not None:
                kept.append(filtered)

        if not kept:
            return
        if isinstance(parsed, list):
            await self._client.send_str(json.dumps(kept))
        else:
            await self._client.send_str(json.dumps(kept[0]))

    def _filter_outbound(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Filter one server message, or return None to drop it."""
        msg_type = message.get("type")

        # Handshake frames carry no user data and must pass through untouched.
        if msg_type in (TYPE_AUTH_REQUIRED, TYPE_AUTH_OK, "auth_invalid", "pong"):
            return message

        msg_id = message.get("id")
        if not isinstance(msg_id, int):
            return message

        command = self._pending.get(msg_id)
        if command is None:
            # A frame that cannot be correlated cannot be filtered, and
            # forwarding it unfiltered is exactly the leak this exists to stop.
            _LOGGER.debug("Dropping websocket frame with uncorrelated id %s", msg_id)
            return None

        ctx = FilterContext(self._proxy._hass, self._permissions.check_entity)

        if msg_type == TYPE_RESULT:
            # A subscription keeps streaming, so its id stays correlated.
            if not command.startswith("subscribe_"):
                self._pending.pop(msg_id, None)
            if message.get("success") and "result" in message:
                return {
                    **message,
                    "result": REGISTRY.filter_result(
                        command, ctx, message["result"]
                    ),
                }
            return message

        if msg_type == TYPE_EVENT and "event" in message:
            filtered = REGISTRY.filter_event(command, ctx, message["event"])
            if filtered is None:
                return None
            return {**message, "event": filtered}

        return message
