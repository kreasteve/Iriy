"""Iriy-Sensoren.

Globale Entities (stabile IDs via translation_key):
  * sensor.iriy_et0_daily    – kanonischer ET0-Tageswert [mm/Tag]
  * sensor.iriy_et0_today    – heute bisher aufsummiert [mm] (sub-taeglich)
  * sensor.iriy_et0_rate     – aktuelle ET-Rate [mm/h]

Pro Zone (ID aus dem deutschen Anzeigenamen):
  * sensor.iriy_<zone>_defizit   – Wasserdefizit [mm]
  * sensor.iriy_<zone>_laufzeit  – noetige Bewaesserungszeit [min]
"""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IriyCoordinator, IriyData, ZoneState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensoren fuer einen Entry anlegen."""
    coordinator: IriyCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        IriyValueSensor(
            coordinator, entry, "et0_daily", "mm", "mdi:water-percent",
            lambda d: d.et0_daily, _et0_attrs, SensorStateClass.MEASUREMENT,
        ),
        IriyValueSensor(
            coordinator, entry, "et0_today", "mm", "mdi:counter",
            lambda d: d.et0_today, None, SensorStateClass.TOTAL_INCREASING,
        ),
        IriyValueSensor(
            coordinator, entry, "et0_rate", "mm/h", "mdi:speedometer",
            lambda d: d.et0_rate, None, SensorStateClass.MEASUREMENT,
        ),
    ]
    for name in coordinator.zones:
        entities.append(IriyZoneDeficitSensor(coordinator, entry, name))
        entities.append(IriyZoneRuntimeSensor(coordinator, entry, name))

    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "Iriy",
        manufacturer="Iriy",
        model="Smart Irrigation (FAO-56)",
    )


def _et0_attrs(data: IriyData) -> dict:
    attrs = dict(data.diagnostics)
    attrs["provisional_today"] = data.et0_daily_provisional
    return attrs


class _IriyBase(CoordinatorEntity[IriyCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: IriyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)


class IriyValueSensor(_IriyBase):
    """Generischer Sensor, der einen Wert aus IriyData liest.

    Name kommt ueber translation_key aus den Uebersetzungen; daraus leitet
    HA auch die stabile entity_id ab (sensor.iriy_<key>).
    """

    def __init__(
        self,
        coordinator: IriyCoordinator,
        entry: ConfigEntry,
        key: str,
        unit: str,
        icon: str,
        getter: Callable[[IriyData], float | None],
        attr_getter: Callable[[IriyData], dict] | None,
        state_class: SensorStateClass,
    ) -> None:
        super().__init__(coordinator, entry)
        self._getter = getter
        self._attr_getter = attr_getter
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_unique_id = f"{entry.entry_id}_{key}"

    @property
    def native_value(self) -> float | None:
        return self._getter(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self._attr_getter is None:
            return None
        return self._attr_getter(self.coordinator.data)


class _IriyZoneBase(_IriyBase):
    def __init__(
        self, coordinator: IriyCoordinator, entry: ConfigEntry, zone_name: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._zone_name = zone_name

    @property
    def _zone(self) -> ZoneState | None:
        return self.coordinator.data.zones.get(self._zone_name)


class IriyZoneDeficitSensor(_IriyZoneBase):
    """Aktuelles Wasserdefizit einer Zone."""

    _attr_native_unit_of_measurement = "mm"
    _attr_icon = "mdi:cup-water"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, zone_name) -> None:
        super().__init__(coordinator, entry, zone_name)
        self._attr_name = f"{zone_name.capitalize()} Defizit"
        self._attr_unique_id = f"{entry.entry_id}_{zone_name}_deficit"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        return round(zone.deficit, 2) if zone else None

    @property
    def extra_state_attributes(self) -> dict | None:
        zone = self._zone
        if not zone:
            return None
        return {
            "kc": zone.kc,
            "etc_today_mm": round(zone.etc_today, 2),
            "max_deficit_mm": zone.max_deficit,
        }


class IriyZoneRuntimeSensor(_IriyZoneBase):
    """Empfohlene Bewaesserungszeit einer Zone."""

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:sprinkler-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, zone_name) -> None:
        super().__init__(coordinator, entry, zone_name)
        self._attr_name = f"{zone_name.capitalize()} Laufzeit"
        self._attr_unique_id = f"{entry.entry_id}_{zone_name}_runtime"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        return zone.runtime_minutes if zone else None

    @property
    def extra_state_attributes(self) -> dict | None:
        zone = self._zone
        if not zone:
            return None
        return {
            "throughput_mm_h": zone.throughput,
            "efficiency": zone.efficiency,
        }
