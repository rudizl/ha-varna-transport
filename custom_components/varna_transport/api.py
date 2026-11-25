"""Clients for the two Varna public transport data sources.

Nothing in this module imports Home Assistant, so it can be tested on its own.

* varnatraffic.com - run by MapSoft on the AVL data of Gradski Transport.
* livetransport.eu - an independent multi-city tracker that gets Varna from
  the TASRUD system. Used as a fallback when varnatraffic is down.

Both sources are turned into the same :class:`Departure` model. Any answer a
parser cannot make sense of raises :class:`TransportDataError`, so the caller
can fall back to the other source instead of crashing.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import html
import logging
import math
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    LT_STOP_CODE_OFFSET,
    REQUEST_TIMEOUT,
    SOURCE_LIVETRANSPORT,
    SOURCE_VARNATRAFFIC,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Sofia")

VT_BASE = "https://varnatraffic.com"
LT_BASE = "https://next.livetransport.eu"
LT_CITY = "varna"

# livetransport sends positional arrays; indices from its public front end,
# checked against live answers (departure = [trip, time, vehicle, occupancy],
# time = [scheduled ms, delay ms, status, platform]).
_LT_STOP = {"id": 0, "city": 1, "name": 3, "code": 4, "location": 5}
_LT_TRIP = {"route": 2, "headsign": 3}
_LT_ROUTE = {"id": 0, "short_name": 5}
_LT_DEP = {"trip": 0, "time": 1, "vehicle": 2}
_LT_TIME = {"scheduled": 0, "delay": 1, "status": 2}
# Status 1: the vehicle is on this trip, so the time is a GPS prediction.
# Status 2: the vehicle is still on an earlier trip; the time is the
# timetable plus any delay carried over. Not a live countdown.
_LT_STATUS_ON_TRIP = 1
_LT_STATUS_ON_PREVIOUS_TRIP = 2
_LT_STATUS_CANCELLED = 3

# varnatraffic: a vehicle silent for longer than this is not "live".
_VT_MAX_SILENCE = 300
# Schedule times are read as the matching clock time in this window.
_CLOCK_PAST = timedelta(hours=3)
_CLOCK_FUTURE = timedelta(hours=21)

_LINE_NAMES_TTL = 24 * 3600
_LINE_NAMES_RETRY = 10 * 60
_STOPS_TTL = 24 * 3600
_DEFAULT_RETRY_AFTER = 600

_LATIN_TO_CYRILLIC = str.maketrans({"a": "а", "b": "б", "v": "в", "e": "е"})
_PARSE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError)


class TransportError(Exception):
    """Base error for a source that could not deliver data."""


class TransportConnectionError(TransportError):
    """Network problem, timeout or HTTP error."""


class TransportRateLimitedError(TransportConnectionError):
    """The source refused us (403/429). Back off for retry_after seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransportDataError(TransportError):
    """The source answered with something we cannot parse."""


def normalize_line(name: str | int | None) -> str:
    """Return a canonical line name: lower case, Cyrillic suffix letters.

    varnatraffic writes "14a" (Latin), livetransport "14A"; both become "14а".
    """
    if name is None:
        return ""
    return str(name).strip().lower().translate(_LATIN_TO_CYRILLIC)


def _local(moment: datetime) -> datetime:
    """Varna local time, whole seconds: one format for every timestamp."""
    return moment.astimezone(TZ).replace(microsecond=0)


@dataclass(slots=True, frozen=True)
class Stop:
    """A stop of one source."""

    id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    code: str | None = None

    def distance_to(self, latitude: float | None, longitude: float | None) -> float:
        """Distance in metres (equirectangular, fine at city scale)."""
        if None in (self.latitude, self.longitude, latitude, longitude):
            return math.inf
        k = math.cos(math.radians((self.latitude + latitude) / 2))
        dx = math.radians(longitude - self.longitude) * k
        dy = math.radians(latitude - self.latitude)
        return 6_371_000 * math.hypot(dx, dy)


