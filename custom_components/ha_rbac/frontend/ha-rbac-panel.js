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
  { value: "none", label: "No access" },
  { value: "read", label: "Read" },
  { value: "control", label: "Read and control" },
  // Distinct from "control": this is the policy Home Assistant itself treats as
  // unrestricted, and it lets the proxy skip filtering entirely. Collapsing it
  // into "control" would quietly cost a clone of Administrator that fast path.
  { value: "full", label: "Unfiltered" },
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


// Home Assistant's own list of panels that are internal and are kept out of
// user-facing navigation. Nobody is choosing whether a role may open
// "notfound", so they are not offered.
const SYSTEM_PANELS = ["_my_redirect", "notfound", "app"];

// The panel is mounted here, and everything after it is ours to route on.
const PANEL_PATH = "/rbac";
const TABS = ["roles", "users", "denials"];

const DAYS = [
  { value: "mon", label: "Mon" },
  { value: "tue", label: "Tue" },
  { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" },
  { value: "fri", label: "Fri" },
  { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];

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
  ha-input, ha-select, ha-selector { display: block; width: 100%; }
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
  /* What happens to everything the rows below do not name. Deliberately looks
     like a row and deliberately is not one: a setting shown where it takes
     effect beats one inferred from a control further up the page. */
  /* Same grid as a rule row, so the access it sets sits in the same column as
     the access on every row that overrides it. */
  .fallback { display: grid; gap: 12px; align-items: center;
              grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 48px;
              padding: 12px 0 12px 12px;
              border-inline-start: 2px dashed var(--divider-color);
              border-bottom: 1px solid var(--divider-color);
              color: var(--secondary-text-color); }
  .fallback .what { min-width: 0; }
  .fallback .value { font-weight: 500; color: var(--primary-text-color); }
  .fallback .note { display: block; margin-top: 2px;
                    font-size: var(--ha-font-size-s, .85rem); }
  .fallback ha-select { width: 100%; }
  @media (max-width: 700px) {
    .fallback { grid-template-columns: minmax(0, 1fr); }
  }
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
  /* The checkbox sits beside a single text column holding both the title and
     the description, so the two line up by construction. Offsetting the
     description by a guess at the checkbox's width did not: the label's real
     offset comes from ha-formfield, and the two numbers were never equal. */
  .cap { display: flex; align-items: flex-start; gap: 4px; }
  .cap-text { min-width: 0; }
  /* No invented heights: the box and the first line of text are both about
     20px, so flex-start lines their centres up on its own. Forcing the title
     into a 40px line box put its centre 10px below the box's. */
  .cap-title { cursor: pointer; }
  .cap .hint { margin: 2px 0 12px; }
  .cap.indent { margin-left: 28px; }
  /* The recording offer is part of the role, not page furniture, so it sits in
     the form with a border of its own rather than loose above the title. */
  .record { display: flex; align-items: center; gap: 16px; margin: 4px 0 20px;
            padding: 12px 16px; border: 1px solid var(--divider-color, #e0e0e0);
            border-radius: 12px; background: var(--card-background-color); }
  .record-text { flex: 1; min-width: 0; }
  .record-text strong { display: flex; align-items: center; gap: 8px; }
  .record .hint { margin: 2px 0 0; }
  .record-actions { display: flex; align-items: center; gap: 4px;
                    flex-wrap: wrap; justify-content: flex-end; }
  .record.recording { border-color: var(--warning-color, #ffa726);
                      background: color-mix(in srgb,
                                  var(--warning-color, #ffa726) 8%,
                                  var(--card-background-color)); }
  .record .seen { margin: 8px 0 0; font-size: var(--ha-font-size-s, .85rem); }
  .record .dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto;
                 background: var(--warning-color, #ffa726);
                 animation: rbac-pulse 1.8s ease-in-out infinite; }
  .record ha-button.quiet { --mdc-theme-primary: var(--secondary-text-color); }
  @keyframes rbac-pulse { 50% { opacity: .25; } }
  @media (prefers-reduced-motion: reduce) { .record .dot { animation: none; } }
  @media (max-width: 600px) {
    .record { flex-direction: column; align-items: stretch; }
    .record-actions { justify-content: flex-start; }
  }
  /* Seven three-letter labels do not need the width an app name does. */
  /* A time picker is hh:mm, a meridiem dropdown and a clear button, which is
     wider than it looks; four controls abreast clipped it. Days take the first
     line with the remove button, the two times share the second. */
  .srule {
    display: grid; gap: 12px 16px; align-items: start;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 48px;
    grid-template-areas: "days days remove" "from until .";
    padding: 12px 0 12px 12px;
    border-inline-start: 2px solid var(--divider-color);
    border-bottom: 1px solid var(--divider-color);
    margin-bottom: 16px;
  }
  .srule:last-of-type { border-bottom: 0; }
  @media (max-width: 700px) {
    .srule { grid-template-columns: minmax(0, 1fr) 48px;
             grid-template-areas: "days remove" "from from" "until until"; }
  }
  .srule .f-days { grid-area: days; }
  .srule .f-from { grid-area: from; }
  .srule .f-until { grid-area: until; }
  .srule .f-remove { grid-area: remove; justify-self: end; }
  ha-alert { display: block; margin-bottom: 16px; }
  ha-expansion-panel { margin-top: 24px; }
`;

/** Read the open tab and role back out of the address bar. */
function readUrl() {
  const parts = window.location.pathname
    .slice(PANEL_PATH.length)
    .split("/")
    .filter(Boolean);
  const tab = TABS.includes(parts[0]) ? parts[0] : "roles";
  return {
    tab,
    role: tab === "roles" && parts[1] ? decodeURIComponent(parts[1]) : null,
  };
}

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

/** Read a role's schedule into editable rows, whichever shape it is stored in. */
function readSchedule(role) {
  const schedule = role.schedule || {};
  const rules = (schedule.rules || []).map((rule) => ({
    days: [...(rule.days || [])],
    start: rule.start || "",
    end: rule.end || "",
  }));
  // The original shape held one window inline. Show it as the first row.
  if ((schedule.days || []).length || schedule.start || schedule.end) {
    rules.unshift({
      days: [...(schedule.days || [])],
      start: schedule.start || "",
      end: schedule.end || "",
    });
  }
  return rules;
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
    const opened = readUrl();
    this._tab = opened.tab;
    this._urlRole = opened.role;
    this._roles = [];
    this._bindings = [];
    this._denials = [];
    this._catalog = null;
    this._recording = {};
    this._selected = null;
    this._draft = null;
    this._notice = null;
    // Set by an action that edited the draft rather than the stored role.
    this._pending = null;
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

  connectedCallback() {
    this._onPopState = () => {
      const opened = readUrl();
      this._tab = opened.tab;
      if (opened.role && opened.role !== this._selected) {
        this._selected = opened.role;
        this._loadDraft();
      }
      this._render();
    };
    window.addEventListener("popstate", this._onPopState);
  }

  disconnectedCallback() {
    if (this._onPopState) window.removeEventListener("popstate", this._onPopState);
  }

  /**
   * Put the open tab and role in the address bar.
   *
   * `location-changed` is how a custom panel tells Home Assistant's router that
   * the address moved under it; pushing state without it leaves the two
   * disagreeing about where you are.
   */
  _syncUrl(replace = false) {
    const path =
      this._tab === "roles" && this._selected
        ? `${PANEL_PATH}/roles/${encodeURIComponent(this._selected)}`
        : `${PANEL_PATH}/${this._tab}`;
    if (window.location.pathname === path) return;
    window.history[replace ? "replaceState" : "pushState"](null, "", path);
    this.dispatchEvent(
      new CustomEvent("location-changed", { bubbles: true, composed: true })
    );
  }

  _call(type, extra = {}) {
    return this._hass.callWS({ type: `ha_rbac/${type}`, ...extra });
  }

  async _refresh(keepDraft = false) {
    try {
      const [roles, bindings, catalog, recording] = await Promise.all([
        this._call("roles/list"),
        this._call("bindings/list"),
        this._call("catalog"),
        this._call("record/status"),
      ]);
      this._roles = roles;
      this._bindings = bindings;
      this._catalog = catalog;
      this._recording = recording || {};
      if (this._urlRole && roles.some((role) => role.id === this._urlRole)) {
        // A link or a reload asked for this one.
        this._selected = this._urlRole;
      }
      this._urlRole = null;
      if (!this._selected && roles.length) this._selected = roles[0].id;
      this._syncUrl(true);
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
          dashboardLevels: { ...((role.apps || {}).dashboards || {}) },
          schedule: readSchedule(role),
          capabilities: [...(role.capabilities || [])],
          tierAllow: [...((role.tiers || {}).allow || [])],
          tierDeny: [...((role.tiers || {}).deny || [])],
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
    this._wire();
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

      <div id="record-host"></div>

      <h3>Entities: what this role can see</h3>
      <p class="hint">The first row is where every entity starts. Add rows below
        it to override that for particular areas, domains, labels, floors or
        devices — most roles are one of each: see everything, except the locks.
        This covers entities only; dashboards, settings and hours are their own
        sections further down.</p>
      <div id="sees-nothing"></div>
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
        Unticking one hides it and refuses the requests behind it. This decides
        which screens they can reach, not what appears on them: a dashboard they
        can open still shows only the entities they are allowed above.</p>
      <div class="checks" id="apps">${this._visibleApps()
        .filter((app) => app.kind !== "lovelace")
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

      <h3>Dashboards</h3>
      <p class="hint">A dashboard can be opened empty, which shows only what the
        role is allowed elsewhere, or it can carry its own contents with it.
        Those are read from the dashboard whenever a request is judged, so
        editing a dashboard changes what its holders see without anyone
        reopening the role. A denial elsewhere still wins.</p>
      <table id="dashboards">
        <thead><tr>
          <th>Dashboard</th><th>Can open</th><th>Sees what is on it</th><th>Can control it</th>
        </tr></thead>
        <tbody>${this._visibleApps()
          .filter((app) => app.kind === "lovelace")
          .map((app) => {
            const denied = draft.appDenied.includes(app.url_path);
            const level = draft.dashboardLevels[app.url_path] || "empty";
            const box = (which, on) =>
              `<ha-checkbox data-dash="${esc(app.url_path)}" data-level="${which}"
                 ${on ? "checked" : ""} ${locked ? "disabled" : ""}></ha-checkbox>`;
            return `<tr>
              <td>${esc(app.label)}</td>
              <td>${box("open", !denied)}</td>
              <td>${box("content", !denied && level !== "empty")}</td>
              <td>${box("control", !denied && level === "control")}</td>
            </tr>`;
          })
          .join("")}</tbody>
      </table>
      <div class="actions">
        <ha-button id="refresh-dashboards">Re-read the dashboards</ha-button>
      </div>

      <h3>What this role can do</h3>
      <p class="hint">Unticked, this role can live in the house but not
        administer it: it reads and controls whatever it is allowed above and
        reaches no settings at all. Tick a part of the settings to hand that
        over, without granting the rest.</p>
      <div class="cap">
        <ha-checkbox id="tier-admin"
          ${(draft.tiers || {}).max === "admin" ? "checked" : ""}
          ${locked ? "disabled" : ""}></ha-checkbox>
        <div class="cap-text">
          <div class="cap-title">All settings</div>
          <p class="hint">Everything below, and anything Home Assistant adds
            later. A full administrator.</p>
        </div>
      </div>
      <div id="caps">${(this._catalog ? this._catalog.capabilities || [] : [])
        .map(
          (cap) => `<div class="cap indent">
            <ha-checkbox data-cap="${esc(cap.id)}"
              ${(draft.capabilities || []).includes(cap.id) ? "checked" : ""}
              ${locked ? "disabled" : ""}></ha-checkbox>
            <div class="cap-text">
              <div class="cap-title">${esc(cap.title)}</div>
              <p class="hint">${esc(cap.description)}</p>
            </div>
          </div>`
        )
        .join("")}</div>
      <ha-alert alert-type="warning">Automations, scripts and scenes run with no
        user context, exactly as they do in stock Home Assistant. Someone who can
        write one can make it do anything, whatever their role allows directly.
        These are a statement that you trust the person, not a way to contain
        them.</ha-alert>

      <h3>When this role applies</h3>
      <p class="hint">Leave this empty and the role is always in force. Add a
        window and it applies only inside it; add several and any one of them is
        enough, so "Monday and Tuesday, 10:00 to 12:00 and 15:00 to 19:00" is two
        rows. Outside them the person holds no role at all, which means no access
        rather than their old access. An end before the start runs through
        midnight, so 22:00 to 06:00 is the night.</p>
      <div id="schedule"></div>
      <div class="actions">
        <ha-button id="add-window" ${locked ? "disabled" : ""}>Add a window</ha-button>
      </div>

      <ha-expansion-panel header="Advanced">
        <div class="card-content">
          <p class="hint">Overrides by command name, and the raw policy this role
            stores. Anything the exception list above cannot express lives here.</p>
          <div class="grid2">
            <div id="adv-allow"></div>
            <div id="adv-deny"></div>
          </div>
          <div class="field" id="adv-raw"></div>
        </div>
      </ha-expansion-panel>

      <div class="actions">
        <ha-button id="save" ${locked ? "disabled" : ""}>Save</ha-button>
        <ha-button id="clone">Clone</ha-button>
        <span class="spacer"></span>
        <ha-button id="delete" ${locked ? "disabled" : ""}>Delete</ha-button>
      </div>`;

    host.querySelector("#record-host").innerHTML = this._recordSection(locked);

    host.querySelector("#adv-allow").appendChild(
      this._multiline(draft.tierAllow.join("\n"), "Always allow (one pattern per line)", locked, (value) => {
        this._draft.tierAllow = value.split("\n").map((l) => l.trim()).filter(Boolean);
      })
    );
    host.querySelector("#adv-deny").appendChild(
      this._multiline(draft.tierDeny.join("\n"), "Always deny (one pattern per line)", locked, (value) => {
        this._draft.tierDeny = value.split("\n").map((l) => l.trim()).filter(Boolean);
      })
    );
    const raw = this._multiline(
      JSON.stringify(writeRules(draft.base, draft.rules), null, 2),
      "Stored policy",
      true,
      () => {}
    );
    raw.id = "raw";
    host.querySelector("#adv-raw").appendChild(raw);

    host.querySelector("#tier-admin").addEventListener("change", () =>
      this._refreshCapabilities()
    );
    host.querySelectorAll(".cap").forEach((row) => {
      const box = row.querySelector("ha-checkbox");
      row.querySelector(".cap-title").addEventListener("click", () => {
        if (box.disabled) return;
        box.checked = !box.checked;
        box.dispatchEvent(new Event("change"));
      });
    });
    host.querySelectorAll("[data-cap]").forEach((box) => {
      box.addEventListener("change", () => {
        const chosen = new Set(this._draft.capabilities || []);
        if (box.checked) chosen.add(box.dataset.cap);
        else chosen.delete(box.dataset.cap);
        this._draft.capabilities = [...chosen];
      });
    });
    this._refreshCapabilities();

    this._wireDashboards(locked);
    this._mountSchedule(host.querySelector("#schedule"), locked);
    this._mountRules(host.querySelector("#rules"), locked);
    this._mountAttrRules(host.querySelector("#attr-rules"), locked);
  }

  /**
   * How much of the settings a role reaches is one question, so it is one
   * list: "All settings" over the parts it covers. It began as a dropdown
   * beside the list, which read as two controls that could contradict each
   * other, and then as a row that hid the rest, which lost sight of what had
   * been picked. Ticking the parent now ticks the children, which is what a
   * parent checkbox is expected to do.
   */
  _refreshCapabilities() {
    const root = this.shadowRoot;
    const everything = root.getElementById("tier-admin");
    if (!everything) return;
    const locked = (this._draft || {}).system_generated;
    const chosen = this._draft.capabilities || [];
    root.querySelectorAll("[data-cap]").forEach((box) => {
      // While "All settings" is on, the children show what it covers rather
      // than what was picked. The pick itself is held on the draft, so turning
      // it off again puts the ticks back where they were.
      box.checked = everything.checked || chosen.includes(box.dataset.cap);
      box.disabled = locked || everything.checked;
    });
  }

  /**
   * Writing a role blind means restricting something and finding out days later
   * that a dashboard is empty. This turns it round: hand the role out, watch
   * what its holder actually reaches for, and keep that.
   *
   * The banner is deliberately hard to ignore. While it runs, everyone holding
   * the role has full access.
   */
  _recordSection(locked) {
    if (locked) return "";
    const live = this._recording[this._selected];
    if (!live) {
      return `<div class="record">
        <div class="record-text">
          <strong>Not sure what to allow?</strong>
          <p class="hint">Hand the role out and let them use Home Assistant
            normally for a few minutes. Everything they touch is added here when
            you stop.</p>
        </div>
        <ha-button id="record-start">Record</ha-button>
      </div>`;
    }
    const counts = [
      [live.entities ? Object.keys(live.entities).length : 0, "entity", "entities"],
      [live.apps ? live.apps.length : 0, "app", "apps"],
      [live.capabilities ? live.capabilities.length : 0, "setting", "settings"],
    ]
      .filter(([n]) => n)
      .map(([n, one, many]) => `${n} ${n === 1 ? one : many}`)
      .join(", ");
    return `<div class="record recording">
        <div class="record-text">
          <strong><span class="dot"></span>Recording</strong>
          <p class="hint">Everyone holding this role has full access until you
            stop, and nothing is enforced for them.</p>
          <p class="seen">Seen so far: ${esc(counts || "nothing yet")}</p>
        </div>
        <div class="record-actions">
          <ha-button id="record-stop">Stop and keep</ha-button>
          <ha-button id="record-discard" class="quiet">Discard</ha-button>
        </div>
      </div>`;
  }

  async _record(action) {
    let message = "";
    await this._guard(
      async () => {
        if (action === "start") {
          await this._call("record/start", { role_id: this._selected });
          message = "Recording. Everyone holding this role is unrestricted.";
          return;
        }
        const result = await this._call("record/stop", {
          role_id: this._selected,
          apply: action === "keep",
        });
        const seen = result.seen || {};
        const count = seen.entities ? Object.keys(seen.entities).length : 0;
        message = result.applied
          ? `Added ${count} ${count === 1 ? "entity" : "entities"} to this role.`
          : "Recording discarded. The role is unchanged.";
      },
      () => message
    );
  }

  /**
   * A role with no baseline and no granting exception can open every dashboard
   * and find all of them empty, because the app list decides which screens
   * exist and this decides what is on them. That is the first thing people get
   * caught by, so it says so rather than leaving them to work it out.
   */
  _refreshSeesNothing() {
    const host = this.shadowRoot.getElementById("sees-nothing");
    if (!host) return;
    const draft = this._draft;
    const grants =
      draft.base !== "none" ||
      draft.rules.some((rule) => rule.access !== "none" && rule.ids.length) ||
      Object.keys(draft.dashboardLevels).length > 0;
    host.innerHTML = grants
      ? ""
      : `<ha-alert alert-type="info">This role can see no entities, so its
         dashboards will open but come up empty. Add an exception below to let
         it see something. Ticking a dashboard under "Where this role can go"
         decides whether the screen is reachable, not what is on it.</ha-alert>`;
  }

  /**
   * The rule the rows below are exceptions to. It sits first because that is
   * the order it is read in -- start here, then override -- which is also why
   * it says "every entity" rather than "every other": at the top, "other" has
   * nothing to be other than yet.
   */
  _fallbackRow(what, value, note, id) {
    const row = document.createElement("div");
    row.className = "fallback";
    if (id) row.id = id;
    row.innerHTML = `<span class="what">${esc(what)}${
      note ? `<span class="note">${esc(note)}</span>` : ""
    }</span>
      <span class="value">${esc(value)}</span>`;
    return row;
  }

  /** The row every other row is an exception to, and where its value is set. */
  _baselineRow(locked) {
    const row = this._fallbackRow(
      "Every entity",
      "",
      "Unless a row below says otherwise",
      "baseline-row"
    );
    row.querySelector(".value").remove();
    row.appendChild(
      this._select(BASE, this._draft.base, locked, "Access", (value) => {
        this._draft.base = value;
        this._refreshSeesNothing();
        this._syncRaw();
      })
    );
    return row;
  }

  _mountRules(host, locked) {
    host.innerHTML = "";
    this._refreshSeesNothing();
    host.appendChild(this._baselineRow(locked));
    this._draft.rules.forEach((rule, index) => {
      host.appendChild(this._ruleRow(rule, index, locked));
    });
  }

  _mountSchedule(host, locked) {
    host.innerHTML = "";
    if (!this._draft.schedule.length) {
      host.appendChild(
        this._fallbackRow("Every hour of every day", "In force")
      );
      return;
    }
    this._draft.schedule.forEach((window, index) => {
      host.appendChild(this._windowRow(window, index, locked));
    });
  }

  _windowRow(window, index, locked) {
    const row = document.createElement("div");
    row.className = "srule";

    const days = document.createElement("ha-selector");
    days.hass = this._hass;
    days.selector = { select: { multiple: true, options: DAYS } };
    days.label = "Days (any if empty)";
    days.required = false;
    days.value = window.days.slice();
    days.disabled = locked;
    days.addEventListener("value-changed", (event) => {
      event.stopPropagation();
      const value = event.detail.value;
      window.days = Array.isArray(value) ? value : value ? [value] : [];
    });

    const from = this._time(window.start, "From", locked, (value) => {
      window.start = value;
    });
    const until = this._time(window.end, "Until", locked, (value) => {
      window.end = value;
    });
    const remove = this._removeButton(locked, () => {
      this._draft.schedule.splice(index, 1);
      this._mountSchedule(this.shadowRoot.getElementById("schedule"), locked);
    });

    days.classList.add("f-days");
    from.classList.add("f-from");
    until.classList.add("f-until");
    remove.classList.add("f-remove");
    row.append(days, from, until, remove);
    return row;
  }

  _mountAttrRules(host, locked) {
    host.innerHTML = "";
    host.appendChild(
      this._fallbackRow(
        "Every detail",
        "Shown in full",
        "Unless a rule below hides it"
      )
    );
    this._draft.attrRules.forEach((rule, index) => {
      host.appendChild(this._attrRow(rule, index, locked));
    });
  }

  /**
   * `ha-select` renders its own items from an `options` property. Slotted
   * children are ignored unless they are `ha-dropdown-item`s, which is why a
   * list of `ha-list-item`s showed the raw value and selected nothing.
   *
   * It is also controlled: choosing an item announces the new value but does
   * not adopt it, so the value has to be written back. Without that the draft
   * changed while the box went on showing the old choice -- which reads as a
   * broken dropdown, and is worse than one, because saving then applied a
   * setting the screen said was not selected.
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
      el.value = next;
      onChange(next);
    });
    return el;
  }

  /** A clock time, stored as HH:MM. */
  _time(value, label, disabled, onChange) {
    const el = document.createElement("ha-selector");
    el.hass = this._hass;
    el.selector = { time: { no_second: true } };
    el.label = label;
    el.required = false;
    el.value = value || undefined;
    el.disabled = disabled;
    el.addEventListener("value-changed", (event) => {
      event.stopPropagation();
      // The selector hands back HH:MM:SS; the seconds are noise here.
      const next = event.detail.value;
      onChange(next ? String(next).split(":").slice(0, 2).join(":") : "");
    });
    return el;
  }

  /** A multi-line field, rendered through the selector Home Assistant uses. */
  _multiline(value, label, disabled, onChange) {
    const el = document.createElement("ha-selector");
    el.hass = this._hass;
    el.selector = { text: { multiline: true } };
    el.label = label;
    // The selector marks its field required by default, which puts a "*" on a
    // label for something that is perfectly fine left empty.
    el.required = false;
    el.value = value;
    el.disabled = disabled;
    el.addEventListener("value-changed", (event) => {
      event.stopPropagation();
      onChange(event.detail.value == null ? "" : String(event.detail.value));
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
      this._refreshSeesNothing();
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
      this._refreshSeesNothing();
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
        this._syncUrl();
        if (this._tab === "denials") this._loadDenials();
        else this._render();
      };
    });
    root.querySelectorAll("[data-role]").forEach((item) => {
      item.onclick = () => {
        this._selected = item.dataset.role;
        this._syncUrl();
        this._loadDraft();
        this._render();
      };
    });

    const on = (id, handler) => {
      const el = root.getElementById(id);
      if (el) el.addEventListener("click", handler);
    };
    on("new-role", () => this._createRole());
    on("record-start", () => this._record("start"));
    on("record-stop", () => this._record("keep"));
    on("record-discard", () => this._record("discard"));
    on("save", () => this._saveRole());
    on("clone", () => this._cloneRole());
    on("delete", () => this._deleteRole());
    on("save-bindings", () => this._saveBindings());
    on("load-denials", () => this._loadDenials());
    on("refresh-dashboards", () => this._refreshDashboards());
    on("add-window", () => {
      this._draft.schedule.push({ days: [], start: "", end: "" });
      this._mountSchedule(root.getElementById("schedule"), false);
    });
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

  /**
   * The three boxes describe one level, not three independent ones, so ticking
   * a deeper one implies the shallower and unticking a shallower one drops
   * what it carried.
   */
  _wireDashboards(locked) {
    if (locked) return;
    const root = this.shadowRoot;
    for (const box of root.querySelectorAll("[data-dash]")) {
      box.addEventListener("change", () => {
        const path = box.dataset.dash;
        const row = [...root.querySelectorAll(`[data-dash="${path}"]`)];
        const at = (which) => row.find((b) => b.dataset.level === which);
        const on = box.checked;
        const which = box.dataset.level;

        if (which === "open" && !on) {
          at("content").checked = false;
          at("control").checked = false;
        } else if (which === "content") {
          if (on) at("open").checked = true;
          else at("control").checked = false;
        } else if (which === "control" && on) {
          at("open").checked = true;
          at("content").checked = true;
        }

        const denied = !at("open").checked;
        const level = at("control").checked
          ? "control"
          : at("content").checked
            ? "content"
            : "empty";
        this._draft.appDenied = this._draft.appDenied.filter((p) => p !== path);
        if (denied) this._draft.appDenied.push(path);
        if (level === "empty") delete this._draft.dashboardLevels[path];
        else this._draft.dashboardLevels[path] = level;
        this._refreshSeesNothing();
      });
    }
  }

  async _refreshDashboards() {
    await this._guard(async () => {
      const result = await this._call("dashboards/refresh");
      const counts = result.dashboards || {};
      const names = Object.keys(counts);
      this._pending = names.length
        ? `Re-read ${names.length} dashboard${names.length === 1 ? "" : "s"}: ${names
            .map((n) => `${n} (${counts[n]})`)
            .join(", ")}.`
        : "No dashboards with a stored config to read.";
    });
  }

  _payload() {
    const root = this.shadowRoot;
    const { allow, deny } = writeRules(this._draft.base, this._draft.rules);
    return {
      name: root.getElementById("name").value.trim(),
      allow,
      deny,
      capabilities: [...(this._draft.capabilities || [])],
      tiers: {
        max: root.getElementById("tier-admin").checked ? "admin" : "user",
        allow: [...this._draft.tierAllow],
        deny: [...this._draft.tierDeny],
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
      schedule: {
        // Written as a list, and the older inline window is cleared so a role
        // saved after an upgrade does not carry both shapes at once.
        rules: this._draft.schedule.filter(
          (window) => window.days.length || window.start || window.end
        ),
        days: [],
        start: "",
        end: "",
      },
      apps: {
        dashboards: { ...this._draft.dashboardLevels },
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
    let keepDraft = false;
    try {
      this._notice = null;
      this._pending = null;
      await action();
      if (this._pending) {
        // The action edited the draft rather than the stored role, so the
        // refresh below must not read it back over the top.
        this._notice = { kind: "ok", text: this._pending };
        keepDraft = true;
      } else if (done) {
        this._notice = {
          kind: "ok",
          text: typeof done === "function" ? done() : done,
        };
      }
    } catch (err) {
      this._notice = { kind: "error", text: err.message || String(err) };
      keepDraft = this._pending !== null;
    }
    this._pending = null;
    await this._refresh(keepDraft);
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
          capabilities: source.capabilities,
          apps: source.apps,
          attributes: source.attributes,
          schedule: source.schedule,
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
