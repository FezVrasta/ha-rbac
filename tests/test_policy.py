"""Tests for role compilation and permission evaluation."""

from typing import Any

import pytest
from homeassistant.auth.permissions.const import (
    CAT_ENTITIES,
    POLICY_CONTROL,
    POLICY_READ,
    SUBCAT_ALL,
)
from homeassistant.auth.permissions.entities import compile_entities
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from tests.common import MockConfigEntry

from custom_components.ha_rbac.const import TIER_ADMIN, TIER_OPEN, TIER_USER
from custom_components.ha_rbac.policy import (
    Permissions,
    compile_role,
    desugar,
)


def _lookup(hass: HomeAssistant) -> PermissionLookup:
    return PermissionLookup(er.async_get(hass), dr.async_get(hass))


def _role(role_id: str = "r", **kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": role_id,
        "name": role_id,
        "allow": {},
        "deny": {},
        "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
    }
    base.update(kwargs)
    return base


async def test_deny_vetoes_allow(hass: HomeAssistant) -> None:
    """Denial is expressible, which HA's own policy schema cannot do."""
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            deny={CAT_ENTITIES: {"entity_ids": {"lock.front": True}}},
        ),
        _lookup(hass),
    )
    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("lock.front", POLICY_READ) is False


async def test_deny_is_coarse_and_has_no_carve_out(hass: HomeAssistant) -> None:
    """A denied domain denies everything in it; a narrower entry cannot rescue one.

    HA's `apply_policy_funcs` treats an empty entry as "no opinion" and falls
    through to the broader rule, and `SINGLE_ENTITY_SCHEMA` cannot express False
    to stop the search. Carve-outs belong on the allow side instead -- see
    `test_carve_out_is_expressed_by_granting_narrowly`.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            deny={
                CAT_ENTITIES: {
                    "domains": {"lock": True},
                    "entity_ids": {"lock.garage": {}},
                }
            },
        ),
        _lookup(hass),
    )
    assert role.check("lock.front", POLICY_READ) is False
    assert role.check("lock.garage", POLICY_READ) is False


async def test_carve_out_is_expressed_by_granting_narrowly(
    hass: HomeAssistant,
) -> None:
    """All locks except the garage is an allow-side grant, not a deny-side hole."""
    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {
                    SUBCAT_ALL: {POLICY_READ: True},
                    "entity_ids": {"lock.garage": {POLICY_CONTROL: True}},
                }
            },
            deny={CAT_ENTITIES: {"domains": {"lock": {POLICY_CONTROL: True}}}},
        ),
        _lookup(hass),
    )
    assert role.check("lock.front", POLICY_CONTROL) is False
    assert role.check("lock.garage", POLICY_CONTROL) is False
    assert role.check("light.kitchen", POLICY_READ) is True


async def test_read_does_not_imply_control(hass: HomeAssistant) -> None:
    """The read_only shape must not permit control."""
    role = compile_role(
        hass,
        _role(allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}}),
        _lookup(hass),
    )
    assert role.check("light.kitchen", POLICY_READ) is True
    assert role.check("light.kitchen", POLICY_CONTROL) is False


async def test_area_desugars_through_the_device(hass: HomeAssistant) -> None:
    """An entity with no area of its own inherits its device's."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")

    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "dev1")},
        connections=set(),
    )
    dev_reg.async_update_device(device.id, area_id=kitchen.id)

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("light", "test", "unique1", device_id=device.id)

    policy = desugar(hass, {CAT_ENTITIES: {"area_ids": {kitchen.id: True}}})
    assert entry.entity_id in policy[CAT_ENTITIES]["entity_ids"]


