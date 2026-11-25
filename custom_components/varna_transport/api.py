"""Clients for the two Varna public transport data sources.

Nothing in this module imports Home Assistant, so it can be tested on its own.

* varnatraffic.com - run by MapSoft on the AVL data of Gradski Transport.
* livetransport.eu - an independent multi-city tracker that gets Varna from
  the TASRUD system. Used as a fallback when varnatraffic is down.

Both sources are turned into the same :class:`Departure` model.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import html
import math
import re
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

TZ = ZoneInfo("Europe/Sofia")

VT_BASE = "https://varnatraffic.com"
LT_BASE = "https://next.livetransport.eu"
LT_CITY = "varna"

# livetransport sends positional arrays; indices from its public front end.
_LT_STOP = {"id": 0, "city": 1, "name": 3, "code": 4, "location": 5}
_LT_TRIP = {"route": 2, "headsign": 3}
_LT_ROUTE = {"id": 0, "short_name": 5}
_LT_DEP = {"trip": 0, "time": 1, "vehicle": 2}
_LT_TIME = {"scheduled": 0, "delay": 1, "status": 2}
_LT_STATUS_ON_TRIP = 1
_LT_STATUS_ON_PREVIOUS_TRIP = 2
_LT_STATUS_CANCELLED = 3

_LATIN_TO_CYRILLIC = str.maketrans({"a": "а", "b": "б", "v": "в", "e": "е"})


class TransportError(Exception):
    """Base error for a source that could not deliver data."""


class TransportConnectionError(TransportError):
    """Network problem, timeout or HTTP error."""


class TransportDataError(TransportError):
    """The source answered with something we cannot parse."""


def normalize_line(name: str | int) -> str:
    """Return a canonical line name: lower case, Cyrillic suffix letters.

    varnatraffic writes "14a" (Latin), livetransport "14A"; both become "14а".
    """
    return str(name).strip().lower().translate(_LATIN_TO_CYRILLIC)


@dataclass(slots=True, frozen=True)
class Stop:
    """A stop of one source."""

    id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    code: str | None = None

    def distance_to(self, latitude: float, longitude: float) -> float:
        """Distance in metres (equirectangular, fine at city scale)."""
        if self.latitude is None or self.longitude is None:
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
    live: bool = False
    destination: str | None = None
    vehicle: str | None = None
    distance: int | None = None  # metres to the stop

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


# --------------------------------------------------------------------------
# varnatraffic.com
# --------------------------------------------------------------------------

_VT_LINE_LINK = re.compile(r'href="/bg/Line/Routes/(\d+)"[^>]*>(.*?)</a>', re.S)


def _parse_clock(text: str, now: datetime) -> datetime | None:
    """Turn "HH:MM" (Varna local time) into the nearest matching datetime."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not match:
        return None
    local_now = now.astimezone(TZ)
    candidate = local_now.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
    # Schedules run past midnight: 00:10 seen at 23:50 is tomorrow,
    # 23:50 seen at 00:10 is yesterday.
    if candidate - local_now > timedelta(hours=12):
        candidate -= timedelta(days=1)
    elif local_now - candidate > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _parse_signed_mmss(text: str | None) -> int | None:
    """ "+1:31" -> 91, "-0:40" -> -40, "24:47" -> 1487, "-" -> None."""
    match = re.fullmatch(r"\s*([+-]?)(\d+):(\d{2})\s*", text or "")
    if not match:
        return None
    seconds = int(match[2]) * 60 + int(match[3])
    return -seconds if match[1] == "-" else seconds


def _parse_distance(text: str | None) -> int | None:
    """ "9.4км." -> 9400, "845м." -> 845, "~16км." -> 16000."""
    match = re.match(r"\s*~?\s*([\d.,]+)\s*(км|м)", text or "")
    if not match:
        return None
    value = float(match[1].replace(",", "."))
    return round(value * 1000) if match[2] == "км" else round(value)


