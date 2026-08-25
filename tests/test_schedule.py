"""Tests for time-limited roles.

A role can carry a schedule, and outside it the role is simply not held. The
hard part is a window that runs through midnight: it belongs to the day it
opened, so "Friday 22:00 to 02:00" is in force at one on Saturday morning and
is not in force at one on Friday morning.
"""

from datetime import datetime

import pytest
import voluptuous as vol
from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_READ, SUBCAT_ALL
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_rbac.policy import (
    ROLE_SCHEMA,
    Evaluator,
    Permissions,
    compile_role,
    schedule_active,
    schedule_windows,
)

# A Wednesday, so the weekday cases are not accidentally symmetric.
WED = datetime(2026, 8, 26, 12, 0)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """Return a time in the week of 24 August 2026, a Monday."""
    return datetime(2026, 8, 24 + day, hour, minute)


MON, TUE, WED_D, THU, FRI, SAT, SUN = range(7)


def test_no_schedule_is_always_in_force() -> None:
    """A role without one must behave exactly as it did before schedules."""
    assert schedule_active(None, WED) is True
    assert schedule_active({}, WED) is True
    assert schedule_active({"days": [], "start": "", "end": ""}, WED) is True


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(7, False), (9, True), (17, True), (18, False), (23, False)],
    ids=["before", "at-start", "inside", "at-end", "after"],
)
def test_a_daytime_window(hour: int, expected: bool) -> None:
    """The start is inclusive and the end is not, so 09:00-18:00 is nine hours."""
    schedule = {"days": [], "start": "09:00", "end": "18:00"}
    assert schedule_active(schedule, _at(WED_D, hour)) is expected


@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [
        (FRI, 21, False),
        (FRI, 22, True),
        (FRI, 23, True),
        (SAT, 1, True),
        (SAT, 2, False),
        (SAT, 22, False),
        (FRI, 1, False),
    ],
    ids=[
        "friday-before-it-opens",
        "friday-as-it-opens",
        "friday-night",
        "saturday-small-hours",
        "saturday-as-it-closes",
        "saturday-night-is-not-fridays-window",
        "friday-small-hours-belong-to-thursday",
    ],
)
def test_a_window_through_midnight_belongs_to_the_day_it_opened(
    day: int, hour: int, expected: bool
) -> None:
    """The case worth writing down: Friday 22:00 to 02:00.

    One on Saturday morning is still Friday's window. One on Friday morning is
    Thursday's, and Thursday is not in the list.
    """
    schedule = {"days": ["fri"], "start": "22:00", "end": "02:00"}
    assert schedule_active(schedule, _at(day, hour)) is expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [(MON, True), (FRI, True), (SAT, False), (SUN, False)],
    ids=["monday", "friday", "saturday", "sunday"],
)
def test_days_without_times_cover_the_whole_day(day: int, expected: bool) -> None:
    """Weekdays only, all day."""
    schedule = {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "", "end": ""}
    assert schedule_active(schedule, _at(day, 3)) is expected


def test_a_window_that_starts_and_ends_together_is_all_day() -> None:
    """Rather than a zero-length window nobody could ever be inside."""
    assert schedule_active({"start": "09:00", "end": "09:00"}, _at(WED_D, 3)) is True


@pytest.mark.parametrize(
    "value", ["", "nonsense", "25:00", "12:61", "::"], ids=lambda v: v or "empty"
)
def test_an_unreadable_time_is_ignored_rather_than_obeyed(value: str) -> None:
    """A schedule nobody can parse must not silently become a lockout."""
    assert schedule_active({"start": value, "end": value}, WED) is True


def test_the_schema_accepts_a_schedule_and_defaults_it_away() -> None:
    """Roles written before schedules existed still validate."""
    role = ROLE_SCHEMA({"id": "r", "name": "R"})
    assert schedule_windows(role["schedule"]) == []

    scheduled = ROLE_SCHEMA(
        {
            "id": "r",
            "name": "R",
            "schedule": {
                "rules": [{"days": ["sat", "sun"], "start": "08:00", "end": "20:00"}]
            },
        }
    )
    assert scheduled["schedule"]["rules"][0]["days"] == ["sat", "sun"]


def test_the_schema_refuses_a_day_that_is_not_one() -> None:
    """Otherwise a typo reads as "never", which is a lockout nobody asked for."""
    with pytest.raises(vol.Invalid):
        ROLE_SCHEMA({"id": "r", "name": "R", "schedule": {"days": ["funday"]}})


