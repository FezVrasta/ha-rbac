"""Role model and permission evaluation.

Home Assistant's own policy compiler is reused rather than forked, but it cannot
express denial: `compile_policy` maps any bool -- including `False` -- to
allow-all, and `SINGLE_ENTITY_SCHEMA` accepts only the literal `True`. So each
role compiles *two* policies, an allow and a deny, and the deny vetoes.

Deny is deliberately coarse: denying a domain denies everything in it, and a
narrower deny entry cannot rescue one member. HA's `apply_policy_funcs` reads an
empty entry as "no opinion" and falls through to the broader rule, and there is
no way to express False to stop it. Since the allow side is default-deny, a
carve-out is written there instead -- grant the one entity rather than deny the
domain and punch a hole in it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatch
from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import (
    CAT_ENTITIES,
    POLICY_CONTROL,
    POLICY_READ,
    SUBCAT_ALL,
)
from homeassistant.auth.permissions.entities import (
    ENTITY_AREAS,
    ENTITY_DEVICE_IDS,
    ENTITY_DOMAINS,
    ENTITY_ENTITY_IDS,
    compile_entities,
)
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.auth.permissions.types import PolicyType
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.util import dt as dt_util

from .const import (
    CAPABILITY_PATTERNS,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_READ_ONLY,
    ROLE_USER,
    TIER_ADMIN,
    TIER_OPEN,
    TIER_ORDER,
    TIER_USER,
)

# The one place a command has to be named. Each of these defeats the rest of the
# layer outright, and none of it is derivable from anything Home Assistant
# exposes -- no decorator, schema or naming convention distinguishes them.
#
# `auth/sign_path` mints a URL that authenticates on its own, with no
# Authorization header and no path restriction, so a signed request arrives
# without a user and skips every gate. The intent commands take free text --
# "unlock the front door" -- so nothing in the payload names the lock.
#
# Applied to every role that is not full access, not just the predefined ones:
# leaving it to role data meant any role created in the panel was exposed, which
# is every role that matters.
BASELINE_DENY = (
    "auth/sign_path",
    "conversation/*",
    "assist_pipeline/*",
    "assist_satellite/*",
)
ACTS_ON_INTENT = BASELINE_DENY

SUBCAT_LABELS = "label_ids"
SUBCAT_FLOORS = "floor_ids"

# Labels and floors are desugared into the categories HA's compiler understands,
# so its evaluator stays the single source of truth.
EXTENDED_POLICY_SCHEMA = vol.Schema(
    {
        vol.Optional(CAT_ENTITIES): vol.Any(True, dict),
        vol.Optional(SUBCAT_LABELS): vol.Any(True, dict),
        vol.Optional(SUBCAT_FLOORS): vol.Any(True, dict),
    }
)


def _tier_ceiling(value: object) -> str:
    """Normalise a role's tier ceiling.

    `open` is not a usable ceiling. Every connection through the proxy is a
    signed-in user, and `auth/current_user` sits behind `ws_require_user`, so a
    role capped at `open` cannot start a frontend at all. It is accepted and
    raised rather than rejected, so roles stored before this was understood keep
    working.
    """
    if not isinstance(value, str) or value not in TIER_ORDER:
        return TIER_USER
    return TIER_USER if value == TIER_OPEN else value


TIERS_SCHEMA = vol.Schema(
    {
        vol.Optional("max", default=TIER_USER): vol.All(
            vol.In(TIER_ORDER), _tier_ceiling
        ),
        vol.Optional("allow", default=list): [str],
        vol.Optional("deny", default=list): [str],
    }
)

# Each rule withholds some attributes from some entities. `target` names the
# kind of thing `ids` are -- the same kinds an entity exception uses -- and an
# empty `ids` means every entity the role can see.
ATTRIBUTE_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional("target", default=ENTITY_DOMAINS): str,
        vol.Optional("ids", default=list): [str],
        vol.Required("names"): [str],
    }
)

ATTRIBUTES_SCHEMA = vol.Schema(
    {
        vol.Optional("rules", default=list): [ATTRIBUTE_RULE_SCHEMA],
        # The original shape: names withheld from every entity. Kept so roles
        # written before rules existed keep working, and read as one rule with
        # no target.
        vol.Optional("deny", default=list): [str],
    }
)

# Monday first, matching `datetime.weekday()`.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# One window: days, a time range, or both. Empty days means every day and empty
# times mean all day, so a window with nothing set is simply "always".
SCHEDULE_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional("days", default=list): [vol.In(DAYS)],
        vol.Optional("start", default=""): str,
        vol.Optional("end", default=""): str,
    }
)

SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("rules", default=list): [SCHEDULE_RULE_SCHEMA],
        # The original shape, a single window written inline. Kept so roles
        # saved before there could be more than one keep working, and read as
        # one entry in the list.
        vol.Optional("days", default=list): [vol.In(DAYS)],
        vol.Optional("start", default=""): str,
        vol.Optional("end", default=""): str,
    }
)

LOCATION_SCHEMA = vol.Schema(
    {
        # Zone entity ids the bound user's person must be inside for the role to
        # apply, e.g. "zone.home". Empty means no location condition at all, the
        # same as an empty schedule.
        vol.Optional("zones", default=list): [str],
        # "in": in force only while inside one of the zones. "not_in": in force
        # only while inside none of them -- "this access, but only when away".
        vol.Optional("mode", default="in"): vol.In(("in", "not_in")),
    }
)

# How much of a dashboard a role gets. Absent means "empty": it can open the
# dashboard, and sees on it only what it is allowed elsewhere.
DASHBOARD_EMPTY = "empty"
DASHBOARD_CONTENT = "content"
DASHBOARD_CONTROL = "control"
DASHBOARD_LEVELS = (DASHBOARD_EMPTY, DASHBOARD_CONTENT, DASHBOARD_CONTROL)

APPS_SCHEMA = vol.Schema(
    {
        vol.Optional("allow", default=list): [str],
        vol.Optional("deny", default=list): [str],
        vol.Optional("dashboards", default=dict): {str: vol.In(DASHBOARD_LEVELS)},
    }
)

ROLE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): str,
        vol.Required("name"): str,
        vol.Optional("description", default=""): str,
        vol.Optional("system_generated", default=False): bool,
        vol.Optional("allow", default=dict): EXTENDED_POLICY_SCHEMA,
        vol.Optional("deny", default=dict): EXTENDED_POLICY_SCHEMA,
        vol.Optional("tiers", default=dict): TIERS_SCHEMA,
        # Named parts of the administrative surface. An id this build does not
        # know is kept and ignored rather than rejected: it grants nothing, and
        # refusing the role outright would take away the access it still
        # describes correctly.
        vol.Optional("capabilities", default=list): [str],
        vol.Optional("apps", default=dict): APPS_SCHEMA,
        vol.Optional("attributes", default=dict): ATTRIBUTES_SCHEMA,
        vol.Optional("schedule", default=dict): SCHEDULE_SCHEMA,
        vol.Optional("location", default=dict): LOCATION_SCHEMA,
    }
)


def _minutes(value: Any) -> int | None:
    """Return "HH:MM" as minutes past midnight, or None if it is not a time."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def schedule_windows(schedule: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return a schedule's windows, including one written in the older shape."""
    if not schedule:
        return []
    windows = [dict(rule) for rule in schedule.get("rules") or []]
    if schedule.get("days") or schedule.get("start") or schedule.get("end"):
        windows.append(
            {
                "days": list(schedule.get("days") or []),
                "start": schedule.get("start") or "",
                "end": schedule.get("end") or "",
            }
        )
    return windows


