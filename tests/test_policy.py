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
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.const import (
    ROLE_EDITOR,
    TIER_ADMIN,
    TIER_OPEN,
    TIER_USER,
)
from custom_components.ha_rbac.decide import KIND_WS, REASON_RESOURCE, Decider
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Permissions,
    compile_role,
    default_roles,
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
        # `open` is raised to `user` on compile -- see test_open_ceiling_is_raised
        (TIER_OPEN, TIER_OPEN, True),
        (TIER_OPEN, TIER_USER, True),
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


async def test_open_ceiling_is_raised_to_user(hass: HomeAssistant) -> None:
    """`open` is not a usable ceiling and is accepted rather than rejected.

    Every connection through the proxy is a signed-in user, and
    `auth/current_user` sits behind `ws_require_user`, so a role capped at
    `open` cannot start a frontend. Roles stored before that was understood are
    raised on load rather than left broken.
    """
    role = compile_role(
        hass, _role(tiers={"max": TIER_OPEN, "allow": [], "deny": []}), _lookup(hass)
    )
    assert role.tier_max == TIER_USER


async def test_a_capability_grants_the_commands_it_names(hass: HomeAssistant) -> None:
    """A role names a capability; the globs it stands for are derived from it.

    Storing the name rather than the globs is what lets the editor show what was
    chosen, and lets a role follow the grouping when Home Assistant moves a
    command under it.
    """
    role = compile_role(
        hass,
        {
            "id": "r",
            "name": "R",
            "capabilities": ["automations"],
            "tiers": {"max": TIER_USER, "allow": [], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    permissions = Permissions(roles=[role])

    assert permissions.tier_allowed("automation/config", TIER_ADMIN) is True
    assert (
        permissions.tier_allowed("POST /api/config/automation/config/1756", TIER_ADMIN)
        is True
    ), "the REST half of the automation editor has to come with it"
    assert permissions.tier_allowed("backup/generate", TIER_ADMIN) is False
    assert permissions.tier_allowed("config/auth/create", TIER_ADMIN) is False


async def test_an_unknown_capability_grants_nothing_and_is_kept(
    hass: HomeAssistant,
) -> None:
    """A name from a newer build must not take the whole role down with it.

    Rejecting it would strip a user of the access the rest of the role still
    describes correctly, and the failure is safe in this direction: an
    unrecognised name simply grants nothing.
    """
    stored = ROLE_SCHEMA(
        {"id": "r", "name": "R", "capabilities": ["automations", "quantum_drive"]}
    )
    assert stored["capabilities"] == ["automations", "quantum_drive"]

    role = compile_role(
        hass, stored, PermissionLookup(er.async_get(hass), dr.async_get(hass))
    )
    permissions = Permissions(roles=[role])
    assert permissions.tier_allowed("automation/config", TIER_ADMIN) is True
    assert permissions.tier_allowed("backup/generate", TIER_ADMIN) is False


async def test_the_editor_role_stops_short_of_the_house_itself(
    hass: HomeAssistant,
) -> None:
    """The preset most people asked for: build things, do not administer them."""
    roles = default_roles()
    editor = compile_role(
        hass,
        roles[ROLE_EDITOR],
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    permissions = Permissions(roles=[editor])

    assert permissions.full_access is False
    assert permissions.check_entity("light.kitchen", POLICY_CONTROL) is True
    for command in ("automation/config", "lovelace/config/save", "counter/create"):
        assert permissions.tier_allowed(command, TIER_ADMIN) is True, command
    for command in (
        "config/auth/create",
        "backup/generate",
        "config_entries/disable",
        "lovelace/resources/create",
    ):
        assert permissions.tier_allowed(command, TIER_ADMIN) is False, command


async def _select_decider(hass: HomeAssistant) -> Decider:
    """Return a decider on an instance with an input_select of names."""
    for domain in ("websocket_api", "config", "api"):
        await async_setup_component(hass, domain, {})
    await async_setup_component(
        hass,
        "input_select",
        {
            "input_select": {
                "announcing": {
                    "options": ["Jan", "Federico", "Nobody"],
                    "initial": "Nobody",
                }
            }
        },
    )
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()
    return Decider(hass, catalog, REGISTRY)


def _may_announce_as(hass: HomeAssistant, *options: str) -> Permissions:
    """Control of the announcer, narrowed to certain options."""
    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {
                    "entity_ids": {
                        "input_select.announcing": {
                            POLICY_READ: True,
                            POLICY_CONTROL: True,
                        }
                    }
                }
            },
            choices={
                "rules": [
                    {
                        "target": "entity_ids",
                        "ids": ["input_select.announcing"],
                        "options": list(options),
                    }
                ]
            },
            tiers={"max": TIER_USER, "allow": [], "deny": []},
        ),
        _lookup(hass),
    )
    return Permissions(roles=[role])


def _choose(option: str) -> dict[str, Any]:
    """Return the payload for choosing one option on the announcer."""
    return {
        "type": "call_service",
        "domain": "input_select",
        "service": "select_option",
        "target": {"entity_id": "input_select.announcing"},
        "service_data": {"option": option},
    }


async def test_a_role_may_be_given_only_some_of_a_selects_options(
    hass: HomeAssistant,
) -> None:
    """Entity permission is the wrong granularity for a select full of people.

    Control of the announcer is control of every name on it, so a role that may
    announce for one person could announce as anybody. The rule narrows the
    options and leaves the entity grant alone.
    """
    decider = await _select_decider(hass)
    permissions = _may_announce_as(hass, "Jan")

    assert decider.decide(permissions, KIND_WS, "call_service", _choose("Jan")).allowed
    refused = decider.decide(permissions, KIND_WS, "call_service", _choose("Federico"))
    assert refused.allowed is False
    assert refused.reason == REASON_RESOURCE
    assert refused.message == "You can only choose certain options there."
    assert "Federico" not in refused.message, "the option is a diagnostic, not a reply"


async def test_cycling_a_select_cannot_walk_past_the_rule(hass: HomeAssistant) -> None:
    """The four that name no option would reach every one of them.

    `select_next` and its siblings do not say where they are going, and where
    they land depends on where the select already is. There is no way to judge
    that without tracking its position, and guessing would be guessing in the
    permissive direction -- so a covered entity refuses them outright. Without
    this the rule is one extra request to step around.
    """
    decider = await _select_decider(hass)
    permissions = _may_announce_as(hass, "Jan")

    for service in ("select_next", "select_previous", "select_first", "select_last"):
        decision = decider.decide(
            permissions,
            KIND_WS,
            "call_service",
            {
                "type": "call_service",
                "domain": "input_select",
                "service": service,
                "target": {"entity_id": "input_select.announcing"},
            },
        )
        assert decision.allowed is False, service


async def test_rewriting_the_options_cannot_widen_the_rule(
    hass: HomeAssistant,
) -> None:
    """`set_options` replaces the list, so a forbidden name could be added."""
    decider = await _select_decider(hass)
    decision = decider.decide(
        _may_announce_as(hass, "Jan"),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "input_select",
            "service": "set_options",
            "target": {"entity_id": "input_select.announcing"},
            "service_data": {"options": ["Jan", "Federico"]},
        },
    )
    assert decision.allowed is False


