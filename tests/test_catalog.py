"""Tests for runtime derivation of the permission surface.

The catalogue is built from undocumented Home Assistant internals -- the
`__wrapped__` chain of a decorator closure, and the shape of `hass.data`. If a
future release changes either, tier derivation degrades toward "open", which
fails *open*. These tests are the alarm for that.
"""

import pathlib
from functools import wraps

import homeassistant
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import (
    Catalog,
    derive_tier,
    introspection_works,
)
from custom_components.ha_rbac.const import CAPABILITIES, TIER_ADMIN, TIER_OPEN
from custom_components.ha_rbac.policy import default_roles
from testsupport.ast_oracle import scan

# The oracle scans the Home Assistant source that is actually installed, so it
# always agrees with the runtime the rest of the suite exercises.
CORE_ROOT = pathlib.Path(homeassistant.__file__).parent


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
        """Do nothing; only the wrapper is inspected."""

    def handler_b() -> None:
        """Do nothing; only the wrapper is inspected."""

    assert derive_tier(require_admin(async_response(handler_a))) == TIER_ADMIN
    assert derive_tier(async_response(require_admin(handler_b))) == TIER_ADMIN
    assert derive_tier(async_response(handler_a)) == TIER_OPEN


@pytest.fixture(name="broad_catalog")
async def broad_catalog_fixture(hass: HomeAssistant) -> Catalog:
    """Return a catalogue with the components the capabilities name."""
    for domain in (
        "websocket_api",
        "config",
        "automation",
        "script",
        "scene",
        "lovelace",
        "counter",
        "timer",
        "input_boolean",
        "tag",
        "backup",
        "blueprint",
        "person",
    ):
        await async_setup_component(hass, domain, {domain: {}})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    return catalog


async def test_every_capability_reaches_something(broad_catalog: Catalog) -> None:
    """A capability matching nothing is a checkbox that silently does nothing.

    Home Assistant renames its namespaces from time to time, and a pattern left
    behind by one of those fails in the direction nobody notices: the box is
    still there, ticking it still saves, and the access never arrives.

    A REST pattern -- one with a method in front of a path -- counts on its own,
    because the config views that serve `/api/config/scene/config/{id}` build
    their URL per instance and so are absent from the derived route table. That
    is why scenes match no websocket command at all.
    """
    matched = {
        capability["id"]
        for capability in broad_catalog.capabilities()
        if capability["commands"]
    }
    for capability in CAPABILITIES:
        rest_only = any(" " in pattern for pattern in capability["patterns"])
        assert capability["id"] in matched or rest_only, capability["id"]


async def test_capabilities_do_not_overlap(broad_catalog: Catalog) -> None:
    """Each command belongs to one group, so revoking one means what it says."""
    seen: dict[str, str] = {}
    for capability in broad_catalog.capabilities():
        for command in capability["commands"]:
            assert command not in seen, (
                f"{command} is in both {seen.get(command)} and {capability['id']}"
            )
            seen[command] = capability["id"]


async def test_the_predefined_roles_name_real_capabilities() -> None:
    """An unknown capability id grants nothing and raises nothing.

    That is the right way round for a role written by a newer build, and the
    wrong way round for a typo in our own presets: the Editor would quietly ship
    with less than it claims.
    """
    known = {capability["id"] for capability in CAPABILITIES}
    for role in default_roles().values():
        assert set(role.get("capabilities") or []) <= known, role["id"]


async def test_a_static_path_derives_as_open(
    hass: HomeAssistant, tmp_path: pathlib.Path
) -> None:
    """Home Assistant hands these to anyone, so a role must not gate them.

    A static path is not a `HomeAssistantView`, so it is absent from the derived
    route table and resolved to the fail-closed admin default. That refused a
    camera snapshot under `/local` to the very people it was put there for,
    while the same request carrying no token at all was forwarded and answered
    -- which is what a dashboard's `<img>` sends.

    Both shapes `async_register_static_paths` produces are covered, because it
    registers a directory and a single file in different ways.
    """
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    await async_setup_component(hass, "http", {})
    (tmp_path / "robots.txt").write_text("")
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig("/local", str(tmp_path), False),
            StaticPathConfig("/robots.txt", str(tmp_path / "robots.txt"), False),
        ]
    )
    catalog = Catalog(hass)
    catalog.rebuild()

    assert catalog.tier_for_request("GET", "/local") == TIER_OPEN
    assert catalog.tier_for_request("GET", "/local/tmp/snapshot.jpg") == TIER_OPEN
    assert catalog.tier_for_request("HEAD", "/local/tmp/snapshot.jpg") == TIER_OPEN
    assert catalog.tier_for_request("GET", "/robots.txt") == TIER_OPEN