def schedule_active(schedule: dict[str, Any] | None, now: datetime) -> bool:
    """Return True if any of a role's windows puts it in force at `now`.

    Several windows describe a shape one cannot: "Monday and Tuesday, ten until
    twelve and three until seven" is two entries, and a role is in force if it
    is inside any of them. No windows at all means always.
    """
    windows = schedule_windows(schedule)
    if not windows:
        return True
    return any(_window_active(window, now) for window in windows)


def _window_active(window: dict[str, Any], now: datetime) -> bool:
    """Return True if one window is open at `now`.

    A window whose end is before its start runs through midnight, and the day
    it belongs to is the day it *opened*: "Friday 22:00 to 02:00" is still in
    force at one on Saturday morning, and is not in force at one on Friday
    morning. Getting that backwards is the whole difficulty here.
    """
    days = window.get("days") or []
    start = _minutes(window.get("start"))
    end = _minutes(window.get("end"))
    minute_of_day = now.hour * 60 + now.minute

    if start is None and end is None:
        in_window, opened = True, now
    else:
        start = start or 0
        end = 0 if end is None else end
        if start == end:
            in_window, opened = True, now
        elif start < end:
            in_window, opened = start <= minute_of_day < end, now
        else:
            # Runs past midnight. Before the end is the tail of yesterday's.
            tail = minute_of_day < end
            in_window = minute_of_day >= start or tail
            opened = now - timedelta(days=1) if tail else now

    if not in_window:
        return False
    return not days or DAYS[opened.weekday()] in days


