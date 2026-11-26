"""Config flow: pick a stop on varnatraffic, pair it with livetransport."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
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

from .api import Stop, TransportError
from .const import (
    CONF_ENRICH,
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
    DEFAULT_ENRICH,
    DEFAULT_FALLBACK,
    DEFAULT_LINE_SENSORS,
    DEFAULT_NIGHT_PAUSE,
    DEFAULT_UPCOMING,
    DOMAIN,
)
from .coordinator import get_clients

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
            client = get_clients(self.hass).varnatraffic
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair (again) with livetransport, e.g. when it was down at setup."""
        entry = self._get_reconfigure_entry()
        self._stop = Stop(
            id=entry.data[CONF_VT_STOP_ID],
            name=entry.data.get(CONF_STOP_NAME, entry.title),
            latitude=entry.data.get(CONF_LATITUDE),
            longitude=entry.data.get(CONF_LONGITUDE),
        )
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the matching livetransport stop used as fallback."""
        assert self._stop is not None
        if user_input is not None:
            pair = user_input[CONF_PAIR]
            lt_stop = None if pair == NO_PAIR else pair
            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={CONF_LT_STOP_ID: lt_stop},
                )
            return self._create(lt_stop)

        reachable = True
        if not self._candidates:
            try:
                self._exact, self._candidates = await get_clients(
                    self.hass
                ).livetransport.match_stop(self._stop)
            except TransportError as err:
                _LOGGER.debug("livetransport unreachable: %s", err)
                reachable = False

        lat, lon = self._stop.latitude, self._stop.longitude
        options = []
        for cand in self._candidates:
            dist = cand.distance_to(lat, lon)
            where = f", {dist:.0f} m" if dist != float("inf") else ""
            mark = " ✓" if cand == self._exact else ""
            options.append(
                SelectOptionDict(
                    value=cand.id, label=f"{cand.name} ({cand.code or cand.id}{where}){mark}"
                )
            )
        options.append(SelectOptionDict(value=NO_PAIR, label="—"))
        # Only the exact code match is preselected. The nearest stop is often
        # the one across the street, for the other direction.
        key = (
            vol.Required(CONF_PAIR, default=self._exact.id)
            if self._exact
            else vol.Required(CONF_PAIR)
        )
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    key: SelectSelector(
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
                CONF_ENRICH: DEFAULT_ENRICH,
                CONF_NIGHT_PAUSE: DEFAULT_NIGHT_PAUSE,
                CONF_LINE_SENSORS: DEFAULT_LINE_SENSORS,
                CONF_LINE_NAMES: "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> VarnaTransportOptionsFlow:
        return VarnaTransportOptionsFlow()


OPTIONS_SCHEMA = vol.Schema(
    {
        # Text fields use suggested values, not defaults: an emptied field is
        # not sent by the frontend, and a default would bring the old value back.
        vol.Optional(CONF_LINES): TextSelector(),
        vol.Required(CONF_UPCOMING, default=DEFAULT_UPCOMING): NumberSelector(
            NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_LINE_SENSORS, default=DEFAULT_LINE_SENSORS): BooleanSelector(),
        vol.Required(CONF_FALLBACK, default=DEFAULT_FALLBACK): BooleanSelector(),
        vol.Required(CONF_ENRICH, default=DEFAULT_ENRICH): BooleanSelector(),
        vol.Required(CONF_NIGHT_PAUSE, default=DEFAULT_NIGHT_PAUSE): BooleanSelector(),
        vol.Optional(CONF_LINE_NAMES): TextSelector(),
    }
)


class VarnaTransportOptionsFlow(OptionsFlow):
    """Lines to watch and behaviour."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **user_input,
                    CONF_UPCOMING: int(user_input[CONF_UPCOMING]),
                    CONF_LINES: user_input.get(CONF_LINES, ""),
                    CONF_LINE_NAMES: user_input.get(CONF_LINE_NAMES, ""),
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
