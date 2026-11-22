"""DataUpdateCoordinator for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    PowerNIMeterData,
    PowerNIProtocolError,
    async_read_meter,
    merge_meter_data,
)
from .const import (
    CONF_PROXY_SOURCE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    POLL_TIMEOUT_SECONDS,
    clamp_scan_interval,
    retry_interval_minutes,
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
        self.scan_interval_minutes = clamp_scan_interval(
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._connect_lock = asyncio.Lock()
        self._consecutive_failures = 0

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{self.address}",
            update_interval=timedelta(minutes=self.scan_interval_minutes),
        )

    def set_scan_interval(self, minutes: int) -> bool:
        """Update the poll interval and reschedule the refresh timer if changed."""
        clamped = clamp_scan_interval(minutes)
        interval = self._next_interval(clamped)
        if clamped == self.scan_interval_minutes and self.update_interval == interval:
            return False

        self.scan_interval_minutes = clamped
        self.update_interval = interval
        if self._listeners:
            self._schedule_refresh()
        self.async_update_listeners()
        _LOGGER.debug(
            "Updated poll interval for %s (%s) to %d min",
            self.device_name,
            self.address,
            clamped,
        )
        return True

    async def async_apply_options(self) -> None:
        """Apply updated config entry options in-place."""
        options = self.config_entry.options
        self.set_scan_interval(
            clamp_scan_interval(
                options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
            )
        )
        new_proxy = (options.get(CONF_PROXY_SOURCE) or "").strip()
        if new_proxy != self.proxy_source:
            self.proxy_source = new_proxy
            _LOGGER.debug(
                "Updated Bluetooth proxy source for %s (%s) to %s",
                self.device_name,
                self.address,
                new_proxy or "automatic",
            )
            await self.async_request_refresh()

    def _next_interval(self, scan_interval: int | None = None) -> timedelta:
        """Return the next poll delay, shortened while recovering from failures."""
        return timedelta(
            minutes=retry_interval_minutes(
                self._consecutive_failures,
                self.scan_interval_minutes if scan_interval is None else scan_interval,
            )
        )

    def _record_result(self, success: bool) -> None:
        """Track consecutive failures and adjust the next poll delay."""
        if success:
            if self._consecutive_failures:
                _LOGGER.info(
                    "Power NI meter %s (%s) recovered after %d failed poll(s)",
                    self.device_name,
                    self.address,
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        # DataUpdateCoordinator schedules the next refresh from update_interval
        # after this update returns, so setting it here takes effect immediately.
        self.update_interval = self._next_interval()
        if not success:
            _LOGGER.debug(
                "Retrying Power NI meter %s in %s (failure #%d)",
                self.address,
                self.update_interval,
                self._consecutive_failures,
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
        """Poll the meter, keeping last-known values for fields a read missed."""
        try:
            async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                new_data = await self._async_poll()
        except TimeoutError as err:
            self._record_result(False)
            raise UpdateFailed(
                f"Polling {self.address} timed out after {POLL_TIMEOUT_SECONDS}s"
            ) from err
        except UpdateFailed:
            self._record_result(False)
            raise
        self._record_result(True)

        merged, carried = merge_meter_data(new_data, self.data)
        if carried:
            _LOGGER.debug(
                "Partial read from %s; kept previous values for: %s",
                self.address,
                ", ".join(carried),
            )
        return merged

    async def _async_poll(self) -> PowerNIMeterData:
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
                if isinstance(err, BleakCharacteristicNotFoundError) and client is not None:
                    # The cached GATT table may be stale or incomplete; force
                    # service discovery on the next connection.
                    try:
                        await client.clear_cache()
                    except Exception as cache_err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Ignoring error clearing service cache for %s: %s",
                            self.address,
                            cache_err,
                        )
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