async def test_a_compiled_role_carries_its_schedule(hass: HomeAssistant) -> None:
    """And answers whether it is in force, which is what the evaluator asks."""
    role = compile_role(
        hass,
        {
            "id": "guests",
            "name": "Guests",
            "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
            "schedule": {"days": ["sat"], "start": "10:00", "end": "18:00"},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )
    assert role.active_at(_at(SAT, 12)) is True
    assert role.active_at(_at(SAT, 19)) is False
    assert role.active_at(_at(SUN, 12)) is False


async def test_holding_no_role_in_force_permits_nothing(hass: HomeAssistant) -> None:
    """Out of hours a role has to grant less, never more.

    Dropping an out-of-hours role could have fallen back to the Home Assistant
    group an unbound user gets, which for an administrator would have *raised*
    their access the moment their restricted role expired.
    """
    permissions = Permissions(roles=[])
    assert permissions.check_entity("light.kitchen", POLICY_READ) is False
    assert permissions.full_access is False


class _Store:
    """The pieces of the store the evaluator reads."""

    def __init__(self, roles, bindings):
        self.roles = roles
        self.bindings = bindings
        self.global_deny = {}


class _User:
    id = "u1"
    is_owner = False
    system_generated = False
    is_admin = True


async def test_the_evaluator_drops_a_role_outside_its_hours(
    hass: HomeAssistant, freezer
) -> None:
    """And picks it up again when the window opens, on the same evaluator.

    Permissions used to be cached on the user alone, so the first answer of the
    day would have been served for the rest of it and the schedule would never
    have taken effect.
    """
    store = _Store(
        roles={
            "guests": {
                "id": "guests",
                "name": "Guests",
                "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                "schedule": {"days": [], "start": "09:00", "end": "18:00"},
            }
        },
        bindings={"u1": ["guests"]},
    )
    # The schedule is read in Home Assistant's own timezone, which the test
    # fixture sets to US/Pacific; pin it so the clock times mean what they say.
    await hass.config.async_set_time_zone("UTC")
    evaluator = Evaluator(hass, store)
    user = _User()

    freezer.move_to("2026-08-26 12:00:00")
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is True
    )

    freezer.move_to("2026-08-26 20:00:00")
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is False
    ), "the cache must not have frozen the answer from earlier in the day"

    freezer.move_to("2026-08-27 09:30:00")
    assert (
        evaluator.async_permissions(user).check_entity("light.a", POLICY_READ) is True
    )


async def test_an_expired_role_does_not_fall_back_to_the_home_assistant_group(
    hass: HomeAssistant, freezer
) -> None:
    """An administrator on a restricted role must not be freed by its expiry."""
    store = _Store(
        roles={
            "night": {
                "id": "night",
                "name": "Night",
                "allow": {CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True}}},
                "schedule": {"days": [], "start": "09:00", "end": "18:00"},
            }
        },
        bindings={"u1": ["night"]},
    )
    await hass.config.async_set_time_zone("UTC")
    evaluator = Evaluator(hass, store)
    user = _User()
    assert user.is_admin is True, "precondition: unbound, they would be Administrator"

    freezer.move_to("2026-08-26 23:00:00")
    permissions = evaluator.async_permissions(user)
    assert permissions.full_access is False
    assert permissions.check_entity("light.a", POLICY_READ) is False


def test_two_windows_on_the_same_days() -> None:
    """The shape a single window cannot describe: Mon and Tue, 10-12 and 15-19."""
    schedule = {
        "rules": [
            {"days": ["mon", "tue"], "start": "10:00", "end": "12:00"},
            {"days": ["mon", "tue"], "start": "15:00", "end": "19:00"},
        ]
    }
    assert schedule_active(schedule, _at(MON, 11)) is True
    assert schedule_active(schedule, _at(MON, 16)) is True
    assert schedule_active(schedule, _at(TUE, 11)) is True
    # The gap between them, and the hours either side.
    assert schedule_active(schedule, _at(MON, 13)) is False
    assert schedule_active(schedule, _at(MON, 9)) is False
    assert schedule_active(schedule, _at(MON, 20)) is False
    # A day that is in neither.
    assert schedule_active(schedule, _at(WED_D, 11)) is False


def test_windows_may_differ_by_day() -> None:
    """Which is why each window carries its own days rather than sharing one set."""
    schedule = {
        "rules": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "09:00",
                "end": "17:00",
            },
            {"days": ["sat", "sun"], "start": "10:00", "end": "22:00"},
        ]
    }
    assert schedule_active(schedule, _at(WED_D, 16)) is True
    assert schedule_active(schedule, _at(WED_D, 20)) is False
    assert schedule_active(schedule, _at(SAT, 20)) is True
    assert schedule_active(schedule, _at(SAT, 9)) is False


def test_one_window_running_past_midnight_among_several() -> None:
    """The wrap still belongs to the day it opened when it is not the only rule."""
    schedule = {
        "rules": [
            {"days": ["mon"], "start": "09:00", "end": "12:00"},
            {"days": ["fri"], "start": "22:00", "end": "02:00"},
        ]
    }
    assert schedule_active(schedule, _at(MON, 10)) is True
    assert schedule_active(schedule, _at(SAT, 1)) is True, "Friday night's tail"
    assert schedule_active(schedule, _at(MON, 1)) is False, "not Sunday night's"


def test_a_schedule_written_in_the_older_single_window_shape_still_works() -> None:
    """Roles saved before a schedule could hold more than one window."""
    legacy = {"days": ["sat"], "start": "10:00", "end": "18:00"}
    assert schedule_windows(legacy) == [
        {"days": ["sat"], "start": "10:00", "end": "18:00"}
    ]
    assert schedule_active(legacy, _at(SAT, 12)) is True
    assert schedule_active(legacy, _at(SAT, 19)) is False


def test_an_old_window_and_new_ones_are_both_honoured() -> None:
    """A role edited after an upgrade must not silently lose its original hours."""
    schedule = {
        "days": ["sat"],
        "start": "10:00",
        "end": "12:00",
        "rules": [{"days": ["sun"], "start": "14:00", "end": "16:00"}],
    }
    assert schedule_active(schedule, _at(SAT, 11)) is True
    assert schedule_active(schedule, _at(SUN, 15)) is True
    assert schedule_active(schedule, _at(SAT, 15)) is False
