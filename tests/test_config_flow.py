"""Tests for Power NI Keypad+ BLE config and options flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.powerni_ble.const import (
    CONF_AUTO_SYNC_HISTORY,
    CONF_PROXY_SOURCE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from tests.helpers import (
    TEST_ADDRESS,
    TEST_ALT_ADDRESS,
    TEST_NAME,
    TEST_PROXY_SOURCE,
    make_config_entry,
    make_scanner_device,
    make_service_info,
)


async def test_bluetooth_discovery_creates_entry(hass: HomeAssistant) -> None:
    """Valid Freedom Unit Bluetooth discovery shows confirmation with scanners & reset code."""
    info = make_service_info(name=TEST_NAME, address=TEST_ADDRESS)
    scanner_dev = make_scanner_device(
        address=TEST_ADDRESS,
        scanner_name="hallway-proxy",
        source=TEST_PROXY_SOURCE,
        rssi=-71,
    )

    with patch(
        "custom_components.powerni_ble.config_flow.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=info,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {
        "name": TEST_NAME,
        "address": TEST_ADDRESS,
        "scanners": "hallway-proxy (-71 dBm)",
        "reset_code": "*159268#",
    }

    with patch(
        "custom_components.powerni_ble.async_setup_entry",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == TEST_NAME
    assert result2["data"] == {CONF_ADDRESS: TEST_ADDRESS, CONF_NAME: TEST_NAME}
    assert result2["result"].unique_id == TEST_ADDRESS


async def test_bluetooth_discovery_by_local_name_without_service_uuid(
    hass: HomeAssistant,
) -> None:
    """PNI##### meters match by local name and show the *0# reset code."""
    info = make_service_info(
        name="PNI12345", address=TEST_ALT_ADDRESS, service_uuids=[]
    )
    with patch(
        "custom_components.powerni_ble.config_flow.async_scanner_devices_by_address",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=info,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {
        "name": "PNI12345",
        "address": TEST_ALT_ADDRESS,
        "scanners": "None currently visible",
        "reset_code": "*0#",
    }


async def test_bluetooth_discovery_aborts_unsupported_and_duplicate(
    hass: HomeAssistant,
) -> None:
    """Non-meter advertisements abort with not_supported; duplicates abort."""
    unrelated = make_service_info(
        name="OtherDevice",
        address=TEST_ALT_ADDRESS,
        service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"],
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=unrelated,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"

    existing = make_config_entry(address=TEST_ADDRESS, name=TEST_NAME)
    existing.add_to_hass(hass)

    duplicate = make_service_info(name=TEST_NAME, address=TEST_ADDRESS)
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=duplicate,
    )
    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_user_flow_selects_discovered_meter(hass: HomeAssistant) -> None:
    """User flow lists discovered meters and creates an entry on selection."""
    info = make_service_info(name=TEST_NAME, address=TEST_ADDRESS)
    with patch(
        "custom_components.powerni_ble.config_flow.async_discovered_service_info",
        return_value=[info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.powerni_ble.async_setup_entry",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: TEST_ADDRESS}
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"] == {CONF_ADDRESS: TEST_ADDRESS, CONF_NAME: TEST_NAME}


async def test_user_flow_can_choose_manual_when_devices_discovered(
    hass: HomeAssistant,
) -> None:
    """User flow allows picking manual entry even when meters are discovered."""
    info = make_service_info(name=TEST_NAME, address=TEST_ADDRESS)
    with patch(
        "custom_components.powerni_ble.config_flow.async_discovered_service_info",
        return_value=[info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_ADDRESS: "__manual__"}
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "manual"


async def test_user_and_manual_flow_validates_mac(hass: HomeAssistant) -> None:
    """Empty discovery falls through to manual step and validates MAC addresses."""
    with patch(
        "custom_components.powerni_ble.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    result_bad = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ADDRESS: "not-a-mac", CONF_NAME: "Hall Meter"},
    )
    assert result_bad["type"] is FlowResultType.FORM
    assert result_bad["errors"] == {CONF_ADDRESS: "invalid_mac"}

    with patch(
        "custom_components.powerni_ble.async_setup_entry",
        return_value=True,
    ):
        result_ok = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_ADDRESS: "aa:bb:cc:dd:ee:02", CONF_NAME: "Hall Meter"},
        )

    assert result_ok["type"] is FlowResultType.CREATE_ENTRY
    assert result_ok["title"] == "Hall Meter"
    assert result_ok["data"] == {
        CONF_ADDRESS: "AA:BB:CC:DD:EE:02",
        CONF_NAME: "Hall Meter",
    }


async def test_options_flow_lists_scanners_and_saves(hass: HomeAssistant) -> None:
    """Options flow offers discovered proxies with RSSI and persists options."""
    entry = make_config_entry(
        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_MINUTES}
    )
    entry.add_to_hass(hass)

    scanner_dev = make_scanner_device(
        address=TEST_ADDRESS,
        scanner_name="hallway-proxy",
        source=TEST_PROXY_SOURCE,
        rssi=-74,
    )
    with patch(
        "custom_components.powerni_ble.config_flow.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["data_schema"] is not None

        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_SCAN_INTERVAL: 5,
                CONF_PROXY_SOURCE: TEST_PROXY_SOURCE,
                CONF_AUTO_SYNC_HISTORY: False,
            },
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"] == {
        CONF_SCAN_INTERVAL: 5,
        CONF_PROXY_SOURCE: TEST_PROXY_SOURCE,
        CONF_AUTO_SYNC_HISTORY: False,
    }


async def test_options_flow_shows_active_automatic_proxy_label(
    hass: HomeAssistant,
) -> None:
    """Automatic option shows the remembered bonded proxy name when available."""
    entry = make_config_entry(
        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_MINUTES}
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(_preferred_proxy_source=TEST_PROXY_SOURCE)

    weak_scanner = make_scanner_device(
        address=TEST_ADDRESS,
        scanner_name="upstairs-proxy",
        source="22:33:44:55:66:77",
        rssi=-86,
    )
    strong_scanner = make_scanner_device(
        address=TEST_ADDRESS,
        scanner_name="hallway-proxy",
        source=TEST_PROXY_SOURCE,
        rssi=-70,
    )
    with patch(
        "custom_components.powerni_ble.config_flow.async_scanner_devices_by_address",
        return_value=[weak_scanner, strong_scanner],
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"] is not None
    proxy_validator = next(
        validator
        for key, validator in result["data_schema"].schema.items()
        if key.schema == CONF_PROXY_SOURCE
    )
    choices = proxy_validator.container
    assert choices[""] == "Automatic (currently: hallway-proxy)"
    # Strongest scanner is sorted right after Automatic.
    assert list(choices.keys()) == ["", TEST_PROXY_SOURCE, "22:33:44:55:66:77"]
