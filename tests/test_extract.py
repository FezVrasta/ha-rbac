"""Tests for the generic resource extractor."""

from typing import Any

import pytest
import voluptuous as vol

from custom_components.ha_rbac.extract import (
    Extracted,
    entity_ids_in,
    extract,
    is_bounded,
    schema_resource_markers,
)


@pytest.mark.parametrize(
    ("payload", "entities", "devices", "areas"),
    [
        pytest.param(
            {
                "type": "call_service",
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.kitchen"},
            },
            {"light.kitchen"},
            set(),
            set(),
            id="call_service-target-entity",
        ),
        pytest.param(
            {
                "type": "call_service",
                "domain": "light",
                "service": "turn_on",
                "target": {"area_id": ["kitchen", "hall"], "device_id": "abc123"},
            },
            set(),
            {"abc123"},
            {"kitchen", "hall"},
            id="call_service-target-area-and-device",
        ),
        pytest.param(
            {
                "type": "call_service",
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.a"},
                "service_data": {"entity_id": ["light.b"]},
            },
            {"light.a", "light.b"},
            set(),
            set(),
            id="service_data-hides-more-entities",
        ),
        pytest.param(
            {
                "trigger": [{"platform": "device", "device_id": "dev1"}],
                "action": [
                    {"service": "light.turn_on", "target": {"entity_id": "light.c"}}
                ],
            },
            {"light.c"},
            {"dev1"},
            set(),
            id="automation-config-nested",
        ),
        pytest.param(
            {
                "type": "history/history_during_period",
                "entity_ids": ["sensor.a", "sensor.b"],
            },
            {"sensor.a", "sensor.b"},
            set(),
            set(),
            id="entity_ids-list",
        ),
        pytest.param({"type": "ping"}, set(), set(), set(), id="no-resources"),
    ],
)
def test_extract_collects_resources(
    payload: dict[str, Any],
    entities: set[str],
    devices: set[str],
    areas: set[str],
) -> None:
    """Resources are collected wherever they appear in the payload."""
    result = extract(payload)
    assert result.entities == entities
    assert result.devices == devices
    assert result.areas == areas
    assert not result.truncated


def test_entity_id_all_sentinel_is_unbounded() -> None:
    """`entity_id: all` names every entity, so it is not a bound."""
    result = extract({"type": "call_service", "target": {"entity_id": "all"}})
    assert result.unbounded is True
    assert result.entities == set()


def test_entity_id_none_sentinel_is_not_a_resource() -> None:
    """`entity_id: none` names nothing and is not an entity id."""
    result = extract({"target": {"entity_id": "none"}})
    assert result.unbounded is False
    assert result.entities == set()


def test_deep_nesting_is_truncated_not_silently_dropped() -> None:
    """A walk that hits its cap reports truncation so callers can fail closed."""
    payload: dict[str, Any] = {"entity_id": "light.top"}
    node = payload
    for _ in range(40):
        node["nested"] = {}
        node = node["nested"]
    node["entity_id"] = "light.buried"

    result = extract(payload)
    assert result.truncated is True
    assert not is_bounded(result, {"entity_id"})


def test_wide_payload_is_truncated() -> None:
    """The node cap bounds work for attacker-controlled payloads."""
    result = extract({"sequence": [{"n": i} for i in range(6000)]})
    assert result.truncated is True


def test_non_string_values_are_ignored() -> None:
    """Malformed payloads must not crash or inject junk resource ids."""
    result = extract({"entity_id": {"unexpected": "shape"}, "device_id": 42})
    assert result.entities == set()
    assert result.devices == set()


def test_schema_resource_markers_separates_required_from_optional() -> None:
    """Provenance drives the boundedness rule, so it must be exact."""
    schema = vol.Schema(
        {
            vol.Required("type"): "render_template",
            vol.Required("template"): str,
            vol.Optional("entity_ids"): list,
        }
    )
    required, optional = schema_resource_markers(schema)
    assert required == set()
    assert optional == {"entity_ids"}


def test_schema_resource_markers_counts_target_as_a_resource_field() -> None:
    """`call_service` is bounded solely through `target`."""
    schema = vol.Schema(
        {
            vol.Required("type"): "call_service",
            vol.Required("domain"): str,
            vol.Required("service"): str,
            vol.Optional("target"): dict,
        }
    )
    _, optional = schema_resource_markers(schema)
    assert "target" in optional


