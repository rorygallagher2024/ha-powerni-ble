# Technical Reference

Low-level Bluetooth protocol details, Home Assistant entity metadata, polling internals, and local development commands for **Power NI Keypad+** (`powerni_ble`).

## Bluetooth & Protocol Details

- **Hardware interface:** Microchip/ISSC Transparent UART BLE module (single concurrent BLE connection).
- **GATT service UUID:** `49535343-fe7d-4ae5-8fa9-9fafd205e455`
  - Notify characteristic: `49535343-1e4d-4bd9-ba61-23c647249616`
  - Write characteristic: `49535343-8841-43f4-a8d4-ecbe34729bb3`
- **Discovery matching:** Matches advertisements carrying the ISSC service UUID (`49535343-fe7d-4ae5-8fa9-9fafd205e455`) or advertised local names matching `^((?:[A-Z]{3}\d{5})|(?:B\d{7}))$`.
- **Authentication:** Requires `LE Legacy + MITM` 6-digit passkey bonding (`ESP_LE_AUTH_REQ_BOND_MITM` / `DisplayOnly` I/O capability).
- **Transparent mode & LCD queries:**
  - Enters transparent mode (`55 BB 01 44` → ACK `55 CC 01 33`), queries balance, live load, cumulative import/export kWh (`0xC2 0x39` cycles `IMP UNITS` → `TEST IMP` → `EXP UNITS`; export is only read when **Total export energy** is enabled), active tariff unit rate (`0xC2 0x33` → `R1 PRICE` in pence converted to `GBP/kWh`), and spend buckets (`0xC2 0x32`), sends keepalive (`55 BB 03 42`) before slow sections, and exits cleanly (`55 BB 02 43` → ACK `55 CC 02 32`) in a `finally` block before calling `stop_notify` and `disconnect`.
- **Energy history (`0xA0` survey):**
  - Reads up to 14 days of 13-block 30-minute counter data (`0xA0` / `0xF1`, `0.00625 kWh` per count).
  - Completed hourly totals are written to an isolated recorder external statistic (`powerni_ble:<mac>_energy_history`, `unit_of_measurement="kWh"`, `has_sum=True`) via `async_add_external_statistics`.

## ESPHome Bluetooth Proxy Pairing Internals

Stock ESPHome `bluetooth_proxy` compiles with `io_capability: none`, so it cannot display a 6-digit passkey during initial SMP pairing. However, ESP-IDF persists BLE bonds in NVS flash (`CONFIG_BT_BLE_SMP_BOND_NVS_FLASH=y`) across OTA firmware updates:

- `examples/esphome-pair-package.yaml` sets `esp32_ble: io_capability: display_only` and uses a one-shot `ble_client` (`auto_connect: false`) triggered by a template button to request `ESP_BLE_SEC_ENCRYPT_MITM` with a static 6-digit passkey, verifies the target BDA via `esp_ble_get_bond_device_list()`, and disconnects after 35s.
- Once bonded, removing the package and flashing normal `bluetooth_proxy` firmware leaves the LTK intact in NVS flash.
- `error: 102` corresponds to `ESP_AUTH_SMP_CONN_TOUT` (the ~30-second SMP passkey timeout expired before `NNNNNN#` was entered on the keypad).

## Polling & Resilience Internals

- **Broadcast-aware polling:** Uses `bluetooth.async_register_advertisement_callback` (requires Home Assistant `2026.10.0+`) for raw per-address advertisement callbacks.
  - Silence between `90s` (`ADVERTISEMENT_SILENT_SECONDS`) and `30m` (`ADVERTISEMENT_WAITING_TIMEOUT_SECONDS`) defers scheduled polls into `waiting` status and bounds `update_interval` to the remaining wait window.
  - Silence past `30m` escalates to a real probe / `error` status with `1 / 2 / 5` minute retry backoff while still auto-recovering immediately when advertisements resume after a gap (`FAILURE_ADV_RECOVERY_COOLDOWN_SECONDS = 15s`).
- **Mid-session disconnect handling:** Aborts waiting immediately on `disconnected_callback`, preserves healthy GATT caches (only clearing cache on `BleakCharacteristicNotFoundError` while connected), waits `3s`, and retries once inside the same poll.
- **RSSI smoothing:** Rolling median over the last 15 connectable advertisements, throttled to publish at most once per `60s` or when median changes by `>= 3 dBm`.
- **Per-field staleness:** Each reading sensor carries a `last_read` ISO timestamp attribute and stays available until older than `max(24 hours, 3 × poll_interval)`.
- **Auto-sticky proxy selection:** When `proxy_source` is empty (`Automatic`), the last working proxy source is persisted in HA storage (`powerni_ble.<entry_id>`) and preferred on future polls, falling back if offline or clearing on auth errors.
- **Entity-driven section skipping:** Before polling, the coordinator checks `entity_registry` via `SECTION_ENTITY_KEYS` and `DISABLED_BY_DEFAULT_ENTITY_KEYS`; if every entity in a section is disabled (including disabled-by-default sensors before registration), `sections_due()` omits that section even on `Refresh`.

## Entity Metadata

| Entity | Platform | Category | Unit | Device / State Class |
| --- | --- | --- | --- | --- |
| **Balance** | `sensor` | Primary | `GBP` | `monetary` / `total` |
| **Days left** | `sensor` | Primary | `d` | — / `measurement` |
| **Current load** | `sensor` | Primary | `kW` | `power` / `measurement` |
| **Current cost rate** | `sensor` | Primary | `GBP/h` | — / `measurement` |
| **Total import energy** | `sensor` | Primary | `kWh` | `energy` / `total_increasing` |
| **Total export energy** | `sensor` | Primary *(disabled by default)* | `kWh` | `energy` / `total_increasing` |
| **Unit rate** | `sensor` | Primary | `GBP/kWh` | — / `measurement` |
| **Yesterday's spend** | `sensor` | Primary | `GBP` | `monetary` / `total` |
| **Last 7 days spend** | `sensor` | Primary | `GBP` | `monetary` / `total` |
| **Last month spend** | `sensor` | Primary | `GBP` | `monetary` / `total` |
| **Last top-up** | `sensor` | Primary | `GBP` | `monetary` / — |
| **Last top-up time** | `sensor` | Primary | — | `timestamp` / — |
| **Refresh** | `button` | Primary | — | — |
| **Sync energy history** | `button` | `CONFIG` | — | — |
| **Poll interval** | `number` | `CONFIG` | `min` | `duration` |
| **Last successful read** | `sensor` | `DIAGNOSTIC` | — | `timestamp` |
| **Poll status** | `sensor` | `DIAGNOSTIC` | — | `enum` |
| **Signal strength** | `sensor` | `DIAGNOSTIC` | `dBm` | `signal_strength` / `measurement` |
| **Updating** | `binary_sensor` | `DIAGNOSTIC` | — | `running` |

## Local Development & Tests

Requires Python `3.14+`:

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests
mypy custom_components/powerni_ble
python -m py_compile custom_components/powerni_ble/*.py tests/*.py
pytest -v
```
