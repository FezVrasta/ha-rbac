"""Response filtering.

A read leaks through its response, so the response is where reads are made safe.
Filters register themselves with a decorator rather than being listed in a table,
and anything without a specific filter falls back to a generic walk that drops
objects carrying a denied entity id.
"""

from collections.abc import Callable
from functools import cached_property
from typing import Any

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.core import HomeAssistant

from .extract import entity_candidate

# Keys of the compressed state-diff protocol used by subscribe_entities.
# Within one entity's compressed state, "a" holds its attributes; at the event
# level the same letter means "added entity". Different levels, same letter.
COMPRESSED_ATTRIBUTES = "a"
STATE_DIFF_ADDITIONS = "+"
STATE_DIFF_REMOVALS = "-"

ENTITY_EVENT_ADD = "a"
ENTITY_EVENT_CHANGE = "c"
ENTITY_EVENT_REMOVE = "r"

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
    ) -> None:
        """Initialise the context."""
        self.hass = hass
        self.check = check
        self._app_allowed = app_allowed
        self._attribute_hidden = attribute_hidden

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
        )

    def app_visible(self, url_path: str) -> bool:
        """Return True if the user may see this sidebar app."""
        return self._app_allowed(url_path) if self._app_allowed else True

    def readable(self, entity_id: str) -> bool:
        """Return True if the user may read an entity."""
        return self.check(entity_id, POLICY_READ)

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
        """Filter one streamed event, falling back to the generic walk."""
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
            for key in LOVELACE_ENTITY_KEYS:
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
