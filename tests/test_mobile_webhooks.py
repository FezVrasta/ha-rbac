"""A Companion app's webhook is held to its owner's role."""

import sys
import types
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.catalog import Catalog
from custom_components.ha_rbac.const import DATA_RBAC
from custom_components.ha_rbac.decide import Decider
from custom_components.ha_rbac.denylog import DenyLog
from custom_components.ha_rbac.filters import REGISTRY
from custom_components.ha_rbac.mobile_webhooks import _Guard
from custom_components.ha_rbac.policy import Evaluator
from custom_components.ha_rbac.store import RbacStore

CONF_USER_ID = "user_id"


@pytest.fixture(name="fake_mobile_app")
def fake_mobile_app_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for `mobile_app`, which needs more of Core than the suite has."""
    ran: list[str] = []
    commands: dict[str, Any] = {}

    def _handler(name: str):
        async def handler(hass, config_entry, data):
            ran.append(name)
            return "ran"

        return handler

    for name in ("call_service", "fire_event", "update_location", "mystery"):
        commands[name] = _handler(name)

    package = types.ModuleType("homeassistant.components.mobile_app")
    package.__path__ = []
    webhook = types.ModuleType("homeassistant.components.mobile_app.webhook")
    webhook.WEBHOOK_COMMANDS = commands
    const = types.ModuleType("homeassistant.components.mobile_app.const")
    const.CONF_USER_ID = CONF_USER_ID
    helpers = types.ModuleType("homeassistant.components.mobile_app.helpers")
    helpers.error_response = lambda code, message, status: (code, status)
    for name, module in {
        "homeassistant.components.mobile_app": package,
        "homeassistant.components.mobile_app.webhook": webhook,
        "homeassistant.components.mobile_app.const": const,
        "homeassistant.components.mobile_app.helpers": helpers,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return {"commands": commands, "ran": ran}


async def test_a_restricted_owner_cannot_act_outside_the_role_from_the_phone(
    hass: HomeAssistant, fake_mobile_app: dict[str, Any]
) -> None:
    """GHSA-r5vh-5j49-6vhw: the webhook ran as the owner's native permissions."""
    await async_setup_component(hass, "websocket_api", {})
    store = RbacStore(hass)
    await store.async_load()
    catalog = Catalog(hass)
    catalog.rebuild()
    denylog = DenyLog(hass)
    hass.data[DATA_RBAC] = SimpleNamespace(
        evaluator=Evaluator(hass, store),
        decider=Decider(hass, catalog, REGISTRY),
        denylog=denylog,
    )
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("lock.front", "locked")

    # The first account Home Assistant creates is its owner, who passes through.
    await hass.auth.async_create_user("House owner", group_ids=["system-admin"])
    restricted = await hass.auth.async_create_user("Kid", group_ids=[GROUP_ID_USER])
    role = await store.async_create_role(
        {
            "name": "Lights only",
            "allow": {
                "entities": {"domains": {"light": {"read": True, "control": True}}}
            },
        }
    )
    await store.async_set_binding(restricted.id, [role["id"]])
    owner = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_USER])
    await store.async_set_binding(owner.id, ["admin"])

    _Guard(hass).install()
    commands = fake_mobile_app["commands"]
    ran = fake_mobile_app["ran"]
    entry = SimpleNamespace(data={CONF_USER_ID: restricted.id})

    allowed = {
        "domain": "light",
        "service": "turn_off",
        "service_data": {"entity_id": "light.kitchen"},
    }
    assert await commands["call_service"](hass, entry, allowed) == "ran"

    ran.clear()
    refused = {
        "domain": "lock",
        "service": "unlock",
        "service_data": {"entity_id": "lock.front"},
    }
    assert await commands["call_service"](hass, entry, refused) == (
        "forbidden",
        HTTPStatus.BAD_REQUEST,
    )
    assert await commands["fire_event"](hass, entry, {"event_type": "x"}) == (
        "forbidden",
        HTTPStatus.BAD_REQUEST,
    )
    assert await commands["mystery"](hass, entry, {}) == (
        "forbidden",
        HTTPStatus.BAD_REQUEST,
    ), "a command nobody taught the guard to judge is refused"
    assert ran == []
    assert {d["name"] for d in denylog.async_recent()} >= {
        "webhook call_service",
        "webhook fire_event",
    }

    # The registration's own business and an unrestricted owner are left to Core.
    assert await commands["update_location"](hass, entry, {}) == "ran"
    assert (
        await commands["call_service"](
            hass, SimpleNamespace(data={CONF_USER_ID: owner.id}), refused
        )
        == "ran"
    )
