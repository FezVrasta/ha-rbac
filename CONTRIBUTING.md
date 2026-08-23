# Contributing

Bug reports from real households are the most useful thing right now —
especially "my dashboard broke, and here's what the Denials tab said".

## Running the tests

The suite reuses Home Assistant core's own pytest fixtures, so it needs a
checkout of [home-assistant/core][core], not just the installed package:

```bash
git clone https://github.com/home-assistant/core ~/src/home-assistant-core
cd ~/src/home-assistant-core && script/setup

cd /path/to/ha-rbac
HA_CORE=~/src/home-assistant-core ./scripts-test
```

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

[core]: https://github.com/home-assistant/core
