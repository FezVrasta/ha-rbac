"""Regressions for defects found in review.

Each test here demonstrated a real failure before its fix. They are kept
together because they document how this layer can fail *open*, which is the
only failure mode that matters.
"""

import json
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
    REASON_APP,
    REASON_TIER,
    Decider,
    Decision,
    _invokes_a_service,
)
from custom_components.ha_rbac.extract import extract
from custom_components.ha_rbac.filters import REGISTRY, FilterContext
from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Permissions,
    compile_role,
    default_roles,
    desugar,
)
from custom_components.ha_rbac.proxy import (
    INIT_HEADERS_FILTER,
    MAX_ENDPOINTS,
    MAX_PENDING_IDS,
    _carries_entity_data,
    _WsSession,
)
from custom_components.ha_rbac.store import RbacStore


class _AllowAll:
    """A decider that permits everything, to isolate the framing logic."""

    @staticmethod
    def decide(*args: Any, **kwargs: Any) -> Decision:
        return Decision(allowed=True)


class _SentinelClient:
    """Collects the frames a session writes back to its client."""

    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    async def send_str(self, raw: str) -> None:
        self._sent.append(raw)


def _lookup(hass: HomeAssistant) -> PermissionLookup:
    return PermissionLookup(er.async_get(hass), dr.async_get(hass))


def _role(role_id: str = "r", **kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": role_id,
        "name": role_id,
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
    sent: list[str] = []
    session = _WsSession.__new__(_WsSession)
    session._pending = OrderedDict()
    session._streaming = {}
    session._endpoints = OrderedDict()
    session._highest_id = 0
    session._user = None
    session._permissions = Permissions(pass_through=True)
    session._client = _SentinelClient(sent)

    assert await session._intercept({"id": 5, "type": "get_states"}) is True
    for filler in range(6, 6 + MAX_PENDING_IDS + 10):
        assert await session._intercept({"id": filler, "type": "get_config"}) is True

    assert 5 not in session._pending, "precondition: the entry was evicted"
    # The reuse check no longer depends on the evicted entry.
    assert await session._intercept({"id": 5, "type": "get_config"}) is False
    assert session._correlate(5) is None
    assert "Message id reused" in sent[-1]


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


async def test_a_service_call_naming_no_entity_is_judged_by_the_service(
    hass: HomeAssistant, decider: Decider
) -> None:
    """`persistent_notification.create` targets nothing, and must still work.

    Treating every targetless call as unbounded refused the lot, so a role could
    not raise a notification, send anything, or run a script.
    """
    await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()
    assert (
        decider._catalog.service_is_admin_only("persistent_notification", "create")
        is False
    )

    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            }
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "persistent_notification",
            "service": "create",
            "service_data": {"message": "hello"},
        },
    )
    assert decision.allowed is True


