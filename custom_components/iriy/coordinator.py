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

import json
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
    CONF_ZONE_AREA,
    CONF_ZONE_BY_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_KC,
    CONF_ZONE_MAX_DEFICIT,
    CONF_ZONE_NAME,
    CONF_ZONE_THROUGHPUT,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_Z2M_BASE_TOPIC,
    VALVE_VOLUME_SUFFIX,
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
# Sicherheits-Obergrenzen fuer ein einzelnes Giesz-Kommando (physisches Ventil):
_VALVE_MAX_LITERS = 2000.0
_VALVE_MAX_MINUTES = 600.0


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
    area: float = 0.0             # Flaeche [m2] – fuer die Liter-Steuergroesse
    by_area: bool = False         # True: nur Liter ueber Flaeche, KEINE Laufzeit
    valve: str | None = None      # switch-Entity des Ventils (z2m), optional
    deficit: float = 0.0          # aktuelles Wasserdefizit [mm]
    etc_today: float = 0.0        # Pflanzenbedarf heute [mm]
    last_etc: float = 0.0         # Bedarf im letzten Intervall [mm]
    gegossen_l: float = 0.0       # heute ausgebracht [L] (vom Ventil gemessen)

    @property
    def runtime_minutes(self) -> float | None:
        """ZEIT-Steuergroesse: Laufzeit [min] aus Defizit + Durchfluss (mm/h).

        None bei Flaechensteuerung (by_area) – dann ist der Durchfluss
        undefiniert/variabel und es wird nur ueber die Liter gesteuert.
        """
        if self.by_area:
            return None
        return round(
            et.irrigation_minutes(self.deficit, self.throughput, self.efficiency), 0
        )

    @property
    def liters_needed(self) -> float | None:
        """LITER-Steuergroesse: auszubringende Menge [L].

        Liter = Defizit[mm] x Flaeche[m2] / Wirkungsgrad. 1 mm ueber 1 m2 = 1 L;
        durch den Wirkungsgrad fuer die Brutto-Menge (analog zur Laufzeit). Nur
        sinnvoll, wenn eine Flaeche gesetzt ist – sonst None.
        """
        if self.area <= 0:
            return None
        eff = self.efficiency if self.efficiency > 0 else 1.0
        return round(self.deficit * self.area / eff, 1)


