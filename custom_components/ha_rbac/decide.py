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
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

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
from homeassistant.helpers import (
    group as group_helper,
)

from .catalog import Catalog
from .const import (
    CAPABILITY_PATTERNS,
    MAX_WALK_DEPTH,
    RESOURCE_KEYS,
    TIER_ADMIN,
    TIER_OPEN,
)
from .extract import Extracted, entity_ids_in, extract, is_bounded
from .filters import FilterRegistry
from .policy import Permissions

if TYPE_CHECKING:
    from .record import Recorder, Recording

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

# The Settings panel, whose url path is also the namespace every
# registry command lives under.
CONFIG_PANEL = "config"


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


# What a refused person is told. `detail` beside it stays a diagnostic for the
# deny log: it names commands, tiers and entity ids, which is noise to the
# person reading it and an existence oracle to anyone probing.
#
# Deliberately vague about *why*. "Your role does not include that screen" and
# "you cannot control that" are both answers a restricted user can act on --
# ask whoever runs the house -- without describing the shape of the policy.
USER_MESSAGES = {
    REASON_TIER: "That is a Home Assistant setting your role does not include.",
    REASON_APP: "Your role does not include that screen.",
    REASON_RESOURCE: "You do not have permission to do that.",
    REASON_UNBOUNDED: "Your role does not allow that action.",
    REASON_DEGRADED: (
        "Access control cannot check permissions right now, so this was "
        "refused. Ask an administrator to check the logs."
    ),
}
DEFAULT_USER_MESSAGE = "You do not have permission to do that."


@dataclass(slots=True)
class Decision:
    """The verdict on one request, and why."""

    allowed: bool
    reason: str = ""
    detail: str = ""
    # What the person is shown. Defaulted from `reason` rather than written at
    # each refusal, so a gate added later cannot leak its diagnostic by anyone
    # forgetting to set this.
    message: str = ""
    # Entities the request named, for the deny log and for response filtering.
    resources: list[str] = None  # type: ignore[assignment]
    filter_response: bool = False

    def __post_init__(self) -> None:
        """Default the resource list and the message shown to the person."""
        if self.resources is None:
            self.resources = []
        if not self.allowed and not self.message:
            self.message = USER_MESSAGES.get(self.reason, DEFAULT_USER_MESSAGE)


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

    # Home Assistant expands group members server-side when a service targets a
    # group, so a denied entity reachable through a group must be checked here
    # too -- otherwise `lock.unlock` aimed at a group containing a denied lock
    # slips past, the group id being an allowed entity of its own. Expand last,
    # over the whole set, since a targeted area can itself contain a group. This
    # is the same helper the service framework uses, so the expansion matches.
    entities.update(group_helper.expand_entity_ids(hass, entities))

    return entities