async def test_a_read_only_role_cannot_call_a_targetless_service(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Bound by the service, not unbound: a reader still may not act."""
    await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()

    decision = decider.decide(
        _read_only(hass),
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "persistent_notification",
            "service": "create",
            "service_data": {"message": "hello"},
        },
    )
    assert decision.allowed is False


async def test_an_administrative_service_needs_the_admin_tier(
    hass: HomeAssistant, decider: Decider
) -> None:
    """Home Assistant records which services are administrative; that is read.

    `homeassistant.restart` is registered with async_register_admin_service, so
    no list of service names is needed to recognise it.
    """
    await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()

    assert decider._catalog.service_is_admin_only("homeassistant", "restart") is True

    role = compile_role(
        hass,
        _role(
            allow={
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            }
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "call_service",
        {"type": "call_service", "domain": "homeassistant", "service": "restart"},
    )
    assert decision.allowed is False
    assert decision.reason == REASON_TIER


async def test_an_unknown_service_is_treated_as_administrative(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A service this build has never seen cannot be reasoned about."""
    assert decider._catalog.service_is_admin_only("nope", "nope") is True


async def test_a_denied_app_is_removed_from_the_sidebar(hass: HomeAssistant) -> None:
    """Add-ons are panels, so one rule covers both them and built-in apps."""
    ctx = FilterContext(hass, lambda e, k: True, lambda url: url != "energy")
    panels = {
        "energy": {"title": "Energy"},
        "history": {"title": "History"},
        "a0d7b954_nodered": {"title": "Node-RED"},
    }
    result = REGISTRY.filter_result("get_panels", ctx, panels)
    assert set(result) == {"history", "a0d7b954_nodered"}


async def test_app_rules_are_deny_by_default_open(hass: HomeAssistant) -> None:
    """No app rules means the tier gate alone decides, as before."""
    role = compile_role(hass, _role(), _lookup(hass))
    assert Permissions(roles=[role]).app_allowed("energy") is True


async def test_an_app_denial_survives_another_role_allowing_it(
    hass: HomeAssistant,
) -> None:
    """Denials win, so composing roles cannot hand back a hidden app."""
    denying = compile_role(
        hass, _role("a", apps={"allow": [], "deny": ["energy"]}), _lookup(hass)
    )
    permissive = compile_role(hass, _role("b"), _lookup(hass))
    perms = Permissions(roles=[denying, permissive])
    assert perms.app_allowed("energy") is False
    assert perms.app_allowed("history") is True


async def test_an_app_allow_list_hides_everything_else(hass: HomeAssistant) -> None:
    """An explicit list is the 'only these' case."""
    role = compile_role(
        hass,
        _role(apps={"allow": ["history", "a0d7b954_*"], "deny": []}),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.app_allowed("history") is True
    assert perms.app_allowed("a0d7b954_nodered") is True
    assert perms.app_allowed("energy") is False


async def test_denying_an_app_also_refuses_the_commands_behind_it(
    hass: HomeAssistant,
) -> None:
    """Hiding a sidebar entry is cosmetic; the address bar still works."""
    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "frontend", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    decider = Decider(hass, catalog, REGISTRY)

    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            apps={"allow": [], "deny": ["energy"]},
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])

    # Only meaningful if the panel is actually registered on this instance.
    if any(app["url_path"] == "energy" for app in catalog.apps()):
        decision = decider.decide(
            perms, KIND_WS, "energy/info", {"type": "energy/info"}
        )
        assert decision.allowed is False
        assert decision.reason == REASON_APP


async def test_denying_one_dashboard_leaves_the_others_alone(
    hass: HomeAssistant,
) -> None:
    """Dashboards are panels, but they all share the `lovelace/` commands.

    Matching by command prefix would take every dashboard down with one of
    them, so a dashboard is matched by the `url_path` the request carries.
    """
    await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    # Two dashboards, as a real instance has.
    catalog.apps = lambda: [
        {
            "url_path": "lovelace",
            "title": "Overview",
            "kind": "lovelace",
            "addon": None,
        },
        {"url_path": "map", "title": "Map", "kind": "lovelace", "addon": None},
    ]
    # Isolate the app gate: this command is not registered in this
    # fixture, so the tier gate would answer first and prove nothing.
    catalog.tier_for = lambda command: TIER_OPEN
    decider = Decider(hass, catalog, REGISTRY)

    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            apps={"allow": [], "deny": ["map"]},
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])

    blocked = decider.decide(
        perms,
        KIND_WS,
        "lovelace/config",
        {"type": "lovelace/config", "url_path": "map"},
    )
    allowed = decider.decide(
        perms,
        KIND_WS,
        "lovelace/config",
        {"type": "lovelace/config", "url_path": "lovelace"},
    )
    assert blocked.allowed is False
    assert blocked.reason == REASON_APP
    assert allowed.allowed is True


async def test_the_default_dashboard_is_not_refused_by_accident(
    hass: HomeAssistant,
) -> None:
    """`lovelace/config` with no url_path means the default dashboard.

    Guessing which one that is and refusing it would take out the home screen
    for everyone, so an unnamed dashboard is left alone.
    """
    await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    catalog.apps = lambda: [
        {"url_path": "lovelace", "title": "Overview", "kind": "lovelace", "addon": None}
    ]
    catalog.tier_for = lambda command: TIER_OPEN
    decider = Decider(hass, catalog, REGISTRY)

    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            apps={"allow": [], "deny": ["lovelace"]},
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "lovelace/config",
        {"type": "lovelace/config"},
    )
    assert decision.allowed is True


