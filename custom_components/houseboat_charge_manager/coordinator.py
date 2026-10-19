"""DataUpdateCoordinator that reads the configured entities and `number`
tunables every UPDATE_INTERVAL_SECONDS, runs logic.compute(), and persists
the small subset of the result that must survive a Home Assistant restart
(see logic.PERSISTED_KEYS).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import logic
from .const import (
    CONF_AVG_LOAD_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_CELL_MAX_VOLTAGE_ENTITY,
    CONF_CELL_MIN_VOLTAGE_ENTITY,
    CONF_FORCE_FULL_CHARGE_ENTITY,
    CONF_GRID_SWITCH_ENTITY,
    CONF_LOW_BATTERY_PERCENTAGE_ENTITY,
    CONF_NOTIFY_TARGETS,
    CONF_PRICE_14D_AVERAGE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SHORE_POWER_ENTITY,
    CONF_START_CHARGE_SCRIPT_ENTITY,
    CONF_START_VOYAGE_ENTITY,
    CONF_STOP_CHARGE_SCRIPT_ENTITY,
    CONF_VACATION_MODE_ENTITY,
    CONF_WALSTROOM_REQUESTED_ENTITY,
    DEFAULT_AUTO_TRIGGER_SCRIPTS,
    DEFAULT_VACATION_DEPARTURE_TARGET_DAYS,
    DOMAIN,
    NOTIFICATION_MESSAGES,
    NUM_VACATION_DEPARTURE_TARGET_DAYS,
    NUMBER_DEFINITIONS,
    STORAGE_KEY_SUFFIX,
    STORAGE_VERSION,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Entities where "unavailable"/"unknown"/missing must NEVER be silently read
# as 0 (or False) and fed into the calculation - a sensor dropout is not the
# same thing as "battery at 0%" or "0 W load", and treating it that way can
# make the planner force an unwanted charge session, or wrongly report the
# cells as perfectly balanced (both readings defaulting to the same 0).
# CONF_GRID_SWITCH_ENTITY is deliberately NOT in this list: that entity's
# own "unavailable" state is how is_grid_unavailable is detected in the
# first place (see below), so it's a meaningful signal there, not an error.
_CRITICAL_ENTITY_KEYS: tuple[tuple[str, str], ...] = (
    (CONF_BATTERY_SOC_ENTITY, "battery SoC"),
    (CONF_BATTERY_VOLTAGE_ENTITY, "battery voltage"),
    (CONF_CELL_MAX_VOLTAGE_ENTITY, "highest cell voltage"),
    (CONF_CELL_MIN_VOLTAGE_ENTITY, "lowest cell voltage"),
    (CONF_PRICE_ENTITY, "electricity price"),
    (CONF_PRICE_14D_AVERAGE_ENTITY, "14-day average price"),
    (CONF_SHORE_POWER_ENTITY, "shore power draw"),
    (CONF_AVG_LOAD_ENTITY, "average load"),
    (CONF_WALSTROOM_REQUESTED_ENTITY, "requested shore power"),
    (CONF_START_VOYAGE_ENTITY, "hours until departure"),
    (CONF_LOW_BATTERY_PERCENTAGE_ENTITY, "low-battery floor"),
)


def _state_float(hass: HomeAssistant, entity_id: str | None, default: float = 0.0) -> float:
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if state is None:
        return default
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return default


def _state_bool(hass: HomeAssistant, entity_id: str | None, default: bool = False) -> bool:
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if state is None:
        return default
    return state.state == "on"


def _state_unavailable(hass: HomeAssistant, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    return state is not None and state.state == "unavailable"


def _price_attr(hass: HomeAssistant, entity_id: str | None, attr: str) -> list[float] | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    value = state.attributes.get(attr)
    if not value:
        return None
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        return None


class HouseboatChargeManagerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the full charge-planning calculation every cycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}{STORAGE_KEY_SUFFIX}")
        self._prev: dict[str, Any] = {}
        self._number_values: dict[str, float] = {key: default for key, default, *_ in NUMBER_DEFINITIONS}
        # Registered by each HouseboatTunableNumber in number.py once it's
        # added to hass - lets the coordinator write a value back into the
        # live entity itself (not just its own cached copy), e.g. resetting
        # vacation_departure_target_days when shore power drops with no
        # departure pending.
        self._number_entities: dict[str, Any] = {}
        # Mirrors switch.py's HouseboatAutoTriggerScriptsSwitch - set True
        # here so a first-ever refresh (before the switch entity exists
        # yet) still behaves like "on", matching the switch's own default.
        self._auto_trigger_scripts_enabled: bool = DEFAULT_AUTO_TRIGGER_SCRIPTS

    async def async_load_persisted_state(self) -> None:
        stored = await self._store.async_load()
        self._prev = logic.deserialize_prev(stored)

    def set_number_value(self, key: str, value: float) -> None:
        """Called by number.py entities when the user changes a tunable."""
        self._number_values[key] = value

    def get_number_value(self, key: str, default: float = 0.0) -> float:
        return self._number_values.get(key, default)

    def register_number_entity(self, key: str, entity: Any) -> None:
        """Called by number.py once an entity is added to hass."""
        self._number_entities[key] = entity

    def unregister_number_entity(self, key: str) -> None:
        self._number_entities.pop(key, None)

    def set_auto_trigger_scripts_enabled(self, enabled: bool) -> None:
        """Called by switch.py when the user flips the switch (or on
        startup, restoring its last state)."""
        self._auto_trigger_scripts_enabled = enabled

    async def _async_reset_number_to_default(self, key: str, default: float) -> None:
        self._number_values[key] = default
        entity = self._number_entities.get(key)
        if entity is not None:
            await entity.async_set_native_value(default)

    def _conf(self, key: str) -> str | None:
        return {**self.entry.data, **self.entry.options}.get(key)

    async def _async_handle_charge_action(self, result: dict[str, Any]) -> None:
        """Fires the configured aan/afkoppel script + notification on the
        exact cycle system_status enters "Start charge"/"100% mode" or
        "Stop charge" (see logic.py's is_connect_triggered/
        is_disconnect_triggered). Both are no-ops if nothing is configured
        for them - this feature is entirely optional - and both are
        skipped entirely while the "Auto-trigger connect/disconnect
        scripts" switch (switch.py) is off: calling a script but not the
        notification describing it (or vice versa) would be misleading,
        so the switch gates both together.
        """
        if not self._auto_trigger_scripts_enabled:
            return
        reason_text = NOTIFICATION_MESSAGES.get(result.get("charge_action_reason"), "")
        if result.get("is_connect_triggered"):
            await self._async_trigger_script(self._conf(CONF_START_CHARGE_SCRIPT_ENTITY))
            await self._async_send_notifications("Walstroom - Aankoppelen", f"Aankoppelen: {reason_text}")
        elif result.get("is_disconnect_triggered"):
            await self._async_trigger_script(self._conf(CONF_STOP_CHARGE_SCRIPT_ENTITY))
            await self._async_send_notifications("Walstroom - Afkoppelen", f"Afkoppelen: {reason_text}")

    async def _async_trigger_script(self, script_entity_id: str | None) -> None:
        if not script_entity_id:
            return
        try:
            await self.hass.services.async_call(
                "script", "turn_on", {"entity_id": script_entity_id}, blocking=False
            )
        except Exception as err:  # noqa: BLE001 - a script failure must never break the coordinator
            _LOGGER.warning("Houseboat Charge Manager: could not call %s: %s", script_entity_id, err)

    async def _async_send_notifications(self, title: str, message: str) -> None:
        targets = self._conf(CONF_NOTIFY_TARGETS) or []
        if isinstance(targets, str):
            targets = [targets]
        for entity_id in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": entity_id, "title": title, "message": message},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001 - a notify failure must never break the coordinator
                _LOGGER.warning("Houseboat Charge Manager: could not notify %s: %s", entity_id, err)

    async def _async_update_data(self) -> dict[str, Any]:
        hass = self.hass
        now = dt_util.now()

        missing: list[str] = []
        for conf_key, label in _CRITICAL_ENTITY_KEYS:
            entity_id = self._conf(conf_key)
            state = hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in ("unavailable", "unknown", ""):
                missing.append(f"{label} ({entity_id or 'not configured'})")
        if missing:
            raise UpdateFailed(
                "Waiting for sensor data - unavailable, unknown or not configured: " + "; ".join(missing)
            )

        soc = _state_float(hass, self._conf(CONF_BATTERY_SOC_ENTITY))
        voltage = _state_float(hass, self._conf(CONF_BATTERY_VOLTAGE_ENTITY))
        cell_max_v = _state_float(hass, self._conf(CONF_CELL_MAX_VOLTAGE_ENTITY))
        cell_min_v = _state_float(hass, self._conf(CONF_CELL_MIN_VOLTAGE_ENTITY))

        price_entity = self._conf(CONF_PRICE_ENTITY)
        price_today = _price_attr(hass, price_entity, "today")
        price_tomorrow = _price_attr(hass, price_entity, "tomorrow")
        price_current = _state_float(hass, price_entity)
        price_14d_average = _state_float(hass, self._conf(CONF_PRICE_14D_AVERAGE_ENTITY))

        shore_power_w = _state_float(hass, self._conf(CONF_SHORE_POWER_ENTITY))
        avg_load_w = _state_float(hass, self._conf(CONF_AVG_LOAD_ENTITY))
        walstroom_requested_amps = _state_float(hass, self._conf(CONF_WALSTROOM_REQUESTED_ENTITY))
        start_voyage_hours_raw = _state_float(hass, self._conf(CONF_START_VOYAGE_ENTITY))
        low_battery_percentage = _state_float(hass, self._conf(CONF_LOW_BATTERY_PERCENTAGE_ENTITY))
        is_vacation_mode = _state_bool(hass, self._conf(CONF_VACATION_MODE_ENTITY))
        is_force_full_charge = _state_bool(hass, self._conf(CONF_FORCE_FULL_CHARGE_ENTITY))
        is_grid_unavailable = _state_unavailable(hass, self._conf(CONF_GRID_SWITCH_ENTITY))

        try:
            result = logic.compute(
                now=now,
                soc=soc,
                voltage=voltage,
                cell_max_v=cell_max_v,
                cell_min_v=cell_min_v,
                price_today=price_today,
                price_tomorrow=price_tomorrow,
                price_current=price_current,
                price_14d_average=price_14d_average,
                shore_power_w=shore_power_w,
                avg_load_w=avg_load_w,
                walstroom_requested_amps=walstroom_requested_amps,
                start_voyage_hours_raw=start_voyage_hours_raw,
                low_battery_percentage=low_battery_percentage,
                is_vacation_mode=is_vacation_mode,
                is_force_full_charge=is_force_full_charge,
                is_grid_unavailable=is_grid_unavailable,
                tunables=self._number_values,
                prev=self._prev,
            )
        except Exception as err:  # noqa: BLE001 - never let a bad cycle crash the coordinator
            raise UpdateFailed(f"Houseboat Charge Manager calculation failed: {err}") from err

        if result.get("should_reset_vacation_departure_target_days"):
            await self._async_reset_number_to_default(
                NUM_VACATION_DEPARTURE_TARGET_DAYS, DEFAULT_VACATION_DEPARTURE_TARGET_DAYS
            )

        await self._async_handle_charge_action(result)

        self._prev = result
        await self._store.async_save(logic.serialize_prev(result))
        return result
