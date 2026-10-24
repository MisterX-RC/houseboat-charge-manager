"""The Houseboat Charge Manager integration - battery/price-aware shore
power charge planning for a Victron-based houseboat setup, ported from a
hand-written Home Assistant trigger-based template sensor into a
configurable, HACS-installable custom integration.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.loader import async_get_integration

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN, PLATFORMS
from .coordinator import HouseboatChargeManagerCoordinator

_LOGGER = logging.getLogger(__name__)

CARDS_FILE = Path(__file__).parent / "frontend" / "houseboat-charge-manager-cards.js"
CARDS_URL = f"/{DOMAIN}/houseboat-charge-manager-cards.js"
_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"


async def _async_register_cards(hass: HomeAssistant) -> None:
    """Serve frontend/houseboat-charge-manager-cards.js and load it on every
    dashboard page, so the bundled price/status cards show up in the "Add
    card" picker. Never fails setup - without http/frontend (e.g. a headless
    install) there's simply no dashboard to add cards to.
    """
    if hass.data.get(_FRONTEND_REGISTERED) or getattr(hass, "http", None) is None:
        return
    hass.data[_FRONTEND_REGISTERED] = True
    try:
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths([StaticPathConfig(CARDS_URL, str(CARDS_FILE), False)])
        except ImportError:  # Home Assistant before 2024.7
            hass.http.register_static_path(CARDS_URL, str(CARDS_FILE), False)
        from homeassistant.components.frontend import add_extra_js_url

        integration = await async_get_integration(hass, DOMAIN)
        add_extra_js_url(hass, f"{CARDS_URL}?v={integration.version}")
    except Exception as err:  # noqa: BLE001 - the cards are optional, planning isn't
        _LOGGER.warning("Houseboat Charge Manager: could not load the dashboard cards: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    await _async_register_cards(hass)

    coordinator = HouseboatChargeManagerCoordinator(hass, entry)
    await coordinator.async_load_persisted_state()

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, DEFAULT_NAME),
        manufacturer="Houseboat Charge Manager",
        model="Battery / shore-power charge planner",
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "device_info": device_info,
    }

    # number.py must be set up before the coordinator's first refresh so the
    # coordinator has live tunable values to read (it seeds its own
    # defaults otherwise, which is fine for the very first boot but would
    # otherwise ignore any already-saved options on a reload).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # The platforms above are already registered for this entry. If a
        # bad first refresh (e.g. a sensor still unavailable right after
        # boot, or an exception inside logic.compute()) is left to propagate
        # as-is - typically as ConfigEntryNotReady, which Home Assistant
        # retries automatically - the retry's own call to
        # async_forward_entry_setups blows up with "has already been setup!"
        # for number/sensor/switch, since they were never unloaded. That
        # turns one transient failure into a permanently broken entry rather
        # than a clean retry (2026-10, Timo hit this after updating: the
        # integration got stuck re-raising "already been setup" instead of
        # retrying and surfacing the actual underlying error). Undo the
        # forward here so a retry starts clean, same as async_unload_entry.
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed (an entity was re-pointed) - entities are re-read
    from entry.data/entry.options on every cycle, so just request a refresh.
    """
    coordinator: HouseboatChargeManagerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_request_refresh()
