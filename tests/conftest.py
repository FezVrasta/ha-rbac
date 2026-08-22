"""Shared fixtures.

Rather than depend on pytest-homeassistant-custom-component (which pins its own
Home Assistant), the suite reuses the test fixtures from the Home Assistant core
checkout, so it always runs against the HA version actually installed.
"""

pytest_plugins = ["tests.conftest"]
