<h1 align="center">Access Control for Home Assistant</h1>

<p align="center">
  <strong>Give everyone in the house their own Home Assistant.</strong><br>
  Guests see the lights. Kids get their own dashboard. Nobody but you
  touches the locks, the add-ons, or the settings.
</p>

<p align="center">
  <img src="https://github.com/FezVrasta/ha-rbac/actions/workflows/ci.yml/badge.svg" alt="CI">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-custom-41BDF5.svg" alt="HACS custom repository"></a>
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="alpha">
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.8%2B-41BDF5" alt="Home Assistant">
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
> It works, it's tested, and it's been through review and a round of trying to
> break it. But it's new, and it hasn't lived in real houses yet.
> **Don't make it the only thing standing between someone and your front door
> yet.** Try it, break it, and
> [tell me what happened](https://github.com/FezVrasta/ha-rbac/issues).

---

## The problem

Home Assistant has two kinds of user: **administrator**, and **everyone else**.

That's the whole model. "Everyone else" still sees every device in your home —
every camera, every lock, every sensor. There's no way to hand your house guest a
dashboard with just the living room lights on it, or to give your kid a tablet
that can't unlock the front door.

## What you can control

Three things, set per role:

### 🏠 Entities — what they can see and touch

Pick **no access**, **read**, or **read and control**, as a baseline plus
exceptions. Target them however you already think about your house:

| | |
| --- | --- |
| **Areas** | "nothing in the bedroom" |
| **Domains** | "no locks, no cameras" |
| **Labels** | "only what I tagged `shared`" |
| **Floors** | "the ground floor only" |
| **Entities / devices** | one specific thing |

Chosen with the same pickers you use everywhere else in Home Assistant.

### 📍 Details — how much of an entity they see

An entity someone can see, they normally see in full — every attribute it
reports. Often that's more than you meant to share:

| | |
| --- | --- |
| **Where someone is** | `latitude`, `longitude` on people and trackers |
| **Access codes** | the code attribute a lock or alarm exposes |
| **Network details** | IP addresses, MAC addresses, hostnames |
| **Identifiers** | serial numbers, device IDs, account names |
| **Noise** | diagnostics and internals nobody needs to read |

Name the attributes and the entities they apply to. Rules are targeted the same
way entity rules are, so hiding `latitude` on people and trackers leaves the
zones that define where home is working normally.

Hidden attributes are gone from the dashboard, the state API, history, the live
updates, and templates.

### 📱 Apps, dashboards and add-ons — where they can go

Everything in the sidebar, ticked or unticked:

| | |
| --- | --- |
| **Dashboards** | give the kids their own and hide yours |
| **Add-ons** | no File Editor, no Terminal, no Node-RED |
| **Built-in screens** | Energy, History, Logbook, Map, Media, To-do |
| **Custom panels** | anything else that shows up there |

Home Assistant treats all of these as the same kind of thing, so this integration
does too — one list, read from your instance, whatever you happen to have
installed.

### ⚙️ Commands — what they can change

**Ordinary use** or **everything, including settings and configuration**. The
administrative half is recognised from Home Assistant's own markings rather than
a list here, so it stays right as Home Assistant grows.

---

### And the parts that make it usable

🙈 **Hidden means hidden.** A restricted entity isn't greyed out — it isn't in
the dashboard, the search, the history, or the API. As far as that person's Home
Assistant is concerned, it doesn't exist.

🖱️ **No YAML.** Everything above is done in a normal Home Assistant panel.

🏠 **Your setup is untouched.** No core files patched, no automations rewritten,
no entities renamed. Uninstall and everything is exactly as it was.

📋 **A log of every refusal**, so when someone says "it stopped working" you can
see what and why.

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
<p align="center"><em>"Read everything, except the locks — and never their location." One role, all three axes.</em></p>
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

This works by sitting in front of Home Assistant, so Home Assistant has to stop
answering the door itself. **One line of config, and it matters** — skip it and
this does nothing at all, because anyone can just knock on the old door. It warns
you at startup if you forget.

### Recommended: keep everyone on `:8123`

Move Home Assistant to a port only this integration can reach, and let it answer
on the address everyone already uses:

```yaml
# configuration.yaml
http:
  server_host: 127.0.0.1
  server_port: 8124
```

Nothing else changes. Bookmarks, the companion app, your Google or Alexa setup,
anything talking to `:8123` — all of it keeps working, and **nobody gets signed
out**, because as far as browsers are concerned it is the same address as before.

In Docker, keep publishing `8123` and don't publish `8124`.

### Alternative: leave Home Assistant where it is

If you would rather not move it, keep Home Assistant on `8123` and have people
visit `:8124` instead:

```yaml
# configuration.yaml
http:
  server_host: 127.0.0.1
```

The catch is that `:8124` is a different address to a browser, so everyone signs
in once more, and anything pointed at `:8123` needs updating.

> **Either way, if this integration stops loading, Home Assistant is only
> reachable from the machine it runs on.** That is deliberate — it fails closed
> rather than throwing the doors open — but it means keeping a way in:
> `ssh -L 8124:127.0.0.1:8124 your-ha-host` and browse `localhost:8124`. Set that
> up before you need it.

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

It reads a few Home Assistant internals to work out what is administrative, so
it checks at startup whether it can still make sense of them — and refuses to
enforce rather than guessing if it can't. Recent versions only; the CI badge
above shows what it's currently built against.

### With HACS

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=FezVrasta&repository=ha-rbac&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open this repository inside the Home Assistant Community Store."></a>

That button adds this as a custom repository in your own Home Assistant. Then
**Download**, and restart.

Doing it by hand instead: HACS → ⋮ → *Custom repositories* → paste
`https://github.com/FezVrasta/ha-rbac`, category **Integration**.

### Without HACS

Copy `custom_components/ha_rbac` into your `config/custom_components` and
restart.

Then add **Access Control** from Settings → Devices & Services. The ports it
offers match the recommended layout above; change them if you chose the
alternative.

Nothing changes until you assign someone a role, so it's safe to install and
look around first.

## Your first role

1. Open **Access Control** in the sidebar.
2. **Clone** *Read only*, name it something like *Guest*, and add the areas or
   domains you want to hide as exceptions. Untick any apps, dashboards or
   add-ons they shouldn't reach.
3. Go to **Users**, pick the person, choose the role, save.

<p align="center">
  <img src="screenshots/panel-users.jpg" alt="Assigning a role to a user" width="820">
</p>

Have them reload, and their Home Assistant is now smaller.

Anyone without a role keeps exactly the access Home Assistant already gave them,
so you can roll this out one person at a time.

### Locked yourself out?

You can't lock the owner account out — that's built in and can't be changed from
the panel. Failing that, tunnel to Home Assistant itself:
`ssh -L 8124:127.0.0.1:8124 your-ha-host`, then browse `localhost:8124` for plain
unfiltered Home Assistant. (Swap the port if you chose the alternative layout.)

## What it can't do yet

- **Automations aren't affected.** They run as the system, not as a person, so an
  automation can still touch anything. Same as stock Home Assistant.
- **Add-on control is tested only in theory.** Add-ons need Home Assistant OS or
  Supervised, which this hasn't run on yet. The mechanism is the same one that
  hides dashboards and built-in screens, and that part is tested — but if you
  hand someone a link to an add-on they already had open, that link keeps
  working until it expires.
- **Hiding an app hides it well, but a determined person knows what exists.**
  A denied dashboard is gone from the sidebar and its config is refused; it does
  not pretend the URL was never there.

## Contributing

Bug reports from real households are the most useful thing right now —
especially "my dashboard broke, and here's what the Denials tab said". Issues
and pull requests welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for how to run
the tests.

## Licence

MIT.
