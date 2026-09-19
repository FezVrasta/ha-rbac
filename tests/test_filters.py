"""Tests for response filtering."""

import json

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
    """An event whose every entity was denied must not be forwarded at all.

    A bare frame is not free. Forwarding one per emptied diff would tell the
    role the instant anything in the house changed, denied entities included --
    an activity clock handed over by the code that hides them. The one frame
    that has to arrive even when empty is the subscription's opening snapshot,
    and the proxy substitutes that, because only the proxy knows which frame is
    the first: see `tests/test_proxy.py`.
    """
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
    assert event is None, "the denied camera, token and all, is gone"


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


def _attr_ctx(
    hass: HomeAssistant, hidden: set[str], only_on: str | None = None
) -> FilterContext:
    """Return a context withholding names, optionally only on one entity."""

    def is_hidden(entity_id: str, name: str) -> bool:
        if only_on is not None and entity_id != only_on:
            return False
        return name in hidden

    return FilterContext(hass, lambda entity_id, key: True, None, is_hidden)


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


async def test_an_attribute_rule_only_touches_the_entities_it_targets(
    hass: HomeAssistant,
) -> None:
    """Hiding a person's location must not hide the zone that defines home."""
    result = REGISTRY.filter_result(
        "get_states",
        _attr_ctx(hass, {"latitude", "longitude"}, only_on="person.me"),
        [
            {"entity_id": "person.me", "attributes": {"latitude": 51.5, "name": "Me"}},
            {"entity_id": "zone.home", "attributes": {"latitude": 51.5, "radius": 100}},
        ],
    )
    assert result[0]["attributes"] == {"name": "Me"}
    assert result[1]["attributes"] == {"latitude": 51.5, "radius": 100}


async def test_a_targeted_rule_applies_per_entity_in_a_subscription(
    hass: HomeAssistant,
) -> None:
    """The compressed stream is keyed by entity, so each gets its own rules."""
    event = REGISTRY.filter_event(
        "subscribe_entities",
        _attr_ctx(hass, {"latitude"}, only_on="person.me"),
        {
            "a": {
                "person.me": {"s": "home", "a": {"latitude": 51.5, "name": "Me"}},
                "zone.home": {"s": "zoning", "a": {"latitude": 51.5, "radius": 100}},
            }
        },
    )
    assert event["a"]["person.me"]["a"] == {"name": "Me"}
    assert event["a"]["zone.home"]["a"] == {"latitude": 51.5, "radius": 100}


async def test_denied_cameras_are_dropped_from_the_media_browser(
    hass: HomeAssistant,
) -> None:
    """The camera media source lists every camera by name, with a thumbnail.

    `get_panels` and `get_states` hide a denied camera, and this listed it again
    by another route: the request named no app and no resource key, so neither
    the app gate nor the generic walk had anything to match on.
    """
    hass.states.async_set("camera.bedroom", "idle")
    hass.states.async_set("camera.porch", "idle")
    result = REGISTRY.filter_result(
        "media_source/browse_media",
        _ctx(hass, {"camera.bedroom"}),
        {
            "title": "Camera",
            "media_content_id": "media-source://camera",
            "children": [
                {
                    "title": "Bedroom",
                    "media_content_id": "media-source://camera/camera.bedroom",
                    "thumbnail": "/api/camera_proxy/camera.bedroom",
                },
                {
                    "title": "Porch",
                    "media_content_id": "media-source://camera/camera.porch",
                    "thumbnail": "/api/camera_proxy/camera.porch",
                },
            ],
        },
    )
    assert [child["title"] for child in result["children"]] == ["Porch"]


async def test_media_that_is_not_an_entity_is_left_alone(
    hass: HomeAssistant,
) -> None:
    """A local file's id ends in something entity-shaped that is not an entity."""
    result = REGISTRY.filter_result(
        "media_source/browse_media",
        _ctx(hass, {"camera.bedroom"}),
        {
            "media_content_id": "media-source://media_source/local",
            "children": [
                {
                    "title": "song.mp3",
                    "media_content_id": "media-source://media_source/local/song.mp3",
                }
            ],
        },
    )
    assert [child["title"] for child in result["children"]] == ["song.mp3"]


