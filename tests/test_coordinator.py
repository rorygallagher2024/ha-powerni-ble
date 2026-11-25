"""Tests for PowerNIDataUpdateCoordinator scheduling, backoff, broadcasts, and survey sync."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from homeassistant.components.recorder.statistics import StatisticMeanType
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.powerni_ble import coordinator as coord_mod
from custom_components.powerni_ble.client import (
    PowerNIDisconnectedError,
    PowerNIMeterData,
)
from custom_components.powerni_ble.const import (
    ADVERTISEMENT_SILENT_SECONDS,
    ADVERTISEMENT_WAITING_TIMEOUT_SECONDS,
    CONF_AUTO_SYNC_HISTORY,
    DOMAIN,
    FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS,
    POLL_STATUS_ERROR,
    POLL_STATUS_IDLE,
    POLL_STATUS_WAITING,
    RSSI_MIN_PUBLISH_INTERVAL_SECONDS,
    SECTION_RATE,
    SECTION_SPEND,
    SECTION_UNITS,
    SURVEY_INCREMENTAL_DAYS,
    SURVEY_MAX_DAYS,
)
from custom_components.powerni_ble.coordinator import (
    EVENT_TOP_UP,
    PowerNIDataUpdateCoordinator,
)
from tests.helpers import (
    TEST_ADDRESS,
    TEST_NAME,
    TEST_PROXY_SOURCE,
    TEST_PROXY_SOURCE_2,
    encode_survey_day,
    hourly_day_counters,
    make_ble_device,
    make_config_entry,
    make_sample_meter_data,
    make_scanner_device,
    make_service_info,
    next_day,
)


def _make_coordinator(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
) -> PowerNIDataUpdateCoordinator:
    entry = make_config_entry(options=options)
    entry.add_to_hass(hass)
    return PowerNIDataUpdateCoordinator(hass, entry, TEST_ADDRESS, TEST_NAME)


def _mock_client(*, is_connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.is_connected = is_connected
    client.disconnect = AsyncMock()
    client.clear_cache = AsyncMock()
    return client


async def test_partial_read_carry_over_and_staleness(hass: HomeAssistant) -> None:
    """Missing fields carry forward while only freshly read fields update field_read_at."""
    coord = _make_coordinator(hass)
    t1 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    first = make_sample_meter_data(balance=25.00, yesterday_cost=3.10)

    with (
        patch.object(coord, "_async_poll", AsyncMock(return_value=first)),
        patch.object(dt_util, "now", return_value=t1),
        patch.object(coord, "_maybe_schedule_survey_sync"),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
    ):
        await coord.async_refresh()

    assert coord.data is not None
    assert coord.data.balance == 25.00
    assert coord.data.yesterday_cost == 3.10
    assert coord.field_read_at["balance"] == t1
    assert coord.field_read_at["yesterday_cost"] == t1
    with patch.object(dt_util, "now", return_value=t1):
        assert not coord.is_field_stale("yesterday_cost")

    # Second read drops yesterday_cost, but balance updates -> yesterday_cost carries over.
    t2 = t1 + timedelta(hours=2)
    second = PowerNIMeterData(balance=24.20, current_load_kw=0.510)
    with (
        patch.object(coord, "_async_poll", AsyncMock(return_value=second)),
        patch.object(dt_util, "now", return_value=t2),
        patch.object(coord, "_maybe_schedule_survey_sync"),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
    ):
        await coord.async_refresh()

    assert coord.data.balance == 24.20
    assert coord.data.yesterday_cost == 3.10
    assert coord.field_read_at["balance"] == t2
    assert coord.field_read_at["yesterday_cost"] == t1

    # Once t_future exceeds max(24h, 3 * poll_interval), stale checks mark old fields.
    notified: list[bool] = []
    coord.async_add_listener(lambda: notified.append(True))
    t_stale = t1 + timedelta(hours=25)
    with patch.object(dt_util, "now", return_value=t_stale):
        assert coord.is_field_stale("yesterday_cost")
        coord._check_and_notify_stale(t_stale)
    assert len(notified) == 1
    # Repeated check for already-notified stale field does not spam listeners.
    with patch.object(dt_util, "now", return_value=t_stale + timedelta(minutes=5)):
        coord._check_and_notify_stale(t_stale + timedelta(minutes=5))
    assert len(notified) == 1


async def test_backoff_and_in_poll_retry_and_cache_clear(hass: HomeAssistant) -> None:
    """Test 1/2/5m backoff, in-poll reconnect retry, and guarded clear_cache."""
    coord = _make_coordinator(hass, options={"scan_interval": 15})
    ble_device = make_ble_device()

    # 1. PowerNIDisconnectedError on attempt 1 -> retries once and succeeds; clear_cache NOT called.
    disconnected_client = _mock_client(is_connected=False)
    ok_client = _mock_client(is_connected=True)
    expected = make_sample_meter_data()

    with (
        patch.object(coord, "_get_ble_device", return_value=ble_device),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(side_effect=[disconnected_client, ok_client]),
        ),
        patch.object(
            coord_mod,
            "async_read_meter",
            AsyncMock(side_effect=[PowerNIDisconnectedError("dropped"), expected]),
        ),
        patch.object(coord_mod, "SESSION_RETRY_DELAY_SECONDS", 0.0),
    ):
        res = await coord._async_poll(frozenset())

    assert res == expected
    disconnected_client.clear_cache.assert_not_called()
    ok_client.disconnect.assert_awaited_once()

    # 2. BleakCharacteristicNotFoundError while still connected -> clear_cache IS called.
    bad_gatt_client = _mock_client(is_connected=True)
    with (
        patch.object(coord, "_get_ble_device", return_value=ble_device),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(side_effect=[bad_gatt_client, bad_gatt_client]),
        ),
        patch.object(
            coord_mod,
            "async_read_meter",
            AsyncMock(
                side_effect=BleakCharacteristicNotFoundError("49535343-missing")
            ),
        ),
        patch.object(coord_mod, "SESSION_RETRY_DELAY_SECONDS", 0.0),
        pytest.raises(UpdateFailed),
    ):
        await coord._async_poll(frozenset())

    assert bad_gatt_client.clear_cache.await_count == 2

    # 3. Consecutive _async_update_data failures step backoff 1m -> 2m -> 5m -> reset 15m.
    with patch.object(
        coord, "_async_poll", AsyncMock(side_effect=UpdateFailed("ble error"))
    ):
        for expected_mins in (1, 2, 5, 5):
            with pytest.raises(UpdateFailed):
                await coord._async_update_data()
            assert coord.poll_status == POLL_STATUS_ERROR
            assert coord.update_interval == timedelta(minutes=expected_mins)

    with (
        patch.object(coord, "_async_poll", AsyncMock(return_value=expected)),
        patch.object(coord, "_maybe_schedule_survey_sync"),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
    ):
        await coord._async_update_data()

    assert coord._consecutive_failures == 0
    assert coord.poll_status == POLL_STATUS_IDLE
    assert coord.update_interval == timedelta(minutes=15)


async def test_top_up_detection_event_and_store_persistence(
    hass: HomeAssistant,
) -> None:
    """Balance rise >= £1 fires powerni_ble_top_up and survives store reload."""
    coord = _make_coordinator(hass)
    events: list[Event] = []
    hass.bus.async_listen(EVENT_TOP_UP, events.append)

    t1 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 22, 10, 15, tzinfo=UTC)

    with patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0):
        coord._check_top_up(10.00, t1)
        coord._check_top_up(30.00, t2)
        await hass.async_block_till_done()
        await coord._store.async_save(coord._stored_state())

    assert len(events) == 1
    assert events[0].data["amount"] == 20.00
    assert events[0].data["balance_before"] == 10.00
    assert events[0].data["balance_after"] == 30.00
    assert coord.last_top_up is not None
    assert coord.last_top_up.amount == 20.00

    # Reload into a fresh coordinator backed by the same config entry / Store file.
    reloaded = PowerNIDataUpdateCoordinator(
        hass, coord.config_entry, TEST_ADDRESS, TEST_NAME
    )
    await reloaded.async_load_stored_state()
    assert reloaded._top_up_baseline == 30.00
    assert reloaded.last_top_up is not None
    assert reloaded.last_top_up.amount == 20.00
    assert reloaded.last_top_up.balance_before == 10.00
    assert reloaded.last_top_up.balance_after == 30.00


async def test_waiting_escalates_to_error_after_timeout(
    hass: HomeAssistant,
) -> None:
    """Prolonged silence shortens waiting re-check, escalates to error, and still auto-recovers."""
    coord = _make_coordinator(hass, options={"scan_interval": 120})
    coord.data = make_sample_meter_data()

    # Brief silence defers cleanly and schedules a re-check at the waiting timeout.
    t_adv = 1000.0
    t_wait = t_adv + ADVERTISEMENT_SILENT_SECONDS + 30.0
    coord._last_adv_monotonic = t_adv
    with patch.object(coord_mod.time, "monotonic", return_value=t_wait):
        deferred = await coord._async_update_data()

    assert deferred is coord.data
    assert coord._poll_deferred is True
    assert coord.poll_status == POLL_STATUS_WAITING
    assert coord.update_interval == timedelta(
        seconds=ADVERTISEMENT_WAITING_TIMEOUT_SECONDS - (t_wait - t_adv)
    )

    # Once silence reaches ADVERTISEMENT_WAITING_TIMEOUT_SECONDS, stop deferring and probe.
    t_expired = t_adv + ADVERTISEMENT_WAITING_TIMEOUT_SECONDS + 5.0
    with (
        patch.object(coord_mod.time, "monotonic", return_value=t_expired),
        patch.object(
            coord,
            "_async_poll",
            AsyncMock(side_effect=UpdateFailed("Meter not visible")),
        ) as mock_poll,
        pytest.raises(UpdateFailed, match="Meter not visible"),
    ):
        await coord._async_update_data()

    mock_poll.assert_awaited_once()
    assert coord._poll_deferred is False
    assert coord.poll_status == POLL_STATUS_ERROR
    assert coord._consecutive_failures == 1

    # Advertisements resuming later after the gap still trigger an immediate recovery poll.
    t_resume = t_expired + FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS + 5.0
    with (
        patch.object(coord_mod.time, "monotonic", return_value=t_resume),
        patch.object(coord, "async_request_refresh", AsyncMock()) as mock_refresh,
    ):
        coord._on_advertisement(make_service_info(rssi=-74))
        await hass.async_block_till_done()

    mock_refresh.assert_awaited_once()


async def test_broadcast_deferral_resume_and_rssi_throttling(
    hass: HomeAssistant,
) -> None:
    """Silent broadcasts defer polls; resuming triggers catch-up; RSSI is median-smoothed and throttled."""
    coord = _make_coordinator(hass)
    coord.data = make_sample_meter_data()

    # Make advertisements appear silent (> ADVERTISEMENT_SILENT_SECONDS).
    t_now = 1000.0
    coord._last_adv_monotonic = t_now - (ADVERTISEMENT_SILENT_SECONDS + 10.0)
    with patch.object(coord_mod.time, "monotonic", return_value=t_now):
        deferred_data = await coord._async_update_data()

    assert deferred_data is coord.data
    assert coord._poll_deferred is True
    assert coord.poll_status == POLL_STATUS_WAITING

    # Resuming advertisement clears deferral and triggers async_request_refresh once.
    with (
        patch.object(coord, "async_request_refresh", AsyncMock()) as mock_refresh,
        patch.object(coord_mod.time, "monotonic", return_value=t_now + 5.0),
    ):
        coord._on_advertisement(make_service_info(rssi=-75))
        await hass.async_block_till_done()

    assert coord._poll_deferred is False
    mock_refresh.assert_awaited_once()

    # Non-connectable or wrong-proxy adverts are ignored.
    coord._on_advertisement(make_service_info(connectable=False, rssi=-40))
    assert coord.rssi == -75

    # Continuous adverts immediately after a failure must NOT bypass backoff.
    coord._consecutive_failures = 1
    coord._last_failure_monotonic = t_now + 10.0
    with (
        patch.object(coord, "async_request_refresh", AsyncMock()) as mock_refresh,
        patch.object(
            coord_mod.time,
            "monotonic",
            return_value=t_now + 10.0 + FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS + 1.0,
        ),
    ):
        # Update _last_adv_monotonic right before so there is no silence gap.
        coord._last_adv_monotonic = (
            t_now + 10.0 + FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS
        )
        coord._on_advertisement(make_service_info(rssi=-76))
        await hass.async_block_till_done()
    mock_refresh.assert_not_awaited()
    coord._consecutive_failures = 0

    # RSSI smoothing + 60s/3 dBm throttling:
    updates: list[int | None] = []
    coord.async_add_listener(lambda: updates.append(coord.rssi))
    coord._last_published_rssi = None
    coord._last_rssi_publish_monotonic = None

    base_t = 2000.0
    with patch.object(coord_mod.time, "monotonic", return_value=base_t):
        coord._on_advertisement(make_service_info(rssi=-85))
    assert len(updates) == 1

    # Another -85..-90 advert within 60s does NOT notify listeners even if median changes >= 3 dBm.
    with patch.object(coord_mod.time, "monotonic", return_value=base_t + 10.0):
        for rssi in (-90, -90, -90, -90):
            coord._on_advertisement(make_service_info(rssi=rssi))
    assert len(updates) == 1

    # After 60s has elapsed, listener updates once to the median.
    with patch.object(
        coord_mod.time,
        "monotonic",
        return_value=base_t + RSSI_MIN_PUBLISH_INTERVAL_SECONDS + 1.0,
    ):
        coord._on_advertisement(make_service_info(rssi=-90))
    assert len(updates) == 2
    assert coord.rssi == -90


async def test_proxy_source_option_update_resets_rssi_and_pins(
    hass: HomeAssistant,
) -> None:
    """Changing proxy_source clears old samples and re-seeds from the newly chosen proxy."""
    coord = _make_coordinator(hass, options={"proxy_source": TEST_PROXY_SOURCE})
    coord._rssi_samples.extend([-70, -72])
    scanner_new = make_scanner_device(source=TEST_PROXY_SOURCE_2, rssi=-65)

    hass.config_entries.async_update_entry(
        coord.config_entry,
        options={"scan_interval": 10, "proxy_source": TEST_PROXY_SOURCE_2},
    )
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner_new],
        ),
        patch.object(coord, "async_request_refresh", AsyncMock()) as mock_refresh,
    ):
        await coord.async_apply_options()

    assert coord.scan_interval_minutes == 10
    assert coord.proxy_source == TEST_PROXY_SOURCE_2
    assert coord.rssi == -65
    mock_refresh.assert_awaited_once()


async def test_sync_energy_history_full_incremental_and_no_recorder(
    hass: HomeAssistant,
) -> None:
    """Verify full/incremental 0xA0 survey backfill and no-op when recorder inactive."""
    coord = _make_coordinator(hass)
    assert await coord.async_sync_energy_history(full=True) == 0

    day1 = datetime(2026, 11, 21, tzinfo=UTC)
    day2 = next_day(day1, 1)
    day3 = next_day(day1, 2)
    c1 = hourly_day_counters(100, increment=16)
    c2 = hourly_day_counters(c1[-1] + 16, increment=16)
    c3 = hourly_day_counters(c2[-1] + 16, increment=16)
    blocks_initial = encode_survey_day(day2, c2) + encode_survey_day(day1, c1)

    ble_device = make_ble_device()
    client = _mock_client()
    fake_recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(
            side_effect=lambda fn, *args: fn(*args)
        )
    )
    added_calls: list[tuple[Any, Any]] = []

    hass.config.components.add("recorder")
    now_utc = day3 + timedelta(hours=3)
    try:
        # 1. Initial sync (no prior statistics) -> requests SURVEY_MAX_DAYS.
        with (
            patch.object(coord, "_get_ble_device", return_value=ble_device),
            patch.object(coord_mod, "establish_connection", AsyncMock(return_value=client)),
            patch.object(
                coord_mod,
                "async_read_survey",
                AsyncMock(return_value=blocks_initial),
            ) as mock_read_survey,
            patch.object(coord_mod, "get_instance", return_value=fake_recorder),
            patch.object(coord_mod, "get_last_statistics", return_value={}),
            patch.object(
                coord_mod,
                "async_add_external_statistics",
                side_effect=lambda _hass, meta, rows: added_calls.append((meta, rows)),
            ),
            patch.object(dt_util, "get_default_time_zone", return_value=UTC),
            patch.object(dt_util, "utcnow", return_value=now_utc),
        ):
            imported = await coord.async_sync_energy_history(full=True)

        assert mock_read_survey.await_args is not None
        assert mock_read_survey.await_args.kwargs["days"] == SURVEY_MAX_DAYS
        assert imported > 0
        meta, rows = added_calls[0]
        assert meta["mean_type"] is StatisticMeanType.NONE
        assert meta["unit_class"] == "energy"
        assert meta["has_sum"] is True
        assert meta["statistic_id"].endswith("_energy_history")
        assert rows[-1]["sum"] > 0

        # 2. Incremental sync with cursor at end of day2 -> requests SURVEY_INCREMENTAL_DAYS and resumes running sum.
        last_start = rows[-1]["start"].timestamp()
        last_sum = rows[-1]["sum"]
        blocks_inc = encode_survey_day(day3, c3) + encode_survey_day(day2, c2)
        added_calls.clear()

        with (
            patch.object(coord, "_get_ble_device", return_value=ble_device),
            patch.object(coord_mod, "establish_connection", AsyncMock(return_value=client)),
            patch.object(
                coord_mod,
                "async_read_survey",
                AsyncMock(return_value=blocks_inc),
            ) as mock_read_inc,
            patch.object(coord_mod, "get_instance", return_value=fake_recorder),
            patch.object(
                coord_mod,
                "get_last_statistics",
                return_value={
                    meta["statistic_id"]: [{"start": last_start, "sum": last_sum}]
                },
            ),
            patch.object(
                coord_mod,
                "async_add_external_statistics",
                side_effect=lambda _hass, m, r: added_calls.append((m, r)),
            ),
            patch.object(dt_util, "get_default_time_zone", return_value=UTC),
            patch.object(dt_util, "utcnow", return_value=now_utc),
        ):
            imported_inc = await coord.async_sync_energy_history(full=False)

        assert mock_read_inc.await_args is not None
        assert mock_read_inc.await_args.kwargs["days"] == SURVEY_INCREMENTAL_DAYS
        assert imported_inc == 3
        inc_rows = added_calls[0][1]
        assert all(row["start"].timestamp() > last_start for row in inc_rows)
        assert inc_rows[0]["sum"] > last_sum
    finally:
        hass.config.components.remove("recorder")


def test_section_tracking_helpers() -> None:
    """Ensure slow-section read timestamps record units and spend separately."""
    entry = make_config_entry()
    coord = PowerNIDataUpdateCoordinator.__new__(PowerNIDataUpdateCoordinator)
    coord.config_entry = entry
    coord._section_last_read = {}
    ts = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    coord._mark_sections_read(PowerNIMeterData(balance=10.0), ts)
    assert coord._section_last_read == {}
    coord._mark_sections_read(make_sample_meter_data(), ts)
    assert coord._section_last_read[SECTION_UNITS] == ts
    assert coord._section_last_read[SECTION_RATE] == ts
    assert coord._section_last_read[SECTION_SPEND] == ts


async def test_auto_sticky_proxy_and_pairing_repair_lifecycle(
    hass: HomeAssistant,
) -> None:
    """Automatic mode remembers the working proxy, raises Repairs on auth fail, and self-clears."""
    coord = _make_coordinator(hass)
    scanner_bonded = make_scanner_device(
        scanner_name="bonded-proxy", source=TEST_PROXY_SOURCE, rssi=-78
    )
    scanner_other = make_scanner_device(
        scanner_name="unbonded-proxy", source=TEST_PROXY_SOURCE_2, rssi=-62
    )
    sample = make_sample_meter_data()

    # 1. Successful poll in Automatic mode remembers proxy source in Store and clears issues.
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_ble_device_from_address",
            return_value=scanner_bonded.ble_device,
        ),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(return_value=_mock_client()),
        ),
        patch.object(coord_mod, "async_read_meter", AsyncMock(return_value=sample)),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
    ):
        await coord._async_poll(frozenset())
        await coord._store.async_save(coord._stored_state())

    assert coord._preferred_proxy_source == TEST_PROXY_SOURCE

    # Reloaded coordinator restores sticky proxy and prefers it even when another proxy is stronger.
    reloaded = PowerNIDataUpdateCoordinator(
        hass, coord.config_entry, TEST_ADDRESS, TEST_NAME
    )
    await reloaded.async_load_stored_state()
    assert reloaded._preferred_proxy_source == TEST_PROXY_SOURCE

    with patch.object(
        coord_mod.bluetooth,
        "async_scanner_devices_by_address",
        return_value=[scanner_other, scanner_bonded],
    ):
        chosen = reloaded._get_ble_device()
    assert chosen is scanner_bonded.ble_device

    # If the preferred proxy disappears from range, Automatic mode falls back.
    fallback_dev = make_ble_device()
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner_other],
        ),
        patch.object(
            coord_mod.bluetooth,
            "async_ble_device_from_address",
            return_value=fallback_dev,
        ),
    ):
        assert reloaded._get_ble_device() is fallback_dev

    # 2. Non-auth BleakError must NOT create a pairing Repair issue.
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner_bonded],
        ),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(side_effect=BleakError("Connection slot exhausted")),
        ),
        pytest.raises(UpdateFailed),
    ):
        await reloaded._async_poll(frozenset())

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, reloaded.pairing_issue_id) is None
    assert reloaded._preferred_proxy_source == TEST_PROXY_SOURCE

    # 3. Auth/pairing error creates actionable Repair issue and clears sticky preference.
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_scanner_devices_by_address",
            return_value=[scanner_bonded],
        ),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(side_effect=BleakError("Insufficient authorization (8)")),
        ),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
        pytest.raises(UpdateFailed),
    ):
        await reloaded._async_poll(frozenset())

    issue = issue_reg.async_get_issue(DOMAIN, reloaded.pairing_issue_id)
    assert issue is not None
    assert issue.translation_key == "pairing_required"
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["name"] == TEST_NAME
    assert issue.translation_placeholders["address"] == TEST_ADDRESS
    assert "bonded-proxy" in issue.translation_placeholders["proxy"]
    assert reloaded._preferred_proxy_source is None

    # 4. Next successful poll deletes the Repair issue automatically.
    with (
        patch.object(
            coord_mod.bluetooth,
            "async_ble_device_from_address",
            return_value=scanner_bonded.ble_device,
        ),
        patch.object(
            coord_mod,
            "establish_connection",
            AsyncMock(return_value=_mock_client()),
        ),
        patch.object(coord_mod, "async_read_meter", AsyncMock(return_value=sample)),
        patch.object(coord_mod, "STORE_SAVE_DELAY_SECONDS", 0),
    ):
        await reloaded._async_poll(frozenset())

    assert issue_reg.async_get_issue(DOMAIN, reloaded.pairing_issue_id) is None


async def test_auto_sync_history_option_controls_background_task(
    hass: HomeAssistant,
) -> None:
    """Disabling auto_sync_history prevents scheduling and cancels active background tasks."""
    coord = _make_coordinator(hass, options={CONF_AUTO_SYNC_HISTORY: False})
    assert coord.auto_sync_history is False
    hass.config.components.add("recorder")
    try:
        coord._maybe_schedule_survey_sync(datetime(2026, 11, 22, 12, 0, tzinfo=UTC))
        assert coord._survey_task is None

        # Enable via options -> now schedules when due; turning off cancels running task.
        hass.config_entries.async_update_entry(
            coord.config_entry, options={CONF_AUTO_SYNC_HISTORY: True}
        )
        await coord.async_apply_options()
        assert coord.auto_sync_history is True

        fake_task = MagicMock()
        fake_task.done.return_value = False
        coord._survey_task = fake_task

        hass.config_entries.async_update_entry(
            coord.config_entry, options={CONF_AUTO_SYNC_HISTORY: False}
        )
        await coord.async_apply_options()
        assert coord.auto_sync_history is False
        fake_task.cancel.assert_called_once()
    finally:
        hass.config.components.remove("recorder")
