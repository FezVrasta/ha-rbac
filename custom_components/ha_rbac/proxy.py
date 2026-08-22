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
import base64
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import aiohttp
from aiohttp import ClientTimeout, ClientWebSocketResponse, hdrs, web
from aiohttp.helpers import must_be_empty_body
from homeassistant.auth.models import User
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
    # Whatever the client claims about its own origin is discarded. Home
    # Assistant trusts these from a configured proxy, so relaying a
    # client-supplied value would let anyone spoof their source address past IP
    # banning and the trusted_networks auth provider. The proxy sets them itself
    # when it is trusted, and sends none at all when it is not.
    hdrs.X_FORWARDED_FOR,
    hdrs.X_FORWARDED_HOST,
    hdrs.X_FORWARDED_PROTO,
}
RESPONSE_HEADERS_FILTER = {
    hdrs.TRANSFER_ENCODING,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_ENCODING,
}

MAX_WEBSOCKET_MESSAGE_SIZE = 16 * 1024 * 1024
# Correlations outlive their result, so the map needs a ceiling.
MAX_PENDING_IDS = 8192
# Bodies above this are not filtered, and are refused rather than forwarded.
MAX_FILTERABLE_RESPONSE_SIZE = 16 * 1024 * 1024
DISABLED_TIMEOUT = ClientTimeout(total=None)

WS_PATH = "/api/websocket"

# Websocket message types from the auth handshake.
TYPE_AUTH = "auth"
TYPE_AUTH_REQUIRED = "auth_required"
TYPE_AUTH_OK = "auth_ok"
TYPE_RESULT = "result"
TYPE_EVENT = "event"

ERR_UNAUTHORIZED = "unauthorized"


# Home Assistant's signed-path parameter.
SIGN_QUERY_PARAM = "authSig"

# Anonymous requests are forwarded so the login flow and the static frontend
# work. These are the exception: they reach the API and act, with no user to
# check them against.
UNGOVERNED_API_PREFIXES = ("/api/webhook/",)


def _is_ungoverned_api_path(path: str) -> bool:
    """Return True if an unauthenticated request here would act ungoverned."""
    return path.startswith(UNGOVERNED_API_PREFIXES)


def _unverified_issuer(signature: str) -> str | None:
    """Return the refresh-token id a signed path claims, without verifying it.

    Verification is Home Assistant's job and happens upstream; this only needs
    to know whose permissions to apply, and a forged claim resolves to a token
    whose signature check then fails there.
    """
    try:
        payload = signature.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json_loads(base64.urlsafe_b64decode(padded))
    except (ValueError, IndexError, TypeError):
        return None
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    return issuer if isinstance(issuer, str) else None


