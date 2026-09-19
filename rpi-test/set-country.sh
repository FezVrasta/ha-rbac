#!/usr/bin/env bash
# Set Home Assistant's country, clearing the "country has not been configured"
# repair notice. This is a websocket-only command (config/core/update), so it
# runs inside the container using the container's own Python/aiohttp -- no host
# dependencies. Run after onboarding, while HA still answers on 8123.
#
#   ./set-country.sh [COUNTRY]     (default $HA_COUNTRY or IT)
# Needs ./.token.

set -euo pipefail
cd "$(dirname "$0")"
source "./_engine.sh"

COUNTRY="${1:-${HA_COUNTRY:-IT}}"
[[ -f ./.token ]] || { echo "No ./.token; run ./setup.sh first." >&2; exit 1; }
TOKEN="$(cat ./.token)"

"$ENGINE" exec -i homeassistant \
  env HA_TOKEN="$TOKEN" HA_COUNTRY="$COUNTRY" python3 - <<'PY'
import asyncio, json, os, aiohttp

async def main():
    token = os.environ["HA_TOKEN"]
    country = os.environ["HA_COUNTRY"]
    for port in (8123, 8124):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{port}/api/websocket") as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        continue
                    await ws.send_json(
                        {"id": 1, "type": "config/core/update", "country": country}
                    )
                    reply = await ws.receive_json()
                    print("country set:", reply.get("success"), "->", country)
                    return 0 if reply.get("success") else 1
        except aiohttp.ClientError:
            continue
    print("could not reach the websocket API")
    return 1

raise SystemExit(asyncio.run(main()))
PY
