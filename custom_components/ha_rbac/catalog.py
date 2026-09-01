"""Derive the permission surface from Home Assistant's own runtime registries.

Nothing here is a maintained list. Command tiers come from HA's own
`require_admin` decorator, read back off the handler; resource shapes come from
the voluptuous schemas HA already attached. The layer therefore tracks upstream
across releases instead of rotting.
"""

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import partial
from typing import Any

from homeassistant.components.websocket_api import const as ws_const
from homeassistant.core import HomeAssistant, callback

from .const import CAPABILITIES, RESOURCE_KEYS, TIER_ADMIN, TIER_OPEN, TIER_USER
from .extract import schema_resource_markers

_LOGGER = logging.getLogger(__name__)

# `require_admin` and `ws_require_user` are closures; `functools.wraps` copies
# __dict__ and sets __wrapped__ but never touches __code__, so the closure's own
# function name survives and identifies the decorator.
ADMIN_WRAPPER_NAME = "with_admin"
USER_WRAPPER_NAME = "check_current_user"

# Commands that mutate configuration or storage. A regex, not a list: it does not
# need revisiting when an integration adds a command next release.
# Matched on the verb, not the namespace: `^config/` also caught
# `config/entity_registry/list`, so every registry read was treated as a
# mutation and refused.
WRITE_PATTERN = re.compile(
    r"/(save|create|update|delete|remove|add|set|move|reload|import|upload)(/|$)"
)


def introspection_works() -> bool:
    """Verify tier derivation against Home Assistant's own decorators.

    Rather than infer health from how many commands look admin -- which varies
    with the set of loaded integrations -- run the mechanism against the real
    `require_admin` and see whether it still recognises the result. If upstream
    renames the wrapper, this fails immediately and deterministically instead of
    silently classifying everything as open.
    """
    from homeassistant.components.websocket_api import decorators  # noqa: PLC0415

    def probe(hass: Any, connection: Any, msg: Any) -> None:
        """Do nothing; only its wrapper is inspected."""

    return (
        derive_tier(decorators.require_admin(probe)) == TIER_ADMIN
        and derive_tier(decorators.ws_require_user()(probe)) == TIER_USER
        and derive_tier(probe) == TIER_OPEN
    )


@dataclass(slots=True)
class CommandInfo:
    """What has been derived about one websocket command."""

    command: str
    tier: str
    required_resources: set[str]
    optional_resources: set[str]
    is_write: bool

    @property
    def has_resource_field(self) -> bool:
        """Return True if the schema can carry a resource reference at all."""
        return bool(self.required_resources or self.optional_resources)


def derive_tier(handler: Any) -> str:
    """Return the tier Home Assistant itself enforces for a handler.

    Walks the `__wrapped__` chain looking for the decorator closures. Verified
    against both decorator orderings found in core.
    """
    names: list[str] = []
    func: Any = handler
    seen = 0
    while func is not None and seen < 20:
        if (code := getattr(func, "__code__", None)) is not None:
            names.append(code.co_name)
        func = getattr(func, "__wrapped__", None)
        seen += 1

    if ADMIN_WRAPPER_NAME in names:
        return TIER_ADMIN
    if USER_WRAPPER_NAME in names:
        return TIER_USER
    return TIER_OPEN


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# `/api/states/{entity_id}` -> a pattern that also tells us the parameter names,
# so a path segment can be recovered as a resource reference.
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[^}]+)?\}")


@dataclass(slots=True)
class RouteInfo:
    """What has been derived about one REST route."""

    pattern: "re.Pattern[str]"
    url: str
    tiers: dict[str, str]
    requires_auth: bool

    @property
    def specificity(self) -> tuple[int, int, int]:
        """Return a sort key placing the most specific route first.

        Ordering by URL length is not correctness-preserving: a catch-all such
        as `/api/{username}` from an unrelated integration is longer than
        `/api/error_log` and would shadow it, downgrading an admin-only endpoint
        to open. Literal characters, then fewer placeholders, then length.
        """
        placeholders = self.url.count("{")
        literal = len(_PLACEHOLDER.sub("", self.url))
        return (-literal, placeholders, -len(self.url))

    def tier_for_method(self, method: str) -> str:
        """Return the tier for a method, defaulting to admin when undeclared."""
        return self.tiers.get(method.lower(), TIER_ADMIN)