def _carries_entity_data(content_type: str) -> bool:
    """Return True if a response could disclose entity state.

    Images, video and static assets cannot; anything textual might. Used only to
    decide whether an unfilterable response must be refused.
    """
    # Media cannot disclose an entity's state, and a stream cannot be buffered
    # to filter in any case. A camera feed the role is permitted to see must not
    # be refused for being unfilterable.
    if content_type.startswith(("image/", "video/", "audio/", "font/", "multipart/")):
        return False
    return content_type not in (
        "application/octet-stream",
        "text/css",
        "text/javascript",
        "application/javascript",
    )


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
            headers[hdrs.X_FORWARDED_FOR] = request.remote or ""
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
            if refresh_token := self._hass.auth.async_validate_access_token(token):
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
        if user is None:
            # Anonymous traffic is the login flow and the static frontend, which
            # must pass. Anything that reaches Home Assistant's own API without
            # identifying a user cannot be reasoned about, so it is refused
            # rather than forwarded ungoverned -- `/api/webhook/{id}` is the
            # live example, and the mobile app's webhook can call any service.
            if _is_ungoverned_api_path(request.path):
                _LOGGER.warning(
                    "Refusing unauthenticated %s %s: no user to check it against",
                    request.method,
                    request.path,
                )
                return web.json_response({"message": "Unauthorized"}, status=401)
        elif not permissions.full_access:
            body = await self._peek_json(request)
            name = f"{request.method} {request.path}"
            decision = self._decider.decide(
                permissions, KIND_HTTP, name, body, query=request.query
            )
            if not decision.allowed:
                self._record(user, KIND_HTTP, name, decision)
                return web.json_response({"message": "Unauthorized"}, status=401)

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

            if decision.filter_response:
                filtered, filterable = await self._filter_http_body(
                    result, permissions, request, content_type
                )
                if filterable:
                    return web.json_response(
                        filtered, status=result.status, headers=headers
                    )
                # A request that named its resources has already been checked
                # against them by the resource gate, so an unfilterable response
                # to it discloses nothing new. Refusal is for the unbounded case,
                # where the response was the only thing left to check.
                if not decision.resources and _carries_entity_data(content_type):
                    # A size limit is a performance guard, not a correctness
                    # boundary: streaming here would hand a restricted user every
                    # entity in a large response, silently.
                    _LOGGER.warning(
                        "Refusing %s %s: response could not be filtered "
                        "(content-type %s, length %s)",
                        request.method,
                        request.path,
                        content_type,
                        result.headers.get(hdrs.CONTENT_LENGTH, "unknown"),
                    )
                    return web.json_response(
                        {"message": "Response too large to filter"}, status=403
                    )

            # Anything else -- images, streams, static assets -- carries no
            # entity data of its own, and the state that would reveal it has
            # already been filtered.
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
        self,
        result: aiohttp.ClientResponse,
        permissions: Any,
        request: web.Request,
        content_type: str,
    ) -> tuple[Any, bool]:
        """Filter a JSON body.

        Returns the filtered payload and whether filtering was possible at all,
        so the caller can refuse rather than fall back to streaming.
        """
        if content_type != "application/json":
            return None, False

        length = result.headers.get(hdrs.CONTENT_LENGTH)
        if length is not None and int(length) > MAX_FILTERABLE_RESPONSE_SIZE:
            return None, False

        raw = await result.read()
        if not raw:
            return None, False
        if len(raw) > MAX_FILTERABLE_RESPONSE_SIZE:
            return None, False
        try:
            payload = json_loads(raw)
        except ValueError:
            return None, False

        ctx = FilterContext(self._hass, permissions.check_entity)
        return (
            REGISTRY.filter_result(f"{request.method} {request.path}", ctx, payload),
            True,
        )

    @callback
    def _record(
        self, user: User | None, kind: str, name: str, decision: Decision
    ) -> None:
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
                session = _WsSession(
                    self._hass,
                    self._evaluator,
                    self._decider,
                    self._record,
                    client_ws,
                    server_ws,
                )
                pumps = [
                    create_eager_task(session.pump_inbound()),
                    create_eager_task(session.pump_outbound()),
                ]
                try:
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    # The surviving pump holds a reference to the session and
                    # would otherwise dangle for the life of the process.
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    session.close()
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
        hass: HomeAssistant,
        evaluator: Evaluator,
        decider: Decider,
        record: "Callable[[User | None, str, str, Decision], None]",
        client_ws: web.WebSocketResponse,
        server_ws: ClientWebSocketResponse,
    ) -> None:
        """Initialise the session."""
        self._hass = hass
        self._evaluator = evaluator
        self._decider = decider
        self._record = record
        self._client = client_ws
        self._server = server_ws
        self._user: User | None = None
        self._permissions = evaluator.async_permissions(None)
        # Correlates a result or event back to the command that asked for it.
        # Without it an outbound frame cannot be filtered, so an unknown id is
        # dropped rather than forwarded.
        # Correlations for one-shot commands, bounded because a long-lived
        # session issues a lot of them.
        self._pending: OrderedDict[int, str] = OrderedDict()
        # Ids that have streamed at least one event are subscriptions. They are
        # kept out of the bounded map: they are few, they live for the whole
        # connection, and evicting one silently stops the UI updating.
        self._streaming: dict[int, str] = {}
        # Home Assistant requires ids to increase strictly, so the highest one
        # seen is all that is needed to reject a repeat. Checking membership of
        # the bounded map instead let an attacker evict the entry first and then
        # reuse the id to re-label which filter applied.
        self._highest_id = 0
        self._coalesced = False
        self._unsubscribe_revoke: Any = None

    @callback
    def _remember(self, msg_id: int, msg_type: str) -> None:
        """Record an id correlation, discarding the oldest when full."""
        self._pending[msg_id] = msg_type
        while len(self._pending) > MAX_PENDING_IDS:
            self._pending.popitem(last=False)

    @callback
    def _correlate(self, msg_id: int) -> str | None:
        """Return the command an id belongs to."""
        return self._streaming.get(msg_id) or self._pending.get(msg_id)

    @callback
    def close(self) -> None:
        """Release anything registered for the lifetime of the connection."""
        if self._unsubscribe_revoke is not None:
            self._unsubscribe_revoke()
            self._unsubscribe_revoke = None

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

        # Correlate first. An outbound frame whose id is not in this map is
        # dropped, so a forwarded command that was never recorded would have its
        # reply swallowed and hang the client.
        #
        # Never *re*-label an id, though. Home Assistant requires ids to
        # increase strictly and rejects a repeat outright, so a second use is
        # always an attack: sending [{"id":5,"get_states"},{"id":5,"get_config"}]
        # in one coalesced frame would relabel the pending id, and the
        # get_states result would then be matched to get_config's pass-through
        # filter and forwarded in full.
        if isinstance(msg_id, int) and isinstance(msg_type, str):
            if msg_id in self._pending:
                await self._deny(
                    msg_id,
                    Decision(
                        allowed=False,
                        reason="id_reuse",
                        detail="Message id reused on this connection",
                    ),
                )
                return False
            self._remember(msg_id, msg_type)

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

        decision = self._decider.decide(self._permissions, KIND_WS, msg_type, message)
        if decision.allowed:
            return True

        self._record(self._user, KIND_WS, msg_type, decision)
        if isinstance(msg_id, int):
            self._pending.pop(msg_id, None)
        await self._deny(msg_id, decision)
        return False

    async def _on_auth(self, message: dict[str, Any]) -> None:
        """Learn who the connection belongs to from its auth frame."""
        # A connection authenticates once. A second frame would re-point the
        # permissions mid-stream while Home Assistant carries on as the first
        # user, and would leak the earlier revoke-callback registration.
        if self._user is not None:
            return
        token = message.get("access_token")
        if not isinstance(token, str):
            return
        refresh_token = self._hass.auth.async_validate_access_token(token)
        if refresh_token is None:
            return
        self._user = refresh_token.user
        self._permissions = self._evaluator.async_permissions(self._user)
        # A revoked token must not leave a filtered connection alive, since the
        # permissions were resolved once at authentication time.
        self._unsubscribe_revoke = self._hass.auth.async_register_revoke_token_callback(
            refresh_token.id, self._on_token_revoked
        )

    @callback
    def _on_token_revoked(self) -> None:
        """Tear the connection down when its refresh token is revoked."""
        self._hass.async_create_task(self._client.close())

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

        command = self._correlate(msg_id)
        if command is None:
            # A frame that cannot be correlated cannot be filtered, and
            # forwarding it unfiltered is exactly the leak this exists to stop.
            _LOGGER.debug("Dropping websocket frame with uncorrelated id %s", msg_id)
            return None

        ctx = FilterContext(self._hass, self._permissions.check_entity)

        if msg_type == TYPE_RESULT:
            # The correlation is deliberately kept after the result. Many
            # subscriptions are not spelled `subscribe_*` -- history/stream,
            # logbook/event_stream and weather/subscribe_forecast among them --
            # and dropping the entry by name meant their event frames arrived
            # uncorrelated and were discarded, so history graphs never updated.
            # The map is bounded instead, in _remember.
            if message.get("success") and "result" in message:
                return {
                    **message,
                    "result": REGISTRY.filter_result(command, ctx, message["result"]),
                }
            return message

        if msg_type == TYPE_EVENT and "event" in message:
            # First event proves this id is a subscription; move it somewhere it
            # cannot be evicted, or the UI would quietly stop updating.
            self._streaming.setdefault(msg_id, command)
            filtered = REGISTRY.filter_event(command, ctx, message["event"])
            if filtered is None:
                return None
            return {**message, "event": filtered}

        return message
