/**
 * Iriy – eigenes Sidebar-Panel (panel_custom Webcomponent).
 *
 * Reine Vanilla-Web-Component (keine Build-Kette, keine Fremd-Libs). HA setzt
 * `hass`, `narrow`, `route`, `panel` als Properties. Wir lesen den Zustand und
 * verwalten Zonen ueber die WS-Befehle iriy/overview, iriy/zone/save,
 * iriy/zone/delete. Styling nutzt HA-Theme-Variablen, fuegt sich also ein.
 */

const NUM = (v, d = 2) =>
  v === null || v === undefined || isNaN(v) ? "–" : Number(v).toFixed(d);

// HTML-Escape fuer benutzereingegebene Werte (z. B. Zonennamen) in innerHTML
// und Attributen – verhindert Markup-Bruch / XSS.
const ESC = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

class IriyPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = null; // { instances:[...], kc_table:{} }
    this._entryId = null;
    this._form = null; // null | {mode:'add'|'edit', original?, zone?}
    this._busy = false;
    this._error = "";
    this._timer = null;
    this._initDone = false;
    this._onClick = this._onClick.bind(this);
    this._onSubmit = this._onSubmit.bind(this);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initDone) {
      this._initDone = true;
      this._renderShell();
      this._load();
    }
  }
  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._initDone && !this.shadowRoot.firstChild) this._renderShell();
    // Hintergrund-Refresh NUR wenn nicht gerade editiert wird (sonst gingen
    // ungespeicherte Formular-Eingaben verloren).
    this._timer = setInterval(() => {
      if (!this._form && !this._busy) this._load(true);
    }, 30000);
  }
  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }

  // --- Daten ----------------------------------------------------------

  async _ws(msg) {
    return this._hass.connection.sendMessagePromise(msg);
  }

  async _load(silent = false) {
    if (!this._hass) return;
    try {
      const res = await this._ws({ type: "iriy/overview" });
      this._data = res;
      if (!this._entryId && res.instances.length)
        this._entryId = res.instances[0].entry_id;
      this._error = "";
      this._render();
    } catch (e) {
      if (!silent) {
        this._error = "Konnte Iriy-Daten nicht laden: " + (e.message || e.code || e);
        this._render();
      }
    }
  }

  _instance() {
    if (!this._data) return null;
    return (
      this._data.instances.find((i) => i.entry_id === this._entryId) ||
      this._data.instances[0] ||
      null
    );
  }

  // --- Aktionen -------------------------------------------------------

  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    if (action === "refresh") this._load();
    else if (action === "add-zone") {
      this._form = { mode: "add", zone: {} };
      this._render();
    } else if (action === "edit-zone") {
      const inst = this._instance();
      const zone = inst.zones.find((z) => z.name === el.dataset.name);
      if (!zone) return; // koennte nach Hintergrund-Refresh entfernt sein
      this._form = { mode: "edit", original: zone.name, zone: { ...zone } };
      this._render();
    } else if (action === "cancel-zone") {
      this._form = null;
      this._error = "";
      this._render();
    } else if (action === "delete-zone") {
      this._deleteZone(el.dataset.name);
    }
  }

  async _deleteZone(name) {
    if (!confirm(`Zone „${name}" wirklich löschen?`)) return;
    this._busy = true;
    this._render();
    try {
      await this._ws({
        type: "iriy/zone/delete",
        entry_id: this._entryId,
        name,
      });
      await this._afterMutation();
    } catch (e) {
      this._error = "Löschen fehlgeschlagen: " + (e.message || e.code || e);
    }
    this._busy = false;
    this._render();
  }

  async _onSubmit(ev) {
    ev.preventDefault();
    const f = ev.target;
    if (f.id !== "zone-form") return;
    const zone = {
      name: f.elements.name.value.trim(),
      kc: f.elements.kc.value,
      area: f.elements.area.value,
      throughput: f.elements.throughput.value,
      efficiency: f.elements.efficiency.value,
      max_deficit: f.elements.max_deficit.value,
    };
    if (!zone.name) {
      this._error = "Bitte einen Zonennamen angeben.";
      this._render();
      return;
    }
    this._busy = true;
    this._error = "";
    this._render();
    try {
      const msg = { type: "iriy/zone/save", entry_id: this._entryId, zone };
      if (this._form.mode === "edit") msg.original_name = this._form.original;
      await this._ws(msg);
      this._form = null;
      await this._afterMutation();
    } catch (e) {
      const map = {
        exists: "Eine Zone mit diesem Namen existiert bereits.",
        invalid: "Eingabe unvollständig.",
        not_found: "Zone/Instanz nicht gefunden.",
        unauthorized: "Adminrechte nötig.",
      };
      this._error = map[e.code] || "Speichern fehlgeschlagen: " + (e.message || e);
    }
    this._busy = false;
    this._render();
  }

  async _afterMutation() {
    // Optionsänderung löst einen asynchronen Reload aus. Kein fixer Sleep als
    // Garantie – zweimal nachladen (früh + spät) fängt auch langsame Reloads.
    await new Promise((r) => setTimeout(r, 500));
    await this._load(true);
    await new Promise((r) => setTimeout(r, 1300));
    await this._load(true);
  }

  // --- Rendering ------------------------------------------------------

  _renderShell() {
    this.shadowRoot.innerHTML = `<style>${IriyPanel.styles}</style><div id="root"></div>`;
    this.shadowRoot.addEventListener("click", this._onClick);
    this.shadowRoot.addEventListener("submit", this._onSubmit);
  }

  _render() {
    const root = this.shadowRoot.getElementById("root");
    if (!root) return;
    if (!this._data) {
      root.innerHTML = `<div class="wrap"><p class="muted">Lade…</p></div>`;
      return;
    }
    const inst = this._instance();
    root.innerHTML = `
      <div class="wrap">
        <header>
          <h1>🌱 Iriy</h1>
          <button class="icon" data-action="refresh" title="Aktualisieren">↻</button>
        </header>
        ${this._error ? `<div class="error">${this._error}</div>` : ""}
        ${inst ? this._overview(inst) + this._chart(inst) + this._table(inst) + this._zones(inst) : this._empty()}
      </div>`;
  }

  _empty() {
    return `<div class="card"><p class="muted">Keine Iriy-Instanz gefunden. Ist die Integration eingerichtet?</p></div>`;
  }

  _overview(inst) {
    const d = inst.diagnostics || {};
    const stat = (label, val, unit) =>
      `<div class="stat"><span class="lbl">${label}</span><span class="val">${val}<small>${unit || ""}</small></span></div>`;
    return `
      <div class="card">
        <div class="stats">
          ${stat("ET0 gestern", NUM(inst.et0_daily), " mm")}
          ${stat("ET0 heute", NUM(inst.et0_today), " mm")}
          ${stat("ET0 Rate", NUM(inst.et0_rate), " mm/h")}
        </div>
        <div class="diag muted">
          Ø Temp ${NUM(d.t_min, 1)}–${NUM(d.t_max, 1)} °C ·
          rF ${NUM(d.rh_mean, 0)} % ·
          Wind ${NUM(d.wind_mean_ms, 1)} m/s ·
          Solar ${NUM(d.solar_mean_wm2, 0)} W/m² ·
          Regen ${NUM(d.rain_today_mm, 1)} mm
        </div>
      </div>`;
  }

  _chart(inst) {
    const days = [...(inst.last_days || [])].reverse(); // chronologisch
    if (!days.length)
      return `<div class="card"><h2>Verlauf</h2><p class="muted">Noch keine Tageswerte.</p></div>`;
    const W = 100,
      H = 40,
      max = Math.max(...days.map((d) => d.mm), 0.1);
    const bw = W / days.length;
    const bars = days
      .map((d, i) => {
        const h = (d.mm / max) * (H - 6);
        const x = i * bw + bw * 0.15;
        const w = bw * 0.7;
        const y = H - h;
        return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${w.toFixed(
          2
        )}" height="${h.toFixed(2)}" rx="0.6"><title>${d.date}: ${d.mm.toFixed(
          2
        )} mm</title></rect>`;
      })
      .join("");
    const labels = days
      .map((d, i) => {
        const dd = d.date.slice(5); // MM-TT
        return `<span style="flex:1">${dd}</span>`;
      })
      .join("");
    return `
      <div class="card">
        <h2>ET0 – letzte ${days.length} Tage</h2>
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart">${bars}</svg>
        <div class="xlabels muted">${labels}</div>
      </div>`;
  }

  _table(inst) {
    const days = inst.last_days || [];
    if (!days.length) return "";
    const rows = days
      .map(
        (d) =>
          `<tr><td>${d.date}</td><td class="r">${NUM(d.mm)} mm</td></tr>`
      )
      .join("");
    return `
      <div class="card">
        <h2>Tabelle</h2>
        <table><thead><tr><th>Datum</th><th class="r">ET0</th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
  }

  _zones(inst) {
    const zones = inst.zones || [];
    const list = zones.length
      ? zones
          .map(
            (z) => `
        <div class="zone">
          <div class="zinfo">
            <strong>${ESC(z.name)}</strong>
            <span class="muted">Kc ${NUM(z.kc, 2)}${
              z.area ? " · " + NUM(z.area, 1) + " m²" : ""
            } · ${NUM(z.throughput, 1)} mm/h · η ${NUM(z.efficiency, 2)}</span>
            <span class="badge">Defizit ${NUM(z.deficit)} mm · Laufzeit ${NUM(
              z.runtime_minutes,
              0
            )} min</span>
          </div>
          <div class="zact">
            <button class="icon" data-action="edit-zone" data-name="${ESC(
              z.name
            )}" title="Bearbeiten">✏️</button>
            <button class="icon" data-action="delete-zone" data-name="${ESC(
              z.name
            )}" title="Löschen">🗑️</button>
          </div>
        </div>`
          )
          .join("")
      : `<p class="muted">Noch keine Zonen angelegt.</p>`;
    return `
      <div class="card">
        <div class="cardhead">
          <h2>Zonen</h2>
          ${
            this._form
              ? ""
              : `<button class="primary" data-action="add-zone">+ Zone</button>`
          }
        </div>
        ${this._form ? this._zoneForm() : list}
      </div>`;
  }

  _zoneForm() {
    const z = this._form.zone || {};
    const kcOptions = Object.entries(this._data.kc_table || {})
      .map(([k, v]) => `<option value="${v}">${k} (${v})</option>`)
      .join("");
    const field = (id, label, attrs, val, hint) => `
      <label class="field">
        <span>${label}</span>
        <input id="${id}" name="${id}" ${attrs} value="${ESC(val ?? "")}" />
        ${hint ? `<small class="muted">${hint}</small>` : ""}
      </label>`;
    return `
      <form id="zone-form">
        ${field("name", "Name", "type=text required", z.name, "z. B. Rasen, Hochbeet, Apfelbaum")}
        <div class="grid">
          <label class="field">
            <span>Kc (Pflanzenkoeffizient)</span>
            <input id="kc" name="kc" type="number" step="0.05" min="0.1" max="1.5" value="${
              z.kc ?? 0.8
            }" list="kc-list" />
            <datalist id="kc-list">${kcOptions}</datalist>
          </label>
          ${field("area", "Fläche (m²)", "type=number step=0.1 min=0", z.area, "optional, für Liter-Bilanz")}
          ${field("throughput", "Durchfluss (mm/h)", "type=number step=0.5 min=0.5", z.throughput ?? 20, "")}
          ${field("efficiency", "Wirkungsgrad (0–1)", "type=number step=0.05 min=0.1 max=1", z.efficiency ?? 0.9, "")}
          ${field("max_deficit", "Max. Defizit (mm)", "type=number step=1 min=1", z.max_deficit ?? 30, "")}
        </div>
        <div class="formact">
          <button type="button" class="ghost" data-action="cancel-zone">Abbrechen</button>
          <button type="submit" class="primary" ${this._busy ? "disabled" : ""}>${
      this._busy ? "Speichere…" : "Speichern"
    }</button>
        </div>
      </form>`;
  }
}

