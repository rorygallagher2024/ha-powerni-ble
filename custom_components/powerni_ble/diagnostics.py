"""Diagnostics support for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from . import PowerNIConfigEntry
from .const import CONF_PROXY_SOURCE

TO_REDACT = {
    CONF_ADDRESS,
    CONF_NAME,
    CONF_PROXY_SOURCE,
    "preferred_proxy_source",
    "title",
    "unique_id",
    "source",
}


def _redact_error(
    error: str | None, sensitive_values: Iterable[str | None]
) -> str | None:
    """Scrub known sensitive identifiers (MACs, serials, proxy IDs) from error strings."""
    if not error:
        return error
    redacted = error
    values = sorted(
        {val.strip() for val in sensitive_values if val and val.strip()},
        key=len,
        reverse=True,
    )
    for value in values:
        redacted = re.sub(re.escape(value), REDACTED, redacted, flags=re.IGNORECASE)
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PowerNIConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a Power NI Keypad+ BLE config entry."""
    coordinator = entry.runtime_data

    last_top_up = (
        {
            "amount": coordinator.last_top_up.amount,
            "at": coordinator.last_top_up.at.isoformat(),
            "balance_before": coordinator.last_top_up.balance_before,
            "balance_after": coordinator.last_top_up.balance_after,
        }
        if coordinator.last_top_up is not None
        else None
    )

    scanners = [
        {
            "scanner_name": scanner_dev.scanner.name,
            "source": scanner_dev.scanner.source,
            "rssi": scanner_dev.advertisement.rssi,
            "connectable": scanner_dev.ble_device is not None,
        }
        for scanner_dev in bluetooth.async_scanner_devices_by_address(
            hass, coordinator.address, connectable=True
        )
    ]

    payload: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": {
            "scan_interval_minutes": coordinator.scan_interval_minutes,
            "auto_sync_history": coordinator.auto_sync_history,
            "preferred_proxy_source": coordinator._preferred_proxy_source,
            "poll_status": coordinator.poll_status,
            "last_error": _redact_error(
                coordinator.last_error,
                (
                    coordinator.address,
                    coordinator.device_name,
                    coordinator.proxy_source,
                    coordinator._preferred_proxy_source,
                    entry.title,
                    entry.unique_id,
                    *(str(s["source"]) for s in scanners if s.get("source")),
                ),
            ),
            "consecutive_failures": coordinator._consecutive_failures,
            "poll_deferred": coordinator._poll_deferred,
            "seconds_since_last_advertisement": (
                coordinator.seconds_since_last_advertisement
            ),
            "rssi": coordinator.rssi,
            "last_successful_read": (
                coordinator.last_successful_read.isoformat()
                if coordinator.last_successful_read is not None
                else None
            ),
            "last_survey_sync": (
                coordinator._last_survey_sync.isoformat()
                if coordinator._last_survey_sync is not None
                else None
            ),
            "last_survey_attempt": (
                coordinator._last_survey_attempt.isoformat()
                if coordinator._last_survey_attempt is not None
                else None
            ),
            "section_last_read": {
                key: value.isoformat()
                for key, value in coordinator._section_last_read.items()
            },
            "field_read_at": {
                key: value.isoformat()
                for key, value in coordinator.field_read_at.items()
            },
            "last_top_up": last_top_up,
            "data": (
                dataclasses.asdict(coordinator.data)
                if coordinator.data is not None
                else None
            ),
        },
        "bluetooth_scanners": scanners,
    }
    return dict(async_redact_data(payload, TO_REDACT))
