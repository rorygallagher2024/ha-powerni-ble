<div align="center">
  <img src="custom_components/powerni_ble/brand/icon.png" alt="Power NI Keypad+ icon" width="96" />
  <h1>Power NI Keypad+ for Home Assistant</h1>
  <p>Home Assistant integration for <strong>Power NI (PowerNI) Keypad+</strong> prepay electricity meters (Freedom Units) in Northern Ireland over Bluetooth.</p>

  <p>
    <a href="https://github.com/rorygallagher2024/ha-powerni-ble/releases"><img src="https://img.shields.io/github/v/release/rorygallagher2024/ha-powerni-ble?style=flat-square" alt="GitHub Release" /></a>
    <a href="https://www.home-assistant.io/"><img src="https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2?style=flat-square&logo=homeassistant&logoColor=white" alt="Home Assistant 2026.10+" /></a>
    <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=rorygallagher2024&repository=ha-powerni-ble&category=integration"><img src="https://img.shields.io/badge/Marketplace%20%2F%20HACS-Default-41BDF5?style=flat-square" alt="Marketplace / HACS Default" /></a>
    <a href="https://github.com/rorygallagher2024/ha-powerni-ble/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/rorygallagher2024/ha-powerni-ble/ci.yml?branch=main&label=CI&style=flat-square" alt="CI status" /></a>
    <a href="https://github.com/rorygallagher2024/ha-powerni-ble/actions/workflows/validate.yml"><img src="https://img.shields.io/github/actions/workflow/status/rorygallagher2024/ha-powerni-ble/validate.yml?branch=main&label=HACS%20%26%20Hassfest&style=flat-square" alt="Validate status" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/github/license/rorygallagher2024/ha-powerni-ble?style=flat-square" alt="License: MIT" /></a>
  </p>

  <p>
    <a href="#compatibility">Compatibility</a> ·
    <a href="#prerequisites-one-time-6-digit-pin-pairing">Pairing</a> ·
    <a href="#installation">Installation</a> ·
    <a href="#setup--configuration">Configuration</a> ·
    <a href="#exposed-entities">Entities</a> ·
    <a href="#energy-dashboard">Energy Dashboard</a> ·
    <a href="#automations">Automations</a> ·
    <a href="#troubleshooting">Troubleshooting</a>
  </p>
</div>

> [!NOTE]
> **Unofficial / Independent Project:** This integration is not affiliated with, endorsed by, or supported by Power NI, Energia Group, or Eckoh. It communicates **locally and read-only** over Bluetooth Low Energy (BLE) with your own Freedom Unit via a local Linux Bluetooth adapter or an ESPHome Bluetooth Proxy. It never contacts Power NI cloud servers and does not support top-ups or keypad modifications.

## Highlights

- **100% local & read-only:** Reads directly from your keypad over Bluetooth. No cloud account or login needed.
- **Credit and usage at a glance:** See your remaining balance, estimated days left, live electricity draw, and daily, weekly, and monthly spend right in Home Assistant.
- **Low-credit & top-up notifications:** Automatically spots top-ups and gives you everything you need for low-balance alerts.
- **Plays nicely with the official phone app:** Automatically pauses while you're using the Power NI app and catches up as soon as you close it.
- **Optional Energy Dashboard history:** Can pull up to 14 days of hourly electricity usage straight from your meter.

<p align="center">
  <img src="docs/images/dashboard.png" alt="Example Home Assistant dashboard for Power NI Keypad+" width="820" />
  <br />
  <em>Example Home Assistant dashboard showing 7-day credit balance, live load, 14-day meter energy history, and Bluetooth diagnostics.</em>
</p>

## Compatibility

- **Home Assistant:** Requires **`2026.10.0`** or newer (tested live on **`2026.11.1`**).

### Freedom Unit Hardware