def _person_in_zone(hass: HomeAssistant, zone_id: str, person: State) -> bool:
    """Return True if the person is inside one zone, False on any doubt.

    Home Assistant's own zone condition does the work -- it prefers the
    `in_zones` a person reports over recomputing from coordinates, and handles
    the home/passive/radius rules. Imported lazily so the module does not pull in
    the automation stack at import time, and any error (unknown zone, no
    coordinates) is read as "not there" rather than propagated.
    """
    from homeassistant.components.zone.condition import (  # noqa: PLC0415
        zone as zone_condition,
    )

    try:
        return zone_condition(hass, zone_id, person)
    except Exception:  # noqa: BLE001 -- any failure means location unproven
        return False


def location_active(
    hass: HomeAssistant, location: dict[str, Any] | None, person: State | None
) -> bool:
    """Return True if a role's location condition is met right now.

    No zones means no condition, like an empty schedule. Otherwise the bound
    user's person must be inside one of the zones ("in") or inside none of them
    ("not_in"). If where they are cannot be established -- no person is linked to
    the user, or its state is unknown or unavailable -- a location-gated role
    grants nothing: it has to prove the condition holds, never assume it, so that
    losing track of someone can only take access away.

    That last check is what `not_in` needs to be safe. The zone condition answers
    False for a person it cannot place, which is indistinguishable from one it
    has placed outside every zone -- so inverting it turned "we have no idea
    where they are" into "they are provably away", and a tracker going offline
    silently handed out the access. An unplaceable person is refused before the
    inversion can reach it.

    A person Home Assistant reports as `not_home` is placed: that state means
    inside no zone at all, so `not_in` holding for them is the right answer.
    """
    zones = (location or {}).get("zones") or []
    if not zones:
        return True
    if person is None or person.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False
    inside = any(_person_in_zone(hass, zone_id, person) for zone_id in zones)
    if (location or {}).get("mode") == "not_in":
        return not inside
    return inside


def capability_patterns(capabilities: Any) -> list[str]:
    """Return the tier patterns a role's named capabilities stand for.

    Stored as names rather than as the globs they expand to, so a role follows
    the grouping as Home Assistant's command surface moves under it, and so the
    editor can show what was actually chosen rather than reverse-engineering it
    from a list of patterns.
    """
    if not isinstance(capabilities, list):
        return []
    return [
        pattern
        for name in capabilities
        for pattern in CAPABILITY_PATTERNS.get(name, ())
    ]


def default_roles() -> dict[str, dict[str, Any]]:
    """Return the predefined roles.

    `user` deliberately diverges from HA's USER_POLICY, which is `{entities: True}`
    and therefore identical to ADMIN_POLICY. Withholding `edit` lets this layer
    enforce a key core never checks.
    """
    return {
        ROLE_ADMIN: {
            "id": ROLE_ADMIN,
            "name": "Administrator",
            "description": "Full access. The proxy does no filtering for this role.",
            "system_generated": True,
            "allow": {CAT_ENTITIES: True},
            "deny": {},
            "tiers": {"max": TIER_ADMIN, "allow": ["*"], "deny": []},
        },
        ROLE_EDITOR: {
            "id": ROLE_EDITOR,
            "name": "Editor",
            "description": (
                "Everything a User can do, plus building automations, scripts, "
                "scenes, dashboards and helpers. Not users, backups or "
                "integrations."
            ),
            "system_generated": True,
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {},
            "tiers": {"max": TIER_USER, "allow": [], "deny": list(ACTS_ON_INTENT)},
            "capabilities": [
                "automations",
                "scripts",
                "scenes",
                "dashboards",
                "helpers",
            ],
        },
        ROLE_USER: {
            "id": ROLE_USER,
            "name": "User",
            "description": "Read and control every entity; no administrative commands.",
            "system_generated": True,
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {},
            "tiers": {"max": TIER_USER, "allow": [], "deny": list(ACTS_ON_INTENT)},
        },
        ROLE_READ_ONLY: {
            "id": ROLE_READ_ONLY,
            "name": "Read only",
            "description": "Read every entity; control nothing.",
            "system_generated": True,
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            "deny": {},
            "tiers": {"max": TIER_USER, "allow": [], "deny": list(ACTS_ON_INTENT)},
        },
    }


