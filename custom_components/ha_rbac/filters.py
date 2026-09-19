"""Response filtering.

A read leaks through its response, so the response is where reads are made safe.
Filters register themselves with a decorator rather than being listed in a table,
and anything without a specific filter falls back to a generic walk that drops
objects carrying a denied entity id.
"""

from collections.abc import Callable
from functools import cached_property
from typing import Any, Final

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.core import HomeAssistant

from .const import EVENT_RBAC_DENIED, KEY_ENTITY, RESOURCE_KEYS
from .extract import Extracted, entity_candidate

# Keys of the compressed state-diff protocol used by subscribe_entities.
# Within one entity's compressed state, "a" holds its attributes; at the event
# level the same letter means "added entity". Different levels, same letter.
COMPRESSED_ATTRIBUTES = "a"
STATE_DIFF_ADDITIONS = "+"
STATE_DIFF_REMOVALS = "-"

ENTITY_EVENT_ADD = "a"
ENTITY_EVENT_CHANGE = "c"
ENTITY_EVENT_REMOVE = "r"

# What `subscribe_entities` opens with when the role may see nothing.
#
# The subscription's first frame carries every entity's initial state, and the
# frontend treats it as "states have loaded" -- it sits on a spinner until it
# arrives. A role that can read nothing, which a history-only grant makes a
# sensible thing to configure, filters that frame down to nothing, and an event
# filtered to nothing is not forwarded, so the session never finished loading.
#
# Home Assistant sends `{"a": {}}` to a user with no readable states, so that is
# what stands in: the shape the frontend already handles, not an invention.
#
# Only the *first* frame, and the substitution is the proxy's because only the
# proxy knows which frame that is. Answering every emptied diff with a bare
# frame would hand the role a clock: one frame per state change anywhere in the
# house, denied entities included, arriving the moment it happens. Measured on a
# test instance, five deliberate toggles of a light the role could not read
# produced five frames -- a real-time activity oracle for entities the role is
# not supposed to know exist, handed to it by the code that hides them.
OPENING_SNAPSHOT: Final[dict[str, Any]] = {ENTITY_EVENT_ADD: {}}

# Lovelace uses its own conventions, which are not Home Assistant resource keys.
LOVELACE_ENTITY_KEYS = ("entity", "entities", "camera_image")

type CheckFn = Callable[[str, str], bool]


class FilterContext:
    """What a filter needs to know about the requesting user."""

    def __init__(
        self,
        hass: HomeAssistant,
        check: CheckFn,
        app_allowed: "Callable[[str], bool] | None" = None,
        attribute_hidden: "Callable[[str, str], bool] | None" = None,
        history_check: "Callable[[str], bool] | None" = None,
    ) -> None:
        """Initialise the context."""
        self.hass = hass
        self.check = check
        self._app_allowed = app_allowed
        self._attribute_hidden = attribute_hidden
        self._history_check = history_check

    @property
    def hides_attributes(self) -> bool:
        """Return True if any attribute is withheld from this user."""
        return self._attribute_hidden is not None

    def strip_attributes(self, entity_id: str | None, attributes: Any) -> Any:
        """Remove withheld attributes from one entity's mapping of them.

        `entity_id` is None where a response does not say which entity the
        attributes belong to. Every rule is applied in that case, since the
        alternative is disclosing something a rule was written to withhold.
        """
        if self._attribute_hidden is None or not isinstance(attributes, dict):
            return attributes
        return {
            name: value
            for name, value in attributes.items()
            if not self._hidden(entity_id, name)
        }

    def strip_attribute_names(self, entity_id: str | None, names: Any) -> Any:
        """Remove withheld names from a list of them.

        A removal diff names attributes without their values, and forwarding one
        would disclose that the attribute exists at all.
        """
        if self._attribute_hidden is None or not isinstance(names, list):
            return names
        return [name for name in names if not self._hidden(entity_id, name)]

    def _hidden(self, entity_id: str | None, name: str) -> bool:
        """Return True if an attribute is withheld here."""
        if self._attribute_hidden is None:
            return False
        if entity_id is not None:
            return self._attribute_hidden(entity_id, name)
        return self._attribute_hidden(UNKNOWN_ENTITY, name)

    @classmethod
    def for_user(cls, hass: HomeAssistant, permissions: Any) -> "FilterContext":
        """Build the context for a user's permissions.

        The only way one should be constructed. Building them by hand at each
        call site meant the HTTP path silently lost attribute hiding while the
        websocket path kept it -- the same role withheld a location over one
        transport and served it over the other.
        """
        return cls(
            hass,
            permissions.check_entity,
            permissions.app_allowed,
            permissions.attribute_hidden if permissions.hides_attributes else None,
            permissions.history_allowed,
        )

    def app_visible(self, url_path: str) -> bool:
        """Return True if the user may see this sidebar app."""
        return self._app_allowed(url_path) if self._app_allowed else True

    def readable(self, entity_id: str) -> bool:
        """Return True if the user may read an entity."""
        return self.check(entity_id, POLICY_READ)

    def history_readable(self, entity_id: str) -> bool:
        """Return True if the user may read an entity's history.

        History is past state, so anything the role can read live it can also
        read the history of. A history grant adds entities on top of that, for
        a role meant to see one device's trend without being handed its current
        value everywhere else. With no history callback wired, this is exactly
        `readable`, so a context built by hand keeps the old behaviour.
        """
        if self._history_check is not None:
            return self._history_check(entity_id)
        return self.readable(entity_id)

    @cached_property
    def visible_domains(self) -> set[str]:
        """Return the domains the user can read at least one entity in.

        Derived from live state rather than from the policy's shape, so it is
        correct for roles that grant individual entities rather than domains.
        """
        return {
            entity_id.partition(".")[0]
            for entity_id in self.hass.states.async_entity_ids()
            if self.readable(entity_id)
        }