async def test_a_denied_addon_is_refused_at_the_supervisor_api(
    hass: HomeAssistant,
) -> None:
    """An add-on is reached through an endpoint that names its slug."""
    await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    catalog.apps = lambda: [
        {
            "url_path": "a0d7b954_nodered",
            "title": "Node-RED",
            "kind": "app",
            "addon": "a0d7b954_nodered",
        }
    ]
    catalog.tier_for = lambda command: TIER_OPEN
    decider = Decider(hass, catalog, REGISTRY)

    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            apps={"allow": [], "deny": ["a0d7b954_nodered"]},
        ),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "hassio/api",
        {
            "type": "hassio/api",
            "endpoint": "/addons/a0d7b954_nodered/info",
            "method": "get",
        },
    )
    assert decision.allowed is False
    assert decision.reason == REASON_APP


async def test_a_template_reading_attributes_is_refused_when_any_are_hidden(
    hass: HomeAssistant, decider: Decider
) -> None:
    """A render reports the entities it read, never the attributes.

    So `{{ state_attr('person.me', 'latitude') }}` would come back through the
    response filter looking like an ordinary read of an entity the role can see.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            attributes={"rules": [{"ids": [], "names": ["latitude", "longitude"]}]},
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])

    blocked = decider.decide(
        perms,
        KIND_WS,
        "render_template",
        {
            "type": "render_template",
            "template": "{{ state_attr('person.me','latitude') }}",
        },
    )
    assert blocked.allowed is False

    # A template that reads no attribute is unaffected.
    allowed = decider.decide(
        perms,
        KIND_WS,
        "render_template",
        {"type": "render_template", "template": "{{ states('person.me') }}"},
    )
    assert allowed.allowed is True


async def test_a_role_without_attribute_rules_keeps_templates(
    hass: HomeAssistant, decider: Decider
) -> None:
    """The restriction only applies to roles that actually withhold something."""
    role = compile_role(
        hass,
        _role(allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}}),
        _lookup(hass),
    )
    decision = decider.decide(
        Permissions(roles=[role]),
        KIND_WS,
        "render_template",
        {
            "type": "render_template",
            "template": "{{ state_attr('person.me','latitude') }}",
        },
    )
    assert decision.allowed is True


async def test_attribute_patterns_match_by_glob(hass: HomeAssistant) -> None:
    """`gps_*` is easier to write than every attribute a tracker reports."""
    role = compile_role(
        hass,
        _role(attributes={"rules": [{"ids": [], "names": ["gps_*", "latitude"]}]}),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.attribute_hidden("person.me", "gps_accuracy") is True
    assert perms.attribute_hidden("person.me", "latitude") is True
    assert perms.attribute_hidden("person.me", "friendly_name") is False
    assert perms.hides_attributes is True


async def test_an_attribute_rule_is_scoped_to_its_target(
    hass: HomeAssistant,
) -> None:
    """Hiding a person's location must leave the zone that defines home alone."""
    role = compile_role(
        hass,
        _role(
            attributes={
                "rules": [
                    {
                        "target": "domains",
                        "ids": ["person", "device_tracker"],
                        "names": ["latitude", "longitude"],
                    }
                ]
            }
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.attribute_hidden("person.me", "latitude") is True
    assert perms.attribute_hidden("device_tracker.phone", "latitude") is True
    assert perms.attribute_hidden("zone.home", "latitude") is False


async def test_an_attribute_rule_can_name_one_entity(hass: HomeAssistant) -> None:
    """The narrowest case: this lock's code, not every lock's."""
    role = compile_role(
        hass,
        _role(
            attributes={
                "rules": [
                    {
                        "target": "entity_ids",
                        "ids": ["lock.front_door"],
                        "names": ["code"],
                    }
                ]
            }
        ),
        _lookup(hass),
    )
    perms = Permissions(roles=[role])
    assert perms.attribute_hidden("lock.front_door", "code") is True
    assert perms.attribute_hidden("lock.back_door", "code") is False


async def test_the_original_untargeted_shape_still_works(
    hass: HomeAssistant,
) -> None:
    """Roles written before rules existed must keep working after an upgrade."""
    role = compile_role(hass, _role(attributes={"deny": ["latitude"]}), _lookup(hass))
    perms = Permissions(roles=[role])
    assert perms.attribute_hidden("person.me", "latitude") is True
    assert perms.attribute_hidden("zone.home", "latitude") is True


async def test_the_owner_keeps_every_attribute(hass: HomeAssistant) -> None:
    """Pass-through means pass-through."""
    perms = Permissions(pass_through=True)
    assert perms.attribute_hidden("person.me", "latitude") is False
    assert perms.hides_attributes is False


async def test_every_transport_gets_the_same_filter_context(
    hass: HomeAssistant,
) -> None:
    """One constructor, so a transport cannot silently lose a restriction.

    Building contexts by hand at each call site meant the HTTP path lost
    attribute hiding while the websocket path kept it: the same role withheld a
    location over one transport and served it over the other.
    """
    role = compile_role(
        hass,
        _role(
            allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            attributes={"rules": [{"ids": [], "names": ["latitude"]}]},
        ),
        _lookup(hass),
    )
    ctx = FilterContext.for_user(hass, Permissions(roles=[role]))

    assert ctx.hides_attributes is True
    assert ctx.strip_attributes("person.me", {"latitude": 51.5, "battery": 80}) == {
        "battery": 80
    }

    # And a role with no attribute rules must still pay nothing.
    plain = FilterContext.for_user(
        hass, Permissions(roles=[compile_role(hass, _role(), _lookup(hass))])
    )
    assert plain.hides_attributes is False


def _admin_clone(hass: HomeAssistant, **extra: Any) -> Any:
    """Compile a role cloned from Administrator, plus one restriction."""
    return compile_role(
        hass,
        {
            "id": "clone",
            "name": "Clone",
            "allow": {"entities": True},
            "tiers": {"max": TIER_ADMIN, "allow": ["*"], "deny": []},
            **extra,
        },
        _lookup(hass),
    )


async def test_an_unrestricted_admin_clone_is_full_access(hass: HomeAssistant) -> None:
    """The precondition for the three tests below: without a restriction it is."""
    assert _admin_clone(hass).full_access is True


@pytest.mark.parametrize(
    "restriction",
    [
        {"attributes": {"rules": [{"names": ["latitude"]}]}},
        {"attributes": {"deny": ["latitude"]}},
        {"apps": {"allow": ["lovelace"]}},
        {"apps": {"deny": ["core_ssh"]}},
    ],
    ids=["targeted-rules", "legacy-deny", "app-allowlist", "app-denylist"],
)
async def test_a_restricted_admin_clone_is_not_full_access(
    hass: HomeAssistant, restriction: dict[str, Any]
) -> None:
    """Full access skips every gate, so it has to mean nothing is restricted.

    Cloning Administrator and withholding one thing is the obvious authoring
    flow. Compiling that to full access made the proxy serve exactly what the
    role was written to withhold -- and an app *allow* list restricts every app
    not on it, which is a stronger statement than any deny list.
    """
    assert _admin_clone(hass, **restriction).full_access is False


async def test_attribute_rule_domains_are_lowercased(hass: HomeAssistant) -> None:
    """Entity ids are lowercase, so a domain typed `Light` has to match too."""
    role = compile_role(
        hass,
        {
            "id": "shouty",
            "name": "Shouty",
            "allow": {"entities": {"all": {"read": True}}},
            "attributes": {
                "rules": [{"target": "domains", "ids": ["Light"], "names": ["rgb"]}]
            },
        },
        _lookup(hass),
    )
    permissions = Permissions(roles=[role])
    assert permissions.attribute_hidden("light.kitchen", "rgb") is True


async def test_creating_a_role_cannot_replace_a_predefined_one(
    hass: HomeAssistant,
) -> None:
    """It would apply in memory and vanish on restart, which reads as a bug."""
    store = RbacStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="predefined"):
        await store.async_create_role({"id": ROLE_READ_ONLY, "name": "Impostor"})
    assert store.roles[ROLE_READ_ONLY]["system_generated"] is True


async def test_a_non_state_change_event_is_pruned(hass: HomeAssistant) -> None:
    """Registering a filter for `subscribe_events` bypasses the generic walk.

    A `call_service` event names its targets under `service_data`, so matching
    only a top-level `entity_id` forwarded it whole to a restricted subscriber.
    """
    ctx = FilterContext(hass, lambda entity_id, key: entity_id != "lock.front")
    event = REGISTRY.filter_event(
        "subscribe_events",
        ctx,
        {
            "event_type": "call_service",
            "data": {
                "domain": "lock",
                "service": "unlock",
                "service_data": {"entity_id": "lock.front"},
            },
        },
    )
    leaked = json.dumps(event) if event is not None else ""
    assert "lock.front" not in leaked


async def test_supervisor_calls_in_flight_are_refused_not_forgotten() -> None:
    """Evicting a correlation would leave its reply unjudged.

    `strip_denied_addons` only fires while the reply is still tied to the
    endpoint that asked for it, so forgetting an older call let a Supervisor
    listing come back with every denied add-on in it.
    """
    sent: list[str] = []
    session = _WsSession.__new__(_WsSession)
    session._pending = OrderedDict()
    session._streaming = {}
    session._endpoints = OrderedDict()
    session._highest_id = 0
    session._user = None
    session._permissions = Permissions()
    session._client = _SentinelClient(sent)
    session._record = lambda *args: None
    session._decider = _AllowAll()

    for msg_id in range(1, MAX_ENDPOINTS + 1):
        assert (
            await session._intercept(
                {"id": msg_id, "type": "supervisor/api", "endpoint": "/addons"}
            )
            is True
        )

    overflow = MAX_ENDPOINTS + 1
    assert (
        await session._intercept(
            {"id": overflow, "type": "supervisor/api", "endpoint": "/addons"}
        )
        is False
    )
    assert len(session._endpoints) == MAX_ENDPOINTS
    assert 1 in session._endpoints, "the oldest call kept its correlation"


async def test_denying_settings_leaves_the_registries_readable(
    hass: HomeAssistant,
) -> None:
    """`config/` is Home Assistant's namespace, not the Settings panel's.

    The convention that an app's data comes from commands sharing its name is a
    guess. For Settings it was wrong in the worst way: `config/area_registry/list`
    and its siblings are what every dashboard reads before it can draw, so
    hiding one sidebar entry left the user staring at a dashboard that failed
    to load.
    """
    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "frontend", {})
    await hass.async_block_till_done()

    catalog = Catalog(hass)
    catalog.rebuild()
    decider = Decider(hass, catalog, REGISTRY)
    perms = Permissions(
        roles=[
            compile_role(
                hass,
                _role(
                    allow={CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                    tiers={"max": TIER_ADMIN, "allow": ["*"], "deny": []},
                    apps={"allow": [], "deny": ["config"]},
                ),
                _lookup(hass),
            )
        ]
    )
    assert any(app["url_path"] == "config" for app in catalog.apps()), (
        "precondition: the Settings panel is registered"
    )

    for command in (
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list_for_display",
        "config/floor_registry/list",
    ):
        decision = decider.decide(perms, KIND_WS, command, {"type": command})
        assert decision.allowed is True, f"{command} must still be readable"

    # Changing something through the panel that was denied is still refused.
    blocked = decider.decide(
        perms,
        KIND_WS,
        "config/area_registry/create",
        {"type": "config/area_registry/create", "name": "New"},
    )
    assert blocked.allowed is False
    assert blocked.reason == REASON_APP
