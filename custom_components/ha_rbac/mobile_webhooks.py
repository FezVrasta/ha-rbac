"""Hold a Companion app's own webhook to the role of the person it belongs to.

`/api/webhook/{id}` carries no user, so the proxy forwards it: the id is the
credential. For `mobile_app` that credential sits on the phone of the person a
role restricts, and Core then runs the registration's commands with that
person's *native* permissions, which for a non-administrator is every entity.
So a restricted account could act outside its role from its own phone.

The proxy cannot read these requests, since the body may be encrypted end to
end, but Core decrypts before it dispatches. Guarding Core's decrypted command
registry applies the registration owner's role whatever the transport (local
HTTP, WebSocket, the cloud relay), and never trusts a user named in the payload:
the owner is read from the registration.

Each command that reaches beyond the registration is judged as the websocket
command it stands for, so the same service, target, tier and resource checks
apply, and a role opts in to more the same way it does anywhere else (for
example `tiers.allow: ["fire_event"]` for a phone that fires events). Commands
that only touch the registration itself -- location, sensors, encryption --
are left to Core.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .const import DATA_RBAC
from .decide import KIND_WS
from .denylog import Denial

_LOGGER = logging.getLogger(__name__)

# The registration's own business. None of it grants control of an entity.
LEFT_TO_CORE = frozenset(
    {
        "update_location",
        "update_registration",
        "enable_encryption",
        "register_sensor",
        "update_sensor_states",
        "get_zones",
        "get_config",
    }
)

# Webhook command -> the websocket command it is judged as.
JUDGED_AS = {
    "call_service": "call_service",
    "fire_event": "fire_event",
    "scan_tag": "fire_event",
    "render_template": "render_template",
    "stream_camera": "camera/stream",
    "conversation_process": "conversation/process",
}

_MARK = "_ha_rbac_guarded"


def _payloads(command: str, data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return the websocket requests a webhook command amounts to."""
    judged_as = JUDGED_AS[command]
    if not isinstance(data, dict):
        return [(judged_as, {"type": judged_as})]

    if command == "call_service":
        return [
            (
                judged_as,
                {
                    "type": judged_as,
                    "domain": data.get("domain"),
                    "service": data.get("service"),
                    "service_data": data.get("service_data") or {},
                },
            )
        ]
    if command == "render_template":
        # One request carries several named templates; each is judged on its own.
        return [
            (judged_as, {"type": judged_as, "template": item.get("template", "")})
            for item in data.values()
            if isinstance(item, dict)
        ] or [(judged_as, {"type": judged_as})]
    if command == "stream_camera":
        return [
            (
                judged_as,
                {"type": judged_as, "entity_id": data.get("camera_entity_id")},
            )
        ]
    return [(judged_as, {"type": judged_as, **data})]


class _Guard:
    """Wraps Core's webhook command registry."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the guard."""
        self._hass = hass
        self._originals: dict[str, Callable[..., Any]] = {}

    def install(self) -> None:
        """Wrap every command Core has registered. Safe to call again."""
        try:
            from homeassistant.components.mobile_app.webhook import (  # noqa: PLC0415
                WEBHOOK_COMMANDS,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("mobile_app is not importable; its webhooks are not guarded")
            return
        for command, handler in list(WEBHOOK_COMMANDS.items()):
            if getattr(handler, _MARK, False):
                continue
            self._originals[command] = handler
            WEBHOOK_COMMANDS[command] = self._wrap(command, handler)

    def uninstall(self) -> None:
        """Put Core's own handlers back."""
        try:
            from homeassistant.components.mobile_app.webhook import (  # noqa: PLC0415
                WEBHOOK_COMMANDS,
            )
        except Exception:  # noqa: BLE001
            return
        for command, handler in self._originals.items():
            WEBHOOK_COMMANDS[command] = handler
        self._originals.clear()

    def _wrap(self, command: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        guard = self

        async def guarded(hass: HomeAssistant, config_entry: ConfigEntry, data: Any):
            if (
                response := await guard._refuse(config_entry, command, data)
            ) is not None:
                return response
            return await handler(hass, config_entry, data)

        setattr(guarded, _MARK, True)
        return guarded

    async def _refuse(self, config_entry: ConfigEntry, command: str, data: Any):
        """Return an error response if the owner's role refuses this command."""
        from homeassistant.components.mobile_app.const import (  # noqa: PLC0415
            CONF_USER_ID,
        )
        from homeassistant.components.mobile_app.helpers import (  # noqa: PLC0415
            error_response,
        )

        hass = self._hass
        rbac = hass.data.get(DATA_RBAC)
        user_id = config_entry.data.get(CONF_USER_ID)
        if rbac is None or not user_id or command in LEFT_TO_CORE:
            return None
        if (user := await hass.auth.async_get_user(user_id)) is None:
            return None
        permissions = rbac.evaluator.async_permissions(user)
        if permissions.full_access:
            return None

        # A command this build does not know how to judge is refused rather than
        # handed the registration owner's native permissions.
        requests = (
            _payloads(command, data)
            if command in JUDGED_AS
            else [(f"webhook/{command}", {"type": f"webhook/{command}"})]
        )
        for name, payload in requests:
            decision = rbac.decider.decide(permissions, KIND_WS, name, payload)
            if decision.allowed:
                continue
            rbac.denylog.async_record(
                Denial(
                    user_id=user.id,
                    user_name=user.name or "",
                    kind="webhook",
                    name=f"webhook {command}",
                    reason=decision.reason,
                    resources=decision.resources,
                    detail=decision.detail,
                )
            )
            return error_response(
                "forbidden", decision.message or "Unauthorized", HTTPStatus.BAD_REQUEST
            )
        return None


@callback
def async_guard(hass: HomeAssistant) -> Callable[[], None]:
    """Guard mobile_app's webhooks now and whenever it loads; return the undo."""
    guard = _Guard(hass)

    @callback
    def _loaded(event: Event) -> None:
        if event.data.get("component") == "mobile_app":
            guard.install()

    unsubscribe = hass.bus.async_listen("component_loaded", _loaded)
    if "mobile_app" in hass.config.components:
        guard.install()

    @callback
    def undo() -> None:
        unsubscribe()
        guard.uninstall()

    return undo
