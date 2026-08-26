"""Tests for recording what a role needs.

Writing a role blind means restricting something and finding out days later
that a dashboard is empty. Recording turns that round: the role is unrestricted
for a while, everything its holders touch is noted, and the notes are written
into the role afterwards.

The dangerous half is the "unrestricted for a while" part, so most of these are
about it ending when it should.
"""

from homeassistant.auth.permissions.const import (
    CAT_ENTITIES,
    POLICY_CONTROL,
    POLICY_READ,
)
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac import record
from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.const import TIER_OPEN
from custom_components.ha_rbac.decide import KIND_WS, REASON_RESOURCE, Decider
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.policy import Permissions, compile_role
from custom_components.ha_rbac.record import Recorder


def _role(hass: HomeAssistant, role_id: str = "guests") -> dict:
    """Return a role that can see nothing at all."""
    return {
        "id": role_id,
        "name": "Guests",
        "allow": {},
        "deny": {},
        "tiers": {"max": TIER_OPEN, "allow": [], "deny": []},
    }


def _permissions(hass: HomeAssistant, role: dict) -> Permissions:
    """Return the permissions of someone holding one role."""
    return Permissions(
        roles=[
            compile_role(
                hass,
                role,
                PermissionLookup(er.async_get(hass), dr.async_get(hass)),
            )
        ]
    )


async def _decider(hass: HomeAssistant, recorder: Recorder) -> Decider:
    """Return a decider backed by a real command catalogue."""
    for domain in ("websocket_api", "config", "api"):
        await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
    catalog = Catalog(hass)
    catalog.rebuild()
    return Decider(hass, catalog, REGISTRY, recorder)


async def test_a_recorded_role_is_allowed_what_it_would_be_refused(
    hass: HomeAssistant,
) -> None:
    """Otherwise the recording only ever sees what the role already permits.

    Which would make it useless: the point is to find out what is missing, and
    a request that was refused never says what it wanted.
    """
    hass.states.async_set("lock.front", "locked")
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    role = _role(hass)
    permissions = _permissions(hass, role)

    name = "config/entity_registry/get"
    payload = {"type": name, "entity_id": "lock.front"}
    assert decider.decide(permissions, KIND_WS, name, payload).allowed is False

    recorder.start("guests")
    assert decider.decide(permissions, KIND_WS, name, payload).allowed is True

    # `get_states` is allowed either way -- it names nothing, so it is the
    # response that is emptied. While recording it must come back whole, or the
    # person is using a Home Assistant that is still missing things and the
    # recording never learns what they were.
    reads = decider.decide(permissions, KIND_WS, "get_states", {"type": "get_states"})
    assert reads.filter_response is False, "a recording must not trim the response"


async def test_what_was_touched_is_written_down(hass: HomeAssistant) -> None:
    """Reads and controls are told apart, because the role has to tell them apart."""
    hass.states.async_set("light.kitchen", "off")
    hass.states.async_set("lock.front", "locked")
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    permissions = _permissions(hass, _role(hass))
    recorder.start("guests")

    decider.decide(
        permissions,
        KIND_WS,
        "config/entity_registry/get",
        {"type": "config/entity_registry/get", "entity_id": "lock.front"},
    )
    decider.decide(
        permissions,
        KIND_WS,
        "call_service",
        {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
        },
    )

    seen = recorder.stop("guests")
    assert seen is not None
    assert seen.entities == {
        "lock.front": POLICY_READ,
        "light.kitchen": POLICY_CONTROL,
    }


async def test_control_outranks_a_read_of_the_same_entity(
    hass: HomeAssistant,
) -> None:
    """Someone reads a light before they turn it on; the role needs both."""
    recorder = Recorder()
    recording = recorder.start("guests")
    recording.note_entity("light.kitchen", POLICY_CONTROL)
    recording.note_entity("light.kitchen", POLICY_READ)
    assert recording.entities["light.kitchen"] == POLICY_CONTROL


async def test_the_recording_is_written_into_the_role(hass: HomeAssistant) -> None:
    """And only ever adds. A recording says what was needed, never what wasn't.

    An entity nobody happened to open while it ran has not been shown to be
    unnecessary, so taking anything away on the strength of that would be
    guessing with someone else's access.
    """
    role = {
        "id": "guests",
        "name": "Guests",
        "allow": {CAT_ENTITIES: {"entity_ids": {"light.hall": {POLICY_READ: True}}}},
        "apps": {"deny": ["energy", "history"], "allow": [], "dashboards": {}},
        "capabilities": ["scripts"],
    }
    recording = record.Recording(role_id="guests", started=None)
    recording.note_entity("light.kitchen", POLICY_CONTROL)
    recording.note_entity("lock.front", POLICY_READ)
    recording.apps.add("energy")
    recording.capabilities.add("automations")

    changes = record.apply(role, recording)

    granted = changes["allow"][CAT_ENTITIES]["entity_ids"]
    assert granted["light.hall"] == {POLICY_READ: True}, "what was there survives"
    assert granted["light.kitchen"] == {POLICY_READ: True, POLICY_CONTROL: True}
    assert granted["lock.front"] == {POLICY_READ: True}
    assert changes["apps"]["deny"] == ["history"], (
        "the app they opened is no longer denied"
    )
    assert changes["capabilities"] == ["automations", "scripts"]


