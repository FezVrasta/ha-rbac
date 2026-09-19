#!/usr/bin/env bash
# Create a non-admin Home Assistant user bound to a restricted RBAC role, for
# reproducing the iOS Companion app onboarding issue (#17): onboarding a
# non-admin/custom-role user through the proxy. Runs the websocket commands
# inside the container so there are no host dependencies.
#
#   ./create-test-user.sh                 # guest / guest, bound to read_only
#   TEST_USER=guest TEST_PASS=guest TEST_ROLE=read_only ./create-test-user.sh
# Needs ./.token (an owner/admin token).

set -euo pipefail
cd "$(dirname "$0")"
source "./_engine.sh"

TEST_NAME="${TEST_NAME:-Guest}"
TEST_USER="${TEST_USER:-guest}"
TEST_PASS="${TEST_PASS:-guest}"
TEST_ROLE="${TEST_ROLE:-read_only}"
[[ -f ./.token ]] || { echo "No ./.token; run ./setup.sh first." >&2; exit 1; }
TOKEN="$(cat ./.token)"

"$ENGINE" exec -i homeassistant \
  env HA_TOKEN="$TOKEN" T_NAME="$TEST_NAME" T_USER="$TEST_USER" \
      T_PASS="$TEST_PASS" T_ROLE="$TEST_ROLE" python3 - <<'PY'
import asyncio, os, aiohttp

async def cmd(ws, mid, payload):
    await ws.send_json({"id": mid, **payload})
    while True:
        msg = await ws.receive_json()
        if msg.get("id") == mid and msg.get("type") == "result":
            return msg

async def main():
    token = os.environ["HA_TOKEN"]
    for port in (8123, 8124):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{port}/api/websocket") as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        continue

                    users = await cmd(ws, 1, {"type": "config/auth/list"})
                    existing = next(
                        (u for u in users.get("result", [])
                         if os.environ["T_NAME"] in (u.get("name") or "")),
                        None,
                    )
                    if existing:
                        uid = existing["id"]
                        print("user already exists:", uid)
                    else:
                        created = await cmd(ws, 2, {
                            "type": "config/auth/create",
                            "name": os.environ["T_NAME"],
                            "group_ids": ["system-users"],  # non-admin
                        })
                        if not created.get("success"):
                            print("create user failed:", created.get("error")); return 1
                        uid = created["result"]["user"]["id"]
                        print("created non-admin user:", uid)

                        cred = await cmd(ws, 3, {
                            "type": "config/auth_provider/homeassistant/create",
                            "user_id": uid,
                            "username": os.environ["T_USER"],
                            "password": os.environ["T_PASS"],
                        })
                        if not cred.get("success"):
                            print("create credential failed:", cred.get("error")); return 1
                        print("login credential set:", os.environ["T_USER"])

                    bound = await cmd(ws, 4, {
                        "type": "ha_rbac/bindings/set",
                        "user_id": uid,
                        "role_ids": [os.environ["T_ROLE"]],
                    })
                    if not bound.get("success"):
                        print("bind role failed:", bound.get("error")); return 1
                    print("bound to RBAC role:", os.environ["T_ROLE"])
                    return 0
        except aiohttp.ClientError:
            continue
    print("could not reach the websocket API")
    return 1

raise SystemExit(asyncio.run(main()))
PY
