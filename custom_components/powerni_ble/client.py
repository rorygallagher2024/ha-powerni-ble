"""BLE protocol client and LCD frame parser for Power NI Keypad+ Freedom Units."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import (
    ALL_SECTIONS,
    CMD_DISPLAY_RESET,
    CMD_ENTER_TRANSPARENT,
    CMD_GET_BALANCE,
    CMD_KEEPALIVE,
    CMD_LEAVE_TRANSPARENT,
    CMD_PREV_USAGE,
    CMD_READ_SURVEY,
    CMD_SURVEY_NEXT,
    CMD_TOTAL_CONSUMPTION,
    CMD_TOTAL_UNITS,
    CMD_UNIT_RATE,
    NOTIFY_UUID,
    RSP_DISCONNECT_ACK,
    RSP_KEEPALIVE_ACK,
    RSP_SURVEY_ACK,
    RSP_SURVEY_BLOCK_HEADER,
    RSP_SURVEY_END,
    RSP_TRANSPARENT_ACK,
    SECTION_LIVE,
    SECTION_RATE,
    SECTION_SPEND,
    SECTION_UNITS,
    SURVEY_BLOCK_SIZE,
    SURVEY_BLOCKS_PER_DAY,
    SURVEY_KEEPALIVE_EVERY_BLOCKS,
    SURVEY_MAX_DAYS,
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

RE_IGNORE_HEADER = re.compile(r"(ACCOUNT|DAYS LEFT|KW  LOAD|LOAD COST|R1 PRICE)")
RE_RESET_ACK = re.compile(r"%%%%%%%%%")
RE_BALANCE = re.compile(r"(?:^|\s)(\d+)\s*(-?#\d+\.\d+)")
RE_DAYS_LEFT = re.compile(r"(?:(MATCH|OVER|ABOUT|BELOW)\s+(\d+)|(NO DATA))")
RE_BALANCE_OR_DAYS_LEFT = re.compile(
    f"(?:{RE_BALANCE.pattern})|(?:{RE_DAYS_LEFT.pattern})"
)
RE_LOAD_KW = re.compile(r"KW\s*(-?\d+\.\d+)")
RE_LOAD_COST = re.compile(r"\s*#?(\d+\.\d+)/HR")
RE_IMP_UNITS = re.compile(r"(\d+)\s*IMP UNITS")
RE_UNIT_RATE_PENCE = re.compile(r"(?:^|\s)(\d+(?:\.\d+)?)\s*P(?:\s|$)")
RE_PREV_LABEL = re.compile(r"PREV ([A-Z]+)")
RE_MONTH_USAGE = re.compile(
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*#?(\d+\.\d+)"
)
RE_USAGE_AMOUNT = re.compile(r"\s*(-)?#(\d+\.\d+)")

COMMAND_PACING_DELAY = 0.25
SURVEY_PACING_DELAY = 0.05
SURVEY_ACK_TIMEOUT = 5.0
LINE_TIMEOUT = 4.0
TRANSPARENT_ACK_TIMEOUT = 5.0
KEEPALIVE_ACK_TIMEOUT = 2.0
RESET_ACK_TIMEOUT = 1.5
DISCONNECT_ACK_TIMEOUT = 0.3


class PowerNIProtocolError(Exception):
    """Raised when the meter fails to respond with expected protocol frames."""


class PowerNIDisconnectedError(PowerNIProtocolError):
    """Raised when the BLE connection drops mid-poll."""


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
    unit_rate: float | None = None
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
                self.unit_rate,
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


class SurveyFrameAssembler:
    """Reassembles binary 10-byte 0x8A survey blocks and single-byte markers."""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending_bytes(self) -> int:
        """Return the number of incomplete trailing bytes buffered."""
        return len(self._buf)

    @property
    def pending_hex(self) -> str:
        """Return buffered trailing bytes formatted as space-separated hex."""
        return self._buf.hex(" ")

    def feed(
        self, chunk: bytes
    ) -> tuple[list[bytes], list[bytes], bool, bool]:
        """Feed raw BLE notification bytes and return parsed survey events."""
        self._buf.extend(chunk)
        controls: list[bytes] = []
        blocks: list[bytes] = []
        got_ack = False
        got_end = False

        while self._buf:
            matched_ctrl = False
            for ctrl in CONTROL_FRAMES:
                if self._buf.startswith(ctrl):
                    controls.append(bytes(ctrl))
                    del self._buf[: len(ctrl)]
                    matched_ctrl = True
                    break
            if matched_ctrl:
                continue

            if len(self._buf) < 4 and any(
                ctrl.startswith(bytes(self._buf)) for ctrl in CONTROL_FRAMES
            ):
                break

            head = self._buf[0]
            if head == RSP_SURVEY_ACK:
                got_ack = True
                del self._buf[0]
                continue
            if head == RSP_SURVEY_END:
                got_end = True
                del self._buf[0]
                continue
            if head == RSP_SURVEY_BLOCK_HEADER:
                if len(self._buf) < SURVEY_BLOCK_SIZE:
                    break
                blocks.append(bytes(self._buf[:SURVEY_BLOCK_SIZE]))
                del self._buf[:SURVEY_BLOCK_SIZE]
                continue

            del self._buf[0]

        return controls, blocks, got_ack, got_end


def _is_disconnected(
    client: BleakClient, disconnected: asyncio.Event | None
) -> bool:
    """Return True if the disconnect event fired or the client reports disconnected."""
    return (disconnected is not None and disconnected.is_set()) or not getattr(
        client, "is_connected", True
    )


def _check_connected(
    client: BleakClient, disconnected: asyncio.Event | None
) -> None:
    """Raise PowerNIDisconnectedError if the BLE link dropped."""
    if _is_disconnected(client, disconnected):
        raise PowerNIDisconnectedError("BLE connection dropped during meter read")


async def _wait_queue_or_disconnect[T](
    queue: asyncio.Queue[T],
    timeout: float,
    disconnected: asyncio.Event | None,
) -> T:
    """Wait for the next item from `queue`, aborting immediately on disconnect."""
    if disconnected is None:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    if disconnected.is_set():
        raise PowerNIDisconnectedError("BLE connection dropped during meter read")

    get_task: asyncio.Task[T] = asyncio.create_task(queue.get())
    disc_task: asyncio.Task[bool] = asyncio.create_task(disconnected.wait())
    try:
        done, pending = await asyncio.wait(
            (get_task, disc_task),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if get_task in done:
            return get_task.result()
        if disc_task in done:
            raise PowerNIDisconnectedError("BLE connection dropped during meter read")
        raise TimeoutError
    except BaseException:
        for task in (get_task, disc_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(get_task, disc_task, return_exceptions=True)
        raise


async def _wait_control_frame(
    ctrl_queue: asyncio.Queue[bytes],
    expected: bytes,
    timeout: float,
    disconnected: asyncio.Event | None = None,
) -> bool:
    """Wait until `expected` 4-byte control frame arrives or timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        try:
            frame = await _wait_queue_or_disconnect(ctrl_queue, remaining, disconnected)
        except TimeoutError:
            return False
        if frame == expected:
            return True