def _hiding(hass: HomeAssistant, hidden: dict[str, set[str]]) -> FilterContext:
    """Return a context hiding named attributes on specific entities.

    Targeted the way a real rule is -- "hide latitude on person.jane" -- so a
    filter that loses the entity id cannot match it, which is the bug.
    """
    return FilterContext(
        hass,
        lambda entity_id, key: True,
        None,
        lambda entity_id, name: name in hidden.get(entity_id, set()),
    )


async def test_history_hides_attributes_a_targeted_rule_names(
    hass: HomeAssistant,
) -> None:
    """GHSA-px8f-g9qh-j6vc: history is where the entity is not in the sample.

    A compressed history sample carries only `s`, `a`, `lu` and `lc`, keyed by
    entity id one level up, so the generic walk recovered no entity id and
    stripped attributes with `None`. Only a rule written against no entity or
    domain in particular matches that, so "hide latitude and longitude on
    person.jane" was silently skipped for history while working correctly for
    `get_states` and `subscribe_entities` -- the location a role was written to
    withhold came back in full through a different command.
    """
    ctx = _hiding(hass, {"person.jane": {"latitude", "longitude"}})
    states = {
        "person.jane": [
            {
                "s": "home",
                "a": {"latitude": 51.5, "longitude": -0.1, "icon": "x"},
                "lu": 1,
            }
        ],
        "light.kitchen": [{"s": "on", "a": {"brightness": 5}, "lu": 2}],
    }

    result = REGISTRY.filter_result("history/history_during_period", ctx, states)
    assert result["person.jane"][0]["a"] == {"icon": "x"}
    assert result["light.kitchen"][0]["a"] == {"brightness": 5}, "untouched"

    event = REGISTRY.filter_event(
        "history/stream", ctx, {"states": states, "start_time": 1, "end_time": 2}
    )
    assert event["states"]["person.jane"][0]["a"] == {"icon": "x"}
    assert event["start_time"] == 1, "the frame around it survives"


async def test_history_drops_an_entity_the_role_cannot_read(
    hass: HomeAssistant,
) -> None:
    """An entity id with an empty history still says the entity exists."""
    result = REGISTRY.filter_result(
        "history/history_during_period",
        _ctx(hass, {"lock.front"}),
        {
            "lock.front": [{"s": "locked", "a": {}, "lu": 1}],
            "light.kitchen": [{"s": "on", "a": {}, "lu": 1}],
        },
    )
    assert set(result) == {"light.kitchen"}


async def test_an_event_naming_only_an_area_is_judged_by_it(
    hass: HomeAssistant,
) -> None:
    """GHSA-px8f-g9qh-j6vc: the walk knew entity ids and nothing else.

    Home Assistant fires `call_service` with the call's *original* target,
    before it is resolved, so the payload names an area rather than any entity.
    `device_id`, `area_id`, `label_id` and `floor_id` are first-class on the
    request side and were not recognised here at all, so a role subscribed to
    events could watch a service being invoked against an area containing
    nothing it may see.
    """
    from homeassistant.helpers import area_registry as ar  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    areas = ar.async_get(hass)
    bedroom = areas.async_get_or_create("Bedroom")
    entities = er.async_get(hass)
    entry = entities.async_get_or_create("lock", "demo", "bed1")
    entities.async_update_entity(entry.entity_id, area_id=bedroom.id)

    ctx = _ctx(hass, {entry.entity_id})
    event = {
        "event_type": "call_service",
        "data": {"domain": "lock", "service": "unlock", "service_data": {}},
        "target": {"area_id": bedroom.id},
    }

    # The object naming the area goes, the same way one naming a denied entity
    # does: the walk drops the object that carries the reference, not the frame
    # around it. What is left no longer says which area, or that it exists.
    filtered = prune(ctx, event)
    assert "target" not in filtered
    assert bedroom.id not in json.dumps(filtered)

    # An area holding something readable is not hidden, and neither is a
    # reference that resolves to no Home Assistant resource at all -- a
    # `device_id` in a Z-Wave payload is a Z-Wave node id, not an HA device.
    assert "target" in prune(_ctx(hass, set()), event)
    assert prune(ctx, {"device_id": "a-zwave-node-id"}) == {
        "device_id": "a-zwave-node-id"
    }


