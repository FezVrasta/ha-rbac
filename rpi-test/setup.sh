#!/usr/bin/env bash
# One-shot: bring up Home Assistant, wait for it, create the owner account, get
# an access token, and install the integration. After this, Home Assistant is
# running with an owner and the integration on disk -- you just add it in the UI
# (or via ./add-integration.sh, best-effort).
#
# Env (all optional):
#   HA_URL        base url                (default http://localhost:8123)
#   HA_USER       owner username          (default admin)
#   HA_PASS       owner password          (default admin)
#   HA_NAME       owner display name      (default Admin)
#   REPO / BRANCH passed to install-integration.sh
#   SKIP_INSTALL=1  bring up + onboard only, do not install the integration
#
# Writes the token to ./.token so other scripts can reuse it.

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_engine.sh
source "./_engine.sh"

HA_URL="${HA_URL:-http://localhost:8123}"
HA_USER="${HA_USER:-admin}"
HA_PASS="${HA_PASS:-admin}"
HA_NAME="${HA_NAME:-Admin}"
HA_COUNTRY="${HA_COUNTRY:-IT}"
CLIENT_ID="$HA_URL/"

echo "==> Starting Home Assistant ($ENGINE) ..."
COMPOSE up -d

echo "==> Waiting for the API to answer at $HA_URL ..."
for i in $(seq 1 120); do
  if curl -fsS -o /dev/null "$HA_URL/manifest.json" 2>/dev/null \
     || curl -fsS -o /dev/null "$HA_URL/api/onboarding" 2>/dev/null; then
    break
  fi
  sleep 2
  [[ "$i" == 120 ]] && { echo "Home Assistant did not come up in time." >&2; exit 1; }
done

step_done() {
  curl -fsS "$HA_URL/api/onboarding" 2>/dev/null \
    | grep -Eq "\"step\" *: *\"$1\" *, *\"done\" *: *true"
}

TOKEN=""
if step_done user; then
  echo "==> Owner already exists; skipping account creation."
else
  echo "==> Creating owner account '$HA_USER' ..."
  AUTH_CODE="$(curl -fsS -X POST "$HA_URL/api/onboarding/users" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$HA_NAME\",\"username\":\"$HA_USER\",\"password\":\"$HA_PASS\",\"client_id\":\"$CLIENT_ID\",\"language\":\"en\"}" \
    | sed -n 's/.*"auth_code": *"\([^"]*\)".*/\1/p')"

  if [[ -z "$AUTH_CODE" ]]; then
    echo "Could not create the owner (no auth_code returned)." >&2
    exit 1
  fi

  echo "==> Exchanging the auth code for a token ..."
  TOKEN="$(curl -fsS -X POST "$HA_URL/auth/token" \
    -d "client_id=$CLIENT_ID" \
    -d "grant_type=authorization_code" \
    -d "code=$AUTH_CODE" \
    | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')"

  if [[ -z "$TOKEN" ]]; then
    echo "Could not obtain an access token from the auth code." >&2
    exit 1
  fi

  # The frontend hangs on a half-finished onboarding, so complete every step
  # while this freshly minted token is still valid. integration needs the
  # client_id/redirect_uri; the others just need the bearer.
  echo "==> Finishing onboarding (core_config, analytics, integration) ..."
  auth=(-H "Authorization: Bearer $TOKEN")
  for step in core_config analytics; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
      "${auth[@]}" -X POST "$HA_URL/api/onboarding/$step" || echo 000)"
    echo "    $step -> HTTP $code"
  done
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${auth[@]}" \
    -H 'Content-Type: application/json' -X POST "$HA_URL/api/onboarding/integration" \
    -d "{\"client_id\":\"$CLIENT_ID\",\"redirect_uri\":\"$CLIENT_ID\"}" || echo 000)"
  echo "    integration -> HTTP $code"

  if step_done integration; then
    echo "==> Onboarding complete."
  else
    echo "!! Onboarding did not fully complete; finish it in the browser at $HA_URL" >&2
  fi
fi

# A token is only mintable via onboarding while onboarding is open. For an
# already-onboarded instance, log in through the auth flow instead. Either way
# an old ./.token from a previous run is likely stale, so mint a fresh one.
if [[ -z "$TOKEN" ]]; then
  echo "==> Logging in to mint an access token ..."
  FLOW="$(curl -fsS -X POST "$HA_URL/auth/login_flow" \
    -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"$CLIENT_ID\",\"handler\":[\"homeassistant\",null],\"redirect_uri\":\"$CLIENT_ID\"}" 2>/dev/null || true)"
  FLOW_ID="$(printf '%s' "$FLOW" | sed -n 's/.*"flow_id": *"\([^"]*\)".*/\1/p')"
  if [[ -n "$FLOW_ID" ]]; then
    AUTH_CODE="$(curl -fsS -X POST "$HA_URL/auth/login_flow/$FLOW_ID" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"$HA_USER\",\"password\":\"$HA_PASS\",\"client_id\":\"$CLIENT_ID\"}" 2>/dev/null \
      | sed -n 's/.*"result": *"\([^"]*\)".*/\1/p')"
    if [[ -n "$AUTH_CODE" ]]; then
      TOKEN="$(curl -fsS -X POST "$HA_URL/auth/token" \
        -d "client_id=$CLIENT_ID" -d "grant_type=authorization_code" -d "code=$AUTH_CODE" \
        | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')"
    fi
  fi
fi

if [[ -n "$TOKEN" ]]; then
  printf '%s' "$TOKEN" > ./.token
  echo "==> Access token saved to ./.token"
else
  echo "!! No token available; ./add-integration.sh will not work until you log in." >&2
fi

if [[ -n "$TOKEN" ]]; then
  echo "==> Setting country ($HA_COUNTRY) to clear the repair notice ..."
  ./set-country.sh "$HA_COUNTRY" || echo "    (could not set country; harmless)"
fi

if [[ "${SKIP_INSTALL:-}" == "1" ]]; then
  echo "==> SKIP_INSTALL set; not installing the integration."
else
  echo "==> Installing the integration ..."
  NO_TAIL=1 ./install-integration.sh "$@"
fi

cat <<EOF

==> Done.
    Home Assistant: $HA_URL   (owner: $HA_USER / $HA_PASS)
    Token:          ./.token

Next: add the integration in the UI
  Settings > Devices & Services > Add Integration > "RBAC Access Control"
  enable "manage HTTP", accept defaults (proxy 8123, HA -> 127.0.0.1:8124).

Or try the best-effort API path:
  ./add-integration.sh
EOF
