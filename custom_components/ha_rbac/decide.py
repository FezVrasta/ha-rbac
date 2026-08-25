"""The decision procedure.

Four gates, in order of cost: a pass-through fast path, the tier gate derived
from Home Assistant's own decorators, a resource check, and the boundedness rule.

The shape of the whole thing follows from one observation: reads and mutations
fail differently. A read leaks through its *response*, so it can be allowed and
its response filtered -- a command returning nothing resource-shaped needs no
classification at all. A mutation's damage is not visible in the response, so it
has to be judged up front.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)

from .catalog import Catalog
from .const import MAX_WALK_DEPTH, RESOURCE_KEYS, TIER_ADMIN
from .extract import Extracted, extract, is_bounded
from .filters import FilterRegistry
from .policy import Permissions

KIND_WS = "ws"
KIND_HTTP = "http"

# Home Assistant spells some resources differently in query strings.
QUERY_RESOURCE_ALIASES = {
    "filter_entity_id": "entity_id",
    "entity": "entity_id",
}

# Keys that mean "invoke something", wherever they appear in a payload.
SERVICE_KEYS = frozenset({"service", "action"})

# How a Home Assistant template reaches an entity's attributes.
ATTRIBUTE_TEMPLATE_MARKERS = ("state_attr", ".attributes", "attributes[")

REASON_TIER = "tier"
REASON_RESOURCE = "resource"
REASON_UNBOUNDED = "unbounded"
REASON_DEGRADED = "degraded"
REASON_APP = "app"

# Panels registered by Lovelace: every dashboard, sharing one set of commands.
DASHBOARD_KIND = "lovelace"

# Never a real entity; it makes a domain-level policy rule answer for a call
# that names no particular entity.
DOMAIN_PROBE = "_rbac_probe"


def _reads_attributes(node: Any, depth: int = 0) -> bool:
    """Return True if a payload contains a template that reads attributes.

    A rendered template reports the entities it read but not the attributes, so
    there is nothing in the response to check. These markers are how Home
    Assistant templates reach an attribute.
    """
    if depth > MAX_WALK_DEPTH:
        return True
    if isinstance(node, str):
        return any(marker in node for marker in ATTRIBUTE_TEMPLATE_MARKERS)
    if isinstance(node, dict):
        return any(_reads_attributes(value, depth + 1) for value in node.values())
    if isinstance(node, list):
        return any(_reads_attributes(item, depth + 1) for item in node)
    return False


def _invokes_a_service(node: Any, depth: int = 0) -> bool:
    """Return True if a payload calls a service anywhere inside it.

    Home Assistant spells a call as `service` or, more recently, `action`, and
    `execute_script` buries it inside a sequence -- so the whole payload is
    searched rather than just the top level.

    The value has to look like a service though. Lovelace writes
    `{"action": "toggle"}` for a tap action, and treating that as a service call
    would deny reads of any dashboard containing a button.
    """
    if depth > MAX_WALK_DEPTH:
        # A payload too deep to inspect is assumed to act, not to observe.
        return True
    if isinstance(node, dict):
        for key in SERVICE_KEYS:
            value = node.get(key)
            # `light.turn_on`, or `turn_on` alongside an explicit domain.
            if isinstance(value, str) and ("." in value or "domain" in node):
                return True
        return any(_invokes_a_service(value, depth + 1) for value in node.values())
    if isinstance(node, list):
        return any(_invokes_a_service(item, depth + 1) for item in node)
    return False


@dataclass(slots=True)
class Decision:
    """The verdict on one request, and why."""

    allowed: bool
    reason: str = ""
    detail: str = ""
    # Entities the request named, for the deny log and for response filtering.
    resources: list[str] = None  # type: ignore[assignment]
    filter_response: bool = False

    def __post_init__(self) -> None:
        """Default the resource list."""
        if self.resources is None:
            self.resources = []


@callback
def expand_to_entities(hass: HomeAssistant, found: Extracted) -> set[str]:
    """Resolve every referenced resource to concrete entity ids.

    Ids that do not resolve in the matching registry are dropped rather than
    treated as denied resources. A `device_id` in a Z-Wave payload is a Z-Wave
    node id, not a Home Assistant device -- guessing otherwise would deny
    unrelated commands. Those commands are covered by the tier gate instead.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    entities = set(found.entities)

    for device_id in found.devices:
        if dev_reg.async_get(device_id) is None:
            continue
        entities.update(
            entry.entity_id
            for entry in er.async_entries_for_device(
                ent_reg, device_id, include_disabled_entities=True
            )
        )

    areas = set(found.areas)
    for floor_id in found.floors:
        areas.update(area.id for area in ar.async_entries_for_floor(area_reg, floor_id))

    for area_id in areas:
        if area_reg.async_get_area(area_id) is None:
            continue
        entities.update(
            entry.entity_id for entry in er.async_entries_for_area(ent_reg, area_id)
        )
        for device in dr.async_entries_for_area(dev_reg, area_id):
            entities.update(
                entry.entity_id
                for entry in er.async_entries_for_device(
                    ent_reg, device.id, include_disabled_entities=True
                )
                if entry.area_id is None
            )

    for label_id in found.labels:
        entities.update(
            entry.entity_id for entry in er.async_entries_for_label(ent_reg, label_id)
        )
        for device in dr.async_entries_for_label(dev_reg, label_id):
            entities.update(
                entry.entity_id
                for entry in er.async_entries_for_device(
                    ent_reg, device.id, include_disabled_entities=True
                )
            )

    return entities


