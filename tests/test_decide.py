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
    REASON_APP,
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
    # `media_source` is loaded so its commands classify as themselves rather
    # than as the unknown-and-therefore-admin default.
    for domain in ("websocket_api", "config", "api", "media_source"):
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


async def test_render_template_is_judged_by_its_response(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A decoy entity_ids buys nothing; the render is judged by what it read.

    The subscription is permitted because every result it streams reports the
    states that render actually read -- a stronger check than the request could
    support. The response side is covered in test_filters and test_proxy.
    """
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
    assert decision.allowed is True
    assert decision.filter_response is True


async def test_an_unregistered_template_command_is_denied(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A template command this build has never seen resolves to admin."""
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


async def test_scene_apply_cannot_reach_a_denied_entity(
    hass: HomeAssistant, decider: Decider
) -> None:
    """The entities a scene reproduces are mapping keys, not values.

    `scene.apply` takes `{"entities": {"lock.front": "unlocked"}}`, so the
    payload names no resource any key-based extraction can see. It reached
    `_decide_service`, which reads a call naming nothing as bounded by its
    service, and a role allowed to control the scene domain then set the state
    of anything at all.
    """
    hass.states.async_set("lock.front", "locked")
    role = compile_role(
        hass,
        {
            "id": "s",
            "name": "s",
            "allow": {CAT_ENTITIES: {"domains": {"scene": {POLICY_CONTROL: True}}}},
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
            "domain": "scene",
            "service": "apply",
            "service_data": {"entities": {"lock.front": "unlocked"}},
        },
    )
    assert decision.allowed is False
    assert "lock.front" in decision.detail


async def test_resolving_a_denied_camera_as_media_is_refused(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Cameras are a media source, and resolving one returns a stream URL.

    The entity is named in the tail of a `media-source://` URI rather than under
    any resource key, and the URL that comes back authenticates on its own, so
    nothing downstream could recover what the request gave away.
    """
    hass.states.async_set("camera.bedroom", "idle")
    permissions = _read_only(hass)
    assert permissions.check_entity("camera.bedroom", POLICY_READ) is True

    denying = Permissions(
        roles=[
            compile_role(
                hass,
                {
                    "id": "d",
                    "name": "d",
                    "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                    "deny": {CAT_ENTITIES: {"domains": {"camera": True}}},
                    "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
                },
                PermissionLookup(er.async_get(hass), dr.async_get(hass)),
            )
        ]
    )
    decision = decider.decide(
        denying,
        KIND_WS,
        "media_source/resolve_media",
        {
            "type": "media_source/resolve_media",
            "media_content_id": "media-source://camera/camera.bedroom",
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE


async def test_an_ordinary_media_id_is_not_read_as_an_entity(
    hass: HomeAssistant, decider: Decider
) -> None:
    """`local/song.mp3` has the shape of an entity id and is not one.

    Treating the tail of every media URI as an entity would deny the whole media
    browser, so the registry decides rather than the shape.
    """
    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "media_source/resolve_media",
        {
            "type": "media_source/resolve_media",
            "media_content_id": "media-source://media_source/local/song.mp3",
        },
    )
    assert decision.allowed is True


async def test_group_membership_cannot_reach_a_denied_entity(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A group expands to its members server-side, so it must here too.

    `lock.unlock` aimed at `group.locks` names only the group -- an entity the
    role is free to control -- while Home Assistant expands the group and acts
    on `lock.front` inside it. Without expanding the group the resource gate
    checked the wrapper and missed the denied member.
    """
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("group.locks", "locked", {"entity_id": ["lock.front"]})
    role = compile_role(
        hass,
        {
            "id": "g",
            "name": "g",
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {CAT_ENTITIES: {"domains": {"lock": True}}},
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
            "domain": "lock",
            "service": "unlock",
            "target": {"entity_id": "group.locks"},
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE
    assert "lock.front" in decision.resources


async def test_nested_group_membership_is_expanded(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Group expansion recurses, so a group of groups is still caught."""
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("group.inner", "locked", {"entity_id": ["lock.front"]})
    hass.states.async_set(
        "group.outer", "locked", {"entity_id": ["group.inner", "light.kitchen"]}
    )
    role = compile_role(
        hass,
        {
            "id": "g",
            "name": "g",
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {CAT_ENTITIES: {"domains": {"lock": True}}},
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
            "domain": "lock",
            "service": "unlock",
            "target": {"entity_id": "group.outer"},
        },
    )
    assert decision.allowed is False
    assert "lock.front" in decision.resources


async def _history_decider(hass: HomeAssistant) -> Decider:
    """Return a decider whose catalogue knows the history command and panel.

    The real `history` component pulls in `recorder`, which needs a database
    the test harness does not stand up. Only two things about history matter to
    the decision: its command classifies as user-tier (so the tier gate lets it
    through), and its panel is registered (so the app gate can deny it). Both
    are registered here directly, which is exactly what the component would do.
    """
    import voluptuous as vol  # noqa: PLC0415
    from homeassistant.components import websocket_api  # noqa: PLC0415
    from homeassistant.components.frontend import (  # noqa: PLC0415
        async_register_built_in_panel,
    )

    for domain in ("websocket_api", "config", "api"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()

    @websocket_api.ws_require_user()
    @websocket_api.websocket_command(
        {
            vol.Required("type"): "history/history_during_period",
            vol.Required("start_time"): str,
            vol.Optional("entity_ids"): [str],
        }
    )
    def _history_cmd(hass, connection, msg):  # pragma: no cover - never called
        connection.send_result(msg["id"], {})

    websocket_api.async_register_command(hass, _history_cmd)

    # `frontend` will not set up without the `hass_frontend` package, but its
    # panel-registration helper only writes to the panel registry the app gate
    # reads, so it works on its own -- exactly what the component would record.
    async_register_built_in_panel(hass, "history", "History", "hass:chart-box")

    catalog = Catalog(hass)
    catalog.rebuild()
    return Decider(hass, catalog, REGISTRY)


def _history_only(hass: HomeAssistant, ids: list[str]) -> Permissions:
    """Return a role that denies the History app but is granted history of `ids`.

    It can read nothing live -- an empty allow -- so history for the granted
    entities can only be coming from the history grant, which is the point.
    """
    role = compile_role(
        hass,
        {
            "id": "h",
            "name": "h",
            "allow": {},
            "deny": {},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
            "apps": {"deny": ["history"]},
            "history": {"rules": [{"target": "entity_ids", "ids": ids}]},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    return Permissions(roles=[role])


async def test_bounded_history_request_allows_a_granted_entity(
    hass: HomeAssistant,
) -> None:
    """A history request naming a granted-but-unreadable entity is not refused.

    The frontend's history card names its entities, so the request is bounded
    and reaches the resource gate. Without widening it there, the gate refuses
    the entity for lack of live read and the grant never reaches the response
    filter.
    """
    decider = await _history_decider(hass)
    perms = _history_only(hass, ["climate.trend"])

    decision = decider.decide(
        perms,
        KIND_WS,
        "history/history_during_period",
        {
            "type": "history/history_during_period",
            "entity_ids": ["climate.trend"],
            "start_time": "2024-01-01T00:00:00+00:00",
        },
    )
    assert decision.allowed is True, decision.detail
    assert decision.filter_response is True


async def test_bounded_history_request_still_refuses_an_ungranted_entity(
    hass: HomeAssistant,
) -> None:
    """The widening is exactly the grant -- an entity outside it is still refused."""
    decider = await _history_decider(hass)
    perms = _history_only(hass, ["climate.trend"])

    decision = decider.decide(
        perms,
        KIND_WS,
        "history/history_during_period",
        {
            "type": "history/history_during_period",
            "entity_ids": ["lock.secret"],
            "start_time": "2024-01-01T00:00:00+00:00",
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE


async def test_history_app_deny_still_refuses_without_a_grant(
    hass: HomeAssistant,
) -> None:
    """Denying the History app with no grant refuses its commands, as before."""
    decider = await _history_decider(hass)
    role = compile_role(
        hass,
        {
            "id": "n",
            "name": "n",
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
            "apps": {"deny": ["history"]},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "history/history_during_period",
        {
            "type": "history/history_during_period",
            "start_time": "2024-01-01T00:00:00+00:00",
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_APP


async def test_a_history_grant_lets_the_command_past_the_app_gate(
    hass: HomeAssistant,
) -> None:
    """Denying History but holding a grant lets the command reach the filter.

    The panel stays hidden; the command is allowed so the per-entity response
    filter can narrow it. Refusing it here would make the grant dead letter.
    """
    decider = await _history_decider(hass)
    perms = _history_only(hass, ["climate.trend"])

    decision = decider.decide(
        perms,
        KIND_WS,
        "history/history_during_period",
        {
            "type": "history/history_during_period",
            "start_time": "2024-01-01T00:00:00+00:00",
        },
    )
    assert decision.allowed is True, decision.detail
    assert decision.filter_response is True
