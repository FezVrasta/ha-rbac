#!/usr/bin/env bash
# Tail Home Assistant logs, filtered to this integration and the HTTP/setup
# lines that matter when testing the proxy move and reload.
#
#   ./logs.sh            # follow everything from ha_rbac + setup/bootstrap
#   ./logs.sh raw        # follow the full, unfiltered container log

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_engine.sh
source "./_engine.sh"

if [[ "${1:-}" == "raw" ]]; then
  COMPOSE logs -f homeassistant
  exit 0
fi

# grep -E for the lines that tell the reload/move story:
#  - "Moving Home Assistant"  -> a move was staged (a restart is coming)
#  - "RBAC proxy listening"    -> the proxy bound its port
#  - "Confirmed .* move"        -> the move was promoted (interlock passed)
#  - "revert"                   -> the safety net kicked in
COMPOSE logs -f homeassistant 2>&1 \
  | grep --line-buffered -E "ha_rbac|RBAC proxy|Moving Home Assistant|Confirmed|revert|Setting up ha_rbac|ConfigEntry"
