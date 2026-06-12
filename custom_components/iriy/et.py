"""Iriy – reiner Rechenkern fuer die Referenz-Evapotranspiration (FAO-56).

Dieses Modul hat ABSICHTLICH keine Home-Assistant-Abhaengigkeiten, damit
der Kern isoliert getestet werden kann (siehe tests/test_et.py). Wer die
Formeln anpassen oder verstehen will, ist hier richtig.

Implementiert:
  * ET0 nach FAO-56 Penman-Monteith fuer den TAGESschritt (Gl. 6)
  * ET0 nach FAO-56 Penman-Monteith fuer SUB-TAEGLICHE Schritte (Gl. 53,
    z. B. stuendlich) inkl. korrekter extraterrestrischer Strahlung ueber
    ein beliebiges Zeitfenster (Gl. 28) aus der Sonnenposition.
  * Ableitung der Bewaesserungszeit aus einem Wasserdefizit.

Referenz: Allen, Pereira, Raes, Smith (1998), FAO Irrigation and Drainage
Paper 56 ("Crop evapotranspiration"). Gleichungsnummern beziehen sich
darauf.
"""
from __future__ import annotations

import math

# Stefan-Boltzmann-Konstante
SIGMA_DAY = 4.903e-9          # MJ K^-4 m^-2 Tag^-1
SIGMA_HOUR = SIGMA_DAY / 24.0  # MJ K^-4 m^-2 h^-1  (= 2.043e-10)
ALBEDO = 0.23                 # Albedo der Referenz-Grasflaeche (FAO-56)
GSC = 0.0820                  # Solarkonstante [MJ m^-2 min^-1]


# --- Grundgroessen ------------------------------------------------------

def saturation_vapor_pressure(t_c: float) -> float:
    """Saettigungsdampfdruck e0(T) in kPa (Gl. 11). Harte Funktion von T."""
    return 0.6108 * math.exp(17.27 * t_c / (t_c + 237.3))


def slope_svp(t_c: float) -> float:
    """Steigung der Saettigungsdampfdruckkurve Delta in kPa/degC (Gl. 13)."""
    return 4098 * saturation_vapor_pressure(t_c) / (t_c + 237.3) ** 2


def psychrometric_constant(pressure_kpa: float) -> float:
    """Psychrometerkonstante gamma in kPa/degC (Gl. 8)."""
    return 0.000665 * pressure_kpa


def atmospheric_pressure(elevation_m: float) -> float:
    """Luftdruck aus der Hoehe schaetzen [kPa] (Gl. 7).

    Fallback, falls kein echter Drucksensor vorliegt.
    """
    return 101.3 * ((293 - 0.0065 * elevation_m) / 293) ** 5.26


def wind_speed_at_2m(wind_ms: float, sensor_height_m: float) -> float:
    """Rechnet Wind von Sensorhoehe auf die FAO-Referenzhoehe 2 m um (Gl. 47)."""
    if abs(sensor_height_m - 2.0) < 1e-6:
        return wind_ms
    return wind_ms * 4.87 / math.log(67.8 * sensor_height_m - 5.42)


# --- Extraterrestrische Strahlung Ra -----------------------------------

def _solar_declination(doy: int) -> float:
    """Sonnendeklination delta [rad] (Gl. 24)."""
    return 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)


def _inverse_relative_distance(doy: int) -> float:
    """Inverse relative Erde-Sonne-Distanz dr (Gl. 23)."""
    return 1 + 0.033 * math.cos(2 * math.pi * doy / 365)


def _sunset_hour_angle(phi: float, decl: float) -> float:
    """Sonnenuntergangs-Stundenwinkel ws [rad] (Gl. 25), rundungsfest geklemmt."""
    x = max(-1.0, min(1.0, -math.tan(phi) * math.tan(decl)))
    return math.acos(x)


def _seasonal_correction(doy: int) -> float:
    """Saisonale Korrektur der Sonnenzeit Sc in Stunden (Gl. 32/33)."""
    b = 2 * math.pi * (doy - 81) / 364
    return 0.1645 * math.sin(2 * b) - 0.1255 * math.cos(b) - 0.025 * math.sin(b)