async def test_a_custom_card_key_naming_an_entity_is_scrubbed(
    hass: HomeAssistant,
) -> None:
    """GHSA-23ch-3r34-x2hq, second half: `camera_entity` was not on the list.

    Lovelace's own keys were enumerated, so a custom card naming its entity any
    other way kept it. Advanced Camera Card uses `camera_entity`, and a denied
    camera stayed in the dashboard configuration under it -- handing over the
    entity id of something the role hides entirely, and with it the name to go
    looking for on that integration's own routes.

    Matched by the suffix the convention uses rather than by a longer list, so
    a card key nobody has heard of is covered the day somebody writes it.
    """
    result = REGISTRY.filter_result(
        "lovelace/config",
        _ctx(hass, {"camera.bedroom"}),
        {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:advanced-camera-card",
                            "cameras": [{"camera_entity": "camera.bedroom"}],
                        },
                        {"type": "picture", "camera_entity": "camera.hall"},
                    ]
                }
            ]
        },
    )

    assert "camera.bedroom" not in json.dumps(result)
    assert "camera.hall" in json.dumps(result), "a camera they may see stays"


async def test_search_related_drops_denied_entities(hass: HomeAssistant) -> None:
    """search/related lists related ids as bare strings the generic walk missed.

    The response is `{item_type: [ids]}` with the ids as plain strings, so the
    generic walk -- which only reads a dict's own `entity_id` field -- returned
    every related entity, denied or not. Here the request names one item and is
    gated up front, but its result enumerated things hidden everywhere else.
    """
    result = REGISTRY.filter_result(
        "search/related",
        _ctx(hass, {"lock.front", "automation.secret"}),
        {
            "entity": ["light.kitchen", "lock.front"],
            "automation": ["automation.lights", "automation.secret"],
            "config_entry": ["abc123"],
        },
    )
    assert result["entity"] == ["light.kitchen"]
    assert result["automation"] == ["automation.lights"]
    assert result["config_entry"] == ["abc123"], "non-entity ids pass untouched"


async def test_search_related_drops_a_type_that_empties(hass: HomeAssistant) -> None:
    """A related type whose every id is denied is dropped, not left empty."""
    result = REGISTRY.filter_result(
        "search/related",
        _ctx(hass, {"lock.front"}),
        {"entity": ["lock.front"], "scene": []},
    )
    assert "entity" not in result, "emptied by filtering"
    assert "scene" not in result, "already empty"


async def test_search_related_hides_a_device_with_nothing_readable(
    hass: HomeAssistant,
) -> None:
    """A device the role can read nothing in must not appear in results."""
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        MockConfigEntry,
    )

    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", "secret-device")},
    )
    entities = er.async_get(hass)
    e = entities.async_get_or_create("lock", "demo", "sec1", device_id=device.id)

    ctx = _ctx(hass, {e.entity_id})
    result = REGISTRY.filter_result(
        "search/related",
        ctx,
        {"device": [device.id], "entity": [e.entity_id]},
    )
    assert "device" not in result, "no readable entity in it, so it is hidden"
    assert "entity" not in result


async def test_energy_prefs_drops_a_source_naming_a_denied_stat(
    hass: HomeAssistant,
) -> None:
    """Energy prefs name meters by statistic id in non-`entity_id` fields.

    A recorder statistic for a sensor is its entity id, carried under
    `stat_energy_from` and friends, so the generic walk returned the whole
    topology -- every grid, solar and battery meter -- of things the role is
    hidden from.
    """
    result = REGISTRY.filter_result(
        "energy/get_prefs",
        _ctx(hass, {"sensor.secret_grid"}),
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [{"stat_energy_from": "sensor.secret_grid"}],
                },
                {"type": "solar", "stat_energy_from": "sensor.public_solar"},
            ],
            "device_consumption": [
                {"stat_consumption": "sensor.secret_grid", "name": "Hidden"},
                {"stat_consumption": "sensor.public_solar", "name": "Shown"},
            ],
        },
    )
    src_types = [s["type"] for s in result["energy_sources"]]
    assert src_types == ["solar"], "the grid source naming a denied meter is gone"
    assert [d["name"] for d in result["device_consumption"]] == ["Shown"]


