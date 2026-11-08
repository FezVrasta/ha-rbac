"""Install the libraries the suite needs from Home Assistant's own integrations.

`pytest-homeassistant` installs Home Assistant but leaves each integration's
libraries to the project. The suite imports the Supervisor integration, so it
needs that integration's libraries at the versions the installed Home Assistant
declares. Reading the manifest from the installed copy keeps them in step.

    python testsupport/install_integration_requirements.py
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

# The integrations the suite imports beyond what Home Assistant installs.
INTEGRATIONS = ("hassio",)


def main() -> None:
    """Install the requirements of each integration in INTEGRATIONS."""
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or spec.origin is None:
        sys.exit("Home Assistant is not installed")
    components = Path(spec.origin).parent / "components"
    requirements = [
        requirement
        for integration in INTEGRATIONS
        for requirement in json.loads(
            (components / integration / "manifest.json").read_text()
        ).get("requirements", [])
    ]
    if requirements:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *requirements])


if __name__ == "__main__":
    main()
