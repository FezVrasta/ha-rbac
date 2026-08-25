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
  <img src="screenshots/panel-roles.jpg" alt="The Access Control panel, editing a Guests role" width="880">
</p>

<p align="center"><em>One role, read out loud: see and control everything, except the locks and cameras, and don't show anyone where people are.</em></p>

---

> [!CAUTION]
> **This is an alpha.** It works, it's tested, and it survived a round of
> deliberately trying to break it. But it's new, and it hasn't lived in real
> houses yet. **Don't make it the only thing between someone and your front
> door yet.** Try it, break it, and
> [tell me what happened](https://github.com/FezVrasta/ha-rbac/issues).

---

## The problem

Home Assistant has two kinds of user: **administrator**, and **everyone else**.

That's the whole model. "Everyone else" still sees every device in your home:
every camera, every lock, every sensor. There's no way to hand a house guest a
dashboard with just the living room lights on it, or to give your kid a tablet
that can't unlock the front door.

## What you can control

### 🏠 Entities: what they see and touch

**No access**, **read**, or **read and control**, as a baseline plus exceptions.
Target them however you already think about your house:

| | |
| --- | --- |
| **Areas** | "nothing in the bedroom" |
| **Domains** | "no locks, no cameras" |
| **Labels** | "only what I tagged `shared`" |
| **Floors** | "the ground floor only" |
| **Entities / devices** | one specific thing |

Chosen with the same pickers you use everywhere else in Home Assistant.

### 📍 Details: how much of an entity they see

An entity someone can see, they normally see in full: every attribute it
reports. Often that's more than you meant to share:

| | |
| --- | --- |
| **Where someone is** | `latitude`, `longitude` on people and trackers |
| **Access codes** | the code attribute a lock or alarm exposes |
| **Network details** | IP addresses, MAC addresses, hostnames |
| **Identifiers** | serial numbers, device IDs, account names |
| **Noise** | diagnostics and internals nobody needs to read |

Rules name both the attributes and the entities they apply to, so hiding
`latitude` on people and trackers leaves the zones that define where home is
working normally.

Hidden attributes are gone from the dashboard, the state API, history, live
updates, and templates.

### 📱 Apps, dashboards and add-ons: where they can go

Everything in the sidebar, ticked or unticked:

| | |
| --- | --- |
| **Dashboards** | give the kids their own and hide yours |
| **Add-ons** | no File Editor, no Terminal, no Node-RED |
| **Built-in screens** | Energy, History, Logbook, Map, Media, To-do |
| **Custom panels** | anything else that shows up there |

Home Assistant treats all of these as the same kind of thing, so this does too:
one list, read from your instance, whatever you happen to have installed.

### 🕗 Hours: when the role applies at all

Give a role a window of days, hours, or both, and it only applies inside it.
Add as many windows as the arrangement needs; any one of them is enough:

| | |
| --- | --- |
| **A cleaner** | weekdays, 09:00 to 17:00 |
| **Someone with a split shift** | Monday and Tuesday, 10:00 to 12:00, and again 15:00 to 19:00 |
| **A babysitter** | Friday and Saturday, 18:00 to 23:00 |
| **Weekends on different hours** | weekdays 09:00 to 17:00, weekends 10:00 to 22:00 |
| **A night guest** | every day, 22:00 to 06:00 |

Outside them the role is simply not held, and holding no role means no access
rather than the access they had before. An end before the start runs through
midnight, and the window belongs to the day it opened, so Friday 22:00 to 02:00
is still in force at one on Saturday morning.

It takes effect on connections that are already open, so someone does not keep
what they had until they close the tab.

### ⚙️ Commands: what they can change

**Ordinary use**, or **everything including settings**. Which half is which is
read from Home Assistant's own markings rather than a list kept here, so it
stays right as Home Assistant grows.

---

And the parts that make it usable:

🙈 **Hidden means hidden.** A restricted entity isn't greyed out. It isn't in
the dashboard, the search, the history, or the API. As far as that person's Home
Assistant is concerned, it doesn't exist.

🖱️ **No YAML.** All of the above is done in a normal Home Assistant panel.

🏠 **Your setup is untouched.** No core files patched, no automations rewritten,
no entities renamed. Uninstall and everything is exactly as it was.

📋 **A log of every refusal**, so when someone says "it stopped working" you can
see what and why.

## Take a look

<table>
<tr>
<td width="50%">
<img src="screenshots/guest-search-no-locks.jpg" alt="A guest searching for locks finds none">
<p align="center"><em>A guest searching for "lock". There's nothing to find.</em></p>
</td>
<td width="50%">
<img src="screenshots/owner-sidebar.jpg" alt="The same search as the owner, showing four locks">
<p align="center"><em>The same search, same house, signed in as yourself.</em></p>
</td>
</tr>
<tr>
<td width="50%">
<img src="screenshots/panel-users.jpg" alt="Assigning roles to people">
<p align="center"><em>Who gets what. Leave someone unassigned and nothing changes for them.</em></p>
</td>
<td width="50%">
<img src="screenshots/panel-denials.jpg" alt="The denials log">
<p align="center"><em>When someone says "it stopped working", look here first.</em></p>
</td>
</tr>
</table>

## How it works, in one minute

It sits in front of Home Assistant and reads everything going past. When your
guest's browser asks for the state of the house, it answers, minus the parts
they're not allowed to see. When it asks to unlock a door, it says no.

The useful part is that it ships no list of what's dangerous. Home Assistant
already marks its own administrative features, and this reads those markings
live, on your instance, with your integrations installed. That's why it doesn't
need updating every time Home Assistant does.

Longer version in [docs/DESIGN.md](docs/DESIGN.md).

## Install

### 1. Add the integration

**With HACS:**

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=FezVrasta&repository=ha-rbac&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open this repository inside the Home Assistant Community Store."></a>

That button adds this as a custom repository in your own Home Assistant. Then
**Download**, and restart. By hand instead: HACS → ⋮ → *Custom repositories* →
paste `https://github.com/FezVrasta/ha-rbac`, category **Integration**.

**Without HACS:** copy `custom_components/ha_rbac` into your
`config/custom_components` and restart.

Then add **Access Control** from Settings → Devices & Services.

> [!NOTE]
> Nothing changes until you give someone a role, so it's safe to install and
> look around first.

### 2. Close Home Assistant's own door

> [!IMPORTANT]
> **This step is the one that matters.** This works by standing in front of
> Home Assistant, so Home Assistant has to stop answering directly. Otherwise
> anyone can walk straight around it with the login they already have. It warns
> you at startup if you skip it.

What you want is Home Assistant listening on `127.0.0.1:8124`, and this
integration (which defaults to exactly that) answering on `8123`, the address
everyone already uses. Nothing else changes: bookmarks, the companion app, your
Google or Alexa setup all keep working, and **nobody gets signed out**, because
to a browser it is the same address as before. In Docker, keep publishing `8123`
and don't publish `8124`.

Set it in **Settings → System → Network**, in the **HTTP** section:

| | |
| --- | --- |
| **Server port** | `8124` |
| **Server host** (under *Advanced*) | `127.0.0.1` |

Home Assistant restarts to apply it, then asks you to confirm once you can
still reach it. If you can't, it puts the old settings back by itself, so a
mistake here locks you out for a few minutes rather than for good.

> [!CAUTION]
> **If this integration ever fails to load, Home Assistant is reachable only
> from its own machine.** That's deliberate: it fails closed rather than
> throwing the doors open. But keep a way in,
> `ssh -L 8124:127.0.0.1:8124 your-ha-host` and then browse `localhost:8124`,
> and set that up before you need it.

<details>
<summary><strong>What this protects against, honestly</strong></summary>

<br>

**It holds** against anyone on your network. Someone with a guest login cannot
see or touch what their role forbids, from a browser, the app, or the API.

**It doesn't hold** against someone with a login *on the machine Home Assistant
runs on*. Anyone with a shell there can read Home Assistant's credential store
and impersonate you, which beats Home Assistant's own security, not just this.
If that's a person in your house, don't give them a shell account.

**On Home Assistant OS and Supervised**, add-ons talk to Home Assistant through
a private channel nothing can sit in front of, so an add-on with API access can
do as it likes regardless of anyone's role. The loopback setting above is fine
here, though: Supervisor reaches Home Assistant over a Unix socket rather than
the network port.

Full detail in [docs/DESIGN.md](docs/DESIGN.md).

</details>

## Your first role

1. Open **Access Control** in the sidebar.
2. **Clone** *Read only*, name it something like *Guest*, and add the areas or
   domains to hide as exceptions. Untick any apps, dashboards or add-ons they
   shouldn't reach.
3. Go to **Users**, pick the person, choose the role, save.

Have them reload, and their Home Assistant is now smaller.

Anyone without a role keeps exactly the access Home Assistant already gave them,
so you can roll this out one person at a time.

**Locked yourself out?** You can't lock out the owner account: that's built in
and can't be changed from the panel. Failing that, use the SSH tunnel from the
install step for plain, unfiltered Home Assistant.

## What it can't do yet

- **Automations aren't affected.** They run as the system, not as a person, so
  an automation can still touch anything. Same as stock Home Assistant.
- **Hiding something hides it well, but doesn't rewrite history.** A denied
  dashboard is gone from the sidebar and its config is refused; a denied add-on
  leaves the sidebar, its Supervisor endpoints are refused, it's dropped from
  the add-on listings, and its own web page is refused even to someone who
  knows its address. What none of it does is pretend the thing was never
  installed. Someone determined can still tell something is being withheld.

## Contributing

Bug reports from real households are the most useful thing right now,
especially "my dashboard broke, and here's what the Denials tab said". Issues
and pull requests welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for how to run
the tests.

## Licence

MIT.