type FilterFn = Callable[[FilterContext, Any], Any]


class FilterRegistry:
    """Response filters, keyed by request type."""

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._result: dict[str, FilterFn] = {}
        self._event: dict[str, FilterFn] = {}

    def result(self, *commands: str) -> Callable[[FilterFn], FilterFn]:
        """Register a filter for a command's `result` payload."""

        def register(func: FilterFn) -> FilterFn:
            for command in commands:
                self._result[command] = func
            return func

        return register

    def event(self, *commands: str) -> Callable[[FilterFn], FilterFn]:
        """Register a filter for the events a subscription streams."""

        def register(func: FilterFn) -> FilterFn:
            for command in commands:
                self._event[command] = func
            return func

        return register

    def has(self, command: str) -> bool:
        """Return True if a command has a filter of either kind."""
        return command in self._result or command in self._event

    def filter_result(self, command: str, ctx: FilterContext, payload: Any) -> Any:
        """Filter a result payload, falling back to the generic walk."""
        if (func := self._result.get(command)) is not None:
            return func(ctx, payload)
        return prune(ctx, payload)

    def filter_event(self, command: str, ctx: FilterContext, payload: Any) -> Any:
        """Filter one streamed event, falling back to the generic walk.

        Every event frame the proxy relays passes through here, whatever the
        subscription was called, so this is where an event that must not reach
        a filtered connection at all is dropped -- ahead of any per-command
        filter and of the generic walk.

        There is exactly one: this integration's own denial event. It carries
        `detail`, which names commands, tiers and entity ids and is documented
        in `decide.py` as a diagnostic that must not reach an end user, plus
        the id and name of whoever was refused. It was reaching every
        restricted user on the instance -- `subscribe_events` with no filter is
        an ordinary command that every frontend session issues on load -- and
        neither the state-changed filter nor `prune` touched it, because
        `resources` is not a key either of them recognises and `detail` is free
        text. So a guest could watch every refusal in the house, learn the
        entity ids of things their own role hides entirely, and see which
        household member attempted what.

        Dropped rather than redacted: nothing a filtered connection does needs
        it. A refused request already carries its own `message` in the reply,
        and the deny log itself is behind an admin-only websocket command. A
        connection that is not being filtered never reaches this code, so
        automations and the panel are unaffected.
        """
        if isinstance(payload, dict) and payload.get("event_type") == EVENT_RBAC_DENIED:
            return None
        if (func := self._event.get(command)) is not None:
            return func(ctx, payload)
        return prune(ctx, payload)


REGISTRY = FilterRegistry()


# Stands in for "some entity, we do not know which". A rule targeting a domain
# or a specific entity will not match it, but an untargeted rule will -- which
# is the conservative reading when a response does not say what it describes.
UNKNOWN_ENTITY = "\x00unknown.\x00unknown"

# `config/entity_registry/list_for_display` spells entity_id this way.
DISPLAY_ENTITY_ID = "ei"

# Keys that only appear on a compressed state, used to tell one apart from any
# other object that happens to have an "a".
COMPRESSED_STATE_MARKERS = ("s", "lu", "lc")


def _is_compressed_state(node: dict[str, Any]) -> bool:
    """Return True if a mapping is a state in Home Assistant's compressed form."""
    return COMPRESSED_ATTRIBUTES in node and any(
        marker in node for marker in COMPRESSED_STATE_MARKERS
    )


def _looks_like_entity_id(value: Any) -> bool:
    """Return True if a string has the shape of an entity id."""
    return isinstance(value, str) and value.count(".") == 1 and " " not in value


def _container_visible(ctx: FilterContext, node: dict[str, Any]) -> bool:
    """Return True unless this object names a container the role cannot see.

    A device, area, label or floor stands for the entities inside it, so the
    question is whether the role can read any of them. If it can read none, the
    container is one it is not supposed to know exists, and the object naming it
    goes. If it can read some, the object stays and the walk keeps pruning what
    is inside it -- the same treatment an area gets everywhere else.

    A reference that resolves to nothing is left alone rather than treated as
    denied. `expand_to_entities` drops ids that are not in the registries, and a
    `device_id` in a Z-Wave payload is a Z-Wave node id, not a Home Assistant
    device; refusing on that basis would empty responses that name no Home
    Assistant resource at all.
    """
    # Imported here because `decide` imports this module: the expansion is one
    # already-tested implementation of "what entities does this stand for", and
    # a second one in a security path is a second one to get wrong.
    from .decide import expand_to_entities  # noqa: PLC0415

    found = Extracted()
    buckets = found.buckets
    named = False
    for key, kind in RESOURCE_KEYS.items():
        if kind == KEY_ENTITY or (value := node.get(key)) is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                buckets[kind].add(item)
                named = True
    if not named:
        return True

    entities = expand_to_entities(ctx.hass, found)
    if not entities:
        return True
    return any(ctx.readable(entity_id) for entity_id in entities)