def test_a_template_defeats_a_decoy_resource_list() -> None:
    """The rule that stops render_template exfiltrating via a decoy list."""
    payload = {"template": "{{ states('lock.front_door') }}", "entity_ids": ["sun.sun"]}
    result = extract(payload)
    assert result.entities == {"sun.sun"}
    assert result.templated is True
    assert is_bounded(result) is False


def test_template_hidden_in_service_data_is_caught() -> None:
    """A schema-shape rule would miss this; scanning the payload does not."""
    result = extract(
        {
            "type": "call_service",
            "domain": "notify",
            "service": "persistent_notification",
            "target": {"entity_id": "light.kitchen"},
            "service_data": {"message": "{{ states('lock.front_door') }}"},
        }
    )
    assert result.templated is True
    assert is_bounded(result) is False


def test_statement_template_syntax_is_caught() -> None:
    """`{% ... %}` is a template too."""
    result = extract(
        {
            "entity_id": "light.a",
            "x": "{% for s in states %}{{ s.entity_id }}{% endfor %}",
        }
    )
    assert result.templated is True


def test_plain_string_is_not_a_template() -> None:
    """Ordinary strings must not be mistaken for templates."""
    result = extract({"entity_id": "light.a", "message": "kitchen is on"})
    assert result.templated is False
    assert is_bounded(result) is True


def test_call_service_with_optional_target_still_bounds() -> None:
    """`call_service` declares `target` optional; requiring it would deny all control."""
    result = extract(
        {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.a"},
        }
    )
    assert is_bounded(result) is True


def test_naming_nothing_is_not_bounded() -> None:
    """A payload that names no resource cannot bound its command."""
    assert is_bounded(Extracted()) is False


def test_entity_ids_in_finds_them_wherever_a_card_puts_them() -> None:
    """Lovelace names entities under a dozen keys, and custom cards add more.

    So the walk tests every string for the shape of an entity id and then
    against the machine, rather than carrying a list of card schemas.
    """
    known = {
        "light.kitchen",
        "lock.front",
        "camera.porch",
        "sensor.power",
        "binary_sensor.door",
    }
    config = {
        "views": [
            {
                "badges": [{"type": "entity", "entity": "binary_sensor.door"}],
                "cards": [
                    {"type": "light", "entity": "light.kitchen"},
                    {
                        "type": "entities",
                        "entities": ["lock.front", {"entity": "sensor.power"}],
                    },
                    {"type": "picture-glance", "camera_image": "camera.porch"},
                    # A card nobody has heard of, naming one under its own key.
                    {"type": "custom:whatever", "some_new_key": "sensor.power"},
                ],
            }
        ]
    }
    assert entity_ids_in(config, known.__contains__) == known


def test_entity_ids_in_ignores_strings_that_only_look_like_one() -> None:
    """A title with a dot in it is not an entity, and neither is a dead id."""
    known = {"light.kitchen"}
    config = {
        "title": "Ground floor",
        "theme": "custom.theme",
        "cards": [
            {"entity": "light.kitchen"},
            {"entity": "light.removed_last_year"},
            {"url": "https://example.com/x.png"},
        ],
    }
    assert entity_ids_in(config, known.__contains__) == {"light.kitchen"}


def test_an_entity_named_as_a_mapping_key_is_found() -> None:
    """`scene.apply` says which states to reproduce by keying on entity id."""
    payload = {"entities": {"lock.front": "unlocked", "light.a": {"state": "on"}}}
    assert entity_ids_in(payload, {"lock.front", "light.a"}.__contains__) == {
        "lock.front",
        "light.a",
    }


def test_a_media_source_uri_names_the_entity_in_its_tail() -> None:
    """And only where the registry agrees that tail is really an entity."""
    known = {"camera.bedroom"}.__contains__
    assert entity_ids_in(
        {"media_content_id": "media-source://camera/camera.bedroom"}, known
    ) == {"camera.bedroom"}
    assert (
        entity_ids_in(
            {"media_content_id": "media-source://media_source/local/song.mp3"}, known
        )
        == set()
    )
