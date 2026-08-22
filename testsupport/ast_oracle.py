"""Static oracle for the catalogue tests.

Walks the Home Assistant source with `ast` and reports what each
`@websocket_command` declares. The runtime catalogue is derived from private
internals, so it needs an independent second opinion to be checked against.
"""

import ast
import pathlib
from dataclasses import dataclass, field

RESOURCE_FIELDS = {
    "entity_id",
    "entity_ids",
    "device_id",
    "device_ids",
    "area_id",
    "area_ids",
    "label_id",
    "label_ids",
    "floor_id",
    "floor_ids",
    "target",
}


@dataclass(slots=True)
class Declared:
    """What a websocket_command decorator declares, read statically."""

    command: str
    file: str
    admin: bool
    require_user: bool
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)


def _dotted(node: ast.expr) -> str:
    """Return the dotted name of a decorator expression."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def scan(root: pathlib.Path) -> dict[str, Declared]:
    """Return every statically declared websocket command under `root`."""
    found: dict[str, Declared] = {}

    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            decorators = [_dotted(d) for d in node.decorator_list]
            calls = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call) and _dotted(d).endswith("websocket_command")
            ]
            if not calls:
                continue

            declared = Declared(
                command="",
                file=str(path),
                admin=any(d.endswith("require_admin") for d in decorators),
                require_user=any(d.endswith("ws_require_user") for d in decorators),
            )

            arg = calls[0].args[0] if calls[0].args else None
            if isinstance(arg, ast.Dict):
                for key, value in zip(arg.keys, arg.values, strict=False):
                    marker, name = None, None
                    if isinstance(key, ast.Call):
                        marker = _dotted(key)
                        if key.args and isinstance(key.args[0], ast.Constant):
                            name = key.args[0].value
                    elif isinstance(key, ast.Constant):
                        marker, name = "vol.Required", key.value

                    if name == "type" and isinstance(value, ast.Constant):
                        declared.command = value.value
                    if name in RESOURCE_FIELDS and marker:
                        bucket = (
                            declared.required
                            if marker.endswith("Required")
                            else declared.optional
                        )
                        bucket.add(name)

            # Commands whose schema is built dynamically have no literal type.
            if declared.command:
                found[declared.command] = declared

    return found
