# Quickstart — test ha-rbac in a local Podman container

Full detail is in `README.md`. Docker also works: prefix any command with
`ENGINE=docker`.

## Prerequisites

Podman + a compose front-end (`podman compose` on Podman 4.x+, or
`pip install podman-compose`). On macOS, start the Podman VM first:

```bash
podman machine init && podman machine start
```

Also needs `git`, `python3`, and `curl` (you have all three).

## Fully automated (one command)

From nothing to a running proxy:

```bash
cd rpi-test
./bootstrap.sh
```

That will:
1. start the Home Assistant container and wait for its API,
2. create the owner account (`admin` / `admin`) and save a token to `./.token`,
3. install the integration (defaults to your fix branch),
4. best-effort add the config entry, which moves Home Assistant to loopback and
   restarts it so the proxy answers on 8123.

Then watch it settle:

```bash
./logs.sh
```

Open **http://localhost:8123** (owner `admin` / `admin`).

### Variations

```bash
./bootstrap.sh --no-add                 # stop before adding the entry (add it in the UI)
./bootstrap.sh --local ..               # install from THIS checkout (uncommitted edits)
REPO=https://github.com/FezVrasta/ha-rbac BRANCH=main ./bootstrap.sh   # stock main
HA_USER=me HA_PASS=secret ./bootstrap.sh
```

## The best-effort caveat

Adding the config entry drives Home Assistant's config flow over the API, and
completing it with `manage_http=true` makes Home Assistant **move its own port
and restart mid-flow**. That is inherently fragile. If `add-integration.sh`
does not complete cleanly, just add it in the UI — the reliable path:

> Settings > Devices & Services > Add Integration > "RBAC Access Control",
> enable "manage HTTP", accept defaults (proxy 8123, HA -> 127.0.0.1:8124).

Everything up to that point (container, owner, token, code on disk) is solid.

## Step-by-step (if you'd rather not use bootstrap)

```bash
podman compose up -d          # or: podman-compose up -d
./setup.sh --no-add           # onboard owner + token + install code, no entry
# add the integration in the UI, or:
./add-integration.sh          # best-effort
./logs.sh
```

## Test the reload fix (#23)

Settings > Devices > RBAC > **(three dots) > Reload**, while watching `./logs.sh`:

- **Fixed:** the proxy just restarts. No repeated
  `Moving Home Assistant to 127.0.0.1:8124 ... restarting`.
- **Broken:** that line fires and Home Assistant loops.

Compare against stock: `REPO=https://github.com/FezVrasta/ha-rbac BRANCH=main ./install-integration.sh`.

## If you get locked out

```bash
podman exec -it homeassistant bash
curl -sS http://127.0.0.1:8124/manifest.json   # stock HA, bypassing the proxy
```

or `./recover.sh both`. Wipe clean: `podman compose down && rm -rf config .token`.
