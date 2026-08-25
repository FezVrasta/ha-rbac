# How it works

The reference for anyone extending this, reviewing it, or wondering why a
particular request was refused. The [README](../README.md) is the short version.

## Design

The permission surface is **derived from Home Assistant's own runtime
registries**, not from a table that has to be updated every release:

| Source | Yields |
| --- | --- |
| `hass.data["websocket_api"]` | every websocket command and its voluptuous schema |
| the handler's `__wrapped__` chain | whether Home Assistant marks the command admin-only |
| `hass.http.app.router` and the view subclasses | the REST surface and its auth requirements |
| the panel registry | every dashboard, add-on and built-in screen |
| entity / device / area / label / floor registries | the resource graph |

If that derivation ever stops working, the catalog reports itself **degraded**
and every request is refused. The failure direction for an introspection-based
scheme is otherwise silent and open, which is the worst way for it to break.

## The gates

A request passes through these in order, and the first refusal wins.

**1. Pass-through.** The owner, Home Assistant's own system users, and any role
that restricts nothing skip every parse. This is the fast path that keeps the
proxy cheap for administrators. A role only counts as unrestricted if it really
is: an attribute rule, an app list or a tier denial all disqualify it.

**2. Tier gate.** Home Assistant marks its own administrative commands with
`require_admin`, and that marking is read at runtime. This covers most of the
command surface without naming any of it. REST routes are derived separately,
sorted by specificity rather than length so a catch-all cannot outrank a
specific path; an unmatched route is treated as administrative.

**3. App gate.** Hiding a dashboard, add-on or screen from the sidebar is
cosmetic on its own, since the address bar still works, so the ways into a
denied one are refused too. Three matches, in order of precision: the request
names the app outright (`lovelace/config` carries its own `url_path`), an
add-on is reached through a Supervisor endpoint naming its slug, or the
convention that an app's data comes from commands sharing its name.

That last one is a guess, and it has an exception. `config/` is not the
Settings panel's namespace, it is Home Assistant's namespace for every
registry, and the area, device, entity and floor lists behind it are what any
dashboard reads before it can draw. So for Settings the convention is narrowed
to requests that change something.

**4. Resource gate.** A recursive walk of the payload collects `entity_id`,
`device_id`, `area_id`, `label_id`, `floor_id` and `target`, expands them
through the registries, and requires every resulting entity to be permitted.
Path and query parameters are folded in for REST, because `?filter_entity_id=`
names its own target and a minimal history response would not give it back.

**5. Boundedness gate.** A payload that names nothing does not constrain its own
command. The rule that decides this is:

> **A payload does not bound its command if it carries a template.** A Jinja
> template's reach is unrelated to the resources the payload names, so any
> string containing `{{` or `{%` makes the command unbounded regardless of what
> else it declares.

This is what stops `render_template`, whose schema carries an optional
`entity_ids` that is a *rendering hint* rather than a constraint: without the
rule, extraction would check a decoy list and allow arbitrary server-side Jinja.

An earlier formulation of this rule was "an optional resource field does not
bound a command". That is wrong, and testing it against the real schemas is how
we found out: `call_service` declares `vol.Optional("target")`, so it would have
refused every control action in Home Assistant. Scanning for the template
markers is also strictly stronger, because it catches a template hidden in
`service_data` that no schema-shape rule can see, and because it correctly
*allows* `group/start_preview` and the other previews that really are bounded by
the entities they name. Only `template/start_preview` evaluates arbitrary Jinja.

A service call that names no entity is not unbounded either, it is bounded by
the service: `persistent_notification.create` and `homeassistant.restart` target
nothing, and refusing the lot left a role unable to raise a notification.

## Responses

Reads are allowed and their **responses filtered**. Leakage from a read is by
definition in the response, so a command returning nothing resource-shaped needs
no classification at all. Mutations are gated up front instead, since their
damage is not visible in what comes back.

The standing weakness of that design is that the generic walk recognises one
spelling of a thing and Home Assistant has several: `entity_id` and `ei`,
`attributes` and `a`. Each abbreviation is a potential leak and they cannot be
enumerated in advance, which is the argument for re-running an adversarial sweep
periodically rather than trusting the walk.

A response too large to filter is refused rather than streamed. A size limit is
not a correctness boundary, so it fails closed.

## What a role carries

- **Entities**: a baseline plus exceptions, targeted by area, domain, label,
  floor, entity or device. Home Assistant's own `compile_entities` does the
  evaluation, compiled twice per role because it cannot express denial:
  `allow_fn(x) and not deny_fn(x)`. Areas, labels and floors are desugared to
  entity ids at compile time, which also sidesteps a bug in Home Assistant's
  own area lookup that ignores an entity's directly assigned area.
- **Attributes**: names withheld from entities the role can otherwise see,
  targeted the same way. The compressed diffs carry attribute *names*, so
  stripping the same names from the initial state, the additions and the
  removal list is enough for the client's picture to stay consistent. Removals
  matter as much as values: `{"-": {"a": ["latitude"]}}` names an attribute
  without its value, which still discloses that it exists.
- **Apps**: which sidebar entries the role may open.
- **Commands**: ordinary use, or everything including settings.
- **Schedule**: a list of windows, each with its own days and times. The role is
  in force if any window is open. A window whose end precedes its start runs
  through midnight and belongs to the day it *opened*, so Friday 22:00 to 02:00
  is in force at one on Saturday morning. Outside every window the role is not
  held at all, and holding no role means no access rather than the access an
  unbound user would get; falling back would *raise* an administrator's access
  the moment their restricted role expired.

