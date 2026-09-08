"""Tests for role persistence and user bindings."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_rbac.const import (
    EVENT_RBAC_DENIED,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_READ_ONLY,
    ROLE_USER,
)
from custom_components.ha_rbac.denylog import Denial, DenyLog
from custom_components.ha_rbac.store import RbacStore


@pytest.fixture(name="store")
async def store_fixture(hass: HomeAssistant) -> RbacStore:
    """Return a loaded, empty store."""
    store = RbacStore(hass)
    await store.async_load()
    return store


async def test_predefined_roles_are_seeded(store: RbacStore) -> None:
    """A fresh install has the predefined roles without any configuration."""
    assert set(store.roles) == {ROLE_ADMIN, ROLE_EDITOR, ROLE_USER, ROLE_READ_ONLY}
    assert all(role["system_generated"] for role in store.roles.values())


async def test_predefined_roles_cannot_be_edited(store: RbacStore) -> None:
    """System roles are defined in code, mirroring HA's own system groups."""
    with pytest.raises(ValueError, match="predefined"):
        await store.async_update_role(ROLE_ADMIN, {"name": "Hijacked"})
    with pytest.raises(ValueError, match="predefined"):
        await store.async_delete_role(ROLE_READ_ONLY)


async def test_create_update_delete_custom_role(store: RbacStore) -> None:
    """Custom roles round-trip through the store."""
    role = await store.async_create_role({"name": "Guests"})
    assert role["system_generated"] is False

    updated = await store.async_update_role(role["id"], {"name": "Visitors"})
    assert updated["name"] == "Visitors"

    await store.async_delete_role(role["id"])
    assert role["id"] not in store.roles


async def test_deleting_a_role_unbinds_its_users(store: RbacStore) -> None:
    """A dangling binding would resolve to nothing and silently deny everything."""
    role = await store.async_create_role({"name": "Guests"})
    await store.async_set_binding("user-1", [role["id"], ROLE_USER])
    await store.async_set_binding("user-2", [role["id"]])

    await store.async_delete_role(role["id"])

    assert store.bindings["user-1"] == [ROLE_USER]
    # user-2 had no other role; the entry is dropped so the derived default applies
    assert "user-2" not in store.bindings


async def test_binding_to_an_unknown_role_is_rejected(store: RbacStore) -> None:
    """Typos must not produce a binding that silently denies everything."""
    with pytest.raises(KeyError, match="unknown roles"):
        await store.async_set_binding("user-1", ["does-not-exist"])


async def test_clearing_a_binding_removes_the_entry(store: RbacStore) -> None:
    """An empty list means 'use the derived default', not 'no roles'."""
    await store.async_set_binding("user-1", [ROLE_USER])
    await store.async_set_binding("user-1", [])
    assert "user-1" not in store.bindings


async def test_only_custom_roles_are_persisted(
    hass: HomeAssistant, store: RbacStore
) -> None:
    """Predefined roles come from code, so a stored copy can never shadow them."""
    await store.async_create_role({"id": "guests", "name": "Guests"})

    reloaded = RbacStore(hass)
    await reloaded.async_load()

    assert "guests" in reloaded.roles
    assert reloaded.roles[ROLE_ADMIN]["name"] == "Administrator"


async def test_creating_a_role_never_replaces_an_existing_one(
    store: RbacStore,
) -> None:
    """`roles/create` takes an unconstrained dict, so the id is the caller's.

    An id matching an existing custom role used to be accepted and overwrite
    that role outright -- allow, deny, tiers, apps -- with no error and nothing
    to say a replace had happened instead of a create. Everyone bound to it
    would get whatever the new definition said, wider or narrower. A retried
    request or two admin tabs are enough to do it by accident.

    Editing has its own deliberate path in `async_update_role`, so this one
    only ever creates. The existing role is left exactly as it was.
    """
    original = await store.async_create_role(
        {
            "id": "guests",
            "name": "Guests",
            "deny": {"entities": {"domains": {"lock": True}}},
        }
    )

    with pytest.raises(ValueError, match="already exists"):
        await store.async_create_role({"id": "guests", "name": "Impostor"})

    assert store.roles["guests"] == original


async def test_store_notifies_listeners_on_write(store: RbacStore) -> None:
    """Compiled policies must be dropped when stored data changes."""
    calls: list[int] = []
    store.async_add_listener(lambda: calls.append(1))

    await store.async_create_role({"name": "Guests"})
    assert calls


async def test_denylog_keeps_recent_and_fires_an_event(hass: HomeAssistant) -> None:
    """Operators need to see why a UI broke."""
    events: list[dict] = []
    hass.bus.async_listen(EVENT_RBAC_DENIED, lambda event: events.append(event.data))

    log = DenyLog(hass)
    log.async_record(
        Denial(
            user_id="u1",
            user_name="Guest",
            kind="ws",
            name="render_template",
            reason="unbounded",
            resources=[],
        )
    )
    await hass.async_block_till_done()

    recent = log.async_recent()
    assert len(recent) == 1
    assert recent[0]["name"] == "render_template"
    assert events and events[0]["reason"] == "unbounded"


async def test_denylog_returns_newest_first(hass: HomeAssistant) -> None:
    """The most recent denial is the one an operator is debugging."""
    log = DenyLog(hass)
    for index in range(3):
        log.async_record(Denial("u1", "Guest", "ws", f"cmd{index}", "tier", []))
    assert [entry["name"] for entry in log.async_recent()] == [
        "cmd2",
        "cmd1",
        "cmd0",
    ]