def prune(ctx: FilterContext, node: Any) -> Any:
    """Drop anything carrying a denied entity id.

    Deliberately conservative about *how* it drops: it removes dict entries and
    list elements, but never reorders or renumbers, because clients hold onto
    indices. Where dropping an element would change the meaning of its
    container, a specific filter is registered instead.
    """
    if isinstance(node, dict):
        entity_id = node.get("entity_id") or node.get(DISPLAY_ENTITY_ID)
        if _looks_like_entity_id(entity_id) and not ctx.readable(entity_id):
            return None

        # An entity id is not the only way an object names something. The
        # request side treats `device_id`, `area_id`, `label_id` and `floor_id`
        # as first-class, and this walk did not: an event echoing one of those
        # rather than a resolved entity went through untouched. Home Assistant
        # fires `call_service` with the call's *original* target, before it is
        # resolved, so a role subscribed to events could watch a service being
        # invoked against an area or device it has no access to at all.
        if not _container_visible(ctx, node):
            return None

        # History and statistics return states in the compressed form, where
        # attributes live under "a" rather than "attributes". Recognised only
        # when the surrounding object really is a state, since "a" means
        # "added entities" one level up in the subscription protocol.
        compressed = _is_compressed_state(node)

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "attributes" and isinstance(value, dict):
                out[key] = ctx.strip_attributes(
                    entity_id if isinstance(entity_id, str) else None, value
                )
                continue
            if compressed and key == COMPRESSED_ATTRIBUTES and isinstance(value, dict):
                out[key] = ctx.strip_attributes(
                    entity_id if isinstance(entity_id, str) else None, value
                )
                continue
            if key in ("entity_id", "entity_ids") and isinstance(value, list):
                out[key] = [
                    item
                    for item in value
                    if not _looks_like_entity_id(item) or ctx.readable(item)
                ]
                continue
            pruned = prune(ctx, value)
            if pruned is not None or value is None:
                out[key] = pruned
        return out

    if isinstance(node, list):
        kept = [prune(ctx, item) for item in node]
        return [item for item in kept if item is not None]

    return node


@REGISTRY.result("get_states")
def _filter_get_states(ctx: FilterContext, result: Any) -> Any:
    """Drop states the role cannot read."""
    if not isinstance(result, list):
        return result
    return [
        _strip_state(ctx, state)
        for state in result
        if not isinstance(state, dict) or ctx.readable(state.get("entity_id", ""))
    ]


def _strip_state(ctx: FilterContext, state: Any) -> Any:
    """Remove withheld attributes from an uncompressed state object."""
    if not isinstance(state, dict) or "attributes" not in state:
        return state
    entity_id = state.get("entity_id")
    return {
        **state,
        "attributes": ctx.strip_attributes(
            entity_id if isinstance(entity_id, str) else None, state["attributes"]
        ),
    }


@REGISTRY.event("subscribe_entities")
def _filter_entity_event(ctx: FilterContext, event: Any) -> Any:
    """Filter a compressed state diff.

    Entity-level only. Filtering individual attributes would need per-connection
    shadow state to keep the `+`/`-` diffs coherent, and is not attempted.
    Dropping the whole entity also removes the rotating camera token that rides
    in `entity_picture`, which filtering the HTTP route alone would not.
    """
    if not isinstance(event, dict):
        return event

    out: dict[str, Any] = {}
    for key in (ENTITY_EVENT_ADD, ENTITY_EVENT_CHANGE):
        if isinstance(section := event.get(key), dict):
            kept = {
                entity_id: _strip_compressed(ctx, entity_id, key, value)
                for entity_id, value in section.items()
                if ctx.readable(entity_id)
            }
            if kept:
                out[key] = kept

    if isinstance(removed := event.get(ENTITY_EVENT_REMOVE), list):
        kept_ids = [entity_id for entity_id in removed if ctx.readable(entity_id)]
        if kept_ids:
            out[ENTITY_EVENT_REMOVE] = kept_ids

    # Nothing left means nothing is sent. The opening snapshot is the one
    # exception and the proxy makes it, not this: see OPENING_SNAPSHOT.
    return out or None


