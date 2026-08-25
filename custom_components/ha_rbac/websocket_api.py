"""Admin websocket API for managing roles and bindings."""

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import const as ws_const
from homeassistant.core import HomeAssistant, callback

from .const import DATA_RBAC, DOMAIN
from .extract import entity_ids_in

COMMANDS = (
    "roles/list",
    "roles/create",
    "roles/update",
    "roles/delete",
    "bindings/list",
    "bindings/set",
    "catalog",
    "dashboard_entities",
    "denials/recent",
    "simulate",
)


@callback
def async_unregister(hass: HomeAssistant) -> None:
    """Remove the rbac/* commands.

    They close over the integration's runtime state, so leaving them registered
    after unload turns every call into a KeyError.
    """
    handlers = hass.data.get(ws_const.DOMAIN)
    if not handlers:
        return
    for command in COMMANDS:
        handlers.pop(f"{DOMAIN}/{command}", None)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the rbac/* commands."""
    for handler in (
        handle_roles_list,
        handle_roles_create,
        handle_roles_update,
        handle_roles_delete,
        handle_bindings_list,
        handle_bindings_set,
        handle_catalog,
        handle_dashboard_entities,
        handle_denials_recent,
        handle_simulate,
    ):
        websocket_api.async_register_command(hass, handler)


@callback
def _data(hass: HomeAssistant) -> Any:
    """Return the integration's runtime state."""
    return hass.data[DATA_RBAC]


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/roles/list"})
@callback
def handle_roles_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return every role."""
    connection.send_result(msg["id"], list(_data(hass).store.roles.values()))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/roles/create",
        vol.Required("role"): dict,
    }
)
@websocket_api.async_response
async def handle_roles_create(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create a custom role."""
    try:
        role = await _data(hass).store.async_create_role(msg["role"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(err))
        return
    connection.send_result(msg["id"], role)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/roles/update",
        vol.Required("role_id"): str,
        vol.Required("changes"): dict,
    }
)
@websocket_api.async_response
async def handle_roles_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Update a custom role."""
    try:
        role = await _data(hass).store.async_update_role(msg["role_id"], msg["changes"])
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown role")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(err))
        return
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], role)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/roles/delete",
        vol.Required("role_id"): str,
    }
)
@websocket_api.async_response
async def handle_roles_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Delete a custom role."""
    try:
        await _data(hass).store.async_delete_role(msg["role_id"])
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown role")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/bindings/list"})
@websocket_api.async_response
async def handle_bindings_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return every user alongside the roles bound to them."""
    store = _data(hass).store
    users = await hass.auth.async_get_users()
    connection.send_result(
        msg["id"],
        [
            {
                "user_id": user.id,
                "name": user.name,
                "is_owner": user.is_owner,
                "is_admin": user.is_admin,
                "system_generated": user.system_generated,
                "role_ids": store.bindings.get(user.id, []),
            }
            for user in users
            if not user.system_generated
        ],
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/bindings/set",
        vol.Required("user_id"): str,
        vol.Required("role_ids"): [str],
    }
)
@websocket_api.async_response
async def handle_bindings_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Bind a user to a set of roles."""
    try:
        await _data(hass).store.async_set_binding(msg["user_id"], msg["role_ids"])
    except KeyError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/dashboard_entities",
        vol.Required("url_paths"): [vol.Any(str, None)],
    }
)
@websocket_api.async_response
async def handle_dashboard_entities(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return every entity the named dashboards mention.

    Answers the question a role author actually has: "let them see what is on
    these screens". Working that out by hand means opening each dashboard and
    copying entity ids out of it, which is where people give up.
    """
    try:
        from homeassistant.components.lovelace.const import (  # noqa: PLC0415
            LOVELACE_DATA,
        )
    except ImportError:
        connection.send_result(msg["id"], {"entity_ids": [], "unreadable": []})
        return

    data = hass.data.get(LOVELACE_DATA)
    dashboards = getattr(data, "dashboards", {}) if data else {}

    known = set(hass.states.async_entity_ids())
    found: set[str] = set()
    unreadable: list[str] = []
    for url_path in msg["url_paths"]:
        # The default dashboard is stored under None rather than its url path.
        key = None if url_path in (None, "lovelace") else url_path
        config_holder = dashboards.get(key)
        if config_holder is None:
            unreadable.append(url_path or "lovelace")
            continue
        try:
            config = await config_holder.async_load(False)
        except Exception:  # noqa: BLE001
            # A dashboard with no stored config yet, or one in a mode this
            # cannot read. Reported rather than silently counted as empty.
            unreadable.append(url_path or "lovelace")
            continue
        found |= entity_ids_in(config, known.__contains__)

    connection.send_result(
        msg["id"], {"entity_ids": sorted(found), "unreadable": unreadable}
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/catalog"})
@callback
def handle_catalog(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the derived permission surface.

    This is what lets the role editor offer real options without the
    integration shipping a list of command names.
    """
    data = _data(hass)
    connection.send_result(
        msg["id"],
        {
            "commands": data.catalog.as_dict(),
            "apps": data.catalog.apps(),
            "degraded": data.catalog.degraded,
            "domains": sorted(
                {
                    entity_id.partition(".")[0]
                    for entity_id in hass.states.async_entity_ids()
                }
            ),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/denials/recent",
        vol.Optional("limit", default=100): int,
    }
)
@callback
def handle_denials_recent(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return recent denials, newest first."""
    connection.send_result(msg["id"], _data(hass).denylog.async_recent(msg["limit"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/simulate",
        vol.Required("user_id"): str,
        vol.Required("command"): str,
        vol.Optional("kind", default="ws"): vol.In(("ws", "http")),
        vol.Optional("payload", default=dict): dict,
    }
)
@websocket_api.async_response
async def handle_simulate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Explain what a given user would be allowed to do, and why.

    A denied request reaches the user as a broken UI with no explanation, so
    without this the whole layer is guesswork to administer.
    """
    data = _data(hass)
    user = await hass.auth.async_get_user(msg["user_id"])
    if user is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown user")
        return

    permissions = data.evaluator.async_permissions(user)
    decision = data.decider.decide(
        permissions, msg["kind"], msg["command"], msg["payload"]
    )
    connection.send_result(
        msg["id"],
        {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "detail": decision.detail,
            "resources": decision.resources,
            "filter_response": decision.filter_response,
            "tier": data.catalog.tier_for(msg["command"]),
            "role_ids": [role.role_id for role in permissions.roles],
            "pass_through": permissions.pass_through,
        },
    )
