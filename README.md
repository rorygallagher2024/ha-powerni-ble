# Power NI Keypad+ BLE for Home Assistant

> **Unofficial / Independent Project:** This integration is not affiliated with, endorsed by, or supported by Power NI, Energia Group, or Eckoh. It communicates **locally and read-only** over Bluetooth Low Energy (BLE) with your own Freedom Unit — it never contacts Power NI cloud servers and does not support top-ups or keypad modifications.

Read live electricity telemetry from Power NI **Keypad+ Freedom Units** (`B#######` / `AAA#####` serials) into Home Assistant via the Microchip/ISSC Transparent UART GATT service (`49535343-fe7d-4ae5-8fa9-9fafd205e455`), using either a local Linux Bluetooth adapter or an ESPHome Bluetooth Proxy.

## Exposed Entities

| Entity | Description | Unit | Device / State Class |
| --- | --- | --- | --- |
| **Balance** | Remaining Keypad+ credit balance | `GBP` | `monetary` |
| **Days left** | Estimated days remaining (`status` attribute e.g. `ABOUT 51`) | `d` | `measurement` |
| **Current load** | Instantaneous power draw | `kW` | `power` / `measurement` |
| **Current cost rate** | Current hourly cost at active load | `GBP/h` | `measurement` |
| **Total import energy** | Cumulative import meter register (Energy Dashboard ready) | `kWh` | `energy` / `total_increasing` |
| **Yesterday's spend** | Previous day electricity spend | `GBP` | `monetary` |
| **Last 7 days spend** | Rolling 7-day spend | `GBP` | `monetary` |
| **Last month spend** | Previous month spend | `GBP` | `monetary` |
| **Refresh** button | Triggers an immediate on-demand BLE read | — | `button` |

## Prerequisites: One-Time 6-Digit PIN Pairing

Power NI Freedom Units require an authenticated BLE bond (`LE Legacy + MITM 6-digit PIN`) before accepting GATT commands. You must pair the unit **once** with the Bluetooth adapter or ESP32 proxy that Home Assistant will connect through.

### Option A: Local Linux Bluetooth (`bluetoothctl` on Home Assistant host)

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

### Option B: ESPHome Bluetooth Proxy (e.g., ESP32 / Everything Presence Lite)

Stock ESPHome `bluetooth_proxy` uses `io_capability: none` and cannot display a 6-digit PIN. However, ESP-IDF stores BLE bonds persistently in NVS flash (`CONFIG_BT_BLE_SMP_BOND_NVS_FLASH=y`), which survives OTA firmware updates:

1. Keep your stock ESPHome proxy firmware ready to re-flash afterward.
2. Temporarily OTA-flash the target ESP32 using [examples/esphome-pair-helper.yaml](examples/esphome-pair-helper.yaml) (updated with your Freedom Unit's MAC address and your chosen 6-digit PIN).
3. Watch the ESPHome logs until `=== ENTER PASSKEY ON FREEDOM UNIT: 123456# ===` appears, then type that 6-digit PIN followed by `#` on your Freedom Unit keypad.
4. Flash your normal ESPHome Bluetooth Proxy firmware back onto the ESP32 — the BLE bond remains saved in NVS flash.
5. In Home Assistant, open **Configure** on the **Power NI Keypad+ BLE** integration and pin **Bluetooth proxy / adapter** to that specific bonded proxy.

*(Tip: If your Freedom Unit stops responding to BLE pairing, entering `*159268#` on B-series units or `*0#` on PNI-series units toggles its internal Bluetooth radio off and back on.)*

## Installation

### HACS (Custom Repository)

1. In HACS -> **Integrations** -> menu (`⋮`) -> **Custom repositories**, add `https://github.com/rorygallagher2024/ha-powerni-ble` with category **Integration**.
2. Install **Power NI Keypad+ BLE** and restart Home Assistant.

### Manual Installation

1. Copy `custom_components/powerni_ble` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup & Configuration

1. Go to **Settings -> Devices & services**:
   - Auto-discovered meters appear under **Discovered**; click **Add** to confirm.
   - Or click **+ Add integration**, search for **Power NI Keypad+ BLE**, and pick your meter from the list (or choose **Enter MAC address manually**).
2. Click **Configure** on the integration entry at any time to change:
   - **Poll interval (minutes):** `1` to `1440` minutes (default `15`).
   - **Bluetooth proxy / adapter:** Leave **Automatic** or pin to the bonded ESPHome proxy / local adapter.
3. Each poll connects over BLE, reads the metrics, leaves transparent mode cleanly, and disconnects so your mobile app can still connect when needed. You can also press the **Refresh** button entity (or call `homeassistant.update_entity`) at any time.
