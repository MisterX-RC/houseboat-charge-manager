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

## [2026.10.10] - 2026-10-23

### Fixed
- **The price-ceiling window search could pick a more expensive window
  while a cheaper one sat earlier in the exact same horizon.** Each
  candidate start position is checked against its own SoC-projected
  ceiling, which loosens further out (more "urgency" assumed by then) -
  so a genuinely cheaper window early in the day could fail its own
  (stricter) ceiling and get skipped, while a pricier window later on,
  where the ceiling had since loosened, passed and got used instead.
  Confirmed with a real example: a 7.25h window at 10:45 averaging
  €0.1115 was rejected (ceiling €0.0997 at that position), while the
  session ended up starting at 16:45 instead, averaging a higher €0.1177
  (ceiling €0.1196 there) - even though €0.1115 was plainly the cheaper
  option and was sitting right there in the same search. Once *any*
  position's ceiling is satisfied anywhere in the horizon - i.e. charging
  at roughly that price is justified at all - the search now uses the
  outright cheapest full-duration window in the horizon instead of
  whichever one happened to be first to individually clear its own,
  position-dependent bar. The ceiling itself still fully decides *whether*
  to accept charging at all (that part is unchanged); it just no longer
  also decides *which* window, once it's decided yes.

## [2026.10.9] - 2026-10-20

### Fixed
- **A locked charge plan or pending plan that was still live past local
  midnight showed a wildly wrong remaining time and stop time - "22 hours
  left" right before it should have finished, a stop time stuck showing
  "tomorrow afternoon", or the remaining time briefly jumping UP instead
  of down.** `interval_start_unit` (everything "now"-relative is built
  from it) resets to a small number every local midnight, but
  `charge_plan_start_unit`/`pending_plan_start_unit`, once locked, were
  carried forward across cycles completely unchanged - so once the real
  clock crossed midnight while one was still active, it was being
  compared against a different day's frame than the one it was set in,
  with no adjustment. Each is now paired with its own reference date
  (`charge_plan_reference_date`/`pending_plan_reference_date`, both new,
  persisted attributes) and rebased by a whole number of days on every
  read, so the comparison stays correct no matter how many midnights a
  locked plan lives through - including a session that (as of 2026.10.7)
  can legitimately span more than a day.
- `start_charge_time`/`stop_charge_time` only ever handled "today" or
  "tomorrow" (anything else produced a nonsensical hour). They're now
  genuinely day-aware: today, "Morgen" for tomorrow, and an actual
  `dd-mm` date for anything further out or already in the past.

## [2026.10.8] - 2026-10-19

### Added
- **A second, lower session cap for when the battery is genuinely low.**
  Two new `number` entities:
  - *Session energy cap - SoC below floor* (default 10h) is used instead
    of the existing *Session energy cap* (now labelled *- SoC above
    floor*, unchanged, still default 30h) whenever the battery is below
    the existing *Price-ceiling SoC floor* setting (default 40%) -
    re-evaluated every cycle, not latched. This lets the two be tuned
    independently: comfortably larger above the floor, short on purpose
    below it.
  - *Search horizon - SoC below floor* (default 8h) caps how far ahead
    the price search is allowed to look while below the floor - or less,
    if the battery would hit `low_battery_percentage` sooner than that
    (same `units_left` calculation removed from the general-purpose
    horizon in 2026.10.5, reintroduced here scoped to this one case).
  - Below the floor, the average-price ceiling relaxation
    (`price_limited_start_index`/the price-trim search) is skipped
    entirely - deliberately: that mechanism exists to patiently trade a
    longer wait for a cheaper average price, which is the opposite of
    the point here. The plain cheapest full-duration window within the
    short horizon is used instead.
  - The flat-block extension is unchanged below the floor - a genuinely
    flat/cheap stretch can still extend a session past this new, shorter
    cap, same as it can above the floor.
  - At/above the floor, nothing changes: the existing cap, full horizon,
    and price-ceiling relaxation all behave exactly as before.

## [2026.10.7] - 2026-10-19

### Fixed
- **The flat-block extension was no longer allowed to exceed the session
  cap - that was wrong.** 2026.10.6 bounded the extension's headroom to
  the session-capped `charge_kwh`, on the mistaken assumption that
  exceeding the cap was itself the bug. It isn't: the whole point of the
  flat-block extension is that when prices stay flat/cheap for an
  extended stretch, charging more right now - rather than waiting for
  another day's separately-capped session - is the efficient thing to
  do. Headroom is measured again against the remaining gap to the SoC
  target, as it was before 2026.10.6, not against the session cap. The
  session cap still fully governs the *base* window the search picks
  (so a low battery still doesn't force one huge session trying to hit
  90%/100% in a single day); it's only the opportunistic extension on
  top of that base window - which only grows into neighbouring
  similarly-priced units in the first place - that's allowed to push
  past it. One genuine refinement survives from 2026.10.6: headroom is
  measured from what the base window *actually* delivers (which can be
  slightly less than `charge_kwh` when price-trimming shortened it),
  not from `charge_kwh` directly - that part was a real, if minor,
  correctness improvement.

