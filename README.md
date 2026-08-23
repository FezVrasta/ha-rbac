<h1 align="center">Access Control for Home Assistant</h1>

<p align="center">
  <strong>Give everyone in the house their own Home Assistant.</strong><br>
  Guests see the lights. Kids don't see the locks. You see everything.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="alpha">
  <img src="https://img.shields.io/badge/Home%20Assistant-2025.1%2B-41BDF5" alt="Home Assistant 2025.1+">
  <img src="https://img.shields.io/badge/config-no%20YAML-brightgreen" alt="no YAML">
  <img src="https://img.shields.io/badge/licence-MIT-blue" alt="MIT">
</p>

<p align="center">
  <img src="screenshots/guest-dashboard.jpg" alt="A guest's dashboard, with the locks filtered out" width="820">
</p>

<p align="center"><em>A guest's dashboard. Their lights are there. The front door lock isn't.</em></p>

---

> ### ⚠️ This is an alpha
>
> It works, it has 160 tests, and it has been run against a real Home Assistant —
> but it is new, it has had one round of review, and it has not been through a
> long tail of real households. **Don't make it the only thing standing between
> someone and your front door yet.** Try it, break it, and
> [tell me what happened](https://github.com/FezVrasta/ha-rbac/issues).

---

## The problem

Home Assistant has two kinds of user: **administrator**, and **everyone else**.

That's the whole model. "Everyone else" still sees every device in your home —
every camera, every lock, every sensor. There's no way to hand your house guest a
dashboard with just the living room lights on it, or to give your kid a tablet
that can't unlock the front door.

## What you get

🎭 **Roles that actually mean something** — read, control, or nothing, per entity,
per device, per area, per label, or per floor, chosen with the same pickers you
use everywhere else in Home Assistant.

🙈 **Hidden means hidden** — a restricted entity doesn't appear greyed out. It
isn't in the dashboard, the search, the history, or the API. As far as that
person's Home Assistant is concerned, it doesn't exist.

🖱️ **No YAML** — roles are created and assigned in a normal Home Assistant panel.

🔌 **Nothing to maintain** — it reads Home Assistant's own permission markings at
startup, so it keeps working as Home Assistant adds features.

🏠 **Your setup is untouched** — no core files patched, no automations rewritten,
no entities renamed. Uninstall and everything is exactly as it was.

## Take a look

<table>
<tr>
<td width="50%">
<img src="screenshots/guest-search-no-locks.jpg" alt="Searching for locks as a guest finds nothing">
<p align="center"><em>A guest searching for "lock". There's nothing to find.</em></p>
</td>
<td width="50%">
<img src="screenshots/owner-sidebar.jpg" alt="The owner's sidebar, with Access Control and Settings">
<p align="center"><em>Same house, same address — signed in as yourself.</em></p>
</td>
</tr>
<tr>
<td width="50%">
<img src="screenshots/panel-roles.jpg" alt="The role editor">
<p align="center"><em>"Read everything, except the locks." Pick a baseline, add exceptions.</em></p>
</td>
<td width="50%">
<img src="screenshots/panel-denials.jpg" alt="The denials log">
<p align="center"><em>When someone says "it stopped working", look here first.</em></p>
</td>
</tr>
</table>

## How it works, in one minute

It sits in front of Home Assistant and reads everything going past. When your
guest's browser asks for the state of the house, it answers — minus the parts
they're not allowed to see. When it asks to unlock a door, it says no.

The clever part is that it doesn't ship a list of what's dangerous. Home
Assistant already marks its own administrative features, and this reads those
markings live, on your instance, with your integrations installed. That's why it
doesn't need updating every time Home Assistant does.

There's a longer explanation in [docs/DESIGN.md](docs/DESIGN.md) if you want it.

## Before you install

**One line of config, and it matters.** Because this works by sitting in front of
Home Assistant, Home Assistant has to stop answering the door itself:

```yaml
# configuration.yaml
http:
  server_host: 127.0.0.1
```

Then everyone visits **port 8124** instead of 8123. That's it — but skip it and
this does nothing at all, because anyone can just knock on the old door. It warns
you at startup if you forget.

<details>
<summary><strong>What this protects against, honestly</strong></summary>

<br>

**It holds** against anyone on your network. Someone with a guest login cannot
see or touch what their role forbids, from a browser, the app, or the API.

**It doesn't hold** against someone with a login *on the machine Home Assistant
runs on*. Anyone with a shell there can read Home Assistant's own credential
store and impersonate you — that beats Home Assistant's security, not just this.
If that's a person in your house, don't give them a shell account.

**Home Assistant OS users:** add-ons talk to Home Assistant through a private
channel that nothing can sit in front of. Any add-on you install is outside this.

Full detail in [docs/DESIGN.md](docs/DESIGN.md).

</details>

## Install

**HACS** — add this repository as a custom repository, install, restart.

**By hand** — copy `custom_components/ha_rbac` into your `config/custom_components`,
restart.

Then add **Access Control** from Settings → Devices & Services.

Nothing changes until you assign someone a role, so it's safe to install and
look around first.

## Your first role

1. Open **Access Control** in the sidebar.
2. **Clone** *Read only*, name it something like *Guest*, and add the domains or
   areas you want to hide under **Deny**.
3. Go to **Users**, pick the person, choose the role, save.

<p align="center">
  <img src="screenshots/panel-users.jpg" alt="Assigning a role to a user" width="820">
</p>

Have them reload, and their Home Assistant is now smaller.

Anyone without a role keeps exactly the access Home Assistant already gave them,
so you can roll this out one person at a time.

### Locked yourself out?

You can't lock the owner account out — that's built in and can't be changed from
the panel. Failing that, `ssh -L 8123:127.0.0.1:8123 your-ha-host` and browse
`localhost:8123` for plain unfiltered Home Assistant.

## What it can't do yet

- **All or nothing per entity.** If someone can see a light, they see everything
  about that light. No hiding individual attributes.
- **Automations aren't affected.** They run as the system, not as a person, so an
  automation can still touch anything. Same as stock Home Assistant.
- **One extra login.** Moving everyone from `:8123` to `:8124` signs them out once.
- **Not tried on Home Assistant OS.** See the note above about add-ons.

## Contributing

Bug reports from real households are the most useful thing right now — especially
"my dashboard broke and here's what the Denials tab said". Issues and pull
requests welcome.

## Licence

MIT.
