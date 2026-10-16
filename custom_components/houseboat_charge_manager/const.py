"""Constants for the Houseboat Charge Manager integration.

Ported from a hand-written Home Assistant trigger-based template sensor
(``sensor.ess_charge_manager``) into a configurable, HACS-installable
custom integration. Every external entity the original sensor hardcoded is
now picked in the config flow (CONF_* below); every tunable threshold the
original sensor hardcoded as a bare number in a Jinja template is now a
``number`` entity (NUM_* below) that can be adjusted from the UI without
editing YAML.
"""
from __future__ import annotations

DOMAIN = "houseboat_charge_manager"
DEFAULT_NAME = "Houseboat Charge Manager"

PLATFORMS = ["number", "sensor"]

UPDATE_INTERVAL_SECONDS = 15

STORAGE_VERSION = 1
STORAGE_KEY_SUFFIX = "_houseboat_charge_manager_state"

CONF_NAME = "name"

# ---------------------------------------------------------------------------
# Config entry keys - external entities (picked once in the config flow,
# editable later via Configure). These replace every hardcoded entity_id in
# the original sensor's Jinja templates.
# ---------------------------------------------------------------------------
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_VOLTAGE_ENTITY = "battery_voltage_entity"
CONF_CELL_MAX_VOLTAGE_ENTITY = "cell_max_voltage_entity"
CONF_CELL_MIN_VOLTAGE_ENTITY = "cell_min_voltage_entity"

# A Nordpool-style sensor whose state is the current price and whose
# `today` / `tomorrow` attributes are the 15-minute (or hourly) price
# arrays for the day - matches sensor.nordpool_kwh_nl_eur_3_10_0 from the
# original setup. Used for both is_price_negative and the price arrays, so
# the original's separate sensor.stroomprijs_huidig helper is no longer
# needed.
CONF_PRICE_ENTITY = "price_entity"
# A 14-day rolling average price sensor (e.g. a Home Assistant statistics
# helper) - the basis of the dynamic price ceiling. Kept external rather
# than computed internally, same as the original.
CONF_PRICE_14D_AVERAGE_ENTITY = "price_14d_average_entity"

CONF_SHORE_POWER_ENTITY = "shore_power_entity"
CONF_AVG_LOAD_ENTITY = "avg_load_entity"
CONF_WALSTROOM_REQUESTED_ENTITY = "walstroom_requested_entity"
CONF_START_VOYAGE_ENTITY = "start_voyage_entity"
CONF_LOW_BATTERY_PERCENTAGE_ENTITY = "low_battery_percentage_entity"
CONF_VACATION_MODE_ENTITY = "vacation_mode_entity"
CONF_FORCE_FULL_CHARGE_ENTITY = "force_full_charge_entity"
# The physical shore-power relay/switch - watched purely for 'unavailable'
# (grid physically absent), same as the original's is_grid_unavailable.
CONF_GRID_SWITCH_ENTITY = "grid_switch_entity"

REQUIRED_ENTITY_KEYS = (
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_CELL_MAX_VOLTAGE_ENTITY,
    CONF_CELL_MIN_VOLTAGE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_14D_AVERAGE_ENTITY,
    CONF_SHORE_POWER_ENTITY,
    CONF_AVG_LOAD_ENTITY,
    CONF_WALSTROOM_REQUESTED_ENTITY,
    CONF_START_VOYAGE_ENTITY,
    CONF_LOW_BATTERY_PERCENTAGE_ENTITY,
    CONF_VACATION_MODE_ENTITY,
    CONF_FORCE_FULL_CHARGE_ENTITY,
    CONF_GRID_SWITCH_ENTITY,
)

# ---------------------------------------------------------------------------
# Tunables - exposed as `number` entities (number.py) rather than baked
# into Jinja, so they can be adjusted from the dashboard like any other
# Home Assistant entity. NUM_* is both the config-flow seed key and the
# entity's unique-id suffix. Defaults match the values hardcoded in the
# original sensor.
# ---------------------------------------------------------------------------
NUM_BATTERY_SIZE_KWH = "battery_size_kwh"
DEFAULT_BATTERY_SIZE_KWH = 36.0

