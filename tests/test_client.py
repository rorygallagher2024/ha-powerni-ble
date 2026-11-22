"""Unit tests for the Power NI BLE frame assembler and protocol client."""

from __future__ import annotations

from pathlib import Path
import sys
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
    PowerNIProtocolError,
    async_read_meter,
)
from custom_components.powerni_ble.const import (  # noqa: E402
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


class TestAsyncReadMeter(unittest.IsolatedAsyncioTestCase):
    """End-to-end protocol tests against FakeBleakClient."""

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


if __name__ == "__main__":
    unittest.main()
