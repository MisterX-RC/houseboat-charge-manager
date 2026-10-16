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
from datetime import datetime, timedelta
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
    NUM_SESSION_CAP_HOURS,
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
    "pending_plan_duration_units",
    "pending_plan_average_price",
    "pending_plan_target_soc",
    "charge_plan_start_unit",
    "charge_plan_duration_units",
    "charge_plan_end_unit",
    "charge_plan_price_limit",
    "charge_plan_target_soc",
    "is_planned_charge_start",
    "charge_session_ended_at",
    "is_charging",
    "is_within_post_charge_lock",
    "is_grid_unavailable",
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


def _fmt_unit_clock(unit: int) -> str:
    """Render an absolute 15-minute unit index (0 = midnight today) as a
    clock time, prefixed with "Morgen" past unit 96 (tomorrow) - matches
    the original sensor's start_charge_time/stop_charge_time formatting.
    """
    if unit > UNITS_PER_DAY:
        unit -= UNITS_PER_DAY
        prefix = "Morgen "
    else:
        prefix = ""
    hour = unit // UNITS_PER_HOUR
    minute = (unit - hour * UNITS_PER_HOUR) * UNIT_MINUTES
    return f"{prefix}{hour:02d}:{minute:02d}"


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

    session_cap_kwh = round((avg_load_w * T[NUM_SESSION_CAP_HOURS]) / 1000, 1)
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
    charge_time_units = math.ceil((charge_time_minutes + calibration_grid_buffer_minutes) / 15)
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
    normal_horizon = 0 if shore_power_w > T[NUM_HIGH_SHORE_LOAD_THRESHOLD_W] else units_left
    if is_voyage_pending and not is_vacation_mode:
        units_into_hour = interval_start_unit - (now.hour * 4)
        voyage_deadline_units = round(start_voyage_hours * 4) - units_into_hour
        voyage_horizon = max(voyage_deadline_units, 0)
        last_considered_interval_index = min(normal_horizon, voyage_horizon)
    else:
        last_considered_interval_index = normal_horizon
    r["last_considered_interval_index"] = last_considered_interval_index

    duration = max(charge_time_units, 1)
    horizon = max(last_considered_interval_index, 0)
    soc_floor = float(T[NUM_SOC_FLOOR_PERCENT])
    soc_ceiling = float(T[NUM_SOC_CEILING_PERCENT])
    drain_per_unit = battery_drain_rate_percent_per_hour / 4

    best_average, best_index = 1000.0, -1
    for idx in range(horizon):
        window = interval_price[idx : idx + duration]
        if not window:
            continue
        window_average = _avg(window)
        if window_average < best_average:
            best_average, best_index = window_average, idx
    cheapest_start_index = best_index if best_index >= 0 else 0
    r["cheapest_start_index"] = cheapest_start_index

    best_average, best_index = 1000.0, -1
    for idx in range(horizon):
        window = interval_price[idx : idx + duration]
        if not window:
            continue
        window_average = _avg(window)
        projected_soc = max(soc - drain_per_unit * idx, soc_floor)
        projected_ceiling = price_14d_average * max(1 - ((projected_soc - soc_floor) / (soc_ceiling - soc_floor)), 0)
        if window_average <= projected_ceiling and window_average < best_average:
            best_average, best_index = window_average, idx
    price_limited_start_index = best_index
    r["price_limited_start_index"] = price_limited_start_index

    anchor = cheapest_start_index
    full_duration = max(charge_time_units, 1)
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

    if price_limited_start_index >= 0:
        start_index = price_limited_start_index
    elif price_trim_duration_units < charge_time_units:
        start_index = price_trim_start_index
    else:
        start_index = cheapest_start_index
    r["start_index"] = start_index
    effective_duration_units = (
        charge_time_units if price_limited_start_index >= 0 else price_trim_duration_units
    )
    r["effective_duration_units"] = effective_duration_units
    r["low_price"] = min(all_price) if all_price else 0.0

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

    percentage_threshold = (
        charge_window_average_price * flat_block_price_margin
        if charge_window_average_price >= 0
        else charge_window_average_price / flat_block_price_margin
    )
    absolute_threshold = charge_window_average_price + flat_block_price_tolerance
    flat_block_price_threshold = max(percentage_threshold, absolute_threshold)
    r["flat_block_price_threshold"] = flat_block_price_threshold

    headroom_kwh = target_charge_level - battery_energy - charge_kwh
    if battery_charge_speed > 0 and charger_efficiency > 0:
        max_extra_units = math.floor(headroom_kwh / ((battery_charge_speed / 4) * charger_efficiency))
    else:
        max_extra_units = 0
    max_extra_units = max(max_extra_units, 0)

    extending, extra_units = True, 0
    for i in range(48):
        idx = start + ext_duration + i
        if extending and extra_units < max_extra_units and idx < len(interval_price) and interval_price[idx] <= flat_block_price_threshold:
            extra_units += 1
        else:
            extending = False
    forward_units = extra_units
    r["charge_window_forward_extension_units"] = forward_units

    remaining_budget = max(max_extra_units - forward_units, 0)
    extending, extra_units = True, 0
    for i in range(48):
        idx = start - 1 - i
        if extending and extra_units < remaining_budget and idx >= 0 and interval_price[idx] <= flat_block_price_threshold:
            extra_units += 1
        else:
            extending = False
    backward_units = extra_units
    r["charge_window_backward_extension_units"] = backward_units

    charge_window_extension_units = forward_units + backward_units
    r["charge_window_extension_units"] = charge_window_extension_units
    effective_start_index = max(start - backward_units, 0)
    r["effective_start_index"] = effective_start_index

    planned_charge_time_units = effective_duration_units + charge_window_extension_units
    r["planned_charge_time_units"] = planned_charge_time_units
    r["planned_charge_time_hours"] = round(planned_charge_time_units / 4, 2)

    if effective_duration_units < max(charge_time_units, 1):
        effective_charge_kwh = round(max(effective_duration_units, 1) * (battery_charge_speed / 4) * charger_efficiency, 2)
    else:
        effective_charge_kwh = charge_kwh
    r["effective_charge_kwh"] = effective_charge_kwh

    planned_charge_kwh = round(
        effective_charge_kwh + (charge_window_extension_units * (battery_charge_speed / 4) * charger_efficiency), 2
    )
    r["planned_charge_kwh"] = planned_charge_kwh
    planned_charge_kwh_gross = (
        round(planned_charge_kwh / charger_efficiency, 2) if charger_efficiency > 0 else planned_charge_kwh
    )
    r["planned_charge_kwh_gross"] = planned_charge_kwh_gross
    r["best_interval_price"] = interval_price[effective_start_index : effective_start_index + planned_charge_time_units]

    # ---------------------------------------------------------------
    # PRE-START LOCK
    # ---------------------------------------------------------------
    is_start_imminent = (not is_charging) and has_charge_need and (effective_start_index <= 1)
    r["is_start_imminent"] = is_start_imminent

    prev_pending_start_raw = P.get("pending_plan_start_unit")
    prev_pending_start = prev_pending_start_raw if _is_plain_number(prev_pending_start_raw) else None
    qualifies = (price_limited_start_index >= 0) or (effective_duration_units < charge_time_units)
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
    elif pending_mode in ("seed", "reseed"):
        pending_plan_start_unit = live_start_unit
        pending_plan_duration_units = planned_charge_time_units
        pending_plan_average_price = charge_window_average_price
    else:
        pending_plan_start_unit = prev_pending_start
        pending_plan_duration_units = P.get("pending_plan_duration_units")
        pending_plan_average_price = P.get("pending_plan_average_price")
    r["pending_plan_start_unit"] = pending_plan_start_unit
    r["pending_plan_duration_units"] = pending_plan_duration_units
    r["pending_plan_average_price"] = pending_plan_average_price

    # ---------------------------------------------------------------
    # SESSION LOCK-IN
    # ---------------------------------------------------------------
    prev_charge_plan_start = P.get("charge_plan_start_unit")
    if is_charging:
        fallback_start = pending_plan_start_unit if pending_plan_start_unit is not None else live_start_unit
        charge_plan_start_unit = prev_charge_plan_start if prev_charge_plan_start is not None else fallback_start
    else:
        charge_plan_start_unit = None
    r["charge_plan_start_unit"] = charge_plan_start_unit

    prev_charge_plan_duration = P.get("charge_plan_duration_units")
    if is_charging:
        fallback_duration = (
            pending_plan_duration_units if pending_plan_duration_units is not None else planned_charge_time_units
        )
        charge_plan_duration_units = prev_charge_plan_duration if prev_charge_plan_duration is not None else fallback_duration
    else:
        charge_plan_duration_units = None
    r["charge_plan_duration_units"] = charge_plan_duration_units

    if is_charging and charge_plan_start_unit is not None and charge_plan_duration_units is not None:
        charge_plan_end_unit = int(charge_plan_start_unit) + int(charge_plan_duration_units)
    else:
        charge_plan_end_unit = None
    r["charge_plan_end_unit"] = charge_plan_end_unit

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
    r["start_charge_time"] = "Idle" if is_idle else _fmt_unit_clock(start_unit)

    if is_charging:
        display_duration_units = int(charge_plan_duration_units or 0)
    elif pending_plan_duration_units is not None:
        display_duration_units = int(pending_plan_duration_units)
    else:
        display_duration_units = planned_charge_time_units
    r["stop_charge_time"] = "Idle" if is_idle else _fmt_unit_clock(start_unit + display_duration_units)

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
    if is_grid_unavailable:
        system_status = STATUS_STANDBY
    elif is_100_percent_mode:
        system_status = STATUS_100_PERCENT_MODE
    elif is_price_negative and not is_drawing_any_power and not is_vacation_mode:
        system_status = STATUS_START_CHARGE
    elif is_price_negative and is_drawing_any_power and not is_vacation_mode:
        system_status = STATUS_ACTIEF
    elif (is_voyage_hold_active or (is_voyage_approach_due and has_charge_need)) and not is_charging:
        system_status = STATUS_START_CHARGE
    elif is_charging and is_vacation_mode and is_vacation_soc_full and not is_voyage_charge_forced:
        system_status = STATUS_STOP_CHARGE
    elif (
        is_charging
        and not is_vacation_mode
        and not is_planned_charge_start
        and not effective_full_charge_cycle
        and not is_voyage_approach_due
        and is_unplanned_target_reached
    ):
        system_status = STATUS_STOP_CHARGE
    elif is_charging and is_charge_complete and not is_voyage_hold_active and r["completion_ends_session"]:
        system_status = STATUS_STOP_CHARGE
    elif (
        is_charging
        and is_duration_elapsed
        and not is_voyage_approach_due
        and (not effective_full_charge_cycle or not charge_plan_voltage_target_reached)
    ):
        system_status = STATUS_STOP_CHARGE
    elif is_charging:
        system_status = STATUS_ACTIEF
    elif is_within_cooldown:
        system_status = STATUS_COOLDOWN
    elif is_vacation_mode and is_vacation_soc_low:
        system_status = STATUS_START_CHARGE
    elif not is_vacation_mode and best_window_starts_now and has_charge_need and is_price_favorable:
        system_status = STATUS_START_CHARGE
    elif not is_vacation_mode and best_window_starts_now and has_charge_need and is_soc_critical:
        system_status = STATUS_START_CHARGE
    elif not is_vacation_mode and has_charge_need and (is_price_favorable or is_soc_critical_at_window_start):
        system_status = STATUS_WAIT_FOR_START
    else:
        system_status = STATUS_STANDBY
    r["system_status"] = system_status

    return r