NUM_CHARGER_EFFICIENCY_PERCENT = "charger_efficiency_percent"
DEFAULT_CHARGER_EFFICIENCY_PERCENT = 92.0

NUM_SOC_FLOOR_PERCENT = "soc_floor_percent"
DEFAULT_SOC_FLOOR_PERCENT = 40.0
NUM_SOC_CEILING_PERCENT = "soc_ceiling_percent"
DEFAULT_SOC_CEILING_PERCENT = 90.0

NUM_VACATION_FLOOR_PERCENT = "vacation_floor_percent"
DEFAULT_VACATION_FLOOR_PERCENT = 70.0
NUM_VACATION_CEILING_PERCENT = "vacation_ceiling_percent"
DEFAULT_VACATION_CEILING_PERCENT = 90.0

NUM_FLAT_BLOCK_MARGIN = "flat_block_price_margin"
DEFAULT_FLAT_BLOCK_MARGIN = 1.08
NUM_FLAT_BLOCK_TOLERANCE = "flat_block_price_tolerance"
DEFAULT_FLAT_BLOCK_TOLERANCE = 0.02

NUM_CALIBRATION_INTERVAL_DAYS = "calibration_interval_days"
DEFAULT_CALIBRATION_INTERVAL_DAYS = 30.0

NUM_BALANCE_VOLTAGE_THRESHOLD = "balance_voltage_threshold"
DEFAULT_BALANCE_VOLTAGE_THRESHOLD = 27.8
NUM_CELL_BALANCE_TOLERANCE = "cell_balance_tolerance_v"
DEFAULT_CELL_BALANCE_TOLERANCE = 0.02
NUM_BALANCE_SETTLE_MINUTES = "balance_settle_minutes"
DEFAULT_BALANCE_SETTLE_MINUTES = 60.0

NUM_COOLDOWN_MINUTES = "cooldown_minutes"
DEFAULT_COOLDOWN_MINUTES = 120.0
NUM_COOLDOWN_SOC_GUARD_PERCENT = "cooldown_soc_guard_percent"
DEFAULT_COOLDOWN_SOC_GUARD_PERCENT = 50.0

NUM_VOYAGE_IDLE_SETTLE_HOURS = "voyage_idle_settle_hours"
DEFAULT_VOYAGE_IDLE_SETTLE_HOURS = 3.0
NUM_VOYAGE_SETTLE_MARGIN_HOURS = "voyage_settle_margin_hours"
DEFAULT_VOYAGE_SETTLE_MARGIN_HOURS = 1.0
NUM_VOYAGE_APPROACH_BUFFER_HOURS = "voyage_approach_buffer_hours"
DEFAULT_VOYAGE_APPROACH_BUFFER_HOURS = 2.0
NUM_VOYAGE_POST_DEADLINE_HOLD_HOURS = "voyage_post_deadline_hold_hours"
DEFAULT_VOYAGE_POST_DEADLINE_HOLD_HOURS = 2.0

NUM_UNPLANNED_TARGET_BUFFER_PERCENT = "unplanned_target_buffer_percent"
DEFAULT_UNPLANNED_TARGET_BUFFER_PERCENT = 10.0

NUM_SESSION_CAP_HOURS = "session_cap_hours"
DEFAULT_SESSION_CAP_HOURS = 30.0

NUM_CALIBRATION_GRID_BUFFER_MINUTES = "calibration_grid_buffer_minutes"
DEFAULT_CALIBRATION_GRID_BUFFER_MINUTES = 120.0

NUM_HAS_CHARGE_NEED_THRESHOLD_KWH = "has_charge_need_threshold_kwh"
DEFAULT_HAS_CHARGE_NEED_THRESHOLD_KWH = 2.0

NUM_IS_CHARGING_THRESHOLD_W = "is_charging_threshold_w"
DEFAULT_IS_CHARGING_THRESHOLD_W = 200.0
NUM_IS_DRAWING_ANY_POWER_THRESHOLD_W = "is_drawing_any_power_threshold_w"
DEFAULT_IS_DRAWING_ANY_POWER_THRESHOLD_W = 100.0
NUM_HIGH_SHORE_LOAD_THRESHOLD_W = "high_shore_load_threshold_w"
DEFAULT_HIGH_SHORE_LOAD_THRESHOLD_W = 500.0

