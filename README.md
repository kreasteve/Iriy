# 🌱 Iriy – Smart Irrigation für Home Assistant

**Iriy** (weiblicher Bewässerungs-Bot, von *irrigation*) berechnet aus den
Sensoren deiner Wetterstation die **Referenz-Verdunstung (ET₀)** nach
**FAO-56 Penman-Monteith** und führt pro Bewässerungszone eine laufende
**Wasser-Defizit-Bilanz**. Daraus leitet sie ab, wie viel nachgegossen werden
muss – und steuert das später automatisch (Ventile, Strategien).

> Status: **v0.1 – Fundament.** ET₀ (Tag + stündlich live) und Zonen-Defizit
> funktionieren. Ventilsteuerung, Strategien und die editierbare Oberfläche
> sind als saubere Erweiterungspunkte vorbereitet (siehe [Roadmap](#roadmap)).

---

## Was v0.1 schon kann

- Liest die Rohsensoren deiner Station (Temperatur, Feuchte, Wind, Strahlung,
  optional Druck und Regen) – komplett **per UI** ausgewählt, kein YAML.
- **Zwei ET-Spuren**, beide nützlich:
  - **Tageswert** (kanonisch, vertrauenswürdig): Summe der FAO-56-Stunden­
    gleichung (Gl. 53) über die **Stundenstatistik des Recorders** – zeit­
    gewichtet und lückenrobust (unabhängig davon, ob HA durchlief). Die Zahl
    für die Morgen-Automation; wird kurz nach Mitternacht finalisiert. (Die
    reine Tagesmittel-Gleichung dient nur noch als Fallback ohne Stundenmodus.)
  - **Stündlich live**: dieselbe Stundengleichung, die sich zu „heute bisher"
    aufsummiert und das Zonen-Defizit in Echtzeit speist – reagiert sofort
    auf Regen.
- Pro Zone ein **Defizit-Bucket** (mm) und eine **empfohlene Laufzeit** (min).
- Übersteht Neustarts (persistente Bilanz via HA-Storage).
- Services: `iriy.recalculate`, `iriy.reset_bucket`, `iriy.add_water`.

### Erzeugte Entitäten

| Entität | Bedeutung |
|---|---|
| `sensor.iriy_et0_daily` | kanonischer ET₀-Tageswert [mm/Tag] (+ Diagnose-Attribute) |
| `sensor.iriy_et0_today` | heute bisher aufsummiert [mm] |
| `sensor.iriy_et0_rate` | aktuelle ET-Rate [mm/h] |
| `sensor.iriy_<zone>_defizit` | Wasserdefizit der Zone [mm] |
| `sensor.iriy_<zone>_laufzeit` | nötige Bewässerungszeit [min] |
| `button.iriy_et0_verlauf_neu_berechnen` | ET₀-Verlauf der letzten Tage neu berechnen (idempotenter Upsert) |

---

## Warum Tag *und* Stunde? (die „3h"-Frage)

Kurz: **Der Tageswert bleibt die Wahrheit, die Stunde ist das Live-Bild.**

- Die **FAO-56-Tagesgleichung** ist der etablierte, robuste Standard. Ihr
  Schwachpunkt im klassischen Ansatz: ein belastbarer Wert entsteht erst kurz
  vor Mitternacht (man braucht Tages-Min/Max). Für die Bewässerung früh morgens
  nimmt man deshalb den **Vortageswert**.
- Die **FAO-56-Stundengleichung** (Gl. 53) ist physikalisch sauber definiert –
  mit eigenen Tag/Nacht-Koeffizienten (Cd = 0,24 / 0,96) und Bodenwärmestrom
  (G = 0,1·Rn tags, 0,5·Rn nachts). Die **Summe der 24 Stundenwerte ≈ Tageswert**
  (in Iriy als Test abgesichert).
- **Eine echte 3h-Auflösung** lohnt selten als eigene Berechnung – sie ist nur
  eine *Aggregation* der Stundenwerte. Der natürliche sub-tägliche Takt ist die
  **Stunde**: feiner bringt durch Sensorrauschen wenig, gröber (3h/6h) verschenkt
  Reaktionsfähigkeit. Iriy rechnet daher **stündlich** und summiert; eine 3h- oder
  6h-Sicht kannst du in HA jederzeit per Statistik/Utility-Meter daraus bilden.

**Nutzen der Stundenspur konkret:**
1. **Live-Defizit** – das Bucket steigt über den Tag sichtbar und fällt bei Regen
   sofort, statt erst um Mitternacht.
2. **Morgen-Strategie** – um 4 Uhr steht das Defizit des Vortages fest bereit
   zum Nachgießen.
3. **Hitze-/Wind-Reaktion** – ein heißer, windiger Nachmittag treibt das Defizit
   sichtbar, ein kühler bedeckter Vormittag kaum.

> Einstellbar: Stundenspur an/aus und Update-Intervall (Standard 60 min) im
> Config-Flow.

---

## Architektur

Bewusst zukunftsoffen aufgebaut – jede spätere Funktion hat ihren Platz:

```
Rohsensoren ─▶ Akkumulatoren (Tag + Intervall) ─▶ et.py (FAO-56) ─▶ ET₀
                                                          │
                                                          ▼
                               Zonen-Buckets (Wasserdefizit in mm)
                                                          │
                                          (Roadmap) Strategien ─▶ Ventile
```

| Datei | Rolle |
|---|---|
| `et.py` | **Reiner Rechenkern**, keine HA-Abhängigkeit → isoliert testbar. Tages- *und* Stundengleichung. |
| `coordinator.py` | Sammelt Sensorwerte, hält die Bilanz, persistiert sie. Die „Drehscheibe". |
| `config_flow.py` | Einrichtung **und** Pflege per UI (inkl. Zonen-Menü). |
| `sensor.py` | Entitäten aus dem Koordinator. |
| `const.py` | Konstanten, Standardwerte, Kc-Referenztabelle. |

**Was wo editierbar ist:**
- **Sensoren / Standort / Zonen / Kc** → komplett über die HA-UI (Zahnrad).
- **Die Formel selbst** → `et.py` (sauber getrennt, getestet).

---

## Installation

### Manuell
1. Ordner `custom_components/iriy/` nach `<config>/custom_components/` kopieren.
2. Home Assistant neu starten.
3. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Iriy"**.
4. Sensoren und Standort auswählen, fertig. Zonen danach übers Zahnrad anlegen.

### HACS (custom repository)
`https://github.com/kreasteve/Iriy` als benutzerdefiniertes Repository
(Kategorie *Integration*) hinzufügen.

---

## Tests

Der Rechenkern ist gegen die FAO-56-Referenzbeispiele geprüft (Beispiel 18
Tag, Beispiel 19 Stunde) – läuft auch ohne pytest:

```bash
python3 tests/test_et.py
# oder
pytest tests/ -v
```

---

## Roadmap

- [x] **v0.1** ET₀ (Tag + stündlich), Zonen-Defizit, Config-Flow, Persistenz
- [ ] **Ventile**: Zone optional an einen `switch`/`valve` koppeln
- [ ] **Strategien** (pluggable): „Morgens um 4 das Defizit nachgießen",
      Bewässerungsfenster, Max-Laufzeit, Regen-Sperre, Liter statt Minuten
- [ ] **Saisonale Kc-Kurven** (Frühling/Hochsommer/Hitzewelle)
- [x] **Historischer Backfill**: Tagesbilanz beim Einrichten aus der Recorder-
      History rekonstruieren; vergangene Tage als ET₀-Langzeitstatistik einspeisen
      (`iriy.backfill`, `days`-Parameter)
- [ ] **Editierbare Oberfläche** (eigenes Lovelace-Panel, z2m-Stil) für Zonen,
      Strategien und Live-Bilanz
- [ ] **HACS-Release** + Übersetzungen

---

## Dashboard: 7-Tage-Diagramm

Ein kombiniertes Wochen-Diagramm (Sonne, Wind, Regen, ET₀ in *einem* Chart mit
zwei Achsen) geht am einfachsten mit der
**[ApexCharts-Card](https://github.com/RomRider/apexcharts-card)** (über HACS
installieren). ET₀ kommt aus der **Langzeitstatistik** (dauerhaft, via Iriy-Import),
die Wetter-Serien aus der **Roh-History** per `group_by` (~letzte 10 Tage).

> **Entitäten:** ET₀ = `sensor.iriy_et0_tag` (heißt bei dieser Anlage so – im
> Zweifel in *Entwicklerwerkzeuge → Zustände* prüfen). Für Solar/Wind/Regen lesen
> wir bewusst per `group_by` aus der Roh-History statt aus der Tages-Langzeit­
> statistik – denn nicht jede Station erzeugt für diese Sensoren eine stündliche/
> tägliche LTS (bei GW3000A/WS90 fehlt sie aktuell). Passe die `sensor.gw3000a_*`-
> IDs an deine Station an.

```yaml
type: custom:apexcharts-card
graph_span: 7d
span:
  end: day
header:
  show: true
  title: Wetter & ET₀ (7 Tage)
yaxis:
  - id: mm
    min: 0
    decimals: 1
    apex_config:
      title: { text: mm }
  - id: env
    opposite: true            # rechte Achse
    min: 0
    apex_config:
      title: { text: "W/m² · m/s" }
series:
  - entity: sensor.iriy_et0_tag               # ET₀ mm/Tag – LTS via Iriy-Import
    name: ET₀
    type: column
    yaxis_id: mm
    statistics: { type: max, period: day }
  - entity: sensor.gw3000a_daily_rain_piezo   # Regen-Tageszähler → Tagesmaximum
    name: Regen
    type: column
    yaxis_id: mm
    group_by: { func: max, duration: 1d }
  - entity: sensor.gw3000a_solar_radiation    # Globalstrahlung W/m²
    name: Solar
    type: line
    yaxis_id: env
    group_by: { func: avg, duration: 1d }
  - entity: sensor.gw3000a_wind_speed         # Wind
    name: Wind
    type: line
    yaxis_id: env
    group_by: { func: avg, duration: 1d }
```

**Wichtig:** Bei top-level `yaxis:` *nicht* zusätzlich `yaxis` in `apex_config`
setzen (würde überschrieben). Defizit/Laufzeit je Zone zeigst du am besten mit
einer `tile`- oder `entities`-Karte (`sensor.iriy_<zone>_defizit`,
`sensor.iriy_<zone>_laufzeit`).

---

## Dank

Aufgebaut auf einem vorhandenen FAO-56-Ansatz (`smart_et`) für die Ecowitt
WS90 – Rechenkern erweitert (Stundengleichung), Architektur auf Config-Flow,
Koordinator und Zonen-Bilanz umgestellt.

## Lizenz

[AGPL-3.0](LICENSE) – frei nutzbar und veränderbar; Änderungen, die als
Netzwerkdienst betrieben werden, müssen ihren Quellcode offenlegen.
