"""Constants for the RBAC access control integration."""

from typing import TYPE_CHECKING, Final

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .models import RbacData

DOMAIN: Final = "ha_rbac"

STORAGE_KEY: Final = "ha_rbac.roles"
STORAGE_VERSION: Final = 1

CONF_PROXY_PORT: Final = "proxy_port"
CONF_BIND_ADDRESS: Final = "bind_address"
CONF_UPSTREAM_HOST: Final = "upstream_host"
CONF_UPSTREAM_PORT: Final = "upstream_port"
CONF_FAIL_OPEN: Final = "fail_open"

DEFAULT_PROXY_PORT: Final = 8124
DEFAULT_BIND_ADDRESS: Final = "0.0.0.0"
DEFAULT_UPSTREAM_HOST: Final = "127.0.0.1"

PANEL_URL_PATH: Final = "rbac"
STATIC_URL_PATH: Final = "/ha_rbac_static"

EVENT_RBAC_DENIED: Final = "rbac_denied"
DENYLOG_SIZE: Final = 500

# Tiers, ordered least to most privileged. Derived from HA's own decorators;
# never enumerated per command.
TIER_OPEN: Final = "open"
TIER_USER: Final = "user"
TIER_ADMIN: Final = "admin"
TIER_ORDER: Final = (TIER_OPEN, TIER_USER, TIER_ADMIN)

# Predefined role ids. system_generated, not editable, but cloneable.
ROLE_ADMIN: Final = "admin"
ROLE_USER: Final = "user"
ROLE_READ_ONLY: Final = "read_only"

# Resource keys the extractor collects. Each accepts str | list[str].
KEY_ENTITY: Final = "entity"
KEY_DEVICE: Final = "device"
KEY_AREA: Final = "area"
KEY_LABEL: Final = "label"
KEY_FLOOR: Final = "floor"

RESOURCE_KEYS: Final[dict[str, str]] = {
    "entity_id": KEY_ENTITY,
    "entity_ids": KEY_ENTITY,
    "device_id": KEY_DEVICE,
    "device_ids": KEY_DEVICE,
    "area_id": KEY_AREA,
    "area_ids": KEY_AREA,
    "label_id": KEY_LABEL,
    "label_ids": KEY_LABEL,
    "floor_id": KEY_FLOOR,
    "floor_ids": KEY_FLOOR,
}

# cv.entity_ids accepts these sentinels; "all" means the command is unbounded.
SENTINEL_ALL: Final = "all"
SENTINEL_NONE: Final = "none"

# An execute_script payload is attacker-controlled JSON, so the walk is capped.
MAX_WALK_DEPTH: Final = 12
MAX_WALK_NODES: Final = 5000

DATA_RBAC: "HassKey[RbacData]" = HassKey(DOMAIN)
