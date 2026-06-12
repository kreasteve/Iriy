"""Iriy – Smart Irrigation fuer Home Assistant.

Iriy ("weiblicher Bewaesserungs-Bot") berechnet die Referenz-
Evapotranspiration (FAO-56) aus deiner Wetterstation und fuehrt pro
Zone eine Wasser-Defizit-Bilanz. Spaeter steuert sie daraus Ventile.

Dieses Modul ist nur die Verdrahtung: ConfigEntry einrichten, den
Koordinator starten, Plattformen laden und Services registrieren.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import IriyCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_RECALCULATE = "recalculate"
SERVICE_RESET_BUCKET = "reset_bucket"
SERVICE_ADD_WATER = "add_water"

ATTR_ZONE = "zone"
ATTR_MM = "mm"

_RESET_SCHEMA = vol.Schema({vol.Optional(ATTR_ZONE): cv.string})
_ADD_WATER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE): cv.string,
        vol.Required(ATTR_MM): vol.Coerce(float),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen Iriy-ConfigEntry einrichten."""
    coordinator = IriyCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen ConfigEntry sauber abbauen."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: IriyCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            _unregister_services(hass)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bei Options-Aenderung den Entry neu laden (Zonen/Quellen aktualisieren)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinators(hass: HomeAssistant) -> list[IriyCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RECALCULATE):
        return

    async def _recalculate(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            await coord.async_request_refresh()

    async def _reset_bucket(call: ServiceCall) -> None:
        zone = call.data.get(ATTR_ZONE)
        for coord in _coordinators(hass):
            coord.reset_bucket(zone)

    async def _add_water(call: ServiceCall) -> None:
        zone = call.data[ATTR_ZONE]
        mm = call.data[ATTR_MM]
        for coord in _coordinators(hass):
            coord.add_water(zone, mm)

    hass.services.async_register(
        DOMAIN, SERVICE_RECALCULATE, _recalculate
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_BUCKET, _reset_bucket, schema=_RESET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_WATER, _add_water, schema=_ADD_WATER_SCHEMA
    )


def _unregister_services(hass: HomeAssistant) -> None:
    for service in (SERVICE_RECALCULATE, SERVICE_RESET_BUCKET, SERVICE_ADD_WATER):
        hass.services.async_remove(DOMAIN, service)
