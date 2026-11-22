"""DataUpdateCoordinator for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    POLL_STATUS_CONNECTING,
    POLL_STATUS_ERROR,
    POLL_STATUS_IDLE,
    POLL_STATUS_READING,
    POLL_STATUS_RETRYING,
    POLL_TIMEOUT_SECONDS,
    SECTION_SPEND,
    SECTION_UNITS,
    SESSION_RETRY_ATTEMPTS,
    SESSION_RETRY_DELAY_SECONDS,
    clamp_scan_interval,
    retry_interval_minutes,
    sections_due,
)
from .tracking import (
    detect_top_up,
    fresh_fields,
    is_stale,
    record_read_times,
    stale_after,
)

_LOGGER = logging.getLogger(__name__)

EVENT_TOP_UP = f"{DOMAIN}_top_up"
STORE_VERSION = 1
STORE_SAVE_DELAY_SECONDS = 10


@dataclass(frozen=True)
class TopUp:
    """A detected top-up (amount is the balance rise between two reads)."""

    amount: float
    at: datetime
    balance_before: float
    balance_after: float


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
        # When each slow reading group was last read successfully.
        self._section_last_read: dict[str, datetime] = {}
        self._force_full_read = False
        # When each field was last returned by the meter (not carried over).
        self.field_read_at: dict[str, datetime] = {}
        self._stale_notified: set[str] = set()
        self.last_successful_read: datetime | None = None
        self.last_top_up: TopUp | None = None
        self.poll_status: str = POLL_STATUS_IDLE
        self.last_error: str | None = None
        # Last freshly read balance, compared with the next to detect top-ups.
        self._top_up_baseline: float | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{config_entry.entry_id}"
        )

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
        now = dt_util.now()
        max_age = stale_after(clamped)
        self._stale_notified = {
            field
            for field, ts in self.field_read_at.items()
            if is_stale(ts, now, max_age)
        }
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

    @property
    def is_polling(self) -> bool:
        """Return True while a BLE poll or in-poll retry is active."""
        return self.poll_status in (
            POLL_STATUS_CONNECTING,
            POLL_STATUS_READING,
            POLL_STATUS_RETRYING,
        )

    def _set_poll_status(self, status: str, *, error: str | None = None) -> None:
        """Update live poll status and push to Home Assistant listeners."""
        next_error = (
            None
            if status == POLL_STATUS_IDLE
            else (error if error is not None else self.last_error)
        )
        if status == self.poll_status and next_error == self.last_error:
            return
        self.poll_status = status
        self.last_error = next_error
        if self._listeners:
            self.async_update_listeners()

    def _handle_partial_progress(self, partial: PowerNIMeterData) -> None:
        """Publish readings section-by-section while a BLE poll is still running."""
        if not self._listeners:
            return
        now = dt_util.now()
        record_read_times(self.field_read_at, partial, now)
        self._stale_notified.difference_update(fresh_fields(partial))
        if partial.days_left_status is not None:
            self._stale_notified.discard("days_left")
        self.data, _ = merge_meter_data(partial, self.data)
        self.async_update_listeners()

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
            now = dt_util.now()
            max_age = stale_after(self.scan_interval_minutes)
            newly_stale = {
                field
                for field, ts in self.field_read_at.items()
                if is_stale(ts, now, max_age)
            } - self._stale_notified
            if newly_stale:
                self._stale_notified.update(newly_stale)
                self.async_update_listeners()

    async def async_request_full_refresh(self) -> None:
        """Refresh now, reading every value regardless of schedule."""
        self._force_full_read = True
        await self.async_request_refresh()

    def _mark_sections_read(self, data: PowerNIMeterData, now: datetime) -> None:
        """Record which slow groups this read actually returned values for."""
        if data.total_import_kwh is not None:
            self._section_last_read[SECTION_UNITS] = now
        if any(
            value is not None
            for value in (
                data.yesterday_cost,
                data.last_7_days_cost,
                data.last_month_cost,
            )
        ):
            self._section_last_read[SECTION_SPEND] = now

    async def async_load_stored_state(self) -> None:
        """Restore the top-up baseline and last top-up from storage."""
        stored = await self._store.async_load() or {}
        balance = stored.get("last_balance")
        self._top_up_baseline = float(balance) if balance is not None else None
        last_top_up = stored.get("last_top_up")
        if last_top_up:
            at = dt_util.parse_datetime(last_top_up.get("at") or "")
            if at is not None:
                self.last_top_up = TopUp(
                    amount=float(last_top_up["amount"]),
                    at=at,
                    balance_before=float(last_top_up["balance_before"]),
                    balance_after=float(last_top_up["balance_after"]),
                )

    def _stored_state(self) -> dict[str, Any]:
        """Return the state persisted across restarts."""
        last_top_up = None
        if self.last_top_up is not None:
            last_top_up = {
                "amount": self.last_top_up.amount,
                "at": self.last_top_up.at.isoformat(),
                "balance_before": self.last_top_up.balance_before,
                "balance_after": self.last_top_up.balance_after,
            }
        return {"last_balance": self._top_up_baseline, "last_top_up": last_top_up}

    def _check_top_up(self, balance: float | None, now: datetime) -> None:
        """Fire a top-up event when a freshly read balance rose since the last one.

        Only freshly read balances are compared (never carried-over values), and
        the baseline survives restarts so a top-up made while Home Assistant was
        down is still detected.
        """
        if balance is None:
            return
        previous = self._top_up_baseline
        self._top_up_baseline = balance
        amount = detect_top_up(previous, balance)
        if amount is not None and previous is not None:
            self.last_top_up = TopUp(
                amount=amount, at=now, balance_before=previous, balance_after=balance
            )
            _LOGGER.info(
                "Top-up of £%.2f detected on %s (%.2f -> %.2f)",
                amount,
                self.address,
                previous,
                balance,
            )
            self.hass.bus.async_fire(
                EVENT_TOP_UP,
                {
                    "device_id": self._device_id(),
                    "address": self.address,
                    "name": self.device_name,
                    "amount": amount,
                    "balance_before": previous,
                    "balance_after": balance,
                },
            )
        if previous != balance:
            self._store.async_delay_save(self._stored_state, STORE_SAVE_DELAY_SECONDS)

    def _device_id(self) -> str | None:
        """Return this meter's device registry id, if registered."""
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.address)}
        )
        return device.id if device else None

    def is_field_stale(self, field: str) -> bool:
        """Return True if a reading was never taken or is too old to trust."""
        return is_stale(
            self.field_read_at.get(field),
            dt_util.now(),
            stale_after(self.scan_interval_minutes),
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
        now = dt_util.now()
        sections = sections_due(
            now,
            self._section_last_read,
            force_full=self._force_full_read or self.data is None,
        )
        _LOGGER.debug(
            "Polling %s for sections: %s", self.address, ", ".join(sorted(sections))
        )
        try:
            async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                new_data = await self._async_poll(sections)
        except TimeoutError as err:
            self._record_result(False)
            error_msg = f"Polling {self.address} timed out after {POLL_TIMEOUT_SECONDS}s"
            self._set_poll_status(POLL_STATUS_ERROR, error=error_msg)
            raise UpdateFailed(error_msg) from err
        except UpdateFailed as err:
            self._record_result(False)
            self._set_poll_status(POLL_STATUS_ERROR, error=str(err))
            raise
        except asyncio.CancelledError:
            self.poll_status = POLL_STATUS_IDLE
            raise
        except Exception as err:
            self._record_result(False)
            self._set_poll_status(POLL_STATUS_ERROR, error=str(err))
            raise
        self._record_result(True)
        self._force_full_read = False
        self._mark_sections_read(new_data, now)
        self.last_successful_read = now
        record_read_times(self.field_read_at, new_data, now)
        self._stale_notified.difference_update(fresh_fields(new_data))
        if new_data.days_left_status is not None:
            self._stale_notified.discard("days_left")
        self._check_top_up(new_data.balance, now)

        merged, carried = merge_meter_data(new_data, self.data)
        if carried:
            _LOGGER.debug(
                "Partial read from %s; kept previous values for: %s",
                self.address,
                ", ".join(carried),
            )
        self.poll_status = POLL_STATUS_IDLE
        self.last_error = None
        return merged

    async def _async_poll(self, sections: frozenset[str]) -> PowerNIMeterData:
        """Connect over BLE, read telemetry, and disconnect."""
        async with self._connect_lock:
            last_err: Exception | None = None
            for attempt in range(1, SESSION_RETRY_ATTEMPTS + 1):
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
                disconnected = asyncio.Event()
                should_retry = False

                def _on_disconnect(_client: Any) -> None:
                    disconnected.set()

                try:
                    self._set_poll_status(POLL_STATUS_CONNECTING)
                    _LOGGER.debug(
                        "Connecting to Power NI meter %s (%s, details=%s, attempt=%d/%d)%s",
                        self.device_name,
                        self.address,
                        details,
                        attempt,
                        SESSION_RETRY_ATTEMPTS,
                        f" via {self.proxy_source}" if self.proxy_source else "",
                    )
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        ble_device,
                        self.device_name,
                        disconnected_callback=_on_disconnect,
                        ble_device_callback=self._get_ble_device,
                        use_services_cache=True,
                        pair=True,
                        max_attempts=3,
                    )
                    self._set_poll_status(POLL_STATUS_READING)
                    return await async_read_meter(
                        client,
                        _LOGGER,
                        sections,
                        disconnected=disconnected,
                        on_progress=self._handle_partial_progress,
                    )
                except (BleakError, PowerNIProtocolError, TimeoutError) as err:
                    last_err = err
                    if (
                        isinstance(err, BleakCharacteristicNotFoundError)
                        and client is not None
                        and client.is_connected
                    ):
                        # Only clear cached services while still connected; a
                        # mid-read disconnect clears client.services on the backend.
                        try:
                            await client.clear_cache()
                        except Exception as cache_err:  # noqa: BLE001
                            _LOGGER.debug(
                                "Ignoring error clearing service cache for %s: %s",
                                self.address,
                                cache_err,
                            )
                    if client is not None and attempt < SESSION_RETRY_ATTEMPTS:
                        should_retry = True
                    else:
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
                                "Ignoring error disconnecting %s: %s",
                                self.address,
                                err,
                            )

                if should_retry:
                    self._set_poll_status(POLL_STATUS_RETRYING, error=str(last_err))
                    _LOGGER.info(
                        "Power NI meter %s (%s) read interrupted (%s); retrying in %.0fs (attempt %d/%d)",
                        self.device_name,
                        self.address,
                        last_err,
                        SESSION_RETRY_DELAY_SECONDS,
                        attempt + 1,
                        SESSION_RETRY_ATTEMPTS,
                    )
                    await asyncio.sleep(SESSION_RETRY_DELAY_SECONDS)

            raise UpdateFailed(f"BLE read failed: {last_err}") from last_err
