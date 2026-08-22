"""The decision procedure.

Four gates, in order of cost: a pass-through fast path, the tier gate derived
from Home Assistant's own decorators, a resource check, and the boundedness rule.

The shape of the whole thing follows from one observation: reads and mutations
fail differently. A read leaks through its *response*, so it can be allowed and
its response filtered -- a command returning nothing resource-shaped needs no
classification at all. A mutation's damage is not visible in the response, so it
has to be judged up front.
"""

from dataclasses import dataclass
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .catalog import Catalog
from .const import RESOURCE_KEYS
from .extract import Extracted, extract, is_bounded
from .filters import FilterRegistry
from .policy import Permissions

KIND_WS = "ws"
KIND_HTTP = "http"

REASON_TIER = "tier"
REASON_RESOURCE = "resource"
REASON_UNBOUNDED = "unbounded"
REASON_DEGRADED = "degraded"


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

        found = extract(payload)
        if kind == KIND_HTTP:
            # A path parameter is a resource reference the body never carries.
            for key, value in self._catalog.path_resources(method, path).items():
                found = self._merge_path_resource(found, key, value)
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
        #    template, does not constrain its own command. Such a request is
        #    safe only if its response can be filtered.
        if not is_bounded(found):
            if self._filters.has(name):
                return Decision(
                    allowed=True, resources=sorted(entities), filter_response=True
                )
            if self._is_mutation(kind, name, payload):
                return Decision(
                    allowed=False,
                    reason=REASON_UNBOUNDED,
                    detail=f"{name!r} mutates without naming what it affects",
                )
            if found.templated:
                return Decision(
                    allowed=False,
                    reason=REASON_UNBOUNDED,
                    detail=(
                        f"{name!r} carries a template, whose reach is not limited "
                        "to the entities named"
                    ),
                )
            # An unbounded *read* with no registered filter: allow it, but run
            # the generic response filter. If the response carries nothing
            # resource-shaped there was nothing to leak.
            return Decision(
                allowed=True, resources=sorted(entities), filter_response=True
            )

        return Decision(
            allowed=True, resources=sorted(entities), filter_response=True
        )

    @staticmethod
    @callback
    def _split_http(name: str) -> tuple[str, str]:
        """Split a `"METHOD /path"` request name."""
        method, _, path = name.partition(" ")
        return method.upper(), path or "/"

    @staticmethod
    @callback
    def _merge_path_resource(found: Extracted, key: str, value: str) -> Extracted:
        """Fold a resource named in the URL into the extracted set."""
        bucket = found.buckets[RESOURCE_KEYS[key]]
        bucket.add(value)
        return found

    @callback
    def _is_mutation(self, kind: str, name: str, payload: dict[str, Any]) -> bool:
        """Return True if a request changes state rather than reading it."""
        if kind == KIND_HTTP:
            return name.split(" ", 1)[0].upper() not in ("GET", "HEAD", "OPTIONS")

        if (info := self._catalog.info_for(name)) is not None and info.is_write:
            return True
        # `call_service` is the mutation that carries no write-shaped name.
        return "domain" in payload and "service" in payload
