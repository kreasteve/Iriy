"""Config-Flow fuer Iriy – komplette Einrichtung und Pflege ueber die UI.

Erststart  -> async_step_user  : Standort + Quell-Sensoren + Berechnung.
Zahnrad    -> Options-Flow      : Menue zum Bearbeiten der Einstellungen und
                                  zum Verwalten der Bewaesserungszonen.

Zonen liegen als Liste von Dicts in entry.options[CONF_ZONES]. Der
Koordinator liest Options mit Vorrang vor Daten, daher wirkt jede
UI-Aenderung nach dem automatischen Reload sofort.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    DEFAULT_HOURLY,
    DEFAULT_MAX_DEFICIT,
    DEFAULT_PRESSURE_UNIT,
    DEFAULT_RAIN_MODE,
    DEFAULT_THROUGHPUT,
    DEFAULT_UPDATE_MINUTES,
    DEFAULT_WIND_HEIGHT,
    DEFAULT_WIND_UNIT,
    DOMAIN,
    MIN_UPDATE_MINUTES,
)

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _num(minimum: float, maximum: float, step: float, unit: str | None = None):
    # Optionale Config-Felder NUR setzen, wenn nicht None – HA validiert
    # unit_of_measurement gegen cv.string, None wuerde eine Exception werfen.
    config = selector.NumberSelectorConfig(
        min=minimum,
        max=maximum,
        step=step,
        mode=selector.NumberSelectorMode.BOX,
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _select(options: list[str], translation_key: str | None = None):
    config = selector.SelectSelectorConfig(
        options=options,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
    if translation_key is not None:
        config["translation_key"] = translation_key
    return selector.SelectSelector(config)


def _settings_schema(defaults: dict) -> vol.Schema:
    """Gemeinsames Schema fuer Erststart und Options-Einstellungen.

    Entity-Felder tragen bewusst KEIN voluptuous-default (ein leeres
    optionales Feld muss leer bleiben duerfen). Vorbelegung beim Bearbeiten
    laeuft ueber add_suggested_values_to_schema im jeweiligen Step.
    Zahlen/Einheiten/Boolean haben statische Defaults (nie None).
    """
    return vol.Schema(
        {
            vol.Required(CONF_TEMP): _SENSOR,
            vol.Required(CONF_HUMIDITY): _SENSOR,
            vol.Required(CONF_WIND): _SENSOR,
            vol.Required(
                CONF_WIND_UNIT, default=defaults.get(CONF_WIND_UNIT, DEFAULT_WIND_UNIT)
            ): _select(["km/h", "m/s"]),
            vol.Required(
                CONF_WIND_HEIGHT,
                default=defaults.get(CONF_WIND_HEIGHT, DEFAULT_WIND_HEIGHT),
            ): _num(0.5, 50, 0.5, "m"),
            vol.Required(CONF_SOLAR): _SENSOR,
            vol.Optional(CONF_PRESSURE): _SENSOR,
            vol.Required(
                CONF_PRESSURE_UNIT,
                default=defaults.get(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT),
            ): _select(["hPa", "kPa"]),
            vol.Optional(CONF_RAIN): _SENSOR,
            vol.Required(
                CONF_RAIN_MODE,
                default=defaults.get(CONF_RAIN_MODE, DEFAULT_RAIN_MODE),
            ): _select(["cumulative_daily", "incremental", "rate"], "rain_mode"),
            vol.Required(
                CONF_LATITUDE, default=defaults.get(CONF_LATITUDE)
            ): _num(-90, 90, 0.0001, "°"),
            vol.Required(
                CONF_LONGITUDE, default=defaults.get(CONF_LONGITUDE)
            ): _num(-180, 180, 0.0001, "°"),
            vol.Required(
                CONF_ELEVATION, default=defaults.get(CONF_ELEVATION)
            ): _num(-100, 5000, 1, "m"),
            vol.Required(
                CONF_HOURLY, default=defaults.get(CONF_HOURLY, DEFAULT_HOURLY)
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_UPDATE_MINUTES,
                default=defaults.get(CONF_UPDATE_MINUTES, DEFAULT_UPDATE_MINUTES),
            ): _num(MIN_UPDATE_MINUTES, 720, 5, "min"),
        }
    )


def _zone_schema(defaults: dict, with_name: bool = True) -> vol.Schema:
    fields: dict = {}
    if with_name:
        fields[vol.Required(CONF_ZONE_NAME, default=defaults.get(CONF_ZONE_NAME, ""))] = (
            selector.TextSelector()
        )
    fields[vol.Required(CONF_ZONE_KC, default=defaults.get(CONF_ZONE_KC, 0.8))] = _num(
        0.1, 1.5, 0.05
    )
    fields[
        vol.Required(
            CONF_ZONE_THROUGHPUT,
            default=defaults.get(CONF_ZONE_THROUGHPUT, DEFAULT_THROUGHPUT),
        )
    ] = _num(0.5, 200, 0.5, "mm/h")
    fields[
        vol.Required(
            CONF_ZONE_EFFICIENCY,
            default=defaults.get(CONF_ZONE_EFFICIENCY, DEFAULT_EFFICIENCY),
        )
    ] = _num(0.1, 1.0, 0.05)
    fields[
        vol.Required(
            CONF_ZONE_MAX_DEFICIT,
            default=defaults.get(CONF_ZONE_MAX_DEFICIT, DEFAULT_MAX_DEFICIT),
        )
    ] = _num(1, 100, 1, "mm")
    return vol.Schema(fields)


class IriyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Erststart-Flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Iriy", data=user_input)

        defaults = {
            CONF_LATITUDE: round(self.hass.config.latitude, 4),
            CONF_LONGITUDE: round(self.hass.config.longitude, 4),
            CONF_ELEVATION: float(self.hass.config.elevation or 0),
        }
        return self.async_show_form(
            step_id="user", data_schema=_settings_schema(defaults)
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return IriyOptionsFlow(entry)


class IriyOptionsFlow(OptionsFlow):
    """Bearbeiten von Einstellungen und Zonen ueber die UI."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._edit_zone: str | None = None

    # --- aktueller Stand (Options haben Vorrang vor Daten) --------------

    def _merged(self) -> dict:
        return {**self._entry.data, **self._entry.options}

    def _zones(self) -> list[dict]:
        return list(self._merged().get(CONF_ZONES, []))

    def _save(self, options: dict) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=options)

    # --- Menue ----------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "add_zone", "edit_zone", "remove_zone"],
        )

    # --- Einstellungen --------------------------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            merged = {**self._entry.options, **user_input}
            merged[CONF_ZONES] = self._zones()
            return self._save(merged)
        # Aktuelle Werte (inkl. optionaler Sensoren) als Vorbelegung einspielen.
        schema = self.add_suggested_values_to_schema(
            _settings_schema(self._merged()), self._merged()
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    # --- Zone hinzufuegen ----------------------------------------------

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_ZONE_NAME].strip()
            if not name:
                errors["base"] = "name_required"
            elif any(z[CONF_ZONE_NAME] == name for z in self._zones()):
                errors["base"] = "name_exists"
            else:
                zones = self._zones()
                zones.append({**user_input, CONF_ZONE_NAME: name})
                return self._save({**self._entry.options, CONF_ZONES: zones})
        return self.async_show_form(
            step_id="add_zone", data_schema=_zone_schema({}), errors=errors
        )

    # --- Zone bearbeiten -----------------------------------------------

    async def async_step_edit_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._zones()
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            self._edit_zone = user_input[CONF_ZONE_NAME]
            return await self.async_step_edit_zone_form()
        names = [z[CONF_ZONE_NAME] for z in zones]
        return self.async_show_form(
            step_id="edit_zone",
            data_schema=vol.Schema({vol.Required(CONF_ZONE_NAME): _select(names)}),
        )

    async def async_step_edit_zone_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._zones()
        current = next(
            (z for z in zones if z[CONF_ZONE_NAME] == self._edit_zone), None
        )
        if current is None:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            new = [
                {**user_input, CONF_ZONE_NAME: self._edit_zone}
                if z[CONF_ZONE_NAME] == self._edit_zone
                else z
                for z in zones
            ]
            return self._save({**self._entry.options, CONF_ZONES: new})
        return self.async_show_form(
            step_id="edit_zone_form",
            data_schema=_zone_schema(current, with_name=False),
            description_placeholders={"zone": self._edit_zone},
        )

    # --- Zone entfernen -------------------------------------------------

    async def async_step_remove_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._zones()
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            name = user_input[CONF_ZONE_NAME]
            new = [z for z in zones if z[CONF_ZONE_NAME] != name]
            return self._save({**self._entry.options, CONF_ZONES: new})
        names = [z[CONF_ZONE_NAME] for z in zones]
        return self.async_show_form(
            step_id="remove_zone",
            data_schema=vol.Schema({vol.Required(CONF_ZONE_NAME): _select(names)}),
        )
