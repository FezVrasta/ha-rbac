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
    start = source.index("_deleteRole() {")
    body = source[start : source.index('"roles/delete"', start)]

    assert "_confirm(" in body, "_deleteRole must confirm before it calls the API"


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


def test_the_tab_strip_is_a_real_tablist() -> None:
    """The ARIA tabs pattern is all-or-nothing, so its pieces are pinned together.

    A `role=tablist` whose buttons are not `role=tab`, or tabs that do not point
    at a panel, or a strip where every button is in the tab order, is a strip
    that reads as four unrelated buttons to a screen reader. Each piece is easy
    to drop in a refactor of the markup and nothing else would notice.
    """
    source = PANEL.read_text()

    assert 'role="tablist"' in source
    assert 'role="tab"' in source
    assert 'role="tabpanel"' in source
    assert "aria-controls=" in source, "a tab has to name the panel it controls"
    assert 'tabindex="${selected ? "0" : "-1"}"' in source, (
        "roving tabindex: Tab reaches the strip once, not once per tab"
    )
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in source, f"the tabs pattern needs {key}"


def test_keyboard_tab_selection_focuses_after_the_render_not_before() -> None:
    """Arrow-keying onto a tab must not drop keyboard focus.

    `_selectTab` cannot focus the new tab button itself. Denials and Settings
    fetch their own data and re-render when it lands, which is after the call
    returns, so a button focused there is thrown away by that render and focus
    falls back to the body -- one arrow key into the strip. The tab to focus is
    recorded and `_render` consumes it once the buttons exist.
    """
    source = PANEL.read_text()

    select = source[source.index("async _selectTab(") : source.index("  _wire() {")]
    assert "this._focusTab = tab" in select, "the tab to focus is recorded"
    assert ".focus()" not in select, (
        "_selectTab must not focus a button that a later render will replace"
    )

    render = source[source.index("  _render() {") : source.index("  _chrome() {")]
    assert "this._focusTab" in render and ".focus()" in render, (
        "_render is where the recorded tab is focused"
    )


def test_every_way_out_of_an_unsaved_role_edit_asks_first() -> None:
    """The draft lives only in the panel, so every exit has to be guarded.

    There are three: picking another role, leaving the Roles tab, and creating a
    role. Guarding only the first is the shape this started as, and it leaves the
    other two silently dropping an access-control change that reads as applied.
    """
    source = PANEL.read_text()

    assert "_confirmDiscard()" in source
    switch = source[source.index("async _switchRole(") :][:400]
    assert "_confirmDiscard()" in switch, "picking another role"

    select = source[source.index("async _selectTab(") : source.index("  _wire() {")]
    assert "_confirmDiscard()" in select, "leaving the Roles tab"

    wire = source[source.index("  _wire() {") :]
    new_role = wire[wire.index('on("new-role"') :][:200]
    assert "_confirmDiscard()" in new_role, "creating a role"


def test_the_confirmation_dialog_does_not_go_through_show_dialog() -> None:
    """Reaching `dialog-box` through `show-dialog` breaks every later dialog.

    The manager caches by tag: `element: dialogImport().then(() => createElement(tag))`,
    then awaits it. A panel cannot fetch the content-hashed chunk that defines
    `dialog-box`, so the promise never settles -- the dialog never opens, and the
    cache entry hangs every subsequent `dialog-box` for the life of the page, so
    the next "Restart Home Assistant?" silently does nothing.

    This pins the panel to its own dialog instead. If somebody reaches for
    `show-dialog` again, this is the test that says why not.
    """
    source = PANEL.read_text()

    # The name appears in the comment on `_confirm` explaining why not, so what
    # is pinned is that it is never used as an event name in code.
    assert '"show-dialog"' not in source, (
        "see _confirm: show-dialog poisons the frontend's dialog cache"
    )
    assert 'role="alertdialog"' in source, "the panel's own dialog"
    assert 'aria-modal="true"' in source


def test_the_dialog_survives_a_render() -> None:
    """A render replaces the shadow root, and one can fire while a dialog is open.

    The record poll re-renders on a timer. If that wiped the overlay, the dialog
    would vanish mid-question and its promise would never settle, so whatever was
    awaiting the answer would hang for ever.
    """
    source = PANEL.read_text()
    render = source[source.index("  _render() {") : source.index("  _chrome() {")]
    assert "this._modal" in render, "_render has to put an open dialog back"


def test_the_denials_table_can_be_filtered_and_says_when() -> None:
    """The Denials tab exists to find one row out of a hundred.

    Filtering in place matters as much as filtering at all: re-rendering the tab
    on every keystroke takes focus off the field mid-word, which makes the filter
    unusable with a keyboard.
    """
    source = PANEL.read_text()

    assert "_denialMatches(" in source
    assert "timeAgo(" in source, "a denial says how long ago it happened"
    # The field's own listener only, not the chip handlers below it: a chip click
    # holds no focus, so that one does re-render.
    wiring = source[source.index('getElementById("denial-filter")') :]
    listener = wiring[: wiring.index("});") + 3]
    assert "_refreshDenialRows()" in listener, (
        "typing must redraw the rows in place, not re-render the tab"
    )
    assert "_render()" not in listener, "a full render would steal focus mid-word"


def test_the_new_role_button_uses_an_attribute_this_frontend_reads() -> None:
    """Emphasis on the New role button has to use `appearance`, not `raised`.

    `raised` is the old mwc-button attribute. This frontend's `ha-button` takes
    `appearance` (filled/plain/accent), and an attribute it does not know is
    silently ignored -- so `raised` renders a plain button and the empty state's
    "press this" emphasis quietly does nothing.
    """
    source = PANEL.read_text()
    tag_start = source.index('<ha-button id="new-role"')
    tag = source[tag_start : source.index(">", tag_start) + 1]
    assert "appearance=" in tag, tag
    assert "raised" not in tag, tag