class Decider:
    """Applies the gates to inbound requests."""

    def __init__(
        self, hass: HomeAssistant, catalog: Catalog, filter_registry: FilterRegistry
    ) -> None:
        """Initialise the decider."""
        self._hass = hass
        self._catalog = catalog
        self._filters = filter_registry

    @callback
    def decide(
        self,
        permissions: Permissions,
        kind: str,
        name: str,
        payload: dict[str, Any],
        query: "Mapping[str, str] | None" = None,
    ) -> Decision:
        """Return the verdict for one request."""
        # 1. Pass-through. Owner, system users and full-access roles skip every
        #    parse, which is what keeps the proxy cheap for administrators.
        if permissions.full_access:
            return Decision(allowed=True)

        # If tier derivation has stopped working, every command would look
        # unrestricted. Refuse rather than fail open.
        if self._catalog.degraded:
            return Decision(
                allowed=False,
                reason=REASON_DEGRADED,
                detail="permission derivation is not working on this HA version",
            )

        # 2. Tier gate. Covers everything Home Assistant already marks
        #    require_admin without naming a single command. REST routes are
        #    derived separately -- looking a path up in the websocket catalogue
        #    would make every request unknown, and therefore admin.
        method, path = self._split_http(name) if kind == KIND_HTTP else ("", "")
        tier = (
            self._catalog.tier_for_request(method, path)
            if kind == KIND_HTTP
            else self._catalog.tier_for(name)
        )
        if not permissions.tier_allowed(name, tier):
            return Decision(
                allowed=False,
                reason=REASON_TIER,
                detail=f"role does not permit {tier}-tier request {name!r}",
            )

        # 2b. App gate. Hiding an app from the sidebar is cosmetic on its own --
        #     the address bar still works -- so the ways into a denied one are
        #     refused as well.
        if (
            app_decision := self._decide_app(permissions, kind, name, payload)
        ) is not None:
            return app_decision

        # 2c. A template reports which entities it read, but not which
        #     attributes -- so a role that withholds any cannot let one through.
        if permissions.hides_attributes and _reads_attributes(payload):
            return Decision(
                allowed=False,
                reason=REASON_UNBOUNDED,
                detail=(
                    "a template that reads attributes cannot be checked against "
                    "a role that withholds them"
                ),
            )

        found = extract(payload)
        if kind == KIND_HTTP:
            # A path parameter is a resource reference the body never carries.
            for key, value in self._catalog.path_resources(method, path).items():
                self._merge_named_resource(found, key, value)
            # So is a query parameter. `?filter_entity_id=lock.front` names its
            # own target, and with `minimal_response` the entity id appears only
            # on the first history sample, so the response filter cannot recover
            # what the request gave away.
            for key, value in (query or {}).items():
                self._merge_query_resource(found, key, value)
        entities = expand_to_entities(self._hass, found)
        key = POLICY_CONTROL if self._is_mutation(kind, name, payload) else POLICY_READ

        # 3. Resource gate. Every entity the request names must be permitted.
        denied = sorted(
            entity_id
            for entity_id in entities
            if not permissions.check_entity(entity_id, key)
        )
        if denied:
            return Decision(
                allowed=False,
                reason=REASON_RESOURCE,
                detail=f"no {key} access to {', '.join(denied[:5])}",
                resources=denied,
            )

        # 4. Boundedness. A payload that names nothing, or that carries a
        #    template, does not constrain its own command.
        if not is_bounded(found):
            # A service call that names no entity is not unbounded, it is bound
            # by the service. `persistent_notification.create`, `notify.*` and
            # `homeassistant.restart` all target nothing, and refusing the lot
            # left a role unable to make a notification.
            if (
                service_decision := self._decide_service(permissions, payload)
            ) is not None:
                return service_decision

            if self._is_mutation(kind, name, payload):
                return Decision(
                    allowed=False,
                    reason=REASON_UNBOUNDED,
                    detail=f"{name!r} mutates without naming what it affects",
                )
            if found.templated and not self._filters.has(name):
                # A template reaches past whatever its request names, so it can
                # only be allowed where the response says what it actually read.
                # `render_template` reports that in every result it streams; a
                # template smuggled into another command's payload does not, and
                # stays refused.
                return Decision(
                    allowed=False,
                    reason=REASON_UNBOUNDED,
                    detail=(
                        f"{name!r} carries a template, whose reach is not limited "
                        "to the entities named"
                    ),
                )
            # Otherwise it is a read that named nothing, which is the ordinary
            # case: `get_panels`, `person/list`, `energy/info` and most of a
            # frontend's boot sequence. Allow it and filter the response --
            # leakage from a read is in the response by definition, and a
            # payload carrying nothing resource-shaped has nothing to leak.
            #
            # Requiring an explicit response filter here instead was tried, to
            # catch commands like `conversation/process` that act on free text.
            # It denied 17 of the 27 commands a real frontend issues on load, so
            # those few are named in the predefined roles' tier denials, where an
            # administrator can see and change them.

        return Decision(allowed=True, resources=sorted(entities), filter_response=True)

    @callback
    def _decide_app(
        self,
        permissions: Permissions,
        kind: str,
        name: str,
        payload: dict[str, Any],
    ) -> Decision | None:
        """Refuse a request that reaches into an app the role cannot see.

        Hiding an app from the sidebar is cosmetic on its own -- the address bar
        still works -- so the routes behind it are refused too. Three ways in,
        in order of precision:

        * the request names the app outright, which is how dashboards work:
          `lovelace/config` carries the dashboard's own `url_path`;
        * an add-on is reached through the Supervisor API, which names its slug;
        * anything else, by the convention that an app's data comes from
          commands sharing its name.
        """
        denied = [
            app
            for app in self._catalog.apps()
            if not permissions.app_allowed(app["url_path"])
        ]
        if not denied:
            return None

        named = payload.get("url_path")
        for app in denied:
            url_path = app["url_path"]

            if isinstance(named, str) and named == url_path:
                return Decision(
                    allowed=False,
                    reason=REASON_APP,
                    detail=f"no access to {app['title']}",
                )

            if slug := app.get("addon"):
                endpoint = payload.get("endpoint")
                if isinstance(endpoint, str) and f"/{slug}" in endpoint:
                    return Decision(
                        allowed=False,
                        reason=REASON_APP,
                        detail=f"no access to the {app['title']} add-on",
                    )
                continue

            # Dashboards all share the `lovelace/` commands, so the prefix rule
            # would take every dashboard down with one of them. They are covered
            # by the `url_path` check above instead.
            if app.get("kind") == DASHBOARD_KIND:
                continue

            prefix = url_path.replace("-", "_")
            if kind != KIND_HTTP and name.startswith(f"{prefix}/"):
                return Decision(
                    allowed=False,
                    reason=REASON_APP,
                    detail=f"no access to {app['title']}",
                )
        return None

    @callback
    def _decide_service(
        self, permissions: Permissions, payload: dict[str, Any]
    ) -> Decision | None:
        """Judge a service call that named no entity, or None if not one.

        The service itself is the bound. Home Assistant already records which
        services it considers administrative, so that is read rather than
        listed; anything else is allowed to a role that may control the domain.
        """
        domain = payload.get("domain")
        service = payload.get("service")
        if not isinstance(domain, str) or not isinstance(service, str):
            return None
        service_data = payload.get("service_data")
        if payload.get("target") or (
            isinstance(service_data, dict) and service_data.get("entity_id")
        ):
            return None

        if self._catalog.service_is_admin_only(domain, service):
            if not permissions.tier_allowed(f"{domain}.{service}", TIER_ADMIN):
                return Decision(
                    allowed=False,
                    reason=REASON_TIER,
                    detail=(f"{domain}.{service} is an administrative service"),
                )
            return Decision(allowed=True, filter_response=True)

        # The probe is never a real entity; it makes the domain-level rule in
        # the policy answer for a call that names no particular one.
        if not permissions.check_entity(f"{domain}.{DOMAIN_PROBE}", POLICY_CONTROL):
            return Decision(
                allowed=False,
                reason=REASON_RESOURCE,
                detail=f"no control access to the {domain} domain",
            )
        return Decision(allowed=True, filter_response=True)

    @staticmethod
    @callback
    def _split_http(name: str) -> tuple[str, str]:
        """Split a `"METHOD /path"` request name."""
        method, _, path = name.partition(" ")
        return method.upper(), path or "/"

    @staticmethod
    @callback
    def _merge_named_resource(found: Extracted, key: str, value: str) -> None:
        """Fold a resource named in the URL into the extracted set."""
        found.buckets[RESOURCE_KEYS[key]].add(value.lower())

    @staticmethod
    @callback
    def _merge_query_resource(found: Extracted, key: str, value: str) -> None:
        """Fold a resource named in the query string into the extracted set.

        Home Assistant spells some of these differently in query strings than in
        bodies, so the recognised names are normalised rather than assumed.
        """
        normalised = QUERY_RESOURCE_ALIASES.get(key, key)
        if (kind := RESOURCE_KEYS.get(normalised)) is None:
            return
        for item in value.split(","):
            if cleaned := item.strip().lower():
                found.buckets[kind].add(cleaned)

    @callback
    def _is_mutation(self, kind: str, name: str, payload: dict[str, Any]) -> bool:
        """Return True if a request changes state rather than reading it."""
        if kind == KIND_HTTP:
            return name.split(" ", 1)[0].upper() not in ("GET", "HEAD", "OPTIONS")

        if (info := self._catalog.info_for(name)) is not None and info.is_write:
            return True
        # `call_service` is the mutation with no write-shaped name, and
        # `execute_script` hides the same shape inside a sequence -- checking
        # only the top level asked for READ access to entities it then controls.
        return _invokes_a_service(payload)
