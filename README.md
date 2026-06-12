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
  - **Tageswert** (kanonisch, vertrauenswürdig): FAO-56-Tagesgleichung über die
    Min/Max/Mittel des Tages – die Zahl für die Morgen-Automation.
  - **Stündlich live**: FAO-56-Stundengleichung (Gl. 53), die sich zu „heute
    bisher" aufsummiert und das Zonen-Defizit in Echtzeit speist – reagiert
    sofort auf Regen.
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

## Dank

Aufgebaut auf einem vorhandenen FAO-56-Ansatz (`smart_et`) für die Ecowitt
WS90 – Rechenkern erweitert (Stundengleichung), Architektur auf Config-Flow,
Koordinator und Zonen-Bilanz umgestellt.

## Lizenz

[AGPL-3.0](LICENSE) – frei nutzbar und veränderbar; Änderungen, die als
Netzwerkdienst betrieben werden, müssen ihren Quellcode offenlegen.
