"""Varna public transport arrivals, with failover between two sources."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LiveTransportClient, VarnaTrafficClient
from .const import DOMAIN
from .coordinator import ClientSet, VarnaTransportConfigEntry, VarnaTransportCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


def _clients(hass: HomeAssistant) -> ClientSet:
    """One client per source, shared by all stops (line list and stop cache)."""
    if DOMAIN not in hass.data:
        session = async_get_clientsession(hass)
        hass.data[DOMAIN] = ClientSet(
            varnatraffic=VarnaTrafficClient(session),
            livetransport=LiveTransportClient(session),
        )
    return hass.data[DOMAIN]


async def async_setup_entry(hass: HomeAssistant, entry: VarnaTransportConfigEntry) -> bool:
    """Set up one stop."""
    coordinator = VarnaTransportCoordinator(hass, entry, _clients(hass))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: VarnaTransportConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VarnaTransportConfigEntry) -> bool:
    """Unload one stop."""
    # The shared clients stay in hass.data: they only hold caches.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
