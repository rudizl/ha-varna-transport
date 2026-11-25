"""Diagnostics download."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import VarnaTransportConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VarnaTransportConfigEntry
) -> dict[str, Any]:
    """Return what is needed to debug a stop. Nothing here is personal."""
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    board = coordinator.data
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "active_source": coordinator.active_source,
        "failovers": coordinator.failovers,
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        "health": {
            name: {k: str(v) if v is not None else None for k, v in asdict(h).items()}
            for name, h in coordinator.clients.health.items()
        },
        "board": {
            "source": board.source,
            "fetched": board.fetched.isoformat(),
            "departures": [d.as_dict(now) for d in board.departures],
        }
        if board
        else None,
    }
