"""Static checks on the admin panel.

The panel is one plain ES module with no build step and no JavaScript test
runner, so nothing here executes it. These cover the mistakes that take the
whole panel down while leaving the file syntactically valid -- the ones that
reach a browser looking fine and render a blank page.
"""

import pathlib
import re

PANEL = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "ha_rbac"
    / "frontend"
    / "ha-rbac-panel.js"
)


def _template_literal(name: str) -> str:
    """Return the contents of a top-level ``const <name> = `...`;`` literal."""
    source = PANEL.read_text()
    opening = f"const {name} = `"
    start = source.index(opening) + len(opening)
    return source[start : source.index("`;", start)]


def test_the_stylesheet_contains_no_backtick() -> None:
    """One backtick in the CSS ends the template literal and the panel dies.

    A comment reading "same grid as `.rule`" did exactly that: the file still
    parsed, so neither `node --check` nor anything in this suite noticed, and
    what followed was parsed as a tagged template. The panel rendered as a blank
    page with one TypeError in a console nobody was looking at.
    """
    assert "`" not in _template_literal("STYLES")


def test_every_element_the_panel_looks_up_is_one_it_renders() -> None:
    """A renamed id fails silently: `querySelector` returns null and stops.

    The ids are written in one place and read in another, so this pins them
    together. It is a text search, not a DOM, so it only knows about ids that
    appear literally in both -- which is all of them today.
    """
    source = PANEL.read_text()
    # In the markup, and set on an element built in code -- `row.id = "x"`.
    rendered = set(re.findall(r'\sid="([a-z0-9-]+)"', source))
    rendered |= set(re.findall(r'\.id = "([a-z0-9-]+)"', source))
    # `.getElementById("x")` and `.querySelector("#x")`, ignoring compound
    # selectors like "#caps ha-checkbox" which name a child, not the id.
    looked_up = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', source))
    looked_up |= set(re.findall(r'querySelector\("#([a-z0-9-]+)"\)', source))

    assert looked_up <= rendered, (
        f"looked up but never rendered: {looked_up - rendered}"
    )
