# Contributing

Bug reports from real households are the most useful thing right now — especially "my dashboard broke, and here's what the Denials tab said".

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python testsupport/install_integration_requirements.py
./scripts-test
```

That pulls in `pytest-homeassistant-custom-component`, which packages Home Assistant's own test fixtures — so the suite needs nothing but the installed package, and runs identically here and in CI. It pins the oldest Home Assistant the integration supports. The second step installs the libraries of the two Home Assistant integrations the suite uses, at the versions that Home Assistant pins: the Supervisor's, and the built frontend, which is large (about 100 MB). Without the frontend, the check that needs it is skipped locally; in CI it fails.

That package stops at Home Assistant 2026.10. For newer releases, and betas, Home Assistant publishes its own, `pytest-homeassistant`, versioned with Home Assistant. CI runs the suite four ways:

| Run | Home Assistant | On a PR |
| --- | --- | --- |
| Tests | the oldest supported, `requirements-test.txt` | must pass |
| last known good | the newest release known to pass, `requirements-test-latest.txt` | must pass |
| latest | the newest release | warns |
| beta | the newest beta | warns |

The last two also run weekly on their own, and fail when they break, so a new release or beta that breaks the integration is reported without anyone pushing. A fix for such a break must still pass the two pinned runs, so it works on the release people are on now as well as the new one.

Dependabot opens a PR when a new release is out to move `requirements-test-latest.txt` forward. Merge it once its checks pass: that's what records a release as known good. Don't move `requirements-test.txt`: it marks the oldest release the integration supports, with `hacs.json`.

To run the last known good locally (or the newest: `pip install pytest-homeassistant`, adding `--pre` for the beta):

```bash
python3 -m venv .venv-latest
.venv-latest/bin/pip install -r requirements-test-latest.txt
.venv-latest/bin/python testsupport/install_integration_requirements.py
HA_PYTHON=.venv-latest/bin/python ./scripts-test
```

Import Home Assistant's test helpers (`MockConfigEntry` and the rest) from `testsupport.ha_test`, which works with either package.

Lint and format with the same configuration Home Assistant uses:

```bash
ruff check . && ruff format --check .
```

## What the tests are for

Most of them pin behaviour that failed open at some point. The ones worth knowing about before changing anything:

- `tests/test_catalog.py` cross-checks the runtime permission derivation against an independent static scan of the Home Assistant source. If Home Assistant changes how it marks administrative commands, this is what tells you — without it, the failure is silent and permissive.
- `tests/test_regressions.py` is one test per way the layer has been broken before. Each one failed before its fix.
- `tests/test_deployment_guard.py` covers the check for the single configuration mistake that reduces the whole thing to decoration.
- `tests/test_home_assistant_surface.py` checks what the integration relies on in Home Assistant itself: every element the panel uses still exists in the frontend, and every Supervisor websocket command has been reviewed. Home Assistant 2026.11 broke both (#83, #84/#85) with the rest of the suite green. When it fails on a beta, that's the warning: fix it before the release, and add a newly reviewed command to its list.
