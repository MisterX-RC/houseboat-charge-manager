# Houseboat Charge Manager

A Home Assistant custom integration that plans shore-power charging for a
Victron-based houseboat battery bank: dynamic electricity-price search,
flat-block extension, a SoC-based price ceiling, a 30-day calibration/full
-charge cycle, and voyage/departure-aware forcing - all price- and
SoC-aware, no fixed schedule.

This is a full rewrite of a hand-maintained Home Assistant trigger-based
template sensor (`sensor.ess_charge_manager`, ~1600 lines of Jinja) into a
proper HACS-installable integration, modeled on the structure of
[MisterX-RC/ESS-manager-HA](https://github.com/MisterX-RC/ESS-manager-HA).
Every entity the original hardcoded is now picked in a setup wizard, and
every threshold that used to be a bare number buried in a Jinja template
(battery size, charger efficiency, SoC floor/ceiling, flat-block margin,
calibration interval, voyage buffers, ...) is now a `number` entity you can
adjust from the dashboard.

## Installing

### Via HACS (once this repo is added as a custom repository)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo's URL,
   category "Integration".
2. Find "Houseboat Charge Manager" in HACS and install it.
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → "Houseboat Charge
   Manager".
5. Fill in the setup wizard - every field is an entity picker, no YAML.

### Manually

Copy `custom_components/houseboat_charge_manager` into your `config/
custom_components/` folder, restart Home Assistant, then follow steps 4-5
above.

## Setup wizard fields

All required - these replace every `states('sensor.xxx')` /
`is_state('input_boolean.xxx', ...)` call the original template hardcoded:

| Field | What it was in the original |
|---|---|
| Battery SoC sensor | `sensor.victron_bmv_soc_238` |
| Battery voltage sensor | `sensor.victron_bmv_voltage_238` |
| Highest/lowest cell voltage sensors | `sensor.victron_lifepo4_system_max/mincellvoltage` |
| Electricity price sensor | `sensor.nordpool_kwh_nl_eur_3_10_0` (state = current price, `today`/`tomorrow` attributes = the price arrays - this also replaces the original's separate `sensor.stroomprijs_huidig` helper, since a standard Nordpool sensor's own state already *is* the current price) |
| 14-day average price sensor | `sensor.stroomprijs_gemiddeld` |
| Shore power draw sensor | `sensor.walstroom_total_power` |
| 24h average load sensor | `sensor.gemiddeld_stroomverbruik_24_uur` |
| Requested shore power (input_number, amps) | `input_number.walstroom_requested` |
| Hours until departure (input_number) | `input_number.start_voyage` |
| Low-battery floor % (input_number) | `input_number.low_battery_percentage` |
| Vacation mode switch | `input_boolean.vakantie_modus` |
| Force-100% override switch | `input_boolean.battery_naar_100` |
| Shore power relay/switch | `switch.walstroom_l` |

All of these can be re-pointed later from the integration's "Configure"
button, without removing/re-adding it.

## Entities this creates

- **`sensor.<name>_status`** - the main sensor. Its state is the exact same
  status string the original sensor used (`Standby`, `Start charge`,
  `Actief`, `Stop charge`, `Cooldown`, `Wait for start`, `100% mode`), so
  existing automations that match on those strings keep working once
  pointed at the new entity_id. It also carries the *entire* calculation
  (price arrays, the locked charge plan, projected SoC, etc.) as
  attributes - this is what the bundled dashboard cards read.
- A set of standalone **value sensors** for the figures most useful on
  their own: charge target %, projected end SoC, charge energy needed,
  planned charge energy (net and gross), start/stop time, time remaining,
  days since full charge, the live and window-start price limits, the
  window's own average price, battery/usable energy, and the hours still
  needed to reach full for a pending departure.
- A **`number` entity per tunable** (battery size, charger efficiency, SoC
  floor/ceiling, vacation band, flat-block margin/tolerance, calibration
  interval, balance voltage/tolerance/settle time, cooldown
  duration/SoC guard, voyage timing buffers, unplanned-session buffer,
  session energy cap, and the charging/drawing-power/high-load power
  thresholds) - see `const.py`'s `NUMBER_DEFINITIONS` for the full list
  and defaults (all seeded from the original's hardcoded values).

## Dashboard cards

The original `custom:houseboat-price-card` and `custom:houseboat-status-
card` are bundled in `frontend/houseboat-charge-manager-cards.js` and
auto-registered as a Lovelace resource the moment the integration is set
up - no manual resource step. Add a card with `type: custom:houseboat-
price-card` or `custom:houseboat-status-card` and `entity:` pointed at the
new status sensor (e.g. `sensor.houseboat_charge_manager_status`).

## What changed vs. a literal line-for-line port

Home Assistant's trigger-based template sensors have a quirk: every
`this.attributes.X` reference always resolves to the *previous* published
state, never to a value computed earlier in the same pass - even where the
two attributes sit next to each other in the file. In a few places (most
notably `is_voyage_charge_pending` <-> `is_full_charge_cycle` <->
`is_voyage_hold_active`) that's load-bearing: it's a genuine cycle that
only resolves because each side reads the other's previous-cycle value.

This port keeps that exact behaviour, but **only** for the things that are
genuinely stateful by design - the locks and latches explicitly described
as such in the original's own comments (the pending/charge-plan locks, the
voyage timers, the balance/calibration tracker, the cooldown timer, the
session-start snapshots). Everything else (the price search, the
flat-block extension, the schedule display, SoC/price projections) is
computed fresh every cycle in correct dependency order instead of
inheriting an extra 15-second lag purely as a side effect of how the old
template engine worked. Behaviourally this is equivalent for every
deliberate design decision in the comments, and strictly more responsive
everywhere else. See the top of `logic.py` for the exact reasoning, and
`PERSISTED_KEYS` there for exactly which values survive a Home Assistant
restart.

## Status

Built as a working skeleton first: the full calculation engine is ported
(see `logic.py`, a 1:1 behavioural port of every subsystem in the original
`charge_system_sensor.yaml`), config flow, number tunables, status + value
sensors, and the bundled dashboard cards are all in place and have been
smoke-tested standalone (multi-cycle simulation + edge cases, no Home
Assistant required - see the project notes for how). It has **not** been
run against a real Home Assistant instance yet - do that before trusting it
with the actual charger. Start with the entities picked correctly and the
`number` entities left at their defaults (which match the original's
hardcoded values exactly), watch the main status sensor's attributes for a
day or two alongside the old template sensor if you can run both in
parallel, and compare.
