"""Watch what a role actually needs, instead of guessing and then chasing it.

Writing a role blind is the hard way round: you restrict something, hand it
over, and find out days later that a dashboard is empty or a phone stopped
reporting. So a role can be put into *recording* for a while. While it is, its
holders are unrestricted and everything they touch is noted, and when it is
stopped the notes are written into the role.

Two consequences worth being clear about.

Recording is a **temporary grant of full access to everyone holding the role**,
so it is loud: the panel says so while it runs, and setup says so if one is
somehow still running.

It lives in memory only. A restart therefore ends every recording and the role
goes back to its own rules, which loses the notes but never leaves a role
quietly unrestricted -- persisting the flag would mean a crash mid-recording
left the door open with nobody watching.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .policy import Permissions, compile_role

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Recording:
    """What one role's holders have been seen to need."""

    role_id: str
    started: datetime
    # entity id -> the strongest access seen, read or control.
    entities: dict[str, str] = field(default_factory=dict)
    apps: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)

    @callback
    def note_entity(self, entity_id: str, key: str) -> None:
        """Record an entity, keeping the stronger of two access levels."""
        if key == POLICY_CONTROL or self.entities.get(entity_id) != POLICY_CONTROL:
            self.entities[entity_id] = key

    @callback
    def as_dict(self) -> dict[str, object]:
        """Return what has been seen, for the panel to show."""
        return {
            "role_id": self.role_id,
            "started": self.started.isoformat(),
            "entities": dict(sorted(self.entities.items())),
            "apps": sorted(self.apps),
            "capabilities": sorted(self.capabilities),
        }


class Recorder:
    """Holds the recordings that are currently running."""

    def __init__(self) -> None:
        """Start with nothing recording."""
        self._recordings: dict[str, Recording] = {}

    @callback
    def start(self, role_id: str) -> Recording:
        """Begin recording a role, discarding anything noted before."""
        _LOGGER.warning(
            "Recording role %s: everyone holding it is unrestricted until this "
            "is stopped",
            role_id,
        )
        recording = Recording(role_id=role_id, started=dt_util.utcnow())
        self._recordings[role_id] = recording
        return recording

    @callback
    def stop(self, role_id: str) -> Recording | None:
        """End a recording and return what it saw, if it was running."""
        recording = self._recordings.pop(role_id, None)
        if recording is not None:
            _LOGGER.info(
                "Stopped recording role %s: %s entities, %s apps",
                role_id,
                len(recording.entities),
                len(recording.apps),
            )
        return recording

    @callback
    def stop_all(self) -> None:
        """End every recording, so no role is left unrestricted."""
        for role_id in list(self._recordings):
            self.stop(role_id)

    @callback
    def get(self, role_id: str) -> Recording | None:
        """Return a running recording, if there is one."""
        return self._recordings.get(role_id)

    @property
    def active(self) -> list[str]:
        """Return the roles currently recording."""
        return sorted(self._recordings)

    @callback
    def for_permissions(self, permissions: Permissions) -> Recording | None:
        """Return the recording that covers a user, if any of their roles is.

        One recording role is enough. A user holding a recorded role alongside
        an ordinary one is unrestricted for the duration, which is the point:
        the recording has to see what they would have been refused.

        This is asked on every request, ahead of the full-access short-circuit
        that keeps the proxy cheap for administrators, so the answer when
        nothing is recording -- which is almost always -- costs one empty-dict
        check rather than a walk over the user's roles.
        """
        if not self._recordings:
            return None
        for role in permissions.roles:
            if (recording := self._recordings.get(role.role_id)) is not None:
                return recording
        return None


@callback
def apply(role: dict[str, object], recording: Recording) -> dict[str, object]:
    """Return the changes a recording makes to a role.

    Only ever additive. A recording says what was needed, never what was not,
    so nothing it produces can take access away -- an entity nobody touched
    while it ran might simply not have come up.
    """
    changes: dict[str, object] = {}
    if recording.entities:
        changes["allow"] = _merged_entities(role, recording)

    if recording.apps:
        apps = dict(role.get("apps") or {})
        # Apps are held as a denial list, so granting one is usually just
        # removing it. But once a role also holds an allow whitelist,
        # app_allowed() requires an explicit match against *that* list --
        # removing a denial does nothing for an app the whitelist never
        # named. An empty allow list means "everything not denied" and has
        # to stay empty rather than being narrowed to only what was seen.
        apps["deny"] = [
            url_path
            for url_path in (apps.get("deny") or [])
            if url_path not in recording.apps
        ]
        existing_allow = apps.get("allow") or []
        if existing_allow:
            apps["allow"] = sorted(set(existing_allow) | recording.apps)
        changes["apps"] = apps

    if recording.capabilities:
        changes["capabilities"] = sorted(
            set(role.get("capabilities") or []) | recording.capabilities
        )

    return changes


def _merged_entities(role: dict[str, object], recording: Recording) -> dict[str, dict]:
    """Fold the recorded entities into a role's allow policy."""
    allow = {
        category: dict(rules) if isinstance(rules, dict) else rules
        for category, rules in (role.get("allow") or {}).items()
    }
    entities = allow.get("entities")
    if entities is True:
        # The role already allows every entity; there is nothing to add.
        return allow
    entities = dict(entities) if isinstance(entities, dict) else {}
    by_id = dict(entities.get("entity_ids") or {})

    for entity_id, key in recording.entities.items():
        existing = by_id.get(entity_id)
        grant = dict(existing) if isinstance(existing, dict) else {}
        grant[POLICY_READ] = True
        if key == POLICY_CONTROL:
            grant[POLICY_CONTROL] = True
        by_id[entity_id] = grant

    entities["entity_ids"] = by_id
    allow["entities"] = entities
    return allow


def _permissions_for(hass: HomeAssistant, role: dict[str, object]) -> Permissions:
    """Compile a role the way still_blocked/blocked_apps need to check it."""
    compiled = compile_role(
        hass, role, PermissionLookup(er.async_get(hass), dr.async_get(hass))
    )
    return Permissions(roles=[compiled])


@callback
def still_blocked(
    hass: HomeAssistant, role: dict[str, object], recording: Recording
) -> list[str]:
    """Return the recorded entities the role still refuses, after applying.

    Granting on the allow side does not always win: within a role a denial
    vetoes, so an entity recorded under a `deny` rule is added and then
    immediately overruled. Removing the denial instead would be this feature
    quietly undoing a decision somebody made on purpose, which is worse -- so
    it is reported and left to them.
    """
    permissions = _permissions_for(hass, role)
    return sorted(
        entity_id
        for entity_id, key in recording.entities.items()
        if not permissions.check_entity(entity_id, key)
    )


@callback
def blocked_apps(
    hass: HomeAssistant, role: dict[str, object], recording: Recording
) -> list[str]:
    """Return the recorded apps the role still cannot open, after applying.

    Same reasoning as still_blocked, and for the same reason it cannot be
    folded into that check: apply() only ever grows a role's own apps.allow,
    so a deny elsewhere in the role, or an allow list left empty on purpose,
    can still refuse an app that was just recorded.
    """
    permissions = _permissions_for(hass, role)
    return sorted(
        url_path for url_path in recording.apps if not permissions.app_allowed(url_path)
    )
