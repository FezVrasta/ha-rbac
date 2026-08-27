"""Tests for an exception that narrows what the baseline hands out.

Home Assistant's own compiler reads a key missing from a matched grant as "no
opinion" and falls through to the broader one, so `{"read": True}` on a single
entity withheld nothing from a role whose baseline was read and control. The
editor offered "Read" per entity and it silently did nothing -- a garage door
marked read-only could still be opened. These tests pin the narrowing direction
in both places it can be undone: the baseline, and a dashboard held at control.
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

from custom_components.ha_rbac.policy import compile_role

GARAGE = "cover.garage_door"
BLINDS = "cover.living_room_blinds"

# What the panel writes for each access level.
READ = {POLICY_READ: True}
CONTROL = {POLICY_READ: True, POLICY_CONTROL: True}


def _role(hass: HomeAssistant, allow, deny=None, dashboards=None, shows=None):
    return compile_role(
        hass,
        {
            "id": "kids",
            "name": "Kids",
            "allow": {CAT_ENTITIES: allow},
            "deny": deny or {},
            "apps": {"allow": [], "deny": [], "dashboards": dashboards or {}},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
        shows,
    )


async def test_read_only_caps_an_entity_the_baseline_would_control(
    hass: HomeAssistant,
) -> None:
    """The reported failure: read-only on the garage door, everything else free."""
    role = _role(hass, {SUBCAT_ALL: CONTROL, "entity_ids": {GARAGE: READ}})

    assert role.check(GARAGE, POLICY_READ) is True
    assert role.check(GARAGE, POLICY_CONTROL) is False
    # Everything not singled out still follows the baseline.
    assert role.check(BLINDS, POLICY_CONTROL) is True


async def test_a_control_dashboard_does_not_lift_a_cap(hass: HomeAssistant) -> None:
    """Marked read-only, and drawn on a dashboard the role may operate."""
    role = _role(
        hass,
        {SUBCAT_ALL: CONTROL, "entity_ids": {GARAGE: READ}},
        dashboards={"kids": "control"},
        shows=lambda _p: {GARAGE, BLINDS},
    )

    assert role.check(GARAGE, POLICY_READ) is True
    assert role.check(GARAGE, POLICY_CONTROL) is False
    assert role.check(BLINDS, POLICY_CONTROL) is True


async def test_a_domain_can_be_capped(hass: HomeAssistant) -> None:
    """The same narrowing, expressed over a whole domain."""
    role = _role(hass, {SUBCAT_ALL: CONTROL, "domains": {"cover": READ}})

    assert role.check(GARAGE, POLICY_READ) is True
    assert role.check(GARAGE, POLICY_CONTROL) is False
    assert role.check("light.playroom", POLICY_CONTROL) is True


async def test_the_most_specific_exception_wins(hass: HomeAssistant) -> None:
    """An entity named outright beats the domain it belongs to, either way round."""
    role = _role(
        hass,
        {
            SUBCAT_ALL: CONTROL,
            "domains": {"cover": READ},
            "entity_ids": {BLINDS: CONTROL},
        },
    )

    # Named outright as controllable, against a read-only domain.
    assert role.check(BLINDS, POLICY_CONTROL) is True
    # And the rest of the domain stays capped.
    assert role.check(GARAGE, POLICY_CONTROL) is False


async def test_widening_still_works(hass: HomeAssistant) -> None:
    """An exception that grants more than the baseline is unaffected."""
    role = _role(hass, {SUBCAT_ALL: READ, "entity_ids": {BLINDS: CONTROL}})

    assert role.check(BLINDS, POLICY_CONTROL) is True
    assert role.check(GARAGE, POLICY_CONTROL) is False
    assert role.check(GARAGE, POLICY_READ) is True


async def test_a_read_only_baseline_still_takes_a_control_dashboard(
    hass: HomeAssistant,
) -> None:
    """The dashboard feature itself is untouched.

    A baseline is not an instruction about any particular entity, so a dashboard
    the role is meant to operate still grants control on what it shows. Only an
    exception naming an entity outranks it.
    """
    role = _role(
        hass,
        {SUBCAT_ALL: READ},
        dashboards={"kids": "control"},
        shows=lambda _p: {BLINDS},
    )

    assert role.check(BLINDS, POLICY_CONTROL) is True


async def test_no_access_still_beats_everything(hass: HomeAssistant) -> None:
    """The pre-existing denial path is unchanged."""
    role = _role(
        hass,
        {SUBCAT_ALL: CONTROL},
        deny={CAT_ENTITIES: {"entity_ids": {GARAGE: True}}},
        dashboards={"kids": "control"},
        shows=lambda _p: {GARAGE},
    )

    assert role.check(GARAGE, POLICY_READ) is False
    assert role.check(GARAGE, POLICY_CONTROL) is False