| Unit Serial | Status | Bluetooth Reset Code | Notes |
| --- | --- | --- | --- |
| `B#######` (e.g. `B21xxxxx`) | Verified working | `*159268#` | Tested live on real hardware |
| `PNI#####` (e.g. `PNI12345`) | Supported (untested) | `*0#` | Community reports welcome via Issues |
| Other `AAA#####` Freedom Units | Supported (untested) | `*0#` or `*159268#` | Matched automatically by config flow |

### Bluetooth Transports

| Transport | Status | One-Time Bonding Method | Notes |
| --- | --- | --- | --- |
| Local Linux Bluetooth (`bluetoothctl` / BlueZ) | Supported | Pair directly on HA host with `DisplayOnly` agent | Bond persists in host BlueZ storage |
| ESPHome Bluetooth Proxy (`esp-idf`) | Verified working | Drop-in package [examples/esphome-pair-package.yaml](examples/esphome-pair-package.yaml) (adds a **Pair Power NI Meter** button in HA) | Bond persists in ESP32 NVS flash across OTA updates; **Automatic** remembers the bonded proxy (or pin manually in **Configure**) |

## Prerequisites: One-Time 6-Digit PIN Pairing

Power NI Freedom Units require an authenticated 6-digit PIN BLE bond before accepting commands. Pair the unit **once** using whichever Bluetooth transport you have:

<details>
<summary><strong>Option A: Local Linux Bluetooth (<code>bluetoothctl</code> on Home Assistant host)</strong></summary>

If your Freedom Unit is within range of your Home Assistant server's built-in Bluetooth adapter:

1. Run `bluetoothctl` on the Home Assistant host:
   ```text
   power on
   agent off
   agent DisplayOnly
   default-agent
   scan on
   ```
2. Note your Freedom Unit's MAC address (`AA:BB:CC:DD:EE:FF`, device name like `B1234567`), then run:
   ```text
   scan off
   pair AA:BB:CC:DD:EE:FF
   ```
3. When `bluetoothctl` prints a 6-digit passkey, type that 6-digit PIN followed by `#` on your physical Freedom Unit keypad.
4. Once `Pairing successful` appears, run `trust AA:BB:CC:DD:EE:FF` and `disconnect AA:BB:CC:DD:EE:FF`.

</details>

<details>
<summary><strong>Option B: ESPHome Bluetooth Proxy (e.g., ESP32 / Everything Presence Lite)</strong></summary>

Stock ESPHome `bluetooth_proxy` uses `io_capability: none` and cannot display a 6-digit PIN, but ESP-IDF stores BLE bonds persistently in NVS flash (`CONFIG_BT_BLE_SMP_BOND_NVS_FLASH=y`) across OTA updates. Using the drop-in package keeps your existing board, Wi-Fi, and sensors intact:

