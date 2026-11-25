"""Config flow, failover and sensors inside a real Home Assistant.

The clock is frozen at noon in Varna, so the night mode never interferes
unless a test moves the clock there on purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.varna_transport.api import (
    TZ,
    Departure,
    Stop,
    StopBoard,
    TransportConnectionError,
    TransportRateLimitedError,
)
from custom_components.varna_transport.const import (
    CONF_FALLBACK,
    CONF_LINE_NAMES,
    CONF_LINE_SENSORS,
    CONF_LINES,
    CONF_LT_STOP_ID,
    CONF_NIGHT_PAUSE,
    CONF_STOP_NAME,
    CONF_UPCOMING,
    CONF_VT_STOP_ID,
    DOMAIN,
)

API = "custom_components.varna_transport.api"
NOON = datetime(2026, 11, 25, 12, 0, tzinfo=TZ)
VT_STOP = Stop("771", "МАЙСТОР МАНОЛ-2 /за Варна/", 43.26412, 28.0185833)
LT_STOP = Stop("1080", "Майстор Манол /за Варна/", 43.2642, 28.01865, "880771")
LT_OTHER = Stop("1119", "Майстор Манол /за КК/", 43.26257, 28.01842, "880271")
MINUTES = "sensor.maistor_manol_2_za_varna_next_departure_in"
NEXT = "sensor.maistor_manol_2_za_varna_next_departure"
SOURCE = "sensor.maistor_manol_2_za_varna_data_source"


@pytest.fixture(autouse=True)
def noon(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOON)
    return freezer


def board(source: str, *lines: tuple[str, int, bool], vehicle: str = "8577") -> StopBoard:
    now = dt_util.utcnow()
    return StopBoard(
        source=source,
        departures=[
            Departure(
                line=line,
                expected=now + timedelta(minutes=mins, seconds=30),
                live=live,
                source=source,
                vehicle=vehicle if live else None,
            )
            for line, mins, live in lines
        ],
    )


def fresh(source: str, *lines: tuple[str, int, bool], vehicle: str = "8577"):
    """A side effect that builds the board at call time, so times stay relative."""

    async def _call(*_args, **_kwargs) -> StopBoard:
        return board(source, *lines, vehicle=vehicle)

    return _call


def entry(unique: str = "771", **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"vt_{unique}",
        title=VT_STOP.name if unique == "771" else f"Stop {unique}",
        data={
            CONF_VT_STOP_ID: unique,
            CONF_LT_STOP_ID: "1080",
            CONF_STOP_NAME: VT_STOP.name if unique == "771" else f"Stop {unique}",
        },
        options={CONF_FALLBACK: True, CONF_LINES: "", **options},
    )


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------- config flow


async def _flow_to_pair(hass: HomeAssistant, stops: list[Stop]):
    with (
        patch(f"{API}.VarnaTrafficClient.search_stops", AsyncMock(return_value=[VT_STOP])),
        patch(f"{API}.LiveTransportClient.all_stops", AsyncMock(return_value=stops)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"query": "манол"}
        )
        assert result["step_id"] == "stop"
        return await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "771"})


def _pair_default(result):
    key = next(k for k in result["data_schema"].schema if k == "pair")
    return key.default() if callable(key.default) else None


async def test_config_flow_pairs_by_stop_code(hass: HomeAssistant) -> None:
    result = await _flow_to_pair(hass, [LT_OTHER, LT_STOP])
    assert result["step_id"] == "pair"
    assert _pair_default(result) == "1080"

    with patch(
        f"{API}.VarnaTrafficClient.departures",
        AsyncMock(return_value=board("varnatraffic", ("9", 5, True))),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pair": "1080"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LT_STOP_ID] == "1080"


async def test_config_flow_no_default_without_code_match(hass: HomeAssistant) -> None:
    """The nearest stop is often across the street, so nothing is preselected."""
    result = await _flow_to_pair(hass, [LT_OTHER])
    assert result["step_id"] == "pair"
    assert _pair_default(result) is None


async def test_config_flow_livetransport_down(hass: HomeAssistant) -> None:
    with (
        patch(f"{API}.VarnaTrafficClient.search_stops", AsyncMock(return_value=[VT_STOP])),
        patch(
            f"{API}.LiveTransportClient.all_stops",
            AsyncMock(side_effect=TransportConnectionError("HTTP 520")),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"query": "x"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "771"})
    assert result["errors"] == {"base": "livetransport_unreachable"}
    with patch(
        f"{API}.VarnaTrafficClient.departures",
        AsyncMock(return_value=board("varnatraffic", ("9", 5, True))),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pair": "none"})
        await hass.async_block_till_done()
    assert result["data"][CONF_LT_STOP_ID] is None


async def test_config_flow_cannot_connect(hass: HomeAssistant) -> None:
    with patch(
        f"{API}.VarnaTrafficClient.nearest_stops",
        AsyncMock(side_effect=TransportConnectionError("down")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_config_flow_duplicate_stop(hass: HomeAssistant) -> None:
    entry().add_to_hass(hass)
    with patch(f"{API}.VarnaTrafficClient.search_stops", AsyncMock(return_value=[VT_STOP])):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"query": "x"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "771"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_pairs_later(hass: HomeAssistant) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    hass.config_entries.async_update_entry(cfg, data={**cfg.data, CONF_LT_STOP_ID: None})
    vt = AsyncMock(return_value=board("varnatraffic", ("9", 5, True)))
    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.all_stops", AsyncMock(return_value=[LT_STOP])),
    ):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        result = await cfg.start_reconfigure_flow(hass)
        assert result["step_id"] == "pair" and _pair_default(result) == "1080"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pair": "1080"})
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert cfg.data[CONF_LT_STOP_ID] == "1080"


async def test_options_can_be_cleared(hass: HomeAssistant) -> None:
    cfg = entry(**{CONF_LINES: "9, 109", CONF_LINE_NAMES: "219=209б"})
    cfg.add_to_hass(hass)
    with patch(
        f"{API}.VarnaTrafficClient.departures",
        AsyncMock(return_value=board("varnatraffic", ("9", 5, True))),
    ):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(cfg.entry_id)
        # The frontend leaves emptied text fields out of the answer.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_UPCOMING: 5,
                CONF_LINE_SENSORS: False,
                CONF_FALLBACK: True,
                CONF_NIGHT_PAUSE: True,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cfg.options[CONF_LINES] == "" and cfg.options[CONF_LINE_NAMES] == ""
    assert cfg.options[CONF_UPCOMING] == 5


# ------------------------------------------------------------------ failover


async def test_failover_and_recovery(hass: HomeAssistant, noon: FrozenDateTimeFactory) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    vt_ok = fresh("varnatraffic", ("109", 4, True), ("9", 12, False))
    lt_ok = fresh("livetransport", ("109", 6, True))
    vt = AsyncMock(side_effect=vt_ok)
    lt = AsyncMock(side_effect=lt_ok)

    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        coordinator = cfg.runtime_data
        assert hass.states.get(MINUTES).state == "4"
        assert hass.states.get(NEXT).attributes["live"] is True
        assert hass.states.get(SOURCE).state == "varnatraffic"
        lt.assert_not_called()

        # varnatraffic down: the same poll falls through to livetransport.
        vt.side_effect = TransportConnectionError("timeout")
        await tick(hass, noon, 31)
        assert hass.states.get(SOURCE).state == "livetransport"
        assert hass.states.get(MINUTES).state == "6"
        assert coordinator.update_interval == timedelta(seconds=60)
        assert coordinator.failovers == 1

        # Still failing a minute later: varnatraffic is skipped for 5 minutes.
        await tick(hass, noon, 61)
        vt_calls = vt.call_count
        await tick(hass, noon, 61)
        assert vt.call_count == vt_calls  # not asked any more
        assert hass.states.get(SOURCE).state == "livetransport"

        # Both down: unavailable, and polling slows down.
        lt.side_effect = TransportConnectionError("also down")
        await tick(hass, noon, 61)
        assert hass.states.get(MINUTES).state == "unavailable"
        first_backoff = coordinator.update_interval
        await tick(hass, noon, first_backoff.total_seconds() + 1)
        assert coordinator.update_interval > first_backoff

        # Both back; varnatraffic is first again once its pause is over.
        vt.side_effect = vt_ok
        lt.side_effect = lt_ok
        await tick(hass, noon, 400)
        assert hass.states.get(SOURCE).state == "varnatraffic"
        assert hass.states.get(MINUTES).state == "4"
        assert coordinator.update_interval == timedelta(seconds=30)
        assert coordinator.failovers == 1  # returning is not a failover


async def test_short_outage_across_stops_does_not_skip_primary(
    hass: HomeAssistant, noon: FrozenDateTimeFactory
) -> None:
    """Four stops failing in the same instant are one outage, not four."""
    vt = AsyncMock(return_value=board("varnatraffic", ("9", 5, True)))
    lt = AsyncMock(return_value=board("livetransport", ("9", 6, True)))
    entries = [entry(str(i)) for i in (771, 271, 795, 295)]
    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        for cfg in entries:
            cfg.add_to_hass(hass)
            await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()

        vt.side_effect = TransportConnectionError("blip")
        await tick(hass, noon, 31)
        health = entries[0].runtime_data.clients.health["varnatraffic"]
        assert health.failures == 4
        assert health.skip_until is None

        vt.side_effect = None
        await tick(hass, noon, 61)
        assert health.failures == 0
        assert all(c.runtime_data.active_source == "varnatraffic" for c in entries)


async def test_rate_limit_pauses_the_source(
    hass: HomeAssistant, noon: FrozenDateTimeFactory
) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    vt = AsyncMock(return_value=board("varnatraffic", ("9", 5, True)))
    lt = AsyncMock(return_value=board("livetransport", ("9", 6, True)))
    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        vt.side_effect = TransportRateLimitedError("HTTP 429", 900)
        await tick(hass, noon, 31)
        calls = vt.call_count
        for _ in range(5):
            await tick(hass, noon, 61)
        assert vt.call_count == calls  # 15 minutes of silence towards varnatraffic
        assert hass.states.get(SOURCE).state == "livetransport"


async def test_bug_in_primary_still_falls_back(
    hass: HomeAssistant, noon: FrozenDateTimeFactory
) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    vt = AsyncMock(return_value=board("varnatraffic", ("9", 5, True)))
    lt = AsyncMock(return_value=board("livetransport", ("9", 6, True)))
    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        vt.side_effect = KeyError("liveData")
        await tick(hass, noon, 31)
    assert hass.states.get(SOURCE).state == "livetransport"


async def test_without_fallback_no_livetransport_calls(hass: HomeAssistant) -> None:
    cfg = entry(**{CONF_FALLBACK: False})
    cfg.add_to_hass(hass)
    lt = AsyncMock()
    with (
        patch(
            f"{API}.VarnaTrafficClient.departures",
            AsyncMock(side_effect=TransportConnectionError("down")),
        ),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
    lt.assert_not_called()
    assert cfg.state is config_entries.ConfigEntryState.SETUP_RETRY


async def test_night_interval_wakes_up_at_the_end(
    hass: HomeAssistant, noon: FrozenDateTimeFactory
) -> None:
    noon.move_to(datetime(2026, 11, 26, 1, 0, tzinfo=TZ))
    cfg = entry()
    cfg.add_to_hass(hass)
    vt = AsyncMock(return_value=board("varnatraffic", ("9", 300, False)))
    with patch(f"{API}.VarnaTrafficClient.departures", vt):
        await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        coordinator = cfg.runtime_data
        assert coordinator.update_interval == timedelta(minutes=15)
        noon.move_to(datetime(2026, 11, 26, 4, 40, tzinfo=TZ))
        await coordinator.async_refresh()
        assert coordinator.update_interval == timedelta(minutes=5)
        noon.move_to(datetime(2026, 11, 26, 4, 46, tzinfo=TZ))
        await coordinator.async_refresh()
        assert coordinator.update_interval == timedelta(seconds=30)


# ------------------------------------------------------------------- sensors


async def test_line_filter_and_line_sensors(hass: HomeAssistant) -> None:
    cfg = entry(**{CONF_LINES: "9, 14a", CONF_LINE_SENSORS: True})
    cfg.add_to_hass(hass)
    data = board("varnatraffic", ("109", 2, True), ("14а", 7, True), ("9", 12, False))
    with patch(f"{API}.VarnaTrafficClient.departures", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get(MINUTES).state == "7"
        assert hass.states.get(MINUTES).attributes["line"] == "14а"
        line9 = hass.states.get("sensor.maistor_manol_2_za_varna_line_9")
        assert line9 is not None and line9.attributes["minutes"] == 12

        # Dropping a line removes its sensor instead of leaving an orphan.
        hass.config_entries.async_update_entry(cfg, options={**cfg.options, CONF_LINES: "14a"})
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.maistor_manol_2_za_varna_line_9") is None
    assert registry.async_get("sensor.maistor_manol_2_za_varna_line_14a") is not None


async def test_combined_run_is_one_row(hass: HomeAssistant) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    data = board("varnatraffic", ("17", 5, True), ("46", 5, True), vehicle="8606")
    with patch(f"{API}.VarnaTrafficClient.departures", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
    upcoming = hass.states.get(NEXT).attributes["upcoming"]
    assert [d["line"] for d in upcoming] == ["17/46"]


async def test_diagnostics_probe_both_sources(hass: HomeAssistant) -> None:
    from custom_components.varna_transport.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    cfg = entry()
    cfg.add_to_hass(hass)
    vt = AsyncMock(return_value=board("varnatraffic", ("109", 4, True)))
    lt = AsyncMock(return_value=board("livetransport", ("109", 6, True)))
    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()
        lt.assert_not_called()

        diag = await async_get_config_entry_diagnostics(hass, cfg)
        assert diag["probe"]["livetransport"]["ok"] is True
        assert diag["probe"]["livetransport"]["departures"][0]["line"] == "109"

        lt.side_effect = TransportConnectionError("HTTP 520")
        diag = await async_get_config_entry_diagnostics(hass, cfg)
        assert diag["probe"]["livetransport"] == {"stop": "1080", "ok": False, "error": "HTTP 520"}

        lt.side_effect = KeyError("bug")
        diag = await async_get_config_entry_diagnostics(hass, cfg)
        assert diag["probe"]["livetransport"]["ok"] is False

        # A probe does not count as a failure of the running source.
        assert diag["health"]["livetransport"]["failures"] == "0"
        assert diag["active_source"] == "varnatraffic"