@dataclass(slots=True)
class Departure:
    """One upcoming bus at a stop, in a source-neutral form."""

    line: str
    expected: datetime
    source: str
    scheduled: datetime | None = None
    delay: int | None = None  # seconds, positive = late
    live: bool = False  # True only for a GPS countdown on this very trip
    destination: str | None = None
    vehicle: str | None = None
    distance: int | None = None  # metres to the stop

    def __post_init__(self) -> None:
        self.expected = _local(self.expected)
        if self.scheduled is not None:
            self.scheduled = _local(self.scheduled)

    def minutes(self, now: datetime) -> int:
        """Whole minutes until the bus is expected, never negative."""
        return max(0, int((self.expected - now).total_seconds() // 60))

    def as_dict(self, now: datetime) -> dict[str, Any]:
        """Attribute-friendly form."""
        data = asdict(self)
        data["expected"] = self.expected.isoformat()
        data["scheduled"] = self.scheduled.isoformat() if self.scheduled else None
        data["minutes"] = self.minutes(now)
        data.pop("source")
        return data


@dataclass(slots=True)
class StopBoard:
    """What a source returned for one stop."""

    source: str
    departures: list[Departure] = field(default_factory=list)
    fetched: datetime = field(default_factory=lambda: datetime.now(UTC))


class _BaseClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        as_text: bool = False,
    ) -> Any:
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(url, params=params, headers=hdrs) as resp:
                    if resp.status in (403, 429):
                        raise TransportRateLimitedError(
                            f"HTTP {resp.status} from {url}",
                            _retry_after(resp.headers.get("Retry-After")),
                        )
                    if resp.status != 200:
                        raise TransportConnectionError(f"HTTP {resp.status} from {url}")
                    if as_text:
                        return await resp.text()
                    try:
                        # Both servers mislabel content types now and then.
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        # e.g. a Cloudflare HTML error page with status 200
                        raise TransportDataError(f"Not JSON from {url}") from err
        except TimeoutError as err:
            raise TransportConnectionError(f"Timeout for {url}") from err
        except aiohttp.ClientError as err:
            raise TransportConnectionError(f"{err} for {url}") from err


def _retry_after(value: str | None) -> int:
    """Seconds from a Retry-After header; at least 5 minutes, at most a day."""
    try:
        seconds = int(value) if value is not None else _DEFAULT_RETRY_AFTER
    except ValueError:
        seconds = _DEFAULT_RETRY_AFTER
    return min(max(seconds, 300), 86400)


# --------------------------------------------------------------------------
# varnatraffic.com
# --------------------------------------------------------------------------

_VT_LINE_LINK = re.compile(r'href="/bg/Line/Routes/(\d+)"[^>]*>(.*?)</a>', re.S)


def _parse_clock(text: Any, now: datetime) -> datetime | None:
    """Turn "HH:MM" (Varna local time) into a datetime near now.

    The result lies between 3 hours ago and 21 hours ahead, so at 06:00 a
    22:45 departure is tonight and at 00:10 a 23:50 one was last night.
    """
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "") if isinstance(text, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    local_now = now.astimezone(TZ)
    candidate = datetime.combine(
        local_now.date(), datetime.min.time().replace(hour=int(match[1]), minute=int(match[2]))
    ).replace(tzinfo=TZ)
    if candidate < local_now - _CLOCK_PAST:
        candidate = (candidate + timedelta(days=1)).replace(tzinfo=TZ)
    elif candidate > local_now + _CLOCK_FUTURE:
        candidate = (candidate - timedelta(days=1)).replace(tzinfo=TZ)
    return candidate


def _parse_signed_mmss(text: Any) -> int | None:
    """ "+1:31" -> 91, "-0:40" -> -40, "24:47" -> 1487, "-" -> None."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"\s*([+-]?)(\d+):(\d{2})\s*", text)
    if not match:
        return None
    seconds = int(match[2]) * 60 + int(match[3])
    return -seconds if match[1] == "-" else seconds


def _parse_distance(text: Any) -> int | None:
    """ "9.4км." -> 9400, "845м." -> 845, "~16км." -> 16000."""
    if not isinstance(text, str):
        return None
    match = re.match(r"\s*~?\s*([\d.,]+)\s*(км|м)", text)
    if not match:
        return None
    try:
        value = float(match[1].replace(",", "."))
    except ValueError:
        return None
    return round(value * 1000) if match[2] == "км" else round(value)


def _fleet_number(device: Any) -> str | None:
    """ "8577_2" (varnatraffic) or "3/8577" (livetransport) -> "8577"."""
    if not device:
        return None
    device = str(device)
    if "/" in device:
        device = device.rsplit("/", 1)[1]
    return device.split("_", 1)[0] or None


class VarnaTrafficClient(_BaseClient):
    """Client for the AJAX endpoints behind varnatraffic.com."""

    source = SOURCE_VARNATRAFFIC

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._line_names: dict[int, str] = {}
        self._line_names_next = 0.0  # monotonic time of the next refresh
        self._line_names_lock = asyncio.Lock()

    async def _ajax(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._get(
            f"{VT_BASE}/Ajax/{path}",
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{VT_BASE}/"},
        )

    @staticmethod
    def _stops(data: Any, what: str) -> list[Stop]:
        if not isinstance(data, list):
            raise TransportDataError(f"{what} did not return a list")
        stops = []
        for raw in data:
            try:
                pos = raw.get("position") or {}
                stops.append(
                    Stop(
                        id=str(raw["id"]),
                        name=str(raw.get("text") or raw["id"]).strip(),
                        latitude=pos.get("lat"),
                        longitude=pos.get("lon"),
                    )
                )
            except _PARSE_ERRORS:
                continue
        return stops

    async def search_stops(self, query: str) -> list[Stop]:
        """Stops whose name matches the query."""
        return self._stops(await self._ajax("FindStation", {"query": query}), "FindStation")

    async def nearest_stops(
        self, latitude: float, longitude: float, radius: int = 500
    ) -> list[Stop]:
        """Stops within radius metres of a point, nearest first."""
        data = await self._ajax(
            "FindNearestStations", {"lat": latitude, "lon": longitude, "range": radius}
        )
        return self._stops(data, "FindNearestStations")

    async def line_names(self) -> dict[int, str]:
        """Internal line id -> public name, scraped from the home page.

        The API speaks in internal ids: 114 is "14а", 1018 is "18а". The list
        is refreshed once a day. If a refresh fails, the last good list is
        kept and the next try waits 10 minutes, so an outage of the home page
        never doubles the requests of every poll.
        """
        async with self._line_names_lock:
            if time.monotonic() < self._line_names_next:
                return self._line_names
            try:
                page = await self._get(
                    f"{VT_BASE}/bg", as_text=True, headers={"Accept": "text/html"}
                )
                names = parse_line_names(page)
            except TransportError as err:
                _LOGGER.debug("Line list not refreshed: %s", err)
                self._line_names_next = time.monotonic() + _LINE_NAMES_RETRY
                return self._line_names
            self._line_names = names
            self._line_names_next = time.monotonic() + _LINE_NAMES_TTL
            return names

    async def departures(
        self, stop_id: str, extra_names: dict[int, str] | None = None
    ) -> StopBoard:
        """Live and scheduled departures for one stop."""
        names = {**await self.line_names(), **(extra_names or {})}
        data = await self._ajax("FindStationDevices", {"stationId": stop_id})
        return parse_varnatraffic(data, names, datetime.now(UTC))


def parse_line_names(page: str) -> dict[int, str]:
    """Line links on the varnatraffic home page -> {internal id: name}."""
    names: dict[int, str] = {}
    for match in _VT_LINE_LINK.finditer(page or ""):
        label = html.unescape(re.sub(r"<[^>]+>", "", match[2])).strip()
        if label:
            names[int(match[1])] = normalize_line(label)
    if not names:
        raise TransportDataError("No line list on the varnatraffic home page")
    return names


def parse_varnatraffic(data: Any, names: dict[int, str], now: datetime) -> StopBoard:
    """Parse a FindStationDevices answer."""
    if not isinstance(data, dict) or not isinstance(data.get("liveData"), list):
        raise TransportDataError("FindStationDevices answer has no liveData list")
    try:
        departures = _varnatraffic_departures(data, names, now)
    except _PARSE_ERRORS as err:
        raise TransportDataError(f"Unexpected varnatraffic answer: {err!r}") from err
    return StopBoard(source=SOURCE_VARNATRAFFIC, departures=_finish(departures, now), fetched=now)


def _varnatraffic_departures(
    data: dict[str, Any], names: dict[int, str], now: datetime
) -> list[Departure]:
    def line_name(line_id: Any) -> str:
        try:
            return names.get(int(line_id), normalize_line(line_id))
        except (TypeError, ValueError):
            return normalize_line(line_id)

    schedule = data.get("schedule") or []
    if not isinstance(schedule, list):
        raise TransportDataError("schedule is not a list")

    # The schedule tells which lines a vehicle is actually running. That
    # matters for combined runs: one bus serving 46 and 17 shows as line 700
    # and is listed once per line, so a filter on either line finds it.
    device_lines: dict[str, list[Any]] = {}
    for sched in schedule:
        for slot in sched.get("data") or []:
            if slot.get("device"):
                device_lines.setdefault(slot["device"], []).append(sched.get("line"))

    departures: list[Departure] = []
    covered: set[str] = set()

    for item in data["liveData"]:
        device = item.get("device")
        line_ids = device_lines.get(device) or item.get("currentLines") or [item.get("line")]
        scheduled = _parse_clock(item.get("arriveTime"), now)
        arrive_in = _parse_signed_mmss(item.get("arriveIn"))
        silence = item.get("lastComm")
        fresh = not isinstance(silence, (int, float)) or silence <= _VT_MAX_SILENCE
        live = arrive_in is not None and fresh
        if arrive_in is not None:
            expected = now + timedelta(seconds=max(arrive_in, 0))
        elif scheduled is not None:
            expected = scheduled
        else:
            continue
        for line_id in dict.fromkeys(line_ids):
            name = line_name(line_id)
            if not name:
                continue
            departures.append(
                Departure(
                    line=name,
                    expected=expected,
                    scheduled=scheduled,
                    delay=_parse_signed_mmss(item.get("delay")),
                    live=live,
                    vehicle=_fleet_number(device),
                    distance=_parse_distance(item.get("distanceLeft")),
                    source=SOURCE_VARNATRAFFIC,
                )
            )
        if device:
            covered.add(device)

    for sched in schedule:
        name = line_name(sched.get("line"))
        if not name:
            continue
        for slot in sched.get("data") or []:
            if slot.get("device") in covered:
                continue  # already listed with live data
            when = _parse_clock(slot.get("text"), now)
            if when is None:
                continue
            departures.append(
                Departure(
                    line=name,
                    expected=when,
                    scheduled=when,
                    vehicle=_fleet_number(slot.get("device")),
                    source=SOURCE_VARNATRAFFIC,
                )
            )
    return departures


# --------------------------------------------------------------------------
# livetransport.eu
# --------------------------------------------------------------------------


def _field(value: Any) -> str | None:
    """livetransport text is either a string or {"bg": ..., "en": ...}."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("bg") or value.get("en") or next(iter(value.values()), None)
    return str(value)


class LiveTransportClient(_BaseClient):
    """Client for the REST side of next.livetransport.eu."""

    source = SOURCE_LIVETRANSPORT

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._stops: list[Stop] | None = None
        self._stops_at = 0.0

    async def all_stops(self) -> list[Stop]:
        """Varna stops (the city feed also carries BDZ rail stops)."""
        if self._stops is None or time.monotonic() - self._stops_at > _STOPS_TTL:
            data = await self._get(f"{LT_BASE}/{LT_CITY}/data")
            self._stops = parse_livetransport_stops(data)
            self._stops_at = time.monotonic()
        return self._stops

    async def match_stop(self, varnatraffic_stop: Stop) -> tuple[Stop | None, list[Stop]]:
        """Find the livetransport twin of a varnatraffic stop.

        Returns (exact match by stop code or None, nearby candidates).
        """
        stops = await self.all_stops()
        exact = None
        if varnatraffic_stop.id.isdigit():
            code = str(LT_STOP_CODE_OFFSET + int(varnatraffic_stop.id))
            exact = next((s for s in stops if s.code == code), None)
        lat, lon = varnatraffic_stop.latitude, varnatraffic_stop.longitude
        nearby = sorted(
            (s for s in stops if s.distance_to(lat, lon) <= 400),
            key=lambda s: s.distance_to(lat, lon),
        )[:8]
        if exact and exact not in nearby:
            nearby.insert(0, exact)
        return exact, nearby

    async def departures(self, stop_id: str, limit: int = 30) -> StopBoard:
        """Upcoming departures for one stop."""
        data = await self._get(
            f"{LT_BASE}/{LT_CITY}/virtual-board/{stop_id}", params={"limit": limit}
        )
        return parse_livetransport(data, datetime.now(UTC))


def parse_livetransport_stops(data: Any) -> list[Stop]:
    """Stops of the Varna network from the city data feed."""
    if not isinstance(data, dict) or not isinstance(data.get("stops"), list):
        raise TransportDataError("livetransport data has no stops")
    stops = []
    for raw in data["stops"]:
        try:
            if raw[_LT_STOP["city"]] != LT_CITY:
                continue
            lat, lon = (raw[_LT_STOP["location"]] or [None, None])[:2]
            code = raw[_LT_STOP["code"]]
            stops.append(
                Stop(
                    id=str(raw[_LT_STOP["id"]]),
                    name=_field(raw[_LT_STOP["name"]]) or str(raw[_LT_STOP["id"]]),
                    latitude=lat,
                    longitude=lon,
                    code=str(code) if code else None,
                )
            )
        except _PARSE_ERRORS:
            continue
    if not stops:
        raise TransportDataError("livetransport data has no Varna stops")
    return stops


def _lt_departure(dep: Any) -> Departure | None:
    """One virtual-board row, or None for a cancelled trip."""
    trip = dep[_LT_DEP["trip"]]
    times = dep[_LT_DEP["time"]]
    status = times[_LT_TIME["status"]]
    if status == _LT_STATUS_CANCELLED:
        return None
    route = trip[_LT_TRIP["route"]]
    if isinstance(route, list):
        short = route[_LT_ROUTE["short_name"]] or route[_LT_ROUTE["id"]]
    else:
        short = route
    # Route ids carry the GTFS type: "14a-3", "82-11".
    line = normalize_line(re.sub(r"-\d+$", "", str(short)))
    if not line:
        raise ValueError("no line name")
    scheduled_ms = times[_LT_TIME["scheduled"]]
    if not isinstance(scheduled_ms, (int, float)) or scheduled_ms < 1e12:
        raise ValueError(f"scheduled is not epoch milliseconds: {scheduled_ms!r}")
    scheduled = datetime.fromtimestamp(scheduled_ms / 1000, UTC)
    delay_ms = times[_LT_TIME["delay"]] if len(times) > _LT_TIME["delay"] else None
    vehicle = dep[_LT_DEP["vehicle"]] if len(dep) > _LT_DEP["vehicle"] else None
    tracked = bool(vehicle) and status in (_LT_STATUS_ON_TRIP, _LT_STATUS_ON_PREVIOUS_TRIP)
    delay = round(delay_ms / 1000) if tracked and isinstance(delay_ms, (int, float)) else None
    return Departure(
        line=line,
        expected=scheduled + timedelta(seconds=delay or 0),
        scheduled=scheduled,
        delay=delay,
        live=bool(vehicle) and status == _LT_STATUS_ON_TRIP,
        destination=_field(trip[_LT_TRIP["headsign"]]),
        vehicle=_fleet_number(vehicle),
        source=SOURCE_LIVETRANSPORT,
    )


def parse_livetransport(data: Any, now: datetime) -> StopBoard:
    """Parse a virtual-board answer.

    Rows that do not parse are skipped and logged. If none parses, the answer
    is treated as an error, so a format change triggers a failover instead of
    an empty board that looks like "no buses".
    """
    if not isinstance(data, dict) or not isinstance(data.get("departures"), list):
        raise TransportDataError("virtual-board answer has no departures list")
    departures: list[Departure] = []
    skipped = 0
    for dep in data["departures"]:
        try:
            parsed = _lt_departure(dep)
        except _PARSE_ERRORS as err:
            if not skipped:
                _LOGGER.debug("Skipped livetransport row %r: %r", dep, err)
            skipped += 1
            continue
        if parsed is not None:
            departures.append(parsed)
    if skipped and not departures:
        raise TransportDataError(f"None of {skipped} livetransport rows could be parsed")
    return StopBoard(source=SOURCE_LIVETRANSPORT, departures=_finish(departures, now), fetched=now)


def _finish(departures: list[Departure], now: datetime) -> list[Departure]:
    """Drop buses that have left and sort by expected time.

    A live bus may be a minute late on the countdown. A timetable-only row
    whose time has passed is dropped: its bus is late by an unknown amount,
    and showing it as "0 minutes" would be wrong.
    """
    live_cutoff = now - timedelta(minutes=1)
    return sorted(
        (d for d in departures if d.expected >= (live_cutoff if d.live else now)),
        key=lambda d: (d.expected, d.line),
    )
