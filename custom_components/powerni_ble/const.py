"""Constants for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta

DOMAIN = "powerni_ble"

METER_NAME_REGEX = re.compile(r"^((?:[A-Z]{3}\d{5})|(?:B\d{7}))$")

# Microchip/ISSC Transparent UART GATT service and characteristics
SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

# Transparent mode control frames
CMD_ENTER_TRANSPARENT = bytes([0x55, 0xBB, 0x01, 0x44])
CMD_KEEPALIVE = bytes([0x55, 0xBB, 0x03, 0x42])
CMD_LEAVE_TRANSPARENT = bytes([0x55, 0xBB, 0x02, 0x43])

RSP_TRANSPARENT_ACK = bytes([0x55, 0xCC, 0x01, 0x33])
RSP_KEEPALIVE_ACK = bytes([0x55, 0xCC, 0x03, 0x31])
RSP_DISCONNECT_ACK = bytes([0x55, 0xCC, 0x02, 0x32])

# LCD keypad query commands
CMD_DISPLAY_RESET = bytes([0xC2, 0x30])
CMD_GET_BALANCE = bytes([0xC2, 0x31])
CMD_PREV_USAGE = bytes([0xC2, 0x32])
CMD_TOTAL_CONSUMPTION = bytes([0xC2, 0x36])
CMD_TOTAL_UNITS = bytes([0xC2, 0x39])

# 14-day half-hourly energy survey commands & markers
CMD_READ_SURVEY = bytes([0xA0])
CMD_SURVEY_NEXT = bytes([0xF1])
RSP_SURVEY_ACK = 0xFC
RSP_SURVEY_BLOCK_HEADER = 0x8A
RSP_SURVEY_END = 0xFF
SURVEY_BLOCK_SIZE = 10
SURVEY_BLOCKS_PER_DAY = 13
SURVEY_MAX_DAYS = 14
SURVEY_INCREMENTAL_DAYS = 2
SURVEY_KEEPALIVE_EVERY_BLOCKS = 10
SURVEY_SYNC_INTERVAL = timedelta(hours=6)

# Defer scheduled polls when no connectable advertisement has been heard for
# this long (e.g. while the phone app holds the meter's single BLE link),
# then catch up as soon as advertisements resume.
ADVERTISEMENT_SILENT_SECONDS = 90.0
FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS = 15.0
RSSI_MIN_PUBLISH_INTERVAL_SECONDS = 60.0
RSSI_MEDIAN_WINDOW = 15

# Options
CONF_AUTO_SYNC_HISTORY = "auto_sync_history"
CONF_PROXY_SOURCE = "proxy_source"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_AUTO_SYNC_HISTORY = True
DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 1440
PAIRING_LEARN_MORE_URL = (
    "https://github.com/rorygallagher2024/ha-powerni-ble"
    "#prerequisites-one-time-6-digit-pin-pairing"
)

# After a failed poll, retry sooner than the normal interval (minutes, per
# consecutive failure). Never slower than the configured interval.
RETRY_BACKOFF_MINUTES = (1, 2, 5)

# Upper bound for one complete connect + read cycle, so a hung BLE operation
# can never hold the connection lock and block later polls.
POLL_TIMEOUT_SECONDS = 180
SURVEY_TIMEOUT_SECONDS = 300

# Retry a transient mid-session BLE drop or protocol timeout once inside the
# same poll after letting the Freedom Unit's BLE radio return to idle.
SESSION_RETRY_ATTEMPTS = 2
SESSION_RETRY_DELAY_SECONDS = 3.0

# Live poll lifecycle states (exposed via the Poll status enum sensor)
POLL_STATUS_IDLE = "idle"
POLL_STATUS_CONNECTING = "connecting"
POLL_STATUS_READING = "reading"
POLL_STATUS_RETRYING = "retrying"
POLL_STATUS_WAITING = "waiting"
POLL_STATUS_ERROR = "error"
POLL_STATUS_OPTIONS: tuple[str, ...] = (
    POLL_STATUS_IDLE,
    POLL_STATUS_CONNECTING,
    POLL_STATUS_READING,
    POLL_STATUS_RETRYING,
    POLL_STATUS_WAITING,
    POLL_STATUS_ERROR,
)


# Groups of readings fetched in one poll. Balance/days-left is read on every
# poll; slow-changing groups are only re-read when they are due, which keeps
# most BLE sessions short.
SECTION_BALANCE = "balance"
SECTION_LIVE = "live"  # current kW load and £/hr
SECTION_UNITS = "units"  # cumulative import kWh
SECTION_SPEND = "spend"  # yesterday / last 7 days / last month
ALL_SECTIONS = frozenset({SECTION_BALANCE, SECTION_LIVE, SECTION_UNITS, SECTION_SPEND})

# Maximum age before a slow section is read again.
SECTION_REFRESH_INTERVALS = {
    SECTION_UNITS: timedelta(hours=1),
    SECTION_SPEND: timedelta(hours=6),
}


def sections_due(
    now: datetime,
    last_read: Mapping[str, datetime],
    force_full: bool = False,
) -> frozenset[str]:
    """Return the reading groups to fetch on this poll.

    A slow section is due when it has never been read, its refresh interval
    has elapsed, or (for daily spend figures) the local date has changed
    since it was last read.
    """
    if force_full:
        return ALL_SECTIONS
    due = {SECTION_BALANCE, SECTION_LIVE}
    for section, max_age in SECTION_REFRESH_INTERVALS.items():
        last = last_read.get(section)
        if (
            last is None
            or now - last >= max_age
            or (section == SECTION_SPEND and now.date() != last.date())
        ):
            due.add(section)
    return frozenset(due)


def retry_interval_minutes(consecutive_failures: int, scan_interval: int) -> int:
    """Return the delay before the next poll given consecutive failures."""
    if consecutive_failures <= 0:
        return scan_interval
    step = min(consecutive_failures, len(RETRY_BACKOFF_MINUTES)) - 1
    return min(RETRY_BACKOFF_MINUTES[step], scan_interval)


def clamp_scan_interval(value: float | int | str | None) -> int:
    """Parse and clamp a poll interval in minutes to the valid range."""
    try:
        minutes = round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        minutes = DEFAULT_SCAN_INTERVAL_MINUTES
    return max(MIN_SCAN_INTERVAL_MINUTES, min(MAX_SCAN_INTERVAL_MINUTES, minutes))