async def test_entity_level_area_overrides_its_device(hass: HomeAssistant) -> None:
    """HA's own _lookup_area ignores entity_entry.area_id; desugaring must not.

    An entity assigned directly to the study, whose device sits in the kitchen,
    belongs to the study.
    """
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")
    study = area_reg.async_create("Study")

    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "dev2")},
        connections=set(),
    )
    dev_reg.async_update_device(device.id, area_id=kitchen.id)

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("light", "test", "unique2", device_id=device.id)
    ent_reg.async_update_entity(entry.entity_id, area_id=study.id)

    study_policy = desugar(hass, {CAT_ENTITIES: {"area_ids": {study.id: True}}})
    kitchen_policy = desugar(hass, {CAT_ENTITIES: {"area_ids": {kitchen.id: True}}})

    assert entry.entity_id in study_policy[CAT_ENTITIES]["entity_ids"]
    assert entry.entity_id not in kitchen_policy.get(CAT_ENTITIES, {}).get(
        "entity_ids", {}
    )


async def test_labels_desugar_to_entities(hass: HomeAssistant) -> None:
    """Labels are not a category HA's compiler knows; desugaring adds them."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("light", "test", "unique3")
    ent_reg.async_update_entity(entry.entity_id, labels={"shared"})

    policy = desugar(hass, {"label_ids": {"shared": True}})
    assert entry.entity_id in policy[CAT_ENTITIES]["entity_ids"]


async def test_roles_compose_permissively(hass: HomeAssistant) -> None:
    """A deny in one role must not subtract from another; roles stay composable."""
    lookup = _lookup(hass)
    restrictive = compile_role(
        hass,
        _role(
            "a",
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            deny={CAT_ENTITIES: {"entity_ids": {"lock.front": True}}},
        ),
        lookup,
    )
    permissive = compile_role(
        hass,
        _role(
            "b",
            allow={CAT_ENTITIES: {"entity_ids": {"lock.front": {POLICY_READ: True}}}},
        ),
        lookup,
    )
    perms = Permissions(roles=[restrictive, permissive])
    assert perms.check_entity("lock.front", POLICY_READ) is True


async def test_global_deny_beats_every_role(hass: HomeAssistant) -> None:
    """Per-user denial expresses what role composition cannot."""
    lookup = _lookup(hass)
    role = compile_role(hass, _role(allow={CAT_ENTITIES: True}), lookup)
    deny_fn = compile_entities({"entity_ids": {"camera.bedroom": True}}, lookup)
    perms = Permissions(roles=[role], global_deny_fn=deny_fn)

    assert perms.check_entity("light.kitchen", POLICY_READ) is True
    assert perms.check_entity("camera.bedroom", POLICY_READ) is False


@pytest.mark.parametrize(
    ("role_max", "command_tier", "expected"),
    [
        (TIER_OPEN, TIER_OPEN, True),
        (TIER_OPEN, TIER_USER, False),
        (TIER_OPEN, TIER_ADMIN, False),
        (TIER_USER, TIER_USER, True),
        (TIER_USER, TIER_ADMIN, False),
        (TIER_ADMIN, TIER_ADMIN, True),
    ],
)
async def test_tier_ranking(
    hass: HomeAssistant, role_max: str, command_tier: str, expected: bool
) -> None:
    """A role admits commands at or below its maximum tier."""
    role = compile_role(
        hass, _role(tiers={"max": role_max, "allow": [], "deny": []}), _lookup(hass)
    )
    assert Permissions(roles=[role]).tier_allowed("x", command_tier) is expected


async def test_tier_globs_override_the_ranking(hass: HomeAssistant) -> None:
    """Operator overrides win over the derived tier, deny before allow."""
    role = compile_role(
        hass,
        _role(
            tiers={
                "max": TIER_OPEN,
                "allow": ["frontend/*"],
                "deny": ["frontend/secret"],
            }
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.tier_allowed("frontend/get_themes", TIER_ADMIN) is True
    assert perms.tier_allowed("frontend/secret", TIER_OPEN) is False


async def test_owner_is_pass_through(hass: HomeAssistant) -> None:
    """The lockout escape hatch lives in code, not in editable data."""
    perms = Permissions(pass_through=True)
    assert perms.check_entity("anything.at_all", POLICY_CONTROL) is True
    assert perms.tier_allowed("config/auth/delete", TIER_ADMIN) is True
    assert perms.full_access is True
