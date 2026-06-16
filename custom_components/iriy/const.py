"""Konstanten der Iriy-Integration.

Standardwerte (Standort, Pflanzen-Koeffizienten) sind hier; die echte
Konfiguration kommt aus dem Config-Flow (UI) und liegt im ConfigEntry.
"""
from __future__ import annotations

DOMAIN = "iriy"
PLATFORMS = ["sensor"]

# --- Config-/Options-Schluessel ----------------------------------------
# Standort
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_ELEVATION = "elevation"

# Quell-Entities (Wetterstation)
CONF_TEMP = "temperature"
CONF_HUMIDITY = "humidity"
CONF_WIND = "wind"
CONF_WIND_UNIT = "wind_unit"          # "km/h" oder "m/s"
CONF_WIND_HEIGHT = "wind_height"      # Sensorhoehe in m
CONF_SOLAR = "solar"
CONF_PRESSURE = "pressure"            # optional; sonst aus Hoehe geschaetzt
CONF_PRESSURE_UNIT = "pressure_unit"  # "hPa" oder "kPa"
CONF_RAIN = "rain"                    # optional
CONF_RAIN_MODE = "rain_mode"          # "cumulative_daily" | "rate" | "incremental"

# Berechnung
CONF_HOURLY = "hourly"                # sub-taegliche Live-Berechnung an/aus
CONF_UPDATE_MINUTES = "update_minutes"

# Historischer Import beim Einrichten
CONF_IMPORT_HISTORY = "import_history"  # Haken: vergangene Tage importieren
CONF_HISTORY_DAYS = "history_days"      # wie viele Tage rueckwirkend

# Zonen (Liste von Dicts in options[CONF_ZONES])
CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_ZONE_KC = "kc"
CONF_ZONE_THROUGHPUT = "throughput"   # mm/h
CONF_ZONE_AREA = "area"               # m2 (optional, fuer spaetere Liter-Bilanz)
CONF_ZONE_MAX_DEFICIT = "max_deficit"  # mm, Kappung des Buckets (RAW)
CONF_ZONE_EFFICIENCY = "efficiency"   # 0..1
CONF_ZONE_BY_AREA = "by_area"         # True: nur Liter ueber Flaeche, KEINE Laufzeit
CONF_ZONE_CALC_LITERS = "calc_liters"  # True: ausgebrachte Menge RECHNEN statt vom Ventil messen
CONF_ZONE_VALVE = "valve"             # optional: switch-Entity des Ventils (z2m)
CONF_ZONE_INTERVAL_DAYS = "interval_days"  # spaetestens alle N Tage giessen
CONF_ZONE_TRIGGER = "trigger"         # Gieß-Schwelle (Wert), Einheit s. trigger_unit
CONF_ZONE_TRIGGER_UNIT = "trigger_unit"  # "mm" | "L" – Eingabe-Einheit der Schwelle

# Automatik (autonomes Giessen)
CONF_AUTO_IRRIGATE = "auto_irrigate"  # Automatik an/aus (Default AUS – Sicherheit)
CONF_AUTO_HOUR = "auto_hour"          # Uhrzeit (Stunde 0-23), zu der entschieden wird
CONF_RAIN_SKIP_MM = "rain_skip_mm"    # Forecast-Regen >= mm -> um einen Tag verschieben
CONF_WEATHER_ENTITY = "weather_entity"  # weather.* fuer die Tages-Vorhersage

# z2m-Ventilsteuerung (GiEX/Tuya cyclic irrigation)
DEFAULT_Z2M_BASE_TOPIC = "zigbee2mqtt"
VALVE_VOLUME_SUFFIX = "_daily_irrigation_volume"  # Mess-Sensor am selben Geraet

# --- Neutrale Fallback-Standardwerte -----------------------------------
# Nur Rueckfall, falls der HA-Standort leer ist. Der echte Standort wird im
# Config-Flow aus hass.config vorbelegt und dort eingestellt.
DEFAULT_LATITUDE = 51.0
DEFAULT_LONGITUDE = 10.0
DEFAULT_ELEVATION = 0.0
DEFAULT_WIND_HEIGHT = 10.0
DEFAULT_WIND_UNIT = "km/h"
DEFAULT_PRESSURE_UNIT = "hPa"
DEFAULT_RAIN_MODE = "cumulative_daily"
DEFAULT_HOURLY = True
DEFAULT_UPDATE_MINUTES = 60
MIN_UPDATE_MINUTES = 5
DEFAULT_BACKFILL_DAYS = 30  # so weit zurueck wie History reicht (gekappt durch Datenlage)
DEFAULT_IMPORT_HISTORY = True

DEFAULT_THROUGHPUT = 20.0   # mm/h Tropfschlauch
DEFAULT_MAX_DEFICIT = 30.0  # mm bis "Welkepunkt"
DEFAULT_EFFICIENCY = 0.9

# Automatik-Defaults (bewusst konservativ: AUS, nachts, mit Regen-Sperre)
DEFAULT_AUTO_IRRIGATE = False
DEFAULT_AUTO_HOUR = 4           # 04:00 Uhr
DEFAULT_RAIN_SKIP_MM = 3.0      # ab 3 mm Vorhersage einen Tag warten
DEFAULT_INTERVAL_DAYS = 3       # spaetestens alle 3 Tage giessen
DEFAULT_TRIGGER_UNIT = "mm"
# Wenn keine Gieß-Schwelle gesetzt ist: bei diesem Anteil vom Max-Defizit giessen.
DEFAULT_TRIGGER_FRACTION = 0.667
# Bagatell-Grenze: Tagesmengen unter diesem mm-Defizit lohnen kein Giessen.
AUTO_MIN_DEFICIT_MM = 1.0

# Storage
STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN  # je Entry wird ".{entry_id}" angehaengt

# --- Referenz-Kc-Tabelle (etablierte Pflanzen, Hauptsaison) ------------
# Nur Vorschlagswerte fuer die UI; die echten Kc stehen pro Zone im Entry.
DEFAULT_KC = {
    "rasen": 1.00,
    "amberbaum": 0.75,
    "rotbuche": 0.80,
    "obstbaeume": 0.75,
    "steinobst": 0.80,
    "himbeeren": 0.90,
    "brombeeren": 0.85,
    "johannisbeere": 0.85,
    "rosen": 0.90,
    "jungpflanzung": 0.55,
    "gemuese": 0.95,
}