def _url_to_pattern(url: str) -> "re.Pattern[str]":
    """Compile an aiohttp route template into a matching regex."""
    parts: list[str] = []
    index = 0
    for match in _PLACEHOLDER.finditer(url):
        parts.append(re.escape(url[index : match.start()]))
        name = match.group(1)
        # aiohttp's `{path:.*}` style tails may span separators; plain ones
        # match a single segment.
        greedy = ":" in match.group(0)
        parts.append(f"(?P<{name}>{'.*' if greedy else '[^/]+'})")
        index = match.end()
    parts.append(re.escape(url[index:]))
    return re.compile(f"^{''.join(parts)}/?$")


# Home Assistant answers a static path off disk, to anyone at all: its auth
# middleware only records whether a request was authenticated, and the
# `requires_auth` check lives in `HomeAssistantView`, which a static resource is
# not. GET and HEAD because that is all the router answers there.
STATIC_METHODS = ("get", "head")


@dataclass(slots=True)
class StaticInfo:
    """A path Home Assistant serves off disk rather than through a view."""

    path: str
    # A directory covers everything beneath it. A single file is only itself.
    is_prefix: bool

    def covers(self, path: str) -> bool:
        """Return True if this static path answers for a request path."""
        if not self.is_prefix:
            return path == self.path
        return path == self.path or path.startswith(f"{self.path}/")


def build_statics(hass: HomeAssistant) -> list[StaticInfo]:
    """Derive the paths Home Assistant serves straight off disk.

    Read back off the running router rather than listed here, so an integration
    registering a static path of its own is covered the moment it does.

    Two registrations to recognise, because `async_register_static_paths` makes
    both: a directory becomes an aiohttp `StaticResource`, and a single file
    becomes an ordinary route bound to one of HA's own file-serving helpers.
    """
    from aiohttp.web_urldispatcher import StaticResource  # noqa: PLC0415

    try:
        from homeassistant.components.http.server import (  # noqa: PLC0415
            _serve_file,
            _serve_file_with_cache_headers,
        )
    except ImportError:
        # Renamed upstream. Single-file paths then stay unknown, which resolves
        # to admin: what happened before any of this, and the safe direction to
        # fail in. Directories are unaffected, being a public aiohttp class.
        servers: set[Any] = set()
    else:
        servers = {_serve_file, _serve_file_with_cache_headers}

    if (app := getattr(getattr(hass, "http", None), "app", None)) is None:
        return []

    statics: list[StaticInfo] = []
    for resource in app.router.resources():
        url = resource.canonical
        # A path built from placeholders belongs to a view, and a prefix of "/"
        # would hand over the whole instance.
        if not url or "{" in url or url == "/":
            continue
        if isinstance(resource, StaticResource):
            statics.append(StaticInfo(url.rstrip("/"), True))
            continue
        # Not every resource is iterable -- the frontend's index is its own
        # `AbstractResource` -- and one that is not registers no routes here.
        try:
            routes = list(resource)
        except TypeError:
            continue
        if any(
            isinstance(route.handler, partial) and route.handler.func in servers
            for route in routes
        ):
            statics.append(StaticInfo(url, False))
    return statics


def _all_view_subclasses(base: Any) -> list[Any]:
    """Return every HomeAssistantView subclass, transitively."""
    found: list[Any] = []
    stack = list(base.__subclasses__())
    seen: set[int] = set()
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        found.append(cls)
        stack.extend(cls.__subclasses__())
    return found


