"""Constants for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import re

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

# Options
CONF_PROXY_SOURCE = "proxy_source"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 1440


def clamp_scan_interval(value: float | int | str | None) -> int:
    """Parse and clamp a poll interval in minutes to the valid range."""
    try:
        minutes = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        minutes = DEFAULT_SCAN_INTERVAL_MINUTES
    return max(MIN_SCAN_INTERVAL_MINUTES, min(MAX_SCAN_INTERVAL_MINUTES, minutes))
