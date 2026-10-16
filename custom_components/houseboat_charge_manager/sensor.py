"""Sensor platform.

- HouseboatStatusSensor: the main sensor, state = system_status (the exact
  same string values the original sensor.ess_charge_manager used), with the
  full calculation result as attributes - this is what houseboat_cards.js
  reads (today/tomorrow price arrays, charge_plan_*, pending_plan_*, etc.),
  so the bundled dashboard card works against it with no changes beyond
  pointing `entity:` at the new entity_id.
- HouseboatValueSensor: the "charge targets etc." as their own standalone
  sensors, for easy use in automations/history graphs without having to
  reach into the status sensor's attributes.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HouseboatChargeManagerCoordinator

# (key, name, result_key, unit, icon)
VALUE_SENSORS: tuple[tuple[str, str, str, str | None, str], ...] = (
    ("charge_target_percentage", "Charge target percentage", "target_charge_percentage", "%", "mdi:battery-charging-high"),
    ("projected_end_soc", "Projected end SoC", "projected_end_soc", "%", "mdi:battery-charging-100"),
    ("target_soc_display", "Charge target (display)", "target_soc", None, "mdi:battery-charging-high"),
    ("charge_kwh", "Charge energy needed", "charge_kwh", "kWh", "mdi:battery-charging"),
    ("planned_charge_kwh", "Planned charge energy", "planned_charge_kwh", "kWh", "mdi:battery-charging"),
    ("planned_charge_kwh_gross", "Planned grid draw", "planned_charge_kwh_gross", "kWh", "mdi:transmission-tower"),
    ("start_charge_time", "Charge start time", "start_charge_time", None, "mdi:clock-start"),
    ("stop_charge_time", "Charge stop time", "stop_charge_time", None, "mdi:clock-end"),
    ("charge_time_remaining", "Charge time remaining", "charge_time_remaining", None, "mdi:timer-sand"),
    ("days_since_full_charge", "Days since full charge", "days_since_full_charge", "d", "mdi:calendar-clock"),
    ("price_limit", "Price limit (now)", "price_limit", "EUR/kWh", "mdi:currency-eur"),
    ("price_limit_at_window_start", "Price limit (at window start)", "price_limit_at_window_start", "EUR/kWh", "mdi:currency-eur"),
    ("charge_window_average_price", "Charge window average price", "charge_window_average_price", "EUR/kWh", "mdi:currency-eur"),
    ("battery_energy", "Battery energy", "battery_energy", "kWh", "mdi:battery"),
    ("usable_energy", "Usable energy", "usable_energy", "kWh", "mdi:battery-50"),
    ("dynamic_voyage_charge_hours", "Hours needed to reach full for departure", "dynamic_voyage_charge_hours", "h", "mdi:sail-boat"),
    ("vacation_departure_target_percent", "Vacation departure target", "vacation_departure_target_percent", "%", "mdi:beach"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HouseboatChargeManagerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_info: DeviceInfo = hass.data[DOMAIN][entry.entry_id]["device_info"]

    entities: list[SensorEntity] = [HouseboatStatusSensor(coordinator, entry, device_info)]
    for key, name, result_key, unit, icon in VALUE_SENSORS:
        entities.append(HouseboatValueSensor(coordinator, entry, device_info, key, name, result_key, unit, icon))
    async_add_entities(entities)


class HouseboatStatusSensor(CoordinatorEntity[HouseboatChargeManagerCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:home-battery"

    def __init__(
        self, coordinator: HouseboatChargeManagerCoordinator, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("system_status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self.coordinator.data or {})


class HouseboatValueSensor(CoordinatorEntity[HouseboatChargeManagerCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HouseboatChargeManagerCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        key: str,
        name: str,
        result_key: str,
        unit: str | None,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._result_key = result_key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._result_key)
