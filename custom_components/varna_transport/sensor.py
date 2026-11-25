"""Sensors for one stop."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import Departure
from .const import (
    CONF_LINE_SENSORS,
    CONF_STOP_NAME,
    CONF_UPCOMING,
    DEFAULT_LINE_SENSORS,
    DEFAULT_UPCOMING,
    DOMAIN,
    SOURCES,
)
from .coordinator import VarnaTransportConfigEntry, VarnaTransportCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VarnaTransportConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors of one stop."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NextDepartureSensor(coordinator),
        MinutesSensor(coordinator),
        SourceSensor(coordinator),
    ]
    if entry.options.get(CONF_LINE_SENSORS, DEFAULT_LINE_SENSORS):
        entities += [LineSensor(coordinator, line) for line in sorted(coordinator.monitored_lines)]

    # Line sensors for lines no longer watched would stay behind as orphans.
    wanted = {e.unique_id for e in entities}
    registry = er.async_get(hass)
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain == "sensor" and reg.unique_id not in wanted:
            registry.async_remove(reg.entity_id)

    async_add_entities(entities)


def _entry_key(coordinator: VarnaTransportCoordinator) -> str:
    entry = coordinator.config_entry
    return entry.unique_id or entry.entry_id


class StopEntity(CoordinatorEntity[VarnaTransportCoordinator]):
    """Common base: one device per stop."""

    _attr_has_entity_name = True
    # The board changes every poll; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"upcoming", "minutes"})

    def __init__(self, coordinator: VarnaTransportCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{_entry_key(coordinator)}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, _entry_key(coordinator))},
            name=entry.data.get(CONF_STOP_NAME, entry.title),
            manufacturer="Varna public transport",
            model="Bus stop",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=(
                f"https://varnatraffic.com/Station/Index/{coordinator.vt_stop}"
                if coordinator.vt_stop
                else None
            ),
        )

    def _departures(self) -> list[Departure]:
        return self.coordinator.upcoming()

    @property
    def _now(self) -> datetime:
        return dt_util.utcnow()


class NextDepartureSensor(StopEntity, SensorEntity):
    """When the next bus is expected, with the full board as an attribute."""

    _attr_translation_key = "next_departure"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: VarnaTransportCoordinator) -> None:
        super().__init__(coordinator, "next_departure")

    @property
    def native_value(self) -> datetime | None:
        deps = self._departures()
        return deps[0].expected if deps else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self._now
        deps = self._departures()
        limit = int(self.coordinator.config_entry.options.get(CONF_UPCOMING, DEFAULT_UPCOMING))
        attrs: dict[str, Any] = {
            "source": self.coordinator.active_source,
            "upcoming": [d.as_dict(now) for d in deps[:limit]],
        }
        if deps:
            first = deps[0]
            attrs.update(
                line=first.line,
                destination=first.destination,
                minutes=first.minutes(now),
                delay=first.delay,
                live=first.live,
                vehicle=first.vehicle,
            )
        return attrs


class MinutesSensor(StopEntity, SensorEntity):
    """Minutes until the next bus - easy for voice and automations."""

    _attr_translation_key = "next_departure_minutes"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator: VarnaTransportCoordinator) -> None:
        super().__init__(coordinator, "next_departure_minutes")

    @property
    def native_value(self) -> int | None:
        deps = self._departures()
        return deps[0].minutes(self._now) if deps else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        deps = self._departures()
        return {"line": deps[0].line, "live": deps[0].live} if deps else {}


class LineSensor(StopEntity, SensorEntity):
    """Next departure of one line."""

    _attr_translation_key = "line"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: VarnaTransportCoordinator, line: str) -> None:
        super().__init__(coordinator, f"line_{line}")
        self._line = line
        self._attr_translation_placeholders = {"line": line}

    def _departures(self) -> list[Departure]:
        return self.coordinator.upcoming(self._line)

    @property
    def native_value(self) -> datetime | None:
        deps = self._departures()
        return deps[0].expected if deps else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self._now
        deps = self._departures()
        attrs: dict[str, Any] = {"upcoming": [d.as_dict(now) for d in deps[:5]]}
        if deps:
            attrs.update(
                minutes=deps[0].minutes(now),
                live=deps[0].live,
                delay=deps[0].delay,
                vehicle=deps[0].vehicle,
            )
        return attrs


class SourceSensor(StopEntity, SensorEntity):
    """Which source is serving data right now."""

    _attr_translation_key = "data_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SOURCES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: VarnaTransportCoordinator) -> None:
        super().__init__(coordinator, "data_source")

    @property
    def available(self) -> bool:
        # Stays available during an outage so the failover is visible.
        return self.coordinator.active_source is not None

    @property
    def native_value(self) -> str | None:
        return self.coordinator.active_source if self.coordinator.last_update_success else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self.coordinator.clients.health
        return {
            "failovers": self.coordinator.failovers,
            **{f"{name}_failures": h.failures for name, h in health.items()},
            **{f"{name}_last_error": h.last_error for name, h in health.items()},
        }
