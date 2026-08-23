# How it works

The reference for anyone extending this, reviewing it, or wondering why a
particular request was refused. The [README](../README.md) is the short version.

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
   covers the large majority of the command surface without naming any of it.
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


---

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

