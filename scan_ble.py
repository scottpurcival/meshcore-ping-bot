"""Lists nearby BLE devices and their addresses, to help pick the right
'address' value for config.json. Devices with no advertised name are
skipped since they can't be identified anyway.
"""
import asyncio

from bleak import BleakScanner


async def main():
    print("Scanning for 10 seconds...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)

    named = [(addr, adv) for addr, (_, adv) in devices.items() if adv.local_name]
    named.sort(key=lambda pair: pair[1].rssi, reverse=True)

    for address, adv in named:
        marker = " <-- MeshCore device" if "meshcore" in adv.local_name.lower() else ""
        print(f"{address}  rssi={adv.rssi:>4}  name={adv.local_name!r}{marker}")


if __name__ == "__main__":
    asyncio.run(main())