async def test_the_admin_default_survives_the_static_exception(
    hass: HomeAssistant, tmp_path: pathlib.Path
) -> None:
    """Only a path Home Assistant really serves off disk may take this way out.

    The exception is narrow on purpose: the verbs the static router answers, the
    file itself rather than a namespace under it, and never in front of a view.
    A static path registered over `/api` must not downgrade the admin-only
    endpoints beneath it, which is why views are consulted first.
    """
    import homeassistant.components.api  # noqa: F401, PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    await async_setup_component(hass, "http", {})
    (tmp_path / "robots.txt").write_text("")
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig("/local", str(tmp_path), False),
            StaticPathConfig("/robots.txt", str(tmp_path / "robots.txt"), False),
            StaticPathConfig("/api", str(tmp_path), False),
        ]
    )
    catalog = Catalog(hass)
    catalog.rebuild()

    assert catalog.tier_for_request("POST", "/local/tmp/snapshot.jpg") == TIER_ADMIN
    assert catalog.tier_for_request("GET", "/robots.txt/anything") == TIER_ADMIN
    assert catalog.tier_for_request("GET", "/nothing/registered") == TIER_ADMIN
    assert catalog.tier_for_request("GET", "/api/error_log") == TIER_ADMIN


async def test_a_view_that_gates_on_admin_inline_is_not_read_as_open(
    hass: HomeAssistant,
) -> None:
    """GHSA-rf3j-8w24-5pc8: the decorator is not the only way core gates admin.

    `derive_tier` reads Home Assistant's `require_admin` decorator off the
    handler. Several core views carry no decorator and check
    `request["hass_user"].is_admin` in the body instead, raising `Unauthorized`
    themselves. Those derived as *open* -- not as the fail-closed admin default
    used for a route this build has never seen -- so the tier gate imposed
    nothing on them for any role.

    `POST /api/states/{entity_id}` is the one to hold onto: it overwrites what
    Home Assistant reports for an entity without going near the device, which
    is why core reserves it to administrators, and a role with ordinary control
    of that entity satisfied the resource gate.
    """
    import homeassistant.components.api  # noqa: F401, PLC0415

    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "api", {})
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()

    assert catalog.tier_for_request("POST", "/api/states/light.kitchen") == TIER_ADMIN
    assert catalog.tier_for_request("DELETE", "/api/states/light.kitchen") == TIER_ADMIN


async def test_reading_the_state_list_stays_open(hass: HomeAssistant) -> None:
    """The check that must not be mistaken for a gate.

    `APIStatesView.get` reads `is_admin` too, but to choose whether to filter
    the list rather than whether to answer at all -- both branches return
    states. Treating that as a gate would make the single most-used endpoint in
    Home Assistant administrator-only and empty every restricted dashboard,
    which is why the detector wants a refusal beside the flag rather than the
    flag alone.
    """
    import homeassistant.components.api  # noqa: F401, PLC0415

    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "api", {})
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()

    assert catalog.tier_for_request("GET", "/api/states") == TIER_OPEN


def test_the_inline_gate_detector_reads_both_shapes() -> None:
    """Raising and answering 401 are both refusals; branching is not."""
    from homeassistant.exceptions import Unauthorized  # noqa: PLC0415

    from custom_components.ha_rbac.catalog import gates_on_admin  # noqa: PLC0415

    def raises(request):
        if not request["hass_user"].is_admin:
            raise Unauthorized

    def answers_401(request):
        if not request["hass_user"].is_admin:
            return {"status": 401}
        return {"status": 200}

    def branches(request):
        return "all" if request["hass_user"].is_admin else "mine"

    def unrelated(request):
        return request.query.get("q")

    assert gates_on_admin(raises) is True
    assert gates_on_admin(answers_401) is True
    assert gates_on_admin(branches) is False
    assert gates_on_admin(unrelated) is False
