"""Config flow: pick every external entity the original sensor hardcoded.
A single step is enough here (14 entities) - unlike a multi-source energy
planner, every one of these is required for the calculation to mean
anything, so there is no meaningful "skip this part" path to branch on.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AVG_LOAD_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_CELL_MAX_VOLTAGE_ENTITY,
    CONF_CELL_MIN_VOLTAGE_ENTITY,
    CONF_FORCE_FULL_CHARGE_ENTITY,
    CONF_GRID_SWITCH_ENTITY,
    CONF_LOW_BATTERY_PERCENTAGE_ENTITY,
    CONF_NAME,
    CONF_PRICE_14D_AVERAGE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SHORE_POWER_ENTITY,
    CONF_START_VOYAGE_ENTITY,
    CONF_VACATION_MODE_ENTITY,
    CONF_WALSTROOM_REQUESTED_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)

# (config key, selector domains)
_ENTITY_FIELDS: tuple[tuple[str, list[str]], ...] = (
    (CONF_BATTERY_SOC_ENTITY, ["sensor"]),
    (CONF_BATTERY_VOLTAGE_ENTITY, ["sensor"]),
    (CONF_CELL_MAX_VOLTAGE_ENTITY, ["sensor"]),
    (CONF_CELL_MIN_VOLTAGE_ENTITY, ["sensor"]),
    (CONF_PRICE_ENTITY, ["sensor"]),
    (CONF_PRICE_14D_AVERAGE_ENTITY, ["sensor"]),
    (CONF_SHORE_POWER_ENTITY, ["sensor"]),
    (CONF_AVG_LOAD_ENTITY, ["sensor"]),
    (CONF_WALSTROOM_REQUESTED_ENTITY, ["input_number", "number"]),
    (CONF_START_VOYAGE_ENTITY, ["input_number", "number"]),
    (CONF_LOW_BATTERY_PERCENTAGE_ENTITY, ["input_number", "number"]),
    (CONF_VACATION_MODE_ENTITY, ["input_boolean"]),
    (CONF_FORCE_FULL_CHARGE_ENTITY, ["input_boolean"]),
    (CONF_GRID_SWITCH_ENTITY, ["switch"]),
)


def _entities_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key, domains in _ENTITY_FIELDS:
        selector_config = selector.EntitySelectorConfig(domain=domains)
        default = defaults.get(key)
        if default:
            fields[vol.Required(key, default=default)] = selector.EntitySelector(selector_config)
        else:
            fields[vol.Required(key)] = selector.EntitySelector(selector_config)
    return vol.Schema(fields)


class HouseboatChargeManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.pop(CONF_NAME, DEFAULT_NAME)
            return self.async_create_entry(title=name, data={CONF_NAME: name, **user_input})

        schema = vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NAME): str}).extend(_entities_schema({}).schema)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return HouseboatChargeManagerOptionsFlow(config_entry)


class HouseboatChargeManagerOptionsFlow(config_entries.OptionsFlow):
    """Lets the entities be re-pointed later without removing/re-adding the
    integration. Number (tunable) entities are edited directly as regular
    `number` entities, not here.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = _entities_schema(current)
        return self.async_show_form(step_id="init", data_schema=schema)