async def test_energy_prefs_keep_an_external_statistic(hass: HomeAssistant) -> None:
    """An external statistic id is not an entity and must not be dropped."""
    result = REGISTRY.filter_result(
        "energy/get_prefs",
        _ctx(hass, set()),
        {
            "energy_sources": [
                {"type": "gas", "stat_energy_from": "co2signal:intensity"}
            ],
            "device_consumption": [],
        },
    )
    assert len(result["energy_sources"]) == 1, "external stat id left alone"


async def test_energy_prefs_price_entity_is_gated(hass: HomeAssistant) -> None:
    """entity_energy_price holds an entity id outright; a denied one hides the source."""
    result = REGISTRY.filter_result(
        "energy/get_prefs",
        _ctx(hass, {"sensor.secret_price"}),
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [
                        {
                            "stat_energy_from": "sensor.public_meter",
                            "entity_energy_price": "sensor.secret_price",
                        }
                    ],
                }
            ],
            "device_consumption": [],
        },
    )
    assert result["energy_sources"] == [], "a denied price entity withholds the source"


async def test_statistic_metadata_drops_denied_rows(hass: HomeAssistant) -> None:
    """Recorder metadata rows are keyed on `statistic_id`, not `entity_id`.

    The generic walk left them, disclosing the id, name, source and unit of
    every recorded sensor the role is hidden from.
    """
    result = REGISTRY.filter_result(
        "recorder/list_statistic_ids",
        _ctx(hass, {"sensor.secret_power"}),
        [
            {
                "statistic_id": "sensor.public_power",
                "name": "Public",
                "source": "recorder",
            },
            {
                "statistic_id": "sensor.secret_power",
                "name": "Secret",
                "source": "recorder",
            },
            {
                "statistic_id": "co2signal:intensity",
                "name": "CO2",
                "source": "co2signal",
            },
        ],
    )
    ids = [r["statistic_id"] for r in result]
    assert ids == ["sensor.public_power", "co2signal:intensity"], (
        "denied sensor dropped; external statistic kept"
    )


async def test_statistic_metadata_passthrough_when_not_a_list(
    hass: HomeAssistant,
) -> None:
    """A shape that is not the expected list is returned untouched."""
    result = REGISTRY.filter_result(
        "recorder/get_statistics_metadata", _ctx(hass, {"sensor.x"}), {"unexpected": 1}
    )
    assert result == {"unexpected": 1}


async def test_a_denied_floor_or_label_is_not_named_by_a_search(
    hass: HomeAssistant,
) -> None:
    """A search result names floors and labels as well as devices and areas.

    Searching an automation returns the floors and labels that automation
    *targets*, and a role allowed to read the automation is not thereby allowed
    to know what is upstairs. `device` and `area` were filtered by a hardcoded
    pair of names and `floor` and `label` were not, so both came back for a
    role that can read nothing inside either -- which is the leak the filter
    was written to close, read one key over.
    """
    from homeassistant.helpers import area_registry as ar  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    from homeassistant.helpers import floor_registry as fr  # noqa: PLC0415
    from homeassistant.helpers import label_registry as lr  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        MockConfigEntry,
    )

    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    floor = fr.async_get(hass).async_create("Attic")
    label = lr.async_get(hass).async_create("Security")
    area = ar.async_get(hass).async_create("Vault", floor_id=floor.floor_id)
    hidden = er.async_get(hass).async_get_or_create(
        "lock",
        "demo",
        "vault",
        config_entry=entry,
    )
    er.async_get(hass).async_update_entity(
        hidden.entity_id, area_id=area.id, labels={label.label_id}
    )

    result = REGISTRY.filter_result(
        "search/related",
        _ctx(hass, {hidden.entity_id}),
        {
            "floor": [floor.floor_id],
            "label": [label.label_id],
            "area": [area.id],
            "automation": ["automation.probe"],
        },
    )

    assert "floor" not in result, "nothing readable on that floor"
    assert "label" not in result, "nothing readable carries that label"
    assert "area" not in result, "the area was already filtered; it still is"
    assert result["automation"] == ["automation.probe"], "the readable item stays"


