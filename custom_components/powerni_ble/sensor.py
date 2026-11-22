"""Sensor platform for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PowerNIConfigEntry
from .client import PowerNIMeterData
from .const import DOMAIN
from .coordinator import PowerNIDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class PowerNISensorEntityDescription(SensorEntityDescription):
    """Describes a Power NI BLE sensor entity."""

    value_fn: Callable[[PowerNIMeterData], StateType]
    attrs_fn: Callable[[PowerNIMeterData], dict[str, Any] | None] | None = None


SENSORS: tuple[PowerNISensorEntityDescription, ...] = (
    PowerNISensorEntityDescription(
        key="balance",
        name="Balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=None,
        suggested_display_precision=2,
        icon="mdi:cash",
        value_fn=lambda data: data.balance,
    ),
    PowerNISensorEntityDescription(
        key="days_left",
        name="Days left",
        device_class=None,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-clock",
        value_fn=lambda data: data.days_left,
        attrs_fn=lambda data: (
            {"status": data.days_left_status}
            if data.days_left_status is not None
            else None
        ),
    ),
    PowerNISensorEntityDescription(
        key="current_load_kw",
        name="Current load",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.current_load_kw,
    ),
    PowerNISensorEntityDescription(
        key="current_cost_per_hour",
        name="Current cost rate",
        device_class=None,
        native_unit_of_measurement="GBP/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:currency-gbp",
        value_fn=lambda data: data.current_cost_per_hour,
    ),
    PowerNISensorEntityDescription(
        key="total_import_kwh",
        name="Total import energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.total_import_kwh,
    ),
    PowerNISensorEntityDescription(
        key="yesterday_cost",
        name="Yesterday's spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=None,
        suggested_display_precision=2,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.yesterday_cost,
    ),
    PowerNISensorEntityDescription(
        key="last_7_days_cost",
        name="Last 7 days spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=None,
        suggested_display_precision=2,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.last_7_days_cost,
    ),
    PowerNISensorEntityDescription(
        key="last_month_cost",
        name="Last month spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=None,
        suggested_display_precision=2,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.last_month_cost,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerNIConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Power NI BLE sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        PowerNISensorEntity(coordinator, description) for description in SENSORS
    )


class PowerNISensorEntity(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], SensorEntity
):
    """Sensor representing a Power NI Freedom Unit reading."""

    entity_description: PowerNISensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerNIDataUpdateCoordinator,
        description: PowerNISensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="Power NI",
            model="Keypad+ Freedom Unit",
        )

    @property
    def native_value(self) -> StateType:
        """Return the sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional extra state attributes."""
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
