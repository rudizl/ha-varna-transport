"""Diagnostics download, with a live probe of both sources."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import StopBoard, TransportError
from .const import DEFAULT_LINE_NAMES, SOURCE_LIVETRANSPORT, SOURCE_VARNATRAFFIC
from .coordinator import VarnaTransportConfigEntry, VarnaTransportCoordinator


def _board(board: StopBoard, now: datetime, limit: int | None = None) -> dict[str, Any]:
    deps = board.departures if limit is None else board.departures[:limit]
    return {
        "source": board.source,
        "fetched": board.fetched.isoformat(),
        "count": len(board.departures),
        "departures": [d.as_dict(now) for d in deps],
    }


async def _probe(coordinator: VarnaTransportCoordinator, source: str) -> dict[str, Any]:
    """Ask one source directly, outside the failover logic.

    This is how the fallback can be checked while the primary still works.
    The health counters are not touched.
    """
    stop = coordinator.vt_stop if source == SOURCE_VARNATRAFFIC else coordinator.lt_stop
    if not stop:
        return {"stop": None, "ok": False, "error": "no stop configured for this source"}
    started = dt_util.utcnow()
    try:
        if source == SOURCE_VARNATRAFFIC:
            board = await coordinator.clients.varnatraffic.departures(stop, DEFAULT_LINE_NAMES)
        else:
            board = await coordinator.clients.livetransport.departures(stop)
    except TransportError as err:
        return {"stop": stop, "ok": False, "error": str(err)}
    elapsed = (dt_util.utcnow() - started).total_seconds()
    return {"stop": stop, "ok": True, "seconds": round(elapsed, 2), **_board(board, started, 10)}


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
        "board": _board(board, now) if board else None,
        "probe": {
            SOURCE_VARNATRAFFIC: await _probe(coordinator, SOURCE_VARNATRAFFIC),
            SOURCE_LIVETRANSPORT: await _probe(coordinator, SOURCE_LIVETRANSPORT),
        },
    }
