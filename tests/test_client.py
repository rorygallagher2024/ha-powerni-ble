"""Unit tests for the Power NI BLE frame assembler and protocol client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components" / "powerni_ble"

# Load client/const without executing __init__.py (which requires Home Assistant).
_cc = types.ModuleType("custom_components")
_cc.__path__ = [str(ROOT / "custom_components")]
_pkg = types.ModuleType("custom_components.powerni_ble")
_pkg.__path__ = [str(PKG_ROOT)]
sys.modules.setdefault("custom_components", _cc)
sys.modules.setdefault("custom_components.powerni_ble", _pkg)

from custom_components.powerni_ble import client as client_mod  # noqa: E402
from custom_components.powerni_ble.client import (  # noqa: E402
    FrameAssembler,
    PowerNIDisconnectedError,
    PowerNIMeterData,
    PowerNIProtocolError,
    SurveyFrameAssembler,
    async_read_meter,
    async_read_survey,
    merge_meter_data,
)
from custom_components.powerni_ble.survey import (  # noqa: E402
    aggregate_hourly,
    counter_delta_kwh,
    decode_survey_date,
    parse_survey_blocks,
)
from custom_components.powerni_ble.tracking import (  # noqa: E402
    detect_top_up,
    is_stale,
    record_read_times,
    stale_after,
)
from custom_components.powerni_ble.const import (  # noqa: E402
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
    DEFAULT_SCAN_INTERVAL_MINUTES,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    NOTIFY_UUID,
    RSP_DISCONNECT_ACK,
    RSP_KEEPALIVE_ACK,
    RSP_SURVEY_ACK,
    RSP_SURVEY_BLOCK_HEADER,
    RSP_SURVEY_END,
    RSP_TRANSPARENT_ACK,
    SECTION_BALANCE,
    SECTION_LIVE,
    SECTION_SPEND,
    SECTION_UNITS,
    WRITE_UUID,
    clamp_scan_interval,
    retry_interval_minutes,
    sections_due,
)


def encode_lcd(text: str) -> bytes:
    """Encode a plain LCD line into the meter's reversed 10/20-byte frame format."""
    width = 9 if len(text) <= 9 else 19
    padded = text.ljust(width)[:width]
    return padded[::-1].encode("ascii") + b"\r"


def encode_lcd_pair(first: str, second: str) -> bytes:
    """Encode two 9-char LCD lines into one 20-byte multi-line frame."""
    return encode_lcd(second) + encode_lcd(first)


class FakeBleakClient:
    """Minimal async BleakClient mock reproducing Freedom Unit notify replies."""

    def __init__(self, script: dict[bytes, list[list[bytes]]]) -> None:
        self._script: dict[bytes, list[list[bytes]]] = {
            k: list(v) for k, v in script.items()
        }
        self._callback = None
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_started = False
        self.notify_stopped = False
        self.services = []

    async def start_notify(self, uuid: str, callback) -> None:
        assert uuid == NOTIFY_UUID
        self._callback = callback
        self.notify_started = True

    async def stop_notify(self, uuid: str) -> None:
        assert uuid == NOTIFY_UUID
        self.notify_stopped = True

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = False
    ) -> None:
        assert uuid == WRITE_UUID
        self.writes.append((uuid, bytes(data), response))
        replies = self._script.get(bytes(data), [])
        if replies and self._callback is not None:
            chunks = replies.pop(0)
            for chunk in chunks:
                self._callback(NOTIFY_UUID, bytearray(chunk))


