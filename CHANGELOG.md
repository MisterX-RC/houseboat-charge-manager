# Changelog

Every push to this repo gets a new version in
`custom_components/houseboat_charge_manager/manifest.json` and an entry
here - this is what lets HACS reliably tell installed instances an update
exists, since `houseboat-charge-manager-sync-and-push.command` publishes a
matching GitHub Release (tag `v<version>`) automatically as the last step
of every push (see the README) - a plain git tag on its own isn't enough
for HACS to notice.

Versions are plain semver (`MAJOR.MINOR.PATCH`).

## [0.1.0] - 2026-10-09

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
