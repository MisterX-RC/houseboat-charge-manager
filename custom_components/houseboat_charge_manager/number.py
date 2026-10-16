"""Exposes every tunable threshold from the original sensor's Jinja
templates (soc_floor_percentage, charger_efficiency, flat_block_price_margin,
etc.) as a `number` entity, so they can be adjusted from the dashboard
instead of edited in YAML. Values are persisted into the config entry's
options, so they survive a Home Assistant restart.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, NUMBER_DEFINITIONS
from .coordinator import HouseboatChargeManagerCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HouseboatChargeManagerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_info: DeviceInfo = hass.data[DOMAIN][entry.entry_id]["device_info"]

    entities = [
        HouseboatTunableNumber(coordinator, entry, device_info, key, default, unit, minimum, maximum, step, icon, name)
        for key, default, unit, minimum, maximum, step, icon, name in NUMBER_DEFINITIONS
    ]
    async_add_entities(entities)


class HouseboatTunableNumber(RestoreEntity, NumberEntity):
    """A single configurable threshold, seeded from entry.options (falling
    back to its default), and written back to entry.options whenever
    changed so it survives a restart.
    """

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: HouseboatChargeManagerCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        key: str,
        default: float,
        unit: str | None,
        minimum: float,
        maximum: float,
        step: float,
        icon: str,
        name: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._key = key
        self._default = default
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_icon = icon
        self._attr_device_info = device_info
        seeded = entry.options.get(key, default)
        self._attr_native_value = seeded
        coordinator.set_number_value(key, seeded)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                value = float(last_state.state)
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self._attr_native_value = value
                self._coordinator.set_number_value(self._key, value)
        # Lets the coordinator write this entity's value back directly (see
        # vacation_departure_target_days' grid-unavailable reset) without
        # this entity being the only source of truth for its own value.
        self._coordinator.register_number_entity(self._key, self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_number_entity(self._key)
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._coordinator.set_number_value(self._key, value)
        new_options = {**self._entry.options, self._key: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()
