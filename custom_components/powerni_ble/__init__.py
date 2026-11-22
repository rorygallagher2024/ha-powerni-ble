"""The Power NI Keypad+ BLE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant

from .coordinator import PowerNIDataUpdateCoordinator

PowerNIConfigEntry = ConfigEntry[PowerNIDataUpdateCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: PowerNIConfigEntry) -> bool:
    """Set up Power NI Keypad+ BLE from a config entry."""
    address = (entry.data.get(CONF_ADDRESS) or entry.unique_id or "").upper()
    name = entry.data.get(CONF_NAME) or entry.title or f"Power NI {address}"

    coordinator = PowerNIDataUpdateCoordinator(hass, entry, address, name)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: PowerNIConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PowerNIConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
