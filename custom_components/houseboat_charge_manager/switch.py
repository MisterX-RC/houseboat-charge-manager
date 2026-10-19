"""Switch platform.

Currently a single entity: a user-facing on/off control for whether the
integration calls the configured aan/afkoppel scripts (and sends the
matching notification) at all - without having to clear the script
fields in Reconfigure to pause it temporarily. See coordinator.py's
_async_handle_charge_action, which checks this before doing anything.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_AUTO_TRIGGER_SCRIPTS, DOMAIN
from .coordinator import HouseboatChargeManagerCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HouseboatChargeManagerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_info: DeviceInfo = hass.data[DOMAIN][entry.entry_id]["device_info"]
    async_add_entities([HouseboatAutoTriggerScriptsSwitch(coordinator, entry, device_info)])


class HouseboatAutoTriggerScriptsSwitch(RestoreEntity, SwitchEntity):
    """On (default): the configured aan/afkoppel scripts and their
    notification fire automatically, as described in the CHANGELOG. Off:
    the integration still computes everything as normal (system_status,
    charge_action_reason, ...) but never calls a script or sends a
    notification - useful to pause it temporarily (e.g. while testing
    something else) without having to clear the script fields.
    """

    _attr_has_entity_name = True
    _attr_name = "Auto-trigger connect/disconnect scripts"
    _attr_icon = "mdi:script-text-play-outline"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self, coordinator: HouseboatChargeManagerCoordinator, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_auto_trigger_scripts"
        self._attr_device_info = device_info
        self._attr_is_on = DEFAULT_AUTO_TRIGGER_SCRIPTS
        coordinator.set_auto_trigger_scripts_enabled(DEFAULT_AUTO_TRIGGER_SCRIPTS)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            is_on = last_state.state == "on"
            self._attr_is_on = is_on
            self._coordinator.set_auto_trigger_scripts_enabled(is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._coordinator.set_auto_trigger_scripts_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._coordinator.set_auto_trigger_scripts_enabled(False)
        self.async_write_ha_state()
