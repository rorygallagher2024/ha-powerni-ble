"""Config and options flows for the Power NI Keypad+ BLE integration."""

from __future__ import annotations

import re
from typing import Any

import probatio as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_scanner_devices_by_address,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_AUTO_SYNC_HISTORY,
    CONF_PROXY_SOURCE,
    CONF_SCAN_INTERVAL,
    DEFAULT_AUTO_SYNC_HISTORY,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    METER_NAME_REGEX,
    MIN_SCAN_INTERVAL_MINUTES,
    SERVICE_UUID,
)

_MANUAL_CHOICE = "__manual__"
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _is_powerni_meter(info: BluetoothServiceInfoBleak) -> bool:
    """Return True if advertisement matches Power NI Freedom Unit name or service UUID."""
    name = (info.name or "").strip()
    if METER_NAME_REGEX.match(name):
        return True
    target = SERVICE_UUID.lower()
    return any(uuid.lower() == target for uuid in info.service_uuids)


class PowerNIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Power NI Keypad+ BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerNIOptionsFlow:
        """Return the options flow."""
        return PowerNIOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth auto-discovery."""
        if not _is_powerni_meter(discovery_info):
            return self.async_abort(reason="not_supported")

        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        name = discovery_info.name or f"Power NI {address}"
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm Bluetooth discovery."""
        assert self._discovery_info is not None
        address = self._discovery_info.address.upper()
        name = self._discovery_info.name or f"Power NI {address}"

        if user_input is not None:
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: address, CONF_NAME: name},
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": name, "address": address},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated setup."""
        if user_input is not None:
            selection = user_input[CONF_ADDRESS]
            if selection == _MANUAL_CHOICE:
                return await self.async_step_manual()
            address = selection.upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            label = self._discovered_devices.get(address, f"Power NI {address}")
            name = label.split(" (")[0]
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: address, CONF_NAME: name},
            )

        current_ids = self._async_current_ids()
        self._discovered_devices = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            address = info.address.upper()
            if (
                address in current_ids
                or address in self._discovered_devices
                or not _is_powerni_meter(info)
            ):
                continue
            name = info.name or "Power NI Meter"
            self._discovered_devices[address] = f"{name} ({address})"

        if not self._discovered_devices:
            return await self.async_step_manual()

        choices = {
            **self._discovered_devices,
            _MANUAL_CHOICE: "Enter MAC address manually",
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(choices)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual Bluetooth MAC entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_mac = user_input[CONF_ADDRESS].strip()
            formatted = format_mac(raw_mac)
            if not _MAC_RE.match(formatted):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                address = formatted.upper()
                name = (user_input.get(CONF_NAME) or f"Power NI {address}").strip()
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address, CONF_NAME: name},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME, default="Power NI Meter"): str,
                }
            ),
            errors=errors,
        )


class PowerNIOptionsFlow(OptionsFlow):
    """Handle options flow for Power NI Keypad+ BLE."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage poll interval and optional proxy pinning."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_PROXY_SOURCE: user_input.get(CONF_PROXY_SOURCE, ""),
                    CONF_AUTO_SYNC_HISTORY: bool(
                        user_input.get(CONF_AUTO_SYNC_HISTORY, DEFAULT_AUTO_SYNC_HISTORY)
                    ),
                },
            )

        address = (
            self.config_entry.data.get(CONF_ADDRESS)
            or self.config_entry.unique_id
            or ""
        ).upper()
        current_interval = int(
            self.config_entry.options.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
            )
        )
        current_proxy = self.config_entry.options.get(CONF_PROXY_SOURCE, "")
        current_auto_sync = bool(
            self.config_entry.options.get(
                CONF_AUTO_SYNC_HISTORY, DEFAULT_AUTO_SYNC_HISTORY
            )
        )

        scanner_choices: dict[str, str] = {
            "": "Automatic (strongest proxy / adapter)"
        }
        if address:
            for sd in async_scanner_devices_by_address(
                self.hass, address, connectable=True
            ):
                source = sd.scanner.source
                label = sd.scanner.name
                if source.upper() in label.upper():
                    scanner_choices[source] = f"{label} ({sd.advertisement.rssi} dBm)"
                else:
                    scanner_choices[source] = (
                        f"{label} ({source}, {sd.advertisement.rssi} dBm)"
                    )
        if current_proxy and current_proxy not in scanner_choices:
            scanner_choices[current_proxy] = f"{current_proxy} (configured)"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL_MINUTES,
                            max=MAX_SCAN_INTERVAL_MINUTES,
                        ),
                    ),
                    vol.Optional(CONF_PROXY_SOURCE, default=current_proxy): vol.In(
                        scanner_choices
                    ),
                    vol.Optional(
                        CONF_AUTO_SYNC_HISTORY, default=current_auto_sync
                    ): bool,
                }
            ),
        )
