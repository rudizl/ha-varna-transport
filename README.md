# Varna Transport for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/)
[![Validate](https://github.com/rudizl/ha-varna-transport/actions/workflows/validate.yml/badge.svg)](https://github.com/rudizl/ha-varna-transport/actions/workflows/validate.yml)

Bus and trolleybus arrivals for stops in Varna, Bulgaria.

The integration reads [varnatraffic.com](https://varnatraffic.com). When that site is down, it switches to [livetransport.eu](https://livetransport.eu/#/varna), which gets Varna data from a separate system (TASRUD), and switches back once varnatraffic answers again.

## Installation

1. In HACS, open **Custom repositories** and add `https://github.com/rudizl/ha-varna-transport` as an **Integration**.
2. Install **Varna Transport** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Varna Transport**.

## Adding a stop

1. Type part of the stop name, or leave the field empty to list stops within 600 m of your home.
2. Pick the stop. Most stops come in pairs, one per direction (`/за Варна/`, `/за КК/`).
3. Confirm the fallback stop on livetransport.eu. It is matched by stop code (livetransport code = 880000 + varnatraffic id) and marked with ✓. Choose `—` if you only want varnatraffic.

Add each stop and direction separately. Each one becomes a device.

## Entities

| Entity | State | Notes |
|---|---|---|
| `sensor.<stop>_next_departure` | Timestamp of the next bus | Attributes: `line`, `minutes`, `delay` (s), `live`, `vehicle`, `destination`, `source`, and `upcoming` |
| `sensor.<stop>_next_departure_in` | Minutes until the next bus | Attributes: `line`, `live` |
| `sensor.<stop>_line_<n>` | Timestamp of the next bus on one line | Only when **One sensor per line** is on |
| `sensor.<stop>_data_source` | `varnatraffic` or `livetransport` | Diagnostic. Attributes count failures and failovers |

`upcoming` is a list of departures, soonest first. All times are Varna local time in whole seconds:

```yaml
- line: "109"
  expected: "2026-11-25T18:08:12+02:00"
  scheduled: "2026-11-25T18:05:00+02:00"
  minutes: 7
  delay: 161          # seconds, negative = early
  live: true          # GPS countdown for this very trip
  vehicle: "8563"     # fleet number
  distance: 3100      # metres to the stop (varnatraffic only)
  destination: null   # headsign (livetransport only)
```

- `live: false` with a `vehicle` means a bus is assigned but not yet on this trip (waiting at the terminus, or still on its previous trip). The time is the timetable plus any delay carried over.
- A timetable-only row is dropped once its time has passed. Its bus is late by an unknown amount, so it does not show up as "0 minutes".
- One bus running two lines at once (varnatraffic line 700) appears as one row, for example `17/46`. A line filter or a line sensor still finds it under each line.
- Fleet numbers can differ between the sources: varnatraffic `40229` is livetransport `0229`.

`upcoming` and `minutes` are not written to the recorder database, because they change on every poll.

## Options

- **Lines to show**: comma separated, for example `9, 109, 14a`. Empty shows every line. `14a` and `14а` are treated as the same line.
- **Departures in the board attribute**: length of `upcoming` (default 10).
- **One sensor per line**: adds a sensor for each line above.
- **Fall back to livetransport.eu**: on by default.
- **Poll every 15 minutes at night**: between 00:30 and 04:45, when no city lines run.
- **Extra line names**: varnatraffic uses internal ids. Most are mapped from its line list (114 → 14а, 1018 → 18а). For seasonal lines it does not list, add pairs such as `219=209б` (this one is built in).

## How failover works

- Polling: every 30 s on varnatraffic, every 60 s on livetransport.
- If varnatraffic fails, the same poll asks livetransport, so no update is skipped.
- If varnatraffic keeps failing for a minute without a single success at any stop, it is skipped for 5 minutes and then tried again. A short blip that hits all stops at once does not count as an outage.
- If both fail, the sensors become `unavailable` instead of showing an old board, and polling slows down to 1, 2, then 5 minutes.
- A source that answers 403 or 429 is left alone for its `Retry-After` time (at least 5 minutes).
- Between 00:30 and 04:45 Varna time the stop is checked every 15 minutes, and polling speeds up again right at 04:45.

You can be notified on a switch with an automation on `sensor.<stop>_data_source`.

If livetransport.eu was down when you added a stop, pair it later with **Reconfigure** on the stop's entry.

## Voice example

```yaml
sequence:
  - variables:
      s: sensor.maistor_manol_2_za_varna_next_departure
      result:
        stop: "{{ device_attr(device_id(s), 'name') }}"
        next: "{{ (state_attr(s, 'upcoming') or [])[:3] }}"
  - stop: Buses
    response_variable: result
```

## About the data

Neither site offers a public API. The integration uses the same endpoints as their web pages. It sends an honest `User-Agent` and polls gently. livetransport.eu is run by one person, so it gets the longer interval and is only used as a fallback. If either site asks for this to stop, it will.

This project is not affiliated with Gradski Transport, TASRUD, MapSoft or livetransport.eu.

## Development

```bash
pip install -r requirements_test.txt
ruff check . && pytest
```

The varnatraffic fixtures in `tests/fixtures` are real answers. The livetransport fixture follows the field order in its front end.
