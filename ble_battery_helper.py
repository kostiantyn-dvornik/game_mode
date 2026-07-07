"""
Reads a Bluetooth LE device's real battery level via its GATT Battery
Service (0x180F), and prints it (0-100) to stdout, or nothing if not found.

Run as a subprocess by game_mode.py rather than imported directly: winrt's
async Bluetooth calls hang indefinitely if the process has already
initialized a COM apartment via comtypes (used elsewhere in game_mode.py
for audio device switching) — the two don't share an apartment model.

Usage: ble_battery_helper.py <bluetooth-device-name-substring>
"""
import sys
import asyncio

from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattDeviceService, GattServiceUuids, GattCharacteristicUuids, GattCommunicationStatus,
)
from winrt.windows.devices.enumeration import DeviceInformation
from winrt.windows.storage.streams import DataReader


async def read_battery_percent(name_hint: str) -> int | None:
    ble_devices = await DeviceInformation.find_all_async_aqs_filter(BluetoothLEDevice.get_device_selector())
    target_mac = None
    for d in ble_devices:
        if name_hint.lower() not in d.name.lower():
            continue
        dev = await BluetoothLEDevice.from_id_async(d.id)
        if dev is not None:
            target_mac = ':'.join(f'{b:02x}' for b in dev.bluetooth_address.to_bytes(6, 'big'))
            break
    if target_mac is None:
        return None

    batt_devices = await DeviceInformation.find_all_async_aqs_filter(
        GattDeviceService.get_device_selector_from_uuid(GattServiceUuids.battery))
    batt_id = next((d.id for d in batt_devices if target_mac in d.id.lower()), None)
    if batt_id is None:
        return None

    service = await GattDeviceService.from_id_async(batt_id)
    chars = await service.get_characteristics_for_uuid_async(GattCharacteristicUuids.battery_level)
    if chars.status != GattCommunicationStatus.SUCCESS or not chars.characteristics:
        return None
    read = await chars.characteristics[0].read_value_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
    if read.status != GattCommunicationStatus.SUCCESS:
        return None
    data = bytearray(read.value.length)
    DataReader.from_buffer(read.value).read_bytes(data)
    return data[0] if data else None


if __name__ == "__main__":
    name_hint = sys.argv[1] if len(sys.argv) > 1 else "Xbox"
    pct = asyncio.run(read_battery_percent(name_hint))
    if pct is not None:
        print(pct)
