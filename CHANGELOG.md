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

## [2026.10.30] - 2026-10-30

### Fixed
- **`soc_at_window_start` projected ABOVE the live SoC when the battery was
  already below the floor.** Timo's battery was at 30.1% (floor 40%), an
  imminent charge window (45 min out) was clearly going to be needed, but
  the price card showed it red/unfavorable and the SoC line jumped
  steeply up to the floor right before the window started, then stayed
  flat through the whole thing instead of rising. Root cause (`logic.py`):
  `soc_at_window_start = max(soc - drain_per_unit * units_ahead,
  soc_floor)` - the floor was meant as a believable lower bound on a
  short no-charging drain (if a pure drain would carry us past the floor,
  the below-floor logic would already have intervened first) - but that
  reasoning only holds when `soc` starts ABOVE the floor. Once `soc` is
  already below it (`is_below_soc_floor`), `max(..., soc_floor)` instead
  projects the SoC UPWARD to the floor regardless of how little time has
  passed, which is backwards for a plain drain projection. That one
  inflated value fed three things at once: `soc_at_window_start` itself
  (30.1% -> a nonsensical 40%), `price_limit_at_window_start` (computed
  as if SoC had already recovered to the floor, instead of the
  more-lenient-than-floor limit a still-below-floor SoC should get), and
  `is_soc_critical_at_window_start` (wrongly `False`, which suppressed
  the critical-SoC override the price card uses to force a window
  favorable regardless of price). Fixed by only applying the
  floor-as-lower-bound when `soc >= soc_floor`; below the floor the drain
  projection now continues naturally, bounded only at 0. Verified: the
  above-floor case (where the old clamp-to-floor behavior is actually
  correct) reproduces identically; the below-floor case now projects a
  realistic ~29.96% instead of 40%, `price_limit_at_window_start` lands
  within a cent of the live `price_limit` (as it should for a 45-minute
  gap) instead of artificially low, and `is_soc_critical_at_window_start`
  correctly flips to `True` - which is what actually fixes the price
  card: the critical override now kicks in, the window renders blue, and
  the SoC line rises smoothly through it instead of front-loading a fake
  jump and flatlining. Confirmed against Timo's own live sensor dump via
  the Playwright card-preview harness.

## [2026.10.26] - 2026-10-26

