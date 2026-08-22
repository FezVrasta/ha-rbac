"""Tests for response filtering."""

from homeassistant.core import HomeAssistant

from custom_components.ha_rbac.filters import REGISTRY, FilterContext, prune


def _ctx(hass: HomeAssistant, denied: set[str]) -> FilterContext:
    """Return a context denying read on the given entities."""
    return FilterContext(hass, lambda entity_id, key: entity_id not in denied)


async def test_get_states_drops_denied_entities(hass: HomeAssistant) -> None:
    """The most common read must not leak."""
    result = REGISTRY.filter_result(
        "get_states",
        _ctx(hass, {"lock.front"}),
        [
            {"entity_id": "light.kitchen", "state": "on"},
            {"entity_id": "lock.front", "state": "unlocked"},
        ],
    )
    assert [state["entity_id"] for state in result] == ["light.kitchen"]


async def test_compressed_state_add_is_filtered(hass: HomeAssistant) -> None:
    """subscribe_entities sends its initial state under `a`."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _ctx(hass, {"lock.front"}),
        {"a": {"light.kitchen": {"s": "on"}, "lock.front": {"s": "unlocked"}}},
    )
    assert set(event["a"]) == {"light.kitchen"}


async def test_compressed_state_change_and_remove_are_filtered(
    hass: HomeAssistant,
) -> None:
    """Diffs arrive under `c`, removals under `r`."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _ctx(hass, {"lock.front"}),
        {
            "c": {"lock.front": {"+": {"s": "locked"}}, "light.a": {"+": {"s": "on"}}},
            "r": ["lock.front", "light.b"],
        },
    )
    assert set(event["c"]) == {"light.a"}
    assert event["r"] == ["light.b"]


async def test_event_filtered_to_nothing_is_dropped(hass: HomeAssistant) -> None:
    """An event whose every entity was denied must not be forwarded at all."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _ctx(hass, {"lock.front"}),
        {"a": {"lock.front": {"s": "unlocked"}}},
    )
    assert event is None


async def test_camera_token_goes_with_the_entity(hass: HomeAssistant) -> None:
    """entity_picture carries a live capability URL, so the whole state must go."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _ctx(hass, {"camera.bedroom"}),
        {
            "a": {
                "camera.bedroom": {
                    "s": "idle",
                    "a": {
                        "entity_picture": "/api/camera_proxy/camera.bedroom?token=s3cret"
                    },
                }
            }
        },
    )
    assert event is None


async def test_state_changed_events_are_dropped(hass: HomeAssistant) -> None:
    """HA rechecks these for non-admins, but the proxy's identity is often admin."""
    denied = REGISTRY.filter_event(
        "subscribe_events",
        _ctx(hass, {"lock.front"}),
        {"event_type": "state_changed", "data": {"entity_id": "lock.front"}},
    )
    allowed = REGISTRY.filter_event(
        "subscribe_events",
        _ctx(hass, {"lock.front"}),
        {"event_type": "state_changed", "data": {"entity_id": "light.a"}},
    )
    assert denied is None
    assert allowed is not None


async def test_generic_prune_drops_objects_by_entity_id(hass: HomeAssistant) -> None:
    """Anything without a specific filter falls back to the generic walk."""
    result = REGISTRY.filter_result(
        "config/entity_registry/list",
        _ctx(hass, {"lock.front"}),
        [
            {"entity_id": "light.kitchen", "area_id": "a1"},
            {"entity_id": "lock.front", "area_id": "a1"},
        ],
    )
    assert [entry["entity_id"] for entry in result] == ["light.kitchen"]


async def test_generic_prune_filters_entity_id_lists(hass: HomeAssistant) -> None:
    """search/related returns lists of ids rather than objects."""
    result = prune(
        _ctx(hass, {"lock.front"}),
        {
            "entity": ["light.kitchen", "lock.front"],
            "entity_id": ["light.a", "lock.front"],
        },
    )
    assert result["entity_id"] == ["light.a"]


async def test_lovelace_cards_are_filtered_by_its_own_conventions(
    hass: HomeAssistant,
) -> None:
    """Lovelace uses `entity`/`entities`, which are not HA resource keys."""
    config = {
        "views": [
            {
                "cards": [
                    {"type": "button", "entity": "lock.front"},
                    {"type": "button", "entity": "light.kitchen"},
                    {"type": "entities", "entities": ["light.a", "lock.front"]},
                ]
            }
        ]
    }
    result = REGISTRY.filter_result(
        "lovelace/config", _ctx(hass, {"lock.front"}), config
    )
    cards = result["views"][0]["cards"]
    assert len(cards) == 2
    assert cards[0]["entity"] == "light.kitchen"
    assert cards[1]["entities"] == ["light.a"]


async def test_current_user_is_reported_as_non_admin(hass: HomeAssistant) -> None:
    """Cosmetic: stops the frontend offering admin UI that would fail."""
    result = REGISTRY.filter_result(
        "auth/current_user", _ctx(hass, set()), {"id": "u1", "is_admin": True}
    )
    assert result["is_admin"] is False


async def test_catalogues_are_passed_through(hass: HomeAssistant) -> None:
    """Pruning a themes payload would corrupt the UI and conceal nothing."""
    payload = {"themes": {"dark": {"primary-color": "#000"}}, "default_theme": "dark"}
    assert (
        REGISTRY.filter_result("frontend/get_themes", _ctx(hass, set()), payload)
        == payload
    )


async def test_get_services_hides_domains_the_role_cannot_reach(
    hass: HomeAssistant,
) -> None:
    """Derived from live state, so it is right for entity-specific grants too."""
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("lock.front", "locked")

    result = REGISTRY.filter_result(
        "get_services",
        _ctx(hass, {"lock.front"}),
        {
            "light": {"turn_on": {}},
            "lock": {"lock": {}},
            "homeassistant": {"restart": {}},
        },
    )
    assert "light" in result
    assert "lock" not in result
    # A service domain with no entities at all is kept; hiding it would break
    # the UI without concealing anything about the user's devices.
    assert "homeassistant" in result


async def test_prune_preserves_list_order(hass: HomeAssistant) -> None:
    """Clients hold onto indices, so filtering must not reorder."""
    result = prune(
        _ctx(hass, {"lock.b"}),
        [{"entity_id": "light.a"}, {"entity_id": "lock.b"}, {"entity_id": "light.c"}],
    )
    assert [item["entity_id"] for item in result] == ["light.a", "light.c"]
