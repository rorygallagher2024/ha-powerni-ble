"""Tests for Power NI BLE entity setup, translated names, actions, and diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.powerni_ble import coordinator as coord_mod
from custom_components.powerni_ble.const import (
    CONF_SCAN_INTERVAL,
    DOMAIN,
    POLL_STATUS_IDLE,
)
from custom_components.powerni_ble.coordinator import (
    TopUp,
    pairing_issue_id_for_address,
)
from custom_components.powerni_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.helpers import (
    TEST_ADDRESS,
    TEST_NAME,
    TEST_PROXY_SOURCE,
    make_config_entry,
    make_sample_meter_data,
    make_scanner_device,
)

EXPECTED_ENTITIES: dict[str, tuple[str, EntityCategory | None]] = {
    f"{TEST_ADDRESS}_balance": ("B1234567 Balance", None),
    f"{TEST_ADDRESS}_days_left": ("B1234567 Days left", None),
    f"{TEST_ADDRESS}_current_load_kw": ("B1234567 Current load", None),
    f"{TEST_ADDRESS}_current_cost_per_hour": ("B1234567 Current cost rate", None),
    f"{TEST_ADDRESS}_total_import_kwh": ("B1234567 Total import energy", None),
    f"{TEST_ADDRESS}_yesterday_cost": ("B1234567 Yesterday's spend", None),
    f"{TEST_ADDRESS}_last_7_days_cost": ("B1234567 Last 7 days spend", None),
    f"{TEST_ADDRESS}_last_month_cost": ("B1234567 Last month spend", None),
    f"{TEST_ADDRESS}_poll_status": ("B1234567 Poll status", EntityCategory.DIAGNOSTIC),
    f"{TEST_ADDRESS}_last_successful_read": (
        "B1234567 Last successful read",
        EntityCategory.DIAGNOSTIC,
    ),
    f"{TEST_ADDRESS}_last_top_up": ("B1234567 Last top-up", None),
    f"{TEST_ADDRESS}_last_top_up_time": ("B1234567 Last top-up time", None),
    f"{TEST_ADDRESS}_signal_strength": (
        "B1234567 Signal strength",
        EntityCategory.DIAGNOSTIC,
    ),
    f"{TEST_ADDRESS}_updating": ("B1234567 Updating", EntityCategory.DIAGNOSTIC),
    f"{TEST_ADDRESS}_refresh": ("B1234567 Refresh", None),
    f"{TEST_ADDRESS}_sync_energy_history": (
        "B1234567 Sync energy history",
        EntityCategory.CONFIG,
    ),
    f"{TEST_ADDRESS}_poll_interval": ("B1234567 Poll interval", EntityCategory.CONFIG),
}


async def test_setup_entities_actions_and_unload(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Full entry setup registers 17 translated entities and supports controls + unload."""
    sample = make_sample_meter_data()
    fake_client = MagicMock(is_connected=True, disconnect=AsyncMock())
    scanner = make_scanner_device(rssi=-73)
    entry = make_config_entry(
        options={CONF_SCAN_INTERVAL: 15, "proxy_source": TEST_PROXY_SOURCE}
    )
    entry.add_to_hass(hass)

    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner],
        ),
        patch.object(
            coord_mod, "establish_connection", AsyncMock(return_value=fake_client)
        ),
        patch.object(coord_mod, "async_read_meter", AsyncMock(return_value=sample)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_reg, entry.entry_id)
    assert len(entries) == 17

    by_unique_id = {ent.unique_id: ent for ent in entries}
    assert set(by_unique_id) == set(EXPECTED_ENTITIES)
    for uid, (expected_name, expected_cat) in EXPECTED_ENTITIES.items():
        reg_entry = by_unique_id[uid]
        assert reg_entry.entity_category is expected_cat
        state = hass.states.get(reg_entry.entity_id)
        assert state is not None
        assert state.attributes["friendly_name"] == expected_name

    # Check sensor states and attributes
    balance_state = hass.states.get(by_unique_id[f"{TEST_ADDRESS}_balance"].entity_id)
    assert balance_state is not None
    assert balance_state.state == "24.5"
    assert "last_read" in balance_state.attributes

    days_left_state = hass.states.get(
        by_unique_id[f"{TEST_ADDRESS}_days_left"].entity_id
    )
    assert days_left_state is not None
    assert days_left_state.attributes["status"] == "ABOUT 18"

    status_state = hass.states.get(
        by_unique_id[f"{TEST_ADDRESS}_poll_status"].entity_id
    )
    assert status_state is not None
    assert status_state.state == POLL_STATUS_IDLE

    rssi_state = hass.states.get(
        by_unique_id[f"{TEST_ADDRESS}_signal_strength"].entity_id
    )
    assert rssi_state is not None
    assert rssi_state.state == "-73"

    # Press Refresh button -> invokes async_request_full_refresh
    coord = entry.runtime_data
    with patch.object(coord, "async_request_full_refresh", AsyncMock()) as mock_refresh:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": by_unique_id[f"{TEST_ADDRESS}_refresh"].entity_id},
            blocking=True,
        )
    mock_refresh.assert_awaited_once()

    # Press Sync energy history button -> invokes async_sync_energy_history(full=True)
    with patch.object(
        coord, "async_sync_energy_history", AsyncMock(return_value=12)
    ) as mock_sync:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": by_unique_id[f"{TEST_ADDRESS}_sync_energy_history"].entity_id},
            blocking=True,
        )
    mock_sync.assert_awaited_once_with(full=True)

    # Set Poll interval number -> updates coordinator and entry.options
    number_eid = by_unique_id[f"{TEST_ADDRESS}_poll_interval"].entity_id
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": number_eid, "value": 5},
        blocking=True,
    )
    assert coord.scan_interval_minutes == 5
    assert entry.options[CONF_SCAN_INTERVAL] == 5

    # Clean unload and removal
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)