def test_every_search_item_type_is_classified() -> None:
    """Pin the search key sets against Home Assistant's own list of item types.

    The filter splits `search/related`'s keys three ways: ids that are entities,
    ids that are containers, and ids that are neither and pass untouched. That
    split is only safe while it covers every type Home Assistant can answer
    with, and a type added upstream would otherwise join the third group in
    silence -- which is how `floor` and `label` came to be unfiltered. So the
    third group is spelled out here: if `ItemType` grows a member, this fails
    and somebody decides which group it belongs in.
    """
    from homeassistant.components.search import ItemType  # noqa: PLC0415

    from custom_components.ha_rbac.filters import (  # noqa: PLC0415
        _SEARCH_CONTAINER_KEYS,
        _SEARCH_ENTITY_KEYS,
    )

    # Neither an entity nor a container: a blueprint path, a config entry id and
    # an integration domain name nothing a role can be hidden from.
    not_a_resource = {
        "automation_blueprint",
        "config_entry",
        "integration",
        "script_blueprint",
    }

    classified = set(_SEARCH_ENTITY_KEYS) | set(_SEARCH_CONTAINER_KEYS) | not_a_resource
    upstream = {str(item) for item in ItemType}
    assert upstream <= classified, (
        f"unclassified search item types: {upstream - classified}"
    )
    assert classified <= upstream, (
        f"search keys Home Assistant never sends: {classified - upstream}"
    )


def _past_ctx(
    hass: HomeAssistant,
    readable: set[str],
    history: set[str] | None = None,
    logbook: set[str] | None = None,
) -> FilterContext:
    """Return a context that reads some entities and holds the past of others.

    `readable` are entities the role can read live, which carry their past for
    free; `history` and `logbook` are entities it may only see the recorded past
    of, per section. Kept separate deliberately, so a test can prove the past
    follows the grant and not the read -- and that the two sections are
    independent of each other.
    """
    granted = {"history": history or set(), "logbook": logbook or set()}
    return FilterContext(
        hass,
        lambda entity_id, key: entity_id in readable,
        None,
        None,
        lambda section, entity_id: (
            entity_id in readable or entity_id in granted[section]
        ),
    )


async def test_history_follows_a_history_grant_not_only_read(
    hass: HomeAssistant,
) -> None:
    """An entity granted history but not read still comes back in history.

    The whole point of the fine-grained grant: a role that may see one device's
    trend without being handed its live state everywhere. The websocket history
    filter must keep such an entity while still dropping one the role can
    neither read nor has been granted.
    """
    ctx = _past_ctx(hass, readable={"light.kitchen"}, history={"climate.trend"})
    states = {
        "light.kitchen": [{"s": "on", "a": {}, "lu": 1}],
        "climate.trend": [{"s": "20", "a": {}, "lu": 1}],
        "lock.secret": [{"s": "locked", "a": {}, "lu": 1}],
    }

    result = REGISTRY.filter_result("history/history_during_period", ctx, states)
    assert set(result) == {"light.kitchen", "climate.trend"}
    assert "lock.secret" not in result, "neither readable nor history-granted"


