"""Tests for granting a role whatever its dashboards show.

The point of resolving this when a request is judged, rather than when the role
is saved, is that editing a dashboard changes who can see what. A snapshot
taken at save time would drift out of step silently, which is the failure these
tests exist to prevent.
"""

from homeassistant.auth.permissions.const import (
    CAT_ENTITIES,
    POLICY_CONTROL,
    POLICY_READ,
    SUBCAT_ALL,
)
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_rbac.policy import Permissions, compile_role


def _lookup(hass: HomeAssistant) -> PermissionLookup:
    return PermissionLookup(er.async_get(hass), dr.async_get(hass))


def _role(hass: HomeAssistant, levels: dict, shows, deny=None):
    """Compile a role granting nothing on its own, plus dashboard levels."""
    return compile_role(
        hass,
        {
            "id": "r",
            "name": "R",
            "allow": {},
            "deny": deny or {},
            "apps": {"allow": [], "deny": [], "dashboards": levels},
        },
        _lookup(hass),
        shows,
    )


async def test_a_dashboard_can_be_opened_without_carrying_its_contents(
    hass: HomeAssistant,
) -> None:
    """The screen opens, and shows only what the role is allowed elsewhere."""
    role = _role(hass, {"guest": "empty"}, lambda _p: {"light.kitchen"})
    assert role.check("light.kitchen", POLICY_READ) is False


async def test_content_grants_reading_what_is_on_it(hass: HomeAssistant) -> None:
    """And only reading."""
    role = _role(hass, {"guest": "content"}, lambda _p: {"light.kitchen"})
    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("light.kitchen", POLICY_CONTROL) is False
    assert role.check("light.hallway", POLICY_READ) is False


async def test_control_grants_both(hass: HomeAssistant) -> None:
    """The deepest level, for a dashboard someone is meant to actually use."""
    role = _role(hass, {"guest": "control"}, lambda _p: {"light.kitchen"})
    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("light.kitchen", POLICY_CONTROL) is True


async def test_editing_the_dashboard_changes_what_the_role_sees(
    hass: HomeAssistant,
) -> None:
    """The reason this resolves on demand rather than at save time.

    Nothing about the role changes here. The dashboard does, and the answer
    follows it.
    """
    shown = {"light.kitchen"}
    role = _role(hass, {"guest": "content"}, lambda _p: shown)

    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("lock.front", POLICY_READ) is False

    shown.add("lock.front")
    assert role.check("lock.front", POLICY_READ) is True, (
        "an entity added to the dashboard is covered without touching the role"
    )

    shown.discard("light.kitchen")
    assert role.check("light.kitchen", POLICY_READ) is False, (
        "and one taken off it stops being covered"
    )


async def test_a_denial_still_beats_a_dashboard(hass: HomeAssistant) -> None:
    """Putting a locked-down entity on a dashboard must not unlock it.

    Otherwise anyone who can edit a dashboard can grant themselves whatever the
    role was written to withhold.
    """
    role = _role(
        hass,
        {"guest": "control"},
        lambda _p: {"lock.front"},
        deny={CAT_ENTITIES: {"entity_ids": {"lock.front": True}}},
    )
    assert role.check("lock.front", POLICY_READ) is False
    assert role.check("lock.front", POLICY_CONTROL) is False


async def test_only_the_named_dashboards_count(hass: HomeAssistant) -> None:
    """A dashboard the role does not hold grants nothing, whatever is on it."""
    shows = {"held": {"light.kitchen"}, "other": {"lock.front"}}
    role = _role(hass, {"held": "content"}, lambda path: shows.get(path, set()))
    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("lock.front", POLICY_READ) is False


async def test_a_role_granting_a_dashboard_is_not_unrestricted(
    hass: HomeAssistant,
) -> None:
    """Or the proxy would skip the filtering that makes the grant mean anything."""
    role = compile_role(
        hass,
        {
            "id": "r",
            "name": "R",
            "allow": {CAT_ENTITIES: True},
            "tiers": {"max": "admin", "allow": ["*"], "deny": []},
            "apps": {"allow": [], "deny": [], "dashboards": {"guest": "content"}},
        },
        _lookup(hass),
        lambda _p: set(),
    )
    assert role.full_access is False


async def test_permissions_combine_dashboard_grants_across_roles(
    hass: HomeAssistant,
) -> None:
    """Holding two roles means holding what either of them grants."""
    lights = _role(
        hass, {"a": "content"}, lambda p: {"light.kitchen"} if p == "a" else set()
    )
    locks = _role(
        hass, {"b": "control"}, lambda p: {"lock.front"} if p == "b" else set()
    )
    permissions = Permissions(roles=[lights, locks])
    assert permissions.check_entity("light.kitchen", POLICY_READ) is True
    assert permissions.check_entity("lock.front", POLICY_CONTROL) is True
    assert permissions.check_entity("light.kitchen", POLICY_CONTROL) is False


async def test_without_a_lookup_a_dashboard_grants_nothing(
    hass: HomeAssistant,
) -> None:
    """Compiled outside the integration, there is nothing to resolve against."""
    role = compile_role(
        hass,
        {
            "id": "r",
            "name": "R",
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {}}},
            "apps": {"allow": [], "deny": [], "dashboards": {"guest": "control"}},
        },
        _lookup(hass),
    )
    assert role.check("light.kitchen", POLICY_READ) is False
