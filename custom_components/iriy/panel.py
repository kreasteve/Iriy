"""Iriy-Sidebar-Panel + WebSocket-API fuer die eigene Oberflaeche.

Registriert EINEN eigenen Eintrag „Iriy" in der HA-Seitenleiste (panel_custom),
liefert die zugehoerige Web-Oberflaeche (frontend/iriy-panel.js) aus und stellt
WS-Befehle bereit, mit denen das Panel den Zustand liest und Zonen verwaltet.

Bewusst KEIN Add-on (laeuft nur auf HA-OS/Supervisor) und KEIN iframe – ein
panel_custom-Webcomponent laeuft auf allen Installationsarten und ist Teil des
HACS-Pakets.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ZONE_AREA,
    CONF_ZONE_BY_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_KC,
    CONF_ZONE_MAX_DEFICIT,
    CONF_ZONE_NAME,
    CONF_ZONE_THROUGHPUT,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_EFFICIENCY,
    DEFAULT_KC,
    DEFAULT_MAX_DEFICIT,
    DEFAULT_THROUGHPUT,
    DOMAIN,
)
from .coordinator import IriyCoordinator

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "iriy"
PANEL_STATIC_URL = "/iriy_frontend"
# Eigener Top-Level-Key (NICHT in hass.data[DOMAIN], wo nur Koordinatoren liegen).
_STATIC_FLAG = f"{DOMAIN}_static_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """WS-Befehle/statische Pfade EINMAL registrieren; das Panel bei JEDEM
    Setup mit der aktuellen Version – so folgt module_url ?v= der installierten
    Version und der Browser-Cache bricht nach jedem Update auf (auch ohne
    vollen HA-Neustart)."""
    integration = await async_get_integration(hass, DOMAIN)
    version = integration.version or "0"

    # WS-Befehle + statische Pfade nur EINMAL pro HA-Lauf (eine zweite
    # Pfad-Registrierung wuerde fehlschlagen).
    if not hass.data.get(_STATIC_FLAG):
        websocket_api.async_register_command(hass, ws_overview)
        websocket_api.async_register_command(hass, ws_zone_save)
        websocket_api.async_register_command(hass, ws_zone_delete)
        frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, frontend_dir, False)]
        )
        hass.data[_STATIC_FLAG] = True

    # Panel bei jedem Setup frisch registrieren (alte Registrierung vorher
    # entfernen). Bricht den ?v=-Cache nach einem Update auf.
    from homeassistant.components import frontend

    try:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception:  # noqa: BLE001
        pass
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="iriy-panel",
        module_url=f"{PANEL_STATIC_URL}/iriy-panel.js?v={version}",
        sidebar_title="Iriy",
        sidebar_icon="mdi:sprinkler-variant",
        require_admin=False,
        config={},
        embed_iframe=False,
    )
    _LOGGER.debug("Iriy: Sidebar-Panel registriert (v%s)", version)


# --- Helfer ------------------------------------------------------------


def _merged(entry: ConfigEntry) -> dict:
    return {**entry.data, **entry.options}


def _f(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _instance_zones(coord: IriyCoordinator) -> list[dict]:
    """Laufzeit-Zustand (Defizit/Laufzeit) mit der Roh-Konfig je Zone mischen."""
    raw_by_name = {
        z[CONF_ZONE_NAME]: z
        for z in _merged(coord.entry).get(CONF_ZONES, [])
        if CONF_ZONE_NAME in z
    }
    out = []
    for zone in coord.zones.values():
        raw = raw_by_name.get(zone.name, {})
        out.append(
            {
                "name": zone.name,
                "kc": zone.kc,
                "throughput": zone.throughput,
                "efficiency": zone.efficiency,
                "max_deficit": zone.max_deficit,
                "area": raw.get(CONF_ZONE_AREA),
                "by_area": zone.by_area,
                "valve": zone.valve,
                "deficit": round(zone.deficit, 2),
                "etc_today": round(zone.etc_today, 2),
                "runtime_minutes": zone.runtime_minutes,
                "liters_needed": zone.liters_needed,
                "gegossen_l": round(zone.gegossen_l, 1),
            }
        )
    return out


def _coordinators(hass: HomeAssistant) -> dict[str, IriyCoordinator]:
    return {
        eid: c
        for eid, c in hass.data.get(DOMAIN, {}).items()
        if isinstance(c, IriyCoordinator)
    }


# --- WebSocket-Befehle -------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.async_response
async def ws_overview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Gesamtzustand aller Iriy-Instanzen fuer das Panel."""
    instances = []
    for entry_id, coord in _coordinators(hass).items():
        d = coord.data
        instances.append(
            {
                "entry_id": entry_id,
                "title": coord.entry.title or "Iriy",
                "et0_daily": d.et0_daily,
                "et0_daily_provisional": d.et0_daily_provisional,
                "et0_today": round(d.et0_today, 2) if d.et0_today is not None else None,
                "et0_rate": d.et0_rate,
                "diagnostics": d.diagnostics,
                "last_days": [
                    {"date": k, "mm": v}
                    for k, v in sorted(coord.et0_recent.items(), reverse=True)
                ],
                "rain_recent": coord.rain_recent,
                "zone_history": coord.zone_recent,
                "rain_today": round(d.diagnostics.get("rain_today_mm", 0.0), 1)
                if d.diagnostics
                else None,
                "zones": _instance_zones(coord),
            }
        )
    connection.send_result(
        msg["id"], {"instances": instances, "kc_table": DEFAULT_KC}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/zone/save",
        vol.Required("entry_id"): str,
        vol.Required("zone"): dict,
        vol.Optional("original_name"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_zone_save(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Zone anlegen oder bearbeiten (schreibt in options -> loest Reload aus)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Adminrechte noetig")
        return
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "Instanz nicht gefunden")
        return

    zone_in = msg["zone"]
    name = str(zone_in.get(CONF_ZONE_NAME, "")).strip()
    if not name:
        connection.send_error(msg["id"], "invalid", "Name fehlt")
        return

    zones = list(_merged(entry).get(CONF_ZONES, []))
    original = msg.get("original_name")

    # Beim Bearbeiten die bestehende Zone als Basis nehmen, damit unbekannte
    # Schluessel (z. B. valve fuer spaetere Ventilsteuerung) erhalten bleiben.
    base: dict = {}
    if original:
        existing = next(
            (z for z in zones if z.get(CONF_ZONE_NAME) == original), None
        )
        if existing is None:
            connection.send_error(msg["id"], "not_found", "Zone nicht gefunden")
            return
        if name != original and any(z.get(CONF_ZONE_NAME) == name for z in zones):
            connection.send_error(msg["id"], "exists", "Name existiert bereits")
            return
        base = dict(existing)
    elif any(z.get(CONF_ZONE_NAME) == name for z in zones):
        connection.send_error(msg["id"], "exists", "Name existiert bereits")
        return

    zone = {
        **base,
        CONF_ZONE_NAME: name,
        CONF_ZONE_KC: _f(zone_in.get(CONF_ZONE_KC), 0.8),
        CONF_ZONE_THROUGHPUT: _f(zone_in.get(CONF_ZONE_THROUGHPUT), DEFAULT_THROUGHPUT),
        CONF_ZONE_EFFICIENCY: _f(zone_in.get(CONF_ZONE_EFFICIENCY), DEFAULT_EFFICIENCY),
        CONF_ZONE_MAX_DEFICIT: _f(
            zone_in.get(CONF_ZONE_MAX_DEFICIT), DEFAULT_MAX_DEFICIT
        ),
    }
    zone[CONF_ZONE_BY_AREA] = bool(zone_in.get(CONF_ZONE_BY_AREA))
    area = zone_in.get(CONF_ZONE_AREA)
    if area not in (None, ""):
        zone[CONF_ZONE_AREA] = _f(area, 0.0)
    else:
        zone.pop(CONF_ZONE_AREA, None)  # leeres Feld -> Flaeche entfernen
    valve = zone_in.get(CONF_ZONE_VALVE)
    if valve:
        zone[CONF_ZONE_VALVE] = str(valve)
    else:
        zone.pop(CONF_ZONE_VALVE, None)

    if original:
        zones = [zone if z.get(CONF_ZONE_NAME) == original else z for z in zones]
    else:
        zones.append(zone)

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ZONES: zones}
    )
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/zone/delete",
        vol.Required("entry_id"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_zone_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Zone loeschen (schreibt in options -> loest Reload aus)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Adminrechte noetig")
        return
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "Instanz nicht gefunden")
        return
    zones = [
        z
        for z in _merged(entry).get(CONF_ZONES, [])
        if z.get(CONF_ZONE_NAME) != msg["name"]
    ]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ZONES: zones}
    )
    connection.send_result(msg["id"], {"ok": True})
