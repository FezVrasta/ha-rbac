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


def test_deleting_a_role_asks_first() -> None:
    """Delete is the one button in the panel that cannot be taken back.

    Its holders fall back to their Home Assistant group the moment it lands, so
    a misplaced click hands someone the whole instance. Every other action here
    is editable afterwards, which is why this is the only one pinned: the
    confirmation is easy to drop in a refactor and nothing else would notice.
    """
    source = PANEL.read_text()
    start = source.index("  _deleteRole() {")
    body = source[start : source.index('"roles/delete"', start)]

    assert "confirm(" in body, "_deleteRole must confirm before it calls the API"


def test_every_editable_rule_list_is_read_back_when_a_role_is_saved() -> None:
    """A section that renders but is never saved looks like it works and does not.

    Each rule list is read out of the role into a draft, rendered, and written
    back on save. Miss the last step and the panel accepts the edit, redraws it,
    and drops it -- which for an access-control rule reads as "I restricted
    that" when nothing was restricted. Pinned as a set so a section added later
    is caught by the same check.
    """
    source = PANEL.read_text()
    drafts = set(
        re.findall(r"(\w+): read(?:Attribute|Choice|Schedule)?\w*\(role\)", source)
    )
    assert {"attrRules", "choiceRules"} <= drafts, drafts

    saved = source[source.index("_payload()") :]
    for draft in drafts:
        assert f"this._draft.{draft}" in saved, f"{draft} is edited but never saved"
