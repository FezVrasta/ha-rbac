#!/usr/bin/env bash
# Recovery lever for when testing the proxy locks you out. Run from this folder.
#
# The integration moves Home Assistant to 127.0.0.1:8124 and puts the proxy on
# 8123. If the proxy misbehaves, these put Home Assistant back on the network
# WITHOUT going through the proxy.
#
#   ./recover.sh disable   # remove the integration folder, then restart
#   ./recover.sh unmove    # drop server_host from the stored HTTP config, then restart
#   ./recover.sh both      # do both

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_engine.sh
source "./_engine.sh"

CONFIG_DIR="./config"
HTTP_STORE="$CONFIG_DIR/.storage/http"

do_disable() {
  echo "Removing custom_components/ha_rbac ..."
  rm -rf "$CONFIG_DIR/custom_components/ha_rbac"
}

do_unmove() {
  if [[ ! -f "$HTTP_STORE" ]]; then
    echo "No $HTTP_STORE; nothing to unmove."
    return
  fi
  echo "Backing up $HTTP_STORE -> $HTTP_STORE.bak"
  cp "$HTTP_STORE" "$HTTP_STORE.bak"
  # Drop server_host from the stable block so Home Assistant answers on every
  # interface again. Uses python3 for a safe JSON edit rather than sed.
  python3 - "$HTTP_STORE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
stable = (data.get("data") or {}).get("stable")
if isinstance(stable, dict):
    stable.pop("server_host", None)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("Removed server_host from the stable HTTP config.")
PY
}

case "${1:-}" in
  disable) do_disable ;;
  unmove)  do_unmove ;;
  both)    do_disable; do_unmove ;;
  *) echo "usage: $0 {disable|unmove|both}"; exit 1 ;;
esac

echo "Restarting Home Assistant ($ENGINE) ..."
COMPOSE restart homeassistant
echo "Done. Home Assistant should be reachable directly again shortly."