def extraterrestrial_radiation_daily(latitude_deg: float, doy: int) -> float:
    """Extraterrestrische Strahlung Ra [MJ m^-2 Tag^-1] (Gl. 21) – reine Astronomie."""
    phi = math.radians(latitude_deg)
    dr = _inverse_relative_distance(doy)
    decl = _solar_declination(doy)
    ws = _sunset_hour_angle(phi, decl)
    return (24 * 60 / math.pi) * GSC * dr * (
        ws * math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * math.sin(ws)
    )


def solar_time_angle(longitude_east_deg: float, doy: int, utc_hour: float) -> float:
    """Sonnenstundenwinkel omega [rad] zur UTC-Stunde (Gl. 31, aber aus UTC).

    Wir gehen bewusst ueber UTC + geografische Laenge statt ueber die
    Zeitzonen-Mittelmeridian-Formel der FAO – das vermeidet Sommerzeit-
    und Zeitzonen-Verwechslungen. omega = 0 zur lokalen Sonnen-Mittagszeit.
    """
    sc = _seasonal_correction(doy)
    solar_time = utc_hour + longitude_east_deg / 15.0 + sc
    return math.pi / 12.0 * (solar_time - 12.0)


def extraterrestrial_radiation_hour(
    latitude_deg: float,
    longitude_east_deg: float,
    doy: int,
    utc_hour_mid: float,
    period_hours: float = 1.0,
) -> float:
    """Ra ueber ein Zeitfenster [MJ m^-2 / Fenster] (Gl. 28).

    utc_hour_mid ist die UTC-Uhrzeit der Fenstermitte (z. B. 12.5 fuer das
    Fenster 12:00–13:00 UTC). period_hours ist die Fensterlaenge in Stunden.
    Nachts (Sonne unter Horizont) liefert die Funktion 0.
    """
    phi = math.radians(latitude_deg)
    dr = _inverse_relative_distance(doy)
    decl = _solar_declination(doy)
    omega = solar_time_angle(longitude_east_deg, doy, utc_hour_mid)
    half = math.pi * period_hours / 24.0  # halbe Fensterbreite im Stundenwinkel
    w1 = omega - half
    w2 = omega + half
    ws = _sunset_hour_angle(phi, decl)
    # Auf den Tageslichtbogen [-ws, ws] beschneiden.
    w1 = max(w1, -ws)
    w2 = min(w2, ws)
    if w2 <= w1:
        return 0.0
    ra = (12 * 60 / math.pi) * GSC * dr * (
        (w2 - w1) * math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * (math.sin(w2) - math.sin(w1))
    )
    return max(ra, 0.0)


# --- ET0: Tagesschritt --------------------------------------------------

def et0_daily(
    t_min: float,
    t_max: float,
    rh_mean: float,
    wind_ms: float,
    solar_w_m2: float,
    pressure_kpa: float,
    latitude_deg: float,
    elevation_m: float,
    day_of_year: int,
    wind_sensor_height_m: float = 2.0,
) -> float:
    """Referenz-Evapotranspiration ET0 in mm/Tag (FAO-56 Gl. 6).

    Erwartet TAGESWERTE:
      t_min, t_max      Tages-Min/Max [degC]
      rh_mean           mittlere Luftfeuchte [%]
      wind_ms           mittlerer Wind [m/s] (NICHT km/h!)
      solar_w_m2        mittlere Globalstrahlung [W/m2]
      pressure_kpa      Luftdruck [kPa]
    """
    t_mean = (t_min + t_max) / 2

    # Dampfdruck: es aus dem Mittel von e0(Tmax)/e0(Tmin) (Kruemmung!), ea ueber RH
    es = (saturation_vapor_pressure(t_max) + saturation_vapor_pressure(t_min)) / 2
    ea = es * rh_mean / 100
    vpd = es - ea

    delta = slope_svp(t_mean)
    gamma = psychrometric_constant(pressure_kpa)
    u2 = wind_speed_at_2m(wind_ms, wind_sensor_height_m)

    # Strahlungsbilanz
    ra = extraterrestrial_radiation_daily(latitude_deg, day_of_year)
    rs = solar_w_m2 * 0.0864  # mittlere W/m2 -> MJ/m2/Tag
    rso = (0.75 + 2e-5 * elevation_m) * ra
    rs_rso = min(rs / rso, 1.0) if rso > 0 else 0.5
    rns = (1 - ALBEDO) * rs
    rnl = (
        SIGMA_DAY
        * (((t_max + 273.16) ** 4 + (t_min + 273.16) ** 4) / 2)
        * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
        * (1.35 * rs_rso - 0.35)
    )
    rn = max(rns - rnl, 0.0)

    numerator = 0.408 * delta * rn + gamma * 900 / (t_mean + 273) * u2 * vpd
    denominator = delta + gamma * (1 + 0.34 * u2)
    return max(numerator / denominator, 0.0)