class TestFrameAssembler(unittest.TestCase):
    """Tests for FrameAssembler framing, chunking, control ACKs, and resync."""

    def test_single_10_byte_and_20_byte_frames(self) -> None:
        asm = FrameAssembler()
        c1, l1 = asm.feed(encode_lcd("01 #14.25"))
        self.assertEqual(c1, [])
        self.assertEqual(l1, ["01 #14.25"])

        c2, l2 = asm.feed(encode_lcd("12345 IMP UNITS"))
        self.assertEqual(c2, [])
        self.assertEqual([line.strip() for line in l2], ["12345 IMP UNITS"])

    def test_split_chunks_and_multiline_20_byte_frames(self) -> None:
        asm = FrameAssembler()
        frame_a = encode_lcd("01 #14.25")
        self.assertEqual(len(frame_a), 10)

        # Split first frame 4B + 6B
        self.assertEqual(asm.feed(frame_a[:4]), ([], []))
        self.assertEqual(asm.feed(frame_a[4:]), ([], ["01 #14.25"]))

        # Feed a 20-byte two-line frame with embedded \r in one chunk
        multiline = encode_lcd_pair("12345", "IMP UNITS")
        self.assertEqual(len(multiline), 20)
        c, lines = asm.feed(multiline)
        self.assertEqual(c, [])
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"12345\s+IMP UNITS")

        # Also check 20-byte Balance and Days Left multi-line frames
        c_bal, l_bal = asm.feed(encode_lcd_pair("12", "#45.67"))
        self.assertEqual(c_bal, [])
        self.assertRegex(l_bal[0], r"12\s+#45\.67")

        c_dl, l_dl = asm.feed(encode_lcd_pair("ABOUT", "12"))
        self.assertEqual(c_dl, [])
        self.assertRegex(l_dl[0], r"ABOUT\s+12")

    def test_interleaved_and_split_control_acks(self) -> None:
        asm = FrameAssembler()
        frame = encode_lcd("KW  0.452")

        # Split 4-byte control ACK (3B + 1B)
        self.assertEqual(asm.feed(RSP_TRANSPARENT_ACK[:3]), ([], []))
        c1, l1 = asm.feed(RSP_TRANSPARENT_ACK[3:])
        self.assertEqual(c1, [RSP_TRANSPARENT_ACK])
        self.assertEqual(l1, [])

        # Control ACK before and after LCD frame in one notify payload
        c2, l2 = asm.feed(RSP_KEEPALIVE_ACK + frame + RSP_DISCONNECT_ACK)
        self.assertEqual(c2, [RSP_KEEPALIVE_ACK, RSP_DISCONNECT_ACK])
        self.assertEqual(l2, ["KW  0.452"])

    def test_resync_drops_misaligned_prefix(self) -> None:
        asm = FrameAssembler()
        valid = encode_lcd("01 #14.25")
        # 5-byte corrupt prefix ending in CR, then valid 10-byte frame (total 15B)
        self.assertEqual(asm.feed(b"JUNK\r"), ([], []))
        c, lines = asm.feed(valid)
        self.assertEqual(c, [])
        self.assertEqual(lines, ["01 #14.25"])


def full_script() -> dict[bytes, list[list[bytes]]]:
    """Meter replies for a complete poll of every section."""
    return {
        CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
        CMD_GET_BALANCE: [
            [encode_lcd("ACCOUNT"), encode_lcd("01 #14.25")],
            [encode_lcd("ABOUT 12")],
        ],
        CMD_DISPLAY_RESET: [[encode_lcd("%%%%%%%%%")] for _ in range(4)],
        CMD_TOTAL_CONSUMPTION: [
            [encode_lcd("KW  LOAD"), encode_lcd("KW  0.452")],
            [encode_lcd("LOAD COST"), encode_lcd(" #0.14/HR")],
        ],
        CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
        CMD_TOTAL_UNITS: [[encode_lcd("12345 IMP UNITS")]],
        CMD_PREV_USAGE: [
            [encode_lcd("PREV DAY")],
            [encode_lcd("   #3.20")],
            [encode_lcd("PREV WEEK")],
            [encode_lcd("  #21.45")],
            [encode_lcd("PREV MNTH")],
            [encode_lcd("  #89.10")],
        ],
        CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
    }


