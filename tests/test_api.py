"""Parser tests on real (varnatraffic) and shaped (livetransport) answers."""

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
    normalize_line,
    parse_livetransport,
    parse_varnatraffic,
)

FIXTURES = Path(__file__).parent / "fixtures"
NAMES = {114: "14а", 117: "17а", 1018: "18а"}


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("14a", "14а"), ("14A", "14а"), ("209б", "209б"), (" 9 ", "9"), (109, "109")],
)
def test_normalize_line(raw, expected):
    assert normalize_line(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+1:31", 91), ("-0:40", -40), ("24:47", 1487), ("-", None), (None, None)],
)
def test_signed_mmss(raw, expected):
    assert _parse_signed_mmss(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("9.4км.", 9400), ("845м.", 845), ("~16км.", 16000), ("-", None)],
)
def test_distance(raw, expected):
    assert _parse_distance(raw) == expected


def test_clock_crosses_midnight():
    late = datetime(2026, 11, 25, 23, 50, tzinfo=TZ)
    assert _parse_clock("00:10", late) == datetime(2026, 11, 26, 0, 10, tzinfo=TZ)
    early = datetime(2026, 11, 26, 0, 10, tzinfo=TZ)
    assert _parse_clock("23:50", early) == datetime(2026, 11, 25, 23, 50, tzinfo=TZ)
    assert _parse_clock("garbage", late) is None


def test_varnatraffic_station_102():
    now = datetime(2026, 11, 25, 13, 32, 0, tzinfo=UTC)
    board = parse_varnatraffic(load("varnatraffic_station_102.json"), NAMES, now)
    deps = board.departures

    assert deps == sorted(deps, key=lambda d: (d.expected, d.line))
    assert all(d.expected >= now - timedelta(minutes=1) for d in deps)

    # Bus 8606 runs as 17 and 46 at once (line 700); listed for both.
    combined = [d for d in deps if d.vehicle == "8606"]
    assert sorted(d.line for d in combined) == ["17", "46"]
    assert all(d.live and d.delay == 91 and d.distance == 9400 for d in combined)
    assert combined[0].expected == now + timedelta(minutes=24, seconds=47)

    # Waiting at the terminus: assigned bus, no live countdown.
    first_12 = next(d for d in deps if d.line == "12")
    assert first_12.vehicle == "8566" and not first_12.live
    assert first_12.expected == datetime(2026, 11, 25, 15, 40, tzinfo=TZ)

    # Schedule slots of buses already listed are not repeated.
    assert sum(1 for d in deps if d.vehicle == "8566") == 1
    assert any(d.line == "17а" for d in deps)


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


def test_varnatraffic_bad_answer():
    with pytest.raises(TransportDataError):
        parse_varnatraffic({"error": "x"}, {}, datetime.now(UTC))


def test_livetransport_board():
    now = datetime.fromtimestamp(1795614000, UTC)
    deps = parse_livetransport(load("livetransport_board.json"), now).departures

    assert [d.line for d in deps] == ["109", "9", "14а", "9"]  # cancelled trip dropped
    first = deps[0]
    assert first.live and first.delay == 94 and first.vehicle == "8577"
    assert first.destination == "ЖП Гара"
    assert first.expected == first.scheduled + timedelta(seconds=94)
    assert deps[1].destination == "ЖП Гара"  # plain-string headsign
    assert deps[2].live  # on its previous trip, still tracked
    assert not deps[3].live and deps[3].delay is None


def test_livetransport_bad_answer():
    with pytest.raises(TransportDataError):
        parse_livetransport("<html>520</html>", datetime.now(UTC))


def test_stop_distance():
    a = Stop("771", "А", 43.26412, 28.0185833)
    assert a.distance_to(43.2642, 28.01865) < 15
    assert Stop("x", "no position").distance_to(43.0, 28.0) == float("inf")
