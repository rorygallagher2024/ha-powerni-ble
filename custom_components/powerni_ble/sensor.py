"""Sensor platform for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PowerNIConfigEntry
from .client import PowerNIMeterData
from .const import (
    DOMAIN,
    POLL_STATUS_CONNECTING,
    POLL_STATUS_ERROR,
    POLL_STATUS_IDLE,
    POLL_STATUS_OPTIONS,
    POLL_STATUS_READING,
    POLL_STATUS_RETRYING,
    POLL_STATUS_WAITING,
)
from .coordinator import PowerNIDataUpdateCoordinator

POLL_STATUS_ICONS: dict[str, str] = {
    POLL_STATUS_IDLE: "mdi:check-circle-outline",
    POLL_STATUS_CONNECTING: "mdi:bluetooth-connect",
    POLL_STATUS_READING: "mdi:sync",
    POLL_STATUS_RETRYING: "mdi:refresh-auto",
    POLL_STATUS_WAITING: "mdi:bluetooth-off",
    POLL_STATUS_ERROR: "mdi:alert-circle-outline",
}


@dataclass(frozen=True, kw_only=True)
class PowerNISensorEntityDescription(SensorEntityDescription):
    """Describes a Power NI BLE sensor entity."""

    value_fn: Callable[[PowerNIMeterData], StateType]
    attrs_fn: Callable[[PowerNIMeterData], dict[str, Any] | None] | None = None


SENSORS: tuple[PowerNISensorEntityDescription, ...] = (
    PowerNISensorEntityDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash",
        value_fn=lambda data: data.balance,
    ),
    PowerNISensorEntityDescription(
        key="days_left",
        translation_key="days_left",
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
        translation_key="current_load",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.current_load_kw,
    ),
    PowerNISensorEntityDescription(
        key="current_cost_per_hour",
        translation_key="current_cost_rate",
        device_class=None,
        native_unit_of_measurement="GBP/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:currency-gbp",
        value_fn=lambda data: data.current_cost_per_hour,
    ),
    PowerNISensorEntityDescription(
        key="total_import_kwh",
        translation_key="total_import_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.total_import_kwh,
    ),
    PowerNISensorEntityDescription(
        key="total_export_kwh",
        translation_key="total_export_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.total_export_kwh,
    ),
    PowerNISensorEntityDescription(
        key="unit_rate",
        translation_key="unit_rate",
        device_class=None,
        native_unit_of_measurement="GBP/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:tag-outline",
        value_fn=lambda data: data.unit_rate,
    ),
    PowerNISensorEntityDescription(
        key="yesterday_cost",
        translation_key="yesterday_spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.yesterday_cost,
    ),
    PowerNISensorEntityDescription(
        key="last_7_days_cost",
        translation_key="last_7_days_spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.last_7_days_cost,
    ),
    PowerNISensorEntityDescription(
        key="last_month_cost",
        translation_key="last_month_spend",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
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
    entities: list[SensorEntity] = [
        PowerNISensorEntity(coordinator, description) for description in SENSORS
    ]
    entities.extend(
        PowerNIStatusSensor(coordinator, description) for description in STATUS_SENSORS
    )
    async_add_entities(entities)


def _device_info(coordinator: PowerNIDataUpdateCoordinator) -> DeviceInfo:
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        identifiers={(DOMAIN, coordinator.address)},
        name=coordinator.device_name,
        manufacturer="Power NI",
        model="Keypad+ Freedom Unit",
    )


class PowerNISensorEntity(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], SensorEntity
):
    """Sensor representing a Power NI Freedom Unit reading.

    Availability follows the age of the reading rather than the last poll:
    a value kept from an earlier poll stays available (with its read time in
    the last_read attribute) until it is older than the staleness limit.
    """

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
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        """Return True while the reading is recent enough to trust."""
        return self.coordinator.data is not None and not self.coordinator.is_field_stale(
            self.entity_description.key
        )

    @property
    def native_value(self) -> StateType:
        """Return the sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return when the value was read, plus any description attributes."""
        attrs: dict[str, Any] = {}
        last_read = self.coordinator.field_read_at.get(self.entity_description.key)
        if last_read is not None:
            attrs["last_read"] = last_read.isoformat()
        if self.coordinator.data is not None and self.entity_description.attrs_fn:
            attrs.update(self.entity_description.attrs_fn(self.coordinator.data) or {})
        return attrs or None


@dataclass(frozen=True, kw_only=True)
class PowerNIStatusSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor derived from coordinator state rather than a reading."""

    value_fn: Callable[[PowerNIDataUpdateCoordinator], StateType | datetime]
    attrs_fn: Callable[[PowerNIDataUpdateCoordinator], dict[str, Any] | None] | None = None
    icon_fn: Callable[[PowerNIDataUpdateCoordinator], str] | None = None


STATUS_SENSORS: tuple[PowerNIStatusSensorEntityDescription, ...] = (
    PowerNIStatusSensorEntityDescription(
        key="poll_status",
        translation_key="poll_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(POLL_STATUS_OPTIONS),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.poll_status,
        attrs_fn=lambda c: {"last_error": c.last_error} if c.last_error else None,
        icon_fn=lambda c: POLL_STATUS_ICONS.get(
            c.poll_status, "mdi:bluetooth-transfer"
        ),
    ),
    PowerNIStatusSensorEntityDescription(
        key="last_successful_read",
        translation_key="last_successful_read",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-check-outline",
        value_fn=lambda c: c.last_successful_read,
    ),
    PowerNIStatusSensorEntityDescription(
        key="last_top_up",
        translation_key="last_top_up",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value_fn=lambda c: c.last_top_up.amount if c.last_top_up else None,
        attrs_fn=lambda c: (
            {
                "balance_before": c.last_top_up.balance_before,
                "balance_after": c.last_top_up.balance_after,
            }
            if c.last_top_up
            else None
        ),
    ),
    PowerNIStatusSensorEntityDescription(
        key="last_top_up_time",
        translation_key="last_top_up_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cash-clock",
        value_fn=lambda c: c.last_top_up.at if c.last_top_up else None,
    ),
    PowerNIStatusSensorEntityDescription(
        key="signal_strength",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.rssi,
    ),
)


class PowerNIStatusSensor(
    CoordinatorEntity[PowerNIDataUpdateCoordinator], SensorEntity
):
    """Sensor reporting integration state such as the last read or top-up."""

    entity_description: PowerNIStatusSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerNIDataUpdateCoordinator,
        description: PowerNIStatusSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        """Stay available after failed polls; these describe past events."""
        return True

    @property
    def icon(self) -> str | None:
        """Return dynamic icon when configured on the description."""
        if self.entity_description.icon_fn is not None:
            return self.entity_description.icon_fn(self.coordinator)
        return super().icon

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value from coordinator state."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional extra state attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)
