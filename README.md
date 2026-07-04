# MeshcoreChatterbot

A small bot that connects to a MeshCore companion radio node over BLE,
logs private and channel messages it sees, and replies `pong` to any
private message containing `ping`. It also gives you a live prompt for
sending messages and commands yourself.

## Setup

1. Create a virtual environment and install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

2. Copy `config.example.json` to `config.json` (the latter is gitignored
   since it holds your specific device's address).

3. Make sure your MeshCore companion device is powered on and not already
   connected to another app (e.g. the MeshCore phone app) — BLE devices
   usually only accept one connection at a time.

4. Leave `address` in `config.json` as `null` to have the bot auto-scan for
   and connect to the first device advertising as `MeshCore...`. If you have
   more than one MeshCore device nearby, or scanning doesn't find yours, run:

   ```
   .venv\Scripts\python.exe scan_ble.py
   ```

   to list nearby BLE devices with their name and address, then set
   `address` in `config.json` to the MAC address of the right one (e.g.
   `"AA:BB:CC:DD:EE:FF"`).

5. If your device requires a BLE pairing PIN, set it in `config.json`'s
   `pin` field; otherwise leave it `null`.

## Running

Double-click `run_bot.bat`, or run manually:

```
.venv\Scripts\python.exe pingpong_bot.py
```

The console will log connection status and every message it sees or
sends. A `>` prompt at the bottom stays put while log output scrolls
above it, and accepts:

- `@Name message` — send a private message to a contact, e.g. `@Scoot-Wio hey`
- `@#channel message` — send to a channel, e.g. `@#test hey all` or `@Public hi`
- `message` (no `@`) — send to whichever target you last used
- `/advert` or `/advert flood` — broadcast a self-advertisement
- `/help` — list commands

Ctrl+C or Ctrl+D to stop.
