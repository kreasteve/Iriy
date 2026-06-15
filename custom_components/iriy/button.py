"""Iriy-Buttons.

  * button.iriy_verlauf_neu_berechnen – ET0-Langzeitstatistik der letzten Tage
    neu aus der Recorder-STUNDENstatistik berechnen (kanonische Methode,
    idempotenter Upsert). Raeumt alte, frueher count-gewichtet geschriebene
    Tageswerte im Verlaufsgraphen auf.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_BACKFILL_DAYS, DOMAIN
from .coordinator import IriyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Buttons fuer einen Iriy-ConfigEntry anlegen."""
    coordinator: IriyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IriyRecalcHistoryButton(coordinator, entry)])


class IriyRecalcHistoryButton(CoordinatorEntity[IriyCoordinator], ButtonEntity):
    """Stoesst die rueckwirkende ET0-Neuberechnung an (Verlauf aufraeumen)."""

    _attr_has_entity_name = True
    _attr_translation_key = "recalc_history"
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: IriyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_recalc_history"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or "Iriy",
            manufacturer="Iriy",
            model="Smart Irrigation (FAO-56)",
        )

    async def async_press(self) -> None:
        """Vergangene Tage (so viele wie konfiguriert) neu rechnen + gestern finalisieren."""
        days = self.coordinator.history_days or DEFAULT_BACKFILL_DAYS
        await self.coordinator.async_import_history_statistics(days)
        await self.coordinator.async_finalize_yesterday()