def _entities_for_area(hass: HomeAssistant, area_id: str) -> set[str]:
    """Return every entity in an area.

    HA's own `_lookup_area` resolves entity -> device -> area and ignores
    `entity_entry.area_id`, so an entity assigned directly to an area whose
    device sits elsewhere resolves to the wrong area. Desugaring here avoids
    inheriting that.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # The registry indexes only entities carrying an explicit area_id.
    found = {entry.entity_id for entry in er.async_entries_for_area(ent_reg, area_id)}
    # Entities with no area of their own inherit their device's.
    for device in dr.async_entries_for_area(dev_reg, area_id):
        found.update(
            entry.entity_id
            for entry in er.async_entries_for_device(
                ent_reg, device.id, include_disabled_entities=True
            )
            if entry.area_id is None
        )
    return found


def _entities_for_label(hass: HomeAssistant, label_id: str) -> set[str]:
    """Return every entity carrying a label, directly or via its device."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    found = {entry.entity_id for entry in er.async_entries_for_label(ent_reg, label_id)}
    for device in dr.async_entries_for_label(dev_reg, label_id):
        found.update(
            entry.entity_id
            for entry in er.async_entries_for_device(
                ent_reg, device.id, include_disabled_entities=True
            )
        )
    return found


def _areas_for_floor(hass: HomeAssistant, floor_id: str) -> set[str]:
    """Return every area on a floor."""
    area_reg = ar.async_get(hass)
    return {area.id for area in ar.async_entries_for_floor(area_reg, floor_id)}


@callback
def desugar(hass: HomeAssistant, policy: dict[str, Any]) -> PolicyType:
    """Expand labels and floors into a policy HA's compiler understands.

    Floors become areas, areas and labels become entity ids. The result is a
    plain HA `PolicyType`, which is what keeps a role's `allow` block portable
    into `.storage/auth` if this layer is ever removed.
    """
    entities = policy.get(CAT_ENTITIES)
    if entities is True:
        return {CAT_ENTITIES: True}
    if not isinstance(entities, dict):
        entities = {}

    # `all: True` and `domains: True` are valid Home Assistant policy, so a
    # bare bool has to survive rather than being copied as if it were a mapping.
    out: dict[str, Any] = {}
    for key in (SUBCAT_ALL, ENTITY_DOMAINS, ENTITY_DEVICE_IDS, ENTITY_ENTITY_IDS):
        if key not in entities:
            continue
        value = entities[key]
        if isinstance(value, dict):
            out[key] = dict(value)
        elif isinstance(value, bool):
            out[key] = value

    existing_entities = out.get(ENTITY_ENTITY_IDS)
    explicit_entities: dict[str, Any] = (
        dict(existing_entities) if isinstance(existing_entities, dict) else {}
    )

    # Floors -> areas, merged with any areas named directly.
    areas: dict[str, Any] = dict(entities.get(ENTITY_AREAS, {}) or {})
    for floor_id, value in (policy.get(SUBCAT_FLOORS) or {}).items():
        for area_id in _areas_for_floor(hass, floor_id):
            areas.setdefault(area_id, value)

    # Areas -> entities, so the entity-level area_id override is honoured.
    for area_id, value in areas.items():
        for entity_id in _entities_for_area(hass, area_id):
            explicit_entities.setdefault(entity_id, value)

    # Labels -> entities.
    for label_id, value in (policy.get(SUBCAT_LABELS) or {}).items():
        for entity_id in _entities_for_label(hass, label_id):
            explicit_entities.setdefault(entity_id, value)

    if explicit_entities:
        out[ENTITY_ENTITY_IDS] = explicit_entities

    return {CAT_ENTITIES: out} if out else {}


