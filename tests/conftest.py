"""Shared fixtures.

Home Assistant's own test fixtures come from `pytest-homeassistant` (2026.11 and
later) or `pytest-homeassistant-custom-component` (earlier releases). Either one
registers itself as a pytest plugin, so the suite needs only the installed
package, not a checkout of Home Assistant core. Tests import the helpers through
`testsupport.ha_test`, which works with both.
"""

import pytest


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load this integration in every test."""
