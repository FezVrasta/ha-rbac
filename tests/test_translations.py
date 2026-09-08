"""Checks on the JSON the integration ships.

These files are read by Home Assistant with a strict parser, and by nothing in
the request path -- so a broken one does not fail a test, it fails the config
flow at the moment somebody is trying to set the integration up.

The way that happens is a formatter. Ruff formats JSON as well as Python now,
and writes a trailing comma before the closing brace, which is valid JSON5 and
not valid JSON. `ruff format .` walks for Python and never reaches these, but
an editor formatting the file you have open names it directly and does. The
ruff config excludes them, with `force-exclude` so the exclude survives a path
given explicitly; this is the check that notices if that stops working.
"""

import json
import pathlib

import pytest

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / "ha_rbac"
JSON_FILES = sorted(COMPONENT.rglob("*.json"))


def test_the_component_ships_the_json_it_is_expected_to() -> None:
    """Guard the guard: a glob that matched nothing would pass every test."""
    names = {path.name for path in JSON_FILES}
    assert {"manifest.json", "strings.json"} <= names, names
    assert (COMPONENT / "translations" / "en.json") in JSON_FILES


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda path: path.name)
def test_every_shipped_json_file_is_strictly_valid(path: pathlib.Path) -> None:
    """Trailing commas parse as JSON5 and are what a formatter leaves behind."""
    json.loads(path.read_text())


def test_the_translation_matches_the_strings_it_translates() -> None:
    """`strings.json` is the source and `en.json` is served to the browser.

    Adding a message to one and not the other shows an untranslated key in the
    UI, which is the sort of thing only somebody hitting that exact step sees.
    """
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())

    assert strings == english