# Home Assistant's own precedence, most specific first. `desugar` folds areas,
# labels and floors into entity ids, so `ENTITY_AREAS` cannot survive into a
# compiled policy; it is listed anyway so this stays a faithful mirror of
# `compile_entities` rather than something that has to be revisited if that
# changes.
_PRECEDENCE = (
    ENTITY_ENTITY_IDS,
    ENTITY_DEVICE_IDS,
    ENTITY_AREAS,
    ENTITY_DOMAINS,
    SUBCAT_ALL,
)
# The same, minus the baseline: the levels a role uses to name an *exception*.
_EXCEPTION_LEVELS = _PRECEDENCE[:-1]


def _named_at(level: str, value: Any) -> Any:
    """Reduce one level's grants to "does it name this entity at all".

    Which keys are granted is dropped, so the result answers matching alone.
    `SUBCAT_ALL` is not a mapping of targets -- the value *is* the grant -- so it
    matches everything or nothing.
    """
    if level == SUBCAT_ALL:
        return True
    if isinstance(value, dict):
        return dict.fromkeys(value, True)
    return value


def _levels(
    entities: dict[str, Any],
    perm_lookup: PermissionLookup,
    levels: tuple[str, ...],
) -> list[tuple[Callable[[str, str], bool], Callable[[str, str], bool]]]:
    """Compile each level on its own, as (names it, grants the key) pairs.

    Compiling one level at a time is the whole point: it is what removes Home
    Assistant's fall-through, because a policy with a single subcategory has
    nowhere to fall through to.
    """
    return [
        (
            compile_entities({level: _named_at(level, value)}, perm_lookup),
            compile_entities({level: value}, perm_lookup),
        )
        for level in levels
        if (value := entities.get(level)) is not None
    ]


def compile_capped_entities(
    entities: Any, perm_lookup: PermissionLookup
) -> Callable[[str, str], bool]:
    """Compile an allow policy where the most specific grant is the whole answer.

    Home Assistant reads a key missing from a matched grant as "no opinion" and
    keeps looking, so `{"read": True}` on one entity does not withhold the
    control a broader `all` grant hands out. Every narrowing exception in the
    editor was therefore silently doing nothing: a role whose baseline is read
    and control could still control the one entity marked read-only.

    Here the first level that names an entity settles every key for it, which is
    what an exception says it does. Widening is unaffected -- an entity granted
    read *and* control still gets both under a read-only baseline, because that
    level names it and grants the key.
    """
    if entities is True:
        return lambda entity_id, key: True
    if not isinstance(entities, dict) or not entities:
        return lambda entity_id, key: False

    levels = _levels(entities, perm_lookup, _PRECEDENCE)

    def apply_capped(entity_id: str, key: str) -> bool:
        for names, grants in levels:
            if names(entity_id, key):
                return grants(entity_id, key)
        return False

    return apply_capped


def compile_caps(
    entities: Any, perm_lookup: PermissionLookup
) -> Callable[[str, str], bool]:
    """Compile "an exception names this entity and withholds this key from it".

    Consulted so that a dashboard held at `control` cannot hand back what an
    exception took away. The baseline is deliberately not a level here: a
    read-only baseline with a dashboard the role is meant to operate is the
    feature working as intended, whereas an entity singled out as read-only is
    an instruction about that entity.
    """
    if entities is True or not isinstance(entities, dict) or not entities:
        return lambda entity_id, key: False

    levels = _levels(entities, perm_lookup, _EXCEPTION_LEVELS)

    def apply_caps(entity_id: str, key: str) -> bool:
        for names, grants in levels:
            if names(entity_id, key):
                return not grants(entity_id, key)
        return False

    return apply_caps


@dataclass(slots=True)
class CompiledAttributeRule:
    """Attribute names withheld from the entities a rule targets."""

    names: list[str]
    # None means every entity; otherwise the exact set this rule covers.
    entity_ids: set[str] | None
    domains: set[str] | None

    def covers(self, entity_id: str) -> bool:
        """Return True if this rule applies to an entity."""
        if self.entity_ids is None and self.domains is None:
            return True
        if self.domains is not None and entity_id.partition(".")[0] in self.domains:
            return True
        return self.entity_ids is not None and entity_id in self.entity_ids

    def hides(self, name: str) -> bool:
        """Return True if this rule withholds an attribute name."""
        return any(fnmatch(name, pattern) for pattern in self.names)