def _fleet_number(device: str | None) -> str | None:
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
        self._line_names: dict[int, str] | None = None

    async def _ajax(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._get(
            f"{VT_BASE}/Ajax/{path}",
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{VT_BASE}/"},
        )

    @staticmethod
    def _stop(raw: dict[str, Any]) -> Stop:
        pos = raw.get("position") or {}
        return Stop(
            id=str(raw["id"]),
            name=str(raw.get("text", raw["id"])).strip(),
            latitude=pos.get("lat"),
            longitude=pos.get("lon"),
        )

    async def search_stops(self, query: str) -> list[Stop]:
        """Stops whose name matches the query."""
        data = await self._ajax("FindStation", {"query": query})
        if not isinstance(data, list):
            raise TransportDataError("FindStation did not return a list")
        return [self._stop(item) for item in data if "id" in item]

    async def nearest_stops(
        self, latitude: float, longitude: float, radius: int = 500
    ) -> list[Stop]:
        """Stops within radius metres of a point, nearest first."""
        data = await self._ajax(
            "FindNearestStations",
            {"lat": latitude, "lon": longitude, "range": radius},
        )
        if not isinstance(data, list):
            raise TransportDataError("FindNearestStations did not return a list")
        return [self._stop(item) for item in data if "id" in item]

    async def all_stops(self) -> list[Stop]:
        """Every stop in the network."""
        data = await self._ajax("GetStations")
        if not isinstance(data, list):
            raise TransportDataError("GetStations did not return a list")
        return [self._stop(item) for item in data if "id" in item]

    async def line_names(self) -> dict[int, str]:
        """Internal line id -> public name, scraped once from the home page.

        The API speaks in internal ids: 114 is "14а", 1018 is "18а".
        """
        if self._line_names is None:
            page = await self._get(f"{VT_BASE}/bg", as_text=True, headers={"Accept": "text/html"})
            names: dict[int, str] = {}
            for match in _VT_LINE_LINK.finditer(page):
                label = html.unescape(re.sub(r"<[^>]+>", "", match[2])).strip()
                if label:
                    names[int(match[1])] = normalize_line(label)
            if not names:
                raise TransportDataError("No line list on the varnatraffic home page")
            self._line_names = names
        return self._line_names

    async def departures(
        self, stop_id: str, extra_names: dict[int, str] | None = None
    ) -> StopBoard:
        """Live and scheduled departures for one stop."""
        try:
            names = await self.line_names()
        except TransportError:
            names = {}  # fall back to raw ids; the board itself matters more
        if extra_names:
            names = {**names, **extra_names}
        data = await self._ajax("FindStationDevices", {"stationId": stop_id})
        return parse_varnatraffic(data, names, datetime.now(UTC))


def parse_varnatraffic(data: Any, names: dict[int, str], now: datetime) -> StopBoard:
    """Parse a FindStationDevices answer."""
    if not isinstance(data, dict) or "liveData" not in data:
        raise TransportDataError("FindStationDevices answer has no liveData")

    def line_name(line_id: Any) -> str:
        try:
            return names.get(int(line_id), normalize_line(line_id))
        except (TypeError, ValueError):
            return normalize_line(line_id)

    # The schedule tells which lines a vehicle is actually running. That
    # matters for combined runs: one bus serving 46 and 17 shows as line 700
    # and is listed once per line, so a filter on either line finds it.
    device_lines: dict[str, list[Any]] = {}
    for sched in data.get("schedule") or []:
        for slot in sched.get("data") or []:
            if slot.get("device"):
                device_lines.setdefault(slot["device"], []).append(sched.get("line"))

    board = StopBoard(source=SOURCE_VARNATRAFFIC, fetched=now)
    covered: set[str] = set()

    for item in data.get("liveData") or []:
        device = item.get("device")
        line_ids = device_lines.get(device) or item.get("currentLines") or [item.get("line")]
        scheduled = _parse_clock(item.get("arriveTime", ""), now)
        arrive_in = _parse_signed_mmss(item.get("arriveIn"))
        live = arrive_in is not None
        if live:
            expected = now + timedelta(seconds=max(arrive_in, 0))
        elif scheduled is not None:
            expected = scheduled
        else:
            continue
        for line_id in dict.fromkeys(line_ids):
            board.departures.append(
                Departure(
                    line=line_name(line_id),
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

    for sched in data.get("schedule") or []:
        name = line_name(sched.get("line"))
        for slot in sched.get("data") or []:
            if slot.get("device") in covered:
                continue  # already listed with live data
            when = _parse_clock(slot.get("text", ""), now)
            if when is None:
                continue
            board.departures.append(
                Departure(
                    line=name,
                    expected=when,
                    scheduled=when,
                    vehicle=_fleet_number(slot.get("device")),
                    source=SOURCE_VARNATRAFFIC,
                )
            )

    board.departures = _finish(board.departures, now)
    return board


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

    async def all_stops(self) -> list[Stop]:
        """Varna stops (the city feed also carries BDZ rail stops)."""
        if self._stops is None:
            data = await self._get(f"{LT_BASE}/{LT_CITY}/data")
            if not isinstance(data, dict) or not isinstance(data.get("stops"), list):
                raise TransportDataError("livetransport data has no stops")
            stops = []
            for raw in data["stops"]:
                try:
                    if raw[_LT_STOP["city"]] != LT_CITY:
                        continue
                    lat, lon = (raw[_LT_STOP["location"]] or [None, None])[:2]
                    stops.append(
                        Stop(
                            id=str(raw[_LT_STOP["id"]]),
                            name=_field(raw[_LT_STOP["name"]]) or str(raw[_LT_STOP["id"]]),
                            latitude=lat,
                            longitude=lon,
                            code=str(raw[_LT_STOP["code"]]) if raw[_LT_STOP["code"]] else None,
                        )
                    )
                except (IndexError, TypeError, ValueError):
                    continue
            self._stops = stops
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
        nearby: list[Stop] = []
        if varnatraffic_stop.latitude is not None and varnatraffic_stop.longitude is not None:
            lat, lon = varnatraffic_stop.latitude, varnatraffic_stop.longitude
            nearby = sorted(
                (s for s in stops if s.distance_to(lat, lon) <= 400),
                key=lambda s: s.distance_to(lat, lon),
            )[:8]
        if exact and exact not in nearby:
            nearby.insert(0, exact)
        return exact, nearby

    async def departures(self, stop_id: str, limit: int = 20) -> StopBoard:
        """Upcoming departures for one stop."""
        data = await self._get(
            f"{LT_BASE}/{LT_CITY}/virtual-board/{stop_id}", params={"limit": limit}
        )
        return parse_livetransport(data, datetime.now(UTC))


def parse_livetransport(data: Any, now: datetime) -> StopBoard:
    """Parse a virtual-board answer."""
    if not isinstance(data, dict) or not isinstance(data.get("departures"), list):
        raise TransportDataError("virtual-board answer has no departures")
    board = StopBoard(source=SOURCE_LIVETRANSPORT, fetched=now)
    for dep in data["departures"]:
        try:
            trip = dep[_LT_DEP["trip"]]
            times = dep[_LT_DEP["time"]]
            status = times[_LT_TIME["status"]]
            if status == _LT_STATUS_CANCELLED:
                continue
            route = trip[_LT_TRIP["route"]]
            short = route[_LT_ROUTE["short_name"]] if isinstance(route, list) else route
            if not short and isinstance(route, list):
                short = str(route[_LT_ROUTE["id"]]).split("-", 1)[0]
            scheduled = datetime.fromtimestamp(times[_LT_TIME["scheduled"]] / 1000, UTC)
            delay_ms = times[_LT_TIME["delay"]] if len(times) > 1 else None
            vehicle = dep[_LT_DEP["vehicle"]] if len(dep) > 2 else None
            live = bool(vehicle) and status in (_LT_STATUS_ON_TRIP, _LT_STATUS_ON_PREVIOUS_TRIP)
            delay = round(delay_ms / 1000) if isinstance(delay_ms, (int, float)) else None
            expected = scheduled + timedelta(seconds=delay or 0) if live else scheduled
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
        board.departures.append(
            Departure(
                line=normalize_line(short),
                expected=expected,
                scheduled=scheduled,
                delay=delay if live else None,
                live=live,
                destination=_field(trip[_LT_TRIP["headsign"]]),
                vehicle=_fleet_number(vehicle),
                source=SOURCE_LIVETRANSPORT,
            )
        )
    board.departures = _finish(board.departures, now)
    return board


def _finish(departures: list[Departure], now: datetime) -> list[Departure]:
    """Drop buses that have left and sort by expected time."""
    cutoff = now - timedelta(minutes=1)
    return sorted(
        (d for d in departures if d.expected >= cutoff), key=lambda d: (d.expected, d.line)
    )
