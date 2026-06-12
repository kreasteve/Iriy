"""Tests fuer den Iriy-Rechenkern (custom_components/iriy/et.py).

Laeuft mit pytest ODER direkt:  python3 tests/test_et.py

Referenzfaelle aus FAO-56 (Allen et al. 1998):
  * Beispiel 18 (Bruessel, 6. Juli)   -> ET0 ~ 3.9 mm/Tag
  * Beispiel 19 (N'Diaye, 1. Oktober) -> ET0 ~ 0.63 mm/h fuer 14:00-15:00
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "iriy"))

import et  # noqa: E402


def test_saturation_vapor_pressure():
    # FAO-56: e0(20 degC) ~ 2.338 kPa
    assert math.isclose(et.saturation_vapor_pressure(20), 2.338, abs_tol=0.01)


def test_slope_svp():
    # FAO-Tabelle: Delta(20) ~ 0.145 kPa/degC
    assert math.isclose(et.slope_svp(20), 0.1448, abs_tol=0.001)


def test_psychrometric_constant():
    assert math.isclose(et.psychrometric_constant(101.3), 0.0674, abs_tol=0.0002)


def test_atmospheric_pressure():
    # FAO-56: 1800 m -> ~81.8 kPa, Meereshoehe ~101.3 kPa
    assert math.isclose(et.atmospheric_pressure(0), 101.3, abs_tol=0.1)
    assert math.isclose(et.atmospheric_pressure(1800), 81.8, abs_tol=0.2)


def test_wind_correction():
    assert et.wind_speed_at_2m(3.0, 2.0) == 3.0
    # Faktor bei 10 m ~ 0.748
    assert math.isclose(et.wind_speed_at_2m(1.0, 10.0), 0.748, abs_tol=0.005)
    # Faktor bei 13 m ~ 0.719
    assert math.isclose(et.wind_speed_at_2m(1.0, 13.0), 0.719, abs_tol=0.005)


def test_ra_daily_reasonable():
    # Sommer auf der Nordhalbkugel: Ra deutlich groesser als im Winter
    ra_summer = et.extraterrestrial_radiation_daily(49.4, 180)
    ra_winter = et.extraterrestrial_radiation_daily(49.4, 350)
    assert 38 < ra_summer < 43        # ~41 MJ/m2/d
    assert 7 < ra_winter < 12         # ~9 MJ/m2/d


def test_ra_hourly_sum_matches_daily():
    """Summe von 24 Stunden-Ra muss dem Tages-Ra entsprechen (Gl. 28 vs 21)."""
    lat, lon, doy = 49.404, 10.78, 180
    hourly_sum = sum(
        et.extraterrestrial_radiation_hour(lat, lon, doy, h + 0.5, 1.0)
        for h in range(24)
    )
    daily = et.extraterrestrial_radiation_daily(lat, doy)
    assert math.isclose(hourly_sum, daily, rel_tol=0.03), (
        f"hourly_sum={hourly_sum:.2f} vs daily={daily:.2f}"
    )


def test_fao_example18_brussels_daily():
    """FAO-56 Beispiel 18: ET0 ~ 3.9 mm/Tag."""
    es = (
        et.saturation_vapor_pressure(21.5) + et.saturation_vapor_pressure(12.3)
    ) / 2
    rh = 1.409 / es * 100           # so dass ea ~ 1.409 kPa
    solar_w = 22.07 / 0.0864        # MJ/m2/d zurueck nach W/m2
    et0 = et.et0_daily(
        t_min=12.3,
        t_max=21.5,
        rh_mean=rh,
        wind_ms=2.078,
        solar_w_m2=solar_w,
        pressure_kpa=100.1,
        latitude_deg=50.8,
        elevation_m=100,
        day_of_year=187,
        wind_sensor_height_m=2.0,
    )
    assert 3.5 < et0 < 4.3, f"ET0={et0}, erwartet ~3.9"


def test_summer_day_central_europe_daily():
    et0 = et.et0_daily(
        t_min=14,
        t_max=27,
        rh_mean=60,
        wind_ms=2.0,
        solar_w_m2=280,
        pressure_kpa=96.9,
        latitude_deg=49.404,
        elevation_m=373,
        day_of_year=180,
        wind_sensor_height_m=13.0,
    )
    assert 3.0 < et0 < 6.0, f"ET0={et0} unplausibel"


def test_fao_example19_ndiaye_hourly():
    """FAO-56 Beispiel 19, 14:00-15:00: ET0 ~ 0.63 mm/h."""
    solar_w = 2.450 / 0.0036        # Rs=2.450 MJ/m2/h -> W/m2
    et0 = et.et0_hourly(
        t_air=38.0,
        rh=52.0,
        wind_ms=3.3,
        solar_w_m2=solar_w,
        pressure_kpa=et.atmospheric_pressure(8),
        latitude_deg=16.217,
        longitude_east_deg=-16.25,
        elevation_m=8,
        day_of_year=274,
        utc_hour_mid=14.5,
        period_hours=1.0,
        wind_sensor_height_m=2.0,
    )
    assert 0.55 < et0 < 0.78, f"ET0={et0} mm/h, erwartet ~0.63"


def test_hourly_midday_positive():
    et0 = et.et0_hourly(
        t_air=28.0,
        rh=50.0,
        wind_ms=2.0,
        solar_w_m2=700.0,
        pressure_kpa=96.9,
        latitude_deg=49.404,
        longitude_east_deg=10.78,
        elevation_m=373,
        day_of_year=180,
        utc_hour_mid=11.5,   # ~Sonnenmittag Ortszeit
        period_hours=1.0,
        wind_sensor_height_m=2.0,
    )
    assert 0.2 < et0 < 1.0, f"Mittags-ET0={et0} unplausibel"


def test_hourly_night_near_zero():
    et0 = et.et0_hourly(
        t_air=14.0,
        rh=92.0,
        wind_ms=1.0,
        solar_w_m2=0.0,
        pressure_kpa=96.9,
        latitude_deg=49.404,
        longitude_east_deg=10.78,
        elevation_m=373,
        day_of_year=180,
        utc_hour_mid=0.5,    # tiefe Nacht (UTC ~ Ortssonnenzeit 1:15)
        period_hours=1.0,
        wind_sensor_height_m=2.0,
    )
    assert abs(et0) < 0.1, f"Nacht-ET0={et0} sollte ~0 sein"


def test_irrigation_minutes():
    # Defizit 3 mm, 20 mm/h, Wirkungsgrad 1 -> 9 min
    assert math.isclose(et.irrigation_minutes(3.0, 20.0, 1.0), 9.0, abs_tol=0.01)
    # Wirkungsgrad 0.5 verdoppelt die Zeit
    assert math.isclose(et.irrigation_minutes(3.0, 20.0, 0.5), 18.0, abs_tol=0.01)
    # Kein Defizit -> 0
    assert et.irrigation_minutes(0.0, 20.0, 1.0) == 0.0


# --- Standalone-Runner (ohne pytest) -----------------------------------

if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(funcs)
    print(f"\n{total - failed}/{total} Tests bestanden.")
    sys.exit(1 if failed else 0)