### Fixed
- **Price card: block B of a split session was always colored off block A's
  favorability, never its own.** Timo noticed on the live chart that the
  first (nearer) charge block was clearly going to be skipped - its average
  price doesn't clear `price_limit_at_window_start` - yet the card's
  projected price-limit/SoC curve still rose through it as if it would
  charge, and the second block was shown the same color as the first
  regardless of whether its own, much cheaper price would actually clear
  the limit by the time it starts. Root cause, both symptoms: `drawWindow`
  for block B never got its own color (it silently fell back to block A's
  `windowColor`), and the projected-SoC waypoints builder always assumed
  block A executes, so block B's baseline SoC - and therefore the limit it
  has to beat - was computed as if A had already charged, even when A's own
  check says it won't. Fixed by judging each block's favorability
  independently: block B's own average price (`split_b_average_price` /
  `pending_plan_b_average_price`) is now compared against the price limit
  projected at ITS OWN start, computed as if block A is skipped (straight
  drain through A's slot) rather than inheriting A's assumed rise. Whichever
  block(s) actually clear their own bar carry the full projected rise to
  `projected_end_soc`; a skipped block just drains like any other idle
  stretch. Verified against Timo's own live sensor dump (block A: 0.089
  avg vs. 0.0756 limit - unfavorable/red; block B: 0.0611 avg vs. 0.1045
  limit once correctly projected off the drained, not charged, baseline -
  favorable/blue) via the Playwright card-preview harness - matches exactly
  what Timo predicted by eye ("als ik de stijgende lijn doortrek alsof de
  eerste sessie niet bestaat gaat hij die [tweede] denk ik wel pakken").

## [2026.10.25] - 2026-10-25

### Added
- **New tunable: "Negative-price charging threshold"** (`price_negative_threshold`,
  default **-0.05 €/kWh**). Timo noticed the system had just started an
  opportunistic charge session purely because the live price ticked
  fractionally below zero, and asked for a bit more margin: "Die prijs
  moet wel iets verder negatief zijn" - any price under 0.0 was enough to
  trigger `is_price_negative` (and override the normal plan/stop
  conditions for as long as it held), which is noise-prone since price
  feeds can wobble a few tenths of a cent either side of zero. The check
  (`logic.py`, `is_price_negative`) now compares against this new number
  entity instead of a hardcoded `0.0`, so a real, worthwhile dip is
  required before forced charging kicks in - set it back to 0 to restore
  the old "any negative price" behavior. Shows up as a new number entity
  in Settings -> Devices & services -> Houseboat Charge Manager, grouped
  with the other live-decision thresholds (charging-detected, any-draw,
  high shore-load).

## [2026.10.24] - 2026-10-24

### Fixed
- **No, the engine did not correctly handle the DST "fall back" change**
  (Timo asked specifically, prompted by today's Nordpool data visibly
  having more price blocks than usual - the 2026-10-25 day genuinely has
  100 15-minute slots, not the usual 96, since local clocks repeat the
  02:00-03:00 hour). `interval_start_unit` - the position used to index
  into `price_today`/`price_tomorrow` for literally everything the engine
  decides - was computed as `now.hour * 4 + now.minute // 15 + 1`, naive
  wall-clock arithmetic that can't tell the repeated hour's two
  occurrences apart. From the transition onward for the rest of that day,
  this read a flat 1 hour (4 units) behind where it should, i.e. the
  engine would plan and judge prices against the PREVIOUS hour's slot
  instead of the real current one, self-correcting only at the next local
  midnight. Same root problem (a flat `UNITS_PER_DAY=96` assumed for every
  day) also affected `_fmt_unit_clock` (unit-to-clock-time display - units
  96-99, the long day's genuinely-still-today extra hour, rendered as
  "Morgen 00:00"-"00:45" instead of "23:00"-"23:45") and `_rebase_unit` (a
  locked plan surviving the night's own midnight would have been
  rebased by the wrong, flat 96 instead of the real 100).

  Fixed by switching all three to real-elapsed-time arithmetic off two
  genuine timezone-aware datetimes instead of flat unit-count assumptions
  - new `_units_since_midnight`/`_real_minutes_between`/`_add_real_minutes`
  helpers. Caught and fixed a second bug while building this: plain `-`/`+`
  on two AWARE datetimes with a fold-aware tzinfo (zoneinfo) is *not*
  reliably DST-correct in CPython either (confirmed empirically - it
  silently drops the extra folded hour), so these go through `.timestamp()`
  instead, which doesn't have that problem; naive (no-tzinfo) datetimes -
  used throughout this project's own test scripts - keep plain
  subtraction/addition, unaffected, with no DST concept to get right in
  the first place. Verified: a dedicated DST simulation script confirming
  correct unit counts/clock labels/rebasing across the actual 2026-10-25
  transition (including the ambiguous 02:00-03:00 hour's two distinct
  `fold` values), a full end-to-end `compute()` run with a realistic
  100-entry price array for that day confirming the right price slot gets
  read as "now", and a before/after diff against the previously-shipped
  logic.py across a wide spread of ordinary (non-DST) times and an
  ordinary midnight-crossing locked-plan scenario, confirming zero
  behavior change there.

  Known follow-up, NOT included here: the price chart's own x-axis still
  spaces price bars evenly by array index assuming a flat 96-per-day,
  while the "now" line and charge-window highlighting are positioned by
  real elapsed time - on this one day a year they can visually drift
  apart by up to an hour for the portion of the chart after the
  transition (self-corrects the next day). This is cosmetic only, not
  part of the planning engine the fixes above cover - flag it if you want
  that looked at separately.

## [2026.10.23] - 2026-10-24

### Fixed
- **Price card: the "Prijslimiet" projection line ignored the charge plan
  entirely.** It projected SoC forward with its own simple "always
  draining" formula - so while the SoC line correctly showed SoC rising
  during a planned charge window, the price-limit line kept climbing
  straight through that same window as if SoC were still dropping, even
  though price_limit is itself a function of SoC and should fall as SoC
  rises (a fuller battery lowers how much you're willing to pay). The two
  lines visibly disagreed about what SoC was doing at the same point in
  time (Timo noticed this comparing them side by side). Fixed by having
  both lines read from one shared set of projected-SoC waypoints (now ->
  drain to soc_at_window_start -> rise through the charge window(s),
  split proportionally for a split plan -> drain again to the end of the
  48h horizon) instead of each computing - and disagreeing on - its own.
  The price-limit line now dips during a planned charge window and rises
  again afterward, matching the SoC line's shape inverted, same as the
  sensor's own price_limit genuinely behaves.

## [2026.10.22] - 2026-10-24

### Fixed
- **Version bump only, no code change from 2026.10.21** - `v2026.10.19`,
  `v2026.10.20` and `v2026.10.21` all exist as real GitHub Releases, but all
  three ended up tagging the *same* commit as `v2026.10.18`: a silent
  `git commit` failure in `houseboat-charge-manager-sync-and-push.command`
  (now fixed, see its own history) meant the repo's actual pushed content
  never advanced past `.18` while the release step - which reads the
  version straight from the on-disk manifest.json, not from git - kept
  cutting new, correctly-numbered releases on top of that stale commit.
  HACS has no way to know those three release tags don't actually contain
  what their version number promises, and won't re-offer an "update" to a
  tag it's already seen - so this just mints a fresh, never-before-seen
  version number to get a release that's actually backed by the real
  `.21` content (the status-card timeline color fix + the stuck-setup-retry
  fix) onto GitHub where HACS will offer it.

## [2026.10.21] - 2026-10-24

### Fixed
- **A failed first refresh could leave the integration permanently stuck
  instead of retrying cleanly.** `async_setup_entry` forwards `number`/
  `sensor`/`switch` setup *before* the coordinator's first refresh (needed
  so the coordinator can read already-saved tunables from `number.py` on a
  reload), but didn't undo that if the first refresh then failed - e.g. a
  sensor still unavailable right after boot, or an exception inside
  `logic.compute()`. That failure normally becomes `ConfigEntryNotReady`,
  which Home Assistant retries automatically, but with the platforms left
  registered, the retry's own `async_forward_entry_setups` call immediately
  errored with `ValueError: ... has already been setup!` for all three
  platforms - replacing the real, retryable error with a permanent one
  (Timo hit this after updating: logs showed only the "already been setup"
  errors, with the actual first-refresh failure masked). `async_setup_entry`
  now unloads the platforms it just forwarded before re-raising, same
  cleanup `async_unload_entry` already does, so a bad cycle produces a
  normal retry (and a visible underlying error in the log) instead of a
  wedged entry.

## [2026.10.20] - 2026-10-24

### Fixed
- **Status card: the history timeline/status dot had charging (Actief) in
  red and Cooldown in blue** - backwards from the rest of the dashboard,
  where blue (`buy_color`) consistently means "charging" (the CHARGE
  tile, the price chart's planned/historical blue blocks). That swap was
  made in an earlier 2026-10 change specifically to match the red the
  CHARGE block/price chart use for an *unfavorable/forced* charge, but in
  practice it just made the timeline contradict the dashboard's own color
  language once seen side-by-side (Timo: "staan rood en blauw andersom?").
  Actief now uses the card's own `buy_color` (blue, same as everywhere
  else charging is shown); Cooldown gets its own muted amber instead of
  blue, so it stays visually distinct from both charging and plain idle;
  "100% mode" (forced/non-price-driven charging, e.g. a calibration
  cycle) keeps the vivid red, now correctly read as the one state that's
  actually non-favorable rather than as "charging" in general.

## [2026.10.19] - 2026-10-24

### Added
- **Price card: SoC line, now backed by real history before "now".**
  Shown as a preview first (purely a forward projection from the live
  SoC), then extended once it became clear a separate Victron SoC
  sensor with recorder history is actually available: the already-
  elapsed part of today is now the REAL measured SoC (solid line),
  joined at "now" to the same forward-looking projection as before
  (drain to the planned window, rise through it, drain again - see
  2026-10's own comment in the card), now drawn dashed to mark it as an
  estimate rather than a measurement, matching the price-limit line's
  existing dashed convention. New card option `soc_entity`: the
  recorder-tracked battery SoC sensor's entity_id - without it the card
  falls back to projection-only (dashed from "now" onward, nothing
  before it), same as before this entry. `show_soc` (default `true`)
  turns the whole line off; `soc_color` picks its color. Own 0/50/100%
  axis on the chart's right edge, own legend entry.

## [2026.10.18] - 2026-10-24

### Added
- **The price card now also shows today's already-finished charging
  sessions**, as the same shaded blue block (and bottom streak) the
  planned/live window already uses - not just the upcoming plan. Reads
  the sensor's own state history (the same recorder query the status
  card's timeline already uses) for today, keeps only the segments
  where the state was actually a charging state (`Actief`/`100% mode`),
  and draws one block per session, without its own time label (to avoid
  crowding the chart when several short sessions happened). Only
  today's portion is ever shown, since the chart's left edge is always
  local midnight - a historical block from before today would be off
  the left edge of the chart anyway. Re-checks every 5 minutes so a
  session that starts or stops while the card is open shows up without
  a reload. New card option `show_history` (default `true`) turns it
  off. Requested via this chat after shipping the look-ahead planning
  change, to also see where charging actually already happened today on
  the same chart.

## [2026.10.17] - 2026-10-24

### Changed
- **The look-ahead SoC projection from 2026.10.16 is no longer only shown
  on the dashboard - it now actually feeds the plan.** Until now, the
  window search/price-trim/split-search/flat-block-extension pipeline
  always sized itself from the LIVE charge_kwh/charge_time_units, even
  when the window it landed on sat far enough in the future that the
  battery would genuinely have drained to a lower SoC - and therefore
  had a bigger real energy gap to the target - by the time it actually
  started. That meant a long, flat/cheap stretch far ahead could get
  under-planned: the search would size for today's smaller gap, and the
  state machine's voltage-reached bypass would only discover the extra
  real need live, once charging, potentially running on past the cheap
  stretch into a pricier period. Now, after the search finds a
  candidate window, the projected SoC at that window's own start
  (`soc_at_window_start`'s own drain formula, unchanged) is used to
  recompute the energy need, and if that is meaningfully bigger than
  the live figure, the ENTIRE search/price-trim/split/extension
  pipeline is re-run with the bigger need - which can land on a
  longer, or even differently-placed, window, not merely a longer
  bolt-on extension of the same spot. Bounded to a handful of passes
  (self-limiting in every scenario tried - a pass only grows the need
  further if the window moved even further out than the previous pass
  already accounted for). New attributes exposed: `charge_kwh_planned`,
  `charge_time_units_planned` (what the pipeline actually searched
  with) and `charge_plan_lookahead_adjusted` (whether a correction
  happened at all this cycle). `charge_kwh`/`charge_time_units`
  themselves keep reporting the live, right-now snapshot - unchanged,
  still what `has_charge_need` and friends key off.
  `projected_charge_kwh_at_window_start` (2026.10.16) stays as the
  simple, dashboard-facing "what does the need look like at the
  window's current start" figure; once a plan is settled it now
  typically lands very close to `charge_kwh_planned`, acting as a nice
  cross-check between the two. Scope note, same as 2026.10.16's: this
  only projects the PRIMARY window (block A) - block B's start in a
  chosen split is not adjusted this way, since its true projected SoC
  would need to account for block A's own delivered energy plus drain
  during the pause, not simple linear drain from "now". Requested via
  this chat as a direct follow-up to 2026.10.16's informational-only
  preview, once it became clear the user wanted the bigger future need
  actually planned for, not just displayed.

## [2026.10.16] - 2026-10-24

### Added
- **Forward-looking preview of the charge need: `projected_charge_kwh_at_window_start`**
  (plus its supporting `projected_battery_energy_at_window_start`). Until now
  the dashboard only showed `charge_kwh`/`battery_energy`, both based on the
  LIVE SoC right now - accurate for what actually gets planned (nothing
  locks in until the window is imminent, at which point live SoC and SoC-at-
  start are practically the same value), but not very informative about what
  will actually happen if the chosen window sits far in the future and the
  battery keeps draining in the meantime. The existing `soc_at_window_start`
  projection (already used internally to feed the dynamic price ceiling) is
  now also used to compute what the energy gap/`charge_kwh` would look like
  AT that projected SoC, purely for display. This is informational only - it
  does **not** feed back into `charge_kwh`, `charge_time_units`, or the
  window search itself, which deliberately stay on the live SoC for the
  reason above. Scope note: this currently covers the primary window
  (block A) only, not block B's start in a split session - block B's true
  projected SoC would need a different calculation (accounting for the
  energy block A delivers plus drain during the pause in between, not a
  simple linear drain from "now"), which isn't implemented yet. Requested
  via this chat, after noticing a day with a long flat/cheap stretch where
  it would help to see the expected SoC at the moment of starting, not just
  right now.

## [2026.10.15] - 2026-10-24

### Changed
- **A full charge cycle's planned duration (`charge_time_units`) now
  includes the balance-settle hold time**, not just the existing
  calibration grid buffer. A calibration/voyage-hold cycle only ever
  completes once `is_balance_settled` (voltage full + cells balanced,
  held for `balance_settle_minutes`) - so for exactly this cycle type,
  that hold is a certainty, not a maybe, the moment the cycle starts.
  Previously only `calibration_grid_buffer_minutes` was added to the
  planned duration; `balance_settle_minutes` was left out entirely, so
  the window search (and, when a split is chosen, the split search) sized
  itself for too little time. The real session already ran longer when
  needed - the state machine's voltage-reached bypass (2026.10.11) keeps
  charging past the planned duration regardless of price until settled -
  but that only discovers the extra time live, after the planned window
  (and whatever cheap/flat stretch it sat in) has already passed, so the
  settle tail could land anywhere, cheap or not. Planning for it up front
  lets the search place the WHOLE required stretch - settle hold
  included - inside a cheap/flat period when one is available, instead of
  only the bulk-charge portion. New attribute `balance_settle_buffer_minutes`
  exposed alongside the existing `calibration_grid_buffer_minutes`, same
  gating (`is_full_charge_cycle`). Found and designed via this chat after
  looking at a live "Wait for start" dump on an unusually flat/cheap day -
  spotted that the planned window stopped well before the day's cheap
  stretch actually ended, which was being left entirely to chance.

## [2026.10.14] - 2026-10-23

### Changed
- **Flat-block extension for a chosen split now judges and extends each
  block on its own terms**, instead of only ever touching block A's
  backward direction (2026.10.13). Per the user's explicit design: each
  block gets its own average price and threshold (previously block B's
  own average was computed but silently never used for anything - see
  Fixed below); the block with the LOWER average is processed first and
  gets first claim on the one shared headroom budget; each block can
  extend in BOTH directions, including toward the other block, but
  extending toward the other block is always capped so a full cooldown
  period (the same `split_min_gap_units` the split search itself enforces)
  remains between them - that gap is the entire reason the split exists,
  so neither block may eat into it from either side. The pricier block is
  processed second, with its own "toward the other block" limit based on
  the cheap block's now-settled (possibly already-extended) boundary, not
  its original search position. New attributes exposed for block B,
  mirroring block A's existing ones: `split_b_forward_extension_units`,
  `split_b_backward_extension_units`, `split_b_extension_units`,
  `split_b_effective_start_index`, `split_b_planned_duration_units` - the
  dashboard cards and the session-lock-in reservation for block B now both
  read the EXTENDED window (previously they only ever saw the raw,
  un-extended search position).

### Fixed
- **A chosen split's headroom calculation only ever counted block A's own
  base energy, silently dropping block B's share** (2026.10.11/.13) -
  `base_charge_kwh` used `effective_duration_units` (block A's duration
  alone) even though a split's two base blocks together always total the
  full required `charge_time_units`/`charge_kwh`. This overstated how
  much "spare" headroom was left for flat-block extension (sometimes by
  the device's entire base charge), which is what let 2026.10.13's
  backward-extension fix appear to work in ad hoc testing - the headroom
  it was riding was partly fictitious. Also silently affected
  `planned_charge_kwh`/`projected_end_soc` (the dashboard's own "→ NN%"
  readout) for every split session, which never counted block B's energy
  at all until now.
- **`is_split_chosen` could flip to `true` on pure floating-point noise
  when the split and single-block costs were actually tied** - most
  visible with a flat or strongly step-shaped price curve (easy to get
  from a fixed/uniform tariff block, and exactly what several of this
  project's own test scenarios use), where many different placements of
  the "overflow" duration cost exactly the same and summation order alone
  can tip one a few ULPs below the other. When that happened, the chosen
  window silently got TRUNCATED to the spurious split's block A duration
  instead of the full required window - a real, if rare, mis-sized
  charging window, not just a flat-block-extension quirk. Fixed with a
  1e-6 margin on the split-vs-single-block cost comparison (far below any
  price difference that could ever be a genuine saving). Found via this
  project's own regression scripts while verifying the change above -
  several of them use flat background prices and had been silently
  hitting this since 2026.10.11 without anyone noticing, because the
  pre-2026.10.13 code simply skipped flat-block extension outright
  whenever `is_split_chosen` was true, which happened to mask the
  truncation's main visible symptom.
- **`split_b_average_price` was computed but never actually exposed** as
  a sensor attribute (2026.10.11) - only read internally via the local
  Python variable, so it never reached the dashboard or `extra_state_attributes`
  at all.

## [2026.10.13] - 2026-10-23

### Fixed
- **Flat-block extension (2026.10.11 split-session work) no longer skips the
  backward direction when a split is chosen.** Spotted by looking at a real
  dashboard render: a flat/cheap price stretch sitting right before block
  A's own start wasn't being ridden into, even though nothing about it was
  risky. The original 2026.10.11 scoping turned the whole extension off for
  a chosen split, reasoning that extending could eat into the
  deliberately-priced-out gap before block B - true for the *forward*
  direction (extending past block A's own end, toward the gap/peak), but
  the *backward* direction (extending earlier, before block A's start)
  never touches that gap at all. Forward extension stays off for a split;
  backward extension now keeps its full headroom budget, exactly as it
  already does for a normal (non-split) plan. Verified with a dedicated
  regression case (a flat-priced stretch placed immediately before a
  chosen split's block A) confirming `charge_window_backward_extension_units
  > 0` while `charge_window_forward_extension_units` stays `0`, plus a
  re-run of the existing split/non-split simulations showing no change
  in their outcomes.
- **`houseboat-price-card`: block A's and block B's "Laden gepland <time>"
  labels could overlap/run into each other** on a split plan when the two
  charge windows land close together on the time axis relative to the
  card's width (common on a narrow/mobile dashboard). Block B's label now
  sits on its own row, 12px below block A's, whenever a split is in play
  (reserving a taller label area up front instead of trying to measure
  rendered text width at render time), so the two never collide regardless
  of card width or how close the two blocks are.

## [2026.10.12] - 2026-10-23

### Added
- **The dashboard cards now show block B of a split-session plan**
  (2026.10.11), instead of only ever showing block A and leaving the split
  itself invisible on the dashboard:
  - `houseboat-price-card`: draws a second highlighted bar and "Laden
    gepland <time>" label for block B's own reserved slot, alongside
    block A's - the same three-tier fallback as block A (locked session
    plan -> locked pending plan -> live search preview), just reading
    `charge_plan_b_*`/`pending_plan_b_*`/`split_b_start_index` +
    `split_b_duration_units` instead.
  - `houseboat-status-card`: the CHARGE tile gets a slim extra line
    ("dan <start>–<stop>") under block A's own progress bar whenever a
    block B is pending - no second progress bar (block B isn't running
    yet, so there'd be nothing live to show), just enough to make the
    pause-and-resume visible without cluttering the tile.
  - A normal, non-split plan is entirely unaffected: `splitBWindow()`
    returns null and neither card renders anything extra.

## [2026.10.11] - 2026-10-23

### Added
- **Split-session charging: a required charging block can now be split in
  two around an expensive price peak**, instead of always charging one
  contiguous block straight through it. Example from a real price curve:
  charging 12:15-16:30 (18 units), skipping the 16:45-20:45 peak, then
  resuming 21:00-23:45 (12 units) came out 15.8% cheaper overall (avg
  €0.0993/unit) than the best single contiguous block covering the same
  duration (avg €0.1178/unit, forced through the peak). The search always
  splits once it's cheaper - no extra savings threshold - subject to two
  hard floors: each half must be at least 1 hour (4 units), and the gap
  between the two halves must be at least one full cooldown period
  (`cooldown_minutes`), so block B is never due before the reconnect
  lock-out from block A's stop has actually cleared. Both floors, and
  "always split when cheaper", were explicit choices made when this was
  discussed and confirmed.
  - Only considered in the same case where the search would otherwise
    charge the full required duration as one block (the ceiling has
    already justified charging at all) - not below the SoC floor (that
    mode deliberately ignores price nuance) and not combined with
    price-trimming or the flat-block extension (different, separate
    shortening/extension mechanisms this isn't meant to interact with -
    a chosen split skips the flat-block extension outright, since
    extending block A could eat into the deliberately-priced-out gap).
  - Block A's stop (reaching its own planned duration) is a deliberate
    pause, not the end of the plan - it fires a new `split_pause`
    notification reason (distinct from "geplande laadtijd verstreken")
    and, unlike a normal stop, isn't held back by a full-charge/
    calibration cycle's balance-confirmation guard (that guard protects
    against ending the *whole* plan before balance is confirmed - a
    split's pause isn't that, since block B resumes it). Block B's own
    completion is entirely ordinary once its reservation is consumed,
    including that same calibration/balance guard where it applies -
    confirmed with a simulated calibration cycle where block B correctly
    kept charging well past its own nominal duration, waiting for
    balance to settle, rather than stopping on duration alone.
  - Block B's reserved slot is decided once (alongside block A, when
    that plan locks in) and held fixed - including across a Home
    Assistant restart - through block A's charging and the pause after
    it, rather than being re-searched fresh every cycle during the gap
    (which has no notion of "resume the second half of an already-
    committed split" and could easily land on something else entirely
    once the peak itself has come and gone).

### Fixed
- **Session lock-in could commit to a different duration (and, in
  principle, start) than what had actually been pending**, on the one
  cycle a plan actually starts charging. The previous cycle's pending
  values are cleared (by design) before lock-in runs that same cycle, so
  lock-in fell back to a fresh recompute instead - usually harmless for
  the *start* position (by the time "now" reaches it, a fresh search
  naturally agrees), but not for *duration* whenever the search itself is
  position-dependent (price-trim, and now a chosen split): a single
  15-minute step can shift which partition looks optimal, so locking in
  with a fresh recompute could commit to a visibly different duration
  than the one actually pending and acted on a moment earlier - first
  seen as a chosen split committing 16 units instead of the 10 that had
  just been decided and started on. Lock-in now reads the actual
  previously-pending value (from persisted store) instead of
  re-deriving it fresh at the lock-in cycle itself.

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
