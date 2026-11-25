"""Data coordinator with automatic failover between the two sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    LiveTransportClient,
    StopBoard,
    TransportError,
    VarnaTrafficClient,
    normalize_line,
)
from .const import (
    CONF_FALLBACK,
    CONF_LINE_NAMES,
    CONF_LINES,
    CONF_LT_STOP_ID,
    CONF_NIGHT_PAUSE,
    CONF_VT_STOP_ID,
    DEFAULT_FALLBACK,
    DEFAULT_LINE_NAMES,
    DEFAULT_NIGHT_PAUSE,
    DOMAIN,
    FAILURES_BEFORE_FAILOVER,
    INTERVAL_LIVETRANSPORT,
    INTERVAL_NIGHT,
    INTERVAL_VARNATRAFFIC,
    NIGHT_END,
    NIGHT_START,
    PRIMARY_RETRY,
    SOURCE_LIVETRANSPORT,
    SOURCE_VARNATRAFFIC,
)

_LOGGER = logging.getLogger(__name__)

type VarnaTransportConfigEntry = ConfigEntry[VarnaTransportCoordinator]


@dataclass
class SourceHealth:
    """Bookkeeping for one source."""

    failures: int = 0
    last_error: str | None = None
    last_success: datetime | None = None
    skip_until: datetime | None = None


@dataclass
class ClientSet:
    """Shared API clients, one per source, for all stops."""

    varnatraffic: VarnaTrafficClient
    livetransport: LiveTransportClient
    health: dict[str, SourceHealth] = field(
        default_factory=lambda: {
            SOURCE_VARNATRAFFIC: SourceHealth(),
            SOURCE_LIVETRANSPORT: SourceHealth(),
        }
    )


def parse_line_names(text: str | None) -> dict[int, str]:
    """Options text "219=209б, 700=46/17" -> {219: "209б", 700: "46/17"}."""
    names: dict[int, str] = {}
    for part in (text or "").replace(";", ",").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip().isdigit() and value.strip():
            names[int(key.strip())] = normalize_line(value)
    return names


def parse_lines(text: str | None) -> set[str]:
    """Options text "9, 109, 14a" -> {"9", "109", "14а"}."""
    return {normalize_line(p) for p in (text or "").replace(";", ",").split(",") if p.strip()}


def is_night(now: datetime) -> bool:
    """True inside the nightly break, when no city lines run."""
    local = dt_util.as_local(now).time()
    return NIGHT_START <= local < NIGHT_END


class VarnaTransportCoordinator(DataUpdateCoordinator[StopBoard]):
    """Fetches one stop, preferring varnatraffic and falling back to livetransport."""

    config_entry: VarnaTransportConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: VarnaTransportConfigEntry, clients: ClientSet
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=INTERVAL_VARNATRAFFIC,
            always_update=True,
        )
        self.clients = clients
        self.active_source: str | None = None
        self.failovers = 0

    @property
    def vt_stop(self) -> str | None:
        return self.config_entry.data.get(CONF_VT_STOP_ID)

    @property
    def lt_stop(self) -> str | None:
        return self.config_entry.data.get(CONF_LT_STOP_ID)

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.lt_stop) and self.config_entry.options.get(CONF_FALLBACK, DEFAULT_FALLBACK)

    @property
    def monitored_lines(self) -> set[str]:
        return parse_lines(self.config_entry.options.get(CONF_LINES))

    def _order(self, now: datetime) -> list[str]:
        """Sources to try this round, best first."""
        order: list[str] = []
        vt_health = self.clients.health[SOURCE_VARNATRAFFIC]
        vt_skipped = vt_health.skip_until is not None and now < vt_health.skip_until
        if self.vt_stop and not vt_skipped:
            order.append(SOURCE_VARNATRAFFIC)
        if self.fallback_enabled:
            order.append(SOURCE_LIVETRANSPORT)
        if self.vt_stop and vt_skipped:
            order.append(SOURCE_VARNATRAFFIC)  # last resort
        return order

    async def _fetch(self, source: str) -> StopBoard:
        if source == SOURCE_VARNATRAFFIC:
            extra = {
                **DEFAULT_LINE_NAMES,
                **parse_line_names(self.config_entry.options.get(CONF_LINE_NAMES)),
            }
            return await self.clients.varnatraffic.departures(self.vt_stop, extra)
        return await self.clients.livetransport.departures(self.lt_stop)

    async def _async_update_data(self) -> StopBoard:
        now = dt_util.utcnow()
        night = self.config_entry.options.get(CONF_NIGHT_PAUSE, DEFAULT_NIGHT_PAUSE) and is_night(
            now
        )

        errors: list[str] = []
        for source in self._order(now):
            health = self.clients.health[source]
            try:
                board = await self._fetch(source)
            except TransportError as err:
                health.failures += 1
                health.last_error = str(err)
                if source == SOURCE_VARNATRAFFIC and health.failures >= FAILURES_BEFORE_FAILOVER:
                    health.skip_until = now + PRIMARY_RETRY
                errors.append(f"{source}: {err}")
                _LOGGER.debug("%s failed for %s: %s", source, self.config_entry.title, err)
                continue

            if health.failures:
                _LOGGER.info("%s is answering again", source)
            health.failures = 0
            health.last_error = None
            health.last_success = now
            health.skip_until = None

            if self.active_source and source != self.active_source:
                self.failovers += 1
                _LOGGER.warning(
                    "%s: switched data source %s -> %s",
                    self.config_entry.title,
                    self.active_source,
                    source,
                )
            self.active_source = source

            if night:
                self.update_interval = INTERVAL_NIGHT
            elif source == SOURCE_LIVETRANSPORT:
                self.update_interval = INTERVAL_LIVETRANSPORT
            else:
                self.update_interval = INTERVAL_VARNATRAFFIC
            return board

        self.update_interval = INTERVAL_NIGHT if night else INTERVAL_VARNATRAFFIC
        raise UpdateFailed("; ".join(errors) or "No data source configured")

    def upcoming(self, line: str | None = None):
        """Departures after filters, soonest first."""
        if not self.data:
            return []
        wanted = {line} if line else self.monitored_lines
        return [d for d in self.data.departures if not wanted or d.line in wanted]
