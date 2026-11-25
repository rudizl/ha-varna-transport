# Fixtures

- `varnatraffic_station_102.json`, `varnatraffic_station_271.json`: real answers of
  `/Ajax/FindStationDevices`, saved on 2026-11-25.
- `livetransport_board_1124.json`: real time arrays, statuses and vehicles of
  `/varna/virtual-board/1124` on 2026-11-25 17:20 (first 10 rows). Trip and route arrays
  around them are rebuilt in the same positional format with the real line names.
- `livetransport_board.json`: hand-made edge cases (cancelled trip, no vehicle,
  plain-string headsign).
- `varnatraffic_home.html`: the line links of the varnatraffic home page.
