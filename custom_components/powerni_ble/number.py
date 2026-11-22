"""Number platform for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PowerNIConfigEntry
from .const import (
    CONF_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    clamp_scan_interval,
)
from .coordinator import PowerNIDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerNIConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Power NI BLE number entities from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([PowerNIPollIntervalNumber(coordinator)])


class PowerNIPollIntervalNumber(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], NumberEntity
):
    """Configuration entity controlling how often the meter is polled."""

    _attr_has_entity_name = True
    _attr_name = "Poll interval"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = float(MIN_SCAN_INTERVAL_MINUTES)
    _attr_native_max_value = float(MAX_SCAN_INTERVAL_MINUTES)
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: PowerNIDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_poll_interval"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="Power NI",
            model="Keypad+ Freedom Unit",
        )

    @property
    def available(self) -> bool:
        """Keep poll interval adjustable even if the last BLE read failed."""
        return True

    @property
    def native_value(self) -> float:
        """Return the current poll interval in minutes."""
        return float(self.coordinator.scan_interval_minutes)

    async def async_set_native_value(self, value: float) -> None:
        """Set the poll interval in minutes and persist it to options."""
        minutes = clamp_scan_interval(value)
        self.coordinator.set_scan_interval(minutes)
        entry = self.coordinator.config_entry
        if entry.options.get(CONF_SCAN_INTERVAL) != minutes:
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, CONF_SCAN_INTERVAL: minutes},
            )
        self.async_write_ha_state()
