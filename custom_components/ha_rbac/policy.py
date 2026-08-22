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

from .const import (
    ROLE_ADMIN,
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

ROLE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): str,
        vol.Required("name"): str,
        vol.Optional("description", default=""): str,
        vol.Optional("system_generated", default=False): bool,
        vol.Optional("allow", default=dict): EXTENDED_POLICY_SCHEMA,
        vol.Optional("deny", default=dict): EXTENDED_POLICY_SCHEMA,
        vol.Optional("tiers", default=dict): TIERS_SCHEMA,
    }
)


def default_roles() -> dict[str, dict[str, Any]]:
    """Return the three predefined roles.

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


@dataclass(slots=True)
class CompiledRole:
    """A role with its allow and deny policies compiled."""

    role_id: str
    name: str
    allow_fn: Callable[[str, str], bool]
    deny_fn: Callable[[str, str], bool]
    tier_max: str
    tier_allow: list[str]
    tier_deny: list[str]
    full_access: bool

    def check(self, entity_id: str, key: str) -> bool:
        """Return True if this role permits `key` access to an entity."""
        return self.allow_fn(entity_id, key) and not self.deny_fn(entity_id, key)


def compile_role(
    hass: HomeAssistant, role: dict[str, Any], perm_lookup: PermissionLookup
) -> CompiledRole:
    """Compile one role into a pair of policy functions."""
    allow_policy = desugar(hass, role.get("allow") or {})
    deny_policy = desugar(hass, role.get("deny") or {})

    tiers = role.get("tiers") or {}
    tier_max = _tier_ceiling(tiers.get("max"))
    tier_allow = list(tiers.get("allow") or [])

    return CompiledRole(
        role_id=role["id"],
        name=role.get("name", role["id"]),
        allow_fn=compile_entities(allow_policy.get(CAT_ENTITIES), perm_lookup),
        deny_fn=compile_entities(deny_policy.get(CAT_ENTITIES), perm_lookup),
        tier_max=tier_max,
        tier_allow=tier_allow,
        tier_deny=[*BASELINE_DENY, *(tiers.get("deny") or [])],
        # Full access skips every gate, so it has to mean *nothing* is
        # restricted. Ignoring the tier denials here silently disabled the whole
        # layer for the obvious authoring flow of cloning Administrator and
        # denying one namespace.
        full_access=(
            allow_policy.get(CAT_ENTITIES) is True
            and not (role.get("deny") or {})
            and tier_max == TIER_ADMIN
            and "*" in tier_allow
            and not tiers.get("deny")
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

    @property
    def full_access(self) -> bool:
        """Return True if filtering can be skipped entirely for this user."""
        return self.pass_through or (
            self.global_deny_fn is None and any(role.full_access for role in self.roles)
        )


class Evaluator:
    """Resolves users to permissions, with caching keyed on user id."""

    def __init__(self, hass: HomeAssistant, store: Any) -> None:
        """Initialise the evaluator."""
        self._hass = hass
        self._store = store
        self._compiled: dict[str, CompiledRole] = {}
        self._cache: dict[str, Permissions] = {}
        self._perm_lookup = PermissionLookup(er.async_get(hass), dr.async_get(hass))

    @callback
    def invalidate(self, _event: Any = None) -> None:
        """Drop compiled roles and per-user permissions.

        Called on store writes and on registry updates, because a role that
        names an area or label desugars to concrete entity ids at compile time.
        """
        self._compiled.clear()
        self._cache.clear()

    def _compiled_role(self, role_id: str) -> CompiledRole | None:
        """Return a role, compiling it on first use."""
        if (compiled := self._compiled.get(role_id)) is not None:
            return compiled
        if (role := self._store.roles.get(role_id)) is None:
            return None
        compiled = compile_role(self._hass, role, self._perm_lookup)
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

        if (cached := self._cache.get(user.id)) is not None:
            return cached

        role_ids = self._store.bindings.get(user.id)
        if not role_ids:
            # Unbound users fall back to their existing HA group, so installing
            # the integration changes no behaviour until roles are assigned.
            role_ids = [ROLE_ADMIN if user.is_admin else ROLE_USER]

        roles = [
            compiled
            for role_id in role_ids
            if (compiled := self._compiled_role(role_id)) is not None
        ]

        global_deny_fn = None
        if raw_deny := self._store.global_deny.get(user.id):
            deny_policy = desugar(self._hass, raw_deny)
            global_deny_fn = compile_entities(
                deny_policy.get(CAT_ENTITIES), self._perm_lookup
            )

        permissions = Permissions(roles=roles, global_deny_fn=global_deny_fn)
        self._cache[user.id] = permissions
        return permissions
