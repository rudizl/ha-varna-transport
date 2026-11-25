"""Varna public transport arrivals, with failover between two sources."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import VarnaTransportConfigEntry, VarnaTransportCoordinator, get_clients

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: VarnaTransportConfigEntry) -> bool:
    """Set up one stop."""
    coordinator = VarnaTransportCoordinator(hass, entry, get_clients(hass))
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
