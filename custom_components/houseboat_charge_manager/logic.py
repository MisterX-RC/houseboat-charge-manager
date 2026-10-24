"""Pure-Python port of the original ``sensor.ess_charge_manager`` trigger
template sensor (charge_system_sensor.yaml).

Design notes (read before changing anything here)
---------------------------------------------------
The original ran as a Home Assistant trigger-based template sensor, which
has a well-known quirk: every ``this.attributes.X`` reference inside a
template always resolves to the PREVIOUS published state, never to a value
computed earlier in the same evaluation pass - even where the two
attributes are textually adjacent. In several places that quirk is load
-bearing (e.g. ``is_voyage_charge_pending`` <-> ``is_full_charge_cycle`` <->
``is_voyage_hold_active`` form a genuine cycle that only resolves because
each side reads the other's *previous* value).

This port keeps that exact one-cycle-lag behaviour, but only where it is
genuinely meaningful: the long-lived locks and latches (voyage timers, the
balance/calibration tracker, the pending/charge-plan locks, the cooldown
timer). Those read ``prev`` (``P`` below) - the previous cycle's full
result dict - exactly where the original read ``this.attributes.X``. Purely
derivational, non-self-referencing values (the price search, the flat-block
extension, the schedule display, projected SoC) are instead computed fresh
every cycle in correct dependency order, which removes a lot of pointless
extra 15-second lag the original's template-engine quirk forced on EVERY
inter-attribute dependency, not just the deliberate ones - this is strictly
more responsive and behaviourally equivalent for anything that isn't an
intentional latch.

``compute()`` is a pure function: given "now", the live entity readings,
the tunables and the previous cycle's result dict, it returns the new
result dict. The coordinator is responsible for reading entities, loading/
persisting the small subset of that dict that must survive a Home
Assistant restart, and calling this function every UPDATE_INTERVAL_SECONDS.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Optional

from .const import (
    NUM_BALANCE_SETTLE_MINUTES,
    NUM_BALANCE_VOLTAGE_THRESHOLD,
    NUM_BATTERY_SIZE_KWH,
    NUM_CALIBRATION_GRID_BUFFER_MINUTES,
    NUM_CALIBRATION_INTERVAL_DAYS,
    NUM_CELL_BALANCE_TOLERANCE,
    NUM_CHARGER_EFFICIENCY_PERCENT,
    NUM_COOLDOWN_MINUTES,
    NUM_COOLDOWN_SOC_GUARD_PERCENT,
    NUM_FLAT_BLOCK_MARGIN,
    NUM_FLAT_BLOCK_TOLERANCE,
    NUM_HAS_CHARGE_NEED_THRESHOLD_KWH,
    NUM_HIGH_SHORE_LOAD_THRESHOLD_W,
    NUM_IS_CHARGING_THRESHOLD_W,
    NUM_IS_DRAWING_ANY_POWER_THRESHOLD_W,
    NUM_LOW_SOC_SEARCH_HORIZON_HOURS,
    NUM_SESSION_CAP_HOURS,
    NUM_SESSION_CAP_LOW_SOC_HOURS,
    NUM_SOC_CEILING_PERCENT,
    NUM_SOC_CRITICAL_PERCENT,
    NUM_SOC_FLOOR_PERCENT,
    NUM_UNPLANNED_TARGET_BUFFER_PERCENT,
    NUM_VACATION_CEILING_PERCENT,
    NUM_VACATION_DEPARTURE_BUFFER_HOURS,
    NUM_VACATION_DEPARTURE_TARGET_DAYS,
    NUM_VACATION_FLOOR_PERCENT,
    NUM_VOYAGE_APPROACH_BUFFER_HOURS,
    NUM_VOYAGE_IDLE_SETTLE_HOURS,
    NUM_VOYAGE_POST_DEADLINE_HOLD_HOURS,
    NUM_VOYAGE_SETTLE_MARGIN_HOURS,
    REASON_CHARGE_COMPLETE,
    REASON_DURATION_ELAPSED,
    REASON_FORCE_FULL_CHARGE,
    REASON_PLANNED_WINDOW,
    REASON_PRICE_NEGATIVE,
    REASON_SOC_CRITICAL,
    REASON_SPLIT_PAUSE,
    REASON_UNPLANNED_TARGET_REACHED,
    REASON_VACATION_LOW_SOC,
    REASON_VACATION_TARGET_REACHED,
    REASON_VOYAGE_DEADLINE,
    STATUS_100_PERCENT_MODE,
    STATUS_ACTIEF,
    STATUS_COOLDOWN,
    STATUS_STANDBY,
    STATUS_START_CHARGE,
    STATUS_STOP_CHARGE,
    STATUS_WAIT_FOR_START,
)

UNIT_MINUTES = 15
UNITS_PER_HOUR = 60 // UNIT_MINUTES  # 4
UNITS_PER_DAY = 24 * UNITS_PER_HOUR  # 96

# Split-session minimum block length (2026.10.11): "we moeten een minimale
# charge tijd handhaven van een uur" - a hard safety/sanity floor on each
# half of a split, not a day-to-day tunable, so it's a constant rather than
# a number entity.
MIN_SPLIT_BLOCK_UNITS = UNITS_PER_HOUR

# Both mean "we want shore power connected" - "100% mode" bypasses the
# normal charge-need logic entirely, so it must trigger the connect script
# itself rather than relying on also reaching "Start charge".
_CONNECT_STATUSES = (STATUS_START_CHARGE, STATUS_100_PERCENT_MODE)

# Keys whose previous-cycle value must survive a Home Assistant restart.
# Excludes the large per-cycle arrays (all_price/interval_price/
# best_interval_price), which are cheap to recompute fresh on the first
# refresh and not meaningful to persist.
PERSISTED_KEYS = (
    "voyage_idle_since",
    "is_voyage_charge_pending",
    "voyage_completed_since",
    "charge_plan_is_full_charge_cycle",
    "charge_plan_voltage_target_reached",
    "balance_confirmed_since",
    "last_full_charge_completed_at",
    "is_charge_complete",
    "pending_plan_start_unit",
    "pending_plan_reference_date",
    "pending_plan_duration_units",
    "pending_plan_average_price",
    "pending_plan_target_soc",
    "pending_plan_b_start_unit",
    "pending_plan_b_reference_date",
    "pending_plan_b_duration_units",
    "pending_plan_b_average_price",
    "charge_plan_start_unit",
    "charge_plan_reference_date",
    "charge_plan_duration_units",
    "charge_plan_end_unit",
    "charge_plan_price_limit",
    "charge_plan_target_soc",
    "charge_plan_b_start_unit",
    "charge_plan_b_reference_date",
    "charge_plan_b_duration_units",
    "is_planned_charge_start",
    "charge_session_ended_at",
    "is_charging",
    "is_within_post_charge_lock",
    "is_grid_unavailable",
    "system_status",
)

_DATETIME_KEYS = (
    "voyage_idle_since",
    "voyage_completed_since",
    "balance_confirmed_since",
    "last_full_charge_completed_at",
    "charge_session_ended_at",
)


def _avg(values) -> float:
    values = list(values)
    return mean(values) if values else 0.0


def _is_plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt_unit_clock(unit: int, now: datetime) -> str:
    """Render a 15-minute unit index (0 = midnight of `now`'s date) as a
    clock time, with a day-aware prefix. Uses real calendar-date math
    (divmod against `now`'s date), not a single "past unit 96 = tomorrow"
    check - that only ever handled one day ahead and silently produced
    garbage for anything further out (now a real possibility - see the
    flat-block extension) or for a unit that's actually in the past (a
    locked plan read after it should already be rebased - see
    _rebase_unit - but this stays correct even if it somehow isn't).
    """
    day_offset, unit_of_day = divmod(unit, UNITS_PER_DAY)
    hour = unit_of_day // UNITS_PER_HOUR
    minute = (unit_of_day - hour * UNITS_PER_HOUR) * UNIT_MINUTES
    if day_offset == 0:
        prefix = ""
    elif day_offset == 1:
        prefix = "Morgen "
    else:
        prefix = f"{(now.date() + timedelta(days=day_offset)).strftime('%d-%m')} "
    return f"{prefix}{hour:02d}:{minute:02d}"


def _rebase_unit(value: Optional[float], reference_date_str: Optional[str], now: datetime) -> Optional[float]:
    """Shift a persisted, day-relative 15-minute unit position (0 =
    midnight of `reference_date_str`) so it's expressed relative to
    midnight of `now`'s date instead.

    `interval_start_unit` (and therefore every freshly-computed position -
    live_start_unit, the search's start_index, ...) is always relative to
    TODAY's midnight and resets to a small number every local midnight.
    `pending_plan_start_unit`/`charge_plan_start_unit`, once locked, are
    instead carried forward unchanged cycle to cycle while a plan is
    pending or a session is charging - so without this rebase, a plan or
    session that's still live past midnight gets compared against a
    completely different day's frame: `interval_start_unit` has reset to
    ~1, but the locked position hasn't, so e.g. "remaining time" can
    suddenly jump UP by about a day's worth of units, and the displayed
    stop time can show the wrong day. Rebasing on every read (not just
    once) keeps this correct across any number of consecutive midnights.
    """
    if value is None:
        return None
    if not reference_date_str:
        return value
    try:
        reference_date = date.fromisoformat(reference_date_str)
    except (TypeError, ValueError):
        return value
    days_elapsed = (now.date() - reference_date).days
    return value if days_elapsed == 0 else value - days_elapsed * UNITS_PER_DAY


def _best_two_block_split(
    prices: list[float],
    horizon: int,
    total_duration: int,
    min_block_units: int,
    min_gap_units: int,
) -> Optional[tuple[float, int, int, int, int]]:
    """Find the cheapest way to deliver `total_duration` units of charging
    as two separated, non-overlapping full-length blocks within
    `prices[0:horizon]` - e.g. to charge now, pause across an expensive
    price peak, then resume - instead of one contiguous block.

    Both blocks must be at least `min_block_units` long (the "minimale
    charge tijd van een uur" floor) and at least `min_gap_units` apart (the
    cooldown period, so the pause is never shorter than the real
    reconnect lock-out - see REASON_SPLIT_PAUSE in the state machine).

    Returns (total_cost, s1, l1, s2, l2) for the cheapest valid split
    found, or None if no valid split exists (duration too short to split
    at all, or the horizon has no room for both blocks plus the gap).
    Cost is compared as a plain SUM of unit prices (not an average) - valid
    because every unit delivers the same fixed energy, and both the split
    and the single-block alternative it's compared against cover the same
    total number of units, so summing is equivalent to averaging and
    simpler to combine across two blocks.
    """
    if total_duration < 2 * min_block_units or horizon < total_duration + min_gap_units:
        return None

    prefix = [0.0] * (horizon + 1)
    for i in range(horizon):
        prefix[i + 1] = prefix[i] + prices[i]

    def window_sum(start: int, length: int) -> float:
        return prefix[start + length] - prefix[start]

    best: Optional[tuple[float, int, int, int, int]] = None
    for l1 in range(min_block_units, total_duration - min_block_units + 1):
        l2 = total_duration - l1
        if l1 > horizon or l2 > horizon:
            continue
        max_s2 = horizon - l2
        if max_s2 < 0:
            continue
        # Suffix-min of block-2's cost over every valid start s2, computed
        # once per l1 so the s1 loop below can look up "cheapest valid
        # block 2 starting at or after here" in O(1).
        suffix_min_cost = [float("inf")] * (max_s2 + 2)
        suffix_min_idx = [-1] * (max_s2 + 2)
        for s2 in range(max_s2, -1, -1):
            cost2 = window_sum(s2, l2)
            if cost2 < suffix_min_cost[s2 + 1]:
                suffix_min_cost[s2] = cost2
                suffix_min_idx[s2] = s2
            else:
                suffix_min_cost[s2] = suffix_min_cost[s2 + 1]
                suffix_min_idx[s2] = suffix_min_idx[s2 + 1]

        max_s1 = horizon - l1
        for s1 in range(max_s1 + 1):
            s2_floor = s1 + l1 + min_gap_units
            if s2_floor > max_s2:
                continue
            total_cost = window_sum(s1, l1) + suffix_min_cost[s2_floor]
            if best is None or total_cost < best[0]:
                best = (total_cost, s1, l1, suffix_min_idx[s2_floor], l2)
    return best


def serialize_prev(result: dict[str, Any]) -> dict[str, Any]:
    """Project a result dict down to the Store-safe, restart-persisted subset."""
    out: dict[str, Any] = {}
    for key in PERSISTED_KEYS:
        if key not in result:
            continue
        value = result[key]
        if key in _DATETIME_KEYS and isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def deserialize_prev(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Inverse of serialize_prev, for loading from Store at startup."""
    if not stored:
        return {}
    out: dict[str, Any] = {}
    for key, value in stored.items():
        if key in _DATETIME_KEYS and isinstance(value, str):
            try:
                out[key] = datetime.fromisoformat(value)
            except ValueError:
                out[key] = None
        else:
            out[key] = value
    return out


def compute(
    *,
    now: datetime,
    soc: float,
    voltage: float,
    cell_max_v: float,
    cell_min_v: float,
    price_today: list[float] | None,
    price_tomorrow: list[float] | None,
    price_current: float,
    price_14d_average: float,
    shore_power_w: float,
    avg_load_w: float,
    walstroom_requested_amps: float,
    start_voyage_hours_raw: Any,
    low_battery_percentage: float,
    is_vacation_mode: bool,
    is_force_full_charge: bool,
    is_grid_unavailable: bool,
    tunables: dict[str, float],
    prev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run one full evaluation cycle. Returns the complete new result dict
    (everything a sensor might want to expose, plus the latches that
    ``serialize_prev`` will persist for next time).
    """
    P: dict[str, Any] = prev or {}
    T = tunables
    r: dict[str, Any] = {}

    def pf(key: str, default: float = 0.0) -> float:
        value = P.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def pb(key: str, default: bool = False) -> bool:
        value = P.get(key, default)
        return bool(value) if value is not None else default

    def pnum_or_none(key: str) -> Optional[float]:
        value = P.get(key)
        return value if _is_plain_number(value) else None

    def pdt(key: str) -> Optional[datetime]:
        value = P.get(key)
        return value if isinstance(value, datetime) else None

    # ---------------------------------------------------------------
    # BATTERY CAPACITY
    # ---------------------------------------------------------------
    battery_size = float(T[NUM_BATTERY_SIZE_KWH])
    r["battery_size"] = battery_size
    battery_energy = round(battery_size * (soc / 100), 2)
    r["battery_energy"] = battery_energy
    low_battery_energy = round(battery_size * (low_battery_percentage / 100), 2)
    r["low_battery_energy"] = low_battery_energy
    usable_energy = round(max(battery_energy - low_battery_energy, 0.0), 2)
    r["usable_energy"] = usable_energy

    units_of_runway = round((usable_energy / (avg_load_w / 1000)) * 4) if avg_load_w > 0 else 0
    units_elapsed_today = math.ceil(now.hour * 4 + now.minute / 15)
    units_left_today = (UNITS_PER_DAY - units_elapsed_today) - 8
    if units_of_runway < 1:
        units_left = 1
    elif now.hour < 14 and units_of_runway > units_left_today:
        units_left = units_left_today
    elif units_of_runway > (units_left_today + UNITS_PER_DAY):
        units_left = units_left_today + UNITS_PER_DAY
    else:
        units_left = units_of_runway
    r["units_left"] = units_left

    # ---------------------------------------------------------------
    # VACATION DEPARTURE TARGET (dynamic day-based target that replaces
    # the fixed vacation ceiling - computed early because the voyage
    # timing section below needs it). vacation_departure_target_days is a
    # whole number of days of usable energy (above the low-battery floor)
    # required at departure time; the default (15) is chosen to always
    # saturate to 100%, so a lower, limited target is an opt-in. The
    # buffer hours are added on top as extra kWh, to cover consumption
    # during the buffer window between "charging finished" and the actual
    # departure (the matching time-shift lives in DYNAMIC VOYAGE TIMING).
    # ---------------------------------------------------------------
    vacation_departure_target_days = float(T[NUM_VACATION_DEPARTURE_TARGET_DAYS])
    vacation_departure_buffer_hours = float(T[NUM_VACATION_DEPARTURE_BUFFER_HOURS])
    r["vacation_departure_target_days"] = vacation_departure_target_days
    r["vacation_departure_buffer_hours"] = vacation_departure_buffer_hours

    percent_per_hour_of_load = (((avg_load_w / 1000) / battery_size) * 100) if battery_size > 0 else 0.0
    required_runway_percent = vacation_departure_target_days * 24 * percent_per_hour_of_load
    departure_buffer_percent = vacation_departure_buffer_hours * percent_per_hour_of_load
    raw_vacation_departure_target_percent = low_battery_percentage + required_runway_percent + departure_buffer_percent
    vacation_departure_target_percent = round(
        min(max(raw_vacation_departure_target_percent, low_battery_percentage), 100.0), 2
    )
    r["vacation_departure_target_percent"] = vacation_departure_target_percent

    # ---------------------------------------------------------------
    # SIMPLE LIVE DECISION FLAGS
    # ---------------------------------------------------------------
    is_drawing_any_power = shore_power_w > T[NUM_IS_DRAWING_ANY_POWER_THRESHOLD_W]
    is_charging = shore_power_w > T[NUM_IS_CHARGING_THRESHOLD_W]
    r["is_drawing_any_power"] = is_drawing_any_power
    r["is_charging"] = is_charging
    r["is_grid_unavailable"] = is_grid_unavailable
    is_100_percent_mode = is_force_full_charge
    r["is_100_percent_mode"] = is_100_percent_mode
    is_price_negative = price_current < 0.0
    r["is_price_negative"] = is_price_negative

    # ---------------------------------------------------------------
    # CALIBRATION TRACKING (reads the latch as of the END of the previous
    # cycle - last_full_charge_completed_at is (re)written further down)
    # ---------------------------------------------------------------
    prev_last_full_charge = pdt("last_full_charge_completed_at")
    if prev_last_full_charge is not None:
        days_since_full_charge = round((now - prev_last_full_charge).total_seconds() / 86400, 1)
        is_calibration_cycle = days_since_full_charge >= T[NUM_CALIBRATION_INTERVAL_DAYS]
    else:
        days_since_full_charge = None
        is_calibration_cycle = True
    r["days_since_full_charge"] = days_since_full_charge
    r["is_calibration_cycle"] = is_calibration_cycle

    # ---------------------------------------------------------------
    # VOYAGE / VACATION
    # ---------------------------------------------------------------
    r["is_vacation_mode"] = is_vacation_mode
    try:
        start_voyage_hours = int(start_voyage_hours_raw)
    except (TypeError, ValueError):
        start_voyage_hours = 0
    r["start_voyage_hours"] = start_voyage_hours
    is_voyage_pending = start_voyage_hours > 0
    r["is_voyage_pending"] = is_voyage_pending

    # Edge-triggered (not level-triggered): the departure countdown can
    # legitimately sit at 0 for a long time while shore power stays
    # connected and nothing is pending, so the reset may only fire the
    # instant the grid actually becomes unavailable - not continuously
    # for as long as both conditions happen to hold.
    was_grid_unavailable = pb("is_grid_unavailable", False)
    should_reset_vacation_departure_target_days = (
        is_grid_unavailable and not was_grid_unavailable and start_voyage_hours == 0
    )
    r["should_reset_vacation_departure_target_days"] = should_reset_vacation_departure_target_days

    prev_voyage_idle_since = pdt("voyage_idle_since")
    if is_voyage_pending:
        voyage_idle_since = None
    else:
        voyage_idle_since = prev_voyage_idle_since if prev_voyage_idle_since is not None else now
    r["voyage_idle_since"] = voyage_idle_since
    is_voyage_idle_settled = (
        (now - voyage_idle_since) >= timedelta(hours=T[NUM_VOYAGE_IDLE_SETTLE_HOURS])
        if voyage_idle_since is not None
        else False
    )
    r["is_voyage_idle_settled"] = is_voyage_idle_settled

    # ---------------------------------------------------------------
    # CHARGE RATE (computed early: dynamic_voyage_charge_hours needs it)
    # ---------------------------------------------------------------
    charger_efficiency = float(T[NUM_CHARGER_EFFICIENCY_PERCENT]) / 100.0
    r["charger_efficiency"] = charger_efficiency
    battery_charge_speed = round(((walstroom_requested_amps * 220) - avg_load_w) / 1000, 2)
    r["battery_charge_speed"] = battery_charge_speed

    # ---------------------------------------------------------------
    # DYNAMIC VOYAGE TIMING
    # ---------------------------------------------------------------
    # In vacation mode, aim the deadline math at the real (possibly lower)
    # day-based target instead of always assuming a full 100% charge -
    # otherwise a deliberately lower target would still force charging to
    # start as early as a full charge would, defeating the point of
    # setting it lower. Outside vacation mode this is unchanged (always
    # a full charge).
    if is_vacation_mode:
        voyage_target_energy = round(battery_size * (vacation_departure_target_percent / 100), 2)
    else:
        voyage_target_energy = battery_size
    kwh_to_full = max(voyage_target_energy - battery_energy, 0.0)
    charge_hours = (kwh_to_full / battery_charge_speed) if battery_charge_speed > 0 else 0.0
    voyage_settle_margin_hours = T[NUM_VOYAGE_SETTLE_MARGIN_HOURS] + (
        vacation_departure_buffer_hours if is_vacation_mode else 0.0
    )
    dynamic_voyage_charge_hours = round(charge_hours + voyage_settle_margin_hours, 2)
    r["dynamic_voyage_charge_hours"] = dynamic_voyage_charge_hours

    voyage_approach_trigger_hours = dynamic_voyage_charge_hours + T[NUM_VOYAGE_APPROACH_BUFFER_HOURS]
    r["voyage_approach_trigger_hours"] = voyage_approach_trigger_hours

    is_voyage_final_due = is_voyage_pending and (start_voyage_hours <= dynamic_voyage_charge_hours)
    r["is_voyage_final_due"] = is_voyage_final_due

    is_voyage_approach_due = (
        is_voyage_pending and not is_voyage_final_due and (start_voyage_hours <= voyage_approach_trigger_hours)
    )
    r["is_voyage_approach_due"] = is_voyage_approach_due

    # is_voyage_charge_pending <-> is_charge_complete is a genuine cycle in
    # the original - broken, as there, by reading the PREVIOUS cycle's
    # is_charge_complete rather than trying to compute it fresh here.
    was_voyage_charge_pending = pb("is_voyage_charge_pending", False)
    prev_is_charge_complete = pb("is_charge_complete", False)
    is_voyage_charge_pending = (
        True if is_voyage_final_due else (was_voyage_charge_pending and not prev_is_charge_complete)
    )
    r["is_voyage_charge_pending"] = is_voyage_charge_pending

    is_voyage_charge_just_completed = was_voyage_charge_pending and prev_is_charge_complete
    r["is_voyage_charge_just_completed"] = is_voyage_charge_just_completed

    prev_voyage_completed_since = pdt("voyage_completed_since")
    if is_voyage_charge_just_completed:
        voyage_completed_since = now
    elif prev_voyage_completed_since is not None and (now - prev_voyage_completed_since) < timedelta(
        hours=T[NUM_VOYAGE_POST_DEADLINE_HOLD_HOURS]
    ):
        voyage_completed_since = prev_voyage_completed_since
    else:
        voyage_completed_since = None
    r["voyage_completed_since"] = voyage_completed_since

    is_within_post_deadline_hold = voyage_completed_since is not None
    r["is_within_post_deadline_hold"] = is_within_post_deadline_hold
    is_voyage_hold_active = is_voyage_charge_pending or is_within_post_deadline_hold
    r["is_voyage_hold_active"] = is_voyage_hold_active
    is_full_charge_cycle = is_calibration_cycle or is_voyage_hold_active
    r["is_full_charge_cycle"] = is_full_charge_cycle

    # ---------------------------------------------------------------
    # SESSION-LOCKED "is this a full-charge cycle" SNAPSHOT
    # ---------------------------------------------------------------
    prev_charge_plan_fcc = P.get("charge_plan_is_full_charge_cycle")
    if is_charging:
        charge_plan_is_full_charge_cycle = (
            prev_charge_plan_fcc if prev_charge_plan_fcc is not None else is_full_charge_cycle
        )
    else:
        charge_plan_is_full_charge_cycle = None
    r["charge_plan_is_full_charge_cycle"] = charge_plan_is_full_charge_cycle
    effective_full_charge_cycle = (
        charge_plan_is_full_charge_cycle if charge_plan_is_full_charge_cycle is not None else is_full_charge_cycle
    )

    is_voltage_full = voltage > T[NUM_BALANCE_VOLTAGE_THRESHOLD]
    r["is_voltage_full"] = is_voltage_full

    prev_voltage_target_reached = P.get("charge_plan_voltage_target_reached")
    if is_charging:
        charge_plan_voltage_target_reached = True if (bool(prev_voltage_target_reached) or is_voltage_full) else False
    else:
        charge_plan_voltage_target_reached = None
    r["charge_plan_voltage_target_reached"] = charge_plan_voltage_target_reached

    is_voyage_charge_forced = is_voyage_hold_active or is_voyage_approach_due
    r["is_voyage_charge_forced"] = is_voyage_charge_forced

    # ---------------------------------------------------------------
    # VACATION BAND
    # ---------------------------------------------------------------
    # vacation_floor_percent/vacation_ceiling_percent configure the WIDTH
    # of the start/stop band (default 90-70 = 20 points of hysteresis),
    # not fixed absolute levels any more: in vacation mode the ceiling is
    # now the dynamic vacation_departure_target_percent, and the floor
    # trails it by that same configured width.
    vacation_floor = float(T[NUM_VACATION_FLOOR_PERCENT])
    vacation_ceiling = float(T[NUM_VACATION_CEILING_PERCENT])
    vacation_band_width = max(vacation_ceiling - vacation_floor, 0.0)
    if is_vacation_mode:
        effective_vacation_ceiling = vacation_departure_target_percent
        effective_vacation_floor = max(effective_vacation_ceiling - vacation_band_width, 0.0)
    else:
        effective_vacation_ceiling = vacation_ceiling
        effective_vacation_floor = vacation_floor
    r["vacation_charge_floor_percentage"] = effective_vacation_floor
    r["vacation_charge_ceiling_percentage"] = effective_vacation_ceiling
    is_vacation_soc_low = soc < effective_vacation_floor
    is_vacation_soc_full = soc >= effective_vacation_ceiling
    r["is_vacation_soc_low"] = is_vacation_soc_low
    r["is_vacation_soc_full"] = is_vacation_soc_full

    # ---------------------------------------------------------------
    # CHARGE TARGET
    # ---------------------------------------------------------------
    # Calibration always wins (battery-health balancing is independent of
    # vacation mode). Otherwise, in vacation mode the dynamic day-based
    # target IS the target - at every stage (idle topping-off, voyage
    # approach, voyage final-due/hold) - which is what lets a lower
    # day-count actually limit charging instead of being overridden back
    # to a flat 95/100 once a departure gets close.
    if is_calibration_cycle:
        target_charge_percentage = 100
    elif is_vacation_mode:
        target_charge_percentage = vacation_departure_target_percent
    elif effective_full_charge_cycle or is_voyage_approach_due:
        target_charge_percentage = 100 if effective_full_charge_cycle else 95
    elif is_voyage_pending:
        target_charge_percentage = 95
    else:
        target_charge_percentage = 90
    r["target_charge_percentage"] = target_charge_percentage
    target_charge_level = round(battery_size * (target_charge_percentage / 100), 2)
    r["target_charge_level"] = target_charge_level

    # Two separate session caps, picked by whether the battery is
    # currently below the "Price-ceiling SoC floor" (soc_floor_percent,
    # default 40%) - the same threshold the price-ceiling relaxation
    # below already keys off. Below it, a lower cap (its own tunable, so
    # it can be set independently of the above-floor one) keeps sessions
    # short on purpose; at/above it, the normal, usually larger cap
    # applies. Re-evaluated fresh every cycle - not latched.
    is_below_soc_floor = soc < float(T[NUM_SOC_FLOOR_PERCENT])
    r["is_below_soc_floor"] = is_below_soc_floor
    session_cap_hours = (
        float(T[NUM_SESSION_CAP_LOW_SOC_HOURS]) if is_below_soc_floor else float(T[NUM_SESSION_CAP_HOURS])
    )
    r["session_cap_hours"] = session_cap_hours
    session_cap_kwh = round((avg_load_w * session_cap_hours) / 1000, 1)
    gap_kwh = max(round(target_charge_level - battery_energy, 1), 0.0)
    charge_kwh = gap_kwh if is_voyage_charge_forced else min(gap_kwh, session_cap_kwh)
    r["charge_kwh"] = charge_kwh
    has_charge_need = charge_kwh > T[NUM_HAS_CHARGE_NEED_THRESHOLD_KWH]
    has_no_charge_need = charge_kwh <= 0
    r["has_charge_need"] = has_charge_need
    r["has_no_charge_need"] = has_no_charge_need

    # ---------------------------------------------------------------
    # BALANCE CONFIRMATION
    # ---------------------------------------------------------------
    is_cell_balanced = abs(cell_max_v - cell_min_v) <= T[NUM_CELL_BALANCE_TOLERANCE]
    r["is_cell_balanced"] = is_cell_balanced
    is_full_and_balanced = is_voltage_full and is_cell_balanced
    r["is_full_and_balanced"] = is_full_and_balanced

    prev_balance_confirmed_since = pdt("balance_confirmed_since")
    if is_full_and_balanced:
        balance_confirmed_since = prev_balance_confirmed_since if prev_balance_confirmed_since is not None else now
    else:
        balance_confirmed_since = None
    r["balance_confirmed_since"] = balance_confirmed_since
    is_balance_settled = (
        (now - balance_confirmed_since) >= timedelta(minutes=T[NUM_BALANCE_SETTLE_MINUTES])
        if balance_confirmed_since is not None
        else False
    )
    r["is_balance_settled"] = is_balance_settled

    if is_balance_settled and effective_full_charge_cycle:
        last_full_charge_completed_at = now
    elif prev_last_full_charge is not None:
        last_full_charge_completed_at = prev_last_full_charge
    else:
        last_full_charge_completed_at = None
    r["last_full_charge_completed_at"] = last_full_charge_completed_at

    is_charge_complete = is_balance_settled if effective_full_charge_cycle else has_no_charge_need
    r["is_charge_complete"] = is_charge_complete

    # ---------------------------------------------------------------
    # CHARGE RATE & DURATION (remainder)
    # ---------------------------------------------------------------
    charge_kwh_gross = round(charge_kwh / charger_efficiency, 2) if charger_efficiency > 0 else charge_kwh
    r["charge_kwh_gross"] = charge_kwh_gross
    charge_time_minutes = round((charge_kwh_gross / battery_charge_speed) * 60) if battery_charge_speed > 0 else 0
    r["charge_time_minutes"] = charge_time_minutes
    calibration_grid_buffer_minutes = T[NUM_CALIBRATION_GRID_BUFFER_MINUTES] if is_full_charge_cycle else 0
    r["calibration_grid_buffer_minutes"] = calibration_grid_buffer_minutes
    # A full charge cycle (calibration or voyage-hold) only ever completes
    # once is_balance_settled - see BALANCE CONFIRMATION above - so for
    # exactly this cycle type the settle hold isn't a maybe, it's a
    # certainty: whenever is_full_charge_cycle is true, the session WILL
    # need balance_settle_minutes of continued full+balanced holding after
    # voltage target is reached before it's actually done. Planning for it
    # up front - same way calibration_grid_buffer_minutes already is -
    # means the window search sees the TRUE required duration and picks a
    # placement where that whole stretch, hold included, fits a cheap/flat
    # period, instead of only discovering live (post duration-elapsed, see
    # the state machine's voltage-reached bypass) that more time is still
    # needed and potentially running on into a pricier period by then.
    balance_settle_buffer_minutes = T[NUM_BALANCE_SETTLE_MINUTES] if is_full_charge_cycle else 0
    r["balance_settle_buffer_minutes"] = balance_settle_buffer_minutes
    charge_time_units = math.ceil(
        (charge_time_minutes + calibration_grid_buffer_minutes + balance_settle_buffer_minutes) / 15
    )
    r["charge_time_units"] = charge_time_units
    r["charge_time_hours"] = round(charge_time_units / 4, 2)

    # ---------------------------------------------------------------
    # PRICE DATA
    # ---------------------------------------------------------------
    interval_start_unit = int(now.minute // 15) + (now.hour * 4) + 1
    r["interval_start_unit"] = interval_start_unit
    all_price = list(price_today or []) + list(price_tomorrow or [])
    r["all_price"] = all_price
    interval_price = all_price[interval_start_unit:]
    r["interval_price"] = interval_price

    battery_drain_rate_percent_per_hour = (
        round(((avg_load_w / 1000) / battery_size) * 100, 3) if battery_size > 0 else 0.0
    )
    r["battery_drain_rate_percent_per_hour"] = battery_drain_rate_percent_per_hour

    # ---------------------------------------------------------------
    # CHEAPEST WINDOW SEARCH
    # ---------------------------------------------------------------
    # The horizon is everything the price sensor currently knows (today +
    # tomorrow, once published) - NOT units_left (time until usable_energy
    # hits low_battery_percentage). It used to be units_left, which made
    # the search give up early and grab a "good enough" nearby price
    # purely because the battery was getting low - even though patiently
    # waiting a day for a much cheaper block is fine as long as it isn't
    # actually approaching low_battery_percentage. That real safety net
    # doesn't depend on the horizon at all: price_limited_start_index
    # below already relaxes the acceptable price (via soc_floor/
    # soc_ceiling) as the projected SoC nears soc_floor, so an early,
    # not-great-but-acceptable window still gets picked up well before
    # low_battery_percentage is actually at risk.
    normal_horizon = 0 if shore_power_w > T[NUM_HIGH_SHORE_LOAD_THRESHOLD_W] else len(interval_price)
    if is_voyage_pending and not is_vacation_mode:
        units_into_hour = interval_start_unit - (now.hour * 4)
        voyage_deadline_units = round(start_voyage_hours * 4) - units_into_hour
        voyage_horizon = max(voyage_deadline_units, 0)
        last_considered_interval_index = min(normal_horizon, voyage_horizon)
    else:
        last_considered_interval_index = normal_horizon

    # Below the SoC floor, deliberately don't let the search roam the full
    # horizon above: cap it at a short, dedicated number of hours (default
    # 8h) - or, if the battery would reach low_battery_percentage sooner
    # than that, at `units_left` instead, same as the pre-2026.10.5
    # definition (reintroduced here on purpose, scoped to this one case,
    # not as the general-purpose horizon it used to be).
    if is_below_soc_floor:
        low_soc_horizon_units = round(float(T[NUM_LOW_SOC_SEARCH_HORIZON_HOURS]) * UNITS_PER_HOUR)
        last_considered_interval_index = min(last_considered_interval_index, low_soc_horizon_units, units_left)
    r["last_considered_interval_index"] = last_considered_interval_index

    soc_floor = float(T[NUM_SOC_FLOOR_PERCENT])
    soc_ceiling = float(T[NUM_SOC_CEILING_PERCENT])
    drain_per_unit = battery_drain_rate_percent_per_hour / 4

    # -----------------------------------------------------------------
    # CHEAPEST WINDOW SEARCH / SPLIT-SESSION SEARCH / FLAT-BLOCK EXTENSION
    # -----------------------------------------------------------------
    # Wrapped in a function of (plan_charge_kwh, plan_charge_time_units) -
    # 2026.10.17 - so the whole placement pipeline (search, price-trim,
    # split search, flat-block extension) can be re-run with a bigger,
    # look-ahead-adjusted pair instead of only the live snapshot. See the
    # pass loop right after this function's definition, and
    # projected_charge_kwh_at_window_start's own comment further down,
    # for the full reasoning. Safe to call more than once: every r[]
    # write below is simply overwritten by a later call, and only the
    # LAST call's return value is ever unpacked into the local variables
    # the sections after the loop actually use.
    def _plan_window(plan_charge_kwh: float, plan_charge_time_units: int) -> dict:
        duration = max(plan_charge_time_units, 1)
        horizon = max(last_considered_interval_index, 0)

        # Both loops require a FULL-length window (len(window) == duration),
        # not merely a non-empty one: interval_price[idx:idx+duration] silently
        # truncates near the end of the array instead of raising, so without
        # this check a 2-unit sliver right at the edge of the horizon was
        # compared on equal footing against genuine full-duration placements -
        # and, being an average of only its very cheapest couple of units,
        # would usually "win" even though it doesn't represent a real
        # placement for a session that actually needs `duration` units of
        # charging. That's what previously made a long required session latch
        # onto a tiny sliver far from any real cheap stretch instead of a
        # genuinely well-priced placement.
        best_average, best_index = 1000.0, -1
        for idx in range(horizon):
            window = interval_price[idx : idx + duration]
            if len(window) < duration:
                continue
            window_average = _avg(window)
            if window_average < best_average:
                best_average, best_index = window_average, idx
        cheapest_start_index = best_index if best_index >= 0 else 0
        r["cheapest_start_index"] = cheapest_start_index

        best_average, best_index = 1000.0, -1
        for idx in range(horizon):
            window = interval_price[idx : idx + duration]
            if len(window) < duration:
                continue
            window_average = _avg(window)
            projected_soc = max(soc - drain_per_unit * idx, soc_floor)
            projected_ceiling = price_14d_average * max(1 - ((projected_soc - soc_floor) / (soc_ceiling - soc_floor)), 0)
            if window_average <= projected_ceiling and window_average < best_average:
                best_average, best_index = window_average, idx
        price_limited_start_index = best_index
        r["price_limited_start_index"] = price_limited_start_index

        anchor = cheapest_start_index
        full_duration = max(plan_charge_time_units, 1)
        window = interval_price[anchor : anchor + full_duration]
        full_average = _avg(window)
        full_projected_soc = max(soc - drain_per_unit * anchor, soc_floor)
        full_ceiling = price_14d_average * max(1 - ((full_projected_soc - soc_floor) / (soc_ceiling - soc_floor)), 0)
        best_len, best_a = 0, 0
        if window and full_average > full_ceiling and min(window) < full_ceiling:
            for a in range(full_duration):
                for b in range(full_duration - a):
                    trial_len = full_duration - a - b
                    if trial_len <= best_len:
                        continue
                    trial_start = anchor + a
                    trial_window = interval_price[trial_start : trial_start + trial_len]
                    if not trial_window:
                        continue
                    trial_average = _avg(trial_window)
                    trial_soc = max(soc - drain_per_unit * trial_start, soc_floor)
                    trial_ceiling = price_14d_average * max(1 - ((trial_soc - soc_floor) / (soc_ceiling - soc_floor)), 0)
                    if trial_average <= trial_ceiling:
                        best_len, best_a = trial_len, a
        price_trim_start_index = (anchor + best_a) if best_len > 0 else anchor
        price_trim_duration_units = best_len if best_len > 0 else full_duration
        r["price_trim_start_index"] = price_trim_start_index
        r["price_trim_duration_units"] = price_trim_duration_units

        # Below the SoC floor, the average-price ceiling is no longer
        # consulted at all - that relaxation exists to patiently trade a
        # longer wait for a cheaper average price, which is the opposite of
        # what's wanted here (a short, already tightly-capped session within
        # a short horizon). Just take the plain cheapest full-duration window
        # found above.
        #
        # When price_limited_start_index >= 0, some position's (SoC-projected)
        # ceiling was satisfied somewhere in the horizon - i.e. charging at
        # roughly that price is justified at all. But that candidate was only
        # ever compared against windows that ALSO passed their own (position-
        # dependent, generally stricter earlier in the horizon) ceiling - not
        # against every window outright. cheapest_start_index's average is
        # always <= price_limited's, by construction (it's the unconditional
        # minimum over the same full set of full-duration windows), so once
        # charging is justified at all, the actually-cheapest full-duration
        # window is also justified - no need to settle for whatever first
        # cleared its own, possibly-too-strict-for-that-early-position bar.
        # (Without this, a genuinely cheaper window earlier in the day could
        # be skipped purely because the SoC-projected ceiling hadn't loosened
        # enough yet AT THAT position, while a pricier window later - where
        # the ceiling had since loosened - got used instead.)
        # ---------------------------------------------------------------
        # SPLIT-SESSION SEARCH (2026.10.11)
        # ---------------------------------------------------------------
        # Only considered in exactly the branch above that would otherwise
        # charge the FULL required duration as one contiguous block starting
        # at cheapest_start_index (price_limited_start_index >= 0, i.e.
        # charging is justified at all) - not below the SoC floor (that mode
        # deliberately ignores price nuance, by earlier design choice) and not
        # combined with price-trimming (a different, incompatible shortening
        # mechanism). "Always split once it's cheaper" (user's explicit
        # choice) - the only floor is MIN_SPLIT_BLOCK_UNITS per block, plus a
        # minimum gap of one full cooldown period so block B is never due
        # before the reconnect lock-out from block A's stop has actually
        # cleared (also the user's explicit choice, over an unconditional
        # 1-unit gap).
        split_min_gap_units = max(math.ceil(float(T[NUM_COOLDOWN_MINUTES]) / UNIT_MINUTES), 1)
        split_candidate: Optional[tuple[float, int, int, int, int]] = None
        if not is_below_soc_floor and price_limited_start_index >= 0:
            split_candidate = _best_two_block_split(
                interval_price, horizon, duration, MIN_SPLIT_BLOCK_UNITS, split_min_gap_units
            )

        cheapest_window = interval_price[cheapest_start_index : cheapest_start_index + duration]
        single_block_cost = sum(cheapest_window) if len(cheapest_window) == duration else None

        # The epsilon guards against a split being "chosen" by pure
        # floating-point noise when the two options are actually tied - e.g.
        # a uniform/flat background price (easy to hit with a synthetic test
        # price curve, essentially never with a real day-ahead market) lets
        # many different placements of the "overflow" duration cost exactly
        # the same, and summation order alone can then make split_candidate's
        # sum a few ULPs below single_block_cost. 1e-6 is far below any
        # price difference that could matter (prices/durations here are never
        # fine enough for a genuine saving to be that small) while still
        # comfortably covering float summation error.
        is_split_chosen = (
            split_candidate is not None
            and single_block_cost is not None
            and split_candidate[0] < single_block_cost - 1e-6
        )
        r["is_split_chosen"] = is_split_chosen

        if is_below_soc_floor:
            start_index = cheapest_start_index
        elif is_split_chosen:
            start_index = split_candidate[1]
        elif price_limited_start_index >= 0:
            start_index = cheapest_start_index
        elif price_trim_duration_units < plan_charge_time_units:
            start_index = price_trim_start_index
        else:
            start_index = cheapest_start_index
        r["start_index"] = start_index

        if is_split_chosen:
            effective_duration_units = split_candidate[2]
        else:
            effective_duration_units = (
                plan_charge_time_units if (is_below_soc_floor or price_limited_start_index >= 0) else price_trim_duration_units
            )
        r["effective_duration_units"] = effective_duration_units
        r["low_price"] = min(all_price) if all_price else 0.0

        # Block B of a chosen split - interval-relative (same frame as
        # start_index), consumed by PRE-START LOCK / SESSION LOCK-IN below to
        # reserve its slot across the pause. None whenever no split is live.
        if is_split_chosen:
            split_b_start_index = split_candidate[3]
            split_b_duration_units = split_candidate[4]
            split_b_window = interval_price[split_b_start_index : split_b_start_index + split_b_duration_units]
            split_b_average_price = round(_avg(split_b_window), 4) if split_b_window else 0.0
        else:
            split_b_start_index = None
            split_b_duration_units = None
            split_b_average_price = None
        r["split_b_start_index"] = split_b_start_index
        r["split_b_duration_units"] = split_b_duration_units
        # Was computed above but never actually exposed before 2026.10.13 -
        # pending_plan_b_average_price (below) read the local variable
        # directly, so this gap was invisible downstream, but it meant the
        # sensor never carried block B's own average price as an attribute.
        r["split_b_average_price"] = split_b_average_price

        # ---------------------------------------------------------------
        # FLAT-BLOCK EXTENSION
        # ---------------------------------------------------------------
        start = start_index
        ext_duration = max(effective_duration_units, 1)
        cwap_window = interval_price[start : start + ext_duration]
        charge_window_average_price = round(_avg(cwap_window), 4) if cwap_window else 0.0
        r["charge_window_average_price"] = charge_window_average_price

        flat_block_price_margin = float(T[NUM_FLAT_BLOCK_MARGIN])
        flat_block_price_tolerance = float(T[NUM_FLAT_BLOCK_TOLERANCE])
        r["flat_block_price_margin"] = flat_block_price_margin
        r["flat_block_price_tolerance"] = flat_block_price_tolerance

        def _flat_threshold(avg_price: float) -> float:
            percentage_threshold = (
                avg_price * flat_block_price_margin if avg_price >= 0 else avg_price / flat_block_price_margin
            )
            absolute_threshold = avg_price + flat_block_price_tolerance
            return max(percentage_threshold, absolute_threshold)

        flat_block_price_threshold = _flat_threshold(charge_window_average_price)
        r["flat_block_price_threshold"] = flat_block_price_threshold

        # How much the base (price-ceiling-qualifying, possibly price-trimmed)
        # window delivers on its own - computed here (and reused below as
        # effective_charge_kwh) because the extension needs it as its
        # starting point: "how much is already granted before any flat-block
        # extension happens". That can be less than plan_charge_kwh itself when
        # price-trimming shortened the base window below plan_charge_time_units. A
        # chosen split's two base blocks (l1+l2) always total exactly
        # plan_charge_time_units (see _best_two_block_split), so together they
        # deliver exactly plan_charge_kwh, same as an un-trimmed single block -
        # using only block A's own l1 here would silently drop block B's
        # share of the already-granted energy and overstate how much headroom
        # is left for extension.
        if is_split_chosen:
            base_charge_kwh = plan_charge_kwh
        elif effective_duration_units < max(plan_charge_time_units, 1):
            base_charge_kwh = round(max(effective_duration_units, 1) * (battery_charge_speed / 4) * charger_efficiency, 2)
        else:
            base_charge_kwh = plan_charge_kwh

        # The flat-block extension is deliberately allowed to push PAST the
        # session cap: the whole point of it is that a long flat/uniformly-
        # cheap stretch is exactly the situation where taking more energy now
        # - rather than waiting for another day's separately-capped session -
        # is the efficient thing to do. So headroom is measured against the
        # full remaining gap to the SoC target, not against plan_charge_kwh (which
        # is itself session-capped unless is_voyage_charge_forced). The
        # session cap still fully governs the BASE window(s) chosen by the
        # search above; it just doesn't also limit how far a genuinely flat
        # stretch beyond that base window can be ridden.
        headroom_kwh = max(target_charge_level - battery_energy - base_charge_kwh, 0.0)
        if battery_charge_speed > 0 and charger_efficiency > 0:
            max_extra_units = math.floor(headroom_kwh / ((battery_charge_speed / 4) * charger_efficiency))
        else:
            max_extra_units = 0
        max_extra_units = max(max_extra_units, 0)

        def _extend(probe_start: int, step: int, budget: int, threshold: float, limit_idx: Optional[int] = None) -> int:
            """Walk from `probe_start` in `step` direction (+1/-1), counting
            consecutive in-bounds units priced at or below `threshold`,
            stopping at the first disqualifying unit (same "must be
            contiguous" rule as before 2026.10.13). Capped at `budget` units
            and at 48 total. `limit_idx`, when given, is the first index (in
            the direction of travel) that must never be claimed - used to
            keep a full cooldown gap to the other split block."""
            extending, extra = True, 0
            for i in range(48):
                if not extending or extra >= budget:
                    break
                idx = probe_start + step * i
                if idx < 0 or idx >= len(interval_price):
                    break
                if limit_idx is not None and ((step > 0 and idx >= limit_idx) or (step < 0 and idx <= limit_idx)):
                    break
                if interval_price[idx] <= threshold:
                    extra += 1
                else:
                    extending = False
            return extra

        # split_b_* defaults for the non-split path; overwritten below when a
        # split is actually chosen.
        split_b_forward_extension_units: Optional[int] = None
        split_b_backward_extension_units: Optional[int] = None
        split_b_extension_units: Optional[int] = None
        split_b_effective_start_index: Optional[int] = None
        split_b_planned_duration_units: Optional[int] = None

        if not is_split_chosen:
            # Single block (unchanged pre-2026.10.13 behaviour): forward gets
            # first claim on the budget, backward gets whatever's left.
            forward_units = _extend(start + ext_duration, 1, max_extra_units, flat_block_price_threshold)
            remaining_budget = max(max_extra_units - forward_units, 0)
            backward_units = _extend(start - 1, -1, remaining_budget, flat_block_price_threshold)
            total_extension_units = forward_units + backward_units
        else:
            # Chosen split (2026.10.13 refinement): each block is judged on
            # its own average price/threshold and gets its own two-directional
            # extension, instead of the whole mechanism being switched off.
            # The direction AWAY from the other block is as free as a normal
            # single block's; the direction TOWARD the other block is capped
            # so at least a full cooldown period (split_min_gap_units) always
            # remains between them - that gap is the whole reason a split
            # exists, so neither block may eat into it from either side.
            # Processed cheapest-block-first (user's explicit choice) so it
            # gets first claim on the one shared headroom budget; the pricier
            # block then extends into whatever's left, with its inward limit
            # based on the cheap block's now-settled (possibly already
            # extended) boundary rather than its original search position.
            flat_block_price_threshold_b = _flat_threshold(split_b_average_price)
            a_is_cheaper = charge_window_average_price <= split_b_average_price
            remaining_budget = max_extra_units

            if a_is_cheaper:
                a_back = _extend(start - 1, -1, remaining_budget, flat_block_price_threshold)
                remaining_budget = max(remaining_budget - a_back, 0)
                a_fwd_limit = split_b_start_index - split_min_gap_units
                a_fwd = _extend(start + ext_duration, 1, remaining_budget, flat_block_price_threshold, a_fwd_limit)
                remaining_budget = max(remaining_budget - a_fwd, 0)

                a_new_end = start + ext_duration + a_fwd
                b_fwd = _extend(
                    split_b_start_index + split_b_duration_units, 1, remaining_budget, flat_block_price_threshold_b
                )
                remaining_budget = max(remaining_budget - b_fwd, 0)
                b_back_limit = a_new_end + split_min_gap_units - 1
                b_back = _extend(
                    split_b_start_index - 1, -1, remaining_budget, flat_block_price_threshold_b, b_back_limit
                )
            else:
                b_fwd = _extend(
                    split_b_start_index + split_b_duration_units, 1, remaining_budget, flat_block_price_threshold_b
                )
                remaining_budget = max(remaining_budget - b_fwd, 0)
                b_back_limit = (start + ext_duration) + split_min_gap_units - 1
                b_back = _extend(
                    split_b_start_index - 1, -1, remaining_budget, flat_block_price_threshold_b, b_back_limit
                )
                remaining_budget = max(remaining_budget - b_back, 0)

                b_new_start = split_b_start_index - b_back
                a_back = _extend(start - 1, -1, remaining_budget, flat_block_price_threshold)
                remaining_budget = max(remaining_budget - a_back, 0)
                a_fwd_limit = b_new_start - split_min_gap_units
                a_fwd = _extend(start + ext_duration, 1, remaining_budget, flat_block_price_threshold, a_fwd_limit)

            forward_units, backward_units = a_fwd, a_back
            split_b_forward_extension_units, split_b_backward_extension_units = b_fwd, b_back
            split_b_extension_units = b_fwd + b_back
            split_b_effective_start_index = max(split_b_start_index - b_back, 0)
            split_b_planned_duration_units = split_b_duration_units + split_b_extension_units
            total_extension_units = forward_units + backward_units + split_b_extension_units

        r["charge_window_forward_extension_units"] = forward_units
        r["charge_window_backward_extension_units"] = backward_units
        r["split_b_forward_extension_units"] = split_b_forward_extension_units
        r["split_b_backward_extension_units"] = split_b_backward_extension_units
        r["split_b_extension_units"] = split_b_extension_units
        r["split_b_effective_start_index"] = split_b_effective_start_index
        r["split_b_planned_duration_units"] = split_b_planned_duration_units

        charge_window_extension_units = forward_units + backward_units
        r["charge_window_extension_units"] = charge_window_extension_units
        effective_start_index = max(start - backward_units, 0)
        r["effective_start_index"] = effective_start_index

        planned_charge_time_units = effective_duration_units + charge_window_extension_units
        r["planned_charge_time_units"] = planned_charge_time_units
        r["planned_charge_time_hours"] = round(planned_charge_time_units / 4, 2)

        effective_charge_kwh = base_charge_kwh
        r["effective_charge_kwh"] = effective_charge_kwh

        # Total projected energy for the WHOLE plan (both blocks, when split -
        # this feeds projected_end_soc below, so dropping block B's extension
        # here would understate the actual projected end-of-session SoC).
        planned_charge_kwh = round(
            effective_charge_kwh + (total_extension_units * (battery_charge_speed / 4) * charger_efficiency), 2
        )
        r["planned_charge_kwh"] = planned_charge_kwh
        planned_charge_kwh_gross = (
            round(planned_charge_kwh / charger_efficiency, 2) if charger_efficiency > 0 else planned_charge_kwh
        )
        r["planned_charge_kwh_gross"] = planned_charge_kwh_gross
        return {
            "price_limited_start_index": price_limited_start_index,
            "effective_duration_units": effective_duration_units,
            "is_split_chosen": is_split_chosen,
            "split_b_average_price": split_b_average_price,
            "charge_window_average_price": charge_window_average_price,
            "split_b_effective_start_index": split_b_effective_start_index,
            "split_b_planned_duration_units": split_b_planned_duration_units,
            "charge_window_extension_units": charge_window_extension_units,
            "effective_start_index": effective_start_index,
            "planned_charge_time_units": planned_charge_time_units,
            "planned_charge_kwh": planned_charge_kwh,
        }

    # -----------------------------------------------------------------
    # LOOK-AHEAD PASS (2026.10.17)
    # -----------------------------------------------------------------
    # Run the plan above with the live charge_kwh/charge_time_units
    # first, exactly as always. If the window it lands on sits far
    # enough ahead that the battery will have drained down to a
    # meaningfully LOWER projected SoC by the time it actually starts,
    # there is more genuine headroom to the target than today's live
    # snapshot shows - so re-run the whole placement pipeline with that
    # bigger, projected need, which can shift the search onto a
    # longer/different placement (same idea as
    # projected_charge_kwh_at_window_start below, but now actually fed
    # back into the plan instead of only shown for information).
    # Bounded to a few passes as a hard backstop, not because real data
    # is expected to need more than one correction: each pass only ever
    # grows plan_charge_kwh/plan_charge_time_units, and growth continues
    # only if the chosen start moved further out than the previous pass
    # already accounted for - every scenario tried settles within 1-2
    # passes. Below the SoC floor or voyage-forced charging effectively
    # never grow here (is_voyage_charge_forced already ignores the
    # session cap in the live figure too, and is_below_soc_floor
    # charging is deliberately short and urgent, not something to plan
    # further out for) - projected_charge_kwh_candidate comes out equal
    # to the live figure already in both cases, so the loop below
    # converges on its very first pass.
    plan_charge_kwh = charge_kwh
    plan_charge_time_units = charge_time_units
    charge_plan_lookahead_adjusted = False
    plan = _plan_window(plan_charge_kwh, plan_charge_time_units)
    for _lookahead_pass in range(3):
        units_ahead_candidate = plan["effective_start_index"]
        soc_at_candidate_start = max(soc - drain_per_unit * units_ahead_candidate, soc_floor)
        projected_energy_candidate = round(battery_size * (soc_at_candidate_start / 100), 2)
        projected_gap_candidate = max(round(target_charge_level - projected_energy_candidate, 1), 0.0)
        projected_charge_kwh_candidate = (
            projected_gap_candidate if is_voyage_charge_forced else min(projected_gap_candidate, session_cap_kwh)
        )
        if projected_charge_kwh_candidate <= plan_charge_kwh + 0.05:
            break
        charge_plan_lookahead_adjusted = True
        plan_charge_kwh = projected_charge_kwh_candidate
        lookahead_kwh_gross = (
            round(plan_charge_kwh / charger_efficiency, 2) if charger_efficiency > 0 else plan_charge_kwh
        )
        lookahead_minutes = (
            round((lookahead_kwh_gross / battery_charge_speed) * 60) if battery_charge_speed > 0 else 0
        )
        plan_charge_time_units = math.ceil(
            (lookahead_minutes + calibration_grid_buffer_minutes + balance_settle_buffer_minutes) / 15
        )
        plan = _plan_window(plan_charge_kwh, plan_charge_time_units)

    r["charge_plan_lookahead_adjusted"] = charge_plan_lookahead_adjusted
    r["charge_kwh_planned"] = plan_charge_kwh
    r["charge_time_units_planned"] = plan_charge_time_units
    charge_time_units_used = plan_charge_time_units

    price_limited_start_index = plan["price_limited_start_index"]
    effective_duration_units = plan["effective_duration_units"]
    is_split_chosen = plan["is_split_chosen"]
    split_b_average_price = plan["split_b_average_price"]
    charge_window_average_price = plan["charge_window_average_price"]
    split_b_effective_start_index = plan["split_b_effective_start_index"]
    split_b_planned_duration_units = plan["split_b_planned_duration_units"]
    charge_window_extension_units = plan["charge_window_extension_units"]
    effective_start_index = plan["effective_start_index"]
    planned_charge_time_units = plan["planned_charge_time_units"]
    planned_charge_kwh = plan["planned_charge_kwh"]
    r["best_interval_price"] = interval_price[effective_start_index : effective_start_index + planned_charge_time_units]

    # ---------------------------------------------------------------
    # PRE-START LOCK
    # ---------------------------------------------------------------
    prev_charge_plan_b_start_raw = P.get("charge_plan_b_start_unit")
    prev_charge_plan_b_start = _rebase_unit(
        prev_charge_plan_b_start_raw if _is_plain_number(prev_charge_plan_b_start_raw) else None,
        P.get("charge_plan_b_reference_date"),
        now,
    )
    prev_charge_plan_b_duration = P.get("charge_plan_b_duration_units")
    # True from block A's session lock-in all the way through block B's own
    # lock-in (spans block A's charging AND the deliberate pause after it -
    # see SESSION LOCK-IN below, which holds/consumes this same persisted
    # pair). While it's live and we're not currently charging, we're in
    # that pause: block B's slot was already decided when block A locked
    # in and must NOT be re-derived from a fresh search below - a fresh
    # search has no notion of "resume the second half of an already-
    # committed split" and could easily pick something else entirely now
    # that conditions have moved on.
    awaiting_split_b = (not is_charging) and prev_charge_plan_b_start is not None

    if awaiting_split_b:
        is_start_imminent = int(prev_charge_plan_b_start) - interval_start_unit <= 1
        r["is_start_imminent"] = is_start_imminent
        live_start_unit = int(prev_charge_plan_b_start)
        pending_plan_start_unit = live_start_unit
        pending_plan_duration_units = (
            int(prev_charge_plan_b_duration) if prev_charge_plan_b_duration is not None else None
        )
        pending_plan_average_price = P.get("pending_plan_b_average_price")
        # No NEW split can be considered while one is already pending
        # block B's start - just keep tracking the reserved slot itself.
        pending_plan_b_start_unit = live_start_unit
        pending_plan_b_duration_units = pending_plan_duration_units
        pending_plan_b_average_price = pending_plan_average_price
        # Downstream code (pending_plan_target_soc) only distinguishes
        # "clear" from everything else - "keep" is the accurate label for
        # "holding an already-decided reservation unchanged".
        pending_mode = "keep"
    else:
        is_start_imminent = (not is_charging) and has_charge_need and (effective_start_index <= 1)
        r["is_start_imminent"] = is_start_imminent

        prev_pending_start_raw = P.get("pending_plan_start_unit")
        prev_pending_start = _rebase_unit(
            prev_pending_start_raw if _is_plain_number(prev_pending_start_raw) else None,
            P.get("pending_plan_reference_date"),
            now,
        )
        qualifies = (not is_below_soc_floor and price_limited_start_index >= 0) or (
            effective_duration_units < charge_time_units_used
        )
        locked_idx = (int(prev_pending_start) - interval_start_unit) if prev_pending_start is not None else -1
        locked_duration = max(int(pf("pending_plan_duration_units", 0)), 1)
        if 0 <= locked_idx < len(interval_price):
            locked_window = interval_price[locked_idx : locked_idx + locked_duration]
            locked_avg = _avg(locked_window) if locked_window else 1000.0
        else:
            locked_avg = 1000.0
        lock_active = is_start_imminent or is_charging or pb("is_within_post_charge_lock", False)
        live_start_unit = interval_start_unit + effective_start_index

        if is_charging or (not has_charge_need) or (not lock_active):
            pending_mode = "clear"
        elif prev_pending_start is None:
            pending_mode = "seed"
        elif qualifies and (live_start_unit > prev_pending_start or charge_window_average_price < locked_avg):
            pending_mode = "reseed"
        else:
            pending_mode = "keep"

        if pending_mode == "clear":
            pending_plan_start_unit = pending_plan_duration_units = pending_plan_average_price = None
            pending_plan_b_start_unit = pending_plan_b_duration_units = pending_plan_b_average_price = None
        elif pending_mode in ("seed", "reseed"):
            pending_plan_start_unit = live_start_unit
            pending_plan_duration_units = planned_charge_time_units
            pending_plan_average_price = charge_window_average_price
            if is_split_chosen:
                # Reserve block B's EXTENDED window (2026.10.13), not its
                # raw search position - split_b_effective_start_index/
                # split_b_planned_duration_units already fold in whatever
                # flat-block extension block B itself earned above.
                pending_plan_b_start_unit = interval_start_unit + split_b_effective_start_index
                pending_plan_b_duration_units = split_b_planned_duration_units
                pending_plan_b_average_price = split_b_average_price
            else:
                pending_plan_b_start_unit = pending_plan_b_duration_units = pending_plan_b_average_price = None
        else:
            pending_plan_start_unit = prev_pending_start
            pending_plan_duration_units = P.get("pending_plan_duration_units")
            pending_plan_average_price = P.get("pending_plan_average_price")
            prev_pending_b_start_raw = P.get("pending_plan_b_start_unit")
            pending_plan_b_start_unit = _rebase_unit(
                prev_pending_b_start_raw if _is_plain_number(prev_pending_b_start_raw) else None,
                P.get("pending_plan_b_reference_date"),
                now,
            )
            if pending_plan_b_start_unit is not None:
                pending_plan_b_duration_units = P.get("pending_plan_b_duration_units")
                pending_plan_b_average_price = P.get("pending_plan_b_average_price")
            else:
                pending_plan_b_duration_units = pending_plan_b_average_price = None
    r["pending_plan_start_unit"] = pending_plan_start_unit
    # Always stamped fresh (not just on seed/reseed): whatever value is
    # being stored this cycle - freshly computed or carried forward from
    # an already-rebased prev_pending_start - is, by construction,
    # expressed relative to TODAY's midnight at this point, so today is
    # always the correct reference date for it.
    r["pending_plan_reference_date"] = now.date().isoformat()
    r["pending_plan_duration_units"] = pending_plan_duration_units
    r["pending_plan_average_price"] = pending_plan_average_price
    r["pending_plan_b_start_unit"] = pending_plan_b_start_unit
    r["pending_plan_b_reference_date"] = now.date().isoformat() if pending_plan_b_start_unit is not None else None
    r["pending_plan_b_duration_units"] = pending_plan_b_duration_units
    r["pending_plan_b_average_price"] = pending_plan_b_average_price

    # ---------------------------------------------------------------
    # SESSION LOCK-IN
    # ---------------------------------------------------------------
    prev_charge_plan_start_raw = P.get("charge_plan_start_unit")
    prev_charge_plan_start = _rebase_unit(
        prev_charge_plan_start_raw if _is_plain_number(prev_charge_plan_start_raw) else None,
        P.get("charge_plan_reference_date"),
        now,
    )
    # True on the one cycle block B itself actually starts charging after
    # a split's pause: no primary plan locked in yet this "session"
    # (prev_charge_plan_start is None, same as any fresh lock-in), but a
    # block-B reservation from block A's split is still outstanding. Its
    # start/duration are used directly instead of the normal
    # pending_plan_start_unit/live_start_unit fallback, which can't be
    # trusted to still equal the reserved slot here (see PRE-START LOCK).
    resuming_split_b = is_charging and prev_charge_plan_start is None and prev_charge_plan_b_start is not None

    # On the one cycle a plan locks in (prev_charge_plan_start is None),
    # pending_plan_start_unit/duration (the LOCAL variables above) have
    # already been wiped to None by PRE-START LOCK's own pending_mode ==
    # "clear" (is_charging is True this same cycle) - so the fallback
    # here reads `prev_pending_start` instead: the already-rebased value
    # PRE-START LOCK read from PERSISTED STORE a moment ago, i.e. what was
    # actually seeded/kept as of the END of the PREVIOUS cycle. That's
    # the real decision being locked in; re-deriving it fresh THIS cycle
    # (`live_start_unit`/`planned_charge_time_units`) is only a fallback
    # for the (normal, non-split) cases that never went through a pending
    # lock at all (REASON_PRICE_NEGATIVE, REASON_VOYAGE_DEADLINE, ...) -
    # and, for duration specifically, re-deriving fresh is actively wrong
    # whenever the search is itself position-dependent (price-trim, and
    # now a chosen split): a single 15-minute step can shift which
    # partition looks optimal, so locking in with a fresh recompute could
    # commit to a materially different (start, duration) than what was
    # actually pending and acted on. `prev_pending_start`/the raw
    # "pending_plan_duration_units" read below come from PRE-START LOCK's
    # non-awaiting_split_b branch above, which always runs whenever
    # is_charging is True (awaiting_split_b requires `not is_charging`).
    if is_charging:
        # Safe to reference prev_pending_start/prev_pending_duration_raw
        # here unconditionally: awaiting_split_b (the only branch above
        # that does NOT define prev_pending_start) requires `not
        # is_charging`, so whenever is_charging is True PRE-START LOCK
        # always took its other (non-awaiting_split_b) branch instead.
        prev_pending_duration_raw = P.get("pending_plan_duration_units") if prev_pending_start is not None else None
        if resuming_split_b:
            fallback_start = int(prev_charge_plan_b_start)
        elif prev_pending_start is not None:
            fallback_start = int(prev_pending_start)
        else:
            fallback_start = live_start_unit
        charge_plan_start_unit = prev_charge_plan_start if prev_charge_plan_start is not None else fallback_start
    else:
        charge_plan_start_unit = None
    r["charge_plan_start_unit"] = charge_plan_start_unit
    # Same reasoning as pending_plan_reference_date above: whatever is
    # stored this cycle is, by construction, relative to today's midnight.
    r["charge_plan_reference_date"] = now.date().isoformat() if charge_plan_start_unit is not None else None

    prev_charge_plan_duration = P.get("charge_plan_duration_units")
    if is_charging:
        if resuming_split_b:
            fallback_duration = (
                int(prev_charge_plan_b_duration) if prev_charge_plan_b_duration is not None else planned_charge_time_units
            )
        elif prev_pending_duration_raw is not None:
            fallback_duration = int(prev_pending_duration_raw)
        else:
            fallback_duration = planned_charge_time_units
        charge_plan_duration_units = prev_charge_plan_duration if prev_charge_plan_duration is not None else fallback_duration
    else:
        charge_plan_duration_units = None
    r["charge_plan_duration_units"] = charge_plan_duration_units

    if is_charging and charge_plan_start_unit is not None and charge_plan_duration_units is not None:
        charge_plan_end_unit = int(charge_plan_start_unit) + int(charge_plan_duration_units)
    else:
        charge_plan_end_unit = None
    r["charge_plan_end_unit"] = charge_plan_end_unit

    # Block B reservation bookkeeping: seeded the moment block A locks in
    # (if this plan is itself a chosen split), held unchanged through
    # block A's charging and the pause after it, and consumed - cleared
    # back to None - the moment block B itself locks in (resuming_split_b
    # above), at which point block B proceeds as an entirely ordinary
    # (final) block, including the normal calibration/balance completion
    # guard on its own end.
    if resuming_split_b:
        charge_plan_b_start_unit = None
        charge_plan_b_duration_units = None
    elif prev_charge_plan_b_start is not None:
        charge_plan_b_start_unit = prev_charge_plan_b_start
        charge_plan_b_duration_units = prev_charge_plan_b_duration
    elif is_charging and prev_charge_plan_start is None:
        # Seed block A's own reservation for B, if this lock-in is itself
        # a chosen split. Deliberately read from PERSISTED STORE (last
        # cycle's pending_plan_b_*), not the pending_plan_b_start_unit
        # local variable computed above: PRE-START LOCK already cleared
        # that local variable to None THIS cycle (pending_mode == "clear"
        # fires on exactly this is_charging-just-became-True cycle), so
        # it can no longer be trusted as the seed source here.
        prev_pending_b_start_raw = P.get("pending_plan_b_start_unit")
        prev_pending_b_start = _rebase_unit(
            prev_pending_b_start_raw if _is_plain_number(prev_pending_b_start_raw) else None,
            P.get("pending_plan_b_reference_date"),
            now,
        )
        charge_plan_b_start_unit = prev_pending_b_start
        charge_plan_b_duration_units = (
            P.get("pending_plan_b_duration_units") if prev_pending_b_start is not None else None
        )
    else:
        charge_plan_b_start_unit = None
        charge_plan_b_duration_units = None
    r["charge_plan_b_start_unit"] = charge_plan_b_start_unit
    r["charge_plan_b_reference_date"] = now.date().isoformat() if charge_plan_b_start_unit is not None else None
    r["charge_plan_b_duration_units"] = charge_plan_b_duration_units

    # True throughout block A's own active charging (its stop must bypass
    # the calibration/balance guard below - see the state machine - since
    # this isn't really the end of the plan, just a deliberate pause
    # before block B resumes it). False everywhere else, including during
    # block B's own charging (charge_plan_b_start_unit has been consumed
    # by then), so block B's completion is governed normally.
    is_split_active = is_charging and charge_plan_b_start_unit is not None
    r["is_split_active"] = is_split_active

    # ---------------------------------------------------------------
    # SCHEDULE
    # ---------------------------------------------------------------
    if charge_plan_start_unit is not None:
        start_unit = int(charge_plan_start_unit)
    elif pending_plan_start_unit is not None:
        start_unit = int(pending_plan_start_unit)
    else:
        start_unit = live_start_unit
    r["start_unit"] = start_unit

    is_idle = (not is_charging) and charge_plan_start_unit is None and pending_plan_start_unit is None and not has_charge_need
    r["start_charge_time"] = "Idle" if is_idle else _fmt_unit_clock(start_unit, now)

    if is_charging:
        display_duration_units = int(charge_plan_duration_units or 0)
    elif pending_plan_duration_units is not None:
        display_duration_units = int(pending_plan_duration_units)
    else:
        display_duration_units = planned_charge_time_units
    r["stop_charge_time"] = "Idle" if is_idle else _fmt_unit_clock(start_unit + display_duration_units, now)

    # ---------------------------------------------------------------
    # DYNAMIC PRICE CEILING
    # ---------------------------------------------------------------
    r["soc_floor_percentage"] = soc_floor
    r["soc_ceiling_percentage"] = soc_ceiling
    r["current_soc"] = soc
    r["price_14d_average"] = price_14d_average
    price_ceiling_multiplier = round(max(1 - ((soc - soc_floor) / (soc_ceiling - soc_floor)), 0), 2)
    r["price_ceiling_multiplier"] = price_ceiling_multiplier
    r["price_limit"] = round(price_14d_average * price_ceiling_multiplier, 4)

    units_ahead = max(start_unit - interval_start_unit, 0)
    soc_at_window_start = round(max(soc - drain_per_unit * units_ahead, soc_floor), 2)
    r["soc_at_window_start"] = soc_at_window_start
    price_limit_at_window_start = round(
        price_14d_average * max(1 - ((soc_at_window_start - soc_floor) / (soc_ceiling - soc_floor)), 0), 4
    )
    r["price_limit_at_window_start"] = price_limit_at_window_start

    # What charge_kwh/gap_kwh looks like using the PROJECTED
    # soc_at_window_start above instead of the live soc - originally
    # (2026.10.16) a purely informational preview, but since 2026.10.17
    # this same projected-need idea IS also what the LOOK-AHEAD PASS
    # above already fed back into the actual plan (charge_kwh_planned/
    # charge_time_units_planned, and from there effective_start_index/
    # planned_charge_time_units/planned_charge_kwh) whenever the chosen
    # window sits far enough ahead for the drain to matter. So once a
    # plan is settled this will typically land very close to
    # charge_kwh_planned (same formula, now evaluated at start_unit -
    # the actual settled/pending/live start - instead of the lookahead
    # loop's own last candidate start) - this attribute is kept as the
    # simple, always-recomputed-fresh "what does the energy need look
    # like at the window's current start" figure for the dashboard,
    # while charge_kwh_planned/charge_time_units_planned are the ones
    # that actually drove the search.
    # Scope note: this (and the look-ahead pass itself) only projects
    # the PRIMARY window (block A). Block B's start in a chosen split
    # is not adjusted this way - its true projected SoC would need a
    # different calculation (block A's own delivered energy plus drain
    # during the pause, not simple linear drain from "now"), which
    # isn't implemented.
    projected_battery_energy_at_window_start = round(battery_size * (soc_at_window_start / 100), 2)
    r["projected_battery_energy_at_window_start"] = projected_battery_energy_at_window_start
    projected_gap_kwh_at_window_start = max(
        round(target_charge_level - projected_battery_energy_at_window_start, 1), 0.0
    )
    projected_charge_kwh_at_window_start = (
        projected_gap_kwh_at_window_start
        if is_voyage_charge_forced
        else min(projected_gap_kwh_at_window_start, session_cap_kwh)
    )
    r["projected_charge_kwh_at_window_start"] = projected_charge_kwh_at_window_start

    prev_charge_plan_price_limit = P.get("charge_plan_price_limit")
    if is_charging:
        charge_plan_price_limit = (
            prev_charge_plan_price_limit if prev_charge_plan_price_limit is not None else price_limit_at_window_start
        )
    else:
        charge_plan_price_limit = None
    r["charge_plan_price_limit"] = charge_plan_price_limit

    # ---------------------------------------------------------------
    # PROJECTED END SOC / TARGET SOC DISPLAY
    # ---------------------------------------------------------------
    hours_until_start = max(start_unit - interval_start_unit, 0) / 4
    consumption_until_start = (avg_load_w / 1000) * hours_until_start
    projected_end_soc = round((((battery_energy - consumption_until_start) + planned_charge_kwh) / battery_size) * 100, 2)
    r["projected_end_soc"] = projected_end_soc

    if pending_mode == "clear":
        pending_plan_target_soc = None
    elif pending_mode in ("seed", "reseed"):
        pending_plan_target_soc = projected_end_soc
    else:
        pending_plan_target_soc = P.get("pending_plan_target_soc")
    r["pending_plan_target_soc"] = pending_plan_target_soc

    prev_charge_plan_target_soc = P.get("charge_plan_target_soc")
    if is_charging:
        fallback_target_soc = pending_plan_target_soc if pending_plan_target_soc is not None else projected_end_soc
        charge_plan_target_soc = (
            prev_charge_plan_target_soc if prev_charge_plan_target_soc is not None else fallback_target_soc
        )
    else:
        charge_plan_target_soc = None
    r["charge_plan_target_soc"] = charge_plan_target_soc

    if has_no_charge_need or is_idle:
        target_soc = "Idle"
    else:
        locked_target = (
            charge_plan_target_soc
            if charge_plan_target_soc is not None
            else (pending_plan_target_soc if pending_plan_target_soc is not None else projected_end_soc)
        )
        target_soc = f"{round(float(locked_target), 1)} %"
    r["target_soc"] = target_soc

    # ---------------------------------------------------------------
    # CHARGE TIME REMAINING / COMPLETION
    # ---------------------------------------------------------------
    if charge_plan_duration_units is not None:
        remaining_duration_units = int(charge_plan_duration_units)
    elif pending_plan_duration_units is not None:
        remaining_duration_units = int(pending_plan_duration_units)
    else:
        remaining_duration_units = planned_charge_time_units
    planned_hours = remaining_duration_units / 4

    if shore_power_w > T[NUM_IS_CHARGING_THRESHOLD_W]:
        end_unit = int(charge_plan_end_unit) if charge_plan_end_unit is not None else (interval_start_unit + remaining_duration_units)
        remaining_units = end_unit - interval_start_unit
        charge_time_remaining = str(timedelta(minutes=remaining_units * 15)) if remaining_units > 0 else "Stop"
    elif is_idle:
        charge_time_remaining = "Idle"
    else:
        charge_time_remaining = f"{round(planned_hours * 60)} minuten"
    r["charge_time_remaining"] = charge_time_remaining
    is_duration_elapsed = charge_time_remaining == "Stop"
    r["is_duration_elapsed"] = is_duration_elapsed
    r["completion_ends_session"] = True

    # ---------------------------------------------------------------
    # DECISION FLAGS (price favorability, critical SoC, unplanned target)
    # ---------------------------------------------------------------
    window_price = pending_plan_average_price if pending_plan_average_price is not None else charge_window_average_price
    ceiling_price = charge_plan_price_limit if charge_plan_price_limit is not None else price_limit_at_window_start
    is_price_favorable = float(window_price) <= float(ceiling_price)
    r["is_price_favorable"] = is_price_favorable

    is_soc_critical = soc < T[NUM_SOC_CRITICAL_PERCENT]
    r["is_soc_critical"] = is_soc_critical
    is_soc_critical_at_window_start = soc_at_window_start < T[NUM_SOC_CRITICAL_PERCENT]
    r["is_soc_critical_at_window_start"] = is_soc_critical_at_window_start
    is_unplanned_target_reached = soc >= (low_battery_percentage + T[NUM_UNPLANNED_TARGET_BUFFER_PERCENT])
    r["is_unplanned_target_reached"] = is_unplanned_target_reached

    # ---------------------------------------------------------------
    # SESSION TRANSITION LATCHES
    # ---------------------------------------------------------------
    prev_is_planned_charge_start = pb("is_planned_charge_start", False)
    was_charging = pb("is_charging", False)
    if is_charging and not was_charging:
        is_planned_charge_start = pnum_or_none("pending_plan_start_unit") is not None or P.get("pending_plan_start_unit") is not None
    elif is_charging:
        is_planned_charge_start = prev_is_planned_charge_start
    else:
        is_planned_charge_start = False
    r["is_planned_charge_start"] = is_planned_charge_start

    prev_charge_session_ended_at = pdt("charge_session_ended_at")
    if is_charging:
        charge_session_ended_at = None
    elif was_charging:
        charge_session_ended_at = now
    else:
        charge_session_ended_at = prev_charge_session_ended_at
    r["charge_session_ended_at"] = charge_session_ended_at

    is_within_post_charge_lock = charge_session_ended_at is not None and now < (
        charge_session_ended_at + timedelta(minutes=T[NUM_COOLDOWN_MINUTES])
    )
    r["is_within_post_charge_lock"] = is_within_post_charge_lock

    cooldown_eligible = (not is_vacation_mode) and is_voyage_idle_settled
    is_within_cooldown = cooldown_eligible and is_within_post_charge_lock and soc > T[NUM_COOLDOWN_SOC_GUARD_PERCENT]
    r["is_within_cooldown"] = is_within_cooldown

    if pending_plan_start_unit is not None:
        best_window_starts_now = interval_start_unit >= int(pending_plan_start_unit)
    else:
        best_window_starts_now = effective_start_index == 0
    r["best_window_starts_now"] = best_window_starts_now

    # ---------------------------------------------------------------
    # STATE MACHINE
    # ---------------------------------------------------------------
    # charge_action_reason is set alongside every branch that enters
    # STATUS_START_CHARGE / STATUS_100_PERCENT_MODE / STATUS_STOP_CHARGE -
    # it's what drives the aan/afkoppel-script notification text below.
    # None everywhere else (nothing to report).
    charge_action_reason: str | None = None
    if is_grid_unavailable:
        system_status = STATUS_STANDBY
    elif is_100_percent_mode:
        system_status = STATUS_100_PERCENT_MODE
        charge_action_reason = REASON_FORCE_FULL_CHARGE
    elif is_price_negative and not is_drawing_any_power and not is_vacation_mode:
        system_status = STATUS_START_CHARGE
        charge_action_reason = REASON_PRICE_NEGATIVE
    elif is_price_negative and is_drawing_any_power and not is_vacation_mode:
        system_status = STATUS_ACTIEF
    elif (is_voyage_hold_active or (is_voyage_approach_due and has_charge_need)) and not is_charging:
        system_status = STATUS_START_CHARGE
        charge_action_reason = REASON_VOYAGE_DEADLINE
    elif is_charging and is_vacation_mode and is_vacation_soc_full and not is_voyage_charge_forced:
        system_status = STATUS_STOP_CHARGE
        charge_action_reason = REASON_VACATION_TARGET_REACHED
    elif (
        is_charging
        and not is_vacation_mode
        and not is_planned_charge_start
        and not effective_full_charge_cycle
        and not is_voyage_approach_due
        and is_unplanned_target_reached
    ):
        system_status = STATUS_STOP_CHARGE
        charge_action_reason = REASON_UNPLANNED_TARGET_REACHED
    elif is_charging and is_charge_complete and not is_voyage_hold_active and r["completion_ends_session"]:
        system_status = STATUS_STOP_CHARGE
        charge_action_reason = REASON_CHARGE_COMPLETE
    elif (
        is_charging
        and is_duration_elapsed
        and not is_voyage_approach_due
        and (is_split_active or not effective_full_charge_cycle or not charge_plan_voltage_target_reached)
    ):
        system_status = STATUS_STOP_CHARGE
        charge_action_reason = REASON_SPLIT_PAUSE if is_split_active else REASON_DURATION_ELAPSED
    elif is_charging:
        system_status = STATUS_ACTIEF
    elif is_within_cooldown:
        system_status = STATUS_COOLDOWN
    elif is_vacation_mode and is_vacation_soc_low:
        system_status = STATUS_START_CHARGE
        charge_action_reason = REASON_VACATION_LOW_SOC
    elif not is_vacation_mode and best_window_starts_now and has_charge_need and is_price_favorable:
        system_status = STATUS_START_CHARGE
        charge_action_reason = REASON_PLANNED_WINDOW
    elif not is_vacation_mode and best_window_starts_now and has_charge_need and is_soc_critical:
        system_status = STATUS_START_CHARGE
        charge_action_reason = REASON_SOC_CRITICAL
    elif not is_vacation_mode and has_charge_need and (is_price_favorable or is_soc_critical_at_window_start):
        system_status = STATUS_WAIT_FOR_START
    else:
        system_status = STATUS_STANDBY
    r["charge_action_reason"] = charge_action_reason

    # Edge-triggered (same reasoning as should_reset_vacation_departure_
    # target_days above): the aan/afkoppel-scripts must fire once, on the
    # cycle the status actually changes - not every cycle the status
    # happens to still read "Start charge"/"100% mode"/"Stop charge".
    # "Start charge" and "100% mode" both mean "we want it connected" (see
    # CHANGELOG) - physically plugging in shore power already connects it
    # regardless of these scripts (safety default), so this can also fire
    # straight into STATUS_STOP_CHARGE with no prior "Start charge" at all
    # (e.g. plugging in with no charge need) - that's intentional, not a
    # bug: the afkoppel-script is exactly what undoes that.
    was_system_status = P.get("system_status")
    is_connect_triggered = system_status in _CONNECT_STATUSES and was_system_status not in _CONNECT_STATUSES
    is_disconnect_triggered = system_status == STATUS_STOP_CHARGE and was_system_status != STATUS_STOP_CHARGE
    r["is_connect_triggered"] = is_connect_triggered
    r["is_disconnect_triggered"] = is_disconnect_triggered
    r["system_status"] = system_status

    return r
