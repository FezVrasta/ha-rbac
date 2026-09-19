# Local Podman test harness for ha-rbac

> **Optional.** This is a convenience for manual testing, not part of the
> build or CI, and nothing in the project depends on it. Ignore it entirely if
> you prefer — the unit test suite is the source of truth.

Runs Home Assistant in a **local Podman container** and installs this
integration into it, so you can try the proxy by hand in a throwaway sandbox
rather than against a real instance. Docker also works: prefix any command
with `ENGINE=docker`.

Only the scripts and this documentation are tracked. Everything the harness
generates — the `config/` directory, tokens, the database — is git-ignored, so
running it never adds your instance's data to the repository.

## Layout

- `docker-compose.yml` — HA container with port 8123 published, config bind-mounted to `./config`.
- `_engine.sh` — picks `podman`/`podman compose` (or `podman-compose`), sourced by the others.
- `install-integration.sh` — install/reinstall the integration from a branch (or a local checkout), restart HA, tail logs.
- `logs.sh` — follow the log lines that matter for the proxy move/reload.
- `recover.sh` — get Home Assistant back if the proxy locks you out.

## Prerequisites

- Podman, plus a compose front-end: either Podman 4.x+ (`podman compose`) or
  `podman-compose` (`pip install podman-compose`).
- On macOS: `podman machine init && podman machine start` first.
- `git` and `python3`.

## Steps

1. **Start Home Assistant** (from this folder):
   ```bash
   cd rpi-test
   podman compose up -d      # or: podman-compose up -d
   ./logs.sh raw             # wait for "Home Assistant initialized", then Ctrl-C
   ```
   Open `http://localhost:8123`, create the owner account, let it settle.

2. **Install the integration.** Defaults to your fix branch:
   ```bash
   ./install-integration.sh
   ```
   It prints the installed version so you can be sure it is not a stale copy.
   Alternatives:
   ```bash
   REPO=https://github.com/FezVrasta/ha-rbac BRANCH=main ./install-integration.sh
   ./install-integration.sh --local ..     # install from THIS repo checkout
   ```
   > `--local ..` installs from the repo you are sitting in, so you can test
   > uncommitted local edits to `custom_components/ha_rbac`.

3. **Add it in the UI**: Settings > Devices & Services > Add Integration >
   "RBAC Access Control". For a first test, enable "manage HTTP" and accept the
   defaults:
   - proxy answers on **8123** (the published port)
   - Home Assistant moves to **127.0.0.1:8124** (loopback = inside the container)
   - Home Assistant **restarts** to apply the move; if the proxy does not serve
     within five minutes it reverts by itself.

   After the restart, `http://localhost:8123` is going through the proxy.

   > Only 8123 is published. 8124 is deliberately NOT exposed to your machine —
   > that is the whole point: Home Assistant must only be reachable through the
   > proxy.

## Testing the reload fix (issue #23)

The bug: reloading the integration on an already-moved instance restarts HA in
a loop and takes it off the network. The fix makes `is_aligned()` recognise a
loopback-only bind however it is spelled.

1. Make sure you installed the **fix branch** (`./install-integration.sh` default)
   or your local checkout (`--local ..`).
2. In the UI: Settings > Devices > RBAC > (three dots) > **Reload**.
3. Watch `./logs.sh`:
   - **Fixed:** the proxy just restarts. You should NOT see repeated
     "Moving Home Assistant to 127.0.0.1:8124 ... restarting" lines.
   - **Broken (old code):** that move+restart line fires on the reload, and the
     instance keeps restarting.

To compare against the broken behaviour, reinstall stock main and repeat:
```bash
REPO=https://github.com/FezVrasta/ha-rbac BRANCH=main ./install-integration.sh
```

## If you get locked out

Reach unfiltered Home Assistant directly on the upstream loopback port by
running a shell inside the container:
```bash
podman exec -it homeassistant bash
# inside the container:
curl -sS http://127.0.0.1:8124/manifest.json    # stock HA, bypassing the proxy
```
Or recover by editing the bind-mounted config (you are never truly stuck):
```bash
./recover.sh disable   # remove the integration, restart
./recover.sh unmove    # drop server_host from the stored HTTP config, restart
./recover.sh both
```

## Notes

- `./config/.storage/http` holds the (possibly moved) HTTP config.
- `./config/.storage/ha_rbac.roles` holds the roles; delete it and restart to
  reset to derived defaults.
- To wipe everything and start over: `podman compose down && rm -rf config`.
- The `:Z` on the volume mount relabels for SELinux (Fedora/RHEL rootless
  Podman). Harmless on macOS; change to `:z` if another container shares it.