async def test_rest_history_drops_a_denied_series_whole(
    hass: HomeAssistant,
) -> None:
    """GHSA REST leak: minimised samples of a denied entity must not survive.

    `/api/history/period` answers as a list of lists, and Home Assistant puts
    the entity id only on the FIRST sample of each series -- the rest are
    minimised to a state and a timestamp. The generic walk recovered an id only
    from a sample's own key, so it dropped a denied entity's first sample and
    kept every later one, leaking its values and timestamps. The whole series
    belongs to one entity, so it is dropped whole.
    """
    from custom_components.ha_rbac.filters import filter_rest_history  # noqa: PLC0415

    ctx = _past_ctx(hass, readable={"sensor.ok"}, history=set())
    payload = [
        [
            {"entity_id": "lock.secret", "state": "locked", "last_changed": "t0"},
            {"state": "unlocked", "last_changed": "t1"},
            {"state": "locked", "last_changed": "t2"},
        ],
        [
            {"entity_id": "sensor.ok", "state": "1", "last_changed": "t0"},
            {"state": "2", "last_changed": "t1"},
        ],
    ]

    result = filter_rest_history(ctx, payload)
    flat = json.dumps(result)
    assert "lock.secret" not in flat, "the id is gone"
    assert "unlocked" not in flat, "and so are its later, minimised samples"
    assert len(result) == 1
    assert result[0][0]["entity_id"] == "sensor.ok"
    assert len(result[0]) == 2, "the allowed series is untouched"


async def test_rest_history_grant_keeps_a_granted_series(
    hass: HomeAssistant,
) -> None:
    """A history-granted entity's REST series survives even without read."""
    from custom_components.ha_rbac.filters import filter_rest_history  # noqa: PLC0415

    ctx = _past_ctx(hass, readable=set(), history={"climate.trend"})
    payload = [
        [
            {"entity_id": "climate.trend", "state": "20", "last_changed": "t0"},
            {"state": "21", "last_changed": "t1"},
        ],
        [
            {"entity_id": "lock.secret", "state": "locked", "last_changed": "t0"},
            {"state": "unlocked", "last_changed": "t1"},
        ],
    ]

    result = filter_rest_history(ctx, payload)
    flat = json.dumps(result)
    assert "climate.trend" in flat and "21" in flat, "granted series kept whole"
    assert "lock.secret" not in flat and "unlocked" not in flat, "denied gone whole"


async def test_rest_history_also_handles_the_dict_shape(
    hass: HomeAssistant,
) -> None:
    """The REST endpoint can also answer in the entity-keyed mapping shape."""
    from custom_components.ha_rbac.filters import filter_rest_history  # noqa: PLC0415

    ctx = _past_ctx(hass, readable={"sensor.ok"}, history=set())
    payload = {
        "sensor.ok": [{"s": "1", "a": {}, "lu": 1}],
        "lock.secret": [{"s": "locked", "a": {}, "lu": 1}],
    }
    result = filter_rest_history(ctx, payload)
    assert set(result) == {"sensor.ok"}


async def test_statistics_drop_a_denied_entity(hass: HomeAssistant) -> None:
    """Statistics were keyed like history and left unfiltered -- a second leak.

    `history/statistics_during_period` answers `{statistic_id: [rows]}`, and a
    recorder statistic for an entity uses the entity id as its key while each
    row names no entity of its own. The generic walk recovered nothing from the
    key, so a denied entity's numbers passed straight through.
    """
    ctx = _past_ctx(hass, readable={"sensor.power_ok"}, history=set())
    stats = {
        "sensor.power_ok": [{"start": "t0", "mean": 1.0}],
        "sensor.power_secret": [{"start": "t0", "mean": 9.0}],
    }
    result = REGISTRY.filter_result("history/statistics_during_period", ctx, stats)
    assert set(result) == {"sensor.power_ok"}


async def test_statistics_leave_an_external_statistic_alone(
    hass: HomeAssistant,
) -> None:
    """An external statistic id like `energy:solar` is not an entity to gate."""
    ctx = _past_ctx(hass, readable=set(), history=set())
    stats = {
        "energy:solar": [{"start": "t0", "sum": 5.0}],
        "sensor.secret": [{"start": "t0", "mean": 9.0}],
    }
    result = REGISTRY.filter_result("recorder/statistics_during_period", ctx, stats)
    assert "energy:solar" in result, "not an entity; left alone"
    assert "sensor.secret" not in result, "an entity, and denied"


