"""Shared fixtures.

Home Assistant's own test fixtures come from `pytest-homeassistant-custom-component`,
which registers itself as a pytest plugin — so the suite needs only the
installed package, not a checkout of Home Assistant core.
"""

import pytest


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load this integration in every test."""
