"""Shared test helpers and fake Bluetooth fixtures for Power NI BLE tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothScannerDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerni_ble.client import PowerNIMeterData
from custom_components.powerni_ble.const import (
    DOMAIN,
    RSP_SURVEY_BLOCK_HEADER,
    SERVICE_UUID,
)

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"
TEST_ALT_ADDRESS = "AA:BB:CC:DD:EE:01"
TEST_NAME = "B1234567"
TEST_PROXY_SOURCE = "11:22:33:44:55:66"
TEST_PROXY_SOURCE_2 = "22:33:44:55:66:77"


def make_ble_device(
    address: str = TEST_ADDRESS,
    name: str | None = TEST_NAME,
    details: Any = None,
) -> BLEDevice:
    """Create a synthetic Bleak BLEDevice."""
    return BLEDevice(address, name, details or {"source": TEST_PROXY_SOURCE})


def make_service_info(
    *,
    name: str = TEST_NAME,
    address: str = TEST_ADDRESS,
    rssi: int = -72,
    service_uuids: list[str] | None = None,
    source: str = TEST_PROXY_SOURCE,
    connectable: bool = True,
    time_seen: float = 100.0,
) -> BluetoothServiceInfoBleak:
    """Create a synthetic BluetoothServiceInfoBleak advertisement."""
    uuids = [SERVICE_UUID] if service_uuids is None else service_uuids
    device = make_ble_device(address=address, name=name, details={"source": source})
    advertisement = AdvertisementData(
        local_name=name or None,
        manufacturer_data={},
        service_data={},
        service_uuids=uuids,
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data={},
        service_data={},
        service_uuids=uuids,
        source=source,
        device=device,
        advertisement=advertisement,
        connectable=connectable,
        time=time_seen,
        tx_power=None,
    )


def make_scanner_device(
    *,
    address: str = TEST_ADDRESS,
    name: str = TEST_NAME,
    scanner_name: str = "esp-proxy-1",
    source: str = TEST_PROXY_SOURCE,
    rssi: int = -72,
) -> BluetoothScannerDevice:
    """Create a synthetic BluetoothScannerDevice with scanner metadata."""
    scanner: Any = SimpleNamespace(name=scanner_name, source=source)
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothScannerDevice(
        scanner=scanner,
        ble_device=make_ble_device(address=address, name=name, details={"source": source}),
        advertisement=advertisement,
    )


def make_sample_meter_data(**overrides: Any) -> PowerNIMeterData:
    """Return a populated PowerNIMeterData snapshot with optional overrides."""
    payload: dict[str, Any] = {
        "balance": 24.50,
        "days_left": 18,
        "days_left_status": "ABOUT 18",
        "current_load_kw": 0.485,
        "current_cost_per_hour": 0.14,
        "total_import_kwh": 11410,
        "unit_rate": 0.33285,
        "yesterday_cost": 2.77,
        "last_7_days_cost": 20.15,
        "last_month_cost": 65.05,
    }
    payload.update(overrides)
    return PowerNIMeterData(**payload)


def make_config_entry(
    *,
    address: str = TEST_ADDRESS,
    name: str = TEST_NAME,
    options: dict[str, Any] | None = None,
    entry_id: str = "test_entry_id",
) -> MockConfigEntry:
    """Create a MockConfigEntry for Power NI Keypad+ BLE."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        unique_id=address.upper(),
        data={CONF_ADDRESS: address.upper(), CONF_NAME: name},
        options=options or {},
        entry_id=entry_id,
    )


def encode_survey_day(day: datetime, counters: list[int]) -> list[bytes]:
    """Encode 48 half-hour 13-bit counters + date header into 13 0x8A survey blocks."""
    assert len(counters) == 48
    blocks = [
        bytes([RSP_SURVEY_BLOCK_HEADER])
        + counters[i].to_bytes(2, "little")
        + counters[i + 1].to_bytes(2, "little")
        + counters[i + 2].to_bytes(2, "little")
        + counters[i + 3].to_bytes(2, "little")
        + b"\x00"
        for i in range(0, 48, 4)
    ]
    jan1 = datetime(day.year, 1, 1, tzinfo=day.tzinfo)
    day_offset = (day - jan1).days
    word = ((day.year - 1988) << 9) | (day_offset & 0x1FF)
    blocks.append(
        bytes([RSP_SURVEY_BLOCK_HEADER])
        + word.to_bytes(2, "little")
        + b"\x00\xfe\xff"
        + b"\x00" * 4
    )
    return blocks


def hourly_day_counters(start_counter: int, increment: int = 16) -> list[int]:
    """Generate 48 monotonic counters stepping by `increment` each half-hour."""
    return [(start_counter + idx * increment) % 8000 for idx in range(48)]


def next_day(day: datetime, days: int = 1) -> datetime:
    """Return `day` shifted forward by `days`."""
    return day + timedelta(days=days)