async def test_a_script_cannot_carry_the_forbidden_choice(hass: HomeAssistant) -> None:
    """`execute_script` holds its calls in a sequence, so the walk goes in."""
    decider = await _select_decider(hass)
    decision = decider.decide(
        _may_announce_as(hass, "Jan"),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "input_select",
            "service": "select_option",
            "target": {"entity_id": "input_select.announcing"},
            "data": {"option": "Federico"},
        },
    )
    assert decision.allowed is False


async def test_a_select_no_rule_covers_is_untouched(hass: HomeAssistant) -> None:
    """A rule narrows the selects it names and nothing else.

    The role controls both selects; only one of them is ruled. The other must
    behave exactly as it did before choices existed.
    """
    decider = await _select_decider(hass)
    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {
                    "entity_ids": {
                        "input_select.announcing": {
                            POLICY_READ: True,
                            POLICY_CONTROL: True,
                        },
                        "input_select.other": {
                            POLICY_READ: True,
                            POLICY_CONTROL: True,
                        },
                    }
                }
            },
            choices={
                "rules": [
                    {
                        "target": "entity_ids",
                        "ids": ["input_select.announcing"],
                        "options": ["Jan"],
                    }
                ]
            },
            tiers={"max": TIER_USER, "allow": [], "deny": []},
        ),
        _lookup(hass),
    )
    permissions = Permissions(roles=[role])

    unruled = decider.decide(
        permissions,
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "input_select",
            "service": "select_option",
            "target": {"entity_id": "input_select.other"},
            "service_data": {"option": "anything"},
        },
    )
    assert unruled.allowed is True, "no rule covers it, so nothing is narrowed"

    # And cycling it, which is refused only where a rule applies.
    cycled = decider.decide(
        permissions,
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "input_select",
            "service": "select_next",
            "target": {"entity_id": "input_select.other"},
        },
    )
    assert cycled.allowed is True

    # The ruled one still is.
    assert not decider.decide(
        permissions, KIND_WS, "call_service", _choose("Federico")
    ).allowed


async def test_a_role_without_choice_rules_is_unaffected(hass: HomeAssistant) -> None:
    """The gate must cost nothing for the roles that do not use it."""
    decider = await _select_decider(hass)
    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            tiers={"max": TIER_USER, "allow": [], "deny": []},
        ),
        _lookup(hass),
    )
    permissions = Permissions(roles=[role])
    assert permissions.restricts_options is False
    assert decider.decide(
        permissions, KIND_WS, "call_service", _choose("Federico")
    ).allowed
