"""Config flow, failover and sensors inside a real Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.varna_transport.api import (
    Departure,
    Stop,
    StopBoard,
    TransportConnectionError,
)
from custom_components.varna_transport.const import (
    CONF_FALLBACK,
    CONF_LINE_SENSORS,
    CONF_LINES,
    CONF_LT_STOP_ID,
    CONF_STOP_NAME,
    CONF_VT_STOP_ID,
    DOMAIN,
)

API = "custom_components.varna_transport.api"
VT_STOP = Stop("771", "МАЙСТОР МАНОЛ-2 /за Варна/", 43.26412, 28.0185833)
LT_STOP = Stop("1080", "Майстор Манол /за Варна/", 43.2642, 28.01865, "880771")


def board(source: str, *lines: tuple[str, int, bool]) -> StopBoard:
    now = dt_util.utcnow()
    return StopBoard(
        source=source,
        departures=[
            Departure(
                line=line,
                expected=now + timedelta(minutes=mins, seconds=30),
                live=live,
                source=source,
                vehicle="8577" if live else None,
            )
            for line, mins, live in lines
        ],
    )


def entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="vt_771",
        title=VT_STOP.name,
        data={CONF_VT_STOP_ID: "771", CONF_LT_STOP_ID: "1080", CONF_STOP_NAME: VT_STOP.name},
        options={CONF_FALLBACK: True, CONF_LINES: "", **options},
    )


async def test_config_flow_pairs_by_stop_code(hass: HomeAssistant) -> None:
    with (
        patch(f"{API}.VarnaTrafficClient.search_stops", AsyncMock(return_value=[VT_STOP])),
        patch(
            f"{API}.LiveTransportClient.all_stops",
            AsyncMock(
                return_value=[
                    Stop("1119", "Майстор Манол /за КК/", 43.26257, 28.01842, "880271"),
                    LT_STOP,
                ]
            ),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"query": "манол"}
        )
        assert result["step_id"] == "stop"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "771"})
        assert result["step_id"] == "pair"
        # The exact match by stop code is preselected, not the nearer-looking one.
        schema_default = next(k.default() for k in result["data_schema"].schema if k == "pair")
        assert schema_default == "1080"

        with patch(
            f"{API}.VarnaTrafficClient.departures",
            AsyncMock(return_value=board("varnatraffic", ("9", 5, True))),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"pair": "1080"}
            )
            await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VT_STOP_ID] == "771"
    assert result["data"][CONF_LT_STOP_ID] == "1080"


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


async def test_sensors_and_failover(hass: HomeAssistant) -> None:
    cfg = entry()
    cfg.add_to_hass(hass)
    vt = AsyncMock(return_value=board("varnatraffic", ("109", 4, True), ("9", 12, False)))
    lt = AsyncMock(return_value=board("livetransport", ("109", 6, True)))

    with (
        patch(f"{API}.VarnaTrafficClient.departures", vt),
        patch(f"{API}.LiveTransportClient.departures", lt),
    ):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()

        minutes = hass.states.get("sensor.maistor_manol_2_za_varna_next_departure_in")
        nxt = hass.states.get("sensor.maistor_manol_2_za_varna_next_departure")
        source = hass.states.get("sensor.maistor_manol_2_za_varna_data_source")
        assert minutes.state == "4" and minutes.attributes["line"] == "109"
        assert nxt.attributes["live"] is True
        assert [d["line"] for d in nxt.attributes["upcoming"]] == ["109", "9"]
        assert source.state == "varnatraffic"
        lt.assert_not_called()

        # varnatraffic goes down: the same poll falls through to livetransport.
        vt.side_effect = TransportConnectionError("timeout")
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        assert hass.states.get(source.entity_id).state == "livetransport"
        assert hass.states.get(minutes.entity_id).state == "6"

        # Both down: sensors become unavailable instead of showing old data.
        lt.side_effect = TransportConnectionError("also down")
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
        await hass.async_block_till_done()
        assert hass.states.get(minutes.entity_id).state == "unavailable"

        # varnatraffic is back after its cool-down.
        vt.side_effect = None
        lt.side_effect = None
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
        await hass.async_block_till_done()
        assert hass.states.get(source.entity_id).state == "varnatraffic"
        assert hass.states.get(minutes.entity_id).state == "4"


async def test_line_filter_and_line_sensors(hass: HomeAssistant) -> None:
    cfg = entry(**{CONF_LINES: "9, 14a", CONF_LINE_SENSORS: True})
    cfg.add_to_hass(hass)
    data = board("varnatraffic", ("109", 2, True), ("14а", 7, True), ("9", 12, False))
    with patch(f"{API}.VarnaTrafficClient.departures", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(cfg.entry_id)
        await hass.async_block_till_done()

    minutes = hass.states.get("sensor.maistor_manol_2_za_varna_next_departure_in")
    assert minutes.state == "7" and minutes.attributes["line"] == "14а"
    line9 = hass.states.get("sensor.maistor_manol_2_za_varna_line_9")
    assert line9 is not None and line9.attributes["minutes"] == 12


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
