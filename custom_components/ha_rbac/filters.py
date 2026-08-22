"""Response filtering.

A read leaks through its response, so the response is where reads are made safe.
Filters register themselves with a decorator rather than being listed in a table,
and anything without a specific filter falls back to a generic walk that drops
objects carrying a denied entity id.
"""

from collections.abc import Callable
from typing import Any

from functools import cached_property

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.core import HomeAssistant

# Keys of the compressed state-diff protocol used by subscribe_entities.
ENTITY_EVENT_ADD = "a"
ENTITY_EVENT_CHANGE = "c"
ENTITY_EVENT_REMOVE = "r"

# Lovelace uses its own conventions, which are not Home Assistant resource keys.
LOVELACE_ENTITY_KEYS = ("entity", "entities", "camera_image")

type CheckFn = Callable[[str, str], bool]


class FilterContext:
    """What a filter needs to know about the requesting user."""

    def __init__(self, hass: HomeAssistant, check: CheckFn) -> None:
        """Initialise the context."""
        self.hass = hass
        self.check = check

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
        entity_id = node.get("entity_id")
        if _looks_like_entity_id(entity_id) and not ctx.readable(entity_id):
            return None

        out: dict[str, Any] = {}
        for key, value in node.items():
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
        state
        for state in result
        if not isinstance(state, dict)
        or ctx.readable(state.get("entity_id", ""))
    ]


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
                entity_id: value
                for entity_id, value in section.items()
                if ctx.readable(entity_id)
            }
            if kept:
                out[key] = kept

    if isinstance(removed := event.get(ENTITY_EVENT_REMOVE), list):
        kept_ids = [
            entity_id for entity_id in removed if ctx.readable(entity_id)
        ]
        if kept_ids:
            out[ENTITY_EVENT_REMOVE] = kept_ids

    return out or None


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
    if _looks_like_entity_id(entity_id) and not ctx.readable(entity_id):
        return None
    return event


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
        if domain in visible or not any(
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