IriyPanel.styles = `
  :host { display:block; background: var(--primary-background-color); min-height:100%; color: var(--primary-text-color); }
  .wrap { max-width: 780px; margin: 0 auto; padding: 12px 16px 48px; box-sizing:border-box; }
  header { display:flex; align-items:center; justify-content:space-between; margin: 8px 0 4px; }
  h1 { font-size: 1.5rem; font-weight: 500; margin: 0; }
  h2 { font-size: 1rem; font-weight: 500; margin: 0 0 8px; }
  .card { background: var(--card-background-color, #fff); border-radius: 12px; padding: 16px;
          margin-top: 12px; box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1)); }
  .cardhead { display:flex; align-items:center; justify-content:space-between; }
  .muted { color: var(--secondary-text-color); font-size: .85rem; }
  .error { background: var(--error-color, #db4437); color:#fff; padding:8px 12px; border-radius:8px; margin-top:12px; font-size:.9rem; }
  .stats { display:flex; gap: 12px; flex-wrap:wrap; }
  .stat { flex:1; min-width: 120px; display:flex; flex-direction:column; gap:2px; }
  .stat .lbl { font-size:.8rem; color: var(--secondary-text-color); }
  .stat .val { font-size: 1.7rem; font-weight: 500; }
  .stat .val small { font-size: .9rem; color: var(--secondary-text-color); }
  .diag { margin-top: 10px; line-height: 1.5; }
  .chart { width:100%; height: 160px; display:block; }
  .chart rect { fill: var(--primary-color, #03a9f4); }
  .chart .bval { fill: var(--secondary-text-color); font-size: 2px; text-anchor: middle; }
  .xlabels { display:flex; margin-top:4px; text-align:center; }
  .xlabels span { font-size:.7rem; }
  table { width:100%; border-collapse: collapse; }
  th, td { padding: 6px 8px; border-bottom: 1px solid var(--divider-color, #e0e0e0); font-size:.9rem; }
  th { text-align:left; color: var(--secondary-text-color); font-weight:500; }
  .r { text-align:right; }
  .zone { display:flex; align-items:center; justify-content:space-between; gap:8px;
          padding: 10px 0; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
  .zone:last-child { border-bottom: none; }
  .zinfo { display:flex; flex-direction:column; gap:3px; }
  .zinfo .badge { font-size:.78rem; color: var(--primary-color); }
  .zact { display:flex; gap:4px; }
  button { font: inherit; cursor:pointer; border-radius: 8px; border: none; }
  button.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); padding: 8px 14px; }
  button.ghost { background: transparent; color: var(--primary-text-color); padding: 8px 14px; border:1px solid var(--divider-color,#ccc); }
  button.icon { background: transparent; font-size: 1.1rem; padding: 4px 8px; }
  button:disabled { opacity:.6; cursor: default; }
  form { display:flex; flex-direction:column; gap: 12px; margin-top: 8px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 12px; }
  .field { display:flex; flex-direction:column; gap:4px; }
  .field span { font-size:.85rem; color: var(--secondary-text-color); }
  .field input { font: inherit; padding: 8px; border-radius: 8px; border: 1px solid var(--divider-color, #ccc);
                 background: var(--primary-background-color); color: var(--primary-text-color); }
  .formact { display:flex; justify-content:flex-end; gap:8px; }
`;

if (!customElements.get("iriy-panel")) {
  customElements.define("iriy-panel", IriyPanel);
}