async def test_diagnostics_redacts_sensitive_fields(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Diagnostics exposes coordinator/scanner telemetry while redacting MACs and proxy IDs."""
    sample = make_sample_meter_data()
    scanner = make_scanner_device(scanner_name="hallway-proxy", rssi=-68)
    entry = make_config_entry(
        options={CONF_SCAN_INTERVAL: 15, "proxy_source": TEST_PROXY_SOURCE}
    )
    entry.add_to_hass(hass)

    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner],
        ),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(return_value=MagicMock(is_connected=True, disconnect=AsyncMock())),
        ),
        patch.object(coord_mod, "async_read_meter", AsyncMock(return_value=sample)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coord = entry.runtime_data
    coord._preferred_proxy_source = TEST_PROXY_SOURCE
    coord.last_top_up = TopUp(
        amount=25.0,
        at=datetime(2026, 11, 22, 9, 0, tzinfo=UTC),
        balance_before=5.0,
        balance_after=30.0,
    )

    with patch(
        "custom_components.powerni_ble.diagnostics.bluetooth.async_scanner_devices_by_address",
        return_value=[scanner],
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["title"] == TEST_NAME
    assert diag["entry"]["unique_id"] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_ADDRESS] == "**REDACTED**"
    assert diag["entry"]["options"]["proxy_source"] == "**REDACTED**"

    assert diag["coordinator"]["poll_status"] == POLL_STATUS_IDLE
    assert diag["coordinator"]["auto_sync_history"] is True
    assert diag["coordinator"]["preferred_proxy_source"] == "**REDACTED**"
    assert diag["coordinator"]["rssi"] == -68
    assert diag["coordinator"]["data"]["balance"] == 24.5
    assert diag["coordinator"]["last_top_up"]["amount"] == 25.0

    assert len(diag["bluetooth_scanners"]) == 1
    assert diag["bluetooth_scanners"][0]["name"] == "hallway-proxy"
    assert diag["bluetooth_scanners"][0]["source"] == "**REDACTED**"
    assert diag["bluetooth_scanners"][0]["rssi"] == -68

    # Open repair issues are deleted when the config entry is removed.
    issue_id = pairing_issue_id_for_address(TEST_ADDRESS)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="pairing_required",
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
