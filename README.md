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
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/how-it-works-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/how-it-works-light.svg">
    <img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/how-it-works-light.svg" alt="Everyone's requests pass through Access Control on the way to Home Assistant. A guest asking to unlock the front door is refused; the same request from the owner goes through. Answers coming back are trimmed to what that person may see." width="880">
  </picture>
</p>

<p align="center"><em>Everyone talks to the same Home Assistant. Each person only ever gets their own slice of it.</em></p>

---

Home Assistant has two kinds of user: administrator, and everyone else. "Everyone
else" still sees every camera, every lock and every sensor you own. There's no
way to hand a guest a dashboard with just the living room lights on it, or to
give your kid a tablet that can't open the front door.

This adds roles. You decide what each one can see and do, then hand them out.

It isn't only for people. Point an **AI assistant or agent** at Home Assistant
through its own restricted login and it's held to the same role. It reads and
changes exactly what you allowed, and nothing else. A useful fence to put around
what an LLM can do in your home.

## What a role decides

| | |
| --- | --- |
| **What they see** | By area, domain, label, floor, or one particular device. *Nothing in the bedroom. No locks.* |
| **How much** | Look only, or look and touch. |
| **What stays private** | Hide details: where someone is, a door code, a serial number. |
| **Where they can go** | Which dashboards, add-ons and screens are in their sidebar. |
| **When** | Days and hours. *A cleaner, weekdays 9 to 5. A babysitter, Friday evenings.* |
| **What they can change** | Nothing, everything, or one part of the settings: automations, dashboards, helpers, users, backups. |

Hidden means hidden. A restricted device isn't greyed out, it's absent: from
the dashboard, from search, from history, and from the API.

Four roles to start from: **Administrator**, **Editor**, **User** and
**Read only**. Copy any of them and change it.

## Take a look

<table>
<tr>
<td colspan="2">
<img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/panel-roles.jpg" alt="The Access Control panel, editing a Guests role">
<p align="center"><em>One role at a glance: where it can go, what it can see, what it can change. Each line opens.</em></p>
</td>
</tr>
<tr>
<td width="50%">
<img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/guest-search-no-locks.jpg" alt="A guest searching for locks finds none">
<p align="center"><em>A guest searching for "lock". There's nothing to find.</em></p>
</td>
<td width="50%">
<img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/owner-sidebar.jpg" alt="The same search as the owner, showing four locks">
<p align="center"><em>The same search, same house, signed in as yourself.</em></p>
</td>
</tr>
<tr>
<td width="50%">
<img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/panel-users.jpg" alt="Assigning roles to people">
<p align="center"><em>Who gets what.</em></p>
</td>
<td width="50%">
<img src="https://raw.githubusercontent.com/FezVrasta/ha-rbac/main/screenshots/panel-denials.jpg" alt="The denials log">
<p align="center"><em>When someone says "it stopped working", look here first.</em></p>
</td>
</tr>
</table>

## Install

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=FezVrasta&repository=ha-rbac&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open this repository inside the Home Assistant Community Store."></a>

That button adds this to HACS. **Download**, restart, then add **Access
Control** from Settings → Devices & Services and say yes when it offers to move
Home Assistant for you.

Home Assistant restarts once and comes back about fifteen seconds later. The
address doesn't change, so bookmarks, the companion app and your Google or Alexa
setup keep working, and nobody gets signed out.

If it doesn't come back, Home Assistant undoes the change by itself within five
minutes and restarts on the port it was on before.

<details>
<summary>Why does Home Assistant have to move?</summary>

<br>

Roles are applied to traffic passing through this integration, so Home
Assistant has to stop answering on your network directly. Otherwise anyone could
go straight round it with the login they already have.

So Home Assistant moves to a port reachable only from its own machine, and this
takes over the address everyone already uses.

Prefer to do it yourself? Decline the offer, then set **Server port** to `8124`
under Settings → System → Network, add **Access Control**, and finally set
**Server host** to `127.0.0.1`. In that order, because each step leaves you
with a Home Assistant you can still reach.

</details>

## Your first role

1. Open **Access Control** in the sidebar.
2. Copy **Read only**, call it *Guest*, and add what they may see.
3. Go to **Users**, pick the person, choose the role, save.

Have them reload. Their Home Assistant is now smaller.

Nothing changes for anyone until you give them a role, so you can do this one
person at a time.

Not sure what to allow? Hit **Record what this role needs**, let them use Home
Assistant normally for a few minutes, and stop. Everything they touched is added
to the role, so you build it from what they actually use instead of guessing and
then chasing what broke. While it records they have full access, and the panel
says so.

