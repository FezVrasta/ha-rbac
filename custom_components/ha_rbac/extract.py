"""Extract resource references from request payloads.

The point of this module is that it knows nothing about individual commands.
It walks whatever JSON it is handed, collecting the resource keys Home Assistant
uses by convention, so that new commands and new integrations are covered
without anyone maintaining a table.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from .const import (
    KEY_AREA,
    KEY_DEVICE,
    KEY_ENTITY,
    KEY_FLOOR,
    KEY_LABEL,
    MAX_WALK_DEPTH,
    MAX_WALK_NODES,
    RESOURCE_KEYS,
    SENTINEL_ALL,
    SENTINEL_NONE,
)

# Keys whose values are themselves selectors or nested action structures. The
# walk descends into everything, so this is only used to recognise a `target`
# field as resource-bearing when classifying a schema.
TARGET_KEY = "target"

# Home Assistant templates require one of these markers; `Template.async_render`
# returns any other string verbatim. Scanning for them is how a template's
# unbounded reach is detected without naming the commands that accept one.
TEMPLATE_MARKERS = ("{{", "{%")

# A media source names its entity in the tail of a URI rather than under a
# resource key: `media-source://camera/camera.front_door`.
MEDIA_SOURCE_PREFIX = "media-source://"


def _is_template(value: str) -> bool:
    """Return True if a string would be interpreted as a Jinja template."""
    return any(marker in value for marker in TEMPLATE_MARKERS)


def entity_candidate(value: Any) -> str | None:
    """Return the entity id a string might name, for the registry to confirm."""
    if not isinstance(value, str):
        return None
    if value.startswith(MEDIA_SOURCE_PREFIX):
        value = value.rpartition("/")[2]
    if value.count(".") != 1 or " " in value:
        return None
    return value.lower()


@dataclass(slots=True)
class Extracted:
    """Resources referenced by a payload."""

    entities: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    areas: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    floors: set[str] = field(default_factory=set)
    # Set when the payload names every entity, e.g. `entity_id: "all"`.
    unbounded: bool = False
    # Set when any string in the payload carries Jinja template syntax. A
    # template's reach is not constrained by the resources the payload names,
    # so this makes the command unbounded no matter what else it declares.
    templated: bool = False
    # Set when the walk hit its depth or node cap and may have missed a
    # reference. Treated as unbounded so a truncated walk never grants access.
    truncated: bool = False

    @property
    def buckets(self) -> dict[str, set[str]]:
        """Return the collected resources keyed by resource kind."""
        return {
            KEY_ENTITY: self.entities,
            KEY_DEVICE: self.devices,
            KEY_AREA: self.areas,
            KEY_LABEL: self.labels,
            KEY_FLOOR: self.floors,
        }

    def is_empty(self) -> bool:
        """Return True if the payload named no resources at all."""
        return not any(self.buckets.values())


def _add(target: Extracted, kind: str, value: Any) -> None:
    """Record one resource reference, which may be a string or a list."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [v for v in value if isinstance(v, str)]
    else:
        return

    bucket = target.buckets[kind]
    for raw in values:
        # Home Assistant lowercases these on the way in (`cv.entity_id`), and
        # its policy lookup is an exact dict match -- so comparing the raw string
        # would let `LOCK.Front` miss a deny rule written for `lock.front`, and
        # `ALL` miss the sentinel while still counting as one named resource,
        # which would satisfy the boundedness gate.
        item = raw.lower()
        if kind == KEY_ENTITY and item in (SENTINEL_ALL, SENTINEL_NONE):
            if item == SENTINEL_ALL:
                target.unbounded = True
            continue
        bucket.add(item)


