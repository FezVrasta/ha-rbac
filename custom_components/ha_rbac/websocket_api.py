"""Admin websocket API for managing roles and bindings."""

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import const as ws_const
from homeassistant.core import HomeAssistant, callback

from . import record
from .const import (
    CONF_BIND_ADDRESS,
    CONF_MANAGE_HTTP,
    CONF_PROXY_PORT,
    CONF_RESTORE_ON_REMOVAL,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DATA_PREVIOUS_HTTP,
    DATA_RBAC,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PROXY_PORT,
    DEFAULT_RESTORE_ON_REMOVAL,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
    LOOPBACK_BIND_WARNING,
)
from .util import is_loopback_bind

COMMANDS = (
    "roles/list",
    "roles/create",
    "roles/update",
    "roles/delete",
    "bindings/list",
    "bindings/set",
    "catalog",
    "record/start",
    "record/stop",
    "record/status",
    "dashboards/refresh",
    "denials/recent",
    "simulate",
    "settings/get",
    "settings/set",
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
        handle_record_start,
        handle_record_stop,
        handle_record_status,
        handle_dashboards_refresh,
        handle_denials_recent,
        handle_simulate,
        handle_settings_get,
        handle_settings_set,
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
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/dashboards/refresh"})
@websocket_api.async_response
async def handle_dashboards_refresh(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Re-read every dashboard now.

    The list is kept current by Home Assistant's own "a dashboard changed"
    event, so this should never be necessary. It is here for when it is: a
    dashboard edited outside the usual path, or a suspicion that the two have
    drifted, should not need a restart to settle.
    """
    lookup = _data(hass).dashboard_entities
    if lookup is None:
        connection.send_result(msg["id"], {"dashboards": {}})
        return
    await lookup.async_refresh()
    connection.send_result(
        msg["id"],
        {
            "dashboards": {
                url_path: len(lookup.entities_for(url_path))
                for url_path in sorted(lookup.known())
            }
        },
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
            "capabilities": data.catalog.capabilities(),
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/record/start",
        vol.Required("role_id"): str,
    }
)
@callback
def handle_record_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Begin recording what a role's holders need.

    Refused for the predefined roles: a recording ends by writing what it saw
    into the role, and those cannot be edited.
    """
    data = _data(hass)
    role = data.store.roles.get(msg["role_id"])
    if role is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown role")
        return
    if role.get("system_generated"):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_NOT_ALLOWED,
            f"{msg['role_id']} is a predefined role and cannot be edited",
        )
        return
    connection.send_result(msg["id"], data.recorder.start(msg["role_id"]).as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/record/stop",
        vol.Required("role_id"): str,
        # Stopping without keeping is how a recording is abandoned.
        vol.Optional("apply", default=True): bool,
    }
)
@websocket_api.async_response
async def handle_record_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """End a recording and write what it saw into the role."""
    data = _data(hass)
    recording = data.recorder.stop(msg["role_id"])
    if recording is None:
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, "That role is not recording"
        )
        return

    seen = recording.as_dict()
    if not msg["apply"]:
        connection.send_result(msg["id"], {"applied": False, "seen": seen})
        return

    role = data.store.roles.get(msg["role_id"])
    if role is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown role")
        return
    try:
        updated = await data.store.async_update_role(
            msg["role_id"], record.apply(role, recording)
        )
    except (KeyError, ValueError, vol.Invalid) as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "applied": True,
            "seen": seen,
            "role": updated,
            # Adding to the allow side does not always take effect: a denial on
            # the role vetoes it. Saying which ones beats leaving someone to
            # find out from a broken dashboard.
            "blocked": record.still_blocked(hass, updated, recording),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/record/status"})
@callback
def handle_record_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return which roles are recording, and what they have seen so far."""
    recorder = _data(hass).recorder
    connection.send_result(
        msg["id"],
        {
            role_id: recording.as_dict()
            for role_id in recorder.active
            if (recording := recorder.get(role_id)) is not None
        },
    )


# The settings that live on the config entry rather than in a role. Home
# Assistant has an options flow for these already, and it cannot be reached:
# `config_panel_domain` points the Configure gear at this panel, and there is no
# second affordance. So they are served here, where the rest of this
# integration is administered anyway.
_SETTING_DEFAULTS = {
    CONF_PROXY_PORT: DEFAULT_PROXY_PORT,
    CONF_BIND_ADDRESS: DEFAULT_BIND_ADDRESS,
    CONF_UPSTREAM_HOST: DEFAULT_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT: DEFAULT_UPSTREAM_PORT,
    CONF_MANAGE_HTTP: False,
    CONF_RESTORE_ON_REMOVAL: DEFAULT_RESTORE_ON_REMOVAL,
}


@callback
def _entry(hass: HomeAssistant) -> Any:
    """Return this integration's config entry, or None if it has gone."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings/get"})
@callback
def handle_settings_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the entry's settings, with the defaults filled in."""
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No config entry")
        return
    current = {**entry.data, **entry.options}
    connection.send_result(
        msg["id"],
        {
            **{
                key: current.get(key, default)
                for key, default in _SETTING_DEFAULTS.items()
            },
            # Whether this integration moved Home Assistant, which decides
            # whether there is anything for the restore setting to put back.
            "moved": bool(entry.data.get(DATA_PREVIOUS_HTTP)),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/settings/set",
        vol.Optional(CONF_PROXY_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_BIND_ADDRESS): str,
        vol.Optional(CONF_UPSTREAM_HOST): str,
        vol.Optional(CONF_UPSTREAM_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_MANAGE_HTTP): bool,
        vol.Optional(CONF_RESTORE_ON_REMOVAL): bool,
    }
)
@callback
def handle_settings_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change the entry's settings.

    Written to `options` rather than `data`, which is where the options flow put
    them and what `async_setup_entry` reads on top of `data`. Updating the entry
    reloads it, so a changed port takes effect without a restart -- and takes the
    connection that asked for it with it, which the panel is told to expect.
    """
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No config entry")
        return
    changes = {key: msg[key] for key in _SETTING_DEFAULTS if key in msg}
    if not changes:
        connection.send_error(msg["id"], "invalid_format", "Nothing to change")
        return
    # Merge with current values: settings/set accepts partial updates, so the
    # combination of old and new has to be checked, not just the incoming keys.
    current = {**entry.data, **entry.options}
    effective = {
        **{
            key: current.get(key, default) for key, default in _SETTING_DEFAULTS.items()
        },
        **changes,
    }
    warning = None
    if effective.get(CONF_MANAGE_HTTP) and is_loopback_bind(
        str(effective.get(CONF_BIND_ADDRESS, DEFAULT_BIND_ADDRESS))
    ):
        warning = LOOPBACK_BIND_WARNING

    hass.config_entries.async_update_entry(entry, options={**entry.options, **changes})
    result: dict[str, Any] = {"changed": sorted(changes)}
    if warning is not None:
        result["warning"] = warning
    connection.send_result(msg["id"], result)
