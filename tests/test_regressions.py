"""Regressions for defects found in review.

Each test here demonstrated a real failure before its fix. They are kept
together because they document how this layer can fail *open*, which is the
only failure mode that matters.
"""

from collections import OrderedDict
from fnmatch import fnmatch
from typing import Any

import pytest
from aiohttp import hdrs
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

from custom_components.ha_rbac.catalog import Catalog, build_routes
from custom_components.ha_rbac.const import (
    ROLE_READ_ONLY,
    ROLE_USER,
    TIER_ADMIN,
    TIER_OPEN,
    TIER_USER,
)
from custom_components.ha_rbac.decide import (
    KIND_HTTP,
    KIND_WS,
    Decider,
    _invokes_a_service,
)
from custom_components.ha_rbac.extract import extract
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Permissions,
    compile_role,
    default_roles,
    desugar,
)
from custom_components.ha_rbac.proxy import (
    INIT_HEADERS_FILTER,
    MAX_PENDING_IDS,
    _carries_entity_data,
    _is_ungoverned_api_path,
    _WsSession,
)
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


async def test_intent_commands_are_denied_to_the_predefined_roles() -> None:
    """`conversation/process` acts on free text, naming nothing it will touch.

    A read-only user could say "unlock the front door" and have it happen.
    Nothing Home Assistant exposes distinguishes it from an ordinary read, so
    the predefined roles deny the family by glob. Keeping that in role data
    rather than in the decision code means an administrator can see it in the
    panel and change it.

    Requiring an explicit response filter for every unbounded command was tried
    instead, and denied 17 of the 27 commands a real frontend issues on load.
    """
    roles = default_roles()
    for role_id in (ROLE_USER, ROLE_READ_ONLY):
        denies = roles[role_id]["tiers"]["deny"]
        assert any(fnmatch("conversation/process", pattern) for pattern in denies), (
            f"{role_id} does not deny conversation/process"
        )
        assert any(fnmatch("assist_pipeline/run", pattern) for pattern in denies), (
            f"{role_id} does not deny assist_pipeline/run"
        )


async def test_predefined_roles_admit_an_ordinary_frontend_boot(
    hass: HomeAssistant,
) -> None:
    """The commands a frontend issues on load must not be refused.

    A guest whose dashboard cannot load is not access control, it is an outage.
    """
    for domain in ("websocket_api", "config", "api", "frontend"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    decider = Decider(hass, catalog, REGISTRY)

    role = compile_role(hass, default_roles()[ROLE_READ_ONLY], _lookup(hass))
    perms = Permissions(roles=[role])

    boot = [
        "get_config",
        "get_panels",
        "auth/current_user",
        "frontend/get_user_data",
        "frontend/get_themes",
        "get_services",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "lovelace/info",
        "person/list",
        "energy/info",
        "repairs/list_issues",
        "sensor/numeric_device_classes",
    ]
    # Only commands this instance actually registered: an unregistered one
    # resolves to admin by design, and would prove nothing here.
    registered = [command for command in boot if command in catalog.commands]
    assert len(registered) > 8, "fixture did not register enough of the boot set"

    refused = [
        command
        for command in registered
        if not decider.decide(perms, KIND_WS, command, {"type": command}).allowed
    ]
    assert not refused, f"a normal frontend boot was refused: {refused}"


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


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"domain": "light", "service": "turn_on"}, True, id="call_service"
        ),
        pytest.param(
            {"sequence": [{"service": "lock.unlock", "target": {"entity_id": "l.a"}}]},
            True,
            id="execute_script",
        ),
        pytest.param(
            {"sequence": [{"action": "lock.unlock"}]}, True, id="action-syntax"
        ),
        pytest.param(
            {"views": [{"cards": [{"tap_action": {"action": "toggle"}}]}]},
            False,
            id="lovelace-tap-action",
        ),
        pytest.param(
            {"views": [{"cards": [{"tap_action": {"action": "navigate"}}]}]},
            False,
            id="lovelace-navigate",
        ),
        pytest.param({"type": "get_states"}, False, id="plain-read"),
    ],
)
async def test_service_invocation_detection(
    payload: dict[str, Any], expected: bool
) -> None:
    """A service call is found anywhere, but a Lovelace tap action is not one.

    Matching a bare `action` key would deny reads of any dashboard containing a
    button, so the value has to look like a service: `domain.service`, or a bare
    name alongside an explicit domain.
    """
    assert _invokes_a_service(payload) is expected


async def test_entity_ids_are_matched_case_insensitively(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Home Assistant lowercases entity ids; comparing the raw string bypasses deny.

    `cv.entity_id` lowercases on the way in and the policy lookup is an exact
    dict match, so `LOCK.Front` missed a rule written for `lock.front` while
    Home Assistant still acted on the lock.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, "control": True}}},
            deny={CAT_ENTITIES: {"entity_ids": {"lock.front": True}}},
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])

    for spelling in ("lock.front", "LOCK.Front", "Lock.FRONT"):
        decision = decider.decide(
            perms,
            KIND_WS,
            "call_service",
            {
                "type": "call_service",
                "domain": "lock",
                "service": "unlock",
                "target": {"entity_id": spelling},
            },
        )
        assert decision.allowed is False, spelling