# --- ET0: Sub-taeglicher Schritt (stuendlich) ---------------------------

def et0_hourly(
    t_air: float,
    rh: float,
    wind_ms: float,
    solar_w_m2: float,
    pressure_kpa: float,
    latitude_deg: float,
    longitude_east_deg: float,
    elevation_m: float,
    day_of_year: int,
    utc_hour_mid: float,
    period_hours: float = 1.0,
    wind_sensor_height_m: float = 2.0,
    night_cloud_ratio: float = 0.8,
) -> float:
    """Referenz-ET ueber ein sub-taegliches Fenster in mm/Fenster (FAO-56 Gl. 53).

    Erwartet MITTELWERTE ueber das Fenster (typisch 1 h):
      t_air         Lufttemperatur [degC]
      rh            relative Luftfeuchte [%]
      wind_ms       Wind [m/s]
      solar_w_m2    mittlere Globalstrahlung im Fenster [W/m2]
      pressure_kpa  Luftdruck [kPa]
      utc_hour_mid  UTC-Uhrzeit der Fenstermitte [h]
      period_hours  Fensterlaenge [h]

    Rueckgabe kann nachts leicht negativ sein (Taubildung) – die
    aufrufende Schicht klemmt fuer die Defizit-Bilanz bei 0.
    night_cloud_ratio ist das ersatzweise Rs/Rso fuer die Nacht (FAO
    empfiehlt den Wert aus den letzten Tagesstunden; 0.8 ist robust).
    """
    es = saturation_vapor_pressure(t_air)
    ea = es * rh / 100
    vpd = es - ea

    delta = slope_svp(t_air)
    gamma = psychrometric_constant(pressure_kpa)
    u2 = wind_speed_at_2m(wind_ms, wind_sensor_height_m)

    # Energie im Fenster: mittlere W/m2 -> MJ/m2 (1 W/m2 * 3600 s = 3600 J = 3.6e-3 MJ)
    rs = solar_w_m2 * 0.0036 * period_hours

    ra = extraterrestrial_radiation_hour(
        latitude_deg, longitude_east_deg, day_of_year, utc_hour_mid, period_hours
    )
    daytime = ra > 0.0
    rso = (0.75 + 2e-5 * elevation_m) * ra
    if rso > 0 and rs > 0:
        ratio = min(max(rs / rso, 0.25), 1.0)
    else:
        ratio = night_cloud_ratio
    fcd = 1.35 * ratio - 0.35

    rns = (1 - ALBEDO) * rs
    rnl = (
        SIGMA_HOUR
        * ((t_air + 273.16) ** 4)
        * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
        * fcd
    )
    rn = rns - rnl

    # Bodenwaermestrom G und Kd/Kn-Koeffizienten je nach Tag/Nacht (FAO-56 Gl. 53)
    if daytime:
        g = 0.1 * rn
        cd = 0.24
    else:
        g = 0.5 * rn
        cd = 0.96
    cn = 37.0

    numerator = 0.408 * delta * (rn - g) + gamma * cn / (t_air + 273) * u2 * vpd
    denominator = delta + gamma * (1 + cd * u2)
    return numerator / denominator


# --- Bewaesserung -------------------------------------------------------

def irrigation_minutes(
    deficit_mm: float, throughput_mm_h: float, efficiency: float = 1.0
) -> float:
    """Bewaesserungszeit in Minuten, um ein Defizit auszugleichen.

    deficit_mm        auszugleichendes Wasserdefizit [mm]
    throughput_mm_h   Niederschlagsrate des Auslasses [mm/h]
    efficiency        Bewaesserungs-Wirkungsgrad 0..1 (Verdunstung/Abfluss)
    """
    if throughput_mm_h <= 0 or efficiency <= 0:
        return 0.0
    deficit = max(deficit_mm, 0.0)
    return deficit / (throughput_mm_h * efficiency) * 60
