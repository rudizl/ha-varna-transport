"""Parser tests on real answers of both sources."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from custom_components.varna_transport.api import (
    TZ,
    Stop,
    TransportDataError,
    _parse_clock,
    _parse_distance,
    _parse_signed_mmss,
    enrich,
    normalize_line,
    parse_line_names,
    parse_livetransport,
    parse_varnatraffic,
)

FIXTURES = Path(__file__).parent / "fixtures"
NAMES = {114: "14а", 117: "17а", 1018: "18а"}


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("14a", "14а"), ("14A", "14а"), ("209б", "209б"), (" 9 ", "9"), (109, "109"), (None, "")],
)
def test_normalize_line(raw, expected):
    assert normalize_line(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+1:31", 91), ("-0:40", -40), ("24:47", 1487), ("-", None), (None, None), (150, None)],
)
def test_signed_mmss(raw, expected):
    assert _parse_signed_mmss(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("9.4км.", 9400), ("845м.", 845), ("~16км.", 16000), ("-", None), (3, None)],
)
def test_distance(raw, expected):
    assert _parse_distance(raw) == expected


def test_clock_window():
    late = datetime(2026, 11, 25, 23, 50, tzinfo=TZ)
    assert _parse_clock("00:10", late) == datetime(2026, 11, 26, 0, 10, tzinfo=TZ)
    early = datetime(2026, 11, 26, 0, 10, tzinfo=TZ)
    assert _parse_clock("23:50", early) == datetime(2026, 11, 25, 23, 50, tzinfo=TZ)
    # Up to 21 hours ahead: the evening timetable is visible in the morning.
    morning = datetime(2026, 11, 25, 6, 0, tzinfo=TZ)
    assert _parse_clock("22:45", morning) == datetime(2026, 11, 25, 22, 45, tzinfo=TZ)
    assert _parse_clock("garbage", late) is None
    assert _parse_clock("24:10", late) is None
    assert _parse_clock(None, late) is None


def test_varnatraffic_station_102():
    now = datetime(2026, 11, 25, 13, 32, 0, tzinfo=UTC)
    board = parse_varnatraffic(load("varnatraffic_station_102.json"), NAMES, now)
    deps = board.departures

    assert deps == sorted(deps, key=lambda d: (d.expected, d.line))
    assert all(d.expected >= now for d in deps if not d.live)

    # Bus 8606 runs as 17 and 46 at once (line 700); listed for both.
    combined = [d for d in deps if d.vehicle == "8606"]
    assert sorted(d.line for d in combined) == ["17", "46"]
    assert all(d.live and d.delay == 91 and d.distance == 9400 for d in combined)
    # extrasFlags 3 = air conditioning + accessible for wheelchairs.
    assert combined[0].features == ["air_conditioning", "wheelchair"]
    assert combined[0].expected == now + timedelta(minutes=24, seconds=47)

    # Waiting at the terminus: assigned bus, no live countdown.
    first_12 = next(d for d in deps if d.line == "12")
    assert first_12.vehicle == "8566" and not first_12.live
    assert first_12.expected == datetime(2026, 11, 25, 15, 40, tzinfo=TZ)

    # Schedule slots of buses already listed are not repeated.
    assert sum(1 for d in deps if d.vehicle == "8566") == 1
    assert any(d.line == "17а" for d in deps)

    # One format for every timestamp: Varna time, whole seconds.
    assert all(d.expected.tzinfo == TZ and d.expected.microsecond == 0 for d in deps)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (0, None),
        (None, None),
        (True, None),
        (1, ["air_conditioning"]),
        (15, ["air_conditioning", "wheelchair", "blind_assistance", "toilet"]),
        (16, None),
    ],
)
def test_features(flags, expected):
    from custom_components.varna_transport.api import _features

    assert _features(flags) == expected


def test_varnatraffic_station_271():
    now = datetime(2026, 11, 25, 14, 0, 30, tzinfo=UTC)
    deps = parse_varnatraffic(load("varnatraffic_station_271.json"), {}, now).departures
    first = deps[0]
    assert (first.line, first.vehicle, first.live, first.delay, first.distance) == (
        "109",
        "8563",
        True,
        161,
        3100,
    )
    nine = next(d for d in deps if d.vehicle == "8570")
    assert nine.delay == -135 and nine.distance == 16000


def test_varnatraffic_timetable_row_in_the_past_is_dropped():
    """A bus without countdown whose time has passed is not "0 minutes away"."""
    now = datetime(2026, 11, 25, 13, 45, 0, tzinfo=UTC)  # 15:45 local
    deps = parse_varnatraffic(load("varnatraffic_station_102.json"), NAMES, now).departures
    assert not any(d.vehicle == "8566" for d in deps)  # line 12 at 15:40, no countdown


def test_varnatraffic_silent_vehicle_is_not_live():
    data = load("varnatraffic_station_102.json")
    for item in data["liveData"]:
        item["lastComm"] = 900
    now = datetime(2026, 11, 25, 13, 32, 0, tzinfo=UTC)
    deps = parse_varnatraffic(data, NAMES, now).departures
    assert not any(d.live for d in deps)


@pytest.mark.parametrize(
    "data",
    [
        {"error": "x"},
        {"liveData": {"a": 1}},
        {"liveData": [{"device": "1_2", "arriveTime": "10:00"}], "schedule": "oops"},
        {"liveData": [], "schedule": [{"line": 9, "data": [None]}]},
    ],
)
def test_varnatraffic_bad_answers_raise_data_error(data):
    with pytest.raises(TransportDataError):
        parse_varnatraffic(data, {}, datetime.now(UTC))


def test_livetransport_real_board():
    now = datetime.fromtimestamp(1795619700, UTC)  # 17:15 local
    deps = parse_livetransport(load("livetransport_board_1124.json"), now).departures

    # Status 1: on this trip, GPS countdown. Status 2: still on its previous trip.
    live = [d for d in deps if d.live]
    assert [d.line for d in live] == ["31а", "409", "9", "109"]
    first = deps[0]
    assert first.delay == 248 and first.vehicle == "8661"
    assert first.expected == first.scheduled + timedelta(seconds=248)
    assert first.destination == "Аспарухово"

    waiting = [d for d in deps if not d.live]
    assert len(waiting) == 6
    assert waiting[0].delay == 86  # carried over from the previous trip
    assert all(d.vehicle for d in waiting)


def test_livetransport_edge_cases():
    now = datetime.fromtimestamp(1795614000, UTC)
    deps = parse_livetransport(load("livetransport_board.json"), now).departures
    assert [d.line for d in deps] == ["109", "9", "14а", "9"]  # cancelled trip dropped
    assert deps[1].destination == "ЖП Гара"  # plain-string headsign
    assert deps[1].live
    assert not deps[2].live and deps[2].delay == 0  # on previous trip
    assert not deps[3].live and deps[3].delay is None  # no vehicle


def test_livetransport_route_as_string():
    now = datetime.fromtimestamp(1795614000, UTC)
    data = load("livetransport_board.json")
    data["departures"][0][0][2] = "9-3"
    assert parse_livetransport(data, now).departures[0].line == "9"


def test_livetransport_skips_bad_rows_but_not_all():
    now = datetime.fromtimestamp(1795614000, UTC)
    data = load("livetransport_board.json")
    data["departures"][0][0] = ["short", "trip"]  # missing route
    assert len(parse_livetransport(data, now).departures) == 3

    # Seconds instead of milliseconds in every row: a format change, not "no buses".
    for dep in data["departures"]:
        dep[1][0] = dep[1][0] // 1000
    with pytest.raises(TransportDataError):
        parse_livetransport(data, now)


def test_livetransport_bad_answer():
    with pytest.raises(TransportDataError):
        parse_livetransport("<html>520</html>", datetime.now(UTC))


def test_line_names_from_home_page():
    names = parse_line_names((FIXTURES / "varnatraffic_home.html").read_text(encoding="utf-8"))
    assert names[114] == "14а" and names[1018] == "18а" and names[1118] == "118а"
    assert names[9] == "9"
    with pytest.raises(TransportDataError):
        parse_line_names("<html>maintenance</html>")


def test_stop_distance():
    a = Stop("771", "А", 43.26412, 28.0185833)
    assert a.distance_to(43.2642, 28.01865) < 15
    assert Stop("x", "no position").distance_to(43.0, 28.0) == float("inf")
    assert a.distance_to(None, None) == float("inf")


def test_version_matches_manifest():
    from custom_components.varna_transport.const import VERSION

    manifest = json.loads(
        (Path(__file__).parents[1] / "custom_components/varna_transport/manifest.json").read_text()
    )
    assert manifest["version"] == VERSION


def test_enrich_pairs_by_line_and_time():
    from custom_components.varna_transport.api import Departure, StopBoard

    t = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)
    vt = StopBoard(
        source="varnatraffic",
        departures=[
            Departure(line="109", expected=t, scheduled=t, source="varnatraffic"),
            Departure(
                line="109",
                expected=t + timedelta(minutes=45),
                scheduled=t + timedelta(minutes=45),
                source="varnatraffic",
            ),
            Departure(
                line="9",
                expected=t + timedelta(minutes=10),
                scheduled=t + timedelta(minutes=10),
                source="varnatraffic",
            ),
        ],
    )
    lt = StopBoard(
        source="livetransport",
        departures=[
            # Same trip, livetransport's timetable 2 minutes later.
            Departure(
                line="109",
                expected=t,
                scheduled=t + timedelta(minutes=2),
                delay=300,
                vehicle="0229",
                destination="ЖП Гара",
                source="livetransport",
            ),
            # Same line but 20 minutes away: a different trip, not paired.
            Departure(
                line="9",
                expected=t,
                scheduled=t + timedelta(minutes=30),
                delay=0,
                vehicle="9611",
                source="livetransport",
            ),
        ],
    )
    out = enrich(vt, lt).departures
    first = next(d for d in out if d.vehicle == "0229")
    assert first.delay == 300 and not first.live
    assert first.expected == first.scheduled + timedelta(seconds=300)
    assert first.destination == "ЖП Гара"
    assert next(d for d in out if d.line == "9").vehicle is None
    # Each livetransport row is used once: the later 109 stays as it was.
    assert sum(1 for d in out if d.vehicle == "0229") == 1
    assert out == sorted(out, key=lambda d: (d.expected, d.line))


def test_same_vehicle_ignores_padding():
    from custom_components.varna_transport.api import _same_vehicle

    assert _same_vehicle("40229", "0229")
    assert _same_vehicle("8570", "8570")
    assert not _same_vehicle("8570", "8571")
    assert not _same_vehicle(None, "8570")
