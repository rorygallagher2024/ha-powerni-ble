"""DataUpdateCoordinator for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .client import (
    PowerNIMeterData,
    PowerNIProtocolError,
    async_read_meter,
    async_read_survey,
    merge_meter_data,
)
from .const import (
    ADVERTISEMENT_SILENT_SECONDS,
    ADVERTISEMENT_WAITING_TIMEOUT_SECONDS,
    CONF_AUTO_SYNC_HISTORY,
    CONF_PROXY_SOURCE,
    CONF_SCAN_INTERVAL,
    DEFAULT_AUTO_SYNC_HISTORY,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DISABLED_BY_DEFAULT_ENTITY_KEYS,
    DOMAIN,
    FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS,
    PAIRING_LEARN_MORE_URL,
    POLL_STATUS_CONNECTING,
    POLL_STATUS_ERROR,
    POLL_STATUS_IDLE,
    POLL_STATUS_READING,
    POLL_STATUS_RETRYING,
    POLL_STATUS_WAITING,
    POLL_TIMEOUT_SECONDS,
    RSSI_MEDIAN_WINDOW,
    RSSI_MIN_PUBLISH_INTERVAL_SECONDS,
    SECTION_ENTITY_KEYS,
    SECTION_EXPORT,
    SECTION_RATE,
    SECTION_SPEND,
    SECTION_UNITS,
    SESSION_RETRY_ATTEMPTS,
    SESSION_RETRY_DELAY_SECONDS,
    SURVEY_INCREMENTAL_DAYS,
    SURVEY_MAX_DAYS,
    SURVEY_SYNC_INTERVAL,
    SURVEY_TIMEOUT_SECONDS,
    clamp_scan_interval,
    retry_interval_minutes,
    sections_due,
)
from .survey import aggregate_hourly, parse_survey_blocks
from .tracking import (
    detect_top_up,
    fresh_fields,
    is_auth_or_pairing_error,
    is_stale,
    record_read_times,
    stale_after,
)

_LOGGER = logging.getLogger(__name__)

EVENT_TOP_UP = f"{DOMAIN}_top_up"
STORE_VERSION = 1
STORE_SAVE_DELAY_SECONDS = 10
RSSI_PUBLISH_THRESHOLD_DBM = 3
SURVEY_BACKGROUND_DELAY_SECONDS = 1.0
SURVEY_FAILURE_COOLDOWN = timedelta(minutes=30)


def pairing_issue_id_for_address(address: str) -> str:
    """Return the repair issue id used for a meter MAC address."""
    return f"pairing_required_{address.lower().replace(':', '_')}"


def _get_address_lock(hass: HomeAssistant, address: str) -> asyncio.Lock:
    """Return a BLE connection lock shared across reloads for one meter."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(f"{DOMAIN}_locks", {})
    lock = locks.get(address)
    if lock is None:
        lock = asyncio.Lock()
        locks[address] = lock
    return lock


