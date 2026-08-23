"""Tests for response filtering."""

from homeassistant.auth.permissions.const import POLICY_READ
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


async def test_template_result_is_withheld_when_it_read_a_denied_entity(
    hass: HomeAssistant,
) -> None:
    """The listeners report what this render actually read, so it can be judged."""
    event = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "result": "unlocked",
            "listeners": {"all": False, "entities": ["lock.front"], "domains": []},
        },
    )
    assert event is None


async def test_template_result_is_delivered_when_it_read_nothing(
    hass: HomeAssistant,
) -> None:
    """A dashboard heading reads no entity, and must render rather than break."""
    event = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "result": "Welcome home",
            "listeners": {"all": False, "entities": [], "domains": []},
        },
    )
    assert event["result"] == "Welcome home"


async def test_template_result_is_delivered_when_every_entity_is_readable(
    hass: HomeAssistant,
) -> None:
    """Templates over permitted entities are ordinary reads."""
    event = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "result": "on",
            "listeners": {"all": False, "entities": ["light.kitchen"], "domains": []},
        },
    )
    assert event["result"] == "on"


async def test_a_template_reading_all_states_is_withheld(hass: HomeAssistant) -> None:
    """`states | count` reads everything, so nothing about it can be cleared."""
    event = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {"result": "42", "listeners": {"all": True, "entities": [], "domains": []}},
    )
    assert event is None


async def test_a_domain_listener_is_checked_across_that_domain(
    hass: HomeAssistant,
) -> None:
    """A domain listener also covers entities that do not exist yet."""
    hass.states.async_set("lock.front", "unlocked")
    hass.states.async_set("light.kitchen", "on")

    denied = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "result": "1",
            "listeners": {"all": False, "entities": [], "domains": ["lock"]},
        },
    )
    allowed = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "result": "1",
            "listeners": {"all": False, "entities": [], "domains": ["light"]},
        },
    )
    assert denied is None
    assert allowed is not None


async def test_a_result_with_no_listeners_is_withheld(hass: HomeAssistant) -> None:
    """A render that does not account for what it read cannot be cleared."""
    event = REGISTRY.filter_event(
        "render_template", _ctx(hass, set()), {"result": "unlocked"}
    )
    assert event is None


async def test_a_template_error_is_not_echoed_verbatim(hass: HomeAssistant) -> None:
    """Jinja errors can quote the value that caused them."""
    event = REGISTRY.filter_event(
        "render_template",
        _ctx(hass, {"lock.front"}),
        {
            "error": "TypeError: can only concatenate str to 'unlocked'",
            "level": "ERROR",
        },
    )
    assert "unlocked" not in event["error"]
    assert event["level"] == "ERROR"


async def test_an_empty_denied_domain_is_still_withheld(hass: HomeAssistant) -> None:
    """A domain listener covers entities that do not exist yet.

    Checking only the current members let an empty domain through, and the count
    alone tells the reader how many of something they cannot see exist.
    """

    def check(entity_id: str, key: str) -> bool:
        return not entity_id.startswith("lock.")

    ctx = FilterContext(hass, check)
    assert not hass.states.async_entity_ids("lock"), "precondition: no locks exist"

    event = REGISTRY.filter_event(
        "render_template",
        ctx,
        {
            "result": "0",
            "listeners": {"all": False, "entities": [], "domains": ["lock"]},
        },
    )
    assert event is None
    assert check("lock.anything", POLICY_READ) is False


def _attr_ctx(hass: HomeAssistant, hidden: set[str]) -> FilterContext:
    """Return a context that withholds the given attribute names."""
    return FilterContext(
        hass, lambda entity_id, key: True, None, lambda name: name in hidden
    )


async def test_hidden_attributes_are_stripped_from_states(
    hass: HomeAssistant,
) -> None:
    """Seeing that someone is home should not mean seeing where they are."""
    result = REGISTRY.filter_result(
        "get_states",
        _attr_ctx(hass, {"latitude", "longitude"}),
        [
            {
                "entity_id": "person.me",
                "state": "home",
                "attributes": {
                    "latitude": 51.5,
                    "longitude": -0.1,
                    "friendly_name": "Me",
                },
            }
        ],
    )
    assert result[0]["attributes"] == {"friendly_name": "Me"}
    assert result[0]["state"] == "home"


async def test_hidden_attributes_are_stripped_from_the_initial_state(
    hass: HomeAssistant,
) -> None:
    """subscribe_entities sends a full state first, under `a`."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _attr_ctx(hass, {"latitude"}),
        {"a": {"person.me": {"s": "home", "a": {"latitude": 51.5, "source": "gps"}}}},
    )
    assert event["a"]["person.me"]["a"] == {"source": "gps"}


async def test_hidden_attributes_are_stripped_from_diffs(hass: HomeAssistant) -> None:
    """Otherwise the attribute would arrive on the next change instead."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _attr_ctx(hass, {"latitude"}),
        {
            "c": {
                "person.me": {
                    "+": {"s": "not_home", "a": {"latitude": 52.0, "source": "gps"}}
                }
            }
        },
    )
    diff = event["c"]["person.me"]["+"]
    assert diff["a"] == {"source": "gps"}
    assert diff["s"] == "not_home"


