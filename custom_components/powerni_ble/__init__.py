"""The Power NI Keypad+ BLE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import STORE_VERSION, PowerNIDataUpdateCoordinator

PowerNIConfigEntry = ConfigEntry[PowerNIDataUpdateCoordinator]

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: PowerNIConfigEntry) -> bool:
    """Set up Power NI Keypad+ BLE from a config entry."""
    address = (entry.data.get(CONF_ADDRESS) or entry.unique_id or "").upper()
    name = entry.data.get(CONF_NAME) or entry.title or f"Power NI {address}"

    coordinator = PowerNIDataUpdateCoordinator(hass, entry, address, name)
    await coordinator.async_load_stored_state()
    entry.async_on_unload(coordinator.async_start_advertisement_listener())
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: PowerNIConfigEntry) -> None:
    """Apply updated options without reloading entities."""
    await entry.runtime_data.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: PowerNIConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    if coordinator._survey_task is not None and not coordinator._survey_task.done():
        coordinator._survey_task.cancel()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored top-up state when the meter is removed."""
    await Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
