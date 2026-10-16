# Changelog

Every push to this repo gets a new version in
`custom_components/houseboat_charge_manager/manifest.json` and an entry
here - this is what lets HACS reliably tell installed instances an update
exists, since `houseboat-charge-manager-sync-and-push.command` publishes a
matching GitHub Release (tag `v<version>`) automatically as the last step
of every push (see the README) - a plain git tag on its own isn't enough
for HACS to notice.

Versions are **year.month.release**, like ESS Manager's own (as of
2026.10.0): the year and month the release is made, then a number counting
the releases in that month from 0 - `2026.10.0`, `2026.10.1`, ...,
`2026.11.0`.

## [2026.10.2] - 2026-10-16

### Added
- **Day-based vacation departure target.** A new `number` entity,
  *Vacation departure runway (days)* (default 15, whole days), replaces the
  fixed 90% vacation ceiling with a dynamic target: in vacation mode the
  system now aims to have that many days of usable energy (above the
  low-battery floor) available at departure time, converted to a
  percentage from your battery size and average load. The default (15)
  is deliberately large enough to always saturate at 100%, so setting a
  lower, limited target is an opt-in - the integration never limits
  charging on its own.
- A second new `number` entity, *Vacation departure buffer* (default 2h),
  is added on top: charging still finishes that many hours before the
  actual departure time even with a lower target, with a little extra
  energy bought upfront to cover consumption during that buffer window.
- The existing *Vacation charge floor/ceiling* settings now configure the
  WIDTH of the start/stop band (20 points by default) rather than two
  fixed percentages - in vacation mode the ceiling is the new dynamic
  target and the floor trails it by that same width, so the 20-point
  margin still applies however the target is set.
- The runway-days number automatically resets to its default (15, i.e.
  "just keep it full") the moment shore power becomes unavailable while
  no departure is pending - so a limited target set for one trip never
  silently carries over and under-charges the battery afterwards.

## [2026.10.1] - 2026-10-16

### Fixed
- **Reconfigure/Options crashed with "Config flow could not be loaded: 500
  Internal Server Error".** The options flow set `self.config_entry`
  manually in `__init__`, the pattern every HA integration used for years -
  recent Home Assistant Core versions provide it automatically and treat it
  as read-only, so setting it themselves now throws. Removed; the base
  class supplies it.
- **A sensor going `unavailable`/`unknown` (a brief dropout, a reboot, an
  integration reload) was silently read as `0`** and fed straight into the
  calculation - a battery SoC sensor blip could make the whole system
  briefly believe the battery was at 0%, forcing a "critical SoC" charge
  decision, and a momentary cell-voltage dropout made `is_cell_balanced`
  look `true` (both readings defaulting to the same 0, "balanced" by
  coincidence) instead of reflecting reality. The coordinator now checks
  every required source entity before computing anything; if any of them
  is missing, `unavailable` or `unknown`, the whole update is skipped (the
  status sensor goes `unavailable` until the source recovers, same as any
  other HA integration whose source drops out) instead of quietly
  continuing on fabricated zeros. `switch.walstroom_l`'s own `unavailable`
  state is unaffected - that one is how `is_grid_unavailable` is detected
  in the first place, not an error.

## [2026.10.0] - 2026-10-09

### Added
- First working version: a full Python port of the original
  `sensor.ess_charge_manager` trigger-based template sensor into a
  configurable, HACS-installable integration.
- Config flow for every external entity the original hardcoded (battery
  SoC/voltage/cell-voltage sensors, price sensor, 14-day average price,
  shore power, average load, requested shore power, departure countdown,
  low-battery floor, vacation mode, force-100% override, grid switch).
- `number` entities for every tunable threshold (battery size, charger
  efficiency, SoC floor/ceiling, vacation band, flat-block margin/
  tolerance, calibration interval, balance voltage/tolerance/settle time,
  cooldown duration/SoC guard, voyage timing buffers, unplanned-session
  buffer, session energy cap, and the power thresholds).
- Main status sensor (state = the original's `system_status` values) plus
  standalone value sensors for the figures most useful on their own.
- The original `custom:houseboat-price-card` / `custom:houseboat-status-
  card` dashboard cards, bundled and auto-registered as a Lovelace
  resource on setup.