1. Add these lines to your existing ESPHome Bluetooth Proxy YAML (using your Freedom Unit's MAC address and any 6-digit PIN you choose):
   ```yaml
   substitutions:
     powerni_mac: "AA:BB:CC:DD:EE:FF"
     powerni_pin: "123456"

   packages:
     powerni_pair:
       url: https://github.com/rorygallagher2024/ha-powerni-ble
       ref: main
       files: [examples/esphome-pair-package.yaml]
   ```
2. Install (OTA) onto the ESP32 proxy.
3. In Home Assistant, open that ESPHome device and press **Pair Power NI Meter**.
4. When **Power NI Pairing Status** shows `Enter 123456# on keypad`, type that 6-digit PIN followed by `#` on your physical Freedom Unit keypad.
5. Wait ~35 seconds until status reports **`Bonded! Safe to remove pairing package`**, then remove the `substitutions:` / `packages:` lines and install again — the BLE bond remains saved in NVS flash.
6. **Automatic** proxy selection remembers and reuses that bonded proxy after the first successful poll, or you can open **Configure** on **Power NI Keypad+** and pin **Bluetooth proxy / adapter** explicitly.

*(Setting up a blank ESP32 outside an existing config? A standalone minimal firmware is also available in [examples/esphome-pair-helper.yaml](examples/esphome-pair-helper.yaml).)*

</details>

> [!TIP]
> If your Freedom Unit ignores pairing or stops advertising, reset its internal Bluetooth radio from the keypad by entering **`*159268#`** (`B#######` units) or **`*0#`** (`PNI#####` units).

## Installation

Requires **Home Assistant `2026.10.0` or newer**.

### Home Assistant Marketplace / HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rorygallagher2024&repository=ha-powerni-ble&category=integration)

Or install directly from the UI:

1. Open **Settings → Marketplace** (Home Assistant `2026.11+`), or **HACS → Integrations** on Home Assistant `2026.10`.
2. Search for **`Power NI Keypad+`** (or **`powerni`**).
3. Click **Download** / **Install** and restart Home Assistant.

### Manual Installation

1. Copy `custom_components/powerni_ble` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup & Configuration

1. Open **Settings → Devices & services**:
   - Auto-discovered meters appear under **Discovered** — click **Add** to confirm.
   - Or click **+ Add integration**, search for **Power NI Keypad+**, and pick your meter (or choose **Enter MAC address manually**).
2. Click **Configure** on the integration entry at any time to adjust:
   - **Poll interval (minutes):** `1` to `1440` minutes (default `15`).
   - **Bluetooth proxy / adapter:** Leave **Automatic** to remember and reuse the last working bonded proxy automatically (falling back if offline), or pin a specific bonded proxy when multiple proxies are in range.
   - **Automatically sync 14-day energy history (every 6 hours):** Disable if you only want manual **Sync energy history** imports (useful when dedicated CT clamps or solar inverters already feed your Energy dashboard).

<p align="center">
  <img src="docs/images/configure-dialog.png" alt="Power NI Keypad+ options dialog" width="400" />
  <br />
  <em>Integration options dialog for adjusting the poll interval and pinning the bonded Bluetooth proxy.</em>
</p>

## Exposed Entities

### Sensors

| Entity | Description | Unit | Device / State Class |
| --- | --- | --- | --- |
| **Balance** | Remaining Keypad+ credit balance (hourly Long-Term Statistics) | `GBP` | `monetary` / `total` |
| **Days left** | Estimated days remaining (`status` attribute e.g. `ABOUT 51`) | `d` | `measurement` |
| **Current load** | Instantaneous power draw | `kW` | `power` / `measurement` |
| **Current cost rate** | Current hourly cost at active load | `GBP/h` | `measurement` |
| **Total import energy** | Cumulative import meter register (Energy Dashboard ready) | `kWh` | `energy` / `total_increasing` |
| **Yesterday's spend** | Previous day electricity spend (hourly Long-Term Statistics) | `GBP` | `monetary` / `total` |
| **Last 7 days spend** | Rolling 7-day spend (hourly Long-Term Statistics) | `GBP` | `monetary` / `total` |
| **Last month spend** | Previous month spend (hourly Long-Term Statistics) | `GBP` | `monetary` / `total` |
| **Last top-up** | Estimated last top-up amount (`balance_before` / `balance_after` attributes) | `GBP` | `monetary` |
| **Last top-up time** | Timestamp when the last top-up was detected | — | `timestamp` |

### Controls & Configuration

| Entity | Type | Description | Unit |
| --- | --- | --- | --- |
| **Refresh** | `button` (Control) | Triggers an immediate full BLE read across all sections | — |
| **Sync energy history** | `button` (Configuration) | Imports up to 14 days of 30-minute meter history (`0xA0`) | — |
| **Poll interval** | `number` (Configuration) | Adjustable polling interval synced with **Configure** (`1`–`1440` min) | `min` |

### Diagnostics

| Entity | Description | Unit | Type / Device Class |
| --- | --- | --- | --- |
| **Last successful read** | Timestamp of the most recent successful BLE poll | — | `timestamp` |
| **Poll status** | Live BLE poll state (`idle`, `connecting`, `reading`, `retrying`, `waiting`, `error`) | — | `enum` |
| **Signal strength** | Smoothed Bluetooth RSSI (rolling median) from the selected adapter or proxy | `dBm` | `signal_strength` / `measurement` |
| **Updating** | `on` while connecting, reading, or retrying a BLE poll | — | `binary_sensor` (`running`) |

<p align="center">
  <img src="docs/images/device-page.png" alt="Power NI Keypad+ device page in Home Assistant" width="520" />
  <br />
  <em>Home Assistant device page showing live telemetry, Refresh control, and configurable Poll interval.</em>
</p>

## Energy Dashboard

The integration provides two ways to feed Home Assistant's **Energy** dashboard:

- **`Total import energy` sensor** (`kWh`, integer precision): updates on live polls as whole kilowatt-hours tick up.
- **`Energy history` external statistic** (`powerni_ble:<mac>_energy_history`, `0.00625 kWh` counter resolution): backfills up to 14 days of completed hourly energy totals from the meter's 30-minute internal survey (`0xA0`) into an isolated external statistic without altering any existing Energy dashboard sensors or sources. Disable **Automatically sync 14-day energy history** in **Configure** if you prefer manual-only imports via the **Sync energy history** button.

1. Open **Settings → Dashboards → Energy**.
2. Choose the section matching your setup:
   - **Electricity grid → Add consumption:** Select **Total import energy** (or **`<meter> Energy history`** for 0.006 kWh hourly resolution) to track grid import alongside solar or battery systems, and optionally add a static or entity tariff price (`GBP/kWh`).
   - **Home consumption:** Select **Total import energy** (or **`<meter> Energy history`**) under **Home energy** and optionally **Current load** under **Home power** if monitoring whole-home electricity directly from the Freedom Unit, then click **Save**.
3. Wait 1–2 hours for Home Assistant's hourly Long-Term Statistics bars to update (historical `Energy history` bars appear right after the first survey sync).

<p align="center">
  <img src="docs/images/energy-dashboard.png" alt="Adding Total import energy to the Home Assistant Energy Dashboard" width="480" />
  <br />
  <em>Adding Total import energy in Home Assistant's Energy Dashboard settings.</em>
</p>

> [!NOTE]
> The **Total import energy** sensor reports the cumulative register in whole `kWh` integers (`0` decimal precision on the meter's LCD), so its hourly Energy Dashboard bars increment in `1 kWh` steps. For finer `0.00625 kWh` hourly resolution, add **`<meter> Energy history`** instead.

## Automations

### Dynamic & Adaptive Polling

Alongside **Configure**, the **Poll interval** `number` entity (`number.<meter>_poll_interval`, `EntityCategory.CONFIG`) can be changed directly on dashboards or in automations:

- Changes stay synced between **Configure** and **Poll interval**, persist across restarts, and reschedule polling immediately without reloading entities.
- Trigger a full on-demand read across all sections at any time using **Refresh** (or `homeassistant.update_entity`).

<details>
<summary>Example automation: poll every 2 minutes while home during the day, 30 minutes overnight</summary>

```yaml
alias: Power NI adaptive BLE poll rate
mode: single
triggers:
  - trigger: homeassistant
    event: start
  - trigger: numeric_state
    entity_id: zone.home
    above: 0
  - trigger: numeric_state
    entity_id: zone.home
    below: 1
  - trigger: time
    at:
      - "07:00:00"
      - "23:00:00"
actions:
  - action: number.set_value
    target:
      entity_id: number.b1234567_poll_interval
    data:
      value: >
        {% set daytime = 7 <= now().hour < 23 %}
        {% set someone_home = states('zone.home') | int(0) > 0 %}
        {{ 2 if (daytime and someone_home) else 30 }}
```

</details>

### Top-Up Detection

When a freshly read **Balance** rises by at least **`£1.00`** compared to the previous fresh reading (persisted across Home Assistant restarts):

- Updates **Last top-up** (with `balance_before` and `balance_after` attributes) and **Last top-up time**.
- Fires a `powerni_ble_top_up` event carrying `device_id`, `address`, `name`, `amount`, `balance_before`, and `balance_after`.

Because the Freedom Unit exposes only net credit balance, `amount` is an estimate slightly reduced by any electricity consumed between the two polls.

<details>
<summary>Example automation: send a phone notification when a top-up is detected</summary>

```yaml
alias: Power NI top-up notification
mode: single
triggers:
  - trigger: event
    event_type: powerni_ble_top_up
actions:
  - action: notify.notify
    data:
      title: Electricity top-up detected
      message: >
        Added ~£{{ '%.2f' | format(trigger.event.data.amount) }}.
        New balance: £{{ '%.2f' | format(trigger.event.data.balance_after) }}.
```

</details>

<details>
<summary>Example automation: alert when balance or days remaining drops low</summary>

```yaml
alias: Power NI low credit alert
mode: single
triggers:
  - trigger: numeric_state
    entity_id: sensor.b1234567_balance
    below: 10
  - trigger: numeric_state
    entity_id: sensor.b1234567_days_left
    below: 4
actions:
  - action: notify.notify
    data:
      title: Power NI credit running low
      message: >
        Remaining balance: £{{ '%.2f' | format(states('sensor.b1234567_balance') | float(0)) }}
        (~{{ states('sensor.b1234567_days_left') }} days left).
```

</details>

## How Polling Works

The Freedom Unit only allows one Bluetooth connection at a time. To keep BLE sessions short and resilient:

### Tiered Read Schedule

| Readings | Read frequency | Notes |
| --- | --- | --- |
| **Balance** & **Days left** | Every poll | Always read first |
| **Current load** & **Current cost rate** | Every poll | Live demand and hourly rate |
| **Total import energy** | Once per hour | Also read on startup and **Refresh** |
| **Yesterday / Last 7 days / Last month spend** | Every 6 hours & after midnight | Also read on startup and **Refresh** |
| **30-min energy survey (`0xA0`)** | Every 6 hours (background) | Backfills completed hourly `kWh` buckets into Long-Term Statistics |

### Live Feedback & Resilience

- **Broadcast-aware polling:** Listens to per-address BLE advertisements from the meter. If broadcasts stop (for example while the mobile app holds the meter's single connection), scheduled polls pause cleanly in **`Waiting for meter`** status without logging failures and catch up immediately once broadcasts resume (escalating to **`Error`** after 30 minutes of continuous silence while still auto-recovering once broadcasts return).
- **Live refresh feedback:** Pressing **Refresh** immediately transitions **Poll status** (`Connecting` → `Reading` → `Idle`) and flips **Updating** (`on` → `off`). Sections publish progressively during the session, so **Balance**, **Days left**, and **Current load** appear in ~6–10 seconds without waiting for spend history to finish.
- **Disconnect detection & in-poll retry:** If a BLE link drops mid-session, the client aborts waiting immediately without clearing a healthy GATT cache, waits 3 seconds for the meter radio to return to idle, and retries once within the same poll before falling back to shortened `1 / 2 / 5` minute retries.
- **Age-based availability:** Every meter reading sensor carries a `last_read` ISO timestamp attribute showing when that field was actually returned by the meter, and stays available across brief BLE interruptions until older than `max(24 hours, 3 × poll_interval)`.

## Troubleshooting

<details>
<summary><strong>Weak BLE signal (<code>RSSI</code>) or intermittent timeouts</strong></summary>

- Check the diagnostic **Signal strength** (`dBm`) sensor on the device page or the RSSI shown next to your adapter/proxy in **Configure**. Signal stronger than **`-80 dBm`** is recommended; weaker than `-88 dBm` can drop while negotiating GATT encryption.
- Move an ESPHome Bluetooth Proxy closer to the Freedom Unit (same room or hallway) and use the `esp-idf` framework.

</details>

<details>
<summary><strong>Freedom Unit stops responding to pairing or scans</strong></summary>

- Freedom Units occasionally stop accepting new bonds or wedge their BLE radio after failed pairing attempts.
- Toggle Bluetooth off and back on from the physical keypad:
  - **`B#######` units:** `*159268#`
  - **`PNI#####` units:** `*0#`

</details>

<details>
<summary><strong>Phone app contention (stuck on <code>Waiting for meter</code>)</strong></summary>

- The Microchip/ISSC BLE module supports only **one active BLE connection at a time**.
- While the official Power NI Keypad+ phone app is open and connected, the meter stops advertising and Home Assistant automatically pauses scheduled polls in **`Waiting for meter`** status without logging errors, then triggers an immediate catch-up poll once the phone app is closed.
- If advertisements remain absent for more than **30 minutes**, Home Assistant escalates to **`Error`** so prolonged dropouts (such as a stuck background app or offline proxy) are visible, while still auto-recovering the moment advertisements resume.
- **Android background sync tip:** The official Power NI Keypad+ Android app runs an hourly background sync job that can connect to your meter even when you haven't opened the app and hold the BLE connection open until your phone leaves range. To prevent this without unpairing your meter or breaking manual top-ups, open **Settings → Apps → Power NI Keypad+ → App battery usage** and turn off **Allow background usage** (or set it to **Restricted**), and swipe the app away from Recents after topping up.
- Always close the phone app before running the one-time PIN pairing. During normal polling, Home Assistant connects briefly and disconnects cleanly afterward.

</details>

<details>
<summary><strong><code>Insufficient authorization (8)</code> or <code>Pairing failed due to error: 102</code></strong></summary>

- Home Assistant automatically raises a **Repair** notification under **Settings → System → Repairs** naming the unbonded adapter or proxy if an authentication or pairing error occurs, and clears it once a poll succeeds.
- **`Insufficient authorization (8)`:** The adapter or proxy Home Assistant selected does not hold a valid 6-digit PIN bond. Complete the [One-Time 6-Digit PIN Pairing](#prerequisites-one-time-6-digit-pin-pairing) steps on that proxy and pin **Bluetooth proxy / adapter** in **Configure** if needed.
- **`error: 102` (`ESP_AUTH_SMP_CONN_TOUT`):** During ESPHome pairing, the 6-digit PIN was not entered as `NNNNNN#` on the Freedom Unit keypad before the ~30-second SMP timeout. Enter `*159268#` (or `*0#`) on the keypad and retry while watching logs.
- Use **Download diagnostics** on the integration entry (**⋮ → Download diagnostics**) for a redacted JSON snapshot of coordinator state, poll status, and visible Bluetooth proxies when filing an issue.

</details>

<details>
<summary><strong>Brand icon shows "icon not available"</strong></summary>

- Local integration brand icons (`custom_components/powerni_ble/brand/`) are served by Home Assistant 2026.x+. Restart Home Assistant after installing and hard-refresh your browser (`Cmd+Shift+R` / `Ctrl+Shift+R`).

</details>

## Support & Development

- **Reporting issues:** Download a redacted diagnostics bundle via **Settings → Devices & services → Power NI Keypad+ → `⋮` → Download diagnostics** (meter serials, Bluetooth MAC addresses, unique IDs, and proxy sources are automatically redacted) and attach it to a [GitHub Issue](https://github.com/rorygallagher2024/ha-powerni-ble/issues).
- **Local checks & tests (Python 3.14+):**
  ```bash
  pip install -r requirements-dev.txt
  ruff check custom_components tests
  mypy custom_components/powerni_ble
  pytest -v
  ```
