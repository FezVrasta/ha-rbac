# Contributing

Bug reports from real households are the most useful thing right now — especially "my dashboard broke, and here's what the Denials tab said".

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
./scripts-test
```

That pulls in `pytest-homeassistant-custom-component`, which packages Home Assistant's own test fixtures — so the suite needs nothing but the installed package, and runs identically here and in CI. It pins the oldest Home Assistant the integration supports.

That package stops at Home Assistant 2026.10. For newer releases, and betas, Home Assistant publishes its own, `pytest-homeassistant`, versioned with Home Assistant. CI runs the suite three ways: the pinned oldest release (which must pass), the newest release, and the newest beta. The last two warn without blocking a PR, and run weekly on their own. To run the newest locally:

```bash
python3 -m venv .venv-latest
.venv-latest/bin/pip install pytest-homeassistant    # add --pre for the beta
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