async def test_statistics_grant_keeps_a_granted_entity(
    hass: HomeAssistant,
) -> None:
    """A history grant covers the downsampled statistic as well as raw history."""
    ctx = _past_ctx(hass, readable=set(), history={"sensor.power_trend"})
    stats = {
        "sensor.power_trend": [{"start": "t0", "mean": 1.0}],
        "sensor.power_secret": [{"start": "t0", "mean": 9.0}],
    }
    result = REGISTRY.filter_result("history/statistics_during_period", ctx, stats)
    assert set(result) == {"sensor.power_trend"}


async def test_past_readable_falls_back_to_read_without_a_grant(
    hass: HomeAssistant,
) -> None:
    """A context built with no history callback behaves exactly as before.

    Every hand-built `FilterContext` in the codebase omits the history check,
    so history there must remain "whatever the role can read" -- the change is
    additive, never a silent widening.
    """
    ctx = _ctx(hass, {"lock.front"})
    assert ctx.past_readable("history", "light.kitchen") is True
    assert ctx.past_readable("history", "lock.front") is False
    assert ctx.past_readable("logbook", "light.kitchen") is True
    assert ctx.past_readable("logbook", "lock.front") is False


async def test_logbook_drops_a_row_about_a_denied_entity(
    hass: HomeAssistant,
) -> None:
    """A row whose `entity_id` the role cannot see is withheld whole."""
    result = REGISTRY.filter_result(
        "logbook/get_events",
        _past_ctx(hass, readable={"light.kitchen"}),
        [
            {"entity_id": "light.kitchen", "when": 1, "state": "on"},
            {"entity_id": "lock.front", "when": 2, "state": "unlocked"},
        ],
    )
    assert [r["entity_id"] for r in result] == ["light.kitchen"]


async def test_logbook_drops_a_row_caused_by_a_denied_entity(
    hass: HomeAssistant,
) -> None:
    """The `context_entity_id` leak, closed for every role whether it grants or not.

    A row names an entity twice: `entity_id` is what it is about, and
    `context_entity_id` is what caused it. "The front door unlocked because Alice
    arrived" carries the door under the first, readable, and the person under the
    second, denied. The generic walk only ever looked at the first, so the person
    leaked -- who came home, disclosed through a door the role legitimately
    watches. The row goes whole, because either end naming something unseen is
    enough and a row stripped of its cause still discloses the timing.
    """
    result = REGISTRY.filter_result(
        "logbook/get_events",
        _past_ctx(hass, readable={"lock.front"}),
        [
            {
                "entity_id": "lock.front",
                "when": 1,
                "state": "unlocked",
                "context_entity_id": "person.alice",
                "context_message": "triggered by Alice",
            }
        ],
    )
    assert result == [], "a row caused by a denied entity is withheld whole"
    assert "person.alice" not in json.dumps(result)


async def test_logbook_follows_a_grant_not_only_read(hass: HomeAssistant) -> None:
    """An entity granted logbook but not read still comes back in the logbook."""
    result = REGISTRY.filter_result(
        "logbook/get_events",
        _past_ctx(hass, readable={"light.kitchen"}, logbook={"climate.trend"}),
        [
            {"entity_id": "light.kitchen", "when": 1},
            {"entity_id": "climate.trend", "when": 2},
            {"entity_id": "lock.secret", "when": 3},
        ],
    )
    assert [r["entity_id"] for r in result] == ["light.kitchen", "climate.trend"]


async def test_logbook_context_is_allowed_when_the_cause_is_granted(
    hass: HomeAssistant,
) -> None:
    """A cause the role may see, by grant or by read, does not drop the row."""
    result = REGISTRY.filter_result(
        "logbook/get_events",
        _past_ctx(hass, readable={"lock.front"}, logbook={"person.alice"}),
        [{"entity_id": "lock.front", "when": 1, "context_entity_id": "person.alice"}],
    )
    assert len(result) == 1, "both ends are visible, so the row stays"


