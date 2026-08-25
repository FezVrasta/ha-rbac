"""Tests for the add-on ingress gate.

Home Assistant serves `/api/hassio_ingress/{token}/...` with
`requires_auth = False`, so these requests reach the proxy with no bearer token
and the ordinary decision path cannot judge them. Denying an add-on used to
hide its panel and refuse its Supervisor endpoints while leaving the add-on's
own web UI reachable to anyone holding its ingress path, which is stable for
the life of the installation.
"""

import time

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_rbac.filters import FilterContext, strip_denied_addons
from custom_components.ha_rbac.ingress import (
    MISS_RELOAD_INTERVAL,
    SESSION_TTL,
    IngressGuard,
    session_from,
    token_from,
)


def _ctx(hass: HomeAssistant, denied: set[str]) -> FilterContext:
    """Return a context denying the given apps."""
    return FilterContext(
        hass, lambda entity_id, key: True, lambda app: app not in denied
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/hassio_ingress/abc123/", "abc123"),
        ("/api/hassio_ingress/abc123/deep/path", "abc123"),
        ("/api/hassio_ingress/abc123", "abc123"),
        ("/api/hassio_ingress/", None),
        ("/api/states", None),
        ("/api/hassio/addons/core_ssh/info", None),
    ],
    ids=["trailing-slash", "nested", "bare", "empty", "unrelated", "near-miss"],
)
def test_token_from(path: str, expected: str | None) -> None:
    """Only an ingress path yields a token, and it is the first segment."""
    assert token_from(path) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"session": "s1"}, "s1"),
        ({"data": {"session": "s1"}}, "s1"),
        ({"data": {}}, None),
        ({"session": 5}, None),
        ("not a dict", None),
    ],
    ids=["unwrapped", "wrapped", "absent", "wrong-type", "not-a-mapping"],
)
def test_session_from(payload: object, expected: str | None) -> None:
    """Supervisor's two response shapes both yield the session."""
    assert session_from(payload) == expected


async def test_guard_maps_session_to_user(hass: HomeAssistant) -> None:
    """A session minted on an authenticated connection identifies its user."""
    guard = IngressGuard(hass)
    guard.remember_session("sess-1", "user-1")
    assert guard.user_id_for("sess-1") == "user-1"


async def test_guard_rejects_unknown_session(hass: HomeAssistant) -> None:
    """A session the proxy never saw cannot be tied to anyone, so it is nobody.

    This is what makes the gate fail closed: an attacker who mints a session
    out of band, or replays one from before a restart, resolves to no user and
    is refused rather than passed through.
    """
    guard = IngressGuard(hass)
    assert guard.user_id_for("never-seen") is None
    assert guard.user_id_for(None) is None


async def test_guard_expires_stale_sessions(hass: HomeAssistant) -> None:
    """A session past its lifetime stops identifying anyone."""
    guard = IngressGuard(hass)
    guard.remember_session("sess-1", "user-1")
    guard._sessions["sess-1"] = ("user-1", time.monotonic() - SESSION_TTL - 1)
    assert guard.user_id_for("sess-1") is None


async def test_guard_touch_extends_session(hass: HomeAssistant) -> None:
    """Revalidation keeps a panel that stays open from expiring under the user."""
    guard = IngressGuard(hass)
    guard.remember_session("sess-1", "user-1")
    guard._sessions["sess-1"] = ("user-1", time.monotonic() - SESSION_TTL + 5)
    guard.touch_session("sess-1")
    assert guard.user_id_for("sess-1") == "user-1"


async def test_guard_without_supervisor_maps_nothing(hass: HomeAssistant) -> None:
    """With no Supervisor there is no ingress, and no token resolves."""
    assert "hassio" not in hass.config.components
    guard = IngressGuard(hass)
    assert await guard.async_slug_for("anything") is None


async def test_addon_listing_drops_denied_addons(hass: HomeAssistant) -> None:
    """`/addons` names no add-on, so the app gate never fires on it."""
    result = strip_denied_addons(
        _ctx(hass, {"core_ssh"}),
        "/addons",
        {"addons": [{"slug": "core_ssh"}, {"slug": "core_configurator"}]},
    )
    assert [entry["slug"] for entry in result["addons"]] == ["core_configurator"]


async def test_ingress_panels_drops_denied_addons(hass: HomeAssistant) -> None:
    """The panel listing is keyed by slug rather than carrying it as a value."""
    result = strip_denied_addons(
        _ctx(hass, {"core_ssh"}),
        "/ingress/panels",
        {
            "data": {
                "panels": {"core_ssh": {"title": "Terminal"}, "core_configurator": {}}
            }
        },
    )
    assert set(result["data"]["panels"]) == {"core_configurator"}


async def test_unrelated_endpoint_is_untouched(hass: HomeAssistant) -> None:
    """Only the listings are rewritten; everything else passes through."""
    payload = {"addons": [{"slug": "core_ssh"}]}
    assert (
        strip_denied_addons(_ctx(hass, {"core_ssh"}), "/supervisor/info", payload)
        is payload
    )


async def test_a_miss_rebuilds_the_token_map(hass: HomeAssistant) -> None:
    """An add-on installed since the map was built must not slip through.

    Its token is absent from a map that is otherwise still current, and
    answering "not an add-on" forwards the request unguarded -- so a miss has
    to rebuild rather than trust what it has.
    """
    guard = IngressGuard(hass)
    loads = 0

    async def _fake_load() -> None:
        nonlocal loads
        loads += 1
        guard._slugs = {"fresh": "core_ssh"}
        guard._loaded_at = time.monotonic()

    guard._async_load = _fake_load
    guard._slugs = {"known": "core_configurator"}
    guard._loaded_at = time.monotonic() - MISS_RELOAD_INTERVAL - 1

    # A hit is answered from the map without troubling Supervisor.
    assert await guard.async_slug_for("known") == "core_configurator"
    assert loads == 0

    # A miss rebuilds, and finds the add-on installed since.
    assert await guard.async_slug_for("fresh") == "core_ssh"
    assert loads == 1


async def test_repeated_misses_are_rate_limited(hass: HomeAssistant) -> None:
    """Rebuilding costs a Supervisor call per add-on, and misses are forgeable.

    The cost of the limit is a window, no longer than the interval, in which an
    add-on installed moments ago is not yet in the map. That is the tradeoff
    being made here, not an oversight.
    """
    guard = IngressGuard(hass)
    loads = 0

    async def _fake_load() -> None:
        nonlocal loads
        loads += 1

    guard._async_load = _fake_load
    guard._loaded_at = time.monotonic()

    for attempt in range(20):
        assert await guard.async_slug_for(f"invented-{attempt}") is None
    assert loads == 0, "a burst of forged tokens must not become a burst of calls"


async def test_the_first_lookup_always_builds_the_map(hass: HomeAssistant) -> None:
    """Nothing is known before the first load, so it cannot be rate limited."""
    guard = IngressGuard(hass)
    loads = 0

    async def _fake_load() -> None:
        nonlocal loads
        loads += 1

    guard._async_load = _fake_load
    assert await guard.async_slug_for("anything") is None
    assert loads == 1
