/**
 * Admin panel for RBAC access control.
 *
 * A plain custom element with no build step: Lit is not reliably importable
 * from a custom panel without bundling, and this is small enough not to need it.
 * Home Assistant sets `hass`, `narrow` and `panel` as properties.
 */

const TIERS = ["open", "user", "admin"];

const STYLES = `
  :host { display: block; padding: 16px; }
  .layout { display: grid; grid-template-columns: minmax(220px, 280px) 1fr; gap: 16px; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
    padding: 16px; margin-bottom: 16px;
  }
  h2 { margin: 0 0 12px; font-size: 1.1rem; font-weight: 500; }
  h3 { margin: 16px 0 8px; font-size: .95rem; font-weight: 500;
       color: var(--secondary-text-color); }
  ul.roles { list-style: none; margin: 0; padding: 0; }
  ul.roles li {
    padding: 10px 12px; border-radius: 8px; cursor: pointer;
    display: flex; justify-content: space-between; align-items: center;
  }
  ul.roles li:hover { background: var(--secondary-background-color, #f0f0f0); }
  ul.roles li[aria-selected="true"] {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .badge {
    font-size: .7rem; opacity: .75; border: 1px solid currentColor;
    border-radius: 10px; padding: 1px 6px;
  }
  label { display: block; margin: 10px 0 4px; font-size: .85rem;
          color: var(--secondary-text-color); }
  input[type=text], textarea, select {
    width: 100%; box-sizing: border-box; padding: 8px;
    border: 1px solid var(--divider-color, #ccc); border-radius: 6px;
    background: var(--primary-background-color, #fff);
    color: var(--primary-text-color, #000); font: inherit;
  }
  textarea { min-height: 70px; font-family: var(--code-font-family, monospace);
             font-size: .82rem; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > * { flex: 1 1 220px; }
  button {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    border: none; border-radius: 6px; padding: 9px 16px;
    font: inherit; cursor: pointer;
  }
  button.secondary {
    background: transparent; color: var(--primary-color);
    border: 1px solid var(--primary-color);
  }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--divider-color, #eee); }
  th { color: var(--secondary-text-color); font-weight: 500; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
  .tabs button { background: transparent; color: var(--primary-text-color);
                 border-bottom: 2px solid transparent; border-radius: 0; }
  .tabs button[aria-selected="true"] {
    color: var(--primary-color); border-bottom-color: var(--primary-color);
  }
  .warn {
    background: var(--warning-color, #ffa726); color: #000;
    padding: 12px 16px; border-radius: 8px; margin-bottom: 16px;
  }
  .muted { color: var(--secondary-text-color); font-size: .85rem; }
  code { font-family: var(--code-font-family, monospace); font-size: .82rem; }
`;

class HaRbacPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "roles";
    this._roles = [];
    this._bindings = [];
    this._denials = [];
    this._catalog = null;
    this._selected = null;
    this._loaded = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._refresh();
    }
  }

  async _call(type, extra = {}) {
    return this._hass.callWS({ type: `ha_rbac/${type}`, ...extra });
  }

  async _refresh() {
    try {
      const [roles, bindings, catalog] = await Promise.all([
        this._call("roles/list"),
        this._call("bindings/list"),
        this._call("catalog"),
      ]);
      this._roles = roles;
      this._bindings = bindings;
      this._catalog = catalog;
      if (!this._selected && roles.length) this._selected = roles[0].id;
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>${this._body()}`;
    this._wire();
  }

  _body() {
    const warning =
      this._catalog && this._catalog.degraded
        ? `<div class="warn">Permission derivation is not working on this Home
             Assistant version. Every request is being denied rather than
             silently allowed. This usually means an upstream change to how
             <code>require_admin</code> is implemented.</div>`
        : "";
    const error = this._error ? `<div class="warn">${esc(this._error)}</div>` : "";
    const tabs = ["roles", "users", "denials"]
      .map(
        (tab) =>
          `<button data-tab="${tab}" aria-selected="${this._tab === tab}">
             ${tab[0].toUpperCase()}${tab.slice(1)}</button>`
      )
      .join("");

    let content = "";
    if (this._tab === "roles") content = this._rolesView();
    else if (this._tab === "users") content = this._usersView();
    else content = this._denialsView();

    return `${warning}${error}<div class="tabs">${tabs}</div>${content}`;
  }

  _rolesView() {
    const list = this._roles
      .map(
        (role) => `
        <li data-role="${esc(role.id)}" aria-selected="${this._selected === role.id}">
          <span>${esc(role.name)}</span>
          ${role.system_generated ? '<span class="badge">built in</span>' : ""}
        </li>`
      )
      .join("");

    const role = this._roles.find((r) => r.id === this._selected);
    return `
      <div class="layout">
        <div class="card">
          <h2>Roles</h2>
          <ul class="roles">${list}</ul>
          <div class="actions"><button id="new-role">New role</button></div>
        </div>
        <div class="card">${role ? this._editor(role) : "<p>Select a role.</p>"}</div>
      </div>`;
  }

  _editor(role) {
    const locked = role.system_generated;
    const tiers = role.tiers || {};
    return `
      <h2>${esc(role.name)}</h2>
      ${
        locked
          ? `<p class="muted">Built-in roles cannot be edited. Clone this one to
               start from its settings.</p>`
          : ""
      }
      <label for="name">Name</label>
      <input id="name" type="text" value="${esc(role.name)}" ${locked ? "disabled" : ""}>

      <h3>Entities</h3>
      <p class="muted">Written as a Home Assistant policy. Denial is coarse:
        denying a domain denies everything in it, so grant narrowly rather than
        denying broadly and carving out exceptions.</p>
      <div class="row">
        <div>
          <label for="allow">Allow</label>
          <textarea id="allow" ${locked ? "disabled" : ""}>${esc(
            JSON.stringify(role.allow || {}, null, 2)
          )}</textarea>
        </div>
        <div>
          <label for="deny">Deny</label>
          <textarea id="deny" ${locked ? "disabled" : ""}>${esc(
            JSON.stringify(role.deny || {}, null, 2)
          )}</textarea>
        </div>
      </div>

      <h3>Commands</h3>
      <p class="muted">Tiers come from Home Assistant's own
        <code>require_admin</code>, read at runtime — nothing is hard-coded.
        ${this._catalog ? this._catalog.commands.length : 0} commands derived.</p>
      <label for="tier">Highest tier this role may use</label>
      <select id="tier" ${locked ? "disabled" : ""}>
        ${TIERS.map(
          (tier) =>
            `<option value="${tier}" ${
              (tiers.max || "open") === tier ? "selected" : ""
            }>${tier}</option>`
        ).join("")}
      </select>
      <div class="row">
        <div>
          <label for="tier-allow">Always allow (one glob per line)</label>
          <textarea id="tier-allow" ${locked ? "disabled" : ""}>${esc(
            (tiers.allow || []).join("\n")
          )}</textarea>
        </div>
        <div>
          <label for="tier-deny">Always deny (one glob per line)</label>
          <textarea id="tier-deny" ${locked ? "disabled" : ""}>${esc(
            (tiers.deny || []).join("\n")
          )}</textarea>
        </div>
      </div>

      <div class="actions">
        <button id="save" ${locked ? "disabled" : ""}>Save</button>
        <button id="clone" class="secondary">Clone</button>
        <button id="delete" class="secondary" ${locked ? "disabled" : ""}>Delete</button>
      </div>`;
  }

  _usersView() {
    const rows = this._bindings
      .map((user) => {
        const options = this._roles
          .map(
            (role) =>
              `<option value="${esc(role.id)}" ${
                user.role_ids.includes(role.id) ? "selected" : ""
              }>${esc(role.name)}</option>`
          )
          .join("");
        const note = user.is_owner
          ? '<span class="badge">owner — always unrestricted</span>'
          : "";
        const fallback = user.role_ids.length
          ? ""
          : `<span class="muted">defaults to ${
              user.is_admin ? "Administrator" : "User"
            }</span>`;
        return `<tr>
          <td>${esc(user.name || user.user_id)} ${note}</td>
          <td>
            <select multiple size="3" data-user="${esc(user.user_id)}"
              ${user.is_owner ? "disabled" : ""}>${options}</select>
            ${fallback}
          </td>
        </tr>`;
      })
      .join("");

    return `<div class="card">
      <h2>Users</h2>
      <p class="muted">A user with no role assigned keeps the access their Home
        Assistant group already gives them, so installing this changes nothing
        until you assign one. The owner is always unrestricted — that is the way
        back in if a role locks you out.</p>
      <table><thead><tr><th>User</th><th>Roles</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div class="actions"><button id="save-bindings">Save assignments</button></div>
    </div>`;
  }

  _denialsView() {
    const rows = this._denials
      .map(
        (denial) => `<tr>
          <td>${esc(denial.user_name || denial.user_id)}</td>
          <td><code>${esc(denial.name)}</code></td>
          <td>${esc(denial.reason)}</td>
          <td class="muted">${esc(denial.resources.join(", "))}</td>
        </tr>`
      )
      .join("");

    return `<div class="card">
      <h2>Recent denials</h2>
      <p class="muted">A refused request reaches the user as a broken screen with
        no explanation. This is where to look when someone reports one.</p>
      <div class="actions"><button id="load-denials">Refresh</button></div>
      <table><thead><tr><th>User</th><th>Request</th><th>Reason</th>
        <th>Entities</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="muted">Nothing denied yet.</td></tr>'}</tbody>
      </table>
    </div>`;
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((button) => {
      button.onclick = () => {
        this._tab = button.dataset.tab;
        if (this._tab === "denials") this._loadDenials();
        else this._render();
      };
    });
    root.querySelectorAll("[data-role]").forEach((item) => {
      item.onclick = () => {
        this._selected = item.dataset.role;
        this._render();
      };
    });

    const on = (id, handler) => {
      const element = root.getElementById(id);
      if (element) element.onclick = handler;
    };
    on("new-role", () => this._createRole());
    on("save", () => this._saveRole());
    on("clone", () => this._cloneRole());
    on("delete", () => this._deleteRole());
    on("save-bindings", () => this._saveBindings());
    on("load-denials", () => this._loadDenials());
  }

  _readEditor() {
    const root = this.shadowRoot;
    const lines = (id) =>
      root
        .getElementById(id)
        .value.split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    return {
      name: root.getElementById("name").value.trim(),
      allow: JSON.parse(root.getElementById("allow").value || "{}"),
      deny: JSON.parse(root.getElementById("deny").value || "{}"),
      tiers: {
        max: root.getElementById("tier").value,
        allow: lines("tier-allow"),
        deny: lines("tier-deny"),
      },
    };
  }

  async _guard(action) {
    try {
      this._error = null;
      await action();
    } catch (err) {
      this._error = err.message || String(err);
    }
    await this._refresh();
  }

  _createRole() {
    this._guard(async () => {
      const role = await this._call("roles/create", {
        role: { name: "New role" },
      });
      this._selected = role.id;
    });
  }

  _saveRole() {
    this._guard(async () => {
      await this._call("roles/update", {
        role_id: this._selected,
        changes: this._readEditor(),
      });
    });
  }

  _cloneRole() {
    this._guard(async () => {
      const source = this._roles.find((role) => role.id === this._selected);
      const role = await this._call("roles/create", {
        role: {
          name: `${source.name} copy`,
          allow: source.allow,
          deny: source.deny,
          tiers: source.tiers,
        },
      });
      this._selected = role.id;
    });
  }

  _deleteRole() {
    this._guard(async () => {
      await this._call("roles/delete", { role_id: this._selected });
      this._selected = null;
    });
  }

  _saveBindings() {
    this._guard(async () => {
      const selects = this.shadowRoot.querySelectorAll("[data-user]");
      for (const select of selects) {
        const roleIds = Array.from(select.selectedOptions).map((o) => o.value);
        await this._call("bindings/set", {
          user_id: select.dataset.user,
          role_ids: roleIds,
        });
      }
    });
  }

  async _loadDenials() {
    try {
      this._denials = await this._call("denials/recent", { limit: 100 });
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }
}

function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );
}

customElements.define("ha-rbac-panel", HaRbacPanel);
