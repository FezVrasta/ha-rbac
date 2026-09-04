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

## Why this is not a core patch

Home Assistant's architecture discussion
[#1374](https://github.com/home-assistant/architecture/discussions/1374) is a
worked RBAC proposal for core -- deny-overrides-allow, label-based entity
permissions, custom role CRUD, per-service and per-automation categories --
with a branch behind it. It was declined, and so was a follow-up asking for
about eighty lines: `AuthManager` group CRUD, and widening the policy schema to
accept `False`.

The reason given was not disagreement about the feature. It was that
authentication and authorization need "significant care and oversight from the
Foundation, well beyond just code review" -- external audits, ongoing security
monitoring, long-term maintenance -- and those resources are not allocated.

Two things follow for this project. Waiting for either change would be waiting
indefinitely, which is why deny is expressed by compiling two policies rather
than by asking the schema to accept `False`, and why roles live in this
integration's own store rather than in `.storage/auth`. And the same reasoning
applies here with the same force: this layer has had two adversarial passes by
its author and no external audit, which is what the alpha warning is about.

The proposal is worth reading anyway. It is an independent design of the same
thing, and the places it reaches that this does not -- per-automation `read`,
`edit` and `trigger` in particular -- are a fair list of what is still missing.

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

One exception to that default: a path Home Assistant serves straight off disk.
Static paths are not views, so they match no route and used to resolve to
administrative -- which refused a signed-in restricted user a file Home
Assistant hands to anyone at all, the proxy included, since the same request
without a token is forwarded and answered. `/local`, the frontend bundles and
any integration's own static path are read back off the running router and
count as open. Views are consulted first, so a static path cannot lower the
tier of an endpoint underneath it.

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

Those keys are not the only place an entity is named. `scene.apply` takes
`{"entities": {"lock.front": "unlocked"}}`, where the entity is a mapping *key*,
and a media source names one in the tail of a URI,
`media-source://camera/camera.bedroom`. Neither can be recognised by shape
alone — the tail of `media-source://media_source/local/song.mp3` looks exactly
the same — so a second walk tests every string and every key against the
registry and folds in whatever really is an entity. It adds to the check without
counting as a bound, so a payload that names nothing a schema recognises stays
unbounded and is judged as before.

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

Position varies as well as spelling. Cameras are a media source, so
`media_source/browse_media` listed every one of them — friendly name and
`/api/camera_proxy/` thumbnail — after the same cameras had been hidden from the
sidebar, the states and the registry listings, because the entity sits in the
tail of a `media-source://` URI rather than under any key. That has its own
filter now, and the request side refuses to resolve one, which matters more:
`media_source/resolve_media` returns a signed stream URL that authenticates on
its own, so nothing downstream could have recovered what the request gave away.

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
- **Apps**: which sidebar entries the role may open. Dashboards carry a level as
  well: a role can open one empty, read what is on it, or control it. That is
  resolved against the dashboard when a request is judged rather than recorded
  when the role is saved, so a dashboard being edited changes who can see what
  without anyone reopening the role. The entity list per dashboard is cached and
  refreshed on Home Assistant's own `lovelace_updated` event, because it is
  consulted on the hottest path there is. A denial is checked first, so putting
  a forbidden entity on a granted dashboard does not unlock it, which would
  otherwise hand a grant to anyone who can edit a dashboard.
- **Commands**: ordinary use, or everything including settings, plus any
  *capabilities* — named parts of the administrative surface, so that granting
  automations or dashboards does not mean writing a glob. A role stores the
  names rather than the patterns they expand to, which is what lets the editor
  show what was chosen and lets a role follow the grouping when Home Assistant
  moves a command under it. An unknown name grants nothing and is kept, since a
  role written by a newer build should lose that one grant rather than all of
  them. What the grouping cannot make safe is that automations, scripts and
  scenes run with no user context: whoever can write one can make it do
  anything, so those capabilities record trust rather than confine it.
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

## Multiple roles

A user can hold more than one role at once. Each of the user's active roles
(active meaning bound and currently in force by its own schedule) is compiled
and checked on its own; nothing about the roles themselves is merged. A check
then asks each role in turn and grants access if **any** role says yes --
`check_entity` is a plain `any(role.check(entity_id, key) for role in roles)`,
and the same shape covers tiers and apps. So two roles with conflicting
exceptions on the same entity resolve to the more permissive of the two: a
role that hides an entity does not override another role that grants it, even
though *within* a single role deny still beats allow.

Roles are meant to be composed rather than layered: build the narrowest role
that covers the common case, then add a second role for the extra access
someone occasionally needs, rather than relying on one role's deny to trim
another's allow.

The one grant nothing can widen is the per-user global deny. It is checked
before any role is consulted and vetoes a request outright, regardless of
what any role would otherwise allow.

## Recording what a role needs

Writing a role blind is the hard way round: restrict something, hand it over,
and find out days later that a dashboard is empty. So a role can be put into
recording. While it runs, its holders skip every gate and each request is noted
instead of judged -- the same extraction the resource gate performs, used to
write down rather than to refuse. Responses are not filtered either, since a
recording that trimmed them would be recording a smaller Home Assistant than
the one the person is actually using.

The check sits *above* the gates on purpose. A recording that only saw what the
role already allows would tell nobody anything; the point is to learn what it
was missing, and a refused request never says what it wanted.

What comes out is only ever additive. A recording says what was needed, never
what was not: an entity nobody happened to open while it ran has not been shown
to be unnecessary, so nothing it produces can take access away.

Two consequences of it being a temporary grant of full access. It is loud --
the panel carries a warning while it runs and the log says so at the start. And
it lives in memory only, so a restart or an unload ends every recording and the
role goes back to its own rules. That loses the notes, which is the cheaper of
the two failures: persisting the flag would mean a crash mid-recording left a
role unrestricted with nobody watching.

The app match is shared with the app gate rather than reimplemented, because
the recording needs the same answer for the opposite purpose. Two copies that
had to agree would be the defect, not the duplication.

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

The setup flow does this, and `http_config.py` is where. Home Assistant cannot
change its HTTP config while running -- a config is *staged*, applied on the
next start, and reverted automatically unless something promotes it within five
minutes -- so the move is one restart rather than a live reconfiguration.

That trial is what makes automating it safe. The promotion is deferred until
the proxy has served a real request through the new arrangement, so a proxy
that binds its port but cannot forward leaves the revert armed and Home
Assistant comes back where it was. Binding is not evidence of serving, which is
why the check is a request and not a socket.

By hand it is three steps and the order is not guessable: move the port first,
install this, then set the server host to `127.0.0.1`, all under **Settings >
System > Network**. Each step leaves a reachable instance, and taking the port
before Home Assistant has vacated it is a bind error rather than a working
setup.

`127.0.0.1` is doing the work here, not any firewall. It is the only address
Home Assistant will accept a connection on, so after that setting it is not
listening on the network at all and `8124` cannot be opened from off the
machine. This layer is in-process and reaches it over the same loopback address,
so it never needs the port exposed. That holds identically on Home Assistant OS,
Supervised, Container and Core. Not publishing `8124` in Docker is worth doing
as well, but it is not what makes this safe, and publishing it would not undo
the setting either.

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
If the integration itself fails to load, nothing answers on either port, and the
setting has to be undone instead: remove `server_host` from the `stable` block
of `.storage/http` and restart.

Removing or disabling the integration does that undoing for you. Stopping the
proxy frees the address but leaves Home Assistant on loopback, so on its own it
would take the instance off the network entirely and getting back in would need
a shell. So the move is reversed and Home Assistant restarts onto the address it
answered on before, using the snapshot taken when it was moved. The restore is
promoted straight to stable rather than staged: a staged config is a trial that
reverts unless something confirms it, and the thing that would confirm it is the
integration being removed. An installation moved by a build older than this has
no snapshot, so the port the proxy is on is used instead, which is the address
Home Assistant was on by definition. Turn it off per entry with **Put Home
Assistant back on its original address** if you would rather it stayed put.

Every one of those needs access to the machine, so confirm you have some before
enabling enforcement rather than after. On Home Assistant OS and Supervised that
is not the Terminal & SSH add-on by default: add-ons are bridge-networked, so
`127.0.0.1` inside one is the add-on and the tunnel reaches nothing, and the web
terminal is served through the Home Assistant that is down. The add-on can still
edit `.storage/http`, which it mounts read-write, if it has a real SSH port
rather than ingress alone.

## Known limitations

- **Binary websocket frames are relayed unfiltered.** The handler id is
  per-connection and the payload is opaque. The commands that negotiate them are
  admin-gated today, which is not the same as being safe forever.
- **Automations execute with no user context**, unchanged from stock Home
  Assistant.
- **Assist is granted or withheld, not filtered.** A sentence names no entity
  until Home Assistant resolves it, and by the time the result comes back the
  action has happened, so the whole intent surface is refused for any role that
  restricts anything. A role allowed to control its own lights still cannot ask
  Assist to turn them on. Both spellings are refused, the websocket command
  `conversation/process` and the service `conversation.process`: a glob written
  for one does not match the other, and denying only the command left the
  service open until v0.15.1. [ASSIST.md](ASSIST.md) is what enforcing it rather
  than refusing it would take.
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
