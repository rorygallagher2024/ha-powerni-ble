"""BLE protocol client and LCD frame parser for Power NI Keypad+ Freedom Units."""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Any

from .const import (
    CMD_DISPLAY_RESET,
    CMD_ENTER_TRANSPARENT,
    CMD_GET_BALANCE,
    CMD_KEEPALIVE,
    CMD_LEAVE_TRANSPARENT,
    CMD_PREV_USAGE,
    CMD_TOTAL_CONSUMPTION,
    CMD_TOTAL_UNITS,
    NOTIFY_UUID,
    RSP_DISCONNECT_ACK,
    RSP_KEEPALIVE_ACK,
    RSP_TRANSPARENT_ACK,
    WRITE_UUID,
)

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

CONTROL_FRAMES = (
    RSP_TRANSPARENT_ACK,
    RSP_KEEPALIVE_ACK,
    RSP_DISCONNECT_ACK,
)

RE_IGNORE_HEADER = re.compile(r"(ACCOUNT|DAYS LEFT|KW  LOAD|LOAD COST)")
RE_RESET_ACK = re.compile(r"%%%%%%%%%")
RE_BALANCE = re.compile(r"(?:^|\s)(\d+)\s*(-?#\d+\.\d+)")
RE_DAYS_LEFT = re.compile(r"(?:(MATCH|OVER|ABOUT|BELOW)\s+(\d+)|(NO DATA))")
RE_BALANCE_OR_DAYS_LEFT = re.compile(
    f"(?:{RE_BALANCE.pattern})|(?:{RE_DAYS_LEFT.pattern})"
)
RE_LOAD_KW = re.compile(r"KW\s*(-?\d+\.\d+)")
RE_LOAD_COST = re.compile(r"\s*#?(\d+\.\d+)/HR")
RE_IMP_UNITS = re.compile(r"(\d+)\s*IMP UNITS")
RE_PREV_LABEL = re.compile(r"PREV ([A-Z]+)")
RE_MONTH_USAGE = re.compile(
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*#?(\d+\.\d+)"
)
RE_USAGE_AMOUNT = re.compile(r"\s*(-)?#(\d+\.\d+)")

COMMAND_PACING_DELAY = 0.25
LINE_TIMEOUT = 4.0
TRANSPARENT_ACK_TIMEOUT = 5.0
KEEPALIVE_ACK_TIMEOUT = 2.0
RESET_ACK_TIMEOUT = 1.5
DISCONNECT_ACK_TIMEOUT = 0.3


class PowerNIProtocolError(Exception):
    """Raised when the meter fails to respond with expected protocol frames."""


class _LineTimeoutError(asyncio.TimeoutError):
    """Timeout carrying any LCD lines observed before timing out."""

    def __init__(self, seen_lines: list[str]) -> None:
        super().__init__()
        self.seen_lines = seen_lines


@dataclass
class PowerNIMeterData:
    """Parsed telemetry values from a Power NI Freedom Unit."""

    balance: float | None = None
    days_left: int | None = None
    days_left_status: str | None = None
    current_load_kw: float | None = None
    current_cost_per_hour: float | None = None
    total_import_kwh: int | None = None
    yesterday_cost: float | None = None
    last_7_days_cost: float | None = None
    last_month_cost: float | None = None

    def has_any_value(self) -> bool:
        """Return True if at least one metric was parsed."""
        return any(
            value is not None
            for value in (
                self.balance,
                self.days_left,
                self.days_left_status,
                self.current_load_kw,
                self.current_cost_per_hour,
                self.total_import_kwh,
                self.yesterday_cost,
                self.last_7_days_cost,
                self.last_month_cost,
            )
        )


def merge_meter_data(
    new: PowerNIMeterData, previous: PowerNIMeterData | None
) -> tuple[PowerNIMeterData, list[str]]:
    """Fill fields missing from a partial read with the previous known values.

    Returns the merged data and the names of the fields carried over.
    """
    if previous is None:
        return new, []
    carried: list[str] = []
    merged = dataclasses.replace(new)
    for field in dataclasses.fields(PowerNIMeterData):
        if getattr(merged, field.name) is None:
            old_value = getattr(previous, field.name)
            if old_value is not None:
                setattr(merged, field.name, old_value)
                carried.append(field.name)
    return merged, carried


