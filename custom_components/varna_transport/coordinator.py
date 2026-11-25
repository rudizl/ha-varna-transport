"""Data coordinator with automatic failover between the two sources."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    TZ,
    Departure,
    LiveTransportClient,
    StopBoard,
    TransportError,
    TransportRateLimitedError,
    VarnaTrafficClient,
    normalize_line,
)
from .const import (
    BACKOFF,
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
    FAILOVER_AFTER,
    INTERVAL_LIVETRANSPORT,
    INTERVAL_NIGHT,
    INTERVAL_VARNATRAFFIC,
    LT_LIMIT,
    LT_LIMIT_FILTERED,
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
    """Bookkeeping for one source, shared by all stops."""

    failures: int = 0
    first_failure: datetime | None = None
    last_error: str | None = None
    last_success: datetime | None = None
    skip_until: datetime | None = None  # failover: try the other source first
    blocked_until: datetime | None = None  # 403/429: do not ask at all

    def success(self, now: datetime) -> None:
        self.failures = 0
        self.first_failure = None
        self.last_error = None
        self.last_success = now
        self.skip_until = None

    def failure(self, now: datetime, err: Exception) -> None:
        self.failures += 1
        self.first_failure = self.first_failure or now
        self.last_error = str(err) or type(err).__name__
        if isinstance(err, TransportRateLimitedError):
            self.blocked_until = now + timedelta(seconds=err.retry_after)

    def skipped(self, now: datetime) -> bool:
        return self.skip_until is not None and now < self.skip_until

    def blocked(self, now: datetime) -> bool:
        return self.blocked_until is not None and now < self.blocked_until


@dataclass
class ClientSet:
    """Shared API clients, one per source, for all stops and the config flow."""

    varnatraffic: VarnaTrafficClient
    livetransport: LiveTransportClient
    health: dict[str, SourceHealth] = field(
        default_factory=lambda: {
            SOURCE_VARNATRAFFIC: SourceHealth(),
            SOURCE_LIVETRANSPORT: SourceHealth(),
        }
    )


def get_clients(hass: HomeAssistant) -> ClientSet:
    """The shared clients, created on first use (they hold caches)."""
    if DOMAIN not in hass.data:
        session = async_get_clientsession(hass)
        hass.data[DOMAIN] = ClientSet(
            varnatraffic=VarnaTrafficClient(session),
            livetransport=LiveTransportClient(session),
        )
    return hass.data[DOMAIN]


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
    """True inside the nightly break, in Varna time."""
    return NIGHT_START <= now.astimezone(TZ).time() < NIGHT_END


def night_interval(now: datetime) -> timedelta:
    """Poll slowly at night, but wake up right when the break ends."""
    local = now.astimezone(TZ)
    end = datetime.combine(local.date(), NIGHT_END, tzinfo=TZ)
    return max(timedelta(seconds=30), min(INTERVAL_NIGHT, end - local))


def merge_combined(departures: list[Departure]) -> list[Departure]:
    """One row per bus: a combined run (same bus, same time) becomes "17/46"."""
    merged: list[Departure] = []
    seen: dict[tuple[str, datetime], int] = {}
    for dep in departures:
        key = (dep.vehicle, dep.expected) if dep.vehicle else None
        if key and key in seen:
            first = merged[seen[key]]
            if dep.line not in first.line.split("/"):
                merged[seen[key]] = replace(first, line=f"{first.line}/{dep.line}")
            continue
        if key:
            seen[key] = len(merged)
        merged.append(dep)
    return merged


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
        self._all_failed = 0

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

    @property
    def line_names(self) -> dict[int, str]:
        return {
            **DEFAULT_LINE_NAMES,
            **parse_line_names(self.config_entry.options.get(CONF_LINE_NAMES)),
        }

    def _order(self, now: datetime) -> list[str]:
        """Sources to try this round, best first."""
        vt = self.clients.health[SOURCE_VARNATRAFFIC]
        lt = self.clients.health[SOURCE_LIVETRANSPORT]
        use_vt = bool(self.vt_stop) and not vt.blocked(now)
        use_lt = self.fallback_enabled and not lt.blocked(now)
        if use_vt and vt.skipped(now) and use_lt:
            return [SOURCE_LIVETRANSPORT, SOURCE_VARNATRAFFIC]
        return [
            s for s, ok in ((SOURCE_VARNATRAFFIC, use_vt), (SOURCE_LIVETRANSPORT, use_lt)) if ok
        ]

    async def _fetch(self, source: str) -> StopBoard:
        if source == SOURCE_VARNATRAFFIC:
            return await self.clients.varnatraffic.departures(self.vt_stop, self.line_names)
        limit = LT_LIMIT_FILTERED if self.monitored_lines else LT_LIMIT
        return await self.clients.livetransport.departures(self.lt_stop, limit)

    def _note_failure(self, source: str, now: datetime, err: Exception) -> None:
        health = self.clients.health[source]
        health.failure(now, err)
        if (
            source == SOURCE_VARNATRAFFIC
            and health.failures >= 2
            and now - health.first_failure >= FAILOVER_AFTER
            and not health.skipped(now)
        ):
            health.skip_until = now + PRIMARY_RETRY
            _LOGGER.warning(
                "varnatraffic has failed for %s; using livetransport first for %s",
                FAILOVER_AFTER,
                PRIMARY_RETRY,
            )

    async def _async_update_data(self) -> StopBoard:
        now = dt_util.utcnow()
        night = self.config_entry.options.get(CONF_NIGHT_PAUSE, DEFAULT_NIGHT_PAUSE) and is_night(
            now
        )

        errors: list[str] = []
        for source in self._order(now):
            try:
                board = await self._fetch(source)
            except TransportError as err:
                self._note_failure(source, now, err)
                errors.append(f"{source}: {err}")
                _LOGGER.debug("%s failed for %s: %s", source, self.config_entry.title, err)
                continue
            except Exception as err:
                _LOGGER.exception("Unexpected error from %s", source)
                self._note_failure(source, now, err)
                errors.append(f"{source}: {err!r}")
                continue

            self.clients.health[source].success(now)
            if source != self.active_source:
                if self.active_source == SOURCE_VARNATRAFFIC:
                    self.failovers += 1
                if self.active_source:
                    _LOGGER.warning(
                        "%s: data source %s -> %s",
                        self.config_entry.title,
                        self.active_source,
                        source,
                    )
            self.active_source = source
            self._all_failed = 0

            if night:
                self.update_interval = night_interval(now)
            elif source == SOURCE_LIVETRANSPORT:
                self.update_interval = INTERVAL_LIVETRANSPORT
            else:
                self.update_interval = INTERVAL_VARNATRAFFIC
            return board

        # Nothing worked: ask less and less often, both for our sake and theirs.
        self._all_failed += 1
        backoff = BACKOFF[min(self._all_failed, len(BACKOFF)) - 1]
        self.update_interval = max(backoff, night_interval(now)) if night else backoff
        raise UpdateFailed("; ".join(errors) or "No data source available")

    def upcoming(self, line: str | None = None) -> list[Departure]:
        """Departures after filters, soonest first."""
        if not self.data:
            return []
        if line:
            return [d for d in self.data.departures if d.line == line]
        wanted = self.monitored_lines
        deps = [d for d in self.data.departures if not wanted or d.line in wanted]
        return merge_combined(deps)
