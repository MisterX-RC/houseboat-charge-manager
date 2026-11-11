"""Diagnostics for Houseboat Charge Manager.

Settings -> Devices & services -> Houseboat Charge Manager -> (three dots)
-> "Download diagnostics" produces a single JSON file with everything needed
to troubleshoot the planner, far beyond what the sensor's attributes show:

- ``meta``: integration/HA version, time zone, coordinator health (last
  successful update, last error, which source sensors were missing).
- ``config``: the config entry (entity choices from the setup wizard/options).
- ``tunables``: every number tunable, current value next to its default.
- ``source_entities``: the live state + attributes of every configured
  source entity (incl. the price entity's today/tomorrow lists), exactly as
  Home Assistant saw them when the file was generated.
- ``integration_entities``: the entities this integration created, with
  their current state (to spot disabled/unavailable ones).
- ``last_result``: the complete result dict of the most recent cycle (all
  ~150 derived values, not just the ones a sensor exposes).
- ``replay``: the EXACT inputs of that same cycle - the ``compute()``
  keyword arguments plus the full previous-cycle state (``prev``). Feeding
  these back into ``logic.compute()`` offline reproduces ``last_result``
  1-on-1, so a reported situation can be debugged in a simulation script
  without having to reconstruct it by hand.
- ``persisted_store``: what is saved to disk to survive a restart.
- ``decision_log``: a timeline (in memory, since the last restart/reload)
  of every cycle where the plan/decision changed, with which fields changed.

Nothing in here is secret (entity ids, sensor values, prices), so nothing is
redacted except notify targets' and scripts' attributes, which are not
needed and may contain personal device names.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
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
    DOMAIN,
    NUMBER_DEFINITIONS,
    UPDATE_INTERVAL_SECONDS,
)

# Bump when the layout of the diagnostics file changes, so a replay script
# can tell which format it is reading.
DIAGNOSTICS_FORMAT_VERSION = 1

# Source entities whose full attributes are useful for troubleshooting.
_SOURCE_ENTITY_KEYS: tuple[str, ...] = (
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
# Configured, but only their state matters (attributes not needed).
_STATE_ONLY_ENTITY_KEYS: tuple[str, ...] = (
    CONF_START_CHARGE_SCRIPT_ENTITY,
    CONF_STOP_CHARGE_SCRIPT_ENTITY,
)


def _jsonable(value: Any) -> Any:
    """Convert anything the planner produces into plain JSON types."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _state_dump(hass: HomeAssistant, entity_id: str | None, with_attributes: bool = True) -> dict[str, Any]:
    if not entity_id:
        return {"entity_id": None, "note": "not configured"}
    state = hass.states.get(entity_id)
    if state is None:
        return {"entity_id": entity_id, "note": "entity does not exist (no state object)"}
    out: dict[str, Any] = {
        "entity_id": entity_id,
        "state": state.state,
        "last_changed": _jsonable(state.last_changed),
        "last_updated": _jsonable(state.last_updated),
    }
    if with_attributes:
        out["attributes"] = _jsonable(dict(state.attributes))
    return out


