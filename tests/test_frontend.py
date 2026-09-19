"""Static checks on the admin panel.

The panel is one plain ES module with no build step and no JavaScript test
runner, so nothing here executes it. These cover the mistakes that take the
whole panel down while leaving the file syntactically valid -- the ones that
reach a browser looking fine and render a blank page.
"""

import pathlib
import re

_INTEGRATION = pathlib.Path(__file__).parent.parent / "custom_components" / "ha_rbac"
PANEL = _INTEGRATION / "frontend" / "ha-rbac-panel.js"
CONST = _INTEGRATION / "const.py"


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

    # The grant sections are declared as a list rather than one function call
    # each, so they are read back through the list. Every `draft:` key in it has
    # to be written on save, the same requirement by a different route.
    grant_drafts = set(re.findall(r'draft: "(\w+)"', source))
    assert grant_drafts, "the grant section descriptors disappeared"
    assert "this._draft[section.draft]" in saved, (
        "the grant sections are edited but never saved"
    )


def test_the_panel_offers_an_editor_for_every_grant_section() -> None:
    """A grant section with no editor is a grant nobody can write.

    The sections are declared twice by necessity -- once in const.py for the
    schema and the gates, once in the panel for the editor -- so they are pinned
    against each other. A section added to const.py with no descriptor here
    validates, compiles and enforces, while being invisible and unwritable in the
    only interface anybody uses.
    """
    source = PANEL.read_text()
    declared = set(re.findall(r'\{\s*\n\s*id: "(\w+)",\s*\n\s*draft: "\w+"', source))
    assert declared, "the grant section descriptors disappeared"

    # const.py is read rather than imported: nothing in this file executes the
    # integration, and a regex over the tuple is enough to compare two lists of
    # names.
    const = CONST.read_text()
    tuple_body = const.split("GRANT_SECTIONS: Final[tuple[str, ...]] = (", 1)[1]
    in_python = {
        name.lower() for name in re.findall(r"GRANT_(\w+)", tuple_body.split(")", 1)[0])
    }

    assert declared == in_python, (
        f"panel sections {sorted(declared)} do not match const.py {sorted(in_python)}"
    )
