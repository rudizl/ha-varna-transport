"""Constants for the Varna Transport integration."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Final

DOMAIN: Final = "varna_transport"
VERSION: Final = "0.1.0"

USER_AGENT: Final = f"ha-varna-transport/{VERSION} (+https://github.com/rudizl/ha-varna-transport)"

SOURCE_VARNATRAFFIC: Final = "varnatraffic"
SOURCE_LIVETRANSPORT: Final = "livetransport"
SOURCES: Final = [SOURCE_VARNATRAFFIC, SOURCE_LIVETRANSPORT]

# Config entry data
CONF_VT_STOP_ID: Final = "varnatraffic_stop_id"
CONF_LT_STOP_ID: Final = "livetransport_stop_id"
CONF_STOP_NAME: Final = "stop_name"
CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"

# Options
CONF_LINES: Final = "lines"
CONF_UPCOMING: Final = "upcoming"
CONF_FALLBACK: Final = "fallback"
CONF_NIGHT_PAUSE: Final = "night_pause"
CONF_LINE_SENSORS: Final = "line_sensors"
CONF_LINE_NAMES: Final = "line_names"

DEFAULT_UPCOMING: Final = 10
DEFAULT_FALLBACK: Final = True
DEFAULT_NIGHT_PAUSE: Final = True
DEFAULT_LINE_SENSORS: Final = False

# Polling. varnatraffic refreshes its own page every 5 s; livetransport is a
# one-person hobby project, so it gets a gentler interval.
INTERVAL_VARNATRAFFIC: Final = timedelta(seconds=30)
INTERVAL_LIVETRANSPORT: Final = timedelta(seconds=60)
INTERVAL_NIGHT: Final = timedelta(minutes=15)

NIGHT_START: Final = time(0, 30)
NIGHT_END: Final = time(4, 45)

# Failover: after this many consecutive failures the primary source is
# skipped for PRIMARY_RETRY, then tried again.
FAILURES_BEFORE_FAILOVER: Final = 3
PRIMARY_RETRY: Final = timedelta(minutes=5)

REQUEST_TIMEOUT: Final = 15

# livetransport stop codes are 880000 + the varnatraffic station id
# (880771 <-> 771). Used to pair the two sources exactly.
LT_STOP_CODE_OFFSET: Final = 880000

# Internal varnatraffic line ids that are not on its line list (seasonal
# lines). Users can add more in the options.
DEFAULT_LINE_NAMES: Final[dict[int, str]] = {219: "209б"}