NUM_SOC_CRITICAL_PERCENT = "soc_critical_percent"
DEFAULT_SOC_CRITICAL_PERCENT = 40.0

# In vacation mode, how many days of usable energy (above the low-battery
# floor) should be available at departure time. Replaces the fixed 90%
# vacation ceiling with a dynamic target; the default (15) is deliberately
# large enough to saturate at 100% for any realistic load/battery size, so
# "limited" vacation charging is an opt-in the user dials down themselves.
NUM_VACATION_DEPARTURE_TARGET_DAYS = "vacation_departure_target_days"
DEFAULT_VACATION_DEPARTURE_TARGET_DAYS = 15.0

# Extra hours before the actual departure time that charging must be
# finished, kept as spare buffer for unexpected consumption during
# charging. Inflates the day-based target by that many hours' worth of
# load, and shifts the voyage charge-by deadline earlier by the same
# amount (vacation mode only - deliberately separate from
# voyage_settle_margin_hours, which still applies to the non-vacation
# voyage-timing path unchanged).
NUM_VACATION_DEPARTURE_BUFFER_HOURS = "vacation_departure_buffer_hours"
DEFAULT_VACATION_DEPARTURE_BUFFER_HOURS = 2.0

# (key, default, unit, min, max, step, icon, category, name)
NUMBER_DEFINITIONS: tuple[tuple, ...] = (
    (NUM_BATTERY_SIZE_KWH, DEFAULT_BATTERY_SIZE_KWH, "kWh", 1, 500, 0.5, "mdi:battery-high", "Battery pack size"),
    (NUM_CHARGER_EFFICIENCY_PERCENT, DEFAULT_CHARGER_EFFICIENCY_PERCENT, "%", 50, 100, 0.5, "mdi:transmission-tower", "Charger efficiency"),
    (NUM_SOC_FLOOR_PERCENT, DEFAULT_SOC_FLOOR_PERCENT, "%", 0, 100, 1, "mdi:battery-low", "Price-ceiling SoC floor"),
    (NUM_SOC_CEILING_PERCENT, DEFAULT_SOC_CEILING_PERCENT, "%", 0, 100, 1, "mdi:battery-high", "Price-ceiling SoC ceiling / normal charge target"),
    (NUM_VACATION_FLOOR_PERCENT, DEFAULT_VACATION_FLOOR_PERCENT, "%", 0, 100, 1, "mdi:beach", "Vacation charge floor"),
    (NUM_VACATION_CEILING_PERCENT, DEFAULT_VACATION_CEILING_PERCENT, "%", 0, 100, 1, "mdi:beach", "Vacation charge ceiling"),
    (NUM_FLAT_BLOCK_MARGIN, DEFAULT_FLAT_BLOCK_MARGIN, None, 1.0, 2.0, 0.01, "mdi:chart-timeline-variant", "Flat-block price margin"),
    (NUM_FLAT_BLOCK_TOLERANCE, DEFAULT_FLAT_BLOCK_TOLERANCE, "€/kWh", 0, 0.5, 0.005, "mdi:chart-timeline-variant", "Flat-block price tolerance"),
    (NUM_CALIBRATION_INTERVAL_DAYS, DEFAULT_CALIBRATION_INTERVAL_DAYS, "d", 1, 180, 1, "mdi:calendar-sync", "Calibration cycle interval"),
    (NUM_BALANCE_VOLTAGE_THRESHOLD, DEFAULT_BALANCE_VOLTAGE_THRESHOLD, "V", 0, 60, 0.1, "mdi:flash", "Full-voltage threshold"),
    (NUM_CELL_BALANCE_TOLERANCE, DEFAULT_CELL_BALANCE_TOLERANCE, "V", 0, 1, 0.005, "mdi:scale-balance", "Cell balance tolerance"),
    (NUM_BALANCE_SETTLE_MINUTES, DEFAULT_BALANCE_SETTLE_MINUTES, "min", 0, 600, 5, "mdi:timer-sand", "Balance settle time"),
    (NUM_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES, "min", 0, 600, 5, "mdi:timer-sand", "Cooldown duration"),
    (NUM_COOLDOWN_SOC_GUARD_PERCENT, DEFAULT_COOLDOWN_SOC_GUARD_PERCENT, "%", 0, 100, 1, "mdi:battery-alert", "Cooldown SoC guard"),
    (NUM_VOYAGE_IDLE_SETTLE_HOURS, DEFAULT_VOYAGE_IDLE_SETTLE_HOURS, "h", 0, 48, 0.5, "mdi:sail-boat", "Voyage idle settle time"),
    (NUM_VOYAGE_SETTLE_MARGIN_HOURS, DEFAULT_VOYAGE_SETTLE_MARGIN_HOURS, "h", 0, 12, 0.25, "mdi:sail-boat", "Voyage charge settle margin"),
    (NUM_VOYAGE_APPROACH_BUFFER_HOURS, DEFAULT_VOYAGE_APPROACH_BUFFER_HOURS, "h", 0, 24, 0.5, "mdi:sail-boat", "Voyage approach buffer"),
    (NUM_VOYAGE_POST_DEADLINE_HOLD_HOURS, DEFAULT_VOYAGE_POST_DEADLINE_HOLD_HOURS, "h", 0, 24, 0.5, "mdi:sail-boat", "Voyage post-deadline hold"),
    (NUM_UNPLANNED_TARGET_BUFFER_PERCENT, DEFAULT_UNPLANNED_TARGET_BUFFER_PERCENT, "%", 0, 50, 1, "mdi:shield-check", "Unplanned-session safety buffer"),
    (NUM_SESSION_CAP_HOURS, DEFAULT_SESSION_CAP_HOURS, "h", 1, 100, 1, "mdi:clock-outline", "Session energy cap (hours of avg load)"),
    (NUM_CALIBRATION_GRID_BUFFER_MINUTES, DEFAULT_CALIBRATION_GRID_BUFFER_MINUTES, "min", 0, 600, 5, "mdi:timer-sand", "Full-charge grid buffer"),
    (NUM_HAS_CHARGE_NEED_THRESHOLD_KWH, DEFAULT_HAS_CHARGE_NEED_THRESHOLD_KWH, "kWh", 0, 20, 0.1, "mdi:battery-charging", "Minimum charge need"),
    (NUM_IS_CHARGING_THRESHOLD_W, DEFAULT_IS_CHARGING_THRESHOLD_W, "W", 0, 5000, 10, "mdi:power-plug", "Charging-detected threshold"),
    (NUM_IS_DRAWING_ANY_POWER_THRESHOLD_W, DEFAULT_IS_DRAWING_ANY_POWER_THRESHOLD_W, "W", 0, 5000, 10, "mdi:power-plug-outline", "Any-draw-detected threshold"),
    (NUM_HIGH_SHORE_LOAD_THRESHOLD_W, DEFAULT_HIGH_SHORE_LOAD_THRESHOLD_W, "W", 0, 5000, 10, "mdi:power-plug", "High shore-load threshold"),
    (NUM_SOC_CRITICAL_PERCENT, DEFAULT_SOC_CRITICAL_PERCENT, "%", 0, 100, 1, "mdi:battery-alert", "Critical SoC threshold"),
    (NUM_VACATION_DEPARTURE_TARGET_DAYS, DEFAULT_VACATION_DEPARTURE_TARGET_DAYS, "d", 1, 15, 1, "mdi:calendar-clock", "Vacation departure runway (days)"),
    (NUM_VACATION_DEPARTURE_BUFFER_HOURS, DEFAULT_VACATION_DEPARTURE_BUFFER_HOURS, "h", 0, 12, 0.5, "mdi:timer-sand", "Vacation departure buffer"),
)

# system_status string values - kept identical to the original sensor so
# existing automations/dashboards that match on these strings keep working.
STATUS_STANDBY = "Standby"
STATUS_100_PERCENT_MODE = "100% mode"
STATUS_START_CHARGE = "Start charge"
STATUS_ACTIEF = "Actief"
STATUS_STOP_CHARGE = "Stop charge"
STATUS_COOLDOWN = "Cooldown"
STATUS_WAIT_FOR_START = "Wait for start"
