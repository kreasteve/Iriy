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
    CONF_HISTORY_DAYS,
    CONF_HOURLY,
    CONF_HUMIDITY,
    CONF_IMPORT_HISTORY,
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
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_EFFICIENCY,
    DEFAULT_ELEVATION,
    DEFAULT_HOURLY,
    DEFAULT_IMPORT_HISTORY,
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
        self._import_history = bool(
            self._opt(CONF_IMPORT_HISTORY, DEFAULT_IMPORT_HISTORY)
        )
        self._history_days = int(self._opt(CONF_HISTORY_DAYS, DEFAULT_BACKFILL_DAYS))
        self._history_imported = False  # aus Store geladen

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
        # True, wenn beim Setup schon persistierter Zustand vorlag -> dann
        # KEIN automatischer Backfill (wuerde doppelt zaehlen).
        self._loaded_existing = False

        # Zonen
        self.zones: dict[str, ZoneState] = {}
        self._build_zones()

    # --- Konfig-Helfer --------------------------------------------------

    def _opt(self, key: str, default=None):
        """Options haben Vorrang vor Daten (Options-Flow ueberschreibt Setup)."""
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def loaded_existing(self) -> bool:
        """True, wenn beim Setup schon persistierter Zustand vorlag."""
        return self._loaded_existing

    @property
    def import_history(self) -> bool:
        return self._import_history

    @property
    def history_days(self) -> int:
        return self._history_days

    @property
    def history_imported(self) -> bool:
        return self._history_imported

    async def mark_history_imported(self) -> None:
        self._history_imported = True
        await self._async_save()

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
        # Kurz nach Mitternacht den gestrigen Tageswert aus der Recorder-
        # Stundenstatistik finalisieren (dann ist die letzte Stunde verdichtet).
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._handle_finalize, hour=0, minute=20, second=0
            )
        )
        # Frische Einrichtung: Tagesbilanz aus der Recorder-History aufbauen,
        # damit sofort sinnvolle Werte da sind statt erst ab morgen.
        if not self._loaded_existing:
            await self._async_backfill()
        await self.async_config_entry_first_refresh()

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._async_save()
        await super().async_shutdown()

    # --- Backfill aus Recorder-History ----------------------------------

    async def async_backfill(self) -> None:
        """Service: heutige Spuren verwerfen und aus der History neu aufbauen."""
        for acc in self._day.values():
            acc.reset()
        self.et0_today = 0.0
        self.et0_rate = None
        self._rain_day = 0.0
        for zone in self.zones.values():
            zone.etc_today = 0.0
        await self._async_backfill()
        self.async_set_updated_data(self._snapshot())

    async def _async_backfill(self) -> None:
        """Tagesbilanz aus der Recorder-History rekonstruieren (Warmstart).

        Setzt et0_daily auf den gestrigen Volltag und fuellt die heutigen
        Akkumulatoren + et0_today + Zonen-Defizit aus den schon vorhandenen
        Sensordaten – damit Iriy sofort sinnvolle Werte zeigt.
        """
        if "recorder" not in self.hass.config.components:
            _LOGGER.warning("Iriy: Recorder nicht aktiv – Backfill uebersprungen")
            return
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return

        now = dt_util.utcnow()
        today0 = dt_util.start_of_local_day()
        start = today0 - timedelta(days=1)
        ids = [e for e in self._src.values() if e]
        if not ids:
            return
        try:
            raw = await get_instance(self.hass).async_add_executor_job(
                history.get_significant_states,
                self.hass, start, now, ids, None, True, False, False, True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Iriy: Backfill-History fehlgeschlagen: %s", err)
            return

        # Messreihen je Groesse aufbauen (Einheiten-Umrechnung wie im Live-Pfad)
        series: dict[str, list[tuple[float, float]]] = {}
        keymap = {
            "temp": CONF_TEMP,
            "rh": CONF_HUMIDITY,
            "wind": CONF_WIND,
            "solar": CONF_SOLAR,
            "pressure": CONF_PRESSURE,
        }
        for key, conf_key in keymap.items():
            eid = self._src.get(conf_key)
            pts: list[tuple[float, float]] = []
            for stt in (raw.get(eid, []) if eid else []):
                try:
                    val = float(stt.state)
                except (ValueError, TypeError):
                    continue
                if key == "wind" and self._wind_unit == "km/h":
                    val /= 3.6
                elif key == "pressure" and self._pressure_unit == "hPa":
                    val /= 10.0
                pts.append((stt.last_updated.timestamp(), val))
            pts.sort()
            series[key] = pts

        def mean_win(key: str, t0: float, t1: float) -> float | None:
            pts = series.get(key, [])
            vals = [v for ts, v in pts if t0 <= ts < t1]
            if vals:
                return sum(vals) / len(vals)
            prev = [v for ts, v in pts if ts < t1]  # Carry-forward
            return prev[-1] if prev else None

        def pressure_at(t1: float) -> float:
            prev = [v for ts, v in series.get("pressure", []) if ts <= t1]
            return prev[-1] if prev else et.atmospheric_pressure(self._elev)

        t0 = today0.timestamp()
        tn = now.timestamp()

        # 1) Gestern: kanonischer Tageswert = Summe der Stundenwerte aus der
        #    Recorder-STUNDENstatistik (zeit-gewichtet + lueckenrobust,
        #    FAO-konform) – NICHT die count-gewichtete Tagesmittel-Gleichung.
        by_day = await self._et0_days_from_stats(start, today0)
        yday = (today0 - timedelta(days=1)).date()
        if yday in by_day:
            self.et0_daily = by_day[yday]

        # 2) Heute: Tages-Akkumulatoren fuellen (fuer Provisorisch + Mitternacht)
        for key in ("temp", "rh", "wind", "solar", "pressure"):
            for ts, val in series[key]:
                if ts >= t0:
                    self._day[key].add(val)

        # 3) Heute: stuendliche ET0 aufsummieren
        self.et0_today = 0.0
        h = t0
        while h < tn:
            he = min(h + 3600.0, tn)
            tm = mean_win("temp", h, he)
            rh = mean_win("rh", h, he)
            wind = mean_win("wind", h, he)
            solar = mean_win("solar", h, he)
            if None not in (tm, rh, wind, solar):
                mid = dt_util.utc_from_timestamp((h + he) / 2.0)
                try:
                    et0_h = et.et0_hourly(
                        t_air=tm,
                        rh=rh,
                        wind_ms=wind,
                        solar_w_m2=solar,
                        pressure_kpa=pressure_at(he),
                        latitude_deg=self._lat,
                        longitude_east_deg=self._lon,
                        elevation_m=self._elev,
                        day_of_year=mid.timetuple().tm_yday,
                        utc_hour_mid=mid.hour + mid.minute / 60.0,
                        period_hours=(he - h) / 3600.0,
                        wind_sensor_height_m=self._wind_h,
                    )
                    self.et0_today += max(et0_h, 0.0)
                except (ValueError, ZeroDivisionError):
                    pass
            h = he

        # 4) Regen heute
        rain_eid = self._src.get(CONF_RAIN)
        if rain_eid:
            rpts: list[tuple[float, float]] = []
            for stt in raw.get(rain_eid, []):
                try:
                    rpts.append((stt.last_updated.timestamp(), float(stt.state)))
                except (ValueError, TypeError):
                    continue
            rpts.sort()
            today_r = [v for ts, v in rpts if ts >= t0]
            if self._rain_mode == "incremental":
                self._rain_day = sum(max(v, 0.0) for v in today_r)
            elif self._rain_mode == "rate":
                self._rain_day = 0.0  # Rate ist nicht rueckwirkend integrierbar
            else:  # cumulative_daily: aktueller Tageszaehler
                self._rain_day = today_r[-1] if today_r else 0.0
                if rpts:
                    self._rain_last = rpts[-1][1]

        # 5) Zonen-Defizit aus der heutigen Bilanz
        for zone in self.zones.values():
            zone.etc_today = self.et0_today * zone.kc
            zone.deficit = min(
                max(zone.etc_today - self._rain_day, 0.0), zone.max_deficit
            )

        self._current_day = dt_util.now().date().isoformat()
        await self._async_save()
        _LOGGER.info(
            "Iriy: Backfill – ET0 gestern=%s mm, heute bisher=%.2f mm",
            self.et0_daily,
            self.et0_today,
        )

    async def async_import_history_statistics(self, days: int = 30) -> int:
        """Vergangene Tage als ET0-Langzeitstatistik in den Tagessensor einspeisen.

        Kanonische Methode: ET0 je Tag = Summe von et0_hourly ueber die
        STUENDLICHEN Recorder-Statistiken (zeit-gewichtet + lueckenrobust,
        FAO-56/ASCE-konform). Nur ABGESCHLOSSENE Tage (vor heute), idempotent
        (Upsert in die Historie von sensor.iriy_et0_*).
        """
        today0 = dt_util.start_of_local_day()
        # start_of_local_day normalisiert auf lokale Mitternacht (auch ueber
        # DST-Grenzen) -> der aelteste Tag ist vollstaendig, kein Teiltag.
        start = dt_util.start_of_local_day(today0 - timedelta(days=max(1, int(days))))
        by_day = await self._et0_days_from_stats(start, today0)
        return await self._import_et0_points(by_day)

    async def _et0_days_from_stats(
        self, start_local: datetime, end_local: datetime
    ) -> dict:
        """ET0 je abgeschlossenem Tag aus der STUENDLICHEN Recorder-Statistik.

        Summiert et0_hourly ueber die stuendlichen Mittel des Recorders. Quelle
        ist der Recorder (kein Live-Akkumulator) -> unabhaengig davon, ob Iriy
        durchlief, und OHNE den count-Gewichtungs-Bias der Tagesmittel (der die
        Tagesgleichung kuenstlich aufblaeht). Liefert {date: et0_mm}.
        """
        if "recorder" not in self.hass.config.components:
            return {}
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except ImportError:
            return {}

        roles = {
            self._src.get(CONF_TEMP): "temp",
            self._src.get(CONF_HUMIDITY): "rh",
            self._src.get(CONF_WIND): "wind",
            self._src.get(CONF_SOLAR): "solar",
            self._src.get(CONF_PRESSURE): "pressure",
        }
        ids = {sid for sid in roles if sid}
        present = {roles[s] for s in ids}
        if not {"temp", "rh", "wind", "solar"} <= present:
            _LOGGER.debug(
                "Iriy: ET0-Statistik uebersprungen – Pflichtsensoren fehlen (%s)",
                present,
            )
            return {}
        # Stundenstatistik bevorzugt; manche Installationen liefern dafuer aber
        # NICHTS (nur 5-Minuten-Kurzzeitstatistik vorhanden, keine stuendliche
        # Langzeitstatistik). Dann auf 5-Minuten zurueckfallen und selbst auf
        # Stundenmittel verdichten. So funktioniert es auf beiden Welten.
        rows: dict = {}
        for period in ("hour", "5minute"):
            try:
                rows = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start_local,
                    end_local,
                    ids,
                    period,
                    None,
                    {"mean"},
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Iriy: Statistik-Lesen (%s) fehlgeschlagen: %s", period, err
                )
                rows = {}
            if any(rows.get(sid) for sid in ids):
                _LOGGER.debug("Iriy: ET0 aus Statistik-Periode '%s'", period)
                break

        # Sample-Mittel je Groesse in UTC-Stundenbins verdichten
        # (granularitaets-agnostisch: 'hour' -> 1 Wert/Stunde, '5minute' ->
        # Mittel der ~12 Werte je Stunde), inkl. Einheiten-Umrechnung.
        bins: dict[str, dict[int, list]] = {}
        for sid, vals in rows.items():
            role = roles.get(sid)
            if not role:
                continue
            for row in vals:
                ts = row.get("start")
                mean = row.get("mean")
                if ts is None or mean is None:
                    continue
                if ts > 1e12:  # ms -> s
                    ts /= 1000.0
                val = float(mean)
                if role == "wind" and self._wind_unit == "km/h":
                    val /= 3.6
                elif role == "pressure" and self._pressure_unit == "hPa":
                    val /= 10.0
                hts = int(ts // 3600) * 3600  # UTC-Stundenbeginn
                acc = bins.setdefault(role, {}).setdefault(hts, [0.0, 0])
                acc[0] += val
                acc[1] += 1
        series: dict[str, list[tuple[float, float]]] = {
            role: sorted((h, total / cnt) for h, (total, cnt) in hrs.items())
            for role, hrs in bins.items()
        }

        # Ein einzelnes fehlendes Stunden-Sample wird per Carry-forward
        # ueberbrueckt, aber NICHT laenger – sonst wuerden bei echten Recorder-
        # Luecken Werte fabriziert; dann lieber die Stunde auslassen.
        max_carry = 2 * 3600.0

        def at(role: str, ts: float) -> float | None:
            pts = series.get(role, [])
            prev = [(t, v) for t, v in pts if t <= ts]
            if not prev:
                return None
            t_prev, val = prev[-1]
            if ts - t_prev > max_carry:
                return None
            return val

        hours = sorted({t for pts in series.values() for t, _ in pts})
        by_day: dict = {}
        for ts in hours:
            tm = at("temp", ts)
            rh = at("rh", ts)
            wind = at("wind", ts)
            solar = at("solar", ts)
            if None in (tm, rh, wind, solar):
                continue
            pressure = at("pressure", ts)
            if pressure is None:
                pressure = et.atmospheric_pressure(self._elev)
            mid = dt_util.utc_from_timestamp(ts + 1800.0)  # Stundenmitte
            # Tag konsistent ueber denselben Zeitpunkt (Stundenmitte) zuordnen,
            # damit Physik und Tagesschluessel auch bei fraktionalen Zeitzonen
            # und ueber DST-Grenzen uebereinstimmen.
            day = dt_util.as_local(mid).date()
            try:
                e = et.et0_hourly(
                    t_air=tm,
                    rh=rh,
                    wind_ms=wind,
                    solar_w_m2=solar,
                    pressure_kpa=pressure,
                    latitude_deg=self._lat,
                    longitude_east_deg=self._lon,
                    elevation_m=self._elev,
                    day_of_year=mid.timetuple().tm_yday,
                    utc_hour_mid=mid.hour + mid.minute / 60.0,
                    period_hours=1.0,
                    wind_sensor_height_m=self._wind_h,
                )
            except (ValueError, ZeroDivisionError):
                continue
            by_day[day] = by_day.get(day, 0.0) + max(e, 0.0)
        return {d: round(v, 2) for d, v in by_day.items()}

    async def _import_et0_points(self, by_day: dict) -> int:
        """{date: et0_mm} als korrekt datierte Tagesstatistik IN DIE ENTITAET
        sensor.iriy_et0_* schreiben (idempotenter Upsert).

        Funktioniert sauber, weil die "gestern"-Entitaet KEIN state_class hat
        (siehe sensor.py): HA zeichnet sie also nicht selbst auf, es gibt keine
        Kollision, und der Wert von Tag D liegt korrekt auf Tag D. So bekommt
        genau die Entitaet, die der Nutzer ansieht, ihre richtige Historie –
        beliebig viele Tage zurueck (Button / Finalizer).
        """
        if not by_day or "recorder" not in self.hass.config.components:
            return 0
        try:
            from homeassistant.components.recorder.statistics import (
                async_import_statistics,
            )
        except ImportError:
            return 0
        try:
            from homeassistant.components.recorder.models import StatisticMeanType

            mean_meta: dict = {"mean_type": StatisticMeanType.ARITHMETIC}
        except ImportError:  # aeltere HA-Versionen
            mean_meta = {"has_mean": True}

        from homeassistant.helpers import entity_registry as er

        stat_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_et0_daily"
        )
        if not stat_id:
            _LOGGER.warning("Iriy: ET0-Tagessensor noch nicht registriert")
            return 0

        points = [
            {
                "start": dt_util.start_of_local_day(day),
                "min": value,
                "max": value,
                "mean": value,
            }
            for day, value in sorted(by_day.items())
        ]
        metadata = {
            **mean_meta,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": stat_id,
            "unit_class": None,
            "unit_of_measurement": "mm",
        }
        async_import_statistics(self.hass, metadata, points)
        _LOGGER.info(
            "Iriy: %d Tage ET0 in die Entitaet geschrieben (%s)", len(points), stat_id
        )
        return len(points)

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
        """Tag abschliessen und Tag zuruecksetzen.

        Der KANONISCHE ET0-Tageswert wird kurz nach Mitternacht aus der
        Recorder-STUNDENstatistik finalisiert (async_finalize_yesterday),
        sobald die letzte Stunde verdichtet ist. Hier setzen wir nur einen
        sofortigen Provisorisch-Wert, damit der Sensor nahtlos weiterlaeuft.
        """
        # Den kanonischen Tageswert setzt kurz darauf async_finalize_yesterday()
        # aus der Statistik – hier KEIN Provisorium (vermeidet den sichtbaren
        # Tagesbruch 00:00 -> 00:20). Ohne Sub-Tagesspur trotzdem das Defizit
        # einmal taeglich fuettern.
        if not self._hourly:
            et0 = self._compute_daily()
            if et0 is not None:
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

    async def async_finalize_yesterday(self) -> None:
        """Gestrigen ET0-Tageswert aus der Recorder-Stundenstatistik bilden.

        Das ist der KANONISCHE Wert: Summe von et0_hourly ueber die
        stuendlichen Mittel des Recorders – zeit-gewichtet, lueckenrobust und
        unabhaengig davon, ob Iriy durchlief. Schreibt den Wert auch in die
        Langzeitstatistik (Upsert), damit Sensor und Verlaufsgraph stimmen.
        Die heutige Defizit-Bilanz wird dabei NICHT angetastet.
        """
        today0 = dt_util.start_of_local_day()
        yesterday0 = today0 - timedelta(days=1)
        by_day = await self._et0_days_from_stats(yesterday0, today0)
        yday = yesterday0.date()
        if yday not in by_day:
            _LOGGER.debug(
                "Iriy: Finalisierung – keine Stundenstatistik fuer %s gefunden", yday
            )
            return
        self.et0_daily = by_day[yday]
        await self._import_et0_points({yday: by_day[yday]})
        await self._async_save()
        self.async_set_updated_data(self._snapshot())
        _LOGGER.info("Iriy: ET0 gestern aus Statistik = %s mm", self.et0_daily)

    async def _handle_finalize(self, now: datetime) -> None:
        await self.async_finalize_yesterday()

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
        # Bei aktiver Sub-Tagesspur ist die laufende Summe selbst der beste
        # provisorische Tageswert (gleiche Methode wie der finale Wert).
        if self._hourly:
            return self.et0_today
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
        self._loaded_existing = True

        # Langlebiger Zustand wird IMMER wiederhergestellt (ueberlebt Tageswechsel):
        self.et0_daily = data.get("et0_daily")
        self._rain_last = data.get("rain_last")
        self._history_imported = bool(data.get("history_imported", False))
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
                "history_imported": self._history_imported,
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
