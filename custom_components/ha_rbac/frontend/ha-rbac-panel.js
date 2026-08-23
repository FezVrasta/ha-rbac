/**
 * Admin panel for RBAC access control.
 *
 * A plain custom element with no build step: Lit is not reliably importable
 * from a custom panel without bundling, and this is small enough not to need it.
 * Home Assistant sets `hass`, `narrow` and `panel` as properties.
 *
 * The role editor presents a policy as a base level plus a list of exceptions,
 * because that is how people describe access out loud -- "they can see
 * everything except the locks". The stored shape is still a Home Assistant
 * policy; `readRules` and `writeRules` convert between the two, and anything
 * the rules cannot express stays editable as raw policy under Advanced.
 */

const ACCESS = [
  { value: "none", label: "No access" },
  { value: "read", label: "Read" },
  { value: "control", label: "Read and control" },
];

const BASE = [
  { value: "none", label: "Nothing by default" },
  { value: "read", label: "Read everything" },
  { value: "control", label: "Read and control everything" },
];

// Where each kind of exception lives in a stored policy.
const TARGETS = [
  { value: "area_ids", label: "Areas", block: "entities", selector: { area: { multiple: true } } },
  { value: "domains", label: "Domains", block: "entities", selector: null },
  { value: "entity_ids", label: "Entities", block: "entities", selector: { entity: { multiple: true } } },
  { value: "device_ids", label: "Devices", block: "entities", selector: { device: { multiple: true } } },
  { value: "label_ids", label: "Labels", block: "root", selector: { label: { multiple: true } } },
  { value: "floor_ids", label: "Floors", block: "root", selector: { floor: { multiple: true } } },
];

const TIERS = ["user", "admin"];

const STYLES = `
  :host { display: block; padding: 16px 16px 48px; }
  .layout { display: grid; grid-template-columns: minmax(200px, 260px) 1fr; gap: 16px; align-items: start; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
    padding: 16px; margin-bottom: 16px;
  }
  h2 { margin: 0 0 4px; font-size: 1.15rem; font-weight: 500; }
  h3 { margin: 24px 0 4px; font-size: .95rem; font-weight: 500; }
  .hint { color: var(--secondary-text-color); font-size: .85rem; margin: 0 0 12px; line-height: 1.45; }
  ul.roles { list-style: none; margin: 0; padding: 0; }
  ul.roles li {
    padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 2px;
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }
  ul.roles li:hover { background: var(--secondary-background-color, #f0f0f0); }
  ul.roles li[aria-selected="true"] {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .badge { font-size: .68rem; opacity: .8; border: 1px solid currentColor;
           border-radius: 10px; padding: 1px 6px; white-space: nowrap; }
  label.field { display: block; margin: 12px 0 4px; font-size: .85rem;
                color: var(--secondary-text-color); }
  input[type=text], textarea, select {
    width: 100%; box-sizing: border-box; padding: 9px;
    border: 1px solid var(--divider-color, #ccc); border-radius: 6px;
    background: var(--primary-background-color, #fff);
    color: var(--primary-text-color, #000); font: inherit;
  }
  textarea { min-height: 84px; font-family: var(--code-font-family, monospace); font-size: .8rem; }
  .rule {
    display: grid; grid-template-columns: 150px 1fr 170px 40px;
    gap: 8px; align-items: center; margin-bottom: 8px;
  }
  @media (max-width: 700px) { .rule { grid-template-columns: 1fr; } }
  .rule .picker { min-width: 0; }
  .rule.deny { border-left: 3px solid var(--error-color, #db4437); padding-left: 8px; margin-left: -11px; }
  .icon-btn {
    background: none; border: none; cursor: pointer; border-radius: 50%;
    color: var(--secondary-text-color); width: 36px; height: 36px; font-size: 1.1rem;
  }
  .icon-btn:hover { background: var(--secondary-background-color, #eee); color: var(--error-color, #db4437); }
  button {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    border: none; border-radius: 6px; padding: 9px 16px; font: inherit; cursor: pointer;
  }
  button.secondary { background: transparent; color: var(--primary-color);
                     border: 1px solid var(--primary-color); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .actions { display: flex; gap: 8px; margin-top: 20px; flex-wrap: wrap; align-items: center; }
  .actions .spacer { flex: 1; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--divider-color, #eee);
           vertical-align: top; }
  th { color: var(--secondary-text-color); font-weight: 500; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
  .tabs button { background: transparent; color: var(--primary-text-color);
                 border-bottom: 2px solid transparent; border-radius: 0; padding: 9px 14px; }
  .tabs button[aria-selected="true"] { color: var(--primary-color);
                                       border-bottom-color: var(--primary-color); }
  .banner { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; line-height: 1.45; }
  .banner.warn { background: var(--warning-color, #ffa726); color: #000; }
  .banner.error { background: var(--error-color, #db4437); color: #fff; }
  .banner.ok { background: var(--success-color, #43a047); color: #fff; }
  code { font-family: var(--code-font-family, monospace); font-size: .82rem; }
  details { margin-top: 20px; }
  summary { cursor: pointer; color: var(--secondary-text-color); font-size: .9rem; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 700px) { .grid2 { grid-template-columns: 1fr; } }
  .checks { display: flex; flex-wrap: wrap; gap: 6px 18px; }
  label.check { display: flex; align-items: center; gap: 6px; font-size: .9rem;
                cursor: pointer; }
  label.check input { accent-color: var(--primary-color); }
  .pill { display: inline-block; font-size: .75rem; padding: 2px 8px; border-radius: 10px;
          background: var(--secondary-background-color, #eee); color: var(--secondary-text-color); }
`;

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