async def _wait_matching_line(
    line_queue: asyncio.Queue[str],
    pattern: re.Pattern[str] | None,
    timeout: float,
    *,
    ignore_reset: bool = True,
    disconnected: asyncio.Event | None = None,
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
            line = await _wait_queue_or_disconnect(line_queue, remaining, disconnected)
        except TimeoutError as err:
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


async def _enter_transparent_mode(
    client: BleakClient,
    ctrl_queue: asyncio.Queue[bytes],
    raw_notifies: list[str],
    logger: logging.Logger,
    disconnected: asyncio.Event | None,
) -> bool:
    """Enter transparent mode and return whether writes require response=True."""
    logger.debug("Sending ENTER_TRANSPARENT_MODE")
    _check_connected(client, disconnected)
    await client.write_gatt_char(WRITE_UUID, CMD_ENTER_TRANSPARENT, response=False)
    got_ack = await _wait_control_frame(
        ctrl_queue, RSP_TRANSPARENT_ACK, TRANSPARENT_ACK_TIMEOUT, disconnected
    )
    if got_ack:
        logger.info("Entered transparent mode on Power NI meter")
        return False

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
            _check_connected(client, disconnected)
            await client.write_gatt_char(
                WRITE_UUID, CMD_ENTER_TRANSPARENT, response=True
            )
            logger.warning(
                "Write-with-response succeeded for ENTER_TRANSPARENT_MODE; waiting 2.5s for 55 cc 01 33"
            )
            got_ack = await _wait_control_frame(
                ctrl_queue, RSP_TRANSPARENT_ACK, 2.5, disconnected
            )
            if got_ack:
                logger.info("Entered transparent mode on Power NI meter")
                return True
            logger.warning(
                "Write-with-response ACK still timed out; raw_notifies(%d)=%s",
                len(raw_notifies),
                raw_notifies,
            )
        except PowerNIDisconnectedError:
            raise
        except Exception as err:
            logger.warning(
                "Write-with-response error: %s: %s", type(err).__name__, err
            )
    else:
        logger.warning(
            "Skipping write-with-response fallback; write_char=%s",
            _format_char(write_char),
        )

    raise PowerNIProtocolError(
        "Timed out waiting for transparent mode acknowledgement (55 cc 01 33)"
    )


async def _leave_transparent_mode(
    client: BleakClient,
    ctrl_queue: asyncio.Queue[bytes],
    notify_started: bool,
    logger: logging.Logger,
    disconnected: asyncio.Event | None,
) -> None:
    """Exit transparent mode and stop GATT notifications if still connected."""
    if _is_disconnected(client, disconnected):
        return
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


async def async_read_meter(
    client: BleakClient,
    logger: logging.Logger = _LOGGER,
    sections: frozenset[str] = ALL_SECTIONS,
    disconnected: asyncio.Event | None = None,
    on_progress: Callable[[PowerNIMeterData], None] | None = None,
) -> PowerNIMeterData:
    """Run one transparent-mode telemetry poll against a connected Freedom Unit.

    `sections` selects which groups of readings to fetch (balance is always
    read). Fields for skipped sections are left as None. When `on_progress` is
    provided, intermediate section deltas are emitted as each section finishes.
    """
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
        _check_connected(client, disconnected)
        logger.debug("BLE write %s (response=%s)", cmd.hex(" "), write_with_response)
        await client.write_gatt_char(WRITE_UUID, cmd, response=write_with_response)

    async def _display_reset() -> None:
        await _send(CMD_DISPLAY_RESET)
        try:
            await _wait_matching_line(
                line_queue,
                RE_RESET_ACK,
                RESET_ACK_TIMEOUT,
                ignore_reset=False,
                disconnected=disconnected,
            )
        except TimeoutError:
            logger.debug("Display reset ACK timed out; continuing")

    logger.info("Starting Power NI BLE poll")
    _check_connected(client, disconnected)
    await client.start_notify(NOTIFY_UUID, _on_notify)
    notify_started = True

    data = PowerNIMeterData()

    def _emit_progress(delta: PowerNIMeterData) -> None:
        if on_progress is not None and delta.has_any_value():
            on_progress(delta)

    try:
        write_with_response = await _enter_transparent_mode(
            client, ctrl_queue, raw_notifies, logger, disconnected
        )

        # 1. Balance and days remaining (meter may alternate in either order)
        await _display_reset()
        for _ in range(3):
            if data.balance is not None and data.days_left_status is not None:
                break
            await _send(CMD_GET_BALANCE)
            try:
                matched_line, _ = await _wait_matching_line(
                    line_queue,
                    RE_BALANCE_OR_DAYS_LEFT,
                    LINE_TIMEOUT,
                    disconnected=disconnected,
                )
            except TimeoutError as err:
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
        _emit_progress(
            PowerNIMeterData(
                balance=data.balance,
                days_left=data.days_left,
                days_left_status=data.days_left_status,
            )
        )

        # 2. Live kW load and current £/hr rate
        if SECTION_LIVE in sections:
            await _display_reset()
            try:
                await _send(CMD_TOTAL_CONSUMPTION)
                _, match = await _wait_matching_line(
                    line_queue, RE_LOAD_KW, LINE_TIMEOUT, disconnected=disconnected
                )
                assert match is not None
                data.current_load_kw = float(match.group(1))
            except TimeoutError as err:
                logger.warning(
                    "Timed out reading current kW load (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )

            try:
                await _send(CMD_TOTAL_CONSUMPTION)
                _, match = await _wait_matching_line(
                    line_queue, RE_LOAD_COST, LINE_TIMEOUT, disconnected=disconnected
                )
                assert match is not None
                data.current_cost_per_hour = float(match.group(1))
            except TimeoutError as err:
                logger.warning(
                    "Timed out reading current hourly cost (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )
            _emit_progress(
                PowerNIMeterData(
                    current_load_kw=data.current_load_kw,
                    current_cost_per_hour=data.current_cost_per_hour,
                )
            )

        # 3. Keepalive, then cumulative import units (kWh)
        if (
            SECTION_UNITS in sections
            or SECTION_RATE in sections
            or SECTION_SPEND in sections
        ):
            await _send(CMD_KEEPALIVE)
            if not await _wait_control_frame(
                ctrl_queue, RSP_KEEPALIVE_ACK, KEEPALIVE_ACK_TIMEOUT, disconnected
            ):
                logger.debug("Keepalive ACK timed out; continuing")
        if SECTION_UNITS in sections:

            await _display_reset()
            try:
                await _send(CMD_TOTAL_UNITS)
                _, match = await _wait_matching_line(
                    line_queue, RE_IMP_UNITS, LINE_TIMEOUT, disconnected=disconnected
                )
                assert match is not None
                data.total_import_kwh = int(match.group(1))
            except TimeoutError as err:
                logger.warning(
                    "Timed out reading total import kWh units (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )
            _emit_progress(PowerNIMeterData(total_import_kwh=data.total_import_kwh))

        # 4. Active unit rate (R1 PRICE in pence -> GBP/kWh)
        if SECTION_RATE in sections:
            await _display_reset()
            try:
                await _send(CMD_UNIT_RATE)
                _, match = await _wait_matching_line(
                    line_queue,
                    RE_UNIT_RATE_PENCE,
                    LINE_TIMEOUT,
                    disconnected=disconnected,
                )
                assert match is not None
                data.unit_rate = round(float(match.group(1)) / 100.0, 5)
            except TimeoutError as err:
                logger.warning(
                    "Timed out reading unit rate (seen_lines=%r)",
                    getattr(err, "seen_lines", []),
                )
            _emit_progress(PowerNIMeterData(unit_rate=data.unit_rate))

        # 5. Recent spend (yesterday / last 7 days / last month)
        if SECTION_SPEND in sections:
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
                    line, _ = await _wait_matching_line(
                        line_queue, None, LINE_TIMEOUT, disconnected=disconnected
                    )
                except TimeoutError as err:
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
            "load_kw=%s cost_per_hr=%s imp_kwh=%s unit_rate=%s yesterday=%s last_7d=%s last_month=%s",
            data.balance,
            data.days_left,
            data.days_left_status,
            data.current_load_kw,
            data.current_cost_per_hour,
            data.total_import_kwh,
            data.unit_rate,
            data.yesterday_cost,
            data.last_7_days_cost,
            data.last_month_cost,
        )
        return data
    finally:
        await _leave_transparent_mode(
            client, ctrl_queue, notify_started, logger, disconnected
        )


async def async_read_survey(
    client: BleakClient,
    days: int = SURVEY_MAX_DAYS,
    logger: logging.Logger = _LOGGER,
    disconnected: asyncio.Event | None = None,
) -> list[bytes]:
    """Read up to `days` of 13-block 30-minute energy survey history (0xA0)."""
    _log_gatt_services(client, logger)

    assembler = SurveyFrameAssembler()
    ctrl_queue: asyncio.Queue[bytes] = asyncio.Queue()
    event_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    ack_event = asyncio.Event()
    raw_notifies: list[str] = []
    recent_notifies: deque[str] = deque(maxlen=12)
    notify_started = False

    def _on_notify(_sender: Any, payload: bytearray) -> None:
        raw = bytes(payload)
        raw_hex = raw.hex(" ")
        if len(raw_notifies) < 20:
            raw_notifies.append(raw_hex)
        recent_notifies.append(raw_hex)
        logger.debug("Survey BLE notify %dB: %s", len(raw), raw_hex)
        controls, blocks, got_ack, got_end = assembler.feed(raw)
        for ctrl in controls:
            ctrl_queue.put_nowait(ctrl)
        if got_ack:
            ack_event.set()
        for block in blocks:
            event_queue.put_nowait(block)
        if got_end:
            event_queue.put_nowait(None)

    logger.info("Starting Power NI BLE survey read (up to %d days)", days)
    _check_connected(client, disconnected)
    await client.start_notify(NOTIFY_UUID, _on_notify)
    notify_started = True

    try:
        write_with_response = await _enter_transparent_mode(
            client, ctrl_queue, raw_notifies, logger, disconnected
        )

        async def _write(cmd: bytes, delay: float) -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            _check_connected(client, disconnected)
            await client.write_gatt_char(WRITE_UUID, cmd, response=write_with_response)

        await _write(CMD_READ_SURVEY, COMMAND_PACING_DELAY)

        # Wait for 0xFC start acknowledgement or abort on disconnect
        if disconnected is None:
            try:
                await asyncio.wait_for(ack_event.wait(), timeout=SURVEY_ACK_TIMEOUT)
            except TimeoutError as err:
                raise PowerNIProtocolError(
                    f"Timed out waiting for survey acknowledgement (0xFC); raw_notifies={raw_notifies}"
                ) from err
        else:
            ack_task = asyncio.create_task(ack_event.wait())
            disc_task = asyncio.create_task(disconnected.wait())
            try:
                done, pending = await asyncio.wait(
                    (ack_task, disc_task),
                    timeout=SURVEY_ACK_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if disc_task in done:
                    raise PowerNIDisconnectedError(
                        "BLE connection dropped waiting for survey ACK"
                    )
                if ack_task not in done:
                    raise PowerNIProtocolError(
                        f"Timed out waiting for survey acknowledgement (0xFC); raw_notifies={raw_notifies}"
                    )
            except BaseException:
                for task in (ack_task, disc_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(ack_task, disc_task, return_exceptions=True)
                raise

        target_blocks = max(1, min(days, SURVEY_MAX_DAYS)) * SURVEY_BLOCKS_PER_DAY
        blocks: list[bytes] = []
        keepalive_timeouts = 0
        just_kept_alive = False

        def _survey_timeout_error() -> PowerNIProtocolError:
            return PowerNIProtocolError(
                f"Timed out reading survey block #{len(blocks) + 1} "
                f"(keepalive_timeouts={keepalive_timeouts}, "
                f"pending={assembler.pending_hex!r}, "
                f"recent_notifies={list(recent_notifies)})"
            )

        while len(blocks) < target_blocks:
            if blocks and len(blocks) % SURVEY_KEEPALIVE_EVERY_BLOCKS == 0:
                await _write(CMD_KEEPALIVE, COMMAND_PACING_DELAY)
                if not await _wait_control_frame(
                    ctrl_queue, RSP_KEEPALIVE_ACK, KEEPALIVE_ACK_TIMEOUT, disconnected
                ):
                    keepalive_timeouts += 1
                    logger.warning(
                        "Survey keepalive ACK timed out after %d blocks; continuing",
                        len(blocks),
                    )
                just_kept_alive = True

            delay = COMMAND_PACING_DELAY if just_kept_alive else SURVEY_PACING_DELAY
            just_kept_alive = False
            await _write(CMD_SURVEY_NEXT, delay)
            try:
                item = await _wait_queue_or_disconnect(
                    event_queue, LINE_TIMEOUT, disconnected
                )
            except TimeoutError as err:
                if assembler.pending_bytes != 0:
                    raise _survey_timeout_error() from err
                logger.info(
                    "Survey block #%d timed out with empty buffer; retrying 0xF1",
                    len(blocks) + 1,
                )
                await _write(CMD_SURVEY_NEXT, COMMAND_PACING_DELAY)
                try:
                    item = await _wait_queue_or_disconnect(
                        event_queue, LINE_TIMEOUT, disconnected
                    )
                except TimeoutError as retry_err:
                    raise _survey_timeout_error() from retry_err

            if item is None:
                logger.debug("Survey end marker (0xFF) after %d blocks", len(blocks))
                break
            blocks.append(item)

        logger.info(
            "Power NI survey read %d block(s) (%d complete day(s))",
            len(blocks),
            len(blocks) // SURVEY_BLOCKS_PER_DAY,
        )
        return blocks
    finally:
        await _leave_transparent_mode(
            client, ctrl_queue, notify_started, logger, disconnected
        )