def _strip_compressed(ctx: FilterContext, entity_id: str, key: str, value: Any) -> Any:
    """Remove withheld attributes from one entity's compressed state or diff.

    Stripping the same names everywhere is enough -- the client never learns the
    attribute exists, so its picture stays consistent without the proxy having
    to track a shadow copy of it.
    """
    if not ctx.hides_attributes or not isinstance(value, dict):
        return value

    if key == ENTITY_EVENT_ADD:
        if COMPRESSED_ATTRIBUTES not in value:
            return value
        return {
            **value,
            COMPRESSED_ATTRIBUTES: ctx.strip_attributes(
                entity_id, value[COMPRESSED_ATTRIBUTES]
            ),
        }

    out = dict(value)
    added = out.get(STATE_DIFF_ADDITIONS)
    if isinstance(added, dict) and COMPRESSED_ATTRIBUTES in added:
        out[STATE_DIFF_ADDITIONS] = {
            **added,
            COMPRESSED_ATTRIBUTES: ctx.strip_attributes(
                entity_id, added[COMPRESSED_ATTRIBUTES]
            ),
        }
    removed = out.get(STATE_DIFF_REMOVALS)
    if isinstance(removed, dict) and COMPRESSED_ATTRIBUTES in removed:
        names = ctx.strip_attribute_names(entity_id, removed[COMPRESSED_ATTRIBUTES])
        if names:
            out[STATE_DIFF_REMOVALS] = {**removed, COMPRESSED_ATTRIBUTES: names}
        else:
            out.pop(STATE_DIFF_REMOVALS)
    return out


@REGISTRY.event("subscribe_events")
def _filter_subscribed_event(ctx: FilterContext, event: Any) -> Any:
    """Drop state_changed events for entities the role cannot read.

    Home Assistant re-checks these per event for non-admin users, but the proxy's
    upstream identity is frequently an admin, so it cannot rely on that.
    """
    if not isinstance(event, dict):
        return event
    data = event.get("data")
    if not isinstance(data, dict):
        return event
    entity_id = data.get("entity_id")
    if not _looks_like_entity_id(entity_id):
        # Not a state change. `call_service` names its targets under
        # `service_data.entity_id`, and other events bury them deeper still, so
        # the generic walk judges them rather than letting them through whole.
        return prune(ctx, event)
    if not ctx.readable(entity_id):
        return None
    if not ctx.hides_attributes:
        return event
    return {
        **event,
        "data": {
            **data,
            **{
                key: _strip_state(ctx, data[key])
                for key in ("new_state", "old_state")
                if isinstance(data.get(key), dict)
            },
        },
    }


# Keys of the `listeners` payload a template subscription reports.
LISTENER_ALL = "all"
LISTENER_ENTITIES = "entities"
LISTENER_DOMAINS = "domains"

# Never a real entity; it makes the domain-level rule in a policy answer for a
# domain whose members are not known yet.
DOMAIN_PROBE = "_rbac_probe"


@REGISTRY.event("render_template", "template/start_preview")
def _filter_template_event(ctx: FilterContext, event: Any) -> Any:
    """Withhold a rendered template that read something the role cannot.

    A template's reach is not limited by the entities its request names, which
    is why the request alone cannot be judged. But every result it streams
    carries `listeners` -- the states this particular render actually read -- so
    the response can be judged exactly.

    That is better than refusing templates outright, which is what this did
    before: a dashboard heading is a template, so restricted users were shown
    raw Jinja on their home screen for a template that reads nothing at all.
    """
    if not isinstance(event, dict):
        return event

    if "error" in event:
        # Jinja errors can quote the value that caused them, and an error frame
        # carries no listeners to check it against.
        return {**event, "error": "Template error"}

    listeners = event.get("listeners")
    if not isinstance(listeners, dict):
        # A result with no account of what it read cannot be cleared.
        return None

    if listeners.get(LISTENER_ALL):
        return None

    entities = listeners.get(LISTENER_ENTITIES) or ()
    if any(not ctx.readable(entity_id) for entity_id in entities):
        return None

    # A domain listener reads whatever appears in that domain, including
    # entities that do not exist yet -- so checking only the current ones is not
    # enough. An empty domain would pass, and the count alone tells the reader
    # how many of something they cannot see exist. The probe asks the policy
    # directly whether anything in the domain could be read.
    domains = listeners.get(LISTENER_DOMAINS) or ()
    for domain in domains:
        if not ctx.readable(f"{domain}.{DOMAIN_PROBE}"):
            return None
        if any(
            not ctx.readable(entity_id)
            for entity_id in ctx.hass.states.async_entity_ids(domain)
        ):
            return None

    return event


@REGISTRY.result("get_panels")
def _filter_panels(ctx: FilterContext, result: Any) -> Any:
    """Remove denied apps from the sidebar.

    Add-ons appear here too -- `hassio` registers each one as a panel keyed by
    its slug -- so denying an add-on and denying a built-in app are the same
    operation.
    """
    if not isinstance(result, dict):
        return result
    return {
        url_path: panel
        for url_path, panel in result.items()
        if ctx.app_visible(url_path)
    }


