"""Tests for the decision procedure, one per escape hatch."""

import pytest
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
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.const import TIER_OPEN
from custom_components.ha_rbac.decide import (
    KIND_WS,
    REASON_DEGRADED,
    REASON_RESOURCE,
    REASON_TIER,
    REASON_UNBOUNDED,
    Decider,
)
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import Permissions, compile_role


@pytest.fixture(name="decider")
async def decider_fixture(hass: HomeAssistant) -> Decider:
    """Return a decider backed by a real command catalogue."""
    for domain in ("websocket_api", "config", "api"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()
    return Decider(hass, catalog, REGISTRY)


def _read_only(hass: HomeAssistant) -> Permissions:
    """Return permissions equivalent to the predefined read_only role."""
    role = compile_role(
        hass,
        {
            "id": "ro",
            "name": "ro",
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            "deny": {},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    return Permissions(roles=[role])


async def test_render_template_is_denied(hass: HomeAssistant, decider: Decider) -> None:
    """The headline case: a decoy entity_ids must not buy a template access."""
    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "render_template",
        {
            "type": "render_template",
            "template": "{{ states('lock.front_door') }}",
            "entity_ids": ["sun.sun"],
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_UNBOUNDED


async def test_template_start_preview_is_denied(
    hass: HomeAssistant, decider: Decider
) -> None:
    """The same escape hatch under another name, closed by the same rule."""
    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "template/start_preview",
        {"type": "template/start_preview", "template": "{{ states('lock.front') }}"},
    )
    assert decision.allowed is False


async def test_admin_commands_are_denied_by_the_tier_gate(
    hass: HomeAssistant, decider: Decider
) -> None:
    """execute_script and fire_event are covered without being named."""
    for command in ("execute_script", "fire_event", "subscribe_trigger"):
        decision = decider.decide(_read_only(hass), KIND_WS, command, {"type": command})
        assert decision.allowed is False, command
        assert decision.reason == REASON_TIER


async def test_control_is_denied_for_a_read_only_role(
    hass: HomeAssistant, decider: Decider
) -> None:
    """call_service is a mutation even though its name is not write-shaped."""
    hass.states.async_set("light.kitchen", "off")
    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE


async def test_control_is_allowed_when_the_role_permits_it(
    hass: HomeAssistant, decider: Decider
) -> None:
    """The same call succeeds for a role granting control."""
    role = compile_role(
        hass,
        {
            "id": "u",
            "name": "u",
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
        },
    )
    assert decision.allowed is True


async def test_templated_service_data_is_denied(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A template smuggled into service_data reads anything the schema cannot see."""
    role = compile_role(
        hass,
        {
            "id": "u",
            "name": "u",
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "notify",
            "service": "persistent_notification",
            "target": {"entity_id": "light.kitchen"},
            "service_data": {"message": "{{ states('lock.front_door') }}"},
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_UNBOUNDED


async def test_reads_are_allowed_and_filtered(
    hass: HomeAssistant, decider: Decider
) -> None:
    """get_states names nothing, so it is allowed with its response filtered."""
    decision = decider.decide(
        _read_only(hass), KIND_WS, "get_states", {"type": "get_states"}
    )
    assert decision.allowed is True
    assert decision.filter_response is True


async def test_metadata_reads_need_no_classification(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A command returning nothing resource-shaped is safe by construction."""
    decision = decider.decide(
        _read_only(hass), KIND_WS, "get_config", {"type": "get_config"}
    )
    assert decision.allowed is True


async def test_denied_entity_blocks_a_bounded_read(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A role that cannot read an entity cannot ask about it either."""
    role = compile_role(
        hass,
        {
            "id": "ro",
            "name": "ro",
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            "deny": {CAT_ENTITIES: {"entity_ids": {"camera.bedroom": True}}},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "subscribe_entities",
        {"type": "subscribe_entities", "entity_ids": ["camera.bedroom"]},
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE
    assert decision.resources == ["camera.bedroom"]


async def test_pass_through_skips_every_gate(
    hass: HomeAssistant, decider: Decider
) -> None:
    """The owner fast path does no parsing at all."""
    decision = decider.decide(
        Permissions(pass_through=True),
        KIND_WS,
        "render_template",
        {"template": "{{ states('lock.front') }}"},
    )
    assert decision.allowed is True
    assert decision.filter_response is False


async def test_degraded_catalogue_denies_everything(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Broken derivation must fail closed, never open."""
    decider._catalog.degraded = True
    decision = decider.decide(
        _read_only(hass), KIND_WS, "get_states", {"type": "get_states"}
    )
    assert decision.allowed is False
    assert decision.reason == REASON_DEGRADED


async def test_http_method_decides_read_versus_control(
    hass: HomeAssistant, decider: Decider
) -> None:
    """For REST the verb is the signal, not the command name."""
    hass.states.async_set("light.kitchen", "off")
    perms = _read_only(hass)

    allowed = decider.decide(
        perms,
        "http",
        "GET /api/states/light.kitchen",
        {"entity_id": "light.kitchen"},
    )
    denied = decider.decide(
        perms,
        "http",
        "POST /api/services/light/turn_on",
        {"entity_id": "light.kitchen"},
    )
    assert allowed.allowed is True
    assert denied.allowed is False
