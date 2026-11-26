# Changelog

## 0.3.0

- Timetable rows on a varnatraffic board are filled in from livetransport.eu: the bus planned for the trip, the delay it carries from its current trip and its destination. They stay `live: false`. livetransport is asked at most every 3 minutes per stop, and a failure there never affects the board. The option **Fill in planned buses from livetransport.eu** turns it off per stop.
- Live rows get the destination from livetransport.eu when the same bus is found there.
- The integration has its own icon (Home Assistant 2026.3 or newer).
- Releases are tagged, so HACS shows versions and release notes.

## 0.2.1

- Live departures from varnatraffic carry `features`: `air_conditioning`, `wheelchair`, `blind_assistance`, `toilet`.

## 0.2.0

- livetransport `live` is true only when the bus is on this very trip.
- Failover is decided by time: varnatraffic is skipped for 5 minutes after a minute of failures with no success at any stop.
- 403/429 pause a source for its `Retry-After`; when both sources fail, polling slows down to 1, 2, then 5 minutes.
- Emptied option fields stay empty. Reconfigure pairs a stop with livetransport later.
- A combined run shows once as `17/46`. Line sensors are removed when their line is dropped.
- Timetable rows whose time has passed are dropped. Night mode follows Varna time and wakes up at 04:45.

## 0.1.1

- Diagnostics probe both sources directly.

## 0.1.0

- First version: varnatraffic.com with livetransport.eu as fallback, config flow, next-departure, minutes and data-source sensors.
