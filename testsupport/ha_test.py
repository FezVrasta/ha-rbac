"""Home Assistant's test helpers, from whichever package supplies them.

From 2026.11, Home Assistant publishes its test helpers itself, as
`pytest-homeassistant`, versioned with Home Assistant and including betas.
Before that they came only from the community package
`pytest-homeassistant-custom-component`, which stopped at 2026.10. CI tests the
oldest supported Home Assistant with the community package and the newest with
the official one, so the suite imports the helpers through here.
"""

try:
    from pytest_homeassistant.common import MockConfigEntry, MockUser
    from pytest_homeassistant.typing import WebSocketGenerator
except ModuleNotFoundError:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        MockUser,
    )
    from pytest_homeassistant_custom_component.typing import WebSocketGenerator

__all__ = ["MockConfigEntry", "MockUser", "WebSocketGenerator"]
