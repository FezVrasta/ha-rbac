"""Tests for location-limited roles.

A role can be tied to one or more zones, and it is held only while the bound
user's person is inside them (or, in `not_in` mode, only while outside them).
Where the user cannot be located at all, a location-gated role grants nothing:
it has to prove the condition, never assume it, so losing track of someone can
only take access away.
"""

import pytest
from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_READ, SUBCAT_ALL
from homeassistant.core import HomeAssistant

from custom_components.ha_rbac.const import TIER_OPEN
from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Evaluator,
    location_active,
)

# A zone and two points: one inside it, one a long way off.
HOME = {"latitude": 52.0, "longitude": 4.0, "radius": 100, "passive": False}
INSIDE = {"latitude": 52.0, "longitude": 4.0, "gps_accuracy": 0}
AWAY = {"latitude": 53.0, "longitude": 5.0, "gps_accuracy": 0}


@pytest.fixture(name="home_zone", autouse=True)
def home_zone_fixture(hass: HomeAssistant) -> None:
    """Put a zone.home on the map for the condition to test against."""
    hass.states.async_set("zone.home", "zoning", HOME)


def _person(hass: HomeAssistant, attrs: dict) -> None:
    hass.states.async_set("person.alice", "home", {"user_id": "u1", **attrs})


async def test_no_zones_is_no_condition(hass: HomeAssistant) -> None:
    """An empty location block is always in force, like an empty schedule."""
    assert location_active(hass, None, None) is True
    assert location_active(hass, {}, None) is True
    assert location_active(hass, {"zones": []}, None) is True


async def test_unknown_location_grants_nothing(hass: HomeAssistant) -> None:
    """No person, or no location on it, must not satisfy a zone condition."""
    assert location_active(hass, {"zones": ["zone.home"]}, None) is False


async def test_inside_the_zone_is_in_force(hass: HomeAssistant) -> None:
    """The default mode holds the role only while inside a listed zone."""
    _person(hass, INSIDE)
    person = hass.states.get("person.alice")
    assert location_active(hass, {"zones": ["zone.home"]}, person) is True


async def test_outside_the_zone_is_not_in_force(hass: HomeAssistant) -> None:
    """Leaving the zone drops the role."""
    _person(hass, AWAY)
    person = hass.states.get("person.alice")
    assert location_active(hass, {"zones": ["zone.home"]}, person) is False


async def test_not_in_mode_inverts_it(hass: HomeAssistant) -> None:
    """`not_in` grants the role only while outside every listed zone."""
    _person(hass, AWAY)
    away = hass.states.get("person.alice")
    assert (
        location_active(hass, {"zones": ["zone.home"], "mode": "not_in"}, away) is True
    )

    _person(hass, INSIDE)
    home = hass.states.get("person.alice")
    assert (
        location_active(hass, {"zones": ["zone.home"], "mode": "not_in"}, home) is False
    )


def test_the_schema_defaults_and_keeps_a_location() -> None:
    """An absent location reads as no condition; a written one round-trips."""
    assert ROLE_SCHEMA({"id": "r", "name": "R"})["location"] == {
        "zones": [],
        "mode": "in",
    }
    validated = ROLE_SCHEMA(
        {"id": "r", "name": "R", "location": {"zones": ["zone.home"], "mode": "not_in"}}
    )
    assert validated["location"] == {"zones": ["zone.home"], "mode": "not_in"}


class _Store:
    """The pieces of the store the evaluator reads."""

    def __init__(self, roles: dict, bindings: dict) -> None:
        self.roles = roles
        self.bindings = bindings
        self.global_deny: dict = {}


class _User:
    id = "u1"
    is_owner = False
    system_generated = False
    is_admin = True


async def test_the_evaluator_follows_the_user_across_the_boundary(
    hass: HomeAssistant,
) -> None:
    """A role tied to home is dropped when its holder leaves and picked back up.

    The cache is keyed on which roles are in force, so crossing the boundary
    makes a fresh key rather than serving the earlier answer.
    """
    store = _Store(
        roles={
            "home_only": {
                "id": "home_only",
                "name": "Home only",
                "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
                "location": {"zones": ["zone.home"], "mode": "in"},
            }
        },
        bindings={"u1": ["home_only"]},
    )
    evaluator = Evaluator(hass, store)
    user = _User()

    _person(hass, INSIDE)
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is True
    )

    _person(hass, AWAY)
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is False
    ), "leaving the zone must drop the role, not serve the cached answer"

    _person(hass, INSIDE)
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is True
    )


async def test_an_expired_location_role_does_not_free_an_admin(
    hass: HomeAssistant,
) -> None:
    """Leaving the zone must grant less, never fall back to the admin group."""
    store = _Store(
        roles={
            "home_only": {
                "id": "home_only",
                "name": "Home only",
                "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
                "location": {"zones": ["zone.home"], "mode": "in"},
            }
        },
        bindings={"u1": ["home_only"]},
    )
    evaluator = Evaluator(hass, store)
    user = _User()

    _person(hass, AWAY)
    permissions = evaluator.async_permissions(user)
    assert permissions.check_entity("light.a", POLICY_READ) is False
    assert permissions.full_access is False