def _compile_attribute_rules(
    hass: HomeAssistant, attributes: dict[str, Any]
) -> list[CompiledAttributeRule]:
    """Turn a role's attribute section into matchers.

    Domains stay domains rather than being expanded, so a rule keeps covering
    entities that do not exist yet. Areas, labels and floors are resolved to
    entity ids the same way an entity policy resolves them.
    """
    compiled: list[CompiledAttributeRule] = []

    if legacy := list(attributes.get("deny") or []):
        compiled.append(CompiledAttributeRule(legacy, None, None))

    for rule in attributes.get("rules") or []:
        names = list(rule.get("names") or [])
        ids = list(rule.get("ids") or [])
        if not names:
            continue
        if not ids:
            compiled.append(CompiledAttributeRule(names, None, None))
            continue

        target = rule.get("target") or ENTITY_DOMAINS
        if target == ENTITY_DOMAINS:
            # Entity ids are lowercased everywhere else, so a domain written
            # `Light` in the panel has to match `light.kitchen` too.
            compiled.append(
                CompiledAttributeRule(names, None, {i.lower() for i in ids})
            )
            continue
        if target == ENTITY_ENTITY_IDS:
            compiled.append(
                CompiledAttributeRule(names, {i.lower() for i in ids}, None)
            )
            continue

        # Everything else resolves through the registries, reusing the same
        # expansion an entity policy uses.
        policy = desugar(hass, {CAT_ENTITIES: {target: dict.fromkeys(ids, True)}})
        resolved = set((policy.get(CAT_ENTITIES) or {}).get(ENTITY_ENTITY_IDS) or {})
        compiled.append(CompiledAttributeRule(names, resolved, None))

    return compiled


@dataclass(slots=True)
class CompiledRole:
    """A role with its allow and deny policies compiled."""

    role_id: str
    name: str
    allow_fn: Callable[[str, str], bool]
    deny_fn: Callable[[str, str], bool]
    # "an exception names this entity and withholds this key", which a dashboard
    # grant must not undo.
    cap_fn: Callable[[str, str], bool]
    tier_max: str
    tier_allow: list[str]
    tier_deny: list[str]
    app_allow: list[str]
    app_deny: list[str]
    attribute_rules: "list[CompiledAttributeRule]"
    schedule: dict[str, Any]
    location: dict[str, Any]
    # url_path -> level, for dashboards this role gets the contents of.
    dashboard_levels: dict[str, str]
    # Resolved when asked rather than when the role was saved, so editing a
    # dashboard changes who can see what without anyone reopening the role.
    dashboard_entities: "Callable[[str], set[str]] | None"
    full_access: bool

    def active_at(self, now: datetime) -> bool:
        """Return True if this role is in force at `now`."""
        return schedule_active(self.schedule, now)

    def check(self, entity_id: str, key: str) -> bool:
        """Return True if this role permits `key` access to an entity."""
        if self.deny_fn(entity_id, key):
            return False
        if self.allow_fn(entity_id, key):
            return True
        if self.cap_fn(entity_id, key):
            return False
        return self._granted_by_a_dashboard(entity_id, key)

    def _granted_by_a_dashboard(self, entity_id: str, key: str) -> bool:
        """Return True if a dashboard this role gets the contents of shows it.

        A denial still wins, which is checked before this: naming an entity a
        role must not see should not be undone by someone putting it on a
        dashboard the role happens to hold. So does an exception that caps an
        entity below the level asked for -- a garage door marked read-only is
        read-only wherever it is drawn.
        """
        if self.dashboard_entities is None:
            return False
        for url_path, level in self.dashboard_levels.items():
            if level == DASHBOARD_EMPTY:
                continue
            if key == POLICY_CONTROL and level != DASHBOARD_CONTROL:
                continue
            if entity_id in self.dashboard_entities(url_path):
                return True
        return False


