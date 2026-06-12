"""Konstanten der Iriy-Integration.

Standardwerte (Standort, Pflanzen-Koeffizienten) sind hier; die echte
Konfiguration kommt aus dem Config-Flow (UI) und liegt im ConfigEntry.
"""
from __future__ import annotations

from datetime import timedelta

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

# Zonen (Liste von Dicts in options[CONF_ZONES])
CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_ZONE_KC = "kc"
CONF_ZONE_THROUGHPUT = "throughput"   # mm/h
CONF_ZONE_AREA = "area"               # m2 (optional, fuer spaetere Liter-Bilanz)
CONF_ZONE_MAX_DEFICIT = "max_deficit"  # mm, Kappung des Buckets (RAW)
CONF_ZONE_EFFICIENCY = "efficiency"   # 0..1
CONF_ZONE_VALVE = "valve"             # optional: switch/valve-Entity (Zukunft)

# --- Standardwerte (Grosshabersdorf) -----------------------------------
DEFAULT_LATITUDE = 49.404
DEFAULT_LONGITUDE = 10.78
DEFAULT_ELEVATION = 373.0
DEFAULT_WIND_HEIGHT = 13.0
DEFAULT_WIND_UNIT = "km/h"
DEFAULT_PRESSURE_UNIT = "hPa"
DEFAULT_RAIN_MODE = "cumulative_daily"
DEFAULT_HOURLY = True
DEFAULT_UPDATE_MINUTES = 60
MIN_UPDATE_MINUTES = 5

DEFAULT_THROUGHPUT = 20.0   # mm/h Tropfschlauch
DEFAULT_MAX_DEFICIT = 30.0  # mm bis "Welkepunkt"
DEFAULT_EFFICIENCY = 0.9

# Storage
STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN  # je Entry wird ".{entry_id}" angehaengt

SCAN_INTERVAL = timedelta(minutes=DEFAULT_UPDATE_MINUTES)

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
