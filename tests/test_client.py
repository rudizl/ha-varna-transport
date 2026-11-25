"""HTTP handling of the clients, against a mocked aiohttp session."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.varna_transport.api import (
    VT_BASE,
    TransportConnectionError,
    TransportDataError,
    TransportRateLimitedError,
    VarnaTrafficClient,
)
from custom_components.varna_transport.const import USER_AGENT

HOME = (Path(__file__).parent / "fixtures" / "varnatraffic_home.html").read_text(encoding="utf-8")
DEVICES = f"{VT_BASE}/Ajax/FindStationDevices"


async def test_http_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    client = VarnaTrafficClient(async_get_clientsession(hass))

    aioclient_mock.get(DEVICES, status=500)
    with pytest.raises(TransportConnectionError):
        await client._ajax("FindStationDevices")

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES, status=429, headers={"Retry-After": "1200"})
    with pytest.raises(TransportRateLimitedError) as err:
        await client._ajax("FindStationDevices")
    assert err.value.retry_after == 1200

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES, status=200, text="<html>Cloudflare</html>")
    with pytest.raises(TransportDataError):
        await client._ajax("FindStationDevices")

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES, exc=TimeoutError())
    with pytest.raises(TransportConnectionError):
        await client._ajax("FindStationDevices")


async def test_honest_user_agent(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    client = VarnaTrafficClient(async_get_clientsession(hass))
    aioclient_mock.get(DEVICES, json={"liveData": [], "schedule": []})
    await client._ajax("FindStationDevices")
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["User-Agent"] == USER_AGENT


async def test_line_names_are_cached_and_kept_on_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    client = VarnaTrafficClient(async_get_clientsession(hass))
    aioclient_mock.get(f"{VT_BASE}/bg", text=HOME)
    first = await client.line_names()
    assert first[114] == "14а"
    await client.line_names()
    assert aioclient_mock.call_count == 1  # cached for a day

    # Expired and the page is down: the last good list stays, and the next
    # attempt waits instead of retrying on every poll.
    client._line_names_next = 0
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{VT_BASE}/bg", status=503)
    assert await client.line_names() == first
    assert await client.line_names() == first
    assert aioclient_mock.call_count == 1