class FrameAssembler:
    """Reassembles 4-byte control ACKs and 10-byte-multiple reversed LCD frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def _find_first_control_frame(self) -> tuple[int, bytes] | None:
        """Return (offset, frame) for the earliest control ACK inside _buf."""
        best_pos: int | None = None
        best_frame: bytes | None = None
        for frame in CONTROL_FRAMES:
            pos = self._buf.find(frame)
            if pos != -1 and (best_pos is None or pos < best_pos):
                best_pos = pos
                best_frame = frame
        if best_pos is None or best_frame is None:
            return None
        return best_pos, best_frame

    @staticmethod
    def _decode_segment(seg: bytes) -> str:
        """Decode and reverse a complete LCD buffer ending in 0x0D."""
        raw = seg[:-1].decode("ascii", errors="ignore")[::-1]
        return raw.replace("\r", " ").replace("\n", " ")

    def feed(self, data: bytes) -> tuple[list[bytes], list[str]]:
        """Append incoming bytes and return extracted (control_frames, decoded_lines)."""
        self._buf.extend(data)
        controls: list[bytes] = []
        lines: list[str] = []

        while self._buf:
            head = bytes(self._buf[:4])
            if len(self._buf) >= 4 and head in CONTROL_FRAMES:
                controls.append(head)
                del self._buf[:4]
                continue

            if len(self._buf) < 4 and any(
                ctrl.startswith(bytes(self._buf)) for ctrl in CONTROL_FRAMES
            ):
                break

            ctrl_hit = self._find_first_control_frame()
            seg_end = ctrl_hit[0] if ctrl_hit is not None else len(self._buf)
            seg = bytes(self._buf[:seg_end])

            if seg and len(seg) % 10 == 0 and seg[-1] == 0x0D:
                lines.append(self._decode_segment(seg))
                del self._buf[:seg_end]
                continue

            if len(seg) > 10 and seg[-1] == 0x0D:
                first_cr = seg.index(0x0D)
                del self._buf[: first_cr + 1]
                continue

            if ctrl_hit is not None:
                ctrl_pos, ctrl_frame = ctrl_hit
                del self._buf[: ctrl_pos + len(ctrl_frame)]
                controls.append(ctrl_frame)
                continue

            if len(self._buf) > 256:
                self._buf.clear()
            break

        return controls, lines


async def _wait_control_frame(
    ctrl_queue: asyncio.Queue[bytes], expected: bytes, timeout: float
) -> bool:
    """Wait until `expected` 4-byte control frame arrives or timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        try:
            frame = await asyncio.wait_for(ctrl_queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        if frame == expected:
            return True


async def _wait_matching_line(
    line_queue: asyncio.Queue[str],
    pattern: re.Pattern[str] | None,
    timeout: float,
    *,
    ignore_reset: bool = True,
) -> tuple[str, re.Match[str] | None]:
    """Wait for a non-ignored LCD line matching `pattern` (or any line if None)."""
    deadline = asyncio.get_running_loop().time() + timeout
    seen_lines: list[str] = []
    prev_line: str | None = None

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _LineTimeoutError(seen_lines)
        try:
            line = await asyncio.wait_for(line_queue.get(), timeout=remaining)
        except asyncio.TimeoutError as err:
            raise _LineTimeoutError(seen_lines) from err

        seen_lines.append(line)

        if pattern is None:
            has_usage_data = bool(
                RE_PREV_LABEL.search(line)
                or RE_USAGE_AMOUNT.search(line)
                or RE_MONTH_USAGE.search(line)
            )
            if not has_usage_data and (
                RE_IGNORE_HEADER.search(line)
                or (ignore_reset and RE_RESET_ACK.search(line))
            ):
                continue
            return line, None

        candidates = [line]
        if prev_line is not None:
            candidates.extend((f"{line} {prev_line}", f"{prev_line} {line}"))
        for candidate in candidates:
            match = pattern.search(candidate)
            if match is not None:
                return candidate, match

        if RE_IGNORE_HEADER.search(line) or (ignore_reset and RE_RESET_ACK.search(line)):
            if not (ignore_reset and RE_RESET_ACK.search(line)):
                prev_line = line
            continue
        prev_line = line


def _format_char(char: Any) -> str:
    """Format characteristic UUID, handle, properties, and descriptors concisely."""
    if char is None:
        return "None"
    descs = [
        f"{d.uuid}@0x{d.handle:04x}" for d in (getattr(char, "descriptors", None) or [])
    ]
    return (
        f"{char.uuid}@0x{char.handle:04x}"
        f"(props={list(getattr(char, 'properties', ()))}, descs={descs})"
    )


def _summarize_services(client: BleakClient) -> tuple[str, Any, Any]:
    """Build a one-line GATT summary and look up WRITE/NOTIFY characteristics."""
    services = getattr(client, "services", None)
    if not services:
        return "none", None, None

    parts: list[str] = []
    for service in services:
        char_parts = [_format_char(char) for char in service.characteristics]
        parts.append(f"{service.uuid}@0x{service.handle:04x}[{'; '.join(char_parts)}]")

    write_char = (
        services.get_characteristic(WRITE_UUID)
        if hasattr(services, "get_characteristic")
        else None
    )
    notify_char = (
        services.get_characteristic(NOTIFY_UUID)
        if hasattr(services, "get_characteristic")
        else None
    )
    return " | ".join(parts), write_char, notify_char


def _log_gatt_services(client: BleakClient, logger: logging.Logger) -> None:
    """Log discovered GATT services and characteristics at DEBUG level."""
    services = getattr(client, "services", None)
    if not services or not logger.isEnabledFor(logging.DEBUG):
        return
    for service in services:
        logger.debug("GATT service %s (handle 0x%04x)", service.uuid, service.handle)
        for char in service.characteristics:
            logger.debug(
                "  char %s (handle 0x%04x, props=%s)",
                char.uuid,
                char.handle,
                char.properties,
            )


async def async_read_meter(
    client: BleakClient, logger: logging.Logger = _LOGGER
) -> PowerNIMeterData:
    """Run one transparent-mode telemetry poll against a connected Freedom Unit."""
    _log_gatt_services(client, logger)

    assembler = FrameAssembler()
    ctrl_queue: asyncio.Queue[bytes] = asyncio.Queue()
    line_queue: asyncio.Queue[str] = asyncio.Queue()
    raw_notifies: list[str] = []
    notify_started = False
    write_with_response = False

    def _on_notify(_sender: Any, payload: bytearray) -> None:
        raw = bytes(payload)
        if len(raw_notifies) < 20:
            raw_notifies.append(raw.hex(" "))
        logger.debug("BLE notify %dB: %s", len(raw), raw.hex(" "))
        controls, lines = assembler.feed(raw)
        for ctrl in controls:
            logger.debug("Control ACK: %s", ctrl.hex(" "))
            ctrl_queue.put_nowait(ctrl)
        for line in lines:
            logger.debug("LCD line: %r", line)
            line_queue.put_nowait(line)

    async def _send(cmd: bytes) -> None:
        await asyncio.sleep(COMMAND_PACING_DELAY)
        logger.debug("BLE write %s (response=%s)", cmd.hex(" "), write_with_response)
        await client.write_gatt_char(WRITE_UUID, cmd, response=write_with_response)

    async def _display_reset() -> None:
        await _send(CMD_DISPLAY_RESET)
        try:
            await _wait_matching_line(
                line_queue, RE_RESET_ACK, RESET_ACK_TIMEOUT, ignore_reset=False
            )
        except asyncio.TimeoutError:
            logger.debug("Display reset ACK timed out; continuing")

    logger.info("Starting Power NI BLE poll")
    await client.start_notify(NOTIFY_UUID, _on_notify)
    notify_started = True

    data = PowerNIMeterData()

    try:
        logger.debug("Sending ENTER_TRANSPARENT_MODE")
        await client.write_gatt_char(WRITE_UUID, CMD_ENTER_TRANSPARENT, response=False)
        got_ack = await _wait_control_frame(
            ctrl_queue, RSP_TRANSPARENT_ACK, TRANSPARENT_ACK_TIMEOUT
        )
        if not got_ack:
            summary, write_char, notify_char = _summarize_services(client)
            logger.warning(
                "Transparent ACK (response=False) timed out; raw_notifies(%d)=%s; notify_char=%s; gatt=[%s]",
                len(raw_notifies),
                raw_notifies,
                _format_char(notify_char),
                summary,
            )
            if write_char is not None and "write" in getattr(
                write_char, "properties", ()
            ):
                try:
                    await client.write_gatt_char(
                        WRITE_UUID, CMD_ENTER_TRANSPARENT, response=True
                    )
                    logger.warning(
                        "Write-with-response succeeded for ENTER_TRANSPARENT_MODE; waiting 2.5s for 55 cc 01 33"
                    )
                    got_ack = await _wait_control_frame(
                        ctrl_queue, RSP_TRANSPARENT_ACK, 2.5
                    )
                    if got_ack:
                        write_with_response = True
                    else:
                        logger.warning(
                            "Write-with-response ACK still timed out; raw_notifies(%d)=%s",
                            len(raw_notifies),
                            raw_notifies,
                        )
                except Exception as err:
                    logger.warning(
                        "Write-with-response error: %s: %s", type(err).__name__, err
                    )
            else:
                logger.warning(
                    "Skipping write-with-response fallback; write_char=%s",
                    _format_char(write_char),
                )

        if not got_ack:
            raise PowerNIProtocolError(
                "Timed out waiting for transparent mode acknowledgement (55 cc 01 33)"
            )
        logger.info("Entered transparent mode on Power NI meter")

        # 1. Balance and days remaining (meter may alternate in either order)
        await _display_reset()
        for _ in range(3):
            if data.balance is not None and data.days_left_status is not None:
                break
            await _send(CMD_GET_BALANCE)
            try:
                matched_line, _ = await _wait_matching_line(
                    line_queue, RE_BALANCE_OR_DAYS_LEFT, LINE_TIMEOUT
                )
            except asyncio.TimeoutError as err:
                logger.warning(
                    "Timed out reading balance/days remaining (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )
                continue

            bal_match = RE_BALANCE.search(matched_line)
            if bal_match is not None and data.balance is None:
                data.balance = float(bal_match.group(2).replace("#", ""))
            dl_match = RE_DAYS_LEFT.search(matched_line)
            if dl_match is not None and data.days_left_status is None:
                data.days_left_status = " ".join(dl_match.group(0).split())
                data.days_left = (
                    int(dl_match.group(2)) if dl_match.group(2) is not None else None
                )

        # 2. Live kW load and current £/hr rate
        await _display_reset()
        try:
            await _send(CMD_TOTAL_CONSUMPTION)
            _, match = await _wait_matching_line(line_queue, RE_LOAD_KW, LINE_TIMEOUT)
            assert match is not None
            data.current_load_kw = float(match.group(1))
        except asyncio.TimeoutError as err:
            logger.warning(
                "Timed out reading current kW load (seen_lines=%r)",
                getattr(err, "seen_lines", []),
            )

        try:
            await _send(CMD_TOTAL_CONSUMPTION)
            _, match = await _wait_matching_line(line_queue, RE_LOAD_COST, LINE_TIMEOUT)
            assert match is not None
            data.current_cost_per_hour = float(match.group(1))
        except asyncio.TimeoutError as err:
            logger.warning(
                "Timed out reading current hourly cost (seen_lines=%r)",
                getattr(err, "seen_lines", []),
            )

        # 3. Keepalive + cumulative import units (kWh)
        await _send(CMD_KEEPALIVE)
        if not await _wait_control_frame(
            ctrl_queue, RSP_KEEPALIVE_ACK, KEEPALIVE_ACK_TIMEOUT
        ):
            logger.debug("Keepalive ACK timed out; continuing")

        await _display_reset()
        try:
            await _send(CMD_TOTAL_UNITS)
            _, match = await _wait_matching_line(line_queue, RE_IMP_UNITS, LINE_TIMEOUT)
            assert match is not None
            data.total_import_kwh = int(match.group(1))
        except asyncio.TimeoutError as err:
            logger.warning(
                "Timed out reading total import kWh units (seen_lines=%r)",
                getattr(err, "seen_lines", []),
            )

        # 4. Recent spend (yesterday / last 7 days / last month)
        await _display_reset()
        current_bucket: str | None = None
        for _ in range(6):
            if (
                data.yesterday_cost is not None
                and data.last_7_days_cost is not None
                and data.last_month_cost is not None
            ):
                break
            await _send(CMD_PREV_USAGE)
            try:
                line, _ = await _wait_matching_line(line_queue, None, LINE_TIMEOUT)
            except asyncio.TimeoutError as err:
                logger.warning(
                    "Timed out reading recent usage line (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )
                break

            if RE_MONTH_USAGE.search(line):
                break

            prev_match = RE_PREV_LABEL.search(line)
            if prev_match is not None:
                current_bucket = prev_match.group(1).lower()

            amt_match = RE_USAGE_AMOUNT.search(line)
            if amt_match is not None and current_bucket:
                amount = float(amt_match.group(2))
                if amt_match.group(1):
                    amount = -amount
                if current_bucket == "day":
                    data.yesterday_cost = amount
                elif current_bucket == "week":
                    data.last_7_days_cost = amount
                elif current_bucket == "mnth":
                    data.last_month_cost = amount
                current_bucket = None

        if not data.has_any_value():
            raise PowerNIProtocolError("No readings were received from the meter")

        logger.info(
            "Power NI meter read complete: balance=%s days_left=%s (%s) "
            "load_kw=%s cost_per_hr=%s imp_kwh=%s yesterday=%s last_7d=%s last_month=%s",
            data.balance,
            data.days_left,
            data.days_left_status,
            data.current_load_kw,
            data.current_cost_per_hour,
            data.total_import_kwh,
            data.yesterday_cost,
            data.last_7_days_cost,
            data.last_month_cost,
        )
        return data
    finally:
        try:
            await client.write_gatt_char(WRITE_UUID, CMD_LEAVE_TRANSPARENT, response=False)
            await _wait_control_frame(
                ctrl_queue, RSP_DISCONNECT_ACK, DISCONNECT_ACK_TIMEOUT
            )
        except Exception as err:
            logger.debug("Ignoring error leaving transparent mode: %s", err)
        if notify_started:
            try:
                await client.stop_notify(NOTIFY_UUID)
            except Exception as err:
                logger.debug("Ignoring error stopping notifications: %s", err)