@REGISTRY.result("config/entity_registry/list_for_display")
def _filter_display_registry(ctx: FilterContext, result: Any) -> Any:
    """Drop hidden entities from the compact registry listing.

    It abbreviates `entity_id` to `ei`, so the generic walk did not recognise
    the entries and disclosed the name, device and area of every entity the role
    hides -- entities that are absent from every other response.
    """
    if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
        return prune(ctx, result)
    return {
        **result,
        "entities": [
            entry
            for entry in result["entities"]
            if not isinstance(entry, dict)
            or not _looks_like_entity_id(entry.get(DISPLAY_ENTITY_ID))
            or ctx.readable(entry[DISPLAY_ENTITY_ID])
        ],
    }


@REGISTRY.result("lovelace/dashboards/list")
def _filter_dashboards(ctx: FilterContext, result: Any) -> Any:
    """Drop denied dashboards from the listing.

    `get_panels` already hides them from the sidebar, but this lists the same
    dashboards by another route and named none of them in the request, so the
    app gate had nothing to match on.
    """
    if not isinstance(result, list):
        return result
    return [
        dashboard
        for dashboard in result
        if not isinstance(dashboard, dict)
        or not isinstance(dashboard.get("url_path"), str)
        or ctx.app_visible(dashboard["url_path"])
    ]


def _media_readable(ctx: FilterContext, item: Any) -> bool:
    """Return True unless a media item is an entity the role cannot read."""
    if not isinstance(item, dict):
        return True
    candidate = entity_candidate(item.get("media_content_id"))
    # Anything that is not really an entity is left alone: a local file is
    # `media-source://media_source/local/song.mp3`, whose tail has the shape of
    # an entity id and would otherwise empty the media browser.
    if candidate is None or ctx.hass.states.get(candidate) is None:
        return True
    return ctx.readable(candidate)


@REGISTRY.result(
    "media_source/browse_media",
    "media_source/search_media",
    "media_player/browse_media",
)
def _filter_media(ctx: FilterContext, result: Any) -> Any:
    """Drop media items naming entities the role cannot read.

    Cameras are a media source: `camera/media_source.py` lists every one with
    its friendly name and a `/api/camera_proxy/` thumbnail, and resolving one
    returns a stream URL that authenticates on its own. The entity is named in
    the tail of a `media-source://` URI, which is no resource key and which the
    generic walk reads as an ordinary string, so denied cameras were listed here
    after being hidden everywhere else.
    """
    if not isinstance(result, dict):
        return result
    filtered = dict(result)
    for key in ("children", "result"):
        if isinstance(children := filtered.get(key), list):
            filtered[key] = [
                _filter_media(ctx, item)
                for item in children
                if _media_readable(ctx, item)
            ]
    return filtered


# Fields in the energy preferences that name a statistic or an entity. A
# recorder statistic id for a sensor is that sensor's entity id, and the price
# fields hold an entity id outright, so all of them gate on readability. Listed
# because none is spelled `entity_id`, so the generic walk passes them by.
_ENERGY_STAT_FIELDS = (
    "stat_energy_from",
    "stat_energy_to",
    "stat_cost",
    "stat_compensation",
    "stat_rate",
    "stat_rate_from",
    "stat_rate_to",
    "stat_rate_inverted",
    "stat_consumption",
    "stat_soc",
    "included_in_stat",
    "entity_energy_price",
    "entity_energy_price_export",
)


def _energy_ref_readable(ctx: FilterContext, value: Any) -> bool:
    """Return True unless a value is an entity id the role cannot read.

    An external statistic id like `co2signal:co2_intensity` is not an entity
    and is left alone; only an entity-shaped id is gated.
    """
    return not _looks_like_entity_id(value) or ctx.readable(value)


def _energy_source_visible(ctx: FilterContext, source: Any) -> bool:
    """Return True unless an energy source names a statistic the role can't read.

    A source is dropped whole if any statistic or price field on it points at an
    entity the role cannot see: keeping a source with its meter stripped out
    would still disclose the topology -- that a grid, a battery or a solar array
    exists -- which is what this withholds.
    """
    if not isinstance(source, dict):
        return True
    for field in _ENERGY_STAT_FIELDS:
        if not _energy_ref_readable(ctx, source.get(field)):
            return False
    # A grid source nests its meters under flow_from/flow_to lists.
    for flow_key in ("flow_from", "flow_to"):
        flows = source.get(flow_key)
        if isinstance(flows, list) and not all(
            _energy_source_visible(ctx, flow) for flow in flows
        ):
            return False
    return True


@REGISTRY.result("energy/get_prefs")
def _filter_energy_prefs(ctx: FilterContext, result: Any) -> Any:
    """Drop energy sources and devices naming statistics the role cannot read.

    The prefs enumerate every meter, battery, solar array and monitored device
    by its recorder statistic id -- which for a sensor is its entity id -- under
    fields (`stat_energy_from`, `entity_energy_price`, `stat_consumption`, ...)
    that are not spelled `entity_id`, so the generic walk returned the lot. A
    restricted user learned every energy sensor, the grid/solar/battery topology
    and the price-entity ids of things hidden everywhere else.
    """
    if not isinstance(result, dict):
        return result
    filtered = dict(result)
    if isinstance(sources := filtered.get("energy_sources"), list):
        filtered["energy_sources"] = [
            source for source in sources if _energy_source_visible(ctx, source)
        ]
    for devices_key in ("device_consumption", "device_consumption_water"):
        if isinstance(devices := filtered.get(devices_key), list):
            filtered[devices_key] = [
                device for device in devices if _energy_source_visible(ctx, device)
            ]
    return filtered