def extract(payload: Any) -> Extracted:
    """Collect every resource reference in a payload.

    Descends into nested dicts and lists without regard for structure, which is
    what makes `call_service` with `target.area_id`, and automation configs
    carrying `device_id` inside trigger blocks, work without knowing either
    schema. The walk is capped because an `execute_script` payload is
    attacker-controlled JSON.
    """
    found = Extracted()
    nodes = 0
    stack: list[tuple[Any, int]] = [(payload, 0)]

    while stack:
        node, depth = stack.pop()

        nodes += 1
        if nodes > MAX_WALK_NODES or depth > MAX_WALK_DEPTH:
            found.truncated = True
            break

        if isinstance(node, str):
            if _is_template(node):
                found.templated = True
        elif isinstance(node, dict):
            for key, value in node.items():
                if (kind := RESOURCE_KEYS.get(key)) is not None:
                    _add(found, kind, value)
                    # A resource key may still hold structure worth descending
                    # into, e.g. `entity_id: {...}` in a malformed payload.
                    if not isinstance(value, (str, list)):
                        stack.append((value, depth + 1))
                    continue
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)

    return found


def entity_ids_in(payload: Any, exists: "Callable[[str], bool]") -> set[str]:
    """Return every entity a structure mentions, wherever it mentions it.

    Written for Lovelace, which names entities under `entity`, `entities`,
    `camera_image`, `badges` and a dozen card-specific keys that custom cards
    extend at will. Enumerating those keys would be the sort of table this
    project exists to avoid, so instead every string is tested for the shape of
    an entity id and then against the machine: something that both looks like
    one and is one is one. A card key nobody has heard of costs nothing.

    Mapping *keys* are tested too, because Home Assistant sometimes keys a
    structure by entity id instead of naming one in a value: `scene.apply` takes
    `{"entities": {"lock.front": "unlocked"}}` and reproduces those states.
    """
    found: set[str] = set()
    nodes = 0
    stack: list[tuple[Any, int]] = [(payload, 0)]

    while stack:
        node, depth = stack.pop()

        nodes += 1
        if nodes > MAX_WALK_NODES or depth > MAX_WALK_DEPTH:
            break

        if isinstance(node, str):
            if (candidate := entity_candidate(node)) and exists(candidate):
                found.add(candidate)
        elif isinstance(node, dict):
            for key, value in node.items():
                if (candidate := entity_candidate(key)) and exists(candidate):
                    found.add(candidate)
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)

    return found


def schema_resource_markers(schema: Any) -> tuple[set[str], set[str]]:
    """Return the required and optional resource field names of a schema.

    Provenance is load-bearing: an *optional* resource field does not bound a
    command. `render_template` declares `vol.Optional("entity_ids")` purely as a
    rendering hint for the frontend, so treating it as a bound would check a
    decoy list and then allow arbitrary server-side Jinja.
    """
    required: set[str] = set()
    optional: set[str] = set()

    inner = getattr(schema, "schema", None)
    if not isinstance(inner, dict):
        return required, optional

    for marker in inner:
        name = getattr(marker, "schema", marker)
        if not isinstance(name, str):
            continue
        if name not in RESOURCE_KEYS and name != TARGET_KEY:
            continue
        if isinstance(marker, vol.Required):
            required.add(name)
        elif isinstance(marker, vol.Optional):
            optional.add(name)
        else:
            # A bare string key in a voluptuous schema is required.
            required.add(name)

    return required, optional


def is_bounded(extracted: Extracted, required_fields: set[str] | None = None) -> bool:
    """Return True if a command's effect cannot exceed the resources it names.

    Three ways a payload fails to bound its own command:

    * it names nothing at all;
    * it carries a template, whose reach is unrelated to what it names — this is
      what stops `render_template` passing a decoy `entity_ids` alongside
      ``{{ states('lock.front_door') }}``, and it also catches a template hidden
      in `service_data`, which a schema-shape rule would miss;
    * the walk was truncated or hit an `all` sentinel.

    Note this deliberately does *not* require a `vol.Required` resource field.
    `call_service` declares `target` as optional, so such a rule would deny every
    control action. `required_fields` is accepted for callers that already
    computed it, but only its emptiness is informative and it is not consulted.
    """
    if extracted.unbounded or extracted.truncated or extracted.templated:
        return False
    return not extracted.is_empty()
