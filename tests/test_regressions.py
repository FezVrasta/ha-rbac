"""Regressions for defects found in review.

Each test here demonstrated a real failure before its fix. They are kept
together because they document how this layer can fail *open*, which is the
only failure mode that matters.
"""

from typing import Any

import pytest
import voluptuous as vol
from aiohttp import hdrs
from homeassistant.auth.permissions.const import (
    CAT_ENTITIES,
    POLICY_CONTROL,
    POLICY_READ,
    SUBCAT_ALL,
)
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import Catalog, build_routes
from custom_components.ha_rbac.const import TIER_ADMIN, TIER_OPEN
from custom_components.ha_rbac.decide import (
    KIND_HTTP,
    KIND_WS,
    REASON_UNBOUNDED,
    Decider,
)
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Permissions,
    compile_role,
    desugar,
)
from custom_components.ha_rbac.proxy import INIT_HEADERS_FILTER
from custom_components.ha_rbac.store import RbacStore


def _lookup(hass: HomeAssistant) -> PermissionLookup:
    return PermissionLookup(er.async_get(hass), dr.async_get(hass))


def _role(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "r",
        "name": "r",
        "allow": {},
        "deny": {},
        "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
    }
    base.update(kwargs)
    return base


def _read_only(hass: HomeAssistant) -> Permissions:
    role = compile_role(
        hass,
        _role(allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}}),
        _lookup(hass),
    )
    return Permissions(roles=[role])