def _statistic_id_readable(ctx: FilterContext, statistic_id: Any) -> bool:
    """Return True unless a statistic id is an entity the role cannot read.

    External statistics (`co2signal:...`, `tibber:...`) are not entities and
    pass; a recorder statistic for a sensor uses the entity id as its id, so it
    is gated.
    """
    return not _looks_like_entity_id(statistic_id) or ctx.readable(statistic_id)


@REGISTRY.result("recorder/list_statistic_ids", "recorder/get_statistics_metadata")
def _filter_statistic_metadata(ctx: FilterContext, result: Any) -> Any:
    """Drop statistic metadata for entities the role cannot read.

    Each row is keyed on a `statistic_id` field -- not `entity_id` -- so the
    generic walk left them, disclosing the id, friendly name, source and unit of
    every recorded sensor the role is hidden from. An entity-shaped statistic id
    is gated; an external one is left alone.
    """
    if not isinstance(result, list):
        return result
    return [
        row
        for row in result
        if not isinstance(row, dict)
        or _statistic_id_readable(ctx, row.get("statistic_id"))
    ]


# Search result keys whose values are entity ids (bare strings, not objects),
# so the generic walk -- which only inspects a dict's own `entity_id` field --
# never checked them.
_SEARCH_ENTITY_KEYS = ("entity", "automation", "scene", "script", "person", "group")

# The rest of the keys that name something the role may be hidden from are
# containers, and they are the resource kinds this integration already knows
# about: a search result's `device`, `area`, `label` and `floor` are the same
# things `device_id`, `area_id`, `label_id` and `floor_id` name on the request
# side. Derived from `RESOURCE_KEYS` rather than listed, because listing them is
# what let `floor` and `label` through: they were left out of a pair of
# hardcoded names while `device` and `area` were filtered, so an automation the
# role may read handed over the id of a floor and a label holding nothing it is
# allowed to see. `tests/test_filters.py` pins both sets against Home
# Assistant's own `ItemType`, so a kind added upstream fails a test rather than
# leaking quietly.
_SEARCH_CONTAINER_KEYS = tuple(
    sorted({kind for kind in RESOURCE_KEYS.values() if kind != KEY_ENTITY})
)


@REGISTRY.result("search/related")
def _filter_search_related(ctx: FilterContext, result: Any) -> Any:
    """Drop related items the role cannot see from a search result.

    `search/related` answers `{item_type: [ids]}` with the ids as bare strings
    -- `{"entity": ["lock.front"], "device": [...], "area": [...]}` -- so the
    generic walk, which looks only for an object's own `entity_id` field, passed
    the lot. The request names one item and is gated up front, but the response
    enumerates every entity, device and area related to it, hidden or not.
    """
    if not isinstance(result, dict):
        return result
    out: dict[str, Any] = {}
    for key, ids in result.items():
        if not isinstance(ids, list):
            out[key] = ids
            continue
        if key in _SEARCH_ENTITY_KEYS:
            kept = [i for i in ids if not _looks_like_entity_id(i) or ctx.readable(i)]
        elif key in _SEARCH_CONTAINER_KEYS:
            kept = [i for i in ids if _container_id_visible(ctx, key, i)]
        else:
            kept = ids
        if kept:
            out[key] = kept
    return out


def _container_id_visible(ctx: FilterContext, kind: str, container_id: Any) -> bool:
    """Return True if the role can read any entity in a container.

    A device, area, label or floor the role can see nothing in is one it is not
    meant to know exists, so its id is withheld -- the same rule
    `_container_visible` applies to objects that name one.
    """
    if not isinstance(container_id, str):
        return True
    node = {f"{kind}_id": container_id}
    return _container_visible(ctx, node)


@REGISTRY.result("get_services")
def _filter_get_services(ctx: FilterContext, result: Any) -> Any:
    """Hide service domains the role has no entity in.

    Derived from the role's own reach rather than from a list of domains.
    """
    if not isinstance(result, dict):
        return result
    visible = ctx.visible_domains
    return {
        domain: services
        for domain, services in result.items()
        # A domain is visible if the role can read any entity in it. Service
        # domains with no entities at all (`homeassistant`, `persistent_
        # notification`) are kept, since hiding them breaks the UI without
        # concealing anything about the user's devices.
        if domain in visible
        or not any(
            entity_id.startswith(f"{domain}.")
            for entity_id in ctx.hass.states.async_entity_ids()
        )
    }


def _lovelace_entity_keys(node: dict[str, Any]) -> list[str]:
    """Return the keys of one card that name entities.

    The three Lovelace itself uses, plus the convention custom cards follow
    when they add their own: a suffix of `_entity` or `_entities`. Advanced
    Camera Card asks for `camera_entity`, and a denied camera stayed in the
    dashboard configuration under it -- which hands over the entity id of
    something the role hides entirely, and with it the name to go looking for
    on that integration's own routes.

    A suffix rather than a longer list, because the list is the thing this
    project exists to avoid: a card key nobody has heard of is covered the day
    somebody writes it, as long as it is spelled the way the others are.
    """
    return [
        key
        for key in node
        if isinstance(key, str)
        and (key in LOVELACE_ENTITY_KEYS or key.endswith(("_entity", "_entities")))
    ]


