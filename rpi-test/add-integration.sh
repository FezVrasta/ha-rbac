#!/usr/bin/env bash
# BEST-EFFORT: drive the config flow over the API to add the RBAC entry.
#
# This is genuinely the fragile part. Completing the flow with manage_http=true
# makes Home Assistant move its own listener to loopback and RESTART. Driving a
# multi-step flow that also reboots mid-request is inherently brittle and
# version-sensitive -- if it does not work, just add it in the UI instead
# (Settings > Devices & Services > Add Integration > "RBAC Access Control").
#
# Needs a token in ./.token (setup.sh writes one).
#
# Env:
#   MANAGE_HTTP=true|false   (default true -- moves HA to loopback and restarts)
#   PROXY_PORT (default 8123), UPSTREAM_PORT (default 8124),
#   BIND (default 0.0.0.0), UPSTREAM_HOST (default 127.0.0.1)

set -euo pipefail
cd "$(dirname "$0")"

HA_URL="${HA_URL:-http://localhost:8123}"
[[ -f ./.token ]] || { echo "No ./.token; run ./setup.sh first." >&2; exit 1; }
TOKEN="$(cat ./.token)"
AUTH=(-H "Authorization: Bearer $TOKEN")

PROXY_PORT="${PROXY_PORT:-8123}"
UPSTREAM_PORT="${UPSTREAM_PORT:-8124}"
BIND="${BIND:-0.0.0.0}"
UPSTREAM_HOST="${UPSTREAM_HOST:-127.0.0.1}"
MANAGE_HTTP="${MANAGE_HTTP:-true}"

json_get() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"; }

echo "==> Waiting for the API to be ready (it may have just restarted) ..."
ready=0
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null --max-time 3 "${AUTH[@]}" "$HA_URL/api/config/config_entries/entry" 2>/dev/null; then
    ready=1; break
  fi
  sleep 2
done
[[ "$ready" == "1" ]] || { echo "API did not become ready; add it in the UI." >&2; exit 1; }

if printf '%s' "$(curl -fsS --max-time 10 "${AUTH[@]}" "$HA_URL/api/config/config_entries/entry" 2>/dev/null)" \
   | grep -q '"domain": *"ha_rbac"'; then
  echo "==> ha_rbac is already configured; nothing to add."
  exit 0
fi

echo "==> Starting the config flow ..."
START="$(curl -fsS --max-time 15 "${AUTH[@]}" -X POST "$HA_URL/api/config/config_entries/flow" \
  -H 'Content-Type: application/json' \
  -d '{"handler":"ha_rbac","show_advanced_options":false}')"

if printf '%s' "$START" | grep -q '"already_configured"'; then
  echo "==> ha_rbac is already configured; nothing to add."
  exit 0
fi

FLOW_ID="$(printf '%s' "$START" | json_get flow_id)"
STEP="$(printf '%s' "$START" | json_get step_id)"
if [[ -z "$FLOW_ID" ]]; then
  echo "Could not start the flow. Response was:" >&2
  echo "$START" >&2
  echo "Add it in the UI instead." >&2
  exit 1
fi
echo "    flow_id=$FLOW_ID step=$STEP"

if [[ "$STEP" == "user" ]]; then
  echo "==> Submitting ports (proxy=$PROXY_PORT upstream=$UPSTREAM_PORT) ..."
  NEXT="$(curl -fsS --max-time 15 "${AUTH[@]}" -X POST \
    "$HA_URL/api/config/config_entries/flow/$FLOW_ID" \
    -H 'Content-Type: application/json' \
    -d "{\"proxy_port\":$PROXY_PORT,\"bind_address\":\"$BIND\",\"upstream_host\":\"$UPSTREAM_HOST\",\"upstream_port\":$UPSTREAM_PORT}")"
  STEP="$(printf '%s' "$NEXT" | json_get step_id)"
  TYPE="$(printf '%s' "$NEXT" | json_get type)"
  echo "    -> step=$STEP type=$TYPE"
  echo "$NEXT" | grep -q '"port_conflict"' && {
    echo "Port conflict: proxy_port and upstream_port must differ." >&2; exit 1; }
fi

if [[ "$STEP" == "move" ]]; then
  echo "==> Completing the move step (manage_http=$MANAGE_HTTP) ..."
  echo "    If manage_http=true, Home Assistant moves to loopback and RESTARTS,"
  echo "    so this request may not get a clean reply -- that is expected."
  # Bounded on purpose: creating the entry triggers a Home Assistant restart,
  # and the connection this request is riding on can drop as the process goes
  # down. A timeout or empty body here is the move underway, not a failure --
  # bootstrap's wait loop is what actually confirms the proxy comes back.
  DONE="$(curl -sS --max-time 20 "${AUTH[@]}" -X POST \
    "$HA_URL/api/config/config_entries/flow/$FLOW_ID" \
    -H 'Content-Type: application/json' \
    -d "{\"manage_http\":$MANAGE_HTTP}" 2>/dev/null || true)"
  if [[ -n "$DONE" ]]; then
    echo "    $DONE"
  else
    echo "    (no reply -- Home Assistant is most likely restarting into the move)"
  fi
fi

cat <<EOF

==> Flow submitted.
If manage_http was true, Home Assistant is moving to 127.0.0.1:$UPSTREAM_PORT and
restarting; the proxy will answer on $PROXY_PORT. Watch it settle:

  ./logs.sh

If anything above looks wrong, add the integration in the UI instead -- that is
the reliable path. Recover with ./recover.sh both if you get locked out.
EOF
