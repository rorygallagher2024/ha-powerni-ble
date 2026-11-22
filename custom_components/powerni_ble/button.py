"""Button platform for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PowerNIConfigEntry
from .const import DOMAIN
from .coordinator import PowerNIDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerNIConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Power NI BLE button entities from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([PowerNIRefreshButton(coordinator)])


class PowerNIRefreshButton(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], ButtonEntity
):
    """Button triggering an immediate meter read."""

    _attr_has_entity_name = True
    _attr_name = "Refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: PowerNIDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_refresh"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="Power NI",
            model="Keypad+ Freedom Unit",
        )

    @property
    def available(self) -> bool:
        """Allow manual refresh even if the last BLE read failed."""
        return True

    async def async_press(self) -> None:
        """Request an immediate refresh of every reading."""
        await self.coordinator.async_request_full_refresh()