## Good to know

**You can't lock yourself out.** The owner account always keeps full access.

**Not everything goes through it.** Automations, add-ons and webhooks reach Home
Assistant by other routes, and so does anyone with a login to the machine itself.
Roles don't apply there. There's a
[plain list of what's covered and what isn't](https://github.com/FezVrasta/ha-rbac/blob/main/docs/DESIGN.md#what-this-does-and-does-not-protect-against).

**It's an alpha.** Tested, and deliberately attacked twice (two adversarial
reviews by its author, no external audit yet), but it hasn't lived in anyone
else's house yet. Try it on something that isn't your front door, and
[tell me what broke](https://github.com/FezVrasta/ha-rbac/issues).

## How it works

It sits in front of Home Assistant and reads everything going past. When a
guest's browser asks what's in the house, it answers with their part of it. When
something asks to unlock a door it isn't allowed to, it says no.

It ships no list of what's dangerous. Home Assistant already marks its own
administrative features, and this reads those markings on your instance, which
is why it doesn't go stale every time Home Assistant updates.

The long version is in [docs/DESIGN.md](https://github.com/FezVrasta/ha-rbac/blob/main/docs/DESIGN.md).

## Frequently asked questions

<details>
<summary><strong>Is this a fork of Home Assistant? Does it change the interface?</strong></summary>

<br>

No. It's stock Home Assistant, unmodified. This filters the API in front of it,
which is why a hidden device isn't greyed out: your browser was never told it
exists. Nothing is patched, so nothing re-breaks on update.

</details>

<details>
<summary><strong>Can installing this make my security worse?</strong></summary>

<br>

No. It can only remove access, never add it. Home Assistant still authenticates
every request exactly as before; this runs after that and can only say no.

</details>

<details>
<summary><strong>Isn't this security by obscurity?</strong></summary>

<br>

No. Denied data is never sent to the browser and denied requests never reach
Home Assistant. Open the developer tools and read the websocket frames: it isn't
there. The [known limitations](https://github.com/FezVrasta/ha-rbac/blob/main/docs/DESIGN.md#known-limitations) are written down rather
than glossed.

</details>

<details>
<summary><strong>Why not contribute to the official RBAC effort instead?</strong></summary>

<br>

There isn't one to contribute to yet. The
[core proposal](https://github.com/home-assistant/architecture/discussions/1374)
was declined, and so was the small change that would have unblocked doing this
properly, on the grounds that authorisation needs oversight the Foundation
hasn't allocated. [The longer answer](https://github.com/FezVrasta/ha-rbac/blob/main/docs/DESIGN.md#why-this-is-not-a-core-patch).

</details>

<details>
<summary><strong>Does the search dialog (Ctrl+K) leak things?</strong></summary>

<br>

No, it just finds less. Search runs over the data the browser already has, and a
restricted browser was never sent the hidden entities or the denied screens.
There's a [screenshot of exactly that](#take-a-look).

</details>

<details>
<summary><strong>Home Assistant is still listening on 8124. Can't someone go there?</strong></summary>

<br>

Only from the machine itself. Home Assistant's server host is set to
`127.0.0.1`, so it isn't listening on your network at all. That's the mechanism,
not Docker, and it works the same on Home Assistant OS, Supervised, Container
and Core. Anyone with a shell on the box is
[out of scope](https://github.com/FezVrasta/ha-rbac/blob/main/docs/DESIGN.md#what-this-does-and-does-not-protect-against) either way.

</details>

<details>
<summary><strong>Do I have to use 8123 and 8124?</strong></summary>

<br>

No, both are configurable. The default keeps `8123` here so that browsers,
phones and cloud integrations carry on working without being repointed.

</details>

<details>
<summary><strong>Does it work behind NGINX, Traefik or Cloudflare?</strong></summary>

<br>

Yes, with nothing to reconfigure: your reverse proxy already points at `8123`,
and that's still where this answers. Real client addresses survive the extra
hop, because it appends to the `X-Forwarded-For` chain rather than replacing it.

</details>

## Contributing

Bug reports from real houses are the most useful thing right now, especially
"my dashboard broke, and here's what the Denials tab said". See
[CONTRIBUTING.md](https://github.com/FezVrasta/ha-rbac/blob/main/CONTRIBUTING.md) for running the tests.

## Licence

This project is licensed under the [MIT License](LICENSE). 
See the [LICENSE](LICENSE) file for more details, including copyright information and full terms of use.