## [2026.10.6] - 2026-10-18

### Fixed
- **A flat/uniformly-cheap stretch in the price table could blow straight
  through the Session energy cap.** The forward/backward "flat-block
  extension" (which opportunistically widens the charge window into
  neighbouring similarly-priced units) computed its available headroom
  against the full gap to the SoC target, not against the session cap -
  so once the chosen window sat inside a long flat-priced block, it kept
  extending into it well past what the session cap was supposed to
  allow (confirmed with a synthetic 10-hour flat block: a 10h session
  cap still produced a planned 10-hour, 15.68 kWh session). It now
  extends only to close the gap between what the (possibly
  price-trimmed) base window already delivers and the already-decided,
  session-capped `charge_kwh` - the same 10h-cap scenario now stays at
  the capped 4.7 kWh. A real voyage deadline (`is_voyage_charge_forced`)
  is unaffected - that path was never capped and still extends toward
  the full target, as it must.

## [2026.10.5] - 2026-10-18

### Fixed
- **The cheapest-window search could latch onto a misleadingly cheap
  sliver instead of a genuinely well-priced placement.**
  `interval_price[idx:idx+duration]` silently truncates near the end of
  the price data instead of failing, so a tiny 1-2 unit window right at
  the edge of the search horizon was being compared, average-for-average,
  against real full-length placements - and, being an average of only its
  very cheapest couple of units, usually "won" even though it didn't
  represent an actual placement for a session that needs `duration` units
  of charging. Both search passes (the plain cheapest window and the
  price-ceiling-filtered one) now require a full-length window.
- **The search horizon no longer shrinks just because the battery is
  below `soc_floor_percent`.** It used to stop looking ahead at
  `units_left` (time until `usable_energy` hits `low_battery_percentage`)
  - meaning a battery that was simply below the 40%-ish comfort zone, but
  nowhere near truly critical, gave up on finding a cheaper price further
  out and grabbed whatever was "good enough" nearby. The horizon is now
  everything the price sensor currently knows (today + tomorrow, once
  published); the existing price-ceiling relaxation (which already eases
  the acceptable price as the projected SoC nears `soc_floor_percent`)
  is what keeps this safe - it still picks up a nearby acceptable window
  well before `low_battery_percentage` is actually at risk.

Together, these two fixes mean a low-battery charge no longer forces one
long session dragged across both a cheap and an expensive period - it
finds the real cheapest reasonably-sized window instead. For spreading a
big catch-up charge (e.g. after a calibration cycle) across several
*separate*, shorter sessions over several days rather than one long one,
lower the existing **Session energy cap (hours of avg load)** number
(default 30h) - a smaller session is much more likely to fit entirely
inside a single day's cheap window, and the planner will naturally need
more days to close the same gap.

## [2026.10.4] - 2026-10-16

### Added
- **"Auto-trigger connect/disconnect scripts" switch.** A new `switch`
  entity turns calling the configured aan/afkoppel scripts (and sending
  their notification) on or off, without having to clear the script
  fields in Reconfigure. Defaults to on, so existing installs keep
  working exactly as before. Everything else (system_status, the
  calculation itself) keeps running as normal while it's off - only the
  script call and its notification are skipped.

### Changed
- `.github/workflows/validate.yaml` no longer runs the HACS default-list
  validation job (license/topics/brand-asset checks) - those only matter
  for submitting this repo to the public HACS store, not for installing
  it as a "Custom repository", which was producing a permanently red
  check for no actionable reason. Hassfest validation is unaffected.

## [2026.10.3] - 2026-10-16

### Added
- **Aan-/afkoppel-scripts and notifications.** Two new optional fields in
  the config flow (and Reconfigure) let you pick a script to run when
  shore power should connect, and one for when it should disconnect, plus
  one or more `notify.*` targets to message when either happens. Neither
  script needs to know *why* it was called - it just connects or
  disconnects; the integration sends a separate notification (title
  "Walstroom - Aankoppelen"/"Walstroom - Afkoppelen", message starting
  with "Aankoppelen:"/"Afkoppelen:") with the actual reason. All three
  fields are optional - leaving them unset just means no script is
  called and nothing is notified, everything else is unaffected.
- The scripts fire on the exact cycle `system_status` enters "Start
  charge"/"100% mode" (connect) or "Stop charge" (disconnect), not
  continuously while it stays there. Because shore power physically
  connects on its own for safety the moment the cable is plugged in
  (independent of these scripts), this can go straight to "Stop charge"
  with no prior "Start charge" at all - e.g. plugging in with no charge
  need - and that's by design: the disconnect script is exactly what
  undoes an unwanted automatic connection.
- 10 reasons are recognised: force-100% override, negative price, a
  pending departure, vacation mode below its floor, a planned cheap
  window, critical SoC (connect); the vacation target, the unplanned
  target, a completed/balanced session, and an elapsed planned duration
  (disconnect).

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
