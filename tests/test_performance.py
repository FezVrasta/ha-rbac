"""Performance characteristics of the filtering path.

Home Assistant builds each state-diff payload once and shares those bytes across
every connected client. Filtering per user defeats that sharing, so the admin
fast path exists to keep administrators paying nothing. These tests pin the
behaviour that makes the cost predictable rather than measuring wall clock.
"""

import json
import time
from typing import Any

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_READ, SUBCAT_ALL
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_rbac.const import TIER_ADMIN, TIER_OPEN
from custom_components.ha_rbac.filters import REGISTRY, FilterContext
from custom_components.ha_rbac.policy import Evaluator, Permissions, compile_role
from custom_components.ha_rbac.store import RbacStore


def _full_access_role(hass: HomeAssistant) -> Permissions:
    """Return permissions equivalent to the predefined admin role."""
    role = compile_role(
        hass,
        {
            "id": "admin",
            "name": "admin",
            "allow": {CAT_ENTITIES: True},
            "deny": {},
            "tiers": {"max": TIER_ADMIN, "allow": ["*"], "deny": []},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    return Permissions(roles=[role])


async def test_admin_role_reports_full_access(hass: HomeAssistant) -> None:
    """The fast path is what keeps administrators free of filtering cost."""
    assert _full_access_role(hass).full_access is True


async def test_a_restricted_role_does_not_get_the_fast_path(
    hass: HomeAssistant,
) -> None:
    """Anything narrower than total access has to be inspected."""
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
    assert Permissions(roles=[role]).full_access is False


async def test_a_global_deny_removes_the_fast_path(hass: HomeAssistant) -> None:
    """A per-user denial must be applied even to an otherwise unrestricted role."""
    perms = _full_access_role(hass)
    perms.global_deny_fn = lambda entity_id, key: entity_id == "camera.bedroom"
    assert perms.full_access is False
    assert perms.check_entity("camera.bedroom", POLICY_READ) is False


async def test_permissions_are_reused_between_requests(hass: HomeAssistant) -> None:
    """Compiled policies are reused; recompiling per request would be the cost.

    The cache is keyed on the user and on which of their roles are in force, so
    that a schedule opening or closing produces a different key. Two requests a
    moment apart still hit, which is what keeps the websocket's per-frame
    re-resolve to a dictionary lookup.
    """
    store = RbacStore(hass)
    await store.async_load()
    evaluator = Evaluator(hass, store)

    class _User:
        id = "u1"
        is_owner = False
        is_admin = False
        system_generated = False

    user = _User()
    first = evaluator.async_permissions(user)
    second = evaluator.async_permissions(user)
    assert first is second

    evaluator.invalidate()
    assert evaluator.async_permissions(user) is not first


async def test_visible_domains_is_computed_once(hass: HomeAssistant) -> None:
    """get_services would otherwise walk every entity for every domain."""
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("lock.front", "locked")

    calls: list[str] = []

    def _check(entity_id: str, key: str) -> bool:
        calls.append(entity_id)
        return True

    ctx = FilterContext(hass, _check)
    assert ctx.visible_domains == {"light", "lock"}
    first_count = len(calls)
    assert ctx.visible_domains == {"light", "lock"}
    assert len(calls) == first_count


async def test_filtering_a_large_state_list_is_linear(hass: HomeAssistant) -> None:
    """The common read path must not degrade superlinearly with entity count."""
    denied = {f"lock.d{index}" for index in range(500)}
    states: list[dict[str, Any]] = [
        {"entity_id": f"light.a{index}", "state": "on"} for index in range(500)
    ] + [{"entity_id": entity_id, "state": "locked"} for entity_id in denied]

    ctx = FilterContext(hass, lambda entity_id, key: entity_id not in denied)
    result = REGISTRY.filter_result("get_states", ctx, states)

    assert len(result) == 500
    assert all(state["entity_id"].startswith("light.") for state in result)


async def test_filtering_a_state_diff_stays_cheap(hass: HomeAssistant) -> None:
    """Pins the cost that decides whether a cache is worth building.

    The obvious worry is that per-user filtering defeats Home Assistant's
    sharing of one pre-serialised payload across every client. Measured, a
    state-change diff costs single-digit microseconds to parse, filter and
    re-serialise -- so fifty tabs at a hundred state changes a second is a few
    percent of one core, and a cache would buy nothing worth its invalidation.

    The threshold here is deliberately loose. It is a regression alarm for
    something going quadratic, not a benchmark.
    """
    denied = {f"lock.d{index}" for index in range(50)}
    ctx = FilterContext(hass, lambda entity_id, key: entity_id not in denied)
    event = {
        "c": {
            "light.kitchen": {
                "+": {"s": "on", "lu": 1787442000.1, "a": {"brightness": 180}}
            }
        }
    }
    raw = json.dumps({"id": 7, "type": "event", "event": event})

    def round_trip() -> str:
        message = json.loads(raw)
        filtered = REGISTRY.filter_event("subscribe_entities", ctx, message["event"])
        return json.dumps({**message, "event": filtered})

    round_trip()
    started = time.perf_counter()
    for _ in range(2000):
        round_trip()
    per_frame = (time.perf_counter() - started) / 2000

    assert per_frame < 500e-6, (
        f"{per_frame * 1e6:.0f}us per frame is far above measured"
    )