def _serialize_prev_full(prev: dict[str, Any] | None) -> dict[str, Any] | None:
    """Full prev dict (not just PERSISTED_KEYS) in JSON-safe form. Datetimes
    become ISO strings; their keys are listed separately in the replay
    section so a replay script knows exactly which ones to parse back
    (logic.deserialize_prev already does this for logic._DATETIME_KEYS).
    """
    if prev is None:
        return None
    return _jsonable(prev)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    conf = {**entry.data, **entry.options}
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = entry_data.get("coordinator")

    try:
        integration_version = (await async_get_integration(hass, DOMAIN)).version
    except Exception:  # noqa: BLE001 - diagnostics must never fail on a detail
        integration_version = None
    integration_version = str(integration_version) if integration_version is not None else None

    meta: dict[str, Any] = {
        "diagnostics_format_version": DIAGNOSTICS_FORMAT_VERSION,
        "integration_version": integration_version,
        "home_assistant_version": HA_VERSION,
        "generated_at": dt_util.now().isoformat(),
        "time_zone": str(hass.config.time_zone),
        "entry_title": entry.title,
        "entry_state": str(entry.state),
        "update_interval_seconds": UPDATE_INTERVAL_SECONDS,
        "coordinator_loaded": coordinator is not None,
    }

    # ---- tunables -------------------------------------------------------
    current_numbers = coordinator.number_values if coordinator is not None else {}
    tunables = {
        key: {"value": current_numbers.get(key), "default": default, "unit": unit, "name": name}
        for key, default, unit, *_rest, name in NUMBER_DEFINITIONS
    }

    # ---- source entities --------------------------------------------------
    source_entities: dict[str, Any] = {key: _state_dump(hass, conf.get(key)) for key in _SOURCE_ENTITY_KEYS}
    for key in _STATE_ONLY_ENTITY_KEYS:
        source_entities[key] = _state_dump(hass, conf.get(key), with_attributes=False)
    notify_targets = conf.get(CONF_NOTIFY_TARGETS) or []
    if isinstance(notify_targets, str):
        notify_targets = [notify_targets]
    source_entities[CONF_NOTIFY_TARGETS] = [
        _state_dump(hass, entity_id, with_attributes=False) for entity_id in notify_targets
    ]

    # ---- entities created by this integration ----------------------------
    registry = er.async_get(hass)
    integration_entities = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(reg_entry.entity_id)
        integration_entities.append(
            {
                "entity_id": reg_entry.entity_id,
                "unique_id": reg_entry.unique_id,
                "platform_domain": reg_entry.domain,
                "disabled_by": str(reg_entry.disabled_by) if reg_entry.disabled_by else None,
                "state": state.state if state is not None else None,
            }
        )
    integration_entities.sort(key=lambda item: item["entity_id"])

    diag: dict[str, Any] = {
        "meta": meta,
        "config": {
            "data": _jsonable(dict(entry.data)),
            "options": _jsonable(dict(entry.options)),
        },
        "tunables": _jsonable(tunables),
        "source_entities": source_entities,
        "integration_entities": integration_entities,
    }

    if coordinator is None:
        diag["note"] = (
            "The coordinator is not loaded (setup failed or is being retried) - "
            "only config and source entity states are available. Check meta.entry_state "
            "and source_entities for an unavailable/missing sensor."
        )
        return diag

    last_exception = coordinator.last_exception
    meta.update(
        {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(last_exception) if last_exception else None,
            "last_cycle_at": _jsonable(coordinator.last_cycle_at),
            "missing_entities_last_cycle": list(coordinator.last_missing_entities),
            "auto_trigger_scripts_enabled": coordinator.auto_trigger_scripts_enabled,
        }
    )

    last_prev = coordinator.last_compute_prev
    diag["last_result"] = _jsonable(coordinator.data) if coordinator.data is not None else None
    diag["replay"] = {
        "note": (
            "logic.compute(**compute_kwargs, prev=logic.deserialize_prev(prev)) with "
            "compute_kwargs['now'] parsed back via datetime.fromisoformat reproduces "
            "last_result. If compute_error is set, these are instead the inputs of the "
            "cycle in which compute() itself raised that error (last_result is then "
            "still the last successful cycle)."
        ),
        "compute_error": coordinator.last_compute_error,
        "compute_kwargs": _jsonable(coordinator.last_compute_kwargs),
        "prev": _serialize_prev_full(last_prev),
        "prev_datetime_keys": sorted(
            k for k, v in (last_prev or {}).items() if isinstance(v, (datetime, date))
        ),
    }
    try:
        diag["persisted_store"] = _jsonable(await coordinator.async_load_store_raw())
    except Exception as err:  # noqa: BLE001
        diag["persisted_store"] = f"could not read store: {err!r}"
    diag["persisted_keys"] = list(logic.PERSISTED_KEYS)
    diag["decision_log"] = _jsonable(list(coordinator.decision_log))
    return diag
