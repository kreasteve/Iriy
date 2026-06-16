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
    this._wunit = "mm"; // Anzeige-Einheit der "Gegossen"-Spalte: mm | L | T
    this._busy = false;
    this._error = "";
    this._timer = null;
    this._initDone = false;
    this._onClick = this._onClick.bind(this);
    this._onSubmit = this._onSubmit.bind(this);
    this._onChange = this._onChange.bind(this);
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

  // --- Generische Entitäts-Abfrage (primitiv, für jede Entität) --------
  // Das hass-Objekt bringt alles mit: Live-Stände direkt, History/Statistik
  // über HAs eigene WS-Befehle. Damit lassen sich Tabellen jetzt und später
  // ohne neue Backend-Befehle füllen.

  /** Live-Zustand einer beliebigen Entität (sofort, ohne Abruf). */
  _state(entityId) {
    const s = this._hass && this._hass.states[entityId];
    return s ? s.state : null;
  }

  /** Langzeit-Statistik einer Entität: [{start, mean, min, max, sum, state}]. */
  async _statistics(entityId, hours = 168, period = "day") {
    const end = new Date();
    const start = new Date(end.getTime() - hours * 3600000);
    const res = await this._ws({
      type: "recorder/statistics_during_period",
      start_time: start.toISOString(),
      end_time: end.toISOString(),
      statistic_ids: [entityId],
      period,
      types: ["mean", "min", "max", "sum", "state"],
    });
    return (res && res[entityId]) || [];
  }

  /** Roh-State-Verlauf einer Entität: [{s: state, lu: ts}]. */
  async _stateHistory(entityId, hours = 24) {
    const end = new Date();
    const start = new Date(end.getTime() - hours * 3600000);
    const res = await this._ws({
      type: "history/history_during_period",
      start_time: start.toISOString(),
      end_time: end.toISOString(),
      entity_ids: [entityId],
      minimal_response: true,
      no_attributes: true,
    });
    return (res && res[entityId]) || [];
  }

  /** Tag-Label relativ zu heute: heute / gestern / Wochentag. */
  _dayLabel(dateStr) {
    const p = String(dateStr).split("-").map(Number);
    const d = new Date(p[0], p[1] - 1, p[2]);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diff = Math.round((today - d) / 86400000);
    if (diff === 0) return "heute";
    if (diff === 1) return "gestern";
    const wd = [
      "Sonntag",
      "Montag",
      "Dienstag",
      "Mittwoch",
      "Donnerstag",
      "Freitag",
      "Samstag",
    ];
    return wd[d.getDay()];
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
    } else if (action === "irrigate") {
      this._irrigate(el.dataset.name);
    } else if (action === "wunit") {
      this._wunit = el.dataset.unit;
      this._render();
    }
  }

  async _irrigate(name) {
    const inst = this._instance();
    const z = (inst.zones || []).find((x) => x.name === name);
    if (!z) return;
    const isArea = z.area > 0;
    const unit = isArea ? "L" : "min";
    let amount = isArea ? z.liters_needed : z.runtime_minutes;
    const data = { zone: name };
    if (!amount || amount <= 0) {
      // Kein Defizit -> Testmenge abfragen (sonst gäbe es nichts zu gießen).
      const inp = prompt(
        `Zone „${name}" hat aktuell kein Defizit.\nTestmenge in ${unit} eingeben (leer = abbrechen):`
      );
      if (!inp) return;
      amount = parseFloat(String(inp).replace(",", "."));
      if (!(amount > 0)) return;
      data.amount = amount;
    } else if (
      !confirm(
        `Zone „${name}" jetzt gießen (${NUM(amount, 0)} ${unit})?\nDas öffnet das Ventil.`
      )
    ) {
      return;
    }
    this._busy = true;
    this._render();
    try {
      await this._hass.callService("iriy", "irrigate_zone", data);
      await this._afterMutation();
    } catch (e) {
      this._error = "Gießen fehlgeschlagen: " + (e.message || e.code || e);
    }
    this._busy = false;
    this._render();
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
    const byArea = f.elements.by_area ? f.elements.by_area.checked : false;
    const zone = {
      name: f.elements.name.value.trim(),
      kc: f.elements.kc.value,
      area: f.elements.area.value,
      throughput: f.elements.throughput ? f.elements.throughput.value : "",
      efficiency: f.elements.efficiency.value,
      max_deficit: f.elements.max_deficit.value,
      by_area: byArea,
      valve: f.elements.valve ? f.elements.valve.value : "",
    };
    if (!zone.name) {
      this._error = "Bitte einen Zonennamen angeben.";
      this._render();
      return;
    }
    if (byArea && !(parseFloat(zone.area) > 0)) {
      this._error = "Bei Flächensteuerung bitte eine Fläche (m²) angeben.";
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
    this.shadowRoot.addEventListener("change", this._onChange);
  }

  // Modus-Checkbox umgeschaltet: aktuelle Eingaben sichern und Formular neu
  // rendern (zeigt/versteckt das passende Feld).
  _onChange(ev) {
    if (ev.target.id !== "zone-by-area" || !this._form) return;
    this._form.zone = this._readForm();
    this._render();
  }

  _readForm() {
    const f = this.shadowRoot.getElementById("zone-form");
    if (!f) return this._form ? this._form.zone : {};
    return {
      name: f.elements.name.value,
      kc: f.elements.kc.value,
      area: f.elements.area.value,
      throughput: f.elements.throughput ? f.elements.throughput.value : undefined,
      efficiency: f.elements.efficiency.value,
      max_deficit: f.elements.max_deficit.value,
      by_area: f.elements.by_area ? f.elements.by_area.checked : false,
      valve: f.elements.valve ? f.elements.valve.value : "",
    };
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

  // --- Umrechnung (Zahlen ohne Einheit – Einheiten stehen im Kopf) -----
  _litersFromMm(z, mm) {
    const eff = z.efficiency > 0 ? z.efficiency : 1;
    return (mm * (z.area || 0)) / eff;
  }
  _minutesFromMm(z, mm) {
    const eff = z.efficiency > 0 ? z.efficiency : 1;
    return z.throughput > 0 ? (mm / (z.throughput * eff)) * 60 : 0;
  }
  _deficitUnit(z) {
    return z.area > 0 ? "L" : "min";
  }
  _wateredUnit() {
    return this._wunit === "mm" ? "mm" : this._wunit === "L" ? "L" : "min";
  }
  // Defizit als Zahl: Flächen-Zone -> Liter, Zeit-Zone -> Minuten. mm==null -> live.
  _deficitNum(z, mm) {
    if (z.area > 0) return mm == null ? z.liters_needed : this._litersFromMm(z, mm);
    return mm == null ? z.runtime_minutes : this._minutesFromMm(z, mm);
  }
  // Gegossen als Zahl je nach Umschalter (aus gemessenen Litern).
  _wateredNum(z, liters) {
    if (liters == null) return null;
    if (this._wunit === "L") return liters;
    if (z.area > 0) {
      const mm = liters / z.area;
      return this._wunit === "mm" ? mm : this._minutesFromMm(z, mm);
    }
    return null; // ohne Fläche nicht in mm/T umrechenbar
  }
  _cell(num, dec) {
    return `<td class="r">${num == null || isNaN(num) ? "–" : NUM(num, dec)}</td>`;
  }

  _table(inst) {
    const days = inst.last_days || [];
    const zones = inst.zones || [];
    const rain = inst.rain_recent || {};
    const zhist = inst.zone_history || {};
    const U = this._wunit;
    const wdec = U === "T" ? 0 : 1; // Minuten ganzzahlig, mm/L mit 1 Nachkomma

    const chip = (u, lbl) =>
      `<button class="chip${U === u ? " on" : ""}" data-action="wunit" data-unit="${u}">${lbl}</button>`;

    // 3-zeiliger, kompakter Kopf: Name / Defizit·Gegossen / Einheiten.
    const head1 = zones.map((z) => `<th colspan="2">${ESC(z.name)}</th>`).join("");
    const head2 = zones
      .map(() => `<th class="r">Defizit</th><th class="r">Gegossen</th>`)
      .join("");
    const head3 = zones
      .map(
        (z) =>
          `<th class="r u">${this._deficitUnit(z)}</th><th class="r u">${this._wateredUnit()}</th>`
      )
      .join("");
    const thead = `<thead>
      <tr><th rowspan="3">Tag</th><th rowspan="2" class="r">ET0</th><th rowspan="2" class="r">Regen</th>${head1}</tr>
      <tr>${head2}</tr>
      <tr><th class="r u">mm</th><th class="r u">mm</th>${head3}</tr>
    </thead>`;

    const zoneCells = (defMm, getL, isLive) =>
      zones
        .map((z) => {
          const def = isLive ? this._deficitNum(z, null) : this._deficitNum(z, defMm(z));
          const liters = getL(z);
          return this._cell(def, 0) + this._cell(this._wateredNum(z, liters), wdec);
        })
        .join("");

    const row = (label, et0, rainMm, cells) =>
      `<tr><td>${label}</td>${this._cell(et0, 2)}${this._cell(rainMm, 1)}${cells}</tr>`;

    const rows = [
      row(
        "heute",
        inst.et0_today,
        inst.rain_today,
        zoneCells(null, (z) => z.gegossen_l, true)
      ),
    ];
    for (const d of days) {
      const cells = zones
        .map((z) => {
          const h = (zhist[z.name] || {})[d.date];
          const def = h ? this._deficitNum(z, h.deficit) : null;
          const liters = h ? h.gegossen_l : null;
          return this._cell(def, 0) + this._cell(this._wateredNum(z, liters), wdec);
        })
        .join("");
      rows.push(row(this._dayLabel(d.date), d.mm, rain[d.date], cells));
    }

    return `
      <div class="card">
        <div class="cardhead">
          <h2>Tabelle</h2>
          <div class="chips">Gegossen: ${chip("mm", "mm")}${chip("L", "L")}${chip(
      "T",
      "T"
    )}</div>
        </div>
        <div class="tablewrap">
          <table class="grid3">${thead}<tbody>${rows.join("")}</tbody></table>
        </div>
      </div>`;
  }

  _zoneRow(z) {
    // Info-Zeile: bei Flächensteuerung Fläche statt Durchfluss.
    const info = [`Kc ${NUM(z.kc, 2)}`];
    if (z.by_area) info.push(`${NUM(z.area, 1)} m² (Fläche)`);
    else {
      if (z.area) info.push(`${NUM(z.area, 1)} m²`);
      info.push(`${NUM(z.throughput, 1)} mm/h`);
    }
    info.push(`η ${NUM(z.efficiency, 2)}`);
    // Badge: nur die zutreffenden Steuergrößen.
    const badge = [`Defizit ${NUM(z.deficit)} mm`];
    if (z.runtime_minutes != null) badge.push(`${NUM(z.runtime_minutes, 0)} min`);
    if (z.liters_needed != null) badge.push(`${NUM(z.liters_needed, 1)} L`);
    return `
        <div class="zone">
          <div class="zinfo">
            <strong>${ESC(z.name)}</strong>
            <span class="muted">${info.join(" · ")}</span>
            <span class="badge">${badge.join(" · ")}</span>
          </div>
          <div class="zact">
            ${
              z.valve
                ? `<button class="icon" data-action="irrigate" data-name="${ESC(
                    z.name
                  )}" title="Jetzt gießen">💧</button>`
                : ""
            }
            <button class="icon" data-action="edit-zone" data-name="${ESC(
              z.name
            )}" title="Bearbeiten">✏️</button>
            <button class="icon" data-action="delete-zone" data-name="${ESC(
              z.name
            )}" title="Löschen">🗑️</button>
          </div>
        </div>`;
  }

  _zones(inst) {
    const zones = inst.zones || [];
    const list = zones.length
      ? zones.map((z) => this._zoneRow(z)).join("")
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

  _valveOptions(selected) {
    const states = (this._hass && this._hass.states) || {};
    const ids = Object.keys(states)
      .filter((id) => id.startsWith("switch.") || id.startsWith("valve."))
      .sort();
    const opt = (val, label, sel) =>
      `<option value="${ESC(val)}"${sel ? " selected" : ""}>${ESC(label)}</option>`;
    let out = opt("", "— kein Ventil —", !selected);
    for (const id of ids) {
      const fn = (states[id].attributes || {}).friendly_name || id;
      out += opt(id, fn, id === selected);
    }
    // Falls das gespeicherte Ventil aktuell nicht in der Liste ist, trotzdem zeigen.
    if (selected && !ids.includes(selected)) out += opt(selected, selected, true);
    return out;
  }

  _zoneForm() {
    const z = this._form.zone || {};
    const byArea = !!z.by_area;
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
        <label class="check">
          <input id="zone-by-area" name="by_area" type="checkbox" ${
            byArea ? "checked" : ""
          } />
          <span>Nur über Fläche steuern (Liter statt Laufzeit) – bei undefiniertem/variablem Durchfluss</span>
        </label>
        <div class="grid">
          <label class="field">
            <span>Kc (Pflanzenkoeffizient)</span>
            <input id="kc" name="kc" type="number" step="0.05" min="0.1" max="1.5" value="${
              z.kc ?? 0.8
            }" list="kc-list" />
            <datalist id="kc-list">${kcOptions}</datalist>
          </label>
          ${field(
            "area",
            "Fläche (m²)",
            byArea
              ? "type=number step=0.1 min=0.1 required"
              : "type=number step=0.1 min=0",
            z.area,
            byArea ? "erforderlich – ergibt die Liter" : "optional, für Liter-Ausgabe"
          )}
          <div class="field" style="${byArea ? "display:none" : ""}">
            <span>Durchfluss (mm/h)</span>
            <input id="throughput" name="throughput" type="number" step="0.5" min="0.5" value="${
              z.throughput ?? 20
            }" />
            <small class="muted">für die Laufzeit (z. B. Tropfschlauch)</small>
          </div>
          ${field("efficiency", "Wirkungsgrad (0–1)", "type=number step=0.05 min=0.1 max=1", z.efficiency ?? 0.9, "")}
          ${field("max_deficit", "Max. Defizit (mm)", "type=number step=1 min=1", z.max_deficit ?? 30, "")}
          <label class="field">
            <span>Ventil (optional)</span>
            <select id="valve" name="valve">${this._valveOptions(z.valve)}</select>
            <small class="muted">z2m-Switch des Ventils – für „Jetzt gießen" + gegossene Liter</small>
          </label>
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
  h3 { font-size: .92rem; font-weight: 500; margin: 16px 0 6px; color: var(--secondary-text-color); }
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
  .chips { display:flex; align-items:center; gap:6px; font-size:.82rem; color: var(--secondary-text-color); }
  .chip { background: transparent; border:1px solid var(--divider-color,#ccc); color: var(--primary-text-color);
          padding: 3px 10px; border-radius: 14px; font-size:.82rem; }
  .chip.on { background: var(--primary-color, #03a9f4); color: var(--text-primary-color,#fff); border-color: transparent; }
  .tablewrap { overflow-x: auto; }
  select { font: inherit; padding: 8px; border-radius: 8px; border: 1px solid var(--divider-color, #ccc);
           background: var(--primary-background-color); color: var(--primary-text-color); }
  table { width:100%; border-collapse: collapse; white-space: nowrap; }
  th, td { padding: 6px 8px; border-bottom: 1px solid var(--divider-color, #e0e0e0); font-size:.9rem; }
  th { text-align:left; color: var(--secondary-text-color); font-weight:500; }
  thead th { vertical-align: bottom; }
  .grid3 thead tr:first-child th[colspan] { text-align:center; border-bottom: 1px solid var(--divider-color,#e0e0e0); }
  .grid3 th.u { font-weight: 400; font-size: .72rem; padding-top: 0; }
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
  .check { display:flex; align-items:flex-start; gap:8px; font-size:.9rem; cursor:pointer; }
  .check input { margin-top:2px; }
  .field input { font: inherit; padding: 8px; border-radius: 8px; border: 1px solid var(--divider-color, #ccc);
                 background: var(--primary-background-color); color: var(--primary-text-color); }
  .formact { display:flex; justify-content:flex-end; gap:8px; }
`;

if (!customElements.get("iriy-panel")) {
  customElements.define("iriy-panel", IriyPanel);
}