@REGISTRY.result("lovelace/config")
def _filter_lovelace(ctx: FilterContext, result: Any) -> Any:
    """Drop cards referring to entities the role cannot read.

    Needs its own filter because Lovelace's `entity` and `entities` keys are its
    own convention, not Home Assistant resource keys, so the generic walk does
    not recognise them. A heavily filtered dashboard renders with empty views,
    which is the accepted trade.
    """

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            for key in _lovelace_entity_keys(node):
                value = node.get(key)
                if _looks_like_entity_id(value) and not ctx.readable(value):
                    return None
                if isinstance(value, list):
                    kept = []
                    for item in value:
                        if _looks_like_entity_id(item):
                            if ctx.readable(item):
                                kept.append(item)
                        elif isinstance(item, dict):
                            if (scrubbed := scrub(item)) is not None:
                                kept.append(scrubbed)
                        else:
                            kept.append(item)
                    node = {**node, key: kept}
            return {
                key: scrubbed
                for key, value in node.items()
                if (scrubbed := scrub(value)) is not None or value is None
            }
        if isinstance(node, list):
            return [item for item in (scrub(v) for v in node) if item is not None]
        return node

    return scrub(result)


@REGISTRY.result("auth/current_user")
def _filter_current_user(ctx: FilterContext, result: Any) -> Any:
    """Report a restricted user as non-admin so the frontend hides admin UI.

    Cosmetic only. The enforcement is the tier gate; this just stops the UI
    offering things that will fail.
    """
    if isinstance(result, dict):
        return {**result, "is_admin": False}
    return result


# Catalogues that carry no entity ids. Pruning them cannot improve safety and
# would corrupt the frontend, so they are passed through untouched.
@REGISTRY.result(
    "get_config",
    "manifest/list",
    "manifest/get",
    "frontend/get_themes",
    "frontend/get_translations",
    "frontend/get_icons",
    "frontend/get_version",
    "integration/setup_info",
)
def _passthrough(ctx: FilterContext, result: Any) -> Any:
    """Return the payload unchanged."""
    return result


# Supervisor endpoints that list add-ons without naming any, so the app gate
# never fires on them.
SUPERVISOR_ADDON_LISTINGS = frozenset(
    {"/addons", "/apps", "/store", "/store/addons", "/store/apps", "/ingress/panels"}
)


def strip_denied_addons(ctx: FilterContext, endpoint: str, result: Any) -> Any:
    """Remove add-ons the role cannot open from a Supervisor listing.

    The app gate refuses a request that names a denied add-on, but these name
    none -- so without this they list every add-on the sidebar correctly hides,
    which is the same gap `lovelace/dashboards/list` had for dashboards.
    """
    if not isinstance(result, dict):
        return result
    if endpoint.split("?", 1)[0].rstrip("/") not in SUPERVISOR_ADDON_LISTINGS:
        return result

    # Supervisor answers `{"result": "ok", "data": {...}}`, but Home Assistant
    # unwraps `data` on some paths, so both shapes reach here.
    wrapped = isinstance(result.get("data"), dict)
    body = result["data"] if wrapped else result
    cleaned = dict(body)

    if isinstance(entries := body.get("addons"), list):
        cleaned["addons"] = [
            entry
            for entry in entries
            if not isinstance(entry, dict)
            or not isinstance(slug := entry.get("slug"), str)
            or ctx.app_visible(slug)
        ]
    if isinstance(panels := body.get("panels"), dict):
        cleaned["panels"] = {
            slug: panel
            for slug, panel in panels.items()
            if not isinstance(slug, str) or ctx.app_visible(slug)
        }

    return {**result, "data": cleaned} if wrapped else cleaned


def _filter_history_states(ctx: FilterContext, states: Any) -> Any:
    """Filter a mapping of entity id -> that entity's compressed states.

    History is the one response shape where the entity a sample belongs to is
    not in the sample. Each one carries only `s`, `a`, `lu`, `lc`, keyed by
    entity id one level up, so the generic walk recovered no entity id and
    stripped attributes with `None` -- which matches only the rules written
    against no entity or domain in particular. A rule scoped to an entity or a
    domain, "hide latitude and longitude on person.*", was silently skipped for
    history while working correctly for `get_states` and `subscribe_entities`.

    Here the key is the entity id, so it is threaded down into the strip and the
    rule matches. Unreadable entities go entirely rather than being emptied: an
    entity id with a zero-length history still says the entity exists.
    """
    if not isinstance(states, dict):
        return prune(ctx, states)
    out: dict[str, Any] = {}
    for entity_id, samples in states.items():
        if _looks_like_entity_id(entity_id) and not ctx.history_readable(entity_id):
            continue
        named = entity_id if _looks_like_entity_id(entity_id) else None
        if not ctx.hides_attributes or not isinstance(samples, list):
            out[entity_id] = samples
            continue
        out[entity_id] = [
            {
                **sample,
                COMPRESSED_ATTRIBUTES: ctx.strip_attributes(
                    named, sample[COMPRESSED_ATTRIBUTES]
                ),
            }
            if isinstance(sample, dict)
            and isinstance(sample.get(COMPRESSED_ATTRIBUTES), dict)
            else sample
            for sample in samples
        ]
    return out