async def test_the_two_grant_sections_are_independent(hass: HomeAssistant) -> None:
    """A history grant is not a logbook grant, and the other way round.

    They are separate sections precisely so a role can be shown a trend without
    the timeline of who caused it, which is the difference between "the house was
    cold on Tuesday" and "somebody turned the heating down at 9pm". Sharing one
    compiler must not quietly merge them.
    """
    ctx = _past_ctx(
        hass, readable=set(), history={"climate.trend"}, logbook={"lock.front"}
    )

    history = REGISTRY.filter_result(
        "history/history_during_period",
        ctx,
        {"climate.trend": [{"s": "20"}], "lock.front": [{"s": "locked"}]},
    )
    assert set(history) == {"climate.trend"}, "the logbook grant buys no history"

    logbook = REGISTRY.filter_result(
        "logbook/get_events",
        ctx,
        [
            {"entity_id": "climate.trend", "when": 1},
            {"entity_id": "lock.front", "when": 2},
        ],
    )
    assert [r["entity_id"] for r in logbook] == ["lock.front"], (
        "and the history grant buys no logbook"
    )


async def test_logbook_event_stream_frame_is_filtered(hass: HomeAssistant) -> None:
    """A streamed logbook frame wraps its rows under `events`."""
    event = REGISTRY.filter_event(
        "logbook/event_stream",
        _past_ctx(hass, readable={"light.kitchen"}),
        {
            "events": [
                {"entity_id": "light.kitchen", "when": 1},
                {"entity_id": "lock.front", "when": 2},
            ],
            "start_time": 1,
        },
    )
    assert [r["entity_id"] for r in event["events"]] == ["light.kitchen"]
    assert event["start_time"] == 1, "the frame around it survives"


async def test_a_logbook_frame_emptied_by_filtering_is_dropped(
    hass: HomeAssistant,
) -> None:
    """A frame with no rows left is not forwarded as an empty one.

    Same reasoning as an emptied state diff: forwarding the husk tells the role
    that something happened, and when. The panel's first frame is the exception
    and the proxy makes it -- see `opening_frame`.
    """
    event = REGISTRY.filter_event(
        "logbook/event_stream",
        _past_ctx(hass, readable=set()),
        {"events": [{"entity_id": "lock.front", "when": 1}], "start_time": 1},
    )
    assert event is None


async def test_rest_logbook_is_filtered_including_context(
    hass: HomeAssistant,
) -> None:
    """The REST endpoint is narrowed the same way, context included.

    It had no filter of its own, so it fell to the generic walk -- which never
    looked at `context_entity_id` either.
    """
    from custom_components.ha_rbac.filters import filter_rest_logbook  # noqa: PLC0415

    result = filter_rest_logbook(
        _past_ctx(hass, readable={"lock.front"}),
        [
            {"entity_id": "lock.front", "when": 1},
            {"entity_id": "lock.front", "when": 2, "context_entity_id": "person.alice"},
            {"entity_id": "camera.hall", "when": 3},
            {"when": 4, "name": "Home Assistant", "message": "started"},
        ],
    )
    assert [r.get("entity_id") for r in result] == ["lock.front", None], (
        "the row it may see and the entity-less system row stay"
    )
    assert "person.alice" not in json.dumps(result)


def test_the_opening_frame_of_each_blocking_subscription_has_a_shape() -> None:
    """Pin what stands in for an emptied opening frame, per subscription.

    `subscribe_entities` gets the `{"a": {}}` Home Assistant sends a user with no
    readable states. The logbook stream gets the real frame with its rows removed,
    because only the real one carries the window it covers -- inventing a
    `start_time` would be inventing an answer. Everything else gets nothing,
    which is what keeps an emptied frame from becoming a clock.
    """
    from custom_components.ha_rbac.filters import opening_frame  # noqa: PLC0415

    assert opening_frame(
        "subscribe_entities", {"a": {"lock.front": {"s": "locked"}}}
    ) == {"a": {}}
    assert opening_frame(
        "logbook/event_stream",
        {"events": [{"entity_id": "lock.front"}], "start_time": 7},
    ) == {"events": [], "start_time": 7}
    assert opening_frame("subscribe_events", {"event_type": "state_changed"}) is None
    assert opening_frame("history/stream", {"states": {}}) is None