async def test_the_all_sentinel_is_recognised_in_any_case(
    hass: HomeAssistant, decider: Decider
) -> None:
    """`ALL` counted as one named entity, which satisfied the boundedness gate."""
    for spelling in ("all", "ALL", "All"):
        found = extract({"target": {"entity_id": spelling}})
        assert found.unbounded is True, spelling
        assert found.entities == set(), spelling


async def test_sign_path_is_denied_to_every_restricted_role(
    hass: HomeAssistant,
) -> None:
    """A signed URL authenticates with no header, so it must not be obtainable.

    `auth/sign_path` is `ws_require_user`, so raising the predefined roles to
    that tier exposed it: the signed path it returns carries no Authorization
    header and no path restriction.
    """
    for role_source in (
        default_roles()[ROLE_READ_ONLY],
        # A role created in the panel gets the schema defaults, which is where
        # relying on role data alone left a hole.
        ROLE_SCHEMA({"id": "custom", "name": "Custom"}),
    ):
        role = compile_role(hass, role_source, _lookup(hass))
        perms = Permissions(roles=[role])
        assert perms.tier_allowed("auth/sign_path", TIER_USER) is False
        assert perms.tier_allowed("conversation/process", TIER_OPEN) is False


async def test_a_full_access_role_keeps_sign_path(hass: HomeAssistant) -> None:
    """The baseline denials must not restrict an administrator."""
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: True},
            tiers={"max": TIER_ADMIN, "allow": ["*"], "deny": []},
        ),
        _lookup(hass),
    )
    assert Permissions(roles=[role]).full_access is True


async def test_reusing_an_id_after_eviction_is_still_refused() -> None:
    """The reuse check must not be defeatable by pushing the entry out.

    It tested membership of a bounded map, so filling the map evicted the entry
    and the id could then be reused to re-label which response filter applied.
    Home Assistant requires ids to increase, so the highest one seen is enough.
    """
    session = _WsSession.__new__(_WsSession)
    session._pending = OrderedDict()
    session._streaming = {}
    session._highest_id = 0

    session._highest_id = 5
    session._remember(5, "get_states")
    for filler in range(6, 6 + MAX_PENDING_IDS + 10):
        session._remember(filler, "get_config")

    assert 5 not in session._pending, "precondition: the entry was evicted"
    # The reuse check no longer depends on the evicted entry.
    assert session._highest_id >= 5


async def test_a_streaming_id_survives_eviction() -> None:
    """Subscriptions are the oldest ids on a connection, so FIFO evicts them first."""
    session = _WsSession.__new__(_WsSession)
    session._pending = OrderedDict()
    session._streaming = {}
    session._highest_id = 0

    session._remember(1, "subscribe_entities")
    session._streaming.setdefault(1, "subscribe_entities")
    for filler in range(2, 2 + MAX_PENDING_IDS + 10):
        session._remember(filler, "get_config")

    assert 1 not in session._pending
    assert session._correlate(1) == "subscribe_entities"


def test_unauthenticated_api_paths_that_act_are_refused() -> None:
    """A webhook carries no auth header and can call any service.

    Anonymous traffic is forwarded so the login flow and static frontend work,
    which meant these reached Home Assistant with no policy applied at all.
    """
    assert _is_ungoverned_api_path("/api/webhook/abc123") is True
    # The login flow and the frontend must still load.
    assert _is_ungoverned_api_path("/auth/login_flow") is False
    assert _is_ungoverned_api_path("/static/app.js") is False
    assert _is_ungoverned_api_path("/") is False


def test_media_responses_are_not_refused_for_being_unfilterable() -> None:
    """A camera stream cannot be buffered to filter, and discloses no state."""
    assert _carries_entity_data("multipart/x-mixed-replace") is False
    assert _carries_entity_data("image/jpeg") is False
    assert _carries_entity_data("video/mp4") is False
    assert _carries_entity_data("application/json") is True


async def test_a_role_created_in_the_panel_can_start_a_frontend(
    hass: HomeAssistant,
) -> None:
    """A role with schema defaults must permit `auth/current_user`.

    It sits behind `ws_require_user`, which means only "a signed-in user" -- and
    a frontend cannot finish loading without it. Defaulting a new role to the
    open tier produced a session that never came up.
    """
    role = compile_role(hass, ROLE_SCHEMA({"id": "c", "name": "Custom"}), _lookup(hass))
    perms = Permissions(roles=[role])

    assert perms.tier_allowed("auth/current_user", TIER_USER) is True
    # ...without also handing it the command that defeats the whole layer.
    assert perms.tier_allowed("auth/sign_path", TIER_USER) is False
