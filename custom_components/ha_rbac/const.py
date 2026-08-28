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
# Whether this integration moves Home Assistant's own listener for you.
CONF_MANAGE_HTTP: Final = "manage_http"
# Put Home Assistant back where it was if this integration is removed or
# disabled. On by default: leaving it on loopback with nothing in front of it
# takes the instance off the network entirely.
CONF_RESTORE_ON_REMOVAL: Final = "restore_network_on_removal"
DEFAULT_RESTORE_ON_REMOVAL: Final = True
# What the HTTP config looked like before the move, recorded on the config entry
# at the moment it is staged. Without it there is nothing to put back.
DATA_PREVIOUS_HTTP: Final = "previous_http"

DEFAULT_PROXY_PORT: Final = 8123
DEFAULT_BIND_ADDRESS: Final = "0.0.0.0"
DEFAULT_UPSTREAM_HOST: Final = "127.0.0.1"
DEFAULT_UPSTREAM_PORT: Final = 8124

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
ROLE_EDITOR: Final = "editor"
ROLE_USER: Final = "user"
ROLE_READ_ONLY: Final = "read_only"

# Named parts of the administrative surface, so that granting one does not mean
# writing a glob. The commands themselves are still derived -- this only says
# which of them belong together under a name an administrator recognises,
# because a role editor offering four hundred command names is not a role
# editor.
#
# Patterns match the same request name the tier gate sees: a websocket command,
# or `"METHOD /path"` for REST, which is why the ones with a space in them match
# a URL. A namespace nobody has grouped stays behind the `admin` ceiling, which
# is the conservative direction -- it is withheld rather than handed out.
CAPABILITIES: Final = (
    {
        "id": "automations",
        "title": "Automations",
        "description": "Create, edit, debug and run automations and blueprints.",
        "patterns": (
            "automation/*",
            "blueprint/*",
            "trace/*",
            "validate_config",
            "test_condition",
            "subscribe_trigger",
            "subscribe_condition",
            "execute_script",
            "* /api/config/automation/config/*",
        ),
    },
    {
        "id": "scripts",
        "title": "Scripts",
        "description": "Create and edit scripts.",
        "patterns": ("script/*", "* /api/config/script/config/*"),
    },
    {
        "id": "scenes",
        "title": "Scenes",
        "description": "Create and edit scenes.",
        "patterns": ("scene/*", "* /api/config/scene/config/*"),
    },
    {
        "id": "dashboards",
        "title": "Dashboards",
        "description": (
            "Create, edit and delete dashboards. Loading custom resources into "
            "everyone's frontend stays with administrators."
        ),
        # `lovelace/resources/*` is deliberately absent: a resource is
        # JavaScript loaded into every browser including an owner's, so granting
        # it hands over a good deal more than dashboards.
        "patterns": ("lovelace/config/*", "lovelace/dashboards/*"),
    },
    {
        "id": "helpers",
        "title": "Helpers",
        "description": "Create and edit helpers, timers, schedules and tags.",
        "patterns": (
            "input_boolean/*",
            "input_button/*",
            "input_datetime/*",
            "input_number/*",
            "input_select/*",
            "input_text/*",
            "counter/*",
            "timer/*",
            "schedule/*",
            "image/*",
            "tag/*",
        ),
    },
    {
        "id": "organisation",
        "title": "Areas, floors and labels",
        "description": "Rename and rearrange how the house is organised.",
        "patterns": (
            "config/area_registry/*",
            "config/floor_registry/*",
            "config/label_registry/*",
            "config/category_registry/*",
        ),
    },
    {
        "id": "devices",
        "title": "Devices and integrations",
        "description": "Add, configure and remove integrations, devices and entities.",
        "patterns": (
            "config_entries/*",
            "config/device_registry/*",
            "config/entity_registry/*",
            "diagnostics/*",
            "homeassistant/expose_entity*",
            "homeassistant/expose_new_entities/*",
            "* /api/config/config_entries/*",
        ),
    },
    {
        "id": "users",
        "title": "Users",
        "description": "Create users, change their passwords and delete them.",
        "patterns": ("config/auth/*", "config/auth_provider/*"),
    },
    {
        "id": "backups",
        "title": "Backups",
        "description": "Create, download and restore backups.",
        "patterns": ("backup/*",),
    },
)

CAPABILITY_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    capability["id"]: capability["patterns"] for capability in CAPABILITIES
}

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
# aiohttp routes cannot be removed, so the static path is registered once for
# the lifetime of the process rather than per config entry.
DATA_STATIC_PATH_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_static")
