# RBAC Access Control for Home Assistant

Role-based access control for Home Assistant, enforced by a filtering reverse proxy
that runs inside HA as a custom integration.

Home Assistant ships two coarse access mechanisms: a boolean `is_admin`, and an
entity-only policy engine that is checked in roughly fifteen places, cannot express
denial, and has no API to create or edit the groups it attaches to. This integration
adds real roles — read/control/edit over entities, devices, areas, labels and floors —
without patching core.

---

## Read this first: the security boundary

**RBAC applies only to traffic through the proxy. Home Assistant must not be reachable
from the network, or the proxy is decorative.**

```yaml
# configuration.yaml
http:
  server_host: 127.0.0.1   # HA listens on loopback only; port stays 8123
```

The proxy then binds `:8124` on the public interface and forwards to `127.0.0.1:8123`.
Point browsers at `:8124`. In Docker, publish only 8124.

This matters because **an access token is not port-scoped**. A token minted through the
proxy works verbatim against Home Assistant's own listener. If `server_host` is unset,
reverted during an upgrade, or the port is published, every user's existing token grants
full unfiltered access and nothing warns you at request time. The integration checks this
at startup and logs loudly, but it cannot enforce it.

### What this does and does not protect against

**Holds against anyone on the network.** A household member with a valid non-admin login
cannot read or control what their role forbids.

**Does not hold against anyone with host access.** Loopback is reachable with a shell or
code execution on the HA machine, and anyone with that can read `.storage/auth`, which
contains every refresh token and its signing key. They can mint an access token for the
owner and bypass Home Assistant's own authentication entirely — not just this layer. That
is a precondition which already loses, so the proxy does not try to close it.

Two consequences worth stating plainly:

- A **non-admin with a legitimate shell account** on the HA host is not contained. The
  answer is to not give them a shell, or to run HA in a container they cannot enter.
- Under Supervisor, **add-ons reach HA over the Supervisor Unix socket** and
  auto-authenticate with no token at all. Any add-on with `homeassistant_api: true` is
  outside this boundary.

### Recovery if you lock yourself out

The owner account is always pass-through, in code, and cannot be restricted from the UI.
Failing that, tunnel to Home Assistant directly:

```bash
ssh -L 8123:127.0.0.1:8123 <your-ha-host>
# then browse http://localhost:8123 for unfiltered stock HA
```

Or delete `.storage/ha_rbac` and restart to return to derived defaults. All recovery paths
require host access — if you administer HA only through a browser, set up the tunnel
before you enable enforcement.

---

## Design

The permission surface is **derived from Home Assistant's own runtime registries**, not
from a table that has to be updated every release:

| Source | Yields |
| --- | --- |
| `hass.data["websocket_api"]` | every WS command and its voluptuous schema |
| the handler's `__wrapped__` chain | whether HA itself marks the command admin-only |
| entity / device / area / label / floor registries | the resource graph |

Decisions follow four gates:

1. **Pass-through** — owner, system users, and full-access roles skip all parsing.
2. **Tier gate** — HA's own `require_admin` decorator, introspected at runtime. This alone
   covers 307 of 478 commands without naming any of them.
3. **Resource gate** — a recursive walk of the payload collecting `entity_id`, `device_id`,
   `area_id`, `label_id`, `floor_id` and `target`, expanded through the registries.
4. **Boundedness gate** — *an optional resource field does not bound a command*. This is
   what stops `render_template`, whose schema carries an optional `entity_ids` that is a
   rendering hint rather than a constraint; without this rule, extraction would check a
   decoy list and allow arbitrary server-side Jinja. The same rule covers the eight
   `*/start_preview` commands, none of which are named in the code.

Reads are allowed and their **responses filtered** — leakage from a read is by definition
in the response, so a command returning nothing resource-shaped needs no classification.
Mutations are gated up front, since their damage is not visible in the response.

---

## What it looks like

