"""Iriy-Koordinator: sammelt Sensorwerte und rechnet ET0 + Defizit-Bilanz.

Aufbau (bewusst zukunftsoffen):

  Rohsensoren  ->  Akkumulatoren (Tag + Intervall)  ->  ET0
                                                   |
                                                   v
                            Zonen-"Buckets" (Wasserdefizit in mm)
                                                   |
                                                   v
                       (spaeter) Strategien -> Ventile ansteuern

Zwei parallele ET-Spuren, beide nuetzlich:
  * Tagesspur  : kanonischer FAO-56-Tageswert des zuletzt abgeschlossenen
                 Tages -> vertrauenswuerdig fuer die Morgen-Automation.
  * Sub-Tagesspur (stuendlich): live aufsummiert ("heute bisher") und
    speist die Defizit-Buckets in Echtzeit (reagiert sofort auf Regen).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
import homeassistant.util.dt as dt_util

from . import et
from .const import (
    CONF_ELEVATION,
    CONF_HOURLY,
    CONF_HUMIDITY,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PRESSURE,
    CONF_PRESSURE_UNIT,
    CONF_RAIN,
    CONF_RAIN_MODE,
    CONF_SOLAR,
    CONF_TEMP,
    CONF_UPDATE_MINUTES,
    CONF_WIND,
    CONF_WIND_HEIGHT,
    CONF_WIND_UNIT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_KC,
    CONF_ZONE_MAX_DEFICIT,
    CONF_ZONE_NAME,
    CONF_ZONE_THROUGHPUT,
    CONF_ZONES,
    DEFAULT_EFFICIENCY,
    DEFAULT_ELEVATION,
    DEFAULT_HOURLY,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_MAX_DEFICIT,
    DEFAULT_PRESSURE_UNIT,
    DEFAULT_RAIN_MODE,
    DEFAULT_THROUGHPUT,
    DEFAULT_UPDATE_MINUTES,
    DEFAULT_WIND_HEIGHT,
    DEFAULT_WIND_UNIT,
    DOMAIN,
    MIN_UPDATE_MINUTES,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel-Werte, ab denen wir eine Quelle als "nicht messend" behandeln.
_INVALID_STATES = {"unknown", "unavailable", "none", ""}


class _Acc:
    """Sammelt Min/Max/Mittel/Letztwert eines Sensors ueber ein Fenster."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._min: float | None = None
        self._max: float | None = None
        self._sum = 0.0
        self._count = 0
        self.last: float | None = None

    def add(self, value: float) -> None:
        self._min = value if self._min is None else min(self._min, value)
        self._max = value if self._max is None else max(self._max, value)
        self._sum += value
        self._count += 1
        self.last = value

    @property
    def minimum(self) -> float | None:
        return self._min

    @property
    def maximum(self) -> float | None:
        return self._max

    @property
    def mean(self) -> float | None:
        return self._sum / self._count if self._count else None

    def as_dict(self) -> dict:
        return {
            "min": self._min,
            "max": self._max,
            "sum": self._sum,
            "count": self._count,
            "last": self.last,
        }

    def load(self, data: dict | None) -> None:
        if not data:
            return
        self._min = data.get("min")
        self._max = data.get("max")
        self._sum = data.get("sum", 0.0)
        self._count = data.get("count", 0)
        self.last = data.get("last")


@dataclass
class ZoneState:
    """Laufzeit-Zustand einer Bewaesserungszone."""

    name: str
    kc: float
    throughput: float
    efficiency: float
    max_deficit: float
    deficit: float = 0.0          # aktuelles Wasserdefizit [mm]
    etc_today: float = 0.0        # Pflanzenbedarf heute [mm]
    last_etc: float = 0.0         # Bedarf im letzten Intervall [mm]

    @property
    def runtime_minutes(self) -> float:
        return round(
            et.irrigation_minutes(self.deficit, self.throughput, self.efficiency), 0
        )


