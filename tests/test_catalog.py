"""Tests for runtime derivation of the permission surface.

The catalogue is built from undocumented Home Assistant internals -- the
`__wrapped__` chain of a decorator closure, and the shape of `hass.data`. If a
future release changes either, tier derivation degrades toward "open", which
fails *open*. These tests are the alarm for that.
"""

import pathlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import (
    Catalog,
    derive_tier,
    introspection_works,
)
from custom_components.ha_rbac.const import TIER_ADMIN, TIER_OPEN
from testsupport.ast_oracle import scan

CORE_ROOT = pathlib.Path("/Users/federicozivolo/Developer/core/homeassistant")


@pytest.fixture(name="catalog")
async def catalog_fixture(hass: HomeAssistant) -> Catalog:
    """Return a catalogue built from a running Home Assistant."""
    for domain in ("websocket_api", "config", "search", "history", "logbook"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    return catalog


async def test_catalogue_is_populated(catalog: Catalog) -> None:
    """A running instance must yield a non-trivial command surface."""
    assert len(catalog.commands) > 50


async def test_admin_derivation_agrees_with_the_source(catalog: Catalog) -> None:
    """Every command the source marks require_admin must derive as admin.

    This is the regression that matters: a mismatch means HA changed how
    require_admin is implemented and the layer is now failing open.
    """
    declared = scan(CORE_ROOT)

    disagreements = [
        command
        for command, info in catalog.commands.items()
        if (static := declared.get(command)) is not None
        and static.admin
        and info.tier != TIER_ADMIN
    ]
    assert not disagreements, (
        "these commands are @require_admin in source but did not derive as admin: "
        f"{sorted(disagreements)}"
    )


async def test_derivation_finds_no_phantom_admins(catalog: Catalog) -> None:
    """A command not marked admin in source must not derive as admin."""
    declared = scan(CORE_ROOT)

    phantom = [
        command
        for command, info in catalog.commands.items()
        if (static := declared.get(command)) is not None
        and not static.admin
        and info.tier == TIER_ADMIN
    ]
    assert not phantom, f"derived admin for non-admin commands: {sorted(phantom)}"


async def test_known_admin_commands_are_gated(catalog: Catalog) -> None:
    """Spot-check commands whose admin status is load-bearing."""
    for command in ("execute_script", "fire_event", "subscribe_trigger"):
        assert catalog.tier_for(command) == TIER_ADMIN, command


async def test_render_template_is_not_admin(catalog: Catalog) -> None:
    """render_template is open to any user, which is why boundedness matters."""
    assert catalog.tier_for("render_template") == TIER_OPEN


async def test_render_template_declares_only_an_optional_resource(
    catalog: Catalog,
) -> None:
    """Its entity_ids is a rendering hint, not a constraint."""
    info = catalog.info_for("render_template")
    assert info is not None
    assert info.required_resources == set()
    assert info.optional_resources == {"entity_ids"}


async def test_call_service_declares_target_as_optional(catalog: Catalog) -> None:
    """Pins the fact that broke the first boundedness rule."""
    info = catalog.info_for("call_service")
    assert info is not None
    assert "target" in info.optional_resources
    assert "target" not in info.required_resources


async def test_unknown_command_defaults_to_admin(catalog: Catalog) -> None:
    """An unregistered command is treated as the most restrictive thing."""
    assert catalog.tier_for("some/command/that/does/not/exist") == TIER_ADMIN


async def test_write_commands_are_recognised_by_shape(catalog: Catalog) -> None:
    """Mutations are matched by a regex rather than enumerated."""
    info = catalog.info_for("config/entity_registry/update")
    assert info is not None
    assert info.is_write is True


async def test_introspection_self_test_passes_on_this_ha(
    hass: HomeAssistant,
) -> None:
    """The mechanism is checked against HA's real decorators, not by statistics."""
    assert introspection_works() is True


async def test_catalogue_reports_degradation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream rename of the wrapper must disable enforcement, not fail open."""
    await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    assert catalog.degraded is False

    # Simulate upstream replacing require_admin with something unrecognisable.
    monkeypatch.setattr(
        "homeassistant.components.websocket_api.decorators.require_admin",
        lambda func: func,
    )
    catalog.rebuild()
    assert catalog.degraded is True


def test_derive_tier_survives_both_decorator_orderings() -> None:
    """Verified against the two orderings that appear in core."""
    from functools import wraps

    def require_admin(func):
        @wraps(func)
        def with_admin(*args, **kwargs):
            return func(*args, **kwargs)

        return with_admin

    def async_response(func):
        @wraps(func)
        def schedule_handler(*args, **kwargs):
            return func(*args, **kwargs)

        return schedule_handler

    def handler_a() -> None:
        """Handler."""

    def handler_b() -> None:
        """Handler."""

    assert derive_tier(require_admin(async_response(handler_a))) == TIER_ADMIN
    assert derive_tier(async_response(require_admin(handler_b))) == TIER_ADMIN
    assert derive_tier(async_response(handler_a)) == TIER_OPEN