@REGISTRY.result("history/history_during_period")
def _filter_history_result(ctx: FilterContext, result: Any) -> Any:
    """Filter the entity-keyed mapping the result is."""
    return _filter_history_states(ctx, result)


@REGISTRY.event("history/stream", "history/history_during_period")
def _filter_history_event(ctx: FilterContext, event: Any) -> Any:
    """Filter the same mapping, which a stream frame wraps under `states`."""
    if not isinstance(event, dict) or not isinstance(event.get("states"), dict):
        return prune(ctx, event)
    return {**event, "states": _filter_history_states(ctx, event["states"])}


def _filter_statistics(ctx: FilterContext, stats: Any) -> Any:
    """Filter a mapping of statistic id -> that statistic's rows.

    Statistics are keyed the way history is: `{statistic_id: [rows]}`, where a
    recorder statistic for an entity uses the entity id as its key and each row
    (`{start, mean, min, max, sum, ...}`) names no entity of its own. The
    generic walk recovered nothing from the key, so a denied entity's numbers
    passed straight through -- the same leak history had before it got its own
    filter. An entity-shaped key is gated on history access; an external
    statistic id like `energy:solar` is not an entity and is left alone.
    """
    if not isinstance(stats, dict):
        return prune(ctx, stats)
    return {
        statistic_id: rows
        for statistic_id, rows in stats.items()
        if not _looks_like_entity_id(statistic_id) or ctx.history_readable(statistic_id)
    }


@REGISTRY.result(
    "history/statistics_during_period", "recorder/statistics_during_period"
)
def _filter_statistics_result(ctx: FilterContext, result: Any) -> Any:
    """Filter the statistic-id-keyed mapping the result is."""
    return _filter_statistics(ctx, result)


@REGISTRY.event("history/statistics_during_period", "recorder/statistics_during_period")
def _filter_statistics_event(ctx: FilterContext, event: Any) -> Any:
    """Filter a streamed statistics frame, keyed the same way."""
    if isinstance(event, dict) and isinstance(event.get("statistics"), dict):
        return {**event, "statistics": _filter_statistics(ctx, event["statistics"])}
    return _filter_statistics(ctx, event)


def filter_rest_history(ctx: FilterContext, payload: Any) -> Any:
    """Filter the REST `/api/history/period` response, whichever shape it is.

    The REST endpoint answers in one of two shapes:

    * A mapping `{entity_id: [samples]}`, the same shape the websocket result
      carries, so `_filter_history_states` handles it directly.
    * A *list of lists* -- one inner list per entity -- which is what Home
      Assistant returns by default and always with `minimal_response`. Here the
      entity id is not on every sample: only the FIRST object in each inner list
      carries `entity_id`, and the rest are minimised to just a state and a
      timestamp. The generic `prune` walk recovers an id only from a sample's
      own key, so it dropped a denied entity's first sample and kept every
      later one -- leaking the values, timestamps and attributes the denial was
      meant to withhold. The whole inner list belongs to one entity, so it is
      that entity that decides whether the list stays or goes, read from the
      first sample that names it.
    """
    if isinstance(payload, dict):
        return _filter_history_states(ctx, payload)
    if not isinstance(payload, list):
        return prune(ctx, payload)

    out: list[Any] = []
    for series in payload:
        entity_id = _series_entity_id(series)
        if entity_id is not None and not ctx.history_readable(entity_id):
            # One entity owns the whole inner list; drop it entirely rather
            # than sample by sample, so no minimised tail survives.
            continue
        if entity_id is not None and ctx.hides_attributes and isinstance(series, list):
            out.append([_strip_rest_history_sample(ctx, entity_id, s) for s in series])
        else:
            out.append(series)
    return out


def _series_entity_id(series: Any) -> str | None:
    """Return the entity id an inner history list belongs to, if it names one.

    Only the first sample carries it under minimisation; a series that names no
    id anywhere is left for the caller to pass through untouched rather than
    guessed at.
    """
    if not isinstance(series, list):
        return None
    for sample in series:
        if isinstance(sample, dict):
            candidate = sample.get("entity_id")
            if _looks_like_entity_id(candidate):
                return candidate
    return None


def _strip_rest_history_sample(ctx: FilterContext, entity_id: str, sample: Any) -> Any:
    """Strip withheld attributes from one uncompressed REST history sample."""
    if not isinstance(sample, dict) or not isinstance(sample.get("attributes"), dict):
        return sample
    return {
        **sample,
        "attributes": ctx.strip_attributes(entity_id, sample["attributes"]),
    }