async def test_a_removal_diff_does_not_disclose_a_hidden_attribute(
    hass: HomeAssistant,
) -> None:
    """A removal names the attribute without its value, which is still a leak."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _attr_ctx(hass, {"latitude"}),
        {"c": {"person.me": {"+": {"s": "home"}, "-": {"a": ["latitude", "source"]}}}},
    )
    assert event["c"]["person.me"]["-"]["a"] == ["source"]


async def test_a_removal_of_only_hidden_attributes_is_dropped(
    hass: HomeAssistant,
) -> None:
    """An empty removal block would still say something changed."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _attr_ctx(hass, {"latitude"}),
        {"c": {"person.me": {"+": {"s": "home"}, "-": {"a": ["latitude"]}}}},
    )
    assert "-" not in event["c"]["person.me"]


async def test_hidden_attributes_are_stripped_from_state_changed_events(
    hass: HomeAssistant,
) -> None:
    """The other stream carries whole state objects rather than diffs."""
    event = REGISTRY.filter_event(
        "subscribe_events",
        _attr_ctx(hass, {"latitude"}),
        {
            "event_type": "state_changed",
            "data": {
                "entity_id": "person.me",
                "new_state": {
                    "entity_id": "person.me",
                    "state": "home",
                    "attributes": {"latitude": 51.5, "source": "gps"},
                },
            },
        },
    )
    assert event["data"]["new_state"]["attributes"] == {"source": "gps"}


async def test_attributes_are_untouched_when_no_rules_apply(
    hass: HomeAssistant,
) -> None:
    """A role with no attribute rules must pay nothing and change nothing."""
    payload = [
        {"entity_id": "person.me", "attributes": {"latitude": 51.5, "source": "gps"}}
    ]
    result = REGISTRY.filter_result("get_states", _ctx(hass, set()), payload)
    assert result[0]["attributes"] == {"latitude": 51.5, "source": "gps"}


async def test_history_states_have_hidden_attributes_stripped(
    hass: HomeAssistant,
) -> None:
    """History returns states compressed, with attributes under "a".

    The generic walk only knew the spelled-out `attributes`, so a role hiding a
    location served it in full through history while hiding it everywhere else.
    """
    result = REGISTRY.filter_result(
        "history/history_during_period",
        _attr_ctx(hass, {"latitude", "longitude"}),
        {
            "device_tracker.phone": [
                {
                    "s": "home",
                    "a": {"latitude": 51.5, "longitude": -0.1, "battery": 77},
                    "lu": 1787440000.0,
                }
            ]
        },
    )
    assert result["device_tracker.phone"][0]["a"] == {"battery": 77}


async def test_a_bare_a_key_is_not_mistaken_for_attributes(
    hass: HomeAssistant,
) -> None:
    """The key `a` means attributes only on something that is actually a state."""
    payload = {"a": {"latitude": 51.5}, "unrelated": True}
    result = REGISTRY.filter_result(
        "some/other/command", _attr_ctx(hass, {"latitude"}), payload
    )
    assert result["a"] == {"latitude": 51.5}


async def test_the_compact_registry_listing_hides_denied_entities(
    hass: HomeAssistant,
) -> None:
    """It abbreviates entity_id to `ei`, which the generic walk did not know.

    The entries carry name, device and area, so this disclosed the existence and
    details of entities absent from every other response.
    """
    result = REGISTRY.filter_result(
        "config/entity_registry/list_for_display",
        _ctx(hass, {"lock.front_door"}),
        {
            "entity_categories": {"config": 1},
            "entities": [
                {"ei": "light.kitchen", "en": "Kitchen"},
                {"ei": "lock.front_door", "en": "Front Door", "di": "device-1"},
            ],
        },
    )
    assert [entry["ei"] for entry in result["entities"]] == ["light.kitchen"]
    assert result["entity_categories"] == {"config": 1}


async def test_the_dashboard_listing_hides_denied_dashboards(
    hass: HomeAssistant,
) -> None:
    """Another route to the same dashboards, naming none of them in the request."""
    ctx = FilterContext(hass, lambda e, k: True, lambda url: url != "secret-dash")
    result = REGISTRY.filter_result(
        "lovelace/dashboards/list",
        ctx,
        [
            {"id": "map", "url_path": "map", "title": "Map"},
            {"id": "secret", "url_path": "secret-dash", "title": "Secret"},
        ],
    )
    assert [d["url_path"] for d in result] == ["map"]