def _extract_ble_device_source(ble_device: BLEDevice) -> str | None:
    """Return the scanner/adapter source reported on a BLEDevice, if available."""
    details = getattr(ble_device, "details", None)
    raw = (
        details.get("source")
        if isinstance(details, dict)
        else getattr(details, "source", None)
    )
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped or None
    return None


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
        self.auto_sync_history: bool = bool(
            config_entry.options.get(CONF_AUTO_SYNC_HISTORY, DEFAULT_AUTO_SYNC_HISTORY)
        )
        self._preferred_proxy_source: str | None = None
        self.scan_interval_minutes = clamp_scan_interval(
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._connect_lock = _get_address_lock(hass, self.address)
        self._active_poll_task: asyncio.Task[Any] | None = None
        self._write_with_response: bool = False
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
        self._last_adv_monotonic: float | None = None
        self._rssi_samples: deque[int] = deque(maxlen=RSSI_MEDIAN_WINDOW)
        self._last_published_rssi: int | None = None
        self._last_rssi_publish_monotonic: float | None = None
        self._poll_deferred: bool = False
        self._last_failure_monotonic: float | None = None
        self._last_survey_sync: datetime | None = None
        self._last_survey_attempt: datetime | None = None
        self._survey_task: asyncio.Task[None] | None = None
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
    def pairing_issue_id(self) -> str:
        """Return the Home Assistant Repair issue id for this meter."""
        return pairing_issue_id_for_address(self.address)

    @property
    def rssi(self) -> int | None:
        """Return the rolling median connectable RSSI in dBm."""
        return round(median(self._rssi_samples)) if self._rssi_samples else None

    @property
    def seconds_since_last_advertisement(self) -> float | None:
        """Return seconds elapsed since the last connectable advertisement."""
        if self._last_adv_monotonic is None:
            return None
        return round(max(0.0, time.monotonic() - self._last_adv_monotonic), 1)

    @property
    def is_polling(self) -> bool:
        """Return True while a BLE poll or in-poll retry is active."""
        return self.poll_status in (
            POLL_STATUS_CONNECTING,
            POLL_STATUS_READING,
            POLL_STATUS_RETRYING,
        )

    def async_start_advertisement_listener(self) -> Callable[[], None]:
        """Listen to every connectable advertisement from the meter."""
        self._seed_initial_rssi()
        return bluetooth.async_register_advertisement_callback(
            self.hass, self._on_advertisement, self.address
        )

    def _seed_initial_rssi(self) -> None:
        """Populate initial RSSI from Home Assistant's scanner cache if available."""
        for scanner_dev in bluetooth.async_scanner_devices_by_address(
            self.hass, self.address, connectable=True
        ):
            if (
                self.proxy_source
                and scanner_dev.scanner.source.upper() != self.proxy_source.upper()
            ):
                continue
            if scanner_dev.advertisement.rssi is not None:
                self._rssi_samples.append(scanner_dev.advertisement.rssi)
                self._last_published_rssi = self.rssi
                self._last_rssi_publish_monotonic = time.monotonic()
            break

    @callback
    def _on_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Track advertisement liveness and trigger catch-up polls when visible."""
        if not service_info.connectable:
            return
        if (
            self.proxy_source
            and service_info.source.upper() != self.proxy_source.upper()
        ):
            return

        now_mono = time.monotonic()
        prev_adv_monotonic = self._last_adv_monotonic
        self._last_adv_monotonic = now_mono
        if service_info.rssi is not None:
            self._rssi_samples.append(service_info.rssi)

        if self._poll_deferred and not self.is_polling:
            self._poll_deferred = False
            self.update_interval = self._next_interval()
            _LOGGER.debug(
                "Power NI meter %s advertisements resumed; triggering catch-up poll",
                self.address,
            )
            self.hass.async_create_task(self.async_request_refresh())
            return

        adv_resumed_after_gap = (
            prev_adv_monotonic is None
            or (now_mono - prev_adv_monotonic) >= FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS
        )
        if (
            self._consecutive_failures > 0
            and not self.is_polling
            and adv_resumed_after_gap
            and (
                self._last_failure_monotonic is None
                or (now_mono - self._last_failure_monotonic)
                >= FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS
            )
        ):
            self._last_failure_monotonic = now_mono
            _LOGGER.debug(
                "Power NI meter %s advertising after failure #%d; triggering recovery poll",
                self.address,
                self._consecutive_failures,
            )
            self.hass.async_create_task(self.async_request_refresh())
            return

        current_rssi = self.rssi
        if (
            self._listeners
            and current_rssi is not None
            and (
                self._last_published_rssi is None
                or abs(current_rssi - self._last_published_rssi)
                >= RSSI_PUBLISH_THRESHOLD_DBM
            )
            and (
                self._last_rssi_publish_monotonic is None
                or (now_mono - self._last_rssi_publish_monotonic)
                >= RSSI_MIN_PUBLISH_INTERVAL_SECONDS
            )
        ):
            self._last_published_rssi = current_rssi
            self._last_rssi_publish_monotonic = now_mono
            self.async_update_listeners()

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
        self.auto_sync_history = bool(
            options.get(CONF_AUTO_SYNC_HISTORY, DEFAULT_AUTO_SYNC_HISTORY)
        )
        if (
            not self.auto_sync_history
            and self._survey_task is not None
            and not self._survey_task.done()
        ):
            self._survey_task.cancel()
        new_proxy = (options.get(CONF_PROXY_SOURCE) or "").strip()
        if new_proxy != self.proxy_source:
            self.proxy_source = new_proxy
            self._last_adv_monotonic = None
            self._rssi_samples.clear()
            self._last_published_rssi = None
            self._last_rssi_publish_monotonic = None
            self._poll_deferred = False
            self._seed_initial_rssi()
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

    def _check_and_notify_stale(self, now: datetime) -> None:
        """Notify listeners if any readings have crossed the staleness threshold."""
        max_age = stale_after(self.scan_interval_minutes)
        newly_stale = {
            field
            for field, ts in self.field_read_at.items()
            if is_stale(ts, now, max_age)
        } - self._stale_notified
        if newly_stale:
            self._stale_notified.update(newly_stale)
            self.async_update_listeners()

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
            self._last_failure_monotonic = None
        else:
            self._consecutive_failures += 1
            self._last_failure_monotonic = time.monotonic()
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
            self._check_and_notify_stale(dt_util.now())

    async def async_request_full_refresh(self) -> None:
        """Refresh now, reading every value regardless of schedule."""
        self._force_full_read = True
        await self.async_request_refresh()

    def _enabled_sections(self) -> frozenset[str]:
        """Return sections that have at least one enabled entity in the registry."""
        entries = er.async_entries_for_config_entry(
            er.async_get(self.hass), self.config_entry.entry_id
        )
        by_uid = {ent.unique_id: ent for ent in entries}
        enabled: set[str] = set()
        for section, keys in SECTION_ENTITY_KEYS.items():
            for key in keys:
                uid = f"{self.address}_{key}"
                reg_entry = by_uid.get(uid)
                if reg_entry is None:
                    if key not in DISABLED_BY_DEFAULT_ENTITY_KEYS:
                        enabled.add(section)
                        break
                elif reg_entry.disabled_by is None:
                    enabled.add(section)
                    break
        return frozenset(enabled)

    def _mark_sections_read(self, data: PowerNIMeterData, now: datetime) -> None:
        """Record which slow groups this read actually returned values for."""
        if data.total_import_kwh is not None:
            self._section_last_read[SECTION_UNITS] = now
        if data.total_export_kwh is not None:
            self._section_last_read[SECTION_EXPORT] = now
        if data.unit_rate is not None:
            self._section_last_read[SECTION_RATE] = now
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
        """Restore persistent top-up and bonded-proxy state from storage."""
        stored = await self._store.async_load() or {}
        balance = stored.get("last_balance")
        self._top_up_baseline = float(balance) if balance is not None else None
        preferred = stored.get("preferred_proxy_source")
        self._preferred_proxy_source = (
            str(preferred).strip() or None if preferred else None
        )
        last_top_up = stored.get("last_top_up")
        self._write_with_response = bool(stored.get("write_with_response", False))
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
        return {
            "last_balance": self._top_up_baseline,
            "last_top_up": last_top_up,
            "preferred_proxy_source": self._preferred_proxy_source,
            "write_with_response": self._write_with_response,
        }

    def _handle_write_mode(self, write_with_response: bool) -> None:
        """Persist the GATT write mode that succeeded for this meter."""
        if write_with_response != self._write_with_response:
            self._write_with_response = write_with_response
            self._store.async_delay_save(self._stored_state, STORE_SAVE_DELAY_SECONDS)

    async def async_shutdown_ble(self) -> None:
        """Cancel and await any active BLE poll or survey task before unload."""
        tasks = [
            task
            for task in (self._active_poll_task, self._survey_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    def _format_proxy_label(self, source: str | None) -> str:
        """Return a human-readable adapter or ESPHome proxy label for Repair issues."""
        if source:
            target = source.upper()
            for scanner_dev in bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            ):
                if scanner_dev.scanner.source.upper() == target:
                    name = (scanner_dev.scanner.name or "").strip()
                    if not name:
                        return source
                    if target in name.upper():
                        return name
                    return f"{name} ({source})"
            return source
        return "your Bluetooth adapter / proxy"

    def _raise_pairing_issue(self, source: str | None) -> None:
        """Create or update an actionable Repair issue when BLE bonding is missing."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.pairing_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pairing_required",
            translation_placeholders={
                "name": self.device_name,
                "address": self.address,
                "proxy": self._format_proxy_label(source),
            },
            learn_more_url=PAIRING_LEARN_MORE_URL,
        )

    def _clear_pairing_issue(self) -> None:
        """Clear any open pairing Repair issue once an authenticated poll succeeds."""
        ir.async_delete_issue(self.hass, DOMAIN, self.pairing_issue_id)

    def _get_ble_device(self) -> BLEDevice | None:
        """Resolve the target BLEDevice (pinned, auto-sticky bonded, or strongest)."""
        if self.proxy_source:
            for scanner_dev in bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            ):
                if scanner_dev.scanner.source.upper() == self.proxy_source.upper():
                    return scanner_dev.ble_device
            return None
        if self._preferred_proxy_source:
            preferred = self._preferred_proxy_source.upper()
            for scanner_dev in bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            ):
                if scanner_dev.scanner.source.upper() == preferred:
                    return scanner_dev.ble_device
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_update_data(self) -> PowerNIMeterData:
        """Poll the meter, keeping last-known values for fields a read missed."""
        now = dt_util.now()
        silent_for = (
            time.monotonic() - self._last_adv_monotonic
            if self._last_adv_monotonic is not None
            else None
        )
        if (
            not self._force_full_read
            and self.data is not None
            and silent_for is not None
            and ADVERTISEMENT_SILENT_SECONDS
            < silent_for
            < ADVERTISEMENT_WAITING_TIMEOUT_SECONDS
        ):
            self._poll_deferred = True
            self._check_and_notify_stale(now)
            self._set_poll_status(POLL_STATUS_WAITING)
            remaining = max(60.0, ADVERTISEMENT_WAITING_TIMEOUT_SECONDS - silent_for)
            self.update_interval = min(
                self._next_interval(), timedelta(seconds=remaining)
            )
            _LOGGER.debug(
                "Deferring poll for %s: no connectable advertisement for %.0fs "
                "(re-checking in %.0fs)",
                self.address,
                silent_for,
                self.update_interval.total_seconds(),
            )
            return self.data

        if (
            not self._force_full_read
            and silent_for is not None
            and silent_for >= ADVERTISEMENT_WAITING_TIMEOUT_SECONDS
            and (self._poll_deferred or self.poll_status == POLL_STATUS_WAITING)
        ):
            _LOGGER.warning(
                "Power NI meter %s (%s) has not advertised for %.0f min; "
                "probing instead of waiting",
                self.device_name,
                self.address,
                silent_for / 60.0,
            )

        self._poll_deferred = False
        enabled = self._enabled_sections()
        sections = sections_due(
            now,
            self._section_last_read,
            force_full=self._force_full_read or self.data is None,
            enabled=enabled,
        )
        if not sections:
            _LOGGER.debug("No enabled sections due for %s; skipping BLE poll", self.address)
            self._force_full_read = False
            self._check_and_notify_stale(now)
            self._set_poll_status(POLL_STATUS_IDLE)
            return self.data if self.data is not None else PowerNIMeterData()
        _LOGGER.debug(
            "Polling %s for sections: %s", self.address, ", ".join(sorted(sections))
        )
        try:
            self._active_poll_task = asyncio.current_task()
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
        finally:
            self._active_poll_task = None
        self._record_result(True)
        self._force_full_read = False
        self._mark_sections_read(new_data, now)
        self.last_successful_read = now
        self._last_published_rssi = self.rssi
        self._last_rssi_publish_monotonic = time.monotonic()
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
        self._maybe_schedule_survey_sync(now)
        return merged

    def _maybe_schedule_survey_sync(self, now: datetime) -> None:
        """Schedule background 30-min energy history backfill when due."""
        if not self.auto_sync_history:
            return
        if "recorder" not in self.hass.config.components:
            return
        if self._survey_task is not None and not self._survey_task.done():
            return
        if (
            self._last_survey_sync is not None
            and now - self._last_survey_sync < SURVEY_SYNC_INTERVAL
        ):
            return
        if (
            self._last_survey_attempt is not None
            and now - self._last_survey_attempt < SURVEY_FAILURE_COOLDOWN
        ):
            return
        self._survey_task = self.hass.async_create_background_task(
            self._async_background_survey_sync(),
            name=f"{DOMAIN}_{self.address}_survey",
        )

    async def _async_background_survey_sync(self) -> None:
        """Run periodic survey sync without blocking or failing normal polls."""
        try:
            await asyncio.sleep(SURVEY_BACKGROUND_DELAY_SECONDS)
            await self.async_sync_energy_history(full=False)
        except (BleakError, PowerNIProtocolError, TimeoutError, UpdateFailed) as err:
            _LOGGER.warning(
                "Background energy survey sync failed for %s: %s",
                self.address,
                err,
            )

    def _require_ble_device(self) -> BLEDevice:
        """Return a connectable BLEDevice or raise UpdateFailed."""
        ble_device = self._get_ble_device()
        if ble_device is not None:
            return ble_device
        if self.proxy_source:
            raise UpdateFailed(
                f"Configured Bluetooth proxy {self.proxy_source} cannot currently see {self.address}"
            )
        raise UpdateFailed(
            f"No connectable Bluetooth proxy or adapter can currently see {self.address}"
        )

    async def async_sync_energy_history(self, *, full: bool = False) -> int:
        """Read 0xA0 half-hourly survey blocks and import into external statistics."""
        if "recorder" not in self.hass.config.components:
            _LOGGER.warning(
                "Recorder integration is not active; skipping energy history sync for %s",
                self.address,
            )
            return 0

        self._last_survey_attempt = dt_util.now()
        statistic_id = (
            f"{DOMAIN}:{self.address.lower().replace(':', '_')}_energy_history"
        )
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )
        last_rows = last_stats.get(statistic_id) or []
        days = SURVEY_MAX_DAYS if (full or not last_rows) else SURVEY_INCREMENTAL_DAYS
        last_start: float | None = last_rows[0]["start"] if last_rows else None
        running_sum = float(last_rows[0].get("sum") or 0.0) if last_rows else 0.0

        async with self._connect_lock:
            ble_device = self._require_ble_device()
            client: BleakClientWithServiceCache | None = None
            disconnected = asyncio.Event()

            def _on_disconnect(_client: Any) -> None:
                disconnected.set()

            try:
                self._set_poll_status(POLL_STATUS_CONNECTING)
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.device_name,
                    disconnected_callback=_on_disconnect,
                    ble_device_callback=self._require_ble_device,
                    use_services_cache=True,
                    pair=True,
                    max_attempts=3,
                )
                self._set_poll_status(POLL_STATUS_READING)
                async with asyncio.timeout(SURVEY_TIMEOUT_SECONDS):
                    blocks = await async_read_survey(
                        client,
                        days=days,
                        logger=_LOGGER,
                        disconnected=disconnected,
                        prefer_write_with_response=self._write_with_response,
                        on_write_mode=self._handle_write_mode,
                    )
            finally:
                if client is not None and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:
                        _LOGGER.debug(
                            "Ignoring error disconnecting survey client %s: %s",
                            self.address,
                            err,
                        )
                self._set_poll_status(POLL_STATUS_IDLE)

        slots = parse_survey_blocks(blocks, tz=dt_util.get_default_time_zone())
        hourly = aggregate_hourly(slots, dt_util.utcnow())
        new_hours = [
            hour
            for hour in hourly
            if last_start is None or hour.start.timestamp() > last_start
        ]

        self._last_survey_sync = dt_util.now()
        if not new_hours:
            _LOGGER.info(
                "Energy history up to date for %s (read %d blocks, %d hourly buckets)",
                self.address,
                len(blocks),
                len(hourly),
            )
            return 0

        stat_rows: list[StatisticData] = []
        for hour in new_hours:
            running_sum = round(running_sum + hour.kwh, 5)
            stat_rows.append(
                StatisticData(start=hour.start, state=hour.kwh, sum=running_sum)
            )

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{self.device_name} Energy history",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(self.hass, metadata, stat_rows)
        imported_kwh = round(sum(hour.kwh for hour in new_hours), 3)
        _LOGGER.info(
            "Imported %d hourly energy statistics (%.3f kWh, %s .. %s) into %s",
            len(stat_rows),
            imported_kwh,
            new_hours[0].start.isoformat(),
            new_hours[-1].start.isoformat(),
            statistic_id,
        )
        return len(stat_rows)

    async def _async_poll(self, sections: frozenset[str]) -> PowerNIMeterData:
        """Connect over BLE, read telemetry, and disconnect."""
        async with self._connect_lock:
            last_err: Exception | None = None
            for attempt in range(1, SESSION_RETRY_ATTEMPTS + 1):
                ble_device = self._require_ble_device()
                used_source = _extract_ble_device_source(ble_device) or (
                    self.proxy_source or None
                )

                client: BleakClientWithServiceCache | None = None
                details = getattr(ble_device, "details", None)
                disconnected = asyncio.Event()
                should_retry = False

                def _on_disconnect(
                    _client: Any, event: asyncio.Event = disconnected
                ) -> None:
                    event.set()

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
                        ble_device_callback=self._require_ble_device,
                        use_services_cache=True,
                        pair=True,
                        max_attempts=3,
                    )
                    self._set_poll_status(POLL_STATUS_READING)
                    data = await async_read_meter(
                        client,
                        _LOGGER,
                        sections,
                        disconnected=disconnected,
                        on_progress=self._handle_partial_progress,
                        prefer_write_with_response=self._write_with_response,
                        on_write_mode=self._handle_write_mode,
                    )
                    self._clear_pairing_issue()
                    if used_source and used_source != self._preferred_proxy_source:
                        self._preferred_proxy_source = used_source
                        self._store.async_delay_save(
                            self._stored_state, STORE_SAVE_DELAY_SECONDS
                        )
                    return data
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
                        except Exception as cache_err:
                            _LOGGER.debug(
                                "Ignoring error clearing service cache for %s: %s",
                                self.address,
                                cache_err,
                            )
                    if client is not None and attempt < SESSION_RETRY_ATTEMPTS:
                        should_retry = True
                    else:
                        if is_auth_or_pairing_error(err):
                            self._raise_pairing_issue(used_source)
                            if (
                                not self.proxy_source
                                and self._preferred_proxy_source
                                and used_source
                                and used_source.upper()
                                == self._preferred_proxy_source.upper()
                            ):
                                self._preferred_proxy_source = None
                                self._store.async_delay_save(
                                    self._stored_state, STORE_SAVE_DELAY_SECONDS
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