/** Turn a stored permission value into one of none/read/control. */
function accessOf(value) {
  if (value === true) return "control";
  if (!value || typeof value !== "object") return "none";
  if (value.control) return "control";
  if (value.read) return "read";
  return "none";
}

/** Turn none/read/control back into a stored permission value. */
function valueOf(access) {
  if (access === "control") return { read: true, control: true };
  if (access === "read") return { read: true };
  return {};
}

/** Read a policy pair into a base level plus a flat list of exceptions. */
function readRules(role) {
  const allow = role.allow || {};
  const deny = role.deny || {};
  const base = accessOf((allow.entities || {}).all);
  const rules = [];

  const collect = (policy, denying) => {
    for (const target of TARGETS) {
      const bucket =
        target.block === "entities"
          ? (policy.entities || {})[target.value]
          : policy[target.value];
      if (!bucket || typeof bucket !== "object") continue;
      // Group ids that share an access level so one row can hold several.
      const byAccess = {};
      for (const [id, value] of Object.entries(bucket)) {
        const access = denying ? "none" : accessOf(value);
        (byAccess[access] ||= []).push(id);
      }
      for (const [access, ids] of Object.entries(byAccess)) {
        rules.push({ target: target.value, ids, access });
      }
    }
  };
  collect(allow, false);
  collect(deny, true);
  return { base, rules };
}

