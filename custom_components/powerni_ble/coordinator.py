"""DataUpdateCoordinator for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import PowerNIMeterData, PowerNIProtocolError, async_read_meter
from .const import (
    CONF_PROXY_SOURCE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class PowerNIDataUpdateCoordinator(DataUpdateCoordinator[PowerNIMeterData]):
    """Coordinator managing periodic BLE polling of a Power NI Freedom Unit."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        address: str,
        name: str,
    ) -> None:
        self.address = address.upper()
        self.device_name = name
        self.proxy_source: str = (
            config_entry.options.get(CONF_PROXY_SOURCE) or ""
        ).strip()
        scan_minutes = int(
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._connect_lock = asyncio.Lock()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{self.address}",
            update_interval=timedelta(minutes=scan_minutes),
        )

    def _get_ble_device(self) -> BLEDevice | None:
        """Resolve the target BLEDevice (optionally pinned to a specific proxy)."""
        if self.proxy_source:
            for scanner_dev in bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            ):
                if scanner_dev.scanner.source.upper() == self.proxy_source.upper():
                    return scanner_dev.ble_device
            return None
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_update_data(self) -> PowerNIMeterData:
        """Connect over BLE, read telemetry, and disconnect."""
        async with self._connect_lock:
            ble_device = self._get_ble_device()
            if ble_device is None:
                if self.proxy_source:
                    raise UpdateFailed(
                        f"Configured Bluetooth proxy {self.proxy_source} cannot currently see {self.address}"
                    )
                raise UpdateFailed(
                    f"No connectable Bluetooth proxy or adapter can currently see {self.address}"
                )

            client: BleakClientWithServiceCache | None = None
            details = getattr(ble_device, "details", None)
            try:
                _LOGGER.debug(
                    "Connecting to Power NI meter %s (%s, details=%s)%s",
                    self.device_name,
                    self.address,
                    details,
                    f" via {self.proxy_source}" if self.proxy_source else "",
                )
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.device_name,
                    ble_device_callback=self._get_ble_device,
                    use_services_cache=True,
                    pair=True,
                    max_attempts=3,
                )
                return await async_read_meter(client, _LOGGER)
            except (BleakError, PowerNIProtocolError, TimeoutError) as err:
                _LOGGER.warning(
                    "Failed reading Power NI meter %s (%s, details=%s): %s",
                    self.device_name,
                    self.address,
                    details,
                    err,
                )
                raise UpdateFailed(f"BLE read failed: {err}") from err
            finally:
                if client is not None and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:
                        _LOGGER.debug(
                            "Ignoring error disconnecting %s: %s", self.address, err
                        )
