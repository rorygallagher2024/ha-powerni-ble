"""Global pytest configuration for Power NI Keypad+ BLE tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom_components/powerni_ble in Home Assistant tests."""
