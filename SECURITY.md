# Security policy

ha-rbac is access-control software: a filtering reverse proxy that decides what
each Home Assistant user may see and do. A bug here is a bug that lets someone
past a role. Reports are very welcome.

It is an **alpha**. It is tested and deliberately attacked, but it has not lived
in anyone else's house yet. Treat it accordingly: try it on something that isn't
your front door.

## Supported versions

Only the **latest release** is supported. Fixes land on `main` and go out in the
next release; older versions are not patched. Pin nothing — update to pick up a
fix.

## Reporting a vulnerability

**Please report privately first, not in a public issue** — a working bypass
posted in the open is a bypass handed to every install running it.

Use GitHub's private reporting:

- Go to the [Security tab](https://github.com/FezVrasta/ha-rbac/security) and
  press **Report a vulnerability**, or open this link directly:
  <https://github.com/FezVrasta/ha-rbac/security/advisories/new>.

A good report includes:

- the role's policy (the `allow` / `deny` / `tiers` / `apps` blocks — redact
  entity names if you like),
- the exact request the restricted user sent (WebSocket frame or HTTP call),
- what leaked or changed that the role forbids, and how you confirmed it
  (e.g. the entity's state read back through the owner account).

A minimal reproduction against the demo entities that ship with Home Assistant
is ideal, because it can be replayed without your house.

I'll acknowledge what I can reproduce, agree a disclosure timeline, and credit
you in the release notes unless you'd rather I didn't. There is no bounty — this
is a personal project.

## What is in scope

The boundary this software claims to hold, and where a bypass is a real finding:

- A household member with a **valid login** reading or controlling an entity,
  dashboard, add-on or setting their role forbids — from a browser, the mobile
  app, or the API, over the network.
- Reaching an **administrative command** above the role's tier.
- Any way to make the proxy **fail open** — serve unfiltered data or forward a
  denied action — through the protocol itself (message framing, id correlation,
  coalescing, pre-auth commands, spoofed `X-Forwarded-*`, oversized payloads).

## What is out of scope

These are **known, accepted limitations**, documented in
[docs/DESIGN.md](docs/DESIGN.md#what-this-does-and-does-not-protect-against).
Reports of them are not bugs, because the design does not claim to stop them:

- **Anyone with host access.** A shell or code execution on the machine can read
  `.storage/auth`, mint an owner token, and defeat Home Assistant's own
  authentication — not just this layer. Don't give restricted users a shell.
- **Webhooks** (`/api/webhook/{id}`). They carry no user; the id is the
  credential. Forwarded as-is, same standing as an automation.
- **Automations, scripts and scenes running on their own.** They execute as Home
  Assistant with no user context — unchanged from stock Home Assistant.
- **Supervisor add-ons with `homeassistant_api: true`.** They reach Home
  Assistant over the Supervisor socket and auto-authenticate with no token.
- **The owner account.** Always pass-through, by design, so you cannot lock
  yourself out. Restricting the owner is not a goal.
- **Attribute-level exactness on binary WebSocket frames**, and **timing /
  existence oracles** (a denied entity is distinguishable from a nonexistent
  one). Noted in the design; not currently closed.

If you're unsure whether something is in or out of scope, report it privately and
ask — an unclear boundary is itself worth fixing in the docs.

## Deployment note

Every guarantee rests on Home Assistant being reachable **only** through the
proxy: Home Assistant bound to loopback, the proxy on the public interface. A
bearer token is not port-scoped, so if Home Assistant's own port is exposed, a
valid token works against it directly and roles do not apply. The integration
warns at startup if it detects this. See
[the README](README.md#install) for the setup it performs for you.
