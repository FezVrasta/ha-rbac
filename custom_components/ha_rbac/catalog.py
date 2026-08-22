"""Derive the permission surface from Home Assistant's own runtime registries.

Nothing here is a maintained list. Command tiers come from HA's own
`require_admin` decorator, read back off the handler; resource shapes come from
the voluptuous schemas HA already attached. The layer therefore tracks upstream
across releases instead of rotting.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

from homeassistant.components.websocket_api import const as ws_const
from homeassistant.core import HomeAssistant, callback

from .const import TIER_ADMIN, TIER_OPEN, TIER_USER
from .extract import TARGET_KEY, schema_resource_markers

_LOGGER = logging.getLogger(__name__)

# `require_admin` and `ws_require_user` are closures; `functools.wraps` copies
# __dict__ and sets __wrapped__ but never touches __code__, so the closure's own
# function name survives and identifies the decorator.
ADMIN_WRAPPER_NAME = "with_admin"
USER_WRAPPER_NAME = "check_current_user"

# Commands that mutate configuration or storage. A regex, not a list: it does not
# need revisiting when an integration adds a command next release.
WRITE_PATTERN = re.compile(
    r"^config/|/(save|create|update|delete|remove|add|set|move|reload|import)(/|$)"
)

def introspection_works() -> bool:
    """Verify tier derivation against Home Assistant's own decorators.

    Rather than infer health from how many commands look admin -- which varies
    with the set of loaded integrations -- run the mechanism against the real
    `require_admin` and see whether it still recognises the result. If upstream
    renames the wrapper, this fails immediately and deterministically instead of
    silently classifying everything as open.
    """
    from homeassistant.components.websocket_api import decorators  # noqa: PLC0415

    def probe(hass: Any, connection: Any, msg: Any) -> None:
        """Do nothing; only its wrapper is inspected."""

    return (
        derive_tier(decorators.require_admin(probe)) == TIER_ADMIN
        and derive_tier(decorators.ws_require_user()(probe)) == TIER_USER
        and derive_tier(probe) == TIER_OPEN
    )


@dataclass(slots=True)
class CommandInfo:
    """What has been derived about one websocket command."""

    command: str
    tier: str
    required_resources: set[str]
    optional_resources: set[str]
    is_write: bool

    @property
    def has_resource_field(self) -> bool:
        """Return True if the schema can carry a resource reference at all."""
        return bool(self.required_resources or self.optional_resources)


def derive_tier(handler: Any) -> str:
    """Return the tier Home Assistant itself enforces for a handler.

    Walks the `__wrapped__` chain looking for the decorator closures. Verified
    against both decorator orderings found in core.
    """
    names: list[str] = []
    func: Any = handler
    seen = 0
    while func is not None and seen < 20:
        if (code := getattr(func, "__code__", None)) is not None:
            names.append(code.co_name)
        func = getattr(func, "__wrapped__", None)
        seen += 1

    if ADMIN_WRAPPER_NAME in names:
        return TIER_ADMIN
    if USER_WRAPPER_NAME in names:
        return TIER_USER
    return TIER_OPEN


class Catalog:
    """The derived command catalogue, rebuilt as integrations register."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty catalogue."""
        self._hass = hass
        self._commands: dict[str, CommandInfo] = {}
        self.degraded = False

    @property
    def commands(self) -> dict[str, CommandInfo]:
        """Return the derived command information."""
        return self._commands

    @callback
    def rebuild(self, _event: Any = None) -> None:
        """Re-derive the catalogue from `hass.data["websocket_api"]`."""
        handlers = self._hass.data.get(ws_const.DOMAIN) or {}
        commands: dict[str, CommandInfo] = {}

        for command, entry in handlers.items():
            handler, schema = entry if isinstance(entry, tuple) else (entry, None)
            required, optional = schema_resource_markers(schema)
            commands[command] = CommandInfo(
                command=command,
                tier=derive_tier(handler),
                required_resources=required,
                optional_resources=optional,
                is_write=bool(WRITE_PATTERN.search(command)),
            )

        self._commands = commands
        self._check_not_degraded()

    def _check_not_degraded(self) -> None:
        """Fail closed if tier derivation stops working.

        The introspection relies on undocumented internals, and its failure mode
        is permissive: an unrecognised wrapper makes every command look open. So
        the mechanism is tested directly rather than trusted.
        """
        if introspection_works():
            self.degraded = False
            return

        self.degraded = True
        _LOGGER.error(
            "RBAC tier derivation no longer recognises Home Assistant's own "
            "require_admin decorator. Every command would be classified as "
            "unrestricted, so enforcement is disabled rather than failing open. "
            "This usually means an upstream change to "
            "homeassistant.components.websocket_api.decorators"
        )

    @callback
    def tier_for(self, command: str) -> str:
        """Return the tier for a command, defaulting to admin when unknown.

        An unknown command is one this build has never seen registered, so the
        safe assumption is the most restrictive one.
        """
        if (info := self._commands.get(command)) is None:
            return TIER_ADMIN
        return info.tier

    @callback
    def info_for(self, command: str) -> CommandInfo | None:
        """Return the derived information for a command, if known."""
        return self._commands.get(command)

    @callback
    def as_dict(self) -> list[dict[str, Any]]:
        """Return the catalogue for the admin UI."""
        return sorted(
            (
                {
                    "command": info.command,
                    "tier": info.tier,
                    "resources": sorted(
                        info.required_resources | info.optional_resources
                    ),
                    "bounded_by_schema": info.has_resource_field,
                    "write": info.is_write,
                }
                for info in self._commands.values()
            ),
            key=lambda item: item["command"],
        )