class TestAsyncReadMeter(unittest.IsolatedAsyncioTestCase):
    """End-to-end protocol tests against FakeBleakClient."""

    async def _read(self, sections=ALL_SECTIONS):
        client = FakeBleakClient(full_script())
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client, sections=sections)  # type: ignore[arg-type]
        return client, data

    async def test_balance_and_live_only_skips_slow_sections(self) -> None:
        client, data = await self._read(frozenset({SECTION_BALANCE, SECTION_LIVE}))
        sent = {w[1] for w in client.writes}
        self.assertEqual(data.balance, 14.25)
        self.assertEqual(data.current_load_kw, 0.452)
        self.assertIsNone(data.total_import_kwh)
        self.assertIsNone(data.yesterday_cost)
        for cmd in (CMD_KEEPALIVE, CMD_TOTAL_UNITS, CMD_PREV_USAGE):
            self.assertNotIn(cmd, sent)
        self.assertEqual(client.writes[-1][1], CMD_LEAVE_TRANSPARENT)

    async def test_balance_only(self) -> None:
        client, data = await self._read(frozenset({SECTION_BALANCE}))
        sent = {w[1] for w in client.writes}
        self.assertEqual(data.balance, 14.25)
        self.assertEqual(data.days_left, 12)
        self.assertIsNone(data.current_load_kw)
        self.assertNotIn(CMD_TOTAL_CONSUMPTION, sent)

    async def test_spend_without_units_still_sends_keepalive(self) -> None:
        client, data = await self._read(
            frozenset({SECTION_BALANCE, SECTION_LIVE, SECTION_SPEND})
        )
        sent = {w[1] for w in client.writes}
        self.assertIn(CMD_KEEPALIVE, sent)
        self.assertNotIn(CMD_TOTAL_UNITS, sent)
        self.assertIsNone(data.total_import_kwh)
        self.assertEqual(data.last_month_cost, 89.10)

    async def test_full_meter_poll(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd("ACCOUNT"), encode_lcd("01 #14.25")],
                [encode_lcd("ABOUT 12")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW  LOAD"), encode_lcd("KW  0.452")],
                [encode_lcd("LOAD COST"), encode_lcd(" #0.14/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd("12345 IMP UNITS")]],
            CMD_PREV_USAGE: [
                [encode_lcd("PREV DAY")],
                [encode_lcd("   #3.20")],
                [encode_lcd("PREV WEEK")],
                [encode_lcd("  #21.45")],
                [encode_lcd("PREV MNTH")],
                [encode_lcd("  #89.10")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client)  # type: ignore[arg-type]

        self.assertEqual(data.balance, 14.25)
        self.assertEqual(data.days_left, 12)
        self.assertEqual(data.days_left_status, "ABOUT 12")
        self.assertEqual(data.current_load_kw, 0.452)
        self.assertEqual(data.current_cost_per_hour, 0.14)
        self.assertEqual(data.total_import_kwh, 12345)
        self.assertEqual(data.yesterday_cost, 3.20)
        self.assertEqual(data.last_7_days_cost, 21.45)
        self.assertEqual(data.last_month_cost, 89.10)
        self.assertTrue(client.notify_started)
        self.assertTrue(client.notify_stopped)
        self.assertEqual(client.writes[-1][1], CMD_LEAVE_TRANSPARENT)

    async def test_negative_values_no_data_and_month_stop(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd("02 -#5.20")],
                [encode_lcd("NO DATA")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW -0.250")],
                [encode_lcd("0.00/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd("987 IMP UNITS")]],
            CMD_PREV_USAGE: [
                [encode_lcd("PREV DAY")],
                [encode_lcd("  -#1.10")],
                [encode_lcd("JAN #50.00")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client)  # type: ignore[arg-type]

        self.assertEqual(data.balance, -5.20)
        self.assertIsNone(data.days_left)
        self.assertEqual(data.days_left_status, "NO DATA")
        self.assertEqual(data.current_load_kw, -0.25)
        self.assertEqual(data.current_cost_per_hour, 0.0)
        self.assertEqual(data.total_import_kwh, 987)
        self.assertEqual(data.yesterday_cost, -1.10)
        self.assertIsNone(data.last_7_days_cost)
        self.assertIsNone(data.last_month_cost)

    async def test_multiline_20_byte_and_split_notification_frames(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd_pair("12", "#45.67")],
                [encode_lcd_pair("ABOUT", "12")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd_pair("KW  LOAD", "KW  0.51")],
                [encode_lcd_pair("LOAD COST", " #0.16/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd("IMP UNITS"), encode_lcd("12345")]],
            CMD_PREV_USAGE: [
                [encode_lcd_pair("PREV DAY", "  #2.77")],
                [encode_lcd("PREV WEEK")],
                [encode_lcd("  #20.15")],
                [encode_lcd("PREV MNTH")],
                [encode_lcd("  #65.05")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client)  # type: ignore[arg-type]

        self.assertEqual(data.balance, 45.67)
        self.assertEqual(data.days_left, 12)
        self.assertEqual(data.days_left_status, "ABOUT 12")
        self.assertEqual(data.current_load_kw, 0.51)
        self.assertEqual(data.current_cost_per_hour, 0.16)
        self.assertEqual(data.total_import_kwh, 12345)
        self.assertEqual(data.yesterday_cost, 2.77)
        self.assertEqual(data.last_7_days_cost, 20.15)
        self.assertEqual(data.last_month_cost, 65.05)

    async def test_split_balance_across_two_notifications(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd(" #45.67"), encode_lcd("01")],
                [encode_lcd("12"), encode_lcd("ABOUT")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW  0.51")],
                [encode_lcd(" #0.16/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd_pair("12345", "IMP UNITS")]],
            CMD_PREV_USAGE: [
                [encode_lcd_pair("PREV DAY", "  #2.77")],
                [encode_lcd_pair("PREV WEEK", "  #20.15")],
                [encode_lcd_pair("PREV MNTH", "  #65.05")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client)  # type: ignore[arg-type]

        self.assertEqual(data.balance, 45.67)
        self.assertEqual(data.days_left, 12)
        self.assertEqual(data.days_left_status, "ABOUT 12")
        self.assertEqual(data.total_import_kwh, 12345)
        self.assertEqual(data.yesterday_cost, 2.77)
        self.assertEqual(data.last_7_days_cost, 20.15)
        self.assertEqual(data.last_month_cost, 65.05)

    async def test_balance_and_days_left_swapped_order(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd("DAYS LEFT"), encode_lcd("ABOUT  51")],
                [encode_lcd(" ACCOUNT "), encode_lcd("1 #148.81")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW   0.40")],
                [encode_lcd(" #0.13/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd_pair("11407", "IMP UNITS")]],
            CMD_PREV_USAGE: [
                [encode_lcd("PREV DAY"), encode_lcd("   #2.77")],
                [encode_lcd("PREV WEEK"), encode_lcd("  #20.15")],
                [encode_lcd("PREV MNTH"), encode_lcd("  #65.05")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            data = await async_read_meter(client)  # type: ignore[arg-type]

        self.assertEqual(data.balance, 148.81)
        self.assertEqual(data.days_left, 51)
        self.assertEqual(data.days_left_status, "ABOUT 51")
        self.assertEqual(data.total_import_kwh, 11407)

    async def test_transparent_timeout_cleans_up(self) -> None:
        client = FakeBleakClient({})
        with (
            patch.object(client_mod, "TRANSPARENT_ACK_TIMEOUT", 0.05),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            with self.assertRaises(PowerNIProtocolError):
                await async_read_meter(client)  # type: ignore[arg-type]
        self.assertTrue(client.notify_started)
        self.assertTrue(client.notify_stopped)
        self.assertEqual(client.writes[-1][1], CMD_LEAVE_TRANSPARENT)


class TestClampScanInterval(unittest.TestCase):
    """Tests for poll interval validation and clamping."""

    def test_valid_and_rounded_values(self) -> None:
        self.assertEqual(clamp_scan_interval(15), 15)
        self.assertEqual(clamp_scan_interval("5"), 5)
        self.assertEqual(clamp_scan_interval(2.6), 3)
        self.assertEqual(clamp_scan_interval(2.4), 2)

    def test_bounds_clamping(self) -> None:
        self.assertEqual(clamp_scan_interval(0), MIN_SCAN_INTERVAL_MINUTES)
        self.assertEqual(clamp_scan_interval(-5), MIN_SCAN_INTERVAL_MINUTES)
        self.assertEqual(clamp_scan_interval(9999), MAX_SCAN_INTERVAL_MINUTES)

    def test_invalid_falls_back_to_default(self) -> None:
        self.assertEqual(clamp_scan_interval(None), DEFAULT_SCAN_INTERVAL_MINUTES)
        self.assertEqual(clamp_scan_interval("not-a-number"), DEFAULT_SCAN_INTERVAL_MINUTES)


class TestTracking(unittest.TestCase):
    """Tests for reading freshness and top-up detection."""

    NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

    def test_stale_after_never_below_24h_or_three_polls(self) -> None:
        self.assertEqual(stale_after(15), timedelta(hours=24))
        self.assertEqual(stale_after(1440), timedelta(hours=72))

    def test_is_stale(self) -> None:
        limit = timedelta(hours=24)
        self.assertTrue(is_stale(None, self.NOW, limit))
        self.assertFalse(is_stale(self.NOW - timedelta(hours=23), self.NOW, limit))
        self.assertTrue(is_stale(self.NOW - timedelta(hours=25), self.NOW, limit))

    def test_record_read_times_only_stamps_fresh_fields(self) -> None:
        earlier = self.NOW - timedelta(hours=6)
        read_times = {"last_month_cost": earlier}
        record_read_times(
            read_times,
            PowerNIMeterData(balance=12.5, current_load_kw=0.4),
            self.NOW,
        )
        self.assertEqual(read_times["balance"], self.NOW)
        self.assertEqual(read_times["current_load_kw"], self.NOW)
        self.assertEqual(read_times["last_month_cost"], earlier)
        self.assertNotIn("total_import_kwh", read_times)

    def test_record_read_times_counts_no_data_as_fresh(self) -> None:
        read_times: dict[str, datetime] = {}
        record_read_times(
            read_times, PowerNIMeterData(days_left_status="NO DATA"), self.NOW
        )
        self.assertEqual(read_times["days_left"], self.NOW)

    def test_detect_top_up(self) -> None:
        self.assertEqual(detect_top_up(148.36, 168.30), 19.94)
        self.assertEqual(detect_top_up(-5.20, 14.80), 20.0)
        self.assertIsNone(detect_top_up(148.41, 148.36))
        self.assertIsNone(detect_top_up(148.36, 148.90))
        self.assertIsNone(detect_top_up(None, 150.0))
        self.assertIsNone(detect_top_up(150.0, None))


class TestMergeMeterData(unittest.TestCase):
    """Partial reads keep last-known values instead of going unknown."""

    def test_no_previous_returns_new(self) -> None:
        new = PowerNIMeterData(balance=1.0)
        merged, carried = merge_meter_data(new, None)
        self.assertIs(merged, new)
        self.assertEqual(carried, [])

    def test_missing_fields_filled_from_previous(self) -> None:
        previous = PowerNIMeterData(
            balance=148.81, days_left=51, current_load_kw=0.4, total_import_kwh=11407
        )
        new = PowerNIMeterData(balance=148.79, total_import_kwh=11408)
        merged, carried = merge_meter_data(new, previous)
        self.assertEqual(merged.balance, 148.79)
        self.assertEqual(merged.total_import_kwh, 11408)
        self.assertEqual(merged.days_left, 51)
        self.assertEqual(merged.current_load_kw, 0.4)
        self.assertCountEqual(carried, ["days_left", "current_load_kw"])
        # The fresh read object itself is not mutated.
        self.assertIsNone(new.days_left)

    def test_zero_and_negative_values_are_not_overwritten(self) -> None:
        previous = PowerNIMeterData(balance=10.0, current_load_kw=1.2)
        new = PowerNIMeterData(balance=-2.5, current_load_kw=0.0)
        merged, carried = merge_meter_data(new, previous)
        self.assertEqual(merged.balance, -2.5)
        self.assertEqual(merged.current_load_kw, 0.0)
        self.assertEqual(carried, [])


class TestRetryInterval(unittest.TestCase):
    """Failed polls retry sooner, then return to the configured interval."""

    def test_backoff_steps(self) -> None:
        self.assertEqual(retry_interval_minutes(0, 15), 15)
        self.assertEqual(retry_interval_minutes(1, 15), 1)
        self.assertEqual(retry_interval_minutes(2, 15), 2)
        self.assertEqual(retry_interval_minutes(3, 15), 5)
        self.assertEqual(retry_interval_minutes(10, 15), 5)

    def test_never_slower_than_configured_interval(self) -> None:
        self.assertEqual(retry_interval_minutes(3, 2), 2)
        self.assertEqual(retry_interval_minutes(5, 1), 1)


class TestSectionsDue(unittest.TestCase):
    """Slow-changing readings are only fetched when due."""

    NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)

    def test_first_poll_reads_everything(self) -> None:
        self.assertEqual(sections_due(self.NOW, {}), ALL_SECTIONS)

    def test_force_full(self) -> None:
        last = {SECTION_UNITS: self.NOW, SECTION_SPEND: self.NOW}
        self.assertEqual(sections_due(self.NOW, last, force_full=True), ALL_SECTIONS)

    def test_recent_reads_skip_slow_sections(self) -> None:
        last = {
            SECTION_UNITS: self.NOW - timedelta(minutes=30),
            SECTION_SPEND: self.NOW - timedelta(hours=2),
        }
        self.assertEqual(
            sections_due(self.NOW, last), frozenset({SECTION_BALANCE, SECTION_LIVE})
        )

    def test_units_hourly_spend_six_hourly(self) -> None:
        last = {
            SECTION_UNITS: self.NOW - timedelta(minutes=61),
            SECTION_SPEND: self.NOW - timedelta(hours=5),
        }
        self.assertEqual(
            sections_due(self.NOW, last),
            frozenset({SECTION_BALANCE, SECTION_LIVE, SECTION_UNITS}),
        )
        last[SECTION_SPEND] = self.NOW - timedelta(hours=6)
        self.assertIn(SECTION_SPEND, sections_due(self.NOW, last))

    def test_spend_due_after_midnight(self) -> None:
        now = datetime(2026, 11, 23, 0, 5, tzinfo=timezone.utc)
        last = {
            SECTION_UNITS: now - timedelta(minutes=5),
            SECTION_SPEND: now - timedelta(minutes=20),
        }
        self.assertEqual(
            sections_due(now, last),
            frozenset({SECTION_BALANCE, SECTION_LIVE, SECTION_SPEND}),
        )


class TestDisconnectDetection(unittest.IsolatedAsyncioTestCase):
    """Tests that mid-poll BLE disconnects abort immediately instead of timing out."""

    async def test_disconnect_during_transparent_ack_aborts_immediately(self) -> None:
        disconnected = asyncio.Event()

        class DropOnTransparentClient(FakeBleakClient):
            is_connected = True

            async def write_gatt_char(
                self, uuid: str, data: bytes, response: bool = False
            ) -> None:
                await super().write_gatt_char(uuid, data, response)
                if data == CMD_ENTER_TRANSPARENT:
                    self.is_connected = False
                    disconnected.set()

        client = DropOnTransparentClient({})
        started = time.monotonic()
        with self.assertRaises(PowerNIDisconnectedError):
            await async_read_meter(
                client,  # type: ignore[arg-type]
                disconnected=disconnected,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual([w[1] for w in client.writes], [CMD_ENTER_TRANSPARENT])
        self.assertFalse(client.notify_stopped)

    async def test_disconnect_during_balance_aborts_without_more_commands(self) -> None:
        disconnected = asyncio.Event()

        class DropOnBalanceClient(FakeBleakClient):
            is_connected = True

            async def write_gatt_char(
                self, uuid: str, data: bytes, response: bool = False
            ) -> None:
                await super().write_gatt_char(uuid, data, response)
                if data == CMD_GET_BALANCE:
                    self.is_connected = False
                    disconnected.set()

        client = DropOnBalanceClient(
            {
                CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
                CMD_DISPLAY_RESET: [[encode_lcd("%%%%%%%%%")]],
            }
        )
        started = time.monotonic()
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            self.assertRaises(PowerNIDisconnectedError),
        ):
            await async_read_meter(
                client,  # type: ignore[arg-type]
                disconnected=disconnected,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(
            [w[1] for w in client.writes],
            [CMD_ENTER_TRANSPARENT, CMD_DISPLAY_RESET, CMD_GET_BALANCE],
        )
        self.assertFalse(client.notify_stopped)

    async def test_already_disconnected_client_fails_before_writes(self) -> None:
        class DeadClient(FakeBleakClient):
            is_connected = False

        client = DeadClient({})
        with self.assertRaises(PowerNIDisconnectedError):
            await async_read_meter(client)  # type: ignore[arg-type]
        self.assertEqual(client.writes, [])
        self.assertFalse(client.notify_started)


class TestProgressCallbacks(unittest.IsolatedAsyncioTestCase):
    """Tests for section-by-section progressive updates during a poll."""

    async def test_full_poll_emits_progressive_snapshots(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd("ACCOUNT"), encode_lcd("01 #14.25")],
                [encode_lcd("ABOUT 12")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW  0.452")],
                [encode_lcd(" #0.14/HR")],
            ],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_TOTAL_UNITS: [[encode_lcd("12345 IMP UNITS")]],
            CMD_PREV_USAGE: [
                [encode_lcd("PREV DAY")],
                [encode_lcd("   #3.20")],
                [encode_lcd("PREV WEEK")],
                [encode_lcd("  #21.45")],
                [encode_lcd("PREV MNTH")],
                [encode_lcd("  #89.10")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        snapshots: list[PowerNIMeterData] = []
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            final = await async_read_meter(
                client,  # type: ignore[arg-type]
                on_progress=snapshots.append,
            )

        self.assertEqual(len(snapshots), 3)
        # Snapshot 1: only balance/days left delta
        self.assertEqual(snapshots[0].balance, 14.25)
        self.assertEqual(snapshots[0].days_left, 12)
        self.assertIsNone(snapshots[0].current_load_kw)
        self.assertIsNone(snapshots[0].total_import_kwh)
        # Snapshot 2: only live load/cost delta
        self.assertIsNone(snapshots[1].balance)
        self.assertEqual(snapshots[1].current_load_kw, 0.452)
        self.assertEqual(snapshots[1].current_cost_per_hour, 0.14)
        self.assertIsNone(snapshots[1].total_import_kwh)
        # Snapshot 3: only cumulative import kWh delta
        self.assertIsNone(snapshots[2].balance)
        self.assertIsNone(snapshots[2].current_load_kw)
        self.assertEqual(snapshots[2].total_import_kwh, 12345)
        self.assertIsNone(snapshots[2].yesterday_cost)
        self.assertEqual(final.yesterday_cost, 3.20)
        self.assertEqual(final.last_month_cost, 89.10)

    async def test_fast_poll_emits_two_snapshots_and_none_callback_ok(self) -> None:
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_GET_BALANCE: [
                [encode_lcd("ACCOUNT"), encode_lcd("01 #14.25")],
                [encode_lcd("ABOUT 12")],
            ],
            CMD_DISPLAY_RESET: [
                [encode_lcd("%%%%%%%%%")],
                [encode_lcd("%%%%%%%%%")],
            ],
            CMD_TOTAL_CONSUMPTION: [
                [encode_lcd("KW  0.452")],
                [encode_lcd(" #0.14/HR")],
            ],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        sections = frozenset({SECTION_BALANCE, SECTION_LIVE})
        snapshots: list[PowerNIMeterData] = []
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            await async_read_meter(
                FakeBleakClient(script),  # type: ignore[arg-type]
                sections=sections,
                on_progress=snapshots.append,
            )
            await async_read_meter(
                FakeBleakClient(script),  # type: ignore[arg-type]
                sections=sections,
                on_progress=None,
            )
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0].balance, 14.25)
        self.assertIsNone(snapshots[0].current_load_kw)
        self.assertIsNone(snapshots[1].balance)
        self.assertEqual(snapshots[1].current_load_kw, 0.452)


def encode_survey_counter_block(c0: int, c1: int, c2: int, c3: int) -> bytes:
    """Encode 4 13-bit half-hour counters into a 10-byte 0x8A survey block."""
    return (
        bytes([RSP_SURVEY_BLOCK_HEADER])
        + c0.to_bytes(2, "little")
        + c1.to_bytes(2, "little")
        + c2.to_bytes(2, "little")
        + c3.to_bytes(2, "little")
        + b"\x00"
    )


def encode_survey_date_block(day: datetime) -> bytes:
    """Encode a midnight date into the 13th 0x8A survey header block."""
    jan1 = datetime(day.year, 1, 1, tzinfo=day.tzinfo)
    day_offset = (day - jan1).days
    word = ((day.year - 1988) << 9) | (day_offset & 0x1FF)
    return bytes([RSP_SURVEY_BLOCK_HEADER]) + word.to_bytes(2, "little") + b"\x00\xfe\xff" + b"\x00" * 4


def encode_survey_day(day: datetime, counters: list[int]) -> list[bytes]:
    """Encode 48 half-hour counters + date header into 13 survey blocks."""
    assert len(counters) == 48
    blocks = [
        encode_survey_counter_block(*counters[i : i + 4]) for i in range(0, 48, 4)
    ]
    blocks.append(encode_survey_date_block(day))
    return blocks


class TestSurveyParser(unittest.TestCase):
    """Tests for 0xA0 binary survey date decoding, wrap-around, and hourly rollup."""

    def test_decode_survey_date_and_counter_wraparound(self) -> None:
        day = datetime(2026, 11, 22, tzinfo=timezone.utc)
        header = encode_survey_date_block(day)
        self.assertEqual(decode_survey_date(header), day)
        self.assertIsNone(decode_survey_date(b"\x00\x00\x00"))
        self.assertEqual(counter_delta_kwh(100, 180), 0.5)
        self.assertEqual(counter_delta_kwh(7990, 10), round(20 * 0.00625, 5))
        self.assertEqual(counter_delta_kwh(None, 100), 0.0)

    def test_parse_multi_day_newest_first_and_aggregate_hourly(self) -> None:
        day1 = datetime(2026, 11, 21, tzinfo=timezone.utc)
        day2 = datetime(2026, 11, 22, tzinfo=timezone.utc)

        # Build constant +16 counts (0.1 kWh) per half-hour across day1 -> day2 (00:00..02:30 populated on day2)
        c1 = [1000 + i * 16 for i in range(48)]
        last_c1 = c1[-1]
        c2 = [last_c1 + (i + 1) * 16 for i in range(6)] + [0x1FFF] * 42

        # Meter sends newest day first
        blocks = encode_survey_day(day2, c2) + encode_survey_day(day1, c1)
        slots = parse_survey_blocks(blocks, tz=timezone.utc)
        self.assertEqual(len(slots), 96)
        self.assertEqual(slots[0].start, day1)
        self.assertFalse(slots[0].has_baseline)
        # First hour of day2 crosses midnight cleanly using day1's 23:30 counter
        self.assertEqual(slots[48].start, day2)
        self.assertTrue(slots[48].has_baseline)
        self.assertEqual(slots[48].kwh, 0.1)

        # Current time = 2026-11-22 02:45 UTC -> hour 02:00 is incomplete (and 02:30 is empty)
        now = datetime(2026, 11, 22, 2, 45, tzinfo=timezone.utc)
        hourly = aggregate_hourly(slots, now)
        # day1 has hours 01:00..23:00 (23 hours, since hour 00:00 lacks pre-day1 baseline) + day2 hours 00:00..01:00 (2 hours)
        self.assertEqual(len(hourly), 25)
        self.assertEqual(hourly[0].start, day1 + timedelta(hours=1))
        self.assertEqual(hourly[-1].start, day2 + timedelta(hours=1))
        self.assertTrue(all(h.kwh == 0.2 for h in hourly))


class TestAsyncReadSurvey(unittest.IsolatedAsyncioTestCase):
    """Tests for SurveyFrameAssembler and async_read_survey."""

    def test_survey_frame_assembler_handles_split_blocks_and_payload_fc(self) -> None:
        asm = SurveyFrameAssembler()
        # Block whose payload contains 0xFC and 0xFF inside counter bytes
        block = bytes([RSP_SURVEY_BLOCK_HEADER, 0xFC, 0xFF, 1, 0, 2, 0, 3, 0, 0])
        c, b, ack, end = asm.feed(bytes([RSP_SURVEY_ACK]) + block[:4])
        self.assertEqual((c, b, ack, end), ([], [], True, False))
        c2, b2, ack2, end2 = asm.feed(block[4:] + RSP_KEEPALIVE_ACK[:2])
        self.assertEqual((c2, b2, ack2, end2), ([], [block], False, False))
        c3, b3, ack3, end3 = asm.feed(RSP_KEEPALIVE_ACK[2:] + bytes([RSP_SURVEY_END]))
        self.assertEqual((c3, b3, ack3, end3), ([RSP_KEEPALIVE_ACK], [], False, True))

    async def test_async_read_survey_reads_day_keepalive_and_end_marker(self) -> None:
        day = datetime(2026, 11, 22, tzinfo=timezone.utc)
        blocks = encode_survey_day(day, [100 + i for i in range(48)])
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_READ_SURVEY: [[bytes([RSP_SURVEY_ACK])]],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_SURVEY_NEXT: [[b] for b in blocks] + [[bytes([RSP_SURVEY_END])]],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "SURVEY_PACING_DELAY", 0.0),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            out = await async_read_survey(client, days=2)  # type: ignore[arg-type]

        self.assertEqual(out, blocks)
        self.assertEqual(client.writes[-1][1], CMD_LEAVE_TRANSPARENT)

    async def test_async_read_survey_aborts_immediately_on_disconnect(self) -> None:
        disconnected = asyncio.Event()

        class DropOnSurveyClient(FakeBleakClient):
            is_connected = True

            async def write_gatt_char(
                self, uuid: str, data: bytes, response: bool = False
            ) -> None:
                await super().write_gatt_char(uuid, data, response)
                if data == CMD_SURVEY_NEXT:
                    self.is_connected = False
                    disconnected.set()

        client = DropOnSurveyClient(
            {
                CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
                CMD_READ_SURVEY: [[bytes([RSP_SURVEY_ACK])]],
            }
        )
        started = time.monotonic()
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "SURVEY_PACING_DELAY", 0.0),
            self.assertRaises(PowerNIDisconnectedError),
        ):
            await async_read_survey(
                client,  # type: ignore[arg-type]
                days=1,
                disconnected=disconnected,
            )
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_async_read_survey_retries_single_dropped_f1_when_buffer_empty(self) -> None:
        day = datetime(2026, 11, 22, tzinfo=timezone.utc)
        blocks = encode_survey_day(day, [100 + i for i in range(48)])
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_READ_SURVEY: [[bytes([RSP_SURVEY_ACK])]],
            CMD_KEEPALIVE: [[RSP_KEEPALIVE_ACK]],
            CMD_SURVEY_NEXT: (
                [[], [blocks[0]]] + [[b] for b in blocks[1:]] + [[bytes([RSP_SURVEY_END])]]
            ),
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "SURVEY_PACING_DELAY", 0.0),
            patch.object(client_mod, "LINE_TIMEOUT", 0.03),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            out = await async_read_survey(client, days=2)  # type: ignore[arg-type]

        self.assertEqual(out, blocks)
        f1_writes = [w for w in client.writes if w[1] == CMD_SURVEY_NEXT]
        self.assertEqual(len(f1_writes), len(blocks) + 2)

    async def test_async_read_survey_does_not_retry_with_pending_partial_bytes(self) -> None:
        partial = bytes([RSP_SURVEY_BLOCK_HEADER, 0x01, 0x02, 0x03])
        script = {
            CMD_ENTER_TRANSPARENT: [[RSP_TRANSPARENT_ACK]],
            CMD_READ_SURVEY: [[bytes([RSP_SURVEY_ACK])]],
            CMD_SURVEY_NEXT: [[partial]],
            CMD_LEAVE_TRANSPARENT: [[RSP_DISCONNECT_ACK]],
        }
        client = FakeBleakClient(script)
        with (
            patch.object(client_mod, "COMMAND_PACING_DELAY", 0.0),
            patch.object(client_mod, "SURVEY_PACING_DELAY", 0.0),
            patch.object(client_mod, "LINE_TIMEOUT", 0.03),
            patch.object(client_mod, "DISCONNECT_ACK_TIMEOUT", 0.05),
        ):
            with self.assertRaises(PowerNIProtocolError) as ctx:
                await async_read_survey(client, days=1)  # type: ignore[arg-type]

        self.assertIn("pending='8a 01 02 03'", str(ctx.exception))
        f1_writes = [w for w in client.writes if w[1] == CMD_SURVEY_NEXT]
        self.assertEqual(len(f1_writes), 1)


if __name__ == "__main__":
    unittest.main()
