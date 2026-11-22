"""Binary sensor platform for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
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
    """Set up Power NI BLE binary sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([PowerNIUpdatingBinarySensor(coordinator)])


class PowerNIUpdatingBinarySensor(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], BinarySensorEntity
):
    """Diagnostic sensor indicating when a BLE meter read is in progress."""

    _attr_has_entity_name = True
    _attr_translation_key = "updating"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PowerNIDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_updating"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="Power NI",
            model="Keypad+ Freedom Unit",
        )

    @property
    def available(self) -> bool:
        """Keep diagnostic running status visible during retries and errors."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while connecting, reading, or retrying a poll."""
        return self.coordinator.is_polling