@pytest.fixture(name="decider")
async def decider_fixture(hass: HomeAssistant) -> Decider:
    """Return a decider backed by a real command catalogue."""
    for domain in ("websocket_api", "config", "api", "conversation"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()
    return Decider(hass, catalog, REGISTRY)


async def test_a_command_that_cannot_be_bounded_or_filtered_is_denied(
    hass: HomeAssistant,
) -> None:
    """A command open to any user, naming nothing, with no response filter.

    `conversation/process` is the real example: it executes intents, matches no
    write-shaped name, carries no domain and service, and names none of the
    entities it will act on. It was therefore classified as an unbounded read
    and allowed, so a read-only user could say "unlock the front door" and have
    it happen.

    The command is registered synthetically here so the test exercises the rule
    rather than depending on which integrations a fixture happens to load.
    """
    await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    @websocket_api.websocket_command(
        {vol.Required("type"): "test/acts_without_saying_so", vol.Required("text"): str}
    )
    @callback
    def _handler(hass_: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        """Do nothing; only its registration matters."""

    websocket_api.async_register_command(hass, _handler)

    catalog = Catalog(hass)
    catalog.rebuild()
    decider = Decider(hass, catalog, REGISTRY)

    # Guard against passing for the wrong reason: an unknown command would be
    # denied by the tier gate and prove nothing.
    assert catalog.tier_for("test/acts_without_saying_so") == TIER_OPEN

    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "test/acts_without_saying_so",
        {"type": "test/acts_without_saying_so", "text": "unlock the front door"},
    )
    assert decision.allowed is False
    assert decision.reason == REASON_UNBOUNDED


async def test_execute_script_is_checked_for_control(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Its name is not write-shaped and it carries no top-level domain/service.

    An admin-tier role restricted to reading was granted it, because the gate
    asked for READ access to the entities the script targets.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            tiers={"max": TIER_ADMIN, "allow": [], "deny": []},
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "execute_script",
        {
            "type": "execute_script",
            "sequence": [
                {"service": "lock.unlock", "target": {"entity_id": "lock.front"}}
            ],
        },
    )
    assert decision.allowed is False


async def test_full_access_respects_a_tier_denial(hass: HomeAssistant) -> None:
    """Cloning Administrator and denying config/* must not skip every gate.

    full_access short-circuits all inspection, so computing it without
    consulting tier_deny silently disabled the whole layer for that role.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: True},
            tiers={"max": TIER_ADMIN, "allow": ["*"], "deny": ["config/*"]},
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.full_access is False
    assert perms.tier_allowed("config/core/update_location", TIER_ADMIN) is False


async def test_full_access_respects_an_entity_denial(hass: HomeAssistant) -> None:
    """A deny block that desugars to nothing today may not tomorrow."""
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: True},
            deny={CAT_ENTITIES: {"entity_ids": {"lock.front": True}}},
            tiers={"max": TIER_ADMIN, "allow": ["*"], "deny": []},
        ),
        _lookup(hass),
    )
    assert Permissions(roles=[role]).full_access is False


async def test_desugar_accepts_a_valid_home_assistant_policy(
    hass: HomeAssistant,
) -> None:
    """`{"entities": {"all": True}}` is HA's own schema and must not crash.

    It raised TypeError, which propagated out of permission resolution and
    bricked every request from the bound user.
    """
    role = ROLE_SCHEMA({"id": "x", "name": "x", "allow": {CAT_ENTITIES: {"all": True}}})
    policy = desugar(hass, role["allow"])
    compiled = compile_role(hass, role, _lookup(hass))
    assert policy[CAT_ENTITIES]["all"] is True
    assert compiled.check("light.kitchen", POLICY_CONTROL) is True


async def test_query_string_resources_are_extracted(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A REST caller naming an entity in the query string must be checked.

    `?filter_entity_id=lock.front` was invisible to the resource gate, and
    minimal_response history omits entity_id from all but the first sample, so
    the generic response filter could not recover it either.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            deny={CAT_ENTITIES: {"domains": {"lock": True}}},
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_HTTP,
        "GET /api/history/period",
        {},
        query={"filter_entity_id": "lock.front", "minimal_response": ""},
    )
    assert decision.allowed is False


async def test_route_matching_prefers_the_more_specific_url() -> None:
    """A permissive pattern from another integration must not shadow a core one.

    Routes were ordered by URL length, so `/api/{username}` (15 characters)
    outranked `/api/error_log` (14) and downgraded an admin-only endpoint to
    open.
    """
    import homeassistant.components.api  # noqa: F401, PLC0415
    from homeassistant.components.http import HomeAssistantView  # noqa: PLC0415

    class _ShadowingView(HomeAssistantView):
        """A permissive catch-all, as emulated_hue registers."""

        url = "/api/{username}"
        name = "shadow:test"
        requires_auth = False

        async def get(self, request: object, username: str) -> None:
            """Do nothing."""

    routes = build_routes()

    def _match(path: str) -> str:
        for route in routes:
            if route.pattern.match(path):
                return route.url
        return ""

    assert _match("/api/error_log") == "/api/error_log"
    assert _match("/api/states") == "/api/states"
    assert _match("/api/states/light.kitchen") == "/api/states/{entity_id}"


async def test_client_supplied_forwarded_headers_are_discarded(
    hass: HomeAssistant,
) -> None:
    """A client must not be able to claim its own source address.

    Home Assistant trusts X-Forwarded-For from a configured proxy, so relaying
    whatever the client sent would let anyone spoof their address past IP
    banning and the trusted_networks auth provider.
    """
    assert hdrs.X_FORWARDED_FOR in INIT_HEADERS_FILTER
    assert hdrs.X_FORWARDED_HOST in INIT_HEADERS_FILTER
    assert hdrs.X_FORWARDED_PROTO in INIT_HEADERS_FILTER


async def test_a_malformed_stored_role_is_skipped_not_fatal(
    hass: HomeAssistant,
) -> None:
    """Failing setup would leave the proxy unbound and cut off all access.

    On a loopback-only deployment that is the only route in, so one unreadable
    role must not take the installation down with it.
    """
    store = RbacStore(hass)
    await store._store.async_save(
        {
            "roles": {"broken": {"id": "broken", "tiers": {"max": "not-a-tier"}}},
            "bindings": {},
            "global_deny": {},
        }
    )

    await store.async_load()

    assert "broken" not in store.roles
    # The predefined roles are still available, so users keep working.
    assert "read_only" in store.roles
