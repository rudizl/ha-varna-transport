"""Config flow: pick a stop on varnatraffic, pair it with livetransport."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .api import LiveTransportClient, Stop, TransportError, VarnaTrafficClient
from .const import (
    CONF_FALLBACK,
    CONF_LATITUDE,
    CONF_LINE_NAMES,
    CONF_LINE_SENSORS,
    CONF_LINES,
    CONF_LONGITUDE,
    CONF_LT_STOP_ID,
    CONF_NIGHT_PAUSE,
    CONF_STOP_NAME,
    CONF_UPCOMING,
    CONF_VT_STOP_ID,
    DEFAULT_FALLBACK,
    DEFAULT_LINE_SENSORS,
    DEFAULT_NIGHT_PAUSE,
    DEFAULT_UPCOMING,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

NO_PAIR = "none"
CONF_QUERY = "query"
CONF_STOP = "stop"
CONF_PAIR = "pair"


class VarnaTransportConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one stop."""

    VERSION = 1

    def __init__(self) -> None:
        self._stops: dict[str, Stop] = {}
        self._stop: Stop | None = None
        self._candidates: list[Stop] = []
        self._exact: Stop | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Search by name, or leave empty for stops near home."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = VarnaTrafficClient(async_get_clientsession(self.hass))
            query = (user_input.get(CONF_QUERY) or "").strip()
            try:
                if query:
                    stops = await client.search_stops(query)
                else:
                    stops = await client.nearest_stops(
                        self.hass.config.latitude, self.hass.config.longitude, 600
                    )
            except TransportError as err:
                _LOGGER.debug("Stop search failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if stops:
                    self._stops = {s.id: s for s in stops[:30]}
                    return await self.async_step_stop()
                errors["base"] = "no_stops"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_QUERY, default=""): TextSelector()}),
            errors=errors,
        )

    async def async_step_stop(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose one of the found stops."""
        if user_input is not None:
            self._stop = self._stops[user_input[CONF_STOP]]
            await self.async_set_unique_id(f"vt_{self._stop.id}")
            self._abort_if_unique_id_configured()
            return await self.async_step_pair()

        options = [
            SelectOptionDict(value=s.id, label=f"{s.name} ({s.id})") for s in self._stops.values()
        ]
        return self.async_show_form(
            step_id="stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the matching livetransport stop used as fallback."""
        assert self._stop is not None
        if user_input is not None:
            pair = user_input[CONF_PAIR]
            return self._create(None if pair == NO_PAIR else pair)

        reachable = True
        if not self._candidates:
            client = LiveTransportClient(async_get_clientsession(self.hass))
            try:
                self._exact, self._candidates = await client.match_stop(self._stop)
            except TransportError as err:
                _LOGGER.debug("livetransport unreachable: %s", err)
                reachable = False

        lat, lon = self._stop.latitude, self._stop.longitude
        options = []
        for cand in self._candidates:
            dist = cand.distance_to(lat, lon) if lat is not None and lon is not None else None
            mark = " ✓" if cand == self._exact else ""
            where = f", {dist:.0f} m" if dist is not None and dist != float("inf") else ""
            options.append(
                SelectOptionDict(
                    value=cand.id, label=f"{cand.name} ({cand.code or cand.id}{where}){mark}"
                )
            )
        options.append(SelectOptionDict(value=NO_PAIR, label="—"))
        default = (
            self._exact.id
            if self._exact
            else (self._candidates[0].id if self._candidates else NO_PAIR)
        )
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIR, default=default): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            errors={} if reachable else {"base": "livetransport_unreachable"},
            description_placeholders={"stop": self._stop.name},
        )

    @callback
    def _create(self, lt_stop: str | None) -> ConfigFlowResult:
        assert self._stop is not None
        return self.async_create_entry(
            title=self._stop.name,
            data={
                CONF_VT_STOP_ID: self._stop.id,
                CONF_LT_STOP_ID: lt_stop,
                CONF_STOP_NAME: self._stop.name,
                CONF_LATITUDE: self._stop.latitude,
                CONF_LONGITUDE: self._stop.longitude,
            },
            options={
                CONF_LINES: "",
                CONF_UPCOMING: DEFAULT_UPCOMING,
                CONF_FALLBACK: DEFAULT_FALLBACK,
                CONF_NIGHT_PAUSE: DEFAULT_NIGHT_PAUSE,
                CONF_LINE_SENSORS: DEFAULT_LINE_SENSORS,
                CONF_LINE_NAMES: "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> VarnaTransportOptionsFlow:
        return VarnaTransportOptionsFlow()


class VarnaTransportOptionsFlow(OptionsFlow):
    """Lines to watch and behaviour."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_UPCOMING] = int(user_input[CONF_UPCOMING])
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_LINES, default=opts.get(CONF_LINES, "")): TextSelector(),
                vol.Required(
                    CONF_UPCOMING, default=opts.get(CONF_UPCOMING, DEFAULT_UPCOMING)
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_LINE_SENSORS, default=opts.get(CONF_LINE_SENSORS, DEFAULT_LINE_SENSORS)
                ): BooleanSelector(),
                vol.Required(
                    CONF_FALLBACK, default=opts.get(CONF_FALLBACK, DEFAULT_FALLBACK)
                ): BooleanSelector(),
                vol.Required(
                    CONF_NIGHT_PAUSE, default=opts.get(CONF_NIGHT_PAUSE, DEFAULT_NIGHT_PAUSE)
                ): BooleanSelector(),
                vol.Optional(
                    CONF_LINE_NAMES, default=opts.get(CONF_LINE_NAMES, "")
                ): TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