@dataclass
class IriyData:
    """Snapshot, den die Sensor-Entities lesen."""

    et0_daily: float | None = None          # letzter abgeschlossener Tag [mm]
    et0_daily_provisional: float | None = None  # Tag bisher [mm]
    et0_today: float | None = None          # sub-taeglich aufsummiert [mm]
    et0_rate: float | None = None           # letztes Intervall [mm/h]
    zones: dict[str, ZoneState] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    et0_recent: dict[str, float] = field(default_factory=dict)  # ISO-Datum -> mm


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
        # Rollender Verlauf der letzten Tage (ISO-Datum -> mm) fuer die
        # Dashboard-Tabelle; gespeist aus der importierten Tagesstatistik,
        # persistiert im Store (ueberlebt Neustarts).
        self.et0_recent: dict[str, float] = {}
        # Parallele Tages-Logs fuer die Tabelle (ISO-Datum -> Wert), persistiert.
        self.rain_recent: dict[str, float] = {}           # Regen [mm] je Tag
        self.zone_recent: dict[str, dict[str, dict]] = {}  # zone -> {datum: {...}}
        # Letzter gemessener Ventil-Volumenstand je Zone [L] (fuer Delta -> Defizit).
        self._valve_base: dict[str, float] = {}
        self.et0_today = 0.0
        self.et0_rate: float | None = None
        self._last_tick: datetime | None = None
        # Letzter bereits finalisierter Tag (ISO-Datum) – Marker fuer die
        # selbstheilende Tagesschreibung (verhindert Doppelschreiben).
        self._last_finalized_day: str | None = None
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
                area=float(raw.get(CONF_ZONE_AREA, 0.0) or 0.0),
                by_area=bool(raw.get(CONF_ZONE_BY_AREA, False)),
                valve=raw.get(CONF_ZONE_VALVE) or None,
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

        # Punkt 00:00: Tag zuruecksetzen + gestrigen Tageswert sofort finalisieren
        # (siehe _handle_midnight). Zusaetzlich heilt jeder Koordinator-Tick einen
        # ausgefallenen 00:00-Lauf selbst (async_finalize_yesterday(force=False)).
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=5
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
        """EINMALIGE Historie-Saat: vergangene Tageswerte vorbefuellen.

        ET0 je Tag = Summe von et0_hourly ueber die STUENDLICHEN Recorder-
        Statistiken (zeit-gewichtet + lueckenrobust). "Eigener Tag": Tag D = ET0
        von D, datiert auf D selbst. Nur ABGESCHLOSSENE Tage (bis gestern) –
        heute ist unvollstaendig und faellt automatisch raus.
        """
        today0 = dt_util.start_of_local_day()
        # DST-sicher auf lokale Mitternacht normalisiert; Ende=heute 0:00 (exklusiv)
        # -> deckt abgeschlossene Tage bis gestern ab.
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
        """{date: et0_mm} (eigener Tag) in den Panel-Verlauf et0_recent spiegeln.

        Schreibt NICHT mehr in die Entitaet – deren Langzeitstatistik fuehrt HA
        seit `state_class` selbst (aus dem Zustand). et0_recent ist die nach
        eigenem Tag datierte Reihe fuers Iriy-Panel (per Jinja nicht aus der
        Statistik lesbar, daher diese interne, persistierte Kopie).
        """
        if not by_day:
            return 0
        for day, value in by_day.items():
            self.et0_recent[day.isoformat()] = value
        for old in sorted(self.et0_recent)[:-14]:  # nur die letzten 14 Tage halten
            del self.et0_recent[old]
        return len(by_day)

    async def async_sync_recent_from_stats(self, days: int = 14) -> None:
        """Panel-Verlauf et0_recent (eigener Tag) aus der WETTER-Statistik neu
        berechnen – robust und unabhaengig von der Entitaets-Statistik (die HA
        seit state_class selbst fuehrt). Beim Setup aufgerufen, damit die
        Tabelle sofort gefuellt ist.
        """
        # Nur befuellen, wenn der User die Historie wollte ("7 Tage / keine").
        if self._import_history and self._history_days > 0:
            n = max(1, int(self._history_days))
            today0 = dt_util.start_of_local_day()
            start = dt_util.start_of_local_day(today0 - timedelta(days=n))
            by_day = await self._et0_days_from_stats(start, today0)
            if by_day:
                await self._import_et0_points(by_day)
        self.async_set_updated_data(self._snapshot())
        await self._async_save()

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
        # Selbstheilung: gestrigen Tageswert nachholen, falls der 00:00-Lauf
        # ausfiel (idempotent – tut nur etwas, wenn noch nicht finalisiert).
        await self.async_finalize_yesterday(force=False, refresh=False)
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
        # Rate als Momentanwert aus den aktuellen Sensorstaenden – sofort da,
        # neustart-robust, bei jedem Tick frisch (nicht erst nach 2 Intervallen).
        if self._hourly:
            self.et0_rate = self._instant_rate()
        # Gemessenes Ventil-Volumen verbuchen (Defizit selbstkorrigierend).
        self._read_valve_volumes()
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

        # Regen IMMER verrechnen (ET-Term ist 0, falls Daten fehlten).
        self._apply_to_zones(et0_iv, self._rain_iv)

    def _instant_rate(self) -> float | None:
        """Momentane ET-Rate [mm/h] aus den AKTUELLEN Sensorstaenden.

        Unabhaengig von der Intervall-Akkumulation -> sofort nach (Neu-)Start
        verfuegbar und bei jedem Tick frisch. Liefert None, wenn ein
        Pflichtsensor gerade keinen gueltigen Wert liefert.
        """

        def cur(conf_key: str) -> float | None:
            eid = self._src.get(conf_key)
            if not eid:
                return None
            st = self.hass.states.get(eid)
            if st is None or str(st.state).lower() in _INVALID_STATES:
                return None
            try:
                return float(st.state)
            except (ValueError, TypeError):
                return None

        t = cur(CONF_TEMP)
        rh = cur(CONF_HUMIDITY)
        wind = cur(CONF_WIND)
        solar = cur(CONF_SOLAR)
        if None in (t, rh, wind, solar):
            return None
        if self._wind_unit == "km/h":
            wind /= 3.6
        pressure = cur(CONF_PRESSURE)
        if pressure is None:
            pressure = self._pressure_kpa()
        elif self._pressure_unit == "hPa":
            pressure /= 10.0
        now = dt_util.utcnow()
        try:
            rate = et.et0_hourly(
                t_air=t,
                rh=rh,
                wind_ms=wind,
                solar_w_m2=solar,
                pressure_kpa=pressure,
                latitude_deg=self._lat,
                longitude_east_deg=self._lon,
                elevation_m=self._elev,
                day_of_year=now.timetuple().tm_yday,
                utc_hour_mid=now.hour + now.minute / 60.0,
                period_hours=1.0,
                wind_sensor_height_m=self._wind_h,
            )
        except (ValueError, ZeroDivisionError):
            return None
        return round(max(rate, 0.0), 3)

    # --- Ventile (z2m GiEX/Tuya) ---------------------------------------

    def _valve_volume_entity(self, valve: str) -> str | None:
        """Den taeglichen Volumen-Mess-Sensor am selben Geraet wie der
        Ventil-Switch finden (…_daily_irrigation_volume)."""
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(self.hass)
        ent = reg.async_get(valve)
        if ent is None or ent.device_id is None:
            return None
        for e in reg.entities.values():
            if e.device_id == ent.device_id and e.entity_id.endswith(
                VALVE_VOLUME_SUFFIX
            ):
                return e.entity_id
        return None

    def _valve_set_topic(self, valve: str) -> str | None:
        """z2m-Set-Topic aus dem Geraetenamen ableiten: <base>/<name>/set."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent = er.async_get(self.hass).async_get(valve)
        if ent is None or ent.device_id is None:
            return None
        dev = dr.async_get(self.hass).async_get(ent.device_id)
        if dev is None:
            return None
        name = dev.name_by_user or dev.name
        if not name:
            return None
        return f"{DEFAULT_Z2M_BASE_TOPIC}/{name}/set"

    def _read_valve_volumes(self) -> None:
        """Gemessenes Tagesvolumen je Zone lesen und das Defizit
        selbstkorrigierend verbuchen (Delta seit letztem Stand / Flaeche).

        Geht vom GEMESSENEN Wert aus -> faengt auch manuelles Gieszen. Nur fuer
        Zonen mit Flaeche (sonst keine L->mm-Umrechnung moeglich).
        """
        for zone in self.zones.values():
            if not zone.valve:
                continue
            vol_eid = self._valve_volume_entity(zone.valve)
            if not vol_eid:
                continue
            st = self.hass.states.get(vol_eid)
            if st is None or str(st.state).lower() in _INVALID_STATES:
                continue
            try:
                cur = float(st.state)
            except (ValueError, TypeError):
                continue
            base = self._valve_base.get(zone.name)
            self._valve_base[zone.name] = cur
            # Anzeige "heute gegossen" = Tages-Maximum des Geraete-Zaehlers
            # (monoton steigend bis zum taeglichen Reset) -> zeigt das echte
            # Tagesvolumen und ist robust gegen Reset-/Snapshot-Timing.
            zone.gegossen_l = max(zone.gegossen_l, cur)
            if base is None:
                continue
            # Sensor-Tagesreset abfangen (faellt cur unter base -> neuer Tag).
            delta = cur - base if cur >= base else cur
            if delta <= 0:
                continue
            # Defizit selbstkorrigierend: ausgebrachte BRUTTO-Liter -> NETTO-mm
            # an der Pflanze (x Wirkungsgrad), dann abziehen. Konsistent zu
            # liters_needed = Defizit*Flaeche/Wirkungsgrad. Nur mit Flaeche
            # moeglich (L->mm); Zeit-Zonen ohne Flaeche korrigieren NICHT aus der
            # Messung (dort schaetzt async_irrigate_zone beim Kommandieren).
            if zone.area > 0:
                eff = zone.efficiency if zone.efficiency > 0 else 1.0
                zone.deficit = max(zone.deficit - (delta / zone.area) * eff, 0.0)

    async def async_irrigate_zone(
        self, zone_name: str, amount: float | None = None
    ) -> None:
        """Gieszvorgang am Ventil der Zone ausloesen (benutzer-initiiert).

        Liter-Zone (Flaeche) -> quantitative (irrigation_capacity = Liter).
        Zeit-Zone -> timed (irrigation_duration = Sekunden). Das Ventil giesst
        autonom und schliesst selbst. amount ueberschreibt die Menge (L) bzw.
        Zeit (min) je nach Zonen-Typ.
        """
        zone = self.zones.get(zone_name)
        if zone is None or not zone.valve:
            _LOGGER.warning("Iriy: Zone %r hat kein Ventil", zone_name)
            return
        topic = self._valve_set_topic(zone.valve)
        if not topic:
            _LOGGER.warning(
                "Iriy: Set-Topic fuer Ventil %s nicht ermittelbar", zone.valve
            )
            return

        if zone.area > 0:
            liters = amount if amount is not None else (zone.liters_needed or 0.0)
            if liters <= 0:
                _LOGGER.info("Iriy: Zone %s hat kein Liter-Defizit", zone_name)
                return
            if liters > _VALVE_MAX_LITERS:  # Sicherheits-Deckel (physisches Ventil)
                _LOGGER.warning(
                    "Iriy: Zone %s – Liter %.0f ueber Limit %.0f, gekappt. "
                    "Flaeche/Konfig pruefen.",
                    zone_name,
                    liters,
                    _VALVE_MAX_LITERS,
                )
                liters = _VALVE_MAX_LITERS
            payload = {
                "cyclic_quantitative_irrigation": {
                    "current_count": 0,
                    "total_number": 1,
                    "irrigation_capacity": int(round(liters)),
                    "irrigation_interval": 0,
                }
            }
            # Defizit kommt selbstkorrigierend aus dem gemessenen Volumen.
        else:
            minutes = amount if amount is not None else (zone.runtime_minutes or 0.0)
            if not minutes or minutes <= 0:
                _LOGGER.info("Iriy: Zone %s hat kein Zeit-Defizit", zone_name)
                return
            if minutes > _VALVE_MAX_MINUTES:  # Sicherheits-Deckel
                _LOGGER.warning(
                    "Iriy: Zone %s – Laufzeit %.0f min ueber Limit %.0f, gekappt.",
                    zone_name,
                    minutes,
                    _VALVE_MAX_MINUTES,
                )
                minutes = _VALVE_MAX_MINUTES
            payload = {
                "cyclic_timed_irrigation": {
                    "current_count": 0,
                    "total_number": 1,
                    "irrigation_duration": int(round(minutes * 60)),
                    "irrigation_interval": 0,
                }
            }
            # Zeit-Zone hat keine Flaeche -> Defizit nicht messbar herleitbar;
            # um den kommandierten Betrag schaetzen.
            eff = zone.efficiency if zone.efficiency > 0 else 1.0
            zone.deficit = max(
                zone.deficit - zone.throughput * (minutes / 60.0) * eff, 0.0
            )

        await self.hass.services.async_call(
            "mqtt",
            "publish",
            {"topic": topic, "payload": json.dumps(payload)},
            blocking=True,
        )
        _LOGGER.info("Iriy: Gieszen Zone %s -> %s %s", zone_name, topic, payload)
        await self._async_save()
        self.async_set_updated_data(self._snapshot())

    def _apply_to_zones(self, et0_mm: float, rain_mm: float) -> None:
        for zone in self.zones.values():
            etc = et0_mm * zone.kc
            zone.last_etc = etc
            zone.etc_today += etc
            zone.deficit += etc - rain_mm
            zone.deficit = min(max(zone.deficit, 0.0), zone.max_deficit)

    # --- Mitternacht ----------------------------------------------------

    def _trim_recent(self, keep: int = 14) -> None:
        """Tages-Logs (Regen, Zonen) auf die letzten `keep` Tage begrenzen."""
        for d in sorted(self.rain_recent)[:-keep]:
            del self.rain_recent[d]
        for zlog in self.zone_recent.values():
            for d in sorted(zlog)[:-keep]:
                del zlog[d]

    @callback
    def _handle_midnight(self, now: datetime) -> None:
        """Punkt 00:00: ET0 des gerade beendeten Tages SOFORT finalisieren und
        als heutigen "gestern"-Wert (datiert auf HEUTE 0:00) schreiben; dann den
        Tag zuruecksetzen. Lueckenrobust aus der Recorder-Statistik.
        """
        # Gestrigen Tageswert sofort um 00:00 schreiben (force).
        self.hass.async_create_task(
            self.async_finalize_yesterday(force=True, refresh=True)
        )
        # Ohne Sub-Tagesspur das Defizit einmal taeglich fuettern.
        if not self._hourly:
            et0 = self._compute_daily()
            if et0 is not None:
                self._apply_to_zones(et0, self._rain_day)

        # Tabellen-Tageslog des gerade beendeten Tages festhalten (Regen + je
        # Zone Defizit & gegossene Liter), dann auf 14 Tage begrenzen.
        ended = (now - timedelta(days=1)).date().isoformat()
        self.rain_recent[ended] = round(self._rain_day, 2)
        for zone in self.zones.values():
            self.zone_recent.setdefault(zone.name, {})[ended] = {
                "deficit": round(zone.deficit, 2),
                "gegossen_l": round(zone.gegossen_l, 1),
            }
        self._trim_recent()

        for acc in self._day.values():
            acc.reset()
        self.et0_today = 0.0
        self.et0_rate = self._instant_rate() if self._hourly else None
        self._rain_day = 0.0
        for zone in self.zones.values():
            zone.etc_today = 0.0
            zone.gegossen_l = 0.0  # neuer Tag; _read_valve_volumes fuellt neu
        self._current_day = now.date().isoformat()
        self.hass.async_create_task(self._async_save())
        self.async_set_updated_data(self._snapshot())
        _LOGGER.debug("Iriy: Tageswechsel -> %s", self._current_day)

    async def async_finalize_yesterday(
        self, force: bool = True, refresh: bool = True
    ) -> None:
        """Den ET0 des gestrigen (gerade beendeten) Tages bilden und als
        Sensor-Zustand setzen (= letzter abgeschlossener Tag). HA zeichnet die
        Langzeitstatistik der Entitaet daraus selbst auf (state_class). Zusaetzlich
        wird der Panel-Verlauf (et0_recent, eigener Tag) nachgefuehrt.

        Selbstheilend: mit force=False nur, wenn der Vortag noch nicht geschrieben
        wurde (Marker _last_finalized_day) – so holt jeder Koordinator-Tick einen
        ausgefallenen 00:00-Lauf nach. Heutige Bilanz bleibt unberuehrt.
        """
        today0 = dt_util.start_of_local_day()
        yesterday0 = today0 - timedelta(days=1)
        yday = yesterday0.date()
        if not force and self._last_finalized_day == yday.isoformat():
            return
        by_day = await self._et0_days_from_stats(yesterday0, today0)
        if yday not in by_day:
            _LOGGER.debug("Iriy: %s noch nicht finalisierbar (keine Statistik)", yday)
            return
        self.et0_daily = by_day[yday]
        # Zustand (et0_daily) -> HA fuehrt die Langzeitstatistik selbst (state_class).
        # Hier nur den Panel-Verlauf (eigener Tag) nachfuehren.
        await self._import_et0_points({yday: by_day[yday]})
        self._last_finalized_day = yday.isoformat()
        await self._async_save()
        if refresh:
            self.async_set_updated_data(self._snapshot())
        _LOGGER.info("Iriy: ET0 %s = %s mm geschrieben", yday, self.et0_daily)

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
            et0_recent=dict(self.et0_recent),
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
        self.et0_recent = dict(data.get("et0_recent") or {})
        self.rain_recent = dict(data.get("rain_recent") or {})
        self.zone_recent = {
            z: dict(v) for z, v in (data.get("zone_recent") or {}).items()
        }
        self._valve_base = dict(data.get("valve_base") or {})
        self._rain_last = data.get("rain_last")
        self._history_imported = bool(data.get("history_imported", False))
        self._last_finalized_day = data.get("last_finalized_day")
        for name, zd in (data.get("zones") or {}).items():
            zone = self.zones.get(name)
            if zone is None:
                continue
            if isinstance(zd, dict):
                zone.deficit = float(zd.get("deficit", 0.0))
                zone.etc_today = float(zd.get("etc_today", 0.0))
                zone.gegossen_l = float(zd.get("gegossen_l", 0.0))
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
                zone.gegossen_l = 0.0  # neuer Tag waehrend HA aus
            self._valve_base = {}  # Volumen-Baseline neu setzen (Sensor reset)
            self._current_day = today

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "day": self._current_day,
                "history_imported": self._history_imported,
                "last_finalized_day": self._last_finalized_day,
                "et0_daily": self.et0_daily,
                "et0_recent": self.et0_recent,
                "rain_recent": self.rain_recent,
                "zone_recent": self.zone_recent,
                "valve_base": self._valve_base,
                "et0_today": self.et0_today,
                "et0_rate": self.et0_rate,
                "rain_last": self._rain_last,
                "rain_day": self._rain_day,
                "day_acc": {k: a.as_dict() for k, a in self._day.items()},
                "zones": {
                    n: {
                        "deficit": z.deficit,
                        "etc_today": z.etc_today,
                        "gegossen_l": z.gegossen_l,
                    }
                    for n, z in self.zones.items()
                },
            }
        )


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None