/** Write a base level and exception list back into a policy pair. */
function writeRules(base, rules) {
  const allow = { entities: {} };
  const deny = { entities: {} };
  if (base !== "none") allow.entities.all = valueOf(base);

  for (const rule of rules) {
    if (!rule.ids || !rule.ids.length) continue;
    const target = TARGETS.find((t) => t.value === rule.target);
    if (!target) continue;
    const policy = rule.access === "none" ? deny : allow;
    const holder =
      target.block === "entities"
        ? (policy.entities[target.value] ||= {})
        : (policy[target.value] ||= {});
    for (const id of rule.ids) {
      holder[id] = rule.access === "none" ? true : valueOf(rule.access);
    }
  }
  if (!Object.keys(allow.entities).length) delete allow.entities;
  if (!Object.keys(deny.entities).length) delete deny.entities;
  return { allow, deny };
}

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
    this._draft = null;
    this._notice = null;
    this._loaded = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._refresh();
    }
  }

  _call(type, extra = {}) {
    return this._hass.callWS({ type: `ha_rbac/${type}`, ...extra });
  }

  async _refresh(keepDraft = false) {
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
      if (!keepDraft) this._loadDraft();
    } catch (err) {
      this._notice = { kind: "error", text: err.message || String(err) };
    }
    this._render();
  }

  _loadDraft() {
    const role = this._roles.find((r) => r.id === this._selected);
    this._draft = role ? { ...role, ...readRules(role) } : null;
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>${this._chrome()}`;
    if (this._tab === "roles") this._mountEditor();
    this._wire();
  }

  _chrome() {
    let banner = "";
    if (this._catalog && this._catalog.degraded) {
      banner += `<div class="banner warn">Permission derivation is not working on
        this Home Assistant version, so every request is being denied rather than
        quietly allowed. This usually means an upstream change to how
        <code>require_admin</code> works.</div>`;
    }
    if (this._notice) {
      banner += `<div class="banner ${this._notice.kind}">${esc(this._notice.text)}</div>`;
    }
    const tabs = [
      ["roles", "Roles"],
      ["users", "Users"],
      ["denials", "Denials"],
    ]
      .map(
        ([id, label]) =>
          `<button data-tab="${id}" aria-selected="${this._tab === id}">${label}</button>`
      )
      .join("");

    let body = "";
    if (this._tab === "roles") body = this._rolesView();
    else if (this._tab === "users") body = this._usersView();
    else body = this._denialsView();

    return `${banner}<div class="tabs">${tabs}</div>${body}`;
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

    return `
      <div class="layout">
        <div class="card">
          <h2>Roles</h2>
          <ul class="roles">${list}</ul>
          <div class="actions"><button id="new-role">New role</button></div>
        </div>
        <div class="card" id="editor"></div>
      </div>`;
  }

  /** The editor is built as nodes, not markup: the pickers hold their own state. */
  _mountEditor() {
    const host = this.shadowRoot.getElementById("editor");
    if (!host) return;
    const draft = this._draft;
    if (!draft) {
      host.innerHTML = "<p class='hint'>Select a role.</p>";
      return;
    }
    const locked = draft.system_generated;

    host.innerHTML = `
      <h2>${esc(draft.name)}</h2>
      <p class="hint">${
        locked
          ? "Built-in roles cannot be edited. Clone this one to start from its settings."
          : "Changes apply as soon as you save — no restart, no reload for the people affected."
      }</p>

      <label class="field" for="name">Name</label>
      <input id="name" type="text" value="${esc(draft.name)}" ${locked ? "disabled" : ""}>

      <h3>What this role can see</h3>
      <p class="hint">Start from a baseline, then add exceptions. Most roles are
        one line of each: see everything, except the locks.</p>
      <label class="field" for="base">Baseline</label>
      <select id="base" ${locked ? "disabled" : ""}>
        ${BASE.map(
          (b) => `<option value="${b.value}" ${draft.base === b.value ? "selected" : ""}>${b.label}</option>`
        ).join("")}
      </select>

      <h3>Exceptions</h3>
      <div id="rules"></div>
      <div class="actions">
        <button id="add-rule" class="secondary" ${locked ? "disabled" : ""}>Add exception</button>
      </div>

      <h3>What this role can do</h3>
      <p class="hint">Administrative commands are recognised from Home Assistant's
        own markings, read on this instance —
        <span class="pill">${this._catalog ? this._catalog.commands.length : 0} commands</span>
        nothing hard-coded.</p>
      <label class="field" for="tier">Command level</label>
      <select id="tier" ${locked ? "disabled" : ""}>
        ${TIERS.map(
          (t) =>
            `<option value="${t}" ${(draft.tiers || {}).max === t ? "selected" : ""}>${
              t === "admin" ? "Everything, including settings and configuration" : "Ordinary use only"
            }</option>`
        ).join("")}
      </select>

      <details>
        <summary>Advanced</summary>
        <p class="hint">Overrides by command name, and the raw policy this role
          stores. Anything the exception list above cannot express lives here.</p>
        <div class="grid2">
          <div>
            <label class="field" for="tier-allow">Always allow (one pattern per line)</label>
            <textarea id="tier-allow" ${locked ? "disabled" : ""}>${esc(
              ((draft.tiers || {}).allow || []).join("\n")
            )}</textarea>
          </div>
          <div>
            <label class="field" for="tier-deny">Always deny (one pattern per line)</label>
            <textarea id="tier-deny" ${locked ? "disabled" : ""}>${esc(
              ((draft.tiers || {}).deny || []).join("\n")
            )}</textarea>
          </div>
        </div>
        <label class="field">Stored policy</label>
        <textarea id="raw" readonly>${esc(
          JSON.stringify(writeRules(draft.base, draft.rules), null, 2)
        )}</textarea>
      </details>

      <div class="actions">
        <button id="save" ${locked ? "disabled" : ""}>Save</button>
        <button id="clone" class="secondary">Clone</button>
        <span class="spacer"></span>
        <button id="delete" class="secondary" ${locked ? "disabled" : ""}>Delete</button>
      </div>`;

    this._mountRules(host.querySelector("#rules"), locked);
  }

  _mountRules(host, locked) {
    host.innerHTML = "";
    if (!this._draft.rules.length) {
      host.innerHTML = `<p class="hint">No exceptions — the baseline applies to everything.</p>`;
      return;
    }
    this._draft.rules.forEach((rule, index) => {
      host.appendChild(this._ruleRow(rule, index, locked));
    });
  }

  _ruleRow(rule, index, locked) {
    const row = document.createElement("div");
    row.className = `rule${rule.access === "none" ? " deny" : ""}`;

    const target = document.createElement("select");
    target.innerHTML = TARGETS.map(
      (t) => `<option value="${t.value}" ${rule.target === t.value ? "selected" : ""}>${t.label}</option>`
    ).join("");
    target.disabled = locked;
    target.onchange = () => {
      rule.target = target.value;
      rule.ids = [];
      this._mountRules(this.shadowRoot.getElementById("rules"), locked);
      this._syncRaw();
    };

    const picker = document.createElement("div");
    picker.className = "picker";
    picker.appendChild(this._pickerFor(rule, locked));

    const access = document.createElement("select");
    access.innerHTML = ACCESS.map(
      (a) => `<option value="${a.value}" ${rule.access === a.value ? "selected" : ""}>${a.label}</option>`
    ).join("");
    access.disabled = locked;
    access.onchange = () => {
      rule.access = access.value;
      row.className = `rule${rule.access === "none" ? " deny" : ""}`;
      this._syncRaw();
    };

    const remove = document.createElement("button");
    remove.className = "icon-btn";
    remove.title = "Remove";
    remove.textContent = "✕";
    remove.disabled = locked;
    remove.onclick = () => {
      this._draft.rules.splice(index, 1);
      this._mountRules(this.shadowRoot.getElementById("rules"), locked);
      this._syncRaw();
    };

    row.append(target, picker, access, remove);
    return row;
  }

  _pickerFor(rule, locked) {
    const target = TARGETS.find((t) => t.value === rule.target);

    // Domains have no Home Assistant picker, so one is built from the domains
    // this instance actually has.
    const selector = target.selector || {
      select: {
        multiple: true,
        mode: "dropdown",
        options: (this._catalog ? this._catalog.domains : []).map((d) => ({
          value: d,
          label: d,
        })),
      },
    };

    if (customElements.get("ha-selector")) {
      const el = document.createElement("ha-selector");
      el.hass = this._hass;
      el.selector = selector;
      el.value = rule.ids.slice();
      el.disabled = locked;
      el.addEventListener("value-changed", (event) => {
        event.stopPropagation();
        const value = event.detail.value;
        rule.ids = Array.isArray(value) ? value : value ? [value] : [];
        this._syncRaw();
      });
      return el;
    }

    // Degrade to a plain field rather than losing the row entirely.
    const input = document.createElement("input");
    input.type = "text";
    input.value = rule.ids.join(", ");
    input.placeholder = "comma separated";
    input.disabled = locked;
    input.onchange = () => {
      rule.ids = input.value.split(",").map((v) => v.trim()).filter(Boolean);
      this._syncRaw();
    };
    return input;
  }

  _syncRaw() {
    const raw = this.shadowRoot.getElementById("raw");
    if (raw) {
      raw.value = JSON.stringify(writeRules(this._draft.base, this._draft.rules), null, 2);
    }
  }

  _usersView() {
    const rows = this._bindings
      .map((user) => {
        // Checkboxes rather than a single select: a person can hold more than
        // one role, and a single select would quietly drop the others on save.
        const boxes = this._roles
          .map(
            (role) => `<label class="check">
              <input type="checkbox" data-user="${esc(user.user_id)}"
                     value="${esc(role.id)}"
                     ${user.role_ids.includes(role.id) ? "checked" : ""}
                     ${user.is_owner ? "disabled" : ""}>
              <span>${esc(role.name)}</span></label>`
          )
          .join("");
        const note = user.is_owner
          ? '<span class="badge">owner &mdash; always unrestricted</span>'
          : "";
        const fallback = user.role_ids.length
          ? ""
          : `<div class="hint" style="margin:6px 0 0">Unassigned &mdash; keeps the access
             Home Assistant already gives them (${user.is_admin ? "Administrator" : "User"}).</div>`;
        return `<tr>
          <td>${esc(user.name || user.user_id)} ${note}</td>
          <td><div class="checks">${boxes}</div>${fallback}</td>
        </tr>`;
      })
      .join("");

    return `<div class="card">
      <h2>Users</h2>
      <p class="hint">Anyone unassigned keeps exactly the
        access Home Assistant already gives them, so you can roll this out one
        person at a time. The owner is always unrestricted &mdash; that is the way
        back in if a role locks you out.</p>
      <table><thead><tr><th>Person</th><th>Role</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="actions"><button id="save-bindings">Save</button></div>
    </div>`;
  }

  _denialsView() {
    const rows = this._denials
      .map(
        (d) => `<tr>
          <td>${esc(d.user_name || d.user_id)}</td>
          <td><code>${esc(d.name)}</code></td>
          <td>${esc(this._reason(d.reason))}</td>
          <td class="hint" style="margin:0">${esc(d.resources.join(", "))}</td>
        </tr>`
      )
      .join("");

    return `<div class="card">
      <h2>Recent denials</h2>
      <p class="hint">A refused request reaches the person as a screen that quietly
        does less, with no explanation. This is where to look when someone says
        something stopped working.</p>
      <div class="actions" style="margin-top:0"><button id="load-denials">Refresh</button></div>
      <table>
        <thead><tr><th>Person</th><th>Request</th><th>Why</th><th>Entities</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="hint">Nothing refused yet.</td></tr>'}</tbody>
      </table>
    </div>`;
  }

  _reason(reason) {
    return (
      {
        tier: "Not allowed to use that command",
        resource: "No access to that entity",
        unbounded: "Request did not say what it would touch",
        degraded: "Permission derivation unavailable",
      }[reason] || reason
    );
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((b) => {
      b.onclick = () => {
        this._tab = b.dataset.tab;
        this._notice = null;
        if (this._tab === "denials") this._loadDenials();
        else this._render();
      };
    });
    root.querySelectorAll("[data-role]").forEach((item) => {
      item.onclick = () => {
        this._selected = item.dataset.role;
        this._loadDraft();
        this._render();
      };
    });

    const on = (id, handler) => {
      const el = root.getElementById(id);
      if (el) el.onclick = handler;
    };
    on("new-role", () => this._createRole());
    on("save", () => this._saveRole());
    on("clone", () => this._cloneRole());
    on("delete", () => this._deleteRole());
    on("save-bindings", () => this._saveBindings());
    on("load-denials", () => this._loadDenials());
    on("add-rule", () => {
      this._draft.rules.push({ target: "area_ids", ids: [], access: "none" });
      this._mountRules(root.getElementById("rules"), false);
      this._syncRaw();
    });

    const base = root.getElementById("base");
    if (base) {
      base.onchange = () => {
        this._draft.base = base.value;
        this._syncRaw();
      };
    }
  }

  _payload() {
    const root = this.shadowRoot;
    const lines = (id) =>
      root
        .getElementById(id)
        .value.split("\n")
        .map((l) => l.trim())
        .filter(Boolean);
    const { allow, deny } = writeRules(this._draft.base, this._draft.rules);
    return {
      name: root.getElementById("name").value.trim(),
      allow,
      deny,
      tiers: {
        max: root.getElementById("tier").value,
        allow: lines("tier-allow"),
        deny: lines("tier-deny"),
      },
    };
  }

  async _guard(action, done) {
    try {
      this._notice = null;
      await action();
      if (done) this._notice = { kind: "ok", text: done };
    } catch (err) {
      this._notice = { kind: "error", text: err.message || String(err) };
    }
    await this._refresh();
  }

  _createRole() {
    this._guard(async () => {
      const role = await this._call("roles/create", { role: { name: "New role" } });
      this._selected = role.id;
    }, "Role created.");
  }

  _saveRole() {
    this._guard(async () => {
      await this._call("roles/update", {
        role_id: this._selected,
        changes: this._payload(),
      });
    }, "Saved. Anyone with this role is affected immediately.");
  }

  _cloneRole() {
    this._guard(async () => {
      const source = this._roles.find((r) => r.id === this._selected);
      const role = await this._call("roles/create", {
        role: {
          name: `${source.name} copy`,
          allow: source.allow,
          deny: source.deny,
          tiers: source.tiers,
        },
      });
      this._selected = role.id;
    }, "Cloned.");
  }

  _deleteRole() {
    this._guard(async () => {
      await this._call("roles/delete", { role_id: this._selected });
      this._selected = null;
    }, "Role deleted; anyone who had it falls back to their Home Assistant group.");
  }

  _saveBindings() {
    this._guard(async () => {
      const byUser = {};
      for (const box of this.shadowRoot.querySelectorAll("[data-user]")) {
        (byUser[box.dataset.user] ||= []);
        if (box.checked) byUser[box.dataset.user].push(box.value);
      }
      for (const [userId, roleIds] of Object.entries(byUser)) {
        await this._call("bindings/set", { user_id: userId, role_ids: roleIds });
      }
    }, "Assignments saved.");
  }

  async _loadDenials() {
    try {
      this._denials = await this._call("denials/recent", { limit: 100 });
    } catch (err) {
      this._notice = { kind: "error", text: err.message || String(err) };
    }
    this._render();
  }
}

// The module can be fetched more than once in a session -- the URL carries a
// version, so a reload after an upgrade pulls a second copy -- and defining a
// name twice throws.
if (!customElements.get("ha-rbac-panel")) {
  customElements.define("ha-rbac-panel", HaRbacPanel);
}
