#!/usr/bin/env bash
# Log in through the auth flow and write a fresh access token to ./.token.
# Works whether or not onboarding just happened, and survives the restarts a
# move triggers (an old ./.token goes stale after a restart).
#
#   ./mint-token.sh            # admin / admin
#   HA_USER=admin HA_PASS=admin ./mint-token.sh

set -euo pipefail
cd "$(dirname "$0")"

HA_URL="${HA_URL:-http://localhost:8123}"
HA_USER="${HA_USER:-admin}"
HA_PASS="${HA_PASS:-admin}"
CID="$HA_URL/"

FID="$(curl -fsS -X POST "$HA_URL/auth/login_flow" \
  -H 'Content-Type: application/json' \
  -d "{\"client_id\":\"$CID\",\"handler\":[\"homeassistant\",null],\"redirect_uri\":\"$CID\"}" \
  | sed -n 's/.*"flow_id": *"\([^"]*\)".*/\1/p')"
[[ -n "$FID" ]] || { echo "Could not start the login flow." >&2; exit 1; }

CODE="$(curl -fsS -X POST "$HA_URL/auth/login_flow/$FID" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$HA_USER\",\"password\":\"$HA_PASS\",\"client_id\":\"$CID\"}" \
  | sed -n 's/.*"result": *"\([^"]*\)".*/\1/p')"
[[ -n "$CODE" ]] || { echo "Login failed (wrong credentials?)." >&2; exit 1; }

TOKEN="$(curl -fsS -X POST "$HA_URL/auth/token" \
  -d "client_id=$CID" -d "grant_type=authorization_code" -d "code=$CODE" \
  | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')"
[[ -n "$TOKEN" ]] || { echo "Token exchange failed." >&2; exit 1; }

printf '%s' "$TOKEN" > ./.token
echo "Token written to ./.token"