All of these are one Home Assistant, one dashboard, seen through the proxy by
two different accounts. The guest is bound to a role that denies the `lock`
domain; the owner is unrestricted.

### The guest

![Guest dashboard](screenshots/guest-dashboard.jpg)

An ordinary working dashboard — areas, summaries, a rendered heading. Nothing
announces that anything is being withheld, which is the point. Note the sidebar:
no Settings, no Developer Tools, and no Access Control.

![Guest searching for locks](screenshots/guest-search-no-locks.jpg)

Searching entities for `lock` returns nothing. The lock is absent from
`get_states`, from `/api/states`, and from the compressed state stream the
dashboard subscribes to, so there is nothing for the frontend to find. Asking
for it directly — `GET /api/states/lock.front_door` — returns 401.

### The owner, same proxy

![Owner sidebar](screenshots/owner-sidebar.jpg)

Settings and Access Control appear. The proxy does no parsing or filtering at
all for a fully permitted role, so an administrator pays nothing for this being
installed.

### The Access Control panel

![Roles](screenshots/panel-roles.jpg)

Roles are edited here and stored in `.storage/ha_rbac`, never in YAML. The three
built-in roles cannot be edited, only cloned. Entity rules are written as a Home
Assistant policy, so a role's `allow` block is portable back into a group policy
if you ever remove this.

The line under **Commands** — *"393 commands derived"* — is the point of the
whole design. That count comes from reading Home Assistant's own `require_admin`
decorators at runtime, on this instance, with this set of integrations
installed. It is not a list shipped in the code, and it does not need updating
when Home Assistant adds a command.

![Users](screenshots/panel-users.jpg)

A user with no role keeps whatever their Home Assistant group already gives
them, so installing this changes nothing until you assign one. The owner is
always unrestricted, in code — that is the way back in if a role locks you out.

![Denials](screenshots/panel-denials.jpg)

Every refusal, with the reason and the entities involved. A denied request
reaches the user as a screen that quietly does less, with no explanation, so
this is where to look when someone reports one. The reasons map to the gates:
`tier` is a command the role may not use at all, `resource` is an entity it may
not touch, `unbounded` is a request that named nothing checkable.

## Installation

Install via HACS or copy `custom_components/ha_rbac` into your `config/custom_components`.
Restart, set `http.server_host` as above, then add the integration from
**Settings → Devices & Services**.

Nothing is enforced until you bind users to roles. Unbound users fall back to a role
derived from their existing HA group, so installing changes no behaviour on day one.

## Roles

Three predefined roles ship as `system_generated` — not editable, but cloneable:

| Role | Entities | Tier |
| --- | --- | --- |
| `admin` | everything | admin, full pass-through |
| `user` | read + control on all | open |
| `read_only` | read on all | open |

Custom roles are authored in the **Access Control** panel and stored in
`.storage/ha_rbac` — never in YAML. A role's `allow` block is a valid Home Assistant
`PolicyType`, so it can be lifted into a group policy if you ever migrate off the proxy.

## Known limitations

- **Entity-level filtering only.** If a role can read an entity, it reads all attributes.
- **Binary WebSocket frames are relayed unfiltered.** The handler id is per-connection and
  the payload is opaque. The commands that negotiate them are admin-gated today.
- **Automations execute with no user context**, unchanged from stock HA.
- **Filtering costs CPU, but less than expected.** Home Assistant builds each
  state-diff payload once and shares it with every client; filtering per user
  gives that up. Measured, a state-change diff costs about 5 microseconds to
  parse, filter and re-serialise, so fifty browser tabs at a hundred state
  changes a second come to roughly 2% of one core. A per-role cache was
  considered and dropped: Home Assistant gives each connection its own
  subscription id, so the frames are not byte-identical across connections and
  you have to parse before you could key a cache -- which is most of the cost
  already. `tests/test_performance.py` pins the measurement.
- **Moving users from `:8123` to `:8124` invalidates their sessions** — `client_id` is the
  origin, so everyone logs in once after the switch.

## Licence

MIT