@dataclass
class IriyData:
    """Snapshot, den die Sensor-Entities lesen."""

    et0_daily: float | None = None          # letzter abgeschlossener Tag [mm]
    et0_daily_provisional: float | None = None  # Tag bisher [mm]
    et0_today: float | None = None          # sub-taeglich aufsummiert [mm]
    et0_rate: float | None = None           # letztes Intervall [mm/h]
    zones: dict[str, ZoneState] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class IriyCoordinator(DataUpdateCoordinator[IriyData]):
    """Zentrale Datendrehscheibe der Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        minutes = max(
            MIN_UPDATE_MINUTES,
            int(self._opt(CONF_UPDATE_MINUTES, DEFAULT_UPDATE_MINUTES)),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.title}",
            update_interval=timedelta(minutes=minutes),
        )
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self._unsubs: list = []

        # Standort
        self._lat = float(self._opt(CONF_LATITUDE, DEFAULT_LATITUDE))
        self._lon = float(self._opt(CONF_LONGITUDE, DEFAULT_LONGITUDE))
        self._elev = float(self._opt(CONF_ELEVATION, DEFAULT_ELEVATION))
        self._wind_h = float(self._opt(CONF_WIND_HEIGHT, DEFAULT_WIND_HEIGHT))
        self._hourly = bool(self._opt(CONF_HOURLY, DEFAULT_HOURLY))

        # Quell-Entities + Einheiten
        self._src = {
            CONF_TEMP: self._opt(CONF_TEMP),
            CONF_HUMIDITY: self._opt(CONF_HUMIDITY),
            CONF_WIND: self._opt(CONF_WIND),
            CONF_SOLAR: self._opt(CONF_SOLAR),
            CONF_PRESSURE: self._opt(CONF_PRESSURE),
            CONF_RAIN: self._opt(CONF_RAIN),
        }
        self._wind_unit = self._opt(CONF_WIND_UNIT, DEFAULT_WIND_UNIT)
        self._pressure_unit = self._opt(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT)
        self._rain_mode = self._opt(CONF_RAIN_MODE, DEFAULT_RAIN_MODE)

        # Akkumulatoren: einer pro Spur (Tag bleibt bis Mitternacht, Intervall
        # wird nach jedem Tick geleert).
        self._day = {k: _Acc() for k in ("temp", "rh", "wind", "solar", "pressure")}
        self._iv = {k: _Acc() for k in ("temp", "rh", "wind", "solar", "pressure")}

        # Regen
        self._rain_last: float | None = None      # letzter Zaehlerstand
        self._rain_day = 0.0                       # Summe heute [mm]
        self._rain_iv = 0.0                        # Summe seit letztem Tick [mm]
        self._rain_rate_iv = _Acc()                # mittlere mm/h im Intervall (rate-Modus)

        # ET-Spuren
        self.et0_daily: float | None = None
        self.et0_today = 0.0
        self.et0_rate: float | None = None
        self._last_tick: datetime | None = None
        # Datum, zu dem die Tages-Akkumulatoren gehoeren (lokale ISO-Datum).
        # Dient dem Tageswechsel-Abgleich beim Laden nach einem Neustart.
        self._current_day: str | None = None

        # Zonen
        self.zones: dict[str, ZoneState] = {}
        self._build_zones()

    # --- Konfig-Helfer --------------------------------------------------

    def _opt(self, key: str, default=None):
        """Options haben Vorrang vor Daten (Options-Flow ueberschreibt Setup)."""
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    def _build_zones(self) -> None:
        existing = {n: z.deficit for n, z in self.zones.items()}
        self.zones = {}
        for raw in self._opt(CONF_ZONES, []) or []:
            name = raw[CONF_ZONE_NAME]
            zone = ZoneState(
                name=name,
                kc=float(raw.get(CONF_ZONE_KC, 1.0)),
                throughput=float(raw.get(CONF_ZONE_THROUGHPUT, DEFAULT_THROUGHPUT)),
                efficiency=float(raw.get(CONF_ZONE_EFFICIENCY, DEFAULT_EFFICIENCY)),
                max_deficit=float(raw.get(CONF_ZONE_MAX_DEFICIT, DEFAULT_MAX_DEFICIT)),
                deficit=existing.get(name, 0.0),
            )
            self.zones[name] = zone

    # --- Lifecycle ------------------------------------------------------

    async def async_setup(self) -> None:
        """Persistenz laden, Listener registrieren, ersten Refresh ausloesen."""
        await self._async_load()
        if self._current_day is None:
            self._current_day = dt_util.now().date().isoformat()

        watched = [eid for eid in self._src.values() if eid]
        if watched:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, watched, self._handle_source_update
                )
            )
        # Quellen initial einlesen (Werte, die schon im State sind)
        for eid in watched:
            state = self.hass.states.get(eid)
            if state is not None:
                self._ingest(eid, state.state)

        self._unsubs.append(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=5
            )
        )
        await self.async_config_entry_first_refresh()

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._async_save()
        await super().async_shutdown()

    # --- Quell-Updates --------------------------------------------------

    @callback
    def _handle_source_update(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._ingest(new_state.entity_id, new_state.state)

    @callback
    def _ingest(self, entity_id: str, raw_state) -> None:
        """Einen Rohwert in die passenden Akkumulatoren einsortieren."""
        if raw_state is None or str(raw_state).lower() in _INVALID_STATES:
            return
        try:
            value = float(raw_state)
        except (ValueError, TypeError):
            return

        if entity_id == self._src[CONF_TEMP]:
            self._add("temp", value)
        elif entity_id == self._src[CONF_HUMIDITY]:
            self._add("rh", value)
        elif entity_id == self._src[CONF_WIND]:
            if self._wind_unit == "km/h":
                value /= 3.6
            self._add("wind", value)
        elif entity_id == self._src[CONF_SOLAR]:
            self._add("solar", value)
        elif entity_id == self._src[CONF_PRESSURE]:
            if self._pressure_unit == "hPa":
                value /= 10.0  # hPa -> kPa
            self._add("pressure", value)
        elif entity_id == self._src[CONF_RAIN]:
            self._ingest_rain(value)

    def _add(self, key: str, value: float) -> None:
        self._day[key].add(value)
        self._iv[key].add(value)

    def _ingest_rain(self, value: float) -> None:
        """Regen in mm/Intervall verwandeln, je nach Sensor-Modus."""
        if self._rain_mode == "rate":
            # mm/h -> wird beim Tick mit der Intervalldauer multipliziert;
            # hier nur den Mittelwert ueber das Intervall sammeln.
            self._rain_rate_iv.add(value)
            return
        if self._rain_mode == "incremental":
            # Sensor liefert pro Meldung den Zuwachs dieses Zeitraums direkt.
            inc = max(value, 0.0)
            self._rain_iv += inc
            self._rain_day += inc
            return
        # "cumulative_daily": monoton steigender Zaehler (Reset z. B. Mitternacht).
        if self._rain_last is None:
            self._rain_last = value
            return
        delta = value - self._rain_last
        if delta < 0:  # Zaehler-Reset erkannt -> neuer Stand ist der Zuwachs
            delta = max(value, 0.0)
        self._rain_last = value
        self._rain_iv += delta
        self._rain_day += delta

    # --- Periodischer Tick (DataUpdateCoordinator) ----------------------

    async def _async_update_data(self) -> IriyData:
        now = dt_util.utcnow()
        if self._rain_mode == "rate" and self._rain_rate_iv.mean is not None:
            # Auch das erste Fenster nach (Neu-)Start zaehlen: dann fehlt
            # _last_tick, also das konfigurierte Intervall als Dauer annehmen.
            if self._last_tick is not None:
                hours = (now - self._last_tick).total_seconds() / 3600.0
            else:
                hours = self.update_interval.total_seconds() / 3600.0
            add = max(self._rain_rate_iv.mean, 0.0) * hours
            self._rain_iv += add
            self._rain_day += add

        if self._hourly and self._last_tick is not None:
            self._integrate_interval(self._last_tick, now)
        elif self._hourly:
            # Erstes Fenster ohne ET-Integration: gemessenen Regen trotzdem
            # sofort auf die Buckets anwenden (sonst geht er verloren).
            self._apply_to_zones(0.0, self._rain_iv)

        self._last_tick = now
        self._reset_interval()
        await self._async_save()
        return self._snapshot()

    def _integrate_interval(self, start: datetime, end: datetime) -> None:
        """ET0 ueber [start, end] rechnen, aufsummieren, Buckets fuettern.

        Wichtig: der im Intervall gemessene Regen wird IMMER auf die Buckets
        angewandt – auch wenn ET wegen kurzzeitig fehlender Sensordaten nicht
        berechnet werden kann. Sonst wuerde Regen verschluckt und Iriy
        empfaehle Bewaesserung trotz gefallenen Regens.
        """
        period_h = (end - start).total_seconds() / 3600.0
        if period_h <= 0:
            return

        et0_iv = 0.0
        t = self._iv["temp"].mean
        rh = self._iv["rh"].mean
        wind = self._iv["wind"].mean
        solar = self._iv["solar"].mean
        if None not in (t, rh, wind, solar):
            mid = dt_util.utc_from_timestamp(
                (start.timestamp() + end.timestamp()) / 2.0
            )
            utc_hour_mid = mid.hour + mid.minute / 60.0 + mid.second / 3600.0
            doy = mid.timetuple().tm_yday
            et0_iv = max(
                et.et0_hourly(
                    t_air=t,
                    rh=rh,
                    wind_ms=wind,
                    solar_w_m2=solar,
                    pressure_kpa=self._pressure_kpa(),
                    latitude_deg=self._lat,
                    longitude_east_deg=self._lon,
                    elevation_m=self._elev,
                    day_of_year=doy,
                    utc_hour_mid=utc_hour_mid,
                    period_hours=period_h,
                    wind_sensor_height_m=self._wind_h,
                ),
                0.0,  # Taubildung (negativ) zaehlt nicht als Verlust
            )
            self.et0_today += et0_iv
            self.et0_rate = round(et0_iv / period_h, 3) if period_h else None

        # Regen IMMER verrechnen (ET-Term ist 0, falls Daten fehlten).
        self._apply_to_zones(et0_iv, self._rain_iv)

    def _apply_to_zones(self, et0_mm: float, rain_mm: float) -> None:
        for zone in self.zones.values():
            etc = et0_mm * zone.kc
            zone.last_etc = etc
            zone.etc_today += etc
            zone.deficit += etc - rain_mm
            zone.deficit = min(max(zone.deficit, 0.0), zone.max_deficit)

    # --- Mitternacht ----------------------------------------------------

    @callback
    def _handle_midnight(self, now: datetime) -> None:
        """Tag abschliessen: kanonischen ET0-Tageswert bilden, Tag zuruecksetzen."""
        et0 = self._compute_daily()
        if et0 is not None:
            self.et0_daily = round(et0, 2)
        if not self._hourly and et0 is not None:
            # Ohne Sub-Tagesspur die Buckets einmal taeglich fuettern.
            self._apply_to_zones(et0, self._rain_day)

        for acc in self._day.values():
            acc.reset()
        self.et0_today = 0.0
        self.et0_rate = None
        self._rain_day = 0.0
        for zone in self.zones.values():
            zone.etc_today = 0.0
        self._current_day = now.date().isoformat()
        self.hass.async_create_task(self._async_save())
        self.async_set_updated_data(self._snapshot())
        _LOGGER.debug("Iriy: Tag abgeschlossen, ET0=%s mm", self.et0_daily)

    def _compute_daily(self) -> float | None:
        t = self._day["temp"]
        rh = self._day["rh"].mean
        wind = self._day["wind"].mean
        solar = self._day["solar"].mean
        if None in (t.minimum, t.maximum, rh, wind, solar):
            return None
        try:
            return et.et0_daily(
                t_min=t.minimum,
                t_max=t.maximum,
                rh_mean=rh,
                wind_ms=wind,
                solar_w_m2=solar,
                pressure_kpa=self._pressure_kpa(),
                latitude_deg=self._lat,
                elevation_m=self._elev,
                day_of_year=dt_util.now().timetuple().tm_yday,
                wind_sensor_height_m=self._wind_h,
            )
        except (ValueError, ZeroDivisionError) as err:
            _LOGGER.warning("Iriy: Tagesberechnung fehlgeschlagen: %s", err)
            return None

    def _provisional_daily(self) -> float | None:
        return self._compute_daily()

    # --- Hilfen ---------------------------------------------------------

    def _pressure_kpa(self) -> float:
        acc = self._day["pressure"]
        if acc.last is not None:
            return acc.last
        return et.atmospheric_pressure(self._elev)

    def _reset_interval(self) -> None:
        for acc in self._iv.values():
            acc.reset()
        self._rain_rate_iv.reset()
        self._rain_iv = 0.0

    def _snapshot(self) -> IriyData:
        prov = self._provisional_daily()
        return IriyData(
            et0_daily=self.et0_daily,
            et0_daily_provisional=round(prov, 2) if prov is not None else None,
            et0_today=round(self.et0_today, 2),
            et0_rate=self.et0_rate,
            zones={n: z for n, z in self.zones.items()},
            diagnostics={
                "t_min": self._day["temp"].minimum,
                "t_max": self._day["temp"].maximum,
                "rh_mean": _round(self._day["rh"].mean, 1),
                "wind_mean_ms": _round(self._day["wind"].mean, 2),
                "solar_mean_wm2": _round(self._day["solar"].mean, 1),
                "pressure_kpa": _round(self._pressure_kpa(), 2),
                "rain_today_mm": round(self._rain_day, 2),
                "hourly": self._hourly,
            },
        )

    # --- Services -------------------------------------------------------

    @callback
    def reset_bucket(self, zone_name: str | None = None) -> None:
        for name, zone in self.zones.items():
            if zone_name in (None, name):
                zone.deficit = 0.0
        self.async_set_updated_data(self._snapshot())
        self.hass.async_create_task(self._async_save())

    @callback
    def add_water(self, zone_name: str, mm: float) -> None:
        """Manuell/automatisch ausgebrachtes Wasser vom Defizit abziehen."""
        zone = self.zones.get(zone_name)
        if zone is None:
            return
        zone.deficit = max(zone.deficit - max(mm, 0.0), 0.0)
        self.async_set_updated_data(self._snapshot())
        self.hass.async_create_task(self._async_save())

    # --- Persistenz -----------------------------------------------------

    async def _async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return

        # Langlebiger Zustand wird IMMER wiederhergestellt (ueberlebt Tageswechsel):
        self.et0_daily = data.get("et0_daily")
        self._rain_last = data.get("rain_last")
        for name, zd in (data.get("zones") or {}).items():
            zone = self.zones.get(name)
            if zone is None:
                continue
            if isinstance(zd, dict):
                zone.deficit = float(zd.get("deficit", 0.0))
                zone.etc_today = float(zd.get("etc_today", 0.0))
            else:  # altes Format: nur Defizit als Zahl
                zone.deficit = float(zd)

        stored_day = data.get("day")
        today = dt_util.now().date().isoformat()
        if stored_day == today:
            # Gleicher Tag: Tages-Akkumulatoren weiterfuehren.
            self.et0_today = data.get("et0_today", 0.0)
            self.et0_rate = data.get("et0_rate")
            self._rain_day = data.get("rain_day", 0.0)
            for key, acc in self._day.items():
                acc.load((data.get("day_acc") or {}).get(key))
            self._current_day = stored_day
        else:
            # HA war ueber Mitternacht aus: Tages-Akkumulatoren NICHT
            # weiterzaehlen (sonst leckt der Vortag in den neuen Tag). Der
            # langlebige et0_daily/Defizit bleibt als letzter bekannter Stand.
            self.et0_today = 0.0
            self.et0_rate = None
            self._rain_day = 0.0
            for acc in self._day.values():
                acc.reset()
            for zone in self.zones.values():
                zone.etc_today = 0.0
            self._current_day = today

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "day": self._current_day,
                "et0_daily": self.et0_daily,
                "et0_today": self.et0_today,
                "et0_rate": self.et0_rate,
                "rain_last": self._rain_last,
                "rain_day": self._rain_day,
                "day_acc": {k: a.as_dict() for k, a in self._day.items()},
                "zones": {
                    n: {"deficit": z.deficit, "etc_today": z.etc_today}
                    for n, z in self.zones.items()
                },
            }
        )


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None
