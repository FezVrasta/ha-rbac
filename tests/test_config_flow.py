"""Tests for the setup wizard.

The handler is driven directly rather than through `hass.config_entries.flow`,
which would first set up the integration's dependencies: the frontend package
is not installed in a core checkout, so that fails for reasons that have
nothing to do with the wizard.
"""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.ha_rbac.config_flow import RbacConfigFlow
from custom_components.ha_rbac.const import (
    CONF_BIND_ADDRESS,
    CONF_MANAGE_HTTP,
    CONF_PROXY_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DOMAIN,
)

PORTS = {
    CONF_PROXY_PORT: 8123,
    CONF_BIND_ADDRESS: "0.0.0.0",
    CONF_UPSTREAM_HOST: "127.0.0.1",
    CONF_UPSTREAM_PORT: 8124,
}


@pytest.fixture(name="flow")
async def flow_fixture(hass: HomeAssistant) -> RbacConfigFlow:
    """Return a wizard bound to a Home Assistant with an HTTP server."""
    await async_setup_component(hass, "http", {"http": {}})
    handler = RbacConfigFlow()
    handler.hass = hass
    handler.handler = DOMAIN
    handler.flow_id = "test"
    handler.context = {}
    return handler


async def test_the_wizard_offers_to_move_home_assistant(
    flow: RbacConfigFlow,
) -> None:
    """The step that exists so nobody has to know the install order."""
    step = await flow.async_step_user(PORTS)
    assert step["type"] is FlowResultType.MENU
    assert step["step_id"] == "backup"

    step = await flow.async_step_backup_skip()
    assert step["type"] is FlowResultType.FORM
    assert step["step_id"] == "move"
    assert step["description_placeholders"] == {
        "proxy_port": "8123",
        "upstream_port": "8124",
    }

    step = await flow.async_step_move({CONF_MANAGE_HTTP: True})
    assert step["type"] is FlowResultType.CREATE_ENTRY
    assert step["data"][CONF_MANAGE_HTTP] is True
    assert step["data"][CONF_UPSTREAM_PORT] == 8124


async def test_declining_the_move_still_sets_up(flow: RbacConfigFlow) -> None:
    """Doing it by hand stays a supported route, not a dead end."""
    await flow.async_step_user(PORTS)
    await flow.async_step_backup_skip()
    step = await flow.async_step_move({CONF_MANAGE_HTTP: False})
    assert step["type"] is FlowResultType.CREATE_ENTRY
    assert step["data"][CONF_MANAGE_HTTP] is False


async def test_the_move_is_not_offered_where_it_cannot_be_done(
    flow: RbacConfigFlow,
) -> None:
    """Offering a button that cannot work is worse than not offering it.

    The HTTP config API is private, so a build that has moved it on leaves the
    manual route, and the wizard should not stop to ask about something it
    would then fail to do.
    """
    step = await flow.async_step_user(PORTS)
    assert step["type"] is FlowResultType.MENU
    assert step["step_id"] == "backup"

    with patch(
        "custom_components.ha_rbac.http_config.async_can_manage", return_value=False
    ):
        step = await flow.async_step_backup_skip()

    assert step["type"] is FlowResultType.CREATE_ENTRY
    assert step["data"][CONF_MANAGE_HTTP] is False


def _default(step: Any, field: str) -> Any:
    """Return the default a form offers for one field."""
    return next(
        key.default() for key in step["data_schema"].schema if key.schema == field
    )


@pytest.mark.parametrize(
    ("configured", "expected_proxy", "expected_upstream"),
    [(80, 80, 8124), (8123, 8123, 8124), (9000, 9000, 8124), (8124, 8124, 8125)],
    ids=["supervisor-default", "core-default", "chosen-by-hand", "already-on-8124"],
)
async def test_the_proxy_is_offered_the_port_home_assistant_answers_on(
    hass: HomeAssistant,
    configured: int,
    expected_proxy: int,
    expected_upstream: int,
) -> None:
    """Taking that port is what keeps every browser, phone and bookmark working.

    It is not always 8123. Under Supervisor Home Assistant defaults to 80, and
    plenty of people have picked their own, so the running server is asked
    rather than assumed. The last case is the awkward one: Home Assistant
    already sits on the port it would be moved to, and offering it twice would
    produce a form that refuses itself.
    """
    await async_setup_component(hass, "http", {"http": {"server_port": configured}})
    await hass.async_block_till_done()
    assert hass.http.server_port == configured, "precondition: it really is there"

    handler = RbacConfigFlow()
    handler.hass = hass
    handler.handler = DOMAIN
    handler.flow_id = "test"
    handler.context = {}
    step = await handler.async_step_user()

    assert _default(step, CONF_PROXY_PORT) == expected_proxy
    assert _default(step, CONF_UPSTREAM_PORT) == expected_upstream


async def test_a_proxy_sharing_home_assistants_port_is_refused(
    flow: RbacConfigFlow,
) -> None:
    """Two listeners cannot hold one port, and the error should say so early."""
    step = await flow.async_step_user({**PORTS, CONF_UPSTREAM_PORT: 8123})
    assert step["type"] is FlowResultType.FORM
    assert step["errors"] == {CONF_PROXY_PORT: "port_conflict"}


async def test_no_manual_instruction_when_the_move_is_on_offer(
    hass: HomeAssistant, flow: RbacConfigFlow
) -> None:
    """Telling someone to do a thing you are about to offer to do reads badly.

    On a fresh install Home Assistant is of course still on the network -- that
    is the whole point of the next step -- so saying so here, with directions,
    only makes the offer look like it does not work.
    """
    with patch.object(hass.http, "server_host", ["0.0.0.0"], create=True):
        step = await flow.async_step_user()
    assert step["description_placeholders"] == {"warning": ""}


async def test_the_backup_step_fires_backup_create(
    hass: HomeAssistant, flow: RbacConfigFlow
) -> None:
    """Choosing "create a backup" calls the service, then moves on."""
    await flow.async_step_user(PORTS)

    with patch.object(hass.services, "async_call") as mock_call:
        step = await flow.async_step_backup_create()

    mock_call.assert_called_once_with("backup", "create", blocking=False)
    # It should land on the move step.
    assert step["type"] is FlowResultType.FORM
    assert step["step_id"] == "move"


async def test_the_manual_instruction_survives_where_it_is_the_only_route(
    hass: HomeAssistant, flow: RbacConfigFlow
) -> None:
    """A build this cannot drive leaves the manual route, so it has to say so."""
    with (
        patch.object(hass.http, "server_host", ["0.0.0.0"], create=True),
        patch(
            "custom_components.ha_rbac.http_config.async_can_manage",
            return_value=False,
        ),
    ):
        step = await flow.async_step_user()

    warning = step["description_placeholders"]["warning"]
    assert "127.0.0.1" in warning
    assert "Settings > System > Network" in warning
