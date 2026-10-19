"""A restricted account must still have a working frontend landing page."""

from types import SimpleNamespace

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant

from custom_components.ha_rbac.filters import REGISTRY, FilterContext


async def test_hidden_default_uses_allowed_dashboard(hass: HomeAssistant) -> None:
    """Rewriting one response must not change another user's default."""
    hass.data[DATA_PANELS] = {
        path: SimpleNamespace(component_name="lovelace", require_admin=False)
        for path in ("lovelace", "dashboard-child", "dashboard-other")
    }
    ctx = FilterContext(hass, lambda e, k: True, lambda p: p != "lovelace")
    source = {"value": {"default_panel": "lovelace", "onboarded_version": "2026.9"}}
    result = REGISTRY.filter_result("frontend/get_system_data", ctx, source)
    assert result["value"]["default_panel"] == "dashboard-child"
    assert result["value"]["onboarded_version"] == "2026.9"
    assert source["value"]["default_panel"] == "lovelace"
    assert (
        REGISTRY.filter_event("frontend/subscribe_system_data", ctx, source) == result
    )


async def test_allowed_personal_default_is_preserved(hass: HomeAssistant) -> None:
    """An allowed personal choice wins over the automatic fallback."""
    hass.data[DATA_PANELS] = {
        "dashboard-child": SimpleNamespace(
            component_name="lovelace", require_admin=False
        )
    }
    ctx = FilterContext(hass, lambda e, k: True, lambda p: p == "dashboard-child")
    source = {"value": {"core": {"default_panel": "dashboard-child"}}}
    assert REGISTRY.filter_result("frontend/get_user_data", ctx, source) == source
    assert REGISTRY.filter_event("frontend/subscribe_user_data", ctx, source) == source


async def test_no_allowed_dashboard_has_safe_fallback(hass: HomeAssistant) -> None:
    """A role with no dashboards must not gain one through the fallback."""
    ctx = FilterContext(hass, lambda e, k: False, lambda p: False)
    source = {"value": {"default_panel": "lovelace"}}
    result = REGISTRY.filter_result("frontend/get_system_data", ctx, source)
    assert result["value"]["default_panel"] == "notfound"
    panels = {"lovelace": {"title": "Overview"}, "notfound": {"title": None}}
    assert REGISTRY.filter_result("get_panels", ctx, panels) == {
        "notfound": {"title": None}
    }
