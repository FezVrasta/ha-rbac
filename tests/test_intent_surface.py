"""The conversation agent must not be a way round the entity rules.

An intent takes free text. "Turn off the bed light" names no entity anything
here can see, so nothing downstream can judge what it will touch -- which is why
the intent surface is refused outright rather than filtered.

It is reachable two ways, and they are spelled differently. As a websocket
command it is `conversation/process`; as a service call it is
`conversation.process`. A glob written for one cannot match the other, and
denying only the command left the service open: `_decide_service` saw a call
naming no entity, asked whether the role could control the `conversation`
domain, and let it through. A role forbidden from controlling a light could
turn it off by asking for it in words.
"""

import pytest
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

from custom_components.ha_rbac.const import TIER_ADMIN, TIER_OPEN, TIER_USER
from custom_components.ha_rbac.policy import Permissions, compile_role


def _role(hass: HomeAssistant):
    """Build a role that may control everything, which is the dangerous case.

    A role that can control nothing is refused by the resource gate anyway; the
    escalation matters for the ordinary household role that may use its own
    lights but not the locks.
    """
    return compile_role(
        hass,
        {
            "id": "kids",
            "name": "Kids",
            "allow": {
                CAT_ENTITIES: {SUBCAT_ALL: {POLICY_READ: True, POLICY_CONTROL: True}}
            },
            "deny": {},
        },
        PermissionLookup(er.async_get(hass), dr.async_get(hass)),
    )


@pytest.mark.parametrize(
    "command",
    [
        # As websocket commands.
        "conversation/process",
        "assist_pipeline/run",
        "assist_satellite/intercept_wake_word",
        # As service calls, which is how `_decide_service` sees them.
        "conversation.process",
        "assist_pipeline.reload",
        "assist_satellite.announce",
    ],
)
@pytest.mark.parametrize("tier", [TIER_OPEN, TIER_USER, TIER_ADMIN])
async def test_the_intent_surface_is_refused_however_it_is_spelled(
    hass: HomeAssistant, command: str, tier: str
) -> None:
    """At every tier, because the deny is checked before the ranking."""
    permissions = Permissions(roles=[_role(hass)])
    assert permissions.tier_allowed(command, tier) is False, command


async def test_ordinary_service_calls_are_unaffected(hass: HomeAssistant) -> None:
    """The deny has to be narrow: this is a baseline applied to every role."""
    permissions = Permissions(roles=[_role(hass)])
    for command in ("light.turn_on", "lock.unlock", "persistent_notification.create"):
        assert permissions.tier_allowed(command, TIER_USER) is True, command
