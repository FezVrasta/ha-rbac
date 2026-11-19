"""Install the libraries the suite needs from Home Assistant's own integrations.

Home Assistant's test packages install Home Assistant but leave each
integration's libraries to the project. The suite needs two:

- the Supervisor integration's, because the suite loads its websocket commands;
- the frontend integration's, which is Home Assistant's built frontend, because
  `tests/test_home_assistant_surface.py` checks the panel against it.

Reading each manifest from the installed Home Assistant keeps the versions in
step with it.

    python testsupport/install_integration_requirements.py
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

# The integrations the suite imports beyond what Home Assistant installs.
INTEGRATIONS = ("frontend", "hassio")


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
