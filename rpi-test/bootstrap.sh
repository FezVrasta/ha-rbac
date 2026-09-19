#!/usr/bin/env bash
# Fully automated, one command: from nothing (even a fresh machine / after a
# prune) to a running RBAC proxy.
#
# It will, in order:
#   1. make sure the Podman machine is running (macOS),
#   2. optionally wipe old state for a clean run (--clean),
#   3. bring the container up and wait for the API,
#   4. create the owner account and save a token,
#   5. install the integration,
#   6. best-effort add the config entry (moves HA to loopback and restarts),
#   7. WAIT until the proxy is serving again, then keep tailing logs (does not
#      finish -- Ctrl-C to detach; the container keeps running).
#
# Usage:
#   ./bootstrap.sh                 # everything, reusing any existing config/
#   ./bootstrap.sh --clean         # wipe config/ + .token first (fresh HA)
#   ./bootstrap.sh --no-add        # stop before adding the entry (add it in the UI)
#   ./bootstrap.sh --no-wait       # return to the shell instead of waiting/tailing
#   ./bootstrap.sh --clean --local ..     # fresh, install from THIS checkout
#
# REPO / BRANCH env vars are passed through to the installer:
#   REPO=https://github.com/FezVrasta/ha-rbac BRANCH=main ./bootstrap.sh

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_engine.sh
source "./_engine.sh"

ADD=1
CLEAN=0
WAIT=1
PASS_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-add)  ADD=0; shift ;;
    --clean)   CLEAN=1; shift ;;
    --no-wait) WAIT=0; shift ;;
    *) PASS_ARGS+=("$1"); shift ;;
  esac
done

HA_URL="${HA_URL:-http://localhost:8123}"

# 1. Podman machine (macOS). On Linux/Docker there is no machine and this list
#    is empty, so it is a no-op. If a machine exists but none is running, start.
if [[ "$ENGINE" == "podman" ]] && command -v podman >/dev/null 2>&1; then
  machines="$(podman machine list --format '{{.Running}}' 2>/dev/null || true)"
  if [[ -n "$machines" ]] && ! grep -q 'true' <<<"$machines"; then
    echo "==> Starting the Podman machine ..."
    podman machine start || true
  fi
fi

# 2. Clean slate if asked.
if [[ "$CLEAN" == "1" ]]; then
  echo "==> Wiping previous state (config/ and .token) ..."
  COMPOSE down 2>/dev/null || true
  rm -rf config .token
fi

# 3-6. Everything else lives in setup.sh (up -> wait -> onboard -> token ->
#      install), then the best-effort entry.
./setup.sh ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}

if [[ "$ADD" == "1" ]]; then
  echo
  echo "==> Adding the config entry (best-effort) ..."
  ./add-integration.sh || {
    echo "Adding via API did not complete; add it in the UI instead:" >&2
    echo "  $HA_URL > Settings > Devices & Services > Add Integration" >&2
  }
fi

# Wait until the proxy is actually serving. Adding the entry with manage_http
# moves Home Assistant to loopback and restarts it, so the port goes quiet for
# a bit; "up and running" means it answers again through the proxy.
if [[ "$WAIT" == "1" ]]; then
  echo
  echo "==> Waiting for the move to land and the proxy to serve ..."

  moved() {
    python3 - <<'PY' 2>/dev/null || return 1
import json
d = json.load(open("config/.storage/http"))
s = (d.get("data") or {}).get("stable") or {}
raise SystemExit(0 if s.get("server_port") == 8124 else 1)
PY
  }

  served=0
  for i in $(seq 1 150); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HA_URL/manifest.json" 2>/dev/null || echo 000)"
    if moved && [[ "$code" != "000" ]]; then
      served=1
      echo "    move landed and proxy answering (HTTP $code) after ~$((i*2))s."
      break
    fi
    printf '.'
    sleep 2
  done
  echo
  if [[ "$served" != "1" ]]; then
    echo "!! The move did not complete within ~5 minutes." >&2
    echo "   Home Assistant may have reverted it (its own safety net), or the" >&2
    echo "   entry was not added. Check the log below; ./recover.sh both if locked out." >&2
  else
    echo "==> Up and running through the proxy: $HA_URL  (owner: admin / admin)"
  fi
fi

# Create a non-admin user and bind it to a custom "Energy only" role, for
# testing RBAC from the phone (#17 onboarding, plus app-level filtering). Runs
# whenever the entry was added, regardless of --no-wait, since RBAC is live once
# add-integration returned.
if [[ "$ADD" == "1" && "${CREATE_TEST_USER:-1}" == "1" ]]; then
  echo
  echo "==> Creating a non-admin test user (guest / guest) ..."
  ./mint-token.sh >/dev/null 2>&1 || true
  ./create-test-user.sh || echo "    (could not create the test user)"
  echo "==> Binding guest to a custom 'Energy only' role ..."
  ./create-custom-role.sh || echo "    (could not create/bind the custom role)"
fi

# Do not finish: hand off to a live, filtered log tail so the command stays
# attached until you Ctrl-C. Use --no-wait to return to the shell instead.
if [[ "$WAIT" == "1" ]]; then
  echo "==> Tailing logs. Ctrl-C to detach (the container keeps running)."
  exec ./logs.sh
fi

echo
echo "==> Done. Follow it settle with:  ./logs.sh"
