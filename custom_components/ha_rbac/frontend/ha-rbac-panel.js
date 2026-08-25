/**
 * Admin panel for RBAC access control.
 *
 * A plain custom element with no build step: Lit is not reliably importable
 * from a custom panel without bundling, and this is small enough not to need it.
 * Home Assistant sets `hass`, `narrow` and `panel` as properties.
 *
 * The chrome and the controls are Home Assistant's own components rather than
 * hand-styled markup. `ha-top-app-bar-fixed` supplies the header, and because
 * its `navigationIcon` slot falls back to `ha-menu-button`, leaving that slot
 * empty is what gives a narrow screen its way back to the sidebar. Those
 * components live in chunks the frontend loads on demand, which is fine: an
 * element already in the DOM upgrades itself the moment its definition lands,
 * and `loadCardHelpers` is awaited first to pull the common ones in.
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
  // Distinct from "control": this is the policy Home Assistant itself treats as
  // unrestricted, and it lets the proxy skip filtering entirely. Collapsing it
  // into "control" would quietly cost a clone of Administrator that fast path.
  { value: "full", label: "Everything, unfiltered" },
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

// Home Assistant's own list of panels that are internal and are kept out of
// user-facing navigation. Nobody is choosing whether a role may open
// "notfound", so they are not offered.
const SYSTEM_PANELS = ["_my_redirect", "notfound", "app"];

// mdiClose, mdiPlus: inlined because @mdi/js is a build-time import.
const ICON_CLOSE =
  "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z";

// Layout only. Anything that decides how a control looks is the component's
// own business, and restating it here is how a panel drifts out of step with
// the rest of Home Assistant.
const STYLES = `
  /* Home Assistant constrains its own settings pages rather than letting
     fields stretch across a wide monitor. */
  .wrap { max-width: 1040px; margin: 0 auto;
          padding: max(16px, var(--safe-area-inset-left)) 16px 64px; }
  .layout { display: grid; grid-template-columns: minmax(200px, 280px) 1fr; gap: 16px; align-items: start; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
  ha-card { margin-bottom: 16px; }
  .card-content { padding: 16px; }
  h2 { margin: 0 0 4px; font-size: var(--ha-font-size-l, 1.15rem);
       font-weight: var(--ha-font-weight-medium, 500); }
  h3 { margin: 28px 0 4px; font-size: var(--ha-font-size-m, .95rem);
       font-weight: var(--ha-font-weight-medium, 500); }
  .hint { color: var(--secondary-text-color); font-size: var(--ha-font-size-s, .85rem);
          margin: 0 0 12px; line-height: 1.45; }
  .field { display: block; margin: 12px 0; }
  ha-input, ha-textarea, ha-select { width: 100%; }
  .tabs { display: flex; gap: 4px; padding: 0 8px; overflow-x: auto; }
  .tabs button {
    background: none; border: 0; cursor: pointer; font: inherit;
    color: var(--app-header-text-color, #fff); opacity: .72;
    padding: 12px 16px; border-bottom: 2px solid transparent; white-space: nowrap;
  }
  .tabs button[aria-selected="true"] { opacity: 1; border-bottom-color: currentColor; }
  /* The picker is a control in its own right, wide enough to hold a list of
     entities, so it gets its own line rather than fighting the dropdowns for
     room. The dropdowns keep the top line, where their labels line up. */
  .rule {
    display: grid; gap: 12px; align-items: start;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 48px;
    grid-template-areas: "target detail remove" "picker picker picker";
    padding: 12px 0 12px 12px;
    border-inline-start: 2px solid transparent;
    border-bottom: 1px solid var(--divider-color);
  }
  .rule:last-child { border-bottom: 0; }
  .rule .f-target { grid-area: target; }
  .rule .f-detail { grid-area: detail; }
  .rule .f-remove { grid-area: remove; justify-self: end; }
  .rule .picker { grid-area: picker; min-width: 0; }
  @media (max-width: 700px) {
    .rule { grid-template-columns: minmax(0, 1fr) 48px;
            grid-template-areas: "target remove" "detail detail" "picker picker"; }
  }
  .rule.deny { border-inline-start-color: var(--error-color); }
  .actions { display: flex; gap: 8px; margin-top: 20px; flex-wrap: wrap; align-items: center; }
  .actions .spacer { flex: 1; }
  ul.roles { list-style: none; margin: 0; padding: 0; }
  ul.roles li {
    padding: 12px; border-radius: var(--ha-border-radius-md, 8px); cursor: pointer;
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }
  ul.roles li:hover { background: var(--secondary-background-color); }
  ul.roles li[aria-selected="true"] {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .badge { font-size: var(--ha-font-size-xs, .68rem); opacity: .8;
           border: 1px solid currentColor; border-radius: 10px;
           padding: 1px 6px; white-space: nowrap; }
  table { width: 100%; border-collapse: collapse; font-size: var(--ha-font-size-m, .9rem); }
  th, td { text-align: start; padding: 12px 8px; vertical-align: top;
           border-bottom: 1px solid var(--divider-color); }
  th { color: var(--secondary-text-color); font-weight: var(--ha-font-weight-medium, 500); }
  code { font-family: var(--ha-font-family-code, monospace); font-size: .82rem; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 700px) { .grid2 { grid-template-columns: 1fr; } }
  .checks { display: grid; gap: 4px 16px; align-items: center;
            grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .checks ha-formfield { display: flex; align-items: center; min-height: 40px; }
  ha-alert { display: block; margin-bottom: 16px; }
  ha-expansion-panel { margin-top: 24px; }
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

/** Read a role's attribute section into editable rows. */
function readAttributeRules(role) {
  const attributes = role.attributes || {};
  const rules = (attributes.rules || []).map((rule) => ({
    target: rule.target || "domains",
    ids: [...(rule.ids || [])],
    names: [...(rule.names || [])],
  }));
  // The original shape withheld names from everything; show it as such.
  if ((attributes.deny || []).length) {
    rules.push({ target: "domains", ids: [], names: [...attributes.deny] });
  }
  return rules;
}

/** Read a policy pair into a base level plus a flat list of exceptions. */
function readRules(role) {
  const allow = role.allow || {};
  const deny = role.deny || {};
  const base =
    allow.entities === true ? "full" : accessOf((allow.entities || {}).all);
  const rules = [];

  const collect = (policy, denying) => {
    for (const target of TARGETS) {
      const entities = policy.entities === true ? {} : policy.entities || {};
      const bucket =
        target.block === "entities" ? entities[target.value] : policy[target.value];
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
  const deny = { entities: {} };
  if (base === "full") {
    // Nothing narrows an unrestricted baseline except a denial, so exceptions
    // that grant are meaningless here and are dropped rather than half-applied.
    const allow = { entities: true };
    for (const rule of rules.filter((r) => r.access === "none")) {
      const target = TARGETS.find((t) => t.value === rule.target);
      if (!target || !rule.ids || !rule.ids.length) continue;
      const holder =
        target.block === "entities"
          ? (deny.entities[target.value] ||= {})
          : (deny[target.value] ||= {});
      for (const id of rule.ids) holder[id] = true;
    }
    if (!Object.keys(deny.entities).length) delete deny.entities;
    return { allow, deny };
  }

  const allow = { entities: {} };
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
    this._narrow = false;
  }

  set narrow(value) {
    this._narrow = value;
    const bar = this.shadowRoot && this.shadowRoot.querySelector("ha-top-app-bar-fixed");
    if (bar) bar.narrow = value;
  }

  get narrow() {
    return this._narrow;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._boot();
    }
  }

  /**
   * Home Assistant loads its components in chunks, and a panel reached by its
   * URL rather than from a dashboard may be the first thing on screen. This
   * pulls the common ones in; anything already rendered upgrades itself.
   */
  async _boot() {
    try {
      if (window.loadCardHelpers) await window.loadCardHelpers();
    } catch (err) {
      // Not fatal: the elements arrive with any other panel the user opens.
    }
    await this._refresh();
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
    this._draft = role
      ? {
          ...role,
          ...readRules(role),
          appDenied: [...((role.apps || {}).deny || [])],
          attrRules: readAttributeRules(role),
        }
      : null;
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>${this._chrome()}`;
    const bar = this.shadowRoot.querySelector("ha-top-app-bar-fixed");
    if (bar) bar.narrow = this._narrow;
    if (this._tab === "roles") this._mountEditor();
    this._applyValues(this.shadowRoot);
    this._wire();
  }

  /** A textarea's text is a property, not the content a `<textarea>` would slot. */
  _applyValues(root) {
    root.querySelectorAll("ha-textarea[data-text]").forEach((area) => {
      const value = area.dataset.text;
      const apply = () => {
        area.value = value;
      };
      apply();
      customElements.whenDefined("ha-textarea").then(apply).catch(() => {});
    });
  }

  _chrome() {
    let banners = "";
    if (this._catalog && this._catalog.degraded) {
      banners += `<ha-alert alert-type="warning">Permission derivation is not
        working on this Home Assistant version, so every request is being denied
        rather than quietly allowed. This usually means an upstream change to how
        <code>require_admin</code> works.</ha-alert>`;
    }
    if (this._notice) {
      const type = this._notice.kind === "ok" ? "success" : this._notice.kind;
      banners += `<ha-alert alert-type="${esc(type)}">${esc(this._notice.text)}</ha-alert>`;
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

    // The navigationIcon slot is deliberately left empty: ha-top-app-bar-fixed
    // falls back to ha-menu-button, which is the sidebar toggle a narrow
    // screen needs and which this panel had no way of offering before.
    return `
      <ha-top-app-bar-fixed>
        <div slot="title">Access Control</div>
        <div slot="subRow" class="tabs">${tabs}</div>
        <div class="wrap">${banners}${body}</div>
      </ha-top-app-bar-fixed>`;
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
        <ha-card>
          <div class="card-content">
            <h2>Roles</h2>
            <ul class="roles">${list}</ul>
            <div class="actions"><ha-button id="new-role">New role</ha-button></div>
          </div>
        </ha-card>
        <ha-card><div class="card-content" id="editor"></div></ha-card>
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
          : "Changes apply as soon as you save. No restart, no reload for the people affected."
      }</p>

      <div class="field">
        <ha-input id="name" label="Name" value="${esc(draft.name)}"
          ${locked ? "disabled" : ""}></ha-input>
      </div>

      <h3>What this role can see</h3>
      <p class="hint">Start from a baseline, then add exceptions. Most roles are
        one line of each: see everything, except the locks.</p>
      <div class="field" id="base-host"></div>

      <h3>Exceptions</h3>
      <div id="rules"></div>
      <div class="actions">
        <ha-button id="add-rule" ${locked ? "disabled" : ""}>Add exception</ha-button>
      </div>

      <h3>Details to withhold</h3>
      <p class="hint">An entity someone can see, they see in full: every
        attribute it reports. Hide the ones you did not mean to share: where
        someone is, an access code, an IP address, a serial number. Rules are
        targeted, so hiding <code>latitude</code> on people leaves the zones
        that define where home is working. Comma separated;
        <code>gps_*</code> matches a group, and an empty picker means every
        entity.</p>
      <div id="attr-rules"></div>
      <div class="actions">
        <ha-button id="add-attr" ${locked ? "disabled" : ""}>Hide an attribute</ha-button>
      </div>

      <h3>Where this role can go</h3>
      <p class="hint">Every dashboard, add-on and built-in screen in the sidebar.
        Unticking one hides it and refuses the requests behind it.</p>
      <div class="checks" id="apps">${this._visibleApps()
        .map(
          (app) => `<ha-formfield label="${esc(app.label)}${
            app.addon ? " (add-on)" : ""
          }">
            <ha-checkbox data-app="${esc(app.url_path)}"
              ${draft.appDenied.includes(app.url_path) ? "" : "checked"}
              ${locked ? "disabled" : ""}></ha-checkbox>
          </ha-formfield>`
        )
        .join("")}</div>

      <h3>What this role can do</h3>
      <p class="hint">Administrative commands are recognised from Home Assistant's
        own markings, read on this instance,
        ${this._catalog ? this._catalog.commands.length : 0} commands,
        nothing hard-coded.</p>
      <div class="field" id="tier-host"></div>

      <ha-expansion-panel header="Advanced">
        <div class="card-content">
          <p class="hint">Overrides by command name, and the raw policy this role
            stores. Anything the exception list above cannot express lives here.</p>
          <div class="grid2">
            <ha-textarea id="tier-allow" label="Always allow (one pattern per line)"
              data-text="${esc(((draft.tiers || {}).allow || []).join("\n"))}"
              ${locked ? "disabled" : ""}></ha-textarea>
            <ha-textarea id="tier-deny" label="Always deny (one pattern per line)"
              data-text="${esc(((draft.tiers || {}).deny || []).join("\n"))}"
              ${locked ? "disabled" : ""}></ha-textarea>
          </div>
          <ha-textarea id="raw" label="Stored policy" readonly
            data-text="${esc(JSON.stringify(writeRules(draft.base, draft.rules), null, 2))}"
          ></ha-textarea>
        </div>
      </ha-expansion-panel>

      <div class="actions">
        <ha-button id="save" ${locked ? "disabled" : ""}>Save</ha-button>
        <ha-button id="clone">Clone</ha-button>
        <span class="spacer"></span>
        <ha-button id="delete" ${locked ? "disabled" : ""}>Delete</ha-button>
      </div>`;

    this._applyValues(host);

    host.querySelector("#base-host").appendChild(
      this._select(BASE, draft.base, locked, "Baseline", (value) => {
        this._draft.base = value;
        this._syncRaw();
      })
    );
    host.querySelector("#tier-host").appendChild(
      this._select(
        TIERS.map((t) => ({
          value: t,
          label:
            t === "admin"
              ? "Everything, including settings and configuration"
              : "Ordinary use only",
        })),
        (draft.tiers || {}).max,
        locked,
        "Command level",
        () => {},
        "tier"
      )
    );

    this._mountRules(host.querySelector("#rules"), locked);
    this._mountAttrRules(host.querySelector("#attr-rules"), locked);
  }

  _mountRules(host, locked) {
    host.innerHTML = "";
    if (!this._draft.rules.length) {
      host.innerHTML = `<p class="hint">No exceptions. The baseline applies to everything.</p>`;
      return;
    }
    this._draft.rules.forEach((rule, index) => {
      host.appendChild(this._ruleRow(rule, index, locked));
    });
  }

  _mountAttrRules(host, locked) {
    host.innerHTML = "";
    if (!this._draft.attrRules.length) {
      host.innerHTML = `<p class="hint">Nothing hidden. Entities this role can
        see, it sees in full.</p>`;
      return;
    }
    this._draft.attrRules.forEach((rule, index) => {
      host.appendChild(this._attrRow(rule, index, locked));
    });
  }

  /**
   * `ha-select` renders its own items from an `options` property. Slotted
   * children are ignored unless they are `ha-dropdown-item`s, which is why a
   * list of `ha-list-item`s showed the raw value and selected nothing.
   */
  _select(options, value, locked, label, onChange, id) {
    const el = document.createElement("ha-select");
    if (id) el.id = id;
    el.label = label;
    el.options = options.map((o) => ({ value: o.value, label: o.label }));
    el.value = value;
    el.disabled = locked;
    el.addEventListener("selected", (event) => {
      event.stopPropagation();
      const next = event.detail ? event.detail.value : el.value;
      if (next === undefined || next === null) return;
      onChange(next);
    });
    return el;
  }

  _removeButton(locked, onClick) {
    const button = document.createElement("ha-icon-button");
    button.label = "Remove";
    button.path = ICON_CLOSE;
    button.disabled = locked;
    button.addEventListener("click", onClick);
    return button;
  }

  _attrRow(rule, index, locked) {
    const row = document.createElement("div");
    row.className = "rule deny";

    const target = this._select(TARGETS, rule.target, locked, "Applies to", (value) => {
      rule.target = value;
      rule.ids = [];
      this._mountAttrRules(this.shadowRoot.getElementById("attr-rules"), locked);
    });

    const picker = document.createElement("div");
    picker.className = "picker";
    picker.appendChild(this._pickerFor(rule, locked));

    const names = document.createElement("ha-input");
    names.label = "Attributes";
    names.value = rule.names.join(", ");
    names.placeholder = "latitude, longitude, gps_*";
    names.disabled = locked;
    names.addEventListener("change", () => {
      rule.names = names.value.split(",").map((n) => n.trim()).filter(Boolean);
    });

    const remove = this._removeButton(locked, () => {
      this._draft.attrRules.splice(index, 1);
      this._mountAttrRules(this.shadowRoot.getElementById("attr-rules"), locked);
    });

    target.classList.add("f-target");
    names.classList.add("f-detail");
    remove.classList.add("f-remove");
    row.append(target, picker, names, remove);
    return row;
  }

  _ruleRow(rule, index, locked) {
    const row = document.createElement("div");
    row.className = `rule${rule.access === "none" ? " deny" : ""}`;

    const target = this._select(TARGETS, rule.target, locked, "Applies to", (value) => {
      rule.target = value;
      rule.ids = [];
      this._mountRules(this.shadowRoot.getElementById("rules"), locked);
      this._syncRaw();
    });

    const picker = document.createElement("div");
    picker.className = "picker";
    picker.appendChild(this._pickerFor(rule, locked));

    const access = this._select(ACCESS, rule.access, locked, "Access", (value) => {
      rule.access = value;
      row.className = `rule${rule.access === "none" ? " deny" : ""}`;
      this._syncRaw();
    });

    const remove = this._removeButton(locked, () => {
      this._draft.rules.splice(index, 1);
      this._mountRules(this.shadowRoot.getElementById("rules"), locked);
      this._syncRaw();
    });

    target.classList.add("f-target");
    access.classList.add("f-detail");
    remove.classList.add("f-remove");
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
        options: [...new Set(Object.keys(this._hass.states).map((e) => e.split(".")[0]))]
          .sort()
          .map((d) => ({ value: d, label: d })),
      },
    };

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
            (role) => `<ha-formfield label="${esc(role.name)}">
              <ha-checkbox data-user="${esc(user.user_id)}"
                value="${esc(role.id)}"
                ${user.role_ids.includes(role.id) ? "checked" : ""}
                ${user.is_owner ? "disabled" : ""}></ha-checkbox>
            </ha-formfield>`
          )
          .join("");
        const note = user.is_owner
          ? '<span class="badge">owner, always unrestricted</span>'
          : "";
        const fallback = user.role_ids.length
          ? ""
          : `<div class="hint" style="margin:6px 0 0">Unassigned, so they keep the
             access Home Assistant already gives them (${user.is_admin ? "Administrator" : "User"}).</div>`;
        return `<tr>
          <td>${esc(user.name || user.user_id)} ${note}</td>
          <td><div class="checks">${boxes}</div>${fallback}</td>
        </tr>`;
      })
      .join("");

    return `<ha-card>
      <div class="card-content">
        <h2>Users</h2>
        <p class="hint">Anyone unassigned keeps exactly the
          access Home Assistant already gives them, so you can roll this out one
          person at a time. The owner is always unrestricted, and that is the way
          back in if a role locks you out.</p>
        <table><thead><tr><th>Person</th><th>Role</th></tr></thead><tbody>${rows}</tbody></table>
        <div class="actions"><ha-button id="save-bindings">Save</ha-button></div>
      </div>
    </ha-card>`;
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

    return `<ha-card>
      <div class="card-content">
        <h2>Recent denials</h2>
        <p class="hint">A refused request reaches the person as a screen that quietly
          does less, with no explanation. This is where to look when someone says
          something stopped working.</p>
        <div class="actions" style="margin-top:0">
          <ha-button id="load-denials">Refresh</ha-button>
        </div>
        <table>
          <thead><tr><th>Person</th><th>Request</th><th>Why</th><th>Entities</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="4" class="hint">Nothing refused yet.</td></tr>'}</tbody>
        </table>
      </div>
    </ha-card>`;
  }

  /**
   * Built-in panels carry a translation key in `title` rather than a name, so
   * the sidebar reads it through `localize`. Two of them key off `url_path`
   * instead. Dashboards and add-ons have a real title and fall through.
   */
  _appTitle(app) {
    const key =
      app.url_path === "profile" || app.url_path === "notfound"
        ? `panel.${app.url_path}`
        : `panel.${app.title}`;
    const localize = this._hass && this._hass.localize;
    return (localize && localize(key)) || app.title || app.url_path;
  }

  _visibleApps() {
    return (this._catalog ? this._catalog.apps : [])
      .filter((app) => !SYSTEM_PANELS.includes(app.url_path))
      .map((app) => ({ ...app, label: this._appTitle(app) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  _reason(reason) {
    return (
      {
        tier: "Not allowed to use that command",
        resource: "No access to that entity",
        unbounded: "Request did not say what it would touch",
        degraded: "Permission derivation unavailable",
        app: "No access to that dashboard, add-on or screen",
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
      if (el) el.addEventListener("click", handler);
    };
    on("new-role", () => this._createRole());
    on("save", () => this._saveRole());
    on("clone", () => this._cloneRole());
    on("delete", () => this._deleteRole());
    on("save-bindings", () => this._saveBindings());
    on("load-denials", () => this._loadDenials());
    on("add-attr", () => {
      this._draft.attrRules.push({ target: "domains", ids: [], names: [] });
      this._mountAttrRules(root.getElementById("attr-rules"), false);
    });
    on("add-rule", () => {
      this._draft.rules.push({ target: "area_ids", ids: [], access: "none" });
      this._mountRules(root.getElementById("rules"), false);
      this._syncRaw();
    });

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
      attributes: {
        deny: [],
        rules: this._draft.attrRules
          .filter((rule) => rule.names.length)
          .map((rule) => ({
            target: rule.target,
            ids: rule.ids,
            names: rule.names,
          })),
      },
      apps: {
        allow: [],
        // System panels are not offered, so they have no checkbox. Carry any
        // denial they already had rather than silently granting it back.
        deny: [
          ...this._draft.appDenied.filter((path) => SYSTEM_PANELS.includes(path)),
          ...[...root.querySelectorAll("[data-app]")]
            .filter((box) => !box.checked)
            // A checkbox with no value attribute reports "on", so read the
            // dataset rather than falling back to it.
            .map((box) => box.dataset.app),
        ],
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
          apps: source.apps,
          attributes: source.attributes,
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
        if (box.checked) byUser[box.dataset.user].push(box.getAttribute("value"));
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