class Decider:
    """Applies the gates to inbound requests."""

    def __init__(
        self,
        hass: HomeAssistant,
        catalog: Catalog,
        filter_registry: FilterRegistry,
        recorder: "Recorder | None" = None,
    ) -> None:
        """Initialise the decider."""
        self._hass = hass
        self._catalog = catalog
        self._filters = filter_registry
        self._recorder = recorder

    @callback
    def is_recording(self, permissions: Permissions) -> bool:
        """Return True if a recording covers this user.

        The proxy needs this on the way back as well as on the way in. A
        recording allows every request and filters no response, and the two
        halves have to agree: allowing the request and then filtering the reply
        against rules the recording is meant to be suspending shows the person
        an empty Home Assistant, which is nothing like the one they use.
        """
        if self._recorder is None:
            return False
        return self._recorder.for_permissions(permissions) is not None

    @callback
    def _resource_message(
        self, permissions: Permissions, denied: list[str], key: str
    ) -> str:
        """Say what was refused, in the words the person sees on their screen.

        Only entities the role may *see* are named. Someone refused control of
        the porch light already has it on their dashboard, so naming it tells
        them nothing they did not know and saves them guessing which tap failed.
        Naming one they cannot see would hand over the existence of a thing that
        is otherwise entirely hidden from them, so those stay anonymous.
        """
        names = []
        for entity_id in denied:
            if not permissions.check_entity(entity_id, POLICY_READ):
                continue
            state = self._hass.states.get(entity_id)
            friendly = state.attributes.get("friendly_name") if state else None
            names.append(str(friendly) if friendly else entity_id)

        verb = "control" if key == POLICY_CONTROL else "see"
        if not names:
            return f"You do not have permission to {verb} that."
        if len(names) == 1:
            return f"You do not have permission to {verb} {names[0]}."
        # Long lists help nobody; the count carries the same meaning.
        if len(names) > 3:
            return f"You do not have permission to {verb} {len(names)} of those."
        return (
            f"You do not have permission to {verb} "
            f"{', '.join(names[:-1])} or {names[-1]}."
        )

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

        # A role being recorded is unrestricted while the recording runs, and
        # every request is noted instead of judged. It sits above the gates
        # deliberately: a recording that only saw what the role already allows
        # would tell nobody anything.
        if self._recorder is not None and (
            recording := self._recorder.for_permissions(permissions)
        ):
            return self._observe(recording, kind, name, payload, query)

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
        # A payload can also name an entity where no resource key reaches: as a
        # mapping key, which is how `scene.apply` says which states to
        # reproduce, or in the tail of a `media-source://` URI. Only the
        # registry can tell either from an ordinary string, so these are
        # confirmed against it rather than assumed -- and they are added to the
        # check without counting as a bound, so a payload that names nothing a
        # schema recognises stays unbounded.
        entities |= entity_ids_in(payload, self._entity_exists)
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
                message=self._resource_message(permissions, denied, key),
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

        for app in denied:
            if not self._app_named(app, kind, name, payload):
                continue
            what = f"the {app['title']} add-on" if app.get("addon") else app["title"]
            return Decision(
                allowed=False, reason=REASON_APP, detail=f"no access to {what}"
            )
        return None

    @callback
    def _app_named(
        self, app: dict[str, Any], kind: str, name: str, payload: dict[str, Any]
    ) -> bool:
        """Return True if a request reaches into one app.

        Kept apart from the decision because a recording needs the same answer
        for the opposite purpose: to note which app was opened rather than to
        refuse it. Two copies of this that had to agree would be the defect,
        not the duplication.
        """
        url_path = app["url_path"]

        named = payload.get("url_path")
        if isinstance(named, str) and named == url_path:
            return True

        if slug := app.get("addon"):
            endpoint = payload.get("endpoint")
            return isinstance(endpoint, str) and f"/{slug}" in endpoint

        # Dashboards all share the `lovelace/` commands, so the prefix rule
        # would take every dashboard down with one of them. They are covered by
        # the `url_path` check above instead.
        if app.get("kind") == DASHBOARD_KIND:
            return False

        prefix = url_path.replace("-", "_")
        if kind != KIND_HTTP and name.startswith(f"{prefix}/"):
            # `config/` is not the Settings panel's own namespace, it is Home
            # Assistant's namespace for every registry, and the area, device,
            # entity and floor lists behind it are what any dashboard reads
            # before it can draw anything at all. Denying Settings emptied the
            # whole interface. So for this one panel the convention is narrowed
            # to requests that change something; its reads are filtered like any
            # other, and the tier gate already refuses the administrative half
            # outright.
            return url_path != CONFIG_PANEL or self._is_mutation(kind, name, payload)
        return False

    @callback
    def apps_named(
        self, kind: str, name: str, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return every app a request reaches into, denied or not."""
        return [
            app
            for app in self._catalog.apps()
            if self._app_named(app, kind, name, payload)
        ]

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

        named = f"{domain}.{service}"
        if self._catalog.service_is_admin_only(domain, service):
            if not permissions.tier_allowed(named, TIER_ADMIN):
                return Decision(
                    allowed=False,
                    reason=REASON_TIER,
                    detail=(f"{domain}.{service} is an administrative service"),
                )
            return Decision(allowed=True, filter_response=True)

        # A role may forbid a service Home Assistant does not consider
        # administrative -- one that names no entity, so nothing else here would
        # catch it. Asked at the lowest tier, so the ranked comparison passes
        # and only an explicit rule decides: a denial refuses, an allowance
        # grants, and a role that says nothing about it is unaffected.
        if not permissions.tier_allowed(named, TIER_OPEN):
            return Decision(
                allowed=False,
                reason=REASON_TIER,
                detail=f"role does not permit the {named} action",
            )

        # The probe is never a real entity; it makes the domain-level rule in
        # the policy answer for a call that names no particular one.
        if not permissions.check_entity(f"{domain}.{DOMAIN_PROBE}", POLICY_CONTROL):
            return Decision(
                allowed=False,
                reason=REASON_RESOURCE,
                detail=f"no control access to the {domain} domain",
            )
        return Decision(allowed=True, filter_response=True)

    @callback
    def _observe(
        self,
        recording: "Recording",
        kind: str,
        name: str,
        payload: dict[str, Any],
        query: "Mapping[str, str] | None",
    ) -> Decision:
        """Note what a request needed and allow it.

        The same extraction the resource gate runs, used to write down rather
        than to refuse. Nothing is filtered on the way back either -- a
        recording that trimmed the response would be recording a smaller Home
        Assistant than the one the person is actually using.
        """
        found = extract(payload)
        method, path = self._split_http(name) if kind == KIND_HTTP else ("", "")
        if kind == KIND_HTTP:
            for key, value in self._catalog.path_resources(method, path).items():
                self._merge_named_resource(found, key, value)
            for key, value in (query or {}).items():
                self._merge_query_resource(found, key, value)

        entities = expand_to_entities(self._hass, found)
        entities |= entity_ids_in(payload, self._entity_exists)
        key = POLICY_CONTROL if self._is_mutation(kind, name, payload) else POLICY_READ
        for entity_id in entities:
            recording.note_entity(entity_id, key)

        for app in self.apps_named(kind, name, payload):
            recording.apps.add(app["url_path"])

        tier = (
            self._catalog.tier_for_request(method, path)
            if kind == KIND_HTTP
            else self._catalog.tier_for(name)
        )
        if tier == TIER_ADMIN:
            for capability, patterns in CAPABILITY_PATTERNS.items():
                if any(fnmatch(name, pattern) for pattern in patterns):
                    recording.capabilities.add(capability)

        return Decision(allowed=True, resources=sorted(entities))

    @callback
    def _entity_exists(self, entity_id: str) -> bool:
        """Return True if an entity id names something on this instance.

        The registry is consulted as well as the state machine, so a disabled or
        not-yet-loaded entity is still recognised rather than read as an
        ordinary string.
        """
        return (
            self._hass.states.get(entity_id) is not None
            or er.async_get(self._hass).async_get(entity_id) is not None
        )

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
