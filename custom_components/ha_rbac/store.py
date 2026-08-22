"""Persistence for roles and user bindings.

Home Assistant has no group CRUD API, so `.storage/auth` is left alone entirely
and this integration keeps its own store.
"""

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION
from .policy import ROLE_SCHEMA, default_roles

_LOGGER = logging.getLogger(__name__)


class RbacStore:
    """Roles, user bindings and per-user denials."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self.roles: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, list[str]] = {}
        self.global_deny: dict[str, dict[str, Any]] = {}
        self._listeners: list[Any] = []

    async def async_load(self) -> None:
        """Load from disk, seeding the predefined roles."""
        data = await self._store.async_load() or {}

        self.roles = dict(default_roles())
        for role_id, role in (data.get("roles") or {}).items():
            # System roles are defined in code; a stored copy never overrides
            # them, mirroring how HA treats its own system groups.
            if role_id in self.roles and self.roles[role_id].get("system_generated"):
                continue
            try:
                self.roles[role_id] = ROLE_SCHEMA(role)
            except vol.Invalid:
                # Skipping one unreadable role is far better than failing setup:
                # that would leave the proxy unbound and, on a loopback-only
                # deployment, cut off every route into Home Assistant.
                _LOGGER.exception(
                    "Ignoring stored role %s because it is not valid. Any user "
                    "bound only to this role now has no access at all -- "
                    "rebind them, or delete the role from .storage/%s",
                    role_id,
                    STORAGE_KEY,
                )

        self.bindings = {
            user_id: list(role_ids)
            for user_id, role_ids in (data.get("bindings") or {}).items()
        }
        self.global_deny = dict(data.get("global_deny") or {})

    @callback
    def async_add_listener(self, listener: Any) -> None:
        """Register a callback fired whenever stored data changes."""
        self._listeners.append(listener)

    @callback
    def _notify(self) -> None:
        """Tell listeners to drop anything compiled from stored data."""
        for listener in self._listeners:
            listener()

    async def _async_save(self) -> None:
        """Persist and notify."""
        await self._store.async_save(
            {
                # Predefined roles live in code, so only custom ones are written.
                "roles": {
                    role_id: role
                    for role_id, role in self.roles.items()
                    if not role.get("system_generated")
                },
                "bindings": self.bindings,
                "global_deny": self.global_deny,
            }
        )
        self._notify()

    async def async_create_role(self, role: dict[str, Any]) -> dict[str, Any]:
        """Create a custom role."""
        role = dict(role)
        role.setdefault("id", uuid.uuid4().hex)
        role["system_generated"] = False
        validated = ROLE_SCHEMA(role)
        self.roles[validated["id"]] = validated
        await self._async_save()
        return validated

    async def async_update_role(
        self, role_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a custom role."""
        existing = self.roles.get(role_id)
        if existing is None:
            raise KeyError(role_id)
        if existing.get("system_generated"):
            raise ValueError(f"{role_id} is a predefined role and cannot be edited")

        merged = {**existing, **changes, "id": role_id, "system_generated": False}
        validated = ROLE_SCHEMA(merged)
        self.roles[role_id] = validated
        await self._async_save()
        return validated

    async def async_delete_role(self, role_id: str) -> None:
        """Delete a custom role and unbind every user from it."""
        existing = self.roles.get(role_id)
        if existing is None:
            raise KeyError(role_id)
        if existing.get("system_generated"):
            raise ValueError(f"{role_id} is a predefined role and cannot be deleted")

        del self.roles[role_id]
        for user_id, role_ids in list(self.bindings.items()):
            if role_id in role_ids:
                remaining = [r for r in role_ids if r != role_id]
                # An empty binding would silently fall back to the derived
                # default, so drop the entry and make that explicit.
                if remaining:
                    self.bindings[user_id] = remaining
                else:
                    del self.bindings[user_id]
        await self._async_save()

    async def async_set_binding(self, user_id: str, role_ids: list[str]) -> None:
        """Bind a user to a set of roles."""
        unknown = [role_id for role_id in role_ids if role_id not in self.roles]
        if unknown:
            raise KeyError(f"unknown roles: {', '.join(unknown)}")

        if role_ids:
            self.bindings[user_id] = list(role_ids)
        else:
            self.bindings.pop(user_id, None)
        await self._async_save()

    async def async_set_global_deny(
        self, user_id: str, policy: dict[str, Any] | None
    ) -> None:
        """Set or clear a per-user denial that outranks every role."""
        if policy:
            self.global_deny[user_id] = policy
        else:
            self.global_deny.pop(user_id, None)
        await self._async_save()
