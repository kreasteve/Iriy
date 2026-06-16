# 🌱 Iriy – Smart Irrigation für Home Assistant

**Iriy** (Bewässerungs-Bot, vom englischen *irrigation*) berechnet aus den
Sensoren deiner Wetterstation die **Referenz-Verdunstung (ET₀)** nach
**FAO-56 Penman-Monteith** und führt pro Bewässerungszone eine laufende
**Wasser-Defizit-Bilanz**. Daraus leitet sie ab, wie viel nachgegossen werden
muss – und steuert das später automatisch (Ventile, Strategien).

> Status: **v0.2 – Fundament + eigene Oberfläche.** ET₀ (Tag + stündlich live)
> und Zonen-Defizit funktionieren, und es gibt ein **eigenes Panel in der
> Seitenleiste** (Übersicht, Verlauf, Tabelle, Zonen-Editor). Ventilsteuerung
> und Strategien sind als saubere Erweiterungspunkte vorbereitet (siehe
> [Roadmap](#roadmap)).

---

## Was Iriy kann

- Liest die Rohsensoren deiner Station (Temperatur, Feuchte, Wind, Strahlung,
  optional Druck und Regen) – komplett **per UI** ausgewählt, kein YAML.
- **Zwei ET-Spuren**, beide nützlich:
  - **Tageswert** (kanonisch, vertrauenswürdig): Summe der FAO-56-Stunden­
    gleichung (Gl. 53) über die **Stundenstatistik des Recorders** – zeit­
    gewichtet und lückenrobust (unabhängig davon, ob HA durchlief). Die Zahl
    für die Morgen-Automation; wird um Mitternacht für den abgeschlossenen Tag
    finalisiert. (Die reine Tagesmittel-Gleichung dient nur noch als Fallback
    ohne Stundenmodus.)
  - **Stündlich live**: dieselbe Stundengleichung, die sich zu „heute bisher"
    aufsummiert und das Zonen-Defizit in Echtzeit speist – reagiert sofort
    auf Regen.
- **Eigener-Tag-Datierung**: Der ET₀ von Tag *D* liegt auf Tag *D* selbst
  (lokale Mitternacht), ein Wert pro Tag – die Tageshistorie ist damit korrekt
  datiert und nicht „um einen Tag verschoben".
- **Einmalige Historie beim Einrichten** (optional): Iriy befüllt aus der
  vorhandenen Recorder-History die letzten X Tage ET₀ vor – danach pflegt es
  den Verlauf selbst. Kein Service, kein Button, kein laufender Import.
- Pro Zone ein **Defizit-Bucket** (mm) und eine **empfohlene Laufzeit** (min).
- Übersteht Neustarts (persistente Bilanz via HA-Storage).
- Services: `iriy.recalculate`, `iriy.reset_bucket`, `iriy.add_water`.

### Erzeugte Entitäten

| Entität | Bedeutung |
|---|---|
| `sensor.iriy_et0_gestern` | kanonischer ET₀-Tageswert [mm/Tag] – trägt die korrekt datierte Tageshistorie |
| `sensor.iriy_et0_heute` | heute bisher aufsummiert [mm] |
| `sensor.iriy_et0_rate` | aktuelle ET-Rate [mm/h] |
| `sensor.iriy_<zone>_defizit` | Wasserdefizit der Zone [mm] |
| `sensor.iriy_<zone>_laufzeit` | nötige Bewässerungszeit [min] |

---

## Die Oberfläche (Sidebar-Panel)

Iriy bringt einen **eigenen Eintrag „Iriy" in der HA-Seitenleiste** mit – eine
eigenständige Oberfläche (kein Lovelace-Dashboard nötig), die auf allen HA-
Installationsarten läuft (`panel_custom`, Teil des Pakets):

- **Übersicht**: ET₀ gestern / heute / Rate plus Wetter-Diagnose
  (Temperatur, Feuchte, Wind, Strahlung, Regen).
- **Verlauf**: 7-Tage-Balkendiagramm der ET₀-Tageswerte.
- **Tabelle**: die letzten Tage als Liste – aus derselben Tagesstatistik wie
  das Diagramm, also deckungsgleich.
- **Zonen-Editor**: Zonen direkt im Panel **anlegen, bearbeiten und löschen**
  (Name, Kc mit Vorschlagsliste, Fläche m², Durchfluss, Wirkungsgrad,
  Max-Defizit).

Sensoren und Standort werden weiterhin beim Einrichten bzw. übers Zahnrad
(Optionen) gepflegt.

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
| `panel.py` | Sidebar-Panel (`panel_custom`) + WebSocket-API für die eigene Oberfläche. |
| `frontend/iriy-panel.js` | Die Web-Component der Oberfläche (Vanilla, ohne Build-Kette). |
| `sensor.py` | Entitäten aus dem Koordinator. |
| `const.py` | Konstanten, Standardwerte, Kc-Referenztabelle. |

**Was wo editierbar ist:**
- **Zonen** → direkt im Iriy-Panel (Seitenleiste).
- **Sensoren / Standort / Parameter** → über die HA-UI (Zahnrad / Optionen).
- **Die Formel selbst** → `et.py` (sauber getrennt, getestet).

---

## Installation

### HACS (custom repository)
`https://github.com/kreasteve/Iriy` als benutzerdefiniertes Repository
(Kategorie *Integration*) hinzufügen, installieren und Home Assistant neu
starten.

### Manuell
1. Ordner `custom_components/iriy/` nach `<config>/custom_components/` kopieren.
2. Home Assistant neu starten.

### Einrichten
1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Iriy"**.
2. Sensoren und Standort auswählen, optional die Historie der letzten Tage
   vorbefüllen lassen.
3. Der Eintrag **„Iriy"** erscheint in der Seitenleiste – dort Zonen anlegen.

> Nach einem Update, das das Panel neu registriert, einmal Home Assistant
> neu starten, damit die Seitenleiste den Eintrag zieht (Browser ggf. hart
> neu laden).

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
- [x] **Historie beim Einrichten** aus der Recorder-History rekonstruieren und
      als korrekt datierte ET₀-Tagesstatistik einspeisen (einmalig, automatisch)
- [x] **v0.2 – Eigenes Sidebar-Panel**: Übersicht, 7-Tage-Verlauf + Tabelle,
      Zonen-Editor
- [ ] **Einstellungen im Panel** (Sensoren/Parameter direkt dort ändern)
- [ ] **Ventile**: Zone optional an einen `switch`/`valve` koppeln
- [ ] **Strategien** (pluggable): „Morgens um 4 das Defizit nachgießen",
      Bewässerungsfenster, Max-Laufzeit, Regen-Sperre, Liter statt Minuten
- [ ] **Saisonale Kc-Kurven** (Frühling/Hochsommer/Hitzewelle)

---

## Alternative: ET₀ im Dashboard (Statistik-Karte)

Wer den Verlauf zusätzlich im eigenen Dashboard möchte: Iriy schreibt den
ET₀-Tagesverlauf direkt in die Entität `sensor.iriy_et0_gestern` als korrekt
datierte Tagesstatistik (ein Wert pro Tag, auf dem eigenen Tag). Die Entität
trägt bewusst **kein** `state_class`, damit HA sie nicht zusätzlich – und um
einen Tag verschoben – selbst aufzeichnet. Anzeige mit einer **Statistik-Karte**
(Bordmittel, keine HACS-Karte nötig):

```yaml
type: statistics-graph
title: ET0 (gestern) – Verlauf
chart_type: bar
period: day
days_to_show: 14
stat_types:
  - mean
entities:
  - sensor.iriy_et0_gestern   # entity_id je nach HA-Sprache (engl.: ..._yesterday)
```

> Hinweis: In der HA-Verlaufsansicht (More-Info) wird ein Tageswert wegen der
> Stunden-Bucket-Beschriftung mitunter bei 01:00 angezeigt – die Daten liegen
> aber korrekt auf 00:00. Die Statistik-Karte mit `period: day` beschriftet
> nach Datum und zeigt das sauber.

Ein kombiniertes **Wetter**-Diagramm (Sonne, Wind, Regen) mit zwei Achsen geht mit
der **[ApexCharts-Card](https://github.com/RomRider/apexcharts-card)** (über HACS).
Passe die `sensor.gw3000a_*`-IDs an deine Station an.

```yaml
type: custom:apexcharts-card
graph_span: 7d
span:
  end: day
header:
  show: true
  title: Wetter (7 Tage)
yaxis:
  - id: mm
    min: 0
    decimals: 1
    apex_config:
      title: { text: "Regen mm" }
  - id: env
    opposite: true            # rechte Achse
    min: 0
    apex_config:
      title: { text: "W/m² · m/s" }
series:
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
setzen (würde überschrieben).

---

## Lizenz

[AGPL-3.0](LICENSE) – frei nutzbar und veränderbar; Änderungen, die als
Netzwerkdienst betrieben werden, müssen ihren Quellcode offenlegen.