def compile_role(
    hass: HomeAssistant,
    role: dict[str, Any],
    perm_lookup: PermissionLookup,
    dashboard_entities: "Callable[[str], set[str]] | None" = None,
) -> CompiledRole:
    """Compile one role into a pair of policy functions."""
    allow_policy = desugar(hass, role.get("allow") or {})
    deny_policy = desugar(hass, role.get("deny") or {})

    tiers = role.get("tiers") or {}
    apps = role.get("apps") or {}
    attributes = role.get("attributes") or {}
    tier_max = _tier_ceiling(tiers.get("max"))
    tier_allow = [
        *capability_patterns(role.get("capabilities")),
        *(tiers.get("allow") or []),
    ]
    attribute_rules = _compile_attribute_rules(hass, attributes)

    return CompiledRole(
        role_id=role["id"],
        name=role.get("name", role["id"]),
        allow_fn=compile_capped_entities(allow_policy.get(CAT_ENTITIES), perm_lookup),
        deny_fn=compile_entities(deny_policy.get(CAT_ENTITIES), perm_lookup),
        cap_fn=compile_caps(allow_policy.get(CAT_ENTITIES), perm_lookup),
        tier_max=tier_max,
        tier_allow=tier_allow,
        tier_deny=[*BASELINE_DENY, *(tiers.get("deny") or [])],
        app_allow=list(apps.get("allow") or []),
        app_deny=list(apps.get("deny") or []),
        attribute_rules=attribute_rules,
        schedule=dict(role.get("schedule") or {}),
        location=dict(role.get("location") or {}),
        dashboard_levels={
            url_path: level
            for url_path, level in (apps.get("dashboards") or {}).items()
            if level != DASHBOARD_EMPTY
        },
        dashboard_entities=dashboard_entities,
        # Full access skips every gate, so it has to mean *nothing* is
        # restricted. Ignoring the tier denials here silently disabled the whole
        # layer for the obvious authoring flow of cloning Administrator and
        # denying one namespace. The compiled rules are consulted rather than
        # the raw `deny` list, so the targeted `rules` form counts too, and an
        # app *allow* list restricts just as much as a deny list does.
        full_access=(
            allow_policy.get(CAT_ENTITIES) is True
            and not (role.get("deny") or {})
            and tier_max == TIER_ADMIN
            and "*" in tier_allow
            and not tiers.get("deny")
            and not apps.get("deny")
            and not apps.get("allow")
            and not (apps.get("dashboards") or {})
            and not attribute_rules
        ),
    )


@dataclass(slots=True)
class Permissions:
    """The effective permissions of one user: the union of their roles."""

    roles: list[CompiledRole] = field(default_factory=list)
    global_deny_fn: Callable[[str, str], bool] | None = None
    pass_through: bool = False

    def check_entity(self, entity_id: str, key: str) -> bool:
        """Return True if any role permits access and no global deny vetoes."""
        if self.pass_through:
            return True
        if self.global_deny_fn is not None and self.global_deny_fn(entity_id, key):
            return False
        return any(role.check(entity_id, key) for role in self.roles)

    def tier_allowed(self, command: str, tier: str) -> bool:
        """Return True if any role permits a command at the given tier.

        Explicit globs win over the ranked comparison: a deny glob in any role
        vetoes, then an allow glob in any role grants, then the tier ranking.
        """
        if self.pass_through:
            return True
        if any(
            fnmatch(command, pattern)
            for role in self.roles
            for pattern in role.tier_deny
        ):
            return False
        if any(
            fnmatch(command, pattern)
            for role in self.roles
            for pattern in role.tier_allow
        ):
            return True
        rank = TIER_ORDER.index(tier)
        return any(TIER_ORDER.index(role.tier_max) >= rank for role in self.roles)

    def attribute_hidden(self, entity_id: str, name: str) -> bool:
        """Return True if an attribute must be withheld from this entity.

        Rules are targeted: hiding `latitude` on people does not hide it on the
        zones that define where home is. A rule with no target still covers
        everything, which is what an untargeted rule means.
        """
        if self.pass_through:
            return False
        return any(
            rule.covers(entity_id) and rule.hides(name)
            for role in self.roles
            for rule in role.attribute_rules
        )

    @property
    def hides_attributes(self) -> bool:
        """Return True if any role withholds attributes."""
        return not self.pass_through and any(
            role.attribute_rules for role in self.roles
        )

    def app_allowed(self, url_path: str) -> bool:
        """Return True if any role permits this app.

        Denials win, then an explicit allow list, then the default of allowing
        whatever the tier gate already lets through.
        """
        if self.pass_through:
            return True
        if any(
            fnmatch(url_path, pattern)
            for role in self.roles
            for pattern in role.app_deny
        ):
            return False
        allow_lists = [role.app_allow for role in self.roles if role.app_allow]
        if not allow_lists:
            return True
        return any(
            fnmatch(url_path, pattern)
            for patterns in allow_lists
            for pattern in patterns
        )

    @property
    def full_access(self) -> bool:
        """Return True if filtering can be skipped entirely for this user."""
        return self.pass_through or (
            self.global_deny_fn is None and any(role.full_access for role in self.roles)
        )