Because a schedule changes what applies without anything being edited,
permissions are cached on the user *and* on which of their roles are currently
in force, and a websocket re-resolves them per frame. A connection stays open
for hours, which is the same span a schedule covers.

## Add-on ingress

`HassIOIngress` sets `requires_auth = False`, so an add-on's own web page
arrives with no bearer token and the ordinary path can resolve no user for it.
Denying an add-on would hide its panel and refuse its Supervisor endpoints while
leaving the add-on itself reachable by anyone holding its ingress path, which is
stable for the life of the installation.

The only identity such a request carries is the `ingress_session` cookie, and
that session is minted through a Supervisor command the proxy does see, on a
connection it has already authenticated. So the proxy records who each session
was issued to, resolves the request's token back to an add-on, and applies the
role. A session it never issued belongs to nobody and is refused.

## Read this first: the security boundary

**RBAC applies only to traffic through the proxy. Home Assistant must not be
reachable from the network, or the proxy is decorative.**

Set the server host to `127.0.0.1` and the port to `8124` under **Settings >
System > Network**, and leave this integration on `8123`, which is its default.
In Docker, publish only `8123`.

> On recent Home Assistant versions the `http:` block in `configuration.yaml` is
> ignored: that configuration was migrated into Home Assistant's own store, and
> editing the YAML afterwards changes nothing and raises a repair issue saying
> so.

This matters because **an access token is not port-scoped**. A token minted
through the proxy works verbatim against Home Assistant's own listener. If the
server host is unset, reverted during an upgrade, or the port is published,
every user's existing token grants full unfiltered access and nothing warns you
at request time. The integration checks this at startup and logs loudly, but it
cannot enforce it.

Taking `8123` for the proxy rather than the other way round is deliberate: the
origin every browser, phone and cloud integration already uses does not change,
so no session is invalidated and nothing needs repointing.

### What this does and does not protect against

**Holds against anyone on the network.** A household member with a valid login
cannot read or control what their role forbids, from a browser, the app or the
API.

**Does not hold against anyone with host access.** Loopback is reachable with a
shell or code execution on the machine, and anyone with that can read
`.storage/auth`, which contains every refresh token and its signing key. They
can mint an access token for the owner and bypass Home Assistant's own
authentication entirely, not just this layer. That is a precondition which
already loses, so the proxy does not try to close it.

**Webhooks are outside it too.** `/api/webhook/{id}` carries no user, and the
id is an unguessable secret that Home Assistant treats as the credential for
that endpoint. The body may be encrypted end to end, so there is nothing for
this layer to read even where the owner is recorded, as `mobile_app` records a
`user_id`. They are forwarded for Home Assistant to authenticate as it always
has. Anyone holding a webhook id can act through it without a role applying,
which is the same standing as an automation.

This was learned the expensive way. Webhooks were refused at first, on the
grounds that a request naming no user cannot be judged. The first time the
proxy became the only way in on a real household it took every companion app
offline, because that is the transport `mobile_app` uses.

Three consequences worth stating plainly:

- A **non-admin with a legitimate shell account** on the host is not contained.
  The answer is to not give them a shell, or to run Home Assistant in a
  container they cannot enter.
- Under Supervisor, **add-ons reach Home Assistant over the Supervisor Unix
  socket** and auto-authenticate with no token at all. Any add-on with
  `homeassistant_api: true` is outside this boundary. The loopback setting
  itself is fine on Supervised: Supervisor reaches Home Assistant over that
  socket rather than the network port.

### Recovery if you lock yourself out

The owner account is always pass-through, in code, and cannot be restricted from
the UI. Failing that, tunnel to Home Assistant directly:

```bash
ssh -L 8124:127.0.0.1:8124 <your-ha-host>
# then browse http://localhost:8124 for unfiltered stock Home Assistant
```

Or delete `.storage/ha_rbac.roles` and restart to return to derived defaults.
All recovery paths require host access, so if you administer Home Assistant only
through a browser, set up the tunnel before you enable enforcement.

## Known limitations

- **Binary websocket frames are relayed unfiltered.** The handler id is
  per-connection and the payload is opaque. The commands that negotiate them are
  admin-gated today, which is not the same as being safe forever.
- **Automations execute with no user context**, unchanged from stock Home
  Assistant.
- **Timing and existence oracles.** A denied entity is distinguishable from one
  that does not exist.
- **Out-of-band capability URLs.** A signed path minted for the read-only
  content user erases the requesting user. Camera tokens are covered because the
  entity's whole state is filtered, but a URL obtained another way still works
  until it rotates, and rotation is Home Assistant's, not ours.
- **Filtering costs CPU, but less than expected.** Home Assistant builds each
  state-diff payload once and shares it with every client; filtering per user
  gives that up. Measured, a state-change diff costs about 5 microseconds to
  parse, filter and re-serialise, so fifty browser tabs at a hundred state
  changes a second come to roughly 2% of one core. A per-role cache was
  considered and dropped: Home Assistant gives each connection its own
  subscription id, so the frames are not byte-identical across connections and
  you have to parse before you could key a cache, which is most of the cost
  already. `tests/test_performance.py` pins the measurement.