async def test_a_role_that_already_allows_everything_gains_nothing(
    hass: HomeAssistant,
) -> None:
    """`entities: True` is not a mapping, and must not be turned into one."""
    role = {"id": "r", "name": "R", "allow": {CAT_ENTITIES: True}}
    recording = record.Recording(role_id="r", started=None)
    recording.note_entity("light.kitchen", POLICY_CONTROL)
    assert record.apply(role, recording)["allow"][CAT_ENTITIES] is True


async def test_recording_ends_when_the_integration_unloads(
    hass: HomeAssistant,
) -> None:
    """A recording is a grant of full access, so it cannot outlive its keeper.

    It is held in memory for the same reason: a restart ends every recording
    and the role goes back to its own rules. Persisting the flag would mean a
    crash mid-recording left the door open with nobody watching.
    """
    recorder = Recorder()
    recorder.start("guests")
    recorder.start("kids")
    assert recorder.active == ["guests", "kids"]

    recorder.stop_all()
    assert recorder.active == []
    assert recorder.for_permissions(Permissions(roles=[])) is None


async def test_only_the_recorded_role_is_unrestricted(hass: HomeAssistant) -> None:
    """Recording one role must not quietly free everyone else."""
    hass.states.async_set("lock.front", "locked")
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    recorder.start("guests")

    others = _permissions(hass, _role(hass, "kids"))
    decision = decider.decide(
        others,
        KIND_WS,
        "config/entity_registry/get",
        {"type": "config/entity_registry/get", "entity_id": "lock.front"},
    )
    assert decision.allowed is False
    assert decision.reason == REASON_RESOURCE


async def test_a_second_role_does_not_hide_a_recording(hass: HomeAssistant) -> None:
    """Holding a recorded role alongside an ordinary one still records."""
    recorder = Recorder()
    recorder.start("guests")
    lookup = PermissionLookup(er.async_get(hass), dr.async_get(hass))
    permissions = Permissions(
        roles=[
            compile_role(hass, _role(hass, "kids"), lookup),
            compile_role(hass, _role(hass, "guests"), lookup),
        ]
    )
    recording = recorder.for_permissions(permissions)
    assert recording is not None
    assert recording.role_id == "guests"


async def test_an_unrestricted_user_is_never_recorded(hass: HomeAssistant) -> None:
    """The owner skips every gate, so there is nothing of theirs to record.

    Recording their traffic would fill the role with everything an
    administrator does, which is the opposite of what it is for.
    """
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    recorder.start("guests")

    full = Permissions(
        roles=[
            compile_role(
                hass,
                {
                    "id": "guests",
                    "name": "Guests",
                    "allow": {CAT_ENTITIES: True},
                    "tiers": {"max": "admin", "allow": ["*"], "deny": []},
                },
                PermissionLookup(er.async_get(hass), dr.async_get(hass)),
            )
        ]
    )
    assert full.full_access is True

    decider.decide(full, KIND_WS, "get_states", {"type": "get_states"})
    seen = recorder.stop("guests")
    assert seen is not None
    assert seen.entities == {}


async def test_an_admin_command_records_the_capability_it_belongs_to(
    hass: HomeAssistant,
) -> None:
    """So the role gains "Automations" rather than one glob nobody can read."""
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    recorder.start("guests")

    decider.decide(
        _permissions(hass, _role(hass)),
        KIND_WS,
        "automation/config",
        {"type": "automation/config", "entity_id": "automation.morning"},
    )

    seen = recorder.stop("guests")
    assert seen is not None
    assert seen.capabilities == {"automations"}


async def test_unrestricted_entity_access_is_not_a_grant_of_everything(
    hass: HomeAssistant,
) -> None:
    """`get_states` while recording names no entity, so it adds none.

    A recording that turned every unbounded read into "allow all entities"
    would produce a role indistinguishable from Administrator.
    """
    hass.states.async_set("lock.front", "locked")
    recorder = Recorder()
    decider = await _decider(hass, recorder)
    recorder.start("guests")

    decider.decide(_permissions(hass, _role(hass)), KIND_WS, "get_states", {})

    seen = recorder.stop("guests")
    assert seen is not None
    assert seen.entities == {}


async def test_stopping_something_that_never_started_says_so(
    hass: HomeAssistant,
) -> None:
    """Rather than inventing an empty recording and writing it into a role."""
    assert Recorder().stop("guests") is None


async def test_a_recording_that_saw_nothing_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """Not even the shape of the role.

    Writing an empty `entity_ids` where there was none is behaviourally inert,
    but it turns "we watched and learned nothing" into an edit, and an edited
    role invites the reading that something was granted.
    """
    role = {"id": "r", "name": "R", "allow": {}, "apps": {"deny": ["energy"]}}
    empty = record.Recording(role_id="r", started=None)
    assert record.apply(role, empty) == {}