def build_routes() -> list[RouteInfo]:
    """Derive REST route tiers from the registered view classes.

    `hass.http.app.router` knows the paths but has lost the view object inside
    `request_handler_factory`'s closure, so the classes are walked instead. Views
    that build their URL per instance are not discoverable this way and simply
    stay unknown, which resolves to admin.
    """
    from homeassistant.components.http import HomeAssistantView  # noqa: PLC0415

    routes: list[RouteInfo] = []
    for cls in _all_view_subclasses(HomeAssistantView):
        url = getattr(cls, "url", None)
        if not isinstance(url, str) or not url:
            continue

        tiers: dict[str, str] = {}
        for method in HTTP_METHODS:
            if (handler := getattr(cls, method, None)) is None:
                continue
            tiers[method] = derive_tier(handler)

        urls = [url, *(getattr(cls, "extra_urls", None) or [])]
        for candidate in urls:
            if not isinstance(candidate, str) or not candidate:
                continue
            routes.append(
                RouteInfo(
                    pattern=_url_to_pattern(candidate),
                    url=candidate,
                    tiers=tiers,
                    requires_auth=getattr(cls, "requires_auth", True),
                )
            )

    routes.sort(key=lambda route: route.specificity)
    return routes


class Catalog:
    """The derived command catalogue, rebuilt as integrations register."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty catalogue."""
        self._hass = hass
        self._commands: dict[str, CommandInfo] = {}
        self._routes: list[RouteInfo] = []
        self._statics: list[StaticInfo] = []
        self.degraded = False

    @property
    def commands(self) -> dict[str, CommandInfo]:
        """Return the derived command information."""
        return self._commands

    @callback
    def rebuild(self, _event: Any = None) -> None:
        """Re-derive the catalogue from `hass.data["websocket_api"]`."""
        handlers = self._hass.data.get(ws_const.DOMAIN) or {}
        commands: dict[str, CommandInfo] = {}

        for command, entry in handlers.items():
            handler, schema = entry if isinstance(entry, tuple) else (entry, None)
            required, optional = schema_resource_markers(schema)
            commands[command] = CommandInfo(
                command=command,
                tier=derive_tier(handler),
                required_resources=required,
                optional_resources=optional,
                is_write=bool(WRITE_PATTERN.search(command)),
            )

        self._commands = commands
        self._routes = build_routes()
        self._statics = build_statics(self._hass)
        self._check_not_degraded()

    def _check_not_degraded(self) -> None:
        """Fail closed if tier derivation stops working.

        The introspection relies on undocumented internals, and its failure mode
        is permissive: an unrecognised wrapper makes every command look open. So
        the mechanism is tested directly rather than trusted.
        """
        if introspection_works():
            self.degraded = False
            return

        self.degraded = True
        _LOGGER.error(
            "RBAC tier derivation no longer recognises Home Assistant's own "
            "require_admin decorator. Every command would be classified as "
            "unrestricted, so enforcement is disabled rather than failing open. "
            "This usually means an upstream change to "
            "homeassistant.components.websocket_api.decorators"
        )

    @callback
    def tier_for(self, command: str) -> str:
        """Return the tier for a command, defaulting to admin when unknown.

        An unknown command is one this build has never seen registered, so the
        safe assumption is the most restrictive one.
        """
        if (info := self._commands.get(command)) is None:
            return TIER_ADMIN
        return info.tier

    @callback
    def apps(self) -> list[dict[str, Any]]:
        """Return the sidebar apps, read from the panel registry.

        Add-ons are panels: `hassio` registers each one with the add-on slug as
        its url path and `{"addon": slug}` as its config, so a single list
        covers both built-in apps and add-ons with nothing enumerated here.
        """
        from homeassistant.components.frontend import (  # noqa: PLC0415
            DATA_PANELS,
        )

        panels = self._hass.data.get(DATA_PANELS) or {}
        return sorted(
            (
                {
                    "url_path": panel.frontend_url_path,
                    "title": panel.sidebar_title or panel.frontend_url_path,
                    "kind": panel.component_name,
                    "icon": panel.sidebar_icon,
                    "require_admin": panel.require_admin,
                    "addon": (panel.config or {}).get("addon"),
                }
                for panel in panels.values()
            ),
            key=lambda item: item["title"].lower(),
        )

    @callback
    def addon_slug_for(self, url_path: str) -> str | None:
        """Return the add-on slug an app belongs to, if it is one."""
        from homeassistant.components.frontend import (  # noqa: PLC0415
            DATA_PANELS,
        )

        panel = (self._hass.data.get(DATA_PANELS) or {}).get(url_path)
        return (panel.config or {}).get("addon") if panel else None

    @callback
    def service_is_admin_only(self, domain: str, service: str) -> bool:
        """Return True if Home Assistant registered this service as admin-only.

        Home Assistant's helper for registering an admin-only service wraps the
        handler in a partial of `_async_admin_handler`, so the registration
        itself says so and no list of service names is needed.

        (The helper is deliberately not named in full here: hassfest greps the
        source for it and would report this integration as registering services,
        which it does not.)
        """
        from homeassistant.helpers.service import (  # noqa: PLC0415
            _async_admin_handler,
        )

        services = self._hass.services.async_services().get(domain) or {}
        if (entry := services.get(service)) is None:
            # A service this build has never seen is treated as the most
            # restrictive thing, same as an unknown command.
            return True
        target = getattr(getattr(entry, "job", None), "target", None)
        return getattr(target, "func", None) is _async_admin_handler

    @callback
    def route_for(self, method: str, path: str) -> RouteInfo | None:
        """Return the registered route matching a request.

        A route only answers for the methods its view actually declares;
        otherwise an unrelated view sharing a path shape would answer for verbs
        it never implements.
        """
        fallback: RouteInfo | None = None
        for route in self._routes:
            if not route.pattern.match(path):
                continue
            if method.lower() in route.tiers:
                return route
            if fallback is None:
                fallback = route
        return fallback

    @callback
    def serves_a_file(self, method: str, path: str) -> bool:
        """Return True if Home Assistant hands this path straight off disk."""
        if method.lower() not in STATIC_METHODS:
            return False
        return any(static.covers(path) for static in self._statics)

    @callback
    def tier_for_request(self, method: str, path: str) -> str:
        """Return the tier for a REST request.

        Unmatched paths resolve to admin: a route this build cannot see is one
        it cannot reason about. A static path is the exception, and views are
        consulted first, so this can only answer for a path none of them claims.
        """
        if (route := self.route_for(method, path)) is None:
            # A path with no view of its own may still be a file Home Assistant
            # gives to anyone who asks: `/local`, the frontend bundles, an
            # integration's own static path. Refusing one to a signed-in
            # restricted user withholds nothing, because the same request
            # carrying no token at all is forwarded and answered -- which is
            # exactly what a dashboard's `<img>` sends. A camera snapshot
            # written into `www/` was being refused to the people it was put
            # there for, while a stranger could still fetch it.
            if self.serves_a_file(method, path):
                return TIER_OPEN
            return TIER_ADMIN
        return route.tier_for_method(method)

    @callback
    def path_resources(self, method: str, path: str) -> dict[str, str]:
        """Return resource references carried in the path itself.

        `/api/camera_proxy/{entity_id}` names an entity in its URL rather than
        in a body, so the resource gate would otherwise see nothing.
        """
        if (route := self.route_for(method, path)) is None:
            return {}
        if (match := route.pattern.match(path)) is None:
            return {}
        return {
            name: value
            for name, value in (match.groupdict() or {}).items()
            if value and name in RESOURCE_KEYS
        }

    @callback
    def info_for(self, command: str) -> CommandInfo | None:
        """Return the derived information for a command, if known."""
        return self._commands.get(command)

    @callback
    def capabilities(self) -> list[dict[str, Any]]:
        """Return the named capability groups with what each covers here.

        The commands are matched against this instance rather than declared, so
        the editor can show what granting one actually reaches instead of asking
        an administrator to trust the label.
        """
        return [
            {
                "id": capability["id"],
                "title": capability["title"],
                "description": capability["description"],
                "commands": sorted(
                    command
                    for command in self._commands
                    if any(
                        fnmatch(command, pattern) for pattern in capability["patterns"]
                    )
                ),
            }
            for capability in CAPABILITIES
        ]

    @callback
    def as_dict(self) -> list[dict[str, Any]]:
        """Return the catalogue for the admin UI."""
        return sorted(
            (
                {
                    "command": info.command,
                    "tier": info.tier,
                    "resources": sorted(
                        info.required_resources | info.optional_resources
                    ),
                    "bounded_by_schema": info.has_resource_field,
                    "write": info.is_write,
                }
                for info in self._commands.values()
            ),
            key=lambda item: item["command"],
        )