class Evaluator:
    """Resolves users to permissions, with caching keyed on user id."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: Any,
        dashboard_entities: "Callable[[str], set[str]] | None" = None,
    ) -> None:
        """Initialise the evaluator."""
        self._hass = hass
        self._store = store
        self._dashboard_entities = dashboard_entities
        self._compiled: dict[str, CompiledRole] = {}
        # Keyed on the user and on which of their roles are currently in force,
        # so a schedule opening or closing produces a different key rather than
        # a stale hit. Keying on the user alone would have frozen the first
        # answer of the day for the rest of it.
        self._cache: dict[tuple[str, tuple[str, ...]], Permissions] = {}
        self._perm_lookup = PermissionLookup(er.async_get(hass), dr.async_get(hass))

    @callback
    def invalidate(self, _event: Any = None) -> None:
        """Drop compiled roles and per-user permissions.

        Called on store writes and on registry updates, because a role that
        names an area or label desugars to concrete entity ids at compile time.
        """
        self._compiled.clear()
        self._cache.clear()

    @callback
    def _person_state_for(self, user: Any) -> State | None:
        """Return the person entity tracking this user, if there is one.

        Home Assistant links a person to a user through the person's `user_id`
        attribute, so the person's state -- the zone it is in -- stands in for
        where the user is. No person, or more than one and none conclusive, means
        the location is unknown, which a location-gated role treats as "not
        there".
        """
        for state in self._hass.states.async_all("person"):
            if state.attributes.get("user_id") == user.id:
                return state
        return None

    def _compiled_role(self, role_id: str) -> CompiledRole | None:
        """Return a role, compiling it on first use."""
        if (compiled := self._compiled.get(role_id)) is not None:
            return compiled
        if (role := self._store.roles.get(role_id)) is None:
            return None
        compiled = compile_role(
            self._hass, role, self._perm_lookup, self._dashboard_entities
        )
        self._compiled[role_id] = compiled
        return compiled

    @callback
    def async_permissions(self, user: Any) -> Permissions:
        """Return the effective permissions for a user.

        The owner and system-generated users are always pass-through. This is in
        code rather than in data so that it cannot be edited away from the UI,
        and it is the lockout escape hatch.
        """
        if user is None:
            return Permissions()
        if user.is_owner or user.system_generated:
            return Permissions(pass_through=True)

        role_ids = self._store.bindings.get(user.id)
        if not role_ids:
            # Unbound users fall back to their existing HA group, so installing
            # the integration changes no behaviour until roles are assigned.
            role_ids = [ROLE_ADMIN if user.is_admin else ROLE_USER]

        now = dt_util.now()
        candidates = [
            compiled
            for role_id in role_ids
            if (compiled := self._compiled_role(role_id)) is not None
            and compiled.active_at(now)
        ]
        # Where the user is only matters if some still-in-hours role asks, so the
        # person is looked up only then -- most roles carry no location and pay
        # nothing for the feature.
        person = (
            self._person_state_for(user)
            if any(role.location.get("zones") for role in candidates)
            else None
        )
        roles = [
            role
            for role in candidates
            if location_active(self._hass, role.location, person)
        ]
        # A role that is out of hours, or whose holder is outside the zone it is
        # tied to, is simply not held. If that leaves none, the user has no
        # permissions rather than falling back to the Home Assistant group they
        # would have had unbound: a condition on a role has to grant less, never
        # more. The cache is keyed on which roles are in force, so a schedule
        # opening or someone crossing a zone boundary makes a fresh key rather
        # than a stale hit.
        key = (user.id, tuple(role.role_id for role in roles))
        if (cached := self._cache.get(key)) is not None:
            return cached

        global_deny_fn = None
        if raw_deny := self._store.global_deny.get(user.id):
            deny_policy = desugar(self._hass, raw_deny)
            global_deny_fn = compile_entities(
                deny_policy.get(CAT_ENTITIES), self._perm_lookup
            )

        permissions = Permissions(roles=roles, global_deny_fn=global_deny_fn)
        self._cache[key] = permissions
        return permissions
