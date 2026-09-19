#!/usr/bin/env bash
# Create a custom (non-predefined) RBAC role that can open only the Energy
# dashboard, and bind the guest user to ONLY that role. Lets you test a
# genuinely restrictive custom role from the phone: the guest reaches
# /energy and every other panel is hidden/denied.
#
#   ./create-custom-role.sh
# Needs ./.token (owner/admin). Run ./mint-token.sh first if it is stale.

set -euo pipefail
cd "$(dirname "$0")"
source "./_engine.sh"

[[ -f ./.token ]] || { echo "No ./.token; run ./mint-token.sh first." >&2; exit 1; }
TOKEN="$(cat ./.token)"
GUEST_NAME="${TEST_NAME:-Guest}"

"$ENGINE" exec -i homeassistant env HA_TOKEN="$TOKEN" GUEST="$GUEST_NAME" python3 - <<'PY'
import asyncio, os, aiohttp

PORTS = (8123, 8124)


async def call(payloads):
    """Open a fresh authenticated connection and run payloads in order.

    A fresh connection per step is deliberate: deleting a role drops the
    live websocket ("Lost track of this connection"), so reusing it for the
    next command fails. One connection per logical step sidesteps that.
    Returns the list of result messages, or None if no port answered.
    """
    for port in PORTS:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{port}/api/websocket") as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        continue
                    results = []
                    for mid, payload in payloads:
                        await ws.send_json({"id": mid, **payload})
                        while True:
                            m = await ws.receive_json()
                            if m.get("id") == mid and m.get("type") == "result":
                                results.append(m)
                                break
                    return results
        except aiohttp.ClientError:
            continue
    return None


async def main():
    # Deny every app except energy and lovelace, computed from the live
    # catalogue. A deny-list is what the panel editor shows and edits; an
    # allow-list would render as "everything visible" and be wiped on save.
    #
    # lovelace (the default Overview) is kept reachable on purpose: the app
    # gate can hide a screen but cannot redirect the landing page, and Home
    # Assistant always lands a user on Overview first. Denying it left the
    # guest staring at "your role does not include that screen" right after
    # login, unable to get anywhere. Energy is still reachable, other panels
    # are hidden, and Overview is trimmed to the entities the role allows.
    keep = {"energy", "lovelace"}
    cat = await call([(10, {"type": "ha_rbac/catalog"})])
    if cat is None:
        print("could not reach the websocket API"); return 1
    apps = cat[0].get("result", {}).get("apps", [])
    if not apps:
        # The catalogue is briefly empty while HA settles after a restart.
        # Creating the role now would deny nothing and grant everything, so
        # refuse rather than write a wide-open role.
        print("catalogue is empty (HA still starting?); try again in a moment"); return 1
    deny = [a["url_path"] for a in apps if a.get("url_path") not in keep]

    role = {
        "name": "Energy only",
        "description": "Energy dashboard plus a filtered overview; other panels hidden.",
        "allow": {"entities": True},
        "deny": {},
        "tiers": {"max": "user", "allow": [], "deny": []},
        "apps": {"deny": deny},
    }

    # Recreate cleanly: drop an existing one so a stale form does not linger.
    listed = await call([(1, {"type": "ha_rbac/roles/list"})])
    existing = next(
        (r for r in listed[0].get("result", []) if r.get("name") == "Energy only"),
        None,
    )
    if existing:
        await call([(2, {"type": "ha_rbac/roles/delete", "role_id": existing["id"]})])
        print("removed existing Energy only role:", existing["id"])

    created = await call([(3, {"type": "ha_rbac/roles/create", "role": role})])
    if not created or not created[0].get("success"):
        print("create role failed:", created and created[0].get("error")); return 1
    rid = created[0]["result"]["id"]
    stored = created[0]["result"].get("apps", {}).get("deny", [])
    print(f"created custom role (deny-list, {len(stored)} apps denied):", rid)

    users = await call([(4, {"type": "ha_rbac/bindings/list"})])
    guest = next(
        (u for u in users[0].get("result", [])
         if os.environ["GUEST"] in (u.get("name") or "")),
        None,
    )
    if not guest:
        print("guest user not found; run ./create-test-user.sh first"); return 1

    bound = await call([(5, {
        "type": "ha_rbac/bindings/set",
        "user_id": guest["user_id"],
        "role_ids": [rid],
    })])
    if not bound or not bound[0].get("success"):
        print("bind failed:", bound and bound[0].get("error")); return 1
    print(f"bound guest ({guest['user_id']}) to only: {rid}")
    return 0


raise SystemExit(asyncio.run(main()))
PY
