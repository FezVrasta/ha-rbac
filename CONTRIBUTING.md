# Contributing

Bug reports from real households are the most useful thing right now —
especially "my dashboard broke, and here's what the Denials tab said".

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
./scripts-test
```

That pulls in `pytest-homeassistant-custom-component`, which packages Home
Assistant's own test fixtures — so the suite needs nothing but the installed
package, and runs identically here and in CI. It pins the Home Assistant version
the suite exercises; bump it in `requirements-test.txt` to test against a newer
one.

Lint and format with the same configuration Home Assistant uses:

```bash
ruff check . && ruff format --check .
```

## What the tests are for

Most of them pin behaviour that failed open at some point. The ones worth
knowing about before changing anything:

- `tests/test_catalog.py` cross-checks the runtime permission derivation against
  an independent static scan of the Home Assistant source. If Home Assistant
  changes how it marks administrative commands, this is what tells you — without
  it, the failure is silent and permissive.
- `tests/test_regressions.py` is one test per way the layer has been broken
  before. Each one failed before its fix.
- `tests/test_deployment_guard.py` covers the check for the single
  configuration mistake that reduces the whole thing to decoration.
